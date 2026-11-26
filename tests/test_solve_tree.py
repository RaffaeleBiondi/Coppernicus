"""The power-tree solve for agents: Session.solve_tree and the MCP solve_tree tool return
power_tree.tree_summary unchanged, the document `coppernicus solve --tree --json` prints
(without its summary_file), with errors in the usual {code, message, hint}."""

import json
import shutil
from pathlib import Path

import anyio
import pytest
from mcp import Client

from coppernicus.cli import EXIT_OK, main
from coppernicus.mcp_server import build_server
from coppernicus.project import Project
from coppernicus.service import ServiceError, Session

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"


@pytest.fixture()
def chain(tmp_path):
    shutil.copytree(CHAIN, tmp_path / "chain")
    return tmp_path / "chain" / "power_chain.coppernicus.yml"


def cli_tree(chain, tmp_path, capsys):
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "cli")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    doc.pop("summary_file")
    return doc


def without_ids(doc):
    """The session's tree document without what only the session adds: the tree's id and
    each group's result id."""
    doc = {k: v for k, v in doc.items() if k != "tree_id"}
    doc["groups"] = [{k: v for k, v in g.items() if k != "result_id"} for g in doc["groups"]]
    return doc


def test_solve_tree_is_the_cli_tree_document(chain, tmp_path, capsys):
    expected = cli_tree(chain, tmp_path, capsys)
    got = Session(out_dir=tmp_path / "out").solve_tree(chain)
    assert got["tree_id"] == "t1"
    assert [g["result_id"] for g in got["groups"]] == [f"t1:{g['group']}" for g in got["groups"]]
    assert without_ids(got) == expected
    assert got["kind"] == "power_tree" and {g["kind"] for g in got["groups"]} == {"rail", "ground"}


def test_mcp_solve_tree_tool(chain, tmp_path, capsys):
    expected = cli_tree(chain, tmp_path, capsys)

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            ok = await c.call_tool("solve_tree", {"project_path": str(chain)})
            none = str(tmp_path / "none.coppernicus.yml")
            missing = await c.call_tool("solve_tree", {"project_path": none})
            return tools["solve_tree"], ok, missing
    tool, ok, missing = anyio.run(scenario)
    assert tool.annotations.read_only_hint is True and "power tree" in tool.description
    assert not ok.is_error and without_ids(ok.structured_content) == expected
    assert ok.structured_content["tree_id"] == "t1"
    assert missing.is_error and missing.structured_content["error"]["code"] == "file_not_found"


def test_solve_tree_errors_are_structured(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    project = Project.load(chain)
    project.settings.copper_thickness_mm = {"In9.Cu": 0.07}
    project.save()
    with pytest.raises(ServiceError) as e:
        session.solve_tree(chain)
    assert e.value.code == "layer_not_found" and "copper layers:" in e.value.message
    project.settings.copper_thickness_mm = {}
    project.rail("+3V3").loads[0].ground_net = ""          # a load without its ground pins
    project.rail("+3V3").loads[0].ground_pins = None
    project.save()
    with pytest.raises(ServiceError) as e:
        session.solve_tree(chain)
    assert e.value.code == "solve_failed" and "U4" in e.value.message and e.value.hint


# ---------------------------------------------------------------------- tree records

def group(doc, key):
    return next(g for g in doc["groups"] if g["group"] == key)


def test_probe_on_a_ground_group_reads_its_rise(chain, tmp_path):
    """The power chain's prototype: U3's ground pad, B.Cu (97.5, 1.0), rises 38.470 mV above
    the 0 V at J1's ground pin, the value the tree gives U3's return pin."""
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_tree(chain)
    p = session.probe("t1:GND", "B.Cu", 97.5, 1.0)
    assert p["result_id"] == "t1:GND" and p["group"] == "GND" and p["kind"] == "ground"
    assert p["on_copper"] and p["connected"]
    assert p["drop_mv"] == pytest.approx(38.470, abs=5e-4)
    assert p["voltage_v"] == pytest.approx(group(doc, "GND")["max_rise_mv"] * 1e-3, rel=1e-6)
    assert p["drop_word"] == "rise" and p["drop_unit"] == "mV above 0 V (ground rise)"
    assert p["net"] == "GND"                          # a one-net group: the net is exact
    assert p["drop_pct"] is None                      # no % of 0 V
    json.dumps(p, allow_nan=False)
    rail = session.probe("t1:+3V3", "F.Cu", 50.0, 1.0)  # a two-net group (+3V3, +3V3_F)
    assert rail["kind"] == "rail" and rail["drop_word"] == "drop"
    assert rail["drop_unit"].startswith("mV below 3.30569")
    assert rail.get("net", "+3V3") in ("+3V3", "+3V3_F")   # only when known at the point
    by_net = session.probe("t1:+3V3_F", "F.Cu", 50.0, 1.0)   # a net of the group names it
    assert by_net["result_id"] == "t1:+3V3" and by_net["voltage_v"] == rail["voltage_v"]
    off = session.probe("t1:GND", "B.Cu", 50.0, 20.0)
    assert off["on_copper"] is False and off["group"] == "GND" and "net" not in off
    with pytest.raises(ServiceError) as e:              # GND has no copper on F.Cu
        session.probe("t1:GND", "F.Cu", 97.5, 1.0)
    assert e.value.code == "layer_not_found" and "B.Cu" in e.value.hint


def test_probe_names_the_net_at_the_point_from_the_layers_node_nets(chain, tmp_path):
    """Where the engine gives each node's net (LayerField.node_net), a probe on a group of
    several nets says which one is under the point; anything else leaves the key out."""
    import numpy as np
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    lf = session._results["t1"].tree.groups["+3V3"].layer("F.Cu")
    lf.node_net = np.where(lf.points[:, 0] < 63.0, "+3V3", "+3V3_F")   # FB1 sits at x 61.5-64.5
    assert session.probe("t1:+3V3", "F.Cu", 50.0, 1.0)["net"] == "+3V3"
    assert session.probe("t1:+3V3", "F.Cu", 70.0, 1.0)["net"] == "+3V3_F"
    lf.node_net = np.zeros(len(lf.points), dtype=int)                    # not net names: unknown
    assert "net" not in session.probe("t1:+3V3", "F.Cu", 70.0, 1.0)


def test_render_map_and_cut_current_on_tree_groups(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    m = session.render_map("t1:GND", field="drop")
    assert m["result_id"] == "t1:GND" and m["layer"] == "B.Cu"
    path = Path(m["path"])
    assert path.is_file() and ":" not in path.name and path.read_bytes()[:4] == b"\x89PNG"
    assert m["scale_mv"][1] == pytest.approx(38.470, abs=5e-3)
    # Across +1V8 between U2 (x 82.5) and U3 (x 97.5): U3's 2.0 A, all of it.
    cut = session.cut_current("t1:+1V8", "F.Cu", [90.0, -1.0], [90.0, 3.0])
    assert cut["result_id"] == "t1:+1V8" and cut["width_mm"] == pytest.approx(2.0, rel=1e-6)
    assert cut["abs_current_a"] == pytest.approx(2.0, rel=0.01)
    assert cut["thickness_mm"] == pytest.approx(0.035)


def test_a_tree_group_is_cut_on_the_copper_it_was_solved_with(chain, tmp_path):
    """A project's copper what-if (F.Cu 2 oz): the tree keeps the board as solved, so the cut
    counts 0.07 mm of copper and still carries U3's 2.0 A."""
    project = Project.load(chain)
    project.settings.copper_thickness_mm = {"F.Cu": 0.07}
    project.save()
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    cut = session.cut_current("t1:+1V8", "F.Cu", [90.0, -1.0], [90.0, 3.0])
    assert cut["thickness_mm"] == pytest.approx(0.07)
    assert cut["abs_current_a"] == pytest.approx(2.0, rel=0.01)


@pytest.mark.parametrize("tool, call, hint", [
    ("get_problems", lambda s, rid: s.get_problems(rid), "groups[].problems"),
    ("copper_step", lambda s, rid: s.copper_step(rid, "F.Cu", "up"), "copper_thickness_mm"),
    ("compare_results", lambda s, rid: s.compare_results(rid, rid), "solve_tree"),
    ("compare_revisions", lambda s, rid: s.compare_revisions(rid, rid), "solve_tree"),
    ("propose_fields", lambda s, rid: s.propose_fields(result_id=rid), "project_path"),
])
def test_tools_for_rail_results_refuse_tree_ids_with_a_hint(chain, tmp_path, tool, call, hint):
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    for rid in ("t1:GND", "t1"):
        with pytest.raises(ServiceError) as e:
            call(session, rid)
        assert e.value.code == "invalid_input", (tool, rid)
        assert "power tree" in e.value.message and hint in e.value.hint, (tool, rid, e.value.hint)


def test_tree_ids_that_name_no_group_are_refused_with_the_groups(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    with pytest.raises(ServiceError) as e:
        session.probe("t1", "B.Cu", 97.5, 1.0)          # the tree, not one of its groups
    assert e.value.code == "invalid_input" and "t1:GND" in e.value.hint
    with pytest.raises(ServiceError) as e:
        session.render_map("t1:NOPE")
    assert e.value.code == "result_not_found" and "t1:+1V8" in e.value.hint
    with pytest.raises(ServiceError) as e:
        session.cut_current("t9:GND", "B.Cu", [1, 0], [1, 2])
    assert e.value.code == "result_not_found" and "solve_tree" in e.value.hint


def test_a_tree_is_counted_and_evicted_whole(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out", max_results=1)
    first = session.solve_tree(chain)
    before = session._results["t1"].nbytes
    assert before > 0
    session.probe("t1:GND", "B.Cu", 97.5, 1.0)          # builds the group's layer locator
    assert session._results["t1"].nbytes > before        # ... and counts it in the tree
    session.solve_tree(chain)                              # t2 pushes t1 out, every group of it
    for g in first["groups"]:
        with pytest.raises(ServiceError) as e:
            session.probe(g["result_id"], "B.Cu", 97.5, 1.0)
        assert e.value.code == "result_evicted" and "solve_tree" in e.value.hint
    assert session.probe("t2:GND", "B.Cu", 97.5, 1.0)["on_copper"]
    board = session.open_board(CHAIN / "power_chain.kipi.json.gz")["board_id"]
    session.solve_rail(board, "+1V8", [{"ref": "U2", "voltage_v": 1.8}],
                       [{"ref": "U3", "current_a": 2.0}])      # a rail result pushes t2 out
    with pytest.raises(ServiceError) as e:
        session.probe("t2:GND", "B.Cu", 97.5, 1.0)
    assert e.value.code == "result_evicted"


def test_relative_to_gives_the_voltage_between_two_points(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_tree(chain)
    far = session.probe("t1:GND", "B.Cu", 97.5, 1.0)
    near = session.probe("t1:GND", "B.Cu", 31.5, 1.0)
    within = session.probe("t1:GND", "B.Cu", 97.5, 1.0,
                           relative_to={"layer": "B.Cu", "x_mm": 31.5, "y_mm": 1.0})
    assert within["delta_v"] == pytest.approx(far["voltage_v"] - near["voltage_v"], abs=3e-9)
    assert within["relative_to"]["result_id"] == "t1:GND"
    assert within["relative_to"]["voltage_v"] == near["voltage_v"]
    # Across groups: U3's supply pad against its ground pad is the voltage across U3.
    [u3] = [ld for ld in doc["loads"] if ld["name"] == "U3"]
    across = session.probe("t1:+1V8", "F.Cu", 97.5, 1.0, relative_to={"pad": "U3.2"})
    assert across["relative_to"]["result_id"] == "t1:GND" and across["relative_to"]["pad"] == "U3.2"
    assert across["delta_v"] == pytest.approx(u3["v_across_v"], abs=1e-9)
    brief = session.probe("t1:+1V8", "F.Cu", 97.5, 1.0,     # the brief's x / y spelling
                          relative_to={"result_id": "t1:GND", "layer": "B.Cu", "x": 97.5, "y": 1.0})
    assert brief["delta_v"] == pytest.approx(u3["v_across_v"], abs=1e-9)
    json.dumps(across, allow_nan=False)


def test_relative_to_refuses_what_has_no_common_reference(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    session.solve_tree(chain)
    with pytest.raises(ServiceError) as e:                   # two solves share no 0 V
        session.probe("t1:GND", "B.Cu", 97.5, 1.0,
                      relative_to={"result_id": "t2:GND", "layer": "B.Cu", "x_mm": 31.5, "y_mm": 1.0})
    assert e.value.code == "invalid_input" and "same solve" in e.value.hint
    with pytest.raises(ServiceError) as e:                   # the reference off copper
        session.probe("t1:GND", "B.Cu", 97.5, 1.0,
                      relative_to={"layer": "B.Cu", "x_mm": 50.0, "y_mm": 20.0})
    assert e.value.code == "invalid_input" and "nearest copper" in e.value.hint
    with pytest.raises(ServiceError) as e:
        session.probe("t1:GND", "B.Cu", 97.5, 1.0, relative_to={"pad": "U3"})
    assert e.value.code == "invalid_input" and "REF.PAD" in e.value.hint
    with pytest.raises(ServiceError) as e:
        session.probe("t1:GND", "B.Cu", 97.5, 1.0, relative_to={"pad": "U3.9"})
    assert e.value.code == "pin_not_on_net" and "U3" in e.value.message


def test_report_takes_a_tree_id(chain, tmp_path):
    session = Session(out_dir=tmp_path / "out")
    session.solve_tree(chain)
    with pytest.raises(ServiceError) as e:                   # only a tree: the default has no rail
        session.report()
    assert e.value.code == "result_not_found" and "t1" in e.value.hint
    out = session.report(["t1"], path=tmp_path / "tree.html")
    page = Path(out["path"])
    assert page == tmp_path / "tree.html" and out["tree_id"] == "t1" and out["size_mb"] > 0
    assert set(out["groups"]) == {"t1:+1V8", "t1:+3V3", "t1:VIN", "t1:GND"}
    text = page.read_text(encoding="utf-8")
    assert "power tree" in text and "https://" not in text
    for ids in (["t1:GND"], ["t1", "r1"]):
        with pytest.raises(ServiceError) as e:
            session.report(ids)
        assert e.value.code == "invalid_input" and "t1" in e.value.hint


def test_mcp_tree_groups_round_trip(chain, tmp_path):
    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            tree = await c.call_tool("solve_tree", {"project_path": str(chain)})
            probe = await c.call_tool("probe", {"result_id": "t1:+1V8", "layer": "F.Cu", "x_mm": 97.5,
                                                "y_mm": 1.0, "relative_to": {"pad": "U3.2"}})
            image = await c.call_tool("render_map", {"result_id": "t1:GND"})
            refused = await c.call_tool("get_problems", {"result_id": "t1:GND"})
            page = await c.call_tool("report", {"result_ids": ["t1"]})
            return tools, tree, probe, image, refused, page
    tools, tree, probe, image, refused, page = anyio.run(scenario)
    assert "relative_to" in tools["probe"].input_schema["properties"]
    assert "t1:GND" in tools["probe"].description and "tree_id" in tools["solve_tree"].description
    assert not tree.is_error and tree.structured_content["tree_id"] == "t1"
    assert not probe.is_error and probe.structured_content["group"] == "+1V8"
    assert probe.structured_content["delta_v"] == pytest.approx(1.8, abs=1e-6)
    assert not image.is_error and any(b.type == "image" for b in image.content)
    assert refused.is_error and refused.structured_content["error"]["code"] == "invalid_input"
    assert not page.is_error and Path(page.structured_content["path"]).is_file()
