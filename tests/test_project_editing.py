"""Project editing for agents: Session.read_project / write_project and the MCP tools.
An explicit output path, no overwrite unless asked, unknown keys kept, validated by
Project.load before the file is replaced, the board never touched, and a warning when the
KiCad fields win over the rail being edited."""

import os
from pathlib import Path

import anyio
import pytest
import yaml
from kicad_fixture import write_strip_board
from mcp import Client

from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board
from coppernicus.mcp_server import build_server
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

RAIL = {"net": Strip.NET, "sources": [{"ref": "J1", "voltage_v": 1.0}],
        "loads": [{"ref": "U1", "current_a": 0.5, "pins": ["1"]}], "limits": {"max_drop_pct": 2.0}}


@pytest.fixture()
def board(tmp_path):
    path = tmp_path / "boards" / "strip.kipi.json.gz"
    path.parent.mkdir()
    save_snapshot(strip_board(), path)
    return path


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


def text(path):
    return Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_create_a_project_for_a_board_and_add_a_rail(session, board, tmp_path):
    before = (board.read_bytes(), os.stat(board).st_mtime_ns)
    path = tmp_path / "new.coppernicus.yml"
    out = session.write_project(path, board_path=board, rails=[RAIL])
    assert Path(out["path"]) == path and out["rails"] == [Strip.NET]
    project = Project.load(path)
    assert project.board == "boards/strip.kipi.json.gz"
    assert project.rails == [Rail(Strip.NET, [Source("J1", 1.0)], [Load("U1", 0.5, ["1"])],
                                  limits=Limits(max_drop_pct=2.0))]
    assert (board.read_bytes(), os.stat(board).st_mtime_ns) == before          # never touched
    assert any("comments" in a for a in out["assumptions"])
    read = session.read_project(path)
    assert read["project"]["rails"][0]["net"] == Strip.NET and Path(read["board_path"]) == board


def test_no_overwrite_unless_asked_and_only_project_files(session, board, tmp_path):
    path = tmp_path / "p.coppernicus.yml"
    session.write_project(path, board_path=board)
    with pytest.raises(ServiceError) as e:
        session.write_project(path, board_path=board)
    assert e.value.code == "file_exists" and "overwrite" in e.value.hint
    session.write_project(path, rails=[RAIL], overwrite=True)
    assert Project.load(path).rails[0].net == Strip.NET
    for bad in (tmp_path / "p.yml", board):
        with pytest.raises(ServiceError) as e:
            session.write_project(bad, board_path=board, overwrite=True)
        assert e.value.code == "invalid_input" and ".coppernicus.yml" in e.value.message


def test_replace_keeps_unknown_keys_and_a_refused_edit_changes_nothing(session, board, tmp_path):
    path = tmp_path / "p.coppernicus.yml"
    session.write_project(path, board_path=board, rails=[RAIL, dict(RAIL, net="OTHER")])
    data = yaml.safe_load(text(path))
    data["x_team_note"] = "keep me"
    data["rails"][0]["x_reviewed"] = True
    data["rails"][1]["loads"][0]["x_tag"] = "U1 tag"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    session.write_project(path, rails=[dict(RAIL, loads=[{"ref": "U1", "current_a": 0.7}])],
                          overwrite=True)
    data = yaml.safe_load(text(path))
    assert data["x_team_note"] == "keep me"
    assert data["rails"][0]["x_reviewed"] is True and data["rails"][0]["loads"][0]["current"] == 0.7
    assert data["rails"][1]["loads"][0]["x_tag"] == "U1 tag"
    before = path.read_bytes()
    smps = dict(RAIL, sources=[{"ref": "J1", "voltage_v": 1.0, "kind": "smps", "efficiency": 90,
                                "input": {"net": "VIN"}}])
    with pytest.raises(ServiceError) as e:
        session.write_project(path, rails=[smps], overwrite=True)
    assert e.value.code == "invalid_project" and "efficiency" in e.value.message
    assert path.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".") or p.suffix == ".tmp"]


def test_remove_rails_links_settings_and_the_fields_switch(session, board, tmp_path):
    path = tmp_path / "p.coppernicus.yml"
    link = {"name": "FB1", "ref": "FB1", "a": {"net": Strip.NET}, "b": {"net": "VCC_F", "pins": ["2"]},
            "resistance_ohm": 0.05}
    session.write_project(path, board_path=board, rails=[RAIL], links=[link],
                          settings={"max_edge_mm": 0.3, "copper_thickness_mm": {"F.Cu": 0.07}},
                          use_board_fields=False)
    p = Project.load(path)
    assert p.links[0].resistance_ohm == 0.05 and p.links[0].pins_b == ["2"]
    assert p.settings.max_edge_mm == 0.3 and p.settings.copper_thickness_mm == {"F.Cu": 0.07}
    assert p.use_board_fields is False
    session.write_project(path, remove_rails=[Strip.NET], remove_links=["FB1"], use_board_fields=True,
                          settings={"copper_thickness_mm": None}, overwrite=True)
    p = Project.load(path)
    assert p.rails == [] and p.links == [] and p.use_board_fields is True
    assert p.settings.copper_thickness_mm == {} and "use_board_fields" not in text(path)
    with pytest.raises(ServiceError) as e:
        session.write_project(path, remove_rails=["NOPE"], overwrite=True)
    assert e.value.code == "invalid_input" and "NOPE" in e.value.message
    with pytest.raises(ServiceError) as e:
        session.write_project(path, settings={"max_edge": 1}, overwrite=True)
    assert e.value.code == "invalid_input"


def test_saving_elsewhere_rebases_the_board_path(session, board, tmp_path):
    first = tmp_path / "a" / "p.coppernicus.yml"
    first.parent.mkdir()
    session.write_project(first, board_path=board, rails=[RAIL])
    other = tmp_path / "b" / "c" / "q.coppernicus.yml"
    other.parent.mkdir(parents=True)
    session.write_project(other, from_project=first)
    assert Project.load(other).board_path().resolve() == board.resolve()
    assert Project.load(other).board == "../../boards/strip.kipi.json.gz"


def test_the_fields_win_warning_and_the_override(session, tmp_path):
    fields = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"}, "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}
    kicad = write_strip_board(tmp_path / "strip.kicad_pcb", fields)
    path = tmp_path / "f.coppernicus.yml"
    out = session.write_project(path, board_path=kicad, rails=[dict(RAIL, net="VCC")])
    assert any("project file rail VCC ignored" in w and "override_board_fields: true" in w
               for w in out["warnings"])
    out = session.write_project(path, rails=[dict(RAIL, net="VCC", override_board_fields=True)],
                                overwrite=True)
    assert not any("ignored" in w for w in out["warnings"])
    assert Project.load(path).rails[0].override_board_fields is True
    same = session.write_project(path, rails=[{"net": "VCC", "sources": [{"ref": "J1", "voltage_v": 1.0}],
                                              "loads": [{"ref": "U1", "current_a": 1.0}]}], overwrite=True)
    assert same["warnings"] == []                       # the fields' own rail: nothing to warn
    write_strip_board(kicad, {"U1": {"PI_RAIL": "VCC", "pi_load": "1A"}})       # a malformed field
    os.utime(kicad, ns=(os.stat(kicad).st_atime_ns, os.stat(kicad).st_mtime_ns + 10 ** 9))
    out = session.write_project(path, rails=[dict(RAIL, net="VCC")], overwrite=True)
    assert any("pi_load" in w for w in out["warnings"])                       # a warning, not an error


def test_mcp_read_and_write_project(board, tmp_path):
    path = tmp_path / "m.coppernicus.yml"

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            made = await c.call_tool("write_project", {"project_path": str(path), "board_path": str(board),
                                                       "rails": [RAIL]})
            again = await c.call_tool("write_project", {"project_path": str(path), "board_path": str(board)})
            read = await c.call_tool("read_project", {"project_path": str(path)})
            return tools, made, again, read
    tools, made, again, read = anyio.run(scenario)
    assert tools["write_project"].annotations.destructive_hint is True
    assert tools["read_project"].annotations.read_only_hint is True
    assert not made.is_error and made.structured_content["rails"] == [Strip.NET]
    assert again.is_error and again.structured_content["error"]["code"] == "file_exists"
    assert read.structured_content["project"]["rails"][0]["loads"][0]["current"] == 0.5


def test_a_misspelt_limit_is_refused_not_dropped(board, tmp_path):
    path = tmp_path / "typo.coppernicus.yml"

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            return await c.call_tool("write_project", {"project_path": str(path), "board_path": str(board),
                                                       "rails": [dict(RAIL, limits={"max_drop": 2})]})
    out = anyio.run(scenario)
    assert out.is_error and not path.exists()


def test_read_project_keeps_the_numbers_as_written(session, board, tmp_path):
    path = tmp_path / "exact.coppernicus.yml"
    session.write_project(path, board_path=board,
                          rails=[dict(RAIL, loads=[{"ref": "U1", "current_a": 1.23456789}])])
    assert session.read_project(path)["project"]["rails"][0]["loads"][0]["current"] == 1.23456789
