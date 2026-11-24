"""The power-tree solve (engine/tree.py) on the synthetic power chain, against the hand
calculation of KiPI's power-chain accuracy test (tests/accuracy/test_dc_accuracy.py)."""

import math

import pytest

from coppernicus.engine import Board, EngineError, solve_rail
from coppernicus.engine.synthetic import OZ1, PowerChain, power_chain_board, power_chain_project
from coppernicus.engine.tree import solve_project_tree
from coppernicus.problems import FAIL, find_tree_problems
from coppernicus.project import Limits, Load, Source

RHO = 1.724e-5      # Ω·mm, annealed copper at 20 °C (the engine's constant)
g = PowerChain


def _r(gap):
    return RHO * gap / (g.W * OZ1)


def _gap(pins, a, b):
    """Copper between the 1 mm pads ``a`` and ``b``, pad edge to pad edge."""
    return abs(pins[b] - pins[a]) - 1.0


def _hand():
    """The whole chain by hand: currents first (downstream to upstream), then voltages."""
    out = {"U2.i_in": g.I_U3 + g.IQ_LDO}
    i_out1 = g.I_U4 + out["U2.i_in"]
    r_vin = _r(_gap(g.VIN, "J1.1", "U1.1"))
    r_gnd = _r(_gap(g.GND, "J1.2", "U1.3"))
    p = g.V_SMPS * i_out1 / g.ETA
    a = g.V_SRC - (r_vin + r_gnd) * g.IQ_SMPS
    v_across = (a + math.sqrt(a * a - 4 * (r_vin + r_gnd) * p)) / 2
    out["U1.i_out"] = i_out1
    out["U1.i_in"] = p / v_across + g.IQ_SMPS
    out["U1.gnd"] = out["U1.i_in"] * r_gnd
    out["U1.v_in"] = v_across + out["U1.gnd"]
    out["U1.v_out"] = g.V_SMPS + out["U1.gnd"]
    out["U4.v"] = out["U1.v_out"] - i_out1 * _r(_gap(g.V3V3, "U1.2", "U4.1"))
    out["U2.v_in"] = out["U4.v"] - out["U2.i_in"] * (
        _r(_gap(g.V3V3, "U4.1", "FB1.1")) + g.R_FB + _r(_gap(g.V3V3_F, "FB1.2", "U2.1")))
    out["U4.gnd"] = out["U1.gnd"] + (g.I_U3 + g.IQ_LDO + g.I_U4) * _r(_gap(g.GND, "U1.3", "U4.2"))
    out["U2.gnd"] = out["U4.gnd"] + (g.I_U3 + g.IQ_LDO) * _r(_gap(g.GND, "U4.2", "U2.3"))
    out["U3.gnd"] = out["U2.gnd"] + g.I_U3 * _r(_gap(g.GND, "U2.3", "U3.2"))
    out["U3.v"] = g.V_LDO + out["U3.gnd"]
    out["U2.v_out"] = out["U3.v"] + g.I_U3 * _r(_gap(g.V1V8, "U2.2", "U3.1"))
    return out


@pytest.fixture(scope="module")
def board():
    return Board(power_chain_board())


@pytest.fixture(scope="module")
def tree(board):
    return solve_project_tree(board, power_chain_project())


def _vrm(tree, name):
    return next(v for v in tree.vrms if v.name == name)


def _load(tree, name):
    return next(ld for ld in tree.loads if ld.name == name)


def test_ldo_input_is_output_plus_quiescent(tree):
    assert _vrm(tree, "U2").i_in_a == pytest.approx(g.I_U3 + g.IQ_LDO, rel=1e-9)


def test_switcher_input_from_power_and_efficiency(tree):
    hand = _hand()
    u1 = _vrm(tree, "U1")
    assert u1.i_out_a == pytest.approx(hand["U1.i_out"], rel=1e-9)
    assert u1.i_in_a == pytest.approx(hand["U1.i_in"], rel=1e-6)
    assert _vrm(tree, "J1").i_out_a == pytest.approx(hand["U1.i_in"], rel=1e-6)
    assert u1.p_loss_w == pytest.approx(u1.p_in_w - u1.p_out_w)


def test_ground_strip_drop_is_r_times_i(tree):
    hand, tol = _hand(), dict(abs=2e-5)
    assert _vrm(tree, "U1").v_ground_v == pytest.approx(hand["U1.gnd"], **tol)
    assert _load(tree, "U4").v_ground_v == pytest.approx(hand["U4.gnd"], **tol)
    assert _load(tree, "U3").v_ground_v == pytest.approx(hand["U3.gnd"], **tol)
    assert _vrm(tree, "U1").v_in_v == pytest.approx(hand["U1.v_in"], **tol)
    assert _vrm(tree, "U2").v_in_v == pytest.approx(hand["U2.v_in"], **tol)
    assert _load(tree, "U4").v_supply_v == pytest.approx(hand["U4.v"], **tol)


def test_every_load_sees_supply_minus_ground(tree):
    for ld in tree.loads:
        assert ld.v_across_v == pytest.approx(ld.v_supply_v - ld.v_ground_v, abs=1e-12)
    # The LDO holds U3's supply 1.8 V above U3's ground (remote and ground sense).
    assert _load(tree, "U3").v_across_v == pytest.approx(g.V_LDO, abs=1e-8)


def test_groups_rails_and_ground(tree):
    assert set(tree.groups) == {"VIN", "+3V3", "+1V8", "GND"}
    assert tree.groups["+3V3"].nets[0] == "+3V3" and "+3V3_F" in tree.groups["+3V3"].nets
    assert {k: r.kind for k, r in tree.groups.items()} == {
        "VIN": "rail", "+3V3": "rail", "+1V8": "rail", "GND": "ground"}
    gnd = tree.groups["GND"]
    assert gnd.v_ref == 0.0
    assert gnd.max_drop == pytest.approx(_hand()["U3.gnd"], abs=2e-5)    # largest rise
    assert tree.assumptions and any("switcher" in a for a in tree.assumptions)
    assert {v.group for v in tree.vrms} == {"VIN", "+3V3", "+1V8"}
    assert {ld.group for ld in tree.loads} == {"+3V3", "+1V8"}


def test_verdicts_only_against_user_limits(board):
    project = power_chain_project()
    tree = solve_project_tree(board, project)
    assert all(ld.passed is None for ld in tree.loads)
    project.rail("+1V8").limits = Limits(max_drop_pct=0.01)
    u3 = _load(solve_project_tree(board, project), "U3")
    # U3 is regulated at its own pins: no drop at all, so even a tight limit passes.
    assert u3.drop_pct == pytest.approx(0.0, abs=1e-6) and u3.passed is True
    project.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    u4 = _load(solve_project_tree(board, project), "U4")
    assert u4.drop_pct > 0.01 and u4.passed is False
    assert u4.drop_v == pytest.approx(u4.nominal_v - u4.v_worst_v)


def test_tree_problems_rank_loads_by_across_drop_and_grounds_by_rise(board):
    project = power_chain_project()
    project.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    tree = solve_project_tree(board, project)
    [u4] = [p for p in find_tree_problems(tree, "+3V3", project.rail("+3V3").limits) if p.kind == "load"]
    assert u4.severity == FAIL and u4.data["drop_pct"] == pytest.approx(_load(tree, "U4").drop_pct)
    ground = find_tree_problems(tree, "GND", Limits(max_drop_pct=1.0))
    pins = [p for p in ground if p.kind == "return"]
    assert pins and pins[0].data["rise_mv"] == pytest.approx(_hand()["U3.gnd"] * 1e3, abs=0.02)
    assert all(p.severity != FAIL for p in pins)          # no drop limit on a ground


def test_a_load_without_ground_pins_is_refused_when_grounds_are_solved(board):
    project = power_chain_project()
    u4 = project.rail("+3V3").loads[0]
    u4.ground_net, u4.ground_pins = "", None
    with pytest.raises(EngineError, match="U4.*ground"):
        solve_project_tree(board, project)


def test_a_regulator_without_ground_pins_is_refused_when_grounds_are_solved(board):
    project = power_chain_project()
    u2 = project.rail("+1V8").sources[0]
    u2.ground_net, u2.ground_pins = "", None
    u2.ground_sense_net = ""
    with pytest.raises(EngineError, match="U2.*ground"):
        solve_project_tree(board, project)


def test_a_vrm_fed_by_its_own_output_is_refused(board):
    project = power_chain_project()
    u1 = project.rail("+3V3").sources[0]
    u1.input_net, u1.input_pins = "+3V3", ["2"]
    with pytest.raises(EngineError, match="U1"):
        solve_project_tree(board, project)


# ------------------------------------------------- per-rail solve with estimated draws

def test_per_rail_input_rail_draws_the_estimated_regulator_input(board):
    project = power_chain_project()
    res = solve_rail(board, project.rail("VIN"), project.settings, project=project)
    [est] = [t for t in res.loads if t.name == "U1 (input, estimated)"]
    i_out1 = g.I_U4 + g.I_U3 + g.IQ_LDO
    assert est.current == pytest.approx(g.V_SMPS * i_out1 / (g.ETA * g.V_SRC) + g.IQ_SMPS, rel=1e-12)
    [note] = [a for a in res.assumptions if "estimated at nominal voltages" in a]
    assert "U1" in note and "0.9" in note and "power-tree solve computes it exactly" in note


def test_per_rail_link_draws_what_its_far_side_draws(board):
    project = power_chain_project()
    res = solve_rail(board, project.rail("+3V3"), project.settings, project=project)
    [est] = [t for t in res.loads if t.name == "FB1 (link, estimated)"]
    assert est.current == pytest.approx(g.I_U3 + g.IQ_LDO, rel=1e-12)
    without = solve_rail(board, project.rail("+3V3"), project.settings)
    assert [t.name for t in without.loads] == ["U4"]            # unchanged without project=


def test_per_rail_estimate_refuses_a_stand_in_load_and_a_switcher_without_efficiency(board):
    project = power_chain_project()
    project.rail("VIN").loads.append(Load("U1", 0.8, ["1"], name="Buck input"))
    with pytest.raises(EngineError, match="Buck input.*U1.*remove the load or the VRM input"):
        solve_rail(board, project.rail("VIN"), project.settings, project=project)
    project = power_chain_project()
    project.rail("+3V3").sources[0].efficiency = None
    with pytest.raises(EngineError, match="U1.*efficiency"):
        solve_rail(board, project.rail("VIN"), project.settings, project=project)


def test_per_rail_estimate_is_recursive_through_ldo(board):
    """VIN feeds U1, whose output feeds U2 through FB1: U1's I_out counts U2's input."""
    project = power_chain_project()
    project.rail("+3V3").sources[0] = Source("U1", g.V_SMPS, ["2"], kind="ldo", quiescent_a=0.001,
                                             input_pins=["1"], input_net="VIN",
                                             ground_pins=["3"], ground_net="GND")
    res = solve_rail(board, project.rail("VIN"), project.settings, project=project)
    [est] = [t for t in res.loads if t.name == "U1 (input, estimated)"]
    assert est.current == pytest.approx(g.I_U4 + g.I_U3 + g.IQ_LDO + 0.001, rel=1e-12)


def test_tree_summary_is_plain_json_with_units_and_verdicts(board):
    import json

    from coppernicus.power_tree import tree_summary
    project = power_chain_project()
    doc = json.loads(json.dumps(tree_summary(solve_project_tree(board, project)), allow_nan=False))
    assert doc["verdict"] == "not_judged" and "No limits" in doc["verdict_note"]
    assert {v["name"] for v in doc["vrms"]} == {"J1", "U1", "U2"}
    u1 = next(v for v in doc["vrms"] if v["name"] == "U1")
    assert u1["i_in_a"] == pytest.approx(_hand()["U1.i_in"], rel=1e-6) and u1["kind"] == "smps"
    u3 = next(ld for ld in doc["loads"] if ld["name"] == "U3")
    assert u3["v_across_v"] == pytest.approx(g.V_LDO, abs=1e-8) and u3["exceeds_drop_limit"] is None
    gnd = next(gr for gr in doc["groups"] if gr["group"] == "GND")
    assert gnd["kind"] == "ground" and "max_rise_mv" in gnd and gnd["max_drop_pct"] is None
    assert [k["name"] for k in doc["links"]] == ["FB1"]
    assert doc["links"][0]["current_a"] == pytest.approx(g.I_U3 + g.IQ_LDO, rel=1e-6)
    assert doc["assumptions"] and isinstance(doc["warnings"], list)
    project.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    doc = tree_summary(solve_project_tree(board, project))
    assert doc["verdict"] == "fail" and "+3V3: max_drop_pct = 0.01" in doc["verdict_note"]
    assert next(ld for ld in doc["loads"] if ld["name"] == "U4")["exceeds_drop_limit"] is True


# ------------------------------------------------------------------ review fixes

def test_a_disconnected_load_is_reported_without_crashing(tmp_path):
    import json

    from coppernicus.cli import EXIT_OK, main
    from coppernicus.engine.synthetic import save_snapshot
    from coppernicus.power_tree import tree_summary
    board = Board(power_chain_board(island=True))
    project = power_chain_project()
    project.rail("+3V3").loads.append(Load("U9", 0.1, ["1"], ground_pins=["2"], ground_net="GND"))
    tree = solve_project_tree(board, project)
    u9 = _load(tree, "U9")
    assert u9.status == "DISCONNECTED" and u9.passed is None
    json.dumps(tree_summary(tree), allow_nan=False)
    save_snapshot(power_chain_board(island=True), tmp_path / "power_chain.kipi.json.gz")
    path = project.save(tmp_path / "chain.coppernicus.yml")
    assert main(["solve", str(path), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_OK


def test_a_negative_rail_drop_counts_against_its_limit():
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.project import Project, Rail
    board = Board(strip_board())
    project = Project(rails=[Rail(Strip.NET, sources=[Source("J1", -3.3)], loads=[Load("U1", 1.0)],
                                  limits=Limits(max_drop_pct=0.001))])
    tree = solve_project_tree(board, project)
    u1 = _load(tree, "U1")
    r = RHO * Strip.length() / (Strip.W * OZ1)
    assert u1.drop_v == pytest.approx(r * 1.0, rel=5e-3)              # magnitude lost
    assert u1.drop_pct == pytest.approx(100 * r / 3.3, rel=5e-3) and u1.passed is False
    assert tree.groups[Strip.NET].max_drop == pytest.approx(r, rel=5e-3)


def test_loads_behind_a_regulator_out_of_regulation_are_not_judged(board):
    project = power_chain_project()
    project.rail("+1V8").sources[0].voltage = 3.4                       # an LDO cannot boost
    project.rail("+1V8").limits = Limits(max_drop_pct=1.0)
    u3 = _load(solve_project_tree(board, project), "U3")
    assert u3.status == "VRM OUT OF REGULATION" and u3.passed is None


def test_parallel_links_share_the_estimated_current(board):
    from coppernicus.project import Link
    project = power_chain_project()
    project.links.append(Link("FB2", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", 0.05))
    res = solve_rail(board, project.rail("+3V3"), project.settings, project=project)
    est = [t for t in res.loads if "(link, estimated)" in t.name]
    assert sum(t.current for t in est) == pytest.approx(g.I_U3 + g.IQ_LDO, rel=1e-12)


def test_a_regulator_input_on_a_tied_net_is_estimated():
    from coppernicus.engine.synthetic import SyntheticBoard, two_layer_stackup
    from coppernicus.project import Project, Rail
    b = SyntheticBoard("tie_input", two_layer_stackup())
    b.square_pad("J1", "1", "+5V", 0.0, 0.0, 1.5)
    b.square_pad("NT1", "1", "+5V", 9.5, 0.0, 0.5, value="NetTie_2", lib_id="NetTie:NetTie-2")
    b.square_pad("NT1", "2", "+5V_B", 10.5, 0.0, 0.5, value="NetTie_2", lib_id="NetTie:NetTie-2")
    b.square_pad("U1", "1", "+5V_B", 20.0, 0.0, 1.0)
    b.square_pad("U1", "2", "+3V3", 20.0, 5.0, 1.0)
    b.square_pad("U2", "1", "+3V3", 30.0, 5.0, 1.0)
    for net, a, z in (("+5V", (0.0, 0.0), (9.5, 0.0)), ("+5V_B", (10.5, 0.0), (20.0, 0.0)),
                      ("+3V3", (20.0, 5.0), (30.0, 5.0))):
        b.track(net, 3, 0.5, a, z)
    board = Board(b.snapshot())
    project = Project(rails=[
        Rail("+5V", sources=[Source("J1", 5.0)]),
        Rail("+3V3", sources=[Source("U1", 3.3, ["2"], kind="smps", efficiency=0.9,
                                     input_pins=["1"], input_net="+5V_B")], loads=[Load("U2", 1.0)])])
    res = solve_rail(board, project.rail("+5V"), project.settings, project=project)
    [est] = [t for t in res.loads if t.name == "U1 (input, estimated)"]
    assert est.current == pytest.approx(3.3 * 1.0 / (0.9 * 5.0), rel=1e-12)


def test_ground_rise_limit_judges_ground_groups(board):
    from coppernicus.power_tree import tree_summary
    from coppernicus.problems import limits_dict
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rails.append(Rail("GND", limits=Limits(max_ground_rise_mv=10.0)))
    tree = solve_project_tree(board, project)
    probs = find_tree_problems(tree, "GND", tree.limits["GND"])
    worst = next(p for p in probs if p.kind == "return")
    assert worst.severity == FAIL and worst.data["rise_mv"] == pytest.approx(38.47, abs=0.02)
    assert any(p.kind == "rise" and p.severity == FAIL for p in probs)
    doc = tree_summary(tree)
    gnd = next(gr for gr in doc["groups"] if gr["group"] == "GND")
    assert gnd["verdict"] == "fail" and gnd["limits"] == {"max_ground_rise_mv": 10.0}
    assert doc["verdict"] == "fail" and "GND: max_ground_rise_mv = 10" in doc["verdict_note"]
    project.rail("GND").limits = Limits(max_ground_rise_mv=50.0)
    doc = tree_summary(solve_project_tree(board, project))
    assert next(gr for gr in doc["groups"] if gr["group"] == "GND")["verdict"] == "pass"
    # Not a limit of a supply rail, and a drop limit is not one of a ground.
    both = Limits(max_drop_pct=1.0, max_ground_rise_mv=5.0)
    assert limits_dict(both, "rail") == {"max_drop_pct": 1.0}
    assert limits_dict(both, "ground") == {"max_ground_rise_mv": 5.0}


def test_ground_rise_limit_judges_a_ground_net_solved_alone():
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.problems import find_problems
    from coppernicus.project import Rail
    rail = Rail(Strip.NET, sources=[Source("J1", 0.0)], loads=[Load("U1", 1.0)])
    res = solve_rail(Board(strip_board()), rail)
    rise_mv = RHO * Strip.length() / (Strip.W * OZ1) * 1e3
    [rise] = [p for p in find_problems(res, Limits(max_ground_rise_mv=10.0)) if p.kind == "rise"]
    assert rise.severity == FAIL and rise.data["rise_mv"] == pytest.approx(rise_mv, rel=5e-3)
    [rise] = [p for p in find_problems(res, Limits(max_ground_rise_mv=50.0)) if p.kind == "rise"]
    assert rise.severity != FAIL
    assert not [p for p in find_problems(res, Limits()) if p.kind == "rise"]
