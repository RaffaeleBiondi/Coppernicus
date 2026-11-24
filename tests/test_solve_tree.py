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


def test_solve_tree_is_the_cli_tree_document(chain, tmp_path, capsys):
    expected = cli_tree(chain, tmp_path, capsys)
    got = Session(out_dir=tmp_path / "out").solve_tree(chain)
    assert got == expected
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
    assert not ok.is_error and ok.structured_content == expected
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
