"""The CI helper behind the GitHub Action: solve every matching project in its own process,
aggregate the exit codes, and write a job summary. On copies of the synthetic demo."""

import json
import shutil
from pathlib import Path

import pytest

from coppernicus import ci

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A KiCad-repository-like folder: the demo board and its two projects under hw/."""
    hw = tmp_path / "hw"
    hw.mkdir()
    for name in ("demo.kipi.json.gz", "demo.coppernicus.yml", "demo-relaxed.coppernicus.yml"):
        shutil.copy(DEMO / name, hw / name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_solve_all_aggregates_exit_codes_and_keeps_files_away_from_the_board(repo):
    index = ci.solve_all(["hw/*.coppernicus.yml"], Path("results"), top=3, html=True)
    by_project = {e["project"]: e for e in index["projects"]}
    assert set(by_project) == {"hw/demo.coppernicus.yml", "hw/demo-relaxed.coppernicus.yml"}
    assert by_project["hw/demo.coppernicus.yml"]["exit_code"] == 1             # 0.25 % exceeded
    assert by_project["hw/demo-relaxed.coppernicus.yml"]["exit_code"] == 0     # within 1 %
    assert index["exit_code"] == 1 and index["verdict"] == "fail"
    tight = by_project["hw/demo.coppernicus.yml"]
    folder = Path(tight["folder"])
    assert folder.parent == Path("results") and folder.name == "hw_demo.coppernicus"
    assert (folder / "demo.coppernicus_summary.json").is_file()
    assert (folder / "demo.coppernicus_report.html").is_file()
    assert len(list(folder.glob("*.png"))) == 4
    assert not list((repo / "hw").glob("*.png")) and not list((repo / "hw").glob("*.html"))
    saved = json.loads((Path("results") / ci.INDEX).read_text(encoding="utf-8"))
    assert saved["exit_code"] == 1
    kinds = [p["kind"] for p in tight["doc"]["rails"][0]["problems"]]
    assert kinds.count("via") == 3                                             # --top reached solve

    md = ci.summary_markdown(index, "https://example.invalid/artifacts/1")
    assert md.startswith("## Coppernicus: DC power integrity")
    assert ("| Project | Rail | Worst load | Worst of the copper | Max drop (%) | Copper loss (mW) "
            "| Verdict |") in md
    numbers = "| +3V3 | U1: 9.6 mV | 9.6 mV below 3.3 V | 0.291 | 34.3 |"
    assert f"| `hw/demo.coppernicus.yml` {numbers} **FAIL** (drop ≤ 0.25 %) |" in md
    assert f"| `hw/demo-relaxed.coppernicus.yml` {numbers} PASS (drop ≤ 1 %) |" in md
    assert "**FAIL** U1: 3.2904 V" in md and "<details><summary>Assumptions</summary>" in md
    assert "[download the results](https://example.invalid/artifacts/1)" in md


def test_html_off_errors_and_no_match(repo):
    index = ci.solve_all(["hw/demo-relaxed.coppernicus.yml"], Path("out"), html=False)
    assert index["exit_code"] == 0 and index["verdict"] == "pass"
    assert not list(Path("out").rglob("*.html"))

    bad = repo / "hw" / "bad.coppernicus.yml"
    bad.write_text("board: demo.kipi.json.gz\nrails: [\n  - net: : :\n", encoding="utf-8")
    index = ci.solve_all(["hw/bad.coppernicus.yml\nhw/demo-relaxed.coppernicus.yml"], Path("out2"))
    assert index["exit_code"] == 2 and index["verdict"] == "error"
    [err] = [e for e in index["projects"] if "error" in e]
    assert err["error"]["code"] == "invalid_project"
    assert "**error**: bad.coppernicus.yml is not valid YAML" in ci.summary_markdown(index)

    none = ci.solve_all(["nothing/*.coppernicus.yml"], Path("out3"))
    assert none["exit_code"] == 2 and none["error"]["code"] == "file_not_found"
    assert "No project file matched" in ci.summary_markdown(none)

    # A typo next to a good pattern must not pass unnoticed.
    typo = ci.solve_all(["hw/demo-relaxed.coppernicus.yml\nhw/demo-relaxd.coppernicus.yml"], Path("out4"))
    assert typo["exit_code"] == 2 and typo["unmatched"] == ["hw/demo-relaxd.coppernicus.yml"]
    assert typo["projects"][0]["exit_code"] == 0                       # the good one still ran
    md = ci.summary_markdown(typo)
    assert "No project file matched: hw/demo-relaxd.coppernicus.yml" in md and "PASS (drop" in md

    # The results folder itself must not be a board's folder (the index would land there).
    near = ci.solve_all(["hw/demo-relaxed.coppernicus.yml"], Path("hw"))
    assert near["exit_code"] == 2 and "holds a board file" in near["error"]["message"]
    assert not (repo / "hw" / ci.INDEX).exists()


def test_refuses_to_write_into_the_board_folder(repo):
    # A project whose results subfolder would be the board's own folder.
    board_dir = repo / "x.coppernicus"
    board_dir.mkdir()
    shutil.copy(DEMO / "demo.kipi.json.gz", board_dir / "board.kipi.json.gz")
    text = (DEMO / "demo-relaxed.coppernicus.yml").read_text(encoding="utf-8")
    text = text.replace("demo.kipi.json.gz", "x.coppernicus/board.kipi.json.gz")
    (repo / "x.coppernicus.yml").write_text(text, encoding="utf-8")
    index = ci.solve_all(["x.coppernicus.yml"], Path("."))
    assert index["exit_code"] == 2 and "board's own folder" in index["projects"][0]["error"]["message"]
    assert not list(board_dir.glob("*.png"))


def test_summary_command_and_cell_escaping(repo, capsys):
    assert ci.main(["summary", "--out", "missing"]) == 2
    assert "did not run" in capsys.readouterr().out
    index = {"verdict": "not_judged", "projects": [{"project": "a|b.coppernicus.yml", "exit_code": 0, "doc": {
        "verdict": "not_judged", "rails": [{"net": "<V|1>", "verdict": "not_judged", "limits": {},
                                            "loads": [], "problems": [], "assumptions": []}]}}]}
    md = ci.summary_markdown(index)
    assert "`a\\|b.coppernicus.yml`" in md and "&lt;V\\|1&gt;" in md and "not judged (no limits)" in md
    assert "Not judged: no limits" in md


def test_a_crash_is_an_error_not_an_exceeded_limit(repo, monkeypatch):
    """Python exits with 1 on an uncaught exception, the same code as "limit exceeded": a run
    that printed no result must count as an error (2)."""
    import subprocess

    def crashed(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Traceback (most recent call last)\n")
    monkeypatch.setattr(ci.subprocess, "run", crashed)
    index = ci.solve_all(["hw/demo-relaxed.coppernicus.yml"], Path("out"))
    assert index["exit_code"] == 2 and index["verdict"] == "error"
    assert index["projects"][0]["exit_code"] == 2


def test_summary_of_a_power_tree_project(tmp_path, monkeypatch):
    """The v2 example: its VIN rail has no load of its own but feeds U1's input, so it is
    solved with U1's estimated input current as its load; the rails carry the power-tree
    assumption lines, which the summary lists once. A rail with nothing to draw is skipped."""
    chain = Path(__file__).resolve().parents[1] / "examples" / "power_chain"
    shutil.copytree(chain, tmp_path / "chain")
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["chain/*.coppernicus.yml"], Path("out"), html=False)
    assert index["exit_code"] == 0 and index["verdict"] != "error"
    md = ci.summary_markdown(index)
    assert "| VIN | U1 (input, estimated):" in md
    skipped = {"verdict": "not_judged", "projects": [{"project": "p.coppernicus.yml", "exit_code": 0, "doc": {
        "verdict": "not_judged", "rails": [{"net": "VX", "skipped": {"code": "missing_load", "message": "m",
                                                                    "hint": "h"}}]}}]}
    assert "| VX | – | – | – | – | skipped: nothing to draw |" in ci.summary_markdown(skipped)
    rails = index["projects"][0]["doc"]["rails"]
    lines = {a for r in rails if "assumptions" in r for a in r["assumptions"]}
    assert all(md.count(ci._cell(a)) == 1 for a in lines)        # each assumption listed once


def test_the_helper_itself_crashing_exits_2(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise ZeroDivisionError("forced for the test")
    monkeypatch.setattr(ci, "solve_all", boom)
    assert ci.main(["solve", "--out", "out", "x.coppernicus.yml"]) == 2
    assert capsys.readouterr().err == "error: internal error: ZeroDivisionError: forced for the test\n"


def test_tree_input_adds_the_power_tree_tables_to_the_summary(tmp_path, monkeypatch):
    """With tree, each project is also solved as a whole power tree and its VRM and load
    tables go into the job summary; the tree's verdict is shown but does not set the exit
    code (the rails' solve does)."""
    chain = Path(__file__).resolve().parents[1] / "examples" / "power_chain"
    shutil.copytree(chain, tmp_path / "chain")
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["chain/*.coppernicus.yml"], Path("out"), html=False, tree=True)
    [entry] = index["projects"]
    assert entry["tree"]["kind"] == "power_tree" and index["exit_code"] == entry["exit_code"]
    md = ci.summary_markdown(index)
    assert "### Power tree: `chain/power_chain.coppernicus.yml`" in md
    assert "| VRM | Kind | Group |" in md and "| U1 | smps | +3V3 |" in md
    assert "| Load | Group |" in md and "| U3 | +1V8 |" in md
    plain = ci.solve_all(["chain/*.coppernicus.yml"], Path("out2"), html=False)
    assert "tree" not in plain["projects"][0] and "Power tree" not in ci.summary_markdown(plain)
