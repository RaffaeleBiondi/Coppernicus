"""Per-layer colour maps of a solved rail, written as PNG files (headless, no Qt).

Two maps per copper layer: the voltage *drop* below the source (mV, per node, smooth) and
the current density (A/mm², per triangle). KiCad's y axis points down, so the plots invert
y to look like the board in KiCad.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

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
    text = text.replace("+", "P").replace("-", "N")
    return re.sub(r"[^A-Za-z0-9_.]+", "_", text).strip("_") or "net"


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


def save_layer_maps(result: RailResult, outline: Sequence[np.ndarray], out_dir,
                    problems: Optional[List[Problem]] = None, dpi: int = 150) -> List[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    problems = problems or []
    written = []
    drop_max = max((float(np.nanmax(result.v_ref - lf.voltage)) for lf in result.layers
                    if np.isfinite(lf.voltage).any()), default=0.0) * 1e3
    for layer in result.layers:
        tri = _triangulation(layer)
        stem = f"{safe_name(result.net)}_{safe_name(layer.name)}"

        fig = Figure(figsize=(8, 6), dpi=dpi, layout="constrained")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        drop_mv = np.nan_to_num((result.v_ref - layer.voltage) * 1e3)
        im = ax.tripcolor(tri, drop_mv, shading="gouraud", cmap=DROP_CMAP, vmin=0.0,
                          vmax=max(drop_max, 1e-9), rasterized=True)
        fig.colorbar(im, ax=ax, label=f"drop below {result.v_ref:g} V (mV)")
        ax.set_title(f"{result.net} — {layer.name}: voltage drop")
        _decorate(ax, outline, result, problems, layer.name)
        path = out_dir / f"{stem}_drop.png"
        fig.savefig(path)
        written.append(path)

        fig = Figure(figsize=(8, 6), dpi=dpi, layout="constrained")
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        j = layer.current_density
        top = float(np.percentile(j, J_CLIP_PERCENTILE)) if j.size else 1.0
        im = ax.tripcolor(tri, facecolors=j, cmap=J_CMAP, vmin=0.0, vmax=max(top, 1e-9),
                          rasterized=True)
        fig.colorbar(im, ax=ax, label=f"current density (A/mm²), scale clipped at "
                                      f"{J_CLIP_PERCENTILE:g}th percentile", extend="max")
        ax.set_title(f"{result.net} — {layer.name}: current density")
        _decorate(ax, outline, result, problems, layer.name)
        path = out_dir / f"{stem}_current.png"
        fig.savefig(path)
        written.append(path)
    return written
