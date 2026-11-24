"""An opened board, read through KiPI's importers (``.kicad_pcb``, ODB++, HyperLynx, snapshots).

The board file is only ever read. Everything the rest of Coppernicus needs to know about
the board (nets, layers, parts and pins, net ties, outline, copper) is asked here.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from kipi.core.geometry.copper import net_layer_copper
from kipi.core.model.board import BoardSnapshot, CopperLayer, Pad
from kipi.core.model.nets import classify_nets
from kipi.core.model.stackup import restack, stack_order
from kipi.sources import registry
from shapely import STRtree
from shapely.geometry import Point, Polygon

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
class PadHit:
    """A pad found under a point on the board (see :meth:`Board.pad_at`)."""
    ref: str
    pin: str
    pad_id: str
    net: str
    position: Tuple[float, float]     # pad centre, mm
    layer_id: int                     # the copper layer of the shape that was hit


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
        self._pad_index: Dict[Optional[int], tuple] = {}

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

    def counts(self) -> Dict[str, int]:
        """How many nets, parts, pads, vias, tracks and zones the board has."""
        s = self._snap
        return {"nets": len(self.nets()), "parts": len(s.footprints), "pads": len(s.pads),
                "vias": len(s.vias), "tracks": len(s.tracks), "zones": len(s.zones)}

    def with_copper_thickness(self, thickness_mm: Dict[str, float]) -> "Board":
        """A what-if copy of this board with some copper layers (by name) set to another
        thickness, the stack rebuilt around them. The board file is not touched."""
        copper = {c.name for c in self._snap.stackup.copper}
        unknown = [n for n in thickness_mm if n not in copper]
        if unknown:
            raise EngineError(f"No copper layer {', '.join(unknown)} on this board "
                              f"(copper layers: {', '.join(c.name for c in self.layers())})")
        stack = self._snap.stackup
        new_mm = [float(thickness_mm.get(e.name, e.thickness_mm)) if isinstance(e, CopperLayer)
                  else e.thickness_mm for e in stack_order(stack)]
        snap = dataclasses.replace(self._snap, stackup=restack(stack, new_mm, edited=True))
        return Board(snap, self.path, self.warnings)

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

    def pad_at(self, x: float, y: float, layer_id: Optional[int] = None,
               tolerance_mm: float = 0.3) -> Optional[PadHit]:
        """The pad under (x, y) mm: among pads whose copper on ``layer_id`` (any layer if
        ``None``) contains the point or lies within ``tolerance_mm`` of it, a pad containing
        the point wins over one that is only near, then the nearest centre. ``None`` if no
        pad is that close. Pads of every net are found; the caller checks the net."""
        tree, entries = self._pad_tree(layer_id)
        if tree is None:
            return None
        point = Point(x, y)
        best, best_key = None, None
        for i in tree.query(point, predicate="dwithin", distance=tolerance_mm):
            pad, lid, geom, order = entries[int(i)]
            cx, cy = pad.position
            key = (geom.distance(point) > 0.0, (cx - x) ** 2 + (cy - y) ** 2, order)
            if best_key is None or key < best_key:
                best, best_key = (pad, lid), key
        if best is None:
            return None
        pad, lid = best
        return PadHit(pad.footprint, pad.number, pad.id, pad.net,
                      (float(pad.position[0]), float(pad.position[1])), lid)

    def pad_rings(self, pad_ids: Sequence[str], layer_id: int) -> List[np.ndarray]:
        """Outline and hole rings, (N, 2) arrays in mm, of the given pads' copper on a
        layer. Unknown pads and pads without copper there are skipped."""
        rings = []
        for pid in pad_ids:
            pad = self._pads_by_id.get(pid)
            for poly in (pad.shapes.get(layer_id, []) if pad is not None else []):
                rings.append(np.asarray(poly.outline, dtype=float))
                rings.extend(np.asarray(h, dtype=float) for h in poly.holes)
        return rings

    def _pad_tree(self, layer_id: Optional[int]):
        """A spatial index of pad copper on one layer (or all), built on first use."""
        if layer_id not in self._pad_index:
            stack = {c.id: c.index for c in self._snap.stackup.copper}
            entries = []
            for pad in self._snap.pads:
                for lid, polys in pad.shapes.items():
                    if layer_id is not None and lid != layer_id:
                        continue
                    for poly in polys:
                        geom = Polygon(poly.outline, poly.holes)
                        if not geom.is_empty:
                            entries.append((pad, lid, geom, stack.get(lid, 0)))
            tree = STRtree([e[2] for e in entries]) if entries else None
            self._pad_index[layer_id] = (tree, entries)
        return self._pad_index[layer_id]

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

    def net_layer_kinds(self, net: str) -> Dict[int, set]:
        """Per copper layer id where ``net`` has copper, what is there: a set of "track",
        "zone", "via" and "pad". Through vias and through-hole pads reach every layer they
        cross, so a layer can hold nothing of the net but their rings."""
        tracks, vias, pads, zones = self._snap.items_on_net(net)
        kinds: Dict[int, set] = {}
        for t in tracks:
            kinds.setdefault(t.layer, set()).add("track")
        for z in zones:
            for lid, polys in z.filled.items():
                if polys:
                    kinds.setdefault(lid, set()).add("zone")
        for v in vias:
            for lid in v.layers:
                kinds.setdefault(lid, set()).add("via")
        for p in pads:
            for lid in p.shapes:
                kinds.setdefault(lid, set()).add("pad")
        order = [c.id for c in self._snap.stackup.copper]
        return {lid: kinds[lid] for lid in order if lid in kinds}


def board_file_filter() -> str:
    """A Qt file-dialog filter for every board format the engine reads."""
    return registry.file_filter()
