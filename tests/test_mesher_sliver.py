"""A copper sliver one KiPI grid step (1 nm) wide: every mesher through the seam must mesh it (synthetic, from
parameters only). Its input angle is a few thousandths of a degree; Delaunay refinement cannot make good
triangles at its apex and must not chase it below the 1 nm resolution KiPI snaps the outline to.

The rust backend accepts such a near-zero-angle wedge as it is (tools/mesher_rs/DESIGN.md, "Near-zero input
angles"): no edge under 1 nm is added, and every triangle outside the wedge keeps the 28 degree bound."""

import math

import numpy as np
import pytest
import shapely
from kipi.core.mesh import mesher as M
from shapely import affinity
from shapely.geometry import Polygon

from coppernicus.engine import meshing

X0, Y0 = 120.0, 80.0                     # board-like coordinates
MAX_AREA = math.sqrt(3) / 4 * 0.5 ** 2


def sliver(width_mm: float, length_mm: float, run_mm: float, rot_deg: float) -> Polygon:
    """A 2 x 3 mm copper rectangle whose bottom edge runs on for ``run_mm`` to an apex, from which the outline
    comes back ``length_mm`` at ``width_mm`` above that edge: a sliver ``length_mm`` long, ``width_mm`` wide
    at its base and 0 at its apex."""
    apex = (2.0 + run_mm, 0.0)
    back = (apex[0] - length_mm, width_mm)
    pts = [(0, 0), (2.0, 0), apex, back, (back[0], 3.0), (0, 3.0)]
    return affinity.rotate(Polygon([(X0 + x, Y0 + y) for x, y in pts]), rot_deg, origin=(X0, Y0))


def without_sliver(poly: Polygon) -> Polygon:
    """The same rectangle cut where the sliver starts: its bottom edge ends at the foot of the sliver's
    base, below the vertical edge."""
    ring = np.asarray(poly.exterior.coords)
    o, apex, back = ring[0], ring[2], ring[3]
    d = (apex - o) / np.hypot(*(apex - o))
    foot = o + d * np.dot(back - o, d)
    return Polygon([tuple(o), tuple(foot), tuple(ring[4]), tuple(ring[5])])


@pytest.fixture(autouse=True)
def _restore():
    yield
    meshing.uninstall()


def _two_a(p):
    return ((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
            - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))


def _angles(pts, tris):
    p = pts[tris]
    e = np.stack([np.linalg.norm(p[:, 1] - p[:, 2], axis=1), np.linalg.norm(p[:, 2] - p[:, 0], axis=1),
                  np.linalg.norm(p[:, 0] - p[:, 1], axis=1)], axis=1)
    a, b, c = e.T
    A = np.degrees(np.arccos(np.clip((b * b + c * c - a * a) / (2 * b * c), -1, 1)))
    B = np.degrees(np.arccos(np.clip((a * a + c * c - b * b) / (2 * a * c), -1, 1)))
    return np.minimum(np.minimum(A, B), 180 - A - B)


def _check_rust_quality(poly, pts, tris, base=None, cut=None, size_fn=None):
    """No edge under 1 nm added by refinement; 28 degrees except in the wedge (every vertex on its two sides,
    within the 1 nm snap); the triangle count a small multiple of the polygon's without the sliver. ``base``
    is the sliver polygon the wedge's sides are read from (``poly`` itself by default), ``cut`` what was cut
    out of it (taken out of the polygon without the sliver too)."""
    base = poly if base is None else base
    ring = list(base.exterior.coords)
    verts, segs = M._linework([poly.exterior])
    given = {tuple(sorted((tuple(verts[a]), tuple(verts[b])))) for a, b in segs}
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    e = np.unique(e, axis=0)
    short = np.hypot(*(pts[e[:, 0]] - pts[e[:, 1]]).T) < M.NODING_GRID_MM
    made = [k for k in np.flatnonzero(short)
            if tuple(sorted((tuple(pts[e[k, 0]]), tuple(pts[e[k, 1]])))) not in given]
    assert not made, f"{len(made)} edge(s) under 1 nm added by refinement"
    sides = shapely.MultiLineString([[ring[1], ring[2]], [ring[2], ring[3]]])
    near = shapely.distance(sides, shapely.points(pts)) <= M.NODING_GRID_MM
    in_wedge = near[tris].all(axis=1)
    assert _angles(pts, tris)[~in_wedge].min() >= 28 - 1e-6
    assert in_wedge.sum() <= 2
    plain_poly = without_sliver(base) if cut is None else without_sliver(base).difference(cut)
    _, plain = M.mesh_polygon(plain_poly, [], MAX_AREA, 28.0, size_fn)
    assert len(tris) <= 4 * len(plain)


@pytest.mark.parametrize("name", ["triangle", "rust"])
@pytest.mark.parametrize("rot", [0.0, 30.0, 60.0])
def test_a_one_grid_step_sliver_meshes(name, rot):
    if name not in meshing.available():
        pytest.skip(f"{name} not available here")
    poly = sliver(width_mm=1e-6, length_mm=0.05, run_mm=0.1, rot_deg=rot)
    meshing.install(name)
    pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0)
    two_a = _two_a(pts[tris])
    assert len(tris) and np.all(two_a != 0)
    assert abs(np.abs(two_a).sum() / 2 - poly.area) <= poly.boundary.length * M.NODING_GRID_MM
    if name == "rust":
        _check_rust_quality(poly, pts, tris)


@pytest.mark.parametrize("width_mm, length_mm, run_mm", [
    (1e-6, 0.05, 0.1),        # one grid step, every rotation
    (1e-5, 0.05, 0.17),       # ten grid steps
    (1.7e-6, 0.2, 0.68),      # a 0.2 mm sliver on a side longer than the largest edge
])
def test_rust_meshes_a_near_zero_sliver_at_every_rotation(width_mm, length_mm, run_mm):
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    meshing.install("rust")
    for rot in range(0, 91, 1 if width_mm == 1e-6 else 15):
        poly = sliver(width_mm, length_mm, run_mm, float(rot))
        pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0)
        assert abs(np.abs(_two_a(pts[tris])).sum() / 2 - poly.area) <= poly.boundary.length * M.NODING_GRID_MM
        _check_rust_quality(poly, pts, tris)


def test_rust_sliver_mesh_is_deterministic():
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    meshing.install("rust")
    poly = sliver(1e-6, 0.05, 0.1, 31.0)
    a, b = M.mesh_polygon(poly, [], MAX_AREA, 28.0), M.mesh_polygon(poly, [], MAX_AREA, 28.0)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def _wedge(apex_deg, short_mm, long_mm, rot):
    """The rectangle with a wedge of ``apex_deg`` on its bottom edge: the edge runs ``long_mm`` past x = 2
    to the apex and the outline returns ``short_mm`` from it."""
    a = math.radians(apex_deg)
    A = (2.0 + long_mm, 0.0)
    P1 = (A[0] - short_mm * math.cos(a), short_mm * math.sin(a))
    pts = [(0, 0), (2.0, 0), A, P1, (P1[0], 3.0), (0, 3.0)]
    return affinity.rotate(Polygon([(X0 + x, Y0 + y) for x, y in pts]), rot, origin=(X0, Y0))


@pytest.mark.parametrize("rule", ["area", "edge"])
def test_rust_still_meets_the_size_bound_inside_a_near_zero_wedge(rule):
    """A 0.5 degree wedge 6 mm long is over the size bound: its triangles are refined to it though they
    stay skinny (only skinny triangles within the size bound are accepted)."""
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    from coppernicus.engine.meshing.rust_backend import RustBackend
    meshing.register("rust_" + rule, lambda: RustBackend(rule))
    meshing.install("rust_" + rule)
    for rot in (0.0, 30.0, 60.0):
        poly = _wedge(0.5, 6.0, 6.5, rot)
        pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0)
        assert np.all(np.abs(_two_a(pts[tris])) / 2 <= MAX_AREA * (1 + 1e-12))
        if rule == "edge":
            e = np.hypot(*(pts[tris] - pts[np.roll(tris, 1, axis=1)]).transpose(2, 0, 1))
            assert e.max() <= 0.5 * (1 + 1e-12)
        ring = list(poly.exterior.coords)
        sides = shapely.MultiLineString([[ring[1], ring[2]], [ring[2], ring[3]]])
        in_wedge = (shapely.distance(sides, shapely.points(pts)) <= M.NODING_GRID_MM)[tris].all(axis=1)
        assert _angles(pts, tris)[~in_wedge].min() >= 28 - 1e-6


def test_rust_grades_toward_a_via_beside_a_near_zero_sliver():
    """KiPI's graded field (a via 0.3 mm from the sliver's base) is met at every centroid, sliver included."""
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    meshing.install("rust")
    for rot in (0.0, 31.0, 60.0):
        via = affinity.rotate(shapely.Point(X0 + 1.75, Y0 + 0.6), rot, origin=(X0, Y0))
        hole = via.buffer(0.15, quad_segs=2)
        base = sliver(1e-6, 0.05, 0.1, rot)
        poly = base.difference(hole)
        size_fn = M.graded_size(np.array([[via.x, via.y, 0.15]]), 0.02, 0.5, 0.2)
        pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28.0, size_fn)
        h = size_fn(pts[tris].mean(axis=1))
        limit = np.minimum(math.sqrt(3) / 4 * h * h, MAX_AREA)
        assert np.all(np.abs(_two_a(pts[tris])) / 2 <= limit * (1 + 1e-12))
        _check_rust_quality(poly, pts, tris, base=base, cut=hole, size_fn=size_fn)
