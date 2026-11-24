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
from coppernicus.project import Rail, Settings, pins_text
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
        f"Via plating: {plating}. Filled vias are solved unfilled unless the board says otherwise.",
        f"Copper at {s.copper_temp_c:g} °C: ρ = {rho_t:.4g} Ω·mm "
        f"(ρ₂₀ = {RHO_CU_OHM_MM:g} Ω·mm, α = {TCR_CU:g} /°C).",
        f"Mesh: largest edge {s.max_edge_mm:g} mm on planes, refined toward plated holes.",
        "Loads draw their current split equally across their pins; source pins are held at "
        "the source voltage.",
    ]
    return SolveSettings(max_edge_mm=s.max_edge_mm, via_rules=rules), assumptions


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


def solve_rail(board: Board, rail: Rail, settings: Optional[Settings] = None,
               progress: Optional[Callable[[str], None]] = None,
               cancelled: Callable[[], bool] = lambda: False) -> RailResult:
    settings = settings or Settings()
    if not rail.sources:
        raise EngineError(f"{rail.net}: add a source (the regulator output) before solving")
    if not rail.loads:
        raise EngineError(f"{rail.net}: add at least one load before solving")

    nets, ties = tied_nets(board, rail.net) if rail.follow_net_ties else ([rail.net], [])
    src_names = _unique([s.label for s in rail.sources])
    load_names = _unique([ld.label for ld in rail.loads])
    sources = [VoltageSource(name, [p.id for p in board.pads(nets, s.ref, s.pins)], s.voltage)
               for name, s in zip(src_names, rail.sources)]
    loads = [CurrentLoad(name, [p.id for p in board.pads(nets, ld.ref, ld.pins)], ld.current)
             for name, ld in zip(load_names, rail.loads)]
    links = [SeriesLink(f"net tie {t.ref}", [t.pad_a], [t.pad_b], 0.0) for t in ties]

    solve_settings, assumptions = engine_settings(settings)
    problem = NetProblem(net=" + ".join(nets), sources=sources, loads=loads,
                         settings=solve_settings, series=links, nets=nets)
    if settings.copper_temp_c != REF_TEMP_C:
        t_c = float(settings.copper_temp_c)
        problem.temperature = lambda xy: np.full(len(np.atleast_2d(xy)), t_c)
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

    out = _convert(board, rail, nets, result, src_names, load_names)
    out.assumptions = assumptions
    out.stats["seconds"] = seconds
    return out


def _convert(board: Board, rail: Rail, nets: List[str], result, src_names, load_names) -> RailResult:
    mesh, sol, fields = result.mesh, result.solution, result.fields

    layers = []
    for lid in mesh.layers:
        mask = mesh.tri_layer == lid
        if not mask.any():
            continue
        tris = mesh.tris[mask]
        used, local = np.unique(tris, return_inverse=True)
        layers.append(LayerField(
            layer_id=int(lid), name=board.layer_name(lid), points=mesh.points[used],
            tris=local.reshape(tris.shape).astype(np.int64), voltage=sol.node_v[used],
            current_density=fields.j_density[mask], tri_power=fields.tri_power[mask],
            tri_area=fields.tri_area[mask]))

    terms = {t.name: t for t in fields.terminals}
    refs = {**{n: s.ref for n, s in zip(src_names, rail.sources)},
            **{n: ld.ref for n, ld in zip(load_names, rail.loads)}}

    def terminal(name: str, kind: str) -> TerminalResult:
        t = terms[name]
        return TerminalResult(name, kind, refs[name], float(t.current), float(t.v_min),
                              float(t.v_max), list(t.pad_ids),
                              [tuple(board.pad(pid).position) for pid in t.pad_ids])

    vias = [ViaCurrent(v.owner_id, v.kind, tuple(v.position), v.drill_mm, float(v.current),
                       float(v.power)) for v in fields.vias]
    sm = fields.summary
    warnings = list(sol.warnings) + [
        f"Load {name}: pins with no copper path to a source draw nothing: {', '.join(pads)}"
        for name, pads in sol.dead_pads.items()]
    return RailResult(
        net=rail.net, nets=list(nets), v_ref=float(fields.v_ref), layers=layers,
        sources=[terminal(n, "source") for n in src_names],
        loads=[terminal(n, "load") for n in load_names],
        vias=vias, max_drop=float(sm["max_drop"]), j_max=float(sm["j_max"]),
        power_loss=float(sm["power_loss"]),
        energy_balance_error=float(sm["energy_balance_error"]),
        loss_vias=float(sum(v.power for v in fields.vias)),
        loss_series=float(sum(s.power for s in fields.series if np.isfinite(s.power))),
        stats={"triangles": float(sol.stats["n_tris"]), "unknowns": float(sol.stats["n_unknowns"])},
        warnings=warnings,
    )


def describe_pins(rail: Rail) -> List[str]:
    """One line per terminal, for logs."""
    return ([f"source {s.label}: {s.ref} pins {pins_text(s.pins)} at {s.voltage:g} V" for s in rail.sources]
            + [f"load {ld.label}: {ld.ref} pins {pins_text(ld.pins)} drawing {ld.current:g} A"
               for ld in rail.loads])
