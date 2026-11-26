"""The label above every |J| colour scale (HTML report, PNG maps, app): line 1 the layer's
peak |J| (degenerate triangles left out, mesh-dependent), line 2 the verdict of the current
density on that layer as the problem list judged it (the spot means, never re-judged):
against the user's max_current_density, or Coppernicus's attention default when only that
applies. The colour scale itself stays at the layer's 99.5th percentile. Synthetic data only."""

import dataclasses
import json
import os
import re
import subprocess
import sys
import textwrap

import numpy as np
import pytest
from test_degenerate_triangles import HUGE, QUIET, small_result, with_degenerate
from test_density_default import CORE, STRIP_QUADS, sliver_result, strip_result

from coppernicus.problems import Problems, density_check, find_problems, find_tree_problems
from coppernicus.project import Limits


def check(res, layer=0, limits=None, problems=None):
    """The label of ``res``'s layer ``layer``, from ``problems`` or the list ``limits`` judge."""
    lf = res.layers[layer]
    if problems is None and limits is not None:
        problems = find_problems(res, limits)
    return density_check(res, lf, problems, limits)


def corner_result():
    """F.Cu with nothing above the 60 A/mm² default but a 2-element sliver at 70 A/mm² (a
    corner: below the 3 elements of an attention spot); A lowered to 50 A/mm²."""
    res = sliver_result(2)
    j = res.layers[0].current_density
    j[20:22] = j[20 + STRIP_QUADS:22 + STRIP_QUADS] = 50.0
    return res


# ---------------------------------------------------------------------- the problem list

def test_the_problem_list_keeps_what_judged_it():
    lim = Limits(max_current_density=55.0)
    problems = find_problems(strip_result(), lim)
    assert isinstance(problems, list) and isinstance(problems, Problems)
    assert problems.limits == lim and problems.default is True
    assert find_problems(strip_result()).limits == Limits()


# ---------------------------------------------------------------------- line 1: the peak

def test_the_peak_is_per_layer_and_leaves_degenerate_triangles_out():
    res = strip_result()
    res.layers[0] = with_degenerate(res.layers[0], QUIET)          # 1e5 A/mm², an artefact
    front, back = check(res, 0, Limits()), check(res, 1, Limits())
    assert front.peak_j_a_mm2 == 80.0 and back.peak_j_a_mm2 == 50.0
    assert front.peak_text() == "Peak 80 A/mm²" and back.peak_text() == "Peak 50 A/mm²"
    assert front.lines()[0] == "Peak 80 A/mm² (mesh-dependent)"
    assert HUGE > 1e4


def test_a_large_peak_is_written_without_an_exponent():
    res = small_result()
    res.layers[0].current_density[:] *= 25.0                      # top 2500 A/mm²
    assert check(res).peak_text() == "Peak 2500 A/mm²"


# ---------------------------------------------------------------------- line 2: the verdict

def test_a_layer_with_failed_spots_says_fail_with_the_worst_spot():
    """At 55 A/mm²: A (80, 1 mm²) and C's core (70) fail on F.Cu; B.Cu (50) passes."""
    res = strip_result()
    lim = Limits(max_current_density=55.0)
    front, back = check(res, 0, lim), check(res, 1, lim)
    assert (front.verdict, front.spots, front.limit_kind) == ("fail", 2, "limit")
    assert front.worst["mean_j_a_mm2"] == 80.0 and front.worst["area_mm2"] == 1.0
    assert front.verdict_text() == "limit 55 A/mm²: FAIL · worst spot 80.0 A/mm² over 1 mm² · 2 spots above"
    assert (back.verdict, back.verdict_text()) == ("pass", "limit 55 A/mm²: pass")


def test_a_single_failed_spot_is_the_owners_wording():
    res = strip_result()
    c = check(res, 0, Limits(max_current_density=75.0))           # A only
    assert c.verdict_text() == "limit 75 A/mm²: FAIL · worst spot 80.0 A/mm² over 1 mm²"


def test_a_layer_below_the_limit_passes():
    c = check(strip_result(), 0, Limits(max_current_density=100.0))
    assert (c.verdict, c.verdict_text()) == ("pass", "limit 100 A/mm²: pass")
    assert c.lines() == ["Peak 80 A/mm² (mesh-dependent)", "limit 100 A/mm²: pass"]


def test_with_only_the_default_it_counts_the_attention_spots():
    """No max_current_density: A (80) and C's core (70) are above the 60 A/mm² default on
    F.Cu; B.Cu (50) is below it."""
    res = strip_result()
    front, back = check(res, 0, Limits()), check(res, 1, Limits())
    assert (front.verdict, front.spots) == ("attention", 2)
    assert (front.limit_kind, front.limit_j_a_mm2) == ("default", 60.0)
    assert front.verdict_text() == ("default 60 A/mm² (attention): 2 spots above · worst 80.0 A/mm² "
                                    "over 1 mm²")
    assert (back.verdict, back.verdict_text()) == ("below", "default 60 A/mm² (attention): below")


def test_a_peak_above_the_default_at_a_corner_says_the_spots_are_below():
    """A 2-element sliver at 70 A/mm²: the peak is above the default, but it is no attention
    spot (under 3 mesh elements): the layer is below."""
    c = check(corner_result(), 0, Limits())
    assert c.peak_j_a_mm2 == 70.0 and (c.verdict, c.spots) == ("below", 0) and c.peak_above
    assert c.verdict_text() == ("default 60 A/mm² (attention): below · peak above the default at a "
                                "corner; spots below")


def test_against_a_user_limit_a_peak_above_it_is_always_a_failed_spot():
    """problems.py judges every spot against the user's limit, however small: the same
    sliver fails, and the label says it may be a mesh artefact (problems.py's own note)."""
    c = check(corner_result(), 0, Limits(max_current_density=60.0))
    assert (c.verdict, c.spots) == ("fail", 1)
    assert c.verdict_text() == ("limit 60 A/mm²: FAIL · worst spot 70.0 A/mm² over 0.5 mm² · under 3 "
                                "mesh elements: may be a mesh artefact")


def test_the_verdict_is_read_from_the_list_not_judged_again():
    """A list that has no failed spot says pass, whatever |J| is: the label only reads it."""
    res = strip_result()
    judged = find_problems(res, Limits(max_current_density=55.0))
    kept = Problems([p for p in judged if p.severity != "fail"], judged.limits, judged.default)
    assert check(res, 0, problems=kept).verdict == "pass"


def test_a_layer_without_a_thickness_gets_no_default_line():
    res = strip_result()
    res.layers = [dataclasses.replace(lf, thickness_mm=None) for lf in res.layers]
    assert check(res, 0, Limits()).lines() == ["Peak 80 A/mm² (mesh-dependent)"]


def test_without_a_problem_list_or_limits_only_the_peak_is_written():
    res = strip_result()
    assert check(res).lines() == ["Peak 80 A/mm² (mesh-dependent)"]
    # A plain list (not from find_problems) is judged by the limits given with it.
    plain = list(find_problems(res, Limits(max_current_density=55.0)))
    assert check(res, problems=plain, limits=Limits(max_current_density=55.0)).verdict == "fail"


def test_a_group_that_did_not_converge_is_not_judged():
    res = strip_result()
    res.converged = False
    nothing = Problems([], Limits(), default=False)                # find_tree_problems' list then
    c = density_check(res, res.layers[0], nothing, Limits(max_current_density=55.0))
    assert (c.verdict, c.verdict_text()) == ("not_judged", "limit 55 A/mm²: not judged (did not converge)")


def test_a_layer_with_no_current_has_no_spot_and_says_below():
    res = strip_result()
    res.layers[1].current_density[:] = 0.0
    c = check(res, 1, Limits())
    assert c.peak_j_a_mm2 == 0.0 and c.verdict == "below"


# ---------------------------------------------------------------------- the report

def test_the_report_carries_the_label_per_layer_and_keeps_the_scale():
    from coppernicus.report import ReportBuilder
    res = strip_result()
    b = ReportBuilder(title="label")
    b.add_rail(res, Limits(max_current_density=55.0))
    front, back = b.rails[0]["layers"]
    assert front["j_check"]["peak_j_a_mm2"] == 80.0 and back["j_check"]["peak_j_a_mm2"] == 50.0
    assert front["j_check"]["lines"] == [
        "Peak 80 A/mm² (mesh-dependent)",
        "limit 55 A/mm²: FAIL · worst spot 80.0 A/mm² over 1 mm² · 2 spots above"]
    assert back["j_check"]["verdict"] == "pass" and back["j_check"]["limit_j_a_mm2"] == 55.0
    assert front["j_check"]["worst_spot"]["mean_j_a_mm2"] == 80.0
    for lay, lf in zip(b.rails[0]["layers"], res.layers):                  # the scale: unchanged
        assert lay["j_scale_a_mm2"] == float(np.percentile(lf.current_density, 99.5))
    meta = json.dumps(b.meta(), allow_nan=False)                          # plain JSON
    assert "mesh-dependent" in meta


def test_the_page_writes_the_label_above_the_colour_bar():
    from importlib.resources import files
    js = files("coppernicus").joinpath("report_assets", "report.js").read_text(encoding="utf-8")
    html = files("coppernicus").joinpath("report_assets", "report.html").read_text(encoding="utf-8")
    assert "j_check" in js and 'id="bar-check"' in html
    assert html.index('id="bar-check"') < html.index('id="bar-top"')


# ---------------------------------------------------------------------- the PNG maps

def test_the_png_map_writes_the_label_and_keeps_the_scale(tmp_path):
    from coppernicus.maps import render_layer_map
    res = strip_result()
    problems = find_problems(res, Limits(max_current_density=55.0))
    scale = render_layer_map(res, res.layers[0], "current", [], tmp_path / "j.png", problems)
    assert scale["label"] == ["Peak 80 A/mm² (mesh-dependent)",
                              "limit 55 A/mm²: FAIL · worst spot 80.0 A/mm² over 1 mm² · 2 spots above"]
    assert scale["max"] == float(np.percentile(res.layers[0].current_density, 99.5))
    back = render_layer_map(res, res.layers[1], "current", [], tmp_path / "b.png", problems)
    assert back["label"] == ["Peak 50 A/mm² (mesh-dependent)", "limit 55 A/mm²: pass"]
    bare = render_layer_map(res, res.layers[0], "current", [], tmp_path / "n.png")
    assert bare["label"] == ["Peak 80 A/mm² (mesh-dependent)"]
    drop = render_layer_map(res, res.layers[0], "drop", [], tmp_path / "d.png", problems)
    assert "label" not in drop


def test_saved_maps_take_the_limits_when_no_list_is_given(tmp_path, monkeypatch):
    from coppernicus import maps
    seen = []
    real = maps.render_layer_map

    def spy(*a, **kw):
        out = real(*a, **kw)
        seen.append(out.get("label"))
        return out
    res = strip_result()
    res.layers = res.layers[:1]
    monkeypatch.setattr(maps, "render_layer_map", spy)
    maps.save_layer_maps(res, [], tmp_path, limits=Limits(max_current_density=100.0), dpi=40)
    assert ["Peak 80 A/mm² (mesh-dependent)", "limit 100 A/mm²: pass"] in seen


# ---------------------------------------------------------------------- a power tree

@pytest.fixture(scope="module")
def chain():
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    from coppernicus.engine.tree import solve_project_tree
    project = power_chain_project()
    project.rail("+3V3").limits = Limits(max_current_density=1.0)     # every +3V3 spot fails
    return solve_project_tree(Board(power_chain_board(island=True)), project)


def test_a_tree_groups_label_reads_the_trees_problems(chain):
    problems = find_tree_problems(chain, "+3V3")
    assert problems.limits.max_current_density == 1.0
    res = chain.groups["+3V3"]
    c = density_check(res, res.layers[0], problems)
    assert c.verdict == "fail" and c.verdict_text().startswith("limit 1 A/mm²: FAIL · worst spot ")
    assert c.peak_j_a_mm2 == float(res.layers[0].current_density[res.layers[0].resolved()].max())


def test_every_tree_problem_in_the_report_has_a_place_or_none(chain, tmp_path):
    """Each problem row of a tree group zooms to its x_mm, y_mm: a finite place, or null for
    one with no place (a load that cannot be reached), which the page then does not zoom to."""
    from coppernicus.power_tree import tree_summary
    from coppernicus.report import write_tree_report
    path = write_tree_report(chain, tree_summary(chain), tmp_path / "tree.html")
    text = path.read_text(encoding="utf-8")
    meta = json.loads(re.search(r'id="meta">(.*?)</script>', text, re.S).group(1))
    rows = [p for r in meta["rails"] for p in r["problems"]]
    assert rows and any(p["kind"] == "hotspot" for p in rows)
    for p in rows:
        assert (p["x_mm"] is None) == (p["y_mm"] is None), p
    groups = {r["net"]: r for r in meta["rails"]}
    assert groups["+3V3"]["layers"][0]["j_check"]["verdict"] == "fail"


# ---------------------------------------------------------------------- the app (offscreen)

APP = '''
import numpy as np
from PySide6 import QtWidgets
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from test_density_default import strip_result
from coppernicus.app.board_view import BoardView, current_scale
from coppernicus.problems import find_problems
from coppernicus.project import Limits

res = strip_result()
view = BoardView()
view.resize(900, 600)
view.set_problems(res, find_problems(res, Limits(max_current_density=55.0)))
view.show_field(res, res.layers[0], "current", [])
lines = view.colorbar.header()
assert lines[0] == "Peak 80 A/mm² (mesh-dependent)", lines
assert lines[1].startswith("limit 55 A/mm²: FAIL · worst spot 80.0"), lines
assert "mesh-dependent" in view.colorbar.toolTip()
top = float(np.percentile(res.layers[0].current_density, 99.5))
assert view.colorbar._hi == current_scale(res.layers[0])[1] == top
view.show_field(res, res.layers[1], "current", [])                 # another layer
lines = view.colorbar.header()
assert lines == ["Peak 50 A/mm² (mesh-dependent)", "limit 55 A/mm²: pass"], lines
view.show_field(res, res.layers[1], "drop", [])                    # another field: no label
assert view.colorbar.header() == [], view.colorbar.header()
view.show_field(res, res.layers[0], "current", [])
view.set_problems(res, find_problems(res, Limits()))               # the limits edited: re-judged
lines = view.colorbar.header()
assert lines[1].startswith("default 60 A/mm² (attention): 2 spots above"), lines
other = strip_result()                                             # another result: no list yet
view.show_field(other, other.layers[0], "current", [])
assert view.colorbar.header() == ["Peak 80 A/mm² (mesh-dependent)"], view.colorbar.header()
view.colorbar.grab()                                               # paints with the label
view.close()
'''


def test_the_app_label_follows_the_layer_the_field_and_the_limits():
    pytest.importorskip("PySide6")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.pathsep.join([os.path.dirname(__file__), os.environ.get("PYTHONPATH", "")]))
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(APP)], env=env, capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_corner_fixture_is_what_it_says():
    res = corner_result()
    j = res.layers[0].current_density
    assert sorted(set(j[j > 60.0].tolist())) == [70.0] and (j > 60.0).sum() == 2
    assert CORE == (62, 63)
