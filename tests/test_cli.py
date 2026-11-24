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
    assert rail["energy_balance_error"] < 1e-6


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
