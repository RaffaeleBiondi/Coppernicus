"""Solve a project's whole power tree with KiPI's tree solver, ground return included, and
convert the answer to :mod:`coppernicus.results`.

Physics (KiPI ``kipi/dc/pi/tree.py``): each net group (nets joined by links and net ties) is
one FEM problem driven by its one source; groups are solved downstream first, a regulator's
input pins being a load on the group feeding it (LDO: I_in = I_out + I_q; switcher:
I_in = V_out·I_out / (η·V_in) + I_q, both across its own pins). Ground nets are solved with
the loads' and regulators' return currents, referenced to 0 V at the ground pins of the
sources; rails and grounds are iterated together. Field by field: ``docs/engine-mapping.md``.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional

import numpy as np
from kipi.core.progress import Canceled
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import terminal_names, to_pisetup
from coppernicus.engine.solve import Cancelled, _Progress, engine_settings, mesh_fields
from coppernicus.power_tree import judge_loads
from coppernicus.project import Project, pin_ref, pins_text
from coppernicus.results import (
    LoadReport,
    RailResult,
    TerminalResult,
    TreeResult,
    VrmReport,
)

REGULATING = ("ldo", "smps")


def _ground_nets(project: Project) -> List[str]:
    """Ground nets the project names, in order (sources' ground and ground-sense pins, then
    loads' ground pins)."""
    out: List[str] = []
    for r in project.rails:
        for s in r.sources:
            out += [n for n in (s.ground_net, s.ground_sense_net) if n]
        out += [ld.ground_net for ld in r.loads if ld.ground_net]
    return list(dict.fromkeys(out))


def check_grounds(project: Project) -> None:
    """Once a ground net is solved, every load and regulator needs ground pins: otherwise its
    return current would not flow in the ground copper. Plain sources may go without."""
    excluded = set(project.engine_options.get("excluded_nets", []) or [])
    solved = [n for n in _ground_nets(project) if n not in excluded]
    if not solved:
        return
    missing = []
    for r in project.rails:
        if r.net in excluded or not (r.sources or r.loads):
            continue
        missing += [f"source {s.label} ({s.kind}) on {r.net}" for s in r.sources
                    if s.kind in REGULATING and not s.ground_net]
        missing += [f"load {ld.label} on {r.net}" for ld in r.loads if not ld.ground_net]
    if missing:
        raise EngineError(
            f"Ground net {', '.join(solved)} is solved, but {'; '.join(missing)} "
            f"{'has' if len(missing) == 1 else 'have'} no ground pins, so the return current "
            "would not flow in the ground copper: add its ground pins (ground) or exclude the "
            "ground net (engine_options: excluded_nets)")


def _assumptions(project: Project, setup) -> List[str]:
    settings_notes = engine_settings(project.settings)[1][:3]      # plating, ρ(T), mesh
    out = settings_notes + [
        "Power tree: each net group (nets joined by links and net ties) is solved from its one "
        "source. A regulator's input pins are a load on the group feeding it: LDO "
        "I_in = I_out + I_q; switcher I_in = V_out·I_out / (η·V_in) + I_q, with V_out and V_in "
        "across its own pins, from its ground pins.",
        "Loads draw their current split equally across their supply pins and return it "
        "through their ground pins; a regulator returns I_out − I_in at its ground pins.",
        "A load's drop counts its ground: nominal − (lowest supply pin − highest ground pin).",
    ]
    refs = [v.name for v in setup.vrms if v.kind == "source" and v.ground]
    if refs:
        out.append(f"Ground nets are held at 0 V at the ground pins of {', '.join(refs)}; every "
                   "other ground voltage is a rise above that reference.")
    for r in project.rails:
        for s in r.sources:
            sense = pin_ref(s, "sense")
            if sense:
                gs = pin_ref(s, "ground_sense")
                above = f", above {gs[0]} pins {pins_text(gs[1])} on {gs[2]}" if gs else ""
                out.append(f"{s.label} holds {s.voltage:g} V at {sense[0]} pins "
                           f"{pins_text(sense[1])} on {sense[2]} (remote sense){above}.")
    for k in project.links:
        out.append(f"Link {k.name}: {k.resistance_ohm:g} Ω between {k.net_a} and {k.net_b}.")
    ties = [k.name for k in setup.links if k.name.startswith("net tie ")]
    if ties:
        out.append("Net ties solved as shorts: " + ", ".join(t[len("net tie "):] for t in ties) + ".")
    return out


def _terminals(board: Board, fields):
    sources, loads = [], []
    for t in fields.terminals:
        pads = list(t.pad_ids)
        ref = board.pad(pads[0]).footprint if pads else ""
        out = TerminalResult(t.name, t.kind, ref, float(t.current), float(t.v_min), float(t.v_max),
                             pads, [tuple(board.pad(pid).position) for pid in pads])
        (sources if t.kind == "source" else loads).append(out)
    return sources, loads


def _group(board: Board, key: str, g, assumptions: List[str]) -> RailResult:
    result = g.result
    sol, fields = result.solution, result.fields
    layers, vias = mesh_fields(board, result)
    sources, loads = _terminals(board, fields)
    sm = fields.summary
    warnings = list(sol.warnings)
    if not g.converged:
        warnings.append(f"{key}: the iteration did not converge; its loads are not judged")
    return RailResult(
        net=key, nets=[key] + sorted(n for n in g.nets if n != key), v_ref=float(fields.v_ref),
        layers=layers, sources=sources, loads=loads, vias=vias,
        max_drop=float(sm["max_boost"] if g.kind == "ground" else sm["max_drop"]),
        j_max=float(sm["j_max"]), power_loss=float(sm["power_loss"]),
        energy_balance_error=float(sm["energy_balance_error"]),
        loss_vias=float(sum(v.power for v in fields.vias)),
        loss_series=float(sum(s.power for s in fields.series if np.isfinite(s.power))),
        stats={"triangles": float(sol.stats["n_tris"]), "unknowns": float(sol.stats["n_unknowns"]),
               "iterations": float(g.iterations)},
        warnings=warnings, assumptions=list(assumptions), kind=g.kind)


def solve_project_tree(board: Board, project: Project,
                       progress: Optional[Callable[[str], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False) -> TreeResult:
    """Every rail and ground group of the project, solved together (KiPI ``solve_tree``)."""
    check_grounds(project)
    setup = to_pisetup(board, project)
    temperature = None
    if project.settings.copper_temp_c != REF_TEMP_C:
        t_c = float(project.settings.copper_temp_c)
        temperature = lambda xy: np.full(len(np.atleast_2d(xy)), t_c)   # noqa: E731
    t0 = time.perf_counter()
    try:
        kt = solve_tree(board.snapshot, setup, _Progress(progress, cancelled), temperature=temperature)
    except Canceled as e:
        raise Cancelled("solve cancelled") from e
    except SolveError as e:
        raise EngineError(f"Power tree: {e}") from e
    seconds = time.perf_counter() - t0

    names = terminal_names(project)
    rail_of_vrm = {name: net for (what, net, _i), name in names.items() if what == "source"}
    grounds = _ground_nets(project)
    key_of: Dict[str, str] = {}
    for label, g in kt.groups.items():
        if g.kind == "rail":
            key_of[label] = rail_of_vrm.get(g.driver, label)
        else:
            key_of[label] = next((n for n in grounds if n in g.nets), sorted(g.nets)[0])

    assumptions = _assumptions(project, setup)
    out = TreeResult(assumptions=assumptions, warnings=list(kt.warnings),
                     stats={"seconds": seconds, "groups": float(len(kt.groups))})
    for label, g in kt.groups.items():
        out.groups[key_of[label]] = _group(board, key_of[label], g, assumptions)
        rail = project.rail(key_of[label])
        if rail is not None:
            out.limits[key_of[label]] = rail.limits
        for s in g.result.fields.series:
            k = next((k for k in setup.links if k.name == s.name), None)
            if k is not None:
                out.links.append({"name": s.name, "group": key_of[label],
                                  "nets": [k.side_a.net, k.side_b.net],
                                  "resistance_ohm": float(k.resistance),
                                  "current_a": float(s.current), "power_w": float(s.power)})

    for v in kt.vrms:
        out.vrms.append(VrmReport(
            name=v.name, kind=v.kind, group=key_of.get(v.group, v.group), v_set_v=float(v.v_set),
            v_out_v=float(v.v_out), i_out_a=float(v.i_out), v_in_v=v.v_in, i_in_a=v.i_in,
            p_out_w=float(v.p_out), p_in_w=v.p_in, p_loss_w=v.p_loss, v_ground_v=v.v_ground,
            v_sense_v=v.v_sense, v_ground_sense_v=v.v_ground_sense, regulation=v.regulation))

    for ld in kt.loads:
        key = key_of.get(ld.group, ld.group)
        v_worst = ld.v_low if ld.v_low is not None else ld.v_worst
        drop = ld.nominal - v_worst
        pct = 100.0 * drop / abs(ld.nominal) if ld.nominal else math.nan
        across = ld.v_effective
        out.loads.append(LoadReport(
            name=ld.name, group=key, current_a=float(ld.current), nominal_v=float(ld.nominal),
            v_supply_v=float(ld.v_supply), v_supply_min_v=float(ld.v_supply_min),
            v_ground_v=ld.v_ground, v_across_v=float(across), v_worst_v=float(v_worst),
            drop_v=float(drop), drop_pct=float(pct), status=ld.status))
    judge_loads(out)
    return out
