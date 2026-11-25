"""tree_summary's load rows name the net a load's pins are on ("net") and the rail it is
listed in ("rail"): they differ for a load past a net tie. Additive keys."""

from coppernicus.engine import Board
from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
from coppernicus.engine.tree import solve_project_tree
from coppernicus.power_tree import tree_summary
from coppernicus.project import Load, Project, Rail, Source


def test_a_load_row_names_its_pin_net_and_its_listed_rail():
    b = SyntheticBoard("tie_rows", two_layer_stackup())
    lib = "NetTie:NetTie-2_SMD_Pad0.5mm"
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U2", "1", "VCC", 5.0, 0.0, 1.5)
    b.square_pad("NT1", "1", "VCC", 9.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("NT1", "2", "VCC_B", 10.5, 0.0, 0.5, value="NetTie_2", lib_id=lib)
    b.square_pad("U1", "1", "VCC_B", 30.0, 0.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (9.5, 0.0))
    b.track("VCC_B", F, 0.5, (10.5, 0.0), (30.0, 0.0))
    project = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)],
                                  loads=[Load("U1", 1.0), Load("U2", 0.5)])])
    doc = tree_summary(solve_project_tree(Board(b.snapshot()), project))
    rows = {ld["name"]: ld for ld in doc["loads"]}
    assert (rows["U1"]["net"], rows["U1"]["rail"]) == ("VCC_B", "VCC")
    assert (rows["U2"]["net"], rows["U2"]["rail"]) == ("VCC", "VCC")


def test_the_cli_vrm_table_has_an_i_max_column_only_when_a_rating_exists():
    """power_tree.tree_text: "I max" next to "I out" when some VRM row has i_max_a (a rated
    output current, PI_IMAX: the value the tree judges), and the table exactly as before when
    none has."""
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    from coppernicus.power_tree import RATING_RULE, tree_text
    project = power_chain_project()
    doc = tree_summary(solve_project_tree(Board(power_chain_board()), project))
    plain = tree_text(doc)
    assert "I max" not in plain[1] and not any("i_max_a" in v for v in doc["vrms"])
    project.rail("+3V3").sources[0].current_limit_a = 3.0
    rated = tree_text(tree_summary(solve_project_tree(Board(power_chain_board()), project)))
    assert rated[1].index("I out") < rated[1].index("I max") < rated[1].index("V in")
    u1 = next(line for line in rated if line.lstrip().startswith("U1 "))
    j1 = next(line for line in rated if line.lstrip().startswith("J1 "))
    assert "3.0000A" in u1 and "—" in j1
    assert len(rated) == len(plain) + 1                                  # the rule's assumption line
    assert [line for line in rated if RATING_RULE in line] == [f"  assumption: {RATING_RULE}"]
