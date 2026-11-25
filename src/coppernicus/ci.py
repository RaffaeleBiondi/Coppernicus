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
              tree: bool = False, ac: bool = False, noise: bool = False,
              tolerance: bool = False) -> Dict[str, Any]:
    """Solve every matching project; return the index written to ``out_dir/INDEX``. With
    ``tree``, each project is also solved as a whole power tree (``solve --tree --json``):
    its document goes into the entry's ``tree``, for the summary's tables; it does not set
    the exit code. With ``ac`` (``noise``), each project with probes (aggressors) is also
    solved with ``coppernicus ac`` (``coppernicus noise``); that verdict joins the project's.
    With ``tolerance``, each project with a tolerance set, in its file or its board's KiCad
    fields (or a key that looks like a misspelt one), is also run through ``coppernicus
    tolerance``; that verdict joins the project's."""
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
            _judge_board_temperature(entry)
        if ac and "doc" in entry and _has_probes(project):     # opt-in: heavy
            entry["ac"] = _solve_ac(project, folder, env)
            _judge_ac(entry)
        if noise and "doc" in entry and _has_aggressors(project):     # opt-in: heavy
            entry["noise"] = _solve_noise(project, folder, html, env)
            _judge_noise(entry)
        if tolerance and "doc" in entry and _has_tolerances(project):     # opt-in: heavy where needed
            entry["tolerance"] = _solve_tolerance(project, folder, html, env)
            _judge_tolerance(entry)
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


def _rails_verdict(doc: Dict[str, Any]) -> str:
    """A rail-by-rail document's overall verdict from its rails and its not_judged list, by
    the rule of :meth:`coppernicus.service.Session.solve_project`."""
    rails = doc.get("rails", [])
    verdicts = ["error" if "error" in r else r.get("verdict") for r in rails if "skipped" not in r]
    if "error" in verdicts:
        return "error"
    if "fail" in verdicts:
        return "fail"
    if doc.get("not_judged"):
        return "not_judged"
    if verdicts and all(v == "pass" for v in verdicts):
        return "pass"
    return "pass_partially_judged" if "pass" in verdicts else "not_judged"


def _judge_board_temperature(entry: Dict[str, Any]) -> None:
    """When the tree run solved the board temperature, its verdict judges the project's
    board-temperature limits: the rail-by-rail "not judged" entries for them are replaced by
    it (fail: exit 1; not judged: its own entries, exit 3; pass: nothing left to say)."""
    thermal = (entry.get("tree") or {}).get("thermal")
    doc = entry.get("doc")
    if not thermal or doc is None or entry["exit_code"] == EXIT_ERROR:
        return
    kept = [u for u in doc.get("not_judged", [])
            if not (u.get("rail") is None and u.get("item") == "board temperature")]
    doc["not_judged"] = kept + [{"rail": None, **u} for u in thermal.get("not_judged", [])]
    verdict = _rails_verdict(doc)
    if thermal.get("verdict") == "fail" and verdict != "error":
        verdict = "fail"
    doc["verdict"] = verdict
    doc["board_temperature_verdict"] = thermal.get("verdict")
    entry["exit_code"] = (EXIT_LIMIT if verdict == "fail" else EXIT_ERROR if verdict == "error"
                          else EXIT_NOT_JUDGED if doc["not_judged"] else EXIT_OK)


def _has_probes(project: Path) -> bool:
    try:
        from coppernicus.project import Project
        return bool(Project.load(project).probes)
    except Exception:                    # the rail-by-rail run reports a bad project
        return False


def _solve_ac(project: Path, folder: Path, env) -> Dict[str, Any]:
    """The AC impedance document of a project (``coppernicus ac --json``), or its error."""
    cmd = [sys.executable, "-m", "coppernicus", "ac", str(project), "--json", "--out", str(folder)]
    print(f"coppernicus: solving the impedance of {project.as_posix()}", file=sys.stderr, flush=True)
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if run.stderr:
        sys.stderr.write(run.stderr)
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError:
        return {"error": {"code": "internal_error",
                          "message": f"coppernicus ac exited with {run.returncode} and no JSON",
                          "hint": "see the log above"}}


def _judge_ac(entry: Dict[str, Any]) -> None:
    """The impedance verdict joins the project's: fail sets 1, a target that could not judge
    sets 3 (its entries are added to the project's not_judged), an error sets 2."""
    acdoc, doc = entry.get("ac") or {}, entry.get("doc")
    if doc is None:
        return
    if "error" in acdoc:
        doc["verdict"] = "error"
        entry["exit_code"] = EXIT_ERROR
        return
    doc["not_judged"] = list(doc.get("not_judged", [])) + [
        {"rail": e.get("rail") or None, "item": f"impedance at {e.get('item')}", "limit": e.get("limit"),
         "reason": e.get("reason")} for e in acdoc.get("not_judged", [])]
    if acdoc.get("verdict") == "fail" and doc.get("verdict") != "error":
        doc["verdict"] = "fail"
    elif doc["not_judged"] and doc.get("verdict") in ("pass", "pass_partially_judged"):
        doc["verdict"] = "not_judged"
    doc["impedance_verdict"] = acdoc.get("verdict")
    code = (EXIT_LIMIT if acdoc.get("verdict") == "fail" else
            EXIT_NOT_JUDGED if acdoc.get("not_judged") else EXIT_OK)
    entry["exit_code"] = next(c for c in PRECEDENCE if c in (entry["exit_code"], code))


def _has_aggressors(project: Path) -> bool:
    try:
        from coppernicus.project import Project
        return bool(Project.load(project).aggressors)
    except Exception:                    # the rail-by-rail run reports a bad project
        return False


def _solve_noise(project: Path, folder: Path, html: bool, env) -> Dict[str, Any]:
    """The ripple and noise document of a project (``coppernicus noise --json``, with its
    offline page when ``html``), or its error."""
    cmd = [sys.executable, "-m", "coppernicus", "noise", str(project), "--json", "--out", str(folder)]
    if html:
        cmd.append("--html")
    print(f"coppernicus: solving the ripple and noise of {project.as_posix()}", file=sys.stderr,
          flush=True)
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if run.stderr:
        sys.stderr.write(run.stderr)
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError:
        return {"error": {"code": "internal_error",
                          "message": f"coppernicus noise exited with {run.returncode} and no JSON",
                          "hint": "see the log above"}}


def _judge_noise(entry: Dict[str, Any]) -> None:
    """The noise verdict joins the project's: a victim over its margin sets 1, a margin that
    could not judge sets 3 (its entries, with their aggressor, are added to the project's
    not_judged), an error sets 2. The ripple and the radiated estimate are never judged."""
    ndoc, doc = entry.get("noise") or {}, entry.get("doc")
    if doc is None:
        return
    if "error" in ndoc:
        doc["verdict"] = "error"
        entry["exit_code"] = EXIT_ERROR
        return
    doc["not_judged"] = list(doc.get("not_judged", [])) + [
        {"rail": None, "item": f"noise on {e.get('item')} from {e.get('aggressor')}",
         "limit": e.get("limit"), "reason": e.get("reason")} for e in ndoc.get("not_judged", [])]
    if ndoc.get("verdict") == "fail" and doc.get("verdict") != "error":
        doc["verdict"] = "fail"
    elif doc["not_judged"] and doc.get("verdict") in ("pass", "pass_partially_judged"):
        doc["verdict"] = "not_judged"
    doc["noise_verdict"] = ndoc.get("verdict")
    code = (EXIT_LIMIT if ndoc.get("verdict") == "fail" else
            EXIT_NOT_JUDGED if ndoc.get("not_judged") else EXIT_OK)
    entry["exit_code"] = next(c for c in PRECEDENCE if c in (entry["exit_code"], code))


def _has_tolerances(project: Path) -> bool:
    """Whether a load sets a tolerance, in the project file or in the board's KiCad fields
    (``PI_TOL``, ``PI_DC_TOL``, ``PI_AC_TOL``, unless the project turns the fields off), or a
    load or source holds a key that looks like a misspelt tolerance (``tol_pc``):
    ``coppernicus tolerance`` then says it is not judged, never a silent pass."""
    try:
        import yaml

        from coppernicus.board_fields import LOAD_FIELDS
        from coppernicus.engine.kicad_fields import read_board_fields
        from coppernicus.project import Project
        from coppernicus.tolerance import misspelt_keys
        loaded = Project.load(project)
        if any(ld.has_tolerance() for r in loaded.rails for ld in r.loads):
            return True
        if misspelt_keys(yaml.safe_load(project.read_text(encoding="utf-8")))[1]:
            return True
        fields = read_board_fields(loaded.board_path()).fields if loaded.use_board_fields else {}
        return any((f.get(n) or "").strip() for f in fields.values() for n in LOAD_FIELDS)
    except Exception:                    # the rail-by-rail run reports a bad project
        return False


def _solve_tolerance(project: Path, folder: Path, html: bool, env) -> Dict[str, Any]:
    """The tolerance stack-up document of a project (``coppernicus tolerance --json``, with its
    offline page when ``html``), or its error."""
    cmd = [sys.executable, "-m", "coppernicus", "tolerance", str(project), "--json", "--out", str(folder)]
    if html:
        cmd.append("--html")
    print(f"coppernicus: the tolerance stack-up of {project.as_posix()}", file=sys.stderr, flush=True)
    run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if run.stderr:
        sys.stderr.write(run.stderr)
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError:
        return {"error": {"code": "internal_error",
                          "message": f"coppernicus tolerance exited with {run.returncode} and no JSON",
                          "hint": "see the log above"}}


def _judge_tolerance(entry: Dict[str, Any]) -> None:
    """The tolerance verdict joins the project's: a check over its tolerance sets 1, one that
    could not judge sets 3 (its entries, with the load's net, are added to the project's
    not_judged), an error sets 2. KiPI's transient estimate is never judged."""
    tdoc, doc = entry.get("tolerance") or {}, entry.get("doc")
    if doc is None:
        return
    if "error" in tdoc:
        doc["verdict"] = "error"
        entry["exit_code"] = EXIT_ERROR
        return
    doc["not_judged"] = list(doc.get("not_judged", [])) + [
        {"rail": None, "item": f"tolerance of {e.get('item')} on {e.get('net')}", "limit": e.get("limit"),
         "reason": e.get("reason")} for e in tdoc.get("not_judged", [])]
    if tdoc.get("verdict") == "fail" and doc.get("verdict") != "error":
        doc["verdict"] = "fail"
    elif doc["not_judged"] and doc.get("verdict") in ("pass", "pass_partially_judged"):
        doc["verdict"] = "not_judged"
    doc["tolerance_verdict"] = tdoc.get("verdict")
    code = (EXIT_LIMIT if tdoc.get("verdict") == "fail" else
            EXIT_NOT_JUDGED if tdoc.get("not_judged") else EXIT_OK)
    entry["exit_code"] = next(c for c in PRECEDENCE if c in (entry["exit_code"], code))


def tolerance_markdown(project: str, doc: Dict[str, Any]) -> List[str]:
    """The tolerance stack-up of a project, for the job summary: the verdict and each check
    with a tolerance set, its total against what it allows, load by load."""
    from coppernicus.tolerance import CHECK_NAMES
    lines = ["", f"### Tolerance: `{_cell(project)}`", ""]
    if "error" in doc:
        return lines + [f"**Error:** {_cell(doc['error'].get('message', ''))}"]
    lines += [f"Verdict: **{_cell(doc.get('verdict'))}**. {_cell(doc.get('verdict_note', ''))}", "",
              "| Load | Check | Allowed | Total | Verdict |", "|---|---|---:|---:|---|"]
    for ld in doc.get("loads", []):
        for kind, c in (ld.get("checks") or {}).items():
            if not c.get("limit"):
                continue
            passed = c.get("passed")
            verdict = "**FAIL**" if passed is False else "pass" if passed else "not judged"
            total = f"{_num(c.get('below_mv'), '.4g')} / {_num(c.get('above_mv'), '.4g')} mV"
            if not c.get("complete"):
                total += " (at least: parts missing)"
            allowed = (f"{_num(c.get('allowed_below_mv'), '.4g')} / "
                       f"{_num(c.get('allowed_above_mv'), '.4g')} mV")
            check = f"{CHECK_NAMES.get(kind, kind)} ({c['limit']})"
            lines.append(f"| {_cell(ld['load'])} | {_cell(check)} | {allowed} | {_cell(total)} | {verdict} |")
    lines += ["", "Below / above nominal. Judged only against the tolerances set in the project file "
              "or the KiCad fields, "
              "a check only when every part is known (a missing part is never 0); KiPI's transient "
              "estimate is shown, never judged."]
    return lines


def _mv_cell(x: Any) -> str:
    if x is None:
        return "–"
    return f"{x / 1e3:.4g} V" if abs(x) >= 999.95 else f"{x:.4g} mV"


def noise_markdown(project: str, doc: Dict[str, Any]) -> List[str]:
    """The ripple and coupled noise of a project, for the job summary: the verdict and each
    victim's peak against its margin, aggressor by aggressor."""
    from coppernicus.coupled_noise import victim_verdict_words
    lines = ["", f"### Ripple and noise: `{_cell(project)}`", ""]
    if "error" in doc:
        return lines + [f"**Error:** {_cell(doc['error'].get('message', ''))}"]
    lines += [f"Verdict: **{_cell(doc.get('verdict'))}**. {_cell(doc.get('verdict_note', ''))}", "",
              "| Aggressor | Victim | Peak | Margin | Verdict |", "|---|---|---:|---:|---|"]
    for a in doc.get("aggressors", []):
        for v in a.get("victims", []):
            verdict, _reason = victim_verdict_words(v, a.get("failed", []))
            verdict = {"FAIL": "**FAIL**", "PASS": "pass"}.get(verdict, verdict)
            lines.append(f"| {_cell(a['aggressor'])} | {_cell(v['name'])} | {_mv_cell(v.get('peak_mv'))} | "
                         f"{_mv_cell(v.get('margin_mv'))} | {verdict} |")
    lines += ["", "Judged only against the victims' noise margins. The ripple and the radiated "
              "estimate are never judged; a victim is not judged while a KiPI default stands in "
              "for a value (its reason is listed above)."]
    return lines


def ac_markdown(project: str, doc: Dict[str, Any]) -> List[str]:
    """The impedance of a project, for the job summary: the verdict and each probe's worst
    |Z| against its target."""
    from coppernicus.impedance import hz_text, ohm_text
    lines = ["", f"### Impedance: `{_cell(project)}`", ""]
    if "error" in doc:
        return lines + [f"**Error:** {_cell(doc['error'].get('message', ''))}"]
    lines += [f"Verdict: **{_cell(doc.get('verdict'))}**. {_cell(doc.get('verdict_note', ''))}", "",
              "| Probe | Rail | Worst \\|Z\\| | At | Target | Verdict |", "|---|---|---:|---:|---:|---|"]
    for r in doc.get("rails", []):
        for p in r.get("probes", []):
            passed = p.get("passed")
            verdict = "**FAIL**" if passed is False else "pass" if passed else "not judged"
            worst, at = ohm_text(p.get("worst_z_ohm")), hz_text(p.get("worst_hz"))
            lines.append(f"| {_cell(p['name'])} | {_cell(r['rail'])} | {_cell(worst)} | {_cell(at)} | "
                         f"{_cell(ohm_text(p.get('target_ohm')))} | {verdict} |")
    return lines


def tree_markdown(project: str, doc: Dict[str, Any]) -> List[str]:
    """The VRM and load tables of a power-tree document, for the job summary."""
    lines = ["", f"### Power tree: `{_cell(project)}`", ""]
    if "error" in doc:
        return lines + [f"**Error:** {_cell(doc['error'].get('message', ''))}"]
    from coppernicus.power_tree import attention_text
    lines += [f"Verdict: **{_cell(doc.get('verdict'))}**. {_cell(doc.get('verdict_note', ''))}", ""]
    lines += [x for a in attention_text(doc.get("groups", [])) for x in (_cell(a), "")]
    lines += ["| VRM | Kind | Group | V set (V) | V out (V) | I out (A) | I max (A) | V in (V) "
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
    if doc.get("thermal"):
        lines += _thermal_markdown(doc["thermal"])
    return lines


def _thermal_markdown(th: Dict[str, Any]) -> List[str]:
    """The board temperature of a tree document, for the job summary."""
    def pad(p):
        return f"{p['ref']}.{p['pad']}" if p else "–"
    settled = {True: "settled", False: "not settled"}.get(th.get("settled"), "settling not checked")
    peak = (f"Grid peak {_num(th.get('peak_c'), '.4g')} °C (rise {_num(th.get('peak_rise_c'), '.3g')} °C)"
            f" on {_cell(th.get('peak_layer'))}, nearest pad {_cell(pad(th.get('peak_nearest_pad')))}: "
            f"shown, never judged. Ambient {_num(th.get('ambient_c'), '.3g')} °C, "
            f"{th.get('iterations')} iteration(s), {settled}.")
    lines = ["", "#### Board temperature", "",
             f"Verdict: **{_cell(th.get('verdict'))}**. {_cell(th.get('verdict_note', ''))}", "", peak,
             "", "| Layer | Hottest copper (°C) | Rise (°C) | Nearest pad | Verdict |",
             "|---|---:|---:|---|---|"]
    for r in th.get("layers", []):
        ex = r.get("exceeds_limit")
        judged = "**FAIL**" if ex else "within" if ex is False else "not judged"
        lines.append(f"| {_cell(r['layer'])} | {_num(r.get('t_max_c'), '.4g')} | "
                     f"{_num(r.get('rise_c'), '.3g')} | {_cell(pad(r.get('nearest_pad')))} | {judged} |")
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
    attention = 0                            # spots above the current-density default
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
            attention += sum(p.get("severity") == "attention" for p in problems)
            shown = [p for p in problems if p.get("severity") == "fail"]
            room = max(0, problems_per_rail - len(shown))
            # every attention item on top of the ranked ones, as in the problems list
            shown += [p for p in problems if p.get("severity") == "attention"]
            shown += [p for p in problems if p.get("severity") not in ("fail", "attention")][:room]
            unjudged = rail.get("not_judged") or []
            if shown or unjudged:
                details += ["", f"**{_cell(rail['net'])}** ({_cell(e['project'])}): top problems", ""]
                details += [f"- NOT JUDGED {_cell(u['item'])} ({_cell(u['limit'])}): {_cell(u['reason'])}"
                            for u in unjudged]
                for p in shown:
                    tag = {"fail": "**FAIL** ", "attention": "**ATTENTION** "}.get(p.get("severity"), "")
                    layer = p.get("layer")
                    where = f" on {layer}" if layer and layer not in p["title"] else ""
                    details.append(f"- {tag}{_cell(p['title'])}{_cell(where)} at "
                                   f"({_num(p.get('x_mm'), '.2f')}, {_num(p.get('y_mm'), '.2f')}) mm")
            assumptions += [a for a in rail.get("assumptions", []) if a not in assumptions]
        board = [u for u in e["doc"].get("not_judged", []) if not u.get("rail")]
        if board:                             # of the board, not of a rail: its temperature
            details += ["", f"**{project}**: not judged", ""]
            details += [f"- NOT JUDGED {_cell(u['item'])} ({_cell(u['limit'])}): {_cell(u['reason'])}"
                        for u in board]
    lines += ["", "Verdicts are judged only against the limits written in the project files. Peak current "
              "densities at sharp corners depend on the mesh; the problems give spot means instead."]
    if attention:
        lines += ["", f"{attention} attention item{'' if attention == 1 else 's'}: current crowding above "
                  "Coppernicus's current-density default (scaled with each layer's copper thickness: "
                  "see the assumptions), not a verdict: the exit code does not change. Set "
                  "max_current_density in the project file to judge it."]
    if field_warnings:
        lines += ["", "**Warnings (KiCad footprint fields):**", ""]
        lines += [f"- {_cell(w)}" for w in field_warnings]
    lines += details
    for e in entries:                         # the impedance of each project, when asked
        if "ac" in e:
            lines += ac_markdown(e["project"], e["ac"])
    for e in entries:                         # the ripple and noise of each project, when asked
        if "noise" in e:
            lines += noise_markdown(e["project"], e["noise"])
    for e in entries:                         # the tolerance stack-up of each project, when asked
        if "tolerance" in e:
            lines += tolerance_markdown(e["project"], e["tolerance"])
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
    p.add_argument("--ac", action="store_true",
                   help="also solve the AC impedance of each project with probes (heavy); its "
                        "verdict sets the code")
    p.add_argument("--noise", action="store_true",
                   help="also solve the ripple and coupled noise of each project with aggressors "
                        "(heavy); its verdict, against the noise margins only, sets the code")
    p.add_argument("--tolerance", action="store_true",
                   help="also run the tolerance stack-up of each project with a tolerance set (heavy "
                        "where a load needs AC or noise); its verdict, against the tolerances only, "
                        "sets the code")
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
                          tree=args.tree, ac=args.ac, noise=args.noise, tolerance=args.tolerance)
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
