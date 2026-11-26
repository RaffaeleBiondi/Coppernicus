"""The mesher seam: what a meshing backend receives and returns (experimental).

KiPI meshes each connected copper polygon with ``kipi.core.mesh.mesher.mesh_polygon``.
:mod:`coppernicus.engine.meshing.pipeline` keeps KiPI's own pre-processing and its contract
around that call (clipping of the pad outlines, the 1 nm snap, the PSLG guard, hole seeds, pad
nodes, the fallbacks, the Steiner cap, the solver's checks) and hands the meshing itself to a
backend through one call:

    backend.triangulate(points, segments, holes, max_area, min_angle, size_field,
                        interior_points, first_cap=..., carve=None) -> Triangulation

What a backend must do (Triangle's behaviour under KiPI's switches, stated as a contract):

- ``points`` (N, 2) float64: the PSLG's vertices, already on KiPI's 1 nm grid. Each one stays a
  node, bit for bit.
- ``segments`` (S, 2) int64 into ``points``: the mesh conforms to every one (a segment may be
  split by added nodes on it).
- ``holes`` (H, 2): seeds. The region around each, bounded by segments, is not meshed; so is
  everything outside the outermost segments.
- ``interior_points`` (P, 2) float64, not snapped: each becomes a node (if it lies in the domain).
- Uniform pass: every triangle's area <= ``max_area`` and its smallest angle >= ``min_angle``
  (degrees), except where an input angle is smaller. When it adds ``first_cap`` or more nodes,
  stop and return status ``"cap"``.
- With ``holes`` and nothing left to mesh, return status ``"no_domain"`` (the pipeline then
  calls again without holes and with ``carve``).
- ``carve``: a shapely polygon; keep only the triangles whose centroid lies in it (KiPI's
  ``_carve_holes``), before the graded pass.
- Graded pass, only when ``size_field`` is given and :func:`graded_limits` on the uniform mesh
  says so (KiPI's gate): refine until each triangle's area <= min(sqrt(3)/4 h^2, max_area),
  h = ``size_field`` at its centroid. When it adds :func:`graded_cap` (of that uniform mesh) or
  more nodes, return the uniform mesh with status ``"graded_cap"``.
- Return ``Triangulation(pts (N, 2) float64, tris (M, 3) int64, status, info)``. Unused nodes
  are allowed (the pipeline drops them); a zero-area triangle is an error (the pipeline raises).

The size field is data built once per polygon (:class:`SizeField`), never a Python callback per
insertion: KiPI's ``graded_size`` arrives as its hole features (exact, for a native backend);
any other ``size_fn`` (e.g. the AC far mode's ``zone_size``) is ``"sampled"``: a backend
evaluates it once per pass, vectorised, at points of its choosing (its uniform mesh's nodes or
centroids, as KiPI's own second pass evaluates it at the first pass's centroids).
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, NamedTuple, Optional, Protocol, runtime_checkable

import numpy as np

STATUS_OK = "ok"
STATUS_CAP = "cap"                  # the uniform pass reached first_cap
STATUS_NO_DOMAIN = "no_domain"      # holes left nothing to mesh
STATUS_GRADED_CAP = "graded_cap"    # the graded pass reached its cap: the uniform mesh returned
STATUSES = (STATUS_OK, STATUS_CAP, STATUS_NO_DOMAIN, STATUS_GRADED_CAP)


class Triangulation(NamedTuple):
    pts: np.ndarray                 # (N, 2) float64
    tris: np.ndarray                # (M, 3) int64
    status: str = STATUS_OK
    info: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SizeField:
    """A polygon's target edge length h(x, y), as KiPI's ``size_fn`` gives it, as data.

    ``kind`` "graded": KiPI's ``graded_size``: h = clip(h_min + grading * max(|xy - c_i| - r_i, 0),
    h_min, h_max), i the nearest hole centre (``centres``, ``radii``). ``kind`` "sampled": any other
    size function; :meth:`evaluate` samples it (vectorised; once per pass)."""
    kind: str
    max_area: float
    centres: Optional[np.ndarray] = None
    radii: Optional[np.ndarray] = None
    h_min: float = 0.0
    h_max: float = 0.0
    grading: float = 0.0
    source: Optional[Callable[[np.ndarray], np.ndarray]] = field(default=None, repr=False, compare=False)

    @classmethod
    def from_size_fn(cls, size_fn, max_area: float) -> Optional["SizeField"]:
        """The field of KiPI's ``size_fn`` (None for none). KiPI's ``graded_size`` is recognised by
        its closure (its tree of hole centres, their radii, h_min, h_max, grading); anything else
        is "sampled"."""
        if size_fn is None:
            return None
        try:
            nl = inspect.getclosurevars(size_fn).nonlocals
            centres = np.ascontiguousarray(np.asarray(nl["tree"].data, dtype=float))
            radii = np.ascontiguousarray(np.asarray(nl["radius"], dtype=float))
            return cls("graded", float(max_area), centres, radii, float(nl["h_min"]), float(nl["h_max"]),
                       float(nl["grading"]), size_fn)
        except (KeyError, TypeError, AttributeError):
            return cls("sampled", float(max_area), source=size_fn)

    def evaluate(self, xy: np.ndarray) -> np.ndarray:
        """h at the points ``xy`` (K, 2), exactly as KiPI's ``size_fn`` gives it."""
        return np.asarray(self.source(np.asarray(xy, dtype=float)), dtype=float)


def graded_limits(pts: np.ndarray, tris: np.ndarray, size_field: SizeField, max_area: float):
    """KiPI's gate for its second pass, with its arithmetic: (needed, limit (M,), area (M,)) where
    limit = min(sqrt(3)/4 h^2, max_area), h at each triangle's centroid."""
    h = size_field.evaluate(pts[tris].mean(axis=1))
    limit = np.minimum(math.sqrt(3) / 4 * h * h, max_area)
    x, y = pts[tris, 0], pts[tris, 1]
    area = 0.5 * np.abs((x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0]) - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0]))
    return bool(np.any(limit < max_area)), limit, area


def graded_cap(area_over_limit_sum: float, n_nodes: int) -> int:
    """KiPI's Steiner cap for its second pass (``kipi.core.mesh.mesher._steiner_cap``)."""
    from kipi.core.mesh import mesher as M
    return M._steiner_cap(area_over_limit_sum, n_nodes)


@runtime_checkable
class Backend(Protocol):
    """A meshing backend (see the module docstring for the contract)."""
    name: str

    def available(self) -> bool:
        """Whether it can run here (its library imports)."""
        ...

    def triangulate(self, points: np.ndarray, segments: np.ndarray, holes: np.ndarray, max_area: float,
                    min_angle: float, size_field: Optional[SizeField], interior_points: np.ndarray, *,
                    first_cap: int, carve=None) -> Triangulation:
        ...
