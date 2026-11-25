"""Problem ranking: never judged without a limit, judged when one is set, hot spots merged."""

import pytest

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import demo_board
from coppernicus.problems import FAIL, INFO, find_problems
from coppernicus.project import Limits, Load, Rail, Source


@pytest.fixture(scope="module")
def demo():
    board = Board(demo_board())
    rail = Rail("+3V3", sources=[Source("J1", 3.3)],
                loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])
    return solve_rail(board, rail)


def test_without_limits_nothing_fails_but_everything_is_ranked(demo):
    problems = find_problems(demo, Limits())
    assert problems and all(p.severity == INFO for p in problems)
    kinds = [p.kind for p in problems]
    assert kinds[:3] == ["load"] * 3
    assert "hotspot" in kinds and "via" in kinds
    assert kinds.index("hotspot") < kinds.index("via")


def test_drop_limit_fails_only_the_loads_beyond_it(demo):
    u = {t.name: demo.drop_pct(t) for t in demo.loads}
    limit = (u["U2"] + u["U3"]) / 2                      # between U3 and U2
    problems = find_problems(demo, Limits(max_drop_pct=limit))
    failed = {p.title.split(":")[0] for p in problems if p.severity == FAIL}
    assert failed == {"U1", "U2"}
    assert problems[0].severity == FAIL


def test_uniform_thin_track_is_one_hot_spot(demo):
    spots = [p for p in find_problems(demo) if p.kind == "hotspot" and p.layer == "F.Cu"]
    near_u3_track = [p for p in spots if abs(p.x - 18.0) < 0.5 and 26.0 <= p.y <= 33.0]
    assert len(near_u3_track) == 1
    # 0.3 A in a 0.25 mm × 35 µm track.
    assert near_u3_track[0].value == pytest.approx(0.3 / (0.25 * 0.035), rel=0.05)


def test_via_and_density_limits(demo):
    problems = find_problems(demo, Limits(max_via_current=0.95, max_current_density=1e6))
    via_fails = [p for p in problems if p.kind == "via" and p.severity == FAIL]
    assert via_fails and all(p.value > 0.95 for p in via_fails)
    assert not [p for p in problems if p.kind == "hotspot"]   # nothing above 1e6 A/mm²


def _result(net, v_ref, max_drop, **kw):
    from coppernicus.results import RailResult
    return RailResult(net, [net], v_ref, [], [], [], [], max_drop, 0.0, 0.0, 0.0, **kw)


@pytest.mark.parametrize("res, text", [
    (_result("+3V3", 3.3, 0.01234), "max drop 12.34 mV below 3.3 V"),
    # a negative rail loses its voltage toward 0 V: above its set point
    (_result("-3V3", -3.3, 0.01234, drop_sign=-1.0), "max drop 12.34 mV above -3.3 V (toward 0 V)"),
    # a net at 0 V solved alone: the return current lifts it, a rise
    (_result("GND", 0.0, 0.00567, drop_sign=-1.0), "max ground rise 5.67 mV above 0 V"),
    # a ground group: its rise is signed; below 0 V (under a negative rail) it is a shift
    (_result("GND", 0.0, 0.00567, kind="ground"), "max ground rise 5.67 mV above 0 V"),
    (_result("GND", 0.0, -0.00567, kind="ground"), "max ground shift 5.67 mV below 0 V"),
    (_result("GND", 0.0, -0.00567, drop_sign=-1.0), "max ground shift 5.67 mV below 0 V"),
    # a rail driven at 0 V inside a power tree draws its loads' current: a drop below 0 V
    (_result("VZ", 0.0, 0.00567), "max drop 5.67 mV below 0 V"),
])
def test_the_worst_drop_reads_above_or_below_its_reference_and_rise_on_a_ground(res, text):
    from coppernicus.problems import max_drop_text
    assert max_drop_text(res) == text


@pytest.mark.parametrize("res, unit", [
    (_result("+3V3", 3.3, 0.01234), "mV below 3.3 V"),
    (_result("-3V3", -3.3, 0.01234, drop_sign=-1.0), "mV above -3.3 V (toward 0 V)"),
    (_result("GND", 0.0, 0.00567, drop_sign=-1.0), "mV above 0 V (ground rise)"),
    (_result("GND", 0.0, 0.00567, kind="ground"), "mV above 0 V (ground rise)"),
    (_result("GND", 0.0, 0.0, kind="ground"), "mV above 0 V (ground rise)"),     # nothing solved yet
    (_result("GND", 0.0, -0.00567, kind="ground"), "mV below 0 V (ground shift)"),
    (_result("GND", 0.0, -0.00567, drop_sign=-1.0), "mV below 0 V (ground shift)"),
    (_result("VZ", 0.0, 0.00567), "mV below 0 V"),
])
def test_the_map_unit_names_the_side_the_summary_names(res, unit):
    """The maps' and the hover's unit (RailResult.drop_unit) says what the summary line
    (max_drop_text) says: a ground whose largest deviation lies below 0 V is a shift."""
    from coppernicus.problems import drop_words
    assert res.drop_unit() == unit
    what, where = drop_words(res.max_drop, res.v_ref, res.sign, res.is_rise())
    assert unit.startswith(f"mV {where}")
    assert (f"({what})" in unit) == res.is_rise()


def _demo_loads(voltage):
    """The demo rail's loads declared out of order (neither by drop nor its reverse)."""
    rail = Rail("+3V3", sources=[Source("J1", voltage)],
                loads=[Load("U3", 0.3), Load("U1", 2.0), Load("U2", 1.5)])
    res = solve_rail(Board(demo_board()), rail)
    return [p for p in find_problems(res) if p.kind == "load"]


def test_loads_on_a_rail_at_0_v_are_ranked_by_their_drop_in_mv():
    """At 0 V there is no %: the loads are ranked by |mV|, largest first, never by NaN."""
    loads = _demo_loads(0.0)
    assert [p.data["name"] for p in loads] == ["U1", "U2", "U3"]
    mv = [abs(p.data["drop_mv"]) for p in loads]
    assert mv == sorted(mv, reverse=True)


@pytest.mark.parametrize("voltage", [3.3, -3.3])
def test_loads_on_a_rail_with_a_nominal_keep_their_ranking_by_percent(voltage):
    loads = _demo_loads(voltage)
    assert [p.data["name"] for p in loads] == ["U1", "U2", "U3"]
    assert [p.value for p in loads] == [p.data["drop_pct"] for p in loads]
