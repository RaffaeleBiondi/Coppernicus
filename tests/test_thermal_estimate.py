"""Before an electro-thermal solve: its size on KiPI's own grid, a rough memory estimate from
measured scaling, and the memory peak a solve records (as the AC solve does)."""

import math
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from coppernicus.engine import Board
from coppernicus.engine.synthetic import F, Strip, SyntheticBoard, two_layer_stackup
from coppernicus.engine.thermal import (
    MEM_POINTS,
    ThermalSize,
    thermal_cost_text,
    thermal_ram_estimate,
    thermal_size,
)
from coppernicus.engine.tree import solve_project_tree
from coppernicus.project import Load, Project, Rail, Source, ThermalOptions

ROOT = Path(__file__).resolve().parents[1]
DEMO_THERMAL = ROOT / "examples" / "demo" / "demo-thermal.coppernicus.yml"


@pytest.fixture(scope="module")
def demo_tree():
    project = Project.load(DEMO_THERMAL)
    board = Board.open(project.board_path())
    return board, project, solve_project_tree(board, project)


def strip_with(outline) -> Board:
    g, b = Strip, SyntheticBoard("strip", two_layer_stackup())
    b.square_pad("J1", "1", g.NET, g.X0, g.Y, g.PAD)
    b.square_pad("U1", "1", g.NET, g.X1, g.Y, g.PAD)
    b.track(g.NET, F, g.W, (g.X0, g.Y), (g.X1, g.Y))
    return Board(b.snapshot(outline=outline))


def grid_of(tree):
    t = tree.thermal
    return t.grid_nx, t.grid_ny, t.grid_cells, len(t.maps)


def test_the_size_is_kipis_automatic_grid(demo_tree):
    board, project, tree = demo_tree
    size = thermal_size(board, project.thermal)
    assert (size.nx, size.ny, size.cells, size.layers) == grid_of(tree)
    assert size.cell_mm == pytest.approx(tree.thermal.grid_mm) and size.automatic and size.exact
    assert size.unknowns == tree.thermal.grid_cells * len(tree.thermal.maps) == 83_500


L_SHAPE = Polygon([(-3, -3), (43, -3), (43, 3), (10, 3), (10, 12), (-3, 12)])


@pytest.mark.parametrize("outline, grid", [(L_SHAPE, 0.5), (L_SHAPE, 0.0), (Polygon(), 0.7), (None, 1.3)],
                         ids=["L outline, 0.5 mm", "L outline, automatic", "no outline, 0.7 mm",
                              "box outline, 1.3 mm"])
def test_the_size_is_kipis_grid_for_a_set_grid_and_any_board_shape(outline, grid):
    board = strip_with(outline)
    options = ThermalOptions(enabled=True, grid_mm=grid, iterations=1)
    project = Project(rails=[Rail(Strip.NET, sources=[Source("J1", 3.3)], loads=[Load("U1", 1.0)])],
                      thermal=options)
    tree = solve_project_tree(board, project)
    size = thermal_size(board, options)
    assert (size.nx, size.ny, size.cells, size.layers) == grid_of(tree)
    assert size.cell_mm == pytest.approx(tree.thermal.grid_mm) and size.automatic == (grid == 0)


def test_a_huge_grid_is_counted_from_the_board_area():
    board = strip_with(L_SHAPE)
    size = thermal_size(board, ThermalOptions(grid_mm=0.01))
    assert not size.exact and size.nx * size.ny > 4_000_000
    assert size.cells == pytest.approx(L_SHAPE.area / 0.01 ** 2, rel=1e-6)


# ---------------------------------------------------------------------- memory

def test_the_memory_fit_holds_every_measured_point_within_its_stated_scatter():
    for layers, unknowns, measured_mb in MEM_POINTS:
        ram = thermal_ram_estimate(unknowns, layers, 64e9)
        assert -0.35 < ram.bytes / (measured_mb * 1e6) - 1 < 0.25, (layers, unknowns, ram.bytes)


def test_the_memory_estimate_warns_only_over_this_machines_memory_and_says_how():
    ram = thermal_ram_estimate(150_000, 10, 16e9)
    assert not ram.risk and ram.total_bytes == 16e9
    assert "measured on synthetic" in ram.text and "2 to 10 copper layers" in ram.text
    assert "power tree's own" in ram.text and "of this machine's 16 GB" in ram.text
    assert "outside the measured range" not in ram.text
    big = thermal_ram_estimate(2_000_000, 12, 16e9)
    assert big.risk and "more than this machine's 16 GB" in big.text and "coarser grid" in big.text
    assert "outside the measured range" in big.text
    unknown = thermal_ram_estimate(150_000, 10, None)
    assert not unknown.risk and unknown.total_bytes is None and "cannot be read" in unknown.text


def test_the_cost_says_the_grid_and_the_last_solve_on_this_computer():
    size = ThermalSize(cell_mm=0.24, nx=250, ny=167, cells=41_750, layers=2, automatic=True, exact=True)
    text = thermal_cost_text(size, None)
    assert text.startswith("250 × 167 cells of 0.24 mm (automatic), 41,750 inside the board, × 2 copper "
                           "layers: 83,500 unknowns")
    assert "no earlier electro-thermal solve on this computer" in text
    text = thermal_cost_text(size, {"seconds": 59.0, "unknowns": 428_110.0})
    assert "the last electro-thermal solve on this computer took 59 s for 428,110 unknowns" in text
    assert "no earlier" not in thermal_cost_text(size, {"seconds": 0.7, "unknowns": 83_500.0})
    assert "no earlier" in thermal_cost_text(size, {"seconds": "x"})          # a damaged setting


def test_an_electro_thermal_solve_records_its_memory_peak(demo_tree):
    _board, _project, tree = demo_tree
    st = tree.stats
    assert isinstance(st["memory_peak_raised"], bool)
    assert st["memory_peak_mb"] is None or (math.isfinite(st["memory_peak_mb"]) and st["memory_peak_mb"] > 0)
