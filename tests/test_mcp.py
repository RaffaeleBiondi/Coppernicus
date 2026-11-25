"""The MCP server through the SDK's own client: in memory (no subprocess), and the brief's
acceptance test, an agent's analysis of the demo board done with the tools alone."""

import base64
import json
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

from coppernicus.mcp_server import build_server
from coppernicus.service import Session

DEMO_BOARD = Path(__file__).resolve().parents[1] / "examples" / "demo" / "demo.kipi.json.gz"
PNG = b"\x89PNG\r\n\x1a\n"
TOOLS = {"open_board", "list_nets", "net_parts", "solve_rail", "get_problems", "render_map", "probe",
         "cut_current", "compare_results", "solve_project", "solve_tree", "report",
         "board_fields", "read_project", "write_project", "propose_fields", "copper_step"}


def run_with_client(tmp_path, scenario):
    """Run ``await scenario(client)`` against a fresh server and session."""
    async def main():
        server = build_server(Session(out_dir=tmp_path / "maps"))
        async with Client(server, raise_exceptions=True) as client:
            return await scenario(client)
    return anyio.run(main)


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def image_bytes(result) -> bytes:
    [img] = [b for b in result.content if b.type == "image"]
    assert img.mime_type == "image/png"
    return base64.b64decode(img.data)


def test_tools_are_listed_with_schemas(tmp_path):
    async def scenario(client):
        return (await client.list_tools()).tools
    tools = {t.name: t for t in run_with_client(tmp_path, scenario)}
    assert set(tools) == TOOLS
    assert all(t.description for t in tools.values())
    schema = json.dumps(tools["solve_rail"].input_schema)
    assert "voltage_v" in schema and "current_a" in schema and "max_drop_pct" in schema
    assert "ctx" not in tools["solve_rail"].input_schema["properties"]
    assert tools["probe"].annotations.read_only_hint is True


def test_round_trip_with_progress_image_and_structured_errors(tmp_path):
    async def scenario(client):
        board = await call(client, "open_board", path=str(DEMO_BOARD))
        progress = []

        async def on_progress(value, total, message):
            progress.append((value, message))
        solved = await client.call_tool("solve_rail", {
            "board_id": board["board_id"], "net": "+3V3",
            "sources": [{"ref": "J1", "voltage_v": 3.3}],
            "loads": [{"ref": "U1", "current_a": 2.0}, {"ref": "U2", "current_a": 1.5}]},
            progress_callback=on_progress)
        rid = solved.structured_content["result_id"]
        picture = await client.call_tool("render_map", {"result_id": rid, "field": "current"})
        bad_part = await client.call_tool("solve_rail", {
            "board_id": board["board_id"], "net": "+3V3", "sources": [{"ref": "J1", "voltage_v": 3.3}],
            "loads": [{"ref": "U9", "current_a": 1.0}]})
        bad_args = await client.call_tool("probe", {"result_id": rid, "layer": "B.Cu", "x_mm": 1.0,
                                                    "y_mm": "here"})
        gone = await client.call_tool("get_problems", {"result_id": "r42"})
        return solved, progress, picture, bad_part, bad_args, gone

    solved, progress, picture, bad_part, bad_args, gone = run_with_client(tmp_path, scenario)
    summary = solved.structured_content
    assert summary["verdict"] == "not_judged" and summary["busiest_layer"] == "B.Cu"
    assert json.loads(solved.content[0].text)["result_id"] == summary["result_id"]   # same data as text
    values = [v for v, _ in progress]
    assert values and values == sorted(set(values))                # strictly increasing
    assert any("Meshing" in m for _, m in progress)

    assert image_bytes(picture)[:8] == PNG
    meta = picture.structured_content
    assert meta["layer"] == "B.Cu" and Path(meta["path"]).read_bytes()[:8] == PNG
    assert Path(meta["path"]).parent == tmp_path / "maps"

    assert bad_part.is_error
    err = json.loads(bad_part.content[0].text)["error"]
    assert err == {"code": "part_not_on_net", "message": "U9 has no pads on net +3V3",
                   "hint": "parts on +3V3: J1, U1, U2, U3"}
    assert bad_part.structured_content == {"error": err}
    assert bad_args.is_error and "y_mm" in bad_args.content[0].text        # rejected by the schema
    assert json.loads(gone.content[0].text)["error"]["code"] == "result_not_found"


def test_acceptance_an_agent_reproduces_the_demo_analysis(tmp_path):
    """docs/agent-interface-brief.md §5: with these tools only, on examples/demo, find the power
    net and its parts, solve with the three loads, list the problems, render the drop and
    current maps of the busiest layer, measure a cut through the neck beside the slot, and
    conclude whether the 0.25 % limit holds. The user supplied the load currents and limit."""
    user_currents = {"U1": 2.0, "U2": 1.5, "U3": 0.3}

    async def scenario(client):
        board = await call(client, "open_board", path=str(DEMO_BOARD))
        bid = board["board_id"]
        nets = await call(client, "list_nets", board_id=bid, kind="power")
        net = nets["nets"][0]["name"]
        parts = await call(client, "net_parts", board_id=bid, net=net)
        supply = [p["ref"] for p in parts["parts"] if p["value"] == "SUPPLY"]
        loads = [p["ref"] for p in parts["parts"] if p["ref"] in user_currents]
        summary = await call(client, "solve_rail", board_id=bid, net=net,
                             sources=[{"ref": supply[0], "voltage_v": 3.3}],
                             loads=[{"ref": r, "current_a": user_currents[r]} for r in loads],
                             limits={"max_drop_pct": 0.25})
        rid, busiest = summary["result_id"], summary["busiest_layer"]
        problems = await call(client, "get_problems", result_id=rid, top=10)
        maps = [await client.call_tool("render_map", {"result_id": rid, "layer": busiest, "field": f})
                for f in ("drop", "current")]
        # The neck: the broadest current-crowding spot on the busiest layer. Cut straight across
        # the board there, top to bottom.
        spots = [p for p in problems["problems"] if p["kind"] == "hotspot" and p["layer"] == busiest]
        neck = max(spots, key=lambda p: p["area_mm2"])
        x0, y0, x1, y1 = board["outline_bbox_mm"]
        cut = await call(client, "cut_current", result_id=rid, layer=busiest,
                         x1_mm=neck["x_mm"], y1_mm=y0, x2_mm=neck["x_mm"], y2_mm=y1)
        return net, supply, loads, summary, problems, maps, neck, cut

    net, supply, loads, summary, problems, maps, neck, cut = run_with_client(tmp_path, scenario)
    assert net == "+3V3" and supply == ["J1"] and loads == ["U1", "U2", "U3"]
    assert summary["busiest_layer"] == "B.Cu"
    assert problems["problems"][0]["kind"] == "load"
    for m in maps:
        assert image_bytes(m)[:8] == PNG
    assert 28.0 < neck["x_mm"] < 31.0                                # beside the slot
    assert cut["width_mm"] == pytest.approx(7.0, abs=0.01)           # y 31–38 on B.Cu
    beyond = sum(ld["current_a"] for ld in summary["loads"] if ld["x_mm"] > neck["x_mm"])
    assert beyond == pytest.approx(3.5)
    assert abs(cut["current_a"]) == pytest.approx(beyond, rel=0.01)   # all of it through the neck
    # Conclusion: the 0.25 % budget does not hold, for U1 and U2.
    assert summary["verdict"] == "fail"
    assert {ld["name"] for ld in summary["loads"] if ld["exceeds_drop_limit"]} == {"U1", "U2"}
    assert summary["limits"] == {"max_drop_pct": 0.25}


def test_stdio_subprocess_round_trip(tmp_path):
    """``python -m coppernicus mcp`` as a real child process speaking over stdio: anything
    other than protocol messages on its stdout would break the client."""
    log = tmp_path / "server.log"
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "coppernicus", "mcp", "--out", str(tmp_path / "maps"), "--log-file", str(log)])
    progress = []

    async def main():
        async with Client(params) as client:
            board = await call(client, "open_board", path=str(DEMO_BOARD))
            nets = await call(client, "list_nets", board_id=board["board_id"])

            async def on_progress(value, total, message):
                progress.append(message)
            solved = await client.call_tool("solve_rail", {
                "board_id": board["board_id"], "net": nets["nets"][0]["name"],
                "sources": [{"ref": "J1", "voltage_v": 3.3}],
                "loads": [{"ref": "U1", "current_a": 2.0}]}, progress_callback=on_progress)
            picture = await client.call_tool("render_map", {
                "result_id": solved.structured_content["result_id"]})
            return solved, picture

    solved, picture = anyio.run(main)
    assert not solved.is_error and solved.structured_content["loads"][0]["drop_mv"] > 0
    assert image_bytes(picture)[:8] == PNG
    map_path = Path(picture.structured_content["path"])
    assert map_path.exists() and map_path.parent.parent == (tmp_path / "maps").resolve()
    assert map_path.parent.name.startswith("session-")             # one folder per server run
    assert "MCP server on stdio" in log.read_text(encoding="utf-8")
    assert progress                     # delivered beside the response; may still be arriving


def test_report_tool_writes_one_offline_page(tmp_path):
    async def scenario(client):
        board = await call(client, "open_board", path=str(DEMO_BOARD))
        args = {"board_id": board["board_id"], "net": "+3V3", "sources": [{"ref": "J1", "voltage_v": 3.3}],
                "loads": [{"ref": "U1", "current_a": 2.0}], "limits": {"max_drop_pct": 0.25}}
        a = await call(client, "solve_rail", **args)
        b = await call(client, "solve_rail", **args, settings={"copper_thickness_mm": {"B.Cu": 0.07}})
        both = await call(client, "report")
        one = await call(client, "report", result_ids=[b["result_id"]], path=str(tmp_path / "b.html"))
        gone = await client.call_tool("report", {"result_ids": ["r9"]})
        return a, b, both, one, gone

    a, b, both, one, gone = run_with_client(tmp_path, scenario)
    assert [r["result_id"] for r in both["rails"]] == [a["result_id"], b["result_id"]]
    page = Path(both["path"])
    assert page.parent == tmp_path / "maps" and page.suffix == ".html"
    text = page.read_text(encoding="utf-8")
    assert "copper_thickness_mm = B.Cu 0.07 mm" in text and "https://" not in text
    assert Path(one["path"]) == tmp_path / "b.html" and one["size_mb"] > 0
    assert json.loads(gone.content[0].text)["error"]["code"] == "result_not_found"
