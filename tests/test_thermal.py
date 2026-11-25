"""Board temperature (Task 8): the electro-thermal power-tree solve, its options in the project
file, its results, verdict and summary. Physics checks after KiPI's tests/dc/test_thermal.py;
KiPI itself is reached only through coppernicus.engine."""

from pathlib import Path

import pytest
import yaml

from coppernicus.project import Project, ProjectError, ThermalOptions

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
OLD_FILES = [EXAMPLES / "demo" / "demo.coppernicus.yml", EXAMPLES / "demo" / "demo-relaxed.coppernicus.yml",
             EXAMPLES / "power_chain" / "power_chain.coppernicus.yml"]


# ---------------------------------------------------------------------- project file

@pytest.mark.parametrize("path", OLD_FILES, ids=lambda p: p.name)
def test_old_files_have_no_thermal_key_and_load_the_defaults(path):
    assert "thermal" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    project = Project.load(path)
    assert project.thermal == ThermalOptions()
    assert "thermal" not in project.to_dict()


def test_thermal_round_trip_writes_only_the_keys_set(tmp_path):
    project = Project.load(OLD_FILES[0])
    project.thermal = ThermalOptions(enabled=True, max_rise_c=15.0)
    path = project.save(tmp_path / "t.coppernicus.yml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["thermal"] == {"enabled": True, "max_rise_c": 15.0}
    back = Project.load(path)
    assert back.thermal == project.thermal
    every = ThermalOptions(enabled=True, ambient_c=40.0, iterations=5, airflow_m_s=1.5, grid_mm=0.25,
                           load_heat=True, max_temp_c=105.0, max_rise_c=30.0, regulator_heat="part_pads")
    project.thermal = every
    assert Project.load(project.save(tmp_path / "t.coppernicus.yml")).thermal == every


@pytest.mark.parametrize("thermal, words", [
    ({"iterations": 0}, "iterations"),
    ({"iterations": 1.5}, "iterations"),
    ({"iterations": True}, "iterations"),
    ({"airflow_m_s": -1.0}, "airflow_m_s"),
    ({"grid_mm": -0.1}, "grid_mm"),
    ({"ambient_c": float("nan")}, "ambient_c"),
    ({"max_temp_c": float("inf")}, "max_temp_c"),
    ({"max_rise_c": "hot"}, "max_rise_c"),
    ({"enabled": "yes"}, "enabled"),
    ({"load_heat": 1}, "load_heat"),
    ({"regulator_heat": "package"}, "regulator_heat"),
    ({"edges": []}, "edges"),
    ("on", "thermal"),
])
def test_thermal_values_are_checked(thermal, words):
    data = {"board": "b.kicad_pcb", "rails": [], "thermal": thermal}
    with pytest.raises(ProjectError, match=words):
        Project.from_dict(data)


def test_whole_number_iterations_and_empty_limits_load():
    t = Project.from_dict({"thermal": {"iterations": 4.0, "max_temp_c": None}}).thermal
    assert t.iterations == 4 and isinstance(t.iterations, int) and t.max_temp_c is None
