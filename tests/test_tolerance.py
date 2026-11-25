"""Tolerance stack-up per load (Task 11): the project's tolerances and entered parts, the
engine additions (worst pins, KiPI's transient estimate), the stack-up against KiPI's own
``stack()``, the parts from the tree, AC and noise results, verdicts and outputs. Field by
field: ``docs/engine-mapping.md`` (Task 11)."""

from pathlib import Path

import pytest
import yaml

from coppernicus.project import Load, Project, ProjectError, Source, ToleranceOptions

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*/*.coppernicus.yml"))
OLD = [p for p in EXAMPLES if p.parent.name != "tolerance"]


# ---------------------------------------------------------------------- project file

def _project(load=None, source=None, tolerance=None):
    d = {"board": "b.kipi.json.gz", "rails": [{
        "net": "VCC", "sources": [{"ref": "J1", "pins": ["1"], "voltage": 3.3, **(source or {})}],
        "loads": [{"ref": "U1", "pins": "all", "current": 1.0, **(load or {})}]}]}
    if tolerance is not None:
        d["tolerance"] = tolerance
    return Project.from_dict(d)


@pytest.mark.parametrize("path", OLD, ids=lambda p: p.name)
def test_files_before_t11_load_and_save_unchanged(path):
    p = Project.load(path)
    assert p.tolerance == ToleranceOptions()
    for r in p.rails:
        for ld in r.loads:
            assert (ld.tol_pct, ld.dc_tol_mv, ld.ripple_mv, ld.transient_mv) == (None,) * 4
        for s in r.sources:
            assert (s.accuracy_pct, s.accuracy_mv) == (None, None)
    assert p.to_dict() == yaml.safe_load(path.read_text(encoding="utf-8"))


def test_a_number_is_plus_minus_and_a_pair_is_signed_below_above():
    p = _project(load={"tol_pct": 5, "dc_tol_mv": [-30, 20], "ac_tol_pct": 0, "ripple_mv": 4,
                       "transient_mv": [-25, 0]},
                 source={"accuracy_pct": [-1.5, 1]})
    ld, s = p.rails[0].loads[0], p.rails[0].sources[0]
    assert ld.tol_pct == (-5.0, 5.0) and ld.dc_tol_mv == (-30.0, 20.0)
    assert ld.ac_tol_pct == (0.0, 0.0)                     # 0 is a value
    assert ld.ripple_mv == (-4.0, 4.0) and ld.transient_mv == (-25.0, 0.0)
    assert s.accuracy_pct == (-1.5, 1.0) and s.accuracy_mv is None


def test_tolerances_round_trip_numbers_when_symmetric_pairs_when_not(tmp_path):
    p = _project(load={"tol_mv": 50, "dc_tol_pct": [-3, 2], "ac_tol_mv": [-0.0, 0],
                       "ripple_mv": [-2, 3], "transient_mv": 30},
                 source={"accuracy_mv": [-10, 12]},
                 tolerance={"ripple_sum": "rss", "bw_limit_20mhz": True})
    path = p.save(tmp_path / "t.coppernicus.yml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    load, source = data["rails"][0]["loads"][0], data["rails"][0]["sources"][0]
    assert (load["tol_mv"], load["dc_tol_pct"], load["ac_tol_mv"]) == (50.0, [-3.0, 2.0], 0.0)
    assert (load["ripple_mv"], load["transient_mv"]) == ([-2.0, 3.0], 30.0)
    assert source["accuracy_mv"] == [-10.0, 12.0]
    assert data["tolerance"] == {"ripple_sum": "rss", "bw_limit_20mhz": True}
    q = Project.load(path)
    assert q.rails == p.rails and q.tolerance == p.tolerance
    assert q.to_dict() == p.to_dict()


def test_the_tolerance_block_is_omitted_at_its_defaults(tmp_path):
    p = _project(tolerance={"ripple_sum": "linear", "bw_limit_20mhz": False})
    assert p.tolerance == ToleranceOptions() and "tolerance" not in p.to_dict()
    assert ToleranceOptions().ripple_sum == "linear" and ToleranceOptions().bw_limit_20mhz is False
    only = _project(tolerance={"bw_limit_20mhz": True}).to_dict()["tolerance"]
    assert only == {"bw_limit_20mhz": True}


@pytest.mark.parametrize("load, source, tolerance, words", [
    ({"tol_pct": [2, 5]}, None, None, "below"),               # below must be ≤ 0
    ({"dc_tol_mv": [-5, -1]}, None, None, "above"),           # above must be ≥ 0
    ({"ripple_mv": [-1, 2, 3]}, None, None, "[below, above]"),
    ({"transient_mv": -3}, None, None, "[below, above]"),     # one number is ± that
    ({"ac_tol_pct": "5%"}, None, None, "ac_tol_pct"),
    ({"tol_pct": True}, None, None, "tol_pct"),
    ({"tol_pct": float("nan")}, None, None, "tol_pct"),
    ({"tol_pct": 5, "tol_mv": 100}, None, None, "tol_pct and tol_mv"),
    ({"ac_tol_pct": 5, "ac_tol_mv": 100}, None, None, "ac_tol_pct and ac_tol_mv"),
    (None, {"accuracy_pct": 1, "accuracy_mv": 10}, None, "accuracy_pct and accuracy_mv"),
    (None, {"accuracy_pct": [0.5, 1]}, None, "below"),
    (None, None, {"ripple_sum": "max"}, "ripple_sum"),
    (None, None, {"bw_limit_20mhz": "yes"}, "bw_limit_20mhz"),
    (None, None, {"bw_limit": True}, "unknown"),               # unknown keys still raise
    (None, None, ["linear"], "tolerance"),
])
def test_bad_tolerances_are_refused(load, source, tolerance, words):
    with pytest.raises(ProjectError) as e:
        _project(load=load, source=source, tolerance=tolerance)
    assert words in str(e.value)


def test_tolerances_are_checked_when_made_as_when_loaded():
    assert Load("U1", 1.0, tol_pct=5).tol_pct == (-5.0, 5.0)
    assert Load("U1", 1.0, ripple_mv=[-1, 2]).ripple_mv == (-1.0, 2.0)
    assert Source("J1", 3.3, accuracy_mv=(-10, 10)).accuracy_mv == (-10.0, 10.0)
    with pytest.raises(ProjectError, match="below"):
        Load("U1", 1.0, dc_tol_pct=(1, 2))
    with pytest.raises(ProjectError, match="tol_pct and tol_mv"):
        Load("U1", 1.0, tol_pct=5, tol_mv=10)
    with pytest.raises(ProjectError, match="ripple_sum"):
        ToleranceOptions(ripple_sum="RSS")


def test_an_editor_keeps_the_tolerances_it_does_not_show():
    from coppernicus.project import carry_over
    old = [Load("U1", 1.0, tol_pct=5, transient_mv=30)]
    new = carry_over(old, [Load("U1", 2.0)])
    assert new[0].tol_pct == (-5.0, 5.0) and new[0].transient_mv == (-30.0, 30.0)
    assert new[0].current == 2.0
    src = carry_over([Source("J1", 3.3, accuracy_pct=1)], [Source("J1", 5.0)])
    assert src[0].accuracy_pct == (-1.0, 1.0) and src[0].voltage == 5.0


def test_an_unknown_load_key_is_ignored_as_before():
    """Loads and sources never refused unknown keys (only the blocks do): a typo in a tolerance
    key leaves the load without that tolerance. Recorded, not changed (T11a)."""
    p = _project(load={"tol_pc": 5})
    assert p.rails[0].loads[0].tol_pct is None


# ---------------------------------------------------------------------- the tree: worst pins

def _pins_tree(sense=False, voltage=3.3, **load):
    """``pins_board``: U1's three pins at 10, 12 and 14 mm along a strip fed by J1 at 0 mm;
    with ``sense`` J1 holds its voltage at U1's middle pin (remote sense)."""
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import pins_board
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.project import Rail
    kw = dict(sense_ref="U1", sense_pins=["2"], sense_net="VCC") if sense else {}
    project = Project(rails=[Rail("VCC", sources=[Source("J1", voltage, ["1"], **kw)],
                                  loads=[Load("U1", 3.0, ["1", "2", "3"], **load)])])
    return project, solve_project_tree(Board(pins_board()), project)


@pytest.fixture(scope="module")
def pins_tree():
    return _pins_tree()


@pytest.fixture(scope="module")
def sensed_tree():
    return _pins_tree(sense=True)


def test_a_load_report_keeps_both_worst_pins(pins_tree):
    _, tree = pins_tree
    (u1,) = tree.loads
    # The far pin is the lowest (the worst, as before), the near pin the highest.
    assert u1.v_low_v == u1.v_worst_v
    assert u1.v_low_v < u1.v_across_v < u1.v_high_v < 3.3
    assert u1.drop_v == pytest.approx(3.3 - u1.v_low_v, abs=1e-15)


def test_a_remote_sensed_load_sits_above_and_below_its_nominal(sensed_tree):
    _, tree = sensed_tree
    (u1,) = tree.loads
    # J1 holds the middle pin at 3.3 V: the near pin is above it, the far pin below.
    assert u1.v_low_v < 3.3 < u1.v_high_v
    assert u1.v_worst_v == u1.v_low_v


def test_on_a_negative_rail_the_worst_pin_is_the_highest():
    _, tree = _pins_tree(voltage=-3.3)
    (u1,) = tree.loads
    assert u1.v_worst_v == u1.v_high_v and -3.3 < u1.v_low_v < u1.v_high_v
