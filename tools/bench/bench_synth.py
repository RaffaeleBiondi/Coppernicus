"""Synthetic benchmark boards of growing size, built in memory with
``coppernicus.engine.synthetic`` (no board or project file is written anywhere).

``grid-<side>[x<rails>]``: a square four-layer board ``side`` mm wide (KiPI's validation
stackup: 1 oz outer, 0.5 oz inner copper, FR4). ``rails`` (default 2) horizontal rail planes
RAIL1, RAIL2... lie on F.Cu and again on B.Cu, stitched by two rows of vias (about one pair
per 10 mm) that pass through the GND planes on In1.Cu and In2.Cu in clearance holes. Each
rail has a source PSk at its left end and a row of loads (about one per 15 mm) on F.Cu, every
part with a rail pin and a GND pin; the GND pin sits in a cut-out of the rail plane with a
through via to the GND planes (cut out of B.Cu's rail plane too). RAIL1 also has a probe at
its first load and one modelled capacitor beside it, for the AC steps.

Every net has copper on two layers, so a large one can reach KiPI's process pool (its layers
meshed side by side). With KiPI's 0.5 mm largest edge a plane meshes into about
2 A / (√3/4 · 0.5²) ≈ 18.5 triangles per mm², plus the finer rings around the vias. Measured
(two rails): the power tree (all rails and GND) has 96k triangles at 25 mm, 240k at 50, 766k at
100, 1.2M at 130, 2.0M at 170 and 2.7M at 200 mm; one rail about a quarter of that (22.6k at
25 mm, 525k at 170).
"""

from __future__ import annotations

from typing import Tuple

from shapely.geometry import box

GAP_MM = 1.0           # between rail planes, and their margin to the board edge
PAD_MM = 1.0           # load and GND pads (square)
HOLE_MM = 2.0          # the cut-out around a GND pad (and its via) in a rail plane
SOURCE_PAD_MM = 2.0
ANTIPAD_MM = 1.0       # the clearance hole around a rail via in the GND planes (via pad 0.6 mm)
LOAD_PITCH_MM = 15.0
STITCH_PITCH_MM = 10.0
LOAD_A = 0.5
VOLTS = (3.3, 1.8, 1.2, 5.0, 2.5, 1.0)


def parse(name: str) -> Tuple[float, int]:
    """``grid-<side>[x<rails>]`` -> (side mm, rails)."""
    if not name.startswith("grid-"):
        raise ValueError(f"unknown synthetic case {name!r}: use grid-<side mm>[x<rails>], e.g. grid-100")
    side, _, rails = name[5:].partition("x")
    try:
        side_mm, n = float(side), int(rails or 2)
    except ValueError:
        raise ValueError(f"unknown synthetic case {name!r}: use grid-<side mm>[x<rails>]") from None
    if side_mm < 20 or not 1 <= n <= len(VOLTS):
        raise ValueError(f"{name}: side at least 20 mm, 1 to {len(VOLTS)} rails")
    return side_mm, n


def _square(x: float, y: float, side: float):
    return box(x - side / 2, y - side / 2, x + side / 2, y + side / 2)


def build(name: str):
    """(Board, Project) of a synthetic case, in memory."""
    from coppernicus.engine import Board
    from coppernicus.engine import synthetic as S
    from coppernicus.project import AcOptions, CapModel, Load, Probe, Project, Rail, Source

    side, n_rails = parse(name)
    F, IN1, IN2, B = S.F, S.IN1, S.IN2, S.B
    b = S.SyntheticBoard(name, S.four_layer_stackup())
    height = (side - (n_rails + 1) * GAP_MM) / n_rails
    if height < 9.0:
        raise ValueError(f"{name}: rail planes {height:.1f} mm high, at least 9 mm: fewer rails or a "
                         "larger side")
    rails, antipads = [], []
    probe = cap = None

    for k in range(n_rails):
        net = f"RAIL{k + 1}"
        y0 = GAP_MM + k * (height + GAP_MM)
        yc = y0 + height / 2
        holes = []                         # GND pins' cut-outs, in the rail plane on F and B

        def part(ref: str, x: float, y: float, rail_pad: float, net=net, holes=holes):
            """Pin 1 on the rail at (x, y), pin 2 on GND to its right with a through via."""
            b.square_pad(ref, "1", net, x, y, rail_pad)
            gx = x + rail_pad / 2 + HOLE_MM
            b.square_pad(ref, "2", "GND", gx, y, PAD_MM)
            b.via("GND", gx, y, layers=(F, IN1, IN2, B))
            holes.append(_square(gx, y, HOLE_MM))

        part(f"PS{k + 1}", GAP_MM + 2.0, yc, SOURCE_PAD_MM)
        n_loads = max(2, int(side // LOAD_PITCH_MM))
        x_first, x_last = side * 0.3, side - GAP_MM - 3 * HOLE_MM
        xs = [x_first + i * (x_last - x_first) / (n_loads - 1) for i in range(n_loads)]
        loads = []
        for i, x in enumerate(xs):
            ref = f"U{k + 1}{i + 1:02d}"
            part(ref, x, yc, PAD_MM)
            loads.append(Load(ref, LOAD_A, ["1"], ground_pins=["2"], ground_net="GND"))
        if k == 0:                         # AC: a probe at the first load, a capacitor beside it
            probe = Probe("P1", loads[0].ref, net, ["1"], ground_pins=["2"], ground_net="GND")
            cap = "C1"
            part(cap, xs[0], yc + 2.0, PAD_MM)
        # Stitching vias between the rail's two planes, in two rows clear of the parts' row.
        n_stitch = max(2, int((side - 2 * GAP_MM) // STITCH_PITCH_MM))
        dy = min(height / 2 - 1.0, 4.0)          # rows 3.5 to 4 mm off the parts' row
        for i in range(n_stitch):
            x = GAP_MM + (i + 0.5) * (side - 2 * GAP_MM) / n_stitch
            for y in (yc - dy, yc + dy):
                b.via(net, x, y, layers=(F, IN1, IN2, B))
                antipads.append(_square(x, y, ANTIPAD_MM))
        plane = box(GAP_MM, y0, side - GAP_MM, y0 + height)
        for h in holes:
            plane = plane.difference(h)
        b.zone(net, F, plane)
        b.zone(net, B, plane)
        rails.append(Rail(net, sources=[Source(f"PS{k + 1}", VOLTS[k], ["1"], ground_pins=["2"],
                                               ground_net="GND")], loads=loads))
    ground = box(GAP_MM / 2, GAP_MM / 2, side - GAP_MM / 2, side - GAP_MM / 2)
    for h in antipads:
        ground = ground.difference(h)
    b.zone("GND", IN1, ground)
    b.zone("GND", IN2, ground)
    snap = b.snapshot(outline=box(0, 0, side, side))
    project = Project(board="", rails=rails, probes=[probe],
                      capacitors=[CapModel("10u", 10e-6, 0.5e-9, 0.02, [cap])],
                      ac=AcOptions(f_start_hz=1e3, f_stop_hz=1e8, points_per_decade=20,
                                   field_points_per_decade=5, bandwidth_hz={"PS1": 1e5}))
    return Board(snap), project
