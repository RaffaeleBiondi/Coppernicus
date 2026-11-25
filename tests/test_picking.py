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


# ---------------------------------------------------------------------- AC probes and models

def test_the_first_supply_pick_sets_the_probes_part_and_net():
    from coppernicus.app.picking import add_probe_pin
    from coppernicus.project import Probe
    p = Probe("", "", "")
    msg = add_probe_pin(p, "supply", "U1", "1", "+3V3")
    assert (p.name, p.ref, p.net, p.pins) == ("U1", "U1", "+3V3", ["1"]) and "U1.1" in msg
    msg = add_probe_pin(p, "supply", "U1", "4", "+3V3")
    assert p.pins == ["1", "4"] and "4" in msg
    msg = add_probe_pin(p, "supply", "U1", "4", "+3V3")
    assert p.pins == ["1", "4"] and "already" in msg


def test_a_supply_pick_on_another_net_or_part_is_refused():
    from coppernicus.app.picking import add_probe_pin
    from coppernicus.project import Probe
    p = Probe("U1", "U1", "+3V3", ["1"])
    msg = add_probe_pin(p, "supply", "U1", "2", "GND")
    assert p.pins == ["1"] and "GND" in msg and "+3V3" in msg
    msg = add_probe_pin(p, "supply", "U2", "1", "+3V3")
    assert p.pins == ["1"] and p.ref == "U1" and "U2" in msg
    every = Probe("U1", "U1", "+3V3", None)
    msg = add_probe_pin(every, "supply", "U1", "7", "+3V3")
    assert every.pins is None and "all" in msg


def test_ground_picks_set_the_ground_pins_on_one_net():
    from coppernicus.app.picking import add_probe_pin
    from coppernicus.project import Probe
    p = Probe("U1", "U1", "+3V3", ["1"])
    msg = add_probe_pin(p, "ground", "U1", "2", "GND")
    assert (p.ground_ref, p.ground_net, p.ground_pins) == ("", "GND", ["2"]) and "GND" in msg
    add_probe_pin(p, "ground", "U1", "5", "GND")
    assert p.ground_pins == ["2", "5"]
    msg = add_probe_pin(p, "ground", "U1", "6", "AGND")
    assert p.ground_net == "GND" and "AGND" in msg
    msg = add_probe_pin(p, "ground", "U1", "1", "+3V3")
    assert p.ground_pins == ["2", "5"] and "+3V3" in msg
    other = Probe("U1", "U1", "+3V3", ["1"])
    add_probe_pin(other, "ground", "J1", "2", "GND")            # another part's ground pin
    assert (other.ground_ref, other.ground_net, other.ground_pins) == ("J1", "GND", ["2"])
    msg = add_probe_pin(other, "ground", "U1", "2", "GND")
    assert other.ground_pins == ["2"] and "J1" in msg


def test_a_ground_pick_first_then_the_supply_pick_on_the_same_part():
    from coppernicus.app.picking import add_probe_pin
    from coppernicus.project import Probe
    p = Probe("", "", "")
    add_probe_pin(p, "ground", "U1", "2", "GND")
    add_probe_pin(p, "supply", "U1", "1", "+3V3")
    assert (p.ref, p.ground_ref, p.ground_net) == ("U1", "", "GND")     # "" = the probe's own part


def test_a_picked_part_toggles_in_a_model_and_leaves_any_other():
    from coppernicus.app.picking import toggle_cap_ref
    from coppernicus.project import CapModel
    models = [CapModel("10u", refs=["C1"]), CapModel("100n", refs=["C2"])]
    msg = toggle_cap_ref(models, 1, "C3")
    assert models[1].refs == ["C2", "C3"] and "C3" in msg and "100n" in msg
    msg = toggle_cap_ref(models, 1, "C1")
    assert models[0].refs == [] and models[1].refs == ["C2", "C3", "C1"] and "10u" in msg
    msg = toggle_cap_ref(models, 1, "C2")
    assert models[1].refs == ["C3", "C1"] and "C2" in msg



def test_a_supply_pick_on_the_probes_ground_net_is_refused_in_either_order():
    from coppernicus.app.picking import add_probe_pin
    from coppernicus.project import Probe
    p = Probe("", "", "")
    add_probe_pin(p, "ground", "U1", "2", "GND")
    msg = add_probe_pin(p, "supply", "U1", "3", "GND")
    assert not p.ref and p.pins is None and "ground net" in msg
