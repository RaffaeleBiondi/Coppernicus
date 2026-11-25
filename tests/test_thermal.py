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


# ---------------------------------------------------------------------- solves

import copy  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402

import numpy as np  # noqa: E402
from shapely.geometry import box  # noqa: E402

from coppernicus.engine import Board, Cancelled, solve_rail  # noqa: E402
from coppernicus.engine.synthetic import (  # noqa: E402
    B,
    F,
    Strip,
    SyntheticBoard,
    demo_board,
    power_chain_board,
    power_chain_project,
    strip_board,
    two_layer_stackup,
)
from coppernicus.engine.thermal import engine_defaults, solve_kipi_reference  # noqa: E402
from coppernicus.engine.tree import solve_project_tree  # noqa: E402
from coppernicus.power_tree import tree_summary  # noqa: E402
from coppernicus.problems import FAIL, INFO  # noqa: E402
from coppernicus.project import Load, Rail, Source  # noqa: E402
from coppernicus.temperature import (  # noqa: E402
    NOT_SOLVED,
    apply_thermal_limits,
    judge,
    temperature_text,
    thermal_verdict,
)

TCR = 0.00393          # KiPI's α (kipi/dc/solver/setup.py), as tests/test_engine.py retypes ρ


def strip_project(**thermal):
    return Project(rails=[Rail(Strip.NET, sources=[Source("J1", 3.3)], loads=[Load("U1", 1.0)])],
                   thermal=ThermalOptions(enabled=True, **thermal))


def demo_thermal_project(**thermal):
    """examples/demo/demo-thermal.coppernicus.yml: the demo rail at four times its currents."""
    return Project(rails=[Rail("+3V3", sources=[Source("J1", 3.3, name="Supply")],
                               loads=[Load("U1", 8.0), Load("U2", 6.0), Load("U3", 1.2)])],
                   thermal=ThermalOptions(**{"enabled": True, "max_rise_c": 15.0, **thermal}))


@pytest.fixture(scope="module")
def strip():
    return Board(strip_board())


@pytest.fixture(scope="module")
def demo():
    return Board(demo_board(led_net=True))


@pytest.fixture(scope="module")
def demo_tree(demo):
    return solve_project_tree(demo, demo_thermal_project())


def test_options_default_to_kipis():
    assert ThermalOptions() == engine_defaults()


@pytest.mark.parametrize("path", OLD_FILES, ids=lambda p: p.name)
def test_old_files_solve_the_tree_as_before(path):
    project = Project.load(path)
    board = Board.open(project.board_path())
    plain, off = solve_project_tree(board, project), solve_project_tree(board, project, thermal=False)
    assert plain.thermal is None and plain.thermal_limits is None
    doc = tree_summary(plain)
    assert doc == tree_summary(off) and "thermal" not in doc


def test_thermal_follows_the_project_unless_forced(strip):
    project = strip_project(iterations=1)
    assert solve_project_tree(strip, project, thermal=False).thermal is None
    assert solve_project_tree(strip, project).thermal is not None
    project.thermal.enabled = False
    assert solve_project_tree(strip, project).thermal is None
    assert solve_project_tree(strip, project, thermal=True).thermal is not None


# ---------------------------------------------------------------------- drift test

@pytest.mark.parametrize("which", ["demo", "chain"])
def test_our_loop_is_bit_identical_to_kipis(which, demo):
    """With KiPI's regulator-heat placement our copy of the loop gives KiPI's own
    solve_electrothermal to the last bit: temperatures, history and every load drop."""
    if which == "demo":
        board, project = demo, demo_thermal_project()
    else:
        board, project = Board(power_chain_board()), power_chain_project()
        project.thermal = ThermalOptions(enabled=True)
    ours, kipi = solve_project_tree(board, project), solve_kipi_reference(board, project)
    for a, b in zip(ours.thermal.maps, kipi.thermal.maps, strict=True):
        assert np.array_equal(np.isnan(a.t_c), np.isnan(b.t_c))
        assert np.array_equal(a.t_c, b.t_c, equal_nan=True)
    assert ours.thermal.history == kipi.thermal.history
    assert [ld.drop_v for ld in ours.loads] == [ld.drop_v for ld in kipi.loads]
    assert [r.t_max_c for r in ours.thermal.copper] == [r.t_max_c for r in kipi.thermal.copper]


# ---------------------------------------------------------------------- physics (KiPI's tests)

def _convected_w(temp):
    """Σ d²·h·(rise on top + rise on bottom): the heat the two faces give to the air (no
    solder mask on the synthetic boards)."""
    d, h = temp.grid_mm, temp.h_w_m2k * 1e-6
    top, bottom = temp.maps[0].t_c, temp.maps[-1].t_c
    inside = ~np.isnan(top)
    return float(d * d * h * ((top - temp.ambient_c)[inside] + (bottom - temp.ambient_c)[inside]).sum())


@pytest.mark.parametrize("airflow, h", [(0.0, 10.0), (2.0, 17.6)])
def test_the_faces_convect_all_the_heat(strip, demo, demo_tree, airflow, h):
    temps = [solve_project_tree(strip, strip_project(airflow_m_s=airflow)).thermal]
    temps.append(demo_tree.thermal if airflow == 0 else
                 solve_project_tree(demo, demo_thermal_project(airflow_m_s=airflow, iterations=1)).thermal)
    for temp in temps:
        assert temp.h_w_m2k == pytest.approx(h, rel=1e-12)
        assert _convected_w(temp) == pytest.approx(temp.heat_w, rel=1e-9)


def test_airflow_cools_the_board(strip):
    peaks = [solve_project_tree(strip, strip_project(airflow_m_s=v, iterations=1)).thermal.peak_c
             for v in (0.0, 1.0, 3.0)]
    assert peaks[0] > peaks[1] > peaks[2]


def test_iteration_one_solves_the_copper_at_the_ambient(strip):
    hot = solve_project_tree(strip, strip_project(ambient_c=85.0, iterations=1))
    cold = solve_project_tree(strip, strip_project(ambient_c=20.0, iterations=1))
    assert hot.loads[0].drop_v / cold.loads[0].drop_v == pytest.approx(1 + TCR * 65, rel=1e-3)
    for tree in (hot, cold):        # one iteration: the drop is the one at the ambient
        assert tree.thermal.loads[0]["drop_ambient_v"] == tree.loads[0].drop_v
        assert tree.thermal.settled is None


def test_hot_copper_raises_the_drop_over_the_ambient(strip):
    """The strip at 1 A: 37.928 mV at 20 °C, 38.673 mV with the copper at the 25 °C ambient,
    40.020 mV once the copper has heated itself (3 iterations, settled)."""
    plain = solve_project_tree(strip, strip_project(), thermal=False)
    tree = solve_project_tree(strip, strip_project())
    load = tree.thermal.loads[0]
    assert plain.loads[0].drop_v * 1e3 == pytest.approx(37.928, abs=1e-3)
    assert load["drop_ambient_v"] * 1e3 == pytest.approx(38.673, abs=1e-3)
    assert load["drop_v"] * 1e3 == pytest.approx(40.020, abs=1e-3) and load["drop_v"] == tree.loads[0].drop_v
    dts = [h["dt_max_c"] for h in tree.thermal.history]
    assert tree.thermal.settled is True and dts == sorted(dts, reverse=True)


def test_maps_rows_and_peak(demo, demo_tree):
    temp = demo_tree.thermal
    assert [m.layer_name for m in temp.maps] == ["F.Cu", "B.Cu"]
    for m in temp.maps:
        assert m.t_c.shape == (temp.grid_ny, temp.grid_nx) == (len(m.y_mm), len(m.x_mm))
        assert m.at(-5.0, -5.0) != m.at(-5.0, -5.0)           # off the grid: NaN
        x, y, t = m.hottest()
        assert m.at(x, y) == t == m.peak_c
    assert temp.peak_c == max(m.peak_c for m in temp.maps) == temp.map(temp.peak_layer).peak_c
    assert temp.peak_layer == "F.Cu" and temp.map("F.Cu").peak_c > temp.map("B.Cu").peak_c   # as the rows
    assert temp.grid_cells == int(np.count_nonzero(~np.isnan(temp.maps[0].t_c)))
    assert all(r.t_max_c <= temp.peak_c + 1e-9 for r in temp.copper)
    # The numbers of the gate (§9): settled after 3 iterations, both layers over 15 °C of rise.
    assert temp.peak_c == pytest.approx(42.31, abs=0.01) and temp.settled is True
    rows = {r.layer_name: r for r in temp.copper}
    assert rows["F.Cu"].t_max_c == pytest.approx(42.29, abs=0.01)
    assert rows["B.Cu"].t_max_c == pytest.approx(41.77, abs=0.01)
    u1 = next(ld for ld in temp.loads if ld["name"] == "U1")
    assert u1["drop_ambient_v"] * 1e3 == pytest.approx(39.156, abs=1e-3)
    assert u1["drop_v"] * 1e3 == pytest.approx(41.354, abs=1e-3)


def test_the_board_outside_the_outline_is_nan():
    b = SyntheticBoard("l", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 2.0, 2.0, 1.5)
    b.square_pad("U1", "1", "VCC", 18.0, 18.0, 1.5)
    b.track("VCC", F, 0.5, (2.0, 2.0), (18.0, 2.0))
    b.track("VCC", F, 0.5, (18.0, 2.0), (18.0, 18.0))
    snap = b.snapshot(outline=box(0, 0, 20, 4).union(box(16, 0, 20, 20)))      # an L
    tree = solve_project_tree(Board(snap), Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)],
                                                                loads=[Load("U1", 1.0)])],
                                                   thermal=ThermalOptions(enabled=True, iterations=1)))
    m = tree.thermal.maps[0]
    assert np.isnan(m.at(4.0, 16.0)) and np.isfinite(m.at(18.0, 10.0))
    assert np.isnan(m.t_c).any() and np.isfinite(m.t_c).any()


def test_nearest_pad_is_given_for_every_row_and_the_peak(demo_tree):
    temp = demo_tree.thermal
    rows = {r.layer_name: r for r in temp.copper}
    assert (rows["F.Cu"].pad_ref, rows["F.Cu"].pad_number, rows["F.Cu"].pad_distance_mm) == ("J1", "1", 0.0)
    b = rows["B.Cu"]          # by the slot: the nearest pad is R1.2, 1 mm square at (36, 36)
    assert (b.pad_ref, b.pad_number) == ("R1", "2")
    assert b.pad_distance_mm == pytest.approx(math.hypot(35.5 - b.x_mm, 35.5 - b.y_mm), rel=1e-9)
    assert (temp.peak_pad_ref, temp.peak_pad_number, temp.peak_pad_distance_mm) == ("J1", "1", 0.0)


def test_thicker_copper_runs_cooler_and_says_so(demo, demo_tree):
    project = demo_thermal_project()
    project.settings.copper_thickness_mm = {"F.Cu": 0.07, "B.Cu": 0.07}
    tree = solve_project_tree(demo, project)
    assert tree.thermal.peak_c < demo_tree.thermal.peak_c
    assert any(a.startswith("What-if copper thickness") for a in tree.assumptions)


def test_assumptions(strip, demo_tree):
    thermal = demo_tree.thermal.assumptions
    for prefix in ("Board temperature: steady state", "Electro-thermal: 3 iterations", "Heat: ",
                   "Grid: 250 × 167 cells", "Laminate FR4", "Board shape: the board outline",
                   "Not modelled:", "Board temperature is judged only"):
        assert any(a.startswith(prefix) for a in thermal), prefix
    assert not any(a.startswith("Copper at") for a in demo_tree.assumptions)
    assert any(a.startswith("Electro-thermal: 3 iterations") for a in demo_tree.assumptions)
    assert not any("copper_temp_c" in a for a in demo_tree.assumptions)
    project = strip_project(iterations=1)
    project.settings.copper_temp_c = 40.0
    tree = solve_project_tree(strip, project)
    assert any(a.startswith("copper_temp_c (40 °C) is not used") for a in tree.assumptions)
    assert not any(a.startswith("Copper at") for a in tree.assumptions)
    assert not any(a.startswith("Regulator heat") for a in thermal)      # a plain source only
    # A fail stands on the last iteration's temperatures; no claim that they only rise (on the
    # power chain they fall: the LDO's loss drops as its input sags).
    a9 = next(a for a in thermal if a.startswith("Board temperature is judged only"))
    assert "the fail stands on the last iteration's temperatures" in a9 and "underestimat" not in a9


def test_cancel_raises_cancelled(strip):
    with pytest.raises(Cancelled):
        solve_project_tree(strip, strip_project(), cancelled=lambda: True)


def test_one_rail_is_not_heated_and_says_where_the_temperature_is(strip):
    project = strip_project()
    rail = project.rails[0]
    project.thermal.enabled = False
    off = solve_rail(strip, rail, project.settings, project=project)
    project.thermal.enabled = True
    on = solve_rail(strip, rail, project.settings, project=project)
    assert on.max_drop == off.max_drop and on.loads[0].v_min == off.loads[0].v_min
    note = "board temperature is solved with the power tree only (Ctrl+T / solve --tree)"
    assert [a for a in on.assumptions if a not in off.assumptions] == [
        next(a for a in on.assumptions if note in a)]


# ---------------------------------------------------------------------- verdict

def test_verdict_against_the_users_limits_only(demo_tree):
    tree = copy.deepcopy(demo_tree)
    temp = tree.thermal
    hottest = max(r.t_max_c for r in temp.copper)
    assert thermal_verdict(tree).verdict == "fail"                     # max_rise_c 15
    apply_thermal_limits(tree, ThermalOptions(max_temp_c=hottest + 1.0))
    v = thermal_verdict(tree)
    assert v.verdict == "pass" and not v.not_judged and all(r.passed for r in temp.copper)
    apply_thermal_limits(tree, ThermalOptions(max_rise_c=(hottest - temp.ambient_c) - 0.01))
    v = thermal_verdict(tree)
    assert v.verdict == "fail" and v.failed == 1 and [r.passed for r in temp.copper] == [False, True]
    assert "nearest pad J1.1" in v.note
    assert [p["severity"] for p in v.problems] == [FAIL, INFO]       # as every other problem
    apply_thermal_limits(tree, ThermalOptions())
    v = thermal_verdict(tree)
    assert v.verdict == "not_judged" and not v.not_judged and v.note.startswith("No limits set")
    assert all(r.passed is None for r in temp.copper) and tree_summary(tree)["verdict"] == "not_judged"


def test_an_unsettled_solve_fails_over_a_limit_and_is_not_judged_under_it(demo_tree):
    tree = copy.deepcopy(demo_tree)
    temp = tree.thermal
    temp.settled = False
    v = judge(temp, {"max_rise_c": 15.0}, tree.groups)
    assert v.verdict == "fail" and "not settled: last change" in v.note
    assert v.failed_rows[-1].startswith("not settled: last change")      # in the tree's note too
    assert "not settled" in v.problems[0]["title"]
    v = judge(temp, {"max_temp_c": 100.0}, tree.groups)
    assert v.verdict == "not_judged" and v.not_judged == [
        {"item": "board temperature", "net": "", "limit": "max_temp_c",
         "reason": v.not_judged[0]["reason"]}] and "run more iterations" in v.not_judged[0]["reason"]
    temp.settled = None
    assert "one iteration" in judge(temp, {"max_temp_c": 100.0}).not_judged[0]["reason"]
    temp.settled = True
    tree.groups["+3V3"].converged = False
    v = judge(temp, {"max_temp_c": 100.0, "max_rise_c": 70.0}, tree.groups)
    assert v.verdict == "not_judged" and [u["limit"] for u in v.not_judged] == ["max_temp_c", "max_rise_c"]
    assert "+3V3 did not converge" in v.not_judged[0]["reason"]


def test_the_grid_peak_over_a_limit_is_named_but_not_judged(demo_tree):
    tree = copy.deepcopy(demo_tree)
    temp = tree.thermal
    temp.peak_c = max(r.t_max_c for r in temp.copper) + 5.0          # as on an unsolved pad
    v = judge(temp, {"max_temp_c": temp.peak_c - 1.0})
    assert v.verdict == "pass" and "grid peak" in v.note and "not judged" in v.note


def test_limits_without_a_thermal_solve_are_not_judged(demo):
    project = demo_thermal_project(enabled=False)
    tree = solve_project_tree(demo, project)
    assert tree.thermal is None and tree.thermal_limits == {"max_rise_c": 15.0}
    doc = tree_summary(tree)
    assert "thermal" not in doc and doc["verdict"] == "not_judged"
    assert doc["not_judged"] == [{"item": "board temperature", "net": "", "limit": "max_rise_c",
                                  "reason": NOT_SOLVED}]
    assert f"board temperature (max_rise_c): {NOT_SOLVED}" in doc["verdict_note"]
    project.rails[0].limits.max_drop_pct = 0.25                     # a group fail still wins
    assert tree_summary(solve_project_tree(demo, project))["verdict"] == "fail"


def test_the_summary_has_units_and_no_nan(demo_tree):
    doc = json.loads(json.dumps(tree_summary(demo_tree), allow_nan=False))
    t = doc["thermal"]
    assert doc["verdict"] == "fail" and t["verdict"] == "fail" and doc["failed"] >= t["failed"] == 2
    assert "board temperature: max_rise_c = 15" in doc["verdict_note"]
    # The failing rows are named in the top-level note too, not only in the thermal block.
    named = ("Board temperature over a limit: F.Cu 42.29 °C (rise 17.29 °C, nearest pad J1.1) over "
             "max_rise_c 15; B.Cu 41.77 °C (rise 16.77 °C, nearest pad R1.2) over max_rise_c 15")
    assert named in doc["verdict_note"]
    for key in ("verdict", "verdict_note", "not_judged", "limits", "ambient_c", "airflow_m_s", "h_w_m2k",
                "grid_mm", "grid_cells", "iterations", "settled", "history", "heat_w", "heat_by_kind_w",
                "peak_c", "peak_rise_c", "peak_x_mm", "peak_y_mm", "peak_layer", "peak_nearest_pad",
                "layers", "loads", "problems", "assumptions", "warnings", "regulator_heat", "load_heat"):
        assert key in t, key
    assert set(t["history"][0]) == {"iteration", "heat_w", "t_max_c", "dt_max_c"}
    assert set(t["layers"][0]) == {"layer", "t_max_c", "rise_c", "x_mm", "y_mm", "group", "exceeds_limit",
                                   "nearest_pad"}
    assert t["layers"][0]["nearest_pad"] == {"ref": "J1", "pad": "1", "distance_mm": 0.0}
    assert set(t["loads"][0]) == {"name", "group", "drop_ambient_mv", "drop_mv", "change_pct"}
    assert list(doc).index("thermal") == list(doc).index("assumptions") - 1
    text = "\n".join(temperature_text(t, skip=doc["assumptions"]))
    for words in ("Board temperature", "F.Cu", "B.Cu", "J1.1", "FAIL", "39.156 → 41.354 mV"):
        assert words in text, words


# ---------------------------------------------------------------------- regulator heat

def demo_ldo_board():
    """The demo board with its supply J1 replaced by LDO U9: pad 2 (output) is J1's pad,
    stitched to the B.Cu plane; pad 1 (VIN) is fed from J2 by a 1 mm F.Cu track; pad 3 (GND)
    is copper on its own."""
    b = SyntheticBoard("demo_ldo", two_layer_stackup())
    b.zone("+3V3", B, box(2, 2, 58, 38).difference(box(28, 2, 31, 31)))
    b.square_pad("U9", "2", "+3V3", 6.0, 20.0, 4.0, value="LDO")
    for dx, dy in ((-1.2, -1.2), (1.2, -1.2), (-1.2, 1.2), (1.2, 1.2)):
        b.via("+3V3", 6.0 + dx, 20.0 + dy)
    b.pad("U9", "1", "VIN", box(2.9, 24.75, 4.1, 26.25), value="LDO")
    b.pad("U9", "3", "GND", box(7.9, 24.75, 9.1, 26.25), value="LDO")
    b.square_pad("J2", "1", "VIN", 3.5, 35.0, 2.0, value="SUPPLY")
    b.track("VIN", F, 1.0, (3.5, 25.5), (3.5, 35.0))
    for ref, (x, y) in (("U1", (50.0, 10.0)), ("U2", (50.0, 28.0))):
        b.square_pad(ref, "1", "+3V3", x, y, 2.5, value="LOAD")
        b.via("+3V3", x - 0.7, y)
        b.via("+3V3", x + 0.7, y)
    b.square_pad("U3", "1", "+3V3", 18.0, 33.0, 1.5, value="LOAD")
    b.via("+3V3", 18.0, 26.0)
    b.track("+3V3", F, 0.25, (18.0, 26.0), (18.0, 33.0))
    return b.snapshot(outline=box(0, 0, 60, 40))


def demo_ldo_project(**thermal):
    return Project(rails=[
        Rail("VIN", sources=[Source("J2", 3.6, ["1"])]),
        Rail("+3V3", sources=[Source("U9", 3.3, ["2"], kind="ldo", input_pins=["1"], input_net="VIN")],
             loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])],
        thermal=ThermalOptions(enabled=True, **thermal))


@pytest.fixture(scope="module")
def ldo():
    return Board(demo_ldo_board())


def test_regulator_heat_on_its_output_pads_or_all_its_pads(ldo):
    """KiPI puts an LDO's loss on its output pad; "part_pads" spreads it over the three pads
    of its part, so the grid peak moves to the GND pad (copper of no solved net)."""
    one = {mode: solve_project_tree(ldo, demo_ldo_project(iterations=1, regulator_heat=mode))
           for mode in ("output_pads", "part_pads")}
    a, b = one["output_pads"].thermal, one["part_pads"].thermal
    assert a.heat_w == pytest.approx(b.heat_w, rel=1e-12)
    u9 = next(v for v in one["output_pads"].vrms if v.name == "U9")
    assert a.heat_by_kind_w["VRM conversion"] == pytest.approx(u9.p_loss_w, rel=1e-12) and u9.p_loss_w > 1.0
    out = solve_project_tree(ldo, demo_ldo_project(regulator_heat="output_pads")).thermal
    part = solve_project_tree(ldo, demo_ldo_project(regulator_heat="part_pads")).thermal
    assert (out.peak_pad_ref, out.peak_pad_number, out.peak_pad_distance_mm) == ("U9", "2", 0.0)
    assert (part.peak_pad_ref, part.peak_pad_number, part.peak_pad_distance_mm) == ("U9", "3", 0.0)
    assert part.regulator_heat == "part_pads" and out.regulator_heat == "output_pads"
    line = {t.regulator_heat: next(a for a in t.assumptions if a.startswith("Regulator heat (U9 "))
            for t in (out, part)}
    assert "output pins" in line["output_pads"] and "overestimated" in line["output_pads"]
    assert "every copper pad" in line["part_pads"] and "over- or under-estimated" in line["part_pads"]
    assert "upper-bound" not in line["part_pads"]


# ---------------------------------------------------------------------- stackup and load heat

def _board_with(stackup) -> Board:
    b = SyntheticBoard("strip", stackup)
    b.square_pad("J1", "1", Strip.NET, Strip.X0, Strip.Y, Strip.PAD)
    b.square_pad("U1", "1", Strip.NET, Strip.X1, Strip.Y, Strip.PAD)
    b.track(Strip.NET, F, Strip.W, (Strip.X0, Strip.Y), (Strip.X1, Strip.Y))
    return Board(b.snapshot())


def test_an_unrecognized_laminate_is_named_and_kipis_thermal_tab_advice_is_not_shown():
    """KiPI's prepreg patterns need a word boundary (materials.py:47-48): a prefixed prepreg
    name falls back to FR4 core, and the assumptions say so."""
    stackup = two_layer_stackup()
    stackup.dielectrics[0].material = "PP2116"
    said = []
    tree = solve_project_tree(_board_with(stackup), strip_project(iterations=1), progress=said.append)
    line = "Laminate PP2116 (1.5 mm) not recognized: taken as FR4 core, 0.8 / 0.3 W/m·K."
    assert line in tree.thermal.assumptions
    assert any("not recognized" in m for m in said) and not any("Thermal tab" in m for m in said)


def test_a_solder_mask_is_named_and_holds_heat_in(strip):
    coated = solve_project_tree(_board_with(two_layer_stackup(mask_mm=0.025)), strip_project(iterations=1))
    bare = solve_project_tree(strip, strip_project(iterations=1))
    line = next(a for a in coated.thermal.assumptions if a.startswith("Coatings on the outer faces"))
    assert "top solder mask 25 µm (0.25 W/m·K)" in line and "bottom solder mask 25 µm" in line
    assert not any(a.startswith("Coatings") for a in bare.thermal.assumptions)
    assert coated.thermal.peak_c > bare.thermal.peak_c


def test_load_heat_puts_each_loads_power_at_its_pins(strip, ldo):
    tree = solve_project_tree(strip, strip_project(iterations=1, load_heat=True))
    load = tree.loads[0]
    assert tree.thermal.heat_by_kind_w["loads"] == pytest.approx(load.current_a * load.v_supply_v, rel=1e-9)
    assert tree.thermal.load_heat is True
    assert any("and each load's power at its pins" in a for a in tree.thermal.assumptions)
    off = solve_project_tree(strip, strip_project(iterations=1))
    assert "loads" not in off.thermal.heat_by_kind_w
    assert any("the loads' own power is not counted (load_heat: false)" in a for a in off.thermal.assumptions)
    loads = [solve_project_tree(ldo, demo_ldo_project(iterations=1, load_heat=True, regulator_heat=mode))
             .thermal.heat_by_kind_w["loads"] for mode in ("output_pads", "part_pads")]
    assert loads[0] == pytest.approx(loads[1], rel=1e-12) and loads[0] > 0


def test_an_unsettled_solve_warns_in_the_tree_too(demo):
    """Two iterations on demo-thermal change the copper by 0.92 °C in the last one, more than
    5 % of its 17 °C rise: the warning is the board temperature's and the tree's, as KiPI puts
    it in the tree's warnings."""
    tree = solve_project_tree(demo, demo_thermal_project(iterations=2))
    assert tree.thermal.settled is False
    warning = next(w for w in tree.thermal.warnings if w.startswith("Board temperature may not have settled"))
    assert warning in tree.warnings and warning in tree_summary(tree)["warnings"]


def test_apply_limits_rereads_the_board_temperature_limits_too(strip, demo_tree):
    """Every re-judge path (the app's included) goes through power_tree.apply_limits."""
    from coppernicus.power_tree import apply_limits
    project = strip_project()
    project.thermal.enabled = False
    tree = solve_project_tree(strip, project)
    project.thermal.max_temp_c = 50.0
    apply_limits(tree, project)
    assert tree.thermal_limits == {"max_temp_c": 50.0} and tree_summary(tree)["verdict"] == "not_judged"
    tree = copy.deepcopy(demo_tree)
    project = demo_thermal_project(max_rise_c=None, max_temp_c=100.0)
    apply_limits(tree, project)
    assert tree.thermal_limits == {"max_temp_c": 100.0} and all(r.passed for r in tree.thermal.copper)
    assert tree_summary(tree)["verdict"] == "pass"


def test_no_change_in_percent_of_a_drop_that_prints_as_zero():
    """The power chain's U3 is remote-sensed: its drop is ~1e-9 mV at the ambient and after,
    so a change in percent (+471.6 %) would be round-off. It is null; U4's is given."""
    project = power_chain_project()
    project.thermal = ThermalOptions(enabled=True)
    doc = tree_summary(solve_project_tree(Board(power_chain_board()), project))
    loads = {ld["name"]: ld for ld in doc["thermal"]["loads"]}
    assert abs(loads["U3"]["drop_mv"]) < 5e-4 and loads["U3"]["change_pct"] is None
    assert loads["U4"]["change_pct"] == pytest.approx(
        100 * (loads["U4"]["drop_mv"] - loads["U4"]["drop_ambient_mv"]) / loads["U4"]["drop_ambient_mv"])
    text = "\n".join(temperature_text(doc["thermal"]))
    assert "U3" in text and "471" not in text
