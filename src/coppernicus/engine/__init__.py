"""The solver engine adapter: the only package in Coppernicus that imports KiPI.

Everything else talks to :class:`Board` (an opened board file) and :func:`solve_rail`
(one DC solve), and reads plain :mod:`coppernicus.results`. Replacing the engine means
rewriting this package only.
"""

from coppernicus.engine import fast_trim, superlu_order
from coppernicus.engine.board import Board, EngineError, Layer, NetTie, PadHit, PartPins
from coppernicus.engine.solve import Cancelled, solve_rail, tied_nets

superlu_order.configure_from_env()       # thermal, dc by default; COPPERNICUS_SUPERLU_ORDER=off: KiPI's
fast_trim.configure_from_env()           # on by default, bit-identical; COPPERNICUS_FAST_TRIM=0: KiPI's own

__all__ = ["Board", "EngineError", "Layer", "NetTie", "PadHit", "PartPins", "Cancelled", "solve_rail",
           "tied_nets"]
