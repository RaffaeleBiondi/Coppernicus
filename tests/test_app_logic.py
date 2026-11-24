"""App logic that needs no window: the triangle locator used by the double-click probe."""

import numpy as np

from coppernicus.app.board_view import copper_rings
from coppernicus.fields import TriangleLocator


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


def test_arrow_glyphs_point_along_the_vector_and_scale_with_sqrt_j():
    from coppernicus.app.board_view import arrow_glyphs
    pts = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
    vec = np.array([[4.0, 0.0], [0.0, 1.0], [0.0, 0.0]])      # KiCad frame: +y is down
    segs = arrow_glyphs(pts, vec, length_mm=2.0)
    assert segs.shape == (6, 2, 2)                             # 3 strokes per arrow, zero vector dropped
    shaft1, shaft2 = segs[0], segs[3]
    assert np.allclose(shaft1, [[-1.0, 0.0], [1.0, 0.0]])      # centred, full length, tip at +x
    assert np.allclose(shaft2, [[10.0, -0.5], [10.0, 0.5]])    # √(1/4) of the length, tip toward +y
    for head in segs[1:3]:                                     # head strokes start at the tip, go back
        assert np.allclose(head[0], [1.0, 0.0]) and head[1, 0] < 1.0
    assert arrow_glyphs(np.zeros((0, 2)), np.zeros((0, 2)), 1.0).shape == (0, 2, 2)
