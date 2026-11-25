"""Rails from KiCad footprint fields, as the owner decided: on by default, KiCad wins over a
project-file rail of the same net (with a visible warning listing the differences) unless
that rail sets override_board_fields, converters and links in fields too. The grammar, the
reader, the pure merge, and every solve path that takes them. The board is a synthetic
.kicad_pcb written by tests/kicad_fixture.py."""

import copy
import dataclasses
import json
import os
import re
from pathlib import Path

import anyio
import pytest
from kicad_fixture import PAD, X0, X1, W, write_strip_board
from mcp import Client

from coppernicus import ci
from coppernicus.board_fields import FieldError, field_rails, merge, quantity
from coppernicus.cli import EXIT_LIMIT, EXIT_OK, main
from coppernicus.engine.kicad_fields import read_board_fields, read_part_fields
from coppernicus.engine.synthetic import OZ1, save_snapshot, strip_board
from coppernicus.mcp_server import build_server
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

RHO = 1.724e-5                                              # Ω·mm, the engine's ρ at 20 °C
DROP_MV_PER_A = RHO * (X1 - X0 - PAD) / (W * OZ1) * 1e3     # ≈ 37.9 mV per A
FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"},
          "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1000mA", "PI_PINS": "1", "PI_LIMIT_DROP": "2%"}}
CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain" / "power_chain.coppernicus.yml"
# The power-chain example, described in fields (remote sense stays project-only).
CHAIN_FIELDS = {
    "J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V", "PI_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "2"},
    "U1": {"PI_RAIL": "+3V3", "PI_SOURCE": "3.3V", "PI_PINS": "2", "PI_KIND": "smps", "PI_EFF": "90%",
           "PI_IQ": "5mA", "PI_INPUT": "VIN", "PI_INPUT_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "3"},
    "U4": {"PI_RAIL": "+3V3", "PI_LOAD": "500mA", "PI_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "2"},
    "U2": {"PI_RAIL": "+1V8", "PI_SOURCE": "1.8V", "PI_PINS": "2", "PI_KIND": "ldo", "PI_IQ": "2mA",
           "PI_INPUT": "+3V3_F", "PI_INPUT_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "3"},
    "U3": {"PI_RAIL": "+1V8", "PI_LOAD": "2A", "PI_PINS": "1", "PI_GND": "GND", "PI_GND_PINS": "2"},
    "FB1": {"PI_LINK": "50mOhm"}}
CHAIN_PADS = {"FB1": {"1": "+3V3", "2": "+3V3_F"}}


@pytest.fixture()
def board(tmp_path):
    return write_strip_board(tmp_path / "strip.kicad_pcb", FIELDS)


def project_file(tmp_path, rails=(), name="fields.coppernicus.yml", **kw):
    return Project(board="strip.kicad_pcb", rails=list(rails), **kw).save(tmp_path / name)


def no_sense(rail):
    rail = copy.deepcopy(rail)
    for s in rail.sources:
        s.sense_ref, s.sense_pins, s.sense_net = "", None, ""
        s.ground_sense_ref, s.ground_sense_pins, s.ground_sense_net = "", None, ""
    return rail


# ---------------------------------------------------------------------- the grammar

@pytest.mark.parametrize("name, text, value", [
    ("PI_SOURCE", "3.3V", 3.3), ("PI_SOURCE", "-5V", -5.0), ("PI_SOURCE", "0V", 0.0),
    ("PI_LOAD", "2A", 2.0), ("PI_LOAD", "250mA", 0.25), ("PI_LOAD", "1.5 A", 1.5),
    ("PI_LIMIT_DROP", "2%", 2.0), ("PI_EFF", "90%", 0.9), ("PI_EFF", "100%", 1.0),
    ("PI_IQ", "1mA", 1e-3), ("PI_IQ", "50uA", 50e-6), ("PI_IQ", "50µA", 50e-6),
    ("PI_IQ", "50μA", 50e-6), ("PI_LINK", "50mOhm", 0.05), ("PI_LINK", "0Ohm", 0.0),
    ("PI_LINK", "1.2Ohm", 1.2), ("PI_LINK", "10mΩ", 0.01), ("PI_LINK", "2Ω", 2.0)])
def test_values_carry_their_unit(name, text, value):
    assert quantity("U1", name, text) == pytest.approx(value)


@pytest.mark.parametrize("name, text, words", [
    ("PI_LOAD", "2", "give the unit (A or mA)"), ("PI_SOURCE", "3.3", "a bare number is refused"),
    ("PI_LOAD", "2,5A", "use a dot"), ("PI_SOURCE", "3V3", "not a value with its unit"),
    ("PI_LOAD", "2V", "PI_LOAD takes A or mA, not V"), ("PI_LOAD", "-1A", "≥ 0"),
    ("PI_LIMIT_DROP", "0%", "> 0"), ("PI_EFF", "0.9", "give the unit"), ("PI_EFF", "120%", "100 %"),
    ("PI_IQ", "-1mA", "≥ 0"), ("PI_LINK", "50m", "takes Ohm or mOhm, not m"),
    ("PI_LINK", "-1Ohm", "≥ 0")])
def test_a_value_without_its_unit_is_refused(name, text, words):
    with pytest.raises(FieldError) as e:
        quantity("U1", name, text)
    assert f'U1: {name} = "{text}"' in str(e.value) and words in str(e.value)


def test_one_part_on_several_rails_by_position():
    rails = field_rails({"U2": {"PI_RAIL": "VIN;+3V3", "PI_LOAD": "0.8A;", "PI_SOURCE": ";3.3V",
                                "PI_PINS": ";2", "PI_LIMIT_DROP": ""}})
    [load] = rails["VIN"].loads
    assert (load.term.ref, load.term.current, load.term.pins) == ("U2", 0.8, None)
    assert [fv.text for _, fv in load.values] == ["0.8A"]
    [src] = rails["+3V3"].sources
    assert (src.term.voltage, src.term.pins) == (3.3, ["2"])
    assert [(fv.field, fv.text) for _, fv in src.values] == [("PI_SOURCE", "3.3V"), ("PI_PINS", "2")]


@pytest.mark.parametrize("fields, words", [
    ({"PI_RAIL": "A;B", "PI_LOAD": "1A"}, "has 1 value(s) for 2 rail(s)"),
    ({"PI_LOAD": "1A"}, "PI_LOAD without PI_RAIL"),
    ({"PI_RAIL": "VCC"}, "no PI_SOURCE or PI_LOAD"),
    ({"PI_RAIL": "VCC", "PI_LOADS": "1A"}, "unknown field PI_LOADS"),
    ({"PI_RAIL": "VCC", "pi_load": "1A"}, "unknown field pi_load"),
    ({"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_SOURCE": "1V"}, "both PI_SOURCE and PI_LOAD"),
    ({"PI_RAIL": "A;B", "PI_LOAD": "1A;"}, "rail B (PI_RAIL position 2) has neither"),
    ({"PI_RAIL": "A;", "PI_LOAD": "1A;1A"}, "empty net name"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_KIND": "buck"}, "PI_KIND is one of source, ldo, smps"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_KIND": "smps", "PI_INPUT": "VIN"}, "needs PI_EFF"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_KIND": "ldo"}, "needs PI_INPUT"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_EFF": "90%"}, "PI_EFF is for a switcher"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_IQ": "1mA"}, "PI_IQ is for a regulator"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_INPUT": "VIN"}, "PI_INPUT is for a regulator"),
    ({"PI_RAIL": "V", "PI_LOAD": "1A", "PI_KIND": "ldo"}, "PI_KIND is for a source"),
    ({"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_GND_PINS": "3"}, "PI_GND_PINS without PI_GND"),
    ({"PI_RAIL": "VIN;V", "PI_LOAD": "1A;", "PI_SOURCE": ";1V", "PI_KIND": ";ldo", "PI_INPUT": ";VIN"},
     "counted twice"),
    ({"PI_RAIL": "V", "PI_LOAD": "1A", "PI_LINK": "1Ohm"}, "a link part has no PI_RAIL")])
def test_fields_that_cannot_be_used_name_the_part_and_the_field(fields, words):
    with pytest.raises(FieldError) as e:
        merge(Project(), {"U7": fields}, {"U7": {"1": "V", "2": "W"}})
    assert "U7: " in str(e.value) and words in str(e.value)


def test_the_strictest_drop_limit_wins_and_says_where_it_is_from():
    rails = field_rails({"J1": {"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_LIMIT_DROP": "3%"},
                         "U1": {"PI_RAIL": "V", "PI_LOAD": "1A", "PI_LIMIT_DROP": "2%"},
                         "U2": {"PI_RAIL": "V", "PI_LOAD": "1A", "PI_LIMIT_DROP": "2.5%"}})
    assert rails["V"].rail().limits.max_drop_pct == 2.0
    assert 'limit max_drop_pct 2 (from KiCad field PI_LIMIT_DROP "2%" on U1)' in rails["V"].note()


# ---------------------------------------------------------------------- the merge (pure)

def test_the_power_chain_in_fields_is_the_example_minus_sense():
    """Converters, grounds and a link described in fields map onto the same v2 objects as
    the project file, so the engine is unchanged. VIN has a source and no load, but it feeds
    U1: complete."""
    example = Project.load(CHAIN)
    merged = merge(Project(), CHAIN_FIELDS, CHAIN_PADS)
    got = {r.net: r for r in merged.project.rails}
    assert set(got) == {r.net for r in example.rails}
    for rail in example.rails:
        assert got[rail.net] == no_sense(rail), rail.net
    assert merged.project.links == example.links
    assert merged.warnings == [] and all(o.source == "fields" for o in merged.origins.values())
    note = " ".join(merged.origins["+3V3"].notes)
    for name in ("PI_KIND", "PI_EFF", "PI_IQ", "PI_INPUT", "PI_INPUT_PINS", "PI_GND", "PI_GND_PINS",
                 "PI_LINK"):
        assert f"from KiCad field {name}" in note, name


def test_kicad_wins_over_a_different_project_rail_with_a_warning():
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0, ["1"]), Load("U9", 1.0)],
               limits=Limits(max_drop_pct=5.0, max_via_current=2.0))
    merged = merge(Project(rails=[own]), FIELDS)
    [rail] = merged.project.rails
    assert [(ld.ref, ld.current) for ld in rail.loads] == [("U1", 1.0)]           # the fields'
    assert rail.limits.max_drop_pct == 2.0 and rail.limits.max_via_current == 2.0  # J, via kept
    origin = merged.origins["VCC"]
    assert origin.source == "fields"
    [ignored, limit] = origin.warnings
    assert ignored.startswith("project file rail VCC ignored: KiCad fields define it (")
    assert "KiCad U1 1 A pins 1, project U1 3 A pins 1" in ignored
    assert "project has load U9 1 A, KiCad does not" in ignored
    assert "set override_board_fields: true on the rail to use the project's values" in ignored
    assert "max_drop_pct" in limit and "PI_LIMIT_DROP" in limit and "5" in limit


def test_override_board_fields_keeps_the_project_rail_and_says_so():
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 0.5)], override_board_fields=True)
    merged = merge(Project(rails=[own]), FIELDS)
    # The project rail, but the KiCad PI_LIMIT_DROP still applies to the net (the ruling).
    assert merged.project.rails == [dataclasses.replace(own, limits=Limits(max_drop_pct=2.0))]
    origin = merged.origins["VCC"]
    assert origin.source == "overridden" and origin.warnings == []
    assert origin.board_fields["loads"][0]["current_a"] == 1.0                  # what it overrode
    assert "override_board_fields" in origin.notes[0] and "1000mA" in origin.notes[0]


def test_saving_the_merged_project_makes_the_next_merge_silent(tmp_path):
    """The normal case once the app has saved: the project file holds the fields' rails, so
    merging again changes nothing and warns about nothing."""
    first = merge(Project.load(CHAIN), CHAIN_FIELDS, CHAIN_PADS)     # sense comes from the project
    path = first.project.save(tmp_path / "saved.coppernicus.yml")
    again = merge(Project.load(path), CHAIN_FIELDS, CHAIN_PADS)
    assert again.warnings == [] and all(not o.warnings for o in again.origins.values())
    assert again.project.rails == Project.load(path).rails and again.project.links == first.project.links


def test_remote_sense_is_carried_over_from_an_ignored_project_rail():
    merged = merge(Project.load(CHAIN), CHAIN_FIELDS, CHAIN_PADS)
    u2 = next(s for s in next(r for r in merged.project.rails if r.net == "+1V8").sources)
    assert (u2.sense_ref, u2.sense_net, u2.ground_sense_net) == ("U3", "+1V8", "GND")
    assert any("sense pins of U2 from the project file" in n for n in merged.origins["+1V8"].notes)


def test_incomplete_fields_leave_a_full_project_rail_in_place_with_a_warning():
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])
    merged = merge(Project(rails=[own]), {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "2A"}})
    assert merged.project.rails == [own] and merged.origins["VCC"].source == "project"
    assert "incomplete" in merged.origins["VCC"].warnings[0]
    with pytest.raises(FieldError, match="no part on this net has PI_SOURCE"):
        merge(Project(), {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "2A"}})


@pytest.mark.parametrize("how", ["project regulator", "field regulator", "through a link"])
def test_a_source_only_rail_that_feeds_something_is_complete(how):
    fields = {"J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V"}}
    project = Project()
    reg = {"PI_RAIL": "OUT", "PI_SOURCE": "3.3V", "PI_KIND": "ldo", "PI_INPUT": "VIN"}
    if how == "project regulator":
        project.rails.append(Rail("OUT", [Source("U2", 3.3, kind="ldo", input_net="VIN")], [Load("U4", 1.0)]))
    elif how == "field regulator":
        fields.update(U2=reg, U4={"PI_RAIL": "OUT", "PI_LOAD": "1A"})
    else:                                    # VIN feeds FB1, behind which U2 draws from VIN_F
        fields.update(FB1={"PI_LINK": "10mOhm"}, U2=dict(reg, PI_INPUT="VIN_F"),
                      U4={"PI_RAIL": "OUT", "PI_LOAD": "1A"})
    merged = merge(project, fields, {"FB1": {"1": "VIN", "2": "VIN_F"}})
    assert "VIN" in [r.net for r in merged.project.rails]
    with pytest.raises(FieldError, match="has PI_SOURCE but no part on this net has PI_LOAD"):
        merge(Project(), {"J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V"}})


def test_a_source_only_rail_feeds_through_a_net_tie():
    """A net tie joins nets as the solve does: a regulator whose input is on the tied net is
    fed by the rail, so the rail is complete."""
    project = Project(rails=[Rail("OUT", [Source("U2", 3.3, kind="ldo", input_net="VIN_T")],
                                  [Load("U4", 1.0)])])
    fields = {"J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V"}}
    merged = merge(project, fields, ties=[("VIN", "VIN_T")])
    assert "VIN" in [r.net for r in merged.project.rails]
    with pytest.raises(FieldError, match="feeds no regulator input or link"):
        merge(project, fields)


def test_a_project_link_on_a_field_link_part_is_replaced_with_a_warning():
    project = Project.load(CHAIN)
    project.links[0].resistance_ohm = 0.1
    merged = merge(project, CHAIN_FIELDS, CHAIN_PADS)
    assert merged.project.links[0].resistance_ohm == pytest.approx(0.05)
    assert any("project file link FB1 ignored" in w and "0.1" in w for w in merged.warnings)
    # Per net, structured, under both nets of the link (+3V3_F is no rail of its own).
    [on_33] = merged.link_warnings["+3V3"]
    assert merged.link_warnings["+3V3_F"] == [on_33] and "FB1" in on_33
    assert "VIN" not in merged.link_warnings


def test_merge_never_changes_the_loaded_project():
    project = Project(rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])])
    before = copy.deepcopy(project)
    merge(project, FIELDS)
    assert project == before


def test_a_limits_only_project_rail_adds_its_limits_without_a_warning():
    """The way to give a field rail the limits that only the project file can hold: a rail
    with no sources and no loads is not compared with the fields."""
    merged = merge(Project(rails=[Rail("VCC", limits=Limits(max_via_current=2.0))]), FIELDS)
    [rail] = merged.project.rails
    origin = merged.origins["VCC"]
    assert origin.source == "fields" and origin.warnings == [] and merged.warnings == []
    assert rail.limits.max_via_current == 2.0 and rail.limits.max_drop_pct == 2.0
    assert [ld.ref for ld in rail.loads] == ["U1"]


@pytest.mark.parametrize("change", ["all pins", "sides swapped"])
def test_an_equal_link_written_another_way_gives_no_warning(change):
    project = Project.load(CHAIN)
    k = project.links[0]
    if change == "all pins":
        k.pins_a, k.pins_b = None, None
    else:
        k.pins_a, k.net_a, k.pins_b, k.net_b = k.pins_b, k.net_b, k.pins_a, k.net_a
    assert merge(project, CHAIN_FIELDS, CHAIN_PADS).warnings == []


# ---------------------------------------------------------------------- the reader

def test_reader_finds_field_names_written_without_quotes(board):
    """KiCad writes some property names bare (``(property ki_fp_filters "...")`` in its own
    templates): the scan before the parse must not skip them."""
    bare = re.sub(r'\(property "(PI_[A-Z_]+)"', r"(property \1", board.read_text(encoding="utf-8"))
    assert "(property PI_RAIL " in bare
    board.write_text(bare, encoding="utf-8")
    assert read_part_fields(board) == FIELDS


def test_reader_returns_only_pi_fields_and_the_pads_nets(board, tmp_path, monkeypatch):
    assert read_part_fields(board) == FIELDS
    both = read_board_fields(board)
    assert both.fields == FIELDS and both.pad_nets == {"J1": {"1": "VCC"}, "U1": {"1": "VCC"}}
    write_strip_board(tmp_path / "plain.kicad_pcb", {})
    import coppernicus.engine.kicad_fields as kf
    monkeypatch.setattr(kf, "load_tree", lambda path: (_ for _ in ()).throw(AssertionError("parsed")))
    assert read_board_fields(tmp_path / "plain.kicad_pcb").fields == {}   # no PI_ text: no parse
    monkeypatch.undo()
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    assert read_board_fields(tmp_path / "strip.kipi.json.gz").fields == {}  # another format: none
    dup = write_strip_board(tmp_path / "dup.kicad_pcb", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}},
                            extra_parts=[("U1", 20.0, 3.0, "VCC")])
    with pytest.raises(Exception, match="two footprints are 'U1'"):
        read_part_fields(dup)


# ---------------------------------------------------------------------- solving

def test_fields_are_read_by_default(board, tmp_path):
    before = (board.read_bytes(), os.stat(board).st_mtime_ns)
    project = project_file(tmp_path)
    project_bytes = project.read_bytes()
    doc = Session(out_dir=tmp_path / "out").solve_project(project)
    [rail] = doc["rails"]
    assert rail["net"] == "VCC" and rail["from_board_fields"] is True
    assert rail["overrides_board_fields"] is False
    assert rail["loads"][0]["drop_mv"] == pytest.approx(DROP_MV_PER_A, rel=1e-5)
    assert rail["limits"] == {"max_drop_pct": 2.0} and rail["verdict"] == "fail"   # 3.8 % > 2 %
    [note] = [a for a in rail["assumptions"] if a.startswith("Rail VCC from KiCad footprint fields")]
    for name in ("PI_RAIL", "PI_SOURCE", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP"):
        assert f"from KiCad field {name}" in note
    assert (board.read_bytes(), os.stat(board).st_mtime_ns) == before          # only read
    assert project.read_bytes() == project_bytes                               # never saved


def test_use_board_fields_false_turns_them_off(board, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    off = project_file(tmp_path, use_board_fields=False)
    assert "use_board_fields: false" in off.read_text(encoding="utf-8")
    with pytest.raises(ServiceError, match="No rails to solve"):
        session.solve_project(off)
    with pytest.raises(ServiceError, match="No rails to solve"):
        session.solve_project(project_file(tmp_path), use_board_fields=False)
    assert "use_board_fields" not in project_file(tmp_path).read_text(encoding="utf-8")


def test_an_ignored_project_rail_warns_everywhere(board, tmp_path, capsys, monkeypatch):
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])
    project = project_file(tmp_path, [own])
    [rail] = Session(out_dir=tmp_path / "out").solve_project(project)["rails"]
    assert rail["from_board_fields"] is True
    assert rail["loads"][0]["current_a"] == pytest.approx(1.0)
    [warning] = rail["board_fields_warnings"]
    assert "project file rail VCC ignored" in warning and warning in rail["warnings"]
    assert main(["solve", str(project), "--no-maps", "--out", str(tmp_path / "o")]) == EXIT_LIMIT
    assert "warning: project file rail VCC ignored" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["fields.coppernicus.yml"], Path("results"), html=False)
    assert "project file rail VCC ignored" in ci.summary_markdown(index)


def test_an_overriding_project_rail_is_used_and_reported(board, tmp_path):
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 0.5)], override_board_fields=True)
    [rail] = Session(out_dir=tmp_path / "out").solve_project(project_file(tmp_path, [own]))["rails"]
    assert rail["from_board_fields"] is False and rail["overrides_board_fields"] is True
    assert rail["loads"][0]["drop_mv"] == pytest.approx(DROP_MV_PER_A / 2, rel=1e-5)
    assert rail["board_fields_overridden"]["loads"][0]["current_a"] == 1.0
    assert rail["limits"] == {"max_drop_pct": 2.0} and rail["verdict"] == "pass"   # 1.9 % < 2 %
    assert any("override_board_fields" in a for a in rail["assumptions"])
    assert any("a KiCad limit always applies to its net" in a for a in rail["assumptions"])


def test_errors_name_the_off_switch_and_other_formats_have_no_fields(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    write_strip_board(tmp_path / "strip.kicad_pcb", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    with pytest.raises(ServiceError) as e:
        session.solve_project(project_file(tmp_path))
    assert e.value.code == "invalid_board_field" and "PI_SOURCE" in e.value.message
    assert "use_board_fields: false" in e.value.hint
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    snap = Project(board="strip.kipi.json.gz", rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])])
    [rail] = session.solve_project(snap.save(tmp_path / "snap.coppernicus.yml"))["rails"]
    assert rail["from_board_fields"] is False and rail["verdict"] == "not_judged"


@pytest.mark.parametrize("fields, extra, code, words", [
    ({"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_PINS": "9"}}, (), "pin_not_on_net", "no pin '9'"),
    ({"U2": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}, [("U2", 20.0, 3.0, "GND")], "part_not_on_net",
     "U2 has no pads on net VCC"),
    ({"J9": {"PI_RAIL": "NOPE", "PI_SOURCE": "1V"}, "U9": {"PI_RAIL": "NOPE", "PI_LOAD": "1A"}},
     [("J9", 10.0, 3.0, "VCC"), ("U9", 30.0, 3.0, "VCC")], "net_not_found", "NOPE")])
def test_errors_found_while_solving_a_field_rail_say_where_it_came_from(tmp_path, fields, extra, code,
                                                                        words):
    write_strip_board(tmp_path / "strip.kicad_pcb", {**FIELDS, **fields}, extra_parts=extra)
    with pytest.raises(ServiceError) as e:
        Session(out_dir=tmp_path / "out").solve_project(project_file(tmp_path), stop_on_error=True)
    assert e.value.code == code and words in e.value.message
    assert "from KiCad footprint fields: check PI_RAIL and PI_PINS on" in e.value.message


# ---------------------------------------------------------------------- CLI, MCP, tree

def test_cli_fields_no_board_fields_and_the_report(board, tmp_path, capsys):
    project = project_file(tmp_path)
    assert main(["solve", str(project), "--no-maps", "--out", str(tmp_path / "o"), "--html"]) == EXIT_LIMIT
    assert "assumption: Rail VCC from KiCad footprint fields" in capsys.readouterr().out
    report = (tmp_path / "o" / "fields.coppernicus_report.html").read_text(encoding="utf-8")
    assert "from KiCad field PI_LIMIT_DROP" in report
    assert main(["solve", str(project), "--no-maps", "--json", "--no-board-fields"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["message"].startswith("No rails to solve")
    assert main(["fields", str(board), "--json"]) == EXIT_OK
    listed = json.loads(capsys.readouterr().out)
    [rail] = listed["rails"]
    assert rail["complete"] and rail["loads"][0]["from"] == 'from KiCad field PI_LOAD "1000mA" on U1'


def test_solve_tree_reads_the_fields_in_the_cli_and_the_tool(board, tmp_path, capsys):
    project = project_file(tmp_path)
    assert main(["solve", str(project), "--tree", "--json", "--out", str(tmp_path / "cli")]) == EXIT_LIMIT
    cli_doc = json.loads(capsys.readouterr().out)
    cli_doc.pop("summary_file")
    tool_doc = Session(out_dir=tmp_path / "out").solve_tree(project)
    assert tool_doc == cli_doc and [g["group"] for g in tool_doc["groups"]] == ["VCC"]
    assert any(a.startswith("Rail VCC from KiCad footprint fields") for a in tool_doc["assumptions"])
    assert main(["solve", str(project), "--tree", "--json", "--no-board-fields",
                 "--out", str(tmp_path / "cli")]) == 2
    capsys.readouterr()


def test_mcp_board_fields_and_solve_project(board, tmp_path):
    project = project_file(tmp_path)

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            bid = (await c.call_tool("open_board", {"path": str(board)})).structured_content["board_id"]
            listed = await c.call_tool("board_fields", {"board_id": bid})
            solved = await c.call_tool("solve_project", {"project_path": str(project)})
            off = await c.call_tool("solve_project", {"project_path": str(project),
                                                      "use_board_fields": False})
            return listed.structured_content, solved.structured_content, off
    listed, solved, off = anyio.run(scenario)
    assert listed["rails"][0]["sources"][0]["from"] == 'from KiCad field PI_SOURCE "1V" on J1'
    assert solved["rails"][0]["from_board_fields"] is True and solved["verdict"] == "fail"
    assert off.is_error and off.structured_content["error"]["code"] == "invalid_input"


def test_the_action_reads_the_fields_with_no_setting(board, tmp_path, monkeypatch):
    project_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["fields.coppernicus.yml"], Path("results"), html=False)
    assert index["exit_code"] == 1 and index["verdict"] == "fail"
    md = ci.summary_markdown(index)
    assert "| VCC |" in md and "from KiCad field PI_SOURCE" in md


def test_project_keys_round_trip(tmp_path):
    rail = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)], override_board_fields=True)
    path = Project(board="b.kicad_pcb", rails=[rail], use_board_fields=False).save(
        tmp_path / "p.coppernicus.yml")
    text = path.read_text(encoding="utf-8")
    assert "use_board_fields: false" in text and "override_board_fields: true" in text
    loaded = Project.load(path)
    assert loaded.use_board_fields is False and loaded.rails[0].override_board_fields is True
    plain = Project(board="b.kicad_pcb", rails=[Rail("VCC")]).save(tmp_path / "q.coppernicus.yml")
    assert "board_fields" not in plain.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- review of 4b

def test_pi_limit_drop_applies_under_an_incomplete_or_overridden_rail():
    """The coordinator's ruling: a PI_LIMIT_DROP always applies to its net. A project rail
    cannot loosen a KiCad limit, whether the fields' rail is complete, incomplete (the
    project rail is used) or overridden; a different project max_drop_pct gives a warning."""
    own = Rail("VCC", [Source("J1", 3.3)], [Load("U1", 1.0)], limits=Limits(max_drop_pct=50.0))
    incomplete = merge(Project(rails=[own]),
                       {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_LIMIT_DROP": "0.001%"}})
    [rail] = incomplete.project.rails
    assert incomplete.origins["VCC"].source == "project" and rail.limits.max_drop_pct == 0.001
    assert any("PI_LIMIT_DROP" in w and "50" in w for w in incomplete.origins["VCC"].warnings)
    assert any("PI_LIMIT_DROP" in n and "0.001" in n for n in incomplete.origins["VCC"].notes)
    over = Rail("VCC", [Source("J1", 3.3)], [Load("U1", 1.0)], override_board_fields=True,
                limits=Limits(max_via_current=10.0))
    fields = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"},
              "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_LIMIT_DROP": "0.001%"}}
    overridden = merge(Project(rails=[over]), fields)
    [rail] = overridden.project.rails
    assert overridden.origins["VCC"].source == "overridden"
    assert rail.limits.max_drop_pct == 0.001 and rail.limits.max_via_current == 10.0
    assert any("PI_LIMIT_DROP" in n for n in overridden.origins["VCC"].notes)


def test_a_loads_only_net_behind_a_link_or_a_net_tie_is_complete():
    """Completeness is per group of nets joined by links and net ties: the loads behind a
    ferrite, on a net with no source of its own, belong to the driven net's group."""
    fields = {"J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V"},
              "U1": {"PI_RAIL": "+3V3", "PI_SOURCE": "3.3V", "PI_KIND": "ldo", "PI_INPUT": "VIN"},
              "FB1": {"PI_LINK": "50mOhm"},
              "U2": {"PI_RAIL": "+3V3_F", "PI_LOAD": "1A"}}
    merged = merge(Project(), fields, {"FB1": {"1": "+3V3", "2": "+3V3_F"}})
    rails = {r.net: r for r in merged.project.rails}
    assert rails["+3V3_F"] == Rail("+3V3_F", loads=[Load("U2", 1.0)])
    # A net tie joins the copper in the per-rail solve too: the tied net's loads go into the
    # driven rail, whose solve finds their pads on the tied net.
    tied = {"J1": {"PI_RAIL": "V", "PI_SOURCE": "1V"}, "U2": {"PI_RAIL": "V_T", "PI_LOAD": "1A"}}
    by_tie = merge(Project(), tied, ties=[("V", "V_T")])
    assert by_tie.project.rails == [Rail("V", [Source("J1", 1.0)], [Load("U2", 1.0)])]
    assert any("V_T" in n and "net tie" in n for n in by_tie.origins["V"].notes)
    with pytest.raises(FieldError, match="V_T: loads U2 have PI_LOAD but no part"):
        merge(Project(), tied)


def test_a_limits_only_rail_on_a_tied_net_adds_its_limits_and_the_loads_still_move():
    """The peer's app case (the CLI too): a project rail that holds only limits on a tied net
    must not stop the fields' loads there from moving into the driven rail. It stays as it is,
    limits only: the limits judge that net (per net, as the power tree looks them up)."""
    fields = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"},
              "U2": {"PI_RAIL": "VCC_B", "PI_LOAD": "1A"}}
    ties = [("VCC", "VCC_B")]
    only_limits = Rail("VCC_B", limits=Limits(max_drop_pct=2.0, max_current_density=30.0))
    merged = merge(Project(rails=[only_limits]), fields, ties=ties)
    rails = {r.net: r for r in merged.project.rails}
    assert rails["VCC"] == Rail("VCC", [Source("J1", 3.3)], [Load("U2", 1.0)])
    assert rails["VCC_B"] == only_limits
    assert merged.warnings == []
    assert merged.origins["VCC_B"].source == "project"
    assert any("VCC_B" in n and "net tie" in n for n in merged.origins["VCC"].notes)
    assert any("U2" in n and "VCC" in n for n in merged.origins["VCC_B"].notes)
    # The same when the project file drives the rail.
    driven = Rail("VCC", [Source("J1", 3.3)])
    merged = merge(Project(rails=[driven, only_limits]), {"U2": fields["U2"]}, ties=ties)
    rails = {r.net: r for r in merged.project.rails}
    assert rails["VCC"] == Rail("VCC", [Source("J1", 3.3)], [Load("U2", 1.0)])
    assert rails["VCC_B"] == only_limits
    # A PI_LIMIT_DROP on the tied net still wins over the project's max_drop_pct there.
    strict = {**fields, "U2": {**fields["U2"], "PI_LIMIT_DROP": "1%"}}
    merged = merge(Project(rails=[only_limits]), strict, ties=ties)
    rails = {r.net: r for r in merged.project.rails}
    assert rails["VCC_B"].limits == Limits(max_drop_pct=1.0, max_current_density=30.0)
    assert rails["VCC"].limits == Limits()
    assert any("PI_LIMIT_DROP" in w and "VCC_B" in w for w in merged.warnings)


def test_pins_are_compared_as_pads_not_as_spelling():
    project = Project(rails=[Rail("VCC", [Source("J1", 1.0, ["1"])], [Load("U1", 1.0, ["1"])])])
    fields = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"}, "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}
    merged = merge(project, fields, {"J1": {"1": "VCC"}, "U1": {"1": "VCC", "2": "GND"}})
    assert merged.warnings == []
    differs = merge(Project(rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0, ["2"])])]),
                    fields, {"J1": {"1": "VCC"}, "U1": {"1": "VCC", "2": "VCC"}})
    assert any("U1" in w for w in differs.warnings)


def test_a_footprint_with_fields_but_no_reference_is_a_clear_error(tmp_path):
    from kicad_fixture import write_board

    from coppernicus.engine import EngineError
    board = write_board(tmp_path / "noref.kicad_pcb",
                        [(None, [("1", 12.5, 7.0, "VCC")], {"PI_RAIL": "VCC", "PI_LOAD": "1A"})],
                        [("VCC", (0, 0), (12.5, 7.0), 0.5)])
    with pytest.raises(EngineError, match=r"a footprint at \(0, 0\) mm has PI_\* fields but no "
                                          r"reference: set its reference in KiCad"):
        read_board_fields(board)


CHAIN_TRACKS = [("VIN", (0, 0), (20, 0), 1.0), ("+3V3", (20, 5), (30, 5), 1.0),
                ("+3V3_F", (32, 5), (40, 5), 1.0), ("GND", (0, 10), (40, 10), 1.0)]
FERRITE_FIELDS = {
    "J1": {"PI_RAIL": "VIN", "PI_SOURCE": "12V", "PI_GND": "GND"},
    "U1": {"PI_RAIL": "+3V3", "PI_SOURCE": "3.3V", "PI_KIND": "smps", "PI_EFF": "90%", "PI_IQ": "5mA",
           "PI_INPUT": "VIN", "PI_GND": "GND"},
    "FB1": {"PI_LINK": "50mOhm"},
    "U2": {"PI_RAIL": "+3V3_F", "PI_LOAD": "1A", "PI_GND": "GND"}}


def ferrite_parts(fields):
    return [("J1", [("1", 0, 0, "VIN"), ("2", 0, 10, "GND")], fields.get("J1", {})),
            ("U1", [("1", 20, 0, "VIN"), ("2", 20, 5, "+3V3"), ("3", 20, 10, "GND")], fields.get("U1", {})),
            ("FB1", [("1", 30, 5, "+3V3"), ("2", 32, 5, "+3V3_F")], fields.get("FB1", {})),
            ("U2", [("1", 40, 5, "+3V3_F"), ("2", 40, 10, "GND")], fields.get("U2", {}))]


def test_the_ferrite_chain_in_fields_solves_as_its_project_file(tmp_path):
    """The reviewer's case: a load behind a ferrite, described in fields, gives the same
    results as the same design in the project file, per rail and in the tree."""
    from kicad_fixture import write_board

    from coppernicus.project import Link
    rails = [Rail("VIN", sources=[Source("J1", 12.0, ground_net="GND")]),
             Rail("+3V3", sources=[Source("U1", 3.3, kind="smps", efficiency=0.9, quiescent_a=0.005,
                                          input_net="VIN", ground_net="GND")]),
             Rail("+3V3_F", loads=[Load("U2", 1.0, ground_net="GND")])]
    docs = {}
    for name, fields, project in (
            ("fields", FERRITE_FIELDS, Project(board="b.kicad_pcb")),
            ("project", {}, Project(board="b.kicad_pcb", rails=rails,
                                    links=[Link("FB1", "FB1", None, "+3V3", None, "+3V3_F", 0.05)]))):
        d = tmp_path / name
        d.mkdir()
        write_board(d / "b.kicad_pcb", ferrite_parts(fields), CHAIN_TRACKS)
        path = project.save(d / "p.coppernicus.yml")
        session = Session(out_dir=d / "out")
        docs[name] = (session.solve_project(path), session.solve_tree(path))

    def per_rail(doc):
        return {r["net"]: ([(ld["name"], ld["current_a"], ld["drop_mv"]) for ld in r.get("loads", [])],
                           r.get("error", {}).get("code")) for r in doc["rails"]}

    def tree(doc):
        return ({v["name"]: (v["v_in_v"], v["i_in_a"], v["v_out_v"]) for v in doc["vrms"]},
                {ld["name"]: (ld["v_worst_v"], ld["drop_mv"]) for ld in doc["loads"]},
                [(k["name"], k["current_a"]) for k in doc["links"]])
    assert per_rail(docs["fields"][0]) == per_rail(docs["project"][0])
    assert tree(docs["fields"][1]) == tree(docs["project"][1])


def test_a_loads_only_rail_behind_a_link_is_skipped_not_an_error(tmp_path):
    """Per rail, the loads behind a ferrite are drawn by the driven rail through the link
    (estimated there): their own rail, with no source, is skipped with that said, not an
    error that would fail a valid design (exit 2)."""
    from kicad_fixture import write_board
    write_board(tmp_path / "b.kicad_pcb", ferrite_parts(FERRITE_FIELDS), CHAIN_TRACKS)
    doc = Session(out_dir=tmp_path / "out").solve_project(
        Project(board="b.kicad_pcb").save(tmp_path / "p.coppernicus.yml"))
    rails = {r["net"]: r for r in doc["rails"]}
    assert "error" not in rails["+3V3_F"] and doc["verdict"] != "error"
    message = rails["+3V3_F"]["skipped"]["message"]
    assert "drawn through link FB1 by rail +3V3" in message
    assert [ld["name"] for ld in rails["+3V3"]["loads"]] == ["FB1 (link, estimated)"]
