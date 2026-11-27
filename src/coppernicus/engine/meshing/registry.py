"""Which meshing backend KiPI uses in this process.

    available() -> names that can run here
    get(name) -> the backend
    install(name) -> KiPI's mesh_polygon goes through the seam with that backend
    uninstall() -> KiPI's own path back, exactly as it was
    installed() -> the installed name, or None
    pooled() -> whether KiPI's worker pools run it too

``install`` refuses (RuntimeError, :func:`seam_problem`) on a KiPI other than the one the seam was
written for (:data:`KIPI_VERSION`), or whose functions below have other signatures: the seam
copies KiPI's ``mesh_polygon`` and calls its helpers, so a mesh there could differ from KiPI's
without a word. It does not fall back to KiPI's own mesher: that would give results under a
mesher name that did not mesh them.

``install`` patches, in this process:
- ``kipi.core.mesh.mesher.mesh_polygon``: every copper mesh goes through it (DC rail and tree
  solves via ``mesh_layer``, AC's ``ac/solver/fem.py`` via ``mesh_many``, noise copper via
  ``mesh_net``);
- KiPI's worker pools (``kipi.core.workers.BoardPool``: the layer pool of
  ``kipi.core.mesh.parallel``, the 3D view's) spawn workers that re-import KiPI and would not see
  that patch:
  - a backend of :data:`KNOWN` (built from its module in a worker, see :mod:`.pool`):
    ``kipi.core.workers._start`` becomes a :class:`~.pool.WorkerStart`, so each worker installs
    the same backend with the same settings before KiPI's own initializer; ``KIPI_SERIAL`` and
    ``mesh_layers`` are left as they are;
  - otherwise (a backend added by :func:`register`: a factory a spawned process cannot rebuild;
    a ``stats`` list in the options, which a worker's records would not reach; options that
    cannot be pickled): ``kipi.core.mesh.parallel.mesh_layers`` (to "no pool") and
    ``KIPI_SERIAL=1``, as one process. So with the pools on, :data:`.pipeline.STATS` records only
    the polygons meshed in this process (each worker keeps its own); a ``stats`` list sees all;
- for a backend that offers ``triangle_compat()``, the two KiPI callers that call Triangle
  directly instead of ``mesh_polygon``, through ``sys.modules["triangle"]``:
  ``kipi.noise.extract.xsection.mesh`` imports ``triangle`` when it runs ("pq28a..." then
  "rpq28a" passes), and ``kipi.core.viz.meshes`` binds it as ``_triangle`` when it loads (the 3D
  view's fast path, "pQ"; without Triangle the view uses GEOS); when the 3D view is already
  loaded, its ``_triangle`` is set too (it is not imported here: it loads pyvista).
  ``kipi.core.mesh.mesher`` keeps the ``triangle`` it imported at load.

Installing twice the same name does nothing; installing another first uninstalls; uninstalling
when nothing is installed does nothing. :func:`uninstall` puts back exactly what install changed
(a ``KIPI_SERIAL`` set before install included) and nothing else.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pickle
import sys
from functools import partial
from typing import Callable, Dict, List, Optional

from coppernicus.engine.meshing import pool
from coppernicus.engine.meshing.protocol import Backend

#: name -> "module:attribute" of a backend class (built with no arguments), imported when needed.
KNOWN: Dict[str, str] = {
    "triangle": "coppernicus.engine.meshing.triangle_backend:TriangleBackend",
    "rust": "coppernicus.engine.meshing.rust_backend:RustBackend",
}
_factories: Dict[str, Callable[[], Backend]] = {}
_state: Dict[str, object] = {}          # the installed name and what install() replaced
_VIZ = "kipi.core.viz.meshes"

#: The KiPI the seam was written for.
KIPI_VERSION = "1.0.1"
#: KiPI's functions the seam replaces or hooks into, with their parameters.
_SEAMS = {
    ("kipi.core.mesh.mesher", "mesh_polygon"): ["poly", "constraints", "max_area", "min_angle", "size_fn",
                                                "warn", "points"],
    ("kipi.core.mesh.parallel", "mesh_layers"): ["snap", "jobs", "opts", "features", "progress", "far"],
    ("kipi.core.workers", "_start"): ["path", "colors"],
    ("kipi.core.workers", "BoardPool.__init__"): ["self", "snap", "n", "colors"],
}
#: What the pipeline and the Triangle backend use of ``kipi.core.mesh.mesher``.
_HELPERS = ("_clip_constraint", "_linework", "pslg_problems", "_pad_nodes", "_steiner_cap", "_hole_seeds",
            "_place", "_triangulate", "_carve_holes", "MeshError", "MeshCapHit", "CONSTRAINT_CLEARANCE_MM",
            "NODING_GRID_MM")
_PLAIN = (str, int, float, bool, type(None))


def register(name: str, factory: Callable[[], Backend]) -> None:
    """Add (or replace) a backend by a factory, e.g. one living outside this package."""
    _factories[name] = factory


def _factory(name: str) -> Callable[[], Backend]:
    if name in _factories:
        return _factories[name]
    if name not in KNOWN:
        raise KeyError(f"no mesher named {name!r}; known: {', '.join(sorted(set(KNOWN) | set(_factories)))}")
    module, attr = KNOWN[name].split(":")
    return getattr(importlib.import_module(module), attr)


def get(name: str) -> Backend:
    """The backend ``name``; raises KeyError for an unknown name, RuntimeError where it cannot run."""
    try:
        backend = _factory(name)()
    except ImportError as e:
        raise RuntimeError(f"mesher {name!r} is not installed here: {e}") from None
    if not backend.available():
        raise RuntimeError(f"mesher {name!r} is not available here")
    return backend


def available() -> List[str]:
    """The names of the backends that can run here."""
    out = []
    for name in sorted(set(KNOWN) | set(_factories)):
        try:
            get(name)
        except (KeyError, RuntimeError):
            continue
        out.append(name)
    return out


def installed() -> Optional[str]:
    return _state.get("name")  # type: ignore[return-value]


def pooled() -> bool:
    """Whether KiPI's worker pools run the installed backend (False when none is installed)."""
    return bool(_state.get("pool"))


def seam_problem() -> Optional[str]:
    """Why the seam can't go into this KiPI, or None. While a backend is installed, KiPI's own
    functions are checked, not the ones install put in their place."""
    import kipi
    version = getattr(kipi, "__version__", None)
    if version != KIPI_VERSION:
        return f"written for KiPI {KIPI_VERSION}, this is KiPI {version}"
    ours = _state.get("ours", {})
    for (module, name), params in _SEAMS.items():
        fn = importlib.import_module(module)
        for part in name.split("."):
            fn = getattr(fn, part, None)
        if (module, name) in ours and fn is ours[module, name][0]:
            fn = ours[module, name][1]
        if not callable(fn):
            return f"KiPI has no {module}.{name}"
        try:
            got = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            got = None
        if got != params:
            return f"{module}.{name} takes ({', '.join(got or ['?'])}), not ({', '.join(params)})"
    from kipi.core import workers
    if "_start" not in workers.BoardPool.__init__.__code__.co_names or workers.SERIAL_ENV != "KIPI_SERIAL":
        return "kipi.core.workers.BoardPool no longer starts its workers with the module's _start"
    import kipi.core.mesh.mesher as M
    missing = [h for h in _HELPERS if not hasattr(M, h)]
    if missing:
        return f"KiPI has no kipi.core.mesh.mesher.{', '.join(missing)}"
    return None


def serial_reason(name: str, options: dict) -> Optional[str]:
    """Why ``name`` with ``options`` must keep KiPI's pools in one process, or None."""
    if name in _factories or name not in KNOWN:
        return "added by register(): a spawned worker cannot rebuild its factory"
    if options.get("stats") is not None:
        return "a stats list: the records of a worker would not reach it"
    try:
        pickle.dumps(options)
    except Exception:  # noqa: BLE001  (anything pickle raises)
        return "options that cannot be pickled"
    return None


def _worker_spec() -> Optional[dict]:
    """What a worker needs to install the installed backend (:mod:`.pool`), read now; None when
    nothing pooled is installed. A setting that is not a plain value raises RuntimeError, which
    KiPI takes as a pool that can't start (it meshes in this process)."""
    if not _state.get("pool"):
        return None
    backend = _state["backend"]
    settings = {k: getattr(backend, k) for k in getattr(backend, "settings", ())}
    bad = sorted(k for k, v in settings.items() if not isinstance(v, _PLAIN))
    if bad:
        raise RuntimeError(f"mesher {_state['name']!r}: setting(s) {', '.join(bad)} are not plain values")
    return {"name": _state["name"], "settings": settings, "options": dict(_state["options"])}


def install(name: str, **options) -> Backend:
    """Route KiPI's meshing through the seam with backend ``name`` (see the module docstring).
    ``options`` go to the pipeline (e.g. ``empty="warn"``). RuntimeError when this KiPI is not the
    one the seam was written for (:func:`seam_problem`) or the backend can't run here."""
    if _state.get("name") == name and _state.get("options") == options:
        return _state["backend"]  # type: ignore[return-value]
    uninstall()
    problem = seam_problem()
    if problem is not None:
        raise RuntimeError(f"mesher {name!r} not installed: {problem}")
    backend = get(name)
    _patch(name, backend, options, "serial" if serial_reason(name, options) else "pool")
    return backend


def install_in_worker(name: str, settings: dict, options: dict) -> Backend:
    """In one of KiPI's pool workers (:mod:`coppernicus.mesh_worker`): backend ``name`` of
    :data:`KNOWN`, with its ``settings`` set, installed as in the process that started the pool.
    The pools are not touched here: a worker starts none."""
    uninstall()
    problem = seam_problem()
    if problem is not None:
        raise RuntimeError(f"mesher {name!r} not installed in a worker: {problem}")
    backend = get(name)
    for key, value in settings.items():
        setattr(backend, key, value)
    _patch(name, backend, options, "worker")
    return backend


def _patch(name: str, backend: Backend, options: dict, mode: str) -> None:
    """``mode``: "pool" (KiPI's workers install it too), "serial" (KiPI's pools off) or "worker"."""
    import kipi.core.mesh.mesher as M
    import kipi.core.mesh.parallel as P

    from coppernicus.engine.meshing import pipeline
    viz = sys.modules.get(_VIZ)                 # not imported here: it loads pyvista
    saved = {"mesh_polygon": M.mesh_polygon, "triangle_module": sys.modules.get("triangle"),
             "viz": viz._triangle if viz is not None else None, "viz_loaded": viz is not None}
    M.mesh_polygon = partial(pipeline.mesh_polygon, backend, options)
    ours = {("kipi.core.mesh.mesher", "mesh_polygon"): (M.mesh_polygon, saved["mesh_polygon"])}
    if mode == "serial":
        saved.update(mesh_layers=P.mesh_layers, serial=os.environ.get("KIPI_SERIAL"))
        P.mesh_layers = lambda *a, **k: None
        os.environ["KIPI_SERIAL"] = "1"
        ours["kipi.core.mesh.parallel", "mesh_layers"] = (P.mesh_layers, saved["mesh_layers"])
    elif mode == "pool":
        saved["start"] = pool.hook(_worker_spec)
        from kipi.core import workers
        ours["kipi.core.workers", "_start"] = (workers._start, saved["start"])
    compat = getattr(backend, "triangle_compat", None)
    shim = compat() if compat is not None else None
    if shim is not None:
        sys.modules["triangle"] = shim          # the noise cross-section, and the 3D view if loaded later
        if viz is not None:
            viz._triangle = shim
    _state.update(name=name, options=dict(options), backend=backend, saved=saved, shim=shim,
                  pool=mode == "pool", ours=ours)


def uninstall() -> None:
    """KiPI's own meshing back, exactly as before :func:`install`."""
    saved = _state.get("saved")
    if not saved:
        return
    import kipi.core.mesh.mesher as M
    import kipi.core.mesh.parallel as P
    M.mesh_polygon = saved["mesh_polygon"]
    if "mesh_layers" in saved:
        P.mesh_layers = saved["mesh_layers"]
        if saved["serial"] is None:
            os.environ.pop("KIPI_SERIAL", None)
        else:
            os.environ["KIPI_SERIAL"] = saved["serial"]
    if "start" in saved:
        pool.unhook(saved["start"])
    if saved["triangle_module"] is None:
        sys.modules.pop("triangle", None)
    else:
        sys.modules["triangle"] = saved["triangle_module"]
    viz = sys.modules.get(_VIZ)
    if viz is not None and _state.get("shim") is not None and viz._triangle is _state["shim"]:
        # Loaded before install: what it had; loaded while installed: the real Triangle module.
        viz._triangle = saved["viz"] if saved["viz_loaded"] else saved["triangle_module"]
    _state.clear()
