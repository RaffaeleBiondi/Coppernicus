"""compare_revisions: two results from different board files (before and after a change in
KiCad), so the meshes differ. Loads matched by ref and net, layer losses by name, vias by
position and drill; B's drop mapped onto A's mesh to find where it grew or shrank most; and
what could not be matched (parts moved, removed or added, nets renamed, copper only on one)."""

import os
from pathlib import Path

import anyio
import pytest
from kicad_fixture import write_board
from mcp import Client

from coppernicus.mcp_server import build_server
from coppernicus.service import ServiceError, Session

PNG = b"\x89PNG\r\n\x1a\n"


def board(path, width=0.5, net="VCC", u1_x=40.0, extra=(), via2_x=30.0):
    """J1 at x=0 → F.Cu → via at x=10 → B.Cu → via at via2_x → F.Cu → U1 at u1_x."""
    parts = [("J1", [("1", 0.0, 0.0, net)], {}), ("U1", [("1", u1_x, 0.0, net)], {}), *extra]
    tracks = [(net, (0, 0), (10, 0), width), (net, (10, 0), (via2_x, 0), width, "B.Cu"),
              (net, (via2_x, 0), (u1_x, 0), width)]
    return write_board(path, parts, tracks, vias=[(net, 10.0, 0.0), (net, via2_x, 0.0)])


def solve(session, path, net="VCC", loads=None):
    bid = session.open_board(path)["board_id"]
    return session.solve_rail(bid, net, [{"ref": "J1", "voltage_v": 1.0}],
                              loads or [{"ref": "U1", "current_a": 1.0}])


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


def test_a_wider_track_lowers_the_drop_and_the_map_says_where(session, tmp_path):
    a = solve(session, board(tmp_path / "a.kicad_pcb"))
    b = solve(session, board(tmp_path / "b.kicad_pcb", width=1.0))
    out = session.compare_revisions(a["result_id"], b["result_id"])
    [load] = out["loads"]
    assert load["ref"] == "U1" and load["delta_drop_mv"] < 0
    assert load["drop_mv_b"] == pytest.approx(load["drop_mv_a"] / 2, rel=0.1)
    assert all(row["delta_power_w"] < 0 for row in out["losses"] if row["name"] in ("F.Cu", "B.Cu"))
    assert [(v["x_mm"], v["y_mm"]) for v in out["vias"]["matched"]] == [(10.0, 0.0), (30.0, 0.0)]
    worst = out["drop_map"]["most_decreased"]
    assert worst["layer"] in ("F.Cu", "B.Cu") and worst["delta_mv"] < 0 and worst["x_mm"] > 20
    grew = out["drop_map"]["most_increased"]
    assert grew is None               # wider copper: nothing grew (round-off is not a change)
    coverage = out["drop_map"]["copper"]
    assert coverage["a_not_in_b_mm2"] == pytest.approx(0.0, abs=0.5)    # B is wider: covers A
    assert coverage["b_not_in_a_mm2"] > 5.0
    for path in out["maps"]:
        assert Path(path).read_bytes()[:8] == PNG and Path(path).parent == (tmp_path / "out")
    assert out["unmatched"] == {"loads_only_in_a": [], "loads_only_in_b": [], "moved": [], "nets": None}


def test_moved_added_and_renamed_are_listed(session, tmp_path):
    a = solve(session, board(tmp_path / "a.kicad_pcb"))
    extra = [("U2", [("1", 5.0, 0.0, "VCC")], {})]                 # on the F.Cu track
    b = solve(session, board(tmp_path / "b.kicad_pcb", u1_x=35.0, extra=extra, via2_x=31.0),
              loads=[{"ref": "U1", "current_a": 1.0}, {"ref": "U2", "current_a": 0.0}])
    out = session.compare_revisions(a["result_id"], b["result_id"])
    assert out["unmatched"]["loads_only_in_b"] == ["U2"] and out["unmatched"]["moved"] == ["U1"]
    assert [(v["x_mm"], v["y_mm"]) for v in out["vias"]["only_in_a"]] == [(30.0, 0.0)]
    assert [(v["x_mm"], v["y_mm"]) for v in out["vias"]["only_in_b"]] == [(31.0, 0.0)]
    c = solve(session, board(tmp_path / "c.kicad_pcb", net="VCC_MAIN"), net="VCC_MAIN")
    renamed = session.compare_revisions(a["result_id"], c["result_id"])
    assert renamed["unmatched"]["nets"] == {"a": "VCC", "b": "VCC_MAIN"}
    assert renamed["loads"][0]["ref"] == "U1"                        # still matched by part


def test_the_same_file_edited_in_kicad(session, tmp_path):
    """The usual case: the board file is edited and saved in place. Opening it again gives a
    new board id, and the earlier result keeps its own board, so both can be compared."""
    path = board(tmp_path / "b.kicad_pcb")
    a = solve(session, path)
    board(path, width=1.0)
    os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 10 ** 9))
    b = solve(session, path)
    assert b["board_id"] != a["board_id"]
    out = session.compare_revisions(a["result_id"], b["result_id"])
    assert out["loads"][0]["delta_drop_mv"] < 0
    assert session.probe(a["result_id"], "F.Cu", 5.0, 0.0)["on_copper"] is True


def test_different_currents_or_sources_are_warned(session, tmp_path):
    a = solve(session, board(tmp_path / "a.kicad_pcb"))
    b = solve(session, board(tmp_path / "b.kicad_pcb"), loads=[{"ref": "U1", "current_a": 2.0}])
    out = session.compare_revisions(a["result_id"], b["result_id"])
    assert any("currents differ" in w and "U1" in w for w in out["warnings"])
    with pytest.raises(ServiceError) as e:
        session.compare_revisions(a["result_id"], "r99")
    assert e.value.code == "result_not_found"


def test_mcp_compare_revisions(tmp_path):
    a_path, b_path = board(tmp_path / "a.kicad_pcb"), board(tmp_path / "b.kicad_pcb", width=1.0)

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            ids = []
            for path in (a_path, b_path):
                bid = (await c.call_tool("open_board", {"path": str(path)})).structured_content["board_id"]
                r = await c.call_tool("solve_rail", {"board_id": bid, "net": "VCC",
                                                     "sources": [{"ref": "J1", "voltage_v": 1.0}],
                                                     "loads": [{"ref": "U1", "current_a": 1.0}]})
                ids.append(r.structured_content["result_id"])
            return await c.call_tool("compare_revisions", {"result_a": ids[0], "result_b": ids[1]})
    out = anyio.run(scenario)
    assert not out.is_error and out.structured_content["loads"][0]["delta_drop_mv"] < 0
