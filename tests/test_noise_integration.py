"""Ripple and coupled noise in the agent and CI interfaces: the MCP solve_noise (the
<project>_noise_summary.json document, with each aggressor's CSV and PNG paths and its size and
memory estimate first, refused over this machine's memory unless confirmed), the CLI's estimate
line and its HTML page, and the CI's opt-in noise run with its exit codes. KiPI's synthetic
coupled-noise board."""

import base64
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import coupled_noise_board, coupled_noise_project, save_snapshot
from coppernicus.project import NoiseOptions, Project
from coppernicus.service import ServiceError, Session

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def write(tmp_path):
    """Save the board and return a function writing the project (Qrr entered, 3.3 V logic:
    every victim has KiPI's JESD8 margin)."""
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    path = tmp_path / "n.coppernicus.yml"

    def write(board_model=True, qrr_nc=5.0, margin=None):
        p = coupled_noise_project(qrr_nc=qrr_nc, logic_v=3.3)
        p.noise = NoiseOptions(board_model=board_model)
        if margin is not None:
            p.victims[1].noise_margin_v = margin            # SIG_LOOP, 655 mV
        return p.save(path)
    return write


class Page(HTMLParser):
    """The tags of a page, checked balanced; its images and its text."""
    VOID = {"meta", "img", "br", "hr", "link", "input", "col", "source", "wbr"}

    def __init__(self, text):
        super().__init__()
        self.open, self.images, self.text, self.unbalanced = [], [], [], []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))
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


def test_solve_noise_returns_the_summary_with_its_files_and_the_estimate(write, tmp_path):
    path = write()
    doc = Session(out_dir=tmp_path / "out").solve_noise(path)
    assert doc["kind"] == "noise" and doc["verdict"] == "pass"
    for key in ("verdict_note", "failed", "not_judged", "warnings", "summary_file"):
        assert key in doc, key
    (a,) = doc["aggressors"]
    assert a["aggressor"] == "U1" and a["ripple"] and len(a["victims"]) == 3 and a["assumptions"]
    assert "stand_ins" in a and "warnings" in a
    assert Path(a["csv_file"]).is_file() and len(a["png_files"]) == 3
    assert all(Path(p).is_file() for p in a["png_files"])
    out = (tmp_path / "out").resolve()
    for f in (doc["summary_file"], a["csv_file"], *a["png_files"]):
        assert out in Path(f).resolve().parents, f            # the session's folder
    assert not (tmp_path / "results").exists()                # never next to the board
    est = a["estimate"]                                       # the engine's own figures
    assert est["board_model"] is True and est["triangles"] == int(a["stats"]["triangles"])
    assert est["ports"] == int(a["stats"]["ports"])
    assert est["field_solves"] == int(a["stats"]["field_solves"])
    assert est["unknowns"] >= int(a["stats"]["unknowns"]) and est["exceeds_memory"] is False
    assert est["memory_bytes"] > 0 and "unknowns" in est["text"] and "harmonics" in est["text"]
    assert json.loads(Path(doc["summary_file"]).read_text(encoding="utf-8")) == doc
    json.dumps(doc, allow_nan=False)


def test_a_noise_estimate_over_the_machines_memory_needs_a_confirmation(write, tmp_path, monkeypatch):
    from coppernicus.engine import ac
    path = write(board_model=False)
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_noise(path)
    assert e.value.code == "solve_too_large" and "confirm_large" in e.value.hint
    assert "max_edge_mm" in e.value.hint and "U1" in e.value.message
    assert not list((tmp_path / "out").rglob("*"))                  # nothing solved, nothing written
    doc = session.solve_noise(path, confirm_large=True)
    est = doc["aggressors"][0]["estimate"]
    assert est["exceeds_memory"] is True and est["board_model"] is False
    assert "0 triangles" not in est["text"] and "board model" in est["text"].lower()


def test_solve_noise_refuses_an_unknown_aggressor_and_a_project_without_one(write, tmp_path):
    path = write(board_model=False)
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_noise(path, aggressors=["NOPE"])
    assert e.value.code == "invalid_input" and "NOPE" in e.value.message and "U1" in e.value.hint
    p = Project.load(path)
    p.aggressors[0].fsw_hz = None
    p.save(path)
    with pytest.raises(ServiceError) as e:
        session.solve_noise(path)
    assert e.value.code == "solve_failed" and "enter its fsw_hz" in e.value.message
    p.aggressors = []
    p.save(path)
    with pytest.raises(ServiceError) as e:
        session.solve_noise(path)
    assert e.value.code == "invalid_input" and "no aggressor" in e.value.message.lower()


def test_the_session_and_the_cli_give_the_same_document(write, tmp_path, capsys):
    path = write(board_model=False, margin=0.5)
    doc = Session(out_dir=tmp_path / "s").solve_noise(path, aggressors=["U1"])
    assert main(["noise", str(path), "--json", "--out", str(tmp_path / "c")]) == EXIT_NOT_JUDGED
    captured = capsys.readouterr()
    cli = json.loads(captured.out)                        # --json: only the document on stdout
    assert "Noise of U1: " in captured.err                # the estimate line goes to stderr
    assert set(cli) == set(doc) and set(cli["aggressors"][0]) == set(doc["aggressors"][0])
    for key in ("verdict", "verdict_note", "failed", "not_judged"):
        assert cli[key] == doc[key], key
    assert cli["aggressors"][0]["victims"] == doc["aggressors"][0]["victims"]
    assert cli["aggressors"][0]["estimate"]["triangles"] == doc["aggressors"][0]["estimate"]["triangles"]
    # A board model left out is a stand-in: the victim with a margin is not judged.
    assert doc["verdict"] == "not_judged" and doc["not_judged"][0]["aggressor"] == "U1"


def test_the_mcp_solve_noise_tool(write, tmp_path):
    import anyio
    from mcp import Client

    from coppernicus.mcp_server import INSTRUCTIONS, build_server
    path = write(board_model=False)

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            res = await c.call_tool("solve_noise", {"project_path": str(path), "aggressors": ["U1"]})
            tools = {t.name: t for t in (await c.list_tools()).tools}
            return res.structured_content, tools
    doc, tools = anyio.run(scenario)
    assert doc["kind"] == "noise" and doc["aggressors"][0]["estimate"]["text"]
    tool = tools["solve_noise"]
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
    words = " ".join(tool.description.split())
    assert "noise margin" in words and "never judged" in words and "confirm_large" in words
    assert "solve_noise" in INSTRUCTIONS


def test_the_cli_prints_the_estimate_first_and_warns_on_a_memory_risk(write, tmp_path, monkeypatch, capsys):
    from coppernicus.engine import ac
    path = write(board_model=False)
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)
    main(["noise", str(path), "--out", str(tmp_path / "o")])          # a person's run is not refused
    captured = capsys.readouterr()
    head = captured.out[:captured.out.index("Aggressor U1")]
    assert "Noise of U1: " in head and "harmonics" in head
    assert "warning:" in captured.err and "could exceed this machine's memory" in captured.err


def test_the_cli_writes_an_offline_html_page(write, tmp_path, capsys):
    path = write()
    page = tmp_path / "noise.html"
    assert main(["noise", str(path), "--json", "--out", str(tmp_path / "o"), "--html", str(page)]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert Path(doc["report_file"]) == page
    text = page.read_text(encoding="utf-8")
    parsed = Page(text)
    assert not parsed.unbalanced and not parsed.open, (parsed.unbalanced, parsed.open)
    assert "https://" not in text and "http://" not in text          # offline: everything inside
    (a,) = doc["aggressors"]
    assert len(parsed.images) == len(a["png_files"]) == 3            # waveforms, H and E maps
    for img in parsed.images:
        src = img["src"]
        assert src.startswith("data:image/png;base64,") and img.get("alt")
        assert base64.b64decode(src.split(",", 1)[1])[:8] == PNG
    words = parsed.words()
    assert "Verdict: pass" in words and "Aggressor U1" in words
    assert "Ripple (never judged)" in words and "LOAD" in words
    for header in ("Victim", "Peak", "Margin", "% of margin", "Verdict", "Reason"):
        assert header in words, header
    assert "SIG_LOOP" in words and "SIG_BURIED" in words
    assert "H field" in words and "E field" in words


def test_the_html_page_says_why_a_victim_is_not_judged_or_fails(write, tmp_path):
    from coppernicus.report import write_noise_report
    doc = Session(out_dir=tmp_path / "s").solve_noise(write(board_model=False, margin=0.5))
    a = doc["aggressors"][0]
    words = Page(write_noise_report(doc, tmp_path / "nj.html").read_text(encoding="utf-8")).words()
    assert "Verdict: not_judged" in words and "not judged" in words
    assert "shared-ground coupling left out" in words                 # the stand-in, and why
    assert "Stand-ins" in words
    # A victim that fails shows FAIL and the reason from the aggressor's failed list.
    v = next(v for v in a["victims"] if v["name"] == "SIG_LOOP")
    v["passed"], v["not_judged"] = False, ""
    a["failed"] = [{"item": "SIG_LOOP", "net": v["net"], "limit": "noise_margin_v (V)",
                    "reason": "peak 655 mV at U6.1, above its margin 500 mV"}]
    doc["verdict"] = a["verdict"] = "fail"
    words = Page(write_noise_report(doc, tmp_path / "f.html").read_text(encoding="utf-8")).words()
    assert "FAIL" in words and "above its margin 500 mV" in words


@pytest.mark.parametrize("board_model, margin, code, verdict", [
    (True, 0.5, EXIT_LIMIT, "fail"),
    (False, 0.5, EXIT_NOT_JUDGED, "not_judged"),
])
def test_ci_runs_noise_when_asked_and_its_verdict_sets_the_code(write, tmp_path, monkeypatch,
                                                                board_model, margin, code, verdict):
    path = write(board_model=board_model, margin=margin)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=False, noise=True)
    [entry] = index["projects"]
    assert entry["noise"]["verdict"] == verdict and entry["doc"]["noise_verdict"] == verdict
    assert index["exit_code"] == code
    if code == EXIT_NOT_JUDGED:                          # its entries join the project's
        assert any(u["rail"] is None and "SIG_LOOP" in u["item"] and "U1" in u["item"]
                   for u in entry["doc"]["not_judged"])
    md = ci.summary_markdown(index)
    assert "### Ripple and noise" in md and "| U1 | SIG_LOOP |" in md
    assert "never judged" in md
    if code == EXIT_LIMIT:
        plain = ci.solve_all([path.name], Path("ci2"), html=False)
        assert "noise" not in plain["projects"][0]                    # opt-in, off by default
        assert plain["exit_code"] == EXIT_OK


def test_ci_noise_error_sets_code_2_and_projects_without_aggressors_are_skipped(write, tmp_path,
                                                                               monkeypatch):
    path = write(board_model=False)
    p = Project.load(path)
    p.aggressors[0].fsw_hz = None
    p.save(path)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=False, noise=True)
    [entry] = index["projects"]
    assert "error" in entry["noise"] and index["exit_code"] == EXIT_ERROR
    assert "**Error:**" in ci.summary_markdown(index)
    p.aggressors, p.victims = [], []
    p.save(path)
    index = ci.solve_all([path.name], Path("ci2"), html=False, noise=True)
    assert "noise" not in index["projects"][0] and index["exit_code"] == EXIT_OK


def test_the_action_has_an_opt_in_noise_input(tmp_path):
    import yaml
    action = yaml.safe_load((Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8"))
    assert action["inputs"]["noise"]["default"] == "false"
    solve = next(s for s in action["runs"]["steps"] if s.get("id") == "solve")
    assert solve["env"]["COPPERNICUS_NOISE"] == "${{ inputs.noise }}" and "--noise" in solve["run"]
    # The helper takes --noise (no project matched: an error, not an unknown option).
    assert ci.main(["solve", "--noise", "--no-html", "--out", str(tmp_path / "o"),
                    str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
