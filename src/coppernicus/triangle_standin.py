"""A stand-in for the ``triangle`` package, so that Coppernicus and KiPI load without Triangle.

KiPI imports ``triangle`` in three places: ``kipi.core.mesh.mesher`` when it loads (every
copper mesh), ``kipi.core.viz.meshes`` when it loads (as ``_triangle``, the 3D view's fast
path), and ``kipi.noise.extract.xsection`` when it runs (the noise cross-section). Without
Triangle the first one stops KiPI from loading at all.

:func:`ensure_triangle_importable` puts a stand-in module into ``sys.modules["triangle"]``
when, and only when, the real ``import triangle`` fails. The stand-in has KiPI's one call,
``triangulate(data, switches)``, and that call raises :class:`TriangleNotInstalled`:

- with a backend of the mesher seam installed (``coppernicus.engine.meshing.install``), KiPI's
  meshing goes through that backend, and the rust backend's own stand-in replaces this one in
  ``sys.modules`` for the two direct callers: nothing reaches this module;
- with KiPI's own path (no backend installed), a solve stops with that one clear error instead
  of failing to import; the 3D view catches it and triangulates with GEOS, as KiPI does
  without Triangle.

The stand-in carries :data:`MARKER` (``__coppernicus_standin__ = True``): the "triangle" backend
of the seam reads it to say it is not available.

:func:`use_real_triangle` swaps the real Triangle in when it becomes importable while the
process runs (the packaged app downloads it when the user chooses the triangle mesher).

Only the standard library is imported here: this runs before anything imports KiPI (the
engine package, the packaged app's entry point, a pool worker's initializer).
"""

from __future__ import annotations

import importlib
import importlib.machinery
import sys
import types

NAME = "triangle"
#: The stand-in's marker attribute.
MARKER = "__coppernicus_standin__"
#: The modules of KiPI that bind ``triangle`` when they load, and the name they bind it under.
_BOUND = (("kipi.core.mesh.mesher", "triangle"), ("kipi.core.viz.meshes", "_triangle"))

MESSAGE = "Triangle is not installed"
HINT = ("choose the rust mesher, or download Triangle: in the packaged app start Coppernicus "
        "with --fetch-triangle (or choose the triangle mesher); in a Python install run "
        "'pip install triangle'")


class TriangleNotInstalled(RuntimeError):
    """KiPI called Triangle and only the stand-in is here. ``code``, ``message`` and ``hint`` as
    in Coppernicus's other structured errors."""

    code = "triangle_unavailable"

    def __init__(self, message: str = MESSAGE, hint: str = HINT):
        super().__init__(message)
        self.message, self.hint = message, hint

    def __reduce__(self):                # pickled back from a pool worker with its hint
        return type(self), (self.message, self.hint)

    def __str__(self) -> str:
        return f"{self.message}: {self.hint}"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def _triangulate(data, switches: str = ""):
    raise TriangleNotInstalled()


def _make() -> types.ModuleType:
    module = types.ModuleType(NAME, "Coppernicus's stand-in for Triangle, which is not installed.")
    module.__spec__ = importlib.machinery.ModuleSpec(NAME, None, origin="coppernicus stand-in")
    module.triangulate = _triangulate
    setattr(module, MARKER, True)
    return module


STANDIN = _make()


def is_standin(module) -> bool:
    return bool(getattr(module, MARKER, False))


def ensure_triangle_importable() -> bool:
    """Make ``import triangle`` work: the real package when it imports, else the stand-in.
    Returns whether the stand-in is in place. Idempotent; whatever already is in
    ``sys.modules["triangle"]`` (the real one, the stand-in, a seam backend's) is kept."""
    current = sys.modules.get(NAME)
    if current is not None:
        return is_standin(current)
    try:
        importlib.import_module(NAME)
    except ImportError:
        sys.modules[NAME] = STANDIN
        return True
    return False


def standin_in_use() -> bool:
    """Whether KiPI's mesher has (or would get) the stand-in instead of Triangle."""
    mesher = sys.modules.get(_BOUND[0][0])
    if mesher is not None:
        return is_standin(getattr(mesher, _BOUND[0][1], None))
    return is_standin(sys.modules.get(NAME))


def use_real_triangle() -> bool:
    """Replace the stand-in by the real Triangle when it is now importable (downloaded and put on
    ``sys.path`` after KiPI loaded): in ``sys.modules`` and where KiPI's modules bound it.
    Returns whether the real Triangle is what KiPI's mesher uses. Call it with no seam backend
    installed (``coppernicus.engine.meshing.uninstall()`` first): while one is, its own stand-in
    holds ``sys.modules["triangle"]`` and nothing is changed (False)."""
    if not standin_in_use():
        return True
    held = sys.modules.get(NAME)
    if held is not None and not is_standin(held):
        if not isinstance(held, types.ModuleType):       # a seam backend's stand-in
            return False
        real = held
    else:
        sys.modules.pop(NAME, None)
        try:
            real = importlib.import_module(NAME)
        except ImportError:
            sys.modules[NAME] = STANDIN
            return False
    for module_name, attr in _BOUND:
        module = sys.modules.get(module_name)
        if module is not None and is_standin(getattr(module, attr, None)):
            setattr(module, attr, real)
    return True
