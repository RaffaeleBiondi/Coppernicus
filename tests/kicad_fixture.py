"""A small synthetic .kicad_pcb, written by the tests: a straight 0.5 mm track on F.Cu
between two square SMD pads 40 mm apart (as engine.synthetic.Strip), with the footprints'
fields as the caller gives them. No real board data: every number here is made up."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Dict, Optional, Sequence

PAD, X0, X1, Y, W = 1.5, 0.0, 40.0, 0.0, 0.5       # the same strip as engine.synthetic.Strip


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_strip_board(path: Path, fields: Optional[Dict[str, Dict[str, str]]] = None,
                      net: str = "VCC", extra_parts: Sequence[tuple] = ()) -> Path:
    """Write the strip board to ``path``. ``fields``: per reference (J1 at x=0, U1 at x=40,
    and any ``extra_parts`` given as (ref, x, y, net)), the footprint fields to add, e.g.
    ``{"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"}}``."""
    fields = fields or {}
    ids = (f"00000000-0000-4000-8000-{n:012d}" for n in itertools.count(1))
    parts = [("J1", X0, Y, net), ("U1", X1, Y, net), *extra_parts]
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
        lines.append(f'    (pad "1" smd rect (at 0 0) (size {PAD} {PAD}) (layers "F.Cu") '
                     f'(net {code[pnet]} {_q(pnet)}) (uuid "{next(ids)}"))')
        lines.append("  )")
    lines.append(f'  (segment (start {X0} {Y}) (end {X1} {Y}) (width {W}) (layer "F.Cu") '
                 f'(net {code[net]}) (uuid "{next(ids)}"))')
    lines.append(f'  (gr_rect (start -5 -5) (end 45 5) (stroke (width 0.1) (type default)) '
                 f'(fill none) (layer "Edge.Cuts") (uuid "{next(ids)}"))')
    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_board(path: Path, parts: Sequence[tuple], tracks: Sequence[tuple],
                vias: Sequence[tuple] = ()) -> Path:
    """Write a multi-net synthetic board: ``parts`` as (ref, [(pad, x, y, net)], {field: text})
    with pads at board coordinates (ref None: no reference), ``tracks`` as (net, (x0, y0),
    (x1, y1), width[, layer]) on F.Cu unless a layer is given, ``vias`` as (net, x, y) through
    vias (0.3 mm drill)."""
    ids = (f"00000000-0000-4000-8000-{n:012d}" for n in itertools.count(1))
    nets = sorted({p[3] for _, pads, _ in parts for p in pads} | {t[0] for t in tracks}
                  | {v[0] for v in vias})
    code = {n: i for i, n in enumerate(nets, start=1)}
    lines = ['(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")',
             "  (general (thickness 1.6))",
             '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))', '  (net 0 "")']
    lines += [f"  (net {i} {_q(n)})" for n, i in code.items()]
    xs, ys = [0.0], [0.0]
    for ref, pads, fields in parts:
        lines.append(f'  (footprint "Synthetic:Part" (layer "F.Cu") (uuid "{next(ids)}") (at 0 0)')
        props = ([("Reference", ref)] if ref is not None else []) + list(fields.items())
        for name, text in props:
            lines.append(f'    (property {_q(name)} {_q(text)} (at 0 0 0) (layer "F.Fab") '
                         f'(uuid "{next(ids)}"))')
        for number, x, y, net in pads:
            xs.append(x)
            ys.append(y)
            lines.append(f'    (pad {_q(number)} smd rect (at {x} {y}) (size {PAD} {PAD}) (layers "F.Cu") '
                         f'(net {code[net]} {_q(net)}) (uuid "{next(ids)}"))')
        lines.append("  )")
    for net, a, b, width, *layer in tracks:
        name = layer[0] if layer else "F.Cu"
        lines.append(f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) '
                     f'(layer {_q(name)}) (net {code[net]}) (uuid "{next(ids)}"))')
    for net, x, y in vias:
        lines.append(f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
                     f'(net {code[net]}) (uuid "{next(ids)}"))')
    lines.append(f'  (gr_rect (start {min(xs) - 5} {min(ys) - 5}) (end {max(xs) + 5} {max(ys) + 5}) '
                 f'(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
                 f'(uuid "{next(ids)}"))')
    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
