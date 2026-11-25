"""The board temperature in the agent and CI interfaces: a rail-by-rail solve with
board-temperature limits set lists them as not judged (the temperature is a power-tree solve),
so the CLI and CI exit 3 instead of passing silently. Synthetic examples only."""

import shutil
from pathlib import Path

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_NOT_JUDGED, main
from coppernicus.project import Project
from coppernicus.service import Session

ROOT = Path(__file__).resolve().parents[1]
REASON = "solved with the power tree only"


@pytest.fixture()
def relaxed(tmp_path):
    shutil.copytree(ROOT / "examples" / "demo", tmp_path / "demo")
    return tmp_path / "demo" / "demo-relaxed.coppernicus.yml"


def with_thermal_limit(path: Path, **limits) -> Path:
    p = Project.load(path)
    for k, v in limits.items():
        setattr(p.thermal, k, v)
    p.save()
    return path


def test_a_rail_solve_lists_the_board_temperature_limits_as_not_judged(relaxed, tmp_path, capsys,
                                                                     monkeypatch):
    base = Session(out_dir=tmp_path / "o0").solve_project(relaxed)
    assert base["verdict"] == "pass" and base["not_judged"] == []          # the demo passes 1 %
    with_thermal_limit(relaxed, max_rise_c=20.0, max_temp_c=60.0)
    doc = Session(out_dir=tmp_path / "o1").solve_project(relaxed)
    assert doc["verdict"] == "not_judged"
    board = [u for u in doc["not_judged"] if u["item"] == "board temperature"]
    assert {u["limit"] for u in board} == {"max_rise_c", "max_temp_c"}
    assert all(REASON in u["reason"] and u["rail"] is None for u in board)
    assert "board temperature" in doc["verdict_note"]
    capsys.readouterr()
    assert main(["solve", str(relaxed), "--no-maps", "--out", str(tmp_path / "cli")]) == EXIT_NOT_JUDGED
    assert "NOT JUDGED board temperature (max_rise_c)" in capsys.readouterr().out
    # CI: the run's code is 3 and the job summary says why.
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["demo/demo-relaxed.coppernicus.yml"], Path("ci"), html=False)
    assert index["exit_code"] == EXIT_NOT_JUDGED and index["verdict"] == "not_judged"
    assert "NOT JUDGED board temperature (max_rise_c)" in ci.summary_markdown(index)



@pytest.fixture()
def thermal_demo(tmp_path):
    shutil.copytree(ROOT / "examples" / "demo", tmp_path / "demo")
    return tmp_path / "demo" / "demo-thermal.coppernicus.yml"


def test_solve_tree_takes_thermal_options_and_returns_the_estimate(thermal_demo, tmp_path):
    from coppernicus.service import ServiceError
    session = Session(out_dir=tmp_path / "o")
    doc = session.solve_tree(thermal_demo)                       # the project enables it
    est = doc["thermal_estimate"]
    assert doc["thermal"]["iterations"] == 3 and est["unknowns"] == est["cells"] * est["layers"]
    assert est["exceeds_memory"] is False and est["memory_bytes"] > 0
    assert "unknowns" in est["text"] and "needs about" in est["text"]
    off = session.solve_tree(thermal_demo, thermal=False)
    assert "thermal" not in off and "thermal_estimate" not in off
    tuned = session.solve_tree(thermal_demo, thermal_options={"iterations": 1, "max_rise_c": 100.0})
    assert tuned["thermal"]["iterations"] == 1 and tuned["thermal"]["limits"] == {"max_rise_c": 100.0}
    assert Project.load(thermal_demo).thermal.iterations == 3              # the file is not changed
    with pytest.raises(ServiceError) as e:
        session.solve_tree(thermal_demo, thermal_options={"iterations": 0})
    assert e.value.code == "invalid_input"


def test_an_estimate_over_the_machines_memory_needs_a_confirmation(thermal_demo, tmp_path, monkeypatch):
    """Never a silent multi-GB solve from an agent: over this machine's memory, solve_tree
    refuses with the estimate unless confirm_large is given."""
    from coppernicus.engine import ac
    from coppernicus.service import ServiceError
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)        # a 1 MB machine
    session = Session(out_dir=tmp_path / "o")
    with pytest.raises(ServiceError) as e:
        session.solve_tree(thermal_demo)
    assert e.value.code == "solve_too_large"
    assert "unknowns" in e.value.message and "confirm_large" in e.value.hint and "grid_mm" in e.value.hint
    doc = session.solve_tree(thermal_demo, confirm_large=True)
    assert doc["thermal_estimate"]["exceeds_memory"] is True and "thermal" in doc


def test_the_mcp_solve_tree_tool_takes_the_thermal_options(thermal_demo, tmp_path):
    import anyio
    from mcp import Client

    from coppernicus.mcp_server import build_server

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            ok = await c.call_tool("solve_tree", {"project_path": str(thermal_demo), "thermal": True,
                                                  "thermal_options": {"iterations": 1}})
            bad = await c.call_tool("solve_tree", {"project_path": str(thermal_demo),
                                                   "thermal_options": {"iterationz": 1}})
            return ok.structured_content, bad
    doc, bad = anyio.run(scenario)
    assert doc["thermal"]["iterations"] == 1 and "thermal_estimate" in doc
    assert bad.is_error


def test_the_cli_prints_the_estimate_before_an_electro_thermal_solve(thermal_demo, tmp_path, capsys):
    import json
    code = main(["solve", str(thermal_demo), "--thermal", "--no-maps", "--out", str(tmp_path / "a")])
    assert code in (0, 1)
    out = capsys.readouterr().out
    head = out[:out.index("VRMs")]                        # printed before the solve's tables
    assert "Board temperature: " in head and "unknowns" in head and "needs about" in head
    main(["solve", str(thermal_demo), "--thermal", "--json", "--no-maps", "--out", str(tmp_path / "b")])
    captured = capsys.readouterr()
    assert "unknowns" in captured.err and json.loads(captured.out)["thermal_estimate"]["unknowns"] > 0
