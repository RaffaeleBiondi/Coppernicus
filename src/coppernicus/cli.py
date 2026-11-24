"""Command line.

    coppernicus                         open the app
    coppernicus gui [BOARD|PROJECT]     open the app on a board or project file
    coppernicus nets BOARD [--all]      power and ground nets (all nets with --all)
    coppernicus parts BOARD NET         parts and pins on a net (to write sources and loads)
    coppernicus solve PROJECT [--out DIR] [--rail NET ...] [--no-maps]

``solve`` exits with 0 when every rail solved and no user limit was exceeded, 1 when a
limit was exceeded, 2 on an error (missing file, part not on the net, no copper path...).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from coppernicus import __version__

EXIT_OK, EXIT_LIMIT, EXIT_ERROR = 0, 1, 2


def _cmd_nets(args) -> int:
    from coppernicus.engine import Board
    board = Board.open(args.board)
    nets = board.nets() if args.all else board.power_nets()
    names = {lid.id: lid.name for lid in board.layers()}
    print(f"{board.name}: {len(board.nets())} nets, {len(board.layers())} copper layers")
    for n in nets:
        layers = ", ".join(names[i] for i in board.net_layers(n))
        print(f"  {n:28s} {board.net_kind(n):7s} {layers}")
    _print_board_warnings(board)
    return EXIT_OK


def _cmd_parts(args) -> int:
    from coppernicus.engine import Board
    board = Board.open(args.board)
    parts = board.parts_on_net(args.net)
    if not parts:
        print(f"No parts on net {args.net!r}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{args.net}: {len(parts)} parts")
    for p in parts:
        pins = ", ".join(p.pins[:16]) + (f" … ({len(p.pins)} pins)" if len(p.pins) > 16 else "")
        print(f"  {p.ref:8s} {p.value[:24]:24s} pins {pins}")
    ties = [t for t in board.net_ties() if args.net in (t.net_a, t.net_b)]
    for t in ties:
        print(f"  net tie {t.ref}: {t.net_a} ↔ {t.net_b} (solved as a short)")
    return EXIT_OK


def _print_board_warnings(board):
    for w in board.warnings:
        print(f"  board warning: {w}")


def _cmd_solve(args) -> int:
    from coppernicus.engine import Board, solve_rail
    from coppernicus.maps import safe_name, save_layer_maps
    from coppernicus.problems import FAIL, find_problems
    from coppernicus.project import Project

    project = Project.load(args.project)
    board = Board.open(project.board_path())
    print(f"Board: {board.name} ({len(board.layers())} copper layers)")
    _print_board_warnings(board)
    rails = [r for r in project.rails if not args.rail or r.net in args.rail]
    if not rails:
        print("No rails to solve" + (f" matching {args.rail}" if args.rail else ""), file=sys.stderr)
        return EXIT_ERROR
    out_dir = Path(args.out) if args.out else project.board_path().parent / "results"
    summary, any_fail = [], False
    for rail in rails:
        res = solve_rail(board, rail, project.settings,
                         progress=(lambda m: print(f"    … {m}")) if args.verbose else None)
        problems = find_problems(res, rail.limits)
        any_fail |= any(p.severity == FAIL for p in problems)
        _print_rail(res, problems)
        maps = [] if args.no_maps else save_layer_maps(res, board.outline(), out_dir, problems)
        for m in maps:
            print(f"    map: {m}")
        summary.append(_rail_summary(res, problems, maps))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_name(Path(args.project).stem)}_summary.json"
    path.write_text(json.dumps({"board": board.name, "rails": summary}, indent=1), encoding="utf-8")
    print(f"Summary: {path}")
    return EXIT_LIMIT if any_fail else EXIT_OK


def _print_rail(res, problems):
    from coppernicus.problems import FAIL
    others = [n for n in res.nets if n != res.net]
    nets = f" (with {', '.join(others)} via net ties)" if others else ""
    print(f"\nRail {res.net}{nets}: solved in {res.stats['seconds']:.1f} s, "
          f"{int(res.stats['triangles']):,} triangles")
    for t in res.sources:
        print(f"  source {t.name:14s} {t.v_max:8.4f} V  delivers {t.current:7.3f} A")
    for t in res.loads:
        print(f"  load   {t.name:14s} {t.v_min:8.4f} V  draws    {t.current:7.3f} A  "
              f"drop {res.drop(t) * 1e3:8.2f} mV ({res.drop_pct(t):5.2f} %)")
    shares = res.loss_breakdown()
    total = sum(shares.values()) or 1.0
    split = ", ".join(f"{k} {v / total:.0%}" for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                      if v / total >= 0.01)
    print(f"  max drop {res.max_drop * 1e3:.2f} mV · copper loss {res.power_loss * 1e3:.3g} mW ({split})")
    print("  problems (fails first, then ranked):")
    for p in problems[:12]:
        tag = "FAIL" if p.severity == FAIL else "    "
        where = f" [{p.layer}]" if p.layer else ""
        print(f"    {tag} {p.title}{where} @ ({p.x:.2f}, {p.y:.2f}) mm")
    for a in res.assumptions:
        print(f"  assumption: {a}")
    for w in res.warnings:
        print(f"  warning: {w}")


def _rail_summary(res, problems, maps) -> dict:
    return {
        "net": res.net, "nets": res.nets, "v_ref": res.v_ref,
        "max_drop_v": res.max_drop, "j_max_a_mm2": res.j_max, "power_loss_w": res.power_loss,
        "energy_balance_error": res.energy_balance_error, "loss_breakdown_w": res.loss_breakdown(),
        "sources": [{"name": t.name, "ref": t.ref, "current_a": t.current, "v": t.v_max}
                    for t in res.sources],
        "loads": [{"name": t.name, "ref": t.ref, "current_a": t.current, "v_min": t.v_min,
                   "v_max": t.v_max, "drop_v": res.drop(t), "drop_pct": res.drop_pct(t)}
                  for t in res.loads],
        "problems": [{"severity": p.severity, "kind": p.kind, "title": p.title, "detail": p.detail,
                      "value": p.value, "x_mm": p.x, "y_mm": p.y, "layer": p.layer} for p in problems],
        "assumptions": res.assumptions, "warnings": res.warnings, "stats": res.stats,
        "maps": [str(m) for m in maps],
    }


def _cmd_gui(args) -> int:
    from coppernicus.app import run
    return run(args.path, screenshot=args.screenshot, solve_net=args.solve_net, layer=args.layer,
               field=args.field)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="coppernicus", description="DC power-integrity analysis of KiCad PCBs")
    ap.add_argument("--version", action="version", version=f"coppernicus {__version__}")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="open the app")
    p.add_argument("path", nargs="?", help="a board (.kicad_pcb, ...) or project (.coppernicus.yml)")
    # Test hooks (hidden): open, solve a net, save window and view images, quit.
    for flag in ("--screenshot", "--solve-net", "--layer", "--field"):
        p.add_argument(flag, help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_gui)

    p = sub.add_parser("nets", help="list power and ground nets of a board")
    p.add_argument("board")
    p.add_argument("--all", action="store_true", help="list every net")
    p.set_defaults(func=_cmd_nets)

    p = sub.add_parser("parts", help="list parts and pins on a net")
    p.add_argument("board")
    p.add_argument("net")
    p.set_defaults(func=_cmd_parts)

    p = sub.add_parser("solve", help="solve the rails of a project file")
    p.add_argument("project")
    p.add_argument("--out", help="folder for maps and summary (default: results/ next to the board)")
    p.add_argument("--rail", action="append", help="solve only this net (repeatable)")
    p.add_argument("--no-maps", action="store_true", help="skip the PNG maps")
    p.add_argument("-v", "--verbose", action="store_true", help="show the engine's progress")
    p.set_defaults(func=_cmd_solve)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["gui", *(argv or [])])
    from coppernicus.engine import EngineError
    from coppernicus.project import ProjectError
    try:
        return args.func(args)
    except (EngineError, ProjectError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
