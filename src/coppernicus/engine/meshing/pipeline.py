"""KiPI v1.0.1's ``mesh_polygon`` around a meshing backend (experimental).

The pre-processing and the fallbacks are KiPI's own (``kipi.core.mesh.mesher``), called through
the module at call time, so a wrapper on one of KiPI's helpers reaches every backend:

- the pad outlines whose box meets the polygon, clipped by ``_clip_constraint``;
- noding on the 1 nm grid (``_linework``) and the ``pslg_problems`` guard (a problem counts as a
  cap hit, as in KiPI);
- hole seeds (``_hole_seeds``), and KiPI's Steiner cap (``_steiner_cap``) for the first pass;
- cap hit: mesh without the pad outlines plus one node per pad (``_pad_nodes``), then MeshError;
- seeds that leave nothing: mesh without seeds, keep the triangles whose centroid is in the copper;
- graded pass on the backend's side (:mod:`.protocol`); its cap hit keeps the uniform mesh, with
  KiPI's warning.

Two things differ from KiPI, on purpose: copper that meshes into no triangle raises MeshError
(KiPI's ``mesh_layer`` skips such a polygon silently; ``empty="warn"`` warns instead), and the
mesh is checked as the solver checks it (no zero-area triangle; unused nodes dropped).
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import numpy as np
from kipi.core.mesh import mesher as M

from coppernicus.engine.meshing.protocol import (
    STATUS_CAP,
    STATUS_GRADED_CAP,
    STATUS_NO_DOMAIN,
    SizeField,
)

#: Per-polygon records of the last meshes (benchmarks read it; cleared by the caller). Only the
#: polygons meshed in this process: those of KiPI's pool workers stay there (an ``options["stats"]``
#: list keeps the pools in one process, see :mod:`.registry`).
STATS: List[dict] = []


class _CapHit(RuntimeError):
    pass


def finish(pts: np.ndarray, tris: np.ndarray, where: str):
    """The mesh as the solver needs it: no orphan node (unused nodes dropped; none dropped keeps
    the arrays as they are) and no zero-area triangle (the DC FEM's own test raises MeshError)."""
    pts = np.asarray(pts, dtype=float)
    tris = np.asarray(tris, dtype=np.int64)
    used = np.unique(tris)
    if len(used) != len(pts):
        index = np.full(len(pts), -1, dtype=np.int64)
        index[used] = np.arange(len(used))
        pts, tris = pts[used], index[tris]
    x, y = pts[tris, 0], pts[tris, 1]
    two_a = (x * np.stack([y[:, 1] - y[:, 2], y[:, 2] - y[:, 0], y[:, 0] - y[:, 1]], axis=1)).sum(axis=1)
    bad = int((np.abs(two_a) / 2 <= 0).sum())
    if bad:
        raise M.MeshError(f"The mesher left {bad} zero-area triangle(s) {where}")
    return pts, tris, float(np.abs(two_a).sum() / 2)


def mesh_polygon(backend, options: Optional[Dict] = None, poly=None, constraints=(), max_area=None,
                 min_angle=None, size_fn=None, warn=None, points=None):
    """KiPI's ``mesh_polygon(poly, constraints, max_area, min_angle, size_fn, warn, points)`` with
    ``backend`` meshing (``options``: ``empty`` "raise" | "warn", ``stats`` a list for records)."""
    options = options or {}
    warn = warn or (lambda _t: None)
    t_start = time.perf_counter()
    rec = {"mesher": backend.name, "area": float(poly.area), "status": "ok"}
    stats = options.get("stats", STATS)

    def done(status=None):
        if status:
            rec["status"] = status
        rec["seconds"] = time.perf_counter() - t_start
        if stats is not None:
            stats.append(rec)

    x0, y0, x1, y1 = poly.bounds
    near = [c for c in constraints
            if not (c.bounds[0] > x1 or c.bounds[2] < x0 or c.bounds[1] > y1 or c.bounds[3] < y0)]
    clipped = []
    if near:
        edge = poly.boundary
        near_edge = edge.buffer(M.CONSTRAINT_CLEARANCE_MM, quad_segs=2)
        clipped = [g for g in (M._clip_constraint(c, poly, edge, near_edge) for c in near) if g is not None]
    holes = M._hole_seeds(poly, warn)
    holes_xy = np.array([[h.x, h.y] for h in holes], dtype=float).reshape(-1, 2)
    field = SizeField.from_size_fn(size_fn, max_area)
    where = f"near ({poly.centroid.x:.2f}, {poly.centroid.y:.2f}) mm"
    none = np.zeros((0, 2))

    def first_pass(with_constraints: bool):
        lines = [poly.exterior, *poly.interiors]
        if with_constraints:
            lines += clipped
        verts, segs = M._linework(lines)
        if len(segs) < 3:
            return None
        if M.pslg_problems(verts, segs):
            raise _CapHit()             # malformed input is treated like a non-converging mesh
        interior = none
        if points is not None and len(points):
            interior = np.asarray(points, dtype=float).reshape(-1, 2)
        if not with_constraints:
            inside = M._pad_nodes(poly, near)
            if len(inside):
                interior = np.vstack([interior, inside])
        cap = M._steiner_cap(poly.area / max_area, len(verts) + len(interior))
        res = backend.triangulate(verts, segs, holes_xy, max_area, min_angle, field, interior, first_cap=cap)
        if res.status == STATUS_CAP:
            raise _CapHit()
        if res.status == STATUS_NO_DOMAIN:
            res = backend.triangulate(verts, segs, none, max_area, min_angle, field, interior, first_cap=cap,
                                      carve=poly)
            if res.status == STATUS_CAP:
                raise _CapHit()
            if len(res.tris):
                warn(f"Copper {M._place(poly)}: a hole seed left the mesher with no domain; "
                     "cut the holes out after meshing")
                rec["status"] = "carved"
        return res

    try:
        res = first_pass(True)
    except _CapHit:
        if not clipped:
            done("fail")
            raise M.MeshError(f"Mesher could not converge on the copper {where}") from None
        warn(f"Mesher struggled with pad outlines {where}; meshing without them "
             "(pads are still found by position)")
        rec["status"] = "fallback_no_constraints"
        try:
            res = first_pass(False)
        except _CapHit:
            done("fail")
            raise M.MeshError(f"Mesher could not converge on the copper {where}") from None
    if res is None or not len(res.tris):
        done("empty")
        text = f"The mesher made no triangle in {poly.area:.6g} mm² of copper {where}"
        if options.get("empty", "raise") == "warn":
            warn(text)
            return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)
        raise M.MeshError(text)
    if res.status == STATUS_GRADED_CAP:
        warn(f"Via refinement {where} did not converge; using the unrefined mesh there")
        rec["status"] = "graded_cap"
    pts, tris, mesh_area = finish(res.pts, res.tris, where)
    rec.update(n_tris=int(len(tris)), n_nodes=int(len(pts)), mesh_area=mesh_area, **(res.info or {}))
    done()
    return pts, tris
