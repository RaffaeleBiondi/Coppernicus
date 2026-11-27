"""Meshing backends behind one seam (experimental): KiPI's pre-processing and contract
in :mod:`.pipeline`, the backend contract in :mod:`.protocol`, the choice in :mod:`.registry`."""

from coppernicus.engine.meshing.protocol import (
    STATUS_CAP,
    STATUS_GRADED_CAP,
    STATUS_NO_DOMAIN,
    STATUS_OK,
    Backend,
    SizeField,
    Triangulation,
    graded_cap,
    graded_limits,
)
from coppernicus.engine.meshing.registry import (
    available,
    get,
    install,
    installed,
    register,
    seam_problem,
    uninstall,
)

__all__ = ["Backend", "SizeField", "Triangulation", "graded_cap", "graded_limits", "STATUS_OK", "STATUS_CAP",
           "STATUS_NO_DOMAIN", "STATUS_GRADED_CAP", "available", "get", "install", "installed", "register",
           "seam_problem", "uninstall"]
