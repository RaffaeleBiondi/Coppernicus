"""Helpers on a solved layer's field: current vectors, arrows to draw, isolines, point lookup.

Pure NumPy/SciPy/matplotlib, no window and no engine, so the app, the maps and the service
layer can share them. Coordinates are millimetres in KiCad's frame (x right, **y down**);
a renderer that flips y for display must flip the vectors' y too.

Copper with no path to a source has NaN voltages. It is skipped everywhere: no vectors,
no arrows, no isolines.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

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


def drop_levels(max_drop_mv: float, count: int = 10) -> List[float]:
    """``count`` (at most 20) evenly spaced isoline levels strictly inside (0, max_drop_mv):
    the same levels on every layer, since the drop range is the rail's. Empty for a range
    that is zero or not finite."""
    count = max(0, min(int(count), 20))
    if not np.isfinite(max_drop_mv) or max_drop_mv <= 0 or count == 0:
        return []
    return [float(v) for v in np.linspace(0.0, max_drop_mv, count + 2)[1:-1]]


@dataclass
class Probe:
    """The field at a point of a layer's copper."""
    x_mm: float
    y_mm: float
    triangle: int
    voltage_v: float               # NaN on copper with no path to a source
    current_density_a_mm2: float


def probe(layer: LayerField, locator: "TriangleLocator", x: float, y: float) -> Optional[Probe]:
    """Voltage (interpolated linearly inside the triangle) and |J| at (x, y) mm, or ``None``
    off the layer's copper. A NaN voltage is returned as NaN, never raised."""
    k = locator.locate(x, y)
    if k < 0:
        return None
    w = locator.weights(k, x, y)
    nodes = layer.voltage[layer.tris[k]]
    v = float(w @ nodes) if np.isfinite(nodes).all() else float("nan")
    return Probe(float(x), float(y), k, v, float(layer.current_density[k]))


class TriangleLocator:
    """Which triangle contains a point. A KD-tree on centroids narrows the search to nearby
    triangles, then a barycentric test picks the one containing the point. Unlike
    matplotlib's trapezoid map it accepts meshes made of separate pieces with coincident
    points, as a layer of real copper is.

    Vectorised: the barycentric maps of all triangles are computed once, so
    :meth:`locate_many` places thousands of points (a cross-section cut) in one pass.
    Degenerate (zero-area) triangles never match.

    A point inside a long, thin triangle can be far from that triangle's centroid, farther
    than the centroids of many small triangles around it. Points the nearest ``candidates``
    centroids do not resolve get a wider pass (``WIDE`` centroids), then an exact one: the
    triangles whose bounding box holds the point, through a uniform grid of boxes built the
    first time it is needed."""

    WIDE = 256                 # candidates in the second pass
    GRID_FILL = 16             # triangles per grid cell, on average, in the exact pass
    BIG_SPAN = 64              # triangles whose box covers more cells are always tested

    def __init__(self, points: np.ndarray, tris: np.ndarray, candidates: int = 24):
        self.points = np.asarray(points, dtype=float)
        self.tris = np.asarray(tris, dtype=np.int64)
        self.k = min(candidates, len(self.tris))
        self._grid = None
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
        self._nearest_pass(pts, out, np.arange(len(pts)), self.k, tol)
        todo = np.flatnonzero(out < 0)
        if todo.size and len(self.tris) > self.k:            # long thin triangles: look wider
            self._nearest_pass(pts, out, todo, min(self.WIDE, len(self.tris)), tol)
            todo = np.flatnonzero(out < 0)
        if todo.size:                                        # exact: every box that holds it
            self._box_pass(pts, out, todo, tol)
        return out

    def _nearest_pass(self, pts, out, todo, k, tol):
        """Test the ``k`` triangles with the nearest centroids, nearest first."""
        _, idx = self.tree.query(pts[todo], k=k)
        idx = idx.reshape(len(todo), -1)
        rows = np.arange(len(todo))
        for col in range(idx.shape[1]):
            if not rows.size:
                break
            ks = idx[rows, col]
            valid = ks < len(self.tris)                      # cKDTree pads with n when k > n
            rows, ks = rows[valid], ks[valid]
            w = self._weights(ks, pts[todo[rows]])
            hit = self._ok[ks] & (w.min(axis=1) >= -tol)
            out[todo[rows[hit]]] = ks[hit]
            rows = rows[~hit]

    def _boxes(self):
        """A uniform grid over the mesh: per cell, the triangles whose bounding box overlaps
        it (CSR arrays); triangles covering many cells are kept apart and always tested."""
        if self._grid is None:
            p = self.points[self.tris]
            lo, hi = p.min(axis=1), p.max(axis=1)
            x0, y0 = lo.min(axis=0)
            x1, y1 = hi.max(axis=0)
            g = max(1, int(np.sqrt(len(self.tris) / self.GRID_FILL)))
            cw, ch = max((x1 - x0) / g, 1e-12), max((y1 - y0) / g, 1e-12)
            ix0 = np.clip(((lo[:, 0] - x0) / cw).astype(np.int64), 0, g - 1)
            ix1 = np.clip(((hi[:, 0] - x0) / cw).astype(np.int64), 0, g - 1)
            iy0 = np.clip(((lo[:, 1] - y0) / ch).astype(np.int64), 0, g - 1)
            iy1 = np.clip(((hi[:, 1] - y0) / ch).astype(np.int64), 0, g - 1)
            w, span = ix1 - ix0 + 1, (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
            usable = self._ok
            big = np.flatnonzero(usable & (span > self.BIG_SPAN))
            small = np.flatnonzero(usable & (span <= self.BIG_SPAN))
            n = span[small]
            rep = np.repeat(small, n)
            off = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
            cell = (iy0[rep] + off // w[rep]) * g + ix0[rep] + off % w[rep]
            order = np.argsort(cell, kind="stable")
            starts = np.searchsorted(cell[order], np.arange(g * g + 1))
            self._grid = (x0, y0, x1, y1, cw, ch, g, starts, rep[order], big, lo, hi)
        return self._grid

    def _box_pass(self, pts, out, todo, tol):
        x0, y0, x1, y1, cw, ch, g, starts, ids, big, lo, hi = self._boxes()
        eps = 1e-9 * max(x1 - x0, y1 - y0, 1.0)
        for i in todo:
            x, y = pts[i]
            if not (x0 - eps <= x <= x1 + eps and y0 - eps <= y <= y1 + eps):
                continue                                     # outside every triangle's box
            cx = min(max(int((x - x0) / cw), 0), g - 1)
            cy = min(max(int((y - y0) / ch), 0), g - 1)
            c = cy * g + cx
            cand = np.concatenate([ids[starts[c]:starts[c + 1]], big])
            inside = ((lo[cand, 0] - eps <= x) & (x <= hi[cand, 0] + eps)
                      & (lo[cand, 1] - eps <= y) & (y <= hi[cand, 1] + eps))
            cand = cand[inside]
            if not cand.size:
                continue
            w = self._weights(cand, np.broadcast_to(pts[i], (len(cand), 2)))
            hit = np.flatnonzero(w.min(axis=1) >= -tol)
            if hit.size:
                out[i] = cand[hit[0]]
        return out

    def nearest(self, x: float, y: float) -> Tuple[int, float]:
        """The triangle whose centroid is nearest (x, y), and that distance in mm; (-1, inf)
        for an empty mesh."""
        if self.tree is None:
            return -1, float("inf")
        dist, k = self.tree.query([x, y], k=1)
        return int(k), float(dist)
