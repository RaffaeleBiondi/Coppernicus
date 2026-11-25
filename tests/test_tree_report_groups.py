"""The tree report's groups show what the tree judged: each group's loads and worst card take
the tree's values (across supply and ground pins, from nominal, the tree's verdict), not the
copper drop of a rail solved alone; a tree group carries no rail-alone caveat; its nets are
"joined by net ties or links"; and report.html#group=NET opens that group."""

import json
import re
from importlib.resources import files

import pytest

from coppernicus.engine import Board
from coppernicus.engine.synthetic import power_chain_board, power_chain_project
from coppernicus.engine.tree import solve_project_tree
from coppernicus.power_tree import tree_summary
from coppernicus.project import Limits
from coppernicus.report import write_tree_report


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    project = power_chain_project()
    project.rail("+3V3").limits = Limits(max_drop_pct=1.0)       # an example limit
    tree = solve_project_tree(Board(power_chain_board()), project)
    doc = tree_summary(tree)
    path = write_tree_report(tree, doc, tmp_path_factory.mktemp("r") / "tree.html")
    text = path.read_text(encoding="utf-8")
    meta = json.loads(re.search(r'id="meta">(.*?)</script>', text, re.S).group(1))
    return doc, {r["net"]: r for r in meta["rails"]}


def test_a_groups_loads_and_worst_card_are_the_trees(page):
    doc, rails = page
    g = rails["+3V3"]
    assert g["tree"] is True
    rows = {t["name"]: t for t in g["loads"]}
    u4 = next(ld for ld in doc["loads"] if ld["name"] == "U4")
    u2 = next(v for v in doc["vrms"] if v["name"] == "U2")
    assert rows["U4"]["drop_mv"] == pytest.approx(u4["drop_mv"], rel=1e-12)
    assert rows["U4"]["v_across_v"] == pytest.approx(u4["v_across_v"], rel=1e-12)
    assert rows["U2 (input)"]["drop_mv"] == pytest.approx(u2["input_drop_mv"], rel=1e-12)
    assert rows["U2 (input)"]["exceeds"] is True and rows["U4"]["exceeds"] is False
    assert g["worst"]["load"]["name"] == "U2 (input)"
    assert g["worst"]["load"]["drop_mv"] == pytest.approx(u2["input_drop_mv"], rel=1e-12)
    assert g["worst"]["load"]["nominal_v"] == pytest.approx(3.3)
    assert all(t["pads"] for t in g["loads"])                     # still drawn on the map


def test_a_tree_group_has_no_rail_alone_caveat(page):
    _, rails = page
    for g in rails.values():
        assert not any("A rail solve covers this net only" in a for a in g["assumptions"]), g["net"]


def test_the_page_says_joined_by_net_ties_or_links_and_opens_a_group_from_its_link():
    js = files("coppernicus").joinpath("report_assets", "report.js").read_text(encoding="utf-8")
    assert "joined by net ties or links" in js
    assert "location.hash" in js and "group=" in js
