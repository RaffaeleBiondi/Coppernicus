"""Before/after on a strip: per-load deltas through the service, and the difference layer."""

import numpy as np
import pytest

from coppernicus.compare import difference_layer, same_mesh
from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import Strip, strip_board
from coppernicus.project import Load, Rail, Settings, Source


def solve(board, current, settings=None):
    rail = Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", current)])
    return solve_rail(board, rail, settings or Settings())


@pytest.fixture(scope="module")
def runs():
    board = Board(strip_board())
    return solve(board, 1.0), solve(board, 2.0), solve(board, 1.0, Settings(max_edge_mm=0.2))


def test_compare_strip_two_currents(runs):
    from coppernicus.app.comparing import Run, compare_runs
    one, two, _ = runs
    drop_1a = one.drop(one.loads[0])
    rail = Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)])
    cmp_ = compare_runs(Run(one, rail, Settings(), "strip"), Run(two, rail, Settings(), "strip"))
    assert cmp_["result_a"] == "baseline" and cmp_["result_b"] == "current"
    assert cmp_["max_drop_label"] == "Largest drop on the rail, mV below 1 V"
    assert cmp_["loads"][0]["delta_drop_mv"] == pytest.approx(drop_1a * 1e3, rel=1e-6)   # linear
    diff = difference_layer(one.layers[0], two.layers[0])
    assert same_mesh(one.layers[0], two.layers[0])
    assert np.nanmax(np.abs(diff.voltage)) == pytest.approx(drop_1a, rel=0.01)
    assert np.all(diff.voltage[np.isfinite(diff.voltage)] <= 1e-12)    # b − a: 2 A is lower everywhere


def test_difference_on_another_mesh_is_interpolated(runs):
    one, _, fine = runs
    assert not same_mesh(one.layers[0], fine.layers[0])
    diff = difference_layer(one.layers[0], fine.layers[0])
    assert diff.points is one.layers[0].points                         # on a's nodes
    finite = diff.voltage[np.isfinite(diff.voltage)]
    assert finite.size > 0.95 * diff.voltage.size
    assert np.abs(finite).max() < 0.02 * one.drop(one.loads[0])        # same physics, another mesh


def test_nodes_off_the_other_copper_are_nan(runs):
    import dataclasses
    one, two, _ = runs
    b = two.layers[0]
    keep = b.centroids()[:, 0] < 20.0                                  # b's copper only for x < 20
    cut = dataclasses.replace(b, tris=b.tris[keep], current_density=b.current_density[keep],
                              tri_power=b.tri_power[keep], tri_area=b.tri_area[keep])
    diff = difference_layer(one.layers[0], cut)
    x = one.layers[0].points[:, 0]
    assert np.isnan(diff.voltage[x > 21.0]).all() and np.isfinite(diff.voltage[x < 19.0]).all()


def _rail_result(v_ref):
    """A rail solved alone at ``v_ref``: the current flows into the copper at or below 0 V."""
    from coppernicus.results import RailResult
    return RailResult("V", ["V"], v_ref, [], [], [], [], 0.0, 0.0, 0.0, 0.0,
                      drop_sign=-1.0 if v_ref <= 0 else 1.0)


@pytest.mark.parametrize("v_base, v_now, label", [
    (3.3, 3.3, "Largest drop on the rail, mV below 3.3 V"),
    (-3.3, -3.3, "Largest drop on the rail, mV above -3.3 V (toward 0 V)"),
    (0.0, 0.0, "Largest ground rise on the rail, mV above 0 V"),
    # another reference in the baseline: the current run's side, the baseline's named
    (3.3, 5.0, "Largest drop on the rail, mV below 5 V (baseline: drop below 3.3 V)"),
])
def test_the_largest_drop_is_labelled_with_its_side_of_the_reference(v_base, v_now, label):
    from coppernicus.app.comparing import largest_drop_label
    assert largest_drop_label(_rail_result(v_base), _rail_result(v_now)) == label
