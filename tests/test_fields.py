"""Flow-field helpers on a solved strip: current vectors, arrow sampling, isolines, NaN copper."""

import dataclasses

import numpy as np
import pytest
from shapely.geometry import Point

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import OZ1, F, Strip, strip_board
from coppernicus.fields import current_vectors, isolines, sample_flow
from coppernicus.project import Load, Rail, Source


@pytest.fixture(scope="module")
def strip():
    board = Board(strip_board())
    res = solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)]))
    return board, res, res.layers[0]


def _away_from_pads(layer):
    x = layer.centroids()[:, 0]
    return (x > Strip.X0 + 5) & (x < Strip.X1 - 5)


def test_current_vectors_point_from_source_to_load(strip):
    _, _, layer = strip
    vec = current_vectors(layer)
    assert vec.shape == (len(layer.tris), 2)
    mid = _away_from_pads(layer)
    assert vec[mid, 0].mean() > 0                       # J1 at x=0 → U1 at x=40
    mag = np.hypot(vec[mid, 0], vec[mid, 1])
    assert mag == pytest.approx(np.full(mid.sum(), 1.0 / (Strip.W * OZ1)), rel=0.05)
    assert np.abs(vec[mid, 1]).max() < 0.05 * mag.max()  # along the strip


def test_sample_flow_bounded_and_inside(strip):
    board, _, layer = strip
    copper = board.copper(Strip.NET, F)
    for spacing in (0.01, 1.0, 5.0):
        pts, vec = sample_flow(layer, spacing)
        assert 0 < len(pts) <= 2000 and pts.shape == vec.shape == (len(pts), 2)
        assert all(copper.buffer(1e-6).contains(Point(p)) for p in pts)
    pts, _ = sample_flow(layer, 1.0)
    # The 0.5 mm strip is narrower than the spacing and still gets arrows all along it.
    assert pts[:, 0].min() < Strip.X0 + 2 and pts[:, 0].max() > Strip.X1 - 2
    assert len(sample_flow(layer, 0.01, max_arrows=50)[0]) <= 50


def test_isolines_of_drop_are_vertical_on_a_strip(strip):
    _, res, layer = strip
    drop_mv = (res.v_ref - layer.voltage) * 1e3
    lines = isolines(layer, drop_mv, [5.0])
    assert lines
    for line in lines:
        assert line.shape[1] == 2 and len(line) >= 2
        assert np.ptp(line[:, 0]) < 0.6
    # ρ·x/(w·t) of track past the source pad's edge gives 5 mV.
    x5 = Strip.X0 + Strip.PAD / 2 + 5e-3 * Strip.W * OZ1 / 1.724e-5
    assert np.mean([line[:, 0].mean() for line in lines]) == pytest.approx(x5, abs=0.3)
    assert isolines(layer, drop_mv, []) == []
    assert isolines(layer, drop_mv, [1e6]) == []         # a level outside the range


def test_nan_voltages_are_skipped(strip):
    _, res, layer = strip
    v = layer.voltage.copy()
    v[: len(v) // 2] = np.nan
    half = dataclasses.replace(layer, voltage=v)
    nan_tris = np.isnan(v)[half.tris].any(axis=1)
    assert nan_tris.any() and not nan_tris.all()
    vec = current_vectors(half)
    assert np.all(vec[nan_tris] == 0) and np.isfinite(vec).all()
    pts, _ = sample_flow(half, 0.2)
    cents = half.centroids()
    nan_cents = {tuple(c) for c in cents[nan_tris]}
    assert pts.size and not any(tuple(p) in nan_cents for p in pts)
    lines = isolines(half, (res.v_ref - v) * 1e3, [5.0, 20.0])
    assert all(np.isfinite(line).all() for line in lines)
