"""The "rust" meshing backend (coppermesh, tools/mesher_rs) meets the mesher seam's protocol
(coppernicus.engine.meshing.protocol); KiPI's contract through the seam is tests/test_meshing_seam.py.
Skipped where the Rust extension is not built (`maturin develop --release` in tools/mesher_rs)."""

import math

import numpy as np
import pytest
from shapely.geometry import Point, box

coppermesh = pytest.importorskip("coppermesh")
from kipi.core.mesh import mesher as M  # noqa: E402

from coppernicus.engine.meshing import protocol as P  # noqa: E402
from coppernicus.engine.meshing import rust_backend as RB  # noqa: E402

X0, Y0 = 120.0, 80.0                     # board-like coordinates: an ulp is ~1e-14 mm here
MAX_AREA = math.sqrt(3) / 4 * 0.5 ** 2


def _hole(cx, cy, r):
    return Point(cx, cy).buffer(r, quad_segs=2)


def _angles(pts, tris):
    p = pts[tris]
    e = np.stack([np.linalg.norm(p[:, 1] - p[:, 2], axis=1), np.linalg.norm(p[:, 2] - p[:, 0], axis=1),
                  np.linalg.norm(p[:, 0] - p[:, 1], axis=1)], axis=1)
    a, b, c = e.T
    ang = np.degrees(np.arccos(np.clip([(b * b + c * c - a * a) / (2 * b * c),
                                         (a * a + c * c - b * b) / (2 * a * c)], -1, 1)))
    third = 180 - ang.sum(axis=0)
    return np.vstack([ang, third]).min(axis=0)


def _check(pts, tris, poly):
    """Valid, conforming, all the copper, every input vertex bit-exact (the protocol's output)."""
    assert len(tris) > 0
    p = pts[tris]
    two_a = ((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
             - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
    assert np.all(two_a > 0)                                     # counter-clockwise, none zero-area
    assert abs(two_a.sum() / 2 - poly.area) <= poly.boundary.length * M.NODING_GRID_MM
    verts, segs = M._linework([poly.exterior, *poly.interiors])
    have = set(map(tuple, pts))
    assert all(tuple(v) in have for v in verts)
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    e, n = np.unique(e, axis=0, return_counts=True)
    border = e[n == 1]
    length = np.hypot(*(pts[border[:, 0]] - pts[border[:, 1]]).T).sum()
    snapped = np.hypot(*(verts[segs[:, 0]] - verts[segs[:, 1]]).T).sum()
    assert abs(length - snapped) <= 1e-12 * snapped


# --------------------------------------------------------------------- the backend, by the protocol

def _pslg(poly):
    verts, segs = M._linework([poly.exterior, *poly.interiors])
    return verts, segs


def _plate_with_vias():
    vias = [(X0 + 1 + 0.75 * i, Y0 + 1.5, 0.15) for i in range(3)]
    poly = box(X0, Y0, X0 + 4, Y0 + 3)
    for x, y, r in vias:
        poly = poly.difference(_hole(x, y, r))
    seeds = np.array([[x, y] for x, y, _ in vias])
    return poly, seeds, np.array(vias)


def test_backend_meets_the_protocol_and_its_statuses():
    b = RB.RustBackend()
    assert isinstance(b, P.Backend) and b.name == "rust" and b.available()
    poly, seeds, _ = _plate_with_vias()
    verts, segs = _pslg(poly)
    r = b.triangulate(verts, segs, seeds, MAX_AREA, 28, None, np.zeros((0, 2)), first_cap=10 ** 6)
    assert r.status == P.STATUS_OK and r.pts.dtype == np.float64 and r.tris.dtype == np.int64
    _check(r.pts, r.tris, poly)
    assert _angles(r.pts, r.tris).min() >= 28 - 1e-6
    assert (np.abs(P.graded_limits(r.pts, r.tris, P.SizeField("sampled", MAX_AREA, source=lambda xy: np.full(
        len(xy), 0.5)), MAX_AREA)[2]) <= MAX_AREA * (1 + 1e-12)).all()
    capped = b.triangulate(verts, segs, seeds, MAX_AREA, 28, None, np.zeros((0, 2)), first_cap=5)
    assert capped.status == P.STATUS_CAP and len(capped.tris) == 0
    inside = np.vstack([seeds, [[X0 + 0.5, Y0 + 0.5]]])        # and a seed in the copper itself
    gone = b.triangulate(verts, segs, inside, MAX_AREA, 28, None, np.zeros((0, 2)), first_cap=10 ** 6)
    assert gone.status == P.STATUS_NO_DOMAIN and len(gone.tris) == 0


def test_backend_carves_by_centroid_before_grading():
    b = RB.RustBackend()
    poly, seeds, feats = _plate_with_vias()
    verts, segs = _pslg(poly)
    field = P.SizeField.from_size_fn(M.graded_size(feats, 0.02, 0.5, 0.2), MAX_AREA)
    r = b.triangulate(verts, segs, np.zeros((0, 2)), MAX_AREA, 28, field, np.zeros((0, 2)),
                      first_cap=10 ** 6, carve=poly)
    assert r.status == P.STATUS_OK and r.info["graded"]
    used = np.unique(r.tris)
    _check(r.pts[used], np.searchsorted(used, r.tris), poly)


def test_native_graded_field_equals_kipis_and_bounds_every_centroid():
    poly, seeds, feats = _plate_with_vias()
    size_fn = M.graded_size(feats, 0.02, 0.5, 0.2)
    field = P.SizeField.from_size_fn(size_fn, MAX_AREA)
    assert field.kind == "graded"
    rng = np.random.default_rng(7)
    xy = np.column_stack([X0 - 1 + 6 * rng.random(5000), Y0 - 1 + 5 * rng.random(5000)])
    native = coppermesh.graded_size(field.centres, field.radii, field.h_min, field.h_max, field.grading, xy)
    kipi = size_fn(xy)                                           # SciPy's k-d tree distances may differ
    assert np.all(np.abs(native - kipi) <= 4 * np.spacing(kipi))  # by an ulp or two (FMA in its build)
    verts, segs = _pslg(poly)
    r = RB.RustBackend().triangulate(verts, segs, seeds, MAX_AREA, 28, field, np.zeros((0, 2)),
                                     first_cap=10 ** 6)
    assert r.status == P.STATUS_OK and r.info["graded"]
    _, limit, area = P.graded_limits(r.pts, r.tris, field, MAX_AREA)
    assert np.all(area <= limit * (1 + 1e-12))                   # KiPI's limit at every centroid


def test_sampled_field_refines_and_graded_cap_returns_the_uniform_mesh(monkeypatch):
    b = RB.RustBackend()
    poly, seeds, feats = _plate_with_vias()
    verts, segs = _pslg(poly)
    size_fn = M.graded_size(feats, 0.02, 0.5, 0.2)
    calls = []

    def sampled(xy):
        calls.append(len(xy))
        return size_fn(xy)
    field = P.SizeField("sampled", MAX_AREA, source=sampled)
    uniform = b.triangulate(verts, segs, seeds, MAX_AREA, 28, None, np.zeros((0, 2)), first_cap=10 ** 6)
    r = b.triangulate(verts, segs, seeds, MAX_AREA, 28, field, np.zeros((0, 2)), first_cap=10 ** 6)
    assert r.status == P.STATUS_OK and len(r.tris) > 2 * len(uniform.tris)
    assert len(calls) == 2                       # the gate's centroids, then once at the uniform nodes
    monkeypatch.setattr(P, "graded_cap", lambda *a: 3)
    capped = b.triangulate(verts, segs, seeds, MAX_AREA, 28, field, np.zeros((0, 2)), first_cap=10 ** 6)
    assert capped.status == P.STATUS_GRADED_CAP
    assert np.array_equal(capped.tris, uniform.tris) and np.array_equal(capped.pts, uniform.pts)


def test_edge_rule_bounds_the_longest_edge():
    poly, seeds, _ = _plate_with_vias()
    verts, segs = _pslg(poly)
    r = RB.RustBackend("edge").triangulate(verts, segs, seeds, MAX_AREA, 28, None, np.zeros((0, 2)),
                                           first_cap=10 ** 6)
    e = np.hypot(*(r.pts[r.tris] - r.pts[np.roll(r.tris, 1, axis=1)]).transpose(2, 0, 1))
    assert r.status == P.STATUS_OK and e.max() <= 0.5 * (1 + 1e-12)
    assert _angles(r.pts, r.tris).min() >= 28 - 1e-6


def _two_a(p, t):
    q = p[t]
    return ((q[:, 1, 0] - q[:, 0, 0]) * (q[:, 2, 1] - q[:, 0, 1])
            - (q[:, 2, 0] - q[:, 0, 0]) * (q[:, 1, 1] - q[:, 0, 1]))


def test_rust_stands_in_for_kipis_direct_triangle_callers():
    import shapely
    shim = RB.RustBackend().triangle_compat()
    # The 3D view: a constrained Delaunay triangulation of the rings, no point added.
    poly = box(X0, Y0, X0 + 4, Y0 + 3).difference(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2))
    verts, segs = M._linework([poly.exterior, *poly.interiors])
    out = shim.triangulate({"vertices": verts, "segments": segs}, "pQ")
    assert np.array_equal(out["vertices"], verts)
    c = out["vertices"][out["triangles"]].mean(axis=1)
    kept = out["triangles"][shapely.contains_xy(poly, c[:, 0], c[:, 1])]
    assert abs(np.abs(_two_a(out["vertices"], kept)).sum() / 2 - poly.area) <= 1e-9 * poly.area
    # The noise cross-section: a quality pass with holes, then a refinement with per-triangle limits.
    holes = np.array([[X0 + 1.5, Y0 + 1.5]])
    out = shim.triangulate({"vertices": verts, "segments": segs, "holes": holes}, "pq28a0.125")
    p, t = out["vertices"], out["triangles"]
    assert abs(np.abs(_two_a(p, t)).sum() / 2 - poly.area) <= 1e-9 * poly.area
    assert np.all(np.abs(_two_a(p, t)) / 2 <= 0.125 * (1 + 1e-12)) and _angles(p, t).min() >= 28 - 1e-6
    left = p[t].mean(axis=1)[:, 0] < X0 + 1.0                  # per-triangle limits: fine on the left
    limit = np.where(left, 0.005, 0.05)
    out2 = shim.triangulate({"vertices": p, "triangles": t, "segments": out["segments"], "holes": holes,
                             "triangle_max_area": limit}, "rpq28a")
    p2, t2 = out2["vertices"], out2["triangles"]
    assert len(t2) > len(t) and all(tuple(v) in set(map(tuple, p2)) for v in p)
    assert abs(np.abs(_two_a(p2, t2)).sum() / 2 - poly.area) <= 1e-9 * poly.area
    a2, c2 = np.abs(_two_a(p2, t2)) / 2, p2[t2].mean(axis=1)[:, 0]
    assert np.all(a2 <= 0.05 * (1 + 1e-9)) and _angles(p2, t2).min() >= 28 - 1e-6
    assert np.all(a2[c2 < X0 + 0.5] <= 0.005 * (1 + 1e-9)) and a2[c2 > X0 + 2.5].max() > 0.01


def test_kipis_3d_view_and_noise_cross_section_run_on_the_rust_shim():
    import kipi.core.viz.meshes as V
    from kipi.noise.extract import xsection as X

    from coppernicus.engine import meshing
    poly = box(X0, Y0, X0 + 4, Y0 + 3).difference(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2))
    meshing.install("rust")
    try:
        assert V._triangle is not None and V._triangle.__name__ == "triangle"
        assert type(V._triangle).__module__ == RB.__name__          # the 3D view uses the shim
        pts, tris = V._triangle_fast(poly)
        assert len(tris) and abs(np.abs(_two_a(pts, tris)).sum() / 2 - poly.area) <= 1e-9 * poly.area
        rects = [X.Rect(-0.1, 0.1, 0.0, 0.035, group=X.VICTIM),
                 X.Rect(0.3, 0.5, 0.0, 0.035, group=X.AGGRESSOR)]
        bands = [X.Band(-0.2, 0.0, 4.2)]
        p, t = X.mesh(rects, bands, (-1.0, 1.4, -0.2, 0.5), 0.01, 0.2)
        assert len(t) > 100 and np.all(np.abs(_two_a(p, t)) > 0)
    finally:
        meshing.uninstall()
