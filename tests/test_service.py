"""The service layer on synthetic boards: JSON in and out, structured errors, closed-form
checks for probe and cut_current, the result memory bound."""

import dataclasses
import json
import math
import os

import numpy as np
import pytest

from coppernicus.engine.synthetic import (
    OZ1,
    F,
    Strip,
    SyntheticBoard,
    demo_board,
    net_tie_board,
    save_snapshot,
    strip_board,
    two_layer_stackup,
)
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import ServiceError, Session

RHO = 1.724e-5      # Ω·mm, annealed copper at 20 °C (the engine's constant, as in test_engine)
W, T = Strip.W, OZ1
DEMO_LOADS = [{"ref": "U1", "current_a": 2.0}, {"ref": "U2", "current_a": 1.5},
              {"ref": "U3", "current_a": 0.3}]


def strict_json(doc):
    """Every service output must be plain JSON: no NaN, no numpy types."""
    return json.loads(json.dumps(doc, allow_nan=False))


@pytest.fixture()
def boards(tmp_path):
    paths = {}
    for name, snap in (("strip", strip_board()), ("demo", demo_board()), ("tie", net_tie_board())):
        paths[name] = tmp_path / "boards" / f"{name}.kipi.json.gz"
        paths[name].parent.mkdir(exist_ok=True)
        save_snapshot(snap, paths[name])
    return paths


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


def solve_strip(session, board_id, current=1.0, **kw):
    return session.solve_rail(board_id, Strip.NET, [{"ref": "J1", "voltage_v": 1.0}],
                              [{"ref": "U1", "current_a": current}], **kw)


def test_open_board_list_nets_and_parts(session, boards):
    info = strict_json(session.open_board(boards["demo"]))
    assert info["board_id"] == "b1"
    assert [lay["name"] for lay in info["layers"]] == ["F.Cu", "B.Cu"]
    assert info["layers"][0]["thickness_mm"] == pytest.approx(OZ1)
    assert info["counts"]["parts"] == 4 and info["counts"]["vias"] == 9
    assert info["outline_bbox_mm"] == [0.0, 0.0, 60.0, 40.0]
    assert info["assumptions"] and info["warnings"] == []
    assert session.open_board(str(boards["demo"]))["board_id"] == "b1"     # same file, same id

    nets = strict_json(session.list_nets("b1"))
    assert [n["name"] for n in nets["nets"]] == ["+3V3"]
    assert nets["nets"][0] == {"name": "+3V3", "kind": "power", "layers": ["F.Cu", "B.Cu"],
                               "layers_with_copper": ["F.Cu", "B.Cu"], "layers_with_only_vias_or_pads": []}
    assert session.list_nets("b1", filter="+3v*")["count"] == 1          # a glob matches whole names
    assert session.list_nets("b1", filter="3v")["count"] == 1            # a plain filter is a substring
    assert session.list_nets("b1", filter="gnd")["count"] == 0

    parts = strict_json(session.net_parts("b1", "+3V3"))
    assert [p["ref"] for p in parts["parts"]] == ["J1", "U1", "U2", "U3"]
    assert parts["parts"][0]["pins"] == ["1"] and parts["net_ties"] == []


def test_net_ties_are_listed_and_followed(session, boards):
    bid = session.open_board(boards["tie"])["board_id"]
    parts = session.net_parts(bid, "VA")
    assert parts["net_ties"] == [{"ref": "NT1", "net_a": "VA", "net_b": "VB"}]
    assert parts["solved_with"] == ["VB"]
    res = session.solve_rail(bid, "VA", [{"ref": "J1", "voltage_v": 1.0}], [{"ref": "U1", "current_a": 1.0}])
    assert res["nets_solved"] == ["VA", "VB"]
    with pytest.raises(ServiceError) as e:
        session.solve_rail(bid, "VA", [{"ref": "J1", "voltage_v": 1.0}],
                           [{"ref": "U1", "current_a": 1.0}], follow_net_ties=False)
    assert e.value.code == "part_not_on_net" and "J1" in e.value.hint


def test_solve_summary_has_units_assumptions_and_no_verdict_without_limits(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    res = strict_json(solve_strip(session, bid))
    r = RHO * Strip.length() / (W * T)
    [load] = res["loads"]
    assert load["drop_mv"] == pytest.approx(1e3 * r, rel=5e-3)
    assert load["current_a"] == pytest.approx(1.0)
    assert res["max_drop_mv"] == pytest.approx(1e3 * r, rel=5e-3)
    assert res["power_loss_w"] == pytest.approx(r, rel=5e-3)
    assert res["busiest_layer"] == "F.Cu"
    assert res["verdict"] == "not_judged" and res["limits"] == {}
    assert any("ground return" in a for a in res["assumptions"])
    assert any("Via plating" in a for a in res["assumptions"])
    assert res["energy_balance_rel_error"] < 1e-6
    for key in res:            # units in every numeric key, except counts and ids
        if isinstance(res[key], float):
            assert key.endswith(("_v", "_mv", "_a", "_w", "_pct", "_a_mm2", "_rel_error")), key


def test_limits_judge_and_get_problems_rerank_without_solving(session, boards):
    bid = session.open_board(boards["demo"])["board_id"]
    res = session.solve_rail(bid, "+3V3", [{"ref": "J1", "voltage_v": 3.3}], DEMO_LOADS,
                             limits={"max_drop_pct": 0.25})
    assert res["verdict"] == "fail" and res["failed"] == 2
    assert {d["name"]: d["exceeds_drop_limit"] for d in res["loads"]} == {"U1": True, "U2": True, "U3": False}
    rid = res["result_id"]
    unjudged = strict_json(session.get_problems(rid, limits={}))
    assert unjudged["verdict"] == "not_judged"
    assert all(p["severity"] == "info" for p in unjudged["problems"])
    loose = session.get_problems(rid, limits={"max_drop_pct": 5.0})
    assert loose["verdict"] == "pass"
    again = session.get_problems(rid)                       # the solve's own limits
    assert again["verdict"] == "fail"
    kinds = {p["kind"] for p in again["problems"]}
    assert kinds == {"load", "hotspot", "via"}
    hot = next(p for p in again["problems"] if p["kind"] == "hotspot")
    assert {"mean_j_a_mm2", "peak_j_a_mm2", "area_mm2", "layer", "x_mm", "y_mm"} <= set(hot)


def test_cut_across_the_strip_matches_width_and_current(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    rid = solve_strip(session, bid, 1.0)["result_id"]
    # Downward across the strip at x = 20: the normal points to −x (right of the walk as seen
    # in KiCad), while the current flows from J1 (x = 0) to U1 (x = 40), i.e. +x.
    cut = strict_json(session.cut_current(rid, "F.Cu", [20.0, -2.0], [20.0, 2.0]))
    assert cut["normal"] == [-1.0, 0.0]
    assert cut["width_mm"] == pytest.approx(W, rel=1e-9)
    assert cut["thickness_mm"] == pytest.approx(T)
    assert cut["current_a"] == pytest.approx(-1.0, rel=5e-3)
    assert cut["mean_abs_j_a_mm2"] == pytest.approx(1.0 / (W * T), rel=5e-3)
    assert cut["mean_normal_j_a_mm2"] == pytest.approx(1.0 / (W * T), rel=5e-3)
    assert cut["max_abs_j_a_mm2"] == pytest.approx(1.0 / (W * T), rel=0.02)
    assert cut["warnings"] == []
    # Reversed, the sign flips; oblique at 60° from the strip, the width is w / sin 60°.
    back = session.cut_current(rid, "F.Cu", [20.0, 2.0], [20.0, -2.0])
    assert back["current_a"] == pytest.approx(1.0, rel=5e-3)
    a = math.radians(60)
    c, d = np.array([20.0, 0.0]), 2.0 * np.array([math.cos(a), math.sin(a)])
    oblique = session.cut_current(rid, "F.Cu", list(c - d), list(c + d))
    assert oblique["width_mm"] == pytest.approx(W / math.sin(a), rel=1e-6)
    assert abs(oblique["current_a"]) == pytest.approx(1.0, rel=5e-3)


def test_cut_through_the_demo_neck_carries_the_far_loads(session, boards):
    bid = session.open_board(boards["demo"])["board_id"]
    rid = session.solve_rail(bid, "+3V3", [{"ref": "J1", "voltage_v": 3.3}], DEMO_LOADS)["result_id"]
    # The slot (x 28–31, y 2–31) leaves a 7 mm neck (y 31–38) on B.Cu; U1 and U2 are beyond it.
    cut = session.cut_current(rid, "B.Cu", [29.5, 20.0], [29.5, 40.0])
    assert cut["width_mm"] == pytest.approx(7.0, rel=1e-9)
    assert cut["copper_spans_mm"] == [[pytest.approx(11.0), pytest.approx(18.0)]]
    assert abs(cut["current_a"]) == pytest.approx(3.5, rel=0.01)
    with pytest.raises(ServiceError) as e:
        session.cut_current(rid, "F.Cu", [29.5, 20.0], [29.5, 40.0])
    assert e.value.code == "cut_misses_copper" and "B.Cu" in e.value.hint


def test_probe_reads_voltage_drop_and_current_density(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    rid = solve_strip(session, bid, 1.0)["result_id"]
    x = 20.0
    p = strict_json(session.probe(rid, "F.Cu", x, 0.1))
    expected_drop = RHO * (x - Strip.PAD / 2) / (W * T)          # from the source pad's edge
    assert p["on_copper"] and p["connected"]
    assert p["drop_mv"] == pytest.approx(1e3 * expected_drop, rel=0.01)
    assert p["voltage_v"] == pytest.approx(1.0 - expected_drop, abs=1e-5)
    assert p["j_a_mm2"] == pytest.approx(1.0 / (W * T), rel=0.02)
    jx, jy = p["j_vector_a_mm2"]
    assert jx > 0 and abs(jy) < 0.05 * jx                         # flowing toward the load
    off = strict_json(session.probe(rid, "F.Cu", 20.0, 5.0))
    assert off["on_copper"] is False and off["nearest_copper_mm"]["distance_mm"] > 4.0


def test_probe_relative_to_a_point_or_a_pad_of_the_same_rail(session, boards):
    """delta_v on the strip, closed form: V(30) − V(10) = −ρ·20/(W·T); against U1's pad (an
    equipotential terminal from its edge at x 39.25) +ρ·9.25/(W·T)."""
    bid = session.open_board(boards["strip"])["board_id"]
    rid = solve_strip(session, bid, 1.0)["result_id"]
    p = strict_json(session.probe(rid, "F.Cu", 30.0, 0.0,
                                  relative_to={"layer": "F.Cu", "x_mm": 10.0, "y_mm": 0.0}))
    assert p["delta_v"] == pytest.approx(-RHO * 20.0 / (W * T), rel=0.01)
    assert "net" in p and "group" not in p and "drop_word" not in p     # a rail result's keys
    pad = strict_json(session.probe(rid, "F.Cu", 30.0, 0.0, relative_to={"pad": "U1.1"}))
    assert pad["relative_to"]["result_id"] == rid and pad["relative_to"]["net"] == Strip.NET
    assert pad["delta_v"] == pytest.approx(RHO * (Strip.X1 - Strip.PAD / 2 - 30.0) / (W * T), rel=0.02)


def test_probe_relative_to_a_drilled_pad_reads_its_ring(session, tmp_path):
    """A through-hole pad's centre is its drill, off the mesh: the pad is read at its nearest
    meshed copper, the ring, which holds the terminal's voltage."""
    from shapely.geometry import Point, box

    from coppernicus.engine.synthetic import B
    b = SyntheticBoard("pth", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.pad("U1", "1", "VCC", box(39.0, -1.0, 41.0, 1.0).difference(Point(40.0, 0.0).buffer(0.5)),
          layers=(F, B), drill=1.0)
    b.track("VCC", F, 0.5, (0.0, 0.0), (39.0, 0.0))
    save_snapshot(b.snapshot(), tmp_path / "pth.kipi.json.gz")
    bid = session.open_board(tmp_path / "pth.kipi.json.gz")["board_id"]
    r = session.solve_rail(bid, "VCC", [{"ref": "J1", "voltage_v": 1.0}], [{"ref": "U1", "current_a": 1.0}])
    assert session.probe(r["result_id"], "F.Cu", 40.0, 0.0)["on_copper"] is False   # the drill
    p = session.probe(r["result_id"], "F.Cu", 20.0, 0.0, relative_to={"pad": "U1.1"})
    ref = p["relative_to"]
    assert ref["voltage_v"] == pytest.approx(r["loads"][0]["v_min_v"], abs=1e-6)
    assert 0.5 <= math.hypot(ref["x_mm"] - 40.0, ref["y_mm"]) <= 1.5      # on the ring
    assert p["delta_v"] == pytest.approx(p["voltage_v"] - ref["voltage_v"], abs=2e-7)


def test_copper_thickness_what_if_and_compare(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    a = solve_strip(session, bid)
    b = solve_strip(session, bid, settings={"copper_thickness_mm": {"F.Cu": 2 * OZ1}})
    assert b["loads"][0]["drop_mv"] == pytest.approx(a["loads"][0]["drop_mv"] / 2, rel=1e-6)
    assert any("What-if copper thickness" in s for s in b["assumptions"])
    cmp_ = strict_json(session.compare_results(a["result_id"], b["result_id"]))
    assert cmp_["settings_changed"] == {"copper_thickness_mm": {"a": None, "b": {"F.Cu": 2 * OZ1}}}
    assert cmp_["loads"][0]["delta_drop_mv"] == pytest.approx(-a["loads"][0]["drop_mv"] / 2, rel=1e-5)
    assert cmp_["max_drop_mv"]["delta"] < 0
    # The cut uses the thickness of the solve.
    cut = session.cut_current(b["result_id"], "F.Cu", [20.0, -2.0], [20.0, 2.0])
    assert cut["thickness_mm"] == pytest.approx(2 * OZ1)
    assert abs(cut["current_a"]) == pytest.approx(1.0, rel=5e-3)


def strip_project(tmp_path, thickness=None) -> Project:
    """A project file for the strip board of the ``boards`` fixture (J1 → U1, 1 A)."""
    p = Project(board="boards/strip.kipi.json.gz", rails=[Rail(
        Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)])])
    p.settings.copper_thickness_mm = dict(thickness or {})
    p.save(tmp_path / ("strip-thick.coppernicus.yml" if thickness else "strip.coppernicus.yml"))
    return p


def test_project_copper_thickness_halves_the_strip_drop(session, boards, tmp_path):
    """The setting in a project file is honoured by solve_project (the path of the CLI,
    --json, --html, the MCP solve_project tool and the GitHub Action). The strip has no via,
    so twice the copper is exactly half the drop."""
    plain = session.solve_project(strip_project(tmp_path).path)["rails"][0]
    thick = session.solve_project(strip_project(tmp_path, {"F.Cu": 2 * OZ1}).path)["rails"][0]
    assert thick["loads"][0]["drop_mv"] == pytest.approx(plain["loads"][0]["drop_mv"] / 2, rel=1e-6)
    assert thick["settings"]["copper_thickness_mm"] == {"F.Cu": 2 * OZ1}
    assert "copper_thickness_mm" not in plain["settings"]
    [note] = [a for a in thick["assumptions"] if a.startswith("What-if copper thickness")]
    assert f"F.Cu {2 * OZ1:g} mm (board file: {OZ1:g} mm)" in note
    assert "board file is not changed" in note and "via barrels get longer when the stack thickens" in note
    assert not any(a.startswith("What-if") for a in plain["assumptions"])


def test_project_copper_thickness_unknown_layer_fails_the_project(session, boards, tmp_path):
    """A layer the board does not have is one error for the project (not one per rail),
    and it lists the board's copper layers."""
    p = strip_project(tmp_path, {"In1.Cu": 0.07})
    with pytest.raises(ServiceError) as e:
        session.solve_project(p.path)
    assert e.value.code == "layer_not_found"
    assert "No copper layer In1.Cu" in e.value.message and "F.Cu, B.Cu" in e.value.message
    assert e.value.message.startswith("strip-thick.coppernicus.yml: settings.copper_thickness_mm")


def test_solve_rail_thickness_overrides_the_projects_per_layer(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    a = solve_strip(session, bid)
    project_settings = Project().settings
    project_settings.copper_thickness_mm = {"F.Cu": 2 * OZ1, "B.Cu": 0.07}
    b = solve_strip(session, bid, base_settings=project_settings,
                    settings={"copper_thickness_mm": {"F.Cu": 0.105}})    # 3 oz: one step from the 2 oz
    assert b["settings"]["copper_thickness_mm"] == {"F.Cu": 0.105, "B.Cu": 0.07}
    assert project_settings.copper_thickness_mm == {"F.Cu": 2 * OZ1, "B.Cu": 0.07}   # not edited
    # B.Cu thicker changes the via barrels only; the strip has none, so F.Cu sets the drop.
    assert b["loads"][0]["drop_mv"] == pytest.approx(a["loads"][0]["drop_mv"] / 3, rel=1e-6)


def test_solve_rail_copper_what_ifs_are_one_standard_step(session, boards):
    """The owner's rule for agents: a copper what-if is a standard weight (0.5, 1, 2, 3 oz) at
    most one step from the design's thickness on each layer; anything else points to
    copper_step and to KiCad. (Project files and the CLI keep the free value.)"""
    bid = session.open_board(boards["strip"])["board_id"]
    for mm in (0.0175, 0.035, 0.070):                 # the board's 1 oz, one step down, one up
        solve_strip(session, bid, settings={"copper_thickness_mm": {"F.Cu": mm}})
    for mm in (0.105, 0.05, 0.2):                     # two steps, not standard, far off
        with pytest.raises(ServiceError) as e:
            solve_strip(session, bid, settings={"copper_thickness_mm": {"F.Cu": mm}})
        assert e.value.code == "invalid_input" and "one standard step" in e.value.message
        assert "copper_step" in e.value.hint and "KiCad" in e.value.hint
    project_settings = Project().settings
    project_settings.copper_thickness_mm = {"F.Cu": 2 * OZ1}            # the project's design
    solve_strip(session, bid, base_settings=project_settings,
                settings={"copper_thickness_mm": {"F.Cu": 3 * OZ1}})
    with pytest.raises(ServiceError):
        solve_strip(session, bid, base_settings=project_settings,
                    settings={"copper_thickness_mm": {"F.Cu": 0.0175}})
    project_settings.copper_thickness_mm = {"F.Cu": 0.05}               # a free value, not a weight
    solve_strip(session, bid, base_settings=project_settings,
                settings={"copper_thickness_mm": {"F.Cu": 0.05}})        # the design itself
    with pytest.raises(ServiceError) as e:
        solve_strip(session, bid, base_settings=project_settings,
                    settings={"copper_thickness_mm": {"F.Cu": 0.07}})
    assert "not a standard weight" in e.value.message


def test_solve_rail_refuses_a_thickness_in_microns(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    for bad in (35, 0, -1, "1oz", True):
        with pytest.raises(ServiceError) as e:
            solve_strip(session, bid, settings={"copper_thickness_mm": {"F.Cu": bad}})
        assert e.value.code == "invalid_input" and "1 oz copper is 0.035, not 35" in e.value.message


def test_engine_applies_the_project_thickness(boards, tmp_path):
    """The GUI's path: engine.solve_rail with the project's settings, on the board file as
    opened. The what-if copy is made once and reused; asking a copy (or the file) for the
    thickness it already has returns it unchanged, so nothing is applied twice."""
    from coppernicus.engine import Board, solve_rail

    board = Board.open(boards["strip"])
    project = Project.load(strip_project(tmp_path, {"F.Cu": 2 * OZ1}).path)
    rail = project.rails[0]
    thick = solve_rail(board, rail, project.settings)
    plain = solve_rail(board, rail, Project.load(strip_project(tmp_path).path).settings)
    assert 1.0 - thick.loads[0].v_min == pytest.approx((1.0 - plain.loads[0].v_min) / 2, rel=1e-6)
    assert any(a.startswith("What-if copper thickness") for a in thick.assumptions)
    variant = board.with_copper_thickness({"F.Cu": 2 * OZ1})
    assert board.with_copper_thickness({"F.Cu": 2 * OZ1}) is variant
    assert variant.with_copper_thickness({"F.Cu": 2 * OZ1}) is variant
    assert board.with_copper_thickness({"F.Cu": OZ1}) is board
    assert variant.file_thickness_mm == {"F.Cu": OZ1, "B.Cu": OZ1}
    assert [lay.thickness_mm for lay in variant.layers()] == pytest.approx([2 * OZ1, OZ1])


def test_render_map_writes_pngs_to_the_session_folder(session, boards, tmp_path):
    bid = session.open_board(boards["demo"])["board_id"]
    rid = session.solve_rail(bid, "+3V3", [{"ref": "J1", "voltage_v": 3.3}], DEMO_LOADS)["result_id"]
    out = strict_json(session.render_map(rid))                    # the busiest layer, drop
    assert out["layer"] == "B.Cu" and out["field"] == "drop"
    assert out["scale_mv"][0] == 0.0 and out["scale_mv"][1] > 0
    current = session.render_map(rid, "B.Cu", "current", region_mm=[20, 25, 40, 40], dpi=60)
    voltage = session.render_map(rid, "F.Cu", "voltage")
    assert "scale_a_mm2" in current and "scale_v" in voltage
    for doc in (out, current, voltage):
        path = tmp_path / "out" / os.path.basename(doc["path"])
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not list(boards["demo"].parent.glob("*.png"))          # never next to the board


def test_structured_errors(session, boards):
    bid = session.open_board(boards["demo"])["board_id"]
    src = [{"ref": "J1", "voltage_v": 3.3}]

    def code(fn, *args, **kw):
        with pytest.raises(ServiceError) as e:
            fn(*args, **kw)
        assert e.value.message and isinstance(e.value.hint, str)
        strict_json(e.value.to_dict())
        return e.value

    assert code(session.open_board, boards["demo"].parent / "missing.kicad_pcb").code == "file_not_found"
    assert code(session.list_nets, "b9").code == "board_not_open"
    e = code(session.net_parts, bid, "+3v3")
    assert e.code == "net_not_found" and "'+3V3'" in e.hint
    e = code(session.solve_rail, bid, "+3V3", src, [{"ref": "U9", "current_a": 1}])
    assert e.code == "part_not_on_net" and e.hint == "parts on +3V3: J1, U1, U2, U3"
    e = code(session.solve_rail, bid, "+3V3", [{"ref": "J1", "voltage_v": 3.3, "pins": ["2"]}], DEMO_LOADS)
    assert e.code == "pin_not_on_net" and e.hint == "pins of J1 on this net: 1"
    assert code(session.solve_rail, bid, "+3V3", [], DEMO_LOADS).code == "missing_source"
    assert code(session.solve_rail, bid, "+3V3", src, []).code == "missing_load"
    e = code(session.solve_rail, bid, "+3V3", src, DEMO_LOADS, settings={"temp": 50})
    assert e.code == "invalid_input" and "copper_temp_c" in e.hint
    for bad_load in ({"ref": "U1", "current": 1}, {"ref": "U1", "current_a": -1}, {"current_a": 1}):
        assert code(session.solve_rail, bid, "+3V3", src, [bad_load]).code == "invalid_input"
    e = code(session.solve_rail, bid, "+3V3", src, DEMO_LOADS,
             settings={"copper_thickness_mm": {"In1.Cu": 0.07}})
    assert e.code == "layer_not_found"
    rid = session.solve_rail(bid, "+3V3", src, DEMO_LOADS)["result_id"]
    assert code(session.probe, "r99", "B.Cu", 1, 1).code == "result_not_found"
    e = code(session.probe, rid, "In1.Cu", 1, 1)
    assert e.code == "layer_not_found" and "F.Cu, B.Cu" in e.hint
    assert code(session.render_map, rid, field="power").code == "invalid_input"
    assert code(session.cut_current, rid, "B.Cu", [1, 1], [1, 1]).code == "invalid_input"


def test_unreadable_files_and_unconnected_loads_are_structured_errors(session, boards, tmp_path):
    garbage = tmp_path / "garbage.kicad_pcb"
    garbage.write_text("(kicad_pcb (version 20240108")
    with pytest.raises(ServiceError) as e:
        session.open_board(garbage)
    assert e.value.code == "board_unreadable" and "formats" in e.value.hint
    project = tmp_path / "broken.coppernicus.yml"
    project.write_text("board: x\nrails: [\n  - net: : :\n")
    with pytest.raises(ServiceError) as e:
        session.open_board(project)
    assert e.value.code == "board_unreadable" and "solve_project" in e.value.hint
    with pytest.raises(ServiceError) as e:
        session.solve_project(project)                                  # not valid YAML
    assert e.value.code == "invalid_project"

    island = SyntheticBoard("island", two_layer_stackup())              # U2 has no copper path
    island.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    island.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5)
    island.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    island.square_pad("U2", "1", "VCC", 20.0, 10.0, 1.5)
    save_snapshot(island.snapshot(), tmp_path / "island.kipi.json.gz")
    bid = session.open_board(tmp_path / "island.kipi.json.gz")["board_id"]
    with pytest.raises(ServiceError) as e:
        session.solve_rail(bid, "VCC", [{"ref": "J1", "voltage_v": 1.0}],
                           [{"ref": "U1", "current_a": 1.0}, {"ref": "U2", "current_a": 0.5}])
    assert e.value.code == "solve_failed" and "U2" in e.value.message


def test_memory_bound_evicts_fields_but_keeps_summaries(tmp_path, boards):
    session = Session(out_dir=tmp_path / "out", max_results=1)
    bid = session.open_board(boards["strip"])["board_id"]
    a = solve_strip(session, bid, 1.0)["result_id"]
    before = session._results[a].nbytes
    session.probe(a, "F.Cu", 20.0, 0.0)                          # builds the layer's locator
    assert session._results[a].nbytes > before                   # ... and counts it
    b = solve_strip(session, bid, 2.0)["result_id"]
    with pytest.raises(ServiceError) as e:
        session.probe(a, "F.Cu", 20.0, 0.0)
    assert e.value.code == "result_evicted" and Strip.NET in e.value.hint
    assert session.probe(b, "F.Cu", 20.0, 0.0)["on_copper"]
    cmp_ = session.compare_results(a, b)                         # summaries are kept
    assert cmp_["loads"][0]["delta_drop_mv"] == pytest.approx(cmp_["loads"][0]["drop_mv_a"], rel=1e-6)


def test_changed_board_file_is_flagged(session, boards):
    bid = session.open_board(boards["strip"])["board_id"]
    st = boards["strip"].stat()
    os.utime(boards["strip"], ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert any("changed on disk" in w for w in session.list_nets(bid)["warnings"])
    assert session.open_board(boards["strip"])["board_id"] != bid    # reopened: a new id


def test_solve_project_reports_each_rail(tmp_path, boards, session):
    project = Project(board=str(boards["demo"]), rails=[
        Rail("+3V3", sources=[Source("J1", 3.3, name="Supply")],
             loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)], limits=Limits(max_drop_pct=0.25)),
        Rail("+3V3", sources=[Source("J1", 3.3)], loads=[Load("U9", 1.0)]),
    ])
    path = project.save(tmp_path / "p.coppernicus.yml")
    doc = strict_json(session.solve_project(path))
    good, bad = doc["rails"]
    assert good["verdict"] == "fail" and good["sources"][0]["name"] == "Supply"
    assert bad["error"]["code"] == "part_not_on_net"
    assert doc["verdict"] == "error"
    with pytest.raises(ServiceError) as e:
        session.solve_project(path, stop_on_error=True)
    assert e.value.code == "part_not_on_net"
    with pytest.raises(ServiceError) as e:
        session.solve_project(path, rails=["GND"])
    assert e.value.code == "invalid_input" and "+3V3" in e.value.hint


# A field the project model does not have yet: any path that rebuilds terminals from dicts
# must carry it, like the v2 fields (kind, efficiency, input, ground and sense pins).
@dataclasses.dataclass
class _SourceLater(Source):
    tolerance_pct: float = 0.0


@dataclasses.dataclass
class _RailLater(Rail):
    notes: list = dataclasses.field(default_factory=list)


def test_dict_edits_keep_fields_the_service_does_not_know(session, boards):
    from coppernicus.service.convert import rail_from_dicts, terminal_from_dict
    src = _SourceLater("J1", 3.3, ["2"], "Buck", kind="smps", efficiency=0.9, input_ref="L1",
                       ground_ref="J1", ground_pins=["3"], tolerance_pct=2.0)
    edited = terminal_from_dict({"ref": "J1", "voltage_v": 3.0}, "source", base=src)
    assert type(edited) is _SourceLater
    assert (edited.kind, edited.efficiency, edited.input_ref, edited.ground_pins,
            edited.tolerance_pct, edited.voltage) == ("smps", 0.9, "L1", ["3"], 2.0, 3.0)
    assert (edited.name, edited.pins) == ("Buck", ["2"])       # not in the dict: kept
    renamed = terminal_from_dict({"ref": "J1", "voltage_v": 3.0, "name": "", "pins": "all"}, "source",
                                 base=src)
    assert (renamed.name, renamed.pins, renamed.kind) == ("", None, "smps")   # in the dict: set
    src = dataclasses.replace(src, pins=None)

    base = _RailLater("+3V3", sources=[src],
                      loads=[Load("U2", 1.5, ground_ref="U2", ground_pins=["4"]),
                             Load("U1", 2.0, ground_ref="U1", ground_pins=["8"])], notes=["kept"])
    rail = rail_from_dicts("+3V3", [{"ref": "J1", "voltage_v": 3.3}],
                           [{"ref": "U1", "current_a": 1.0}, {"ref": "U2", "current_a": 0.5},
                            {"ref": "U3", "current_a": 0.3}],
                           {"max_drop_pct": 0.25}, base=base)
    assert type(rail) is _RailLater and rail.notes == ["kept"] and rail.limits.max_drop_pct == 0.25
    assert (rail.sources[0].efficiency, rail.sources[0].tolerance_pct) == (0.9, 2.0)
    # Matched by part (project.carry_over) when the rows moved; a new part gets the defaults.
    assert [(ld.ref, ld.ground_pins, ld.current) for ld in rail.loads] == [
        ("U1", ["8"], 1.0), ("U2", ["4"], 0.5), ("U3", None, 0.3)]

    bid = session.open_board(boards["demo"])["board_id"]
    rid = session.solve_rail(bid, "+3V3", [{"ref": "J1", "voltage_v": 3.3}],
                             [{"ref": "U1", "current_a": 2.0}], base_rail=base)["result_id"]
    kept = session._results[rid].rail
    assert kept.notes == ["kept"] and kept.sources[0].kind == "smps"
    assert kept.loads[0].ground_pins == ["8"]


def test_layers_with_only_via_rings_and_idle_vias_are_told_apart(session, tmp_path):
    """A track on F.Cu with a through via down to an empty B.Cu: B.Cu holds only the via's
    ring, and the via carries nothing but round-off, so it is not listed as a problem."""
    b = SyntheticBoard("idle_via", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.via("VCC", 20.0, 0.0)
    save_snapshot(b.snapshot(), tmp_path / "idle_via.kipi.json.gz")
    bid = session.open_board(tmp_path / "idle_via.kipi.json.gz")["board_id"]
    [net] = session.list_nets(bid, kind="all")["nets"]
    assert net["layers"] == ["F.Cu", "B.Cu"]
    assert net["layers_with_copper"] == ["F.Cu"] and net["layers_with_only_vias_or_pads"] == ["B.Cu"]
    res = session.solve_rail(bid, "VCC", [{"ref": "J1", "voltage_v": 1.0}], [{"ref": "U1", "current_a": 1.0}])
    assert res["via_count"] == 1 and res["via_current_max_a"] < 1e-6
    assert not [p for p in res["problems"] if p["kind"] == "via"]
    assert "round-off" in res["problems_note"]
