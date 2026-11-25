"""Synthetic boards built in Python, with dimensions chosen so results have closed-form
answers. Used by the tests and the committed examples instead of real board files.

All dimensions in mm. Builder modelled on KiPI's ``examples/validation/make_boards.py``.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from kipi.core.model.board import (
    MASK,
    BoardSnapshot,
    Coating,
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
from shapely.geometry import MultiPolygon, box

from coppernicus.project import Link, Load, Project, Rail, Source

F, B = 3, 34                    # KiCad layer ids of F.Cu and B.Cu
OZ1 = 0.035                     # 1 oz copper


def two_layer_stackup(t_cu: float = OZ1, core: float = 1.5, er: float = 4.5,
                      mask_mm: float = 0.0) -> Stackup:
    """F.Cu and B.Cu on an FR4 core; ``mask_mm`` > 0 adds a solder mask that thick on both
    faces (outside the board: it moves no z)."""
    total = 2 * t_cu + core
    copper = [CopperLayer(F, "F.Cu", 0, t_cu, total - t_cu), CopperLayer(B, "B.Cu", 1, t_cu, 0.0)]
    coatings = [Coating(MASK, side, mask_mm) for side in ("top", "bottom")] if mask_mm > 0 else []
    return Stackup(copper, [DielectricLayer(core, "FR4", er, F, B)], total, coatings=coatings)


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


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_strip_kicad_pcb(path, fields: Optional[Dict[str, Dict[str, str]]] = None, net: str = Strip.NET,
                          extra_parts: Sequence[tuple] = ()) -> Path:
    """Write the strip as a small ``.kicad_pcb`` at ``path``, with footprint fields: J1 at
    x=0 and U1 at x=40 joined by the track, and any ``extra_parts`` given as (ref, x, y, net),
    each with the fields ``fields`` gives for its reference, e.g. ``{"J1": {"PI_RAIL": "VCC",
    "PI_SOURCE": "1V"}}``. Synthetic numbers only."""
    g, path, fields = Strip, Path(path), fields or {}
    ids = (f"00000000-0000-4000-8000-{n:012d}" for n in itertools.count(1))
    parts = [("J1", g.X0, g.Y, net), ("U1", g.X1, g.Y, net), *extra_parts]
    nets = sorted({p[3] for p in parts} | {net})
    lines = ['(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")',
             "  (general (thickness 1.6))",
             '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))',
             '  (net 0 "")']
    lines += [f"  (net {i} {_q(n)})" for i, n in enumerate(nets, start=1)]
    code = {n: i for i, n in enumerate(nets, start=1)}
    for ref, x, y, pnet in parts:
        props = [("Reference", ref), ("Value", "PART"), *fields.get(ref, {}).items()]
        lines.append(f'  (footprint "Synthetic:Pad" (layer "F.Cu") (uuid "{next(ids)}") (at {x} {y})')
        for name, text in props:
            lines.append(f'    (property {_q(name)} {_q(text)} (at 0 0 0) (layer "F.Fab") '
                         f'(uuid "{next(ids)}"))')
        lines.append(f'    (pad "1" smd rect (at 0 0) (size {g.PAD} {g.PAD}) (layers "F.Cu") '
                     f'(net {code[pnet]} {_q(pnet)}) (uuid "{next(ids)}"))')
        lines.append("  )")
    lines.append(f'  (segment (start {g.X0} {g.Y}) (end {g.X1} {g.Y}) (width {g.W}) (layer "F.Cu") '
                 f'(net {code[net]}) (uuid "{next(ids)}"))')
    lines.append(f'  (gr_rect (start -5 -5) (end 45 5) (stroke (width 0.1) (type default)) '
                 f'(fill none) (layer "Edge.Cuts") (uuid "{next(ids)}"))')
    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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


def demo_board(led_net: bool = False) -> BoardSnapshot:
    """A 60 × 40 mm two-layer demo: a supply connector on F.Cu stitched to a B.Cu plane that
    a slot squeezes into a neck along the bottom edge (KiCad y points down), three loads,
    one fed by a thin F.Cu track. Made to show voltage drop, current crowding and via
    currents at a glance.

    ``led_net`` adds LED_A, a short F.Cu feed from R1 to an LED (D1) whose name the power
    heuristic does not take for a rail, away from the +3V3 copper: the committed example
    has it, to show marking a net as a rail by hand. It does not change the +3V3 results."""
    b = SyntheticBoard("demo", two_layer_stackup())
    plane = box(2, 2, 58, 38).difference(box(28, 2, 31, 31))     # slot from the top edge
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
    if led_net:
        b.square_pad("R1", "2", "LED_A", 36.0, 36.0, 1.0, value="330R")
        b.square_pad("D1", "1", "LED_A", 46.0, 36.0, 1.0, value="LED")
        b.track("LED_A", F, 0.3, (36.0, 36.0), (46.0, 36.0))
    return b.snapshot(outline=box(0, 0, 60, 40))


class PowerChain:
    """The dimensions of KiPI's power-chain validation board
    (``examples/validation/make_boards.py``), copied so its hand calculations hold here too.

    2 mm wide strips on F.Cu over a 2 mm ground strip on B.Cu, 1 oz copper, pads 1 mm long,
    positions along x (mm). F.Cu: J1.1, U1.1 on VIN | U1.2, U4.1, FB1.1 on +3V3 | FB1.2, U2.1
    on +3V3_F | U2.2, U3.1 on +1V8. B.Cu (GND): J1.2, U1.3, U4.2, U2.3, U3.2.

    A 12 V input J1 feeds switcher U1 (3.3 V); ferrite FB1 joins +3V3 to +3V3_F, which feeds
    LDO U2 (1.8 V), regulated at its load U3 (remote and ground sense)."""
    W = 2.0
    VIN = {"J1.1": 0.0, "U1.1": 29.0}
    V3V3 = {"U1.2": 32.0, "U4.1": 46.0, "FB1.1": 61.0}
    V3V3_F = {"FB1.2": 64.0, "U2.1": 79.0}
    V1V8 = {"U2.2": 82.0, "U3.1": 97.0}
    GND = {"J1.2": 0.0, "U1.3": 31.0, "U4.2": 46.0, "U2.3": 80.5, "U3.2": 97.0}
    V_SRC, V_SMPS, V_LDO = 12.0, 3.3, 1.8
    ETA, IQ_SMPS, IQ_LDO = 0.9, 0.005, 0.002
    R_FB = 0.05
    I_U3, I_U4 = 2.0, 0.5


def power_chain_board(island: bool = False) -> BoardSnapshot:
    """``island`` adds U9, a part whose +3V3 and GND pads sit on copper joined to nothing
    (a load that cannot be reached: disconnected)."""
    g, b = PowerChain, SyntheticBoard("power_chain", two_layer_stackup())
    for net, pins in (("VIN", g.VIN), ("+3V3", g.V3V3), ("+3V3_F", g.V3V3_F), ("+1V8", g.V1V8)):
        xs = list(pins.values())
        b.zone(net, F, box(min(xs), 0, max(xs) + 1, g.W))
        for pin, x in pins.items():
            ref, num = pin.split(".")
            b.pad(ref, num, net, box(x, 0, x + 1, g.W))
    b.zone("GND", B, box(0, 0, max(g.GND.values()) + 1, g.W))
    for pin, x in g.GND.items():
        ref, num = pin.split(".")
        b.pad(ref, num, "GND", box(x, 0, x + 1, g.W), layers=(B,))
    if island:
        b.zone("+3V3", F, box(110, 0, 113, g.W))
        b.pad("U9", "1", "+3V3", box(111, 0, 112, g.W))
        b.zone("GND", B, box(110, 0, 113, g.W))
        b.pad("U9", "2", "GND", box(111, 0, 112, g.W), layers=(B,))
    return b.snapshot()


def power_chain_project(board: str = "power_chain.kipi.json.gz") -> Project:
    """The power chain's setup as a Coppernicus project (KiPI's validation setup, without
    its load tolerances: Coppernicus sets no limit the user did not)."""
    g = PowerChain
    return Project(board=board, rails=[
        Rail("VIN", sources=[Source("J1", g.V_SRC, ["1"], ground_pins=["2"], ground_net="GND")]),
        Rail("+3V3",
             sources=[Source("U1", g.V_SMPS, ["2"], kind="smps", efficiency=g.ETA,
                             quiescent_a=g.IQ_SMPS, input_pins=["1"], input_net="VIN",
                             ground_pins=["3"], ground_net="GND")],
             loads=[Load("U4", g.I_U4, ["1"], ground_pins=["2"], ground_net="GND")]),
        Rail("+1V8",
             sources=[Source("U2", g.V_LDO, ["2"], kind="ldo", quiescent_a=g.IQ_LDO,
                             input_pins=["1"], input_net="+3V3_F", ground_pins=["3"],
                             ground_net="GND", sense_ref="U3", sense_pins=["1"], sense_net="+1V8",
                             ground_sense_ref="U3", ground_sense_pins=["2"],
                             ground_sense_net="GND")],
             loads=[Load("U3", g.I_U3, ["1"], ground_pins=["2"], ground_net="GND")]),
    ], links=[Link("FB1", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", g.R_FB)])


def save_snapshot(snapshot: BoardSnapshot, path) -> None:
    """Write a board snapshot (``*.json.gz``) that :meth:`Board.open` reads back."""
    from kipi.core.model.snapshot_io import save
    save(snapshot, str(path))
