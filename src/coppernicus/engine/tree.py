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
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from kipi.core.progress import Canceled
from kipi.dc.pi.model import active_setup
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import ground_nets, ground_rails, terminal_names, to_pisetup
from coppernicus.engine.solve import Cancelled, _Progress, engine_settings, mesh_fields, thickness_note
from coppernicus.power_tree import LIMITS_RULE, apply_limits
from coppernicus.project import Project, Source, pin_ref, pins_text
from coppernicus.results import (
    DISCONNECTED,
    NOT_CONVERGED,
    OUT_OF_REGULATION,
    LoadReport,
    RailResult,
    TerminalResult,
    TreeResult,
    VrmReport,
)

REGULATING = ("ldo", "smps")


def check_grounds(project: Project) -> None:
    """Once a ground net is solved, every load and regulator needs ground pins: otherwise its
    return current would not flow in the ground copper. Plain sources may go without."""
    excluded = set(project.engine_options.get("excluded_nets", []) or [])
    grounds = ground_nets(project)
    solved = [n for n in grounds if n not in excluded]
    if not solved:
        return
    missing = []
    for r in project.rails:
        if r.net in excluded or r.net in grounds or not (r.sources or r.loads):
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


def _assumptions(project: Project, setup, rho_note: Optional[List[str]] = None) -> List[str]:
    # Only what KiPI solves: excluded groups dropped, a regulator fed from one an ideal source.
    setup = active_setup(setup)[0]
    names = terminal_names(project)
    vrms = {v.name: v for v in setup.vrms}
    links = {k.name for k in setup.links}
    settings_notes = engine_settings(project.settings)[1][:3]      # plating, ρ(T), mesh
    if rho_note is not None:                   # electro-thermal: the copper's own temperature
        settings_notes[1:2] = rho_note
    out = settings_notes + [
        "Power tree: each net group (nets joined by links and net ties) is solved from its one "
        "source. A regulator's input pins are a load on the group feeding it: LDO "
        "I_in = I_out + I_q; switcher I_in = V_out·I_out / (η·V_in) + I_q, with V_out and V_in "
        "across its own pins, from its ground pins.",
        "Loads draw their current split equally across their supply pins and return it "
        "through their ground pins; a regulator returns I_out − I_in at its ground pins.",
        "A load's drop counts its ground: nominal − (lowest supply pin − highest ground pin).",
        LIMITS_RULE,
    ]
    refs = [v.name for v in setup.vrms if v.kind == "source" and v.ground]
    if refs:
        out.append(f"Ground nets are held at 0 V at the ground pins of {', '.join(refs)}; every "
                   "other ground voltage is a rise above that reference.")
    for r in project.rails:
        for i, s in enumerate(r.sources):
            vrm = vrms.get(names.get(("source", r.net, i)))
            sense = pin_ref(s, "sense") if vrm is not None else None
            if sense:
                gs = pin_ref(s, "ground_sense") if vrm.ground_sense is not None else None
                above = f", above {gs[0]} pins {pins_text(gs[1])} on {gs[2]}" if gs else ""
                out.append(f"{s.label} holds {s.voltage:g} V at {sense[0]} pins "
                           f"{pins_text(sense[1])} on {sense[2]} (remote sense){above}.")
    for i, k in enumerate(project.links):
        if names.get(("link", "", i)) in links:
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
                             pads, [tuple(board.pad(pid).position) for pid in pads],
                             net=board.pad(pads[0]).net if pads else "")
        (sources if t.kind == "source" else loads).append(out)
    return sources, loads


def _worst(kind: str, v_ref: float, sm) -> float:
    """A group's largest drop. On a negative rail the voltage lost toward 0 V is above its
    reference: KiPI's "boost". A ground's rise is the one with the largest magnitude, signed:
    above 0 V under positive rails, below it under negative ones."""
    up, down = float(sm["max_boost"]), float(sm["max_drop"])
    if kind == "ground":
        return up if up >= down else -down
    return up if v_ref < 0 else down


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
        max_drop=_worst(g.kind, float(fields.v_ref), sm),
        j_max=float(sm["j_max"]), power_loss=float(sm["power_loss"]),
        energy_balance_error=float(sm["energy_balance_error"]),
        loss_vias=float(sum(v.power for v in fields.vias)),
        loss_series=float(sum(s.power for s in fields.series if np.isfinite(s.power))),
        stats={"triangles": float(sol.stats["n_tris"]), "unknowns": float(sol.stats["n_unknowns"]),
               "iterations": float(g.iterations)},
        warnings=warnings, assumptions=list(assumptions), kind=g.kind, converged=bool(g.converged),
        # KiPI's sign (#220): a negative rail's loads, and a ground's returns, put current in;
        # a rail at 0 V draws, as a positive one.
        drop_sign=-1.0 if g.kind == "ground" or fields.v_ref < 0 else 1.0)


def solve_project_tree(board: Board, project: Project,
                       progress: Optional[Callable[[str], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False,
                       thermal: Optional[bool] = None) -> TreeResult:
    """Every rail and ground group of the project, solved together (KiPI ``solve_tree``).
    With ``thermal`` (``None``: the project's ``thermal.enabled``) the tree is solved
    electro-thermally and the result carries the board temperature
    (:mod:`coppernicus.engine.thermal`)."""
    check_grounds(project)
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    setup = to_pisetup(board, project)
    if project.thermal.enabled if thermal is None else thermal:
        from coppernicus.engine.thermal import solve_thermal_tree
        return solve_thermal_tree(board, project, setup, thickness, progress, cancelled)
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
    return convert_tree(board, project, setup, kt, seconds, thickness)


def group_keys(project: Project, kt) -> Dict[str, str]:
    """KiPI's group label -> the ``TreeResult.groups`` key: a rail group's driving net (the
    net its source drives), a ground group's first ground net the project names."""
    names = terminal_names(project)
    rail_of_vrm = {name: net for (what, net, _i), name in names.items() if what == "source"}
    grounds = ground_nets(project)
    key_of: Dict[str, str] = {}
    for label, g in kt.groups.items():
        if g.kind == "rail":
            key_of[label] = rail_of_vrm.get(g.driver, label)
        else:
            key_of[label] = next((n for n in grounds if n in g.nets), sorted(g.nets)[0])
    return key_of


def load_drop(ld) -> Tuple[float, float]:
    """``(drop, v_worst)`` of a KiPI tree load: the pin that has lost the most toward 0 V
    (the lowest on a positive rail, the highest on a negative one, #220) and how far it is
    from the nominal, toward 0 V."""
    sign = -1.0 if ld.nominal < 0 else 1.0
    side = ld.v_high if sign < 0 else ld.v_low
    v_worst = side if side is not None else ld.v_worst
    return sign * (ld.nominal - v_worst), v_worst


def convert_tree(board: Board, project: Project, setup, kt, seconds: float,
                 thickness: Dict[str, float], rho_note: Optional[List[str]] = None) -> TreeResult:
    """KiPI's tree result ``kt`` as a :class:`TreeResult`, judged against the project's
    limits. ``rho_note`` replaces the copper-temperature assumption (an electro-thermal
    solve sets the copper temperature)."""
    names = terminal_names(project)
    key_of = group_keys(project, kt)

    assumptions = _assumptions(project, setup, rho_note)
    assumptions += thickness_note(thickness, board.file_thickness_mm)
    excluded = set(setup.excluded_nets or [])
    notes = [f"Rail {r.net} is solved as a ground in the power tree (from the ground pins); its "
             f"own sources and loads are used only when {r.net} is solved alone."
             for r in ground_rails(project) if r.net not in excluded]
    notes += [f"{net} is solved as a 0 V rail (no ground pins named): KiPI draws its loads' current "
              "out of it, so its voltages are shown with the opposite sign to a ground's; name "
              "ground pins on it to solve it as a ground."
              for label, g in kt.groups.items() if g.kind == "rail"
              for net in [key_of[label]] if float(g.result.fields.v_ref) == 0.0]
    out = TreeResult(assumptions=assumptions, warnings=notes + list(kt.warnings),
                     stats={"seconds": seconds, "groups": float(len(kt.groups))})
    for label, g in kt.groups.items():
        res = out.groups[key_of[label]] = _group(board, key_of[label], g, assumptions)
        res.warnings += [w for w in notes if w.startswith(f"{res.net} is solved as a 0 V rail")]
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
    sources = {name: project.rail(net).sources[i]
               for (what, net, i), name in names.items() if what == "source"}
    for v in out.vrms:
        _judge_input(out, v, sources.get(v.name))

    # A load's supply net is the one its pins are on: past a net tie, not its rail's (R6).
    rail_of_load = {name: net for (what, net, _i), name in names.items() if what == "load"}
    pad_net = {k.name: k.supply.net for k in setup.loads}
    for ld in kt.loads:
        key = key_of.get(ld.group, ld.group)
        drop, v_worst = load_drop(ld)
        pct = 100.0 * drop / abs(ld.nominal) if ld.nominal else math.nan
        across = ld.v_effective
        out.loads.append(LoadReport(
            name=ld.name, group=key, current_a=float(ld.current), nominal_v=float(ld.nominal),
            v_supply_v=float(ld.v_supply), v_supply_min_v=float(ld.v_supply_min),
            v_ground_v=ld.v_ground, v_across_v=float(across), v_worst_v=float(v_worst),
            drop_v=float(drop), drop_pct=float(pct), status=ld.status,
            net=pad_net.get(ld.name) or rail_of_load.get(ld.name, ""), rail=rail_of_load.get(ld.name, "")))
    apply_limits(out, project)
    return out


def _judge_input(tree: TreeResult, v: VrmReport, s: Optional[Source]) -> None:
    """Fill in regulator ``v``'s input as a load on the rail feeding it: its worst input pin
    across its ground pins (the lowest input pin minus the highest ground pin; on a negative
    rail the highest minus the lowest), its drop toward 0 V from the set voltage of the
    source driving that rail, and why it cannot be judged, if so."""
    if s is None or s.kind not in REGULATING or not s.input_net:
        return
    v.input_net = s.input_net
    key = tree.group_of(s.input_net)
    if key is None:                                  # its input rail is not solved (excluded)
        return
    v.input_group, res = key, tree.groups[key]
    driver = next((u for u in tree.vrms if u.group == key), None)
    nominal = driver.v_set_v if driver is not None else None
    v.input_nominal_v = nominal
    term = next((t for t in res.loads if t.name == f"{v.name} (input)"), None)
    gkey = tree.group_of(s.ground_net) if s.ground_net else None
    gterm = next((t for t in tree.groups[gkey].loads if t.name == f"{v.name} (gnd)"), None) if gkey else None
    if term is None or not (math.isfinite(term.v_min) and math.isfinite(term.v_max)):
        v.input_status = DISCONNECTED
        return
    if not res.converged:
        v.input_status = NOT_CONVERGED
    elif driver is not None and driver.regulation:
        v.input_status = OUT_OF_REGULATION
    sign = -1.0 if (nominal or 0.0) < 0 else 1.0
    ground = 0.0
    if gterm is not None and math.isfinite(gterm.v_max) and math.isfinite(gterm.v_min):
        ground = gterm.v_max if sign > 0 else gterm.v_min
    v.v_in_worst_v = (term.v_min if sign > 0 else term.v_max) - ground
    if nominal is not None:
        v.input_drop_v = sign * (nominal - v.v_in_worst_v)
        v.input_drop_pct = 100.0 * v.input_drop_v / abs(nominal) if nominal else math.nan
