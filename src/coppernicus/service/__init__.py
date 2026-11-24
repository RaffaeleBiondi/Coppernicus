"""The service layer: Coppernicus as plain-data functions over a :class:`Session`.

It sits between the front ends (CLI ``--json``, the MCP server, later the GUI) and the
engine, project, results and problems modules. It never imports KiPI. Every public
function takes and returns JSON-serialisable data, with units in every key name, and
raises :class:`ServiceError` (``code``, ``message``, ``hint``) for anything the caller can fix.
"""

from coppernicus.service.errors import ServiceError
from coppernicus.service.session import Session

__all__ = ["Session", "ServiceError"]
