"""App logic that needs no window: the triangle locator used by the double-click probe."""

import numpy as np

from coppernicus.app.board_view import copper_rings
from coppernicus.fields import TriangleLocator


def test_locator_handles_separate_pieces_with_coincident_points():
    pts = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [1, 0], [2, 0]], float)   # (1, 0) twice
    tris = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 3]])
    loc = TriangleLocator(pts, tris)
    found = [loc.locate(0.2, 0.2), loc.locate(0.8, 0.8), loc.locate(1.2, 0.1), loc.locate(5, 5)]
    assert found == [0, 1, 2, -1]
    w = loc.weights(0, 0.25, 0.25)
    assert np.allclose(w.sum(), 1.0) and np.allclose(w @ pts[tris[0]], [0.25, 0.25])


def test_copper_rings_of_a_polygon_with_a_hole():
    from shapely.geometry import box
    rings = copper_rings(box(0, 0, 10, 10).difference(box(4, 4, 6, 6)))
    assert len(rings) == 2 and all(r.shape[1] == 2 for r in rings)
    assert copper_rings(None) == []


def test_arrow_glyphs_point_along_the_vector_and_scale_with_sqrt_j():
    from coppernicus.app.board_view import arrow_glyphs
    pts = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
    vec = np.array([[4.0, 0.0], [0.0, 1.0], [0.0, 0.0]])      # KiCad frame: +y is down
    segs = arrow_glyphs(pts, vec, length_mm=2.0)
    assert segs.shape == (6, 2, 2)                             # 3 strokes per arrow, zero vector dropped
    shaft1, shaft2 = segs[0], segs[3]
    assert np.allclose(shaft1, [[-1.0, 0.0], [1.0, 0.0]])      # centred, full length, tip at +x
    assert np.allclose(shaft2, [[10.0, -0.5], [10.0, 0.5]])    # √(1/4) of the length, tip toward +y
    for head in segs[1:3]:                                     # head strokes start at the tip, go back
        assert np.allclose(head[0], [1.0, 0.0]) and head[1, 0] < 1.0
    assert arrow_glyphs(np.zeros((0, 2)), np.zeros((0, 2)), 1.0).shape == (0, 2, 2)


def test_rail_filter_joins_heuristic_marked_and_configured_nets():
    from coppernicus.app.panels import rail_nets
    nets = ["+3V3", "GND", "SDA", "VBUS_X", "VIN_SNS"]
    out = rail_nets(nets, heuristic=["+3V3", "GND"], marked=["VBUS_X"], configured=["VIN_SNS"])
    assert out == ["+3V3", "GND", "VBUS_X", "VIN_SNS"]          # board order kept, SDA left out
    assert rail_nets(nets, [], ["NOT_ON_BOARD"], []) == []


def test_macos_app_name_is_written_to_the_main_bundle():
    import sys

    import pytest

    from coppernicus.app import set_macos_app_name
    if sys.platform != "darwin":
        assert set_macos_app_name("Coppernicus") is False
        return
    pytest.importorskip("Foundation")
    from Foundation import NSBundle
    assert set_macos_app_name("Coppernicus") is True
    assert NSBundle.mainBundle().infoDictionary()["CFBundleName"] == "Coppernicus"


def test_drop_mv_is_a_rise_on_a_ground_group():
    from coppernicus.app.board_view import drop_mv
    from coppernicus.results import RailResult
    rail = RailResult("VCC", ["VCC"], 3.3, [], [], [], [], 0.0, 0.0, 0.0, 0.0)
    gnd = RailResult("GND", ["GND"], 0.0, [], [], [], [], 0.0, 0.0, 0.0, 0.0, kind="ground")
    v = np.array([3.29, 0.002])
    assert np.allclose(drop_mv(rail, v), [10.0, 3298.0])
    assert np.allclose(drop_mv(gnd, v), [3290.0, 2.0])


def test_rail_index_skips_rows_without_a_part():
    from coppernicus.app.panels import rail_index
    has_part = [True, False, True]
    assert [rail_index(has_part, r) for r in range(3)] == [0, None, 1]
    assert rail_index(has_part, -1) is None and rail_index(has_part, 5) is None


def test_the_drop_map_of_a_negative_rail_is_positive_toward_0_v():
    from coppernicus.app.board_view import drop_mv
    from coppernicus.results import RailResult
    neg = RailResult("VEE", ["VEE"], -3.3, [], [], [], [], 0.0, 0.0, 0.0, 0.0, drop_sign=-1.0)
    assert np.allclose(drop_mv(neg, np.array([-3.29, -3.3])), [10.0, 0.0])
    assert "toward 0 V" in neg.drop_unit()
    gnd_alone = RailResult("GND", ["GND"], 0.0, [], [], [], [], 0.0, 0.0, 0.0, 0.0, drop_sign=-1.0)
    assert np.allclose(drop_mv(gnd_alone, np.array([0.002])), [2.0]) and gnd_alone.is_rise()


def test_the_tree_header_of_a_negative_rail_reads_its_drop_toward_0_v():
    from coppernicus.app.main_window import tree_header_html
    from coppernicus.power_tree import GroupVerdict
    from coppernicus.results import RailResult
    neg = RailResult("VEE", ["VEE"], -3.3, [], [], [], [], 0.01234, 0.0, 0.002, 0.0, drop_sign=-1.0)
    head = tree_header_html("VEE", neg, GroupVerdict("not_judged", 0, {}, {}))
    assert "max drop 12.34 mV above -3.3 V (toward 0 V)" in head and "below" not in head
    gnd = RailResult("GND", ["GND"], 0.0, [], [], [], [], -0.00567, 0.0, 0.002, 0.0, kind="ground")
    head = tree_header_html("GND", gnd, GroupVerdict("pass", 0, {}, {}))
    assert "max ground shift 5.67 mV below 0 V" in head and "rise" not in head


def test_the_rail_header_of_a_net_at_0_v_solved_alone_reads_a_rise():
    from coppernicus.app.main_window import rail_header_html
    from coppernicus.project import Limits
    from coppernicus.results import RailResult
    gnd = RailResult("GND", ["GND"], 0.0, [], [], [], [], 0.00567, 0.0, 0.002, 0.0, drop_sign=-1.0)
    head = rail_header_html(gnd, Limits(), [])
    assert "max ground rise 5.67 mV above 0 V" in head and "max drop" not in head
    neg = RailResult("VEE", ["VEE"], -3.3, [], [], [], [], 0.01234, 0.0, 0.002, 0.0, drop_sign=-1.0)
    assert "max drop 12.34 mV above -3.3 V (toward 0 V)" in rail_header_html(neg, Limits(), [])
