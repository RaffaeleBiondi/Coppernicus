"""A project rail solved through the service (solve_project: the CLI, --json, --html, the
MCP solve_project tool and the Action) also draws the regulator inputs and links it feeds,
estimated at nominal voltages, exactly as the GUI solves it (engine.solve_rail with the
project). The estimated draws are listed as loads with ``"estimated": true``. MCP solve_rail
has no project and draws nothing extra."""

import json
import re
import shutil
from pathlib import Path

import pytest

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board
from coppernicus.project import Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session
from coppernicus.service.session import compare_summaries

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"


@pytest.fixture()
def chain(tmp_path):
    shutil.copytree(CHAIN, tmp_path / "chain")
    return tmp_path / "chain" / "power_chain.coppernicus.yml"


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


def rails_by_net(doc):
    return {r["net"]: r for r in doc["rails"]}


def test_project_rails_draw_what_they_feed_as_the_gui_does(session, chain):
    doc = session.solve_project(chain)
    rails = rails_by_net(doc)
    assert not any("skipped" in r for r in doc["rails"])              # VIN feeds U1: solved
    project = Project.load(chain)
    board = Board.open(project.board_path())
    for rail in project.rails:
        gui = solve_rail(board, rail, project.settings, project=project)
        got = rails[rail.net]
        assert [ld["name"] for ld in got["loads"]] == [t.name for t in gui.loads]
        for ld, t in zip(got["loads"], gui.loads):
            assert ld["current_a"] == pytest.approx(t.current, rel=1e-6)
            assert ld["drop_mv"] == pytest.approx(gui.drop(t) * 1e3, rel=1e-6, abs=1e-9)
    vin = rails["VIN"]["loads"]
    assert [(ld["name"], ld["ref"], ld["pins"], ld["estimated"]) for ld in vin] == [
        ("U1 (input, estimated)", "U1", "1", True)]
    v33 = rails["+3V3"]["loads"]
    assert [(ld["name"], ld["estimated"]) for ld in v33] == [("U4", False), ("FB1 (link, estimated)", True)]
    assert any("estimated at nominal voltages" in a for a in rails["VIN"]["assumptions"])
    estimated_problems = [p for p in rails["+3V3"]["problems"] if p["kind"] == "load" and p.get("estimated")]
    assert [p["name"] for p in estimated_problems] == ["FB1 (link, estimated)"]


def test_a_rail_that_feeds_nothing_and_has_no_loads_is_still_skipped(session, tmp_path):
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    p = Project(board="strip.kipi.json.gz", rails=[Rail(Strip.NET, sources=[Source("J1", 1.0)])])
    doc = session.solve_project(p.save(tmp_path / "idle.coppernicus.yml"))
    [rail] = doc["rails"]
    assert rail["skipped"]["code"] == "missing_load"
    assert rail["skipped"]["message"] == f"{Strip.NET}: no loads, and it feeds no regulator input or link"


def test_an_estimate_that_cannot_be_made_is_that_rails_error(session, chain):
    project = Project.load(chain)
    project.rail("+3V3").sources[0].efficiency = None            # a switcher without its η
    project.save()
    rails = rails_by_net(session.solve_project(chain))
    assert rails["VIN"]["error"]["code"] == "solve_failed"
    assert "needs its efficiency" in rails["VIN"]["error"]["message"]


def test_mcp_solve_rail_has_no_project_and_draws_nothing_extra(session, chain):
    project = Project.load(chain)
    bid = session.open_board(project.board_path())["board_id"]
    with pytest.raises(ServiceError) as e:
        session.solve_rail(bid, "VIN", [{"ref": "J1", "voltage_v": 12.0, "pins": ["1"]}], [])
    assert e.value.code == "missing_load"
    out = session.solve_rail(bid, "+3V3", [{"ref": "U1", "voltage_v": 3.3, "pins": ["2"]}],
                             [{"ref": "U4", "current_a": 0.5, "pins": ["1"]}])
    assert [(ld["name"], ld["estimated"]) for ld in out["loads"]] == [("U4", False)]


def test_compare_warns_when_the_estimated_draws_differ(session, chain):
    rails = rails_by_net(session.solve_project(chain))
    bid = rails["+3V3"]["board_id"]
    alone = session.solve_rail(bid, "+3V3", [{"ref": "U1", "voltage_v": 3.3, "pins": ["2"]}],
                               [{"ref": "U4", "current_a": 0.5, "pins": ["1"]}])
    cmp_ = compare_summaries(rails["+3V3"], alone)
    [row] = [r for r in cmp_["loads"] if r["name"] == "FB1 (link, estimated)"]
    assert row["estimated"] is True and row["drop_mv_b"] is None
    assert any("estimated draws differ" in w and "FB1 (link, estimated)" in w for w in cmp_["warnings"])


def test_the_report_marks_estimated_loads(chain, tmp_path, capsys):
    from coppernicus.cli import EXIT_OK, main
    out = tmp_path / "out"
    assert main(["solve", str(chain), "--no-maps", "--out", str(out), "--html", "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    text = Path(doc["report_file"]).read_text(encoding="utf-8")
    meta = json.loads(re.search(r'id="meta">(.*?)</script>', text, re.S).group(1))
    loads = {ld["name"]: ld["estimated"] for r in meta["rails"] for ld in r["loads"]}
    assert loads["U1 (input, estimated)"] is True and loads["FB1 (link, estimated)"] is True
    assert loads["U4"] is False and loads["U3"] is False


def test_a_load_named_like_an_estimate_is_not_one(session, tmp_path):
    """Estimated draws are told apart by position (the engine appends them after the rail's
    own loads), not by their name."""
    save_snapshot(strip_board(), tmp_path / "strip.kipi.json.gz")
    p = Project(board="strip.kipi.json.gz", rails=[Rail(
        Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0, name="U9 (input, estimated)")])])
    [rail] = session.solve_project(p.save(tmp_path / "odd.coppernicus.yml"))["rails"]
    assert [(ld["name"], ld["estimated"]) for ld in rail["loads"]] == [("U9 (input, estimated)", False)]
