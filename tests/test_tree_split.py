"""The power tree's per-net split (S1): each node's net, each net's voltages and losses, each
link's voltage across it, and each load's and regulator input's drop as segments that add up
to it exactly. Shown, never judged."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from coppernicus.engine import Board
from coppernicus.engine.synthetic import (
    B,
    F,
    Strip,
    SyntheticBoard,
    pack_board,
    pack_project,
    power_chain_board,
    power_chain_project,
    save_snapshot,
    strip_board,
    two_layer_stackup,
)
from coppernicus.engine.tree import solve_project_tree
from coppernicus.power_tree import tree_summary
from coppernicus.project import Link, Load, Project, Rail, Source

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"
SUM_TOL_V = 1e-12

# The keys of the tree document before the split (examples/power_chain on main e267ae5): the
# split only adds keys.
OLD_GROUP_KEYS = {"group", "kind", "nets", "v_ref_v", "max_drop_mv", "max_rise_mv", "max_drop_pct",
                  "power_loss_w", "loss_breakdown_w", "j_point_max_a_mm2", "degenerate_triangles",
                  "via_current_max_a", "verdict", "limits", "failed", "attention", "limits_by_net",
                  "not_judged", "problems"}
OLD_LINK_KEYS = {"name", "group", "nets", "resistance_ohm", "current_a", "power_w"}
OLD_LOAD_KEYS = {"name", "group", "current_a", "nominal_v", "v_supply_v", "v_supply_min_v",
                 "v_ground_v", "v_across_v", "v_worst_v", "drop_mv", "drop_pct", "status",
                 "exceeds_drop_limit", "net", "rail"}
OLD_VRM_KEYS = {"name", "kind", "group", "v_set_v", "v_out_v", "i_out_a", "i_max_a", "v_in_v", "i_in_a",
                "p_out_w", "p_in_w", "p_loss_w", "v_ground_v", "v_sense_v", "v_ground_sense_v",
                "regulation", "input_net", "input_drop_mv", "input_drop_pct", "input_status",
                "input_exceeds_drop_limit", "exceeds_current_limit", "current_limit_from"}
NEW_KEYS = {"groups": {"net_split", "split_note"}, "links": {"v_a_v", "v_b_v", "across_mv"},
            "loads": {"segments", "segments_sum_mv"}, "vrms": {"segments", "segments_sum_mv"}}


@pytest.fixture(scope="module")
def chain():
    project = Project.load(CHAIN / "power_chain.coppernicus.yml")
    return solve_project_tree(Board.open(project.board_path()), project)


@pytest.fixture(scope="module")
def chain_doc(chain):
    return tree_summary(chain)


def _load(tree, name):
    return next(ld for ld in tree.loads if ld.name == name)


def _vrm(tree, name):
    return next(v for v in tree.vrms if v.name == name)


def _mv(segments):
    return [s.v * 1e3 for s in segments]


def _assert_segments_add_up(tree):
    """Every load and regulator input with a drop: its segments add up to it within 1e-12 V."""
    checked = 0
    for ld in tree.loads:
        if math.isfinite(ld.drop_v):
            assert ld.segments, ld.name
            assert abs(sum(s.v for s in ld.segments) - ld.drop_v) <= SUM_TOL_V, ld.name
            checked += 1
    for v in tree.vrms:
        if v.input_drop_v is not None and math.isfinite(v.input_drop_v):
            assert v.segments, v.name
            assert abs(sum(s.v for s in v.segments) - v.input_drop_v) <= SUM_TOL_V, v.name
            checked += 1
        elif v.input_drop_v is None:
            assert not v.segments, v.name
    return checked


# ---------------------------------------------------------------------- power_chain (prototype)

def test_power_chain_segments_reproduce_the_prototype(chain):
    u2 = _vrm(chain, "U2")
    assert [s.what for s in u2.segments] == ["regulation", "copper", "link", "copper", "ground copper"]
    assert [s.name for s in u2.segments] == ["U1", "+3V3", "FB1", "+3V3_F", "GND"]
    assert _mv(u2.segments) == pytest.approx([0.0, 14.914, 100.100, 6.903, 25.145], abs=5e-4)
    assert u2.input_drop_v * 1e3 == pytest.approx(147.061, abs=5e-4)
    u4 = _load(chain, "U4")
    assert [s.what for s in u4.segments] == ["regulation", "copper", "ground copper"]
    assert _mv(u4.segments) == pytest.approx([0.0, 8.011, 8.627], abs=5e-4)
    assert u4.drop_v * 1e3 == pytest.approx(16.638, abs=5e-4)
    u3 = _load(chain, "U3")
    assert [s.name for s in u3.segments] == ["U2", "+1V8", "GND"]
    assert _mv(u3.segments) == pytest.approx([-14.531, 6.896, 7.635], abs=5e-4)
    assert u3.drop_v * 1e3 == pytest.approx(0.0, abs=5e-4)


def test_power_chain_segments_add_up_to_each_drop(chain):
    assert _assert_segments_add_up(chain) == 4          # U4, U3, U1's and U2's inputs


def test_segments_name_where_they_start_and_end(chain):
    u2 = _vrm(chain, "U2")
    reg, cu, fb1, cu_f, gnd = u2.segments
    assert reg.frm == "3.3 V set" and reg.to == "U1.2 − U1.3"
    assert (cu.frm, cu.to) == ("U1.2", "FB1.1")
    assert (fb1.frm, fb1.to) == ("FB1.1", "FB1.2")
    assert (cu_f.frm, cu_f.to) == ("FB1.2", "U2.1")
    assert (gnd.frm, gnd.to) == ("U2.3", "U1.3")


def test_power_chain_nets_reproduce_the_prototype(chain):
    split = {n.net: n for n in chain.groups["+3V3"].net_split}
    assert set(split) == {"+3V3", "+3V3_F"}
    a, f = split["+3V3"], split["+3V3_F"]
    assert (a.v_min_v, a.v_max_v) == pytest.approx((3.290777, 3.305691), abs=5e-7)
    assert (f.v_min_v, f.v_max_v) == pytest.approx((3.183774, 3.190677), abs=5e-7)
    assert a.copper_loss_w + a.via_loss_w == pytest.approx(33.9e-3, abs=5e-5)
    assert f.copper_loss_w + f.via_loss_w == pytest.approx(13.8e-3, abs=5e-5)
    assert (a.entry_kind, a.entry_name, a.entry_pads) == ("source", "U1", ["U1.2"])
    assert (f.entry_kind, f.entry_name, f.entry_pads) == ("link", "FB1", ["FB1.2"])
    assert a.drop_v * 1e3 == pytest.approx(14.914, abs=5e-4)       # V(entry) − min
    assert f.drop_v * 1e3 == pytest.approx(6.903, abs=5e-4)
    assert f.entry_v == pytest.approx(f.v_max_v, abs=1e-12)
    # Where the extremes are: F.Cu, on the strip, at the end of the net where the current leaves.
    assert a.at_min[2] == "F.Cu" and 60.0 <= a.at_min[0] <= 62.0
    [fb1] = chain.links
    assert fb1["across_mv"] == pytest.approx(100.100, abs=5e-4)
    assert fb1["power_w"] == pytest.approx(200.4e-3, abs=5e-5)     # the brief's 200.2 is a typo: I·across
    assert chain.groups["+3V3"].power_loss == pytest.approx(248e-3, abs=5e-4)
    gnd = chain.groups["GND"].net_split[0]
    assert gnd.net == "GND" and gnd.entry_kind == "reference" and gnd.entry_name == "J1"
    assert gnd.drop_v == pytest.approx(gnd.v_max_v - gnd.entry_v)  # a rise: max − V(exit)


def _assert_losses_add_up(tree):
    for key, res in tree.groups.items():
        links = sum(k["power_w"] for k in tree.links if k["group"] == key)
        nets = sum(n.copper_loss_w + n.via_loss_w for n in res.net_split)
        assert nets + links == pytest.approx(res.power_loss, rel=1e-9), key
        assert {n.net for n in res.net_split} == set(res.nets), key


def test_net_and_link_losses_add_up_to_each_group_loss(chain):
    _assert_losses_add_up(chain)


def _assert_across(tree):
    for k in tree.links:
        assert k["across_mv"] == pytest.approx((k["v_a_v"] - k["v_b_v"]) * 1e3, rel=1e-12)
        if k["resistance_ohm"] > 0:
            assert k["across_mv"] * 1e-3 == pytest.approx(k["current_a"] * k["resistance_ohm"], rel=1e-9)


def test_link_across_is_i_times_r(chain):
    _assert_across(chain)


def _assert_every_node_has_its_net(board, tree, tol_mm=0.01):
    """Each node's net is set, is one of its group's, and the node lies on that net's copper
    (within the mesher's 3 µm outline simplification)."""
    import shapely
    layer_id = {lay.name: lay.id for lay in board.layers()}
    for key, res in tree.groups.items():
        for lf in res.layers:
            assert lf.node_net is not None and len(lf.node_net) == len(lf.points), (key, lf.name)
            assert set(lf.node_net) <= set(res.nets), (key, lf.name)
            for net in set(lf.node_net):
                copper = board.copper(net, layer_id[lf.name])
                pts = lf.points[lf.node_net == net]
                far = float(shapely.distance(copper, shapely.points(pts)).max())
                assert far < tol_mm, (key, lf.name, net, far)
        found = {n for lf in res.layers for n in set(lf.node_net)}
        assert found == set(res.nets), key


def test_every_node_gets_its_net(chain):
    project = Project.load(CHAIN / "power_chain.coppernicus.yml")
    _assert_every_node_has_its_net(Board.open(project.board_path()), chain)


def test_an_island_with_no_pad_takes_its_net_from_the_copper_outline():
    """U9's island on +3V3 holds no terminal pad and no via (U9 is not in the project): the
    copper outline names its net."""
    board = Board(power_chain_board(island=True))
    tree = solve_project_tree(board, power_chain_project())
    lf = tree.groups["+3V3"].layer("F.Cu")
    island = lf.points[:, 0] > 109.0
    assert island.any() and set(lf.node_net[island]) == {"+3V3"}
    assert np.isnan(lf.voltage[island]).all()                      # floating: no voltage
    _assert_every_node_has_its_net(board, tree)
    _assert_segments_add_up(tree)


# ---------------------------------------------------------------------- a ground link

def _ground_link_board():
    """VCC: J1.1 → U1.1 on F.Cu. GND: J1.2 → R1.1 on F.Cu, through two vias and a B.Cu
    stretch; AGND: R1.2 → U1.2. R1 (a resistor, not a net tie) joins GND and AGND."""
    b = SyntheticBoard("ground_link", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.square_pad("J1", "2", "GND", 0.0, 5.0, 1.5)
    b.square_pad("R1", "1", "GND", 19.5, 5.0, 0.6)
    b.square_pad("R1", "2", "AGND", 20.5, 5.0, 0.6)
    b.square_pad("U1", "2", "AGND", 40.0, 5.0, 1.5)
    b.track("GND", F, 0.5, (0.0, 5.0), (8.0, 5.0))
    b.via("GND", 8.0, 5.0)
    b.track("GND", B, 0.5, (8.0, 5.0), (12.0, 5.0))
    b.via("GND", 12.0, 5.0)
    b.track("GND", F, 0.5, (12.0, 5.0), (19.5, 5.0))
    b.track("AGND", F, 0.5, (20.5, 5.0), (40.0, 5.0))
    return Board(b.snapshot())


def _ground_link_project(r_ohm=0.01):
    return Project(rails=[
        Rail("VCC", sources=[Source("J1", 3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="AGND")])],
        links=[Link("R1", "R1", ["1"], "GND", ["2"], "AGND", r_ohm)])


@pytest.fixture(scope="module")
def ground_link():
    board = _ground_link_board()
    return board, solve_project_tree(board, _ground_link_project())


def test_a_ground_link_splits_the_ground_segment(ground_link):
    _board, tree = ground_link
    assert set(tree.groups["GND"].nets) == {"GND", "AGND"}
    u1 = _load(tree, "U1")
    assert [(s.what, s.name) for s in u1.segments] == [
        ("regulation", "J1"), ("copper", "VCC"), ("ground copper", "AGND"), ("ground link", "R1"),
        ("ground copper", "GND")]
    assert [(s.frm, s.to) for s in u1.segments[2:]] == [("U1.2", "R1.2"), ("R1.2", "R1.1"), ("R1.1", "J1.2")]
    [r1] = tree.links
    # The return flows from AGND into GND: R1's side B is above its side A by I·R.
    assert u1.segments[3].v == pytest.approx(-r1["across_mv"] * 1e-3, rel=1e-12)
    assert u1.segments[3].v == pytest.approx(1.0 * 0.01, rel=1e-6)
    assert _assert_segments_add_up(tree) == 1


def test_a_ground_link_nets_and_losses(ground_link):
    board, tree = ground_link
    gnd = tree.groups["GND"]
    split = {n.net: n for n in gnd.net_split}
    assert split["GND"].entry_kind == "reference" and split["GND"].entry_pads == ["J1.2"]
    assert split["AGND"].entry_kind == "link" and split["AGND"].entry_pads == ["R1.2"]
    assert split["GND"].via_loss_w > 0 and split["AGND"].via_loss_w == 0.0
    assert split["AGND"].drop_v == pytest.approx(split["AGND"].v_max_v - split["AGND"].entry_v)
    _assert_losses_add_up(tree)
    _assert_across(tree)
    _assert_every_node_has_its_net(board, tree)


# ---------------------------------------------------------------------- parallel links

def test_parallel_links_follow_the_highest_current_path_and_say_so():
    project = power_chain_project()
    project.links.append(Link("FB2", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", 0.025))   # in parallel
    tree = solve_project_tree(Board(power_chain_board()), project)
    fb1, fb2 = (next(k for k in tree.links if k["name"] == n) for n in ("FB1", "FB2"))
    assert abs(fb2["current_a"]) > abs(fb1["current_a"])
    u2 = _vrm(tree, "U2")
    assert [s.name for s in u2.segments if s.what == "link"] == ["FB2"]
    note = tree.groups["+3V3"].split_note
    assert "FB1" in note and "FB2" in note and "highest-current path" in note
    doc = tree_summary(tree)
    group = next(g for g in doc["groups"] if g["group"] == "+3V3")
    assert group["split_note"] == note
    assert all("split_note" not in g for g in doc["groups"] if g["group"] != "+3V3")
    _assert_segments_add_up(tree)


# ---------------------------------------------------------------------- every kind of tree

def _strip_with_ground():
    b = SyntheticBoard("strip_gnd", two_layer_stackup())
    for ref, x in (("J1", 0.0), ("U1", 40.0)):
        b.square_pad(ref, "1", "VCC", x, 0.0, 1.5)
        b.square_pad(ref, "2", "GND", x, 5.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.track("GND", F, 0.5, (0.0, 5.0), (40.0, 5.0))
    return Board(b.snapshot())


def _tie_load_board():
    b = SyntheticBoard("tie_load", two_layer_stackup())
    lib = "NetTie:NetTie-2_SMD_Pad0.5mm"
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U3", "1", "VCC", 15.0, 0.0, 1.0)
    b.square_pad("NT1", "1", "VCC", 19.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("NT1", "2", "VCC_B", 20.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("U2", "1", "VCC_B", 40.0, 0.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (19.5, 0.0))
    b.track("VCC_B", F, 0.5, (20.5, 0.0), (40.0, 0.0))
    return Board(b.snapshot())


def _negative():
    return _strip_with_ground(), Project(rails=[
        Rail("VCC", sources=[Source("J1", -3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="GND")])])


def _zero_volt():
    return Board(strip_board()), Project(rails=[Rail(Strip.NET, sources=[Source("J1", 0.0)],
                                                     loads=[Load("U1", 1.0)])])


def _tie_load():
    return _tie_load_board(), Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)],
                                                  loads=[Load("U2", 1.0), Load("U3", 0.5)])])


def _pack():
    return Board(pack_board()), pack_project()


def _island():
    return Board(power_chain_board(island=True)), power_chain_project()


@pytest.mark.parametrize("case", [_island, _tie_load, lambda: (_ground_link_board(), _ground_link_project())])
def test_node_nets_follow_kipi_s_mesh_order(case):
    """KiPI concatenates a group's per-net meshes in the order of its sorted nets: the nets the
    pads, holes and outlines give come out as one block per net, in that order."""
    from kipi.dc.pi.tree import solve_tree

    from coppernicus.engine.pisetup import to_pisetup
    from coppernicus.engine.tree import node_nets
    board, project = case()
    kt = solve_tree(board.snapshot, to_pisetup(board, project))
    multi = [g for g in kt.groups.values() if len(g.nets) > 1]
    assert multi
    for g in multi:
        codes = node_nets(board, g.result.mesh, sorted(g.nets))
        assert len(codes) == len(g.result.mesh.points)
        assert np.all(np.diff(codes) >= 0) and set(codes) == set(range(len(g.nets))), g.name


@pytest.mark.parametrize("case", [_negative, _zero_volt, _tie_load, _pack])
def test_segments_add_up_on_every_kind_of_tree(case):
    board, project = case()
    tree = solve_project_tree(board, project)
    assert _assert_segments_add_up(tree) >= 1
    _assert_losses_add_up(tree)
    _assert_across(tree)
    _assert_every_node_has_its_net(board, tree)


def test_a_negative_rail_drop_and_segments_point_toward_0_v():
    board, project = _negative()
    tree = solve_project_tree(board, project)
    u1 = _load(tree, "U1")
    assert u1.drop_v > 0 and all(s.v >= -1e-12 for s in u1.segments if s.what != "regulation")
    [vcc] = tree.groups["VCC"].net_split
    assert vcc.drop_v > 0 and vcc.drop_v == pytest.approx(vcc.v_max_v - vcc.entry_v)   # toward 0 V


def test_a_load_past_a_net_tie_crosses_it_as_a_link():
    board, project = _tie_load()
    tree = solve_project_tree(board, project)
    u2 = _load(tree, "U2")
    assert [(s.what, s.name) for s in u2.segments] == [
        ("regulation", "J1"), ("copper", "VCC"), ("link", "net tie NT1"), ("copper", "VCC_B")]
    assert u2.segments[2].v == 0.0                                  # a short: no voltage across
    [tie] = tree.links
    assert tie["across_mv"] == 0.0 and tie["v_a_v"] == tie["v_b_v"]


# ---------------------------------------------------------------------- the document

def test_the_document_only_adds_keys(chain_doc):
    def added(rows, old):
        return set().union(*(set(r) for r in rows)) - old
    assert added(chain_doc["groups"], OLD_GROUP_KEYS) <= NEW_KEYS["groups"]
    assert added(chain_doc["groups"], OLD_GROUP_KEYS) == {"net_split"}
    assert added(chain_doc["links"], OLD_LINK_KEYS) == NEW_KEYS["links"]
    assert added(chain_doc["loads"], OLD_LOAD_KEYS) == NEW_KEYS["loads"]
    assert added(chain_doc["vrms"], OLD_VRM_KEYS) == NEW_KEYS["vrms"]
    json.dumps(chain_doc, allow_nan=False)


def strip_split(doc):
    """The tree document without the split's keys."""
    doc = json.loads(json.dumps(doc))
    for part, keys in NEW_KEYS.items():
        for row in doc[part]:
            for k in keys:
                row.pop(k, None)
    return doc


def test_the_split_changes_no_other_value(chain, chain_doc):
    """The document of a tree with its split taken out, with the split's keys removed, is the
    document with them removed: the split changes no existing value."""
    import copy
    bare = copy.deepcopy(chain)
    for res in bare.groups.values():
        res.net_split, res.split_note = [], ""
        for lf in res.layers:
            lf.node_net = None
    for row in bare.loads + bare.vrms:
        row.segments = []
    bare.links = [{k: v for k, v in row.items() if k not in NEW_KEYS["links"]} for row in bare.links]
    assert strip_split(tree_summary(bare)) == strip_split(chain_doc)


def test_the_document_holds_the_split(chain_doc):
    group = next(g for g in chain_doc["groups"] if g["group"] == "+3V3")
    rows = {r["net"]: r for r in group["net_split"]}
    a = rows["+3V3"]
    assert a["drop_mv"] == pytest.approx(14.914, abs=5e-4) and "rise_mv" not in a
    assert a["entry"] == {"kind": "source", "name": "U1", "pads": ["U1.2"],
                          "v_v": pytest.approx(3.305691, abs=5e-7)}
    assert set(a["at_min"]) == {"x_mm", "y_mm", "layer"}
    assert a["copper_loss_w"] == pytest.approx(33.9e-3, abs=5e-5) and a["via_loss_w"] == 0.0
    gnd = next(g for g in chain_doc["groups"] if g["group"] == "GND")["net_split"][0]
    assert "rise_mv" in gnd and "drop_mv" not in gnd
    u2 = next(v for v in chain_doc["vrms"] if v["name"] == "U2")
    assert [s["mv"] for s in u2["segments"]] == pytest.approx([0.0, 14.914, 100.100, 6.903, 25.145], abs=5e-4)
    assert set(u2["segments"][0]) == {"what", "name", "from", "to", "mv"}
    assert u2["segments_sum_mv"] == pytest.approx(u2["input_drop_mv"], abs=1e-9)
    j1 = next(v for v in chain_doc["vrms"] if v["name"] == "J1")
    assert j1["segments"] == [] and j1["segments_sum_mv"] is None
    for ld in chain_doc["loads"]:
        assert ld["segments_sum_mv"] == pytest.approx(ld["drop_mv"], abs=1e-9)


# ---------------------------------------------------------------------- CLI and HTML

@pytest.fixture()
def chain_files(tmp_path):
    save_snapshot(power_chain_board(), tmp_path / "power_chain.kipi.json.gz")
    return power_chain_project().save(tmp_path / "chain.coppernicus.yml")


def test_cli_split_prints_the_breakdown(chain_files, tmp_path, capsys):
    from coppernicus.cli import EXIT_OK, main
    assert main(["solve", str(chain_files), "--tree", "--out", str(tmp_path / "a")]) == EXIT_OK
    plain = capsys.readouterr().out
    assert main(["solve", str(chain_files), "--tree", "--split", "--out", str(tmp_path / "b")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Split" not in plain and "Split" in out
    lines = out.splitlines()
    head = next(i for i, ln in enumerate(lines) if ln.startswith("Split"))
    u2 = next(ln for ln in lines[head:] if ln.lstrip().startswith("U2 (input)"))
    assert "147.06" in u2 and "+3V3_F" in u2
    assert any("link" in ln and "FB1" in ln and "100.100" in ln for ln in lines[head:])
    assert any("regulation" in ln and "U2" in ln and "-14.531" in ln for ln in lines[head:])
    assert any(ln.lstrip().startswith("+3V3_F") and "FB1.2" in ln for ln in lines[head:])
    # The rest of the output is the plain one (the first line names the solve time).
    tail = [ln for ln in out.splitlines()[1:] if not ln.startswith(("Summary:",))]
    assert all(ln in tail for ln in plain.splitlines()[1:] if not ln.startswith("Summary:"))


def test_html_report_has_the_nets_table_and_the_split(chain, chain_doc, tmp_path):
    from coppernicus.report import write_tree_report
    path = write_tree_report(chain, chain_doc, tmp_path / "tree.html")
    text = path.read_text(encoding="utf-8")
    meta = json.loads(text.split('<script type="application/json" id="meta">', 1)[1].split("</script>", 1)[0])
    rail = next(r for r in meta["rails"] if r["net"] == "+3V3")
    assert [n["net"] for n in rail["net_split"]] == ["+3V3", "+3V3_F"]
    row = next(r for r in rail["loads"] if r["name"] == "U2 (input)")
    assert [s["mv"] for s in row["segments"]] == pytest.approx([0.0, 14.914, 100.100, 6.903, 25.145],
                                                               abs=5e-4)
    assert row["segments_sum_mv"] == pytest.approx(row["drop_mv"], abs=1e-9)
    assert "netsplit" in text and "Split" in text                  # the page draws them (report.js)
