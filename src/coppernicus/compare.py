"""Before/after on the copper: the difference between two solved layers, node by node.

Per-load and per-layer deltas come from the service layer
(:func:`coppernicus.service.session.compare_summaries`); this module only adds what needs
the meshes. Two solves of the same board with the same mesh settings share their mesh and
subtract exactly; otherwise one voltage is interpolated onto the other mesh, and callers
should say so. Comparing different boards (an edited board) is the caller's to refuse.
"""

from __future__ import annotations

import numpy as np

from coppernicus.fields import TriangleLocator
from coppernicus.results import LayerField


def same_mesh(a: LayerField, b: LayerField) -> bool:
    """True when both layers have the very same nodes and triangles (exact subtraction)."""
    return (a.points.shape == b.points.shape and a.tris.shape == b.tris.shape
            and np.array_equal(a.points, b.points) and np.array_equal(a.tris, b.tris))


def interpolate_voltage(b: LayerField, points: np.ndarray) -> np.ndarray:
    """``b``'s voltage at ``points`` (N, 2) mm, linear inside its triangles; NaN off ``b``'s
    copper or on copper with no path to a source."""
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    out = np.full(len(points), np.nan)
    if len(b.tris) == 0 or len(points) == 0:
        return out
    k = np.asarray(TriangleLocator(b.points, b.tris).locate_many(points))
    hit = np.flatnonzero(k >= 0)
    if hit.size == 0:
        return out
    tri = b.tris[k[hit]]
    p0, p1, p2 = b.points[tri[:, 0]], b.points[tri[:, 1]], b.points[tri[:, 2]]
    # Barycentric weights of q in (p0, p1, p2), from the edge vectors e1, e2 and d = q − p0.
    e1, e2, d = p1 - p0, p2 - p0, points[hit] - p0
    det = e1[:, 0] * e2[:, 1] - e2[:, 0] * e1[:, 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        w1 = (d[:, 0] * e2[:, 1] - e2[:, 0] * d[:, 1]) / det
        w2 = (e1[:, 0] * d[:, 1] - d[:, 0] * e1[:, 1]) / det
    v = b.voltage[tri]
    out[hit] = (1.0 - w1 - w2) * v[:, 0] + w1 * v[:, 1] + w2 * v[:, 2]
    return out


def difference_layer(a: LayerField, b: LayerField) -> LayerField:
    """A layer on ``a``'s mesh whose ``voltage`` is V_b − V_a per node (``b``'s voltage
    interpolated onto ``a``'s nodes when the meshes differ, see :func:`same_mesh`). NaN where
    either has no connected copper. Current density and power are zero: only the voltage
    is a difference."""
    vb = b.voltage if same_mesh(a, b) else interpolate_voltage(b, a.points)
    m = len(a.tris)
    return LayerField(a.layer_id, a.name, a.points, a.tris, vb - a.voltage,
                      np.zeros(m), np.zeros(m), a.tri_area)
