"""Degenerate mesh triangles: a triangle with an edge shorter than 1 nm, the resolution of
KiPI's geometry snap, comes from Steiner points the mesher added below the input's
resolution. Its |J| is set by the last bits of the node voltages, so every reported maximum
or peak |J| leaves it out: the rail's j_max, the current-crowding spots and their peaks, the
|J| colour scales (HTML report, PNG maps, app) and a cut's maximum. The solution, the copper
loss and the areas keep it, and a warning says how many were left out on each layer and the
largest |J| among them. A field without one is unchanged. Synthetic data only."""

import dataclasses

import numpy as np
import pytest
from shapely.geometry import box
from test_density_default import (
    NET,
    OZ1,
    amps_for,
    attention,
    neck_board,
    neck_rail,
    sliver_result,
    spots,
    strip_result,
)

from coppernicus.engine import Board, solve_rail
from coppernicus.problems import ATTENTION, FAIL, find_problems
from coppernicus.project import Limits, Rail, Settings
from coppernicus.results import (
    GEOMETRY_RESOLUTION_MM,
    LayerField,
    RailResult,
    degenerate_triangles,
    exclude_degenerate,
)

# A degenerate triangle of Steiner points: edges 0.48 / 0.48 / 0.16 nm, area 3.8e-14 mm².
APEX_MM, HALF_BASE_MM = 0.4733e-6, 0.08e-6
DEGENERATE_AREA_MM2 = APEX_MM * HALF_BASE_MM          # 3.79e-14
QUIET, IN_A, IN_CORE = 500, 21, 63                    # strip nodes: 1 A/mm², spot A, C's core
HUGE = 1.0e5                                          # A/mm², its artefact |J|


def with_degenerate(layer: LayerField, node: int, j: float = HUGE) -> LayerField:
    """``layer`` with one degenerate triangle appended at ``node``: two new nodes 0.47 nm
    from it, 0.16 nm apart, carrying ``j``. It shares ``node`` with the copper around it."""
    x, y = layer.points[node]
    n = len(layer.points)
    new = [[x + APEX_MM, y - HALF_BASE_MM], [x + APEX_MM, y + HALF_BASE_MM]]
    return dataclasses.replace(
        layer, points=np.vstack([layer.points, new]),
        tris=np.vstack([layer.tris, [[node, n, n + 1]]]),
        voltage=np.append(layer.voltage, [layer.voltage[node]] * 2),
        current_density=np.append(layer.current_density, j),
        tri_power=np.append(layer.tri_power, 1e-12), tri_area=np.append(layer.tri_area, DEGENERATE_AREA_MM2))


def strip_with(*nodes) -> RailResult:
    """:func:`strip_result` with a degenerate triangle at each of ``nodes`` on F.Cu."""
    res = strip_result()
    for node in nodes:
        res.layers[0] = with_degenerate(res.layers[0], node)
    return res


def rows(problems):
    return [(p.severity, p.kind, p.title, p.detail, p.value, p.x, p.y, p.layer, p.data) for p in problems]


SMALL_QUADS = 50


def small_result(*nodes) -> RailResult:
    """A 25 mm × 1 mm strip of 50 quads, |J| 1 to 100 A/mm² triangle by triangle: 100
    triangles, so one more value at the top moves the 99.5th percentile (99.505 without it).
    Degenerate triangles at ``nodes``."""
    n = SMALL_QUADS
    xs = np.arange(n + 1) * 0.5
    points = np.column_stack([np.concatenate([xs, xs]), np.repeat([0.0, 1.0], n + 1)])
    lo = np.arange(n)
    tris = np.concatenate([np.column_stack([lo, lo + 1, lo + n + 2]),
                           np.column_stack([lo, lo + n + 2, lo + n + 1])])
    layer = LayerField(0, "F.Cu", points, tris, np.zeros(len(points)), np.arange(1.0, 2 * n + 1),
                       np.zeros(2 * n), np.full(2 * n, 0.25), OZ1, True)
    for node in nodes:
        layer = with_degenerate(layer, node)
    return RailResult(NET, [NET], 1.0, [layer], [], [], [], 0.0, 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------- the resolution

def test_the_resolution_is_kipis_geometry_snap():
    """KiPI nodes the board geometry on a 1 nm grid (kipi/core/mesh/mesher.py
    NODING_GRID_MM): an edge shorter than that is below the input's resolution."""
    from kipi.core.mesh.mesher import NODING_GRID_MM
    assert GEOMETRY_RESOLUTION_MM == NODING_GRID_MM == 1e-6


@pytest.mark.parametrize("x, y", [(0.007919, 0.015838), (123.456789, 87.654321), (239.999998, 89.999999)])
def test_an_edge_of_one_grid_step_is_resolved_a_shorter_one_is_not(x, y):
    """Two grid vertices 1 nm apart compute a few 1e-15 mm under 1e-6 mm at board
    coordinates: they stay resolved. A 0.48 / 0.48 / 0.16 nm triangle is not."""
    step = [[x, y], [round(x + 1e-6, 6), y], [x, round(y + 1e-6, 6)]]
    tiny = [[x, y], [x + APEX_MM, y - HALF_BASE_MM], [x + APEX_MM, y + HALF_BASE_MM]]
    layer = LayerField(0, "F.Cu", np.array(step + tiny), np.array([[0, 1, 2], [3, 4, 5]]), np.zeros(6),
                       np.ones(2), np.zeros(2), np.ones(2))
    assert layer.resolved().tolist() == [True, False]


# ---------------------------------------------------------------------- j_max and the note

def test_j_max_leaves_a_degenerate_triangle_out_and_a_warning_says_so():
    res = strip_with(QUIET)
    res.j_max = HUGE                                  # KiPI's summary: the largest of every triangle
    note = exclude_degenerate(res)
    assert res.j_max == 80.0                          # spot A, the strongest resolved copper
    assert degenerate_triangles(res) == [{"layer": "F.Cu", "triangles": 1, "j_max_a_mm2": HUGE}]
    assert res.warnings == [note]
    assert "F.Cu 1 triangle, largest |J| 1e+05 A/mm²" in note and "1 nm" in note
    assert "artefact" in note
    assert exclude_degenerate(res) == note and res.warnings == [note]           # once
    front = res.layers[0]
    assert front.tri_area.sum() == pytest.approx(500.0 + DEGENERATE_AREA_MM2, rel=1e-15)
    assert len(front.tris) == 2001                    # kept in the solution


def test_the_note_counts_each_layer_with_its_largest():
    res = strip_with(QUIET, IN_A)
    res.layers[0].current_density[-1] = 2 * HUGE
    res.layers[1] = with_degenerate(res.layers[1], 30, j=3.0)
    note = exclude_degenerate(res)
    assert degenerate_triangles(res) == [{"layer": "F.Cu", "triangles": 2, "j_max_a_mm2": 2 * HUGE},
                                         {"layer": "B.Cu", "triangles": 1, "j_max_a_mm2": 3.0}]
    assert "F.Cu 2 triangles, largest |J| 2e+05 A/mm²; B.Cu 1 triangle, largest |J| 3 A/mm²" in note


def test_a_field_without_degenerate_triangles_is_left_as_it_was():
    res = strip_result()
    res.j_max = kipi = 80.00000000000001              # KiPI's value, kept bit for bit
    assert exclude_degenerate(res) is None
    assert res.j_max == kipi and res.warnings == [] and degenerate_triangles(res) == []
    assert all(lf.resolved().all() for lf in res.layers)


# ---------------------------------------------------------------------- spots and their peaks

@pytest.mark.parametrize("limits", [Limits(), Limits(max_current_density=60.0)])
def test_a_degenerate_triangle_is_no_spot_and_no_member_of_one(limits):
    """One in quiet copper would be a spot of its own (ranked, or a FAIL against a user's
    limit); one on spot A would raise its peak and its element count. Neither: the problems
    are those of the strip without them, to the bit."""
    assert rows(find_problems(strip_with(QUIET, IN_A), limits)) == rows(find_problems(strip_result(), limits))


def test_two_resolved_elements_and_a_degenerate_one_are_not_an_attention_spot():
    """C's 2-element core above the default stays below MIN_SPOT_ELEMENTS: a degenerate
    triangle on it does not make it 3."""
    res = sliver_result(2)
    res.layers[0] = with_degenerate(res.layers[0], IN_CORE)
    assert spots(find_problems(res, Limits()), ATTENTION) == [("F.Cu", 80.0)]
    assert [p.data["elements"] for p in attention(find_problems(res, Limits()))] == [4]


def test_a_failed_spot_keeps_its_peak_and_elements():
    res = strip_with(IN_A)
    [a] = [p for p in find_problems(res, Limits(max_current_density=75.0)) if p.severity == FAIL]
    assert a.data["peak_j_a_mm2"] == 80.0 and a.data["elements"] == 4


# ---------------------------------------------------------------------- report, maps, app

def test_the_report_leaves_them_out_of_every_maximum_and_carries_the_note():
    from coppernicus.report import ReportBuilder
    res = strip_with(QUIET, IN_A)
    res.j_max = HUGE
    note = exclude_degenerate(res)
    b = ReportBuilder(title="degenerate")
    b.add_rail(res, Limits())
    [rail] = b.rails
    front = rail["layers"][0]
    assert front["j_hi_a_mm2"] == front["j_point_max_a_mm2"] == 80.0
    assert rail["worst"]["j_point_max_a_mm2"] == 80.0
    assert rail["worst"]["hotspot"]["peak_j_a_mm2"] == 80.0
    assert rail["degenerate_triangles"] == [{"layer": "F.Cu", "triangles": 2, "j_max_a_mm2": HUGE}]
    assert note in rail["warnings"]


def resolved_top(res):
    j = res.layers[0].current_density[:2 * SMALL_QUADS]
    return float(np.percentile(j, 99.5))


def test_the_report_colour_scale_leaves_them_out():
    from coppernicus.report import ReportBuilder
    res = small_result(10)
    b = ReportBuilder(title="degenerate")
    b.add_rail(res, Limits())
    assert b.rails[0]["layers"][0]["j_scale_a_mm2"] == resolved_top(res) < 100.0


def test_the_png_map_colour_scale_leaves_them_out(tmp_path):
    from coppernicus.maps import render_layer_map
    res = small_result(10)
    scale = render_layer_map(res, res.layers[0], "current", [], tmp_path / "j.png")
    assert scale["max"] == resolved_top(res) < 100.0


def test_the_app_colour_scale_leaves_them_out():
    from coppernicus.app.board_view import current_scale
    res = small_result(10)
    assert current_scale(res.layers[0]) == [0.0, resolved_top(res)]
    plain = small_result().layers[0]
    assert current_scale(plain) == [0.0, float(np.percentile(plain.current_density, 99.5))]


def test_without_them_the_report_scales_are_as_before():
    from coppernicus.report import ReportBuilder
    res = small_result()
    b = ReportBuilder(title="plain")
    b.add_rail(res, Limits())
    [rail] = b.rails
    j = res.layers[0].current_density
    assert rail["layers"][0]["j_hi_a_mm2"] == float(j.max())
    assert rail["layers"][0]["j_scale_a_mm2"] == float(np.percentile(j, 99.5))
    assert "degenerate_triangles" not in rail


def test_a_cut_leaves_them_out_of_its_maximum():
    """A cut samples |J| along its line; a sample on a degenerate triangle does not set its
    maximum."""
    from coppernicus.service.fields import cut_current

    class Locator:                  # every sample on quad 20, the 4th on the degenerate triangle
        def locate_many(self, pts):
            out = np.full(len(pts), 20)
            out[3] = 2 * SMALL_QUADS
            return out

    res = small_result(10)
    lf = res.layers[0]
    j_vec = np.column_stack([lf.current_density, np.zeros(len(lf.current_density))])
    cut = cut_current(lf, Locator(), j_vec, box(0, 0, 25, 1), OZ1, (10.2, 0.0), (10.2, 1.0), spacing_mm=0.125)
    assert cut.samples == 8 and cut.max_abs_j == 21.0


# ---------------------------------------------------------------------- the JSON

def test_the_rail_summary_has_the_key_only_when_some_were_left_out():
    from coppernicus.service.convert import rail_summary
    res = strip_with(QUIET)
    exclude_degenerate(res)
    doc = rail_summary("r1", "b1", res, Rail(NET), find_problems(res, Limits()), Settings(), {}, {})
    assert doc["degenerate_triangles"] == [{"layer": "F.Cu", "triangles": 1, "j_max_a_mm2": HUGE}]
    assert doc["j_point_max_a_mm2"] == 80.0
    assert any("degenerate" in w for w in doc["warnings"])
    plain = strip_result()
    doc = rail_summary("r1", "b1", plain, Rail(NET), find_problems(plain, Limits()), Settings(), {}, {})
    assert "degenerate_triangles" not in doc


def test_a_tree_group_has_the_key_only_when_some_were_left_out():
    from coppernicus.power_tree import tree_summary
    from coppernicus.results import TreeResult
    res = strip_with(QUIET)
    exclude_degenerate(res)
    doc = tree_summary(TreeResult(groups={NET: res, "GND": strip_result()}))
    vcc, gnd = doc["groups"]
    assert vcc["degenerate_triangles"] == [{"layer": "F.Cu", "triangles": 1, "j_max_a_mm2": HUGE}]
    assert "degenerate_triangles" not in gnd


# ---------------------------------------------------------------------- through the engine

def _inject(real, j=1e6):
    """``mesh_fields`` with a degenerate triangle carrying ``j`` added to the first layer of
    the first net result, and KiPI's j_max as KiPI would compute it with that triangle."""
    done = []

    def mesh_fields(board, result):
        layers, vias = real(board, result)
        if not done:
            done.append(float(result.fields.summary["j_max"]))
            layers[0] = with_degenerate(layers[0], 0, j)
            result.fields.summary["j_max"] = j
        return layers, vias
    return mesh_fields, done


def test_a_rail_solve_leaves_them_out_of_j_max_and_warns(monkeypatch):
    from coppernicus.engine import solve as engine
    patched, kipi = _inject(engine.mesh_fields)
    monkeypatch.setattr(engine, "mesh_fields", patched)
    res = solve_rail(Board(neck_board()), neck_rail(amps_for(80.0, OZ1)))
    assert res.j_max == kipi[0]                       # KiPI's j_max without the triangle
    [note] = [w for w in res.warnings if "degenerate" in w]
    assert degenerate_triangles(res)[0]["j_max_a_mm2"] == 1e6 and "largest |J| 1e+06 A/mm²" in note


def test_a_tree_solve_leaves_them_out_of_j_max_and_warns_in_the_tree(monkeypatch):
    from coppernicus.engine import tree as engine
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    from coppernicus.power_tree import tree_summary
    patched, kipi = _inject(engine.mesh_fields)
    monkeypatch.setattr(engine, "mesh_fields", patched)
    tree = engine.solve_project_tree(Board(power_chain_board()), power_chain_project())
    [res] = [r for r in tree.groups.values() if degenerate_triangles(r)]
    assert res.j_max == kipi[0]
    [note] = [w for w in res.warnings if "degenerate" in w]
    assert tree.warnings.count(note) == 1
    doc = tree_summary(tree)
    assert note in doc["warnings"]
    [group] = [g for g in doc["groups"] if "degenerate_triangles" in g]
    assert group["group"] == res.net and group["j_point_max_a_mm2"] == pytest.approx(kipi[0], rel=1e-6)
