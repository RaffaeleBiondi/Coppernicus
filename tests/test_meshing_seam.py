"""The mesher seam (coppernicus.engine.meshing): every available backend keeps KiPI's mesh contract
through it; the Triangle backend through it is bit-identical to KiPI's own path; install and
uninstall are idempotent and restore KiPI exactly."""

import math
import os
import sys
from pathlib import Path

import numpy as np
import pytest
import shapely
from kipi.core.mesh import mesher as M
from shapely.geometry import Point, Polygon, box

from coppernicus.engine import meshing
from coppernicus.engine.meshing import pipeline

X0, Y0 = 120.0, 80.0                     # board-like coordinates: an ulp is ~1e-14 mm here
MAX_AREA = math.sqrt(3) / 4 * 0.5 ** 2
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore():
    yield
    meshing.uninstall()


@pytest.fixture(params=["triangle", "rust"])
def name(request):
    if request.param not in meshing.available():
        pytest.skip(f"mesher {request.param!r} not available here")
    return request.param


def _mesh(name, *args, **kw):
    """One polygon through the seam, as KiPI calls it once ``name`` is installed."""
    meshing.install(name)
    return M.mesh_polygon(*args, **kw)


def _hole(cx, cy, r):
    return Point(cx, cy).buffer(r, quad_segs=2)


def _two_a(pts, tris):
    p = pts[tris]
    return ((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
            - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))


def _check(pts, tris, poly, rings=(), pads=()):
    assert len(tris) > 0
    two_a = _two_a(pts, tris)
    assert np.all(two_a != 0)                                    # no zero-area triangle
    assert len(np.unique(tris)) == len(pts)                      # no orphan node
    # All the copper, to within what KiPI's 1 nm snap can move the outline.
    assert abs(np.abs(two_a).sum() / 2 - poly.area) <= poly.boundary.length * M.NODING_GRID_MM
    verts, segs = M._linework([poly.exterior, *poly.interiors])
    have = set(map(tuple, pts))
    assert all(tuple(v) in have for v in verts)                  # every input vertex, bit-exact
    for ring in rings:                                           # drill ring vertices are nodes
        for xy in np.asarray(ring.exterior.coords)[:-1]:
            assert np.min(np.hypot(*(pts - xy).T)) <= M.NODING_GRID_MM
    for pad in pads:                                             # nodes inside every pad
        assert shapely.contains_xy(pad.buffer(M.RING_EPS_MM), pts[:, 0], pts[:, 1]).any()
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    e, n = np.unique(e, axis=0, return_counts=True)              # conforming: the border is the outline
    border = e[n == 1]
    length = np.hypot(*(pts[border[:, 0]] - pts[border[:, 1]]).T).sum()
    snapped = np.hypot(*(verts[segs[:, 0]] - verts[segs[:, 1]]).T).sum()
    assert abs(length - snapped) <= 1e-12 * snapped


def _angles(pts, tris):
    p = pts[tris]
    e = np.stack([np.linalg.norm(p[:, 1] - p[:, 2], axis=1), np.linalg.norm(p[:, 2] - p[:, 0], axis=1),
                  np.linalg.norm(p[:, 0] - p[:, 1], axis=1)], axis=1)
    a, b, c = e.T
    ang = np.degrees(np.arccos(np.clip([(b * b + c * c - a * a) / (2 * b * c),
                                         (a * a + c * c - b * b) / (2 * a * c)], -1, 1)))
    return e, np.vstack([ang, 180 - ang.sum(axis=0)]).min(axis=0)


def test_holes_pads_and_grading_keep_the_contract(name):
    vias = [_hole(X0 + 3 + i, Y0 + 2.5, 0.15) for i in range(3)]
    poly = box(X0, Y0, X0 + 8, Y0 + 5)
    for v in vias:
        poly = poly.difference(v)
    pads = [box(X0 + 6, Y0 + 1, X0 + 7, Y0 + 2), box(X0 - 0.5, Y0 + 2, X0 + 0.7, Y0 + 3),
            box(X0 + 1.5, Y0 + 4, X0 + 1.58, Y0 + 4.08)]          # one far smaller than the mesh
    feats = np.array([(X0 + 3 + i, Y0 + 2.5, 0.15) for i in range(3)])
    size_fn = M.graded_size(feats, 0.02, 0.5, 0.2)
    uni_pts, uni_tris = _mesh(name, poly, [p.boundary for p in pads], MAX_AREA, 28, None)
    pts, tris = _mesh(name, poly, [p.boundary for p in pads], MAX_AREA, 28, size_fn)
    for p, t in ((uni_pts, uni_tris), (pts, tris)):
        _check(p, t, poly, vias, [q.intersection(poly) for q in pads])
        e, ang = _angles(p, t)
        assert ang.min() >= 28 - 1e-6
        assert np.all(np.abs(_two_a(p, t)) / 2 <= MAX_AREA * (1 + 1e-12))
    assert len(tris) > len(uni_tris) and e.max(axis=1).min() < 0.05      # refined at the holes
    if meshing.get(name).strict_size:        # the size field met at every triangle's own centroid
        h = size_fn(pts[tris].mean(axis=1))
        limit = np.minimum(math.sqrt(3) / 4 * h * h, MAX_AREA)
        assert np.all(np.abs(_two_a(pts, tris)) / 2 <= limit * (1 + 1e-12))


def test_neck_acute_angle_and_interior_points(name):
    neck = box(X0, Y0, X0 + 4, Y0 + 4).union(box(X0 + 6, Y0, X0 + 10, Y0 + 4)).union(
        box(X0 + 3.9, Y0 + 1.925, X0 + 6.1, Y0 + 2.075))
    tip = Y0 + 4 + 0.05 / math.tan(math.radians(2))             # a 4 degree spike
    spike = Polygon([(X0 + 2, Y0 + 4), (X0 + 2.1, Y0 + 4), (X0 + 2.05, tip)])
    hole = _hole(X0 + 8, Y0 + 1, 0.3)
    poly = neck.union(spike).difference(hole)
    inside = np.array([[X0 + 1.2345678901, Y0 + 1.1111111], [X0 + 8.7654321, Y0 + 3.0101010101]])
    in_hole = np.array([[X0 + 8.01, Y0 + 1.02]])                # not copper: must not stay a node
    pts, tris = _mesh(name, poly, [], MAX_AREA, 28, None, points=np.vstack([inside, in_hole]))
    _check(pts, tris, poly, [hole])
    assert np.all(np.abs(_two_a(pts, tris)) / 2 <= MAX_AREA * (1 + 1e-12))
    for xy in inside:                                            # interior points are nodes
        assert np.min(np.hypot(*(pts - xy).T)) == 0.0
    assert np.min(np.hypot(*(pts - in_hole[0]).T)) > 0.1


def test_no_copper_lost_silently_and_kipis_fallbacks(name, monkeypatch):
    poly = box(X0, Y0, X0 + 1, Y0 + 1)
    real_linework = M._linework
    monkeypatch.setattr(M, "_linework", lambda lines: (lambda v, s: (v, s[:2]))(*real_linework(lines)))
    with pytest.raises(M.MeshError, match="no triangle"):        # KiPI would drop it silently
        _mesh(name, poly, [], MAX_AREA, 28)
    said = []
    meshing.install(name, empty="warn")
    pts, tris = M.mesh_polygon(poly, [], MAX_AREA, 28, warn=said.append)
    assert len(tris) == 0 and "no triangle" in said[0]
    monkeypatch.setattr(M, "_linework", real_linework)
    with pytest.raises(M.MeshError, match="zero-area"):
        pipeline.finish(np.array([[0.0, 0], [1, 0], [2, 0], [0, 1]]), np.array([[0, 1, 3], [0, 1, 2]]),
                        "here")
    calls = []
    real_cap = M._steiner_cap

    def cap_once(a, n):                                          # only the first pass is capped
        calls.append(1)
        return 3 if len(calls) == 1 else real_cap(a, n)
    monkeypatch.setattr(M, "_steiner_cap", cap_once)
    pad = box(X0 + 0.4, Y0 + 0.4, X0 + 0.45, Y0 + 0.45)
    said = []
    pts, tris = _mesh(name, poly, [pad.boundary], MAX_AREA, 28, warn=said.append)
    assert any("without them" in s for s in said)
    _check(pts, tris, poly, pads=[pad])
    monkeypatch.setattr(M, "_steiner_cap", lambda a, n: 3)
    with pytest.raises(M.MeshError, match="converge"):
        _mesh(name, poly, [], MAX_AREA, 28)
    monkeypatch.setattr(M, "_steiner_cap", real_cap)
    # Seeds that leave nothing (one in the copper, one in the hole): mesh without seeds, then cut
    # the hole out by centroid, as KiPI does.
    hole = _hole(X0 + 0.5, Y0 + 0.5, 0.2)
    holed = poly.difference(hole)
    seeds = [Point(X0 + 0.05, Y0 + 0.05), Point(X0 + 0.5, Y0 + 0.5)]
    monkeypatch.setattr(M, "_hole_seeds", lambda p, w=None: seeds)
    said = []
    pts, tris = _mesh(name, holed, [], MAX_AREA, 28, warn=said.append)
    assert any("no domain" in s for s in said)
    _check(pts, tris, holed, [hole])


def test_install_and_uninstall_are_idempotent_and_restore_kipi(name):
    import kipi.core.mesh.parallel as P
    import kipi.core.viz.meshes as V
    orig = (M.mesh_polygon, P.mesh_layers, os.environ.get("KIPI_SERIAL"), V._triangle,
            sys.modules.get("triangle"))
    meshing.uninstall()                                          # nothing installed: nothing to do
    b = meshing.install(name)
    first = M.mesh_polygon
    assert meshing.install(name) is b and M.mesh_polygon is first   # twice: unchanged
    assert meshing.installed() == name and P.mesh_layers(None) is None and os.environ["KIPI_SERIAL"] == "1"
    other = [n for n in meshing.available() if n != name]
    if other:
        meshing.install(other[0])                                # another: replaces it
        assert meshing.installed() == other[0]
    meshing.uninstall()
    meshing.uninstall()
    assert meshing.installed() is None
    assert (M.mesh_polygon, P.mesh_layers, os.environ.get("KIPI_SERIAL"), V._triangle,
            sys.modules.get("triangle")) == orig


def _via_array():
    from coppernicus.engine import synthetic as S
    from coppernicus.engine.board import Board
    from coppernicus.project import Load, Project, Rail, Settings, Source
    b = S.SyntheticBoard("via_array", S.two_layer_stackup())
    b.zone("VCC", S.F, box(0, 0, 12, 20))
    b.zone("VCC", S.B, box(8, 0, 20, 20))
    b.pad("J1", "1", "VCC", box(0, 0, 1.5, 20))
    b.pad("U1", "1", "VCC", box(18.5, 0, 20, 20), layers=(S.B,))
    for i in range(5):
        for j in range(5):
            b.via("VCC", 8.5 + 0.75 * i, 8.5 + 0.75 * j)
    project = Project(board="", rails=[Rail("VCC", sources=[Source("J1", 1.0, ["1"])],
                                            loads=[Load("U1", 1.0, ["1"])])], settings=Settings())
    return Board(b.snapshot()), project


def _demo():
    from coppernicus.engine.board import Board
    from coppernicus.project import Project
    yml = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
    project = Project.load(yml)
    return Board.open(yml.parent / project.board), project


@pytest.mark.parametrize("case", [_via_array, _demo], ids=["via_array", "demo"])
def test_triangle_through_the_seam_is_bit_identical_to_kipis_own_path(case):
    if "triangle" not in meshing.available():
        pytest.skip("Triangle not installed")
    from coppernicus.engine.solve import solve_rail
    board, project = case()
    rail = project.rails[0]
    meshing.uninstall()
    pipeline.STATS.clear()
    own = solve_rail(board, rail, project.settings, project=project)
    assert not pipeline.STATS                                    # KiPI's own path: no seam
    meshing.install("triangle")
    seam = solve_rail(board, rail, project.settings, project=project)
    assert pipeline.STATS and all(r["mesher"] == "triangle" for r in pipeline.STATS)   # the seam ran
    pipeline.STATS.clear()
    assert len(own.layers) == len(seam.layers)
    for a, b in zip(own.layers, seam.layers):
        assert np.array_equal(a.points, b.points) and np.array_equal(a.tris, b.tris)
        assert np.array_equal(a.voltage, b.voltage) and np.array_equal(a.current_density, b.current_density)
    assert (own.max_drop, own.power_loss, own.loss_vias) == (seam.max_drop, seam.power_loss, seam.loss_vias)


def test_another_kipi_is_refused_and_nothing_is_patched(name, monkeypatch):
    import kipi
    import kipi.core.mesh.mesher as M
    import kipi.core.mesh.parallel as P
    import kipi.core.workers as W
    before = (M.mesh_polygon, P.mesh_layers, W._start, os.environ.get("KIPI_SERIAL"))
    monkeypatch.setattr(kipi, "__version__", "1.0.2")
    assert meshing.seam_problem() == "written for KiPI 1.0.1, this is KiPI 1.0.2"
    with pytest.raises(RuntimeError, match="not installed: written for KiPI 1.0.1, this is KiPI 1.0.2"):
        meshing.install(name)
    assert meshing.installed() is None
    assert (M.mesh_polygon, P.mesh_layers, W._start, os.environ.get("KIPI_SERIAL")) == before


def test_kipi_functions_of_other_signatures_are_refused(name, monkeypatch):
    import kipi.core.mesh.mesher as M
    import kipi.core.workers as W
    assert meshing.seam_problem() is None
    monkeypatch.setattr(W, "_start", lambda path, colors, extra: None)
    with pytest.raises(RuntimeError, match=r"kipi.core.workers._start takes \(path, colors, extra\)"):
        meshing.install(name)
    monkeypatch.undo()
    monkeypatch.delattr(M, "_carve_holes")
    with pytest.raises(RuntimeError, match="no kipi.core.mesh.mesher._carve_holes"):
        meshing.install(name)
    assert meshing.installed() is None
