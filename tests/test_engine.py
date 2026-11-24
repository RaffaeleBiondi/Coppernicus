"""The engine adapter on synthetic boards with closed-form answers."""

import math

import pytest

from coppernicus.engine import Board, EngineError, solve_rail, tied_nets
from coppernicus.engine.solve import TCR_CU
from coppernicus.engine.synthetic import (
    OZ1,
    Strip,
    net_tie_board,
    pins_board,
    power_chain_board,
    power_chain_project,
    strip_board,
    via_board,
)
from coppernicus.project import Load, Rail, Settings, Source

RHO = 1.724e-5      # Ω·mm, annealed copper at 20 °C (the engine's constant)


def strip_rail(current=1.0):
    return Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", current)])


def test_strip_drop_matches_rho_l_over_wt():
    board = Board(strip_board())
    res = solve_rail(board, strip_rail(1.0))
    r = RHO * Strip.length() / (Strip.W * OZ1)
    load = res.loads[0]
    assert res.drop(load) == pytest.approx(1.0 * r, rel=5e-3)
    assert res.sources[0].current == pytest.approx(1.0, rel=1e-6)
    assert res.energy_balance_error < 1e-6
    assert res.power_loss == pytest.approx(r, rel=5e-3)        # I²R with I = 1 A


def test_strip_result_arrays_are_consistent():
    res = solve_rail(Board(strip_board()), strip_rail())
    [layer] = res.layers
    assert layer.name == "F.Cu"
    assert layer.tris.max() < len(layer.points)
    assert len(layer.current_density) == len(layer.tris) == len(layer.tri_area)
    # Uniform strip: |J| = I / (w t) away from the ends.
    assert res.j_max == pytest.approx(1.0 / (Strip.W * OZ1), rel=0.05)
    assert res.loss_breakdown()["F.Cu"] == pytest.approx(res.power_loss, rel=1e-6)


def test_drop_scales_with_copper_temperature():
    board = Board(strip_board())
    cold = solve_rail(board, strip_rail(), Settings(copper_temp_c=20.0))
    hot = solve_rail(board, strip_rail(), Settings(copper_temp_c=70.0))
    ratio = hot.drop(hot.loads[0]) / cold.drop(cold.loads[0])
    assert ratio == pytest.approx(1 + TCR_CU * 50.0, rel=1e-3)
    assert any("70 °C" in a for a in hot.assumptions)


def test_via_carries_the_whole_load_current_and_plating_matters():
    board = Board(via_board())
    rail = strip_rail(1.0)
    default = solve_rail(board, rail)
    [via] = [v for v in default.vias if v.kind == "via"]
    assert via.current == pytest.approx(1.0, rel=1e-6)
    thin = solve_rail(board, rail, Settings(via_plating_um=10.0))
    assert thin.drop(thin.loads[0]) > default.drop(default.loads[0])
    # Barrel: copper tube of outer diameter d (the drill) and wall tp, ρ·dz/A.
    d, tp, dz = 0.3, 0.010, 1.5 + OZ1
    r_barrel = RHO * dz / (math.pi * (d * tp - tp * tp))
    extra = thin.drop(thin.loads[0]) - default.drop(default.loads[0])
    d_tp20 = RHO * dz / (math.pi * (d * 0.020 - 0.020 ** 2))
    assert extra == pytest.approx(r_barrel - d_tp20, rel=0.02)
    assert any("10 µm" in a for a in thin.assumptions)


def test_pins_all_list_glob_and_errors():
    board = Board(pins_board())
    assert [p.number for p in board.pads("VCC", "U1")] == ["1", "2", "3"]
    assert [p.number for p in board.pads("VCC", "U1", ["1", "3"])] == ["1", "3"]
    assert [p.number for p in board.pads("VCC", "U1", ["*"])] == ["1", "2", "3"]
    with pytest.raises(EngineError, match="pins on this net: 1, 2, 3"):
        board.pads("VCC", "U1", ["4"])
    with pytest.raises(EngineError, match="no pads on net VCC"):
        board.pads("VCC", "U9")
    parts = {p.ref: p.pins for p in board.parts_on_net("VCC")}
    assert parts == {"J1": ["1"], "U1": ["1", "2", "3"]}


def test_net_tie_is_detected_and_solved_as_a_short():
    board = Board(net_tie_board())
    [tie] = board.net_ties()
    assert (tie.ref, {tie.net_a, tie.net_b}) == ("NT1", {"VA", "VB"})
    assert tied_nets(board, "VA")[0] == ["VA", "VB"]

    rail = Rail("VA", sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)])
    res = solve_rail(board, rail)
    assert res.nets == ["VA", "VB"]
    track = RHO * (19.5 - 0.75 - 0.25) / (0.5 * OZ1) + RHO * (40.0 - 0.75 - 20.5 - 0.25) / (0.5 * OZ1)
    assert res.drop(res.loads[0]) == pytest.approx(track, rel=0.02)
    assert any("NT1" in a for a in res.assumptions)

    rail.follow_net_ties = False
    with pytest.raises(EngineError, match="U1 has no pads on net VA"):
        solve_rail(board, rail)


def test_missing_source_or_load_is_a_clear_error():
    board = Board(strip_board())
    with pytest.raises(EngineError, match="add a source"):
        solve_rail(board, Rail(Strip.NET, loads=[Load("U1", 1.0)]))
    with pytest.raises(EngineError, match="at least one load"):
        solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 1.0)]))


def test_pad_at_finds_pad_on_layer_and_nearest():
    from coppernicus.engine.synthetic import B, F
    board = Board(pins_board())
    hit = board.pad_at(12.0, 0.0, F)
    assert (hit.ref, hit.pin, hit.net, hit.layer_id) == ("U1", "2", "VCC", F)
    assert hit.position == pytest.approx((12.0, 0.0))
    assert board.pad_at(100, 100) is None
    # Within the tolerance of pad 2's edge (12.5) and far from pad 3's (13.5): pad 2.
    assert board.pad_at(12.0, 0.6, F).pin == "2"
    # These SMD pads are on F.Cu only: nothing on B.Cu, found with any layer.
    assert board.pad_at(12.0, 0.0, B) is None
    assert board.pad_at(12.0, 0.0).pin == "2"


def test_pad_at_reports_other_net():
    from coppernicus.engine.synthetic import F
    hit = Board(pins_board()).pad_at(16.0, 0.0, F)
    assert (hit.ref, hit.pin, hit.net) == ("U1", "4", "OTHER")


def test_pad_at_prefers_the_nearest_centre_where_pads_overlap():
    from shapely.geometry import box

    from coppernicus.engine.synthetic import B, F, SyntheticBoard, two_layer_stackup
    b = SyntheticBoard("overlap", two_layer_stackup())
    b.pad("J1", "1", "VCC", box(0, 0, 4, 2), layers=(F, B), drill=0.8)    # PTH, both layers
    b.pad("U1", "1", "GND", box(3, 0, 5, 2), layers=(F,))                  # overlaps J1 on F.Cu
    board = Board(b.snapshot())
    assert board.pad_at(3.5, 1.0, F).ref == "U1"       # centre (4, 1) is nearer than (2, 1)
    assert board.pad_at(2.5, 1.0, F).ref == "J1"
    assert board.pad_at(3.5, 1.0, B).ref == "J1"       # only J1 has copper on B.Cu


def test_pad_rings_are_plain_arrays_on_the_layer():
    from coppernicus.engine.synthetic import B, F
    board = Board(pins_board())
    [ring] = board.pad_rings(["u1_2"], F)
    assert ring.shape[1] == 2
    assert ring[:, 0].min() == pytest.approx(11.5) and ring[:, 0].max() == pytest.approx(12.5)
    assert board.pad_rings(["u1_2"], B) == []
    assert board.pad_rings(["nope"], F) == []


def test_demo_led_net_is_a_signal_net_and_leaves_3v3_unchanged():
    """The committed demo adds LED_A (for "Mark as rail"): not a rail by the heuristic, and
    the +3V3 results are exactly those of the board without it."""
    import numpy as np

    from coppernicus.engine.synthetic import demo_board
    plain, led = Board(demo_board()), Board(demo_board(led_net=True))
    assert "LED_A" in led.nets() and led.net_kind("LED_A") == "signal"
    assert "LED_A" not in led.power_nets() and plain.nets() == ["+3V3"]
    rail = Rail("+3V3", sources=[Source("J1", 3.3)],
                loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])
    a, b = solve_rail(plain, rail), solve_rail(led, rail)
    assert [t.v_min for t in b.loads] == [t.v_min for t in a.loads]
    assert b.max_drop == a.max_drop and b.power_loss == a.power_loss
    for la, lb in zip(a.layers, b.layers):
        assert np.array_equal(la.points, lb.points) and np.array_equal(la.voltage, lb.voltage)
    assert [p.ref for p in led.parts_on_net("LED_A")] == ["D1", "R1"]


def test_per_rail_solve_says_what_it_leaves_to_the_power_tree():
    """A v2 rail solved on its own holds its source pins and solves no ground: the
    assumptions say which fields only the power-tree solve uses. v1 rails say nothing new."""
    project = power_chain_project()
    res = solve_rail(Board(power_chain_board()), project.rail("+1V8"), project.settings)
    [note] = [a for a in res.assumptions if "power-tree" in a]
    assert "U2" in note and "ldo" in note and "sense" in note and "U3" in note
    assert not any("power-tree" in a for a in solve_rail(Board(strip_board()), strip_rail()).assumptions)
