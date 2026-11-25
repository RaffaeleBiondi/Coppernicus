"""Continuous integration helper: solve every matching project file, then write a summary.

Used by the GitHub Action (``action.yml`` at the repository root), and usable from any CI:

    python -m coppernicus.ci solve --out DIR [--top N] [--no-html] "PATTERN" ["PATTERN" ...]
    python -m coppernicus.ci summary --out DIR [--artifact-url URL] > summary.md

``solve`` runs ``coppernicus solve PROJECT --json`` once per matching ``*.coppernicus.yml``,
each in its own process and into its own subfolder of DIR, never next to a board. It writes
``DIR/coppernicus-ci.json`` (every project's JSON document and exit code) and exits with
0 (no limit exceeded), 1 (a limit set in a project is exceeded), 3 (a limit that was set could
not judge something) or 2 (an error, or no project matched); precedence 2, 1, 3, 0.
``summary`` turns that file into Markdown for a job summary.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from coppernicus.maps import safe_file_name

INDEX = "coppernicus-ci.json"
EXIT_OK, EXIT_LIMIT, EXIT_ERROR, EXIT_NOT_JUDGED = 0, 1, 2, 3
# Which code wins when projects differ: an error, then a limit exceeded, then a limit that
# could not judge, then all clear.
PRECEDENCE = (EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK)


# ---------------------------------------------------------------------- solving

def find_projects(patterns: Sequence[str]) -> Tuple[List[Path], List[str]]:
    """Project files matching the patterns (globs, ``**`` allowed; one per line or
    argument), relative to the current folder, sorted and without duplicates; and the
    patterns that matched nothing (a mistyped path must not pass unnoticed)."""
    found: List[Path] = []
    unmatched: List[str] = []
    for raw in patterns:
        for pattern in (line.strip() for line in str(raw).splitlines()):
            if not pattern:
                continue
            hits = sorted(glob.glob(pattern, recursive=True)) if glob.has_magic(pattern) else (
                [pattern] if Path(pattern).is_file() else [])
            hits = [Path(h) for h in hits if Path(h).is_file()]
            if not hits:
                unmatched.append(pattern)
            found.extend(hits)
    return list(dict.fromkeys(found)), unmatched


def _subfolder(project: Path) -> str:
    return safe_file_name(project.with_suffix("").as_posix().replace("/", "_"))


def _board_folder(project: Path) -> Optional[Path]:
    try:
        from coppernicus.project import Project
        return Project.load(project).board_path().resolve().parent
    except Exception:                 # an invalid project: solve will report it
        return None


def solve_all(patterns: Sequence[str], out_dir: Path, top: int = 5, html: bool = True,
              tree: bool = False) -> Dict[str, Any]:
    """Solve every matching project; return the index written to ``out_dir/INDEX``. With
    ``tree``, each project is also solved as a whole power tree (``solve --tree --json``):
    its document goes into the entry's ``tree``, for the summary's tables; it does not set
    the exit code."""
    out_dir = Path(out_dir)
    projects, unmatched = find_projects(patterns)
    boards = {p: _board_folder(p) for p in projects}
    if out_dir.resolve() in {b for b in boards.values() if b is not None}:
        # The index itself would land next to a board.
        index = {"patterns": [str(p) for p in patterns], "projects": [], "unmatched": unmatched,
                 "exit_code": EXIT_ERROR, "verdict": "error", "top": top,
                 "error": {"code": "invalid_input", "message": f"{out_dir} holds a board file",
                           "hint": "choose an output folder away from the board files"}}
        return index
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: List[Dict[str, Any]] = []
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for project in projects:
        folder = out_dir / _subfolder(project)
        board_dir = boards[project]
        if board_dir is not None and folder.resolve() == board_dir:
            entries.append({"project": project.as_posix(), "exit_code": EXIT_ERROR,
                            "error": {"code": "invalid_input",
                                      "message": f"{folder} is the board's own folder",
                                      "hint": "choose an output folder away from the board files"}})
            continue
        cmd = [sys.executable, "-m", "coppernicus", "solve", str(project), "--json", "--out", str(folder),
               "--top", str(top)]
        if html:
            cmd.append("--html")
        print(f"coppernicus: solving {project.as_posix()}", file=sys.stderr, flush=True)
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
        if run.stderr:
            sys.stderr.write(run.stderr)
        try:
            doc = json.loads(run.stdout)
        except json.JSONDecodeError:
            doc = {"error": {"code": "internal_error",
                             "message": f"coppernicus solve exited with {run.returncode} and no JSON",
                             "hint": "see the log above"}}
        entry: Dict[str, Any] = {"project": project.as_posix(), "exit_code": run.returncode,
                                 "folder": folder.as_posix()}
        if "error" in doc:
            # A crash also exits with 1 (Python's code for an uncaught exception), which must
            # never read as "limit exceeded": any run without a result is an error.
            entry["error"] = doc["error"]
            entry["exit_code"] = EXIT_ERROR
        else:
            entry["doc"] = doc
        if tree and "doc" in entry:
            entry["tree"] = _solve_tree(project, folder, top, env)
        entries.append(entry)
    exit_code = EXIT_ERROR if unmatched else overall_exit_code(entries)
    verdict = "error" if unmatched else overall_verdict(entries)
    index = {"patterns": [str(p) for p in patterns], "projects": entries, "unmatched": unmatched,
             "exit_code": exit_code, "verdict": verdict, "top": top}
    if unmatched:
        index["error"] = {"code": "file_not_found",
                          "message": ("No project file matched: " + ", ".join(unmatched)),
                          "hint": "give the path or a glob of *.coppernicus.yml files, relative to the "
                                  "repository root"}
    (out_dir / INDEX).write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    return index


def overall_exit_code(entries: Sequence[Dict[str, Any]]) -> int:
    """The code of the whole run, by :data:`PRECEDENCE`; none, or an unknown code, is an error."""
    codes = {e["exit_code"] for e in entries}
    if not entries or not codes <= set(PRECEDENCE):
        return EXIT_ERROR
    return next(c for c in PRECEDENCE if c in codes)


def overall_verdict(entries: Sequence[Dict[str, Any]]) -> str:
    """``error`` if a project could not be solved, else ``fail`` if a limit is exceeded,
    ``pass`` if every project was judged and passed, ``not_judged`` if none had limits."""
    if not entries or any("error" in e for e in entries):
        return "error"
    verdicts = [e["doc"].get("verdict") for e in entries]
    if "error" in verdicts:
        return "error"
    if "fail" in verdicts:
        return "fail"
    if any(e["doc"].get("not_judged") for e in entries):    # a set limit could not judge
        return "not_judged"
    if all(v == "pass" for v in verdicts):
        return "pass"
    return "pass_partially_judged" if any(v in ("pass", "pass_partially_judged") for v in verdicts) \
        else "not_judged"


# ---------------------------------------------------------------------- the summary

def _solve_tree(project: Path, folder: Path, top: int, env) -> Dict[str, Any]:
    """The power-tree document of a project (``coppernicus solve --tree --json``), or its
    error."""
    cmd = [sys.executable, "-m", "coppernicus", "solve", str(project), "--tree", "--json",
           "--out", str(folder), "--top", str(top)]
    print(f"coppernicus: solving the power tree of {project.as_posix()}", file=sys.stderr, flush=True)
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if run.stderr:
        sys.stderr.write(run.stderr)
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError:
        return {"error": {"code": "internal_error",
                          "message": f"coppernicus solve --tree exited with {run.returncode} and no JSON",
                          "hint": "see the log above"}}


def tree_markdown(project: str, doc: Dict[str, Any]) -> List[str]:
    """The VRM and load tables of a power-tree document, for the job summary."""
    lines = ["", f"### Power tree: `{_cell(project)}`", ""]
    if "error" in doc:
        return lines + [f"**Error:** {_cell(doc['error'].get('message', ''))}"]
    lines += [f"Verdict: **{_cell(doc.get('verdict'))}**. {_cell(doc.get('verdict_note', ''))}", "",
              "| VRM | Kind | Group | V set (V) | V out (V) | I out (A) | I max (A) | V in (V) "
              "| I in (A) | Loss (mW) | |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for v in doc.get("vrms", []):
        loss = v.get("p_loss_w")
        loss_mw = loss * 1e3 if loss is not None else None
        lines.append(f"| {_cell(v['name'])} | {_cell(v['kind'])} | {_cell(v['group'])} | "
                     f"{_num(v.get('v_set_v'), '.4g')} | {_num(v.get('v_out_v'), '.5g')} | "
                     f"{_num(v.get('i_out_a'), '.4g')} | {_num(v.get('i_max_a'), '.4g')} | "
                     f"{_num(v.get('v_in_v'), '.5g')} | "
                     f"{_num(v.get('i_in_a'), '.4g')} | {_num(loss_mw, '.3g')} | "
                     f"{_cell(v.get('regulation') or '')} |")
    lines += ["", "| Load | Group | I (A) | Nominal (V) | Across (V) | Drop (mV) | Drop (%) | Verdict |",
              "|---|---|---:|---:|---:|---:|---:|---|"]
    for ld in doc.get("loads", []):
        ex = ld.get("exceeds_drop_limit")
        verdict = ld.get("status") or ("**FAIL**" if ex else "pass" if ex is False else "not judged")
        lines.append(f"| {_cell(ld['name'])} | {_cell(ld['group'])} | {_num(ld.get('current_a'), '.4g')} | "
                     f"{_num(ld.get('nominal_v'), '.4g')} | {_num(ld.get('v_across_v'), '.5g')} | "
                     f"{_num(ld.get('drop_mv'), '.3g')} | {_num(ld.get('drop_pct'), '.3g')} | "
                     f"{_cell(verdict)} |")
    return lines


def _cell(text: Any) -> str:
    """Text safe inside a Markdown table cell."""
    return (str(text).replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", " "))


def _num(x: Any, fmt: str) -> str:
    return "–" if x is None else format(x, fmt)


def _limits_text(limits: Dict[str, Any]) -> str:
    parts = []
    if limits.get("max_drop_pct") is not None:
        parts.append(f"drop ≤ {limits['max_drop_pct']:g} %")
    if limits.get("max_current_density_a_mm2") is not None:
        parts.append(f"|J| ≤ {limits['max_current_density_a_mm2']:g} A/mm²")
    if limits.get("max_via_current_a") is not None:
        parts.append(f"via ≤ {limits['max_via_current_a']:g} A")
    if limits.get("max_ground_rise_mv") is not None:
        parts.append(f"ground rise ≤ {limits['max_ground_rise_mv']:g} mV")
    return ", ".join(parts)


def _verdict_cell(rail: Dict[str, Any]) -> str:
    v, lim = rail.get("verdict"), _limits_text(rail.get("limits") or {})
    if v == "fail":
        return f"**FAIL** ({_cell(lim)})"
    if v == "pass":
        return f"PASS ({_cell(lim)})"
    if rail.get("not_judged"):
        return f"NOT JUDGED ({_cell(lim)})"
    return "not judged (no limits)"


def summary_markdown(index: Dict[str, Any], artifact_url: str = "", problems_per_rail: int = 5) -> str:
    """The job summary: one row per rail, then each rail's top problems, the assumptions and
    where the files are."""
    lines = ["## Coppernicus: DC power integrity", ""]
    entries = index.get("projects", [])
    if index.get("error"):
        lines += [f"**Error:** {_cell(index['error']['message'])}. {_cell(index['error']['hint'])}.", ""]
        if not entries:
            return "\n".join(lines) + "\n"
    verdict = index.get("verdict", "error")
    headline = {"fail": "**FAIL**: a limit set in a project file is exceeded.",
                "pass": "PASS: every rail is within the limits set in its project file.",
                "not_judged": "Not judged: no limits are set in the project files, or a limit set "
                              "could not judge some items (listed below); the numbers are ranked "
                              "only.",
                "pass_partially_judged": "PASS where limits judge; some rails have no limits, or "
                                         "items a limit could not judge (listed below).",
                "error": "**Error**: a project could not be solved."}.get(verdict, verdict)
    lines += [headline, "",
              "| Project | Rail | Worst load | Worst of the copper | Max drop (%) | Copper loss (mW) "
              "| Verdict |",
              "|---|---|---|---:|---:|---:|---|"]
    assumptions: List[str] = []
    details: List[str] = []
    field_warnings: List[str] = []           # from the KiCad footprint fields: shown in full
    for e in entries:
        project = f"`{_cell(e['project'])}`"
        if "error" not in e:
            field_warnings += [w for w in e["doc"].get("board_fields_warnings", [])
                               if w not in field_warnings]
        if "error" in e:
            err = e["error"]
            lines.append(f"| {project} | – | – | – | – | – | **error**: {_cell(err.get('message', ''))} |")
            continue
        for rail in e["doc"].get("rails", []):
            if "error" in rail:
                lines.append(f"| {project} | {_cell(rail.get('net', '?'))} | – | – | – | – | "
                             f"**error**: {_cell(rail['error'].get('message', ''))} |")
                continue
            if "skipped" in rail:      # no loads, feeds nothing: not solved, not an error
                unjudged = rail.get("not_judged") or []
                lines.append(f"| {project} | {_cell(rail.get('net', '?'))} | – | – | – | – | "
                             "skipped: nothing to draw" + (" (NOT JUDGED)" if unjudged else "") + " |")
                if unjudged:            # its limits judged nothing: say so, never a silent pass
                    details += ["", f"**{_cell(rail['net'])}** ({_cell(e['project'])}): not solved", ""]
                    details += [f"- NOT JUDGED {_cell(u['item'])} ({_cell(u['limit'])}): "
                                f"{_cell(u['reason'])}" for u in unjudged]
                continue
            worst = next((ld for ld in rail.get("loads", [])
                          if ld.get("name") == rail.get("worst_load")), None)
            worst_text = (f"{_cell(worst['name'])}: {_num(worst.get('drop_mv'), '.3g')} mV"
                          if worst else "–")
            loss = rail.get("power_loss_w")
            loss_mw = _num(loss * 1e3 if loss is not None else None, ".3g")
            # Which way the copper moves: below the set point, toward 0 V on a negative rail,
            # or a ground rise on a net at 0 V.
            worst_copper = _num(rail.get("max_drop_mv"), ".3g")
            if rail.get("drop_unit") and rail.get("max_drop_mv") is not None:
                worst_copper += f" {rail['drop_unit']}"
            lines.append(f"| {project} | {_cell(rail['net'])} | {worst_text} | "
                         f"{_cell(worst_copper)} | "
                         f"{_num(rail.get('max_drop_pct'), '.3g')} | {loss_mw} | {_verdict_cell(rail)} |")
            problems = rail.get("problems", [])
            shown = [p for p in problems if p.get("severity") == "fail"]
            room = max(0, problems_per_rail - len(shown))
            shown += [p for p in problems if p.get("severity") != "fail"][:room]
            unjudged = rail.get("not_judged") or []
            if shown or unjudged:
                details += ["", f"**{_cell(rail['net'])}** ({_cell(e['project'])}): top problems", ""]
                details += [f"- NOT JUDGED {_cell(u['item'])} ({_cell(u['limit'])}): {_cell(u['reason'])}"
                            for u in unjudged]
                for p in shown:
                    tag = "**FAIL** " if p.get("severity") == "fail" else ""
                    layer = p.get("layer")
                    where = f" on {layer}" if layer and layer not in p["title"] else ""
                    details.append(f"- {tag}{_cell(p['title'])}{_cell(where)} at "
                                   f"({_num(p.get('x_mm'), '.2f')}, {_num(p.get('y_mm'), '.2f')}) mm")
            assumptions += [a for a in rail.get("assumptions", []) if a not in assumptions]
    lines += ["", "Verdicts are judged only against the limits written in the project files. Peak current "
              "densities at sharp corners depend on the mesh; the problems give spot means instead."]
    if field_warnings:
        lines += ["", "**Warnings (KiCad footprint fields):**", ""]
        lines += [f"- {_cell(w)}" for w in field_warnings]
    lines += details
    for e in entries:                         # the power tree of each project, when asked
        if "tree" in e:
            lines += tree_markdown(e["project"], e["tree"])
    if assumptions:
        lines += ["", "<details><summary>Assumptions</summary>", ""]
        lines += [f"- {_cell(a)}" for a in assumptions]
        lines += ["", "</details>"]
    lines += [""]
    if artifact_url:
        lines.append(f"Maps (PNG), the interactive HTML report and the JSON summaries: "
                     f"[download the results]({artifact_url}).")
    else:
        lines.append("Maps (PNG), the interactive HTML report and the JSON summaries are in the "
                     "results folder.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- command line

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m coppernicus.ci", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("solve", help="solve every matching project file")
    p.add_argument("patterns", nargs="+", help="project files or globs (one per line or argument)")
    p.add_argument("--out", required=True, help="results folder")
    p.add_argument("--top", type=int, default=5, help="hotspots and vias listed per rail")
    p.add_argument("--no-html", action="store_true", help="skip the HTML reports")
    p.add_argument("--tree", action="store_true",
                   help="also solve each project's power tree; its tables go into the summary")
    p = sub.add_parser("summary", help="print the Markdown summary of a solve")
    p.add_argument("--out", required=True, help="results folder of the solve")
    p.add_argument("--artifact-url", default="", help="link to the uploaded results")
    p.add_argument("--top", type=int, default=5, help="problems shown per rail")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):          # the summary holds ≤, µ, Ω...
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return _run(args)
    except Exception as e:      # a bug: never exit 1, which reads as "a limit was exceeded"
        print(f"error: internal error: {type(e).__name__}: {e}".replace("\n", " "), file=sys.stderr)
        return EXIT_ERROR


def _run(args) -> int:
    if args.command == "solve":
        index = solve_all(args.patterns, Path(args.out), top=args.top, html=not args.no_html,
                          tree=args.tree)
        if "error" in index:
            print(f"error: {index['error']['message']}", file=sys.stderr)
        return index["exit_code"]
    path = Path(args.out) / INDEX
    if not path.is_file():
        print("## Coppernicus: DC power integrity\n\n**Error:** Coppernicus did not run; see the job log.")
        return EXIT_ERROR
    index = json.loads(path.read_text(encoding="utf-8"))
    print(summary_markdown(index, args.artifact_url, args.top), end="")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
