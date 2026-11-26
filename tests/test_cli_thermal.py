"""``coppernicus solve --tree`` / ``--thermal``: the board temperature from the command line,
on the committed demo-thermal example (the demo rail at four times its currents, with
thermal enabled and max_rise_c 15)."""

import json
import shutil
from pathlib import Path

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, main
from coppernicus.service import Session

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"


@pytest.fixture()
def demo(tmp_path):
    folder = tmp_path / "demo"
    folder.mkdir()
    for name in ("demo.kipi.json.gz", "demo-thermal.coppernicus.yml", "demo.coppernicus.yml"):
        shutil.copy(DEMO / name, folder / name)
    return folder


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def test_tree_on_a_thermal_project_solves_the_board_temperature(demo, tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["solve", str(demo / "demo-thermal.coppernicus.yml"), "--tree", "--out", str(out)])
    assert code == EXIT_LIMIT
    text = capsys.readouterr().out
    for words in ("Board temperature", "F.Cu", "B.Cu", "nearest pad", "J1.1", "FAIL", "Verdict: fail",
                  "load drops, copper at the ambient"):
        assert words in text, words
    summary = next(out.glob("*_tree_summary.json"))           # named as every --tree summary
    doc = json.loads(summary.read_text(encoding="utf-8"))
    assert doc["thermal"]["verdict"] == "fail" and doc["verdict"] == "fail"
    stem = summary.name[:-len("_tree_summary.json")]
    pngs = sorted(p.name for p in out.glob("*.png"))
    assert pngs == [f"{stem}_temperature_B.Cu.png", f"{stem}_temperature_F.Cu.png"]
    assert all(f"Map: {out / name}" in text for name in pngs)


def test_the_files_keep_the_project_files_name_as_ac_noise_and_tolerance(demo, tmp_path, capsys):
    """<project> is the project file's name without .yml, its "-" kept (as safe_file_name, the
    other commands' stem); "+" and "-" become P and N only in net names."""
    out = tmp_path / "out"
    main(["solve", str(demo / "demo-thermal.coppernicus.yml"), "--thermal", "--html", "--json",
          "--out", str(out)])
    doc = _json(capsys)
    stem = "demo-thermal.coppernicus"
    assert sorted(p.name for p in out.iterdir()) == [
        f"{stem}_temperature_B.Cu.png", f"{stem}_temperature_F.Cu.png", f"{stem}_tree_report.html",
        f"{stem}_tree_summary.json"]
    assert Path(doc["summary_file"]).name == f"{stem}_tree_summary.json"
    assert Path(doc["report_file"]).name == f"{stem}_tree_report.html"


@pytest.mark.parametrize("flags", [["--thermal"], ["--thermal", "--tree"], ["--tree", "--thermal"]])
def test_thermal_forces_the_board_temperature_in_any_order(demo, tmp_path, capsys, flags):
    """demo.coppernicus.yml does not enable thermal: --thermal does, with or without --tree."""
    out = tmp_path / "out"
    code = main(["solve", str(demo / "demo.coppernicus.yml"), *flags, "--json", "--out", str(out)])
    doc = _json(capsys)
    assert code == EXIT_LIMIT and doc["kind"] == "power_tree"
    assert doc["thermal"]["iterations"] == 3 and doc["thermal"]["verdict"] == "not_judged"
    pngs = sorted(out.glob("*_temperature_*.png"))
    assert len(pngs) == 2 and sorted(doc["maps"]) == [p.as_posix() for p in pngs]    # as per-rail JSON
    saved = json.loads(Path(doc["summary_file"]).read_text(encoding="utf-8"))
    assert saved["maps"] == doc["maps"]


def test_no_maps_and_plain_tree(demo, tmp_path, capsys):
    out = tmp_path / "out"
    main(["solve", str(demo / "demo-thermal.coppernicus.yml"), "--thermal", "--no-maps", "--json",
          "--out", str(out)])
    doc = _json(capsys)
    assert "thermal" in doc and "maps" not in doc and not list(out.glob("*.png"))
    main(["solve", str(demo / "demo.coppernicus.yml"), "--tree", "--json", "--out", str(out)])
    assert "thermal" not in _json(capsys) and not list(out.glob("*.png"))


def test_thermal_refuses_rail(demo, capsys):
    assert main(["solve", str(demo / "demo.coppernicus.yml"), "--thermal", "--rail", "+3V3"]) == EXIT_ERROR
    assert "--tree" in capsys.readouterr().err


def test_html_report_takes_the_thermal_document(demo, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["solve", str(demo / "demo-thermal.coppernicus.yml"), "--tree", "--html", "--json",
                 "--out", str(out)]) == EXIT_LIMIT
    assert Path(_json(capsys)["report_file"]).is_file()


def test_session_solve_tree_follows_the_project(demo, tmp_path):
    """The agent interface's solve_tree reads thermal.enabled too, through the board-fields
    merge, and returns the same document as the command line."""
    doc = Session(out_dir=tmp_path / "s").solve_tree(demo / "demo-thermal.coppernicus.yml")
    assert doc["thermal"]["verdict"] == "fail" and doc["verdict"] == "fail"
    assert doc["thermal"]["layers"][0]["nearest_pad"] == {"ref": "J1", "pad": "1", "distance_mm": 0.0}
