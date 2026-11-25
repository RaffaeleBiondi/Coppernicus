"""The board temperature in the agent and CI interfaces: a rail-by-rail solve with
board-temperature limits set lists them as not judged (the temperature is a power-tree solve),
so the CLI and CI exit 3 instead of passing silently. Synthetic examples only."""

import shutil
from pathlib import Path

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_NOT_JUDGED, main
from coppernicus.project import Project
from coppernicus.service import Session

ROOT = Path(__file__).resolve().parents[1]
REASON = "solved with the power tree only"


@pytest.fixture()
def relaxed(tmp_path):
    shutil.copytree(ROOT / "examples" / "demo", tmp_path / "demo")
    return tmp_path / "demo" / "demo-relaxed.coppernicus.yml"


def with_thermal_limit(path: Path, **limits) -> Path:
    p = Project.load(path)
    for k, v in limits.items():
        setattr(p.thermal, k, v)
    p.save()
    return path


def test_a_rail_solve_lists_the_board_temperature_limits_as_not_judged(relaxed, tmp_path, capsys,
                                                                     monkeypatch):
    base = Session(out_dir=tmp_path / "o0").solve_project(relaxed)
    assert base["verdict"] == "pass" and base["not_judged"] == []          # the demo passes 1 %
    with_thermal_limit(relaxed, max_rise_c=20.0, max_temp_c=60.0)
    doc = Session(out_dir=tmp_path / "o1").solve_project(relaxed)
    assert doc["verdict"] == "not_judged"
    board = [u for u in doc["not_judged"] if u["item"] == "board temperature"]
    assert {u["limit"] for u in board} == {"max_rise_c", "max_temp_c"}
    assert all(REASON in u["reason"] and u["rail"] is None for u in board)
    assert "board temperature" in doc["verdict_note"]
    capsys.readouterr()
    assert main(["solve", str(relaxed), "--no-maps", "--out", str(tmp_path / "cli")]) == EXIT_NOT_JUDGED
    assert "NOT JUDGED board temperature (max_rise_c)" in capsys.readouterr().out
    # CI: the run's code is 3 and the job summary says why.
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["demo/demo-relaxed.coppernicus.yml"], Path("ci"), html=False)
    assert index["exit_code"] == EXIT_NOT_JUDGED and index["verdict"] == "not_judged"
    assert "NOT JUDGED board temperature (max_rise_c)" in ci.summary_markdown(index)
