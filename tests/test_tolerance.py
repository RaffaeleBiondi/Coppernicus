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


# ---------------------------------------------------------------------- AC: KiPI's transient

QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4,
             max_edge_mm=2.0)


def _decap_solve(bandwidth=1e5, step_a=1.0, slew_a_per_us=1.0):
    """The ac_decap board with a quick sweep, its probe with a load step."""
    from coppernicus.engine import Board
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project
    from coppernicus.project import AcOptions
    project = ac_decap_project()
    project.ac = AcOptions(**QUICK, bandwidth_hz={"U1": bandwidth} if bandwidth else {})
    project.probes[0].step_a, project.probes[0].slew_a_per_us = step_a, slew_a_per_us
    return project, solve_ac_rail(Board(ac_decap_board()), project, "+3V3")


@pytest.fixture(scope="module")
def stepped():
    return _decap_solve()


def test_a_given_bandwidth_and_a_load_step_give_kipis_transient_estimate(stepped):
    import numpy as np
    from kipi.ac.transient import bandwidth_limited, transient
    _, r = stepped
    (p,) = r.probes
    tr = p.transient
    assert tr is not None and tr.bandwidth_hz == 1e5
    kipi = transient(r.freq_hz, p.z_ohm, 1e5, 1.0, 1e6, 0.0)
    bw = bandwidth_limited(kipi, 20e6)
    assert (tr.undershoot_v, tr.overshoot_v) == (kipi.undershoot_v, kipi.overshoot_v)
    assert (tr.undershoot_bw_v, tr.overshoot_bw_v) == (bw.undershoot_v, bw.overshoot_v)
    assert np.array_equal(tr.t_s, kipi.t) and np.array_equal(tr.v, kipi.v)
    assert np.array_equal(tr.i_a, kipi.i) and np.array_equal(tr.v_bw, bw.v)
    assert tr.notes == kipi.notes and tr.t_rise_s == pytest.approx(1e-6)
    assert tr.step_a == 1.0 and tr.slew_a_per_s == 1e6 and tr.undershoot_v > 0


@pytest.mark.parametrize("change", [dict(bandwidth=None), dict(step_a=None), dict(slew_a_per_us=None)],
                         ids=["no bandwidth given", "no step", "no slew"])
def test_no_estimate_without_a_given_bandwidth_a_step_and_a_slew(change, stepped):
    import numpy as np
    _, r = _decap_solve(**change)
    assert r.probes[0].transient is None
    # |Z| does not depend on the bandwidth, the step or the slew: bit-identical.
    assert np.array_equal(r.probes[0].z_ohm, stepped[1].probes[0].z_ohm)


# ---------------------------------------------------------------------- engine: limits and stack

def test_limits_in_percent_and_mv_signed_and_on_a_negative_rail():
    from coppernicus.engine.tolerance import accuracy_sides, allowed
    assert allowed(3.3, (-5.0, 3.0), True) == pytest.approx((0.165, 0.099))
    assert allowed(3.3, (-50.0, 30.0), False) == pytest.approx((0.050, 0.030))
    # A % of a negative rail is of its magnitude (KiPI's pct_to_volts swaps the ends): −5 %
    # takes −3.3 V toward 0 V, which is above it. mV from nominal add as they are.
    assert allowed(-3.3, (-5.0, 3.0), True) == pytest.approx((0.099, 0.165))
    assert allowed(-3.3, (-50.0, 30.0), False) == pytest.approx((0.050, 0.030))
    assert allowed(3.3, None, True) == (None, None) and allowed(None, (-5.0, 5.0), True) == (None, None)
    # A % of 0 V places nothing (KiPI would allow 0 V either side, a spurious fail).
    assert allowed(0.0, (-5.0, 5.0), True) == (None, None)
    assert allowed(0.0, (-50.0, 50.0), False) == pytest.approx((0.05, 0.05))
    assert accuracy_sides(1.8, Source("U1", 1.8, accuracy_pct=(-2, 1))) == pytest.approx((0.036, 0.018))
    assert accuracy_sides(-1.8, Source("U1", -1.8, accuracy_pct=(-2, 1))) == pytest.approx((0.018, 0.036))
    assert accuracy_sides(1.8, Source("U1", 1.8, accuracy_mv=(-10, 0))) == pytest.approx((0.010, 0.0))
    assert accuracy_sides(1.8, Source("U1", 1.8)) is None
    assert accuracy_sides(0.0, Source("U1", 0.0, accuracy_pct=1)) is None


DRIFT = [
    # nominal, load tolerances, source accuracy, dc, ripple, transient (V, below / above)
    (1.8, dict(tol_pct=5, dc_tol_pct=3, ac_tol_mv=40), dict(accuracy_pct=1),
     (0.012, 0.0), (0.004, 0.006), (0.020, 0.015)),
    (1.8, dict(tol_pct=(-3, 2)), dict(accuracy_mv=(-10, 12)),
     (0.030, 0.0), (0.004, 0.006), (0.020, 0.015)),                       # fails below
    (3.3, dict(tol_mv=(-120, 80), ac_tol_pct=(-1, 0.5)), dict(accuracy_pct=(-1.5, 1)),
     (0.002, 0.001), (0.010, 0.012), (0.009, 0.004)),                     # ac fails above
    (-3.3, dict(tol_pct=(-5, 3), dc_tol_mv=(-30, 60)), dict(accuracy_pct=(-1, 2)),
     (0.0, 0.025), (0.005, 0.004), (0.030, 0.010)),                       # a negative rail
    (5.0, dict(dc_tol_pct=0), dict(accuracy_pct=0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)),
]


@pytest.mark.parametrize("nominal, tols, acc, dc, ripple, transient", DRIFT)
def test_our_stack_equals_kipis_where_both_rules_agree(nominal, tols, acc, dc, ripple, transient):
    """Accuracy set, every part present (one aggressor, the mean = the worst pin): our copy of
    KiPI's stack() gives KiPI's numbers and verdicts (KiPI rounds a part to 6 figures)."""
    from coppernicus.engine.tolerance import accuracy_sides, kipi_reference_stack, load_limits, stack
    load, source = Load("U1", 1.0, **tols), Source("J1", nominal, **acc)
    parts = {"dc": dc, "ripple": ripple, "transient": transient,
             "accuracy": accuracy_sides(nominal, source)}
    ours = stack(parts, load_limits(load, nominal))
    kipi = kipi_reference_stack(nominal, load, source, {k: parts[k] for k in ("dc", "ripple", "transient")})
    assert kipi["parts"]["accuracy"] == pytest.approx(parts["accuracy"], abs=1e-15)
    for kind, (below, above, allowed_, complete, passed) in kipi["checks"].items():
        c = ours[kind]
        assert (c.below_v, c.above_v) == pytest.approx((below, above), rel=1e-6, abs=1e-12)
        assert (c.allowed_below_v, c.allowed_above_v) == pytest.approx(allowed_, abs=1e-15)
        assert (c.complete, c.passed) == (complete, passed)


def test_a_check_can_fail_while_incomplete_but_never_pass():
    from coppernicus.engine.tolerance import stack
    limits = {"dc": (0.020, 0.020), "ac": (None, None), "total": (None, None)}
    over = stack({"dc": None, "accuracy": (0.025, 0.0), "ripple": None, "transient": None}, limits)
    assert (over["dc"].complete, over["dc"].passed, over["dc"].missing) == (False, False, ["dc"])
    under = stack({"dc": None, "accuracy": (0.010, 0.0), "ripple": None, "transient": None}, limits)
    assert (under["dc"].complete, under["dc"].passed) == (False, None)
    full = stack({"dc": (0.005, 0.0), "accuracy": (0.010, 0.0), "ripple": None, "transient": None}, limits)
    assert (full["dc"].complete, full["dc"].passed, full["dc"].missing) == (True, True, [])
    assert full["ac"].passed is None and full["ac"].missing == ["ripple", "transient"]
    assert full["dc"].margin_below_v == pytest.approx(0.005) and full["ac"].margin_below_v is None


# ---------------------------------------------------------------------- loads, probes and Q9

def _noise_board():
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import coupled_noise_board
    return Board(coupled_noise_board())


def test_each_load_gets_its_probe_by_kipis_rule():
    from coppernicus.engine.synthetic import coupled_noise_project
    from coppernicus.engine.tolerance import load_probes
    from coppernicus.project import Probe
    board, project = _noise_board(), coupled_noise_project()
    assert load_probes(board, project) == {"U2": "LOAD"}
    # Same part on the same supply net, other pins and no ground: still U2's (KiPI's fallback),
    # after an exact one; a probe whose pins are not on the board is left out, not an error.
    project.probes = [Probe("NOPE", "U2", "VOUT", ["9"]), Probe("ANY", "U2", "VOUT")] + project.probes
    assert load_probes(board, project) == {"U2": "LOAD"}
    project.probes = project.probes[:2]
    assert load_probes(board, project) == {"U2": "ANY"}
    project.probes = []
    assert load_probes(board, project) == {"U2": None}


def test_a_load_ac_tolerance_and_its_probe_ac_tolerance_v_must_agree():
    from coppernicus.engine.tolerance import ac_tolerance_mismatch
    from coppernicus.project import Probe
    probe = Probe("P1", "U1", "VCC", ac_tolerance_v=0.05)
    assert ac_tolerance_mismatch(Load("U1", 1.0, ac_tol_mv=50), probe, 3.3) == ""
    # The tighter side, in volts: 1 % of 3.3 V is 33 mV.
    assert ac_tolerance_mismatch(Load("U1", 1.0, ac_tol_pct=(-2, 1)), Probe(
        "P1", "U1", "VCC", ac_tolerance_v=0.033), 3.3) == ""
    text = ac_tolerance_mismatch(Load("U1", 1.0, ac_tol_mv=(-40, 60)), probe, 3.3)
    assert "ac_tol_mv -40 / +60 mV: 40 mV on its tighter side" in text and "ac_tolerance_v (50 mV" in text
    assert ac_tolerance_mismatch(Load("U1", 1.0), probe, 3.3) == ""
    assert ac_tolerance_mismatch(Load("U1", 1.0, ac_tol_mv=40), Probe("P1", "U1", "VCC"), 3.3) == ""
    assert ac_tolerance_mismatch(Load("U1", 1.0, ac_tol_mv=50 * (1 + 1e-12)), probe, 3.3) == ""


def test_the_ac_result_warns_when_a_load_and_its_probe_disagree():
    from coppernicus.engine import Board
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project
    from coppernicus.project import AcOptions
    project = ac_decap_project()
    project.ac = AcOptions(**QUICK, bandwidth_hz={"U1": 1e5})
    project.probes[0].ac_tolerance_v = 0.05
    project.rails[0].loads = [Load("U1", 0.1, ["1"], ground_pins=["2"], ground_net="GND",
                                   ac_tol_mv=30)]
    r = solve_ac_rail(Board(ac_decap_board()), project, "+3V3")
    (w,) = [w for w in r.warnings if "disagree" in w]
    assert "Load U1's AC tolerance (ac_tol_mv ±30 mV" in w and "probe U1's ac_tolerance_v (50 mV" in w
    project.rails[0].loads[0].ac_tol_mv = (-50.0, 50.0)
    assert not [w for w in solve_ac_rail(Board(ac_decap_board()), project, "+3V3").warnings
                if "disagree" in w]
