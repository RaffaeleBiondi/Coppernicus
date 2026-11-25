"""The tolerance stack-up in the agent and CI interfaces: the MCP solve_tolerance (the
<project>_tolerance_summary.json document of ``coppernicus tolerance --json``, every AC and noise
solve sized first and refused over this machine's memory unless confirmed, the KiCad-fields
carry-over and the misspelt keys as the CLI's), the CLI's offline page, and the CI's opt-in
tolerance run with its exit codes. KiPI's synthetic coupled-noise board and the KiCad strip."""

import dataclasses
import json
import math
from html.parser import HTMLParser
from pathlib import Path

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import coupled_noise_board, coupled_noise_project, save_snapshot
from coppernicus.project import AcOptions, Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

STEM = "t.coppernicus"


@pytest.fixture()
def write(tmp_path):
    """Save the board and return a function writing the project: U2 with a tolerance, U1's
    accuracy, the probe LOAD with a 1 A step at 1 A/µs, U1's bandwidth 100 kHz (as
    test_cli_tolerance.py)."""
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    path = tmp_path / "t.coppernicus.yml"

    def write(tol_pct=6.0, transient_mv=30.0, accuracy_pct=1.0, bandwidth=True, ripple_mv=None, listed=True):
        p = coupled_noise_project(qrr_nc=5.0, ripple_probe=True)
        p.victims = []
        if not listed:                    # U1 does not list LOAD: the entered ripple_mv stands
            p.aggressors[0].ripple_probes = []
        rail = p.rails[1]
        rail.loads[0] = dataclasses.replace(rail.loads[0], tol_pct=tol_pct, transient_mv=transient_mv,
                                            ripple_mv=ripple_mv)
        rail.sources[0] = dataclasses.replace(rail.sources[0], accuracy_pct=accuracy_pct)
        p.probes[0].step_a, p.probes[0].slew_a_per_us = 1.0, 1.0
        p.ac = AcOptions(bandwidth_hz={"U1": 100e3} if bandwidth else {})
        return p.save(path)
    return write


def _misspell(path: Path) -> Path:
    """U2's tol_pct written as tol_pc: the file loads, its tolerance is ignored."""
    text = path.read_text(encoding="utf-8").replace("ripple_mv: 12.0", "ripple_mv: 12.0\n    tol_pc: 6")
    path.write_text(text, encoding="utf-8")
    return path


def _strip(tmp_path, sources, loads, net="VCC"):
    """The strip as a .kicad_pcb whose J1 and U1 define ``net`` in KiCad fields, and a project
    file with its own rail of that net (as test_cli_tolerance.py)."""
    from coppernicus.engine.synthetic import write_strip_kicad_pcb
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {"J1": {"PI_RAIL": net, "PI_SOURCE": "3.3V"},
                                                     "U1": {"PI_RAIL": net, "PI_LOAD": "1A"}}, net=net)
    return Project(board="s.kicad_pcb", rails=[Rail(net, sources=sources, loads=loads)]
                   ).save(tmp_path / "s.coppernicus.yml")


class Page(HTMLParser):
    """The tags of a page, checked balanced; its links and its text."""
    VOID = {"meta", "img", "br", "hr", "link", "input", "col", "source", "wbr"}

    def __init__(self, text):
        super().__init__()
        self.open, self.unbalanced, self.text = [], [], []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.open or self.open[-1] != tag:
            self.unbalanced.append(tag)
        else:
            self.open.pop()

    def handle_data(self, data):
        self.text.append(data)

    def words(self) -> str:
        return " ".join(" ".join(self.text).split())


def _comparable(doc):
    """A tolerance document without what differs from run to run: the files, the times, and
    the estimates' wording (it times a solve from the ones before it)."""
    out = {k: v for k, v in doc.items() if k not in ("stats", "summary_file", "csv_file", "estimates")}
    out["estimates"] = [(e["solve"], e.get("triangles"), e.get("unknowns")) for e in doc["estimates"]]
    return out


def _same(a, b, where="doc"):
    """Two documents alike: the keys, strings and verdicts exact, the numbers within a relative
    1e-4 (two solves of the same board; CI runs on Linux and Windows)."""
    if isinstance(a, float) or isinstance(b, float):
        assert isinstance(a, (int, float)) and isinstance(b, (int, float)), where
        assert math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-9), (where, a, b)
    elif isinstance(a, dict):
        assert isinstance(b, dict) and set(a) == set(b), where
        for k in a:
            _same(a[k], b[k], f"{where}.{k}")
    elif isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple)) and len(a) == len(b), where
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{where}[{i}]")
    else:
        assert a == b, (where, a, b)


# ---------------------------------------------------------------------- the session

def test_solve_tolerance_returns_the_summary_with_its_files_and_the_estimates(write, tmp_path):
    path = write()
    doc = Session(out_dir=tmp_path / "out").solve_tolerance(path)
    assert doc["kind"] == "tolerance" and doc["verdict"] == "pass"
    for key in ("verdict_note", "failed", "not_judged", "options", "warnings", "assumptions", "stats",
                "csv_file", "summary_file", "loads"):
        assert key in doc, key
    (ld,) = doc["loads"]
    assert ld["load"] == "U2" and ld["verdict"] == "pass"
    assert ld["parts"]["ripple"]["source"] == "noise of U1 at LOAD"
    assert "KiPI's estimate with a typical loop at 100 kHz" in ld["parts"]["transient"]["detail"]
    out = (tmp_path / "out").resolve()
    for f in (doc["summary_file"], doc["csv_file"]):
        assert Path(f).is_file() and out in Path(f).resolve().parents, f      # the session's folder
    assert not (tmp_path / "results").exists()                                # never next to the board
    # Every solve sized first, with the figures the CLI prints.
    assert [e["solve"] for e in doc["estimates"]] == ["AC VOUT", "noise U1"]
    ac_est, noise_est = doc["estimates"]
    assert ac_est["unknowns"] > 0 and ac_est["exceeds_memory"] is False and "unknowns" in ac_est["text"]
    assert {"harmonics", "field_solves", "board_model"} <= set(noise_est)
    assert doc["stats"]["ac_rails"] == 1 and doc["stats"]["aggressors"] == 1
    assert json.loads(Path(doc["summary_file"]).read_text(encoding="utf-8")) == doc
    json.dumps(doc, allow_nan=False)


def test_skip_ac_and_skip_noise_solve_nothing_heavy_and_leave_the_ripple_missing(write, tmp_path):
    doc = Session(out_dir=tmp_path / "out").solve_tolerance(write(), skip_ac=True, skip_noise=True)
    assert doc["estimates"] == [] and doc["verdict"] == "not_judged"
    (ld,) = doc["loads"]
    assert ld["parts"]["ripple"] is None and ld["hints"]["ripple"] == "U1 not solved (--skip-noise)"
    assert doc["not_judged"][0]["limit"] == "tol_pct"


@pytest.mark.parametrize("case", ["coupled", "carried", "lost", "misspelt", "skipped"])
def test_the_session_and_the_cli_give_the_same_document(write, tmp_path, capsys, case):
    flags, kwargs = [], {}
    if case == "coupled":                 # AC and noise solved: every part known
        path = write()
    elif case == "carried":               # a KiCad-fields rail carries the project's tolerances by ref
        path = _strip(tmp_path, [Source("J1", 3.3, accuracy_pct=1)],
                      [Load("U1", 1.0, tol_pct=5, ripple_mv=2, transient_mv=3)])
    elif case == "lost":                  # J5 and U9 are not in the fields' rail: lost, not judged
        path = _strip(tmp_path, [Source("J5", 3.3, accuracy_pct=1)],
                      [Load("U1", 1.0, tol_pct=5, ripple_mv=2, transient_mv=3), Load("U9", 1.0, tol_pct=4)])
    elif case == "misspelt":
        path = _misspell(write(tol_pct=None, ripple_mv=12.0, listed=False))
        flags, kwargs = ["--skip-ac"], {"skip_ac": True}
    else:
        path = write()
        flags, kwargs = ["--skip-ac", "--skip-noise"], {"skip_ac": True, "skip_noise": True}
    doc = Session(out_dir=tmp_path / "s").solve_tolerance(path, **kwargs)
    code = main(["tolerance", str(path), "--json", "--out", str(tmp_path / "c"), *flags])
    cli = json.loads(capsys.readouterr().out)
    assert set(cli) == set(doc)
    _same(_comparable(cli), _comparable(doc))
    assert code == {"pass": EXIT_OK, "fail": EXIT_LIMIT}.get(doc["verdict"],
                                                            EXIT_NOT_JUDGED if doc["not_judged"] else EXIT_OK)
    if case == "carried":
        assert doc["verdict"] == "pass"
        assert any("tol_pct, ripple_mv, transient_mv of U1 from the project file" in a
                   for a in doc["assumptions"])
    if case == "lost":
        assert [e["limit"] for e in doc["not_judged"] if e["item"] == "U9"] == ["tol_pct"]
        hint = doc["loads"][0]["hints"]["accuracy"]
        assert hint.startswith("J5's accuracy_pct in the project file was lost")
    if case == "misspelt":
        assert "Load U2 on VOUT: unknown key 'tol_pc' in the project file, ignored: did you mean " \
               "'tol_pct'?" in doc["warnings"]
        assert [(e["item"], e["limit"]) for e in doc["not_judged"]] == [("U2", "tol_pc")]


def test_use_board_fields_false_uses_the_project_rail(tmp_path):
    path = _strip(tmp_path, [Source("J1", 3.3, accuracy_pct=1)],
                  [Load("U1", 1.0, tol_pct=5, ripple_mv=2, transient_mv=3)])
    doc = Session(out_dir=tmp_path / "s").solve_tolerance(path, use_board_fields=False)
    assert doc["verdict"] == "pass" and not [a for a in doc["assumptions"] if "from the project file" in a]


def test_a_tolerance_estimate_over_the_machines_memory_needs_a_confirmation(write, tmp_path, monkeypatch):
    from coppernicus.engine import ac
    path = write(ripple_mv=12.0, listed=False)            # AC only: no noise solve needed
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_tolerance(path)
    assert e.value.code == "solve_too_large" and "confirm_large" in e.value.hint
    assert "max_edge_mm" in e.value.hint and "skip_ac" in e.value.hint
    assert "VOUT" in e.value.message and "unknowns" in e.value.message
    assert not list((tmp_path / "out").rglob("*"))                       # nothing solved, nothing written
    doc = session.solve_tolerance(path, confirm_large=True)
    assert doc["estimates"][0]["exceeds_memory"] is True and doc["verdict"] == "pass"


def test_the_board_temperature_is_sized_before_the_tree(tmp_path, monkeypatch):
    from coppernicus.engine import ac
    demo = Path(__file__).resolve().parents[1] / "examples" / "demo" / "demo-thermal.coppernicus.yml"
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)
    with pytest.raises(ServiceError) as e:
        Session(out_dir=tmp_path / "out").solve_tolerance(demo)
    assert e.value.code == "solve_too_large" and "board temperature" in e.value.message
    assert "grid_mm" in e.value.hint and not list((tmp_path / "out").rglob("*"))


def test_solve_tolerance_errors(write, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_tolerance(tmp_path / "missing.coppernicus.yml")
    assert e.value.code == "file_not_found"
    path = write()
    path.write_text(path.read_text(encoding="utf-8").replace("tol_pct: 6.0", "tol_pct: [2, 5]"),
                    encoding="utf-8")
    with pytest.raises(ServiceError) as e:
        session.solve_tolerance(path)
    assert e.value.code == "invalid_project" and "below" in e.value.message


def test_the_mcp_solve_tolerance_tool(write, tmp_path):
    import anyio
    from mcp import Client

    from coppernicus.mcp_server import INSTRUCTIONS, build_server
    path = write()

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            res = await c.call_tool("solve_tolerance", {"project_path": str(path), "skip_ac": True,
                                                        "skip_noise": True})
            tools = {t.name: t for t in (await c.list_tools()).tools}
            return res.structured_content, tools
    doc, tools = anyio.run(scenario)
    assert doc["kind"] == "tolerance" and doc["verdict"] == "not_judged" and doc["estimates"] == []
    tool = tools["solve_tolerance"]
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
    inputs = set(tool.input_schema["properties"])
    assert {"skip_ac", "skip_noise", "confirm_large", "use_board_fields"} <= inputs
    words = " ".join(tool.description.split())
    for phrase in ("tolerances the user set", "every part is known", "never judged", "confirm_large"):
        assert phrase in words, phrase
    assert "solve_tolerance" in INSTRUCTIONS


# ---------------------------------------------------------------------- the CLI's page

def test_the_cli_writes_an_offline_html_page(write, tmp_path, capsys):
    """examples/tolerance: U2 with all three tolerances, every part known."""
    example = Path(__file__).resolve().parents[1] / "examples" / "tolerance" / "tolerance.coppernicus.yml"
    page = tmp_path / "tolerance.html"
    code = main(["tolerance", str(example), "--json", "--out", str(tmp_path / "o"), "--html", str(page)])
    assert code == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert Path(doc["report_file"]) == page
    saved = json.loads(Path(doc["summary_file"]).read_text(encoding="utf-8"))
    assert saved["report_file"] == doc["report_file"]
    text = page.read_text(encoding="utf-8")
    parsed = Page(text)
    assert not parsed.unbalanced and not parsed.open, (parsed.unbalanced, parsed.open)
    assert "https://" not in text and "http://" not in text          # offline: everything inside
    words = parsed.words()
    assert "Verdict: pass" in words and "Load U2 on VOUT" in words
    for part in ("DC drop", "DC accuracy", "ripple", "transient"):
        assert part in words, part
    for source in ("power tree", "accuracy_pct of U1", "noise of U1 at LOAD", "entered (transient_mv)"):
        assert source in words, source
    for header in ("Below nominal", "Above nominal", "Allowed", "Margin", "Verdict", "Reason"):
        assert header in words, header
    for check in ("tol_pct", "dc_tol_pct", "ac_tol_mv"):
        assert check in words, check
    total = doc["loads"][0]["checks"]["total"]
    assert f"{total['allowed_below_mv']:.4g} / {total['allowed_above_mv']:.4g} mV" in words
    assert "KiPI's transient estimate" in words and "never judged" in words
    assert "Assumptions" in words and "AC VOUT" in words and "noise U1" in words   # the estimates
    # The default path: <out>/<project>_tolerance_report.html.
    assert main(["tolerance", str(write()), "--out", str(tmp_path / "d"), "--skip-ac", "--skip-noise",
                 "--html"]) == EXIT_NOT_JUDGED
    printed = capsys.readouterr().out
    default = tmp_path / "d" / f"{STEM}_tolerance_report.html"
    assert default.is_file() and f"Report: {default.as_posix()}" in printed


def test_the_page_says_what_is_missing_why_it_fails_and_what_is_not_judged(write, tmp_path, capsys):
    from coppernicus.report import write_tolerance_report
    o = ["--json", "--out", str(tmp_path / "o"), "--skip-ac", "--skip-noise"]
    # Missing: the ripple, with its hint; the check not judged, with its reason.
    main(["tolerance", str(write()), *o])
    doc = json.loads(capsys.readouterr().out)
    words = Page(write_tolerance_report(doc, tmp_path / "nj.html").read_text(encoding="utf-8")).words()
    assert "Verdict: not_judged" in words and "missing" in words
    assert "U1 not solved (--skip-noise)" in words and "not judged" in words
    assert "(at least: ripple missing)" in words and "(at most)" in words    # an incomplete check's sums
    assert "0 / 0 mV" not in words.split("ripple", 1)[1].split("transient", 1)[0]   # missing is never 0
    # A fail on the known parts alone: FAIL and the reason from the document's failed list.
    main(["tolerance", str(write(tol_pct=2.0)), *o])
    doc = json.loads(capsys.readouterr().out)
    words = Page(write_tolerance_report(doc, tmp_path / "f.html").read_text(encoding="utf-8")).words()
    assert "Verdict: fail" in words and "FAIL" in words
    assert doc["failed"][0]["reason"] in words and "known parts alone" in words
    # A misspelt key: a not-judged entry with no load row of its own is on the page too.
    main(["tolerance", str(_misspell(write(tol_pct=None, ripple_mv=12.0, listed=False))), *o])
    doc = json.loads(capsys.readouterr().out)
    page = write_tolerance_report(doc, tmp_path / "m.html", project="t.coppernicus.yml")
    words = Page(page.read_text(encoding="utf-8")).words()
    assert "tol_pc" in words and "did you mean" in words and "No tolerance set" in words
    assert "unknown key 'tol_pc'" in words                                   # the warning, too


def test_the_page_escapes_names(tmp_path, capsys):
    from coppernicus.report import write_tolerance_report
    path = _strip(tmp_path, [Source("J5", 3.3, accuracy_pct=1)],
                  [Load("U1", 1.0, tol_pct=5), Load("U9", 1.0, tol_pct=4)], net="V<b>{slash}A")
    main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")])
    doc = json.loads(capsys.readouterr().out)
    text = write_tolerance_report(doc, tmp_path / "e.html").read_text(encoding="utf-8")
    assert "V<b>" not in text and "V&lt;b&gt;{slash}A" in text
    assert not Page(text).unbalanced


# ---------------------------------------------------------------------- CI

@pytest.mark.parametrize("kwargs, code, verdict", [
    (dict(ripple_mv=12.0, listed=False, bandwidth=False), EXIT_OK, "pass"),
    (dict(tol_pct=2.0, ripple_mv=12.0, listed=False, bandwidth=False), EXIT_LIMIT, "fail"),
    (dict(accuracy_pct=None, ripple_mv=12.0, listed=False, bandwidth=False), EXIT_NOT_JUDGED, "not_judged"),
])
def test_ci_runs_tolerance_when_asked_and_its_verdict_sets_the_code(write, tmp_path, monkeypatch,
                                                                    kwargs, code, verdict):
    path = write(**kwargs)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=False, tolerance=True)
    [entry] = index["projects"]
    assert entry["tolerance"]["verdict"] == verdict and entry["doc"]["tolerance_verdict"] == verdict
    assert index["exit_code"] == code
    if code == EXIT_NOT_JUDGED:                          # its entries join the project's
        assert any(u["rail"] is None and "U2" in u["item"] and u["limit"] == "tol_pct"
                   and "accuracy" in u["reason"] for u in entry["doc"]["not_judged"])
    md = ci.summary_markdown(index)
    assert "### Tolerance" in md and "| U2 | total (tol_pct) |" in md
    assert "| Load | Check | Allowed | Total | Verdict |" in md
    if code == EXIT_LIMIT:
        assert "**FAIL**" in md
        plain = ci.solve_all([path.name], Path("ci2"), html=False)
        assert "tolerance" not in plain["projects"][0]                    # opt-in, off by default
        assert plain["exit_code"] == EXIT_OK
    if code == EXIT_NOT_JUDGED:
        assert "NOT JUDGED tolerance of U2" in md


def test_ci_writes_the_tolerance_page_with_html(write, tmp_path, monkeypatch):
    path = write(ripple_mv=12.0, listed=False, bandwidth=False)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=True, tolerance=True)
    report = index["projects"][0]["tolerance"]["report_file"]
    assert Path(report).is_file() and Path(report).name == f"{STEM}_tolerance_report.html"


def test_ci_skips_projects_without_a_tolerance_but_not_a_misspelt_one(write, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write(tol_pct=None, ripple_mv=12.0, listed=False, bandwidth=False)
    index = ci.solve_all([path.name], Path("ci"), html=False, tolerance=True)
    assert "tolerance" not in index["projects"][0] and index["exit_code"] == EXIT_OK
    _misspell(path)                       # its only tolerance is misspelt: run, and not judged
    index = ci.solve_all([path.name], Path("ci2"), html=False, tolerance=True)
    [entry] = index["projects"]
    assert entry["tolerance"]["verdict"] == "not_judged" and index["exit_code"] == EXIT_NOT_JUDGED
    assert any(u["limit"] == "tol_pc" for u in entry["doc"]["not_judged"])


def test_ci_tolerance_error_sets_code_2(write, tmp_path, monkeypatch):
    """U2 without its ground pins: the rail-by-rail solve runs, the power tree (which solves
    GND) cannot."""
    path = write(ripple_mv=12.0, listed=False, bandwidth=False)
    p = Project.load(path)
    p.rails[1].loads[0] = dataclasses.replace(p.rails[1].loads[0], ground_net="", ground_pins=None)
    p.save(path)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=False, tolerance=True)
    [entry] = index["projects"]
    assert "doc" in entry and "error" in entry["tolerance"] and index["exit_code"] == EXIT_ERROR
    md = ci.summary_markdown(index)
    assert "### Tolerance" in md and "**Error:**" in md


def test_the_action_has_an_opt_in_tolerance_input(tmp_path):
    import yaml
    action = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8"))
    assert action["inputs"]["tolerance"]["default"] == "false"
    solve = next(s for s in action["runs"]["steps"] if s.get("id") == "solve")
    assert solve["env"]["COPPERNICUS_TOLERANCE"] == "${{ inputs.tolerance }}"
    assert "--tolerance" in solve["run"]
    # The helper takes --tolerance (no project matched: an error, not an unknown option).
    assert ci.main(["solve", "--tolerance", "--no-html", "--out", str(tmp_path / "o"),
                    str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
