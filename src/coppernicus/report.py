"""An interactive, offline HTML report of solved rails: one self-contained file, no network.

Every rail and copper layer can be shown as voltage drop, voltage or current density, with
zoom, pan and a hover readout. The page also has toggles for sources, loads and
current-crowding spots, the problems table (click to zoom), a worst-values summary per rail,
and the assumptions and warnings.

**How the field gets into the file.** A large board's mesh (millions of triangles) is too big
to embed as-is. Each layer is therefore sampled on a regular grid of square *cells*. At each
cell centre the voltage is interpolated in the full-resolution triangle that holds it, and
|J| is that triangle's value. Both are quantised to 16 bits, delta-coded along rows, and
compressed with zlib. The page inflates them with the browser's own ``DecompressionStream``.
The hover readout shows the values at the centre of the cell under the pointer and says so,
with the cell size and the quantisation step; it never interpolates between cells. Summary
numbers (drops, losses, problems) come from the full-resolution result, not from the cells.

Entry points:

- :func:`write_report` takes all results at once, as the GUI's "Export report" will.
- :class:`ReportBuilder` adds rails one at a time and keeps only their compressed cells, so a
  multi-rail CLI solve need not hold every result in memory.
"""

from __future__ import annotations

import base64
import html
import json
import math
import re
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from coppernicus import __version__
from coppernicus.problems import Problem, find_problems
from coppernicus.project import Limits, Project
from coppernicus.results import LayerField, RailResult

J_CLIP_PERCENTILE = 99.5          # colour-scale top for |J|, as in the maps and the app
V_CODES = 65533                    # voltage levels: 0 = no copper, 1 = copper with no source
J_CODES = 65534                    # |J| levels: 0 = no copper
DEFAULT_BUDGET_MB = 27.0           # whole file, aimed at
MAX_FILE_MB = 30.0                 # the ceiling the owner set; write_report resamples above it
MIN_CELL_MM = 0.005
MAX_CELL_MM = 1.0
MESH_OVERSAMPLING = 6.0            # cells per median mesh edge, at most


# ---------------------------------------------------------------------- sampling on a grid

@dataclass
class Grid:
    """Cells of side ``cell`` mm; cell (i, j) has its centre at
    (x0 + (i + 0.5)·cell, y0 + (j + 0.5)·cell); ``nx`` columns, ``ny`` rows (y down)."""
    x0: float
    y0: float
    cell: float
    nx: int
    ny: int

    def centres_x(self) -> np.ndarray:
        return self.x0 + (np.arange(self.nx) + 0.5) * self.cell

    def centres_y(self) -> np.ndarray:
        return self.y0 + (np.arange(self.ny) + 0.5) * self.cell


def grid_for(bbox: Sequence[float], cell: float) -> Grid:
    """The grid of ``cell`` mm covering ``bbox`` = (x_min, y_min, x_max, y_max)."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    nx = max(1, int(math.ceil((x1 - x0) / cell)))
    ny = max(1, int(math.ceil((y1 - y0) / cell)))
    return Grid(x0, y0, cell, nx, ny)


def cell_triangles(points: np.ndarray, tris: np.ndarray, grid: Grid) -> np.ndarray:
    """For every cell of ``grid`` (shape (ny, nx)), the index of a triangle that contains the
    cell's centre, or -1. Exact point-in-triangle tests: each triangle is tested against the
    cell centres inside its bounding box. Triangles are grouped by the shape of that box (in
    cells), and each group is swept over its offsets, vectorised over the group."""
    out = np.full(grid.ny * grid.nx, -1, dtype=np.int64)
    if len(tris) == 0:
        return out.reshape(grid.ny, grid.nx)
    p = points[tris]                                          # (M, 3, 2)
    lo, hi = p.min(axis=1), p.max(axis=1)
    # Cell-centre index ranges inside each triangle's box.
    i0 = np.maximum(np.ceil((lo[:, 0] - grid.x0) / grid.cell - 0.5), 0).astype(np.int64)
    i1 = np.minimum(np.floor((hi[:, 0] - grid.x0) / grid.cell - 0.5), grid.nx - 1).astype(np.int64)
    j0 = np.maximum(np.ceil((lo[:, 1] - grid.y0) / grid.cell - 0.5), 0).astype(np.int64)
    j1 = np.minimum(np.floor((hi[:, 1] - grid.y0) / grid.cell - 0.5), grid.ny - 1).astype(np.int64)
    w, h = i1 - i0 + 1, j1 - j0 + 1
    # Barycentric maps: (l0, l1) = inv([[x0-x2, x1-x2], [y0-y2, y1-y2]]) @ (q - p2).
    a, b = p[:, 0, 0] - p[:, 2, 0], p[:, 1, 0] - p[:, 2, 0]
    c, d = p[:, 0, 1] - p[:, 2, 1], p[:, 1, 1] - p[:, 2, 1]
    det = a * d - b * c
    keep = np.flatnonzero((w > 0) & (h > 0) & (np.abs(det) > 1e-18))
    if not keep.size:
        return out.reshape(grid.ny, grid.nx)
    inv = 1.0 / det[keep]
    coef = np.column_stack([d[keep] * inv, -b[keep] * inv, -c[keep] * inv, a[keep] * inv])
    # Offsets of the first cell centre from p2, per triangle.
    ox = grid.x0 + (i0[keep] + 0.5) * grid.cell - p[keep, 2, 0]
    oy = grid.y0 + (j0[keep] + 0.5) * grid.cell - p[keep, 2, 1]
    wk, hk, i0k, j0k = w[keep], h[keep], i0[keep], j0[keep]
    shape_key = wk * (grid.ny + 1) + hk
    order = np.argsort(shape_key, kind="stable")
    bounds = np.flatnonzero(np.diff(shape_key[order])) + 1
    tol = 1e-9
    for g in np.split(order, bounds):
        gw, gh = int(wk[g[0]]), int(hk[g[0]])
        A, B, C, D = coef[g, 0], coef[g, 1], coef[g, 2], coef[g, 3]
        gx, gy, gi, gj, tri_ids = ox[g], oy[g], i0k[g], j0k[g], keep[g]
        for dj in range(gh):
            qy = gy + dj * grid.cell
            by, dy = B * qy, D * qy
            row = (gj + dj) * grid.nx + gi
            for di in range(gw):
                qx = gx + di * grid.cell
                l0 = A * qx + by
                l1 = C * qx + dy
                inside = (l0 >= -tol) & (l1 >= -tol) & (l0 + l1 <= 1.0 + tol)
                out[row[inside] + di] = tri_ids[inside]
    return out.reshape(grid.ny, grid.nx)


def sample_layer(layer: LayerField, grid: Grid) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Voltage and |J| at every cell centre, from the full-resolution layer: V interpolated in
    the triangle holding the centre, |J| that triangle's (constant) value. Returns
    (copper mask, V, J), each (ny, nx); V is NaN on copper with no path to a source."""
    tri = cell_triangles(layer.points, layer.tris, grid)
    mask = tri >= 0
    v = np.full(tri.shape, np.nan)
    j = np.zeros(tri.shape)
    k = tri[mask]
    if k.size:
        p = layer.points[layer.tris[k]]
        cy, cx = np.nonzero(mask)
        qx = grid.x0 + (cx + 0.5) * grid.cell - p[:, 2, 0]
        qy = grid.y0 + (cy + 0.5) * grid.cell - p[:, 2, 1]
        a, b = p[:, 0, 0] - p[:, 2, 0], p[:, 1, 0] - p[:, 2, 0]
        c, d = p[:, 0, 1] - p[:, 2, 1], p[:, 1, 1] - p[:, 2, 1]
        inv = 1.0 / (a * d - b * c)
        l0 = (d * qx - b * qy) * inv
        l1 = (a * qy - c * qx) * inv
        nodes = layer.voltage[layer.tris[k]]
        v[mask] = l0 * nodes[:, 0] + l1 * nodes[:, 1] + (1.0 - l0 - l1) * nodes[:, 2]
        jk = layer.current_density[k]
        j[mask] = np.where(np.isfinite(jk), jk, 0.0)
    return mask, v, j


# ---------------------------------------------------------------------- encoding

def quantise_v(mask: np.ndarray, v: np.ndarray, v_lo: float, v_hi: float) -> np.ndarray:
    """uint16 codes: 0 no copper, 1 copper with no source, 2.. the voltage over [v_lo, v_hi]."""
    span = max(v_hi - v_lo, 1e-15)
    q = np.zeros(v.shape, dtype=np.uint16)
    live = mask & np.isfinite(v)
    q[mask & ~live] = 1
    q[live] = 2 + np.clip(np.rint((v[live] - v_lo) / span * (V_CODES - 1)), 0, V_CODES - 1).astype(np.uint16)
    return q


def quantise_j(mask: np.ndarray, j: np.ndarray, j_hi: float) -> np.ndarray:
    """uint16 codes: 0 no copper, 1.. |J| over [0, j_hi]."""
    span = max(j_hi, 1e-15)
    q = np.zeros(j.shape, dtype=np.uint16)
    q[mask] = 1 + np.clip(np.rint(j[mask] / span * (J_CODES - 1)), 0, J_CODES - 1).astype(np.uint16)
    return q


def encode(q: np.ndarray) -> str:
    """uint16 (ny, nx) → base64 of zlib(high bytes ‖ low bytes) of the row-wise differences
    (mod 2¹⁶). The page undoes it with a running sum along each row."""
    d = np.diff(q.astype(np.int64), axis=1, prepend=0).astype(np.uint16)
    payload = (d >> 8).astype(np.uint8).tobytes() + (d & 0xFF).astype(np.uint8).tobytes()
    # Level 6: level 9 saves ~3 % for ~15× the time on a large layer (measured).
    return base64.b64encode(zlib.compress(payload, 6)).decode("ascii")


def decode(text: str, shape: Tuple[int, int]) -> np.ndarray:
    """Inverse of :func:`encode` (used by the tests to check the page's data)."""
    raw = np.frombuffer(zlib.decompress(base64.b64decode(text)), dtype=np.uint8)
    n = shape[0] * shape[1]
    d = (raw[:n].astype(np.uint16) << 8) | raw[n:].astype(np.uint16)
    return (np.cumsum(d.reshape(shape).astype(np.int64), axis=1) % 65536).astype(np.uint16)


# ---------------------------------------------------------------------- the report

BYTES_PER_CELL = 1.3        # base64 bytes per cell, V and |J| together (measured: 1.0–1.5)
MAX_CELLS_PER_LAYER = 12_000_000     # keeps one decoded layer at about 50 MB in the browser
MAX_SIDE = 8192                      # canvas side limit, with margin


@dataclass
class RailReport:
    """One rail to put in a report: the result, the limits it is judged against, and
    optionally its problems (found here when not given) and a label (default: the net)."""
    result: RailResult
    limits: Limits = field(default_factory=Limits)
    problems: Optional[List[Problem]] = None
    label: str = ""
    notes: Sequence[str] = ()
    # The names of its estimated draws (regulator inputs and links a project rail feeds);
    # None: told by the engine's name suffix ", estimated)".
    estimated: Optional[Sequence[str]] = None
    # Its verdict as already judged (a power-tree group: the tree's own), instead of judging
    # ``problems`` against ``limits`` here.
    judged: Optional[Mapping[str, Any]] = None
    # A power-tree group's loads as the tree judged them (:func:`tree_group_loads`): the
    # loads table and the worst card show these instead of the copper drop of the rail.
    tree_loads: Optional[Sequence[Mapping[str, Any]]] = None


def _median_edge_mm(result: RailResult) -> float:
    areas = np.concatenate([lf.tri_area for lf in result.layers]) if result.layers else np.zeros(0)
    areas = areas[areas > 0]
    return math.sqrt(4.0 / math.sqrt(3.0) * float(np.median(areas))) if areas.size else 0.1


def _bbox(layer: LayerField) -> Tuple[float, float, float, float]:
    lo, hi = layer.points.min(axis=0), layer.points.max(axis=0)
    return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


def _cmap(name: str) -> List[int]:
    from matplotlib import colormaps
    rgb = (colormaps[name](np.linspace(0.0, 1.0, 256))[:, :3] * 255).round().astype(int)
    return [int(r) << 16 | int(g) << 8 | int(b) for r, g, b in rgb]


class ReportBuilder:
    """Collects rails for one report. :meth:`add_rail` samples the rail's layers at once and
    keeps only the compressed cells, so the full result can be dropped right after.

    The cell size is chosen per rail so that the whole file stays near ``budget_mb``: each
    rail gets its share of the cell budget by its number of layers (``expected_layers`` is
    the total over all rails, when known in advance). It is never finer than a sixth of the
    rail's median mesh edge (finer cells would add size, not information) nor than 5 µm."""

    def __init__(self, *, title: str = "", board_name: str = "",
                 outline: Sequence[np.ndarray] = (), project: str = "",
                 settings: Optional[Mapping[str, Any]] = None,
                 budget_mb: float = DEFAULT_BUDGET_MB, expected_layers: Optional[int] = None,
                 top: int = 10, cell_scale: float = 1.0):
        self.title = title or "Coppernicus report"
        self.board_name = board_name
        self.outline = [np.asarray(r, dtype=float) for r in outline]
        self.project = project
        self.settings = dict(settings or {})
        self.budget_mb = float(budget_mb)
        self.expected_layers = expected_layers
        self.top = max(1, int(top))           # hotspots and vias ranked per rail
        self.tree: Optional[Dict[str, Any]] = None   # a power-tree solve: its verdict and tables
        self.cell_scale = max(float(cell_scale), 1.0)   # > 1 coarsens every cell (a resample)
        self.rails: List[Dict[str, Any]] = []
        self.blocks: Dict[str, str] = {}
        self.cells_used = 0
        self.bytes_used = 0
        self.layers_added = 0
        self.seconds = 0.0

    # ------------------------------------------------------------------ rails
    def _cell_for(self, result: RailResult) -> float:
        # Bytes per cell: the estimate until a rail is in, then what the cells really took.
        per_cell = self.bytes_used / self.cells_used if self.cells_used else BYTES_PER_CELL
        budget = self.budget_mb * 1e6
        bytes_left = max(0.92 * budget - self.bytes_used, 0.08 * budget)
        expected = max(self.expected_layers or 0, self.layers_added + len(result.layers))
        left_layers = max(expected - self.layers_added, len(result.layers))
        share = bytes_left / max(per_cell, 0.3) * len(result.layers) / left_layers
        area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in map(_bbox, result.layers))
        cell = math.sqrt(area / max(share, 1.0))
        cell = max(cell, _median_edge_mm(result) / MESH_OVERSAMPLING, MIN_CELL_MM)
        for lf in result.layers:                     # per-layer limits for the browser
            x0, y0, x1, y1 = _bbox(lf)
            cell = max(cell, math.sqrt((x1 - x0) * (y1 - y0) / MAX_CELLS_PER_LAYER),
                       (x1 - x0) / MAX_SIDE, (y1 - y0) / MAX_SIDE)
        return min(cell * self.cell_scale, MAX_CELL_MM)

    def add_rail(self, result: RailResult, limits: Optional[Limits] = None,
                 problems: Optional[List[Problem]] = None, label: str = "",
                 notes: Sequence[str] = (), estimated: Optional[Sequence[str]] = None,
                 judged: Optional[Mapping[str, Any]] = None,
                 tree_loads: Optional[Sequence[Mapping[str, Any]]] = None) -> None:
        """Sample and add one solved rail. ``limits`` are the user's (judged only against
        them); ``problems`` default to :func:`find_problems` with those limits. ``estimated``
        names the loads that are estimated draws; without it, the engine's name suffix
        ", estimated)" tells them (results from the app, which has no list)."""
        from coppernicus.service.convert import RAIL_CAVEATS, problem_dict, verdict
        t0 = time.perf_counter()
        limits = limits or Limits()
        problems = problems if problems is not None else find_problems(result, limits, top=self.top)
        index = len(self.rails)
        cell = self._cell_for(result)
        finite = [lf.voltage[np.isfinite(lf.voltage)] for lf in result.layers]
        finite = [v for v in finite if v.size]
        v_lo = float(min(v.min() for v in finite)) if finite else result.v_ref
        v_hi = float(max(v.max() for v in finite)) if finite else result.v_ref
        layers = []
        for li, lf in enumerate(result.layers):
            x0, y0, x1, y1 = _bbox(lf)
            grid = grid_for((x0 - cell / 2, y0 - cell / 2, x1 + cell / 2, y1 + cell / 2), cell)
            mask, v, j = sample_layer(lf, grid)
            jd = lf.current_density[np.isfinite(lf.current_density)]
            j_hi = float(jd.max()) if jd.size else 1.0
            j_scale = float(np.percentile(jd, J_CLIP_PERCENTILE)) if jd.size else 1.0
            vid, jid = f"r{index}l{li}v", f"r{index}l{li}j"
            self.blocks[vid] = encode(quantise_v(mask, v, v_lo, v_hi))
            self.blocks[jid] = encode(quantise_j(mask, j, j_hi))
            self.cells_used += grid.nx * grid.ny
            self.bytes_used += len(self.blocks[vid]) + len(self.blocks[jid])
            lv = lf.voltage[np.isfinite(lf.voltage)]
            layers.append({
                "name": lf.name, "power_w": lf.power, "copper_area_mm2": float(lf.tri_area.sum()),
                "max_drop_mv": float(result.shown_drop_at(lv).max()) * 1e3 if lv.size else None,
                "j_scale_a_mm2": max(j_scale, 1e-12), "j_hi_a_mm2": j_hi,
                "j_point_max_a_mm2": j_hi, "triangles": int(len(lf.tris)),
                "grid": {"x0": grid.x0, "y0": grid.y0, "nx": grid.nx, "ny": grid.ny},
                "v": vid, "j": jid,
            })
        self.layers_added += len(result.layers)
        est = (set(estimated) if estimated is not None else
               {t.name for t in result.loads if t.name.endswith(", estimated)")})
        loads = [{"name": t.name, "ref": t.ref, "pins": len(t.pad_ids), "current_a": t.current,
                  "estimated": t.name in est,
                  "v_min_v": t.v_min, "v_worst_v": result.worst_v(t),   # toward 0 V on a negative rail
                  "drop_mv": result.drop(t) * 1e3, "drop_pct": result.drop_pct(t),
                  "exceeds": (None if limits.max_drop_pct is None
                              or not math.isfinite(result.drop_pct(t))
                              else bool(result.drop_pct(t) > limits.max_drop_pct)),
                  "pads": [list(p) for p in t.positions[:200]]} for t in result.loads]
        if tree_loads is not None:          # a tree group: the loads as the tree judged them
            terms = {t.name: t for t in result.loads}
            loads = []
            for row in tree_loads:
                t = terms.get(row["name"])
                loads.append({**row, "ref": t.ref if t else row["name"].split(" ")[0],
                              "pins": len(t.pad_ids) if t else 0, "estimated": False,
                              "v_min_v": t.v_min if t else None,
                              "pads": [list(p) for p in t.positions[:200]] if t else []})
        sources = [{"name": t.name, "ref": t.ref, "pins": len(t.pad_ids), "current_a": t.current,
                    "v_v": t.v_max, "pads": [list(p) for p in t.positions[:200]]} for t in result.sources]

        def size(d):
            x = d.get("drop_mv")
            return x if x is not None and math.isfinite(x) else -math.inf
        worst_load = max(loads, key=size, default=None)
        hot = [p for p in problems if p.kind == "hotspot"]
        top_hot = max(hot, key=lambda p: p.value, default=None)
        top_via = max(result.vias, key=lambda v: v.current, default=None)
        busiest = max(result.layers, key=lambda lf: lf.power, default=None)
        judged = dict(judged) if judged is not None else verdict(problems, limits, result)
        judged["verdict_text"] = verdict_text(judged)
        shift = -1 if result.is_shift() else 1      # the drop as the app shows it (shown_drop_at)
        worst_mv = shift * result.max_drop * 1e3
        worst_keys = ("name", "drop_mv", "drop_pct") + (("nominal_v",) if tree_loads is not None else ())
        self.rails.append({
            "net": result.net, "label": label or result.net, "nets": list(result.nets),
            "tree": tree_loads is not None,
            "v_ref_v": result.v_ref, "v_lo_v": v_lo, "v_hi_v": v_hi,
            # The drop as the app shows it (RailResult.shown_drop_at): drop = drop_sign · (v_ref
            # − V), toward 0 V on a negative rail, a rise above 0 V on a ground, and on a ground
            # below 0 V (a shift) its depth below 0 V. drop_word names it: drop, rise or shift.
            "drop_sign": int(result.sign) * shift, "drop_unit": result.shown_drop_unit(),
            "drop_word": result.drop_word(),
            "drop_max_mv": max(float(result.shown_drop_at([v_lo, v_hi]).max()), 0.0) * 1e3, "cell_mm": cell,
            **judged,
            "worst": {
                "load": worst_load and {k: worst_load.get(k) for k in worst_keys},
                "max_drop_mv": worst_mv,
                "max_drop_pct": worst_mv / 10.0 / abs(result.v_ref) if result.v_ref else None,
                "power_loss_w": result.power_loss,
                "busiest_layer": busiest.name if busiest else None,
                "hotspot": top_hot and {"mean_j_a_mm2": top_hot.data.get("mean_j_a_mm2", top_hot.value),
                                        "peak_j_a_mm2": top_hot.data.get("peak_j_a_mm2"),
                                        "area_mm2": top_hot.data.get("area_mm2"), "layer": top_hot.layer,
                                        "x_mm": top_hot.x, "y_mm": top_hot.y},
                "via": top_via and {"current_a": top_via.current, "x_mm": top_via.position[0],
                                    "y_mm": top_via.position[1], "drill_mm": top_via.drill_mm},
                "j_point_max_a_mm2": result.j_max,
            },
            "loss_breakdown_w": result.loss_breakdown(),
            "loads": loads, "sources": sources,
            "problems": [{**problem_dict(p), "kind_label": kind_label(p)} for p in problems],
            "layers": layers,
            "busiest": busiest.name if busiest else None,
            # A tree group is solved with its ground return: the caveats of a rail solved
            # alone do not apply to it.
            "assumptions": [*result.assumptions, *(RAIL_CAVEATS if tree_loads is None else ()),
                            f"Maps are sampled on {cell:.4g} mm cells. The readout gives the values at "
                            "the centre of the cell under the pointer, from the full-resolution "
                            "solution; summary numbers and problems use the full mesh."],
            "warnings": list(result.warnings),
            "notes": list(notes),
            "stats": {"triangles": int(result.stats.get("triangles", 0)),
                      "unknowns": int(result.stats.get("unknowns", 0)),
                      "solve_s": result.stats.get("seconds")},
        })
        self.seconds += time.perf_counter() - t0

    # ------------------------------------------------------------------ output
    def meta(self) -> Dict[str, Any]:
        """Everything but the cells, as plain JSON data. Floats keep full precision: the
        quantisation references (v_lo_v, v_hi_v, j_hi_a_mm2) must decode to the stored step."""
        return _plain({
            "title": self.title, "board": self.board_name, "project": self.project,
            "generated": time.strftime("%Y-%m-%d %H:%M"), "version": __version__,
            "settings": _shown_settings(self.settings),
            "outline": [np.round(r, 4).tolist() for r in self.outline],
            "codes": {"v": V_CODES, "j": J_CODES},
            "cmaps": {"viridis": _cmap("viridis"), "magma": _cmap("magma")},
            "rails": self.rails,
            **({"tree": self.tree} if self.tree else {}),
        })

    def html(self) -> str:
        meta = json.dumps(self.meta(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        # "<" only occurs inside JSON strings; as < no text can end or confuse the script
        # block ("</script>", "<!--"), and JSON.parse reads it back unchanged.
        meta = meta.replace("<", "\\u003c")
        blocks = "\n".join(f'<script type="application/octet-stream" id="{k}">{v}</script>'
                           for k, v in self.blocks.items())
        values = {"TITLE": html.escape(self.title), "STYLE": _asset("report.css"),
                  "SCRIPT": _asset("report.js"), "META": meta, "BLOCKS": blocks}
        # One pass over the template: inserted text (net names, data) is never scanned again.
        return re.sub(r"__(TITLE|STYLE|SCRIPT|META|BLOCKS)__", lambda m: values[m.group(1)],
                      _asset("report.html"))

    def write(self, path, text: Optional[str] = None) -> Path:
        """Write the report (creating folders) and return its path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.html() if text is None else text, encoding="utf-8")
        return path

    def size_warning(self, path) -> Optional[str]:
        """A warning when the written file is above the ~30 MB ceiling (the cell size was
        estimated before all rails were in), else None."""
        size = Path(path).stat().st_size
        if size <= MAX_FILE_MB * 1e6:
            return None
        return (f"The HTML report is {size / 1e6:.1f} MB, above the {MAX_FILE_MB:g} MB aimed at: it may "
                "open slowly. Report fewer rails at a time (--rail), or write it with write_report, "
                "which resamples to fit.")


ResultLike = Union[RailResult, RailReport]


def write_report(results: Iterable[ResultLike], project: Optional[Project], path, *,
                 outline: Sequence[np.ndarray] = (), board_name: str = "", title: str = "",
                 budget_mb: float = DEFAULT_BUDGET_MB) -> Path:
    """Write one offline HTML report for solved rails and return its path.

    ``results`` are :class:`RailResult` objects (their limits are looked up in ``project``
    by net) or :class:`RailReport` items (which carry their own limits, problems and label).
    ``outline`` is the board outline (``Board.outline()``); ``board_name`` names the board.
    """
    items = [r if isinstance(r, RailReport) else RailReport(
        r, (project.rail(r.net).limits if project is not None and project.rail(r.net) else Limits()))
        for r in results]
    settings = dict(vars(project.settings)) if project is not None else {}
    name = (Path(project.path).name if project is not None and project.path else "")
    builder, text = build_report(items, title=title or f"Coppernicus report: {name or board_name or 'rails'}",
                                 board_name=board_name, outline=outline, project=name,
                                 settings=settings, budget_mb=budget_mb)
    return builder.write(path, text)


def build_report(items: Sequence[RailReport], *, title: str = "", board_name: str = "",
                 outline: Sequence[np.ndarray] = (), project: str = "",
                 settings: Optional[Mapping[str, Any]] = None,
                 budget_mb: float = DEFAULT_BUDGET_MB,
                 tree: Optional[Mapping[str, Any]] = None) -> Tuple[ReportBuilder, str]:
    """Build a report of all ``items`` and its HTML. When the first estimate of the cell size
    makes the page larger than ``MAX_FILE_MB``, the cells are coarsened and the page rebuilt,
    up to three times (coarser cells compress less well, so one step can fall short)."""
    def build(scale: float) -> ReportBuilder:
        b = ReportBuilder(title=title, board_name=board_name, outline=outline, project=project,
                          settings=settings, budget_mb=budget_mb, cell_scale=scale,
                          expected_layers=sum(len(i.result.layers) for i in items))
        for item in items:
            b.add_rail(item.result, item.limits, item.problems, item.label, item.notes,
                       item.estimated, item.judged, item.tree_loads)
        b.tree = tree
        return b

    scale, seconds = 1.0, 0.0
    builder = build(scale)
    text = builder.html()
    for _ in range(3):
        size = len(text.encode("utf-8"))
        if size <= MAX_FILE_MB * 1e6:
            break
        seconds += builder.seconds
        scale *= 1.05 * math.sqrt(size / (0.9 * MAX_FILE_MB * 1e6))     # cells ~ 1 / side²
        builder = build(scale)
        text = builder.html()
    builder.seconds += seconds
    return builder, text


def write_tree_report(tree, doc: Mapping[str, Any], path, *, outline: Sequence[np.ndarray] = (),
                      board_name: str = "", project: str = "", title: str = "", top: int = 10,
                      settings: Optional[Mapping[str, Any]] = None,
                      budget_mb: float = DEFAULT_BUDGET_MB) -> Path:
    """The report of a power-tree solve: every group (its maps, its problems and the tree's
    own verdict for it, from ``doc``, the :func:`coppernicus.power_tree.tree_summary` of
    ``tree``), with the tree's verdict and its VRM, load and link tables."""
    from coppernicus.power_tree import group_limits
    from coppernicus.problems import find_tree_problems
    groups = {g["group"]: g for g in doc.get("groups", [])}
    items = []
    for key, res in tree.groups.items():
        g = groups.get(key, {})
        judged = {"verdict": g.get("verdict", "not_judged"), "limits": g.get("limits") or {},
                  "failed": g.get("failed", 0), "not_judged": g.get("not_judged") or [],
                  "verdict_note": g.get("verdict_note", "")}
        items.append(RailReport(res, group_limits(tree, key)[0], find_tree_problems(tree, key, top=top),
                                label=f"{key} ({res.kind} group)", judged=judged,
                                tree_loads=tree_group_loads(tree, key)))
    meta = {k: doc.get(k) for k in ("verdict", "verdict_note", "not_judged", "vrms", "loads", "links")}
    if getattr(tree, "thermal", None) is not None and doc.get("thermal"):
        # The board temperature: its summary, and each copper layer's map inside the page.
        meta["thermal"] = {**doc["thermal"], "maps": thermal_map_images(tree.thermal, outline)}
    builder, text = build_report(items, title=title or f"Coppernicus power tree: {project or board_name}",
                                 board_name=board_name, outline=outline, project=project,
                                 settings=settings, budget_mb=budget_mb, tree=meta)
    return builder.write(path, text)


def tree_group_loads(tree, key: str) -> List[Dict[str, Any]]:
    """The loads of tree group ``key`` as the tree judged them: each load and each regulator
    input the group feeds ("U2 (input)"), with the voltage across its supply (input) and ground
    pins, its drop from its nominal voltage and the tree's verdict (``exceeds``: None when not
    judged; ``status`` says why when it could not be)."""
    def mv(v):
        return None if v is None else v * 1e3
    rows = [{"name": ld.name, "current_a": ld.current_a, "nominal_v": ld.nominal_v,
             "v_across_v": ld.v_across_v, "v_worst_v": ld.v_worst_v, "drop_mv": mv(ld.drop_v),
             "drop_pct": ld.drop_pct, "exceeds": None if ld.passed is None else not ld.passed,
             "status": ld.status} for ld in tree.loads if ld.group == key]
    rows += [{"name": f"{v.name} (input)", "current_a": v.i_in_a, "nominal_v": v.input_nominal_v,
              "v_across_v": v.v_in_worst_v, "v_worst_v": v.v_in_worst_v, "drop_mv": mv(v.input_drop_v),
              "drop_pct": v.input_drop_pct,
              "exceeds": None if v.input_passed is None else not v.input_passed,
              "status": v.input_status} for v in tree.vrms if v.input_net and v.input_group == key]
    return rows


def thermal_map_images(temp, outline: Sequence[np.ndarray] = (), dpi: int = 110) -> List[Dict[str, str]]:
    """The board temperature's map of each copper layer (:func:`coppernicus.temperature.write_png`,
    every layer on one scale from the ambient to the peak) as a PNG data URI, for the page."""
    import tempfile

    from coppernicus.temperature import write_png
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        for m in temp.maps:
            png = Path(tmp) / "layer.png"
            write_png(temp, m.layer_name, png, outline, dpi=dpi)
            out.append({"layer": m.layer_name,
                        "png": "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode("ascii")})
    return out


AC_PAGE_CSS = "\n".join([
    "body { font: 14px/1.45 system-ui, sans-serif; margin: 0 auto; max-width: 1100px; padding: 16px;",
    "  color: #1d2329; background: #fff; }",
    "@media (prefers-color-scheme: dark) { body { color: #e6e8eb; background: #15181c; }",
    "  th, td { border-color: #333a42; } }",
    "h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; }",
    ".muted { color: #7a838c; font-weight: normal; } .verdict { font-weight: 600; }",
    "img { width: 100%; max-width: 900px; height: auto; display: block; margin: 8px 0; }",
    "table { border-collapse: collapse; width: 100%; margin: 8px 0; }",
    "th, td { border-bottom: 1px solid #d6dadf; padding: 4px 8px; text-align: left; }",
    "th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }",
])


def write_ac_report(doc: Mapping[str, Any], path, *, project: str = "", board_name: str = "",
                    title: str = "") -> Path:
    """One offline HTML page of an AC impedance document (``coppernicus ac``'s summary): the
    verdict, and per rail its |Z|(f) plot (the PNG the solve wrote, kept inside the page), its
    probes with their worst |Z| against their targets, the assumptions and the warnings."""
    from coppernicus.impedance import hz_text, ohm_text

    def esc(x) -> str:
        return html.escape(str(x if x is not None else "–"))

    def judged(passed) -> str:
        return "FAIL" if passed is False else "pass" if passed else "not judged"
    parts = []
    for r in doc.get("rails", []):
        png = Path(r.get("png_file") or "")
        data = base64.b64encode(png.read_bytes()).decode("ascii") if png.is_file() else ""
        img = f'<img src="data:image/png;base64,{data}" alt="|Z|(f) of {esc(r["rail"])}">' if data else ""
        rows = "".join(
            f"<tr><td>{esc(p['name'])}</td><td>{esc(p['net'])}</td>"
            f"<td class=num>{esc(ohm_text(p.get('worst_z_ohm')))}</td>"
            f"<td class=num>{esc(hz_text(p.get('worst_hz')))}</td>"
            f"<td class=num>{esc(ohm_text(p.get('target_ohm')))}</td>"
            f"<td>{esc(judged(p.get('passed')))}</td></tr>" for p in r.get("probes", []))
        notes = "".join(f"<li>{esc(a)}</li>" for a in r.get("assumptions", []))
        warns = "".join(f"<li>{esc(w)}</li>" for w in r.get("warnings", []))
        parts += [f"<section><h2>Rail {esc(r['rail'])} <span class=muted>(ground {esc(r.get('ground'))}, "
                  f"source {esc(r.get('source'))})</span></h2>", img,
                  "<table><thead><tr><th>Probe</th><th>Net</th><th class=num>Worst |Z|</th>"
                  "<th class=num>At</th><th class=num>Target</th><th>Verdict</th></tr></thead>",
                  f"<tbody>{rows}</tbody></table>",
                  f"<details><summary>Assumptions</summary><ul>{notes}</ul></details>",
                  f"<h3>Warnings</h3><ul>{warns}</ul>" if warns else "", "</section>"]
    head = title or f"Coppernicus impedance: {project or board_name}"
    where = " · ".join(x for x in (f"Project {project}" if project else "",
                                   f"board {board_name}" if board_name else "") if x)
    page = "\n".join([
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(head)}</title>", f"<style>{AC_PAGE_CSS}</style></head><body>",
        f"<h1>{esc(head)}</h1>", f"<p class=muted>{esc(where)}</p>",
        f"<p class=verdict>Verdict: {esc(doc.get('verdict'))}. {esc(doc.get('verdict_note', ''))}</p>",
        *parts,
        f"<p class=muted>Generated by Coppernicus {esc(__version__)}. Board files were only read. This "
        "page works offline: everything it shows is inside this file. Verdicts only against the "
        "probes' own targets.</p>", "</body></html>", ""])
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


NOISE_PAGE_CSS = AC_PAGE_CSS + "\n" + "\n".join([
    "figure { margin: 12px 0; } figcaption { color: #7a838c; font-size: 13px; }",
    ".scroll { overflow-x: auto; } td.fail { color: #c62828; font-weight: 600; }",
    "td.num { white-space: nowrap; }",
])


def _mv_text(x) -> str:
    """A voltage given in mV, in mV below 1 V and in V above (as ``coppernicus noise``)."""
    if x is None:
        return "–"
    return f"{x / 1e3:.4g} V" if abs(x) >= 999.95 else f"{x:.4g} mV"


def _noise_image_label(png: str) -> str:
    stem = Path(png).stem
    if stem.endswith("_h_field"):
        return "H field 5 mm above the top copper, strongest harmonic (an indicator, never judged)"
    if stem.endswith("_e_field"):
        return "E field 5 mm above the top copper, strongest harmonic (an indicator, never judged)"
    return "Waveforms: switch node, inductor current, ripple and each victim's receiver voltage"


def write_noise_report(doc: Mapping[str, Any], path, *, project: str = "", board_name: str = "",
                       title: str = "") -> Path:
    """One offline HTML page of a ``noise`` document (``coppernicus noise``'s summary): the
    verdict, and per aggressor its waveforms and field-map PNGs (the files the solve wrote, kept
    inside the page), the ripple (never judged), the victims with their peak, margin, verdict
    and reason, the stand-ins, the assumptions and the warnings."""
    from coppernicus.coupled_noise import victim_verdict_words
    from coppernicus.impedance import hz_text

    def esc(x) -> str:
        return html.escape(str(x if x is not None else "–"))

    def num(x, fmt: str = ".4g", scale: float = 1.0) -> str:
        return "–" if x is None else format(x * scale, fmt)
    parts = []
    for a in doc.get("aggressors", []):
        name = a.get("aggressor")
        figures = []
        for png in a.get("png_files", []):
            p = Path(png or "")
            if not p.is_file():
                continue
            label = _noise_image_label(png)
            data = base64.b64encode(p.read_bytes()).decode("ascii")
            figures.append(f'<figure><img src="data:image/png;base64,{data}" alt="{esc(label)} of '
                           f'{esc(name)}"><figcaption>{esc(label)}</figcaption></figure>')
        point = ", ".join([
            f"{num(a.get('vin_v'), 'g')} V → {num(a.get('vout_v'), 'g')} V", f"{num(a.get('iout_a'))} A",
            f"duty {num(a.get('duty'))}", hz_text(a.get("fsw_hz")),
            f"edges {num(a.get('rise_s'), 'g', 1e9)} / {num(a.get('fall_s'), 'g', 1e9)} ns"])
        loops = "".join(
            f"<li>phase {esc(h.get('phase'))}: hot loop {esc(num(h.get('loop_l_h'), '.3g', 1e9))} nH, "
            f"switch node {esc(num(h.get('c_sw_f'), '.4g', 1e12))} pF; "
            + (f"rings at {esc(hz_text(h.get('f_ring_hz')))}, ζ {esc(num(h.get('zeta'), '.3g'))}"
               if h.get("f_ring_hz") else "no ringing") + "</li>" for h in a.get("hot_loops", []))
        ripple = "".join(
            f"<tr><td>{esc(r.get('name'))}</td><td>{esc(r.get('where'))}</td>"
            f"<td>{esc(r.get('net'))} − {esc(r.get('ground_net'))}</td>"
            f"<td class=num>{esc(_mv_text(r.get('pp_20mhz_mv')))}</td>"
            f"<td class=num>{esc(_mv_text(r.get('below_20mhz_mv')))} / "
            f"{esc(_mv_text(r.get('above_20mhz_mv')))}</td>"
            f"<td class=num>{esc(_mv_text(r.get('pp_mv')))}</td>"
            f"<td class=num>{esc(_mv_text(r.get('rms_mv')))}</td></tr>" for r in a.get("ripple", []))
        rows = []
        for v in a.get("victims", []):
            verdict, reason = victim_verdict_words(v, a.get("failed", []))
            rows.append(f"<tr><td>{esc(v.get('name'))}</td><td>{esc(v.get('net'))}</td>"
                        f"<td class=num>{esc(_mv_text(v.get('peak_mv')))}</td>"
                        f"<td>{esc(v.get('dominant') or '–')}</td>"
                        f"<td class=num>{esc(_mv_text(v.get('margin_mv')))}</td>"
                        f"<td class=num>{esc(num(v.get('ratio'), '.3g', 100.0))}</td>"
                        f"<td{' class=fail' if verdict == 'FAIL' else ''}>{esc(verdict)}</td>"
                        f"<td>{esc(reason)}</td></tr>")
        stand_ins = list(a.get("stand_ins", []))
        stand_ins += [f"{v.get('name')}: {s}" for v in a.get("victims", []) for s in v.get("stand_ins", [])
                      if s not in a.get("stand_ins", [])]
        stand = ("<ul>" + "".join(f"<li>{esc(s)}</li>" for s in stand_ins) + "</ul>" if stand_ins else
                 "<p>None: no KiPI default stands in for a value.</p>")
        e = a.get("emi") or {}
        radiated = ""
        if e.get("radiated_dbuv_m") is not None:
            limit = (f"CISPR 32 class B {num(e.get('cispr32_b_dbuv_m'), '.0f')} dBµV/m for scale"
                     if e.get("cispr32_b_dbuv_m") is not None else "below the 30 MHz band")
            radiated = (f"<p>Radiated estimate at {esc(num(e.get('distance_m'), 'g'))} m: "
                        f"{esc(num(e.get('radiated_dbuv_m'), '.1f'))} dBµV/m at "
                        f"{esc(hz_text(e.get('radiated_hz')))} ({esc(limit)}; never judged).</p>")
        notes = "".join(f"<li>{esc(x)}</li>" for x in a.get("assumptions", []))
        warns = "".join(f"<li>{esc(w)}</li>" for w in a.get("warnings", []))
        parts += [f"<section><h2>Aggressor {esc(name)} <span class=muted>(source {esc(a.get('vrm'))}, "
                  f"{esc(a.get('topology'))})</span></h2>", f"<p>{esc(point)}</p>",
                  f"<ul>{loops}</ul>" if loops else "",
                  f"<p class=verdict>Victims: {esc(a.get('verdict') or 'partial')}. "
                  f"{esc(a.get('verdict_note', ''))}</p>", *figures,
                  "<h3>Ripple (never judged)</h3><div class=scroll><table><thead><tr><th>Ripple</th>"
                  "<th>Where</th><th>Between</th><th class=num>p-p, 20 MHz</th>"
                  "<th class=num>Below / above, 20 MHz</th><th class=num>p-p, full band</th>"
                  f"<th class=num>RMS</th></tr></thead><tbody>{ripple}</tbody></table></div>",
                  "<h3>Victims</h3><div class=scroll><table><thead><tr><th>Victim</th><th>Net</th>"
                  "<th class=num>Peak</th><th>Mostly</th><th class=num>Margin</th>"
                  "<th class=num>% of margin</th><th>Verdict</th><th>Reason</th></tr></thead>"
                  f"<tbody>{''.join(rows)}</tbody></table></div>",
                  "<h3>Stand-ins</h3><p class=muted>KiPI defaults standing in for a value: while one "
                  "is in play, a victim with a margin is not judged, whatever its peak.</p>", stand,
                  radiated, f"<details><summary>Assumptions</summary><ul>{notes}</ul></details>",
                  f"<h3>Warnings</h3><ul>{warns}</ul>" if warns else "", "</section>"]
    head = title or f"Coppernicus ripple and noise: {project or board_name}"
    where = " · ".join(x for x in (f"Project {project}" if project else "",
                                   f"board {board_name}" if board_name else "") if x)
    top_warns = "".join(f"<li>{esc(w)}</li>" for w in doc.get("warnings", []))
    page = "\n".join([
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(head)}</title>", f"<style>{NOISE_PAGE_CSS}</style></head><body>",
        f"<h1>{esc(head)}</h1>", f"<p class=muted>{esc(where)}</p>",
        f"<p class=verdict>Verdict: {esc(doc.get('verdict'))}. {esc(doc.get('verdict_note', ''))}</p>",
        f"<h3>Warnings</h3><ul>{top_warns}</ul>" if top_warns else "",
        *parts,
        f"<p class=muted>Generated by Coppernicus {esc(__version__)}. Board files were only read. This "
        "page works offline: everything it shows is inside this file. Verdicts only against the "
        "victims' noise margins; the ripple, the near field and the radiated estimate are never "
        "judged.</p>", "</body></html>", ""])
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


TOLERANCE_PAGE_CSS = NOISE_PAGE_CSS + "\n" + "td.missing { color: #b26a00; font-style: italic; }"


def write_tolerance_report(doc: Mapping[str, Any], path, *, project: str = "", board_name: str = "",
                           title: str = "") -> Path:
    """One offline HTML page of a ``tolerance`` document (``coppernicus tolerance``'s summary):
    the verdict, every failed and not-judged entry (those with no load row too: a misspelt key,
    a tolerance lost to a KiCad-fields rail), the warnings, and per load its four parts (mV
    below / above nominal, where each came from; a missing one says so, with its least value
    and what to do, never as 0), KiPI's transient estimate (shown, never judged) and the three
    checks (tolerance, total, allowed, margin, verdict, reason); then the assumptions and each
    solve's estimate."""
    from coppernicus.tolerance import CHECK_NAMES, PART_NAMES, PARTS

    def esc(x) -> str:
        return html.escape(str(x if x is not None else "–"))

    def side(x) -> str:
        return "–" if x is None else f"{x:.4g}"

    def pair(below, above) -> str:
        return f"{side(below)} / {side(above)} mV"
    failed = list(doc.get("failed", []))
    entries = "".join(
        f"<tr><td>{esc(e.get('item'))}</td><td>{esc(e.get('net'))}</td><td>{esc(e.get('limit'))}</td>"
        f"<td{' class=fail' if tag == 'FAIL' else ''}>{tag}</td><td>{esc(e.get('reason'))}</td></tr>"
        for tag, items in (("FAIL", failed), ("not judged", doc.get("not_judged", []))) for e in items)
    parts = []
    for ld in doc.get("loads", []):
        nominal = "no nominal" if ld.get("nominal_v") is None else f"nominal {ld['nominal_v']:g} V"
        via = f" from {ld['source']}" if ld.get("source") else ""
        probe = f", probe {ld['probe']}" if ld.get("probe") else ""
        verdict = {"pass": "PASS", "fail": "FAIL", "not_judged": "NOT JUDGED"}.get(
            ld.get("verdict"), "No tolerance set: the parts are shown, not judged")
        rows = []
        for kind in PARTS:
            p = (ld.get("parts") or {}).get(kind)
            if p is None:                 # missing, never 0: its least value where known, and why
                f = (ld.get("floors") or {}).get(kind)
                least = f"; at least {pair(f['below_mv'], f['above_mv'])} ({f['source']})" if f else ""
                rows.append(f"<tr><td>{esc(PART_NAMES[kind])}</td><td colspan=2 class=missing>"
                            f"{esc('missing' + least)}</td><td>–</td>"
                            f"<td>{esc((ld.get('hints') or {}).get(kind, ''))}</td></tr>")
            else:
                rows.append(f"<tr><td>{esc(PART_NAMES[kind])}</td>"
                            f"<td class=num>{esc(side(p['below_mv']))}</td>"
                            f"<td class=num>{esc(side(p['above_mv']))}</td><td>{esc(p.get('source'))}</td>"
                            f"<td>{esc(p.get('detail') or '')}</td></tr>")
        checks = []
        for kind, c in (ld.get("checks") or {}).items():
            if not c.get("limit"):
                continue
            passed = c.get("passed")
            tag = "FAIL" if passed is False else "PASS" if passed else "not judged"
            if passed is False:           # a failed check's words are in the document's failed list
                key = (ld.get("load"), ld.get("net"), c["limit"])
                reason = next((e.get("reason") for e in failed
                               if (e.get("item"), e.get("net"), e.get("limit")) == key), c.get("reason"))
            elif passed:
                reason = "within it on both sides, every part known"
            else:
                reason = c.get("reason")
            total = pair(c.get("below_mv"), c.get("above_mv"))
            allowed = pair(c.get("allowed_below_mv"), c.get("allowed_above_mv"))
            margin = pair(c.get("margin_below_mv"), c.get("margin_above_mv"))
            if not c.get("complete"):     # its sum is a least value, so its margin a most
                total += f" (at least: {', '.join(c.get('missing') or [])} missing)"
                margin += " (at most)" if c.get("margin_below_mv") is not None else ""
            checks.append(f"<tr><td>{esc(CHECK_NAMES.get(kind, kind))}</td><td>{esc(c['limit'])}</td>"
                          f"<td class=num>{esc(total)}</td><td class=num>{esc(allowed)}</td>"
                          f"<td class=num>{esc(margin)}</td><td{' class=fail' if tag == 'FAIL' else ''}>"
                          f"{tag}</td><td>{esc(reason)}</td></tr>")
        e = ld.get("transient_estimate")
        estimate = (f"<p>KiPI's transient estimate at {esc(ld.get('probe'))} (shown, never judged): "
                    f"{esc(pair(e.get('below_mv'), e.get('above_mv')))} below / above nominal, "
                    f"{esc(pair(e.get('below_20mhz_mv'), e.get('above_20mhz_mv')))} through 20 MHz.</p>"
                    if e else "")
        stand = ("<p>KiPI stand-ins behind an aggressor's ripple (its value not used): "
                 + esc("; ".join(ld["stand_ins"])) + "</p>" if ld.get("stand_ins") else "")
        parts += [f"<section><h2>Load {esc(ld.get('load'))} on {esc(ld.get('net'))} <span class=muted>"
                  f"({esc(nominal + via + probe)})</span></h2>", f"<p class=verdict>{esc(verdict)}</p>",
                  "<h3>Parts</h3><div class=scroll><table><thead><tr><th>Part</th>"
                  "<th class=num>Below nominal (mV)</th><th class=num>Above nominal (mV)</th><th>From</th>"
                  f"<th>Detail, or what to do</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>",
                  estimate, stand,
                  ("<h3>Checks</h3><div class=scroll><table><thead><tr><th>Check</th><th>Tolerance</th>"
                   "<th class=num>Total below / above</th><th class=num>Allowed below / above</th>"
                   "<th class=num>Margin below / above</th><th>Verdict</th><th>Reason</th></tr></thead>"
                   f"<tbody>{''.join(checks)}</tbody></table></div>") if checks else "", "</section>"]
    head = title or f"Coppernicus tolerance stack-up: {project or board_name}"
    where = " · ".join(x for x in (f"Project {project}" if project else "",
                                   f"board {board_name}" if board_name else "") if x)
    warns = "".join(f"<li>{esc(w)}</li>" for w in doc.get("warnings", []))
    notes = "".join(f"<li>{esc(a)}</li>" for a in doc.get("assumptions", []))
    costs = "".join(f"<li>{esc(e.get('solve'))}: {esc(e.get('text'))}</li>" for e in doc.get("estimates", []))
    page = "\n".join([
        "<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(head)}</title>", f"<style>{TOLERANCE_PAGE_CSS}</style></head><body>",
        f"<h1>{esc(head)}</h1>", f"<p class=muted>{esc(where)}</p>",
        f"<p class=verdict>Verdict: {esc(doc.get('verdict'))}. {esc(doc.get('verdict_note', ''))}</p>",
        ("<h3>Failed and not judged</h3><div class=scroll><table><thead><tr><th>Load</th><th>Net</th>"
         "<th>Tolerance</th><th>Verdict</th><th>Reason</th></tr></thead>"
         f"<tbody>{entries}</tbody></table></div>") if entries else "",
        f"<h3>Warnings</h3><ul>{warns}</ul>" if warns else "",
        *parts,
        f"<details><summary>Assumptions</summary><ul>{notes}</ul></details>",
        f"<details><summary>Each solve's cost, estimated before it ran</summary><ul>{costs}</ul></details>"
        if costs else "",
        f"<p class=muted>Generated by Coppernicus {esc(__version__)}. Board files were only read. This "
        "page works offline: everything it shows is inside this file. Verdicts only against the "
        "tolerances set in the project file, a check judged only when every part is known (a missing "
        "part is never 0); KiPI's transient estimate is shown, never judged.</p>", "</body></html>", ""])
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def kind_label(p: Problem) -> str:
    """The word for a problem's kind on the page, from its own title: a ground's "rise" is a
    "ground shift" when its title says so (a ground below 0 V)."""
    if p.kind == "rise":
        return "ground shift" if "shift" in p.title.lower() else "ground rise"
    return {"return": "ground return"}.get(p.kind, p.kind)


def verdict_text(judged: Mapping[str, Any]) -> str:
    """The verdict in words, for the page: the limits that were set, a failure, and every
    item a set limit could not judge (both when a rail fails and has such items)."""
    lim = judged.get("limits") or {}
    words = {"max_drop_pct": "load drop ≤ {:.3g} % of the source voltage",
             "max_current_density_a_mm2": "current-crowding mean |J| ≤ {:.4g} A/mm²",
             "max_via_current_a": "current per via ≤ {:.4g} A",
             "max_ground_rise_mv": "ground rise ≤ {:.4g} mV above 0 V"}
    parts = "; ".join(words[k].format(v) for k, v in lim.items() if k in words)
    unjudged = judged.get("not_judged") or []
    items = "; ".join(f"{u['item']} ({u['limit']}): {u['reason']}" for u in unjudged)
    failed = judged.get("failed", 0)
    if judged.get("verdict") == "fail":
        text = (f"Fails against the limits you set: {parts}. {failed} problem"
                f"{'' if failed == 1 else 's'} over a limit.")
        return f"{text} Not judged: {items}." if unjudged else text
    if judged.get("verdict") == "pass":
        return f"Within the limits you set: {parts}. Limits you did not set are not checked."
    if unjudged:
        return f"Not judged against the limits you set ({parts}): {items}."
    return "Not judged: no limits were set, so the problems below are ranked, not judged."


def _shown_settings(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """The settings as the page lists them, one line each: a mapping (copper_thickness_mm)
    as "B.Cu 0.07 mm, ...", left out when empty."""
    out = {}
    for key, value in settings.items():
        if isinstance(value, Mapping):
            if not value:
                continue
            value = ", ".join(f"{name} {float(mm):g} mm" for name, mm in sorted(value.items()))
        out[key] = value
    return out


def _plain(obj: Any) -> Any:
    """``obj`` as JSON data: numpy scalars and arrays made plain, NaN and inf made ``None``,
    tuples made lists. Unlike the service's JSON, floats are not rounded."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _plain(obj.tolist())
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, Path):
        return obj.as_posix()
    return obj


def _asset(name: str) -> str:
    """The page's template, stylesheet or script, shipped next to this module."""
    from importlib.resources import files
    return files("coppernicus").joinpath("report_assets", name).read_text(encoding="utf-8")
