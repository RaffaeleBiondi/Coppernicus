"""Which mesher a solve uses, chosen per call and safe across threads.

    resolve(explicit, project, app) -> the name: explicit > project > app > DEFAULT
    check(name)                     -> MesherUnavailable (message, hint) when it cannot run here
    using(name, **options)          -> a context: the solve inside meshes with ``name``
    describe(name)                  -> {"name", "version", "experimental"}, recorded in results
    stamp(doc, name)                -> a solve's document with its mesher (and note) recorded

The installed mesher is global to the process: :func:`registry.install` patches KiPI's module
functions. Every solve therefore runs inside :func:`using`, which holds one process-wide lock
for the whole block (meshing, estimates, the solve), so two calls that choose different meshers
(MCP tools run in worker threads) take turns instead of meshing with each other's backend.

The default, Triangle, is KiPI's own meshing: :func:`using` leaves the registry uninstalled for
it (and uninstalls, for the block, a mesher something else installed), exactly today's code
path. Any other name is installed for the block. On leaving, the state found on entering is put
back: the name :func:`registry.installed` gave, installed again with the options this module
installed it with (a mesher installed outside :func:`using` comes back with its default options).

Only the registry's public functions are used.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from importlib import metadata
from typing import Any, Dict, Iterator, List, Optional, Tuple

from coppernicus.engine.meshing import registry

DEFAULT = "triangle"

#: What to do to get a mesher that is not available here.
HINTS = {
    "triangle": ("Triangle comes with KiPI in a normal install (uv sync, pip install); the packaged "
                 "app downloads it on first start"),
    "rust": ("the Rust mesher (coppermesh) is experimental and not built here: build it as "
             "tools/mesher_rs/README.md says (maturin develop --release), or use the default, triangle"),
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


def resolve(explicit: Optional[str] = None, project: Optional[str] = None,
            app: Optional[str] = None) -> str:
    """The mesher of a solve: the explicit choice (CLI ``--mesher``, the MCP ``mesher``
    argument), else the project's ``settings.mesher``, else the app's setting, else
    :data:`DEFAULT`. Names are lower-cased; empty means not set."""
    for name in (explicit, project, app):
        if name is not None and str(name).strip():
            return str(name).strip().lower()
    return DEFAULT


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


def describe(name: str) -> Dict[str, Any]:
    """What a result records of its mesher."""
    return {"name": name, "version": version(name), "experimental": name != DEFAULT}


def note(name: str) -> Optional[str]:
    """The assumption a result made with a non-default mesher carries (none for the default)."""
    if name == DEFAULT:
        return None
    v = version(name)
    return (f"Meshed with the experimental {name} mesher{f' ({v})' if v else ''}, not the default "
            f"{DEFAULT}: small differences from a {DEFAULT} result are the mesh, not the design.")


def stamp(doc: Dict[str, Any], name: str) -> Dict[str, Any]:
    """A solve's document records its mesher (``doc["mesher"]``, :func:`describe`); for a
    non-default one, its assumptions also carry :func:`note`."""
    doc["mesher"] = describe(name)
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
    """Mesh the block with ``name`` (default :data:`DEFAULT`), holding the process-wide lock;
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
            if name == DEFAULT:
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
