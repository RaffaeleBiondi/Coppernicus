"""PI_* field proposals: from a project (or a solved rail), the field text to paste into
KiCad, part by part, split between the library symbol and the schematic instance. The
inverse of board_fields.merge: parsing the proposals gives the project's rails back, remote
sense aside. Nothing is written to KiCad files."""

import copy
import json
from pathlib import Path

import anyio
import pytest
from mcp import Client

from coppernicus.board_fields import merge, propose, quantity
from coppernicus.cli import EXIT_OK, main
from coppernicus.mcp_server import build_server
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import Session

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain" / "power_chain.coppernicus.yml"
PADS = {"FB1": {"1": "+3V3", "2": "+3V3_F"}}


def fields_of(proposal):
    """The proposal as the board's fields: library and instance fields together, per part."""
    return {p["ref"]: {**p["library_symbol"], **p["schematic_instance"]} for p in proposal["parts"]}


def no_sense(rail):
    rail = copy.deepcopy(rail)
    for s in rail.sources:
        s.sense_ref, s.sense_pins, s.sense_net = "", None, ""
        s.ground_sense_ref, s.ground_sense_pins, s.ground_sense_net = "", None, ""
    return rail


def test_the_power_chain_round_trips_through_its_proposed_fields():
    project = Project.load(CHAIN)
    proposal = propose(project, PADS)
    back = merge(Project(), fields_of(proposal), PADS)
    got = {r.net: r for r in back.project.rails}
    assert {r.net: no_sense(r) for r in project.rails} == got
    assert back.project.links == project.links
    parts = {p["ref"]: p for p in proposal["parts"]}
    assert parts["U1"]["library_symbol"] == {"PI_KIND": "smps", "PI_EFF": "90%", "PI_IQ": "5mA"}
    assert parts["U1"]["schematic_instance"] == {
        "PI_RAIL": "+3V3", "PI_SOURCE": "3.3V", "PI_PINS": "2", "PI_INPUT": "VIN", "PI_INPUT_PINS": "1",
        "PI_GND": "GND", "PI_GND_PINS": "3"}
    assert parts["FB1"] == {"ref": "FB1", "library_symbol": {}, "schematic_instance": {"PI_LINK": "50mOhm"}}
    assert any(n["part"] == "U2" and "sense" in n["what"] for n in proposal["not_expressed"])


@pytest.mark.parametrize("name, value, text", [
    ("PI_LOAD", 0.07, "70mA"), ("PI_LOAD", 2.0, "2A"), ("PI_LOAD", 1.25, "1.25A"),
    ("PI_EFF", 0.9, "90%"), ("PI_IQ", 1e-5, "10uA"), ("PI_IQ", 0.005, "5mA"),
    ("PI_LINK", 0.0005, "0.5mOhm"), ("PI_LINK", 1.2, "1.2Ohm"), ("PI_SOURCE", -5.0, "-5V"),
    ("PI_SOURCE", 3.3, "3.3V"), ("PI_LIMIT_DROP", 2.5, "2.5%")])
def test_values_are_written_with_a_unit_and_read_back_exactly(name, value, text):
    from coppernicus.board_fields import field_text
    assert field_text(name, value) == text
    assert quantity("U1", name, text) == value


def test_multi_rail_parts_and_a_part_twice_on_one_rail():
    project = Project(rails=[
        Rail("V", [Source("J1", 3.3)], [Load("U1", 1.2), Load("U9", 0.1, ["1"]), Load("U9", 0.2, ["2"])],
             limits=Limits(max_drop_pct=2.0, max_via_current=1.0)),
        Rail("W", [Source("J2", 1.8)], [Load("U1", 0.4)])])
    proposal = propose(project)
    parts = {p["ref"]: p["schematic_instance"] for p in proposal["parts"]}
    assert parts["U1"] == {"PI_RAIL": "V;W", "PI_LOAD": "1.2A;400mA"}
    assert parts["U9"] == {"PI_RAIL": "V;V", "PI_LOAD": "100mA;200mA", "PI_PINS": "1;2"}
    assert parts["J1"]["PI_LIMIT_DROP"] == "2%"
    assert any("max_via_current" in n["what"] for n in proposal["not_expressed"])
    back = merge(Project(), fields_of(proposal))
    assert {r.net: [(t.ref, t.current, t.pins) for t in r.loads] for r in back.project.rails} == {
        "V": [("U1", 1.2, None), ("U9", 0.1, ["1"]), ("U9", 0.2, ["2"])], "W": [("U1", 0.4, None)]}


def test_what_fields_cannot_carry_is_listed_and_not_proposed():
    project = Project(rails=[
        Rail("OUT", [Source("U2", 3.3, kind="ldo", input_ref="U7", input_net="VIN")], [Load("U4", 1.0)]),
        Rail("VIN", [Source("J1", 12.0, name="Supply")], [Load("U5", 0.1)])])
    proposal = propose(project)
    assert proposal["rails"] == ["VIN"]
    refs = {p["ref"] for p in proposal["parts"]}
    assert "U2" not in refs and "U4" not in refs
    whats = " ".join(n["what"] for n in proposal["not_expressed"])
    assert "input on another part (U7)" in whats and "name" in whats


def test_session_mcp_and_cli_propose(tmp_path, capsys):
    import shutil
    shutil.copytree(CHAIN.parent, tmp_path / "c")
    path = tmp_path / "c" / CHAIN.name
    out = Session(out_dir=tmp_path / "o").propose_fields(project_path=path)
    assert {p["ref"] for p in out["parts"]} >= {"U1", "U2", "FB1"}
    assert any("never writes KiCad files" in a for a in out["assumptions"])

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            res = await c.call_tool("propose_fields", {"project_path": str(path)})
            return res.structured_content
    assert anyio.run(scenario)["parts"] == out["parts"]
    assert main(["fields", "--propose", str(path), "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["parts"] == out["parts"]
    assert main(["fields", "--propose", str(path)]) == EXIT_OK
    text = capsys.readouterr().out
    assert "U1" in text and "library symbol:" in text and "PI_EFF=90%" in text
    session = Session(out_dir=tmp_path / "o2")
    rid = session.solve_project(path)["rails"][1]["result_id"]
    one = session.propose_fields(result_id=rid)
    assert one["rails"] == ["+3V3"]


def test_a_link_whose_pads_are_unknown_is_proposed_without_a_crash(tmp_path, capsys):
    proposal = propose(Project.load(CHAIN))                       # no pads: the app's call
    assert {p["ref"] for p in proposal["parts"]} >= {"FB1", "U1"}
    assert any(n["part"] == "FB1" and "pads" in n["reason"] for n in proposal["not_expressed"])
    lone = tmp_path / "lone.coppernicus.yml"                      # its board is not there
    lone.write_text(CHAIN.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["fields", "--propose", str(lone), "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["parts"]


def test_every_efficiency_in_tenths_of_a_percent_can_be_written():
    from coppernicus.board_fields import field_text
    for k in range(1, 1001):
        text = field_text("PI_EFF", k / 1000)
        assert quantity("U1", "PI_EFF", text) == pytest.approx(k / 1000, rel=1e-12), text
