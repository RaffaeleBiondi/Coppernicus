"""Per-layer colour maps of a solved rail, written as PNG files (headless, no Qt).

Two maps per copper layer: the voltage *drop* below the source (mV, per node, smooth) and
the current density (A/mm², per triangle). :func:`render_layer_map` draws one map (drop,
voltage or current), optionally zoomed to a region. KiCad's y axis points down, so the
plots invert y to look like the board in KiCad.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.tri import Triangulation

from coppernicus.problems import FAIL, Problem
from coppernicus.results import LayerField, RailResult

DROP_CMAP = "viridis"
J_CMAP = "magma"
J_CLIP_PERCENTILE = 99.5      # colour scale top for |J|: point maxima at corners are mesh-dependent


def safe_name(text: str) -> str:
    """A net name as a file name: "+3V3" -> "P3V3", "-5V" -> "N5V"."""
    text = text.replace("+", "P").replace("-", "N")
    return re.sub(r"[^A-Za-z0-9_.]+", "_", text).strip("_") or "net"


def safe_file_name(text: str) -> str:
    """Any other name (a project file's stem) as a file name, keeping letters, digits, ".",
    "_" and "-": "demo-relaxed.coppernicus" stays as it is."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._-") or "project"


def _triangulation(layer: LayerField) -> Triangulation:
    tri = Triangulation(layer.points[:, 0], layer.points[:, 1], layer.tris)
    tri.set_mask(np.isnan(layer.voltage[layer.tris]).any(axis=1))
    return tri


def _decorate(ax, outline: Sequence[np.ndarray], result: RailResult,
              problems: Iterable[Problem], layer_name: str):
    for ring in outline:
        closed = np.vstack([ring, ring[:1]])
        ax.plot(closed[:, 0], closed[:, 1], color="0.35", lw=0.8)
    for t in result.sources:
        xs, ys = zip(*t.positions)
        ax.scatter(xs, ys, marker="^", s=36, c="#1a9850", edgecolors="white", linewidths=0.6, zorder=5)
        ax.annotate(t.name, (xs[0], ys[0]), xytext=(4, 4), textcoords="offset points", fontsize=7,
                    color="#1a9850")
    for t in result.loads:
        xs, ys = zip(*t.positions)
        ax.scatter(xs, ys, marker="o", s=22, c="#d73027", edgecolors="white", linewidths=0.6, zorder=5)
        ax.annotate(t.name, (xs[0], ys[0]), xytext=(4, -9), textcoords="offset points", fontsize=7,
                    color="#d73027")
    for p in problems:
        if p.kind == "hotspot" and p.layer == layer_name:
            ax.scatter([p.x], [p.y], marker="x", s=40,
                       c="#ff0000" if p.severity == FAIL else "#fdae61", zorder=6)
    ax.set_aspect("equal")
    ax.invert_yaxis()                       # KiCad: y down
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")


FIELDS = ("drop", "voltage", "current")
DELTA_CMAP = "RdBu_r"       # red where the drop grew, blue where it shrank


def render_delta_map(result: RailResult, layer: LayerField, delta_mv: np.ndarray,
                     outline: Sequence[np.ndarray], path, title: str, dpi: int = 110) -> Dict[str, object]:
    """A colour map of a drop difference on ``layer``'s mesh (mV per node; NaN where it could
    not be compared, drawn as 0), symmetric around 0, saved as a PNG at ``path``."""
    tri = _triangulation(layer)
    fig = Figure(figsize=(8, 6), dpi=dpi, layout="constrained")
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    finite = delta_mv[np.isfinite(delta_mv)]
    top = max(float(np.abs(finite).max()) if finite.size else 0.0, 1e-9)
    im = ax.tripcolor(tri, np.nan_to_num(delta_mv), shading="gouraud", cmap=DELTA_CMAP,
                      vmin=-top, vmax=top, rasterized=True)
    fig.colorbar(im, ax=ax, label="drop change, B − A (mV): red grew, blue shrank")
    ax.set_title(title)
    _decorate(ax, outline, result, [], layer.name)
    fig.savefig(path)
    return {"unit": "mV", "min": -top, "max": top}


def _rail_voltage_range(result: RailResult):
    """(lowest, highest) node voltage over every layer of the rail, or None."""
    finite = [lf.voltage[np.isfinite(lf.voltage)] for lf in result.layers]
    finite = [v for v in finite if v.size]
    if not finite:
        return None
    return float(min(v.min() for v in finite)), float(max(v.max() for v in finite))


def render_layer_map(result: RailResult, layer: LayerField, field: str,
                     outline: Sequence[np.ndarray], path, problems: Optional[List[Problem]] = None,
                     region: Optional[Sequence[float]] = None, dpi: int = 150) -> Dict[str, object]:
    """One colour map of ``field`` ('drop', 'voltage' or 'current') on ``layer``, saved as
    a PNG at ``path``. ``region`` = (x_min, y_min, x_max, y_max) in mm zooms the view; the
    colour scale stays the rail's (drop, voltage) or the layer's (current). Returns the
    scale: ``{"unit", "min", "max", "clipped"}``."""
    if field not in FIELDS:
        raise ValueError(f"field must be one of {', '.join(FIELDS)}, not {field!r}")
    problems = problems or []
    tri = _triangulation(layer)
    fig = Figure(figsize=(8, 6), dpi=dpi, layout="constrained")
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    vrange = _rail_voltage_range(result)
    clipped = False
    if field == "drop":
        # The drop as drop_unit names it (on a ground below 0 V, the depth below it).
        drop_max = max(float(result.shown_drop_at(vrange).max()), 0.0) * 1e3 if vrange else 0.0
        lo, hi, unit = 0.0, max(drop_max, 1e-9), "mV"
        drop_mv = np.nan_to_num(result.shown_drop_at(layer.voltage) * 1e3)
        im = ax.tripcolor(tri, drop_mv, shading="gouraud", cmap=DROP_CMAP, vmin=lo, vmax=hi,
                          rasterized=True)
        fig.colorbar(im, ax=ax, label=f"{result.drop_word()}, {result.drop_unit()}")
        title = "voltage drop"
    elif field == "voltage":
        lo, hi = vrange if vrange else (0.0, 1.0)
        hi = max(hi, lo + 1e-12)
        unit = "V"
        im = ax.tripcolor(tri, np.nan_to_num(layer.voltage, nan=lo), shading="gouraud",
                          cmap=DROP_CMAP, vmin=lo, vmax=hi, rasterized=True)
        fig.colorbar(im, ax=ax, label="voltage (V)")
        title = "voltage"
    else:
        j = layer.current_density
        top = float(np.percentile(j, J_CLIP_PERCENTILE)) if j.size else 1.0
        lo, hi, unit, clipped = 0.0, max(top, 1e-9), "A/mm²", True
        im = ax.tripcolor(tri, facecolors=j, cmap=J_CMAP, vmin=lo, vmax=hi, rasterized=True)
        fig.colorbar(im, ax=ax, label=f"current density (A/mm²), scale clipped at "
                                      f"{J_CLIP_PERCENTILE:g}th percentile", extend="max")
        title = "current density"
    ax.set_title(f"{result.net} — {layer.name}: {title}")
    _decorate(ax, outline, result, problems, layer.name)
    if region is not None:
        x0, y0, x1, y1 = (float(v) for v in region)
        ax.set_xlim(min(x0, x1), max(x0, x1))
        ax.set_ylim(max(y0, y1), min(y0, y1))   # KiCad: y down
    fig.savefig(path)
    return {"unit": unit, "min": float(lo), "max": float(hi), "clipped": clipped}


def save_layer_maps(result: RailResult, outline: Sequence[np.ndarray], out_dir,
                    problems: Optional[List[Problem]] = None, dpi: int = 150) -> List[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for layer in result.layers:
        stem = f"{safe_name(result.net)}_{safe_name(layer.name)}"
        for field, suffix in (("drop", "drop"), ("current", "current")):
            path = out_dir / f"{stem}_{suffix}.png"
            render_layer_map(result, layer, field, outline, path, problems, dpi=dpi)
            written.append(path)
    return written
