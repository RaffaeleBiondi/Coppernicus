"""Helpers on a solved layer's field: current vectors, arrows to draw, isolines, point lookup.

Pure NumPy/SciPy/matplotlib, no window and no engine, so the app, the maps and the service
layer can share them. Coordinates are millimetres in KiCad's frame (x right, **y down**);
a renderer that flips y for display must flip the vectors' y too.

Copper with no path to a source has NaN voltages. It is skipped everywhere: no vectors,
no arrows, no isolines.
"""

from __future__ import annotations

import warnings
from typing import List, Sequence, Tuple

import numpy as np
from scipy.spatial import cKDTree

from coppernicus.results import LayerField


def current_vectors(layer: LayerField) -> np.ndarray:
    """Per triangle, the current density as a vector (M, 2) in A/mm²: the direction of
    −∇V (current flows downhill), scaled to ``layer.current_density``. Triangles touching a
    NaN voltage, degenerate triangles and triangles with no gradient give (0, 0)."""
    tris = layer.tris
    if len(tris) == 0:
        return np.zeros((0, 2))
    p = layer.points[tris]                                    # (M, 3, 2)
    v = layer.voltage[tris]                                   # (M, 3)
    e1, e2 = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
    dv1, dv2 = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
    det = e1[:, 0] * e2[:, 1] - e2[:, 0] * e1[:, 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        # Gradient of the linear interpolant: solve [e1; e2] · g = [dv1; dv2].
        gx = (dv1 * e2[:, 1] - dv2 * e1[:, 1]) / det
        gy = (dv2 * e1[:, 0] - dv1 * e2[:, 0]) / det
        norm = np.hypot(gx, gy)
        scale = layer.current_density / norm
        out = np.column_stack([-gx * scale, -gy * scale])
    good = np.isfinite(out).all(axis=1) & (norm > 0)
    out[~good] = 0.0
    return out


def sample_flow(layer: LayerField, spacing_mm: float,
                max_arrows: int = 2000) -> Tuple[np.ndarray, np.ndarray]:
    """Where to draw current arrows: at most one per square of ``spacing_mm`` that holds
    copper, at the centroid of the triangle nearest the square's centre, so every point is
    inside copper and tracks narrower than the spacing still get arrows. NaN copper is left
    out. The spacing grows until there are at most ``max_arrows``. Returns points (K, 2) mm
    and current-density vectors (K, 2) A/mm²."""
    vec = current_vectors(layer)
    ok = np.isfinite(layer.voltage[layer.tris]).all(axis=1) if len(layer.tris) else np.zeros(0, bool)
    cents, vec = layer.centroids()[ok] if ok.any() else np.zeros((0, 2)), vec[ok]
    if len(cents) == 0 or max_arrows <= 0:
        return np.zeros((0, 2)), np.zeros((0, 2))
    lo = cents.min(axis=0)
    spacing = max(float(spacing_mm), 1e-9)
    while True:
        cell = np.floor((cents - lo) / spacing).astype(np.int64)
        ncol = int(cell[:, 1].max()) + 1
        key = cell[:, 0] * ncol + cell[:, 1]
        off = cents - (lo + (cell + 0.5) * spacing)
        order = np.lexsort(((off ** 2).sum(axis=1), key))      # by cell, then nearest the centre
        _, first = np.unique(key[order], return_index=True)
        if len(first) <= max_arrows:
            pick = order[first]
            return cents[pick], vec[pick]
        spacing *= 1.05 * np.sqrt(len(first) / max_arrows)


def isolines(layer: LayerField, values: np.ndarray, levels: Sequence[float]) -> List[np.ndarray]:
    """Lines where the per-node ``values`` (e.g. the drop in mV) equal each of ``levels``, as
    polylines (N_i, 2) in mm. Triangles touching a NaN value are left out. Computed with
    matplotlib's ``tricontour`` on a figure that is never shown."""
    from matplotlib.figure import Figure
    from matplotlib.tri import Triangulation

    values = np.asarray(values, dtype=float)
    levels = sorted({float(lv) for lv in levels if np.isfinite(lv)})
    if not levels or len(layer.tris) == 0:
        return []
    bad = ~np.isfinite(values)
    mask = bad[layer.tris].any(axis=1)
    if mask.all():
        return []
    z = np.where(bad, 0.0, values)                  # masked triangles never read these
    tri = Triangulation(layer.points[:, 0], layer.points[:, 1], layer.tris, mask=mask)
    ax = Figure().add_subplot()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")             # "no contour levels within the data range"
        cs = ax.tricontour(tri, z, levels=levels)
    return [np.asarray(seg, dtype=float) for segs in cs.allsegs for seg in segs if len(seg) >= 2]


class TriangleLocator:
    """Which triangle contains a point. A KD-tree on centroids narrows the search to nearby
    triangles, then a barycentric test picks the one containing the point. Unlike
    matplotlib's trapezoid map it accepts meshes made of separate pieces with coincident
    points, as a layer of real copper is.

    Vectorised: the barycentric maps of all triangles are computed once, so
    :meth:`locate_many` places thousands of points (a cross-section cut) in one pass.
    Degenerate (zero-area) triangles never match."""

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
        """Barycentric weights of (x, y) in triangle ``k`` (they sum to 1). Raises
        ``numpy.linalg.LinAlgError`` for a degenerate triangle."""
        if not self._ok[k]:
            raise np.linalg.LinAlgError(f"triangle {k} is degenerate")
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
        """Triangle index per point, shape (N,); -1 where no triangle holds it."""
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
        """The triangle whose centroid is nearest (x, y), and that distance in mm; (-1, inf)
        for an empty mesh."""
        if self.tree is None:
            return -1, float("inf")
        dist, k = self.tree.query([x, y], k=1)
        return int(k), float(dist)
