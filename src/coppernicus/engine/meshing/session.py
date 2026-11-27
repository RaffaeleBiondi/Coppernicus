"""Which mesher a solve uses, chosen per call and safe across threads.

    choose(explicit, project, app)  -> (name, fallback warning or None): explicit > project > app >
                                       DEFAULT (FALLBACK, with the warning, where DEFAULT cannot run)
    resolve(explicit, project, app) -> the name alone
    check(name)                     -> MesherUnavailable (message, hint) when it cannot run here
    using(name, **options)          -> a context: the solve inside meshes with ``name``
    describe(name, fallback)        -> {"name", "version", "experimental", "default"}, recorded in results
    stamp(doc, name, fallback)      -> a solve's document with its mesher (and note or warning) recorded

The installed mesher is global to the process: :func:`registry.install` patches KiPI's module
functions. Every solve therefore runs inside :func:`using`, which holds one process-wide lock
for the whole block (meshing, estimates, the solve), so two calls that choose different meshers
(MCP tools run in worker threads) take turns instead of meshing with each other's backend.

The default is :data:`DEFAULT`, the rust mesher (coppermesh): :func:`using` installs it for the
block. Triangle (:data:`KIPI_OWN`) is KiPI's own meshing: :func:`using` leaves the registry
uninstalled for it (and uninstalls, for the block, a mesher something else installed), exactly
KiPI's code path. Any other name is installed for the block. On leaving, the state found on
entering is put back: the name :func:`registry.installed` gave, installed again with the options
this module installed it with (a mesher installed outside :func:`using` comes back with its
default options).

Where nothing chose the mesher and the rust mesher cannot run here (a plain ``uv sync`` without
``--group rust``, a pip install from git), :func:`choose` gives :data:`FALLBACK` (Triangle) with
:data:`FALLBACK_WARNING`, which the result carries: a default solve never fails for want of
coppermesh. A mesher named anywhere (the call, the project, the app) is never replaced: one that
cannot run is a :class:`MesherUnavailable`.

Only the registry's public functions are used.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from importlib import metadata
from typing import Any, Dict, Iterator, List, Optional, Tuple

from coppernicus import triangle_standin
from coppernicus.engine.meshing import registry

#: The mesher of a solve that names none.
DEFAULT = "rust"
#: KiPI's own meshing (Triangle): :func:`using` leaves the registry uninstalled for it.
KIPI_OWN = "triangle"
#: What a solve that names no mesher meshes with where :data:`DEFAULT` cannot run here.
FALLBACK = KIPI_OWN
#: What a result that records no mesher (written before the mesher was recorded) was meshed with.
RECORDED_BEFORE = KIPI_OWN
#: The warning of a result meshed with :data:`FALLBACK` because :data:`DEFAULT` cannot run here.
FALLBACK_WARNING = ("The rust mesher, the default, is not installed here; meshed with Triangle "
                    "(KiPI's own mesher) instead. Install it (uv sync --group rust, or "
                    "tools/mesher_rs/README.md) for the default, or choose the mesher: triangle "
                    "to mesh with Triangle without this warning.")

#: What to do to get a mesher that is not available here.
HINTS = {
    "triangle": triangle_standin.HINT,
    "rust": ("the rust mesher (coppermesh), the default, is not installed here: install it with "
             "uv sync --group rust, or build it as tools/mesher_rs/README.md says (maturin develop "
             "--release); or choose the mesher triangle"),
}
#: The distribution that gives each backend's version.
_DISTRIBUTIONS = {"triangle": "triangle", "rust": "coppermesh"}

_lock = threading.RLock()
_stack: List[Tuple[str, Dict[str, Any]]] = []      # (name, options) installed by using(), innermost last


class MesherUnavailable(Exception):
    """A mesher that is unknown or cannot run here: a message and a hint."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.message, self.hint = message, hint


def choose(explicit: Optional[str] = None, project: Optional[str] = None,
           app: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """The mesher of a solve and, when it is the fallback, the warning its result carries: the
    explicit choice (CLI ``--mesher``, the MCP ``mesher`` argument), else the project's
    ``settings.mesher``, else the app's setting, else :data:`DEFAULT`; when none is set and
    :data:`DEFAULT` cannot run here but :data:`FALLBACK` can, (:data:`FALLBACK`,
    :data:`FALLBACK_WARNING`). Names are lower-cased; empty means not set. A name that is set
    is returned as it is, whether or not it can run (:func:`check` says)."""
    for name in (explicit, project, app):
        if name is not None and str(name).strip():
            return str(name).strip().lower(), None
    if not _runs(DEFAULT) and _runs(FALLBACK):
        return FALLBACK, FALLBACK_WARNING
    return DEFAULT, None


def resolve(explicit: Optional[str] = None, project: Optional[str] = None,
            app: Optional[str] = None) -> str:
    """The name :func:`choose` gives."""
    return choose(explicit, project, app)[0]


def _runs(name: str) -> bool:
    try:
        check(name)
    except MesherUnavailable:
        return False
    return True


def check(name: str) -> None:
    """Raise :class:`MesherUnavailable` when ``name`` is unknown or cannot run here."""
    try:
        registry.get(name)
    except KeyError:
        known = ", ".join(sorted(set(HINTS) | set(registry.available())))
        raise MesherUnavailable(f"no mesher named {name!r}", f"the meshers are {known} (default "
                                f"{DEFAULT}; available here: {', '.join(registry.available()) or 'none'})"
                                ) from None
    except RuntimeError:
        raise MesherUnavailable(f"the mesher {name!r} is not available here",
                                HINTS.get(name, f"available here: {', '.join(registry.available())}")
                                ) from None


def version(name: str) -> Optional[str]:
    """The installed version of the backend's library, when known."""
    dist = _DISTRIBUTIONS.get(name)
    if dist is None:
        return None
    try:
        return f"{dist} {metadata.version(dist)}"
    except metadata.PackageNotFoundError:
        return None


def describe(name: str, fallback: Optional[str] = None) -> Dict[str, Any]:
    """What a result records of its mesher: its name and version, whether it is experimental
    (neither the rust mesher nor Triangle: a backend added with ``registry.register``) and
    whether it is the default; with ``fallback`` (:func:`choose`'s warning), ``fallback_from``,
    the default it stands in for."""
    out = {"name": name, "version": version(name), "experimental": name not in (DEFAULT, KIPI_OWN),
           "default": name == DEFAULT}
    if fallback:
        out["fallback_from"] = DEFAULT
    return out


def note(name: str) -> Optional[str]:
    """The assumption a result made with a mesher other than the default carries (none for the
    default)."""
    if name == DEFAULT:
        return None
    v = version(name)
    which = "Triangle (KiPI's own mesher" + (f", {v})" if v else ")") if name == KIPI_OWN else (
        f"the experimental {name} mesher{f' ({v})' if v else ''}")
    return (f"Meshed with {which}, not the default {DEFAULT} mesher: small differences from a "
            f"{DEFAULT} result are the mesh, not the design.")


def stamp(doc: Dict[str, Any], name: str, fallback: Optional[str] = None) -> Dict[str, Any]:
    """A solve's document records its mesher (``doc["mesher"]``, :func:`describe`); with
    ``fallback`` (:func:`choose`'s warning), its warnings carry it; else, for a mesher other
    than the default, its assumptions carry :func:`note`."""
    doc["mesher"] = describe(name, fallback)
    if fallback:
        warnings = doc.setdefault("warnings", [])
        if fallback not in warnings:
            warnings.append(fallback)
        return doc
    text = note(name)
    if text:
        doc.setdefault("assumptions", []).append(text)
    return doc


def current() -> Optional[str]:
    """The mesher of the innermost :func:`using` block this process is in (None outside one).
    Meaningful only inside a block: another thread waits for the lock meanwhile."""
    return _stack[-1][0] if _stack else None


@contextmanager
def using(name: Optional[str] = None, **options) -> Iterator[Dict[str, Any]]:
    """Mesh the block with ``name`` (default: :func:`resolve`'s), holding the process-wide lock;
    yields :func:`describe`. Raises :class:`MesherUnavailable` before anything is changed when
    the mesher cannot run here. Nested blocks in one thread are allowed (the lock is
    re-entrant); each puts back what it found."""
    name = resolve(name)
    with _lock:
        check(name)
        before = registry.installed()
        before_options = _stack[-1][1] if _stack and _stack[-1][0] == before else {}
        _stack.append((name, dict(options)))
        try:
            if name == KIPI_OWN:
                if before is not None:
                    registry.uninstall()
            else:
                registry.install(name, **options)
            yield describe(name)
        finally:
            _stack.pop()
            if before is None:
                registry.uninstall()
            else:
                registry.install(before, **before_options)
