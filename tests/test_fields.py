"""Flow-field helpers on a solved strip: current vectors, arrow sampling, isolines, NaN copper."""

import dataclasses

import numpy as np
import pytest
from shapely.geometry import Point

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import OZ1, F, Strip, strip_board
from coppernicus.fields import TriangleLocator, current_vectors, isolines, sample_flow
from coppernicus.project import Load, Rail, Source


@pytest.fixture(scope="module")
def strip():
    board = Board(strip_board())
    res = solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)]))
    return board, res, res.layers[0]


def _away_from_pads(layer):
    x = layer.centroids()[:, 0]
    return (x > Strip.X0 + 5) & (x < Strip.X1 - 5)


def test_current_vectors_point_from_source_to_load(strip):
    _, _, layer = strip
    vec = current_vectors(layer)
    assert vec.shape == (len(layer.tris), 2)
    mid = _away_from_pads(layer)
    assert vec[mid, 0].mean() > 0                       # J1 at x=0 → U1 at x=40
    mag = np.hypot(vec[mid, 0], vec[mid, 1])
    assert mag == pytest.approx(np.full(mid.sum(), 1.0 / (Strip.W * OZ1)), rel=0.05)
    assert np.abs(vec[mid, 1]).max() < 0.05 * mag.max()  # along the strip


def test_sample_flow_bounded_and_inside(strip):
    board, _, layer = strip
    copper = board.copper(Strip.NET, F)
    for spacing in (0.01, 1.0, 5.0):
        pts, vec = sample_flow(layer, spacing)
        assert 0 < len(pts) <= 2000 and pts.shape == vec.shape == (len(pts), 2)
        assert all(copper.buffer(1e-6).contains(Point(p)) for p in pts)
    pts, _ = sample_flow(layer, 1.0)
    # The 0.5 mm strip is narrower than the spacing and still gets arrows all along it.
    assert pts[:, 0].min() < Strip.X0 + 2 and pts[:, 0].max() > Strip.X1 - 2
    assert len(sample_flow(layer, 0.01, max_arrows=50)[0]) <= 50


def test_isolines_of_drop_are_vertical_on_a_strip(strip):
    _, res, layer = strip
    drop_mv = (res.v_ref - layer.voltage) * 1e3
    lines = isolines(layer, drop_mv, [5.0])
    assert lines
    for line in lines:
        assert line.shape[1] == 2 and len(line) >= 2
        assert np.ptp(line[:, 0]) < 0.6
    # ρ·x/(w·t) of track past the source pad's edge gives 5 mV.
    x5 = Strip.X0 + Strip.PAD / 2 + 5e-3 * Strip.W * OZ1 / 1.724e-5
    assert np.mean([line[:, 0].mean() for line in lines]) == pytest.approx(x5, abs=0.3)
    assert isolines(layer, drop_mv, []) == []
    assert isolines(layer, drop_mv, [1e6]) == []         # a level outside the range


def test_nan_voltages_are_skipped(strip):
    _, res, layer = strip
    v = layer.voltage.copy()
    v[: len(v) // 2] = np.nan
    half = dataclasses.replace(layer, voltage=v)
    nan_tris = np.isnan(v)[half.tris].any(axis=1)
    assert nan_tris.any() and not nan_tris.all()
    vec = current_vectors(half)
    assert np.all(vec[nan_tris] == 0) and np.isfinite(vec).all()
    pts, _ = sample_flow(half, 0.2)
    cents = half.centroids()
    nan_cents = {tuple(c) for c in cents[nan_tris]}
    assert pts.size and not any(tuple(p) in nan_cents for p in pts)
    lines = isolines(half, (res.v_ref - v) * 1e3, [5.0, 20.0])
    assert all(np.isfinite(line).all() for line in lines)


def test_locator_many_points_degenerate_triangles_and_nearest():
    """The vectorised locator: one point or many, separate pieces with coincident points,
    a zero-area triangle that never matches, and the nearest triangle off the mesh."""
    pts = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [1, 0], [2, 0], [3, 0], [4, 0], [5, 0]], float)
    tris = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 3], [6, 7, 8]])        # the last is a line
    loc = TriangleLocator(pts, tris)
    queries = np.array([[0.2, 0.2], [0.8, 0.8], [1.2, 0.1], [4.0, 0.0], [5, 5]])
    found = [loc.locate(x, y) for x, y in queries]
    assert found == [0, 1, 2, -1, -1]
    assert list(loc.locate_many(queries)) == found
    w = loc.weights(0, 0.25, 0.25)
    assert np.allclose(w.sum(), 1.0) and np.allclose(w @ pts[tris[0]], [0.25, 0.25])
    with pytest.raises(np.linalg.LinAlgError):
        loc.weights(3, 4.0, 0.0)
    k, dist = loc.nearest(0.3, -1.0)
    assert k == 0 and dist == pytest.approx(np.hypot(0.3 - 1 / 3, -1.0 - 1 / 3))
    assert loc.nbytes > 0
    empty = TriangleLocator(np.zeros((0, 2)), np.zeros((0, 3), dtype=int))
    assert empty.locate(0, 0) == -1 and empty.nearest(0, 0)[0] == -1


def test_probe_interpolates_and_skips_nan_and_outside(strip):
    from coppernicus.fields import TriangleLocator, probe
    _, res, layer = strip
    loc = TriangleLocator(layer.points, layer.tris)
    a, b = probe(layer, loc, 10.0, 0.0), probe(layer, loc, 30.0, 0.0)
    assert res.v_ref > a.voltage_v > b.voltage_v > res.loads[0].v_min
    # Linear along the track: the midpoint reads the mean of its neighbours.
    mid = probe(layer, loc, 20.0, 0.0)
    assert mid.voltage_v == pytest.approx((a.voltage_v + b.voltage_v) / 2, rel=1e-4)
    assert mid.current_density_a_mm2 == pytest.approx(1.0 / (Strip.W * OZ1), rel=0.05)
    assert probe(layer, loc, 20.0, 5.0) is None                  # off the copper
    v = layer.voltage.copy()
    v[layer.points[:, 0] > 20.0] = np.nan
    half = dataclasses.replace(layer, voltage=v)
    p = probe(half, TriangleLocator(half.points, half.tris), 30.0, 0.0)
    assert p is not None and np.isnan(p.voltage_v)               # copper, but no path to a source


def test_drop_levels_are_evenly_spaced_and_bounded():
    from coppernicus.fields import drop_levels
    levels = drop_levels(9.6)
    assert len(levels) == 10 and 0 < levels[0] < levels[-1] < 9.6
    assert np.allclose(np.diff(levels), levels[0])
    assert len(drop_levels(9.6, 50)) == 20
    assert drop_levels(0.0) == [] and drop_levels(float("nan")) == []


def test_locator_finds_points_held_only_by_long_thin_triangles():
    """A node held only by 500 slivers (aspect ~570, centroids ~3.3 mm away), next to a
    patch of 1800 small triangles whose centroids are all nearer: neither the first nor
    the widened nearest-centroid pass reaches a sliver, the exact box pass does."""
    patch = np.stack(np.meshgrid(np.linspace(0.05, 0.35, 31), np.linspace(0.05, 0.35, 31)),
                     -1).reshape(-1, 2)
    tris = []
    for j in range(30):
        for i in range(30):
            a = j * 31 + i
            tris += [[a, a + 1, a + 31], [a + 1, a + 32, a + 31]]
    angles = np.radians(np.arange(200.0, 250.05, 0.1))
    fan = np.column_stack([10 * np.cos(angles), 10 * np.sin(angles)])
    origin = len(patch) + len(fan)
    pts = np.vstack([patch, fan, [[0.0, 0.0]]])
    first_fan = len(patch)
    tris += [[origin, first_fan + k, first_fan + k + 1] for k in range(len(fan) - 1)]
    tris = np.array(tris)
    slivers = np.arange(1800, len(tris))
    loc = TriangleLocator(pts, tris)
    inside_sliver = 0.01 * np.array([np.cos(np.radians(225.05)), np.sin(np.radians(225.05))])
    found = loc.locate_many(np.array([[0.0, 0.0], inside_sliver, [0.2, 0.2], [5.0, 5.0]]))
    assert found[0] in slivers and found[1] in slivers
    assert 0 <= found[2] < 1800 and found[3] == -1          # the patch, and nothing at all
    w = loc.weights(int(found[1]), *inside_sliver)
    assert w.min() >= -1e-9 and np.allclose(w @ pts[tris[found[1]]], inside_sliver)
    assert loc.locate(0.0, 0.0) in slivers                   # the single-point API too


def test_locator_points_off_the_mesh_skip_the_exact_pass():
    """Far from every triangle (beyond the largest centroid-to-vertex distance) or outside
    the mesh's bounding box, a point is answered -1 without building the box grid."""
    pts = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], float)
    loc = TriangleLocator(pts, np.array([[0, 1, 2], [1, 3, 2]]))
    assert loc.locate(50.0, 50.0) == -1 and loc.locate(-0.2, 0.5) == -1
    assert loc._grid is None
    before = loc.nbytes
    loc._boxes()
    assert loc.nbytes > before                               # the grid, once built, is counted


def test_locator_finds_every_node_of_a_random_mesh_of_thin_triangles():
    """Delaunay of random points on 50 horizontal lines: ~400k triangles, median aspect
    ratio ~120 and a long tail of slivers (the case the GUI review found misses in). Every
    node must be found, in a triangle it is a vertex of."""
    from scipy.spatial import Delaunay
    rng = np.random.default_rng(0)
    n = 200_000
    pts = np.column_stack([rng.random(n), rng.integers(0, 50, n) / 50 + 1e-7 * rng.random(n)])
    tris = Delaunay(pts).simplices
    found = TriangleLocator(pts, tris).locate_many(pts)
    assert (found >= 0).all()
    assert (tris[found] == np.arange(n)[:, None]).any(axis=1).all()
