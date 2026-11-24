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


def test_a_copper_limit_with_nothing_to_judge_passes(session, strip):
    """The final rule (as in the power tree): a via, current-density or rise limit with
    nothing to judge passes; only a drop limit that cannot judge a load is not judged."""
    out = solve(session, strip, 1.0, max_via_current_a=1.0)       # the strip has no via
    assert out["verdict"] == "pass" and out["not_judged"] == []
    again = session.get_problems(out["result_id"], limits={"max_drop_pct": 50.0, "max_via_current_a": 1})
    assert again["verdict"] == "pass" and again["not_judged"] == []


def test_a_project_with_a_rail_not_judged_is_not_judged(strip, tmp_path):
    """Project level, the same rule: a rail passing and another with items its limits could
    not judge make the project not_judged (not pass_partially_judged), and the CI index too."""
    def rail(volts, pct):
        return Rail(Strip.NET, sources=[Source("J1", volts)], loads=[Load("U1", 1.0)],
                    limits=Limits(max_drop_pct=pct))
    p = Project(board="boards/strip.kipi.json.gz", rails=[rail(1.0, 50.0), rail(0.0, 1.0)])
    doc = Session(out_dir=tmp_path / "out").solve_project(p.save(tmp_path / "two.coppernicus.yml"))
    assert [r["verdict"] for r in doc["rails"]] == ["pass", "not_judged"]
    assert doc["verdict"] == "not_judged" and len(doc["not_judged"]) == 1
    entries = [{"project": "two.coppernicus.yml", "exit_code": 0, "doc": doc}]
    assert ci.overall_verdict(entries) == "not_judged"
    no_limits = dict(doc, rails=[doc["rails"][0], dict(doc["rails"][1], not_judged=[], limits={})],
                     not_judged=[])
    assert ci.overall_verdict([dict(entries[0], doc=dict(no_limits, verdict="pass_partially_judged"))]) \
        == "pass_partially_judged"


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


SKIPPED_REASON = "not solved alone: no source or loads (solve the power tree, or set up the rail)"


def test_a_skipped_rail_with_limits_is_not_judged_not_passed(tmp_path, monkeypatch, capsys):
    """The reviewer's case: a rail that carries only limits (GND, max_ground_rise_mv) is not
    solved on its own. Its limits must not drop out of the verdict: each becomes a not-judged
    item, so the project is not_judged (exit 0), listed in the CLI text and the CI summary."""
    import shutil
    from pathlib import Path
    chain = Path(__file__).resolve().parents[1] / "examples" / "power_chain"
    shutil.copytree(chain, tmp_path / "chain")
    path = tmp_path / "chain" / "power_chain.coppernicus.yml"
    p = Project.load(path)
    for r in p.rails:
        r.limits = Limits(max_drop_pct=50.0)
    p.rails.append(Rail("GND", limits=Limits(max_ground_rise_mv=1.0)))
    p.save()
    doc = Session(out_dir=tmp_path / "out").solve_project(path)
    gnd = next(r for r in doc["rails"] if r["net"] == "GND")
    assert "skipped" in gnd
    item = {"item": "GND", "limit": "max_ground_rise_mv", "reason": SKIPPED_REASON}
    assert gnd["not_judged"] == [item]
    assert doc["verdict"] == "not_judged" and {"rail": "GND", **item} in doc["not_judged"]
    assert main(["solve", str(path), "--no-maps", "--out", str(tmp_path / "o")]) == EXIT_OK
    assert f"  NOT JUDGED GND (max_ground_rise_mv): {SKIPPED_REASON}" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["chain/power_chain.coppernicus.yml"], Path("res"), html=False)
    assert index["verdict"] == "not_judged" and index["exit_code"] == 0
    assert f"- NOT JUDGED GND (max_ground_rise_mv): {SKIPPED_REASON}" in ci.summary_markdown(index)


@pytest.mark.parametrize("volts", [1.0, -3.3, 0.0])
def test_each_load_gives_its_worst_pin_voltage(session, strip, volts):
    """v_worst_v is the pin furthest from the source in the direction the drop goes: the
    lowest on a positive rail, the highest on a negative rail or a 0 V net."""
    out = solve(session, strip, volts)
    [load] = out["loads"]
    assert load["v_worst_v"] == (load["v_min_v"] if volts > 0 else load["v_max_v"])
