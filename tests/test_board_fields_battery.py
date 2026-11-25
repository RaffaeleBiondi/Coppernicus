"""KiCad footprint fields, third step (additive): a source held at the pads of several parts
(PI_SOURCE_REF with a list of refs: one equipotential terminal), and a source's ground pins on
another part (PI_GND_REF). The case: a 2S2P battery of single-contact cell holders, BT1 and BT5
in parallel on +V_BAT, the negative pole BT4 on -V_BAT. Synthetic parts only."""

from pathlib import Path

import pytest
from kicad_fixture import write_board

from coppernicus.board_fields import FieldError, field_rails, merge, propose
from coppernicus.project import Link, Load, Project, Rail, Source

PADS = {"BT1": {"1": "+V_BAT"}, "BT5": {"1": "+V_BAT"}, "BT4": {"1": "-V_BAT"},
        "U1": {"1": "+V_BAT", "2": "-V_BAT"}}
ANCHOR = {"PI_RAIL": "+V_BAT", "PI_KIND": "source", "PI_SOURCE": "6.4V", "PI_SOURCE_REF": "BT1,BT5",
          "PI_GND": "-V_BAT", "PI_GND_REF": "BT4"}
LOAD = {"PI_RAIL": "+V_BAT", "PI_LOAD": "2A", "PI_GND": "-V_BAT"}
BATTERY = Source("BT1", 6.4, extra_pins=["BT5.*"], ground_ref="BT4", ground_net="-V_BAT")


def test_a_source_at_the_pads_of_several_parts_and_its_ground_on_another():
    merged = merge(Project(), {"BT1": ANCHOR, "U1": LOAD}, PADS)
    [rail] = merged.project.rails
    assert rail.sources == [BATTERY] and rail.loads == [Load("U1", 2.0, ground_net="-V_BAT")]
    note = " ".join(merged.origins["+V_BAT"].notes)
    assert "BT1, BT5" in note and "PI_SOURCE_REF" in note and "PI_GND_REF" in note
    # The anchor need not be one of the parts: the fields name it, the first listed holds the rail.
    on_u9 = merge(Project(), {"U9": {**ANCHOR, "PI_SOURCE_REF": "BT1,BT5"}, "U1": LOAD},
                  {**PADS, "U9": {"1": "NC"}})
    assert on_u9.project.rails[0].sources == [Source("BT1", 6.4, name="U9", extra_pins=["BT5.*"],
                                                     ground_ref="BT4", ground_net="-V_BAT")]


@pytest.mark.parametrize("pins, anchor, extra", [
    ("1", ["1"], ["BT5.1"]),                       # a plain pad: on every listed part
    ("BT1.1,BT5.1", ["1"], ["BT5.1"]),             # REF.PAD: each part its own
    ("BT1.1,BT5.*", ["1"], ["BT5.*"]),
])
def test_pi_pins_with_several_parts(pins, anchor, extra):
    [src] = field_rails({"BT1": {**ANCHOR, "PI_PINS": pins}}, PADS)["+V_BAT"].sources
    assert (src.term.pins, src.term.extra_pins) == (anchor, extra)


def test_what_cannot_be_used_says_why():
    with pytest.raises(FieldError, match=r"BT5 has no pad on \+V_BAT"):
        merge(Project(), {"BT1": ANCHOR, "U1": LOAD}, {**PADS, "BT5": {"1": "-V_BAT"}})
    with pytest.raises(FieldError, match=r"PI_GND_REF BT4 has no pad on -V_BAT"):
        merge(Project(), {"BT1": ANCHOR, "U1": LOAD}, {**PADS, "BT4": {"1": "+V_BAT"}})
    with pytest.raises(FieldError, match="PI_GND_REF without PI_GND"):
        field_rails({"BT1": {**ANCHOR, "PI_GND": ""}})
    with pytest.raises(FieldError, match="one part"):
        field_rails({"BT1": {**ANCHOR, "PI_GND_REF": "BT4,BT8"}})
    with pytest.raises(FieldError, match="BT5 gets no pad"):
        field_rails({"BT1": {**ANCHOR, "PI_PINS": "BT1.1"}})
    with pytest.raises(FieldError, match="PI_GND_REF without PI_RAIL"):
        field_rails({"BT1": {"PI_GND_REF": "BT4"}})


def test_the_extra_pins_round_trip_through_the_project_file(tmp_path):
    p = Project(rails=[Rail("+V_BAT", sources=[BATTERY], loads=[Load("U1", 2.0)])])
    p.save(tmp_path / "b.coppernicus.yml")
    text = (tmp_path / "b.coppernicus.yml").read_text(encoding="utf-8")
    assert "extra_pins:" in text and "BT5.*" in text
    assert Project.load(tmp_path / "b.coppernicus.yml").rails == p.rails
    bad = {"board": "b.kicad_pcb", "rails": [{"net": "V", "sources": [{"ref": "BT1", "voltage": 6.4,
                                                                         "extra_pins": ["BT5"]}]}]}
    with pytest.raises(Exception, match="REF.PAD"):
        Project.from_dict(bad, None)


def test_proposals_write_the_battery_back_as_fields():
    project = Project(rails=[Rail("+V_BAT", sources=[BATTERY], loads=[Load("U1", 2.0, ground_net="-V_BAT")])])
    out = propose(project, PADS)
    parts = {p["ref"]: p for p in out["parts"]}
    assert parts["BT1"]["schematic_instance"] == {"PI_RAIL": "+V_BAT", "PI_SOURCE": "6.4V",
                                                  "PI_SOURCE_REF": "BT1,BT5", "PI_GND": "-V_BAT",
                                                  "PI_GND_REF": "BT4"}
    assert "BT5" not in parts and "BT4" not in parts and out["rails"] == ["+V_BAT"]
    back = merge(Project(), {p["ref"]: {**p["library_symbol"], **p["schematic_instance"]}
                             for p in out["parts"]}, PADS)
    assert back.project.rails[0].sources == [BATTERY]



def battery_board(path):
    """Two single-contact holders BT1, BT5 in parallel on +V_BAT; the fuse F1 to V_F; the
    MOSFET Q1 (pads 1-3 on V_F, gate 4, pad 5 on V_SYS) to V_SYS; the load U1, its ground
    GND_L back through the shunt R1 to GND_S and the net tie NT1 to -V_BAT, the pole BT4."""
    parts = [("BT1", [("1", 0.0, 0.0, "+V_BAT")], ANCHOR),
             ("BT5", [("1", 0.0, 10.0, "+V_BAT")], {}),
             ("BT4", [("1", 0.0, 30.0, "-V_BAT")], {}),
             ("F1", [("1", 10.0, 5.0, "+V_BAT"), ("2", 14.0, 5.0, "V_F")], {"PI_LINK": "10mOhm"}),
             ("Q1", [("1", 20.0, 3.0, "V_F"), ("2", 20.0, 5.0, "V_F"), ("3", 20.0, 7.0, "V_F"),
                     ("4", 22.0, 9.0, "GATE"), ("5", 26.0, 5.0, "V_SYS")],
              {"PI_LINK": "20mOhm", "PI_LINK_PINS": "1,2,3;5"}),
             ("U1", [("1", 40.0, 5.0, "V_SYS"), ("2", 40.0, 25.0, "GND_L")],
              {"PI_RAIL": "V_SYS", "PI_LOAD": "2A", "PI_GND": "GND_L", "PI_GND_PINS": "2"}),
             ("R1", [("1", 30.0, 25.0, "GND_L"), ("2", 26.0, 25.0, "GND_S")], {"PI_LINK": "5mOhm"}),
             ("NT1", [("1", 14.0, 25.0, "GND_S"), ("2", 12.0, 25.0, "-V_BAT")], {"Value": "NetTie_2"})]
    tracks = [("+V_BAT", (0.0, 0.0), (0.0, 10.0), 1.0), ("+V_BAT", (0.0, 5.0), (10.0, 5.0), 1.0),
              ("V_F", (14.0, 5.0), (20.0, 5.0), 1.0), ("V_F", (20.0, 3.0), (20.0, 7.0), 1.0),
              ("V_SYS", (26.0, 5.0), (40.0, 5.0), 1.0), ("GND_L", (40.0, 25.0), (30.0, 25.0), 1.0),
              ("GND_S", (26.0, 25.0), (14.0, 25.0), 1.0), ("-V_BAT", (12.0, 25.0), (0.0, 25.0), 1.0),
              ("-V_BAT", (0.0, 25.0), (0.0, 30.0), 1.0)]
    return write_board(path, parts, tracks)


HAND = dict(rails=[Rail("+V_BAT", sources=[BATTERY]),
                   Rail("V_SYS", loads=[Load("U1", 2.0, ground_pins=["2"], ground_net="GND_L")])],
            links=[Link("F1", "F1", ["1"], "+V_BAT", ["2"], "V_F", 0.01),
                   Link("Q1", "Q1", ["1", "2", "3"], "V_F", ["5"], "V_SYS", 0.02),
                   Link("R1", "R1", ["1"], "GND_L", ["2"], "GND_S", 0.005)])


def test_the_battery_from_fields_equals_the_hand_written_project(tmp_path):
    """End to end on a synthetic board: every pad of the battery's terminal sits at its
    voltage, rail by rail and in the power tree, and the fields give exactly what the
    equivalent project file gives."""
    from coppernicus.engine import Board
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.service import Session
    battery_board(tmp_path / "battery.kicad_pcb")
    fields = tmp_path / "fields.coppernicus.yml"
    Project(board="battery.kicad_pcb").save(fields)
    hand = tmp_path / "hand.coppernicus.yml"
    Project(board="battery.kicad_pcb", use_board_fields=False, **HAND).save(hand)
    session = Session(out_dir=tmp_path / "out")
    a, b = session.solve_project(fields), session.solve_project(hand)
    ra = next(r for r in a["rails"] if r["net"] == "+V_BAT")
    rb = next(r for r in b["rails"] if r["net"] == "+V_BAT")
    assert ra["loads"] == rb["loads"] and ra["max_drop_mv"] == rb["max_drop_mv"]
    res = session.rail_result(ra["result_id"])
    [terminal] = res.sources
    assert len(terminal.pad_ids) == 2 and terminal.v_min == terminal.v_max == 6.4    # BT1.1, BT5.1
    ta, tb = session.solve_tree(fields), session.solve_tree(hand)
    for key in ("vrms", "loads", "links", "verdict"):
        assert ta[key] == tb[key], key
    [u1] = ta["loads"]
    assert u1["v_ground_v"] > 0 and u1["drop_mv"] > 0             # the ground return is solved
    tree = solve_project_tree(Board.open(tmp_path / "battery.kicad_pcb"), Project.load(hand))
    group = next(g for g in tree.groups.values() if "+V_BAT" in g.nets)
    held = [t for t in group.sources]
    assert sum(len(t.pad_ids) for t in held) == 2
    assert all(t.v_min == t.v_max == 6.4 for t in held)


def test_nothing_changes_without_extra_pins():
    """With no extra_pins, a source is mapped exactly as before: its own pads only, no mesh
    sense, on the demo and the power chain."""
    from coppernicus.engine import Board, solve_rail
    from coppernicus.engine.pisetup import to_pisetup
    from coppernicus.engine.synthetic import demo_board, power_chain_board, power_chain_project
    from coppernicus.project import Project as P
    root = Path(__file__).resolve().parents[1]
    demo = P.load(root / "examples" / "demo" / "demo.coppernicus.yml")
    for board, project in ((Board(demo_board()), demo), (Board(power_chain_board()), power_chain_project())):
        setup = to_pisetup(board, project)
        assert all(not v.mesh_sense and not v.sense_points for v in setup.vrms)
        rail = next(r for r in project.rails if r.sources and r.loads)
        res = solve_rail(board, rail, project.settings, project=project)
        s = rail.sources[0]
        assert res.sources[0].pad_ids == [p.id for p in board.pads([rail.net], s.ref, s.pins)]



def test_write_project_takes_extra_pins(tmp_path):
    from coppernicus.engine.synthetic import save_snapshot, strip_board
    from coppernicus.service import Session
    board = tmp_path / "s.kipi.json.gz"
    save_snapshot(strip_board(), board)
    path = tmp_path / "p.coppernicus.yml"
    Session(out_dir=tmp_path / "o").write_project(path, board_path=board, rails=[{
        "net": "VCC", "sources": [{"ref": "J1", "voltage_v": 1.0, "extra_pins": ["U9.*"],
                                   "ground": {"ref": "U8", "net": "GND", "pins": ["1"]}}]}])
    [src] = Project.load(path).rails[0].sources
    assert (src.extra_pins, src.ground_ref) == (["U9.*"], "U8")
