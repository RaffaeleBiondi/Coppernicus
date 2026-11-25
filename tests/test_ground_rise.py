"""The ground-rise limit (``max_ground_rise_mv``) through every path that judges a single
rail: the project file (solve_project), MCP solve_rail and get_problems, the HTML report
and the CI job summary. A strip held at 0 V at one end, with 1 A drawn at the other, is a
ground net solved on its own: its far end rises ρL/(wt) · 1 A ≈ 38 mV."""

import json

import anyio
import pytest
from mcp import Client

from coppernicus import ci
from coppernicus.engine.synthetic import OZ1, Strip, save_snapshot, strip_board
from coppernicus.mcp_server import build_server
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

RHO = 1.724e-5                                           # Ω·mm, the engine's ρ at 20 °C
RISE_MV = RHO * Strip.length() / (Strip.W * OZ1) * 1e3   # ≈ 37.9 mV at 1 A


@pytest.fixture()
def strip(tmp_path):
    path = tmp_path / "boards" / "strip.kipi.json.gz"
    path.parent.mkdir()
    save_snapshot(strip_board(), path)
    return path


def ground_project(tmp_path, limit_mv):
    p = Project(board="boards/strip.kipi.json.gz", rails=[Rail(
        Strip.NET, sources=[Source("J1", 0.0)], loads=[Load("U1", 1.0)],
        limits=Limits(max_ground_rise_mv=limit_mv))])
    return p.save(tmp_path / f"ground-{limit_mv:g}.coppernicus.yml")


@pytest.mark.parametrize("limit_mv, verdict", [(10.0, "fail"), (50.0, "pass")])
def test_project_ground_rise_limit_is_judged_and_named(strip, tmp_path, limit_mv, verdict):
    [rail] = Session(out_dir=tmp_path / "out").solve_project(ground_project(tmp_path, limit_mv))["rails"]
    assert rail["verdict"] == verdict
    assert rail["limits"] == {"max_ground_rise_mv": limit_mv}
    assert rail["verdict_note"] == ("Judged only against the limits you set: "
                                    f"max_ground_rise_mv = {limit_mv:g}")
    [rise] = [p for p in rail["problems"] if p["kind"] == "rise"]
    assert rise["rise_mv"] == pytest.approx(RISE_MV, rel=0.02)
    assert rise["severity"] == ("fail" if verdict == "fail" else "info")


def test_solve_rail_and_get_problems_take_the_ground_rise_limit(strip, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(strip)["board_id"]
    out = session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": 0.0}],
                             [{"ref": "U1", "current_a": 1.0}], limits={"max_ground_rise_mv": 10})
    assert out["verdict"] == "fail" and out["limits"] == {"max_ground_rise_mv": 10}
    again = session.get_problems(out["result_id"], limits={"max_ground_rise_mv": 50})
    assert again["verdict"] == "pass" and again["limits"] == {"max_ground_rise_mv": 50}
    assert any("max_ground_rise_mv" in a for a in again["assumptions"])
    with pytest.raises(ServiceError) as e:
        session.get_problems(out["result_id"], limits={"max_ground_rise_mv": -1})
    assert e.value.code == "invalid_input"


def test_mcp_limits_schema_carries_the_ground_rise_limit(strip, tmp_path):
    async def main():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert "max_ground_rise_mv" in json.dumps(tools["solve_rail"].input_schema)
            bid = (await c.call_tool("open_board", {"path": str(strip)})).structured_content["board_id"]
            res = await c.call_tool("solve_rail", {
                "board_id": bid, "net": Strip.NET, "sources": [{"ref": "J1", "voltage_v": 0.0}],
                "loads": [{"ref": "U1", "current_a": 1.0}], "limits": {"max_ground_rise_mv": 10}})
            assert not res.is_error, res.content[0].text
            got = await c.call_tool("get_problems", {"result_id": res.structured_content["result_id"],
                                                     "limits": {"max_ground_rise_mv": 50}})
            return res.structured_content, got.structured_content
    solved, rejudged = anyio.run(main)
    assert solved["verdict"] == "fail" and solved["limits"] == {"max_ground_rise_mv": 10}
    assert rejudged["verdict"] == "pass"


def test_report_and_ci_summary_name_the_ground_rise_limit(strip, tmp_path):
    from coppernicus.cli import EXIT_LIMIT, main
    project = ground_project(tmp_path, 10.0)
    out = tmp_path / "out"
    assert main(["solve", str(project), "--no-maps", "--out", str(out), "--json", "--html"]) == EXIT_LIMIT
    doc = json.loads((out / "ground-10.coppernicus_summary.json").read_text(encoding="utf-8"))
    html = (out / "ground-10.coppernicus_report.html").read_text(encoding="utf-8")
    assert '"limits":{"max_ground_rise_mv":10.0}' in html and '"verdict":"fail"' in html
    md = ci.summary_markdown({"verdict": "fail", "projects": [
        {"project": "ground.coppernicus.yml", "exit_code": 1, "doc": doc}]})
    assert "**FAIL** (ground rise ≤ 10 mV)" in md
    assert "largest ground" in md          # "rise" or, below 0 V, "shift" (the result's own words)
