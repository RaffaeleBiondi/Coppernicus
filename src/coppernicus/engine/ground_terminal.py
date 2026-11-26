"""A source's ground terminal on several parts in KiPI's power tree (``extra_ground_pins``).

KiPI holds each ground at 0 V at the ground pins of its source VRMs: one voltage source at 0 V
per source, on the pads of its ``ground`` pin reference (``kipi/dc/pi/tree.py``,
``_TreeSolver._ground_system``), and a pin reference names one part. A battery whose negative
pole is several cell holders in parallel has one ground terminal on several parts. Its twin on
the + side, ``extra_pins``, is a voltage source on the union of the parts' pads in the rail
solve (``engine.solve.source_pad_ids``) and one KiPI mesh-sense point per part in the tree:
every pad of the terminal held at the source voltage. KiPI has no such thing for a ground, so
the source's ``ground`` becomes a :class:`PartsPinRef`, a KiPI ``PinRef`` that carries the
other parts, and the ground's 0 V source sits on the union of every listed part's pads: each
held at 0 V, the copper setting how the return splits between them. 0 V is then all of those
pads, as the source voltage is all of the + terminal's.

The seam: KiPI's power tree turns a pin reference into pad ids in one place, its ``_Pads``
(``kipi/dc/pi/tree.py``: ``__call__(self, ref)``, which calls ``pads_for`` with one part).
:func:`require` replaces ``_Pads.__call__``, once, with :func:`_pad_ids`: a
:class:`PartsPinRef` gives the pads of each of its parts (each by KiPI's own function), any other
pin reference exactly KiPI's. It is installed only when a setup holds a :class:`PartsPinRef`
(``engine.pisetup.to_pisetup``), so a project without ``extra_ground_pins`` never reaches it,
and only on the KiPI it was written for (:data:`KIPI_VERSION`, ``_Pads.__call__`` of the expected
parameters). Elsewhere :func:`require` raises :class:`EngineError`: a ground on several parts is
never solved on its first part alone. KiPI's other readers of a VRM's ground (the AC and noise
setups, the electro-thermal heat) take only its net, so they are unchanged.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import kipi
import kipi.dc.pi.tree as tree
from kipi.dc.pi.model import PinRef

from coppernicus.engine.board import EngineError

KIPI_VERSION = "1.0.1"
#: The parameters of KiPI's ``_Pads.__call__`` that :func:`_pad_ids` stands in for.
PARAMETERS = ["self", "ref"]


def _current():
    """The function ``kipi.dc.pi.tree._Pads`` resolves a pin reference with now (None: none)."""
    pads = getattr(tree, "_Pads", None)
    return vars(pads).get("__call__") if isinstance(pads, type) else None


#: KiPI's own ``_Pads.__call__``, as the module had it when this module was imported.
KIPI = _current()

_lock = threading.Lock()


@dataclass
class PartsPinRef(PinRef):
    """The pads on ``net`` of several parts: ``ref``'s (``pins``; empty: all of them on the net),
    then each of ``more``'s, each a pin reference on the same net."""
    more: List[PinRef] = field(default_factory=list)

    def parts(self) -> List[str]:
        return [self.ref] + [p.ref for p in self.more]

    def label(self) -> str:
        return ", ".join([PinRef.label(self)] + [p.label() for p in self.more])


def _pad_ids(self, ref: PinRef) -> List[str]:
    """KiPI's pad ids of ``ref``; of a :class:`PartsPinRef`, those of each of its parts."""
    ids = KIPI(self, ref)
    for other in getattr(ref, "more", None) or ():
        ids += [pid for pid in KIPI(self, other) if pid not in ids]
    return ids


def installed() -> bool:
    """Whether KiPI's power tree resolves pin references with :func:`_pad_ids` now."""
    return _current() is _pad_ids


def seam_problem() -> Optional[str]:
    """Why :func:`_pad_ids` cannot stand in for KiPI's ``_Pads.__call__`` here, or None."""
    version = getattr(kipi, "__version__", None)
    if version != KIPI_VERSION:
        return f"written for KiPI {KIPI_VERSION}, this is KiPI {version}"
    current = _current()
    if KIPI is None or current is None:
        return "KiPI has no kipi.dc.pi.tree._Pads"
    if current is not KIPI and current is not _pad_ids:
        return "kipi.dc.pi.tree._Pads.__call__ was replaced by something else"
    try:
        got = list(inspect.signature(KIPI).parameters)
    except (TypeError, ValueError):
        got = None
    if got != PARAMETERS:
        return (f"kipi.dc.pi.tree._Pads.__call__ takes ({', '.join(got or ['?'])}), "
                f"not ({', '.join(PARAMETERS)})")
    return None


def require(owner: str) -> None:
    """Install :func:`_pad_ids` (once) for ``owner``'s ground terminal on several parts; raises
    :class:`EngineError` naming ``owner`` when this KiPI cannot take it."""
    with _lock:
        if installed():
            return
        problem = seam_problem()
        if problem is not None:
            raise EngineError(f"{owner}: a ground terminal on several parts (extra_ground_pins) "
                              f"cannot be solved with this engine ({problem}); give the ground "
                              "pins on one part")
        tree._Pads.__call__ = _pad_ids


def uninstall() -> None:
    """Put KiPI's own ``_Pads.__call__`` back."""
    with _lock:
        if installed():
            tree._Pads.__call__ = KIPI
