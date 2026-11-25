"""Command line.

    coppernicus                         open the app
    coppernicus gui [BOARD|PROJECT] [--solve NET] [--field drop|voltage|current]
    coppernicus nets BOARD [--all] [--json]   power and ground nets (all nets with --all)
    coppernicus parts BOARD NET [--json]      parts and pins on a net (to write sources and loads)
    coppernicus solve PROJECT [--out DIR] [--rail NET ...] [--no-maps] [--top N] [--json]
                              [--html [PATH]] [--open] [--no-board-fields]
    coppernicus solve PROJECT --tree [--out DIR] [--top N] [--json] [--html [PATH]] [--open]
                                                the whole power tree
    coppernicus solve PROJECT --thermal [--out DIR] [--top N] [--json]   power tree with board temperature
    coppernicus fields BOARD [--json]          the KiCad footprint fields (PI_*) of a board
    coppernicus mcp [--out DIR]               MCP server for AI agents, over stdio
    coppernicus ac PROJECT [--rail NET ...] [--out DIR] [--json]   AC PDN impedance |Z|(f) (CSV + PNG)
    coppernicus noise PROJECT [--aggressor NAME ...] [--out DIR] [--json]   DC-DC ripple and coupled noise

``solve`` exits with 0 when every rail solved and no user limit was exceeded (or none was
set), 1 when a limit was exceeded, 3 when a limit that was set could not judge something (a
% limit on a 0 V net, a limit of the wrong kind, a rail not solved), 2 on an error (missing
file, invalid project file, part not on the net, no copper path...). Precedence: 2, 1, 3, 0.
It writes ``<project>_summary.json`` into the output folder: the same
document ``--json`` prints. ``--html`` also writes an offline, interactive HTML report.

With ``--json`` a command prints one JSON document on stdout (the data the MCP tools
return, units in every key name) and nothing else; an error is printed as
``{"error": {"code", "message", "hint"}}`` with exit code 2. Progress (``-v``) goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from coppernicus import __version__

EXIT_OK, EXIT_LIMIT, EXIT_ERROR, EXIT_NOT_JUDGED = 0, 1, 2, 3
EXIT_CODES = ("exit codes: 0 no limit exceeded (or none set), 1 a limit exceeded, 3 a limit that "
              "was set could not judge something (listed as NOT JUDGED), 2 an error; "
              "precedence 2, 1, 3, 0")


def _exit_for(doc) -> int:
    """The exit code of a solve document: 1 when a limit is exceeded, else 3 when a limit
    that was set could not judge something (its ``not_judged`` list), else 0. With no limit
    at all, 0: nothing to judge is not an error."""
    if doc.get("verdict") == "fail":
        return EXIT_LIMIT
    return EXIT_NOT_JUDGED if doc.get("not_judged") else EXIT_OK


def _print_json(doc) -> None:
    print(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False))


def _run_json(fn) -> int:
    """Run ``fn() -> (document, exit code)`` and print the document, or the error, as JSON."""
    from coppernicus.service import ServiceError
    try:
        doc, code = fn()
    except ServiceError as e:
        _print_json({"error": e.to_dict()})
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    _print_json(doc)
    return code


def _json_nets(args) -> int:
    from coppernicus.service import Session

    def run():
        session = Session()
        board = session.open_board(args.board)
        doc = session.list_nets(board["board_id"], kind="all" if args.all else "power_or_ground",
                                limit=1_000_000)
        return {"board": board, **doc}, EXIT_OK
    return _run_json(run)


def _json_parts(args) -> int:
    from coppernicus.service import Session

    def run():
        from coppernicus.service import ServiceError
        session = Session()
        board = session.open_board(args.board)
        doc = session.net_parts(board["board_id"], args.net)
        if not doc["parts"]:                       # as the human-readable command: exit 2
            raise ServiceError("no_parts_on_net", f"No parts on net {args.net!r}",
                               "sources and loads must be parts with pads on the net")
        return {"board": board, **doc}, EXIT_OK
    return _run_json(run)



def _cmd_fields(args) -> int:
    """``fields BOARD``: the KiCad footprint fields (PI_*) of a board and the rails they make,
    each value with the field it came from. Nothing is solved."""
    from coppernicus.service import ServiceError, Session

    if args.propose:
        return _propose_fields(args)

    def run():
        session = Session()
        board = session.open_board(args.board)
        return {"board": board, **session.board_fields(board["board_id"])}, EXIT_OK
    if args.json:
        return _run_json(run)
    try:
        doc, _ = run()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    if not doc["fields"]:
        print(f"{doc['board']['name']}: no footprint has PI_* fields")
        return EXIT_OK
    for part in doc["fields"]:
        print(f"  {part['ref']:8s} " + "  ".join(f"{k}={v}" for k, v in part["fields"].items()))
    for rail in doc["rails"]:
        print(f"Rail {rail['net']}" + ("" if rail["complete"] else f": {rail['missing']}"))
        if rail["note"]:
            print(f"  {rail['note']}")
        for warning in rail.get("warnings", []):
            print(f"  warning: {warning}")
    for link in doc["links"]:
        print(f"Link {link['ref']} {link['resistance_ohm']:g} Ω between {link['net_a']} and "
              f"{link['net_b']} ({link['from']})")
    return EXIT_OK


def _propose_fields(args) -> int:
    """``fields --propose PROJECT``: the PI_* field text to paste into KiCad, part by part."""
    from coppernicus.service import ServiceError, Session

    def run():
        return Session().propose_fields(project_path=args.board), EXIT_OK
    if args.json:
        return _run_json(run)
    try:
        doc, _ = run()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    for part in doc["parts"]:
        print(part["ref"])
        for where, key in (("library symbol:    ", "library_symbol"),
                           ("schematic instance:", "schematic_instance")):
            if part[key]:
                print(f"  {where} " + "  ".join(f"{k}={v}" for k, v in part[key].items()))
    for n in doc["not_expressed"]:
        where = " ".join(x for x in (n["rail"] and f"rail {n['rail']}", n["part"]) if x)
        print(f"not expressed ({where}): {n['what']}; {n['reason']}")
    for a in doc["assumptions"]:
        print(f"note: {a}")
    return EXIT_OK


def _cmd_nets(args) -> int:
    if args.json:
        return _json_nets(args)
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
    if args.json:
        return _json_parts(args)
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
    """Solve a project through the service layer. The human report (or, with ``--json``, the
    JSON document) goes to stdout; the same document is written to ``*_summary.json``; with
    ``--html`` an offline report is written too, rail by rail, so only one rail's full result
    is in memory at a time."""
    from coppernicus.maps import safe_file_name
    from coppernicus.problems import find_problems
    from coppernicus.service import ServiceError, Session

    quiet = args.json
    stem = safe_file_name(Path(args.project).stem)
    out_dir = Path(args.out) if args.out else Path(args.project).resolve().parent / "results"
    html_path = None
    if args.html is not None or args.open:
        html_path = Path(args.html) if args.html else out_dir / f"{stem}_report.html"
    session = Session(out_dir=out_dir, max_results=1)        # one rail's arrays at a time
    report = {}

    def on_board(info, n_rails, n_layers):
        if not quiet:
            print(f"Board: {info['name']} ({len(info['layers'])} copper layers)")
            for w in info["warnings"]:
                print(f"  board warning: {w}")
        if html_path is not None:
            from coppernicus.report import ReportBuilder
            report["builder"] = ReportBuilder(
                title=f"Coppernicus report: {Path(args.project).name}", board_name=info["name"],
                outline=session.board_outline(info["board_id"]), project=Path(args.project).name,
                expected_layers=n_layers, top=max(args.top, 10))

    def on_rail(rid, summary):
        res, problems = session.rail_result(rid), session.rail_problems(rid)
        if not quiet:
            _print_rail(res, problems)
            for u in summary.get("not_judged", []):
                print(f"  NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
            for m in summary.get("maps", []):
                print(f"    map: {m}")
        builder = report.get("builder")
        if builder is not None:
            if not builder.settings:
                builder.settings = dict(summary.get("settings", {}))
            limits = session.rail_limits(rid)
            builder.add_rail(res, limits, find_problems(res, limits, top=builder.top),
                             estimated=session.rail_estimated(rid))

    stream = sys.stderr if quiet else sys.stdout       # with --json, stdout is only the document

    def progress(message):
        print(f"    … {message}", file=stream)
    try:
        doc = session.solve_project(args.project, rails=args.rail, top=args.top,
                                    maps_dir=None if args.no_maps else out_dir, stop_on_error=True,
                                    progress=progress if args.verbose else None,
                                    on_rail=on_rail, on_board=on_board,
                                    use_board_fields=False if args.no_board_fields else None)
    except ServiceError as e:
        if quiet:
            _print_json({"error": e.to_dict()})
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"{stem}_summary.json"
    doc["summary_file"] = summary_path.as_posix()      # "/" in the document on every system
    size_warning = None
    if "builder" in report:
        doc["report_file"] = report["builder"].write(html_path).as_posix()
        size_warning = report["builder"].size_warning(html_path)
        if size_warning:
            doc["warnings"].append(size_warning)
    summary_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    if quiet:
        _print_json(doc)
    else:
        for rail in doc["rails"]:
            if "skipped" in rail:
                print(f"\nRail {rail['net']}: skipped. {rail['skipped']['message']}; "
                      f"{rail['skipped']['hint']}.")
                for u in rail.get("not_judged", []):
                    print(f"  NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
        for u in doc.get("not_judged", []):
            if not u.get("rail"):                  # of the board, not of a rail (its temperature)
                print(f"NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
        print(f"Summary: {summary_path}")
        if "report_file" in doc:
            print(f"Report: {doc['report_file']}")
        if size_warning:
            print(f"warning: {size_warning}")
    if args.open and "report_file" in doc:
        import webbrowser
        webbrowser.open(Path(doc["report_file"]).resolve().as_uri())
    return _exit_for(doc)


def _cmd_solve_tree(args) -> int:
    """``solve --tree``: every rail and ground of the project solved together (regulator input
    currents, ground return). Prints the VRM, load and group tables, or with ``--json`` the
    :func:`coppernicus.power_tree.tree_summary` document, which is also written to
    ``<project>_tree_summary.json``. Exit codes as ``solve``."""
    from coppernicus import board_fields
    from coppernicus.engine import Board, EngineError
    from coppernicus.engine.kicad_fields import read_board_fields
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.maps import safe_name
    from coppernicus.power_tree import rating_origins, tree_summary, tree_text
    from coppernicus.project import Project, ProjectError
    from coppernicus.service.session import FIELD_HINT

    quiet = args.json

    def fail(code, message, hint=""):
        if quiet:
            _print_json({"error": {"code": code, "message": message, "hint": hint}})
        print(f"error: {message}", file=sys.stderr)
        return EXIT_ERROR
    if args.rail:
        return fail("invalid_input", "--tree solves the whole project: --rail does not apply to it",
                    "leave groups out with engine_options: excluded_nets")
    if not Path(args.project).is_file():
        return fail("file_not_found", f"No such file: {args.project}")
    try:
        project = Project.load(args.project)
        board = Board.open(project.board_path())
    except (OSError, ProjectError) as e:
        return fail("invalid_project", str(e))
    except EngineError as e:
        return fail("board_unreadable", str(e))
    try:                                  # the KiCad footprint fields, as Session.solve_tree
        merged = board_fields.apply(project, lambda: read_board_fields(board.path),
                                    False if args.no_board_fields else None,
                                    ties=[(t.net_a, t.net_b) for t in board.net_ties()])
    except (board_fields.FieldError, EngineError) as e:
        return fail("invalid_board_field", str(e), FIELD_HINT)
    project = merged.project
    stream = sys.stderr if quiet else sys.stdout
    estimate = None
    if args.thermal or project.thermal.enabled:   # warn before a heavy electro-thermal solve
        from coppernicus.service.session import thermal_estimate
        estimate = thermal_estimate(board, project.thermal)
        print(f"Board temperature: {estimate['text']}", file=stream, flush=True)
        if estimate["exceeds_memory"]:
            print("warning: the board temperature's model could exceed this machine's memory; "
                  "a coarser grid (thermal: grid_mm) lowers it", file=sys.stderr, flush=True)
    try:
        tree = solve_project_tree(board, project, progress=(
            (lambda m: print(f"    … {m}", file=stream)) if args.verbose else None),
            thermal=True if args.thermal else None)
    except EngineError as e:
        return fail("solve_failed", str(e), "fix the project file or the KiCad fields (PI_GND, "
                    "PI_INPUT, PI_LINK...), then solve again")
    merged.annotate(tree)
    rating_origins(tree, merged)          # where each regulator's rating was written
    doc = tree_summary(tree, top=args.top)        # with each VRM's I max, the rating judged
    if estimate is not None:
        doc["thermal_estimate"] = estimate
    out_dir = Path(args.out) if args.out else Path(args.project).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / f"{safe_name(Path(args.project).stem)}_tree_summary.json"
    maps = [] if tree.thermal is None or args.no_maps else tree.thermal.maps   # board temperature
    pngs = [out_dir / f"{safe_name(Path(args.project).stem)}_temperature_{safe_name(m.layer_name)}.png"
            for m in maps]
    for m, png in zip(maps, pngs):
        from coppernicus.temperature import write_png
        write_png(tree.thermal, m.layer_name, png, board.outline())
    if pngs:                                   # as the per-rail document's "maps"
        doc["maps"] = [png.as_posix() for png in pngs]
    if args.html is not None or args.open:     # one page: every group, the VRM and load tables
        from coppernicus.report import write_tree_report
        html_path = (Path(args.html) if args.html else
                     out_dir / f"{safe_name(Path(args.project).stem)}_tree_report.html")
        doc["report_file"] = write_tree_report(
            tree, doc, html_path, outline=board.outline(), board_name=board.name,
            project=Path(args.project).name, top=max(args.top, 10),
            settings=dict(vars(project.settings))).as_posix()
    doc["summary_file"] = summary_path.as_posix()      # "/" in the document on every system
    summary_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    if quiet:
        _print_json(doc)
    else:
        print(f"Board: {board.name} · power tree: {len(tree.groups)} groups solved "
              f"in {tree.stats['seconds']:.1f} s")
        print("\n".join(tree_text(doc)))
        print("".join(f"Map: {png}\n" for png in pngs) + f"Summary: {summary_path}")
        if "report_file" in doc:
            print(f"Report: {doc['report_file']}")
    if args.open and "report_file" in doc:
        import webbrowser
        webbrowser.open(Path(doc["report_file"]).resolve().as_uri())
    return _exit_for(doc)


def _cmd_ac(args) -> int:
    """``ac PROJECT``: the open-loop PDN impedance |Z|(f) at the project's probes, rail by rail
    (every rail with a probe, or each ``--rail``): a CSV and a PNG per rail and
    ``<project>_ac_summary.json`` in ``--out`` (default: results/ next to the project file).
    Prints the report, or with ``--json`` the summary. Exit codes as ``solve``."""
    from coppernicus import board_fields
    from coppernicus.engine import Board, EngineError
    from coppernicus.engine.ac import probe_rails, resolve_rail, solve_ac_rail
    from coppernicus.engine.kicad_fields import read_board_fields
    from coppernicus.impedance import ac_summary, ac_text, unsolved_entries, write_csv, write_png
    from coppernicus.maps import safe_file_name, safe_name
    from coppernicus.project import Project, ProjectError
    from coppernicus.service.session import FIELD_HINT

    quiet = args.json
    hint = "fix the probes, capacitor models or ac: block of the project file, then solve again"

    def fail(code, message, hint=""):
        if quiet:
            _print_json({"error": {"code": code, "message": message, "hint": hint}})
        print(f"error: {message}".replace("\n", " "), file=sys.stderr)
        return EXIT_ERROR
    if not Path(args.project).is_file():
        return fail("file_not_found", f"No such file: {args.project}")
    try:
        project = Project.load(args.project)
        board = Board.open(project.board_path())
    except (OSError, ProjectError) as e:
        return fail("invalid_project", str(e))
    except EngineError as e:
        return fail("board_unreadable", str(e))
    try:                                  # the KiCad footprint fields, as the other solves
        merged = board_fields.apply(project, lambda: read_board_fields(board.path),
                                    ties=[(t.net_a, t.net_b) for t in board.net_ties()])
    except (board_fields.FieldError, EngineError) as e:
        return fail("invalid_board_field", str(e), FIELD_HINT)
    project = merged.project
    stream = sys.stderr if quiet else sys.stdout
    progress = (lambda m: print(f"    … {m}", file=stream)) if args.verbose else None
    if not project.probes:
        return fail("invalid_input", "No probe in the project: nothing to solve",
                    "add a probe (probes: in the project file) on a rail that has a source")
    try:
        rails_of = probe_rails(board, project)
    except EngineError as e:
        return fail("solve_failed", str(e), hint)
    not_solved, warnings = [], []
    if args.rail:                         # the rails asked for; other probes left as chosen
        try:                              # each net to the rail it is solved with, once
            rails = list(dict.fromkeys(resolve_rail(board, project, net) for net in args.rail))
        except EngineError as e:
            return fail("solve_failed", str(e), hint)
    else:
        reached = set(rails_of.values())
        rails = [r.net for r in project.rails if r.net in reached]
        if not rails:
            return fail("invalid_input", "No rail with a source reaches the probes: " + ", ".join(
                f"{p.name} on {p.net}" for p in project.probes),
                "add the source of their rail, or a link joining their net to a rail that has one")
        not_solved, warnings = unsolved_entries(project.probes, rails_of)
    stem = safe_file_name(Path(args.project).stem)
    out_dir = Path(args.out) if args.out else Path(args.project).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    results, files = [], {}
    for net in rails:
        try:                              # its cost first: a heavy solve is never a surprise
            from coppernicus.service.session import ac_estimate
            estimate = ac_estimate(board, project, net)
        except EngineError as e:
            return fail("solve_failed", str(e), hint)
        print(f"Impedance of {net}: {estimate['text']}", file=stream, flush=True)
        if estimate["exceeds_memory"]:
            print(f"warning: the AC solve of {net} could exceed this machine's memory; a coarser "
                  "AC mesh (ac: max_edge_mm) lowers it", file=sys.stderr, flush=True)
        try:
            res = solve_ac_rail(board, project, net, progress=progress)
        except EngineError as e:
            return fail("solve_failed", str(e), hint)
        merged.annotate(res)
        csv_path = out_dir / f"{stem}_{safe_name(res.rail)}_impedance.csv"
        png_path = csv_path.with_suffix(".png")
        files[res.rail] = (write_csv(res, csv_path).as_posix(), write_png(res, png_path).as_posix())
        results.append(res)
    doc = ac_summary(results, files, not_solved, warnings)
    summary_path = out_dir / f"{stem}_ac_summary.json"
    doc["summary_file"] = summary_path.as_posix()      # "/" in the document on every system
    if args.html is not None:                  # one offline page: each rail's |Z|(f) and probes
        from coppernicus.report import write_ac_report
        html_path = Path(args.html) if args.html else out_dir / f"{stem}_ac_report.html"
        doc["report_file"] = write_ac_report(doc, html_path, project=Path(args.project).name,
                                             board_name=board.name).as_posix()
    summary_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    if quiet:
        _print_json(doc)
    else:
        print(f"Board: {board.name}")
        print("\n".join(ac_text(doc)))
        print(f"Summary: {summary_path}")
        if "report_file" in doc:
            print(f"Report: {doc['report_file']}")
    return _exit_for(doc)


def _cmd_noise(args) -> int:
    """``noise PROJECT``: the ripple and coupled noise of the project's aggressors (every one,
    or each ``--aggressor``): per aggressor a CSV, a waveforms PNG and H / E field-map PNGs,
    and ``<project>_noise_summary.json`` in ``--out`` (default: results/ next to the project
    file). Prints the report, or with ``--json`` the summary. Exit codes as ``solve``."""
    from coppernicus import board_fields
    from coppernicus.coupled_noise import noise_summary, noise_text, write_files
    from coppernicus.engine import Board, EngineError
    from coppernicus.engine.kicad_fields import read_board_fields
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.maps import safe_file_name, safe_name
    from coppernicus.project import Project, ProjectError
    from coppernicus.service.session import FIELD_HINT, noise_estimate

    quiet = args.json
    hint = "fix the aggressors, victims or noise: block of the project file, then solve again"

    def fail(code, message, hint=""):
        if quiet:
            _print_json({"error": {"code": code, "message": message, "hint": hint}})
        print(f"error: {message}".replace("\n", " "), file=sys.stderr)
        return EXIT_ERROR
    if not Path(args.project).is_file():
        return fail("file_not_found", f"No such file: {args.project}")
    try:
        project = Project.load(args.project)
        board = Board.open(project.board_path())
    except (OSError, ProjectError) as e:
        return fail("invalid_project", str(e))
    except EngineError as e:
        return fail("board_unreadable", str(e))
    try:                                  # the KiCad footprint fields, as the other solves
        merged = board_fields.apply(project, lambda: read_board_fields(board.path),
                                    False if args.no_board_fields else None,
                                    ties=[(t.net_a, t.net_b) for t in board.net_ties()])
    except (board_fields.FieldError, EngineError) as e:
        return fail("invalid_board_field", str(e), FIELD_HINT)
    project = merged.project
    if not project.aggressors:
        return fail("invalid_input", "No aggressor in the project: nothing to solve",
                    "add an aggressor (aggressors: in the project file) on a switcher source")
    known = [a.name for a in project.aggressors]
    names = list(dict.fromkeys(args.aggressor or known))
    unknown = [n for n in names if n not in known]
    if unknown:
        return fail("invalid_input", f"No aggressor named {', '.join(unknown)}",
                    f"the project's aggressors: {', '.join(known)}")
    stream = sys.stderr if quiet else sys.stdout
    progress = (lambda m: print(f"    … {m}", file=stream)) if args.verbose else None
    stem = safe_file_name(Path(args.project).stem)
    out_dir = Path(args.out) if args.out else Path(args.project).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    outline = board.outline()
    results, files, estimates = [], {}, {}
    for name in names:
        try:                              # its cost first: a heavy solve is never a surprise
            estimates[name] = estimate = noise_estimate(board, project, name, progress,
                                                        fields_rails=merged.from_fields)
        except EngineError as e:
            return fail("solve_failed", str(e), hint)
        print(f"Noise of {name}: {estimate['text']}", file=stream, flush=True)
        if estimate["exceeds_memory"]:
            print(f"warning: the noise solve of {name} could exceed this machine's memory; a coarser "
                  "AC mesh (ac: max_edge_mm) or fewer field solves (noise: field_per_decade) lowers "
                  "it", file=sys.stderr, flush=True)
        try:
            res = solve_aggressor(board, project, name, progress=progress,
                                  fields_rails=merged.from_fields)
        except EngineError as e:
            return fail("solve_failed", str(e), hint)
        merged.annotate(res)
        files[name] = write_files(res, out_dir / f"{stem}_{safe_name(name)}_noise", outline)
        results.append(res)
    doc = noise_summary(results, files)
    for a in doc["aggressors"]:
        a["estimate"] = estimates.get(a["aggressor"])
    summary_path = out_dir / f"{stem}_noise_summary.json"
    doc["summary_file"] = summary_path.as_posix()      # "/" in the document on every system
    if args.html is not None:            # one offline page: each aggressor's waveforms and victims
        from coppernicus.report import write_noise_report
        html_path = Path(args.html) if args.html else out_dir / f"{stem}_noise_report.html"
        doc["report_file"] = write_noise_report(doc, html_path, project=Path(args.project).name,
                                                board_name=board.name).as_posix()
    summary_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    if quiet:
        _print_json(doc)
    else:
        print(f"Board: {board.name}")
        print("\n".join(noise_text(doc)))
        print(f"Summary: {summary_path}")
        if "report_file" in doc:
            print(f"Report: {doc['report_file']}")
    return _exit_for(doc)


def _cmd_tolerance(args) -> int:
    """``tolerance PROJECT``: each load's tolerance stack-up (Task 11): the power tree (the DC
    drop on the worst pins), then, only where a load with a tolerance needs it, one AC solve
    per rail for KiPI's transient estimate at its probe (shown, never judged; only with a
    bandwidth given) and one noise solve per aggressor listing its probe (the ripple), each
    cost printed first; ``--skip-ac`` / ``--skip-noise`` leave those parts missing. Writes
    ``<project>_tolerance_summary.json`` and ``<project>_tolerance.csv`` into ``--out``
    (default: results/ next to the project file). Prints the report, or with ``--json`` the
    summary. Exit codes as ``solve``."""
    import time

    from coppernicus import board_fields
    from coppernicus.engine import Board, EngineError
    from coppernicus.engine.ac import peak_rss_bytes, probe_rails, solve_ac_rail
    from coppernicus.engine.kicad_fields import read_board_fields
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.tolerance import load_probes, loads_by_name
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.maps import safe_file_name
    from coppernicus.project import (
        LOAD_PART_KEYS,
        LOAD_TOLERANCE_KEYS,
        SOURCE_ACCURACY_KEYS,
        Project,
        ProjectError,
    )
    from coppernicus.service.session import FIELD_HINT, ac_estimate, noise_estimate, thermal_estimate
    from coppernicus.tolerance import compose, tolerance_summary, tolerance_text, write_csv

    quiet = args.json
    t0 = time.perf_counter()

    def fail(code, message, hint=""):
        if quiet:
            _print_json({"error": {"code": code, "message": message, "hint": hint}})
        print(f"error: {message}".replace("\n", " "), file=sys.stderr)
        return EXIT_ERROR
    if not Path(args.project).is_file():
        return fail("file_not_found", f"No such file: {args.project}")
    try:
        loaded = Project.load(args.project)
        board = Board.open(loaded.board_path())
    except (OSError, ProjectError) as e:
        return fail("invalid_project", str(e))
    except EngineError as e:
        return fail("board_unreadable", str(e))
    try:                                  # the KiCad footprint fields, as solve --tree
        merged = board_fields.apply(loaded, lambda: read_board_fields(board.path),
                                    False if args.no_board_fields else None,
                                    ties=[(t.net_a, t.net_b) for t in board.net_ties()])
    except (board_fields.FieldError, EngineError) as e:
        return fail("invalid_board_field", str(e), FIELD_HINT)
    project = merged.project
    warnings, dropped = list(merged.warnings), []
    for net in merged.from_fields:        # the fields' rail replaces the project's terminals
        own = loaded.rail(net)
        if own is None:
            continue
        keys = ((own.loads, LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS), (own.sources, SOURCE_ACCURACY_KEYS))
        refs = [t.ref for ts, k in keys for t in ts if any(getattr(t, x) is not None for x in k)]
        if refs:
            why = (f"rail {net} comes from the board's KiCad footprint fields, which hold no tolerance "
                   "yet: set override_board_fields: true on the rail to use the project's")
            warnings.append(f"Rail {net} comes from the board's KiCad footprint fields: the tolerances, "
                            f"accuracy and entered parts of {', '.join(refs)} in the project file are not "
                            "used (no KiCad field holds them yet); set override_board_fields: true on the "
                            "rail to use the project's")
            # A tolerance the user set that cannot be judged: not judged (exit 3), never a pass.
            dropped += [{"item": ld.label, "net": net, "limit": key, "reason": why}
                        for ld in own.loads for key in LOAD_TOLERANCE_KEYS if getattr(ld, key) is not None]
    stream = sys.stderr if quiet else sys.stdout
    progress = (lambda m: print(f"    … {m}", file=stream)) if args.verbose else None
    estimates = []
    if project.thermal.enabled:           # warn before a heavy electro-thermal solve
        estimate = thermal_estimate(board, project.thermal)
        estimates.append({"solve": "board temperature", **estimate})
        print(f"Board temperature: {estimate['text']}", file=stream, flush=True)
        if estimate["exceeds_memory"]:
            print("warning: the board temperature's model could exceed this machine's memory; "
                  "a coarser grid (thermal: grid_mm) lowers it", file=sys.stderr, flush=True)
    try:
        tree = solve_project_tree(board, project, progress=progress)
        pairs = load_probes(board, project)
        rails_of = probe_rails(board, project)
    except EngineError as e:
        return fail("solve_failed", str(e), "fix the project file or the KiCad fields, then solve again")
    needed = {pairs[n] for n, (_net, ld) in loads_by_name(project).items()
              if ld.has_tolerance() and pairs.get(n)}             # the probes of loads judged
    ac_notes, noise_notes, rails = {}, {}, []
    for p in project.probes:              # AC only for KiPI's estimate: a step and a bandwidth given
        rail = rails_of.get(p.name)
        if not (p.step_a and p.slew_a_per_us) or rail is None:
            continue
        if p.name not in needed:
            ac_notes[p.name] = "not solved in AC, as no load with a tolerance needs it"
            continue
        source = project.rail(rail).sources[0]
        if project.ac.bandwidth_hz.get(source.label, project.ac.bandwidth_hz.get(source.ref)) is None:
            continue
        if args.skip_ac:
            ac_notes[p.name] = "--skip-ac left it out"
        elif rail not in rails:
            rails.append(rail)
    impedance = []
    for net in rails:
        probes_here = [n for n in needed if rails_of.get(n) == net]
        try:                              # its cost first: a heavy solve is never a surprise
            estimate = ac_estimate(board, project, net)
            estimates.append({"solve": f"AC {net}", **estimate})
            print(f"Impedance of {net}: {estimate['text']}", file=stream, flush=True)
            if estimate["exceeds_memory"]:
                print(f"warning: the AC solve of {net} could exceed this machine's memory; a coarser "
                      "AC mesh (ac: max_edge_mm) lowers it", file=sys.stderr, flush=True)
            impedance.append(solve_ac_rail(board, project, net, progress=progress, transient=True))
        except EngineError as e:          # only KiPI's estimate is lost: shown, never judged
            warnings.append(f"AC solve of {net} for KiPI's transient estimate failed: {e}")
            ac_notes.update({n: f"the AC solve of {net} failed" for n in probes_here})
    noise = []
    for a in project.aggressors:
        if not set(a.ripple_probes) & needed:
            noise_notes[a.name] = "no load with a tolerance needs it"
            continue
        if args.skip_noise:
            noise_notes[a.name] = "--skip-noise"
            continue
        try:                              # its cost first, as coppernicus noise
            estimate = noise_estimate(board, project, a.name, progress, fields_rails=merged.from_fields)
            estimates.append({"solve": f"noise {a.name}", **estimate})
            print(f"Noise of {a.name}: {estimate['text']}", file=stream, flush=True)
            if estimate["exceeds_memory"]:
                print(f"warning: the noise solve of {a.name} could exceed this machine's memory; a "
                      "coarser AC mesh (ac: max_edge_mm) or fewer field solves (noise: "
                      "field_per_decade) lowers it", file=sys.stderr, flush=True)
            res = solve_aggressor(board, project, a.name, progress=progress, fields_rails=merged.from_fields)
            merged.annotate(res)
            noise.append(res)
        except EngineError as e:          # its ripple is missing: not judged, never a pass
            warnings.append(f"Noise solve of {a.name} failed: {e}")
            noise_notes[a.name] = "its noise solve failed"
    merged.annotate(tree)
    result = compose(project, tree, impedance, noise, pairs, ac_notes, noise_notes, warnings, dropped)
    merged.annotate(result)               # the KiCad-field notes, as the other commands
    result.estimates = estimates
    peak = peak_rss_bytes()
    result.stats = {"seconds": time.perf_counter() - t0, "tree_seconds": tree.stats.get("seconds"),
                    "ac_rails": len(impedance), "aggressors": len(noise),
                    "memory_peak_mb": peak / 1e6 if peak else None}
    stem = safe_file_name(Path(args.project).stem)
    out_dir = Path(args.out) if args.out else Path(args.project).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = write_csv(result, out_dir / f"{stem}_tolerance.csv")
    doc = tolerance_summary(result, csv_path.as_posix())
    summary_path = out_dir / f"{stem}_tolerance_summary.json"
    doc["summary_file"] = summary_path.as_posix()      # "/" in the document on every system
    summary_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False),
                            encoding="utf-8")
    if quiet:
        _print_json(doc)
    else:
        print(f"Board: {board.name}")
        print("\n".join(tolerance_text(doc)))
        print(f"Summary: {summary_path}")
    return _exit_for(doc)


def _print_rail(res, problems):
    from coppernicus.problems import FAIL, max_drop_text
    others = [n for n in res.nets if n != res.net]
    nets = f" (with {', '.join(others)} via net ties)" if others else ""
    print(f"\nRail {res.net}{nets}: solved in {res.stats['seconds']:.1f} s, "
          f"{int(res.stats['triangles']):,} triangles")
    for t in res.sources:
        print(f"  source {t.name:14s} {t.v_max:8.4f} V  delivers {t.current:7.3f} A")
    word = "rise" if res.is_rise() else "drop"  # a net at 0 V: the return current lifts it
    for t in res.loads:
        pct = res.drop_pct(t)                   # NaN on a 0 V net: no percentage there
        pct_text = f"{pct:5.2f} %" if math.isfinite(pct) else "  n/a"
        print(f"  load   {t.name:14s} {res.worst_v(t):8.4f} V  draws    {t.current:7.3f} A  "
              f"{word} {res.drop(t) * 1e3:8.2f} mV ({pct_text})")
    shares = res.loss_breakdown()
    total = sum(shares.values()) or 1.0
    split = ", ".join(f"{k} {v / total:.0%}" for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                      if v / total >= 0.01)
    print(f"  {max_drop_text(res)} · copper loss {res.power_loss * 1e3:.3g} mW ({split})")
    print("  problems (fails first, then ranked):")
    for p in problems:
        tag = "FAIL" if p.severity == FAIL else "    "
        where = f" [{p.layer}]" if p.layer else ""
        print(f"    {tag} {p.title}{where} @ ({p.x:.2f}, {p.y:.2f}) mm")
    for a in res.assumptions:
        print(f"  assumption: {a}")
    for w in res.warnings:
        print(f"  warning: {w}")


def _cmd_mcp(args) -> int:
    from coppernicus.mcp_server import run_stdio
    return run_stdio(out_dir=args.out, log_file=args.log_file, log_level=args.log_level,
                     max_results=args.max_results, max_result_mb=args.max_result_mb)


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
    p.add_argument("--solve", dest="solve_net", metavar="NET", help="solve this rail of the project at start")
    p.add_argument("--field", choices=["drop", "voltage", "current"], help="map to show after --solve")
    p.add_argument("--layer", help="layer to show after --solve (default: the one with the most copper loss)")
    # Test hook (hidden): with --solve, save window and view images, then quit.
    p.add_argument("--screenshot", help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_gui)

    p = sub.add_parser("nets", help="list power and ground nets of a board")
    p.add_argument("board")
    p.add_argument("--all", action="store_true", help="list every net")
    p.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")
    p.set_defaults(func=_cmd_nets)

    p = sub.add_parser("parts", help="list parts and pins on a net")
    p.add_argument("board")
    p.add_argument("net")
    p.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")
    p.set_defaults(func=_cmd_parts)

    p = sub.add_parser("fields", help="list a board's KiCad footprint fields (PI_*) and the rails they make")
    p.add_argument("board", help="a KiCad board file (.kicad_pcb); with --propose, a project file")
    p.add_argument("--propose", action="store_true",
                   help="print the PI_* fields that describe the project's rails, to paste into KiCad")
    p.add_argument("--json", action="store_true", help="print JSON (for scripts and agents)")
    p.set_defaults(func=_cmd_fields)

    p = sub.add_parser("solve", help="solve the rails of a project file", epilog=EXIT_CODES)
    p.add_argument("project")
    p.add_argument("--out", help="folder for maps and summary (default: results/ next to the project file)")
    p.add_argument("--rail", action="append", help="solve only this net (repeatable)")
    p.add_argument("--no-maps", action="store_true", help="skip the PNG maps")
    p.add_argument("-v", "--verbose", action="store_true", help="show the engine's progress")
    p.add_argument("--json", action="store_true",
                   help="print a JSON summary on stdout instead of the report (for scripts and agents)")
    p.add_argument("--top", type=int, default=5, metavar="N",
                   help="current-crowding spots and vias listed per rail, each (default: 5; every "
                        "load is always listed)")
    p.add_argument("--html", nargs="?", const="", default=None, metavar="PATH",
                   help="also write an offline HTML report (default path: <out>/<project>_report.html)")
    p.add_argument("--open", action="store_true", help="open the HTML report in the browser (implies --html)")
    p.add_argument("--no-board-fields", action="store_true",
                   help="ignore the board's KiCad footprint fields (PI_RAIL, PI_SOURCE, PI_LOAD, ...), "
                        "as use_board_fields: false in the project file does")
    p.set_defaults(func=_cmd_solve)
    # Power tree (Task 7): every rail and ground solved together; replaces the command run.
    p.add_argument("--tree", action="store_const", dest="func", const=_cmd_solve_tree,
                   help="solve the whole power tree: rails and grounds together, regulator input "
                        "currents, ground return (prints VRM, load and group tables)")
    p.add_argument("--thermal", action="store_true",
                   help="the power tree with the board temperature (electro-thermal; implies --tree; "
                        "writes a temperature map per copper layer)")

    p = sub.add_parser("mcp", help="run the MCP server for AI agents (stdio; see README)")
    p.add_argument("--out", help="folder for maps, one session-... subfolder per run "
                                 "(default: a new folder under the system temp directory)")
    p.add_argument("--log-file", help="write the server log to this file (default: stderr)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="server log level (default: INFO)")
    p.add_argument("--max-results", type=int, default=8,
                   help="solved results kept in memory with their fields (default: 8)")
    p.add_argument("--max-result-mb", type=float, default=2048,
                   help="memory bound for kept results, MB (default: 2048)")
    p.set_defaults(func=_cmd_mcp)

    p = sub.add_parser("ac", help="AC PDN impedance |Z|(f) at the project's probes (CSV + PNG)")
    p.add_argument("project")
    p.add_argument("--out",
                   help="folder for CSV, PNG and summary (default: results/ next to the project file)")
    p.add_argument("--rail", action="append",
                   help="solve only this rail (repeatable; default: every rail with a probe)")
    p.add_argument("-v", "--verbose", action="store_true", help="show the engine's progress")
    p.add_argument("--json", action="store_true",
                   help="print a JSON summary on stdout (for scripts and agents)")
    p.add_argument("--html", nargs="?", const="", default=None, metavar="PATH",
                   help="also write an offline HTML page of the impedance (default path: "
                        "<out>/<project>_ac_report.html)")
    p.set_defaults(func=_cmd_ac)

    p = sub.add_parser("noise", help="DC-DC ripple and coupled noise of the project's aggressors",
                       epilog=EXIT_CODES)
    p.add_argument("project")
    p.add_argument("--aggressor", action="append",
                   help="solve only this aggressor (repeatable; default: every one)")
    p.add_argument("--out",
                   help="folder for CSV, PNG and summary (default: results/ next to the project file)")
    p.add_argument("--no-board-fields", action="store_true",
                   help="ignore the board's KiCad footprint fields (PI_*)")
    p.add_argument("--html", nargs="?", const="", default=None, metavar="PATH",
                   help="also write an offline HTML page of the ripple and noise (default path: "
                        "<out>/<project>_noise_report.html)")
    p.add_argument("-v", "--verbose", action="store_true", help="show the engine's progress")
    p.add_argument("--json", action="store_true",
                   help="print a JSON summary on stdout (for scripts and agents)")
    p.set_defaults(func=_cmd_noise)

    p = sub.add_parser("tolerance", help="each load's tolerance stack-up: DC drop and accuracy, ripple, "
                                         "transient", epilog=EXIT_CODES)
    p.add_argument("project")
    p.add_argument("--skip-ac", action="store_true",
                   help="no AC solve: KiPI's transient estimate (shown, never judged) is left out")
    p.add_argument("--skip-noise", action="store_true",
                   help="no noise solve: the ripple of the aggressors is left missing (not judged)")
    p.add_argument("--out",
                   help="folder for the CSV and summary (default: results/ next to the project file)")
    p.add_argument("--no-board-fields", action="store_true",
                   help="ignore the board's KiCad footprint fields (PI_*)")
    p.add_argument("-v", "--verbose", action="store_true", help="show the engines' progress")
    p.add_argument("--json", action="store_true",
                   help="print a JSON summary on stdout (for scripts and agents)")
    p.set_defaults(func=_cmd_tolerance)
    return ap


def _utf8_output() -> None:
    """Reports and JSON hold Ω, ρ, µ, ≤...: write stdout and stderr as UTF-8 even where the
    locale says otherwise (Windows pipes and files default to cp1252 and would fail)."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):        # a stream that cannot change: leave it
                pass


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["gui", *(argv or [])])
    args.func = _cmd_solve_tree if getattr(args, "thermal", False) else args.func
    if args.command != "mcp":                    # under MCP, stdout belongs to the SDK
        _utf8_output()
    from coppernicus.engine import EngineError
    from coppernicus.project import ProjectError
    try:
        return args.func(args)
    except (EngineError, ProjectError, FileNotFoundError) as e:
        code = ("invalid_project" if isinstance(e, ProjectError) else
                "file_not_found" if isinstance(e, FileNotFoundError) else "solve_failed")
        return _fail(args, code, str(e))
    except Exception as e:
        # A bug. Never let it exit 1, which means "a limit was exceeded" (CI reads the code).
        if getattr(args, "verbose", False):
            traceback.print_exc(file=sys.stderr)
        return _fail(args, "internal_error", f"{type(e).__name__}: {e}",
                     "this is a bug in Coppernicus: please report it with the command you ran",
                     prefix="internal error: ")


def _fail(args, code: str, message: str, hint: str = "", prefix: str = "") -> int:
    """Exit 2 with a one-line error on stderr and, under --json, the error document on stdout."""
    if getattr(args, "json", False):
        _print_json({"error": {"code": code, "message": message, "hint": hint}})
    print(f"error: {prefix}{message}".replace("\n", " "), file=sys.stderr)
    return EXIT_ERROR
