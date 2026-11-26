"""The cross-section cut: copper width along a segment of one layer and the current through it.

The solver's elements are linear (P1) triangles, so the current density J is constant on
each triangle. :func:`coppernicus.fields.current_vectors` gives it as a vector (direction of
−∇V, magnitude the engine's |J|, which already holds ρ at the solve's temperature), and
:class:`coppernicus.fields.TriangleLocator` places the sample points; both are shared with
the app. No resistivity constant is repeated outside the engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from shapely.geometry import LineString, Point

from coppernicus.fields import TriangleLocator
from coppernicus.results import LayerField


@dataclass
class CutResult:
    length_mm: float
    normal: Tuple[float, float]
    intervals: List[Tuple[float, float]]       # along the line from p1, mm
    width_mm: float
    current_a: float
    mean_abs_j: float                          # A/mm², length-weighted along the copper
    max_abs_j: float
    samples: int
    samples_off_mesh: int
    samples_unconnected: int
    meshed_width_mm: float


def copper_intervals(copper, p1: Sequence[float], p2: Sequence[float]) -> List[Tuple[float, float]]:
    """Where the segment p1→p2 lies on ``copper`` (a shapely geometry): (start, end)
    distances from p1 in mm, sorted and merged."""
    line = LineString([tuple(p1), tuple(p2)])
    if copper is None or copper.is_empty:
        return []
    parts, stack = [], [copper.intersection(line)]
    while stack:
        g = stack.pop()
        if g.is_empty:
            continue
        if g.geom_type == "LineString":
            parts.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    spans = []
    for g in parts:
        coords = list(g.coords)
        s = sorted((line.project(Point(coords[0])), line.project(Point(coords[-1]))))
        if s[1] - s[0] > 1e-12:
            spans.append((float(s[0]), float(s[1])))
    spans.sort()
    merged: List[Tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def cut_current(layer: LayerField, locator: TriangleLocator, j_vec: np.ndarray, copper,
                thickness_mm: float, p1: Sequence[float], p2: Sequence[float],
                spacing_mm: Optional[float] = None) -> CutResult:
    """The current through the cross-section along p1→p2 on one layer: I = ∫ J·n t ds over
    the copper the segment crosses, with n the unit normal pointing to the right of p1→p2
    as the board is seen in KiCad (y down): n = (−t_y, t_x) for the unit direction t.
    ``j_vec`` is :func:`coppernicus.fields.current_vectors` of the layer."""
    p1, p2 = np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)
    length = float(np.hypot(*(p2 - p1)))
    if length <= 0:
        raise ValueError("the two points of a cut must differ")
    t = (p2 - p1) / length
    n = np.array([-t[1], t[0]])
    spans = copper_intervals(copper, p1, p2)
    width = float(sum(b - a for a, b in spans))
    if spacing_mm is None:
        spacing_mm = min(max(length / 4000.0, 0.002), 0.02)
    s_mid, ds = [], []
    for a, b in spans:
        k = max(int(math.ceil((b - a) / spacing_mm)), 8)
        h = (b - a) / k
        s_mid.append(a + h * (np.arange(k) + 0.5))
        ds.append(np.full(k, h))
    if not s_mid:
        return CutResult(length, (float(n[0]), float(n[1])), [], 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0)
    s_mid, ds = np.concatenate(s_mid), np.concatenate(ds)
    pts = p1[None, :] + s_mid[:, None] * t[None, :]
    tri = locator.locate_many(pts)
    on = tri >= 0
    live = on.copy()                         # on copper connected to a source (finite voltages)
    live[on] = np.isfinite(layer.voltage[layer.tris[tri[on]]]).all(axis=1)
    j = j_vec[tri[live]]
    jn = j @ n
    jabs = np.hypot(j[:, 0], j[:, 1])
    w = ds[live]
    live_width = float(w.sum())
    peak = jabs[layer.resolved()[tri[live]]]       # no degenerate triangle (edge < 1 nm) sets it
    return CutResult(
        length_mm=length, normal=(float(n[0]), float(n[1])), intervals=spans, width_mm=width,
        current_a=float((jn * w).sum() * thickness_mm),
        mean_abs_j=float((jabs * w).sum() / live_width) if live_width else 0.0,
        max_abs_j=float(peak.max()) if peak.size else 0.0, samples=int(len(pts)),
        samples_off_mesh=int((~on).sum()), samples_unconnected=int((on & ~live).sum()),
        meshed_width_mm=float(ds[on].sum()))
