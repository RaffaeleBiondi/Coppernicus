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


# ---------------------------------------------------------------------- composition (no solve)

def _comp_project(load=None, source=None, aggressors=(), probe=True, voltage=1.8, kind="smps", **opts):
    """VOUT at ``voltage`` from U1 (a switcher), load U2 with probe P1 on its pins; ``aggressors``
    (name, listing P1?) on U1."""
    from coppernicus.project import Aggressor, Probe, Rail
    src = Source("L1", voltage, ["2"], name="U1", kind=kind, efficiency=0.9 if kind == "smps" else None,
                 **(source or {}))
    p = Project(rails=[Rail("VOUT", sources=[src], loads=[Load("U2", 5.0, ["1"], **(load or {}))])],
                tolerance=ToleranceOptions(**opts))
    if probe:
        p.probes = [Probe("P1", "U2", "VOUT", ["1"])]
    p.aggressors = [Aggressor(a, vrm="U1", ripple_probes=["P1"] if listing else [])
                    for a, listing in aggressors]
    return p


def _comp_tree(nominal=1.8, low=None, high=None, mean=None, status="", converged=True):
    """A power tree holding U2 on VOUT, driven by U1: worst pins ``low`` and ``high`` (V)."""
    from coppernicus.results import LoadReport, TreeResult, VrmReport
    low = nominal - 0.010 if low is None else low
    high = nominal - 0.004 if high is None else high
    mean = (low + high) / 2 if mean is None else mean
    ld = LoadReport("U2", "VOUT", 5.0, nominal, mean, low, None, mean, low, nominal - low,
                    100 * (nominal - low) / (abs(nominal) or 1.0), status=status, net="VOUT", rail="VOUT",
                    v_low_v=low, v_high_v=high)
    vrm = VrmReport("U1", "smps", "VOUT", nominal, nominal, 5.0)
    return TreeResult(loads=[ld], vrms=[vrm], converged=converged)


def _noise_result(name, peaks=None, stand_ins=(), probe="P1"):
    """A NoiseResult of aggressor ``name`` showing ripple ``peaks`` (V: below, above, the same
    through 20 MHz) at ``probe`` (no probe ripple when None)."""
    import numpy as np

    from coppernicus.results import NoiseResult, RippleResult
    z = np.zeros(4)
    ripple = [] if peaks is None else [RippleResult(probe, "probe", "VOUT", "GND", "U2", ["1"], *peaks,
                                                    peaks[0] + peaks[1], peaks[2] + peaks[3], 0.0, z, z, z)]
    return NoiseResult(name, "U1", "VOUT", "VIN", "GND", "buck", 12.0, 1.8, 5.0, 0.8, 0.15, 0.9, 1, [],
                       1e6, 2e-9, 2e-9, 1e9, 1000, False, [0.5], [], z, [z], [z], [""], ripple=ripple,
                       stand_ins=list(stand_ins))


def _impedance(probe="P1", estimate=(0.020, 0.012, 0.015, 0.010)):
    """An ImpedanceResult whose probe carries KiPI's transient estimate (V)."""
    import numpy as np

    from coppernicus.results import ImpedanceResult, ProbeImpedance, TransientResult
    z = np.ones(3, dtype=complex)
    tr = None if estimate is None else TransientResult(1e5, 1.0, 1e6, 1e-6, *estimate, z.real, z.real,
                                                       z.real, z.real)
    return ImpedanceResult("VOUT", ["VOUT"], "GND", "U1", 1e5, np.array([1e3, 1e4, 1e5]), np.array([1e3]),
                           probes=[ProbeImpedance(probe, "U2", ["1"], "VOUT", "GND", z, transient=tr)])


def _compose(project, tree=None, impedance=(), noise=(), pairs=None, **kw):
    from coppernicus.tolerance import compose
    pairs = {"U2": "P1" if project.probes else None} if pairs is None else pairs
    return compose(project, tree or _comp_tree(), impedance, noise, pairs, **kw)


COMPLETE = dict(load={"tol_pct": 5, "transient_mv": 20, "ripple_mv": 5}, source={"accuracy_pct": 1})


def test_a_complete_stack_passes_and_names_where_each_part_came_from():
    r = _compose(_comp_project(**COMPLETE))
    (row,) = r.rows
    assert row.verdict == "pass" and r.verdict == "pass" and r.failed == r.not_judged == []
    assert row.nominal_v == 1.8 and row.source == "U1" and row.probe == "P1"
    assert row.parts["dc"].below_v == pytest.approx(0.010) and row.parts["dc"].above_v == 0.0
    assert row.parts["accuracy"].below_v == pytest.approx(0.018)
    assert (row.parts["ripple"].below_v, row.parts["transient"].above_v) == pytest.approx((0.005, 0.020))
    assert row.parts["transient"].source == "entered (transient_mv)"
    assert row.parts["accuracy"].source == "accuracy_pct of U1"
    total = row.checks["total"]
    assert (total.limit, total.complete, total.passed) == ("tol_pct", True, True)
    assert total.below_v == pytest.approx(0.010 + 0.018 + 0.005 + 0.020)
    assert total.allowed_below_v == pytest.approx(0.09)
    assert row.checks["dc"].limit == "" and row.checks["dc"].passed is None


def test_f1_an_unset_accuracy_is_missing_not_zero():
    """KiPI counts a VRM accuracy not set as 0 (accuracy_sides, model.py:171-177): its own
    stack passes; here the accuracy is missing and the check is not judged."""
    from coppernicus.engine.tolerance import kipi_reference_stack
    project = _comp_project(load=COMPLETE["load"])
    row = _compose(project).rows[0]
    kipi = kipi_reference_stack(1.8, project.rails[0].loads[0], project.rails[0].sources[0],
                                {k: (row.parts[k].below_v, row.parts[k].above_v)
                                 for k in ("dc", "ripple", "transient")})
    assert kipi["verdict"] is True                                     # before: a pass
    assert row.parts["accuracy"] is None and row.verdict == "not_judged"   # after
    assert row.hints["accuracy"] == "enter U1's DC accuracy (accuracy_pct)"


def test_f2_ripple_with_an_aggressor_not_solved_is_missing():
    """KiPI sets the ripple from the aggressors solved and only hints at the others
    (gather.py:206-209): complete. Here it is missing."""
    from coppernicus.engine.tolerance import kipi_reference_stack
    project = _comp_project(load={"tol_pct": 5, "transient_mv": 20}, source={"accuracy_pct": 1},
                            aggressors=[("A1", True), ("A2", True)])
    peaks = (0.004, 0.006, 0.003, 0.004)
    row = _compose(project, noise=[_noise_result("A1", peaks)],
                   noise_notes={"A2": "--skip-noise"}).rows[0]
    kipi = kipi_reference_stack(1.8, project.rails[0].loads[0], project.rails[0].sources[0],
                                {"dc": (0.010, 0.0), "ripple": peaks, "transient": (0.020, 0.020)})
    assert kipi["verdict"] is True
    assert row.parts["ripple"] is None and row.verdict == "not_judged"
    assert row.hints["ripple"] == "A2 not solved (--skip-noise)"


@pytest.mark.parametrize("kind, hint", [
    ("ldo", "enter ripple_mv for U2 (no switcher's ripple is solved here)"),
    ("smps", "add an aggressor for U1 with probe P1 in its ripple probes, or enter ripple_mv for U2")])
def test_f3_no_aggressor_on_the_rail_is_missing_not_zero(kind, hint):
    """KiPI counts the ripple of a rail with no aggressor as nothing (NO_SWITCHER,
    gather.py:147, :186): its stack passes. Here it is missing, whatever the source."""
    from coppernicus.engine.tolerance import kipi_reference_stack
    project = _comp_project(load={"tol_pct": 5, "transient_mv": 20}, source={"accuracy_pct": 1}, kind=kind)
    row = _compose(project).rows[0]
    kipi = kipi_reference_stack(1.8, project.rails[0].loads[0], project.rails[0].sources[0],
                                {"dc": (0.010, 0.0), "ripple": None, "transient": (0.020, 0.020)},
                                not_applicable=("ripple",))
    assert kipi["verdict"] is True
    assert row.parts["ripple"] is None and row.verdict == "not_judged" and row.hints["ripple"] == hint
    entered = _compose(_comp_project(load={"tol_pct": 5, "transient_mv": 20, "ripple_mv": [-3, 4]},
                                     source={"accuracy_pct": 1}, kind=kind)).rows[0]
    assert entered.parts["ripple"].source == "entered (ripple_mv)" and entered.verdict == "pass"
    assert (entered.parts["ripple"].below_v, entered.parts["ripple"].above_v) == pytest.approx((0.003, 0.004))


def test_a_listed_probe_without_its_ripple_hints_at_the_aggressor_and_the_probe():
    project = _comp_project(**COMPLETE, aggressors=[("A1", True)])
    row = _compose(project, noise=[_noise_result("A1", None)]).rows[0]
    assert row.parts["ripple"] is None
    assert row.hints["ripple"].startswith("A1 shows no ripple at P1")
    # ripple_mv entered beside a listed aggressor that gives nothing usable: still missing.
    assert "ripple_mv ±5 mV entered, not used" in row.hints["ripple"]
    no_probe = _compose(_comp_project(**COMPLETE, probe=False), pairs={"U2": None}).rows[0]
    assert no_probe.parts["ripple"].source == "entered (ripple_mv)"


def test_a_stand_in_ripple_over_is_not_judged_while_dc_and_accuracy_alone_over_fail():
    project = _comp_project(load={"tol_pct": 3, "transient_mv": 10}, source={"accuracy_pct": 1},
                            aggressors=[("A1", True)])
    big = (0.040, 0.040, 0.030, 0.030)
    stand_in = _noise_result("A1", big, stand_ins=["Coss not entered on phase 1: KiPI's 200 pF sets "
                                                   "the ringing"])
    row = _compose(project, noise=[stand_in]).rows[0]
    # 10 + 18 + 10 = 38 mV known below of 54 allowed; the 40 mV stand-in ripple would put it over.
    assert row.parts["ripple"] is None and row.checks["total"].passed is None
    assert row.verdict == "not_judged" and row.stand_ins
    assert "rests on KiPI's stand-ins (Coss not entered" in row.hints["ripple"]
    assert "40 / 40 mV" in row.hints["ripple"]
    over = _compose(project, tree=_comp_tree(low=1.8 - 0.040), noise=[stand_in]).rows[0]
    assert over.checks["total"].passed is False and over.verdict == "fail"     # 40 + 18 + 10 > 54
    r = _compose(project, tree=_comp_tree(low=1.8 - 0.040), noise=[stand_in])
    assert r.verdict == "fail" and r.failed[0]["limit"] == "tol_pct" and r.failed[0]["item"] == "U2"


def test_two_aggressors_add_linearly_by_default_and_as_kipis_rss_when_chosen():
    import types

    import numpy as np
    from kipi.tolerance.gather import Fresh, Gatherer
    a1, a2 = (0.003, 0.004, 0.002, 0.003), (0.004, 0.003, 0.0025, 0.002)
    noise = [_noise_result("A1", a1), _noise_result("A2", a2)]
    load = {"tol_pct": 10, "transient_mv": 10}
    linear = _compose(_comp_project(load=load, source={"accuracy_pct": 1},
                                    aggressors=[("A1", True), ("A2", True)]), noise=noise).rows[0]
    assert (linear.parts["ripple"].below_v, linear.parts["ripple"].above_v) == pytest.approx((0.007, 0.007))
    assert "sum of the peaks" in linear.parts["ripple"].detail
    rss = _compose(_comp_project(load=load, source={"accuracy_pct": 1}, ripple_sum="rss",
                                 aggressors=[("A1", True), ("A2", True)]), noise=noise).rows[0]
    # KiPI's own Gatherer._ripple on the same peaks (gather.py:178-210).
    g, out = Gatherer(), Fresh()
    results = {n: types.SimpleNamespace() for n in ("A1", "A2")}
    for n, pk in (("A1", a1), ("A2", a2)):
        g._cache[id(results[n])] = (results[n], {"P1": pk})
    setup = types.SimpleNamespace(aggressors=[types.SimpleNamespace(name=n, ripple_probes=["P1"], vrm="U1")
                                              for n in ("A1", "A2")])
    loaded = types.SimpleNamespace(noise={n: (results[n], "noise", "now") for n in ("A1", "A2")})
    g._ripple(out, setup, types.SimpleNamespace(name="P1"), types.SimpleNamespace(name="U1"), loaded)
    kipi = out.values["ripple"]
    assert (rss.parts["ripple"].below_v, rss.parts["ripple"].above_v) == pytest.approx(
        (kipi.below_v, kipi.above_v), rel=1e-6)                     # KiPI keeps 6 figures
    assert rss.parts["ripple"].below_v == pytest.approx(float(np.hypot(a1[0], a2[0])))
    assert "root sum of squares" in rss.parts["ripple"].detail
    # Through 20 MHz when chosen (both always shown).
    bw = _compose(_comp_project(load=load, source={"accuracy_pct": 1}, bw_limit_20mhz=True,
                                aggressors=[("A1", True), ("A2", True)]), noise=noise).rows[0]
    assert (bw.parts["ripple"].below_v, bw.parts["ripple"].above_v) == pytest.approx((0.0045, 0.005))


def test_a_computed_ripple_and_an_entered_one_take_the_larger_per_side():
    project = _comp_project(load={"tol_pct": 10, "transient_mv": 10, "ripple_mv": [-2, 9]},
                            source={"accuracy_pct": 1}, aggressors=[("A1", True)])
    row = _compose(project, noise=[_noise_result("A1", (0.004, 0.006, 0.003, 0.004))]).rows[0]
    assert (row.parts["ripple"].below_v, row.parts["ripple"].above_v) == pytest.approx((0.004, 0.009))
    assert "A1" in row.parts["ripple"].detail and "ripple_mv" in row.parts["ripple"].detail


def test_worst_pins_not_the_mean_and_both_sides_of_a_remote_sensed_load(pins_tree, sensed_tree):
    for (project, tree), both in ((pins_tree, False), (sensed_tree, True)):
        (rep,) = tree.loads
        row = _compose(project, tree=tree, pairs={"U1": None}).rows[0]
        dc = row.parts["dc"]
        assert dc.below_v == pytest.approx(3.3 - rep.v_low_v, abs=1e-15)
        assert dc.below_v > abs(3.3 - rep.v_across_v)                  # the mean drops less
        assert "mean" in dc.detail and dc.source.startswith("power tree")
        assert (dc.above_v > 0) is both
        if both:
            assert dc.above_v == pytest.approx(rep.v_high_v - 3.3, abs=1e-15)


@pytest.mark.parametrize("status, converged, words", [
    ("DISCONNECTED", True, "DISCONNECTED"), ("NOT CONVERGED", True, "NOT CONVERGED"),
    ("VRM OUT OF REGULATION", True, "VRM OUT OF REGULATION"), ("", False, "did not converge")])
def test_no_dc_part_from_a_load_the_tree_could_not_judge(status, converged, words):
    row = _compose(_comp_project(**COMPLETE), tree=_comp_tree(status=status, converged=converged)).rows[0]
    assert row.parts["dc"] is None and words in row.hints["dc"] and row.verdict == "not_judged"


def test_kipis_transient_estimate_is_shown_never_judged():
    project = _comp_project(load={"tol_pct": 5, "ripple_mv": 5}, source={"accuracy_pct": 1})
    project.probes[0].step_a, project.probes[0].slew_a_per_us = 1.0, 1.0
    project.ac.bandwidth_hz = {"U1": 1e5}
    row = _compose(project, impedance=[_impedance()]).rows[0]
    assert row.parts["transient"] is None and row.verdict == "not_judged"
    assert row.hints["transient"] == ("enter transient_mv for U2; KiPI's estimate with a typical loop "
                                      "at 100 kHz: 20 / 12 mV, 15 / 10 mV through 20 MHz (shown, never "
                                      "judged)")
    assert row.transient_estimate == pytest.approx((0.020, 0.012, 0.015, 0.010))
    entered = _compose(_comp_project(**COMPLETE), impedance=[_impedance()]).rows[0]
    assert entered.parts["transient"].above_v == pytest.approx(0.020)          # the entered one
    assert "KiPI's estimate with a typical loop" in entered.parts["transient"].detail


@pytest.mark.parametrize("change, why", [
    (dict(bandwidth=None), "no control loop bandwidth is given for U1 (ac.bandwidth_hz)"),
    (dict(step=None), "probe P1 has no load step (step_a, slew_a_per_us)"),
    (dict(probe=False), "no probe on U2's pins"),
    (dict(note="--skip-ac left it out"), "--skip-ac left it out")])
def test_without_kipis_estimate_the_hint_says_why(change, why):
    project = _comp_project(load={"tol_pct": 5, "ripple_mv": 5}, probe=change.get("probe", True))
    if project.probes and change.get("step", 1):
        project.probes[0].step_a, project.probes[0].slew_a_per_us = 1.0, 1.0
    if change.get("bandwidth", 1):
        project.ac.bandwidth_hz = {"U1": 1e5}
    notes = {"P1": change["note"]} if "note" in change else {}
    row = _compose(project, ac_notes=notes, pairs={"U2": "P1" if project.probes else None}).rows[0]
    assert row.hints["transient"] == f"enter transient_mv for U2; no KiPI estimate: {why}"
    assert row.transient_estimate is None


def test_a_negative_rail_swaps_kipis_estimate_and_judges_in_volts():
    project = _comp_project(load={"tol_pct": (-5, 3), "transient_mv": [-20, 10], "ripple_mv": 5},
                            source={"accuracy_pct": (-1, 2)}, voltage=-1.8)
    tree = _comp_tree(nominal=-1.8, low=-1.8, high=-1.8 + 0.010)      # drops toward 0 V: above
    row = _compose(project, tree=tree, impedance=[_impedance()]).rows[0]
    assert (row.parts["dc"].below_v, row.parts["dc"].above_v) == pytest.approx((0.0, 0.010))
    assert (row.parts["accuracy"].below_v, row.parts["accuracy"].above_v) == pytest.approx((0.036, 0.018))
    assert (row.parts["transient"].below_v, row.parts["transient"].above_v) == pytest.approx((0.020, 0.010))
    # A step pulls a negative rail up (gather.py:163): KiPI's under/overshoot swapped.
    assert row.transient_estimate == pytest.approx((0.012, 0.020, 0.010, 0.015))
    total = row.checks["total"]
    assert (total.allowed_below_v, total.allowed_above_v) == pytest.approx((0.054, 0.090))
    assert total.below_v == pytest.approx(0.0 + 0.036 + 0.005 + 0.020)


def test_a_percent_tolerance_on_a_0_v_rail_is_not_judged():
    project = _comp_project(load={"tol_pct": 5, "transient_mv": 2, "ripple_mv": 1},
                            source={"accuracy_mv": 1}, voltage=0.0)
    row = _compose(project, tree=_comp_tree(nominal=0.0, low=-0.001, high=0.0)).rows[0]
    assert row.checks["total"].passed is None and row.verdict == "not_judged"
    assert "tol_pct is a % of 0 V" in row.checks["total"].reason


def test_no_tolerance_anywhere_is_not_judged_without_an_entry():
    r = _compose(_comp_project(source={"accuracy_pct": 1}))
    assert r.rows[0].verdict == "" and r.verdict == "not_judged" and r.not_judged == r.failed == []
    assert r.verdict_note.startswith("No tolerance set")


def test_not_judged_entries_name_the_key_set_and_every_missing_part():
    project = _comp_project(load={"dc_tol_mv": 40, "ac_tol_pct": 2})
    r = _compose(project)
    by_limit = {e["limit"]: e for e in r.not_judged}
    assert set(by_limit) == {"dc_tol_mv", "ac_tol_pct"}
    assert by_limit["dc_tol_mv"]["reason"] == "accuracy: enter U1's DC accuracy (accuracy_pct)"
    assert "ripple: " in by_limit["ac_tol_pct"]["reason"] and "transient: enter transient_mv" in \
        by_limit["ac_tol_pct"]["reason"]
    assert {e["item"] for e in r.not_judged} == {"U2"} and r.verdict == "not_judged"


def test_q9_mismatch_warns_in_the_tolerance_result():
    project = _comp_project(load={"ac_tol_mv": 30, **COMPLETE["load"]}, source={"accuracy_pct": 1})
    project.probes[0].ac_tolerance_v = 0.05
    r = _compose(project)
    assert any("disagree" in w and "ac_tol_mv ±30 mV" in w for w in r.warnings)


# ---------------------------------------------------------------------- summary, text and CSV

def test_summary_text_and_csv(tmp_path):
    import csv
    import json

    from coppernicus.tolerance import tolerance_summary, tolerance_text, write_csv
    project = _comp_project(load={"tol_pct": 5, "dc_tol_mv": [-15, 10], "transient_mv": 20},
                            source={"accuracy_pct": 1}, aggressors=[("A1", True)])
    r = _compose(project, impedance=[_impedance()], noise=[_noise_result("A1", (0.004, 0.006, 0.003, 0.004))])
    path = write_csv(r, tmp_path / "t.csv")
    doc = tolerance_summary(r, path.as_posix())
    text = json.dumps(doc, allow_nan=False)                      # plain JSON, no NaN
    assert "nan" not in text.lower()
    assert set(doc) == {"kind", "verdict", "verdict_note", "failed", "not_judged", "options", "warnings",
                        "assumptions", "estimates", "stats", "csv_file", "loads"}
    assert doc["kind"] == "tolerance" and doc["options"] == {"ripple_sum": "linear", "bw_limit_20mhz": False}
    (ld,) = doc["loads"]
    assert set(ld) == {"load", "name", "net", "rail", "nominal_v", "source", "probe", "verdict", "parts",
                       "hints", "transient_estimate", "checks", "stand_ins"}
    assert ld["parts"]["accuracy"] == {"below_mv": pytest.approx(18.0), "above_mv": pytest.approx(18.0),
                                       "source": "accuracy_pct of U1", "detail": "accuracy_pct ±1 % of 1.8 V"}
    dc = ld["checks"]["dc"]
    assert dc["limit"] == "dc_tol_mv" and dc["passed"] is False            # 10 + 18 > 15 below
    assert dc["allowed_below_mv"] == pytest.approx(15.0) and dc["margin_below_mv"] == pytest.approx(-13.0)
    assert ld["checks"]["ac"]["limit"] == "" and ld["transient_estimate"]["above_mv"] == pytest.approx(12.0)
    assert doc["verdict"] == "fail" and doc["failed"][0]["reason"].startswith(
        "DC 28 mV below nominal, over the 15 mV allowed")
    lines = "\n".join(tolerance_text(doc))
    for words in ("Load U2 on VOUT (nominal 1.8 V from U1, probe P1): FAIL", "DC accuracy  18 / 18 mV",
                  "check DC    28 / 18 mV of 15 / 10 allowed (dc_tol_mv), margin -13 / -8 mV  FAIL",
                  "check total", "FAIL U2 (dc_tol_mv)", "assumption: Tolerance stack-up",
                  "Verdict: fail."):
        assert words in lines, words
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["load"] == "U2" and rows[0]["verdict"] == "fail"
    assert float(rows[0]["accuracy_below_mv"]) == pytest.approx(18.0) and rows[0]["ac_verdict"] == ""
    assert rows[0]["dc_verdict"] == "fail" and rows[0]["total_limit"] == "tol_pct"
