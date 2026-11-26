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

from coppernicus.project import (
    AcOptions,
    Aggressor,
    CapModel,
    Link,
    Load,
    Phase,
    Probe,
    Project,
    Rail,
    Source,
    Victim,
)

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


class Pack:
    """A cell pack's return, symmetric about x = 30 mm: the negative pole is two holders, BT4
    and BT8, each with its two tabs on -V_BAT, at the two ends of a B.Cu plane; the load U1
    returns its current into the middle of the plane, fed on F.Cu from the positive holder BT1.
    1 oz copper, positions in mm."""
    POS, NEG = "+V_BAT", "-V_BAT"
    PLANE = (0.0, 0.0, 60.0, 20.0)                     # -V_BAT, B.Cu
    TAB = 3.0                                          # a square holder tab
    HOLDERS = {"BT4": ((4.0, 6.0), (4.0, 14.0)), "BT8": ((56.0, 6.0), (56.0, 14.0))}
    U1_GND = (30.0, 10.0)
    RAIL = (26.0, 0.0, 34.0, 20.0)                     # +V_BAT, F.Cu
    BT1, U1 = (30.0, 3.0), (30.0, 17.0)
    V_BAT, I_U1 = 6.4, 2.0


def pack_board() -> BoardSnapshot:
    g, b = Pack, SyntheticBoard("pack", two_layer_stackup())
    b.zone(g.NEG, B, box(*g.PLANE))
    for ref, tabs in g.HOLDERS.items():
        for number, (x, y) in enumerate(tabs, start=1):
            b.square_pad(ref, str(number), g.NEG, x, y, g.TAB, layers=(B,), value="HOLDER")
    b.square_pad("U1", "2", g.NEG, *g.U1_GND, 2.0, layers=(B,), value="LOAD")
    b.zone(g.POS, F, box(*g.RAIL))
    b.square_pad("BT1", "1", g.POS, *g.BT1, g.TAB, value="HOLDER")
    b.square_pad("U1", "1", g.POS, *g.U1, 2.0, value="LOAD")
    return b.snapshot()


def pack_project(grounds: Sequence[str] = ("BT4", "BT8"), board: str = "pack.kipi.json.gz") -> Project:
    """The pack as a project: BT1 holds +V_BAT, its ground terminal on every pad of each holder
    in ``grounds`` (the first is ``ground``, the others ``extra_ground_pins``); U1 draws 2 A."""
    g = Pack
    first, *more = grounds
    source = Source("BT1", g.V_BAT, ground_ref=first, ground_net=g.NEG,
                    extra_ground_pins=[f"{r}.*" for r in more] or None)
    return Project(board=board, rails=[Rail(g.POS, sources=[source], loads=[
        Load("U1", g.I_U1, ["1"], ground_pins=["2"], ground_net=g.NEG)])])


# ---------------------------------------------------------------------- AC boards (Task 9)
# Copied from KiPI's AC validation boards (examples/validation/make_boards.py:65-82,
# :333-449), dimensions and part values unchanged, so its hand calculations hold here too.

IN1, IN2 = 4, 5                 # KiCad layer ids of In1.Cu and In2.Cu
OZ_HALF = 0.0175
AC_ER = 4.5                     # FR4 of KiPI's validation stackup


def four_layer_stackup(copper=(OZ1, OZ_HALF, OZ_HALF, OZ1), diel=(0.2, 1.0, 0.2),
                       er: float = AC_ER) -> Stackup:
    """KiPI's validation stackup: four copper layers (or two, with one dielectric), εr 4.5."""
    ids = [F, IN1, IN2, B] if len(copper) == 4 else [F, B]
    names = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] if len(copper) == 4 else ["F.Cu", "B.Cu"]
    total = sum(copper) + sum(diel)
    z, cu, ds = total, [], []
    for i, (lid, name, t) in enumerate(zip(ids, names, copper)):
        z -= t
        cu.append(CopperLayer(lid, name, i, t, z))
        if i < len(diel):
            z -= diel[i]
            ds.append(DielectricLayer(diel[i], "FR4", er, lid, ids[i + 1]))
    return Stackup(cu, ds, total)


class AcPlane:
    """A rail plane on F.Cu over a GND plane on In1.Cu, 0.2 mm apart. The probe U1 has a
    rail pad on the plane and a GND pad in a cut-out, with a via down to In1.Cu."""
    SIZE = 100.0
    H = 0.2
    PROBE = (5.0, 5.0)
    PAD, HOLE, GAP = 0.6, 1.2, 1.2     # pad side, GND pad cut-out side, rail pad -> GND pad
    MAX_EDGE = 2.0


class AcDecap:
    """A 50 mm plane pair (as :class:`AcPlane`) with one capacitor C1 beside the probe U1."""
    SIZE = 50.0
    PROBE = (25.0, 25.0)
    CAP = (25.0, 30.0)
    C, ESL, ESR = 10e-6, 0.5e-9, 0.02
    MAX_EDGE = 1.0


class AcFerrite:
    """+3V3 (a 40 mm plane pair with a bulk capacitor C1) feeds +3V3_F (a 15 mm plane pair
    with the probe U2) through ferrite FB1, a link of R_FB and L_FB. The source U1 has no
    ground pins."""
    SIZE_A, SIZE_B, GAP = 40.0, 15.0, 5.0
    C, ESL, ESR = 100e-6, 1e-9, 0.01
    R_FB, L_FB = 1.0, 1e-6
    MAX_EDGE = 1.0


AC_BANDWIDTH_HZ = 100.0          # KiPI's validation VRMs: 0.1 kHz, below the sweep


def _ac_part(b: SyntheticBoard, ref, xy, value, rail="+3V3", gap=AcPlane.GAP):
    """A two-pad part: rail pad at ``xy``, GND pad ``gap`` to the right with a via down to
    In1.Cu. Returns the GND pad's cut-out in the rail plane."""
    x, y = xy
    s = AcPlane.PAD / 2
    b.pad(ref, "1", rail, box(x - s, y - s, x + s, y + s), value=value)
    gx = x + gap
    b.pad(ref, "2", "GND", box(gx - s, y - s, gx + s, y + s), value=value)
    b.via("GND", gx, y, 0.3, 0.6, layers=(F, IN1))
    h = AcPlane.HOLE / 2
    return box(gx - h, y - h, gx + h, y + h)


def _ac_board(name, size, parts, rail="+3V3", er: float = AC_ER) -> SyntheticBoard:
    b = SyntheticBoard(name, four_layer_stackup((OZ1, OZ_HALF, OZ_HALF, OZ1), (AcPlane.H, 1.0, 0.2), er))
    holes = [_ac_part(b, ref, xy, value, rail) for ref, xy, value in parts]
    plane = box(0, 0, size, size)
    for h in holes:
        plane = plane.difference(h)
    b.zone(rail, F, plane)
    b.zone("GND", IN1, box(0, 0, size, size))
    return b


def ac_plane_board() -> BoardSnapshot:
    g = AcPlane
    return _ac_board("ac_plane", g.SIZE, [("U1", g.PROBE, "Probe")]).snapshot()


def ac_decap_board(er: float = AC_ER) -> BoardSnapshot:
    """``er`` 0: a stackup that gives no εr (KiPI then uses ac.default_er)."""
    g = AcDecap
    return _ac_board("ac_decap", g.SIZE, [("U1", g.PROBE, "Probe"), ("C1", g.CAP, "10u")], er=er).snapshot()


def ac_ferrite_board() -> BoardSnapshot:
    g = AcFerrite
    b = SyntheticBoard("ac_ferrite", four_layer_stackup((OZ1, OZ_HALF, OZ_HALF, OZ1), (AcPlane.H, 1.0, 0.2)))
    xb = g.SIZE_A + g.GAP                      # +3V3_F plane's left edge
    yb = (g.SIZE_A - g.SIZE_B) / 2
    holes_a = [_ac_part(b, "C1", (20.0, 20.0), "100u")]
    holes_b = [_ac_part(b, "U2", (xb + 7.0, yb + 7.5), "Probe", rail="+3V3_F")]
    b.pad("U1", "1", "+3V3", box(5 - 0.3, 20 - 0.3, 5 + 0.3, 20 + 0.3), value="Regulator")
    s = AcPlane.PAD / 2
    b.pad("FB1", "1", "+3V3", box(g.SIZE_A - 1.0 - s, 20 - s, g.SIZE_A - 1.0 + s, 20 + s),
          value="Ferrite")
    b.pad("FB1", "2", "+3V3_F", box(xb + 1.0 - s, 20 - s, xb + 1.0 + s, 20 + s))
    plane_a, plane_b = box(0, 0, g.SIZE_A, g.SIZE_A), box(xb, yb, xb + g.SIZE_B, yb + g.SIZE_B)
    for h in holes_a:
        plane_a = plane_a.difference(h)
    for h in holes_b:
        plane_b = plane_b.difference(h)
    b.zone("+3V3", F, plane_a)
    b.zone("+3V3_F", F, plane_b)
    b.zone("GND", IN1, box(0, 0, xb + g.SIZE_B, g.SIZE_A))
    return b.snapshot()


def _ac_probe(ref: str, rail: str = "+3V3") -> Probe:
    return Probe(ref, ref, rail, ["1"], ground_pins=["2"], ground_net="GND")


def ac_plane_project(board: str = "ac_plane.kipi.json.gz") -> Project:
    """KiPI's ac_plane setup: the source on the probe's pins, its sweep (to 1 GHz, past the
    first cavity mode at 706.6 MHz) and its 0.1 kHz bandwidth."""
    g = AcPlane
    return Project(board=board, rails=[Rail("+3V3", sources=[
        Source("U1", 3.3, ["1"], ground_pins=["2"], ground_net="GND")])],
        probes=[_ac_probe("U1")],
        ac=AcOptions(f_start_hz=1e3, f_stop_hz=1e9, points_per_decade=100,
                     field_points_per_decade=20, max_edge_mm=g.MAX_EDGE,
                     bandwidth_hz={"U1": AC_BANDWIDTH_HZ}))


def ac_decap_project(board: str = "ac_decap.kipi.json.gz") -> Project:
    """KiPI's ac_decap setup: the plane pair with model "10u" on C1."""
    g = AcDecap
    return Project(board=board, rails=[Rail("+3V3", sources=[
        Source("U1", 3.3, ["1"], ground_pins=["2"], ground_net="GND")])],
        probes=[_ac_probe("U1")], capacitors=[CapModel("10u", g.C, g.ESL, g.ESR, ["C1"])],
        ac=AcOptions(f_start_hz=1e3, f_stop_hz=1e9, points_per_decade=100,
                     field_points_per_decade=10, max_edge_mm=g.MAX_EDGE,
                     bandwidth_hz={"U1": AC_BANDWIDTH_HZ}))


def ac_ferrite_project(board: str = "ac_ferrite.kipi.json.gz") -> Project:
    """KiPI's ac_ferrite setup: FB1 a link of R_FB with L_FB as its series inductance, the
    probe U2 on +3V3_F, the source U1 without ground pins (the ground is the probe's)."""
    g = AcFerrite
    return Project(board=board, rails=[Rail("+3V3", sources=[Source("U1", 3.3, ["1"])])],
                   links=[Link("FB1", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", g.R_FB)],
                   probes=[_ac_probe("U2", "+3V3_F")],
                   capacitors=[CapModel("100u", g.C, g.ESL, g.ESR, ["C1"])],
                   ac=AcOptions(f_start_hz=1e3, f_stop_hz=300e6, points_per_decade=100,
                                field_points_per_decade=10, max_edge_mm=g.MAX_EDGE,
                                bandwidth_hz={"U1": AC_BANDWIDTH_HZ},
                                link_esl_h={"FB1": g.L_FB}))


def save_snapshot(snapshot: BoardSnapshot, path) -> None:
    """Write a board snapshot (``*.json.gz``) that :meth:`Board.open` reads back."""
    from kipi.core.model.snapshot_io import save
    save(snapshot, str(path))


# ---------------------------------------------------------------------- noise board (Task 10)
# Copied from KiPI's coupled-noise validation board (examples/validation/make_boards.py:454-525),
# dimensions and part values unchanged, so its accuracy tests hold here too.

class CoupledNoise:
    """A 12 V to 1.8 V buck (U1, power stage) on F.Cu over a GND plane on In1.Cu, with three
    0.2 mm victims, each between two ICs with a ground pin:

    * SIG_SW beside the switch node's pour (capacitive coupling, with a like inductive part
      through its ends beside the output capacitor's via),
    * SIG_LOOP beside the hot loop's VIN track (inductive coupling dominates),
    * SIG_BURIED on In2.Cu under SIG_LOOP, behind the plane (shielded: far less noise).

    U1's output pin is L1.2 (the inductor's VOUT pad), its input U1.1 and its ground U1.2."""
    FSW, RISE = 1e6, 2e-9
    VIN, VOUT, LOAD = 12.0, 1.8, 5.0
    L, COSS = 1e-6, 300e-12
    ETA = 0.9                        # KiPI's Vrm default (dc/pi/model.py:93), which its setup keeps
    GAP = 0.2                        # victim to aggressor copper
    SW_POUR = 4.0
    C1 = (10e-6, 0.4e-9, 0.003)      # C, ESL, ESR of the input capacitor
    C2 = (47e-6, 0.6e-9, 0.002)      # ... and of the output capacitor
    VICTIMS = (("SIG_SW", "U3", "U4"), ("SIG_LOOP", "U5", "U6"), ("SIG_BURIED", "U7", "U8"))


def coupled_noise_board() -> BoardSnapshot:
    g = CoupledNoise
    b = SyntheticBoard("coupled_noise", four_layer_stackup())

    def sq(x, y, s):
        return box(x - s / 2, y - s / 2, x + s / 2, y + s / 2)

    def part(ref, value, pins, size=1.0):
        for num, net, (x, y) in pins:
            b.pad(ref, num, net, sq(x, y, size), value=value)
            if net == "GND":
                b.via("GND", x, y + size / 2, 0.3, 0.6, layers=(F, IN1))

    part("J1", "CONN", [("1", "VIN", (2, 10)), ("2", "GND", (2, 14))])
    part("C1", "10uF", [("1", "VIN", (6, 10)), ("2", "GND", (6, 14))])
    part("U1", "Power stage", [("1", "VIN", (10, 10)), ("2", "GND", (10, 14)), ("3", "SW", (12, 12))])
    part("L1", "1uH", [("1", "SW", (16, 12)), ("2", "VOUT", (20, 12))])
    part("C2", "47uF", [("1", "VOUT", (24, 12)), ("2", "GND", (24, 15))])
    part("U2", "Load", [("1", "VOUT", (28, 12)), ("2", "GND", (28, 15))])
    for net, a, c in (("VIN", (2, 10), (10, 10)), ("GND", (2, 14), (10, 14)),
                      ("SW", (12, 12), (16, 12)), ("VOUT", (20, 12), (28, 12)),
                      ("GND", (24, 15), (28, 15))):
        b.track(net, F, 1.0, a, c)
    b.zone("SW", F, sq(13.0, 12.0, g.SW_POUR))
    b.zone("GND", IN1, box(0, 0, 32, 20))
    y_sw = 12.0 + g.SW_POUR / 2 + g.GAP + 0.1
    y_loop = 10.0 - 0.5 - g.GAP - 0.1
    for net, layer, y, x0, x1, refs, dy in (
            ("SIG_SW", F, y_sw, 11.5, 24.0, ("U3", "U4"), 0.6),
            ("SIG_LOOP", F, y_loop, 3.0, 9.5, ("U5", "U6"), -0.6),
            ("SIG_BURIED", IN2, y_loop, 3.0, 9.5, ("U7", "U8"), -1.2)):
        for ref, x in zip(refs, (x0, x1)):
            b.pad(ref, "1", net, sq(x, y, 0.3), layers=(layer,), value="IC")
            b.pad(ref, "2", "GND", sq(x, y + dy, 0.3), layers=(layer,), value="IC")
            b.via("GND", x, y + dy, 0.2, 0.3, layers=(F, IN1, IN2) if layer == IN2 else (F, IN1))
        b.track(net, layer, 0.2, (x0, y), (x1, y))
    return b.snapshot()


def coupled_noise_project(board: str = "coupled_noise.kipi.json.gz", qrr_nc: Optional[float] = None,
                          logic_v: Optional[float] = None, ripple_probe: bool = True) -> Project:
    """KiPI's coupled-noise setup as a project: J1 12 V on VIN; U1 a switcher (efficiency 0.9,
    KiPI's default that its setup keeps) holding VOUT at 1.8 V at L1.2, fed at U1.1, ground
    U1.2; U2 drawing 5 A; models 10u on C1 and 47u on C2; aggressor U1 at 1 MHz, 2 ns edges,
    Coss 300 pF; the three victims as cmos lines with both ends. ``qrr_nc`` None leaves KiPI's
    stand-in (as its setup does); ``logic_v`` None sets no margin; ``ripple_probe`` adds the
    probe LOAD at U2 and names it for the ripple."""
    g = CoupledNoise
    gnd = dict(ground_pins=["2"], ground_net="GND")
    project = Project(board=board, rails=[
        Rail("VIN", sources=[Source("J1", g.VIN, ["1"], **gnd)]),
        Rail("VOUT", sources=[Source("L1", g.VOUT, ["2"], name="U1", kind="smps", efficiency=g.ETA,
                                     input_ref="U1", input_pins=["1"], input_net="VIN",
                                     ground_ref="U1", **gnd)],
             loads=[Load("U2", g.LOAD, ["1"], **gnd)])],
        capacitors=[CapModel("10u", *g.C1, ["C1"]), CapModel("47u", *g.C2, ["C2"])])
    phase = Phase(inductor="L1", inductance_h=g.L, switch_net="SW", high_side_ref="U1",
                  high_side_pins=["1"], high_side_net="VIN", low_side_ref="U1", low_side_pins=["2"],
                  low_side_net="GND", input_caps=["C1"], output_caps=["C2"], coss_f=g.COSS,
                  qrr_nc=qrr_nc)
    project.aggressors = [Aggressor("U1", vrm="U1", fsw_hz=g.FSW, rise_s=g.RISE, fall_s=g.RISE,
                                    phases=[phase], ripple_probes=["LOAD"] if ripple_probe else [])]
    if ripple_probe:
        project.probes = [Probe("LOAD", "U2", "VOUT", ["1"], **gnd)]
    project.victims = [Victim(net, preset="cmos", driver_ref=d, driver_pins=["1"], receiver_ref=r,
                              receiver_pins=["1"], logic_v=logic_v) for net, d, r in g.VICTIMS]
    return project
