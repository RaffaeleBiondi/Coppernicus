"""Synthetic boards built in Python, with dimensions chosen so results have closed-form
answers. Used by the tests and the committed examples instead of real board files.

All dimensions in mm. Builder modelled on KiPI's ``examples/validation/make_boards.py``.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

from shapely.geometry import MultiPolygon, Point, box

from kipi.core.model.board import (
    BoardSnapshot,
    CopperLayer,
    DielectricLayer,
    Footprint,
    Net,
    Pad,
    Poly,
    Stackup,
    Track,
    Via,
    Zone,
)

F, B = 3, 34                    # KiCad layer ids of F.Cu and B.Cu
OZ1 = 0.035                     # 1 oz copper


def two_layer_stackup(t_cu: float = OZ1, core: float = 1.5, er: float = 4.5) -> Stackup:
    total = 2 * t_cu + core
    copper = [CopperLayer(F, "F.Cu", 0, t_cu, total - t_cu), CopperLayer(B, "B.Cu", 1, t_cu, 0.0)]
    return Stackup(copper, [DielectricLayer(core, "FR4", er, F, B)], total)


def _polys(geom) -> List[Poly]:
    geoms = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return [Poly(list(g.exterior.coords)[:-1], [list(r.coords)[:-1] for r in g.interiors])
            for g in geoms if not g.is_empty]


class SyntheticBoard:
    def __init__(self, name: str, stackup: Stackup):
        self.name, self.stackup = name, stackup
        self.pads: List[Pad] = []
        self.vias: List[Via] = []
        self.tracks: List[Track] = []
        self.zones: List[Zone] = []
        self.footprints: Dict[str, Footprint] = {}
        self.nets: List[str] = []

    def _net(self, net: str):
        if net not in self.nets:
            self.nets.append(net)

    def footprint(self, ref: str, value: str = "", lib_id: str = "") -> Footprint:
        fp = self.footprints.get(ref)
        if fp is None:
            fp = self.footprints[ref] = Footprint(f"fp_{ref}", ref, value, (0.0, 0.0), "top")
            fp.footprint = lib_id
        return fp

    def pad(self, ref: str, number: str, net: str, geom, layers: Sequence[int] = (F,),
            drill: float = 0.0, value: str = "", lib_id: str = ""):
        pid = f"{ref}_{number}".lower()
        c = geom.centroid
        self.pads.append(Pad(pid, net, ref, number, (c.x, c.y), "pth" if drill else "smd",
                             (drill, drill), {lid: _polys(geom) for lid in layers}))
        fp = self.footprint(ref, value, lib_id)
        fp.position = (c.x, c.y)
        fp.pad_ids.append(pid)
        self._net(net)

    def square_pad(self, ref: str, number: str, net: str, x: float, y: float, side: float,
                   layers: Sequence[int] = (F,), **kw):
        h = side / 2
        self.pad(ref, number, net, box(x - h, y - h, x + h, y + h), layers, **kw)

    def via(self, net: str, x: float, y: float, drill: float = 0.3, dia: float = 0.6,
            layers: Sequence[int] = (F, B)):
        self.vias.append(Via(f"v{len(self.vias)}", net, (x, y), drill, dia, list(layers)))
        self._net(net)

    def track(self, net: str, layer: int, width: float, start, end):
        self.tracks.append(Track(net, layer, width, tuple(start), tuple(end)))
        self._net(net)

    def zone(self, net: str, layer: int, geom):
        self.zones.append(Zone(f"z{len(self.zones)}_{net}", net, "", 0, {layer: _polys(geom)}))
        self._net(net)

    def snapshot(self, outline=None) -> BoardSnapshot:
        nets = [Net(0, "")] + [Net(i, n) for i, n in enumerate(self.nets, start=1)]
        if outline is None:
            x0, y0, x1, y1 = self._extent()
            outline = box(x0, y0, x1, y1)
        return BoardSnapshot(f"{self.name}.kicad_pcb", "", self.stackup, nets, self.tracks,
                             self.vias, self.pads, list(self.footprints.values()), self.zones,
                             _polys(outline), source=f"coppernicus synthetic: {self.name}")

    def _extent(self):
        xs, ys = [], []
        for p in self.pads:
            for ps in p.shapes.values():
                for q in ps:
                    xs += [v[0] for v in q.outline]
                    ys += [v[1] for v in q.outline]
        for z in self.zones:
            for ps in z.filled.values():
                for q in ps:
                    xs += [v[0] for v in q.outline]
                    ys += [v[1] for v in q.outline]
        for t in self.tracks:
            xs += [t.start[0], t.end[0]]
            ys += [t.start[1], t.end[1]]
        return min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2


# ---------------------------------------------------------------------- boards

class Strip:
    """One straight track between two square SMD pads on F.Cu: R = ρL/(wt), L pad edge to
    pad edge (terminal pads are equipotential)."""
    PAD = 1.5
    X0, X1, Y = 0.0, 40.0, 0.0
    W = 0.5
    NET = "VCC"

    @classmethod
    def length(cls) -> float:
        return cls.X1 - cls.X0 - cls.PAD


def strip_board() -> BoardSnapshot:
    g, b = Strip, SyntheticBoard("strip", two_layer_stackup())
    b.square_pad("J1", "1", g.NET, g.X0, g.Y, g.PAD, value="SUPPLY")
    b.square_pad("U1", "1", g.NET, g.X1, g.Y, g.PAD, value="LOAD")
    b.track(g.NET, F, g.W, (g.X0, g.Y), (g.X1, g.Y))
    return b.snapshot()


def via_board() -> BoardSnapshot:
    """F.Cu track J1 → via at x=20 → B.Cu track → U1 (pad on B.Cu): the via carries it all."""
    b = SyntheticBoard("via", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5, layers=(B,))
    b.track("VCC", F, 0.5, (0.0, 0.0), (20.0, 0.0))
    b.via("VCC", 20.0, 0.0)
    b.track("VCC", B, 0.5, (20.0, 0.0), (40.0, 0.0))
    return b.snapshot()


def pins_board() -> BoardSnapshot:
    """A three-pin part on VCC, plus one pin on another net."""
    b = SyntheticBoard("pins", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    for i, x in enumerate((10.0, 12.0, 14.0), start=1):
        b.square_pad("U1", str(i), "VCC", x, 0.0, 1.0)
    b.square_pad("U1", "4", "OTHER", 16.0, 0.0, 1.0)
    b.track("VCC", F, 0.5, (0.0, 0.0), (14.0, 0.0))
    return b.snapshot()


def net_tie_board() -> BoardSnapshot:
    """Net VA (J1 → NT1.1) and net VB (NT1.2 → U1), joined only by net tie NT1, whose copper
    bridge KiCad saves with no net (so it is absent here, as after import)."""
    b = SyntheticBoard("net_tie", two_layer_stackup())
    lib = "NetTie:NetTie-2_SMD_Pad0.5mm"
    b.square_pad("J1", "1", "VA", 0.0, 0.0, 1.5)
    b.square_pad("NT1", "1", "VA", 19.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("NT1", "2", "VB", 20.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("U1", "1", "VB", 40.0, 0.0, 1.5)
    b.track("VA", F, 0.5, (0.0, 0.0), (19.5, 0.0))
    b.track("VB", F, 0.5, (20.5, 0.0), (40.0, 0.0))
    return b.snapshot()


def demo_board() -> BoardSnapshot:
    """A 60 × 40 mm two-layer demo: a supply connector on F.Cu stitched to a B.Cu plane that
    a slot squeezes into a neck, three loads, one fed by a thin F.Cu track. Made to show
    voltage drop, current crowding and via currents at a glance."""
    b = SyntheticBoard("demo", two_layer_stackup())
    plane = box(2, 2, 58, 38).difference(box(28, 2, 31, 31))     # slot: neck at the top
    b.zone("+3V3", B, plane)
    # Supply J1: a large F.Cu pad stitched down with four vias.
    b.square_pad("J1", "1", "+3V3", 6.0, 20.0, 4.0, value="SUPPLY")
    for dx, dy in ((-1.2, -1.2), (1.2, -1.2), (-1.2, 1.2), (1.2, 1.2)):
        b.via("+3V3", 6.0 + dx, 20.0 + dy)
    # Loads U1, U2 on the far side of the slot, each pad with two vias to the plane.
    for ref, (x, y) in (("U1", (50.0, 10.0)), ("U2", (50.0, 28.0))):
        b.square_pad(ref, "1", "+3V3", x, y, 2.5, value="LOAD")
        b.via("+3V3", x - 0.7, y)
        b.via("+3V3", x + 0.7, y)
    # U3: fed from one plane via through a thin F.Cu track.
    b.square_pad("U3", "1", "+3V3", 18.0, 33.0, 1.5, value="LOAD")
    b.via("+3V3", 18.0, 26.0)
    b.track("+3V3", F, 0.25, (18.0, 26.0), (18.0, 33.0))
    return b.snapshot(outline=box(0, 0, 60, 40))
