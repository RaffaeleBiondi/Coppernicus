"""KiCad footprint fields, second schema step (additive): library-level part data that needs
no PI_RAIL (PI_KIND, PI_EFF, PI_IQ, PI_VOUT, PI_IMAX, PI_FSW), external-inductor bucks
(PI_SOURCE_REF) and links on parts with more than two nets (PI_LINK_PINS). Synthetic parts
and boards only."""

import pytest
from kicad_fixture import write_board

from coppernicus.board_fields import FieldError, field_links, field_rails, merge, propose
from coppernicus.engine.kicad_fields import read_board_fields
from coppernicus.project import Link, Load, Project, Rail, Source

VIN = {"J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V"}}
BUCK_PADS = {"U1": {"1": "VIN", "2": "SW", "3": "GND"}, "L1": {"1": "SW", "2": "+3V3"},
             "U4": {"1": "+3V3", "2": "GND"}, "J1": {"1": "VIN", "2": "GND"}}


def rails_of(merged):
    return {r.net: r for r in merged.project.rails}


# ------------------------------------------------------------------ library-level fields

def test_a_part_with_only_library_fields_is_part_data_not_an_error():
    """A symbol from an annotated library carries PI_KIND, PI_EFF, PI_IQ, PI_VOUT, PI_IMAX
    and PI_FSW before it is placed on any rail: that is not an error, it makes no rail."""
    library = {"U9": {"PI_KIND": "smps", "PI_EFF": "90%", "PI_IQ": "2mA", "PI_VOUT": "3.3V",
                      "PI_IMAX": "2A", "PI_FSW": "1.2MHz"}}
    assert field_rails(library) == {}
    merged = merge(Project(), {**VIN, **library, "U2": {"PI_RAIL": "VIN", "PI_LOAD": "1A"}})
    assert [r.net for r in merged.project.rails] == ["VIN"]
    with pytest.raises(FieldError, match="PI_LOAD without PI_RAIL"):     # instance data still needs it
        field_rails({"U9": {"PI_LOAD": "1A"}})
    with pytest.raises(FieldError, match="PI_SOURCE_REF without PI_RAIL"):
        field_rails({"U9": {"PI_SOURCE_REF": "L1"}})


def test_pi_vout_is_the_source_voltage_when_the_instance_gives_none():
    fields = {**VIN, "U1": {"PI_RAIL": "+3V3", "PI_VOUT": "3.3V", "PI_KIND": "ldo", "PI_INPUT": "VIN"},
              "U4": {"PI_RAIL": "+3V3", "PI_LOAD": "0.5A"}}
    merged = merge(Project(), fields)
    [u1] = rails_of(merged)["+3V3"].sources
    assert (u1.ref, u1.voltage, u1.kind, u1.input_net) == ("U1", 3.3, "ldo", "VIN")
    note = " ".join(merged.origins["+3V3"].notes)
    assert 'from KiCad field PI_VOUT "3.3V" on U1' in note
    assert merged.warnings == []
    same = merge(Project(), {**fields, "U1": {**fields["U1"], "PI_SOURCE": "3.3V"}})
    assert same.warnings == []
    differ = merge(Project(), {**fields, "U1": {**fields["U1"], "PI_SOURCE": "3.0V"}})
    assert rails_of(differ)["+3V3"].sources[0].voltage == 3.0                # PI_SOURCE wins
    assert any("PI_SOURCE" in w and "PI_VOUT" in w and "U1" in w for w in differ.warnings)


def test_one_library_value_applies_to_every_source_rail_and_per_rail_values_still_work():
    dual = {"U1": {"PI_RAIL": "+3V3;+1V8", "PI_SOURCE": "3.3V;1.8V", "PI_KIND": "ldo", "PI_IQ": "1mA",
                   "PI_INPUT": "VIN;VIN"}}
    rails = field_rails(dual)
    for net in ("+3V3", "+1V8"):
        [t] = rails[net].sources
        assert (t.term.kind, t.term.quiescent_a) == ("ldo", 0.001)
    per_rail = {"U1": {**dual["U1"], "PI_KIND": "smps;ldo", "PI_EFF": "90%;"}}
    rails = field_rails(per_rail)
    assert rails["+3V3"].sources[0].term.efficiency == 0.9 and rails["+1V8"].sources[0].term.kind == "ldo"
    # A part that is a load on one rail and a source on another: its library data describes
    # the source; on the load's rail it is not used.
    mixed = {"U5": {"PI_RAIL": "VIO;+1V2", "PI_LOAD": "50mA;", "PI_VOUT": "1.2V", "PI_KIND": "ldo",
                    "PI_INPUT": ";VIN", "PI_IQ": "1mA"}}
    rails = field_rails(mixed)
    assert rails["VIO"].loads[0].term == Load("U5", 0.05)
    assert (rails["+1V2"].sources[0].term.voltage, rails["+1V2"].sources[0].term.kind) == (1.2, "ldo")
    with pytest.raises(FieldError, match="for a source"):           # a per-rail value aimed at the load
        field_rails({"U5": {**mixed["U5"], "PI_KIND": "ldo;ldo"}})
    # A part that is a source nowhere but carries a regulator's library data is a mistake
    # (a regulator placed as a load), not part data to ignore.
    with pytest.raises(FieldError, match="PI_VOUT is for a source"):
        field_rails({"U6": {"PI_RAIL": "+3V3", "PI_LOAD": "1A", "PI_VOUT": "3.3V"}})
    with pytest.raises(FieldError, match=r"1 value\(s\) for 2 rail"):   # instance fields: one per rail
        field_rails({"U1": {**dual["U1"], "PI_INPUT": "VIN"}})
    with pytest.raises(FieldError, match=r"3 value\(s\) for 2 rail"):   # library: one, or one per rail
        field_rails({"U1": {**dual["U1"], "PI_IQ": "1mA;1mA;1mA"}})


def test_pi_imax_and_pi_fsw_are_kept_on_the_source():
    fields = {**VIN, "U1": {"PI_RAIL": "+3V3", "PI_VOUT": "3.3V", "PI_KIND": "smps", "PI_EFF": "90%",
                            "PI_INPUT": "VIN", "PI_IMAX": "2A", "PI_FSW": "1.2MHz"},
              "U4": {"PI_RAIL": "+3V3", "PI_LOAD": "500mA"}}
    merged = merge(Project(), fields)
    [u1] = rails_of(merged)["+3V3"].sources
    assert (u1.current_limit_a, u1.fsw_hz) == (2.0, 1.2e6)
    note = " ".join(merged.origins["+3V3"].notes)
    assert 'I_max 2 A (from KiCad field PI_IMAX "2A" on U1)' in note, note   # judged in the tree
    assert "not judged" not in note and "f_sw 1.2e+06 Hz (used by a noise aggressor on it" in note, note
    for text, hz in (("500kHz", 5e5), ("2.2MHz", 2.2e6), ("800000Hz", 8e5)):
        rails = field_rails({"U1": {**fields["U1"], "PI_FSW": text}})
        assert rails["+3V3"].sources[0].term.fsw_hz == pytest.approx(hz, rel=1e-12)
    assert field_rails({"U1": {**fields["U1"], "PI_IMAX": "750mA"}})["+3V3"].sources[0].term.current_limit_a \
        == 0.75
    for name, bad in (("PI_IMAX", "2"), ("PI_IMAX", "0A"), ("PI_FSW", "1.2"), ("PI_FSW", "1.2GHz")):
        with pytest.raises(FieldError, match=name):
            field_rails({"U1": {**fields["U1"], name: bad}})
    with pytest.raises(FieldError, match="PI_FSW is for a switcher"):
        field_rails({"U1": {**fields["U1"], "PI_KIND": "ldo", "PI_EFF": ""}})


def test_the_new_source_values_round_trip_through_the_project_file(tmp_path):
    p = Project(rails=[Rail("+3V3", sources=[Source("U1", 3.3, ["2"], kind="smps", efficiency=0.9,
                                                    input_pins=["1"], input_net="VIN",
                                                    current_limit_a=2.0, fsw_hz=1.2e6)])])
    p.save(tmp_path / "p.coppernicus.yml")
    text = (tmp_path / "p.coppernicus.yml").read_text(encoding="utf-8")
    assert "current_limit_a: 2.0" in text and "fsw_hz: 1200000.0" in text
    assert Project.load(tmp_path / "p.coppernicus.yml").rails == p.rails
    plain = Project(rails=[Rail("+3V3", sources=[Source("U1", 3.3)])])
    plain.save(tmp_path / "q.coppernicus.yml")
    assert "current_limit_a" not in (tmp_path / "q.coppernicus.yml").read_text(encoding="utf-8")
    for key in ("current_limit_a", "fsw_hz"):
        bad = {"board": "b.kicad_pcb", "rails": [{"net": "+3V3", "sources": [{"ref": "U1", "voltage": 3.3,
                                                                              key: -1}]}]}
        with pytest.raises(Exception, match=key):
            Project.from_dict(bad, None)


# ------------------------------------------------------------------ external-inductor bucks

BUCK = {"PI_RAIL": "+3V3", "PI_VOUT": "3.3V", "PI_SOURCE_REF": "L1", "PI_PINS": "2", "PI_KIND": "smps",
        "PI_EFF": "90%", "PI_INPUT": "VIN", "PI_INPUT_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "3"}


def test_an_external_inductor_buck_holds_its_rail_at_the_inductors_pad():
    fields = {**VIN, "U1": BUCK, "U4": {"PI_RAIL": "+3V3", "PI_LOAD": "500mA"}}
    merged = merge(Project(), fields, BUCK_PADS)
    [src] = rails_of(merged)["+3V3"].sources
    assert src == Source("L1", 3.3, ["2"], name="U1", kind="smps", efficiency=0.9, input_ref="U1",
                         input_pins=["1"], input_net="VIN", ground_ref="U1", ground_pins=["3"],
                         ground_net="GND")
    assert any("L1" in n and "PI_SOURCE_REF" in n for n in merged.origins["+3V3"].notes)
    # VIN feeds U1's input: complete with no load of its own.
    assert rails_of(merged)["VIN"].sources[0].ref == "J1"
    wrong = {**BUCK_PADS, "L1": {"1": "SW", "2": "SW"}}
    with pytest.raises(FieldError, match=r"U1: PI_SOURCE_REF L1 has no pad on \+3V3"):
        merge(Project(), fields, wrong)
    with pytest.raises(FieldError, match="no part L9"):
        merge(Project(), {**fields, "U1": {**BUCK, "PI_SOURCE_REF": "L9"}}, BUCK_PADS)
    with pytest.raises(FieldError, match="PI_SOURCE_REF is for a source"):
        field_rails({"U4": {"PI_RAIL": "+3V3", "PI_LOAD": "1A", "PI_SOURCE_REF": "L1"}})


def buck_board(path):
    """J1 (12 V) → U1 (buck IC: VIN, SW, GND) → L1 (SW → +3V3) → U4 (load), synthetic."""
    parts = [("J1", [("1", 0.0, 0.0, "VIN"), ("2", 0.0, 6.0, "GND")],
              {"PI_RAIL": "VIN", "PI_SOURCE": "12V", "PI_GND": "GND", "PI_GND_PINS": "2"}),
             ("U1", [("1", 10.0, 0.0, "VIN"), ("2", 12.0, 0.0, "SW"), ("3", 10.0, 6.0, "GND")], BUCK),
             ("L1", [("1", 16.0, 0.0, "SW"), ("2", 20.0, 0.0, "+3V3")], {}),
             ("U4", [("1", 40.0, 0.0, "+3V3"), ("2", 40.0, 6.0, "GND")],
              {"PI_RAIL": "+3V3", "PI_LOAD": "500mA", "PI_GND": "GND", "PI_GND_PINS": "2"})]
    tracks = [("VIN", (0.0, 0.0), (10.0, 0.0), 0.5), ("SW", (12.0, 0.0), (16.0, 0.0), 0.5),
              ("+3V3", (20.0, 0.0), (40.0, 0.0), 0.5), ("GND", (0.0, 6.0), (40.0, 6.0), 1.0)]
    return write_board(path, parts, tracks)


def test_the_board_reader_reads_the_pads_of_the_part_pi_source_ref_names(tmp_path):
    found = read_board_fields(buck_board(tmp_path / "buck.kicad_pcb"))
    assert "L1" not in found.fields                        # no fields of its own
    assert found.pad_nets["L1"] == {"1": "SW", "2": "+3V3"}


def test_an_external_inductor_buck_solves_from_a_kicad_board(tmp_path):
    from coppernicus.service import Session
    board = buck_board(tmp_path / "buck.kicad_pcb")
    project = tmp_path / "buck.coppernicus.yml"
    Project(board="buck.kicad_pcb").save(project)
    doc = Session(out_dir=tmp_path / "out").solve_project(project, maps_dir=None)
    rails = {r["net"]: r for r in doc["rails"]}
    assert rails["+3V3"]["sources"][0]["ref"] == "L1"
    assert rails["+3V3"]["loads"][0]["current_a"] == 0.5
    est = [ld for ld in rails["VIN"]["loads"] if ld["estimated"]]
    assert est and est[0]["name"].startswith("U1")            # U1's input, drawn from VIN
    tree = Session(out_dir=tmp_path / "out2").solve_tree(project)
    [u1] = [v for v in tree["vrms"] if v["name"] == "U1"]
    assert u1["i_out_a"] == pytest.approx(0.5, rel=1e-6)
    assert board.exists()


# ------------------------------------------------------------------ links on multi-net parts

SWITCH_PADS = {"Q1": {"1": "VIN", "2": "VIN", "3": "EN", "4": "GND", "5": "VOUT"}}


def test_a_link_on_a_part_with_more_than_two_nets_names_its_two_sides():
    links = field_links({"Q1": {"PI_LINK": "20mOhm", "PI_LINK_PINS": "1,2;5"}}, SWITCH_PADS)
    assert [k for k, _ in links] == [Link("Q1", "Q1", ["1", "2"], "VIN", ["5"], "VOUT", 0.02)]
    with pytest.raises(FieldError, match="PI_LINK_PINS"):           # more than two nets, no sides
        field_links({"Q1": {"PI_LINK": "20mOhm"}}, SWITCH_PADS)
    for bad, why in (("1,3;5", "two nets"), ("1;2", "same net"), ("1", "side A ; side B"),
                     ("9;5", "no pad")):
        with pytest.raises(FieldError, match=why):
            field_links({"Q1": {"PI_LINK": "20mOhm", "PI_LINK_PINS": bad}}, SWITCH_PADS)
    with pytest.raises(FieldError, match="PI_LINK_PINS without PI_LINK"):
        field_rails({"Q1": {"PI_LINK_PINS": "1;5"}})
    # A two-net part needs no sides; with them, they choose its pads.
    two = {"FB1": {"1": "+3V3", "2": "+3V3_F"}}
    assert field_links({"FB1": {"PI_LINK": "50mOhm"}}, two)[0][0].pins_b == ["2"]
    assert field_links({"FB1": {"PI_LINK": "50mOhm", "PI_LINK_PINS": "1;2"}}, two)[0][0].pins_a == ["1"]


# ------------------------------------------------------------------ proposals

def test_proposals_carry_the_new_fields_and_read_back():
    buck = Source("L1", 3.3, ["2"], name="U1", kind="smps", efficiency=0.9, input_ref="U1",
                  input_pins=["1"], input_net="VIN", ground_ref="U1", ground_pins=["3"], ground_net="GND",
                  current_limit_a=2.0, fsw_hz=1.2e6)
    project = Project(rails=[Rail("VIN", sources=[Source("J1", 12.0, ["1"])]),
                             Rail("+3V3", sources=[buck], loads=[Load("U4", 0.5, ["1"])])],
                      links=[Link("Q1", "Q1", ["1", "2"], "VIN", ["5"], "VOUT", 0.02)])
    pads = {**BUCK_PADS, **SWITCH_PADS}
    out = propose(project, pads)
    parts = {p["ref"]: p for p in out["parts"]}
    assert parts["U1"]["library_symbol"] == {"PI_KIND": "smps", "PI_EFF": "90%", "PI_IMAX": "2A",
                                             "PI_FSW": "1.2MHz"}
    assert parts["U1"]["schematic_instance"]["PI_SOURCE_REF"] == "L1"
    assert parts["U1"]["schematic_instance"]["PI_PINS"] == "2"
    assert "L1" not in parts
    assert parts["Q1"]["schematic_instance"] == {"PI_LINK": "20mOhm", "PI_LINK_PINS": "1,2;5"}
    assert "+3V3" in out["rails"] and not [n for n in out["not_expressed"] if n["part"] in ("U1", "L1", "Q1")]
    back = merge(Project(), {p["ref"]: {**p["library_symbol"], **p["schematic_instance"]}
                             for p in out["parts"]}, pads)
    assert rails_of(back)["+3V3"].sources == [buck]
    assert back.project.links == project.links


def test_i_max_is_shown_next_to_i_out_and_judged_in_the_tree(tmp_path, capsys):
    """PI_IMAX reaches the summaries beside the current delivered: the rail's sources
    (current_limit_a), the tree's VRM rows (i_max_a, in the CLI's --json too), and the tree
    report's and the CI's VRM tables. The tree judges it (tests/test_tree_ratings.py): U1
    delivers 0.5 A of its rated 2 A."""
    import json
    from importlib.resources import files

    from coppernicus import ci
    from coppernicus.cli import main
    from coppernicus.service import Session
    board = buck_board(tmp_path / "buck.kicad_pcb")
    text = board.read_text(encoding="utf-8")
    assert text.count('(property "PI_KIND" "smps"') == 1
    board.write_text(text.replace('(property "PI_KIND" "smps"',
                                  '(property "PI_IMAX" "2A" (at 0 0 0) (layer "F.Fab")) '
                                  '(property "PI_KIND" "smps"'), encoding="utf-8")
    project = tmp_path / "buck.coppernicus.yml"
    Project(board="buck.kicad_pcb").save(project)
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_project(project, maps_dir=None)
    [src] = next(r for r in doc["rails"] if r["net"] == "+3V3")["sources"]
    assert src["current_limit_a"] == 2.0
    tree = session.solve_tree(project)
    rows = {v["name"]: v for v in tree["vrms"]}
    assert rows["U1"]["i_max_a"] == 2.0 and rows["J1"]["i_max_a"] is None
    from coppernicus.power_tree import RATING_RULE
    assert RATING_RULE in tree["assumptions"], tree["assumptions"]
    assert not any("not judged yet" in a for a in tree["assumptions"]), tree["assumptions"]
    assert tree["verdict"] == "pass"                               # the rating, the only limit
    assert "I max (A)" in "\n".join(ci.tree_markdown("buck", tree))
    capsys.readouterr()
    main(["solve", str(project), "--tree", "--json", "--out", str(tmp_path / "cli")])
    cli_rows = {v["name"]: v for v in json.loads(capsys.readouterr().out)["vrms"]}
    assert cli_rows["U1"]["i_max_a"] == 2.0
    js = files("coppernicus").joinpath("report_assets", "report.js").read_text(encoding="utf-8")
    assert "I max (A)" in js and "i_max_a" in js


def test_the_fields_command_prints_a_rails_warnings(tmp_path, capsys):
    from kicad_fixture import write_strip_board

    from coppernicus.cli import main
    board = write_strip_board(tmp_path / "s.kicad_pcb", {
        "J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V", "PI_VOUT": "1.2V"},
        "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    assert main(["fields", str(board)]) == 0
    out = capsys.readouterr().out
    assert "warning: J1: PI_SOURCE" in out and "PI_VOUT" in out


def test_the_annotation_examples_in_the_agent_guide_hold():
    """docs/agents.md, How to annotate a KiCad library: the fixed LDO, the external-inductor
    buck, the load switch and a load on two rails, field for field."""
    ldo = {"PI_KIND": "ldo", "PI_VOUT": "3.3V", "PI_IQ": "50uA", "PI_IMAX": "500mA",
           "PI_RAIL": "+3V3", "PI_INPUT": "+5V", "PI_GND": "GND"}
    [u3] = field_rails({"U3": ldo})["+3V3"].sources
    assert u3.term == Source("U3", 3.3, kind="ldo", quiescent_a=5e-5, input_net="+5V", ground_net="GND",
                             current_limit_a=0.5)
    buck = {"PI_KIND": "smps", "PI_EFF": "90%", "PI_IQ": "2mA", "PI_IMAX": "3A", "PI_FSW": "1.2MHz",
            "PI_RAIL": "+3V3", "PI_SOURCE": "3.3V", "PI_SOURCE_REF": "L1", "PI_PINS": "2",
            "PI_INPUT": "VIN", "PI_INPUT_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "3"}
    [u1] = field_rails({"U1": buck}, BUCK_PADS)["+3V3"].sources
    assert u1.term == Source("L1", 3.3, ["2"], name="U1", kind="smps", efficiency=0.9, quiescent_a=0.002,
                             input_ref="U1", input_pins=["1"], input_net="VIN", ground_ref="U1",
                             ground_pins=["3"], ground_net="GND", current_limit_a=3.0, fsw_hz=1.2e6)
    [(q1, _)] = field_links({"Q1": {"PI_LINK": "20mOhm", "PI_LINK_PINS": "1,2;5"}}, SWITCH_PADS)
    assert (q1.net_a, q1.pins_a, q1.net_b, q1.pins_b) == ("VIN", ["1", "2"], "VOUT", ["5"])
    two = field_rails({"U7": {"PI_RAIL": "+3V3;+1V2", "PI_LOAD": "200mA;1.5A"}})
    assert two["+3V3"].loads[0].term.current == 0.2 and two["+1V2"].loads[0].term.current == 1.5
