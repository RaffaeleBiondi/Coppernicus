"""The project → KiPI ``PISetup`` mapping (engine/pisetup.py) on synthetic boards."""

import pytest

from coppernicus.engine import Board, EngineError
from coppernicus.engine.pisetup import terminal_names, to_pisetup, validate_setup
from coppernicus.engine.synthetic import (
    F,
    PowerChain,
    SyntheticBoard,
    net_tie_board,
    pins_board,
    power_chain_board,
    power_chain_project,
    two_layer_stackup,
)
from coppernicus.project import Link, Load, Project, Rail, Settings, Source


def pin(ref):
    """A KiPI ``PinRef`` as a plain tuple (tests do not import KiPI)."""
    return None if ref is None else (ref.ref, ref.net, list(ref.pins))


@pytest.fixture(scope="module")
def chain_board():
    return Board(power_chain_board())


def test_to_pisetup_maps_sources_loads_links(chain_board):
    g = PowerChain
    project = power_chain_project()
    setup = to_pisetup(chain_board, project)

    vrms = {v.name: v for v in setup.vrms}
    assert list(vrms) == ["J1", "U1", "U2"]
    j1, u1, u2 = vrms["J1"], vrms["U1"], vrms["U2"]
    assert (j1.kind, j1.voltage, pin(j1.output), j1.input) == ("source", g.V_SRC,
                                                               ("J1", "VIN", ["1"]), None)
    assert pin(j1.ground) == ("J1", "GND", ["2"])
    assert (u1.kind, u1.voltage, u1.efficiency, u1.quiescent_a) == ("smps", g.V_SMPS, g.ETA,
                                                                    g.IQ_SMPS)
    assert pin(u1.output) == ("U1", "+3V3", ["2"])
    assert pin(u1.input) == ("U1", "VIN", ["1"])
    assert pin(u1.ground) == ("U1", "GND", ["3"])
    assert (u2.kind, u2.voltage, u2.quiescent_a) == ("ldo", g.V_LDO, g.IQ_LDO)
    assert pin(u2.input) == ("U2", "+3V3_F", ["1"])
    assert pin(u2.sense) == ("U3", "+1V8", ["1"])
    assert pin(u2.ground_sense) == ("U3", "GND", ["2"])

    loads = {ld.name: ld for ld in setup.loads}
    assert list(loads) == ["U4", "U3"]
    assert (pin(loads["U3"].supply), loads["U3"].current, pin(loads["U3"].ground)) == (
        ("U3", "+1V8", ["1"]), g.I_U3, ("U3", "GND", ["2"]))
    assert (pin(loads["U4"].supply), loads["U4"].current) == (("U4", "+3V3", ["1"]), g.I_U4)

    [fb1] = setup.links
    assert (fb1.name, pin(fb1.side_a), pin(fb1.side_b), fb1.resistance) == (
        "FB1", ("FB1", "+3V3", ["1"]), ("FB1", "+3V3_F", ["2"]), g.R_FB)

    # No verdicts and no placeholder limits inside the engine: Coppernicus judges.
    assert setup.via_current_limit_a is None
    assert all(ld.tol_min is None and ld.tol_max is None for ld in setup.loads)
    # The same solve settings as the per-rail solve.
    assert setup.settings.max_edge_mm == project.settings.max_edge_mm
    # KiPI's own checks accept the mapped tree.
    assert validate_setup(setup) == []


def test_settings_go_through_the_per_rail_settings(chain_board):
    project = power_chain_project()
    project.settings = Settings(max_edge_mm=0.3, via_plating_um=25.0)
    setup = to_pisetup(chain_board, project)
    assert setup.settings.max_edge_mm == 0.3
    assert [(r.kind, r.plating_mm) for r in setup.settings.via_rules] == [("any", 0.025)]


def test_pins_all_and_glob_patterns_are_resolved_on_the_board():
    board = Board(pins_board())          # U1 pins 1, 2, 3 on VCC, pin 4 on OTHER
    project = Project(rails=[Rail("VCC", sources=[Source("J1", 1.0)],
                                  loads=[Load("U1", 1.0, ["[12]"]), Load("U1", 0.5, ["3"])])])
    setup = to_pisetup(board, project)
    assert pin(setup.vrms[0].output) == ("J1", "VCC", [])             # all: every pad
    assert [pin(ld.supply) for ld in setup.loads] == [("U1", "VCC", ["1", "2"]),
                                                      ("U1", "VCC", ["3"])]
    project.rails[0].loads[1].pins = ["9"]
    with pytest.raises(EngineError, match=r"U1.*'9'"):
        to_pisetup(board, project)


def test_duplicate_labels_get_their_net(chain_board):
    project = power_chain_project()
    project.rail("+3V3").loads[0].name = "CPU"
    project.rail("+1V8").loads[0].name = "CPU"
    setup = to_pisetup(chain_board, project)
    assert [ld.name for ld in setup.loads] == ["CPU (+3V3)", "CPU (+1V8)"]
    names = terminal_names(project)
    assert names[("load", "+3V3", 0)] == "CPU (+3V3)"
    assert names[("source", "+1V8", 0)] == "U2"
    assert names[("link", "", 0)] == "FB1"


def test_a_rail_with_two_sources_is_refused(chain_board):
    project = power_chain_project()
    project.rail("+3V3").sources.append(Source("U4", 3.3, ["1"]))
    with pytest.raises(EngineError, match=r"\+3V3.*U1.*U4"):
        to_pisetup(chain_board, project)


def test_a_switcher_needs_its_efficiency(chain_board):
    project = power_chain_project()
    project.rail("+3V3").sources[0].efficiency = None
    with pytest.raises(EngineError, match="U1.*efficiency"):
        to_pisetup(chain_board, project)


def test_a_missing_link_pad_names_the_link(chain_board):
    project = power_chain_project()
    project.links[0] = Link("FB1", "FB1", ["7"], "+3V3", ["2"], "+3V3_F", 0.05)
    with pytest.raises(EngineError, match="FB1.*'7'"):
        to_pisetup(chain_board, project)


def test_a_load_on_a_regulator_input_would_count_twice(chain_board):
    project = power_chain_project()
    project.rail("VIN").loads.append(Load("U1", 0.3, ["1"], name="Buck input"))
    with pytest.raises(EngineError, match="Buck input.*U1.*remove the load or the VRM input"):
        to_pisetup(chain_board, project)


def test_engine_options(chain_board):
    project = power_chain_project()
    project.engine_options = {"excluded_nets": ["+1V8"]}
    assert to_pisetup(chain_board, project).excluded_nets == ["+1V8"]
    project.engine_options = {"solver": "direct"}
    with pytest.raises(EngineError, match="solver.*excluded_nets"):
        to_pisetup(chain_board, project)
    project.engine_options = {"excluded_nets": "+1V8"}
    with pytest.raises(EngineError, match="excluded_nets"):
        to_pisetup(chain_board, project)


def test_a_plain_source_has_no_input_and_empty_rails_are_skipped(chain_board):
    project = power_chain_project()
    project.rail("VIN").sources[0].input_net = "+3V3"    # left over from another kind
    project.rails.append(Rail("+5V"))                     # picked, not set up yet
    setup = to_pisetup(chain_board, project)
    assert setup.vrms[0].input is None
    assert len(setup.vrms) == 3


def test_net_ties_become_short_links():
    board = Board(net_tie_board())        # VA: J1 → NT1.1, VB: NT1.2 → U1
    project = Project(rails=[Rail("VA", sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)])])
    setup = to_pisetup(board, project)
    assert pin(setup.loads[0].supply) == ("U1", "VB", [])
    [tie] = setup.links
    assert (tie.name, pin(tie.side_a), pin(tie.side_b), tie.resistance) == (
        "net tie NT1", ("NT1", "VA", ["1"]), ("NT1", "VB", ["2"]), 0.0)
    assert validate_setup(setup) == []
    # Not following ties: U1 is not on VA.
    project.rails[0].follow_net_ties = False
    with pytest.raises(EngineError, match="U1 has no pads on net VA"):
        to_pisetup(board, project)


def test_pads_on_two_tied_nets_are_refused():
    board = Board(net_tie_board())
    project = Project(rails=[Rail("VA", sources=[Source("J1", 1.0)], loads=[Load("NT1", 0.1)])])
    with pytest.raises(EngineError, match="NT1.*VA.*VB"):
        to_pisetup(board, project)


def test_a_load_on_a_link_pad_would_count_twice(chain_board):
    """A load standing in for what flows through a ferrite (the way a single-rail solve is
    set up) and the ferrite as a link would draw the downstream current twice."""
    project = power_chain_project()
    project.rail("+3V3").loads.append(Load("FB1", 2.0, ["1"], name="To +3V3_F"))
    with pytest.raises(EngineError, match="To \\+3V3_F.*FB1.*remove the load or the link"):
        to_pisetup(chain_board, project)


def test_ties_of_a_rail_that_does_not_follow_them_are_left_out():
    """follow_net_ties: false on VA keeps VA apart, even when the tie is reached from VB."""
    board = Board(net_tie_board())
    project = Project(rails=[
        Rail("VA", sources=[Source("J1", 1.0)], follow_net_ties=False),
        Rail("VB", sources=[Source("U1", 1.0)]),
    ])
    setup = to_pisetup(board, project)
    assert setup.links == []
    assert validate_setup(setup) == []


def _four_net_tie_board():
    """NT1, one net-tie footprint on VA, VB, VC and VD (three ties with one reference)."""
    b = SyntheticBoard("tie4", two_layer_stackup())
    lib = "NetTie:NetTie-4"
    b.square_pad("J1", "1", "VA", 0.0, 0.0, 1.5)
    for i, net in enumerate(("VA", "VB", "VC", "VD"), start=1):
        b.square_pad("NT1", str(i), net, 10.0 + i, 0.0, 0.5, value="NetTie_4", lib_id=lib)
        b.track(net, F, 0.5, (10.0 + i, 0.0), (10.0 + i, 5.0))
    b.square_pad("U1", "1", "VD", 14.0, 5.0, 1.0)
    b.track("VA", F, 0.5, (0.0, 0.0), (11.0, 0.0))
    return b.snapshot()


def test_names_never_collide():
    board = Board(_four_net_tie_board())
    project = Project(rails=[Rail("VA", sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)])])
    setup = to_pisetup(board, project)
    names = [k.name for k in setup.links]
    assert len(names) == 3 and len(set(names)) == 3, names
    assert validate_setup(setup) == []
    # A label typed by hand that looks like a generated one.
    project = power_chain_project()
    project.rail("+3V3").loads = [Load("U4", 0.1, ["1"], name="X"), Load("U4", 0.1, ["1"], name="X"),
                                  Load("U4", 0.1, ["1"], name="X (+3V3) (2)")]
    names = list(terminal_names(project).values())
    assert len(names) == len(set(names)), names
