"""The power-tree solve (engine/tree.py) on the synthetic power chain, against the hand
calculation of KiPI's power-chain accuracy test (tests/accuracy/test_dc_accuracy.py)."""

import math

import numpy as np
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
    [u4] = [p for p in find_tree_problems(tree, "+3V3", project.rail("+3V3").limits)
            if p.kind == "load" and p.data["name"] == "U4"]
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


def test_a_ground_net_set_up_as_a_rail_is_solved_as_a_ground_in_the_tree(board):
    """GND set up as a rail (a 0 V source and its return pins as loads, to solve it alone)
    does not stop the tree: there GND is solved as a ground, from the ground pins."""
    from coppernicus.engine.pisetup import to_pisetup
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rails.append(Rail("GND", sources=[Source("J1", 0.0, ["2"])], loads=[Load("U3", 2.0, ["2"])],
                              limits=Limits(max_ground_rise_mv=50.0)))
    assert all(v.output.net != "GND" for v in to_pisetup(board, project).vrms)
    tree = solve_project_tree(board, project)
    assert tree.groups["GND"].kind == "ground"
    assert tree.groups["GND"].max_drop == pytest.approx(_hand()["U3.gnd"], abs=2e-5)
    assert any("GND" in w and "solved alone" in w for w in tree.warnings)
    assert tree.limits["GND"].max_ground_rise_mv == 50.0


def test_tree_honours_the_copper_thickness_setting(board, tree):
    """The optional what-if ``copper_thickness_mm`` applies to the power tree too: doubling
    every copper layer halves the ground strip's rise, and the assumptions say so."""
    project = power_chain_project()
    project.settings.copper_thickness_mm = {"F.Cu": 2 * OZ1, "B.Cu": 2 * OZ1}
    thick = solve_project_tree(board, project)
    assert _load(thick, "U3").v_ground_v == pytest.approx(_load(tree, "U3").v_ground_v / 2, rel=1e-3)
    assert any("copper_thickness_mm" in a for a in thick.assumptions)
    assert not any("copper_thickness_mm" in a for a in tree.assumptions)


# ------------------------------------------------------------------ second review

def _strip_project(voltage, **rail):
    from coppernicus.engine.synthetic import Strip
    from coppernicus.project import Project, Rail
    return Project(rails=[Rail(Strip.NET, sources=[Source("J1", voltage)], loads=[Load("U1", 1.0)],
                               **rail)])


def test_a_negative_rail_solved_alone_fails_its_drop_limit():
    """The drop of a negative rail solved alone is a fraction of |V|: positive, so the limit
    can fail (review finding 2)."""
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.problems import find_problems
    project = _strip_project(-3.3, limits=Limits(max_drop_pct=0.001))
    res = solve_rail(Board(strip_board()), project.rails[0])
    r = RHO * Strip.length() / (Strip.W * OZ1)
    assert res.drop(res.loads[0]) == pytest.approx(r, rel=5e-3)
    assert res.drop_pct(res.loads[0]) == pytest.approx(100 * r / 3.3, rel=5e-3)
    [u1] = [p for p in find_problems(res, project.rails[0].limits) if p.kind == "load"]
    assert u1.severity == FAIL


def test_tree_text_prints_a_rail_at_0_v_without_a_percentage():
    """A rail driven at 0 V has no drop percentage: the text table shows "—" (finding 3)."""
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.power_tree import tree_summary, tree_text
    doc = tree_summary(solve_project_tree(Board(strip_board()), _strip_project(0.0)))
    [u1] = doc["loads"]
    assert u1["drop_pct"] is None
    lines = "\n".join(tree_text(doc))
    assert "—" in lines and Strip.NET in lines


def _group_lines(doc):
    from coppernicus.power_tree import tree_text
    lines = tree_text(doc)
    keys = {gr["group"] for gr in doc["groups"]}
    return {line.split()[0]: line for line in lines[lines.index("Groups") + 1:]
            if line.split() and line.split()[0] in keys}


def test_tree_text_names_the_side_of_each_group_s_reference(tree):
    """A group's worst reads as the summaries do: below the nominal on a positive rail,
    above it (toward 0 V) on a negative one, a rise or shift from 0 V on a ground; the
    copper loss stays in one column."""
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.power_tree import tree_summary
    doc = tree_summary(tree)
    lines = _group_lines(doc)
    for gr in doc["groups"]:
        if gr["kind"] == "rail":
            want = (f"max drop {gr['max_drop_mv']:.3f} mV ({gr['max_drop_pct']:.3f} %) "
                    f"below {gr['v_ref_v']:g} V,")
        else:
            want = f"max ground rise {gr['max_rise_mv']:.3f} mV above 0 V,"
        assert want in lines[gr["group"]], lines[gr["group"]]
    assert len({line.index("copper loss") for line in lines.values()}) == 1, lines
    neg = tree_summary(solve_project_tree(Board(strip_board()), _strip_project(-3.3)))
    [gr] = neg["groups"]
    assert (f"max drop {gr['max_drop_mv']:.3f} mV ({gr['max_drop_pct']:.3f} %) above -3.3 V "
            "(toward 0 V),") in _group_lines(neg)[Strip.NET]
    zero = tree_summary(solve_project_tree(Board(strip_board()), _strip_project(0.0)))
    [gr] = zero["groups"]
    line = _group_lines(zero)[Strip.NET]
    assert f"max drop {gr['max_drop_mv']:.3f} mV below 0 V," in line and "—" not in line, line


def _mv_in(title, words):
    """The mV figure of a problem title that ends "(<mV> mV <words>)"."""
    import re
    m = re.search(r"\((\d+\.\d\d) mV " + re.escape(words) + r"\)$", title)
    assert m, title
    return float(m.group(1))


def test_a_load_on_a_rail_at_0_v_in_the_tree_has_no_nan_percentage():
    """A 0 V rail has no drop percentage: its load's title gives the mV only, below 0 V
    (the tree draws its current out of it), never "nan %"."""
    from coppernicus.engine.synthetic import Strip, strip_board
    tree = solve_project_tree(Board(strip_board()), _strip_project(0.0))
    [u1] = [p for p in find_tree_problems(tree, Strip.NET) if p.kind == "load"]
    r_mv = RHO * Strip.length() / (Strip.W * OZ1) * 1e3
    assert "nan" not in u1.title.lower()
    assert _mv_in(u1.title, "below 0 V") == pytest.approx(r_mv, rel=5e-3)


def test_a_regulator_input_on_a_rail_at_0_v_in_the_tree_has_no_nan_percentage():
    from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
    from coppernicus.project import Project, Rail
    b = SyntheticBoard("zero_chain", two_layer_stackup())
    b.square_pad("J1", "1", "VZ", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VZ", 40.0, 0.0, 1.5)
    b.square_pad("U1", "2", "VO", 40.0, 5.0, 1.5)
    b.square_pad("U2", "1", "VO", 60.0, 5.0, 1.5)
    b.track("VZ", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.track("VO", F, 0.5, (40.0, 5.0), (60.0, 5.0))
    project = Project(rails=[
        Rail("VZ", sources=[Source("J1", 0.0)]),
        Rail("VO", sources=[Source("U1", 1.0, ["2"], kind="ldo", input_pins=["1"], input_net="VZ")],
             loads=[Load("U2", 0.5)])])
    tree = solve_project_tree(Board(b.snapshot()), project)
    [inp] = [p for p in find_tree_problems(tree, "VZ") if p.kind == "load"]
    assert inp.data["name"] == "U1 (input)" and "nan" not in inp.title.lower()
    # 0.5 A through 40 mm of 0.5 mm track (pad edge to pad edge: 38.5 mm)
    assert _mv_in(inp.title, "below 0 V") == pytest.approx(0.5 * RHO * 38.5 / (0.5 * OZ1) * 1e3, rel=2e-2)


def test_loads_and_regulator_inputs_on_a_rail_at_0_v_in_the_tree_are_ranked_by_mv():
    """A 0 V rail has no %: its loads and the regulator inputs it feeds are ranked together
    by |mV|, largest first. Along one track from J1: U3 at 20 mm, U1's input at 40 mm, U2 at
    60 mm, so the drop grows U3 < U1 (input) < U2."""
    from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
    from coppernicus.project import Project, Rail
    b = SyntheticBoard("zero_rank", two_layer_stackup())
    b.square_pad("J1", "1", "VZ", 0.0, 0.0, 1.5)
    b.square_pad("U3", "1", "VZ", 20.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VZ", 40.0, 0.0, 1.5)
    b.square_pad("U2", "1", "VZ", 60.0, 0.0, 1.5)
    b.square_pad("U1", "2", "VO", 40.0, 5.0, 1.5)
    b.square_pad("U4", "1", "VO", 60.0, 5.0, 1.5)
    b.track("VZ", F, 0.5, (0.0, 0.0), (60.0, 0.0))
    b.track("VO", F, 0.5, (40.0, 5.0), (60.0, 5.0))
    project = Project(rails=[
        Rail("VZ", sources=[Source("J1", 0.0)], loads=[Load("U3", 0.5), Load("U2", 0.5)]),
        Rail("VO", sources=[Source("U1", 1.0, ["2"], kind="ldo", input_pins=["1"], input_net="VZ")],
             loads=[Load("U4", 0.5)])])
    tree = solve_project_tree(Board(b.snapshot()), project)
    loads = [p for p in find_tree_problems(tree, "VZ") if p.kind == "load"]
    assert [p.data["name"] for p in loads] == ["U2", "U1 (input)", "U3"]
    mv = [abs(p.data["drop_mv"]) for p in loads]
    assert mv == sorted(mv, reverse=True)


def test_an_excluded_ground_rail_is_not_said_to_be_solved_as_a_ground(board):
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rails.append(Rail("GND", sources=[Source("J1", 0.0, ["2"])], loads=[Load("U3", 2.0, ["2"])]))
    project.engine_options = {"excluded_nets": ["GND"]}
    tree = solve_project_tree(board, project)
    assert "GND" not in tree.groups
    assert not any("solved as a ground" in w for w in tree.warnings)


@pytest.mark.parametrize("excluded, present, absent", [
    ([], ["at the ground pins of J1;", "U2 holds 1.8 V at U3 pins 1 on +1V8 (remote sense), above U3 pins 2 "
          "on GND.", "Link FB1:"], []),
    # U2's output is not solved: it is an estimated load on +3V3_F, its remote sense is gone
    (["+1V8"], ["at the ground pins of J1;", "Link FB1:"], ["U2 holds"]),
    # no ground solved: no 0 V reference, no ground sense
    (["GND"], ["U2 holds 1.8 V at U3 pins 1 on +1V8 (remote sense)."], ["Ground nets are held", "on GND"]),
    # FB1's group is not solved; U2, fed from it, is an ideal source and references the ground
    (["+3V3_F"], ["at the ground pins of J1, U2;"], ["Link FB1"]),
    # J1 drives VIN, not solved: U1, fed from it, is the source referencing the ground
    (["VIN"], ["at the ground pins of U1;"], ["of J1"]),
])
def test_tree_assumptions_name_only_parts_that_are_solved(board, excluded, present, absent):
    project = power_chain_project()
    project.engine_options = {"excluded_nets": excluded}
    text = "\n".join(solve_project_tree(board, project).assumptions)
    assert all(p in text for p in present), text
    assert not any(a in text for a in absent), text


def test_a_ground_below_0_v_is_a_rise_by_its_magnitude():
    """Under a negative rail the ground sinks below 0 V: the group's largest rise, the rise
    problem and the return pins all give |V| from 0 V, and the limit judges it (finding 9)."""
    from coppernicus.project import Project, Rail
    b = strip_board_with_ground()
    project = Project(rails=[
        Rail("VCC", sources=[Source("J1", -3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="GND")]),
        Rail("GND", limits=Limits(max_ground_rise_mv=1.0))])
    tree = solve_project_tree(b, project)
    gnd = tree.groups["GND"]
    [ret] = [t for t in gnd.loads if t.name == "U1 (return)"]
    assert ret.v_min < -1e-3                                       # below 0 V
    # One number with one sign everywhere: the rise with the largest magnitude, signed.
    assert gnd.max_drop == pytest.approx(ret.v_min, rel=1e-3)
    probs = find_tree_problems(tree, "GND", tree.limits["GND"])
    rise = next(p for p in probs if p.kind == "rise")
    pin = next(p for p in probs if p.kind == "return")
    assert rise.severity == FAIL and pin.severity == FAIL
    assert rise.data["rise_mv"] == pytest.approx(gnd.max_drop * 1e3, rel=1e-3)
    assert pin.data["rise_mv"] == pytest.approx(ret.v_min * 1e3, rel=1e-3)
    assert "below 0 V" in rise.title and "below 0 V" in pin.title
    assert "largest ground shift" in rise.title and "rise" not in rise.title
    assert pin.value == pytest.approx(abs(ret.v_min) * 1e3, rel=1e-3)        # ranked by magnitude
    from coppernicus.power_tree import tree_summary, tree_text
    doc = tree_summary(tree)
    row = next(gr for gr in doc["groups"] if gr["group"] == "GND")
    assert row["max_rise_mv"] == pytest.approx(gnd.max_drop * 1e3) and row["verdict"] == "fail"
    [line] = [line for line in tree_text(doc) if line.lstrip().startswith("GND ")]
    assert "max ground shift" in line and "mV below 0 V" in line and "rise" not in line, line


def strip_board_with_ground():
    """VCC: J1.1 → U1.1 and GND: J1.2 → U1.2, 40 mm tracks on F.Cu."""
    from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
    b = SyntheticBoard("strip_gnd", two_layer_stackup())
    for ref, x in (("J1", 0.0), ("U1", 40.0)):
        b.square_pad(ref, "1", "VCC", x, 0.0, 1.5)
        b.square_pad(ref, "2", "GND", x, 5.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.track("GND", F, 0.5, (0.0, 5.0), (40.0, 5.0))
    return Board(b.snapshot())


def _group_doc(doc, key):
    return next(gr for gr in doc["groups"] if gr["group"] == key)


def test_a_limit_with_an_item_it_cannot_judge_is_not_a_pass(board):
    """A group says pass only when everything its limits apply to was judged and passed
    (review finding 1a, 1c): a load behind a regulator out of regulation, or disconnected,
    makes the group and the overall verdict not_judged, and the note names it."""
    from coppernicus.power_tree import tree_summary
    project = power_chain_project()
    project.rail("+1V8").sources[0].voltage = 3.4                       # an LDO cannot boost
    project.rail("+1V8").limits = Limits(max_drop_pct=1.0)
    doc = tree_summary(solve_project_tree(board, project))
    row = _group_doc(doc, "+1V8")
    assert row["verdict"] == "not_judged"
    assert {"item": "U3", "net": "+1V8", "limit": "max_drop_pct",
            "reason": "VRM OUT OF REGULATION"} in row["not_judged"]
    assert doc["verdict"] == "not_judged" and "U3" in doc["verdict_note"]
    assert row["not_judged"][0] in doc["not_judged"]

    project = power_chain_project()
    project.rail("+3V3").loads = [Load("U9", 0.1, ["1"], ground_pins=["2"], ground_net="GND")]
    project.rail("+3V3").limits = Limits(max_drop_pct=50.0)
    doc = tree_summary(solve_project_tree(Board(power_chain_board(island=True)), project))
    row = _group_doc(doc, "+3V3")
    assert row["verdict"] == "not_judged" and {"item": "U9", "net": "+3V3", "limit": "max_drop_pct",
                                                "reason": "DISCONNECTED"} in row["not_judged"]
    assert doc["verdict"] == "not_judged"


def test_regulator_inputs_are_judged_on_the_rail_feeding_them(board):
    """A regulator's input is a load on the rail that feeds it: judged against that rail's
    drop limit, across its input and ground pins, as VIN solved alone judges U1's estimated
    input (review finding 1b)."""
    from coppernicus.power_tree import tree_summary
    project = power_chain_project()
    project.rail("VIN").limits = Limits(max_drop_pct=0.001)
    tree = solve_project_tree(board, project)
    u1 = _vrm(tree, "U1")
    h = _hand()
    assert u1.input_net == "VIN" and u1.input_group == "VIN"
    assert u1.v_in_worst_v == pytest.approx(h["U1.v_in"] - h["U1.gnd"], abs=2e-6)
    assert u1.input_drop_v == pytest.approx(g.V_SRC - (h["U1.v_in"] - h["U1.gnd"]), abs=2e-6)
    assert u1.input_drop_pct == pytest.approx(100 * u1.input_drop_v / g.V_SRC)
    assert u1.input_passed is False and u1.input_status == ""
    [p] = [p for p in find_tree_problems(tree, "VIN") if p.kind == "load"]
    assert p.data["name"] == "U1 (input)" and p.severity == FAIL
    doc = tree_summary(tree)
    assert _group_doc(doc, "VIN")["verdict"] == "fail" and doc["verdict"] == "fail"
    row = next(v for v in doc["vrms"] if v["name"] == "U1")
    assert row["input_net"] == "VIN" and row["input_exceeds_drop_limit"] is True
    assert row["input_drop_mv"] == pytest.approx(u1.input_drop_v * 1e3)
    # VIN solved alone fails the same limit on U1's estimated input.
    from coppernicus.problems import find_problems
    alone = solve_rail(board, project.rail("VIN"), project.settings, project=project)
    assert any(p.severity == FAIL for p in find_problems(alone, project.rail("VIN").limits)
               if p.kind == "load")
    project.rail("VIN").limits = Limits(max_drop_pct=1.0)
    doc = tree_summary(solve_project_tree(board, project))
    assert _group_doc(doc, "VIN")["verdict"] == "pass"
    assert next(v for v in doc["vrms"] if v["name"] == "J1")["input_exceeds_drop_limit"] is None


def test_limits_are_looked_up_per_net_with_the_driving_net_as_default(board):
    """Each net's limits judge what is on it: U2's input on +3V3_F by +3V3_F's limit, and
    a net without its own limit takes its group's driving net's (review finding 6)."""
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rails.append(Rail("+3V3_F", limits=Limits(max_drop_pct=0.01)))
    tree = solve_project_tree(board, project)
    u2 = _vrm(tree, "U2")
    assert u2.input_net == "+3V3_F" and u2.input_group == "+3V3" and u2.input_passed is False
    assert _load(tree, "U4").passed is None                       # +3V3 itself has no limit
    # The limit only on the driven net judges the far side of the ferrite too.
    project = power_chain_project()
    project.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    tree = solve_project_tree(board, project)
    assert _vrm(tree, "U2").input_passed is False and _load(tree, "U4").passed is False
    assert any("driving net" in a for a in tree.assumptions)
    assert any("strictest" in a for a in tree.assumptions)


def _two_grounds_board():
    """GND and AGND joined by the net tie NT1; J1 returns on GND, U1 on AGND."""
    from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
    b = SyntheticBoard("two_gnd", two_layer_stackup())
    lib = "NetTie:NetTie-2_SMD_Pad0.5mm"
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.square_pad("J1", "2", "GND", 0.0, 5.0, 1.5)
    b.square_pad("NT1", "1", "GND", 19.5, 5.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("NT1", "2", "AGND", 20.5, 5.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("U1", "2", "AGND", 40.0, 5.0, 1.5)
    b.track("GND", F, 0.5, (0.0, 5.0), (19.5, 5.0))
    b.track("AGND", F, 0.5, (20.5, 5.0), (40.0, 5.0))
    return Board(b.snapshot())


@pytest.mark.parametrize("net", ["AGND", "GND"])
def test_a_ground_rise_limit_on_either_tied_ground_net_is_judged(net):
    from coppernicus.power_tree import tree_summary
    from coppernicus.project import Project, Rail
    project = Project(rails=[
        Rail("VCC", sources=[Source("J1", 3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="AGND")]),
        Rail(net, limits=Limits(max_ground_rise_mv=1.0))])
    tree = solve_project_tree(_two_grounds_board(), project)
    assert set(tree.groups["GND"].nets) == {"GND", "AGND"}
    probs = find_tree_problems(tree, "GND")
    pin = next(p for p in probs if p.kind == "return" and p.data["name"] == "U1 (return)")
    rise = next(p for p in probs if p.kind == "rise")
    assert pin.severity == FAIL and rise.severity == FAIL
    # A copper problem names the limit it used and the net it came from.
    assert f"1 mV from {net}" in rise.title and "strictest in group" in rise.title
    doc = tree_summary(tree)
    assert _group_doc(doc, "GND")["verdict"] == "fail" and doc["verdict"] == "fail"
    assert f"{net}: max_ground_rise_mv = 1" in doc["verdict_note"]


def test_limits_can_be_applied_again_without_solving(board):
    """The GUI re-judges a solved tree when a limit changes (or changed while it solved):
    apply_limits reads the project's current limits."""
    from coppernicus.power_tree import apply_limits
    project = power_chain_project()
    tree = solve_project_tree(board, project)
    assert _load(tree, "U4").passed is None
    project.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    apply_limits(tree, project)
    assert _load(tree, "U4").passed is False and _vrm(tree, "U2").input_passed is False
    project.rail("+3V3").limits = Limits()
    apply_limits(tree, project)
    assert _load(tree, "U4").passed is None and _vrm(tree, "U2").input_passed is None


def test_a_group_that_did_not_converge_is_not_judged():
    """Nothing in a group whose iteration did not converge is judged, copper included."""
    from coppernicus.power_tree import group_verdict
    from coppernicus.results import LoadReport, RailResult, TreeResult, ViaCurrent
    res = RailResult(net="VCC", nets=["VCC"], v_ref=3.3, layers=[], sources=[], loads=[],
                     vias=[ViaCurrent("v1", "via", (0.0, 0.0), 0.3, 5.0, 0.01)], max_drop=0.1,
                     j_max=1.0, power_loss=0.1, energy_balance_error=0.0, converged=False)
    tree = TreeResult(groups={"VCC": res}, loads=[LoadReport(
        "U1", "VCC", 1.0, 3.3, 3.2, 3.2, None, 3.2, 3.2, 0.1, 3.03, status="NOT CONVERGED",
        net="VCC")], limits={"VCC": Limits(max_drop_pct=1.0, max_via_current=1.0)})
    gv = group_verdict(tree, "VCC")
    assert gv.verdict == "not_judged" and gv.failed == 0
    assert any(x["item"] == "VCC" and x["net"] == "VCC" and "did not converge" in x["reason"]
               for x in gv.not_judged)
    res.converged = True
    assert group_verdict(tree, "VCC").verdict == "fail"          # the via over 1 A


def test_a_limit_on_a_net_the_tree_does_not_solve_is_not_judged(board):
    from coppernicus.power_tree import tree_summary
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rail("+1V8").limits = Limits(max_drop_pct=50.0)
    project.rails.append(Rail("+5V", limits=Limits(max_drop_pct=1.0)))     # picked, not set up
    doc = tree_summary(solve_project_tree(board, project))
    assert doc["verdict"] == "not_judged"
    assert {"item": "+5V", "net": "+5V", "limit": "max_drop_pct",
            "reason": "not solved in the power tree"} in doc["not_judged"]
    assert "+5V" in doc["verdict_note"]
    project.rails.pop()
    assert tree_summary(solve_project_tree(board, project))["verdict"] == "pass"


def test_a_copper_fail_names_the_limit_of_a_net_joined_by_a_link(board):
    """A current-density limit only on +3V3_F (beyond FB1) judges the copper of the whole
    +3V3 group, the strictest of its nets, and its fails say so (finding 6)."""
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rails.append(Rail("+3V3_F", limits=Limits(max_current_density=1.0)))
    tree = solve_project_tree(board, project)
    probs = find_tree_problems(tree, "+3V3")
    spots = [p for p in probs if p.kind == "hotspot" and p.severity == FAIL]
    assert spots and all("limit 1 A/mm² from +3V3_F, strictest in group" in p.title for p in spots)


def test_a_via_fail_names_the_limit_of_a_net_joined_by_a_net_tie():
    """VCC → NT1 → VCC_B, which changes layer through a via: a via limit only on VCC_B
    judges the group's vias, and the fail says where the limit came from."""
    from coppernicus.engine.synthetic import B, F, SyntheticBoard, two_layer_stackup
    from coppernicus.project import Project, Rail
    b = SyntheticBoard("tie_via", two_layer_stackup())
    lib = "NetTie:NetTie-2_SMD_Pad0.5mm"
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("NT1", "1", "VCC", 9.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("NT1", "2", "VCC_B", 10.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("U1", "1", "VCC_B", 30.0, 0.0, 1.5, layers=(B,))
    b.track("VCC", F, 0.5, (0.0, 0.0), (9.5, 0.0))
    b.track("VCC_B", F, 0.5, (10.5, 0.0), (20.0, 0.0))
    b.via("VCC_B", 20.0, 0.0)
    b.track("VCC_B", B, 0.5, (20.0, 0.0), (30.0, 0.0))
    project = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)], loads=[Load("U1", 1.0)]),
                             Rail("VCC_B", limits=Limits(max_via_current=0.5))])
    tree = solve_project_tree(Board(b.snapshot()), project)
    assert set(tree.groups["VCC"].nets) == {"VCC", "VCC_B"} and tree.groups["VCC"].vias
    [via] = [p for p in find_tree_problems(tree, "VCC") if p.kind == "via"]
    assert via.severity == FAIL and "limit 0.5 A from VCC_B, strictest in group" in via.title


def test_a_drop_limit_on_a_rail_at_0_v_is_not_judged():
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.power_tree import group_verdict
    tree = solve_project_tree(Board(strip_board()), _strip_project(0.0, limits=Limits(max_drop_pct=1.0)))
    gv = group_verdict(tree, Strip.NET)
    assert gv.verdict == "not_judged"
    assert any(x["limit"] == "max_drop_pct" and "0 V" in x["reason"] for x in gv.not_judged)


# ------------------------------------------------------------------ T7.1

def test_a_load_on_a_0_v_net_has_no_percentage_in_its_title():
    from coppernicus.engine.synthetic import strip_board
    from coppernicus.problems import find_problems
    res = solve_rail(Board(strip_board()), _strip_project(0.0).rails[0])
    [u1] = [p for p in find_problems(res) if p.kind == "load"]
    assert "nan" not in u1.title.lower() and "%" not in u1.title


def test_a_limit_of_the_wrong_kind_is_not_judged_in_the_tree(board):
    """One rule by reference voltage: max_ground_rise_mv applies to nets at 0 V, max_drop_pct
    to nets with a nonzero nominal; one set on the other kind is a not-judged entry."""
    from coppernicus.power_tree import tree_summary
    from coppernicus.project import Rail
    project = power_chain_project()
    project.rail("VIN").limits = Limits(max_drop_pct=5.0)
    project.rail("+3V3").limits = Limits(max_ground_rise_mv=0.001)
    doc = tree_summary(solve_project_tree(board, project))
    assert doc["verdict"] == "not_judged"
    [item] = [x for x in doc["not_judged"] if x["net"] == "+3V3"]
    assert (item["item"], item["limit"]) == ("+3V3", "max_ground_rise_mv") and "0 V" in item["reason"]
    assert _group_doc(doc, "+3V3")["verdict"] == "not_judged"
    project.rail("+3V3").limits = Limits()
    project.rails.append(Rail("GND", limits=Limits(max_drop_pct=1.0)))
    doc = tree_summary(solve_project_tree(board, project))
    assert doc["verdict"] == "not_judged"
    [item] = [x for x in doc["not_judged"] if x["net"] == "GND"]
    assert (item["item"], item["limit"]) == ("GND", "max_drop_pct")


def test_a_rail_driven_at_0_v_in_the_tree_takes_a_ground_rise_limit():
    from coppernicus.engine.synthetic import Strip, strip_board
    tree = solve_project_tree(Board(strip_board()),
                              _strip_project(0.0, limits=Limits(max_ground_rise_mv=1.0)))
    probs = find_tree_problems(tree, Strip.NET)
    assert any(p.kind == "rise" and p.severity == FAIL for p in probs)


def test_a_ground_rise_limit_on_a_supply_rail_solved_alone_is_not_judged(tmp_path):
    """The same rule for a rail solved alone: no rise problem on a 3.3 V rail, and the
    service lists the limit as not judged instead of failing it."""
    from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board
    from coppernicus.problems import find_problems, inapplicable_limits
    from coppernicus.service import Session
    res = solve_rail(Board(strip_board()), _strip_project(3.3).rails[0])
    limits = Limits(max_ground_rise_mv=0.001)
    assert not [p for p in find_problems(res, limits) if p.kind == "rise"]
    [item] = inapplicable_limits(Strip.NET, limits, res.v_ref)
    assert item["item"] == Strip.NET and item["limit"] == "max_ground_rise_mv" and "0 V" in item["reason"]
    assert inapplicable_limits(Strip.NET, limits, 0.0) == []
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(tmp_path / "strip.kipi.json.gz")["board_id"]
    out = session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": 3.3}],
                             [{"ref": "U1", "current_a": 1.0}], limits={"max_ground_rise_mv": 0.001})
    assert out["verdict"] == "not_judged" and out["not_judged"][0]["limit"] == "max_ground_rise_mv"


def test_the_note_names_a_wrong_kind_limit_when_it_is_the_only_one(board):
    from coppernicus.power_tree import tree_summary, tree_text
    project = power_chain_project()
    project.rail("+3V3").limits = Limits(max_ground_rise_mv=0.001)
    doc = tree_summary(solve_project_tree(board, project))
    assert doc["verdict"] == "not_judged" and "No limits set" not in doc["verdict_note"]
    assert "+3V3 (max_ground_rise_mv)" in doc["verdict_note"]
    verdict = next(line for line in tree_text(doc) if line.startswith("Verdict:"))
    assert "max_ground_rise_mv" in verdict


# ------------------------------------------------------------------ T7.1 (d): the sign of the load current

def test_a_negative_rail_solved_alone_shows_the_voltages_of_the_tree():
    """On a negative rail the load current flows into the copper (KiPI #220): the load sits
    closer to 0 V than the source, alone as in the tree."""
    from coppernicus.engine.synthetic import Strip, strip_board
    board, project = Board(strip_board()), _strip_project(-3.3)
    alone = solve_rail(board, project.rails[0])
    [tree_u1] = solve_project_tree(board, project).groups[Strip.NET].loads
    [u1] = alone.loads
    r = RHO * Strip.length() / (Strip.W * OZ1)
    assert u1.v_max == pytest.approx(tree_u1.v_max, abs=1e-9)
    assert u1.v_max == pytest.approx(-3.3 + r, rel=5e-3)
    assert alone.drop_sign == -1.0 and alone.worst_v(u1) == u1.v_max
    assert alone.drop(u1) == pytest.approx(r, rel=5e-3) and alone.drop_pct(u1) > 0
    assert alone.max_drop == pytest.approx(r, rel=5e-3)
    assert u1.current == 1.0 and alone.sources[0].current == pytest.approx(1.0)


def test_the_load_current_sign_mirrors_the_copper_and_nothing_else():
    """The same strip at +3.3 V, −3.3 V and 0 V, on the same mesh: node by node V(−3.3) =
    −V(+3.3) and V(0) = 3.3 − V(+3.3); current density, losses and drops are the same."""
    from coppernicus.engine.synthetic import strip_board
    board = Board(strip_board())
    pos, neg, zero = (solve_rail(board, _strip_project(v).rails[0]) for v in (3.3, -3.3, 0.0))
    for a, b, c in zip(pos.layers, neg.layers, zero.layers):
        assert np.allclose(b.voltage, -a.voltage, atol=1e-12, equal_nan=True)
        assert np.allclose(c.voltage, 3.3 - a.voltage, atol=1e-12, equal_nan=True)
        assert np.allclose(b.current_density, a.current_density) and np.allclose(c.tri_power, a.tri_power)
    for res in (neg, zero):
        assert res.power_loss == pytest.approx(pos.power_loss, rel=1e-12)
        assert res.max_drop == pytest.approx(pos.max_drop, rel=1e-9)
        assert res.drop(res.loads[0]) == pytest.approx(pos.drop(pos.loads[0]), rel=1e-9)
        assert np.allclose(res.drop_at(res.layers[0].voltage), pos.drop_at(pos.layers[0].voltage),
                           equal_nan=True)
    assert (pos.drop_sign, neg.drop_sign, zero.drop_sign) == (1.0, -1.0, -1.0)


def test_a_ground_net_solved_alone_rises_as_its_ground_group_in_the_tree():
    """A net driven at 0 V solved alone (a 0 V source and the return pins as loads): the
    return current flows into the copper, so it rises above 0 V as the tree's ground does."""
    from coppernicus.project import Project, Rail
    board = strip_board_with_ground()
    tree = solve_project_tree(board, Project(rails=[
        Rail("VCC", sources=[Source("J1", 3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="GND")])]))
    [ret] = [t for t in tree.groups["GND"].loads if t.name == "U1 (return)"]
    alone = solve_rail(board, Rail("GND", sources=[Source("J1", 0.0, ["2"])], loads=[Load("U1", 1.0, ["2"])]))
    [u1] = alone.loads
    assert ret.v_max > 1e-3 and u1.v_max == pytest.approx(ret.v_max, rel=1e-6)
    assert alone.drop(u1) == pytest.approx(ret.v_max, rel=1e-6)
    assert alone.max_drop == pytest.approx(tree.groups["GND"].max_drop, rel=1e-6)


def test_a_rail_driven_at_0_v_in_the_tree_warns_that_its_sign_is_opposite():
    from coppernicus.engine.synthetic import Strip, strip_board
    from coppernicus.power_tree import tree_summary, tree_text
    tree = solve_project_tree(Board(strip_board()), _strip_project(0.0))
    assert tree.groups[Strip.NET].drop_sign == 1.0
    [w] = [w for w in tree.warnings if "opposite sign" in w]
    assert Strip.NET in w and "ground pins" in w
    assert w in tree.groups[Strip.NET].warnings                 # the group's notes in the GUI
    assert any("opposite sign" in line for line in tree_text(tree_summary(tree)))


def test_sources_on_both_sides_of_0_v_are_refused():
    from coppernicus.engine.synthetic import strip_board
    from coppernicus.project import Rail
    rail = Rail("VCC", sources=[Source("J1", 3.3), Source("U1", -3.3)], loads=[Load("U1", 0.1)])
    with pytest.raises(EngineError, match="J1.*U1.*0 V"):
        solve_rail(Board(strip_board()), rail)


def test_the_probe_summary_and_report_show_a_negative_rail_drop_positive(tmp_path):
    import json
    import re

    from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board
    from coppernicus.service import Session
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(tmp_path / "strip.kipi.json.gz")["board_id"]
    out = session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": -3.3}],
                             [{"ref": "U1", "current_a": 1.0}])
    r_mv = RHO * Strip.length() / (Strip.W * OZ1) * 1e3
    assert out["drop_sign"] == -1 and out["layers"][0]["max_drop_mv"] == pytest.approx(r_mv, rel=5e-3)
    probe = session.probe(out["result_id"], "F.Cu", Strip.X1 - 1.0, Strip.Y)
    assert probe["voltage_v"] > -3.3 and probe["drop_mv"] > 0
    page = session.report([out["result_id"]], path=tmp_path / "neg.html")["path"]
    meta = json.loads(re.search(r'id="meta">(.*?)</script>', open(page, encoding="utf-8").read(),
                                re.S).group(1))
    rail = meta["rails"][0]
    assert rail["drop_sign"] == -1 and rail["drop_max_mv"] == pytest.approx(r_mv, rel=5e-3)
    assert "toward 0 V" in rail["drop_unit"] and rail["layers"][0]["max_drop_mv"] > 0
    assert rail["loads"][0]["v_worst_v"] == pytest.approx(-3.3 + r_mv / 1e3, rel=5e-3)


def test_an_estimated_draw_on_a_negative_rail_flows_into_the_copper():
    """A negative rail feeding a regulator, solved alone with the project: the estimated
    input draw has the rail's sign too, so its pin sits toward 0 V, its current positive."""
    from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
    from coppernicus.project import Project, Rail
    b = SyntheticBoard("neg_chain", two_layer_stackup())
    b.square_pad("J1", "1", "VNEG", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VNEG", 40.0, 0.0, 1.5)
    b.square_pad("U1", "2", "VEE", 40.0, 5.0, 1.5)
    b.square_pad("U2", "1", "VEE", 60.0, 5.0, 1.5)
    b.track("VNEG", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.track("VEE", F, 0.5, (40.0, 5.0), (60.0, 5.0))
    project = Project(rails=[
        Rail("VNEG", sources=[Source("J1", -5.0)]),
        Rail("VEE", sources=[Source("U1", -3.3, ["2"], kind="ldo", input_pins=["1"], input_net="VNEG")],
             loads=[Load("U2", 1.0)])])
    res = solve_rail(Board(b.snapshot()), project.rail("VNEG"), project.settings, project=project)
    [est] = res.loads
    assert est.name == "U1 (input, estimated)" and est.current == pytest.approx(1.0)
    assert est.v_max > -5.0 and res.drop(est) > 0
