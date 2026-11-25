"""copper_step: one copper layer one standard weight up or down (0.5, 1, 2, 3 oz), from the
design's own thickness, re-solved and compared with the baseline. Bigger jumps, a non-standard
start and stepping a step's output are refused: real changes are made in KiCad."""

import shutil
from pathlib import Path

import anyio
import pytest
from mcp import Client

from coppernicus.engine.synthetic import OZ1, Strip, save_snapshot, strip_board
from coppernicus.mcp_server import build_server
from coppernicus.project import Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


@pytest.fixture()
def strip(tmp_path):
    path = tmp_path / "boards" / "strip.kipi.json.gz"
    path.parent.mkdir()
    save_snapshot(strip_board(), path)
    return path


def solve(session, strip, **kw):
    bid = session.open_board(strip)["board_id"]
    return session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": 1.0}],
                              [{"ref": "U1", "current_a": 1.0}], **kw)


def project_solve(session, strip, tmp_path, thickness):
    rail = Rail(Strip.NET, [Source("J1", 1.0)], [Load("U1", 1.0)])
    p = Project(board="boards/strip.kipi.json.gz", rails=[rail])
    p.settings.copper_thickness_mm = thickness
    return session.solve_project(p.save(tmp_path / "p.coppernicus.yml"))["rails"][0]


def test_one_step_up_halves_the_strip_drop_and_says_so(session, strip):
    assert OZ1 == pytest.approx(0.035)
    base = solve(session, strip)
    out = session.copper_step(base["result_id"], "F.Cu", "up")
    assert (out["from_oz"], out["to_oz"]) == ("1 oz", "2 oz")
    assert out["from_mm"] == pytest.approx(0.035) and out["to_mm"] == pytest.approx(0.070)
    [load] = out["compare"]["loads"]
    assert load["drop_mv_b"] == pytest.approx(load["drop_mv_a"] / 2, rel=1e-5)
    assert out["baseline"] == base["result_id"] and out["result_id"] != base["result_id"]
    assert any("via barrels get longer" in a for a in out["assumptions"])
    assert any("KiCad" in a for a in out["assumptions"])


def test_stepping_a_step_and_off_the_ends_and_odd_starts_are_refused(session, strip, tmp_path):
    first = session.copper_step(solve(session, strip)["result_id"], "F.Cu", "up")
    with pytest.raises(ServiceError) as e:
        session.copper_step(first["result_id"], "F.Cu", "up")
    assert e.value.code == "invalid_input" and "already" in e.value.message and "KiCad" in e.value.hint
    with pytest.raises(ServiceError) as e:           # a what-if from solve_rail settings, likewise
        session.copper_step(solve(session, strip, settings={"copper_thickness_mm": {"F.Cu": 0.07}})[
            "result_id"], "F.Cu", "up")
    assert "already" in e.value.message
    top = project_solve(session, strip, tmp_path, {"F.Cu": 0.105})        # the project's 3 oz
    with pytest.raises(ServiceError) as e:
        session.copper_step(top["result_id"], "F.Cu", "up")
    assert "3 oz" in e.value.message and "KiCad" in e.value.hint
    low = project_solve(session, strip, tmp_path, {"F.Cu": 0.0175})
    with pytest.raises(ServiceError) as e:
        session.copper_step(low["result_id"], "F.Cu", "down")
    assert "0.5 oz" in e.value.message
    assert session.copper_step(low["result_id"], "F.Cu", "up")["to_oz"] == "1 oz"
    odd = project_solve(session, strip, tmp_path, {"F.Cu": 0.05})
    with pytest.raises(ServiceError) as e:
        session.copper_step(odd["result_id"], "F.Cu", "up")
    assert "not a standard copper weight" in e.value.message
    with pytest.raises(ServiceError) as e:
        session.copper_step(odd["result_id"], "In1.Cu", "up")
    assert e.value.code == "layer_not_found"
    with pytest.raises(ServiceError) as e:
        session.copper_step(odd["result_id"], "F.Cu", "twice")
    assert e.value.code == "invalid_input"


def test_a_project_rail_keeps_its_estimated_draws(session, tmp_path):
    shutil.copytree(CHAIN, tmp_path / "c")
    doc = session.solve_project(tmp_path / "c" / "power_chain.coppernicus.yml")
    v33 = next(r for r in doc["rails"] if r["net"] == "+3V3")
    out = session.copper_step(v33["result_id"], v33["busiest_layer"], "up")
    names = [row["name"] for row in out["compare"]["loads"]]
    assert "FB1 (link, estimated)" in names
    assert not any("draws differ" in w for w in out["compare"]["warnings"])


def test_mcp_copper_step(strip, tmp_path):
    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            bid = (await c.call_tool("open_board", {"path": str(strip)})).structured_content["board_id"]
            r = await c.call_tool("solve_rail", {"board_id": bid, "net": Strip.NET,
                                                 "sources": [{"ref": "J1", "voltage_v": 1.0}],
                                                 "loads": [{"ref": "U1", "current_a": 1.0}]})
            return await c.call_tool("copper_step", {"result_id": r.structured_content["result_id"],
                                                     "layer": "F.Cu", "step": "down"})
    out = anyio.run(scenario)
    assert not out.is_error and out.structured_content["to_oz"] == "0.5 oz"


def test_a_step_on_another_layer_of_a_what_if_is_refused(session, strip):
    first = session.copper_step(solve(session, strip)["result_id"], "F.Cu", "up")
    with pytest.raises(ServiceError) as e:
        session.copper_step(first["result_id"], "B.Cu", "up")
    assert "already" in e.value.message and "F.Cu" in e.value.message
