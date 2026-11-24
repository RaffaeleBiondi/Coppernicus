"""An opened board, read through KiPI's importers (``.kicad_pcb``, ODB++, HyperLynx, snapshots).

The board file is only ever read. Everything the rest of Coppernicus needs to know about
the board (nets, layers, parts and pins, net ties, outline, copper) is asked here.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
from kipi.core.geometry.copper import net_layer_copper
from kipi.core.model.board import BoardSnapshot, Pad
from kipi.core.model.nets import classify_nets
from kipi.sources import registry

from coppernicus.project import PinSpec


class EngineError(RuntimeError):
    """Something the user can fix: a missing file, a part not on the net, no copper path."""


@dataclass(frozen=True)
class Layer:
    id: int
    name: str
    thickness_mm: float


@dataclass(frozen=True)
class PartPins:
    ref: str
    value: str
    footprint: str
    pins: List[str]


@dataclass(frozen=True)
class NetTie:
    """A net-tie footprint joining two nets. KiCad gives its copper bridge no net, so the
    importer drops it; the solver needs it as an explicit short."""
    ref: str
    pad_a: str
    net_a: str
    pad_b: str
    net_b: str


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: List[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _pin_key(pin: str):
    return (len(pin), pin)


class Board:
    def __init__(self, snapshot: BoardSnapshot, path: Optional[Path] = None,
                 warnings: Sequence[str] = ()):
        self._snap = snapshot
        self.path = Path(path) if path is not None else None
        self.warnings: List[str] = list(dict.fromkeys([*warnings, *snapshot.assumed]))
        self._pads_by_id: Dict[str, Pad] = {p.id: p for p in snapshot.pads}
        self._kinds: Optional[Dict[str, str]] = None

    @classmethod
    def open(cls, path) -> "Board":
        path = Path(path)
        if not path.exists():
            raise EngineError(f"No such file: {path}")
        collect = _Collect()
        logger = logging.getLogger("kipi")
        logger.addHandler(collect)
        try:
            snap = registry.open_board(path).load()
        except ValueError as e:
            raise EngineError(str(e)) from e
        finally:
            logger.removeHandler(collect)
        return cls(snap, path, collect.messages)

    # ------------------------------------------------------------------ basics
    @property
    def snapshot(self) -> BoardSnapshot:
        """The engine's own board model. Only :mod:`coppernicus.engine` should touch it."""
        return self._snap

    @property
    def name(self) -> str:
        return self.path.name if self.path else self._snap.board_name

    def layers(self) -> List[Layer]:
        return [Layer(c.id, c.name, c.thickness_mm) for c in self._snap.stackup.copper]

    def layer_name(self, layer_id: int) -> str:
        return self._snap.stackup.layer(layer_id).name

    def nets(self) -> List[str]:
        return sorted(self._snap.net_names(), key=str.casefold)

    def net_kind(self, net: str) -> str:
        """'power', 'ground' or 'signal' — a name and zone heuristic, not a certainty."""
        if self._kinds is None:
            self._kinds = classify_nets(self._snap)
        return self._kinds.get(net, "signal")

    def power_nets(self) -> List[str]:
        return [n for n in self.nets() if self.net_kind(n) in ("power", "ground")]

    # ------------------------------------------------------------------ parts and pins
    def parts_on_net(self, net: str) -> List[PartPins]:
        _, _, pads, _ = self._snap.items_on_net(net)
        by_ref: Dict[str, List[str]] = {}
        for p in pads:
            if p.footprint:
                by_ref.setdefault(p.footprint, []).append(p.number)
        fps = {f.reference: f for f in self._snap.footprints}
        out = []
        for ref in sorted(by_ref, key=lambda r: (r.rstrip("0123456789"), _pin_key(r))):
            f = fps.get(ref)
            out.append(PartPins(ref, f.value if f else "", f.footprint if f else "",
                                sorted(set(by_ref[ref]), key=_pin_key)))
        return out

    def pads(self, nets, ref: str, pins: PinSpec = None) -> List[Pad]:
        """The pads of part ``ref`` on ``nets`` (one net name or several): all of them, or
        those whose number matches one of ``pins`` (exact or glob). Raises
        :class:`EngineError` saying what *is* there."""
        nets = [nets] if isinstance(nets, str) else list(nets)
        net = " / ".join(nets)
        mine = [p for n in nets for p in self._snap.items_on_net(n)[2] if p.footprint == ref]
        if not mine:
            raise EngineError(f"{ref} has no pads on net {net}")
        if pins is not None:
            available = sorted({p.number for p in mine}, key=_pin_key)
            chosen = []
            for pattern in pins:
                hit = [p for p in mine if fnmatch.fnmatchcase(p.number, pattern)]
                if not hit:
                    raise EngineError(f"{ref} has no pin {pattern!r} on net {net} "
                                      f"(pins on this net: {', '.join(available)})")
                chosen.extend(hit)
            mine = list({id(p): p for p in chosen}.values())
        # A pin split into several pads: keep only the pads that have copper on some layer.
        shaped = {p.number for p in mine if p.shapes}
        mine = [p for p in mine if p.shapes or p.number not in shaped]
        return sorted(mine, key=lambda p: _pin_key(p.number))

    def pad(self, pad_id: str) -> Pad:
        return self._pads_by_id[pad_id]

    # ------------------------------------------------------------------ net ties
    def net_ties(self) -> List[NetTie]:
        """Footprints recognised as net ties: library id or value containing ``nettie`` /
        ``net_tie``, with pads on at least two nets. One tie per pair of nets it joins."""
        ties = []
        for f in self._snap.footprints:
            tag = f"{f.footprint} {f.value}".lower().replace("-", "").replace("_", "")
            if "nettie" not in tag:
                continue
            first_pad_of_net: Dict[str, Pad] = {}
            for pid in f.pad_ids:
                p = self._pads_by_id.get(pid)
                if p is not None and p.net and p.net not in first_pad_of_net:
                    first_pad_of_net[p.net] = p
            pads = list(first_pad_of_net.values())
            for a, b in zip(pads, pads[1:]):
                ties.append(NetTie(f.reference, a.id, a.net, b.id, b.net))
        return ties

    # ------------------------------------------------------------------ geometry
    def outline(self) -> List[np.ndarray]:
        """Board outline rings, each an (N, 2) array in mm."""
        rings = []
        for poly in self._snap.outline:
            rings.append(np.asarray(poly.outline, dtype=float))
            rings.extend(np.asarray(h, dtype=float) for h in poly.holes)
        return rings

    def copper(self, net: str, layer_id: int):
        """The copper of ``net`` on a layer, as a shapely geometry (drills cut out)."""
        return net_layer_copper(self._snap, net, layer_id)

    def net_layers(self, net: str) -> List[int]:
        """Copper layer ids where ``net`` has copper, in stack order."""
        tracks, vias, pads, zones = self._snap.items_on_net(net)
        used = {t.layer for t in tracks}
        for v in vias:
            used.update(v.layers)
        for p in pads:
            used.update(p.shapes)
        for z in zones:
            used.update(lid for lid, polys in z.filled.items() if polys)
        return [c.id for c in self._snap.stackup.copper if c.id in used]


def board_file_filter() -> str:
    """A Qt file-dialog filter for every board format the engine reads."""
    return registry.file_filter()
