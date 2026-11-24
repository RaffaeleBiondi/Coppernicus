"""Point and line queries on a solved layer, with no GUI: which triangle holds a point, the
current-density vector per triangle, and the current through a cross-section cut.

The solver's elements are linear (P1) triangles, so the voltage gradient is constant on
each triangle and so is the current density J = −∇V/ρ. Its *magnitude* is taken from the
engine's own |J| per triangle (which already holds ρ at the solve's temperature); its
*direction* from −∇V computed here. No resistivity constant is repeated outside the engine.

:class:`TriangleLocator` is the logic of ``coppernicus.app.board_view.TriangleLocator``
(a non-GUI copy, vectorised), so the service does not import Qt or VTK.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point

from coppernicus.results import LayerField


class TriangleLocator:
    """Which triangle contains a point. A KD-tree on centroids narrows the search to nearby
    triangles, then a barycentric test picks the one containing the point. Unlike
    matplotlib's trapezoid map it accepts meshes made of separate pieces with coincident
    points, as a layer of real copper is."""

    def __init__(self, points: np.ndarray, tris: np.ndarray, candidates: int = 24):
        self.points = np.asarray(points, dtype=float)
        self.tris = np.asarray(tris, dtype=np.int64)
        self.k = min(candidates, len(self.tris))
        self.centroids = self.points[self.tris].mean(axis=1) if len(self.tris) else np.zeros((0, 2))
        self.tree = cKDTree(self.centroids) if len(self.tris) else None
        if len(self.tris):
            p = self.points[self.tris]                       # (M, 3, 2)
            # Barycentric map: lam = inv([[x0-x2, x1-x2], [y0-y2, y1-y2]]) @ (q - p2).
            a, b = p[:, 0, 0] - p[:, 2, 0], p[:, 1, 0] - p[:, 2, 0]
            c, d = p[:, 0, 1] - p[:, 2, 1], p[:, 1, 1] - p[:, 2, 1]
            det = a * d - b * c
            scale = np.maximum(np.abs(p.reshape(len(p), -1)).max(axis=1), 1.0) ** 2
            self._ok = np.abs(det) > 1e-14 * scale            # degenerate triangles never match
            with np.errstate(divide="ignore", invalid="ignore"):
                inv = 1.0 / np.where(self._ok, det, 1.0)
            self._inv = np.stack([d * inv, -b * inv, -c * inv, a * inv], axis=1)   # (M, 4)
            self._origin = p[:, 2, :].copy()                  # a copy: a view would pin all of p

    @property
    def nbytes(self) -> int:
        """Memory held by the locator (its arrays and KD-tree), roughly."""
        if self.tree is None:
            return 0
        tree = self.tree.data.nbytes + self.tree.indices.nbytes
        return int(self.centroids.nbytes + self._inv.nbytes + self._origin.nbytes
                   + self._ok.nbytes + 2 * tree)

    def weights(self, k: int, x: float, y: float) -> np.ndarray:
        """Barycentric weights of (x, y) in triangle ``k`` (they sum to 1)."""
        return self._weights(np.array([k]), np.array([[x, y]], dtype=float))[0]

    def _weights(self, ks: np.ndarray, pts: np.ndarray) -> np.ndarray:
        inv, o = self._inv[ks], self._origin[ks]
        dx, dy = pts[:, 0] - o[:, 0], pts[:, 1] - o[:, 1]
        l0 = inv[:, 0] * dx + inv[:, 1] * dy
        l1 = inv[:, 2] * dx + inv[:, 3] * dy
        return np.stack([l0, l1, 1.0 - l0 - l1], axis=1)

    def locate(self, x: float, y: float, tol: float = 1e-9) -> int:
        """Index of the triangle holding (x, y), or -1."""
        return int(self.locate_many(np.array([[x, y]], dtype=float), tol)[0])

    def locate_many(self, pts: np.ndarray, tol: float = 1e-9) -> np.ndarray:
        """Triangle index per point (-1 where no triangle holds it)."""
        pts = np.atleast_2d(np.asarray(pts, dtype=float))
        out = np.full(len(pts), -1, dtype=np.int64)
        if self.tree is None or not len(pts):
            return out
        _, idx = self.tree.query(pts, k=self.k)
        idx = idx.reshape(len(pts), -1)
        for col in range(idx.shape[1]):                      # nearest centroid first
            todo = np.flatnonzero(out < 0)
            if not todo.size:
                break
            ks = idx[todo, col]
            valid = ks < len(self.tris)                      # cKDTree pads with n when k > n
            todo, ks = todo[valid], ks[valid]
            w = self._weights(ks, pts[todo])
            hit = self._ok[ks] & (w.min(axis=1) >= -tol)
            out[todo[hit]] = ks[hit]
        return out

    def nearest(self, x: float, y: float) -> Tuple[int, float]:
        """The triangle whose centroid is nearest (x, y), and that distance in mm."""
        if self.tree is None:
            return -1, math.inf
        dist, k = self.tree.query([x, y], k=1)
        return int(k), float(dist)


def voltage_gradients(layer: LayerField) -> np.ndarray:
    """∇V per triangle, V/mm, shape (M, 2). NaN on copper not connected to a source."""
    p = layer.points[layer.tris]
    v = layer.voltage[layer.tris]
    d1, d2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    a, b = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
    det = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        gx = (a * d2[:, 1] - b * d1[:, 1]) / det
        gy = (b * d1[:, 0] - a * d2[:, 0]) / det
    return np.column_stack([gx, gy])


def current_vectors(layer: LayerField) -> np.ndarray:
    """J per triangle, A/mm², shape (M, 2): direction of −∇V, magnitude the engine's |J|."""
    g = voltage_gradients(layer)
    mag = np.hypot(g[:, 0], g[:, 1])
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where(mag > 0, layer.current_density / mag, 0.0)
    return -g * scale[:, None]


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
    inter = copper.intersection(line)
    parts = []
    stack = [inter]
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
    as the board is seen in KiCad (y down): n = (−t_y, t_x) for the unit direction t."""
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
    j = np.full((len(pts), 2), np.nan)
    j[on] = j_vec[tri[on]]
    live = on & np.isfinite(j).all(axis=1)
    jn = j[live] @ n
    jabs = np.hypot(j[live, 0], j[live, 1])
    w = ds[live]
    current = float((jn * w).sum() * thickness_mm)
    live_width = float(w.sum())
    return CutResult(
        length_mm=length, normal=(float(n[0]), float(n[1])), intervals=spans, width_mm=width,
        current_a=current, mean_abs_j=float((jabs * w).sum() / live_width) if live_width else 0.0,
        max_abs_j=float(jabs.max()) if jabs.size else 0.0, samples=int(len(pts)),
        samples_off_mesh=int((~on).sum()), samples_unconnected=int((on & ~live).sum()),
        meshed_width_mm=float(ds[on].sum()))
