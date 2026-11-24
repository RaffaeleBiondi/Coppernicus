"""Real boards, kept out of the repository: every ``boards_local/**/*.coppernicus.yml`` project
is opened and solved. Skipped when the folder or projects are absent (e.g. on CI)."""

from pathlib import Path

import pytest

from coppernicus.engine import Board, solve_rail
from coppernicus.project import Project

LOCAL = Path(__file__).resolve().parents[1] / "boards_local"
PROJECTS = sorted(LOCAL.glob("**/*.coppernicus.yml")) if LOCAL.is_dir() else []


@pytest.mark.skipif(not PROJECTS, reason="no local projects in boards_local/")
@pytest.mark.parametrize("path", PROJECTS, ids=lambda p: p.stem)
def test_local_project_solves(path):
    project = Project.load(path)
    board = Board.open(project.board_path())
    for rail in project.rails:
        res = solve_rail(board, rail, project.settings)
        assert res.energy_balance_error < 1e-6, rail.net
        assert sum(t.current for t in res.sources) == pytest.approx(
            sum(t.current for t in res.loads), rel=1e-6), rail.net
