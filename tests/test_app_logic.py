"""App logic that needs no window: the triangle locator used by the double-click probe."""

import numpy as np

from coppernicus.app.board_view import TriangleLocator, copper_rings


def test_locator_handles_separate_pieces_with_coincident_points():
    pts = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [1, 0], [2, 0]], float)   # (1, 0) twice
    tris = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 3]])
    loc = TriangleLocator(pts, tris)
    found = [loc.locate(0.2, 0.2), loc.locate(0.8, 0.8), loc.locate(1.2, 0.1), loc.locate(5, 5)]
    assert found == [0, 1, 2, -1]
    w = loc.weights(0, 0.25, 0.25)
    assert np.allclose(w.sum(), 1.0) and np.allclose(w @ pts[tris[0]], [0.25, 0.25])


def test_copper_rings_of_a_polygon_with_a_hole():
    from shapely.geometry import box
    rings = copper_rings(box(0, 0, 10, 10).difference(box(4, 4, 6, 6)))
    assert len(rings) == 2 and all(r.shape[1] == 2 for r in rings)
    assert copper_rings(None) == []
