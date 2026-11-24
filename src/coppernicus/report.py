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
                 notes: Sequence[str] = ()) -> None:
        """Sample and add one solved rail. ``limits`` are the user's (judged only against
        them); ``problems`` default to :func:`find_problems` with those limits."""
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
                "max_drop_mv": (result.v_ref - float(lv.min())) * 1e3 if lv.size else None,
                "j_scale_a_mm2": max(j_scale, 1e-12), "j_hi_a_mm2": j_hi,
                "j_point_max_a_mm2": j_hi, "triangles": int(len(lf.tris)),
                "grid": {"x0": grid.x0, "y0": grid.y0, "nx": grid.nx, "ny": grid.ny},
                "v": vid, "j": jid,
            })
        self.layers_added += len(result.layers)
        loads = [{"name": t.name, "ref": t.ref, "pins": len(t.pad_ids), "current_a": t.current,
                  "v_min_v": t.v_min, "drop_mv": result.drop(t) * 1e3, "drop_pct": result.drop_pct(t),
                  "exceeds": (None if limits.max_drop_pct is None
                              or not math.isfinite(result.drop_pct(t))
                              else bool(result.drop_pct(t) > limits.max_drop_pct)),
                  "pads": [list(p) for p in t.positions[:200]]} for t in result.loads]
        sources = [{"name": t.name, "ref": t.ref, "pins": len(t.pad_ids), "current_a": t.current,
                    "v_v": t.v_max, "pads": [list(p) for p in t.positions[:200]]} for t in result.sources]
        worst_load = max(loads, key=lambda d: d["drop_mv"] if math.isfinite(d["drop_mv"]) else -1,
                         default=None)
        hot = [p for p in problems if p.kind == "hotspot"]
        top_hot = max(hot, key=lambda p: p.value, default=None)
        top_via = max(result.vias, key=lambda v: v.current, default=None)
        busiest = max(result.layers, key=lambda lf: lf.power, default=None)
        judged = verdict(problems, limits, result)
        self.rails.append({
            "net": result.net, "label": label or result.net, "nets": list(result.nets),
            "v_ref_v": result.v_ref, "v_lo_v": v_lo, "v_hi_v": v_hi,
            "drop_max_mv": max(result.v_ref - v_lo, 0.0) * 1e3, "cell_mm": cell,
            **judged,
            "worst": {
                "load": worst_load and {k: worst_load[k] for k in ("name", "drop_mv", "drop_pct")},
                "max_drop_mv": result.max_drop * 1e3,
                "max_drop_pct": 100.0 * result.max_drop / abs(result.v_ref) if result.v_ref else None,
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
            "problems": [problem_dict(p) for p in problems],
            "layers": layers,
            "busiest": busiest.name if busiest else None,
            "assumptions": [*result.assumptions, *RAIL_CAVEATS,
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
                 budget_mb: float = DEFAULT_BUDGET_MB) -> Tuple[ReportBuilder, str]:
    """Build a report of all ``items`` and its HTML. When the first estimate of the cell size
    makes the page larger than ``MAX_FILE_MB``, the cells are coarsened and the page rebuilt,
    up to three times (coarser cells compress less well, so one step can fall short)."""
    def build(scale: float) -> ReportBuilder:
        b = ReportBuilder(title=title, board_name=board_name, outline=outline, project=project,
                          settings=settings, budget_mb=budget_mb, cell_scale=scale,
                          expected_layers=sum(len(i.result.layers) for i in items))
        for item in items:
            b.add_rail(item.result, item.limits, item.problems, item.label, item.notes)
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
