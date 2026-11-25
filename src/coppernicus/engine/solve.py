"""Solve one rail with KiPI's DC engine and convert the answer to :mod:`coppernicus.results`.

Physics (KiPI ``kipi/dc/solver/fem.py``): 2.5D P1 finite elements on every copper sheet of
the rail's net, plated barrels as resistors between equipotential drill rings, source pads
held at their voltage (Dirichlet), each load's current split equally across its pads.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from kipi.core.progress import Canceled, ProgressReporter
from kipi.dc.solver import (
    CurrentLoad,
    NetProblem,
    SeriesLink,
    SolveError,
    SolveSettings,
    VoltageSource,
    run,
)
from kipi.dc.solver.setup import REF_TEMP_C, RHO_CU_OHM_MM
from kipi.dc.solver.via_barrel import ViaRule, default_via_rules

from coppernicus.engine.board import Board, EngineError, NetTie
from coppernicus.power_tree import EstimateError, estimate_note, estimated_draws
from coppernicus.problems import density_default_note
from coppernicus.project import SOURCE_PIN_REFS, Project, Rail, Settings, pins_text
from coppernicus.results import LayerField, RailResult, TerminalResult, ViaCurrent

TCR_CU = 0.00393     # per °C, copper; KiPI's default (kipi/dc/solver/setup.py)


class _Progress(ProgressReporter):
    """Forwards the engine's messages to a callback (status bar, log, CLI)."""

    def __init__(self, callback: Optional[Callable[[str], None]], cancelled: Callable[[], bool]):
        super().__init__()
        self._callback = callback
        self._cancelled = cancelled

    def message(self, text, level=20):
        if self._callback is not None and level >= 20:
            self._callback(text)
        if self._cancelled():
            self.cancel()

    def check_cancel(self):
        if self._cancelled():
            self.cancel()
        super().check_cancel()


class Cancelled(RuntimeError):
    pass


def engine_settings(s: Settings) -> Tuple[SolveSettings, List[str]]:
    """KiPI solve settings for ours, and the assumptions to show next to the result."""
    if s.via_plating_um is None:
        rules = default_via_rules()
        plating = "engine default table (IPC-6012 Class 2 minimum average): " + "; ".join(
            f"{r.kind} {r.plating_mm * 1000:g} µm" for r in rules if r.kind != "any")
    else:
        rules = [ViaRule("any", s.via_plating_um / 1000.0)]
        plating = f"{s.via_plating_um:g} µm for every plated hole (user setting)"
    rho_t = RHO_CU_OHM_MM * (1 + TCR_CU * (s.copper_temp_c - REF_TEMP_C))
    assumptions = [
        f"Via plating: {plating}. Via fill is counted as non-conductive: barrels conduct through "
        "their plated wall only.",
        f"Copper at {s.copper_temp_c:g} °C: ρ = {rho_t:.4g} Ω·mm "
        f"(ρ₂₀ = {RHO_CU_OHM_MM:g} Ω·mm, α = {TCR_CU:g} /°C).",
        f"Mesh: largest edge {s.max_edge_mm:g} mm on planes, refined toward plated holes.",
        "Loads draw their current split equally across their pins; source pins are held at "
        "the source voltage.",
    ]
    return SolveSettings(max_edge_mm=s.max_edge_mm, via_rules=rules), assumptions


def thickness_note(thickness: Dict[str, float], file_mm: Dict[str, float]) -> List[str]:
    """The assumption naming the copper layers solved at a what-if thickness (the
    ``copper_thickness_mm`` setting), each with the board file's value; empty without one."""
    if not thickness:
        return []
    layers = ", ".join(f"{name} {mm:g} mm (board file: {file_mm[name]:g} mm)" if name in file_mm
                       else f"{name} {mm:g} mm" for name, mm in sorted(thickness.items()))
    return [f"What-if copper thickness (the copper_thickness_mm setting): {layers}. The board "
            "file is not changed; real changes of copper weight are made in KiCad. The stack is "
            "rebuilt around the new thickness, so via barrels get longer when the stack thickens "
            "(shorter when it thins): even a net with no copper on those layers changes slightly "
            "through its vias."]


def density_note(board: Board) -> List[str]:
    """The current-density default's assumption line, with every copper layer's value at its
    thickness as solved (``board``: the what-if copy when there is one)."""
    outer = board.outer_layer_ids()
    return [density_default_note([(lay.name, lay.thickness_mm, lay.id in outer) for lay in board.layers()])]


def tree_only_fields(rail: Rail) -> List[str]:
    """An assumption naming the rail's fields that only a power-tree solve uses (regulator
    kind, input, ground and sense pins, loads' ground pins); empty for a v1 rail."""
    parts = []
    for s in rail.sources:
        what = [f"kind {s.kind}"] if s.kind != "source" else []
        what += [f"{w.replace('_', ' ')} pins" for w in SOURCE_PIN_REFS if getattr(s, f"{w}_net")]
        if what:
            parts.append(f"source {s.label} ({', '.join(what)})")
    grounded = [ld.label for ld in rail.loads if ld.ground_net]
    if grounded:
        parts.append(f"the ground pins of {', '.join(grounded)}")
    if not parts:
        return []
    return ["Solved as one rail: each source holds its own pins at its voltage and no ground net "
            f"is solved. Used only by the power-tree solve: {'; '.join(parts)}."]


def tied_nets(board: Board, net: str) -> Tuple[List[str], List[NetTie]]:
    """``net`` plus every net reachable from it through net ties, and the ties used."""
    ties = board.net_ties()
    nets, used, grew = [net], [], True
    while grew:
        grew = False
        for t in ties:
            if t in used:
                continue
            if t.net_a in nets or t.net_b in nets:
                used.append(t)
                for n in (t.net_a, t.net_b):
                    if n not in nets:
                        nets.append(n)
                        grew = True
    return nets, used


def _unique(names: List[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


def source_pad_ids(board: Board, nets, source) -> List[str]:
    """The pads a source holds at its voltage: its own (``ref``, ``pins``) and, when set, those
    of ``extra_pins`` ("REF.PAD", the pad may be a glob): one terminal on several parts (cell
    holders in parallel). Without ``extra_pins``, exactly ``board.pads(nets, ref, pins)``."""
    ids = [p.id for p in board.pads(nets, source.ref, source.pins)]
    by_ref: Dict[str, List[str]] = {}
    for entry in getattr(source, "extra_pins", None) or ():
        ref, _, pad = entry.partition(".")
        by_ref.setdefault(ref, []).append(pad)
    for ref, pins in by_ref.items():
        ids += [p.id for p in board.pads(nets, ref, pins) if p.id not in ids]
    return ids


def solve_rail(board: Board, rail: Rail, settings: Optional[Settings] = None,
               progress: Optional[Callable[[str], None]] = None,
               cancelled: Callable[[], bool] = lambda: False,
               project: Optional[Project] = None) -> RailResult:
    """Solve one rail on its own. With ``project``, the rail also delivers the currents of
    the regulator inputs and links it feeds, estimated at nominal voltages
    (:func:`coppernicus.power_tree.estimated_draws`); without it, only its own loads."""
    settings = settings or Settings()
    if not rail.sources:
        raise EngineError(f"{rail.net}: add a source (the regulator output) before solving")
    thickness = dict(settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board

    nets, ties = tied_nets(board, rail.net) if rail.follow_net_ties else ([rail.net], [])
    try:
        ties_all = [(t.net_a, t.net_b) for t in board.net_ties()] if project is not None else []
        draws = estimated_draws(project, nets, ties_all) if project is not None else []
    except EstimateError as e:
        raise EngineError(str(e)) from None
    if not rail.loads and not draws:
        raise EngineError(f"{rail.net}: add at least one load before solving")
    sign = load_sign(rail)
    src_names = _unique([s.label for s in rail.sources])
    names = _unique([ld.label for ld in rail.loads] + [d.name for d in draws])
    load_names = names[:len(rail.loads)]
    sources = [VoltageSource(name, source_pad_ids(board, nets, s), s.voltage)
               for name, s in zip(src_names, rail.sources)]
    # KiPI: amps leaving the copper at the pads. A load on a negative rail or on a net at 0 V
    # returns its current into the copper, as in the power tree (#220).
    loads = [CurrentLoad(name, [p.id for p in board.pads(nets, ld.ref, ld.pins)], sign * ld.current)
             for name, ld in zip(load_names, rail.loads)]
    for name, d in zip(names[len(rail.loads):], draws):
        try:
            pad_ids = [p.id for p in board.pads([d.net], d.ref, d.pins)]
        except EngineError as e:
            raise EngineError(f"{d.what}: {e}") from None
        for ld, spec in zip(loads, rail.loads):
            if set(ld.pad_ids) & set(pad_ids):
                what = "the link" if "(link," in d.name else "the VRM input"
                raise EngineError(
                    f"Load {spec.label} on {rail.net} draws from pins that also carry {d.what}: "
                    f"its current would be counted twice; remove the load or {what}")
        loads.append(CurrentLoad(name, pad_ids, sign * d.current_a))
    links = [SeriesLink(f"net tie {t.ref}", [t.pad_a], [t.pad_b], 0.0) for t in ties]

    solve_settings, assumptions = engine_settings(settings)
    problem = NetProblem(net=" + ".join(nets), sources=sources, loads=loads,
                         settings=solve_settings, series=links, nets=nets)
    if settings.copper_temp_c != REF_TEMP_C:
        t_c = float(settings.copper_temp_c)
        problem.temperature = lambda xy: np.full(len(np.atleast_2d(xy)), t_c)
    assumptions += tree_only_fields(rail) + estimate_note(draws) + sign_note(rail, sign)
    assumptions += thickness_note(thickness, board.file_thickness_mm) + density_note(board)
    if project is not None and project.thermal.enabled:      # coordinator's ruling (T8)
        assumptions.append("Copper not heated by its own losses here: the board temperature is "
                           "solved with the power tree only (Ctrl+T / solve --tree).")
    if ties:
        assumptions.append("Net ties solved as shorts: " + ", ".join(
            f"{t.ref} ({t.net_a} ↔ {t.net_b})" for t in ties) + ".")

    t0 = time.perf_counter()
    try:
        result = run(board.snapshot, problem, _Progress(progress, cancelled))
    except Canceled as e:
        raise Cancelled("solve cancelled") from e
    except SolveError as e:
        raise EngineError(str(e)) from e
    seconds = time.perf_counter() - t0

    out = _convert(board, rail, nets, result, src_names, names,
                   extra_refs={n: d.ref for n, d in zip(names[len(rail.loads):], draws)}, sign=sign)
    out.assumptions = assumptions
    out.stats["seconds"] = seconds
    return out


def mesh_fields(board: Board, result) -> Tuple[List[LayerField], List[ViaCurrent]]:
    """The solved copper of one KiPI net result, per layer, and its via currents."""
    mesh, sol, fields = result.mesh, result.solution, result.fields
    copper = {lay.id: lay for lay in board.layers()}         # as solved: a what-if board's
    outer = board.outer_layer_ids()
    layers = []
    for lid in mesh.layers:
        mask = mesh.tri_layer == lid
        if not mask.any():
            continue
        tris = mesh.tris[mask]
        used, local = np.unique(tris, return_inverse=True)
        lay = copper.get(int(lid))
        layers.append(LayerField(
            layer_id=int(lid), name=board.layer_name(lid), points=mesh.points[used],
            tris=local.reshape(tris.shape).astype(np.int64), voltage=sol.node_v[used],
            current_density=fields.j_density[mask], tri_power=fields.tri_power[mask],
            tri_area=fields.tri_area[mask],
            thickness_mm=float(lay.thickness_mm) if lay is not None else None,
            outer=int(lid) in outer if lay is not None else None))
    vias = [ViaCurrent(v.owner_id, v.kind, tuple(v.position), v.drill_mm, float(v.current),
                       float(v.power)) for v in fields.vias]
    return layers, vias


def load_sign(rail: Rail) -> float:
    """Which way a rail's load current flows, from its sources' set voltages: +1 out of the
    copper (all above 0 V), -1 into it (all at or below 0 V: a negative rail, or a net at
    0 V, a ground, whose loads return their current into it). Raises for sources on both
    sides of 0 V."""
    up = [s.label for s in rail.sources if s.voltage > 0]
    down = [s.label for s in rail.sources if s.voltage <= 0]
    if up and down:
        raise EngineError(f"{rail.net}: sources {', '.join(up)} (above 0 V) and {', '.join(down)} "
                          "(at or below 0 V) drive the same rail: set them all on one side of 0 V")
    return -1.0 if down else 1.0


def sign_note(rail: Rail, sign: float) -> List[str]:
    """The assumption line for a rail whose loads return their current into the copper."""
    if sign > 0:
        return []
    if all(s.voltage == 0 for s in rail.sources):
        return [f"{rail.net} is driven at 0 V: its loads return their current into the copper "
                "(a ground), so it rises above 0 V toward them."]
    return [f"{rail.net} is a negative rail: its loads' current flows into the copper, so the "
            "voltage at the loads moves toward 0 V; drops are measured toward 0 V."]


def _convert(board: Board, rail: Rail, nets: List[str], result, src_names, load_names,
             extra_refs: Optional[Dict[str, str]] = None, sign: float = 1.0) -> RailResult:
    sol, fields = result.solution, result.fields
    layers, vias = mesh_fields(board, result)
    terms = {t.name: t for t in fields.terminals}
    refs = {**{n: s.ref for n, s in zip(src_names, rail.sources)},
            **{n: ld.ref for n, ld in zip(load_names, rail.loads)}, **(extra_refs or {})}

    def terminal(name: str, kind: str) -> TerminalResult:
        t = terms[name]        # current as drawn (load) or delivered (source), whatever the sign
        return TerminalResult(name, kind, refs[name], float(sign * t.current), float(t.v_min),
                              float(t.v_max), list(t.pad_ids),
                              [tuple(board.pad(pid).position) for pid in t.pad_ids])

    sm = fields.summary
    warnings = list(sol.warnings) + [
        f"Load {name}: pins with no copper path to a source draw nothing: {', '.join(pads)}"
        for name, pads in sol.dead_pads.items()]
    return RailResult(
        net=rail.net, nets=list(nets), v_ref=float(fields.v_ref), layers=layers,
        sources=[terminal(n, "source") for n in src_names],
        loads=[terminal(n, "load") for n in load_names],
        vias=vias, max_drop=float(sm["max_boost"] if sign < 0 else sm["max_drop"]),
        j_max=float(sm["j_max"]),
        power_loss=float(sm["power_loss"]),
        energy_balance_error=float(sm["energy_balance_error"]),
        loss_vias=float(sum(v.power for v in fields.vias)),
        loss_series=float(sum(s.power for s in fields.series if np.isfinite(s.power))),
        stats={"triangles": float(sol.stats["n_tris"]), "unknowns": float(sol.stats["n_unknowns"])},
        warnings=warnings, drop_sign=sign,
    )


def describe_pins(rail: Rail) -> List[str]:
    """One line per terminal, for logs."""
    return ([f"source {s.label}: {s.ref} pins {pins_text(s.pins)} at {s.voltage:g} V" for s in rail.sources]
            + [f"load {ld.label}: {ld.ref} pins {pins_text(ld.pins)} drawing {ld.current:g} A"
               for ld in rail.loads])
