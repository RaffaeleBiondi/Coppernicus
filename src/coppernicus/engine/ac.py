"""AC PDN impedance |Z|(f) of one rail with KiPI's AC solver (``kipi/ac``), converted to
:mod:`coppernicus.results`.

Physics (KiPI ``kipi/ac/solver/fem.py``): a 2.5D FEM of the rail's and the ground's copper,
two faces per sheet; facing copper gives the plane capacitance ε₀·εr/h·(1 − j·tanδ) per area
and the loop inductance μ₀·h per square; plated barrels are resistors; capacitors, links and
probes are lumped ports. The port matrix is solved at the field frequencies, interpolated
(PCHIP of jω·Z on log f) and terminated by the parts (``kipi/ac/solver/sweep.py``). The
source (VRM) is left out: the result is the open-loop impedance. Field by field:
``docs/engine-mapping.md`` (Task 9).
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from kipi.ac import cap_file
from kipi.ac.checks import capacitor_candidates as _kipi_candidates
from kipi.ac.checks import ground_nets, rail_nets, validate_ac
from kipi.ac.model import DEFAULT_KEPT_AT_RATED_PCT, ACSettings
from kipi.ac.model import CapModel as KipiCapModel
from kipi.ac.model import Probe as KipiProbe
from kipi.ac.solver.fem import ACError, dielectric_summary, mesh_rail_and_ground
from kipi.ac.solver.field import MEMORY_SHARE
from kipi.ac.solver.physics import EPS0_F_PER_MM  # noqa: F401  (re-exported for the tests)
from kipi.ac.solver.sweep import build_ports, solve_ac
from kipi.ac.target import judged_from, target_at, target_from_transient
from kipi.ac.transient import bandwidth_limited, transient
from kipi.core.eta import factor_work
from kipi.core.memory import free_memory
from kipi.core.mesh.mesher import MeshError
from kipi.core.model.parts import is_capacitor_ref, parse_capacitance
from kipi.core.progress import Canceled
from kipi.dc.pi.model import DEFAULT_BANDWIDTH_KHZ, PinRef, PISetup
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C
from kipi.tolerance.gather import BW_LIMIT_HZ

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import _numbers, _pads, terminal_names, to_pisetup
from coppernicus.engine.pisetup import ground_nets as project_ground_nets
from coppernicus.engine.solve import Cancelled, _Progress, engine_settings, thickness_note
from coppernicus.engine.tolerance import load_probes, mismatch_warnings
from coppernicus.impedance import hz_text, judge, ohm_text
from coppernicus.project import CapModel, Probe, Project, Source
from coppernicus.results import ImpedanceResult, ProbeImpedance, TransientResult

# AcOptions fields that are KiPI's ACSettings fields of the same name.
SWEEP_KEYS = ("f_start_hz", "f_stop_hz", "points_per_decade", "field_points_per_decade",
              "max_edge_mm", "loss_tangent", "default_er")
# Shown, never judged, when the source's bandwidth is not given: KiPI's bandwidth for a new
# VRM (kipi/dc/pi/model.py:57, :127). KiPI's AC model has no VRM impedance (checks.py:6-7):
# the bandwidth only says where the open-loop curve stops being what the rail sees.
ASSUMED_BANDWIDTH_HZ = DEFAULT_BANDWIDTH_KHZ * 1e3
# KiPI log lines that judge against a bandwidth or a target Coppernicus does not use.
_DROPPED = re.compile(r"^(Targets apply above the |  .+: \|Z\| )")


def kipi_ac_defaults() -> Dict[str, Any]:
    """KiPI's defaults for the sweep options :class:`coppernicus.project.AcOptions` copies."""
    s = ACSettings()
    return {k: getattr(s, k) for k in SWEEP_KEYS}


class _AcProgress(_Progress):
    """The engine's messages and its fraction done (one step per field frequency); drops
    KiPI's lines about targets and worst points, which Coppernicus judges itself."""

    def __init__(self, callback, cancelled, fraction: Optional[Callable[[Optional[float]], None]]):
        super().__init__(callback, cancelled)
        self._fraction = fraction

    def message(self, text, level=20):
        if _DROPPED.match(text):
            if self._cancelled():
                self.cancel()
            return
        super().message(text, level)

    def fraction(self, value):
        if self._fraction is not None:
            self._fraction(value)
        if self._cancelled():
            self.cancel()


# ---------------------------------------------------------------------- setup

@dataclass
class _Context:
    setup: PISetup
    vrm: str                       # the VRM's name in the setup
    gnd: str
    rail: str                      # the net the source drives
    source: Source
    bandwidth_hz: Optional[float]  # as given (None: not given)
    probes: List[Probe]
    notes: List[str] = field(default_factory=list)


def source_bandwidth(project: Project, source: Source) -> Optional[float]:
    """The source's control loop bandwidth from ``ac.bandwidth_hz`` (by its name, else its
    ref); None when not given."""
    bw = project.ac.bandwidth_hz
    return bw.get(source.label, bw.get(source.ref))


def _vrm_of(setup: PISetup, name: str):
    return next(v for v in setup.vrms if v.name == name)


def _rail_source(setup: PISetup, project: Project, names, net: str) -> Tuple[str, Source, str, List[str]]:
    """(rail net, its source, VRM name, notes) for ``net``: its own source, or the rail whose
    links join it (a net behind a ferrite has no source of its own)."""
    rail = project.rail(net)
    if rail is not None and rail.sources and ("source", net, 0) in names:
        return net, rail.sources[0], names[("source", net, 0)], []
    for r in project.rails:
        key = ("source", r.net, 0)
        if not r.sources or key not in names:
            continue
        vrm = _vrm_of(setup, names[key])
        gnd = vrm.ground.net if vrm.ground else ""
        if net in rail_nets(setup, vrm, gnd):
            return r.net, r.sources[0], names[key], [
                f"{net} has no source of its own: it is solved with {r.net}, which feeds it "
                "through links"]
    if net in project_ground_nets(project):
        raise EngineError(f"{net} is a ground in this project (ground pins name it): an AC solve "
                          f"looks into a rail against its ground; solve the rail whose probes "
                          f"return to {net}")
    raise EngineError(f"{net} has no source, and no link joins it to a rail that has one: add "
                      "its source (the regulator output) before an AC solve")


def _kipi_probe(board: Board, p: Probe) -> KipiProbe:
    owner = f"Probe {p.name}"
    pads = _pads(board, [p.net], p.ref, p.pins, f"{owner} (supply pins)")
    if not p.ground_net:
        raise EngineError(f"{owner}: set its ground pins (ground) before an AC solve")
    gref = p.ground_ref or p.ref
    gpads = _pads(board, [p.ground_net], gref, p.ground_pins, f"{owner} (ground pins)")
    return KipiProbe(p.name, PinRef(p.ref, p.net, _numbers(pads, p.pins)),
                     PinRef(gref, p.ground_net, _numbers(gpads, p.ground_pins)))


def _kipi_caps(board: Board, models: Sequence[CapModel], rail: set, grounds: set,
               notes: List[str]) -> List[KipiCapModel]:
    """The models with a part between the rail and its ground (KiPI models only parts named
    C<n>, kipi/core/model/parts.py:72; a model's other refs are left as they are)."""
    out = []
    for m in models:
        used = False
        for ref in m.refs:
            nets = set(board.part_nets(ref))
            if not nets:
                raise EngineError(f"Capacitor model {m.name}: {ref} is not on the board")
            if not (nets <= rail | grounds and nets & rail and nets & grounds):
                continue
            if not is_capacitor_ref(ref):
                notes.append(f"Capacitor model {m.name}: {ref} is between the rail and its ground, "
                             "but KiPI models only parts named C<number>: it is left out")
                continue
            used = True
        if used:
            out.append(KipiCapModel(m.name, m.capacitance_f, m.esl_h, m.esr_ohm, list(m.refs),
                                    m.rated_v, DEFAULT_KEPT_AT_RATED_PCT if m.kept_at_rated_pct is None
                                    else m.kept_at_rated_pct, m.dielectric))
    return out


def _context(board: Board, project: Project, net: str) -> _Context:
    setup = to_pisetup(board, project)
    names = terminal_names(project)
    rail_net, source, vrm_name, notes = _rail_source(setup, project, names, net)
    vrm = _vrm_of(setup, vrm_name)
    # KiPI's Vrm defaults to 50 kHz (kipi/dc/pi/model.py:127): always overwritten here.
    bandwidth = source_bandwidth(project, source)
    vrm.bandwidth_khz = (bandwidth if bandwidth is not None else ASSUMED_BANDWIDTH_HZ) / 1e3
    joined = rail_nets(setup, vrm, vrm.ground.net if vrm.ground else "")
    probes = [p for p in project.probes if p.net in joined]
    gnd = source.ground_net or next((p.ground_net for p in probes if p.ground_net), "")
    if not gnd:
        raise EngineError(f"Rail {rail_net}: no ground to solve against: give the source "
                          f"{source.label} or a probe its ground pins")
    on_rail = rail_nets(setup, vrm, gnd)
    grounds = set(ground_nets(setup, vrm, gnd))
    probes = [p for p in project.probes if p.net in on_rail]
    setup.probes = [_kipi_probe(board, p) for p in probes]
    setup.caps = _kipi_caps(board, project.capacitors, set(on_rail), grounds, notes)
    esl = project.ac.link_esl_h
    for i, k in enumerate(project.links):
        value = esl.get(k.ref, esl.get(k.name))
        if value is not None:
            next(x for x in setup.links if x.name == names[("link", "", i)]).esl_h = float(value)
    known = {k.ref for k in project.links} | {k.name for k in project.links}
    notes += [f"ac.link_esl_h names {key}, which is no link of the project: not used"
              for key in esl if key not in known]
    sources = {s.label for r in project.rails for s in r.sources} | {
        s.ref for r in project.rails for s in r.sources}
    notes += [f"ac.bandwidth_hz names {key}, which is no source of the project: not used"
              for key in project.ac.bandwidth_hz if key not in sources]
    a, s = project.ac, ACSettings()
    for key in SWEEP_KEYS:
        setattr(s, key, getattr(a, key))
    s.rail, s.ground = vrm_name, gnd
    if project.settings.copper_temp_c != REF_TEMP_C:
        s.temperature, s.temperature_c = "fixed", float(project.settings.copper_temp_c)
    setup.ac = s
    return _Context(setup, vrm_name, gnd, rail_net, source, bandwidth, probes, notes)


def resolve_rail(board: Board, project: Project, net: str) -> str:
    """The rail an AC solve of ``net`` solves: ``net`` itself when its source drives it, else
    the rail whose links join it. Raises :class:`EngineError` as the solve would."""
    setup = to_pisetup(board, project)
    return _rail_source(setup, project, terminal_names(project), net)[0]


def ac_setup(board: Board, project: Project, rail: str) -> Tuple[PISetup, str, str, List[str]]:
    """KiPI's setup for an AC solve of ``rail`` (``to_pisetup`` plus the rail's probes, the
    capacitor models on it, the sweep and the source's bandwidth): ``(setup, VRM name, ground
    net, notes)``. The ground is the source's ground net, else the first probe's."""
    c = _context(board, project, rail)
    return c.setup, c.vrm, c.gnd, c.notes


def probe_rails(board: Board, project: Project) -> Dict[str, Optional[str]]:
    """Each probe's rail: the net whose source feeds the probe's net, directly or through
    links; None when no rail with a source reaches it (it cannot be solved)."""
    if not project.probes:
        return {}
    setup = to_pisetup(board, project)
    names = terminal_names(project)
    groups = []
    for r in project.rails:
        key = ("source", r.net, 0)
        if r.sources and key in names:
            vrm = _vrm_of(setup, names[key])
            groups.append((r.net, rail_nets(setup, vrm, vrm.ground.net if vrm.ground else "")))
    return {p.name: next((net for net, joined in groups if p.net in joined), None)
            for p in project.probes}


def ac_rails(board: Board, project: Project) -> List[str]:
    """The rails (nets their source drives) with a probe on them or on a net their links
    join, in project order."""
    reached = set(probe_rails(board, project).values())
    return [r.net for r in project.rails if r.net in reached]


# ---------------------------------------------------------------------- targets

def _target(p: Probe) -> Tuple[Optional[float], Optional[float], List[Tuple[float, float]], str]:
    """(target_ohm, target_hz, mask, note): entered, or computed by KiPI from the probe's AC
    tolerance, load step and slew rate (kipi/ac/target.py:53-60); no target otherwise."""
    if p.target_points:
        pts = sorted(p.target_points)
        text = ", ".join(f"({hz_text(f)}, {ohm_text(z)})" for f, z in pts)
        return None, None, pts, (f"Probe {p.name}: target mask {text} (entered; applies only between "
                                 "its first and last points)")
    if p.target_ohm:
        upto = f"up to {hz_text(p.target_hz)}" if p.target_hz else "over the whole sweep"
        text = f"Probe {p.name}: target {ohm_text(p.target_ohm)} {upto} (entered)"
        return p.target_ohm, p.target_hz, [], text
    if p.ac_tolerance_v and p.step_a and p.slew_a_per_us:
        t = target_from_transient(p.ac_tolerance_v, p.step_a, p.slew_a_per_us * 1e6)
        if t is not None:
            return t.z_ohm, t.f_hz, [], (
                f"Probe {p.name}: target {ohm_text(t.z_ohm)} up to {hz_text(t.f_hz)} (computed by "
                "KiPI: Z = V_ac/I_step up to 1/(π·t_rise), t_rise = I_step/slew, from "
                f"ac_tolerance_v {p.ac_tolerance_v:g} V, step_a {p.step_a:g} A, slew_a_per_us "
                f"{p.slew_a_per_us:g})")
    return None, None, [], ""


def _target_curve(freqs: np.ndarray, ohm, hz, points, given: Optional[float], shown: float):
    """(target at each frequency, NaN where none applies; KiPI's judged_from note; the
    bandwidth it is judged above). The target applies above the bandwidth (given, or the
    assumed one for the plot) with KiPI's ``target_at``, except that a flat target ending
    below the sweep applies nowhere, and a mask only between its first and last points: KiPI
    holds both flat beyond their ends, which would judge against values never given."""
    if not (points or ohm):
        return None, "", None
    t = target_at(freqs, ohm, hz, points or None, shown)
    if hz and not points and not (freqs <= hz).any():
        t[:] = np.nan
    if points:
        lo, hi = points[0][0], points[-1][0]
        t[(freqs < lo * (1 - 1e-9)) | (freqs > hi * (1 + 1e-9))] = np.nan
    if given is None:
        return t, "", None
    above, why = judged_from(given, freqs, ohm, hz, points or None)
    if why:
        return t, why.split(", which leaves")[0], None
    return t, "", above


# ---------------------------------------------------------------------- memory

def peak_rss_bytes() -> Optional[int]:
    """This process's peak resident memory so far (bytes), or None where it can't be read.
    It is a high-water mark of the whole process, not of one solve."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            c = Counters()
            c.cb = ctypes.sizeof(Counters)
            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                                   wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return int(c.PeakWorkingSetSize)
            return None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)    # Linux: kB
    except Exception:                                  # noqa: BLE001 - never fail a solve
        return None


# ---------------------------------------------------------------------- solve

def solve_ac_rail(board: Board, project: Project, rail: str,
                  progress: Optional[Callable[[str], None]] = None,
                  cancelled: Callable[[], bool] = lambda: False,
                  fraction: Optional[Callable[[Optional[float]], None]] = None) -> ImpedanceResult:
    """The open-loop PDN impedance of ``rail`` (or of the rail feeding it) at its probes,
    each probe judged against its target (:func:`coppernicus.impedance.judge`).

    ``progress`` is also called from KiPI's worker threads (frequencies solved side by side),
    ``fraction`` and ``cancelled`` from the thread that solves: a GUI must only emit signals
    from them. ``stats["memory_peak_mb"]`` is this process's high-water mark when the solve
    ended (KiPI's mesher worker processes are not counted); ``memory_peak_raised`` says
    whether this solve raised it."""
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    c = _context(board, project, rail)
    peak0 = peak_rss_bytes()
    t0 = time.perf_counter()
    try:
        res = solve_ac(board.snapshot, c.setup, c.vrm, c.gnd, _AcProgress(progress, cancelled, fraction))
    except Canceled as e:
        raise Cancelled("solve cancelled") from e
    except (ACError, SolveError, MeshError) as e:
        raise EngineError(f"AC solve of {c.rail}: {e}") from e
    seconds = time.perf_counter() - t0
    peak1 = peak_rss_bytes()
    out = _convert(board, project, c, res, thickness)
    st = res.stats
    out.stats = {"seconds": seconds, "triangles": float(st.n_tris), "nodes": float(st.n_nodes),
                 "unknowns": float(st.n_unknowns), "ports": float(st.n_ports), "method": st.method,
                 "field_frequencies": float(len(res.field_freqs)), "sweep_points": float(len(res.freqs)),
                 "field_seconds": float(st.times.get("field", math.nan)),
                 "setup_seconds": float(st.times.get("setup", math.nan)),
                 # This process's high-water mark when the solve ended (KiPI's mesher worker
                 # processes not counted); this solve's own peak when it raised it
                 # (memory_peak_raised), else at most this.
                 "memory_peak_mb": peak1 / 1e6 if peak1 else None,
                 "memory_peak_raised": bool(peak0 and peak1 and peak1 > peak0)}
    return judge(out)


def _transient(freqs: np.ndarray, z, bandwidth: Optional[float], p: Probe) -> Optional[TransientResult]:
    """KiPI's transient estimate at probe ``p`` (``kipi.ac.transient.transient``, with its
    20 MHz view ``bandwidth_limited``), only with a step, a slew rate and a bandwidth the user
    gave: never the 50 kHz placeholder written into KiPI's VRM (:func:`_context`), so never
    KiPI's ``transients(result)``. No load line (``ImpedanceResult.load_line_ohm`` is 0)."""
    if bandwidth is None or not (p.step_a and p.slew_a_per_us):
        return None
    tr = transient(freqs, np.asarray(z, dtype=complex), bandwidth, p.step_a, p.slew_a_per_us * 1e6, 0.0)
    if tr is None:
        return None
    bw = bandwidth_limited(tr, BW_LIMIT_HZ)
    return TransientResult(
        bandwidth_hz=float(bandwidth), step_a=float(tr.step_a), slew_a_per_s=p.slew_a_per_us * 1e6,
        t_rise_s=float(tr.t_rise_s), undershoot_v=tr.undershoot_v, overshoot_v=tr.overshoot_v,
        undershoot_bw_v=bw.undershoot_v, overshoot_bw_v=bw.overshoot_v, t_s=tr.t, i_a=tr.i, v=tr.v,
        v_bw=bw.v, notes=list(tr.notes), edge_l_h=tr.edge_l_h, edge_v=float(tr.edge_v),
        load_line_ohm=float(tr.load_line_ohm))


def _dielectric(board: Board, default_er: float) -> Tuple[str, str]:
    """(KiPI's dielectric summary, why εr is assumed or "")."""
    st = board.snapshot.stackup
    summary = dielectric_summary(st, default_er).replace(
        " [stackup edited in KiPI]", " [rebuilt for the copper_thickness_mm what-if]")
    why = []
    if not st.from_board:
        why.append("the stackup was not read from the board (KiPI's default stack)")
    names = {c.id: c.name for c in st.copper}
    defaulted = [f"{d.material or 'dielectric'} {d.thickness_mm * 1000:.0f} µm "
                 f"({names.get(d.above_layer, '?')}–{names.get(d.below_layer, '?')})"
                 for d in st.dielectrics if not d.epsilon_r > 0]
    if not st.dielectrics:
        why.append(f"the stackup lists no dielectrics, so {default_er:g} is used (ac.default_er)")
    elif defaulted:
        why.append(f"the stackup gives no εr for {', '.join(defaulted)}, so {default_er:g} is used "
                   "(ac.default_er)")
    return summary, "; ".join(why)


def _convert(board: Board, project: Project, c: _Context, res, thickness) -> ImpedanceResult:
    freqs = np.asarray(res.freqs, dtype=float)
    shown = c.bandwidth_hz if c.bandwidth_hz is not None else ASSUMED_BANDWIDTH_HZ
    by_name = {p.name: p for p in c.probes}
    probes, notes = [], []
    for curve in res.probes:
        p = by_name[curve.name]
        ohm, hz, pts, note = _target(p)
        t, fallback, judged = _target_curve(freqs, ohm, hz, pts, c.bandwidth_hz, shown)
        probes.append(ProbeImpedance(
            name=p.name, ref=p.ref, pins=p.pins, net=p.net, ground_net=p.ground_net,
            z_ohm=np.asarray(curve.z, dtype=complex), target_ohm=ohm, target_hz=hz,
            target_points=pts, target_curve_ohm=t, target_note=note, judged_from_hz=judged,
            fallback_note=fallback, step_a=p.step_a, slew_a_per_us=p.slew_a_per_us,
            transient=_transient(freqs, curve.z, c.bandwidth_hz, p)))
        if note:
            notes.append(note)
    summary, assumed = _dielectric(board, project.ac.default_er)
    out = ImpedanceResult(
        rail=c.rail, rail_nets=list(res.rail_nets), ground=res.ground, source=c.source.label,
        bandwidth_hz=c.bandwidth_hz, freq_hz=freqs, field_freq_hz=np.asarray(res.field_freqs, float),
        probes=probes, plane_capacitance_f=float(res.plane_capacitance_f), caps=list(res.caps),
        missing_caps=list(res.missing_caps), links=list(res.links),
        cap_derated_f={ref: float(farads) for ref, (_v, farads) in res.cap_bias.items()},
        warnings=list(dict.fromkeys(c.notes + list(res.warnings))),
        assumed_bandwidth_hz=None if c.bandwidth_hz is not None else ASSUMED_BANDWIDTH_HZ,
        dielectric_assumed=assumed)
    out.assumptions = _assumptions(board, project, c, res, summary, assumed, thickness) + notes
    # Task 11 (Q9): a load whose AC tolerance disagrees with its probe's ac_tolerance_v.
    pairs = load_probes(board, project)
    out.warnings += mismatch_warnings(project, pairs, {name: float(c.source.voltage) for name in pairs},
                                      list(by_name))
    return out


def _assumptions(board: Board, project: Project, c: _Context, res, summary: str, assumed: str,
                 thickness) -> List[str]:
    a, src, d = project.ac, c.source.label, ACSettings()
    out = ["AC model (KiPI): rail and ground copper as 2.5D sheets with two faces; facing copper "
           "gives the plane capacitance ε₀·εr/h·(1 − j·tanδ) and the loop inductance μ₀·h per "
           "square; plated barrels are resistors; capacitors, links and probes are lumped ports."]
    if c.bandwidth_hz is not None:
        out.append(f"Open-loop PDN impedance: the source {src} is left out of the AC model, so |Z| "
                   f"rises below its control loop bandwidth, {hz_text(c.bandwidth_hz)}; targets are "
                   "judged only above it.")
    else:
        out.append(f"Open-loop PDN impedance: the source {src} is left out of the AC model, so |Z| "
                   "rises below its control loop bandwidth, and no control loop bandwidth is set "
                   "for it (ac.bandwidth_hz): no target is judged and no worst point is given. "
                   f"The plot marks {hz_text(ASSUMED_BANDWIDTH_HZ)} (KiPI's bandwidth for a new "
                   "regulator) as an assumed bandwidth: below it, what the rail sees depends on "
                   "the regulator's real bandwidth. |Z| itself does not depend on it: KiPI's AC "
                   "model has no regulator impedance.")
    line = f"Dielectric εr, top to bottom: {summary}."
    if "no εr" in assumed:
        line += (f" Where the stackup gives no εr, {a.default_er:g} is used (ac.default_er"
                 f"{', KiPI' + chr(39) + 's default' if a.default_er == d.default_er else ''}).")
    out.append(line)
    out.append(f"Dielectric loss tangent {a.loss_tangent:g} (ac.loss_tangent; KiPI's default "
               f"{d.loss_tangent:g}).")
    n_field, n_sweep = len(res.field_freqs), len(res.freqs)
    if n_field == n_sweep:
        how = f"the copper is solved at every one of the {n_sweep} frequencies"
    else:
        per = f"{a.field_points_per_decade} per decade"
        if a.field_points_per_decade != d.field_points_per_decade:      # e.g. the app's quick preset
            per += f"; KiPI's default {d.field_points_per_decade}"
        how = (f"the copper is solved at {n_field} frequencies ({per}) and interpolated between "
               "them (PCHIP of jω·Z on log f)")
    out.append(f"Sweep {hz_text(a.f_start_hz)} to {hz_text(a.f_stop_hz)}, {a.points_per_decade} "
               f"points per decade; {how}.")
    out.append(f"AC mesh: largest edge {a.max_edge_mm:g} mm, refined toward plated holes near the "
               f"rail from {d.min_edge_mm:g} mm (grading {d.grading:g}), up to "
               f"{d.far_max_edge_mm:g} mm far from it (KiPI's AC defaults).")
    rail_text = " / ".join(res.rail_nets)
    line = (f"Capacitors: ESR + jω·ESL + 1/(jω·C) as entered, {len(res.caps)} modelled")
    if res.missing_caps:
        line += (f"; {len(res.missing_caps)} between {rail_text} and {res.ground} have no model "
                 f"and are left out: {', '.join(res.missing_caps)}")
    out.append(line + ".")
    out += _bias_lines(project, c, res)
    for k in c.setup.links:
        if k.name in res.links:
            esl = f" + jω·{k.esl_h:g} H" if k.esl_h else ""
            plain = "" if k.esl_h else " (no series inductance set: ac.link_esl_h)"
            out.append(f"Link {k.name}: {k.resistance:g} Ω{esl} between {k.side_a.net} and "
                       f"{k.side_b.net}{plain}.")
    inside = set(res.rail_nets) | set(res.ground.split(" + "))
    ties = [k.name[len("net tie "):] for k in c.setup.links if k.name.startswith("net tie ")
            and k.side_a.net in inside and k.side_b.net in inside]
    if ties:
        out.append("Net ties solved as shorts: " + ", ".join(ties) + ".")
    out.append(engine_settings(project.settings)[1][0])
    out += thickness_note(thickness, board.file_thickness_mm)
    if project.settings.copper_temp_c != REF_TEMP_C:
        out.append(f"Copper at {project.settings.copper_temp_c:g} °C (settings.copper_temp_c); "
                   "capacitor models with a dielectric follow KiPI's typical temperature curve.")
    return out


def _bias_lines(project: Project, c: _Context, res) -> List[str]:
    models = {ref: m for m in project.capacitors for ref in m.refs}
    rated = [ref for ref in res.caps if ref in models and models[ref].rated_v
             and models[ref].dielectric != "C0G"]
    if not rated:
        return []
    kept = sorted({models[r].kept_at_rated_pct for r in rated if models[r].kept_at_rated_pct is not None})
    typical = any(models[r].kept_at_rated_pct is None for r in rated)
    entered = ", ".join(f"{k:g}" for k in kept)
    if kept and typical:
        how = (f"{entered} % (entered) or {DEFAULT_KEPT_AT_RATED_PCT:g} % (KiPI's typical class II "
               "value) kept at the rated voltage")
    elif kept:
        how = f"{entered} % kept at the rated voltage (entered)"
    else:
        how = f"{DEFAULT_KEPT_AT_RATED_PCT:g} % kept at the rated voltage (KiPI's typical class II value)"
    return [f"DC bias: {len(rated)} capacitor(s) with a voltage rating derated at the {c.source.label} "
            f"set voltage, {abs(c.source.voltage):g} V: C/(1 + k·(V/V_rated)^1.5), k from {how}; a "
            "datasheet curve is better."]


# ---------------------------------------------------------------------- capacitor files

# KiPI model fields Coppernicus does not keep: an import says when it drops one.
_DROPPED_CAP_FIELDS = ("aging_pct_per_decade", "mfg_tolerance_pct", "spice")


def read_cap_file(path) -> Tuple[CapModel, List[str]]:
    """A KiPI capacitor model file (``kind: kipi-capacitor-model``; kipi/ac/cap_file.py) as a
    model with no parts, and a warning for each field it holds that Coppernicus drops."""
    try:
        m = cap_file.read(path)
    except (OSError, ValueError) as e:
        raise EngineError(f"{Path(path).name}: {e}") from None
    dropped = [k for k in _DROPPED_CAP_FIELDS if getattr(m, k)]
    warnings = [f"{Path(path).name}: {', '.join(dropped)} not used (Coppernicus keeps C, ESL, ESR, "
                "the voltage rating, the capacitance kept at it and the dielectric)"] if dropped else []
    return CapModel(m.name, float(m.capacitance_f), float(m.esl_h), float(m.esr_ohm), [],
                    m.rated_v, m.kept_at_rated_pct, m.dielectric or ""), warnings


def write_cap_file(path, model: CapModel) -> Path:
    """The model as a KiPI capacitor model file, without its parts (KiPI leaves them out)."""
    return cap_file.write(path, KipiCapModel(
        model.name, model.capacitance_f, model.esl_h, model.esr_ohm, [], model.rated_v,
        DEFAULT_KEPT_AT_RATED_PCT if model.kept_at_rated_pct is None else model.kept_at_rated_pct,
        model.dielectric))


@dataclass(frozen=True)
class CapCandidate:
    ref: str
    value: str
    nets: List[str]
    footprint: str
    capacitance_f: Optional[float]     # read from the value field; None if it can't be


def capacitor_candidates(board: Board, nets: Sequence[str]) -> List[CapCandidate]:
    """Capacitors (refs C<n>) with every pad on ``nets``, on at least two of them: the parts a
    model can be assigned to (kipi/ac/checks.py:28-44), each with its value read as farads."""
    return [CapCandidate(c.ref, c.value, list(c.nets), c.footprint, parse_capacitance(c.value))
            for c in _kipi_candidates(board.snapshot, list(nets))]


# ---------------------------------------------------------------------- cost and memory (brief Q7)

# Memory of the mesh, the model and one factorization with its solves, measured on
# ac_plane_board meshed at 2.0, 1.4, 1.0 and 0.7 mm with two frequencies solved one after the
# other (19 196 to 147 110 unknowns; 254 MB to 2.75 GB above an 85 MB process; SuperLU), fitted
# as bytes = MEM_FIT_A · unknowns ** MEM_FIT_B (within 1.2 % of each point). Measured on
# synthetic plane pairs only: other boards may need more or less.
MEM_FIT_A, MEM_FIT_B = 2466.6, 1.1706
MEM_FIT_UNKNOWNS = (19_196, 147_110)
# Unknowns per mesh triangle: 0.98 to 1.0 on those plane pairs.
UNKNOWNS_PER_TRIANGLE = 1.0


@dataclass(frozen=True)
class AcSize:
    """What an AC solve of a rail will hold, before it runs: KiPI's AC mesh of the rail and
    its ground, the ports (probes, modelled capacitors, links) and the frequencies."""
    rail: str
    triangles: int
    nodes: int
    ports: int
    field_frequencies: int
    sweep_points: int
    mesh_seconds: float

    @property
    def unknowns(self) -> int:
        """About one unknown per triangle (see UNKNOWNS_PER_TRIANGLE)."""
        return int(round(self.triangles * UNKNOWNS_PER_TRIANGLE))


def ac_size(board: Board, project: Project, rail: str,
            progress: Optional[Callable[[str], None]] = None,
            cancelled: Callable[[], bool] = lambda: False) -> AcSize:
    """Mesh the rail and its ground as the AC solve will (KiPI ``mesh_rail_and_ground``) and
    count what it will solve. Seconds on a large board; it checks the setup as the solve does."""
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    c = _context(board, project, rail)
    errors = validate_ac(c.setup, c.vrm, c.gnd)
    if errors:
        raise EngineError(f"AC solve of {c.rail}: Setup problems:\n- " + "\n- ".join(errors))
    vrm = _vrm_of(c.setup, c.vrm)
    t0 = time.perf_counter()
    try:
        on_rail, ports, shorts, *_rest = build_ports(board.snapshot, c.setup, c.vrm, c.gnd)
        grounds = [c.gnd, *sorted(ground_nets(c.setup, vrm, c.gnd) - {c.gnd})]
        terminals = list(dict.fromkeys([pid for p in ports for pid in (*p.plus_pads, *p.minus_pads)]
                                       + [pid for a, b in shorts for pid in (*a, *b)]))
        mesh, _kind = mesh_rail_and_ground(board.snapshot, sorted(on_rail), grounds, terminals,
                                           c.setup.settings, c.setup.ac,
                                           _AcProgress(progress, cancelled, None))
    except Canceled as e:
        raise Cancelled("cancelled") from e
    except (ACError, SolveError, MeshError) as e:
        raise EngineError(f"AC solve of {c.rail}: {e}") from e
    return AcSize(c.rail, int(mesh.n_tris), int(mesh.n_nodes), len(ports),
                  len(c.setup.ac.field_frequencies()), len(c.setup.ac.frequencies()),
                  time.perf_counter() - t0)


def _duration(seconds: float) -> str:
    if seconds < 1:
        return "under a second"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def _gb(n: float) -> str:
    return f"{n / 1e9:.3g} GB"


@dataclass(frozen=True)
class CostEstimate:
    triangles: int
    ports: int
    field_frequencies: int
    seconds: Optional[float]       # scaled from the last solve; None without one
    text: str

    @property
    def work(self) -> int:
        """Triangles × ports: the size of the field problem."""
        return self.triangles * self.ports


def _field_work(triangles: float, ports: float, frequencies: float) -> float:
    """KiPI's work of a factorization and its solves (kipi/core/eta.py:37-41) times the
    frequencies. KiPI's right-hand sides are n − n_free + ports (kipi/ac/solver/field.py:44);
    n_free is known only once the model is built, so the ports stand in for them."""
    return frequencies * factor_work(int(triangles * UNKNOWNS_PER_TRIANGLE), int(ports))


def cost_estimate(triangles: int, ports: int, field_frequencies: int,
                  last: Optional[Dict[str, Any]] = None) -> CostEstimate:
    """A rough cost of an AC solve before it starts: triangles × ports and the frequencies the
    copper is solved at, and, with ``last`` (the ``stats`` of an earlier AC solve on this
    computer), a time: its field solves (``field_seconds``) scaled with KiPI's work per
    frequency, n^1.5 + 5·n·rhs, the rest (mesh and model) in proportion to the triangles."""
    text = (f"{triangles:,} triangles × {ports} ports, the copper solved at {field_frequencies} "
            "frequencies")
    keys = ("seconds", "triangles", "ports", "field_frequencies")
    seconds = None
    if last and all(last.get(k) for k in keys):
        total = float(last["seconds"])
        field_s = last.get("field_seconds")
        field_s = float(field_s) if field_s is not None and math.isfinite(field_s) else total
        field_s = min(field_s, total)
        seconds = (field_s * _field_work(triangles, ports, field_frequencies)
                   / _field_work(last["triangles"], last["ports"], last["field_frequencies"])
                   + (total - field_s) * triangles / float(last["triangles"]))
        text += (f": about {_duration(seconds)}, scaled from the last solve ({total:.3g} s, "
                 f"{field_s:.3g} s of it field solves, for {int(last['triangles']):,} triangles × "
                 f"{int(last['ports'])} ports, {int(last['field_frequencies'])} frequencies): the "
                 "field solves with KiPI's work per frequency, n^1.5 + 5·n·rhs (its right-hand "
                 "sides are n − n_free + ports, taken as the ports before the model is built), the "
                 "mesh and model in proportion to the triangles")
    else:
        text += ": no earlier solve on this computer to time it from"
    return CostEstimate(int(triangles), int(ports), int(field_frequencies), seconds, text)


@dataclass(frozen=True)
class RamEstimate:
    bytes: float                   # one factorization with the mesh and the model
    total_bytes: Optional[float]   # this machine's memory; None when it can't be read
    risk: bool                     # one factorization exceeds the machine's memory
    text: str
    # The most KiPI's side-by-side frequencies may take on top: half the memory free
    # (kipi/ac/solver/field.py:68-82); None when not given.
    threads_bytes: Optional[float] = None


def ram_estimate(unknowns: float, total_bytes: Optional[float],
                 free_bytes: Optional[float] = None) -> RamEstimate:
    """Whether one factorization of an AC solve of ``unknowns`` could exceed this machine's
    memory, from the fit measured on synthetic plane pairs (MEM_FIT_A, MEM_FIT_B; other boards
    may need more or less). The run's peak is higher: after
    the first frequency KiPI solves others side by side, each with its own factorization, as
    many as fit in half the memory free (``free_bytes``, see :func:`free_memory_bytes`); that
    part is sized to the memory, so one factorization is the risk."""
    need = MEM_FIT_A * float(unknowns) ** MEM_FIT_B
    beyond = " (extrapolated beyond the measured range)" if unknowns > MEM_FIT_UNKNOWNS[1] else ""
    how = (f"estimated from a fit measured on synthetic plane pairs, {MEM_FIT_UNKNOWNS[0]:,} to "
           f"{MEM_FIT_UNKNOWNS[1]:,} unknowns{beyond}; other boards may need more or less")
    threads = MEMORY_SHARE * float(free_bytes) if free_bytes else None
    more = (" KiPI then solves further frequencies side by side, as many as fit in half the "
            "memory free" + (f" (up to {_gb(threads)} more here)" if threads else "")
            + ", so the run's peak is higher.")
    head = f"About {int(unknowns):,} unknowns: one factorization, with the mesh and the model, needs"
    if not total_bytes:
        return RamEstimate(need, None, False, f"{head} up to {_gb(need)} ({how}); this machine's "
                                              f"memory cannot be read.{more}", threads)
    if need > total_bytes:
        return RamEstimate(need, float(total_bytes), True, (
            f"{head} up to {_gb(need)} ({how}), more than this machine's {_gb(total_bytes)} of "
            f"memory, so it could swap: a coarser AC mesh (ac.max_edge_mm) lowers it.{more}"), threads)
    return RamEstimate(need, float(total_bytes), False,
                       f"{head} up to {_gb(need)} of this machine's {_gb(total_bytes)} ({how}).{more}",
                       threads)


def free_memory_bytes() -> Optional[int]:
    """The memory free now as KiPI reads it to size its threads (kipi/core/memory.py; on macOS
    it takes half of all memory)."""
    return free_memory()


def total_memory_bytes() -> Optional[int]:
    """This machine's physical memory in bytes; None where it can't be read."""
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status()
            st.dwLength = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
            return None
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, AttributeError):
        return None
