"""KiPI's worker pools on the installed mesher.

KiPI's :class:`kipi.core.workers.BoardPool` (the layer pool of ``kipi.core.mesh.parallel``, and
the 3D view's) starts spawned processes with ``initializer=_start``. It looks ``_start`` up in
``kipi.core.workers`` each time a pool is built. While a mesher that a worker can rebuild is
installed, :func:`hook` puts a :class:`WorkerStart` there:

- In this process it calls KiPI's ``_start``.
- Pickled for a worker, it becomes ``functools.partial(coppernicus.mesh_worker.start, spec)``.
  The worker installs the mesher first, then runs KiPI's own ``_start(path, colors)``.

``spec`` is read when the worker is spawned, not at install: the installed name, the backend's
settings (the attributes it names in ``settings``, see :mod:`.protocol`, read from the live
backend, so a setting changed after :func:`~.registry.install` reaches the workers too) and the
pipeline options.
"""

from __future__ import annotations

import functools
from typing import Callable, Optional


class WorkerStart:
    """Stands in for ``kipi.core.workers._start`` while a mesher is installed (see the module
    docstring). ``spec`` returns what a worker needs, or None when nothing is installed."""

    def __init__(self, kipi_start: Callable, spec: Callable[[], Optional[dict]]):
        self.kipi_start = kipi_start
        self.spec = spec

    def __call__(self, *args):
        return self.kipi_start(*args)

    def __reduce__(self):
        from coppernicus import mesh_worker
        return functools.partial, (mesh_worker.start, self.spec())


def hook(spec: Callable[[], Optional[dict]]) -> Callable:
    """Put a :class:`WorkerStart` in ``kipi.core.workers._start``; returns what was there."""
    from kipi.core import workers
    saved = workers._start
    workers._start = WorkerStart(saved, spec)
    return saved


def unhook(saved: Callable) -> None:
    """Put back what :func:`hook` returned."""
    from kipi.core import workers
    workers._start = saved
