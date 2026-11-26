"""The advanced mesh settings near plated holes: ``min_edge_mm`` (the smallest element, at a
hole wall) and ``grading`` (how fast elements grow away from it), KiPI's own options. Unset,
KiPI's defaults apply and nothing changes; set, they reach every DC solve (a rail, the power
tree) through the path max_edge_mm takes: the project file, the service and MCP settings,
write_project and the app. Synthetic boards only."""

import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.solve import engine_settings
from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board, via_board
from coppernicus.project import Load, Project, ProjectError, Rail, Settings, Source
from coppernicus.service import ServiceError, Session
from coppernicus.service.convert import settings_from_dict, settings_to_dict

MESH_LINE = "Mesh: largest edge 0.5 mm on planes, refined toward plated holes."


def via_rail() -> Rail:
    return Rail("VCC", sources=[Source("J1", 1.0)], loads=[Load("U1", 0.5)])


def test_unset_they_are_none_and_kipis_defaults_apply():
    s = Settings()
    assert s.min_edge_mm is None and s.grading is None
    solve, notes = engine_settings(s)
    from kipi.dc.solver.setup import SolveSettings
    assert (solve.min_edge_mm, solve.grading) == (SolveSettings().min_edge_mm, SolveSettings().grading)
    assert MESH_LINE in notes


def test_set_they_reach_kipi_and_the_assumption_line():
    solve, notes = engine_settings(Settings(min_edge_mm=0.01, grading=0.1))
    assert (solve.min_edge_mm, solve.grading) == (0.01, 0.1)
    line = next(n for n in notes if n.startswith("Mesh:"))
    assert "0.01 mm" in line and "grading 0.1" in line


def test_a_finer_mesh_at_the_via_gives_more_elements_the_same_result_otherwise():
    board = Board(via_board())
    base = solve_rail(board, via_rail())
    again = solve_rail(board, via_rail(), Settings())
    fine = solve_rail(board, via_rail(), Settings(min_edge_mm=0.01, grading=0.1))
    tris = [sum(len(lf.tris) for lf in r.layers) for r in (base, again, fine)]
    assert tris[0] == tris[1] < tris[2]
    assert base.max_drop == again.max_drop
    assert fine.max_drop == pytest.approx(base.max_drop, rel=0.02)


def test_the_power_tree_setup_takes_them():
    from coppernicus.engine.pisetup import to_pisetup
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    project = power_chain_project()
    project.settings = Settings(min_edge_mm=0.01, grading=0.1)
    setup = to_pisetup(Board(power_chain_board()), project)
    assert (setup.settings.min_edge_mm, setup.settings.grading) == (0.01, 0.1)


@pytest.mark.parametrize("key, value", [
    ("min_edge_mm", 0), ("min_edge_mm", -0.01), ("min_edge_mm", float("nan")),
    ("min_edge_mm", float("inf")), ("min_edge_mm", "fine"), ("min_edge_mm", True),
    ("grading", -0.1), ("grading", 2.5), ("grading", float("nan")), ("grading", "x"), ("grading", False),
])
def test_bad_values_are_refused(key, value, tmp_path):
    with pytest.raises(ProjectError, match=key):
        Settings(**{key: value})
    path = tmp_path / "bad.coppernicus.yml"
    path.write_text(yaml.safe_dump({"board": "x.kicad_pcb", "settings": {key: value}}), encoding="utf-8")
    with pytest.raises(ProjectError, match=key):
        Project.load(path)


def test_the_grading_range_is_kipis_0_to_2_and_0_turns_it_off():
    assert Settings(grading=0.0).grading == 0.0 and Settings(grading=2.0).grading == 2.0
    assert Settings(min_edge_mm=0.001).min_edge_mm == 0.001


def test_the_project_file_holds_them_only_when_set(tmp_path):
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    p = Project(board="strip.kipi.json.gz", rails=[], path=tmp_path / "p.coppernicus.yml")
    p.save()
    text = (tmp_path / "p.coppernicus.yml").read_text(encoding="utf-8")
    assert "min_edge_mm" not in text and "grading" not in text
    p.settings.min_edge_mm, p.settings.grading = 0.01, 0.1
    p.save()
    back = Project.load(tmp_path / "p.coppernicus.yml").settings
    assert (back.min_edge_mm, back.grading) == (0.01, 0.1)


def test_the_service_settings_take_and_check_them():
    s, _ = settings_from_dict({"min_edge_mm": 0.01, "grading": 0.1})
    assert (s.min_edge_mm, s.grading) == (0.01, 0.1)
    kept, _ = settings_from_dict({"copper_temp_c": 30.0}, base=s)          # the base's stay
    assert (kept.min_edge_mm, kept.grading) == (0.01, 0.1)
    assert settings_to_dict(s)["min_edge_mm"] == 0.01 and settings_to_dict(s)["grading"] == 0.1
    assert "min_edge_mm" not in settings_to_dict(Settings())
    assert "grading" not in settings_to_dict(Settings())
    for bad in ({"grading": 3}, {"min_edge_mm": 0}, {"min_edge_mm": "x"}):
        with pytest.raises(ServiceError) as e:
            settings_from_dict(bad)
        assert e.value.code == "invalid_input"


def test_the_mcp_settings_spec_has_them():
    from pydantic import ValidationError

    from coppernicus.mcp_server import SettingsSpec
    spec = SettingsSpec(min_edge_mm=0.01, grading=0.1)
    assert spec.model_dump(exclude_none=True) == {"min_edge_mm": 0.01, "grading": 0.1}
    for bad in ({"grading": 2.5}, {"grading": -1}, {"min_edge_mm": 0}):
        with pytest.raises(ValidationError):
            SettingsSpec(**bad)


def test_solve_rail_and_write_project_take_them(tmp_path):
    board = tmp_path / "strip.kipi.json.gz"
    save_snapshot(strip_board(), board)
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(board)["board_id"]
    doc = session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": 1.0}],
                             [{"ref": "U1", "current_a": 0.5}],
                             settings={"min_edge_mm": 0.01, "grading": 0.1})
    assert doc["settings"]["min_edge_mm"] == 0.01 and doc["settings"]["grading"] == 0.1
    path = tmp_path / "p.coppernicus.yml"
    session.write_project(path, board_path=board, settings={"min_edge_mm": 0.01, "grading": 0.1})
    assert (Project.load(path).settings.min_edge_mm, Project.load(path).settings.grading) == (0.01, 0.1)
    session.write_project(path, settings={"min_edge_mm": None}, overwrite=True)
    assert Project.load(path).settings.min_edge_mm is None
    with pytest.raises(ServiceError) as e:
        session.write_project(path, settings={"grading": 5}, overwrite=True)
    assert e.value.code == "invalid_project" and "grading" in e.value.message


def test_the_app_settings_edit_them():
    pytest.importorskip("PySide6")
    body = textwrap.dedent('''
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from coppernicus.app.panels import RailPanel
        from coppernicus.project import Settings
        panel = RailPanel()
        s = Settings()
        panel.set_settings(s)
        assert panel.min_edge.text() == "" and panel.grading.text() == ""
        assert "0.02" in panel.min_edge.placeholderText() and "0.2" in panel.grading.placeholderText()
        panel.min_edge.setText("0.01"); panel.min_edge.editingFinished.emit()
        panel.grading.setText("0.1"); panel.grading.editingFinished.emit()
        assert (s.min_edge_mm, s.grading) == (0.01, 0.1), (s.min_edge_mm, s.grading)
        panel.min_edge.setText(""); panel.min_edge.editingFinished.emit()
        assert s.min_edge_mm is None and s.grading == 0.1
        panel.set_settings(Settings(min_edge_mm=0.005, grading=0.0))
        assert (panel.min_edge.text(), panel.grading.text()) == ("0.005", "0"), panel.grading.text()
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_math_check_of_the_kipi_defaults_quoted_in_the_docs():
    from kipi.dc.solver.setup import SolveSettings
    assert SolveSettings().min_edge_mm == 0.02 and SolveSettings().grading == 0.2
    text = (Path(__file__).resolve().parents[1] / "docs" / "technical.md").read_text(encoding="utf-8")
    assert "`min_edge_mm` (0.02 mm)" in text and "`grading`\n(0.2 mm per mm" in text
    assert math.isclose(SolveSettings().max_edge_mm, 0.5)
