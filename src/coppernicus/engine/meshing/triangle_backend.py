"""The Triangle backend: KiPI's own calls into Triangle, with KiPI's switches, behind the seam.

It runs exactly what ``kipi.core.mesh.mesher.mesh_polygon`` runs (``_triangulate`` with
``pq{min_angle}a{max_area}Q`` and KiPI's cap, the re-mesh without seeds then ``_carve_holes``,
the ``rpq{min_angle}aQ`` second pass with per-triangle limits from ``size_fn`` at the first pass's
centroids), so a mesh through the seam is bit-identical to KiPI's. Triangle itself is only ever
called through KiPI's functions.
"""

from __future__ import annotations

import types

import numpy as np

from coppernicus.engine.meshing.protocol import (
    STATUS_CAP,
    STATUS_GRADED_CAP,
    STATUS_NO_DOMAIN,
    STATUS_OK,
    Triangulation,
    graded_cap,
    graded_limits,
)
from coppernicus.triangle_standin import HINT, is_standin

_NONE = (np.zeros((0, 2)), np.zeros((0, 3), dtype=np.int64))


class TriangleBackend:
    name = "triangle"
    strict_size = False          # the graded pass samples size_fn once, at the first pass's centroids
    settings = ()                # KiPI's switches only

    def __init__(self):
        if not self.available():
            raise ImportError(HINT)       # the registry: "mesher 'triangle' is not installed here: ..."

    def available(self) -> bool:
        """Whether KiPI's mesher holds the real Triangle: the module it bound when it loaded, not
        Coppernicus's stand-in (:mod:`coppernicus.triangle_standin`) nor a seam backend's, which
        ``sys.modules["triangle"]`` holds while that backend is installed."""
        try:
            import kipi.core.mesh.mesher as M  # imports triangle at load
        except ImportError:
            return False
        bound = getattr(M, "triangle", None)
        return isinstance(bound, types.ModuleType) and not is_standin(bound)

    def triangulate(self, points, segments, holes, max_area, min_angle, size_field, interior_points, *,
                    first_cap, carve=None) -> Triangulation:
        from kipi.core.mesh import mesher as M
        verts = np.vstack([points, interior_points]) if len(interior_points) else points
        data = {"vertices": verts, "segments": segments}
        if len(holes):
            data["holes"] = np.asarray(holes, dtype=float)
        try:
            out = M._triangulate(data, f"pq{min_angle:g}a{max_area:.6g}Q", first_cap)
        except M.MeshCapHit:
            return Triangulation(*_NONE, STATUS_CAP)
        if len(holes) and not len(out.get("triangles", ())):
            return Triangulation(*_NONE, STATUS_NO_DOMAIN)
        if carve is not None and len(out.get("triangles", ())):
            out = M._carve_holes(out, carve)
        tris = out.get("triangles")
        if tris is None or len(tris) == 0:
            return Triangulation(*_NONE, STATUS_OK)
        pts = np.asarray(out["vertices"], dtype=float)
        tris = np.asarray(tris, dtype=np.int64)
        if size_field is not None:
            needed, limit, area = graded_limits(pts, tris, size_field, max_area)
            if needed:
                cap = graded_cap(float((area / limit).sum()), len(pts))
                try:
                    ref = M._triangulate({"vertices": pts, "triangles": tris, "segments": out["segments"],
                                          "triangle_max_area": limit}, f"rpq{min_angle:g}aQ", cap)
                except M.MeshCapHit:
                    return Triangulation(pts, tris, STATUS_GRADED_CAP)
                pts = np.asarray(ref["vertices"], dtype=float)
                tris = np.asarray(ref["triangles"], dtype=np.int64)
        return Triangulation(pts, tris, STATUS_OK)
