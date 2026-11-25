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
