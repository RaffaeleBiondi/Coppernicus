"""Coppernicus's current-density default: 60 A/mm² at 1 oz, scaled ×√(35 µm / t) with each
layer's copper thickness as solved. Exceeding it is an attention item, never a fail: no
verdict, not-judged list or exit code changes. A max_current_density the user sets replaces
it (a fail when exceeded, no attention items). Synthetic boards only."""

import json
import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from shapely.geometry import box

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import (
    IN1,
    OZ1,
    F,
    SyntheticBoard,
    four_layer_stackup,
    power_chain_board,
    power_chain_project,
    save_snapshot,
    two_layer_stackup,
)
from coppernicus.engine.tree import solve_project_tree
from coppernicus.problems import ATTENTION, FAIL, INFO, find_problems, find_tree_problems
from coppernicus.project import Limits, Load, Project, Rail, Settings, Source

NET = "VCC"
NECK_W = 0.5                    # mm: the neck between two 5 mm wide pours


def neck_board(t_cu: float = OZ1, layer: int = F, stackup=None):
    """Two 5 mm × 10 mm pours on ``layer`` joined by a 0.5 mm × 3 mm neck, J1 at the left
    end and U1 at the right: the neck carries the whole current, J = I / (0.5 mm · t)."""
    b = SyntheticBoard("neck", stackup or two_layer_stackup(t_cu))
    copper = box(0, 0, 10, 5).union(box(10, 2.5 - NECK_W / 2, 13, 2.5 + NECK_W / 2)).union(box(13, 0, 23, 5))
    b.zone(NET, layer, copper)
    b.square_pad("J1", "1", NET, 1.5, 2.5, 2.0, layers=(layer,))
    b.square_pad("U1", "1", NET, 21.5, 2.5, 2.0, layers=(layer,))
    return b.snapshot()


def neck_rail(amps: float, limits: Limits = None) -> Rail:
    return Rail(NET, sources=[Source("J1", 1.0)], loads=[Load("U1", amps)], limits=limits or Limits())


def solve_neck(amps: float, t_cu: float = OZ1, settings: Settings = None, **kw):
    return solve_rail(Board(neck_board(t_cu, **kw)), neck_rail(amps), settings)


def amps_for(j: float, t_cu: float) -> float:
    """The current that drives the neck at ``j`` A/mm²."""
    return j * NECK_W * t_cu


def attention(problems):
    return [p for p in problems if p.severity == ATTENTION]


@pytest.fixture(scope="module")
def neck_80():
    """A 1 oz neck at 80 A/mm²: above the 60 A/mm² default."""
    return solve_neck(amps_for(80.0, OZ1))


# ---------------------------------------------------------------------- the threshold

@pytest.mark.parametrize("t_mm, j", [(0.0175, 84.85), (0.035, 60.0), (0.070, 42.43), (0.105, 34.64)])
def test_the_default_is_60_at_1_oz_scaled_by_the_square_root_of_the_thickness(t_mm, j):
    from coppernicus.problems import density_default
    assert density_default(t_mm, outer=True) == pytest.approx(j, abs=0.01)
    assert density_default(t_mm, outer=False) == pytest.approx(j, abs=0.01)


def test_external_and_internal_layers_have_their_own_constant_both_60_for_now():
    from coppernicus import problems
    assert problems.DENSITY_DEFAULT_EXTERNAL_A_MM2 == 60.0
    assert problems.DENSITY_DEFAULT_INTERNAL_A_MM2 == 60.0
    assert problems.DENSITY_DEFAULT_REF_MM == 0.035


# ---------------------------------------------------------------------- a rail

def test_a_1_oz_neck_above_60_is_an_attention_item_not_a_fail(neck_80):
    problems = find_problems(neck_80, Limits())
    [spot] = attention(problems)
    assert spot.kind == "hotspot" and spot.layer == "F.Cu"
    assert spot.value == pytest.approx(80.0, rel=0.05)              # the spot's mean |J|
    assert spot.data["mean_j_a_mm2"] == pytest.approx(80.0, rel=0.05)
    assert spot.data["default_j_a_mm2"] == pytest.approx(60.0)
    assert spot.data["thickness_mm"] == pytest.approx(OZ1)
    assert "60 A/mm²" in spot.title and "35 µm" in spot.title
    assert "attention only" in spot.detail.lower() and "not a verdict" in spot.detail
    assert "max_current_density" in spot.detail
    assert not [p for p in problems if p.severity == FAIL]


def test_attention_sorts_between_fails_and_ranked_items(neck_80):
    problems = find_problems(neck_80, Limits(max_drop_pct=1e-6))      # every load fails
    ranks = [{FAIL: 0, ATTENTION: 1, INFO: 2}[p.severity] for p in problems]
    assert ranks == sorted(ranks) and set(ranks) == {0, 1, 2}


def test_the_ranked_items_are_unchanged_attention_is_added(neck_80):
    """The info rows (the top 1 % spots, the vias, the loads) are the same as before the
    default: attention rows come on top of them."""
    problems = find_problems(neck_80, Limits(), top=5)
    info_spots = [p for p in problems if p.kind == "hotspot" and p.severity == INFO]
    assert info_spots and "top 1%" in info_spots[0].detail


def test_the_verdict_and_not_judged_are_unchanged_by_attention(neck_80):
    from coppernicus.service.convert import verdict
    none = verdict(find_problems(neck_80, Limits()), Limits(), neck_80)
    assert none["verdict"] == "not_judged" and none["failed"] == 0 and none["not_judged"] == []
    assert none["attention"] == 1
    lim = Limits(max_drop_pct=50.0)
    judged = verdict(find_problems(neck_80, lim), lim, neck_80)
    assert judged["verdict"] == "pass" and judged["failed"] == 0 and judged["attention"] == 1


def test_below_the_default_there_is_no_attention():
    res = solve_neck(amps_for(50.0, OZ1))
    assert not attention(find_problems(res, Limits()))


def test_at_2_oz_the_threshold_is_42_4():
    res = solve_neck(amps_for(50.0, 0.070), t_cu=0.070)            # 50 A/mm²: under 60, over 42.4
    [spot] = attention(find_problems(res, Limits()))
    assert spot.data["default_j_a_mm2"] == pytest.approx(42.43, abs=0.01)
    assert spot.data["thickness_mm"] == pytest.approx(0.070)
    assert spot.value == pytest.approx(50.0, rel=0.05)
    assert "42.4 A/mm²" in spot.title and "70 µm" in spot.title


def test_the_copper_thickness_what_if_sets_the_threshold():
    """t is the thickness as solved: the project's copper_thickness_mm what-if, not the
    board file's."""
    res = solve_neck(amps_for(50.0, 0.070), settings=Settings(copper_thickness_mm={"F.Cu": 0.070}))
    [spot] = attention(find_problems(res, Limits()))
    assert spot.data["thickness_mm"] == pytest.approx(0.070)
    assert spot.data["default_j_a_mm2"] == pytest.approx(42.43, abs=0.01)


def test_a_user_max_current_density_fails_and_replaces_the_default(neck_80):
    over = find_problems(neck_80, Limits(max_current_density=70.0))
    assert [p.kind for p in over if p.severity == FAIL] == ["hotspot"]
    assert not attention(over)
    above = find_problems(neck_80, Limits(max_current_density=200.0))   # more lenient than 60
    assert not attention(above) and not [p for p in above if p.severity == FAIL]


def test_inner_layers_use_the_internal_constant(monkeypatch):
    from coppernicus import problems
    stack = four_layer_stackup(copper=(OZ1, OZ1, OZ1, OZ1))
    res = solve_rail(Board(neck_board(layer=IN1, stackup=stack)), neck_rail(amps_for(50.0, OZ1)))
    lf = res.layer("In1.Cu")
    assert lf.thickness_mm == pytest.approx(OZ1) and lf.outer is False
    assert not attention(find_problems(res, Limits()))
    monkeypatch.setattr(problems, "DENSITY_DEFAULT_INTERNAL_A_MM2", 30.0)
    [spot] = attention(find_problems(res, Limits()))
    assert spot.layer == "In1.Cu" and spot.data["default_j_a_mm2"] == pytest.approx(30.0)
    monkeypatch.setattr(problems, "DENSITY_DEFAULT_INTERNAL_A_MM2", 60.0)
    monkeypatch.setattr(problems, "DENSITY_DEFAULT_EXTERNAL_A_MM2", 30.0)
    assert not attention(find_problems(res, Limits()))


def test_solved_layers_carry_their_thickness_and_whether_they_are_outer(neck_80):
    lf = neck_80.layer("F.Cu")
    assert lf.thickness_mm == pytest.approx(OZ1) and lf.outer is True


def test_a_layer_without_a_thickness_gets_no_default():
    """Layers built by hand (no thickness known) are ranked as before, with no default."""
    import dataclasses
    res = solve_neck(amps_for(80.0, OZ1))
    res.layers = [dataclasses.replace(lf, thickness_mm=None) for lf in res.layers]
    assert not attention(find_problems(res, Limits()))


# ---------------------------------------------------------------------- the assumption line

LINE_1OZ = ("Current-density default (Coppernicus, attention only, not a verdict): 60 A/mm² at "
            "1 oz, scaled ×√(35 µm / t): F.Cu 60, B.Cu 60 A/mm²; set max_current_density to judge "
            "it (it then replaces this default). Sources: docs/technical.md#current-density-default.")


def test_every_rail_result_has_the_assumption_line(neck_80):
    assert LINE_1OZ in neck_80.assumptions


def test_the_assumption_line_names_each_layers_threshold_as_solved():
    res = solve_neck(amps_for(10.0, OZ1), settings=Settings(copper_thickness_mm={"F.Cu": 0.070}))
    line = next(a for a in res.assumptions if a.startswith("Current-density default"))
    assert "F.Cu 42.4, B.Cu 60 A/mm²" in line


def test_the_note_says_both_constants_when_they_differ(monkeypatch):
    from coppernicus import problems
    monkeypatch.setattr(problems, "DENSITY_DEFAULT_INTERNAL_A_MM2", 50.0)
    note = problems.density_default_note([("F.Cu", 0.035, True), ("In1.Cu", 0.035, False)])
    assert "60 A/mm² on outer layers and 50 A/mm² on inner layers at 1 oz" in note
    assert "F.Cu 60, In1.Cu 50 A/mm²" in note


# ---------------------------------------------------------------------- a power tree

def _hot_chain(limits=None):
    """The power chain with U3 at 5 A: +1V8, +3V3 and GND carry over 60 A/mm² on their
    2 mm × 35 µm strips (5 A: 71 A/mm²)."""
    project = power_chain_project()
    project.rail("+1V8").loads[0].current = 5.0
    if limits is not None:
        project.rail("+1V8").limits = limits
    return project


@pytest.fixture(scope="module")
def chain_board():
    return Board(power_chain_board())


@pytest.fixture(scope="module")
def hot_tree(chain_board):
    return solve_project_tree(chain_board, _hot_chain())


def test_a_tree_group_above_the_default_has_attention_and_no_fail(hot_tree):
    from coppernicus.power_tree import group_verdict
    problems = find_tree_problems(hot_tree, "+1V8")
    [spot] = attention(problems)
    assert spot.layer == "F.Cu" and spot.value == pytest.approx(5.0 / (2.0 * OZ1), rel=0.05)
    assert spot.data["default_j_a_mm2"] == pytest.approx(60.0)
    gv = group_verdict(hot_tree, "+1V8")
    assert gv.verdict == "not_judged" and gv.failed == 0 and gv.not_judged == []


def test_the_tree_summary_counts_attention_and_keeps_its_verdict(hot_tree):
    from coppernicus.power_tree import tree_summary, tree_text
    doc = json.loads(json.dumps(tree_summary(hot_tree)))
    assert doc["verdict"] == "not_judged" and doc["failed"] == 0 and doc["not_judged"] == []
    group = next(g for g in doc["groups"] if g["group"] == "+1V8")
    assert group["attention"] == 1
    assert [p["severity"] for p in group["problems"]].count("attention") == 1
    assert doc["attention"] == sum(g["attention"] for g in doc["groups"]) >= 2
    text = "\n".join(tree_text(doc))
    assert "Attention (Coppernicus's current-density default, not a verdict)" in text
    assert "+1V8: 1" in text


def test_every_tree_group_has_the_assumption_line(hot_tree):
    line = LINE_1OZ
    assert line in hot_tree.assumptions
    assert all(line in res.assumptions for res in hot_tree.groups.values())


def test_a_user_limit_on_a_tree_group_replaces_the_default(chain_board):
    from coppernicus.power_tree import group_verdict
    tree = solve_project_tree(chain_board, _hot_chain(Limits(max_current_density=60.0)))
    problems = find_tree_problems(tree, "+1V8")
    assert not attention(problems)
    assert [p.kind for p in problems if p.severity == FAIL] == ["hotspot"]
    assert group_verdict(tree, "+1V8").verdict == "fail"
    lenient = solve_project_tree(chain_board, _hot_chain(Limits(max_current_density=500.0)))
    assert not attention(find_tree_problems(lenient, "+1V8"))
    assert attention(find_tree_problems(lenient, "+3V3"))         # the other groups keep it


def test_a_group_that_did_not_converge_keeps_the_users_limit_in_place_of_the_default(chain_board):
    tree = solve_project_tree(chain_board, _hot_chain(Limits(max_current_density=500.0)))
    tree.groups["+1V8"].converged = False
    problems = find_tree_problems(tree, "+1V8")
    assert not attention(problems) and not [p for p in problems if p.severity == FAIL]


# ---------------------------------------------------------------------- JSON, CLI, CI, report

@pytest.fixture()
def neck_project(tmp_path):
    save_snapshot(neck_board(), tmp_path / "neck.kipi.json.gz")
    project = Project(board="neck.kipi.json.gz", rails=[neck_rail(amps_for(80.0, OZ1))])
    return project.save(tmp_path / "neck.coppernicus.yml")


def test_the_json_severity_is_attention(tmp_path):
    from coppernicus.service import Session
    path = tmp_path / "neck.kipi.json.gz"
    save_snapshot(neck_board(), path)
    s = Session(out_dir=tmp_path / "out")
    bid = s.open_board(path)["board_id"]
    doc = json.loads(json.dumps(s.solve_rail(bid, NET, [{"ref": "J1", "voltage_v": 1.0}],
                                             [{"ref": "U1", "current_a": amps_for(80.0, OZ1)}]),
                                allow_nan=False))
    [spot] = [p for p in doc["problems"] if p["severity"] == "attention"]
    assert spot["kind"] == "hotspot" and spot["default_j_a_mm2"] == pytest.approx(60.0)
    assert spot["thickness_mm"] == pytest.approx(OZ1)
    assert doc["verdict"] == "not_judged" and doc["failed"] == 0 and doc["attention"] == 1
    assert LINE_1OZ in doc["assumptions"]
    again = s.get_problems(doc["result_id"])
    assert [p["severity"] for p in again["problems"]].count("attention") == 1


def test_the_cli_exit_code_is_unchanged_and_the_text_shows_attention(neck_project, tmp_path, capsys):
    from coppernicus.cli import EXIT_OK, main
    assert main(["solve", str(neck_project), "--no-maps", "--out", str(tmp_path / "o")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ATTENTION Current crowding on F.Cu" in out
    p = Project.load(neck_project)
    p.rails[0].limits = Limits(max_drop_pct=50.0)
    p.save()
    assert main(["solve", str(neck_project), "--no-maps", "--out", str(tmp_path / "o2"), "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    [rail] = doc["rails"]
    assert rail["verdict"] == "pass" and rail["attention"] == 1


def test_the_tree_cli_exit_code_is_unchanged(tmp_path):
    from coppernicus.cli import EXIT_OK, main
    save_snapshot(power_chain_board(), tmp_path / "power_chain.kipi.json.gz")
    chain = _hot_chain().save(tmp_path / "chain.coppernicus.yml")
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_OK


def test_the_ci_summary_counts_attention_and_the_exit_code_is_unchanged(neck_project, tmp_path, monkeypatch):
    from coppernicus import ci
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([neck_project.name], Path("results"), html=False)
    assert index["exit_code"] == ci.EXIT_OK
    md = ci.summary_markdown(index)
    assert "1 attention item" in md and "not a verdict" in md
    assert "**ATTENTION** Current crowding on F.Cu" in md


def test_the_html_report_has_an_attention_badge(neck_80, tmp_path):
    from coppernicus.report import ReportBuilder
    b = ReportBuilder(title="neck")
    b.add_rail(neck_80, Limits())
    [rail] = b.rails
    assert [p["severity"] for p in rail["problems"]].count("attention") == 1
    assert "1 attention item" in rail["verdict_text"] and "not a verdict" in rail["verdict_text"]
    # the "strongest current crowding" card keeps the ranked spot it showed before
    info = [p for p in rail["problems"] if p["kind"] == "hotspot" and p["severity"] == "info"]
    assert rail["worst"]["hotspot"]["mean_j_a_mm2"] == pytest.approx(max(p["mean_j_a_mm2"] for p in info))
    assets = Path(__file__).resolve().parents[1] / "src" / "coppernicus" / "report_assets"
    assert '"attention"' in (assets / "report.js").read_text(encoding="utf-8")
    assert ".badge.attention" in (assets / "report.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- the app

def test_the_rail_header_counts_attention_apart_from_exceeded_limits(neck_80):
    from coppernicus.app.main_window import rail_header_html
    head = rail_header_html(neck_80, Limits(), find_problems(neck_80, Limits()))
    assert "1 attention" in head and "limit(s) exceeded" not in head
    lim = Limits(max_drop_pct=1e-6)
    head = rail_header_html(neck_80, lim, find_problems(neck_80, lim))
    assert "1 limit(s) exceeded" in head and "1 attention" in head


def test_the_tree_header_counts_attention(hot_tree):
    from coppernicus.app.main_window import tree_header_html
    from coppernicus.power_tree import group_verdict
    head = tree_header_html("+1V8", hot_tree.groups["+1V8"], group_verdict(hot_tree, "+1V8"))
    assert "1 attention" in head


def test_the_problems_panel_shows_amber_attention_rows():
    pytest.importorskip("PySide6")
    body = textwrap.dedent('''
        from PySide6 import QtWidgets
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        from coppernicus.app.panels import ProblemsPanel, ATTENTION_COLOR
        from coppernicus.problems import ATTENTION, FAIL, INFO, Problem
        panel = ProblemsPanel()
        rows = [Problem(s, "hotspot", s, "", 1.0, 0.0, 0.0, "F.Cu") for s in (FAIL, ATTENTION, INFO)]
        panel.set_result("", rows, [])
        items = [panel.tree.topLevelItem(i) for i in range(3)]
        assert [it.text(0) for it in items] == ["FAIL", "ATTENTION", ""], [it.text(0) for it in items]
        assert items[1].foreground(1).color().name() == ATTENTION_COLOR
        assert items[0].foreground(1).color().name() != ATTENTION_COLOR
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_math_check_of_the_ipc_2221_figure_in_the_docs():
    """docs/technical.md: IPC-2221 external, ΔT 10 °C, 1 oz × 1 mm: about 68 A/mm²."""
    area_mil2 = (1.0 / 0.0254) * (0.035 / 0.0254)          # 1 mm × 35 µm in mil²
    amps = 0.048 * 10 ** 0.44 * area_mil2 ** 0.725
    assert amps / 0.035 == pytest.approx(68.0, abs=1.0)
    assert math.isclose(60 * math.sqrt(0.035 / 0.070), 42.43, abs_tol=0.01)
