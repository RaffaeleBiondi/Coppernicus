"""Adding a pad picked on the board to a rail's sources or loads (no window needed)."""

from coppernicus.app.picking import add_picked_pin, pick_nets
from coppernicus.project import Load, Rail, Source


def test_new_part_gets_a_row_with_that_pin():
    rail = Rail("VCC")
    msg = add_picked_pin(rail, "load", "U1", "2")
    assert rail.loads == [Load("U1", 0.0, ["2"])]
    assert "U1" in msg and "2" in msg


def test_pin_is_appended_to_the_row_of_the_same_part():
    rail = Rail("VCC", loads=[Load("U1", 0.5, ["1"])])
    add_picked_pin(rail, "load", "U1", "3")
    assert rail.loads == [Load("U1", 0.5, ["1", "3"])]


def test_duplicate_pin_and_all_pins_are_left_alone():
    rail = Rail("VCC", loads=[Load("U1", 0.5, ["1"]), Load("U2", 1.0, None)])
    msg = add_picked_pin(rail, "load", "U1", "1")
    assert rail.loads[0].pins == ["1"] and "already" in msg
    msg = add_picked_pin(rail, "load", "U2", "7")
    assert rail.loads[1].pins is None and "all" in msg


def test_new_source_copies_the_voltage_of_an_existing_one():
    rail = Rail("VCC", sources=[Source("L1", 3.3, ["2"])])
    add_picked_pin(rail, "source", "L2", "1")
    assert rail.sources[1] == Source("L2", 3.3, ["1"])
    fresh = Rail("VCC")
    msg = add_picked_pin(fresh, "source", "J1", "1")
    assert fresh.sources == [Source("J1", 0.0, ["1"])] and "voltage" in msg


def test_pick_nets_follow_ties_only_when_asked():
    rail = Rail("VA")
    assert pick_nets(rail, ["VA", "VB"]) == {"VA", "VB"}
    rail.follow_net_ties = False
    assert pick_nets(rail, ["VA", "VB"]) == {"VA"}
