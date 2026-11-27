"""A KiPI worker's first step while a mesher is installed (experimental; see
:mod:`coppernicus.engine.meshing.pool`).

KiPI's pool workers are spawned: each is a fresh Python that re-imports what it needs. While a
mesher is installed, each worker's initializer is ``functools.partial(start, spec)``.
Unpickling it imports this module before anything else from Coppernicus or KiPI. Its imports are
the standard library only. It is outside :mod:`coppernicus.engine`, whose package imports KiPI
and so Triangle. :func:`start` then installs the same mesher, with the same settings, before
KiPI's own initializer runs.

One thing runs before it: spawn re-imports the parent's main module (as ``__mp_main__``).
``coppernicus.__main__`` (the ``coppernicus`` command) and ``coppernicus.ci`` import nothing of
KiPI at module level, so Triangle is not loaded first there. A main module that imports
:mod:`coppernicus.engine` at the top would load Triangle before :func:`start`.

``COPPERNICUS_MESH_WORKER_TRACE``: a file each worker appends one JSON line to once it has
started. The line holds its pid, the mesher installed, the settings, whether KiPI's mesher or
Triangle had been imported before :func:`start` ran, and what the 3D view meshes with there. It
is for the tests and for diagnostics.
"""

from __future__ import annotations

import json
import os
import sys

TRACE_ENV = "COPPERNICUS_MESH_WORKER_TRACE"


def start(spec, *kipi_args) -> None:
    """Install the mesher of ``spec`` here, then run KiPI's own worker initializer with
    ``kipi_args`` (``kipi.core.workers._start(path, colors)``). With ``spec`` None, only KiPI's.

    ``spec`` is plain data: ``{"name", "settings", "options"}``."""
    early = {"triangle": "triangle" in sys.modules, "kipi_mesher": "kipi.core.mesh.mesher" in sys.modules}
    # Nothing here has imported KiPI's mesher or Triangle yet: without the real package, the stand-in
    # takes Triangle's place before KiPI loads (multiprocessing has set sys.path by now).
    from coppernicus.triangle_standin import ensure_triangle_importable
    ensure_triangle_importable()
    installed = None
    if spec is not None:
        from coppernicus.engine.meshing import registry
        registry.install_in_worker(spec["name"], spec["settings"], spec["options"])
        installed = registry.installed()
    from kipi.core import workers
    workers._start(*kipi_args)
    _trace(spec, installed, early)


def _trace(spec, installed, early) -> None:
    path = os.environ.get(TRACE_ENV)
    if not path:
        return
    line = {"pid": os.getpid(), "installed": installed,
            "settings": spec["settings"] if spec is not None else None,
            "options": spec["options"] if spec is not None else None, "early": early,
            "view_triangle": _view_triangle()}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _view_triangle():
    """What the 3D view meshes with here (``kipi.core.viz.meshes._triangle``, which KiPI's
    ``_start`` loads in a 3D-view pool's workers): "module:class" of a stand-in, the module's
    name for Triangle itself, None when the view is not loaded or has no Triangle."""
    viz = sys.modules.get("kipi.core.viz.meshes")
    tri = getattr(viz, "_triangle", None)
    if tri is None:
        return None
    if type(tri).__name__ == "module":
        return tri.__name__
    return f"{type(tri).__module__}:{type(tri).__name__}"
