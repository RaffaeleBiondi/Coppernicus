"""The command line on a synthetic board saved as a snapshot."""

import json

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_OK, main
from coppernicus.engine.synthetic import demo_board, save_snapshot
from coppernicus.project import Limits, Load, Project, Rail, Source


@pytest.fixture()
def demo_project(tmp_path):
    save_snapshot(demo_board(), tmp_path / "demo.kipi.json.gz")
    project = Project(board="demo.kipi.json.gz", rails=[Rail(
        "+3V3", sources=[Source("J1", 3.3)], loads=[Load("U1", 2.0), Load("U3", 0.3)])])
    return project.save(tmp_path / "demo.coppernicus.yml")


def test_nets_and_parts(demo_project, capsys):
    board = str(demo_project.parent / "demo.kipi.json.gz")
    assert main(["nets", board]) == EXIT_OK
    assert "+3V3" in capsys.readouterr().out
    assert main(["parts", board, "+3V3"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "J1" in out and "U3" in out


def test_solve_writes_maps_and_summary(demo_project, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert main(["solve", str(demo_project), "--out", str(out_dir)]) == EXIT_OK
    pngs = sorted(p.name for p in out_dir.glob("*.png"))
    assert pngs == ["P3V3_B.Cu_current.png", "P3V3_B.Cu_drop.png",
                    "P3V3_F.Cu_current.png", "P3V3_F.Cu_drop.png"]
    summary = json.loads((out_dir / "demo.coppernicus_summary.json").read_text())
    [rail] = summary["rails"]
    assert rail["net"] == "+3V3" and len(rail["loads"]) == 2
    assert rail["energy_balance_rel_error"] < 1e-6
    assert "Summary:" in capsys.readouterr().out
    assert not list(out_dir.glob("*.html"))                    # no report unless asked


def test_solve_exit_code_reports_an_exceeded_limit(demo_project, tmp_path):
    p = Project.load(demo_project)
    p.rails[0].limits = Limits(max_drop_pct=0.01)
    p.save()
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(tmp_path)]) == EXIT_LIMIT


def test_errors_are_reported_not_raised(demo_project, tmp_path, capsys):
    p = Project.load(demo_project)
    p.rails[0].loads.append(Load("U99", 1.0))
    p.save()
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(tmp_path)]) == EXIT_ERROR
    assert "U99 has no pads" in capsys.readouterr().err
    assert main(["nets", str(tmp_path / "missing.kicad_pcb")]) == EXIT_ERROR


def test_broken_project_yaml_is_a_clear_error(tmp_path, capsys):
    bad = tmp_path / "bad.coppernicus.yml"
    bad.write_text("board: x.kicad_pcb\nrails: [\n  - net: : :\n")
    assert main(["solve", str(bad), "--out", str(tmp_path)]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error: bad.coppernicus.yml is not valid YAML at line 3, column 3")
    assert "Traceback" not in err
    assert main(["solve", str(bad), "--json", "--out", str(tmp_path)]) == EXIT_ERROR
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_project"


def test_html_report_and_open(demo_project, tmp_path, capsys, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    out_dir = tmp_path / "out"
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(out_dir), "--html"]) == EXIT_OK
    report = out_dir / "demo.coppernicus_report.html"
    assert report.exists() and f"Report: {report}" in capsys.readouterr().out
    assert opened == []
    custom = tmp_path / "elsewhere" / "r.html"
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(out_dir), "--html", str(custom),
                 "--open", "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["report_file"] == str(custom) and custom.exists()
    assert opened == [custom.resolve().as_uri()]
    summary = json.loads((out_dir / "demo.coppernicus_summary.json").read_text())
    assert summary["report_file"] == str(custom)
    # Without --out, everything goes to results/ next to the project, never next to a board
    # elsewhere.
    assert main(["solve", str(demo_project), "--no-maps", "--html"]) == EXIT_OK
    assert (demo_project.parent / "results" / "demo.coppernicus_report.html").exists()


# ---------------------------------------------------------------------- --json

def test_json_nets_and_parts(demo_project, capsys):
    board = str(demo_project.parent / "demo.kipi.json.gz")
    assert main(["nets", board, "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["board"]["name"] == "demo.kipi.json.gz"
    assert doc["nets"] == [{"name": "+3V3", "kind": "power", "layers": ["F.Cu", "B.Cu"]}]
    assert main(["parts", board, "+3V3", "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert [p["ref"] for p in doc["parts"]] == ["J1", "U1", "U2", "U3"]
    assert main(["parts", board, "+3v3", "--json"]) == EXIT_ERROR
    out = capsys.readouterr()
    assert json.loads(out.out)["error"] == {"code": "net_not_found",
                                            "message": "No net '+3v3' on this board",
                                            "hint": "did you mean '+3V3'?"}
    assert "No net" in out.err
    assert main(["nets", str(demo_project.parent / "missing.kicad_pcb"), "--json"]) == EXIT_ERROR
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "file_not_found"


def test_json_solve_prints_only_json_and_keeps_files(demo_project, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert main(["solve", str(demo_project), "--out", str(out_dir), "--json", "-v"]) == EXIT_OK
    captured = capsys.readouterr()
    doc = json.loads(captured.out)                       # stdout is the JSON document alone
    assert "Meshing" in captured.err                     # -v progress went to stderr
    [rail] = doc["rails"]
    assert rail["net"] == "+3V3" and rail["verdict"] == "not_judged" and doc["verdict"] == "not_judged"
    assert [ld["name"] for ld in rail["loads"]] == ["U1", "U3"]
    assert sorted(p.rsplit("/", 1)[-1] for p in rail["maps"]) == [
        "P3V3_B.Cu_current.png", "P3V3_B.Cu_drop.png", "P3V3_F.Cu_current.png", "P3V3_F.Cu_drop.png"]
    # The summary file is the same document as stdout.
    summary = json.loads((out_dir / "demo.coppernicus_summary.json").read_text())
    assert summary == doc
    assert doc["summary_file"].endswith("demo.coppernicus_summary.json")
    # Every problem is there: all loads, then the top hotspots and the top vias.
    kinds = [p["kind"] for p in rail["problems"]]
    assert rail["problems_total"] == len(kinds)
    assert kinds.count("load") == 2 and kinds.count("hotspot") == 5 and kinds.count("via") == 5


def test_top_sets_hotspots_and_vias_per_rail(demo_project, tmp_path, capsys):
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(tmp_path), "--json",
                 "--top", "8"]) == EXIT_OK
    kinds = [p["kind"] for p in json.loads(capsys.readouterr().out)["rails"][0]["problems"]]
    assert kinds.count("via") == 8 and kinds.count("hotspot") == 8 and kinds.count("load") == 2


def test_json_solve_exit_codes(demo_project, tmp_path, capsys):
    p = Project.load(demo_project)
    p.rails[0].limits = Limits(max_drop_pct=0.01)
    p.save()
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(tmp_path), "--json"]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "fail" and doc["rails"][0]["limits"] == {"max_drop_pct": 0.01}
    p.rails[0].loads.append(Load("U99", 1.0))
    p.save()
    assert main(["solve", str(demo_project), "--no-maps", "--out", str(tmp_path), "--json"]) == EXIT_ERROR
    out = capsys.readouterr()
    assert json.loads(out.out)["error"]["code"] == "part_not_on_net"
    assert "U99 has no pads" in out.err
