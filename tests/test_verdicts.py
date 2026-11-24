"""Verdicts of a single rail (the approved rule): ``fail`` when an item exceeds a limit you
set; else ``not_judged`` when a set limit applies to something it could not judge (a % limit
on a 0 V net, no voltage at a load's pins, a limit that judges nothing), with each such item
and its reason listed; else ``pass``. And percentages are of |v_ref|, so a negative rail
gives positive ones."""

import json
import re

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_OK, main
from coppernicus.engine.synthetic import Strip, save_snapshot, strip_board
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.service import Session


@pytest.fixture()
def strip(tmp_path):
    path = tmp_path / "boards" / "strip.kipi.json.gz"
    path.parent.mkdir()
    save_snapshot(strip_board(), path)
    return path


@pytest.fixture()
def session(tmp_path):
    return Session(out_dir=tmp_path / "out")


def solve(session, strip, volts, **limits):
    bid = session.open_board(strip)["board_id"]
    return session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": volts}],
                              [{"ref": "U1", "current_a": 1.0}], limits=limits)


# ---------------------------------------------------------------------- negative rails

def test_negative_rail_percentages_are_of_the_magnitude(session, strip, tmp_path):
    out = solve(session, strip, -3.3)
    assert out["max_drop_pct"] > 0
    assert out["max_drop_pct"] == pytest.approx(100 * out["max_drop_mv"] / 3300, rel=1e-5)
    probe = session.probe(out["result_id"], "F.Cu", 30.0, 0.0)
    assert probe["drop_pct"] == pytest.approx(100 * probe["drop_mv"] / 3300, rel=1e-5)
    page = session.report([out["result_id"]], path=tmp_path / "neg.html")["path"]
    text = open(page, encoding="utf-8").read()
    meta = json.loads(re.search(r'id="meta">(.*?)</script>', text, re.S).group(1))
    assert meta["rails"][0]["worst"]["max_drop_pct"] == pytest.approx(out["max_drop_pct"])


@pytest.mark.xfail(strict=True, reason="needs results.drop_pct over |v_ref| (the peer's "
                                       "fix/t7-review); remove this mark after the rebase")
def test_negative_rail_drop_limit_fires(session, strip):
    out = solve(session, strip, -3.3, max_drop_pct=0.001)
    assert out["loads"][0]["drop_pct"] > 0
    assert out["verdict"] == "fail" and out["loads"][0]["exceeds_drop_limit"] is True


# ---------------------------------------------------------------------- not judged

def test_a_percent_limit_on_a_0_v_net_is_not_judged(session, strip):
    out = solve(session, strip, 0.0, max_drop_pct=1.0)
    assert out["verdict"] == "not_judged" and out["limits"] == {"max_drop_pct": 1.0}
    [item] = out["not_judged"]
    assert item["item"] == "U1" and item["limit"] == "max_drop_pct" and "0 V" in item["reason"]
    assert "Not judged: U1 (max_drop_pct)" in out["verdict_note"]
    assert out["loads"][0]["exceeds_drop_limit"] is None
    # With a limit that does judge it as well, a failure still wins.
    assert solve(session, strip, 0.0, max_drop_pct=1.0, max_ground_rise_mv=1.0)["verdict"] == "fail"


def test_a_limit_that_judges_nothing_is_not_judged(session, strip):
    out = solve(session, strip, 1.0, max_via_current_a=1.0)       # the strip has no via
    assert out["verdict"] == "not_judged"
    assert out["not_judged"] == [{"item": Strip.NET, "limit": "max_via_current_a",
                                  "reason": "the rail has no vias: the limit judges nothing"}]


def test_pass_needs_every_set_limit_to_judge(session, strip):
    out = solve(session, strip, 1.0, max_drop_pct=50.0)
    assert out["verdict"] == "pass" and out["not_judged"] == []
    again = session.get_problems(out["result_id"], limits={"max_drop_pct": 50.0, "max_via_current_a": 1})
    assert again["verdict"] == "not_judged" and again["not_judged"][0]["limit"] == "max_via_current_a"


def test_not_judged_items_are_listed_in_the_cli_text_and_the_ci_summary(strip, tmp_path, capsys):
    p = Project(board="boards/strip.kipi.json.gz", rails=[Rail(
        Strip.NET, sources=[Source("J1", 0.0)], loads=[Load("U1", 1.0)], limits=Limits(max_drop_pct=1.0))])
    project = p.save(tmp_path / "gnd.coppernicus.yml")
    out = tmp_path / "out"
    assert main(["solve", str(project), "--no-maps", "--out", str(out)]) == EXIT_OK   # not a failure
    text = capsys.readouterr().out
    assert "  NOT JUDGED U1 (max_drop_pct): 0 V nominal" in text
    [load_line] = [ln for ln in text.splitlines() if ln.startswith("  load   U1")]
    assert load_line.endswith("mV (  n/a)")                      # no % on a 0 V net
    doc = json.loads((out / "gnd.coppernicus_summary.json").read_text(encoding="utf-8"))
    assert doc["verdict"] == "not_judged" and doc["not_judged"] == [
        {"rail": Strip.NET, **doc["rails"][0]["not_judged"][0]}]
    md = ci.summary_markdown({"verdict": "not_judged", "projects": [
        {"project": "gnd.coppernicus.yml", "exit_code": 0, "doc": doc}]})
    assert "NOT JUDGED (drop ≤ 1 %)" in md
    assert "- NOT JUDGED U1 (max_drop_pct): 0 V nominal" in md
