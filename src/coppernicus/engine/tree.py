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

import heapq
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import scipy.sparse as sp
from kipi.core.progress import Canceled
from kipi.dc.pi.model import GROUND_REF_KINDS, active_setup
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C
from scipy.sparse.csgraph import connected_components

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.ground_terminal import PartsPinRef
from coppernicus.engine.pisetup import ground_nets, ground_rails, terminal_names, to_pisetup
from coppernicus.engine.solve import (
    Cancelled,
    _Progress,
    density_note,
    engine_settings,
    mesh_fields,
    thickness_note,
)
from coppernicus.power_tree import LIMITS_RULE, apply_limits
from coppernicus.project import Project, Source, pin_ref, pins_text
from coppernicus.results import (
    DISCONNECTED,
    NOT_CONVERGED,
    OUT_OF_REGULATION,
    LoadReport,
    NetSplit,
    RailResult,
    Segment,
    TerminalResult,
    TreeResult,
    VrmReport,
    exclude_degenerate,
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
    refs = [v.name + (f" (on {', '.join(v.ground.parts())}, every one of those pads at 0 V)"
                      if isinstance(v.ground, PartsPinRef) else "")
            for v in setup.vrms if v.kind == "source" and v.ground]
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


#: KiPI's warning when its outer (ground-sense) iteration does not settle (kipi/dc/pi/tree.py:
#: 707-711 sets ``tree_converged = False`` and appends it; its TreeResult carries no flag).
OUTER_NOT_CONVERGED = "Ground-sense iteration did not converge"


def outer_converged(kt) -> bool:
    """Whether KiPI's outer iteration over the rails and grounds settled (its warning absent)."""
    return not any(str(w).startswith(OUTER_NOT_CONVERGED) for w in kt.warnings)


def convert_tree(board: Board, project: Project, setup, kt, seconds: float,
                 thickness: Dict[str, float], rho_note: Optional[List[str]] = None) -> TreeResult:
    """KiPI's tree result ``kt`` as a :class:`TreeResult`, judged against the project's
    limits. ``rho_note`` replaces the copper-temperature assumption (an electro-thermal
    solve sets the copper temperature)."""
    names = terminal_names(project)
    key_of = group_keys(project, kt)

    assumptions = _assumptions(project, setup, rho_note)
    assumptions += thickness_note(thickness, board.file_thickness_mm) + density_note(board)
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
                     stats={"seconds": seconds, "groups": float(len(kt.groups))},
                     converged=outer_converged(kt))
    node_net: Dict[str, Tuple[np.ndarray, List[str]]] = {}     # each group's net per mesh node
    for label, g in kt.groups.items():
        res = out.groups[key_of[label]] = _group(board, key_of[label], g, assumptions)
        node_net[res.net] = set_node_nets(board, g.result.mesh, sorted(g.nets), res.layers)
        res.warnings += [w for w in notes if w.startswith(f"{res.net} is solved as a 0 V rail")]
        # j_max without the triangles below the input's 1 nm resolution; the tree says so too
        note = exclude_degenerate(res)
        if note:
            out.warnings.append(note)
        for s in g.result.fields.series:
            k = next((k for k in setup.links if k.name == s.name), None)
            if k is not None:
                out.links.append({"name": s.name, "group": key_of[label],
                                  "nets": [k.side_a.net, k.side_b.net],
                                  "resistance_ohm": float(k.resistance),
                                  "current_a": float(s.current), "power_w": float(s.power),
                                  # the voltage of each side and across it, side A − side B
                                  # (I·R for a resistance; 0 across a net tie, a short)
                                  "v_a_v": float(s.v_a), "v_b_v": float(s.v_b),
                                  "across_mv": float((s.v_a - s.v_b) * 1e3)})

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
        # Its rating, judged in coppernicus.power_tree (judge_ratings), not passed to KiPI.
        v.current_limit_a = getattr(sources.get(v.name), "current_limit_a", None)

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
            net=pad_net.get(ld.name) or rail_of_load.get(ld.name, ""), rail=rail_of_load.get(ld.name, ""),
            v_low_v=_volts(ld.v_low), v_high_v=_volts(ld.v_high)))
    split_tree(board, setup, kt, key_of, out, node_net)
    apply_limits(out, project)
    return out


def _volts(x) -> Optional[float]:
    """A KiPI load's v_low or v_high as a float; None where KiPI gives none."""
    return None if x is None else float(x)


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


# ---------------------------------------------------------------------- the per-net split (S1)
#
# KiPI meshes each net of a group on its own and concatenates the meshes: nets are never joined
# in the mesh (kipi/core/mesh/mesher.py, combine_meshes), a link being an element between pad
# nodes, outside the triangles. So copper connected through triangles, plated holes and pads is
# always one net, named by the pads and holes it holds. Every value here is shown, never judged.

def node_nets(board: Board, mesh, nets: Sequence[str]) -> np.ndarray:
    """The net of every node of a group's mesh (KiPI ``NetMesh``) solved on ``nets``, as an
    (N,) array of indices into ``nets``: the connected copper (triangles, a plated hole's rings
    on every layer, a terminal pad's nodes on every layer) takes the net of the pads and holes
    it holds; an island holding none (floating copper) the net whose copper outline holds its
    first triangle."""
    n, nets = len(mesh.points), list(nets)
    if len(nets) == 1:
        return np.zeros(n, dtype=np.int64)
    rows, cols = [mesh.tris[:, 0], mesh.tris[:, 1]], [mesh.tris[:, 1], mesh.tris[:, 2]]
    joined = [np.concatenate(list(b.ring_nodes.values())) for b in mesh.barrels if b.ring_nodes]
    joined += [np.concatenate(list(per.values())) for per in mesh.pad_nodes.values() if per]
    for nodes in joined:
        rows.append(nodes[:-1])
        cols.append(nodes[1:])
    r, c = np.concatenate(rows).astype(np.int64), np.concatenate(cols).astype(np.int64)
    graph = sp.coo_matrix((np.ones(len(r), dtype=np.int8), (r, c)), shape=(n, n))
    count, comp = connected_components(graph, directed=False)
    index = {net: i for i, net in enumerate(nets)}
    label = np.full(count, -1, dtype=np.int64)
    via_net = {v.id: v.net for v in board.snapshot.vias} if mesh.barrels else {}
    named = [(board.pad(b.owner_id).net if b.kind == "pth" else via_net.get(b.owner_id),
              next(iter(b.ring_nodes.values()))) for b in mesh.barrels if b.ring_nodes]
    named += [(board.pad(pid).net, next(iter(per.values()))) for pid, per in mesh.pad_nodes.items() if per]
    for net, nodes in named:
        if net in index and len(nodes):
            label[comp[nodes[0]]] = index[net]
    missing = np.flatnonzero(label < 0)
    if len(missing):
        label[missing] = _by_outline(board, mesh, comp, count, missing, nets)
    return label[comp]


def _by_outline(board: Board, mesh, comp, count: int, missing, nets: List[str]) -> List[int]:
    """The net (an index into ``nets``) of each component in ``missing``, copper holding no pad
    and no hole: the net whose copper, on the layer of the component's first triangle, holds
    that triangle's centroid, or else lies nearest to it."""
    from shapely.geometry import Point
    first = np.full(count, -1, dtype=np.int64)
    m = len(mesh.tris)
    first[comp[mesh.tris[::-1, 0]]] = np.arange(m - 1, -1, -1)       # the lowest index wins
    copper: Dict[Tuple[str, int], object] = {}

    def distance(net: str, lid: int, pt) -> float:
        if (net, lid) not in copper:
            copper[net, lid] = board.copper(net, lid)
        geom = copper[net, lid]
        return math.inf if geom.is_empty else float(geom.distance(pt))
    out = []
    for c in missing:
        tri = first[c]
        if tri >= 0:
            xy, lid = mesh.points[mesh.tris[tri]].mean(axis=0), int(mesh.tri_layer[tri])
        else:                                    # no triangle: the node's own point
            node = int(np.flatnonzero(comp == c)[0])
            xy, lid = mesh.points[node], int(mesh.node_layer[node])
        pt = Point(float(xy[0]), float(xy[1]))
        out.append(min(range(len(nets)), key=lambda i: (distance(nets[i], lid, pt), i)))
    return out


def set_node_nets(board: Board, mesh, nets: Sequence[str], layers) -> Tuple[np.ndarray, List[str]]:
    """Set ``node_net`` (net names) on a group's layers, each layer's nodes as
    :func:`mesh_fields` selects them, and return the net of every node of the whole mesh as
    indices into ``nets`` (:func:`node_nets`), with ``nets``."""
    nets = list(nets)
    codes, names = node_nets(board, mesh, nets), np.array(nets, dtype=object)
    by_id = {lf.layer_id: lf for lf in layers}
    for lid in mesh.layers:
        lf = by_id.get(int(lid))
        if lf is not None:
            lf.node_net = names[codes[np.unique(mesh.tris[mesh.tri_layer == lid])]]
    return codes, nets


@dataclass
class _Link:
    """A link of a group as solved: each side's net, pad labels and voltage."""
    name: str
    nets: Tuple[str, str]
    pads: Tuple[List[str], List[str]]
    v: Tuple[float, float]
    current: float

    def capacity(self) -> float:
        """The current it carries; a short's is not resolved (NaN): it takes all there is."""
        return abs(self.current) if math.isfinite(self.current) else math.inf


#: A hop along a path: the link, its side on the net before (toward the root), its side after.
_Hop = Tuple[_Link, int, int]


def _widest(roots: Sequence[str], links: Sequence[_Link]) -> Dict[str, Optional[_Hop]]:
    """Each net reached from ``roots`` over ``links`` and the hop it is reached by (None at a
    root), along the highest-current path: the path whose weakest link carries the most
    current, then the one with the fewest links, then the links' order."""
    best = {r: (math.inf, 0) for r in roots}
    parent: Dict[str, Optional[_Hop]] = {r: None for r in roots}
    heap = [(-math.inf, 0, i, r) for i, r in enumerate(roots)]
    heapq.heapify(heap)
    done, seq = set(), len(heap)
    while heap:
        neg, hops, _, net = heapq.heappop(heap)
        if net in done:
            continue
        done.add(net)
        for k in links:
            for near in (0, 1):
                far_net = k.nets[1 - near]
                if k.nets[near] != net or far_net == net or far_net in done:
                    continue
                cand = (min(-neg, k.capacity()), hops + 1)
                old = best.get(far_net)
                if old is None or cand[0] > old[0] or (cand[0] == old[0] and cand[1] < old[1]):
                    best[far_net], parent[far_net] = cand, (k, near, 1 - near)
                    seq += 1
                    heapq.heappush(heap, (-cand[0], cand[1], seq, far_net))
    return parent


def _path(parent: Dict[str, Optional[_Hop]], target: str) -> Optional[List[_Hop]]:
    """The hops from the root to ``target`` (None when it is not reached)."""
    hops, net = [], target
    while True:
        if net not in parent:
            return None
        hop = parent[net]
        if hop is None:
            return hops[::-1]
        hops.append(hop)
        net = hop[0].nets[hop[1]]


def _joined(links: Sequence[_Link], a: str, b: str) -> bool:
    seen, todo = {a}, [a]
    while todo:
        n = todo.pop()
        for k in links:
            for x, y in (k.nets, k.nets[::-1]):
                if x == n and y not in seen:
                    seen.add(y)
                    todo.append(y)
    return b in seen


def loop_note(links: Sequence[_Link]) -> str:
    """The attribution rule, naming the links in parallel or in a loop; "" when there are none
    (every net is reached one way)."""
    edges = [k for k in links if k.nets[0] != k.nets[1]]
    looped = [k.name for k in edges if _joined([e for e in edges if e is not k], *k.nets)]
    if not looped:
        return ""
    return (f"Links in parallel or in a loop ({', '.join(looped)}): each net's entry and each drop's "
            "segments follow the highest-current path (the one whose weakest link carries the most "
            "current); every path adds up to the same total.")


def _labels(board: Board, pad_ids: Sequence[str]) -> List[str]:
    """Pads as "REF.PAD" labels, each once, in order."""
    out: List[str] = []
    for pid in pad_ids:
        p = board.pad(pid)
        text = f"{p.footprint}.{p.number}"
        if text not in out:
            out.append(text)
    return out


def _text(labels: Sequence[str]) -> str:
    return ", ".join(labels[:4]) + (f" (+{len(labels) - 4})" if len(labels) > 4 else "")


def _pin_labels(board: Board, ref) -> List[str]:
    """The pads of a KiPI pin reference (a :class:`PartsPinRef`'s on each of its parts)."""
    if ref is None:
        return []
    out = _labels(board, [p.id for p in board.pads([ref.net], ref.ref, ref.pins or None)])
    for other in getattr(ref, "more", None) or ():
        out += [x for x in _pin_labels(board, other) if x not in out]
    return out


def _worst_pad(board: Board, term, high: bool) -> Optional[Tuple[float, str]]:
    """``(V, "REF.PAD")`` of a KiPI terminal's lowest connected pad (its highest with ``high``)."""
    if term is None:
        return None
    v = np.asarray(term.v_pads, dtype=float)
    live = np.flatnonzero(np.isfinite(v))
    if not len(live):
        return None
    i = live[np.argmax(v[live]) if high else np.argmin(v[live])]
    return float(v[i]), _labels(board, [term.pad_ids[i]])[0]


def split_tree(board: Board, setup, kt, key_of: Dict[str, str], out: TreeResult,
               node_net: Dict[str, Tuple[np.ndarray, List[str]]]) -> None:
    """Each group's per-net split (``net_split``, ``split_note``) and each load's and regulator
    input's drop as segments (``segments``), from KiPI's tree result ``kt``; ``node_net``: each
    group's net per mesh node (:func:`set_node_nets`)."""
    vrm_of = {v.name: v for v in setup.vrms}
    load_of = {ld.name: ld for ld in setup.loads}
    reports = {v.name: v for v in out.vrms}
    links: Dict[str, List[_Link]] = {key: [] for key in out.groups}
    terms: Dict[str, Dict[str, object]] = {}
    for label, g in kt.groups.items():
        key = key_of[label]
        terms[key] = {t.name: t for t in g.result.fields.terminals}
        for s in g.result.fields.series:
            k = next((k for k in setup.links if k.name == s.name), None)
            if k is not None:
                links[key].append(_Link(s.name, (k.side_a.net, k.side_b.net),
                                        (_labels(board, s.pads_a), _labels(board, s.pads_b)),
                                        (float(s.v_a), float(s.v_b)), float(s.current)))
    trees: Dict[Tuple[str, Tuple[str, ...]], Dict[str, Optional[_Hop]]] = {}

    def parents(key: str, roots: Sequence[str]) -> Dict[str, Optional[_Hop]]:
        if (key, tuple(roots)) not in trees:
            trees[key, tuple(roots)] = _widest(roots, links[key])
        return trees[key, tuple(roots)]

    for label, g in kt.groups.items():
        key = key_of[label]
        res = out.groups[key]
        entries: Dict[str, tuple] = {}           # root net -> (kind, name, pads, V)
        if g.kind == "rail" and g.driver in vrm_of:
            vrm, rep = vrm_of[g.driver], reports.get(g.driver)
            entries[vrm.output.net] = ("source", vrm.name, _pin_labels(board, vrm.output),
                                       None if rep is None else float(rep.v_out_v))
        elif g.kind != "rail":
            refs = [v for v in setup.vrms if v.kind in GROUND_REF_KINDS and v.ground
                    and v.ground.net in g.nets and f"{v.name} (gnd)" in terms[key]]
            for net in dict.fromkeys(v.ground.net for v in refs):
                mine = [v for v in refs if v.ground.net == net]
                volts = np.concatenate([np.asarray(terms[key][f"{v.name} (gnd)"].v_pads, dtype=float)
                                        for v in mine])
                pads = list(dict.fromkeys(x for v in mine for x in _pin_labels(board, v.ground)))
                entries[net] = ("reference", ", ".join(v.name for v in mine), pads,
                                float(np.nanmean(volts)) if np.isfinite(volts).any() else None)
        res.net_split = _net_split(board, res, g.result, *node_net[key], entries,
                                   parents(key, list(entries)))
        res.split_note = loop_note(links[key])

    def driver_of(key: str):
        rep = next((u for u in out.vrms if u.group == key), None)     # as _judge_input finds it
        return (rep, vrm_of.get(rep.name)) if rep is not None else (None, None)

    def ground_end(gnet: Optional[str], terminal: str, high: bool) -> Optional[tuple]:
        """(G, its pad, its net, its group) of a ground terminal; None when it has none."""
        gkey = out.group_of(gnet) if gnet else None
        found = _worst_pad(board, terms.get(gkey, {}).get(terminal), high) if gkey else None
        return None if found is None else (found[0], found[1], gnet, gkey)

    for ld in out.loads:
        spec = load_of.get(ld.name)
        if spec is None or not math.isfinite(ld.drop_v):
            continue
        sign = -1.0 if ld.nominal_v < 0 else 1.0
        supply = _worst_pad(board, terms.get(ld.group, {}).get(ld.name), sign < 0)
        ground = ground_end(spec.ground.net if spec.ground else None, f"{ld.name} (return)", sign > 0)
        ld.segments = _segments(board, ld.nominal_v, ld.drop_v, *driver_of(ld.group), ld.group,
                                spec.supply.net, supply, ground, out, parents)
    for v in out.vrms:
        vrm = vrm_of.get(v.name)
        if (vrm is None or vrm.input is None or not v.input_group or v.input_drop_v is None
                or not math.isfinite(v.input_drop_v)):
            continue
        sign = -1.0 if (v.input_nominal_v or 0.0) < 0 else 1.0
        supply = _worst_pad(board, terms.get(v.input_group, {}).get(f"{v.name} (input)"), sign < 0)
        ground = ground_end(vrm.ground.net if vrm.ground else None, f"{v.name} (gnd)", sign > 0)
        v.segments = _segments(board, v.input_nominal_v, v.input_drop_v, *driver_of(v.input_group),
                               v.input_group, vrm.input.net, supply, ground, out, parents)


def _net_split(board: Board, res: RailResult, result, codes: np.ndarray, code_nets: List[str],
               entries: Dict[str, tuple], parent: Dict[str, Optional[_Hop]]) -> List[NetSplit]:
    """One row per net of group ``res``: its voltage range and where, its entry (a root's, or
    the side of the link it is reached by), its drop or rise from the entry, its losses.
    ``codes``: each mesh node's net, an index into ``code_nets``."""
    mesh, v, fields = result.mesh, result.solution.node_v, result.fields
    tri_code = codes[mesh.tris[:, 0]]
    via_loss = np.zeros(len(code_nets))
    for bar, via in zip(mesh.barrels, fields.vias):
        via_loss[codes[next(iter(bar.ring_nodes.values()))[0]]] += float(via.power)
    nowhere = (math.nan, math.nan, "")

    def at(i) -> Tuple[float, float, str]:
        if i is None:
            return nowhere
        return (float(mesh.points[i, 0]), float(mesh.points[i, 1]),
                board.layer_name(int(mesh.node_layer[i])))
    rows = []
    finite = np.isfinite(v)
    for net in res.nets:
        code = code_nets.index(net)
        live = np.flatnonzero((codes == code) & finite)
        lo = live[np.argmin(v[live])] if len(live) else None
        hi = live[np.argmax(v[live])] if len(live) else None
        row = NetSplit(net, float(v[lo]) if lo is not None else math.nan,
                       float(v[hi]) if hi is not None else math.nan, at(lo), at(hi),
                       float(np.nansum(fields.tri_power[tri_code == code])), float(via_loss[code]))
        hop = parent.get(net)
        entry = (entries[net] if net in entries else
                 ("link", hop[0].name, list(hop[0].pads[hop[2]]), hop[0].v[hop[2]]) if hop else None)
        if entry is not None:
            row.entry_kind, row.entry_name, row.entry_pads, row.entry_v = entry
        if row.entry_v is not None and math.isfinite(row.entry_v) and lo is not None:
            if res.kind == "ground":             # the rise of largest magnitude, signed
                up, down = row.v_max_v - row.entry_v, row.v_min_v - row.entry_v
                row.drop_v = up if abs(up) >= abs(down) else down
            else:
                row.drop_v = res.sign * (row.entry_v - (row.v_min_v if res.sign > 0 else row.v_max_v))
        rows.append(row)
    return rows


def _segments(board: Board, nominal: float, drop: float, driver: Optional[VrmReport], vrm,
              key: str, target: str, supply: Optional[Tuple[float, str]], ground: Optional[tuple],
              tree: TreeResult, parents) -> List[Segment]:
    """The drop of a load or regulator input (``drop``, from ``nominal``) as segments: the
    driver's regulation, each net and link of rail group ``key`` from the driver's output pins
    to the worst supply pin (``supply``: its V and pad, on net ``target``), then from the worst
    ground pin (``ground``: V, pad, net, group; None without one) back to the driver's ground
    pins. Each segment is the difference of two potentials and the next one starts from the
    same float, so they add up to the drop exactly; [] if those potentials do not give it."""
    if driver is None or vrm is None or supply is None:
        return []
    sign = -1.0 if nominal < 0 else 1.0
    v_out, v_gd = float(driver.v_out_v), float(driver.v_ground_v or 0.0)
    out_pads, gd_pads = _text(_pin_labels(board, vrm.output)), _text(_pin_labels(board, vrm.ground))
    segs = [Segment("regulation", driver.name, f"{nominal:g} V set",
                    f"{out_pads} − {gd_pads}" if gd_pads else out_pads, sign * (nominal - (v_out - v_gd)))]
    hops = _path(parents(key, [vrm.output.net]), target)
    pos_v, pos, net = v_out, out_pads, vrm.output.net
    for k, near, far in hops or ():
        a, b = _text(k.pads[near]), _text(k.pads[far])
        segs.append(Segment("copper", net, pos, a, sign * (pos_v - k.v[near])))
        segs.append(Segment("link", k.name, a, b, sign * (k.v[near] - k.v[far])))
        pos_v, pos, net = k.v[far], b, k.nets[far]
    segs.append(Segment("copper", net if hops is not None else target, pos, supply[1],
                        sign * (pos_v - supply[0])))
    g_v = 0.0
    if ground is not None:
        g_v, g_pad, g_net, g_key = ground
        d_net = vrm.ground.net if vrm.ground else None
        back = (_path(parents(g_key, [d_net]), g_net)
                if d_net is not None and tree.group_of(d_net) == g_key else None)
        if back is None:                         # not on one ground group: one step
            segs.append(Segment("ground", g_net, g_pad, gd_pads or "0 V", sign * (g_v - v_gd)))
        else:
            pos_v, pos, net = g_v, g_pad, g_net
            for k, near, far in reversed(back):  # from the load's ground toward the driver's
                a, b = _text(k.pads[far]), _text(k.pads[near])
                segs.append(Segment("ground copper", net, pos, a, sign * (pos_v - k.v[far])))
                segs.append(Segment("ground link", k.name, a, b, sign * (k.v[far] - k.v[near])))
                pos_v, pos, net = k.v[near], b, k.nets[near]
            segs.append(Segment("ground copper", net, pos, gd_pads, sign * (pos_v - v_gd)))
    elif v_gd != 0.0:                            # no ground pins: KiPI takes them at 0 V
        segs.append(Segment("ground", vrm.ground.net if vrm.ground else "", "0 V", gd_pads,
                            sign * (0.0 - v_gd)))
    rebuilt = sign * (nominal - (supply[0] - g_v))
    if not (math.isfinite(rebuilt) and abs(rebuilt - drop) <= 1e-9):
        return []                                # not the potentials the drop was taken from
    return segs
