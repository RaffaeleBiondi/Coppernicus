"""Rails from KiCad footprint fields (PI_RAIL, PI_SOURCE, PI_LOAD, PI_PINS, PI_LIMIT_DROP):
the field grammar, the reader, the merge with a project file, and every solve path that
takes them. The board is a synthetic .kicad_pcb written by tests/kicad_fixture.py."""

import json
import os
from pathlib import Path

import anyio
import pytest
from kicad_fixture import PAD, X0, X1, W, write_strip_board
from mcp import Client

from coppernicus import ci
from coppernicus.board_fields import FieldError, field_rails, merge, quantity
from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_OK, main
from coppernicus.engine import EngineError
from coppernicus.engine.kicad_fields import read_part_fields
from coppernicus.engine.synthetic import OZ1, save_snapshot, strip_board
from coppernicus.mcp_server import build_server
from coppernicus.project import Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

RHO = 1.724e-5                                              # Ω·mm, the engine's ρ at 20 °C
DROP_MV_PER_A = RHO * (X1 - X0 - PAD) / (W * OZ1) * 1e3     # ≈ 37.9 mV per A
FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"},
          "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1000mA", "PI_PINS": "1", "PI_LIMIT_DROP": "2%"}}


@pytest.fixture()
def board(tmp_path):
    return write_strip_board(tmp_path / "strip.kicad_pcb", FIELDS)


def project_file(tmp_path, rails=(), use=True, name="fields.coppernicus.yml"):
    return Project(board="strip.kicad_pcb", rails=list(rails), use_board_fields=use).save(tmp_path / name)


# ---------------------------------------------------------------------- the grammar

@pytest.mark.parametrize("name, text, value", [
    ("PI_SOURCE", "3.3V", 3.3), ("PI_SOURCE", "-5V", -5.0), ("PI_SOURCE", "0V", 0.0),
    ("PI_LOAD", "2A", 2.0), ("PI_LOAD", "250mA", 0.25), ("PI_LOAD", "1.5 A", 1.5),
    ("PI_LIMIT_DROP", "2%", 2.0), ("PI_LIMIT_DROP", "0.5%", 0.5)])
def test_values_carry_their_unit(name, text, value):
    assert quantity("U1", name, text) == pytest.approx(value)


@pytest.mark.parametrize("name, text, words", [
    ("PI_LOAD", "2", "give the unit (A or mA)"), ("PI_SOURCE", "3.3", "a bare number is refused"),
    ("PI_LOAD", "2,5A", "use a dot"), ("PI_SOURCE", "3V3", "not a value with its unit"),
    ("PI_LOAD", "2V", "PI_LOAD takes A or mA, not V"), ("PI_SOURCE", "2A", "takes V, not A"),
    ("PI_LOAD", "-1A", "≥ 0"), ("PI_LIMIT_DROP", "0%", "> 0"), ("PI_LIMIT_DROP", "2", "give the unit")])
def test_a_value_without_its_unit_is_refused(name, text, words):
    with pytest.raises(FieldError) as e:
        quantity("U1", name, text)
    assert f'U1: {name} = "{text}"' in str(e.value) and words in str(e.value)


def test_one_part_on_several_rails_by_position():
    """A regulator: a load on its input rail, the source of its output rail; an empty value
    skips that rail, an empty field is not set."""
    rails = field_rails({"U2": {"PI_RAIL": "VIN;+3V3", "PI_LOAD": "0.8A;", "PI_SOURCE": ";3.3V",
                                "PI_PINS": ";2", "PI_LIMIT_DROP": ""}})
    [(load, fv, pins)] = rails["VIN"].loads
    assert (load.ref, load.current, load.pins, fv.text, pins) == ("U2", 0.8, None, "0.8A", None)
    [(src, _, pins)] = rails["+3V3"].sources
    assert (src.voltage, src.pins, pins.text) == (3.3, ["2"], "2")
    assert rails["VIN"].limit is None


@pytest.mark.parametrize("fields, words", [
    ({"PI_RAIL": "A;B", "PI_LOAD": "1A"}, "has 1 value(s) for 2 rail(s)"),
    ({"PI_LOAD": "1A"}, "PI_LOAD without PI_RAIL"),
    ({"PI_RAIL": "VCC"}, "no PI_SOURCE or PI_LOAD"),
    ({"PI_RAIL": "VCC", "PI_LOADS": "1A"}, "unknown field PI_LOADS"),
    ({"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_SOURCE": "1V"}, "both PI_SOURCE and PI_LOAD"),
    ({"PI_RAIL": "A;B", "PI_LOAD": "1A;"}, "rail B (PI_RAIL position 2) has neither"),
    ({"PI_RAIL": "A;", "PI_LOAD": "1A;1A"}, "empty net name")])
def test_fields_that_cannot_be_used_name_the_part_and_the_field(fields, words):
    with pytest.raises(FieldError) as e:
        field_rails({"U7": fields})
    assert str(e.value).startswith("U7: ") and words in str(e.value)


def test_the_strictest_drop_limit_wins_and_says_where_it_is_from():
    rails = field_rails({"J1": {"PI_RAIL": "V", "PI_SOURCE": "1V", "PI_LIMIT_DROP": "3%"},
                         "U1": {"PI_RAIL": "V", "PI_LOAD": "1A", "PI_LIMIT_DROP": "2%"},
                         "U2": {"PI_RAIL": "V", "PI_LOAD": "1A", "PI_LIMIT_DROP": "2.5%"}})
    assert rails["V"].rail().limits.max_drop_pct == 2.0
    assert 'limit max_drop_pct 2 (from KiCad field PI_LIMIT_DROP "2%" on U1)' in rails["V"].note()


def test_merge_never_changes_the_loaded_project():
    project = Project(board="b.kicad_pcb", rails=[Rail("OTHER", [Source("J9", 5.0)], [Load("U9", 1.0)])])
    merged = merge(project, FIELDS)
    assert [r.net for r in project.rails] == ["OTHER"]
    assert [r.net for r in merged.project.rails] == ["OTHER", "VCC"] and merged.from_fields == ["VCC"]
    with pytest.raises(FieldError) as e:
        merge(project, {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    assert "no part on this net has PI_SOURCE" in str(e.value)
    # A project rail on that net replaces the incomplete fields: no error.
    merge(Project(rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])]),
          {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})


# ---------------------------------------------------------------------- the reader

def test_reader_returns_only_pi_fields_of_parts_that_have_them(board, tmp_path):
    fields = read_part_fields(board)
    assert fields == FIELDS                                   # Reference and Value left out
    write_strip_board(tmp_path / "plain.kicad_pcb", {})
    assert read_part_fields(tmp_path / "plain.kicad_pcb") == {}
    with pytest.raises(EngineError, match=r"\.kicad_pcb\) only"):
        read_part_fields(tmp_path / "strip.kipi.json.gz")
    dup = write_strip_board(tmp_path / "dup.kicad_pcb", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}},
                            extra_parts=[("U1", 20.0, 3.0, "VCC")])
    with pytest.raises(EngineError, match="two footprints are 'U1'"):
        read_part_fields(dup)


# ---------------------------------------------------------------------- solving

def test_a_project_with_only_the_board_takes_its_rails_from_the_fields(board, tmp_path):
    before = (board.read_bytes(), os.stat(board).st_mtime_ns)
    project = project_file(tmp_path)
    project_bytes = project.read_bytes()
    doc = Session(out_dir=tmp_path / "out").solve_project(project)
    [rail] = doc["rails"]
    assert rail["net"] == "VCC" and rail["from_board_fields"] is True
    assert rail["loads"][0]["drop_mv"] == pytest.approx(DROP_MV_PER_A, rel=1e-5)
    assert rail["limits"] == {"max_drop_pct": 2.0} and rail["verdict"] == "fail"   # 3.8 % > 2 %
    [note] = [a for a in rail["assumptions"] if a.startswith("Rail VCC from KiCad footprint fields")]
    for name in ("PI_RAIL", "PI_SOURCE", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP"):
        assert f"from KiCad field {name}" in note
    assert 'load U1 1 A (from KiCad field PI_LOAD "1000mA" on U1)' in note
    assert (board.read_bytes(), os.stat(board).st_mtime_ns) == before          # only read
    assert project.read_bytes() == project_bytes                               # never saved


def test_a_project_rail_replaces_the_fields_rail_entirely(board, tmp_path):
    own = Rail("VCC", sources=[Source("J1", 1.0)], loads=[Load("U1", 0.5)])     # no limit
    [rail] = Session(out_dir=tmp_path / "out").solve_project(project_file(tmp_path, [own]))["rails"]
    assert rail["from_board_fields"] is False
    assert rail["loads"][0]["drop_mv"] == pytest.approx(DROP_MV_PER_A / 2, rel=1e-5)
    assert rail["limits"] == {} and rail["verdict"] == "not_judged"            # PI_LIMIT_DROP unused
    assert any("from the project file; the KiCad footprint fields on this net (J1, U1) are not used"
               in a for a in rail["assumptions"])


def test_fields_are_off_unless_asked(board, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_project(project_file(tmp_path, use=False))
    assert "No rails to solve" in e.value.message and "use_board_fields: true" in e.value.hint
    doc = session.solve_project(project_file(tmp_path, use=False), use_board_fields=True, rails=["VCC"])
    assert doc["rails"][0]["from_board_fields"] is True


def test_missing_voltage_and_other_formats_are_errors(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    write_strip_board(tmp_path / "strip.kicad_pcb", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    with pytest.raises(ServiceError) as e:
        session.solve_project(project_file(tmp_path))
    assert e.value.code == "invalid_board_field" and "PI_SOURCE" in e.value.message
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    snap = Project(board="strip.kipi.json.gz", use_board_fields=True).save(tmp_path / "snap.coppernicus.yml")
    with pytest.raises(ServiceError) as e:
        session.solve_project(snap)
    assert e.value.code == "invalid_input" and ".kicad_pcb" in e.value.message


def test_the_project_key_round_trips(tmp_path):
    path = Project(board="b.kicad_pcb", use_board_fields=True).save(tmp_path / "p.coppernicus.yml")
    assert "use_board_fields: true" in path.read_text(encoding="utf-8")
    assert Project.load(path).use_board_fields is True
    off = Project(board="b.kicad_pcb").save(tmp_path / "q.coppernicus.yml")
    assert "use_board_fields" not in off.read_text(encoding="utf-8")
    path.write_text("board: b.kicad_pcb\nuse_board_fields: yes please\n", encoding="utf-8")
    with pytest.raises(Exception, match="use_board_fields must be true or false"):
        Project.load(path)


# ---------------------------------------------------------------------- CLI, MCP, CI

def test_cli_fields_and_solve_with_board_fields(board, tmp_path, capsys):
    project = project_file(tmp_path, use=False)
    assert main(["solve", str(project), "--no-maps", "--out", str(tmp_path / "o"), "--json",
                 "--board-fields"]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    assert doc["rails"][0]["from_board_fields"] is True
    assert main(["solve", str(project), "--no-maps", "--out", str(tmp_path / "o"), "--html",
                 "--board-fields"]) == EXIT_LIMIT
    out = capsys.readouterr().out
    assert "assumption: Rail VCC from KiCad footprint fields" in out
    report = (tmp_path / "o" / "fields.coppernicus_report.html").read_text(encoding="utf-8")
    assert "from KiCad field PI_LIMIT_DROP" in report
    assert main(["fields", str(board), "--json"]) == EXIT_OK
    listed = json.loads(capsys.readouterr().out)
    assert [p["ref"] for p in listed["fields"]] == ["J1", "U1"]
    [rail] = listed["rails"]
    assert rail["complete"] and rail["loads"][0]["from"] == 'from KiCad field PI_LOAD "1000mA" on U1'
    assert main(["fields", str(board)]) == EXIT_OK
    assert "Rail VCC" in capsys.readouterr().out
    assert main(["solve", str(project), "--tree", "--board-fields"]) == EXIT_ERROR
    assert "--board-fields does not apply to --tree" in capsys.readouterr().err


def test_mcp_board_fields_and_solve_project(board, tmp_path):
    project = project_file(tmp_path, use=False)

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            bid = (await c.call_tool("open_board", {"path": str(board)})).structured_content["board_id"]
            listed = await c.call_tool("board_fields", {"board_id": bid})
            solved = await c.call_tool("solve_project", {"project_path": str(project),
                                                         "use_board_fields": True})
            return listed.structured_content, solved.structured_content
    listed, solved = anyio.run(scenario)
    assert listed["rails"][0]["sources"][0]["from"] == 'from KiCad field PI_SOURCE "1V" on J1'
    assert solved["rails"][0]["from_board_fields"] is True and solved["verdict"] == "fail"


def test_the_action_needs_only_the_project_key(board, tmp_path, monkeypatch):
    project_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["fields.coppernicus.yml"], Path("results"), html=False)
    assert index["exit_code"] == 1 and index["verdict"] == "fail"
    md = ci.summary_markdown(index)
    assert "| VCC |" in md and "from KiCad field PI_SOURCE" in md


def test_solve_tree_takes_the_fields_too(board, tmp_path):
    doc = Session(out_dir=tmp_path / "out").solve_tree(project_file(tmp_path))
    assert [g["group"] for g in doc["groups"]] == ["VCC"]
    assert any(a.startswith("Rail VCC from KiCad footprint fields") for a in doc["assumptions"])


@pytest.mark.parametrize("fields, extra, code, words", [
    ({"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_PINS": "9"}}, (), "pin_not_on_net", "no pin '9'"),
    ({"U2": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}, [("U2", 20.0, 3.0, "GND")], "part_not_on_net",
     "U2 has no pads on net VCC"),
    ({"J9": {"PI_RAIL": "NOPE", "PI_SOURCE": "1V"}, "U9": {"PI_RAIL": "NOPE", "PI_LOAD": "1A"}},
     [("J9", 10.0, 3.0, "VCC"), ("U9", 30.0, 3.0, "VCC")],
     "net_not_found", "NOPE")])
def test_errors_found_while_solving_a_field_rail_say_where_it_came_from(tmp_path, fields, extra, code,
                                                                        words):
    write_strip_board(tmp_path / "strip.kicad_pcb", {**FIELDS, **fields}, extra_parts=extra)
    with pytest.raises(ServiceError) as e:
        Session(out_dir=tmp_path / "out").solve_project(project_file(tmp_path), stop_on_error=True)
    assert e.value.code == code and words in e.value.message
    assert "from KiCad footprint fields: check PI_RAIL and PI_PINS on" in e.value.message


def test_a_missing_current_and_a_lower_case_name_are_errors(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    write_strip_board(tmp_path / "strip.kicad_pcb", {"J1": FIELDS["J1"]})
    with pytest.raises(ServiceError) as e:
        session.solve_project(project_file(tmp_path))
    assert e.value.code == "invalid_board_field" and "PI_LOAD" in e.value.message
    lower = {"J1": FIELDS["J1"], "U1": {"PI_RAIL": "VCC", "pi_load": "1A"}}
    write_strip_board(tmp_path / "strip.kicad_pcb", lower)
    assert read_part_fields(tmp_path / "strip.kicad_pcb")["U1"] == lower["U1"]
    with pytest.raises(ServiceError) as e:
        session.solve_project(project_file(tmp_path))
    assert "unknown field pi_load" in e.value.message and "upper case" in e.value.message
