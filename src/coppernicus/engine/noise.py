"""DC-DC ripple and coupled noise of one switching regulator (an aggressor) with KiPI's noise
solver (``kipi/noise``), converted to :mod:`coppernicus.results`.

Method (KiPI kipi/noise/solver.py:1-8, waveforms.py:1-22, extract/common.py:1-25,
victims.py:1-7): the converter's rails and ground are meshed and each phase's hot loop is
traced through the copper (its L, the switch node's C = Coss + copper, the ringing); the
waveforms are exact Fourier series of piecewise-linear periods; AC mode's field model of the
copper, terminated by the capacitors, splits the pulsed currents and gives the ripple at the
device pins and at probes; the near field and a radiated estimate follow; each victim is cut
into 2D cross-sections, coupled capacitively (switch nodes), inductively (current loops) and
through the shared ground, added as phasors. Field by field: ``docs/engine-mapping.md``
(Task 10).

Setup (brief Q7): T9's ``ac_setup`` maps one rail; an aggressor spans two (its input and its
output) and names its probes. So this module builds the setup with T9's per-object mappers
(``_kipi_probe`` for the probes the aggressor names, ``_kipi_caps`` over KiPI's own net sets
of both rails), never with ``ac_setup`` per rail, which would refuse an input rail without a
source of its own in the project (``vin_v`` entered) and resolve probes by rail.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from kipi.ac.checks import ground_nets, rail_caps, rail_nets
from kipi.ac.model import ACSettings
from kipi.ac.solver.fem import ACError, mesh_rail_and_ground
from kipi.core.mesh.mesher import MeshError
from kipi.core.progress import Canceled
from kipi.dc.pi.model import PinRef, PISetup, net_groups
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C
from kipi.noise.checks import NoiseError, find_vrm, input_voltage, operating_point, validate_noise
from kipi.noise.checks import guess_ends as _kipi_guess_ends
from kipi.noise.checks import seed_phases as _kipi_seed_phases
from kipi.noise.checks import topology as _kipi_topology
from kipi.noise.extract.capacitive import MASK_ER
from kipi.noise.extract.common import aggressor_ground, board_ports, ground_pads
from kipi.noise.model import (
    CUSTOM,
    DEFAULT_COSS_F,
    DEFAULT_QRR_C,
    DEFAULT_RDS_ON_OHM,
    INDUCTOR_AXES,
    LOGIC_LEVELS,
    TOPOLOGIES,
    VICTIM_PRESETS,
    AggressorPhase,
    NoiseSettings,
)
from kipi.noise.model import Aggressor as KipiAggressor
from kipi.noise.model import Victim as KipiVictim
from kipi.noise.parasitics import FALLBACK_C_F, FALLBACK_ESL_H, FALLBACK_ESR_OHM
from kipi.noise.solver import solve_noise
from kipi.noise.victims import ends as _kipi_ends
from kipi.noise.waveforms import bandwidth_limited, harmonic_count
from kipi.tolerance.gather import (
    BW_LIMIT_HZ,
    ripple_peaks,  # noqa: F401  (re-exported for the tests)
)

from coppernicus.engine.ac import (
    SWEEP_KEYS,
    CostEstimate,
    RamEstimate,
    _dielectric,
    _kipi_caps,
    _kipi_probe,
    cost_estimate,
    peak_rss_bytes,
)
from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import _numbers, _pads, terminal_names, to_pisetup
from coppernicus.engine.solve import Cancelled, _Progress, engine_settings, thickness_note
from coppernicus.impedance import hz_text
from coppernicus.project import CUSTOM_PRESET, Aggressor, NoiseOptions, Phase, Probe, Project, Victim
from coppernicus.results import (
    FieldMap,
    HotLoop,
    NearField,
    NoiseResult,
    RippleResult,
    VictimResult,
)

# NoiseOptions fields that are KiPI's NoiseSettings fields of the same name.
OPTION_KEYS = ("f_max_hz", "max_harmonics", "dead_time_ns", "segment_mm", "window_mm",
               "board_model", "field_per_decade")
MISSING_LISTED = 20                 # capacitors named in a stand-in reason
MECHANISMS = ("capacitive", "inductive", "common")


def kipi_noise_choices() -> Dict[str, Any]:
    """KiPI's choices and defaults that :mod:`coppernicus.project` copies (it cannot import
    KiPI): victim presets, topologies, inductor axes, the noise options' defaults, the
    aggressor's damping floors and a phase's inductor defaults."""
    s, a, ph = NoiseSettings(), KipiAggressor("x"), AggressorPhase()
    return {"presets": tuple(VICTIM_PRESETS), "custom": CUSTOM, "topologies": tuple(TOPOLOGIES),
            "axes": tuple(k for k, _label in INDUCTOR_AXES),
            "options": {k: getattr(s, k) for k in OPTION_KEYS},
            "zetas": (a.min_ring_zeta, a.soft_edge_zeta),
            "inductor": (ph.inductor_axis, ph.inductor_shielded)}


# ---------------------------------------------------------------------- progress

_BOARD_STAGE = re.compile(r"Board model: (\d+) field solves, (\d+) ports, (\d+) unknowns")
_MESH = re.compile(r"AC mesh: (\d+) nodes, (\d+) triangles")
_FIELD_DONE = re.compile(r"Field solves done in ([0-9.]+) s")


class _NoiseProgress(_Progress):
    """The engine's messages, its fraction done (victims) and its partial results, each
    converted before it is handed on; keeps the sizes KiPI reports for the stats."""

    def __init__(self, callback, cancelled, fraction: Optional[Callable[[Optional[float]], None]],
                 partial: Optional[Callable[[NoiseResult], None]] = None,
                 convert: Optional[Callable[[Any], NoiseResult]] = None):
        super().__init__(callback, cancelled)
        self._fraction, self._partial, self._convert = fraction, partial, convert
        self.sizes: Dict[str, float] = {}

    def message(self, text, level=20):
        for pattern, keys in ((_BOARD_STAGE, ("field_solves", "ports", "unknowns")),
                              (_MESH, ("nodes", "triangles")), (_FIELD_DONE, ("field_seconds",))):
            m = pattern.search(text)
            if m:
                self.sizes.update({k: float(x) for k, x in zip(keys, m.groups())})
        super().message(text, level)

    def fraction(self, value):
        if self._fraction is not None:
            self._fraction(value)
        if self._cancelled():
            self.cancel()

    def partial(self, result):
        if self._partial is not None and self._convert is not None:
            self._partial(self._convert(result))
        if self._cancelled():
            self.cancel()


# ---------------------------------------------------------------------- setup

@dataclass
class _Context:
    setup: PISetup
    agg: Aggressor
    vrm: str
    vin_net: str
    vout_net: str
    gnd: str
    rail: frozenset                # both rails' nets (KiPI's board model)
    grounds: frozenset
    in_rail: frozenset
    victims: List[Victim]
    probes: List[Probe]
    notes: List[str] = field(default_factory=list)
    fsw_note: str = ""             # where fsw_hz came from, when not the aggressor's own


def _aggressor(project: Project, name: str) -> Aggressor:
    a = next((x for x in project.aggressors if x.name == name), None)
    if a is None:
        known = ", ".join(x.name for x in project.aggressors) or "none"
        raise EngineError(f"No aggressor named {name} (the project's: {known})")
    return a


def _pin(board: Board, ref: str, pins, net: str, owner: str) -> Optional[PinRef]:
    if not net:
        return None
    if not ref:
        raise EngineError(f"{owner}: set its part (ref)")
    return PinRef(ref, net, _numbers(_pads(board, [net], ref, pins, owner), pins))


def _kipi_phase(board: Board, ph: Phase, owner: str) -> AggressorPhase:
    return AggressorPhase(
        inductor=ph.inductor, inductance_h=float(ph.inductance_h), dcr_ohm=float(ph.dcr_ohm),
        switch_net=ph.switch_net,
        high_side=_pin(board, ph.high_side_ref, ph.high_side_pins, ph.high_side_net,
                       f"{owner} (high-side pins)"),
        low_side=_pin(board, ph.low_side_ref, ph.low_side_pins, ph.low_side_net,
                      f"{owner} (low-side pins)"),
        input_caps=list(ph.input_caps), output_caps=list(ph.output_caps), coss_f=ph.coss_f,
        qrr_c=None if ph.qrr_nc is None else ph.qrr_nc * 1e-9, rds_on_hs_ohm=ph.rds_on_hs_ohm,
        rds_on_ls_ohm=ph.rds_on_ls_ohm, package_l_h=float(ph.package_l_h),
        inductor_axis=ph.inductor_axis, inductor_shielded=ph.inductor_shielded)


def _kipi_victim(board: Board, v: Victim) -> KipiVictim:
    owner = f"Victim {v.label}"
    if v.net not in board.nets():
        raise EngineError(f"{owner}: net {v.net} is not on the board")
    if v.preset not in (*VICTIM_PRESETS, CUSTOM_PRESET):
        raise EngineError(f"{owner}: choose its type (preset)")
    ends = [_pin(board, getattr(v, f"{w}_ref"), getattr(v, f"{w}_pins"),
                 v.net if getattr(v, f"{w}_ref") else "", f"{owner} ({w} pins)")
            for w in ("driver", "receiver")]
    kv = KipiVictim(v.net, name=v.label, driver=ends[0], receiver=ends[1],
                    # KiPI's 3.3 V when logic_v is empty: it only builds the states then
                    # (neither the noise nor a margin from noise_margin_v or a preset uses it).
                    logic_v=float(v.logic_v) if v.logic_v is not None else KipiVictim("x").logic_v,
                    noise_margin_v=v.noise_margin_v)
    if v.preset in VICTIM_PRESETS:
        kv.apply_preset(v.preset)          # Victim(preset=...) alone keeps cmos terminations
        return kv
    missing = [k for k in ("driver_ohm", "receiver_ohm", "receiver_pf") if getattr(v, k) is None]
    if missing:
        raise EngineError(f"{owner}: a custom victim needs {', '.join(missing)}")
    kv.preset = CUSTOM
    kv.driver_ohm, kv.receiver_ohm = float(v.driver_ohm), float(v.receiver_ohm)
    kv.receiver_pf, kv.termination_ohm = float(v.receiver_pf), v.termination_ohm
    kv.driver_low_ohm = v.driver_low_ohm
    return kv


def _source_fsw(project: Project, a: Aggressor, fields_rails: Sequence[str]) -> Tuple[Optional[float], str]:
    """The switching frequency of the aggressor's regulator (``Source.fsw_hz``, from the
    KiCad field PI_FSW or the project file) and the assumption line naming where it came
    from; (None, "") when the source has none."""
    names = terminal_names(project)
    for r in project.rails:
        for i, s in enumerate(r.sources):
            if names.get(("source", r.net, i)) == a.vrm and s.fsw_hz is not None:
                where = "KiCad field PI_FSW" if r.net in fields_rails else "the project file"
                return float(s.fsw_hz), (f"Switching frequency {hz_text(s.fsw_hz)} from the source "
                                         f"{s.label} ({where}): the aggressor gives no fsw_hz of "
                                         "its own, which would win.")
    return None, ""


def _context(board: Board, project: Project, name: str, fields_rails: Sequence[str] = ()) -> _Context:
    a = _aggressor(project, name)
    fsw, fsw_note = (a.fsw_hz, "") if a.fsw_hz is not None else _source_fsw(project, a, fields_rails)
    missing = [k for k, x in (("fsw_hz", fsw), ("rise_s", a.rise_s), ("fall_s", a.fall_s)) if x is None]
    if missing:
        raise EngineError(f"Aggressor {name}: enter its {', '.join(missing)} before a noise solve "
                          "(they set the spectrum; KiPI's placeholders are not used)")
    if not a.phases:
        raise EngineError(f"Aggressor {name}: add its phases (Fill from board) before a noise solve")
    setup = to_pisetup(board, project)
    vrms = {v.name: v for v in setup.vrms}
    if a.vrm not in vrms:
        switchers = ", ".join(v.name for v in setup.vrms if v.kind == "smps") or "none"
        raise EngineError(f"Aggressor {name}: its source {a.vrm or '(not set)'} is not a source of "
                          f"the project (switchers: {switchers})")
    phases = [_kipi_phase(board, ph, f"Aggressor {name}, phase {i}") for i, ph in enumerate(a.phases, 1)]
    by_name = {p.name: p for p in project.probes}
    unknown = [n for n in a.ripple_probes if n not in by_name]
    if unknown:
        raise EngineError(f"Aggressor {name}: no probe named {', '.join(unknown)} (ripple_probes)")
    probes = [by_name[n] for n in a.ripple_probes]
    kagg = KipiAggressor(a.name, a.vrm, a.topology, a.vin_v, float(fsw), float(a.rise_s),
                         float(a.fall_s), a.duty, a.load_a, phases, list(a.ripple_probes),
                         float(a.min_ring_zeta), float(a.soft_edge_zeta))
    setup.aggressors = [kagg]
    vrm = vrms[a.vrm]
    gnd = vrm.ground.net if vrm.ground else next((p.low_side.net for p in phases if p.low_side), "")
    vin_net = vrm.input.net if vrm.input else ""
    in_rail = (net_groups(setup, [vin_net]).get(vin_net, frozenset([vin_net]))
               if vin_net else frozenset())
    grounds = ground_nets(setup, vrm, gnd)
    rail = frozenset(rail_nets(setup, vrm, gnd) | in_rail) - grounds
    notes: List[str] = []
    setup.probes = [_kipi_probe(board, p) for p in probes]
    setup.caps = _kipi_caps(board, project.capacitors, set(rail), set(grounds), notes)
    esl = project.ac.link_esl_h
    names = terminal_names(project)
    for i, k in enumerate(project.links):
        value = esl.get(k.ref, esl.get(k.name))
        if value is not None:
            next(x for x in setup.links if x.name == names[("link", "", i)]).esl_h = float(value)
    s = ACSettings()
    for key in SWEEP_KEYS:
        setattr(s, key, getattr(project.ac, key))
    if project.settings.copper_temp_c != REF_TEMP_C:
        s.temperature, s.temperature_c = "fixed", float(project.settings.copper_temp_c)
    setup.ac = s
    o = project.noise
    setup.noise = NoiseSettings(aggressor=a.name, **{k: getattr(o, k) for k in OPTION_KEYS})
    setup.victims = [_kipi_victim(board, v) for v in project.victims]
    return _Context(setup, a, a.vrm, vin_net, vrm.output.net, gnd, rail, grounds, in_rail,
                    list(project.victims), probes, notes, fsw_note)


def noise_setup(board: Board, project: Project, name: str,
                fields_rails: Sequence[str] = ()) -> Tuple[PISetup, List[str]]:
    """KiPI's setup for a noise solve of aggressor ``name``: ``to_pisetup`` plus the
    aggressor, the victims, the probes it names, the capacitor models on its two rails, the
    AC mesh settings and the noise options; and the mapping's notes. ``fields_rails``: the
    nets whose rail came from the KiCad fields (``Merged.from_fields``), which only words
    where a switching frequency taken from the source came from."""
    c = _context(board, project, name, fields_rails)
    return c.setup, c.notes


# ---------------------------------------------------------------------- buttons (never at solve)

def _pins_spec(pins: Sequence[str]) -> Optional[List[str]]:
    return list(pins) if pins else None


def seed_phases(board: Board, project: Project, vrm: str, topology: str = "auto",
                vin_v: Optional[float] = None) -> List[Phase]:
    """The phases KiPI reads from the board for the switcher source ``vrm`` (KiPI's
    ``seed_phases``, checks.py:199-254): the high-side pins (its input, buck; its output,
    boost), the low-side pins (its ground), the inductor between a device net and the output
    (buck) or input (boost) and that net as the switch node, its inductance from the value,
    and the capacitors on the rails to ground. For an explicit "Fill from board" action only:
    a solve never calls it."""
    setup = to_pisetup(board, project)
    v = next((x for x in setup.vrms if x.name == vrm), None)
    if v is None:
        raise EngineError(f"No source named {vrm}")
    probe = KipiAggressor("seed", vrm, topology, vin_v)
    try:
        find_vrm(setup, probe)
        topo = topology
        if topo not in ("buck", "boost"):
            vin, _ = input_voltage(setup, probe, v)
            if not vin:
                raise NoiseError(f"the input voltage of {vrm} is unknown: set the topology or Vin")
            topo = _kipi_topology(probe, vin, float(v.voltage))
    except NoiseError as e:
        raise EngineError(str(e).replace("Aggressor seed: ", "")) from None
    out = []
    for ph in _kipi_seed_phases(board.snapshot, setup, v, topo):
        hs, ls = ph.high_side, ph.low_side
        out.append(Phase(inductor=ph.inductor, inductance_h=float(ph.inductance_h),
                         switch_net=ph.switch_net, high_side_ref=hs.ref if hs else "",
                         high_side_pins=_pins_spec(hs.pins) if hs else None,
                         high_side_net=hs.net if hs else "", low_side_ref=ls.ref if ls else "",
                         low_side_pins=_pins_spec(ls.pins) if ls else None,
                         low_side_net=ls.net if ls else "", input_caps=list(ph.input_caps),
                         output_caps=list(ph.output_caps)))
    return out


def guess_ends(board: Board, net: str) -> Tuple[Optional[Tuple[str, List[str]]],
                                                Optional[Tuple[str, List[str]]]]:
    """KiPI's guess at a victim's driver and receiver, ``(ref, pins)`` each (None where it
    finds none): the net's two pads farthest apart, the driver the one on an IC (U…) where
    only one is (checks.py:257-276). For an explicit "Guess ends" action only."""
    d, r = _kipi_guess_ends(board.snapshot, net)
    return ((d.ref, list(d.pins)) if d else None), ((r.ref, list(r.pins)) if r else None)


# ---------------------------------------------------------------------- solve

def solve_aggressor(board: Board, project: Project, name: str,
                    progress: Optional[Callable[[str], None]] = None,
                    cancelled: Callable[[], bool] = lambda: False,
                    fraction: Optional[Callable[[Optional[float]], None]] = None,
                    partial: Optional[Callable[[NoiseResult], None]] = None,
                    fields_rails: Sequence[str] = ()) -> NoiseResult:
    """The ripple and coupled noise of aggressor ``name``, each victim judged against its
    margin (:func:`coppernicus.coupled_noise.judge`). Its switching frequency is its own
    ``fsw_hz``, else its regulator's (``Source.fsw_hz``; ``fields_rails`` as
    :func:`noise_setup`), else the solve is refused. ``partial`` receives the result as KiPI
    hands it over during the solve (the waveforms first, ``partial=True``, no verdict).
    ``progress``, ``fraction``, ``partial`` and ``cancelled`` are called from the thread that
    solves: a GUI must only emit signals from them. KiPI's ``NoiseError`` and the board
    model's mesh and pad errors become :class:`EngineError`, a cancel :class:`Cancelled`; no
    KiPI object is kept."""
    from coppernicus.coupled_noise import judge
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    c = _context(board, project, name, fields_rails)
    reporter = _NoiseProgress(progress, cancelled, fraction, partial,
                              lambda res: _convert(board, project, c, res, {}, thickness, partial=True))
    peak0 = peak_rss_bytes()
    t0 = time.perf_counter()
    try:
        res = solve_noise(board.snapshot, c.setup, name, reporter, dc_result=None)
    except Canceled as e:
        raise Cancelled("solve cancelled") from e
    except (NoiseError, ACError, SolveError, MeshError) as e:
        raise EngineError(f"Noise solve of {name}: {e}") from e
    seconds = time.perf_counter() - t0
    peak1 = peak_rss_bytes()
    st = reporter.sizes
    stats = {"seconds": seconds, "harmonics": float(res.waveforms.harmonics),
             "triangles": st.get("triangles"), "nodes": st.get("nodes"),
             "unknowns": st.get("unknowns"), "ports": st.get("ports"),
             "field_solves": st.get("field_solves"), "field_seconds": st.get("field_seconds"),
             # This process's high-water mark when the solve ended (KiPI's mesher worker
             # processes not counted), as T9's AC solve.
             "memory_peak_mb": peak1 / 1e6 if peak1 else None,
             "memory_peak_raised": bool(peak0 and peak1 and peak1 > peak0)}
    out = _convert(board, project, c, res, stats, thickness)
    return judge(out)


# ---------------------------------------------------------------------- conversion

def _peaks(sp, fsw: float, n: int):
    """KiPI's tolerance values of one ripple spectrum (gather.py:96-108): below and above the
    mean, full band and through a single pole at 20 MHz, with the two sampled periods."""
    s = sp.samples(n) - sp.mean
    b = bandwidth_limited(sp, fsw, BW_LIMIT_HZ).samples(n) - sp.mean
    return (float(max(-s.min(), 0.0)), float(max(s.max(), 0.0)), float(max(-b.min(), 0.0)),
            float(max(b.max(), 0.0))), s, b


def _ripple(name, where, net, ground, ref, pins, sp, fsw, n, t) -> RippleResult:
    (lo, hi, lo_bw, hi_bw), s, b = _peaks(sp, fsw, n)
    return RippleResult(name, where, net, ground, ref, pins, lo, hi, lo_bw, hi_bw,
                        float(np.ptp(s)), float(np.ptp(b)), float(sp.ac_rms()), t, s, b)


def _device_ripple(board: Board, c: _Context, res, used: bool, n, t) -> List[RippleResult]:
    """The ripple at phase 1's device pins (extract/common.py:298-301: buck VIN between its
    high-side and low-side pins, VOUT at the inductor's output pad; a boost the other way),
    or of the capacitor bank without the board model."""
    wf, ph = res.waveforms, c.setup.aggressors[0].phases[0]
    buck = res.operating.topology == "buck"
    hs, ls = ph.high_side, ph.low_side
    ground = ls.net if ls else c.gnd
    ind_rail = (rail_nets(c.setup, next(v for v in c.setup.vrms if v.name == c.vrm), c.gnd)
                if buck else c.in_rail)
    ind_pins = sorted({p.number for p in board.snapshot.pads
                       if p.footprint == ph.inductor and p.net in ind_rail})
    at = {"hs": (hs.ref if hs else "", _pins_spec(hs.pins) if hs else None),
          "ind": (ph.inductor, ind_pins or None)}
    out = []
    for name, sp, net, key in (("output", wf.output_ripple, c.vout_net, "ind" if buck else "hs"),
                               ("input", wf.input_ripple, c.vin_net, "hs" if buck else "ind")):
        if sp is None:
            continue
        ref, pins = at[key] if used else ("", None)
        out.append(_ripple(name, "device pins" if used else "capacitor bank", net, ground, ref,
                           pins, sp, wf.fsw_hz, n, t))
    return out


def _margin(v: Victim, kv: KipiVictim) -> Tuple[Optional[float], str, str, str]:
    """(margin, where it comes from, the limit's name, a warning): the user's
    ``noise_margin_v``, else the named preset's own (lvds, analog), else the JESD8 levels of
    an entered ``logic_v`` on KiPI's table (model.py:96-103), from KiPI's states (the smaller
    of the held-high and held-low margins). KiPI's 35/65 % rule off the table is not used."""
    preset = VICTIM_PRESETS.get(v.preset)
    if v.noise_margin_v is not None:
        return kv.margin(), "noise_margin_v", "noise_margin_v (V)", ""
    if preset is not None and preset.margin_v is not None:
        return kv.margin(), f"preset {v.preset}", f"preset {v.preset} margin (V)", ""
    if v.logic_v is not None:
        on = next((lv for lv in LOGIC_LEVELS if abs(lv - v.logic_v) < 1e-3), None)
        if on is None:
            return None, "", "", (f"Victim {v.label}: logic_v {v.logic_v:g} V is not in KiPI's JESD8 "
                                  "table: enter noise_margin_v")
        vil, vih, _vol, _voh = LOGIC_LEVELS[on]
        return (kv.margin(), f"the JESD8 levels of a {on:g} V family, VIL {vil:g} V, VIH {vih:g} V",
                "logic_v JESD8 margin (V)", "")
    return None, "", "", ""


def _listed(refs: Sequence[str]) -> str:
    return ", ".join(refs[:MISSING_LISTED]) + ("…" if len(refs) > MISSING_LISTED else "")


def _caps(board: Board, c: _Context, used: bool) -> Tuple[List[str], List[str]]:
    """(modelled, without a model): every capacitor between the rails and the ground with
    the board model (KiPI's ``rail_caps``), else the phases' hot-loop capacitors."""
    modelled = {ref for m in c.setup.caps for ref in m.refs}
    phase_caps = list(dict.fromkeys(r for ph in c.agg.phases for r in (*ph.input_caps, *ph.output_caps)))
    if used and c.gnd:
        have, missing = rail_caps(board.snapshot, c.setup, c.rail, c.grounds)
        have = [x[0] for x in have]
        missing = list(dict.fromkeys(missing + [r for r in phase_caps if r not in modelled
                                                and r not in have]))
        return have, missing
    return ([r for r in phase_caps if r in modelled], [r for r in phase_caps if r not in modelled])


def _stand_ins(board: Board, project: Project, c: _Context, res, hot: List[HotLoop],
               capped: bool, f_max: float, used: bool, missing: List[str]) -> List[str]:
    """The KiPI stand-ins that hold for every victim of this aggressor (brief Q2)."""
    out = []
    for h in hot:
        if not h.coss_entered:
            out.append(f"Coss not entered on phase {h.phase}: KiPI's {DEFAULT_COSS_F * 1e12:g} pF "
                       "sets the ringing")
        if not h.qrr_entered:
            out.append(f"Qrr not entered on phase {h.phase}: KiPI's {DEFAULT_QRR_C * 1e9:g} nC sets "
                       "the hard edge (enter 0 for a Schottky or GaN low side)")
    if missing:
        out.append(f"capacitors on the converter's rails without a model: {_listed(missing)}")
    assumed = _dielectric(board, project.ac.default_er)[1]
    if assumed:
        out.append(f"εr assumed: {assumed}")
    if not any(ph.switch_net for ph in c.agg.phases):
        out.append("capacitive coupling left out: no switch net on any phase")
    if capped:
        out.append(f"harmonics stop at {f_max / 1e6:.4g} MHz (noise.max_harmonics "
                   f"{project.noise.max_harmonics})")
    if not used:
        out.append("shared-ground coupling left out: " + _board_off(project, res))
    return out


def _board_off(project: Project, res) -> str:
    if not project.noise.board_model:
        return "board model off (noise.board_model)"
    why = next((w for w in res.warnings if w.startswith("Board model left out")), "")
    return "board model left out" + (f" ({why.split('(', 1)[1].rsplit(')', 1)[0]})" if "(" in why else "")


def _victim(board: Board, c: _Context, v: Victim, kv: KipiVictim, vr, common: List[str],
            used: bool, n: int, warnings: List[str]) -> VictimResult:
    margin, source, limit, warn = _margin(v, kv)
    if warn:
        warnings.append(warn)
    ends_entered = bool(v.driver_ref and v.receiver_ref)
    own = []
    if not ends_entered:
        own.append(f"driver and receiver not both entered: KiPI guessed {vr.driver or '(none)'} and "
                   f"{vr.receiver or '(none)'}")
    if any(w.startswith("No capacitor loops") for w in vr.warnings):
        own.append("inductive coupling left out: no capacitor loops (set the switching device's pins "
                   "and the capacitors)")
    if not vr.receiver_v:
        own.append(f"nothing coupled: {v.net} has no tracks" if not vr.length_mm
                   else "driver or receiver pins not on the board")
    elif used and "common" not in vr.peak_by:
        d, r, _ = _kipi_ends(board.snapshot, kv)
        drv = ground_pads(board.snapshot, d.ref, c.gnd) if d else []
        rcv = ground_pads(board.snapshot, r.ref, c.gnd) if r else []
        if not drv or not rcv:
            own.append(f"shared-ground coupling left out: its driver or receiver has no pad on {c.gnd}")
    states = []
    for st in vr.states:
        if margin is not None and ((st.name == "high" and st.level_v < st.threshold_v)
                                   or (st.name == "low" and st.level_v > st.threshold_v)):
            # KiPI only warns here (victims.py:155-157) and keeps |level - threshold| as a
            # positive margin (model.py:131-133): such a line is never judged.
            own.append(f"held {st.name} it sits at {st.level_v:.3g} V, beyond its "
                       f"{st.threshold_v:.3g} V threshold: no margin left")
        states.append({"name": st.name, "level_v": float(st.level_v),
                       "threshold_v": float(st.threshold_v) if margin is not None else None,
                       "driver_ohm": float(st.driver_ohm), "excursion_v": float(st.excursion_v),
                       "margin_v": float(st.margin_v) if margin is not None else None})
    total = vr.receiver_v.get("total")
    peak = float(vr.peak_v)
    return VictimResult(
        name=v.label, net=v.net, driver=vr.driver, receiver=vr.receiver, ends_entered=ends_entered,
        preset=v.preset, margin_v=margin, margin_from=source, limit=limit, peak_v=peak,
        peak_by_v={k: float(vr.peak_by[k]) for k in MECHANISMS if k in vr.peak_by},
        dominant=vr.dominant, ratio=(peak / margin) if margin else None, states=states,
        length_mm=float(vr.length_mm), coupled_mm=float(vr.coupled_mm),
        cm_f={k: float(x) for k, x in vr.cm_f.items()}, m_h={k: float(x) for k, x in vr.m_h.items()},
        splits=[s.describe() for s in vr.splits],
        receiver_v=total.samples(n) if total is not None else np.zeros(n),
        stand_ins=common + own, warnings=list(vr.warnings))


def _near_field(emi) -> Optional[NearField]:
    if emi is None:
        return None

    def fmap(m):
        return None if m is None else FieldMap(m.kind, m.unit, np.array(m.values, float),
                                               tuple(float(x) for x in m.bounds))

    def finite(x):
        return float(x) if x is not None and math.isfinite(x) else None
    spots = [{"kind": kind, "x_mm": float(s.x_mm), "y_mm": float(s.y_mm), "value": float(s.value),
              "unit": s.unit, "f_hz": float(s.f_hz), "source": s.source}
             for kind, group in (("H", emi.h_hotspots), ("E", emi.e_hotspots)) for s in group]
    radiated = finite(emi.worst_dbuv)
    return NearField(float(emi.height_mm), float(emi.distance_m), fmap(emi.h_map), fmap(emi.e_map),
                     spots, radiated, float(emi.worst_f_hz) if radiated is not None else None,
                     finite(emi.worst_limit_dbuv) if radiated is not None else None,
                     emi.worst_source, list(emi.notes))


def _convert(board: Board, project: Project, c: _Context, res, stats: Dict[str, Any], thickness,
             partial: bool = False) -> NoiseResult:
    op, wf = res.operating, res.waveforms
    a, s = c.setup.aggressors[0], c.setup.noise
    n = wf.samples_per_period()
    t = np.arange(n) / (n * wf.fsw_hz)
    rings = [h.f_ring_hz for h in res.hot_loops if h.f_ring_hz]
    _k, f_max, capped = harmonic_count(a.fsw_hz, a.rise_s, a.fall_s, max(rings) if rings else None,
                                       s.f_max_hz, s.max_harmonics)
    hot = [HotLoop(h.phase, float(h.loop_l_h), float(h.package_l_h), float(h.c_sw_f), float(h.coss_f),
                   not h.coss_default, float(h.qrr_c) * 1e9, not h.qrr_default, float(h.c_copper_f),
                   float(h.r_loop_ohm), float(h.f_ring_hz) if h.f_ring_hz else None, float(h.zeta),
                   float(h.zeta_loop), bool(h.traced), h.image_plane) for h in res.hot_loops]
    used = bool(res.board_note)
    ripple = []
    by_probe = {p.name: p for p in c.probes}
    for name, sp in wf.probe_ripple.items():
        p = by_probe[name]
        ripple.append(_ripple(name, "probe", p.net, p.ground_net, p.ref, p.pins, sp, wf.fsw_hz, n, t))
    if used or not partial:
        ripple += _device_ripple(board, c, res, used, n, t)
    have, missing = _caps(board, c, used)
    # A partial result comes before the board model has run: no stand-ins, no assumptions.
    common = [] if partial else _stand_ins(board, project, c, res, hot, capped, f_max, used, missing)
    warnings = list(dict.fromkeys(c.notes + list(res.warnings)))
    kv = {x.name: x for x in c.setup.victims}
    victims = [_victim(board, c, v, kv[v.label], vr, common, used, n, warnings)
               for v in c.victims for vr in res.victims if vr.name == v.label]
    board_info = {"used": used, "note": res.board_note, "unknowns": stats.get("unknowns"),
                  "field_solves": stats.get("field_solves"), "ports": stats.get("ports"),
                  "caps_modelled": have, "caps_missing": missing}
    out = NoiseResult(
        aggressor=c.agg.name, vrm=c.vrm, rail=c.vout_net, input_net=c.vin_net, ground=c.gnd,
        topology=op.topology, vin_v=float(op.vin), vout_v=float(op.vout), iout_a=float(op.iout),
        iin_a=float(op.iin), duty=float(op.duty), efficiency=float(op.efficiency), phases=op.phases,
        operating_notes=list(op.notes), fsw_hz=float(wf.fsw_hz), rise_s=float(a.rise_s),
        fall_s=float(a.fall_s), f_max_hz=float(wf.f_max_hz), harmonics=wf.harmonics,
        harmonics_capped=bool(capped), inductor_ripple_a=[float(x) for x in wf.ripple_a],
        hot_loops=hot, t_s=t, switch_node_v=[ph.switch_node.samples(n) for ph in wf.phases],
        inductor_a=[ph.inductor.samples(n) for ph in wf.phases], edges=[ph.edges for ph in wf.phases],
        ripple=ripple, victims=victims, board=board_info, emi=_near_field(res.emi), stand_ins=common,
        partial=partial, warnings=warnings, stats=dict(stats))
    if not partial:
        out.assumptions = _assumptions(board, project, c, out, have, missing)
    return out


# ---------------------------------------------------------------------- assumptions

def _mhz(f: float) -> str:
    return f"{f / 1e6:.4g} MHz"


def _assumptions(board: Board, project: Project, c: _Context, r: NoiseResult, have: List[str],
                 missing: List[str]) -> List[str]:
    o, a = project.noise, c.agg
    how = ("noise.f_max_hz" if o.f_max_hz else "ten times the faster edge's corner 1/(π·t) or three "
           "times the ringing, at most 3 GHz")
    out = [f"Coupled noise (KiPI): the converter's waveforms are exact Fourier series of "
           f"piecewise-linear periods, {r.harmonics} harmonics of {_mhz(r.fsw_hz)} to "
           f"{_mhz(r.f_max_hz)} ({how}); the switch node's edges ring through the hot loop's L and C.",
           f"Operating point: {'; '.join(r.operating_notes)}; efficiency {r.efficiency:g} (the source's)."]
    if c.fsw_note:
        out.append(c.fsw_note)
    for h, ph in zip(r.hot_loops, a.phases):
        traced = "traced through the copper" if h.traced else "straight between the pads"
        coss = "entered" if h.coss_entered else f"KiPI's {DEFAULT_COSS_F * 1e12:g} pF, not entered"
        ring = (f"rings at {_mhz(h.f_ring_hz)}, ζ {h.zeta:.3g} (at least min_ring_zeta "
                f"{a.min_ring_zeta:g}; the loop alone gives {h.zeta_loop:.3g})" if h.f_ring_hz
                else "no ringing (no hot-loop inductance)")
        out.append(f"Phase {h.phase}: hot loop {h.loop_l_h * 1e9:.3g} nH ({traced}), switch node "
                   f"{h.c_sw_f * 1e12:.4g} pF (Coss {h.coss_f * 1e12:.4g} pF {coss} + copper "
                   f"{h.c_copper_f * 1e12:.3g} pF); {ring}.")
        qrr = "entered" if h.qrr_entered else f"KiPI's {DEFAULT_QRR_C * 1e9:g} nC, not entered"
        rds = ph.rds_on_hs_ohm if ph.rds_on_hs_ohm is not None else DEFAULT_RDS_ON_OHM
        rds_from = "entered" if ph.rds_on_hs_ohm is not None else f"KiPI's {DEFAULT_RDS_ON_OHM * 1e3:g} mΩ"
        dead = "" if o.dead_time_ns != NoiseOptions().dead_time_ns else ", KiPI's 20 unless set"
        out.append(f"Phase {h.phase}: hard edge with Qrr {h.qrr_nc:.3g} nC ({qrr}); soft edge damped "
                   f"at least soft_edge_zeta {a.soft_edge_zeta:g}; dead time {o.dead_time_ns:g} ns "
                   f"(noise.dead_time_ns{dead}); on-resistance {rds * 1e3:g} mΩ for damping "
                   f"({rds_from}); package loop {h.package_l_h * 1e9:.3g} nH.")
    rails = " and ".join(x for x in (r.input_net, r.rail) if x)
    line = (f"Capacitors on {rails}: ESR + jω·ESL + 1/(jω·C), {len(have)} modelled")
    if missing:
        line += (f"; {len(missing)} without a model taken at their board value with KiPI's "
                 f"{FALLBACK_ESR_OHM * 1e3:g} mΩ and {FALLBACK_ESL_H * 1e9:g} nH "
                 f"({FALLBACK_C_F * 1e6:g} µF when the value cannot be read): {_listed(missing)}")
    out.append(line + ".")
    b = r.board
    if b["used"]:
        u = f"{int(b['unknowns']):,} unknowns, " if b.get("unknowns") else ""
        k = f"{int(b['field_solves'])} field solves " if b.get("field_solves") else "field solves "
        out.append(f"Board model: the copper of {' / '.join(sorted(c.rail))} and "
                   f"{' + '.join([c.gnd, *sorted(c.grounds - {c.gnd})])} as AC mode's 2.5D field "
                   f"model, {u}{k}({o.field_per_decade} per decade); each phase's pulsed and inductor "
                   "currents enter at its pins and divide among the capacitors through the copper; "
                   "quasi-static, approximate above about 1 GHz.")
    else:
        why = _board_off(project, _Warnings(r.warnings))
        head = ("Board model off (noise.board_model)" if not o.board_model
                else "Board model " + why.replace("board model ", "", 1))
        out.append(f"{head}: the capacitors share the current by their own impedance; no shared-ground "
                   "coupling, no ripple at probes.")
    rated = [m for m in c.setup.caps if m.rated_v and m.dielectric != "C0G"]
    if rated and c.setup.ac.dc_bias:
        out.append(f"DC bias: rated capacitors derated at the rails' nominal voltages, {r.vin_v:g} V "
                   f"and {r.vout_v:g} V; no DC solve is passed.")
    summary, assumed = _dielectric(board, project.ac.default_er)
    line = (f"Dielectric εr, top to bottom: {summary}; solder mask εr {MASK_ER:g} (KiPI's typical LPI) "
            "where the stackup has one.")
    if "no εr" in assumed:
        line += f" Where the stackup gives no εr, {project.ac.default_er:g} is used (ac.default_er)."
    out.append(line)
    if r.victims:
        out.append(f"Victims: traces cut every {o.segment_mm:g} mm with copper within {o.window_mm:g} mm "
                   "(2D quasi-static cross-sections); mechanisms capacitive (switch nodes), inductive "
                   "(capacitor and inductor loops) and common (driver's ground against receiver's, the "
                   "copper's resistance only), added as phasors.")
    kv = {x.name: x for x in c.setup.victims}
    for v in r.victims:
        k = kv[v.name]
        label = VICTIM_PRESETS[v.preset].label if v.preset in VICTIM_PRESETS else "Custom"
        low = (f" (low {k.driver_low_ohm:.4g} Ω)" if k.driver_low_ohm is not None
               and k.driver_low_ohm != k.driver_ohm else "")
        term = f", termination {k.termination_ohm:g} Ω" if k.termination_ohm else ""
        ends = ("entered" if v.ends_entered else
                "guessed by KiPI: the net's two pads farthest apart")
        out.append(f"Victim {v.name}: {label}, driver {k.driver_ohm:.4g} Ω{low}, receiver "
                   f"{k.receiver_ohm:.4g} Ω ∥ {k.receiver_pf:g} pF{term}; ends {v.driver} → "
                   f"{v.receiver} ({ends}).")
        if v.margin_v is not None:
            out.append(f"Victim {v.name}: margin {v.margin_v * 1e3:.4g} mV from {v.margin_from}; judged "
                       "as KiPI does: the largest receiver |v|, held high or low, against the smaller "
                       "state margin.")
        else:
            out.append(f"Victim {v.name}: no noise margin set: ranked, not judged.")
    for ph in a.phases:
        if ph.inductor:
            out.append(f"Inductor {ph.inductor}: {'shielded' if ph.inductor_shielded else 'unshielded'}, "
                       f"winding axis {ph.inductor_axis} (KiPI's defaults: vertical, shielded).")
    if r.emi is not None:
        out.append(f"Near field {r.emi.height_mm:g} mm over the top copper and a radiated estimate at "
                   f"{r.emi.distance_m:g} m from the loops' differential-mode currents on a bare board "
                   "(no cables, no enclosure): an indicator, never judged.")
    out.append(engine_settings(project.settings)[1][0])
    out += thickness_note(dict(project.settings.copper_thickness_mm or {}), board.file_thickness_mm)
    if project.settings.copper_temp_c != REF_TEMP_C:
        out.append(f"Copper at {project.settings.copper_temp_c:g} °C (settings.copper_temp_c); "
                   "capacitor models with a dielectric follow KiPI's typical temperature curve.")
    for rp in r.ripple:
        what = ("the board model's voltage between its pins" if rp.where != "capacitor bank"
                else "the capacitor bank's voltage (no board model)")
        out.append(f"Ripple at {rp.name}: {what} ({rp.net} − {rp.ground_net}) at the switching "
                   "harmonics; p-p over one period, and through a single pole at 20 MHz (−3 dB, a "
                   "scope's bandwidth limit); never judged here (Task 11 stacks it per load).")
    return out


@dataclass
class _Warnings:
    warnings: List[str]


# ---------------------------------------------------------------------- cost and memory (brief Q8)

# A whole noise solve's peak memory, measured on synthetic boards (gate T10 §9: KiPI's buck
# board with two probes, its coupled-noise board meshed at 1.0 and 0.3 mm, KiPI's four-phase
# example; 9 691 to 175 084 unknowns, peak RSS 0.55 to 5.05 GB): 0.2 GB plus 26 to 36 kB per
# unknown. The larger figure is used: an upper bound (a real rail of T9's took about 8 kB per
# unknown; fill-in depends on the copper).
NOISE_MEM_BASE_BYTES = 0.2e9
NOISE_MEM_PER_UNKNOWN = 36e3
NOISE_MEM_UNKNOWNS = (9_691, 175_084)
# Unknowns per triangle of the noise mesh: 0.80 to 0.85 on those boards, 0.95 on T9's rail.
NOISE_UNKNOWNS_PER_TRIANGLE = 0.95


@dataclass(frozen=True)
class NoiseSize:
    """What a noise solve will hold, before it runs: the board model's mesh of the
    aggressor's rails and ground, its ports and field solves (none without the board model)."""
    aggressor: str
    board_model: bool
    triangles: int
    nodes: int
    ports: int
    field_solves: int              # from the edges (or noise.f_max_hz); ringing may add a few
    harmonics: int
    f_max_hz: float
    mesh_seconds: float

    @property
    def unknowns(self) -> int:
        """At most about 0.95 unknowns per triangle (see NOISE_UNKNOWNS_PER_TRIANGLE)."""
        return int(round(self.triangles * NOISE_UNKNOWNS_PER_TRIANGLE))


def mesh_estimate(board: Board, project: Project, name: str,
                  progress: Optional[Callable[[str], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False,
                  fields_rails: Sequence[str] = ()) -> NoiseSize:
    """Mesh the aggressor's rails and ground as the solve's board model will (KiPI's
    ``board_ports`` and ``mesh_rail_and_ground``) and count its ports and field solves; it
    checks the setup as the solve does. Seconds on a large board; no field solve runs."""
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    c = _context(board, project, name, fields_rails)
    agg, s = c.setup.aggressors[0], c.setup.noise
    errors = validate_noise(c.setup, board.snapshot, agg)
    if errors:
        raise EngineError(f"Noise solve of {name}: " + "; ".join(errors))
    k, f_max, _capped = harmonic_count(agg.fsw_hz, agg.rise_s, agg.fall_s, None, s.f_max_hz,
                                       s.max_harmonics)
    solves = max(4, int(math.ceil(math.log10(f_max / agg.fsw_hz) * s.field_per_decade)) + 1)
    if not s.board_model:
        return NoiseSize(name, False, 0, 0, 0, 0, k, f_max, 0.0)
    t0 = time.perf_counter()
    try:
        op = operating_point(c.setup, agg)
        gnd = aggressor_ground(c.setup, agg)
        grounds = {}
        for kv in c.setup.victims:
            d, r, _ = _kipi_ends(board.snapshot, kv)
            grounds[kv.name] = (ground_pads(board.snapshot, d.ref, gnd) if d else [],
                                ground_pads(board.snapshot, r.ref, gnd) if r else [])
        bp = board_ports(board.snapshot, c.setup, agg, op, grounds, c.setup.probes)
        mesh, _kind = mesh_rail_and_ground(board.snapshot, sorted(bp.rail), bp.ground_nets(),
                                           bp.terminal_ids(), c.setup.settings, c.setup.ac,
                                           _NoiseProgress(progress, cancelled, None))
    except Canceled as e:
        raise Cancelled("cancelled") from e
    except (NoiseError, ACError, SolveError, MeshError) as e:
        raise EngineError(f"Noise solve of {name}: {e}") from e
    return NoiseSize(name, True, int(mesh.n_tris), int(mesh.n_nodes), len(bp.ports), solves, k,
                     f_max, time.perf_counter() - t0)


def noise_cost_estimate(size: NoiseSize, last: Optional[Dict[str, Any]] = None) -> CostEstimate:
    """T9's rough cost (:func:`coppernicus.engine.ac.cost_estimate`) of the board model's
    field solves, scaled from the ``stats`` of an earlier noise solve on this computer."""
    prior = None
    if last:
        prior = dict(last, field_frequencies=last.get("field_solves"))
    return cost_estimate(size.triangles, size.ports, size.field_solves, prior)


def noise_ram_estimate(unknowns: float, total_bytes: Optional[float]) -> RamEstimate:
    """Whether a noise solve of ``unknowns`` could exceed this machine's memory, from the
    upper bound measured on synthetic boards (NOISE_MEM_BASE_BYTES + NOISE_MEM_PER_UNKNOWN per
    unknown). A warning to confirm, never a cancel (brief Q8)."""
    need = NOISE_MEM_BASE_BYTES + NOISE_MEM_PER_UNKNOWN * float(unknowns)
    beyond = " (extrapolated beyond them)" if unknowns > NOISE_MEM_UNKNOWNS[1] else ""
    how = (f"an upper bound measured on synthetic boards, 0.2 GB plus 26 to 36 kB per unknown from "
           f"{NOISE_MEM_UNKNOWNS[0]:,} to {NOISE_MEM_UNKNOWNS[1]:,} unknowns{beyond}; a real board's "
           "copper may need less")
    head = f"About {int(unknowns):,} unknowns: the noise solve may peak at up to {need / 1e9:.3g} GB"
    if not total_bytes:
        return RamEstimate(need, None, False, f"{head} ({how}); this machine's memory cannot be read.")
    if need > total_bytes:
        return RamEstimate(need, float(total_bytes), True, (
            f"{head} ({how}), more than this machine's {total_bytes / 1e9:.3g} GB of memory, so it "
            "could swap: a coarser AC mesh (ac.max_edge_mm) or fewer field solves "
            "(noise.field_per_decade) lowers it."))
    return RamEstimate(need, float(total_bytes), False,
                       f"{head} of this machine's {total_bytes / 1e9:.3g} GB ({how}).")
