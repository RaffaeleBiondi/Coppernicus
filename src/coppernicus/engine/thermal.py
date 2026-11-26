"""The board temperature: KiPI's electro-thermal co-simulation of the whole power tree.

Each iteration solves every rail and ground group with the copper's resistivity at its
temperature (iteration 1: at the ambient), gathers every loss on the board onto one grid
(copper and via barrels where they are dissipated, links at their pads, each regulator's
conversion loss at its pads, the loads' own power when asked) and solves the board
temperature; the next iteration solves the copper at that temperature, each layer its own.

The loop is a thin copy of KiPI's ``solve_electrothermal`` (``kipi/dc/thermal/cosim.py``
:232-280), using KiPI's own model, tree solve and heat gathering, so that

* iteration 1's load drops (copper at the ambient) are kept as the baseline: no extra solve;
* ``regulator_heat: part_pads`` can spread a regulator's loss over every pad of its part.

With ``regulator_heat: output_pads`` (KiPI's placement) it is bit-identical to KiPI's own
function: :func:`solve_kipi_reference` runs that one for the drift test.
"""

from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import shapely
from kipi.core.geometry.copper import poly_to_shapely
from kipi.core.model.conductors import K_CU_W_MK
from kipi.core.progress import Canceled
from kipi.dc.pi.report import layer_temps
from kipi.dc.pi.tree import solve_tree
from kipi.dc.solver import SolveError
from kipi.dc.solver.setup import REF_TEMP_C, RHO_CU_OHM_MM
from kipi.dc.thermal.cosim import (
    ThermalResult,
    _copper_heat,
    _Heat,
    heat_sources,
    load_powers,
    solve_electrothermal,
)
from kipi.dc.thermal.materials import coating_k
from kipi.dc.thermal.model import (
    H_NATURAL_W_M2K,
    H_PER_M_S,
    BoardThermalModel,
    ThermalSettings,
    board_geometry,
)

from coppernicus.engine import superlu_order
from coppernicus.engine.ac import peak_rss_bytes
from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import to_pisetup
from coppernicus.engine.solve import TCR_CU, Cancelled, _Progress
from coppernicus.engine.tree import check_grounds, convert_tree, group_keys, load_drop
from coppernicus.project import Project, ThermalOptions
from coppernicus.results import BoardTemperature, LayerTemperature, TemperatureMap, TreeResult
from coppernicus.temperature import apply_thermal_limits

# KiPI's settling rule (cosim.py:276): the last iteration changed no cell by more than this
# share of the peak rise.
SETTLE_SHARE = 0.05


def engine_defaults() -> ThermalOptions:
    """KiPI's defaults (``ThermalSettings``, model.py:84-98) as thermal options; the regulator
    heat at KiPI's own placement."""
    k = ThermalSettings()
    return ThermalOptions(enabled=k.enabled, ambient_c=k.ambient_c, iterations=k.iterations,
                          airflow_m_s=k.airflow_m_s, grid_mm=k.grid_mm, load_heat=k.load_power,
                          max_temp_c=k.max_temp_c, max_rise_c=k.max_rise_c, regulator_heat="output_pads")


def thermal_settings(options: ThermalOptions) -> ThermalSettings:
    """KiPI's settings for ours. The limits are not passed: Coppernicus judges them."""
    return ThermalSettings(enabled=True, ambient_c=float(options.ambient_c),
                           iterations=int(options.iterations), airflow_m_s=float(options.airflow_m_s),
                           grid_mm=float(options.grid_mm), load_power=bool(options.load_heat))


class _ThermalProgress(_Progress):
    """The engine's messages, without KiPI's advice to use its own Thermal tab (an
    unrecognized laminate is named in the assumptions instead)."""

    def message(self, text, level=20):
        if "Thermal tab" in str(text):
            if self._cancelled():
                self.cancel()
            return
        super().message(text, level)


# ---------------------------------------------------------------------- the loop

def _part_pads_heat(heat: _Heat, tree, snap, setup) -> None:
    """Each regulator phase's conversion loss spread evenly over every copper pad of its
    part (all nets), each pad's share over its copper layers: KiPI's ``_vrm_heat``
    (cosim.py:205-214) with the part's pads in place of its output pads."""
    vrm_by_name = {v.name: v for v in setup.vrms}
    for rep in tree.vrms:
        if rep.p_loss:
            phases = vrm_by_name[rep.name].all_phases()
            losses = [ph.p_loss for ph in rep.phases] if rep.phases else [rep.p_loss]
            for ph, loss in zip(phases, losses):
                pads = [p.id for p in snap.pads if p.footprint == ph.output.ref and p.shapes]
                heat.add_pads("VRM conversion", pads, loss)


def _heat(tree, snap, setup, ts: ThermalSettings, regulator_heat: str):
    """(xy, watts, layer ids, watts by kind) of every loss on the board (cosim.py:217-229)."""
    if regulator_heat == "output_pads":
        return heat_sources(tree, snap, setup, ts)            # KiPI's own placement
    heat = _Heat(snap)
    _copper_heat(heat, tree)
    _part_pads_heat(heat, tree, snap, setup)
    if ts.load_power:
        for watts, pads in load_powers(tree, snap, setup).values():
            heat.add_pads("loads", pads, watts)
    xy, w, layers = heat.arrays()
    return xy, w, layers, heat.by_kind


@dataclass
class _Run:
    tree: object                       # KiPI's tree of the last iteration, with .thermal
    first_drops: Dict[str, float]      # load name -> drop at iteration 1 (copper at ambient)


def _electrothermal(snap, setup, ts: ThermalSettings, progress, regulator_heat: str) -> _Run:
    """KiPI's ``solve_electrothermal`` loop (cosim.py:232-280), copied: the same model, tree
    solve, heat and temperature solve, in the same order. It differs only in the regulator
    heat (``part_pads``), in keeping iteration 1's load drops, and in leaving the settling
    warning to the caller."""
    with progress.timed("Building the board thermal model"), superlu_order.thermal_model():
        model = BoardThermalModel(snap, ts, progress, setup.settings.conductor_by_layer,
                                  setup.settings.via_rules)
    temps = np.where(model.inside, ts.ambient_c, np.nan)[None].repeat(len(model.g), 0)
    history: List[dict] = []
    tree = None
    meshes: dict = {}                 # the copper does not change between iterations
    factors: dict = {}
    heat_total = 0.0
    loads_w: Dict[str, float] = {}
    by_kind: Dict[str, float] = {}
    edge_w: list = []
    first: Dict[str, float] = {}
    n = max(1, int(ts.iterations))
    for it in range(1, n + 1):
        progress.check_cancel()
        progress.stage(f"Electro-thermal iteration {it}/{n}")
        # Iteration 1 solves the copper at ambient, not at the 20 degC reference.
        tree = solve_tree(snap, setup, progress, temperature=model.sampler(temps),
                          mesh_cache=meshes, factor_cache=factors)
        if it == 1:
            first = {ld.name: load_drop(ld)[0] for ld in tree.loads}
        xy, w, layers, by_kind = _heat(tree, snap, setup, ts, regulator_heat)
        heat = model.empty_heat()
        model.deposit(heat, xy, w, layers)
        heat_total = float(w.sum())
        if ts.load_power:
            loads_w = {k: v[0] for k, v in load_powers(tree, snap, setup).items()}
            progress.info(f"  loads: {sum(max(v, 0.0) for v in loads_w.values()) * 1e3:.1f} mW "
                          f"({len(loads_w)} load(s))")
        progress.info(f"Solving the board temperature for {heat_total * 1e3:.1f} mW of heat")
        new = model.solve(heat)
        dt = float(np.nanmax(np.abs(new - temps)))
        temps = new
        edge_w = model.edge_heat(temps)
        history.append({"iteration": it, "heat_w": heat_total, "t_max": float(np.nanmax(temps)),
                        "dt_max": dt})
        progress.info(f"Iteration {it}: {heat_total * 1e3:.1f} mW into the board, peak "
                      f"{np.nanmax(temps):.2f} °C (ambient {ts.ambient_c:g} °C), "
                      f"max change {dt:.3f} °C")
    tree.thermal = ThermalResult(ts, model.x, model.y, temps, history, heat_w=heat_total,
                                 heat_by_kind=by_kind, dielectrics=model.dielectrics,
                                 load_heat_w=loads_w, layers=model.layers, edge_w=edge_w)
    return _Run(tree, first)


def solve_thermal_tree(board: Board, project: Project, setup, thickness: Dict[str, float],
                       progress: Optional[Callable[[str], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False) -> TreeResult:
    """The power tree solved electro-thermally (called by ``solve_project_tree``, which has
    checked the grounds and built the setup on the what-if board)."""
    options = project.thermal
    peak0 = peak_rss_bytes()
    t0 = time.perf_counter()
    try:
        run = _electrothermal(board.snapshot, setup, thermal_settings(options),
                              _ThermalProgress(progress, cancelled), options.regulator_heat)
    except Canceled as e:
        raise Cancelled("solve cancelled") from e
    except SolveError as e:
        raise EngineError(f"Power tree: {e}") from e
    out = _finish(board, project, setup, thickness, run.tree, run.first_drops, time.perf_counter() - t0)
    # This process's high-water mark when the solve ended (KiPI's mesher worker processes not
    # counted), as the AC solve records it; this solve's own peak when it raised it.
    peak1 = peak_rss_bytes()
    out.stats["memory_peak_mb"] = peak1 / 1e6 if peak1 else None
    out.stats["memory_peak_raised"] = bool(peak0 and peak1 and peak1 > peak0)
    return out


def solve_kipi_reference(board: Board, project: Project) -> TreeResult:
    """The same solve through KiPI's own ``solve_electrothermal``, converted as
    :func:`solve_thermal_tree` converts ours: the drift test compares the two (KiPI's
    regulator-heat placement only; no ambient baseline, which KiPI does not keep)."""
    if project.thermal.regulator_heat != "output_pads":
        raise EngineError("KiPI places the regulator heat at the output pads only")
    check_grounds(project)
    thickness = dict(project.settings.copper_thickness_mm or {})
    board = board.with_copper_thickness(thickness) if thickness else board
    setup = to_pisetup(board, project)
    # KiPI's own factorizations throughout, whatever superlu_order part is on: the reference.
    with superlu_order.kipi_path():
        kt = solve_electrothermal(board.snapshot, setup, thermal_settings(project.thermal),
                                  _ThermalProgress(None, lambda: False))
    return _finish(board, project, setup, thickness, kt, {}, 0.0)


# ---------------------------------------------------------------------- conversion

class _PadIndex:
    """The board's pads, for the pad nearest to a point: the distance in the plane from the
    point to the pad's copper on any layer (0 on the pad), the nearest centre on a tie."""

    def __init__(self, snap):
        self.pads, geoms = [], []
        for p in snap.pads:
            polys = [g for g in (poly_to_shapely(q) for qs in p.shapes.values() for q in qs)
                     if g is not None and not g.is_empty]
            geoms.append(shapely.union_all(polys) if polys else shapely.Point(p.position))
            self.pads.append(p)
        self.tree = shapely.STRtree(geoms) if geoms else None

    def nearest(self, x: float, y: float) -> Tuple[str, str, Optional[float]]:
        if self.tree is None or not (math.isfinite(x) and math.isfinite(y)):
            return "", "", None
        found, dist = self.tree.query_nearest(shapely.Point(x, y), return_distance=True,
                                              all_matches=True)
        best = min(zip(found, dist), key=lambda kd: (
            kd[1], math.hypot(self.pads[kd[0]].position[0] - x, self.pads[kd[0]].position[1] - y)))
        pad = self.pads[int(best[0])]
        return str(pad.footprint), str(pad.number), float(best[1])


def _settled(history: List[dict], ambient_c: float) -> Optional[bool]:
    """KiPI's rule (cosim.py:276): settled when the last iteration changed no cell by more
    than SETTLE_SHARE of the peak rise; None after a single iteration."""
    if len(history) < 2:
        return None
    last = history[-1]
    return bool(last["dt_max"] <= SETTLE_SHARE * max(last["t_max"] - ambient_c, 1e-9))


def _settled_text(settled: Optional[bool]) -> str:
    if settled is None:
        return "one iteration: settling not checked"
    share = f"{SETTLE_SHARE * 100:g} % of the peak rise"
    return f"settled: at most {share}" if settled else f"not settled: more than {share}; run more iterations"


def _rho_notes(project: Project, th: ThermalResult, settled: Optional[bool]) -> List[str]:
    """A2, in place of the copper-temperature line, and A8 when copper_temp_c is set."""
    n = len(th.history)
    dt = th.history[-1]["dt_max"] if th.history else 0.0
    notes = [f"Electro-thermal: {n} iteration{'s' if n != 1 else ''}. Iteration 1 solves the copper "
             "at the ambient, each next one at the temperature the last gave, each layer its own: "
             f"ρ = ρ₂₀·(1 + α·(T − {REF_TEMP_C:g} °C)), ρ₂₀ = {RHO_CU_OHM_MM:g} Ω·mm, "
             f"α = {TCR_CU:g} /°C. Largest change of any cell in the last iteration: {dt:.3g} °C "
             f"({_settled_text(settled)})."]
    if project.settings.copper_temp_c != REF_TEMP_C:
        notes.append(f"copper_temp_c ({project.settings.copper_temp_c:g} °C) is not used: the "
                     "electro-thermal solve sets the copper temperature.")
    return notes


def _breakdown(by_kind: Dict[str, float]) -> str:
    return ", ".join(f"{k} {w:.3g} W" for k, w in sorted(by_kind.items(), key=lambda kv: -kv[1]))


def _assumptions(board: Board, options: ThermalOptions, th: ThermalResult, tree: TreeResult,
                 h_w_m2k: float, cell_mm: float, cells: int, rho_notes: List[str]) -> List[str]:
    snap = board.snapshot
    part = options.regulator_heat == "part_pads"
    where = "spread over every copper pad of its part" if part else "at its output pins"
    load_text = (", and each load's power at its pins" if options.load_heat
                 else "; the loads' own power is not counted (load_heat: false)")
    nx, ny, nl = len(th.x), len(th.y), len(th.layers)
    out = [
        f"Board temperature: steady state, ambient {th.settings.ambient_c:g} °C, airflow "
        f"{th.settings.airflow_m_s:g} m/s: convection h = {h_w_m2k:g} W/m²K on each face (KiPI: "
        f"{H_NATURAL_W_M2K:g} + {H_PER_M_S:g}·v; natural convection and radiation at 0 m/s).",
        *rho_notes,
        "Heat: copper and via-barrel losses of every solved group, link losses at their pads, and "
        f"each regulator's conversion loss (input − output power) {where}{load_text}: "
        f"{th.heat_w:.3g} W ({_breakdown(th.heat_by_kind)}).",
    ]
    regs = [f"{v.name} {v.p_loss_w:.3g} W" for v in tree.vrms if v.p_loss_w and v.p_loss_w > 0]
    if regs:
        if part:
            out.append(f"Regulator heat ({', '.join(regs)}) is spread evenly over every copper pad "
                       "of each regulator's part (all nets), each pad's share over its copper layers, "
                       "as a point in the grid cell that holds the pad centre (regulator_heat: "
                       "part_pads). The package's own spreading and convection are not modelled and "
                       "the split is even, so individual pads can be over- or under-estimated.")
        else:
            out.append(f"Regulator heat ({', '.join(regs)}) goes into the copper at each "
                       "regulator's output pins, split evenly over those pads and each pad's share "
                       "over its copper layers, as a point in the grid cell that holds the pad centre "
                       "(KiPI's placement; regulator_heat: output_pads). The package's own spreading "
                       "and convection are not modelled, so temperatures at those pads are likely "
                       "overestimated: read them as an upper-bound estimate.")
    out.append(f"Grid: {nx} × {ny} cells of {cell_mm:.3g} mm ({cells} inside the board"
               f"{', automatic size' if options.grid_mm <= 0 else ''}) on {nl} copper layer"
               f"{'s' if nl != 1 else ''}. A cell is all copper or none, read at its centre: "
               "features narrower than a cell may be missed or widened. The copper of every net "
               f"spreads heat (k = {K_CU_W_MK:g} W/m·K).")
    for material, mm, info in th.dielectrics:
        name = material or "(unnamed)"
        if info.assumed:
            out.append(f"Laminate {name} ({mm:.3g} mm) not recognized: taken as FR4 core, "
                       f"{info.k_w_mk:g} / {info.k_through:g} W/m·K.")
        else:
            out.append(f"Laminate {name} ({mm:.3g} mm): taken as {info.matched}, k = "
                       f"{info.k_w_mk:g} W/m·K in-plane, {info.k_through:g} through.")
    coatings = list(getattr(snap.stackup, "coatings", []) or [])
    if coatings:
        out.append("Coatings on the outer faces: " + ", ".join(
            f"{c.name.lower()} {c.thickness_mm * 1000:.3g} µm ({coating_k(c):g} W/m·K"
            f"{', where it prints' if 'silk' in c.name.lower() else ''})" for c in coatings) + ".")
    out.append("Board shape: the board outline." if snap.outline else
               "Board shape: no outline: the bounding box of the copper plus 1 mm.")
    pads = "its part's pads" if part else "its output pads"
    out.append("Not modelled: heat sinks, enclosures, mounting hardware and edge clamps (convection "
               f"only); part packages (a regulator's loss goes into the copper at {pads}; no "
               "junction temperature); transient heating.")
    out.append("Board temperature is judged only against max_temp_c / max_rise_c: the hottest "
               "solved copper of each layer, its rise over the ambient. A layer over a limit fails "
               "even when the solve has not settled: the fail stands on the last iteration's "
               "temperatures, and the fail says whether they had settled; otherwise nothing is "
               "judged when the solve did not settle or ran one iteration. The grid peak is shown, "
               "not judged: it can lie on laminate or on copper of a net that is not solved.")
    return out


def _finish(board: Board, project: Project, setup, thickness: Dict[str, float], kt,
            first_drops: Dict[str, float], seconds: float) -> TreeResult:
    """KiPI's electro-thermal tree (``kt.thermal`` set) as a TreeResult with its board
    temperature, judged against the project's limits."""
    th: ThermalResult = kt.thermal
    options = project.thermal
    ambient = float(th.settings.ambient_c)
    settled = _settled(th.history, ambient)
    out = convert_tree(board, project, setup, kt, seconds, thickness,
                       rho_note=_rho_notes(project, th, settled))
    key_of = group_keys(project, kt)
    pads = _PadIndex(board.snapshot)

    maps = [TemperatureMap(int(lid), board.layer_name(lid), np.array(th.x, dtype=float),
                           np.array(th.y, dtype=float), np.array(th.temps[k], dtype=float))
            for k, lid in enumerate(th.layers)]
    rows = []
    for r in layer_temps(kt, board.snapshot):
        ref, num, dist = pads.nearest(r.x, r.y)
        rows.append(LayerTemperature(str(r.layer), float(r.t_max), float(r.rise), float(r.x), float(r.y),
                                     key_of.get(r.group, str(r.group)), None, ref, num, dist))
    k, i, j = (int(n) for n in np.unravel_index(np.nanargmax(th.temps), th.temps.shape))
    px, py, pt = float(th.x[j]), float(th.y[i]), float(th.temps[k, i, j])   # the grid peak
    peak_ref, peak_num, peak_dist = pads.nearest(px, py)
    cell_mm = float(th.x[1] - th.x[0]) if len(th.x) > 1 else float(options.grid_mm)
    cells = int(np.count_nonzero(~np.isnan(th.temps[0])))
    h_w_m2k = float(th.settings.h_w_mm2_k() * 1e6)
    drops = {ld.name: ld for ld in out.loads}
    temp = BoardTemperature(
        maps=maps, copper=rows, ambient_c=ambient, airflow_m_s=float(th.settings.airflow_m_s),
        h_w_m2k=h_w_m2k, grid_mm=cell_mm, grid_nx=len(th.x), grid_ny=len(th.y), grid_cells=cells,
        iterations=len(th.history), settled=settled,
        history=[{"iteration": int(h["iteration"]), "heat_w": float(h["heat_w"]),
                  "t_max_c": float(h["t_max"]), "dt_max_c": float(h["dt_max"])} for h in th.history],
        heat_w=float(th.heat_w), heat_by_kind_w={str(kind): float(w) for kind, w in th.heat_by_kind.items()},
        peak_c=float(pt), peak_x_mm=float(px), peak_y_mm=float(py),
        peak_layer=board.layer_name(th.layers[k]),
        peak_pad_ref=peak_ref, peak_pad_number=peak_num, peak_pad_distance_mm=peak_dist,
        loads=[{"name": name, "group": ld.group, "drop_ambient_v": float(first_drops.get(name, math.nan)),
                "drop_v": float(ld.drop_v)} for name, ld in drops.items()],
        load_heat=bool(options.load_heat), regulator_heat=str(options.regulator_heat),
        assumptions=_assumptions(board, options, th, out, h_w_m2k, cell_mm, cells,
                                 _rho_notes(project, th, settled)))
    if settled is False:
        last = th.history[-1]
        temp.warnings.append(
            f"Board temperature may not have settled: the last iteration still changed it by "
            f"{last['dt_max']:.2f} °C, more than {SETTLE_SHARE * 100:g} % of the "
            f"{last['t_max'] - ambient:.3g} °C peak rise; run more iterations.")
        out.warnings.append(temp.warnings[-1])        # the tree's too, as KiPI warns (cosim.py:276-279)
    out.thermal = temp
    apply_thermal_limits(out, options)
    return out


# ---------------------------------------------------------------------- cost and memory

# Above this many grid points the cells inside the board are counted from its area, not one
# by one (the estimate runs on the GUI thread).
EXACT_CELLS_MAX = 4_000_000


@dataclass(frozen=True)
class ThermalSize:
    """KiPI's thermal grid of a board before the model is built: the square cell, the grid,
    the cells inside the board and the copper layers; one unknown per cell and layer."""
    cell_mm: float
    nx: int
    ny: int
    cells: int                     # inside the board
    layers: int
    automatic: bool                # grid_mm 0: KiPI's automatic size
    exact: bool                    # counted cell by cell (False: board area / cell area)

    @property
    def unknowns(self) -> int:
        return self.cells * self.layers


def thermal_size(board: Board, options: ThermalOptions) -> ThermalSize:
    """The grid KiPI's ``BoardThermalModel`` will build (kipi/dc/thermal/model.py:179-188, the
    board shape of ``board_geometry``), without building it."""
    geom = board_geometry(board.snapshot)
    minx, miny, maxx, maxy = geom.bounds
    span = max(maxx - minx, maxy - miny)
    automatic = not options.grid_mm > 0
    d = max(span / 250.0, 0.2) if automatic else float(options.grid_mm)
    nx, ny = max(2, int(math.ceil((maxx - minx) / d))), max(2, int(math.ceil((maxy - miny) / d)))
    exact = nx * ny <= EXACT_CELLS_MAX
    if exact:
        X, Y = np.meshgrid(minx + (np.arange(nx) + 0.5) * d, miny + (np.arange(ny) + 0.5) * d)
        cells = int(np.count_nonzero(shapely.contains_xy(geom, X, Y)))
    else:
        cells = int(round(geom.area / d ** 2))
    return ThermalSize(d, nx, ny, cells, len(board.snapshot.stackup.copper), automatic, exact)


# Memory of KiPI's thermal model (its grid and one SuperLU factorization) above the process it
# is built in: the peak resident memory added by BoardThermalModel on plain square boards of 2
# to 10 copper layers (0.035 mm copper, 0.2 mm FR4), one fresh process each (macOS arm64,
# SuperLU): (copper layers, unknowns, MB). The laminate couples every cell, so the matrix is the
# full grid whatever the copper.
MEM_POINTS = ((2, 25_088, 62), (2, 100_352, 308), (2, 399_618, 1578), (4, 24_964, 99), (4, 79_524, 704),
              (4, 200_704, 1777), (6, 30_246, 200), (6, 119_286, 1206), (10, 25_000, 254),
              (10, 59_290, 804), (10, 148_840, 2297))
# Least-squares fit of those points, bytes = A · unknowns ** B · layers ** C: within -31 % to
# +20 % of each.
MEM_FIT_A, MEM_FIT_B, MEM_FIT_C = 95.9033, 1.2511, 0.9480
MEM_UNKNOWNS = (min(p[1] for p in MEM_POINTS), max(p[1] for p in MEM_POINTS))
MEM_LAYERS = (min(p[0] for p in MEM_POINTS), max(p[0] for p in MEM_POINTS))


def _size_text(n: float) -> str:
    return f"{n / 1e6:.0f} MB" if n < 1e9 else f"{n / 1e9:.3g} GB"


@dataclass(frozen=True)
class ThermalRam:
    bytes: float                   # the thermal model: its grid and one factorization
    total_bytes: Optional[float]   # this machine's memory; None when it can't be read
    risk: bool                     # it exceeds this machine's memory
    text: str


def thermal_ram_estimate(unknowns: float, layers: int, total_bytes: Optional[float]) -> ThermalRam:
    """Whether KiPI's thermal model of ``unknowns`` (cells × ``layers``) could exceed this
    machine's memory, from the fit of :data:`MEM_POINTS` (other boards may need more or less).
    The power tree's own meshes and factors come on top. With the thermal part of
    :mod:`coppernicus.engine.superlu_order` active (on, with SuperLU as KiPI's backend), the
    text says the fit overstates."""
    ram = _thermal_ram(unknowns, layers, total_bytes)
    if not superlu_order.active("thermal"):
        return ram
    return dataclasses.replace(ram, text=ram.text + (
        f" The factorization runs in SuperLU's symmetric order ({superlu_order.ENV}=thermal), which "
        "takes less memory than KiPI's order this fit was measured with, so this overstates."))


def _thermal_ram(unknowns: float, layers: int, total_bytes: Optional[float]) -> ThermalRam:
    need = MEM_FIT_A * float(unknowns) ** MEM_FIT_B * float(layers) ** MEM_FIT_C
    outside = not (MEM_UNKNOWNS[0] <= unknowns <= MEM_UNKNOWNS[1]
                   and MEM_LAYERS[0] <= layers <= MEM_LAYERS[1])
    how = (f"estimated from a fit measured on synthetic square boards of {MEM_LAYERS[0]} to {MEM_LAYERS[1]} "
           f"copper layers and {MEM_UNKNOWNS[0]:,} to {MEM_UNKNOWNS[1]:,} unknowns with SuperLU, "
           "within about -30 % / +20 % of each"
           + ("; this solve is outside the measured range" if outside else "")
           + "; other boards may need more or less")
    head = (f"The board temperature's model ({int(unknowns):,} unknowns: its grid and one factorization) "
            f"needs about {_size_text(need)}")
    tail = " The power tree's own meshes and factors come on top."
    if not total_bytes:
        return ThermalRam(need, None, False, f"{head} ({how}); this machine's memory cannot be read.{tail}")
    if need > total_bytes:
        return ThermalRam(need, float(total_bytes), True, (
            f"{head}, more than this machine's {_size_text(total_bytes)} of memory ({how}), so it could "
            f"swap: a coarser grid (Grid, mm) lowers it.{tail}"))
    return ThermalRam(need, float(total_bytes), False,
                      f"{head} of this machine's {_size_text(total_bytes)} ({how}).{tail}")


def thermal_cost_text(size: ThermalSize, last: Optional[Dict[str, float]] = None) -> str:
    """A rough cost of an electro-thermal solve before it starts: KiPI's grid and its unknowns,
    and the time of the last electro-thermal solve on this computer (``last``: its
    ``seconds`` and ``unknowns``), shown as measured, not scaled."""
    how = "automatic" if size.automatic else "set"
    count = "inside the board" if size.exact else "inside the board (from its area)"
    text = (f"{size.nx} × {size.ny} cells of {size.cell_mm:.3g} mm ({how}), {size.cells:,} {count}, "
            f"× {size.layers} copper layers: {size.unknowns:,} unknowns")
    try:
        seconds, unknowns = float(last["seconds"]), int(float(last["unknowns"]))
    except (TypeError, KeyError, ValueError):
        return text + "; no earlier electro-thermal solve on this computer to compare with"
    return (text + f"; the last electro-thermal solve on this computer took {seconds:.2g} s for "
            f"{unknowns:,} unknowns")
