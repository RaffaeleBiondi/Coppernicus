"""The "rust" meshing backend (experimental): coppermesh, our own constrained Delaunay triangulation
and Delaunay refinement in Rust (tools/mesher_rs; its DESIGN.md gives the algorithms and sources).

The PSLG is triangulated and carved by its hole seeds in the core, then refined to the angle and
area bounds under ``first_cap``. With a size field, a second refinement follows:
- "graded" (KiPI's ``graded_size``, as hole features): h is evaluated natively, exactly as KiPI
  evaluates it, at every triangle's centroid;
- "sampled": the field is evaluated once, vectorised, at the uniform mesh's nodes, and the core
  interpolates it linearly on that frozen mesh (no Python call per inserted point).

``triangle_compat()`` stands in for Triangle where KiPI calls it directly: the 3D view ("pQ") and
the noise cross-section ("pq<angle>a<area>", then "rpq<angle>a" with per-triangle limits).

``size_rule`` "area" (default) is the protocol's size test. "edge" bounds the longest edge by
min(h, max_edge) instead, which is stricter and still meets the area bounds.
"""

from __future__ import annotations

import math
import re
import time

import numpy as np

from coppernicus.engine.meshing import protocol as _protocol
from coppernicus.engine.meshing.protocol import (
    STATUS_CAP,
    STATUS_GRADED_CAP,
    STATUS_NO_DOMAIN,
    STATUS_OK,
    Triangulation,
)


def _empty():
    return np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64)


def _limits(owner=None):
    """(the floor in mm, the tiny-wedge angle in degrees) for the core."""
    from kipi.core.mesh import mesher as M
    floor = getattr(owner, "min_edge_mm", None)
    return (M.NODING_GRID_MM if floor is None else float(floor),
            float(getattr(owner, "tiny_angle_deg", RustBackend.tiny_angle_deg)))


class _Mesh:
    """One PSLG's mesh in the Rust core. ``export`` is the seam the tests patch."""

    def __init__(self, verts, segs, holes, limits=(0.0, 0.0)):
        import coppermesh
        self.limits = {"min_edge": float(limits[0]), "tiny_angle": float(limits[1])}
        self.core = coppermesh.Mesher(np.ascontiguousarray(verts, dtype=float),
                                      np.ascontiguousarray(segs, dtype=np.int64).reshape(-1, 2),
                                      np.ascontiguousarray(holes, dtype=float).reshape(-1, 2))

    def area(self):
        return self.core.area()

    def refine(self, min_angle, max_area, cap, rule):
        return self.core.refine(float(min_angle), float(max_area), int(cap), rule, **self.limits)

    def refine_sized(self, h, min_angle, max_area, cap, rule):
        return self.core.refine_sized(np.ascontiguousarray(h, dtype=float), float(min_angle),
                                      float(max_area), int(cap), rule, **self.limits)

    def refine_graded(self, field, min_angle, max_area, cap, rule):
        return self.core.refine_graded(np.ascontiguousarray(field.centres, dtype=float).reshape(-1, 2),
                                       np.ascontiguousarray(field.radii, dtype=float).reshape(-1),
                                       float(field.h_min), float(field.h_max), float(field.grading),
                                       float(min_angle), float(max_area), int(cap), rule, **self.limits)

    def retain(self, keep):
        self.core.retain(np.ascontiguousarray(keep, dtype=bool))

    def export(self):
        pts, tris = self.core.export()
        return np.asarray(pts, dtype=float), np.asarray(tris, dtype=np.int64)

    def stats(self):
        return dict(self.core.stats())


class RustBackend:
    """coppermesh behind the mesher seam (see the module docstring)."""

    name = "rust"
    strict_size = True           # KiPI's graded field is met at every triangle's own centroid
    settings = ("size_rule", "min_edge_mm", "tiny_angle_deg")
    #: No point is added closer than this to a vertex (mm); None: KiPI's noding grid (1 nm), the
    #: resolution its input is snapped to.
    min_edge_mm = None
    #: Input segments meeting at less than this (degrees) bound a tiny wedge, accepted as it is
    #: (DESIGN.md, "Near-zero input angles").
    tiny_angle_deg = 1.0

    def __init__(self, size_rule: str = "area"):
        self.size_rule = size_rule

    def available(self) -> bool:
        try:
            import coppermesh  # noqa: F401
        except ImportError:
            return False
        return True

    def triangulate(self, points, segments, holes, max_area, min_angle, size_field, interior_points, *,
                    first_cap, carve=None) -> Triangulation:
        t0 = time.perf_counter()
        info = {"backend": self.name, "rule": self.size_rule, "graded": False}
        verts = np.asarray(points, dtype=float).reshape(-1, 2)
        if interior_points is not None and len(interior_points):
            verts = np.vstack([verts, np.asarray(interior_points, dtype=float).reshape(-1, 2)])
        seeds = np.asarray(holes if holes is not None else np.zeros((0, 2)), dtype=float).reshape(-1, 2)
        m = _Mesh(verts, segments, seeds, _limits(self))
        info["t_build"] = time.perf_counter() - t0
        if len(seeds) and m.area() == 0.0:
            return Triangulation(*_empty(), STATUS_NO_DOMAIN, info)
        t = time.perf_counter()
        ok = m.refine(min_angle, max_area, first_cap, self.size_rule)
        info["t_uniform"] = time.perf_counter() - t
        if not ok:
            info["core"] = m.stats()
            return Triangulation(*_empty(), STATUS_CAP, info)
        pts, tris = m.export()
        if carve is not None and len(tris):
            import shapely
            c = pts[tris].mean(axis=1)
            keep = shapely.contains_xy(carve, c[:, 0], c[:, 1])
            if not keep.all():
                m.retain(keep)
                pts, tris = m.export()
        info["uniform_tris"] = int(len(tris))
        status = STATUS_OK
        if size_field is not None and len(tris):
            needed, limit, area = _protocol.graded_limits(pts, tris, size_field, max_area)
            if needed:
                t = time.perf_counter()
                cap = _protocol.graded_cap(float((area / limit).sum()), len(pts))
                if size_field.kind == "graded":
                    ok = m.refine_graded(size_field, min_angle, max_area, cap, self.size_rule)
                else:
                    h = size_field.evaluate(pts)
                    ok = m.refine_sized(h, min_angle, max_area, cap, self.size_rule)
                info.update(graded=True, t_graded=time.perf_counter() - t)
                if ok:
                    pts, tris = m.export()
                else:
                    status = STATUS_GRADED_CAP
        info.update(core=m.stats(), seconds=time.perf_counter() - t0)
        return Triangulation(pts, tris, status, info)

    def triangle_compat(self):
        return TriangleCompat()


class TriangleCompat:
    """``triangulate(data, switches)`` for the switches KiPI's two direct Triangle callers use:
    - "pQ" (the 3D view): the constrained Delaunay triangulation of the rings, nothing added;
      the region outside the outermost segments is removed, unseeded holes stay meshed;
    - "pq<angle>a<area>" (the noise cross-section's first pass): quality and area bounds, with
      ``holes``;
    - "rpq<angle>a" with ``triangle_max_area`` (its refinement passes): the triangulation of the
      given nodes and segments is rebuilt (the same Delaunay mesh up to cocircular ties) and the
      per-triangle limits become a per-node size field (the smallest limit at each node,
      interpolated in between), so the refinement is close to, not the same as, Triangle's.
    The returned "segments" are the subsegments, so that a later "r" pass conforms to them."""
    __name__ = "triangle"            # looks like the module to ``import triangle`` callers

    def triangulate(self, data, switches: str = ""):
        import coppermesh
        from kipi.core.mesh import mesher as M
        sw = switches.replace("Q", "")
        q = re.search(r"q([0-9.]*)", sw)
        min_angle = float(q.group(1) or 20.0) if q else 0.0
        a = re.search(r"a([0-9.eE+-]*)", sw)
        area_txt = a.group(1) if a else ""

        def arr(key, dtype):
            a = np.asarray(data.get(key, np.zeros((0, 2))), dtype=dtype)
            return np.ascontiguousarray(a.reshape(-1, 2))
        verts, segs, holes = arr("vertices", float), arr("segments", np.int64), arr("holes", float)
        core = coppermesh.Mesher(verts, segs, holes)
        if sw.startswith("r") and "triangle_max_area" in data:
            tri = np.asarray(data["triangles"], dtype=np.int64)
            limit = np.asarray(data["triangle_max_area"], dtype=float)
            h_tri = np.sqrt(4 * limit / math.sqrt(3))
            h_in = np.full(len(verts), np.inf)
            np.minimum.at(h_in, tri.ravel(), np.repeat(h_tri, 3))
            pts, _ = core.export()
            index = {tuple(v): k for k, v in enumerate(map(tuple, verts))}
            h = np.array([h_in[index[tuple(v)]] for v in map(tuple, pts)])
            x, y = verts[tri, 0], verts[tri, 1]
            area = 0.5 * np.abs((x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0])
                                - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0]))
            cap = M._steiner_cap(float((area / limit).sum()), len(verts))
            core.refine_sized(h, min_angle, float(limit.max()), cap, "area", *_limits())
        elif min_angle > 0 or area_txt:
            max_area = float(area_txt) if area_txt else math.inf
            span = verts.max(axis=0) - verts.min(axis=0)
            cap_area = max_area if math.isfinite(max_area) else float(span.prod()) or 1.0
            cap = M._steiner_cap(float(span.prod()) / cap_area, len(verts))
            core.refine(min_angle, max_area, cap, "area", *_limits())
        pts, tris = core.export()
        return {"vertices": np.asarray(pts, dtype=float), "triangles": np.asarray(tris, dtype=np.int64),
                "segments": np.asarray(core.subsegments(), dtype=np.int64).reshape(-1, 2)}
