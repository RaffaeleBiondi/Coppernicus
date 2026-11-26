"""The offline HTML report: parse the file, decode its embedded cells, and check them against
the full-resolution result (closed form on the strip, barycentric probes on the demo)."""

import json
import re

import numpy as np
import pytest

from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import OZ1, Strip, demo_board, strip_board
from coppernicus.fields import TriangleLocator
from coppernicus.project import Limits, Load, Project, Rail, Source
from coppernicus.report import (
    J_CODES,
    V_CODES,
    Grid,
    RailReport,
    cell_triangles,
    decode,
    sample_layer,
    write_report,
)
from coppernicus.results import LayerField

RHO = 1.724e-5      # Ω·mm, annealed copper at 20 °C (the engine's constant)
DEMO_RAIL = Rail("+3V3", sources=[Source("J1", 3.3, name="Supply")],
                 loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)],
                 limits=Limits(max_drop_pct=0.25))


def parse(path):
    """The page's metadata and its data blocks, as the browser sees them."""
    text = path.read_text(encoding="utf-8")
    # The block ends at the first "</script>", as in a browser.
    meta = json.loads(re.search(r'<script type="application/json" id="meta">(.*?)</script>', text, re.S)
                      .group(1))
    blocks = dict(re.findall(r'<script type="application/octet-stream" id="([^"]+)">([^<]*)</script>', text))
    return text, meta, blocks


def cells(meta, blocks, rail=0):
    """Per layer: (grid, V per cell or NaN, |J| per cell, copper mask), decoded as the page does."""
    r = meta["rails"][rail]
    out = {}
    for lay in r["layers"]:
        g = lay["grid"]
        shape = (g["ny"], g["nx"])
        qv, qj = decode(blocks[lay["v"]], shape), decode(blocks[lay["j"]], shape)
        mask = qv > 0
        span = r["v_hi_v"] - r["v_lo_v"]
        v = np.where(qv >= 2, r["v_lo_v"] + (qv.astype(float) - 2) / (V_CODES - 1) * span, np.nan)
        j = np.where(qj >= 1, (qj.astype(float) - 1) / (J_CODES - 1) * lay["j_hi_a_mm2"], 0.0)
        grid = Grid(g["x0"], g["y0"], r["cell_mm"], g["nx"], g["ny"])
        out[lay["name"]] = (grid, v, j, mask, lay)
    return out


@pytest.fixture(scope="module")
def demo():
    board = Board(demo_board())
    return board, solve_rail(board, DEMO_RAIL)


def test_cells_are_assigned_by_exact_point_in_triangle_tests():
    pts = np.array([[0, 0], [2, 0], [0, 2], [2, 2]], float)
    tris = np.array([[0, 1, 2], [1, 3, 2]])
    grid = Grid(0.0, 0.0, 0.5, 5, 5)                      # centres at 0.25, 0.75, ... 2.25
    tri = cell_triangles(pts, tris, grid)
    cx, cy = np.meshgrid(grid.centres_x(), grid.centres_y())
    expected = np.where((cx > 2) | (cy > 2), -1, np.where(cx + cy < 2, 0, 1))
    off_diagonal = np.abs(cx + cy - 2) > 1e-9                # on the shared edge either is right
    assert np.array_equal(tri[off_diagonal], expected[off_diagonal])


def test_demo_report_matches_the_result(demo, tmp_path):
    board, res = demo
    project = Project(board="demo.kipi.json.gz", rails=[DEMO_RAIL])
    path = write_report([res], project, tmp_path / "out" / "demo.html", outline=board.outline(),
                        board_name="demo.kipi.json.gz")
    text, meta, blocks = parse(path)
    assert not re.search(r'\b(src|href)\s*=', text) and "http://" not in text and "https://" not in text
    [rail] = meta["rails"]
    assert rail["net"] == "+3V3" and rail["verdict"] == "fail" and rail["limits"] == {"max_drop_pct": 0.25}
    assert [ld["name"] for ld in rail["loads"]] == ["U1", "U2", "U3"]
    for ld, t in zip(rail["loads"], res.loads):
        assert ld["drop_mv"] == pytest.approx(res.drop(t) * 1e3, rel=1e-6)
    assert {ld["name"] for ld in rail["loads"] if ld["exceeds"]} == {"U1", "U2"}
    assert {p["title"].split(":")[0] for p in rail["problems"] if p["severity"] == "fail"} == {"U1", "U2"}
    assert sum(rail["loss_breakdown_w"].values()) == pytest.approx(res.power_loss, rel=1e-6)
    assert rail["worst"]["max_drop_mv"] == pytest.approx(res.max_drop * 1e3, rel=1e-6)
    assert [lay["name"] for lay in rail["layers"]] == [lf.name for lf in res.layers]
    assert any("mesh-dependent" in p["detail"] for p in rail["problems"] if p["kind"] == "hotspot")
    assert any("Maps are sampled on" in a for a in rail["assumptions"])

    decoded = cells(meta, blocks)
    for lf in res.layers:
        grid, v, j, mask, lay = decoded[lf.name]
        # Copper area from the cells, against the mesh's.
        assert mask.sum() * grid.cell ** 2 == pytest.approx(lf.tri_area.sum(), rel=0.03)
        # Probes: at cell centres well inside a triangle, the stored V and |J| are the
        # full-resolution values to within one quantisation step.
        loc = TriangleLocator(lf.points, lf.tris)
        rng = np.random.default_rng(0)
        jj, ii = np.nonzero(mask)
        pick = rng.choice(len(ii), size=min(400, len(ii)), replace=False)
        vstep = (rail["v_hi_v"] - rail["v_lo_v"]) / (V_CODES - 1)
        jstep = lay["j_hi_a_mm2"] / (J_CODES - 1)
        checked = 0
        for n in pick:
            x, y = grid.centres_x()[ii[n]], grid.centres_y()[jj[n]]
            k = loc.locate(x, y)
            if k < 0:
                continue
            w = loc.weights(k, x, y)
            if w.min() < 0.02:                              # near an edge: neighbours differ in |J|
                continue
            assert v[jj[n], ii[n]] == pytest.approx(w @ lf.voltage[lf.tris[k]], abs=vstep)
            assert j[jj[n], ii[n]] == pytest.approx(lf.current_density[k], abs=jstep)
            checked += 1
        assert checked > 100


def test_strip_cells_follow_the_closed_form(tmp_path):
    board = Board(strip_board())
    res = solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 1.0)], loads=[Load("U1", 1.0)]))
    path = write_report([RailReport(res)], None, tmp_path / "strip.html", outline=board.outline())
    _, meta, blocks = parse(path)
    assert meta["rails"][0]["verdict"] == "not_judged"
    assert all(p["severity"] != "fail" for p in meta["rails"][0]["problems"])
    grid, v, j, mask, _ = cells(meta, blocks)["F.Cu"]
    xs = grid.centres_x()
    on = mask.any(axis=0)
    track = on & (xs > Strip.X0 + 3) & (xs < Strip.X1 - 3)      # away from the pads
    x = xs[track]
    vcol = np.nanmean(np.where(mask, v, np.nan)[:, track], axis=0)
    expected = 1.0 - RHO * (x - Strip.X0 - Strip.PAD / 2) / (Strip.W * OZ1)
    assert vcol == pytest.approx(expected, abs=2e-5)
    jcol = j[:, track][mask[:, track]]
    assert np.median(jcol) == pytest.approx(1.0 / (Strip.W * OZ1), rel=0.02)
    # The track's width from the cells.
    assert mask[:, track].sum(axis=0).mean() * grid.cell == pytest.approx(Strip.W, abs=grid.cell)


@pytest.mark.parametrize("nasty", ['</script><script>alert(1)</script> __META__ "x"',
                                   "<!--<script>", "</SCRIPT >", "a < b & c > d"])
def test_names_cannot_break_out_of_the_page(demo, tmp_path, nasty):
    board, res = demo
    path = write_report([RailReport(res, label=nasty)], None, tmp_path / "x.html")
    text, meta, blocks = parse(path)
    assert meta["rails"][0]["label"] == nasty                  # read back unchanged
    meta_block = re.search(r'id="meta">(.*?)</script>', text, re.S).group(1)
    assert "<" not in meta_block                               # nothing the HTML parser acts on
    assert len(blocks) == 2 * len(res.layers)                  # every data block still found
    assert text.count('id="meta"') == 1


def test_a_page_over_the_ceiling_is_resampled_to_fit(demo, tmp_path, monkeypatch):
    import coppernicus.report as report
    board, res = demo
    natural = write_report([RailReport(res)], None, tmp_path / "natural.html")
    ceiling = 0.5 * natural.stat().st_size / 1e6
    monkeypatch.setattr(report, "MAX_FILE_MB", ceiling)       # as if the estimate were off
    fitted = write_report([RailReport(res)], None, tmp_path / "fitted.html", budget_mb=10 * ceiling)
    assert fitted.stat().st_size <= ceiling * 1e6
    _, meta, _ = parse(fitted)
    assert meta["rails"][0]["cell_mm"] > parse(natural)[1]["rails"][0]["cell_mm"]


def test_budget_sets_the_cell_size(demo, tmp_path):
    board, res = demo
    small = write_report([RailReport(res)], None, tmp_path / "small.html", budget_mb=0.3)
    big = write_report([RailReport(res)], None, tmp_path / "big.html")
    _, m_small, _ = parse(small)
    _, m_big, _ = parse(big)
    assert m_small["rails"][0]["cell_mm"] > m_big["rails"][0]["cell_mm"]
    assert small.stat().st_size < 0.6e6 < big.stat().st_size


def test_the_verdict_sentence_names_failures_and_what_was_not_judged(tmp_path):
    """A rail that fails one limit and cannot judge another says both (it used to say only
    the failure). The sentence is in the page data, so it is the same for every reader."""
    board = Board(strip_board())
    res = solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 0.0)], loads=[Load("U1", 1.0)]))
    limits = Limits(max_drop_pct=1.0, max_ground_rise_mv=1.0)       # % on a 0 V net; rise ~38 mV
    path = write_report([RailReport(res, limits)], None, tmp_path / "both.html", outline=board.outline())
    _, meta, _ = parse(path)
    [rail] = meta["rails"]
    assert rail["verdict"] == "fail"
    text = rail["verdict_text"]
    assert text.startswith("Fails against the limits you set: load drop ≤ 1 % of the source voltage; "
                           "ground rise ≤ 1 mV above 0 V. 1 problem over a limit.")
    assert "Not judged: U1 (max_drop_pct): 0 V nominal" in text
    none = write_report([RailReport(res)], None, tmp_path / "none.html", outline=board.outline())
    assert parse(none)[1]["rails"][0]["verdict_text"].startswith("Not judged: no limits were set")


def _ground(volts):
    """A ground group whose copper sits at ``volts`` (one node per corner), as a power tree
    gives it: below 0 V under a negative rail."""
    from coppernicus.results import LayerField, RailResult
    pts = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]])
    layer = LayerField(0, "F.Cu", pts, np.array([[0, 1, 2], [0, 2, 3]]), np.array(volts, dtype=float),
                       np.ones(2), np.zeros(2), np.full(2, 25.0))
    worst = max(volts, key=abs)
    return RailResult("GND", ["GND"], 0.0, [layer], [], [], [], worst, 1.0, 0.0, 0.0, kind="ground")


@pytest.mark.parametrize("volts, word, unit, sign", [
    ([0.0, -0.002, -0.005, -0.001], "shift", "mV below 0 V (ground shift)", 1),
    ([0.0, 0.002, 0.005, 0.001], "rise", "mV above 0 V (ground rise)", -1),
])
def test_a_ground_below_0_v_reads_as_a_shift_as_in_the_app(tmp_path, volts, word, unit, sign):
    """The page's map, scale, readout and worst card show the drop as the app does
    (RailResult.shown_drop_at / shown_drop_unit): on a ground below 0 V, its depth below 0 V,
    called a shift; on a ground above 0 V, its rise."""
    res = _ground(volts)
    path = write_report([RailReport(res, problems=[])], None, tmp_path / "g.html",
                        outline=[np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]])])
    [rail] = parse(path)[1]["rails"]
    assert (rail["drop_word"], rail["drop_unit"], rail["drop_sign"]) == (word, unit, sign)
    assert rail["drop_max_mv"] == pytest.approx(5.0)
    assert rail["layers"][0]["max_drop_mv"] == pytest.approx(5.0)
    assert rail["worst"]["max_drop_mv"] == pytest.approx(5.0)


@pytest.mark.parametrize("title, label", [("GND: largest ground shift -5.00 mV on F.Cu", "ground shift"),
                                          ("GND: largest ground rise 5.00 mV on F.Cu", "ground rise")])
def test_the_kind_label_follows_the_problems_own_words(tmp_path, title, label):
    """A rise problem below 0 V is a ground shift: the page labels it from its own title,
    not from a fixed "ground rise"."""
    from coppernicus.problems import Problem
    board = Board(strip_board())
    res = solve_rail(board, Rail(Strip.NET, sources=[Source("J1", 0.0)], loads=[Load("U1", 1.0)]))
    problem = Problem("info", "rise", title, "detail", 5.0, 1.0, 0.0, "F.Cu", {"rise_mv": 5.0})
    path = write_report([RailReport(res, problems=[problem])], None, tmp_path / "k.html",
                        outline=board.outline())
    [p] = parse(path)[1]["rails"][0]["problems"]
    assert p["kind_label"] == label


def _cell_triangles_one_offset_at_a_time(points, tris, grid):
    """cell_triangles as it was before its blocks of rows: the reference for the same answer,
    bit for bit, ties on shared edges included."""
    out = np.full(grid.ny * grid.nx, -1, dtype=np.int64)
    p = points[tris]
    lo, hi = p.min(axis=1), p.max(axis=1)
    i0 = np.maximum(np.ceil((lo[:, 0] - grid.x0) / grid.cell - 0.5), 0).astype(np.int64)
    i1 = np.minimum(np.floor((hi[:, 0] - grid.x0) / grid.cell - 0.5), grid.nx - 1).astype(np.int64)
    j0 = np.maximum(np.ceil((lo[:, 1] - grid.y0) / grid.cell - 0.5), 0).astype(np.int64)
    j1 = np.minimum(np.floor((hi[:, 1] - grid.y0) / grid.cell - 0.5), grid.ny - 1).astype(np.int64)
    w, h = i1 - i0 + 1, j1 - j0 + 1
    a, b = p[:, 0, 0] - p[:, 2, 0], p[:, 1, 0] - p[:, 2, 0]
    c, d = p[:, 0, 1] - p[:, 2, 1], p[:, 1, 1] - p[:, 2, 1]
    det = a * d - b * c
    keep = np.flatnonzero((w > 0) & (h > 0) & (np.abs(det) > 1e-18))
    inv = 1.0 / det[keep]
    coef = np.column_stack([d[keep] * inv, -b[keep] * inv, -c[keep] * inv, a[keep] * inv])
    ox = grid.x0 + (i0[keep] + 0.5) * grid.cell - p[keep, 2, 0]
    oy = grid.y0 + (j0[keep] + 0.5) * grid.cell - p[keep, 2, 1]
    wk, hk, i0k, j0k = w[keep], h[keep], i0[keep], j0[keep]
    shape_key = wk * (grid.ny + 1) + hk
    order = np.argsort(shape_key, kind="stable")
    for g in np.split(order, np.flatnonzero(np.diff(shape_key[order])) + 1):
        gw, gh = int(wk[g[0]]), int(hk[g[0]])
        A, B, C, D = coef[g, 0], coef[g, 1], coef[g, 2], coef[g, 3]
        for dj in range(gh):
            qy = oy[g] + dj * grid.cell
            row = (j0k[g] + dj) * grid.nx + i0k[g]
            for di in range(gw):
                qx = ox[g] + di * grid.cell
                l0, l1 = A * qx + B * qy, C * qx + D * qy
                inside = (l0 >= -1e-9) & (l1 >= -1e-9) & (l0 + l1 <= 1.0 + 1e-9)
                out[row[inside] + di] = keep[g][inside]
    return out.reshape(grid.ny, grid.nx)


def _mixed_mesh(seed):
    """A Delaunay mesh of fine points in one corner and a few far ones: small and very large
    triangles, many box shapes."""
    from scipy.spatial import Delaunay
    rng = np.random.default_rng(seed)
    pts = np.vstack([rng.uniform(0, 3, (300, 2)), rng.uniform(0, 20, (40, 2)),
                     [[0, 0], [20, 0], [0, 12], [20, 12]]])
    return pts, Delaunay(pts).simplices.astype(np.int64)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("cell", [0.25, 0.05])
def test_blocks_of_rows_give_the_same_cells_bit_for_bit(seed, cell, monkeypatch):
    pts, tris = _mixed_mesh(seed)
    grid = Grid(-0.1, -0.1, cell, int(20.2 / cell) + 1, int(12.2 / cell) + 1)
    expected = _cell_triangles_one_offset_at_a_time(pts, tris, grid)
    assert np.array_equal(cell_triangles(pts, tris, grid), expected)
    monkeypatch.setattr("coppernicus.report.CELL_CHUNK", 7)          # one row per block, or less
    assert np.array_equal(cell_triangles(pts, tris, grid), expected)


def test_centres_on_shared_edges_go_to_the_same_triangle():
    pts = np.array([[x, y] for y in range(5) for x in range(5)], float)   # a 4 × 4 square grid
    corners = [5 * j + i for j in range(4) for i in range(4)]
    tris = np.array([t for n in corners for t in ([n, n + 1, n + 5], [n + 1, n + 6, n + 5])])
    grid = Grid(-0.25, -0.25, 0.5, 9, 9)                       # centres on every edge and node
    expected = _cell_triangles_one_offset_at_a_time(pts, tris, grid)
    assert np.array_equal(cell_triangles(pts, tris, grid), expected)


def test_sample_layer_in_blocks_matches_one_block(monkeypatch):
    pts, tris = _mixed_mesh(3)
    rng = np.random.default_rng(3)
    layer = LayerField(0, "F.Cu", pts, tris, rng.uniform(3.2, 3.3, len(pts)), rng.uniform(0, 50, len(tris)),
                       np.zeros(len(tris)), np.ones(len(tris)))
    grid = Grid(-0.1, -0.1, 0.1, 203, 123)
    whole = sample_layer(layer, grid)
    monkeypatch.setattr("coppernicus.report.CELL_CHUNK", 1000)
    blocks = sample_layer(layer, grid)
    for a, b in zip(whole, blocks):
        assert np.array_equal(a, b, equal_nan=True)
@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_round_off_reads_as_zero_at_the_clis_decimals():
    import subprocess
    from pathlib import Path

    import coppernicus.report
    assets = Path(coppernicus.report.__file__).parent / "report_assets"
    js = (assets / "report.js").read_text(encoding="utf-8")
    helpers = [line for line in js.splitlines()
               if line.startswith(("const ok =", "function sig(", "  const a = Math.abs(x); return",
                                   "const mv =", "const pct =", "const volt ="))]
    assert len(helpers) == 6, helpers
    probe = "console.log(JSON.stringify([mv(5.971e-10), mv(-0.004), mv(16.64), pct(3.32e-11), pct(0.504), " \
            "volt(1e-9), volt(1.8), volt(3.28336), mv(NaN)]));"
    run = subprocess.run(["node", "-e", "\n".join(helpers) + "\n" + probe], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["0.00", "0.00", "16.64", "0.00", "0.504", "0.00000", "1.8", "3.28336",
                                      "–"]
