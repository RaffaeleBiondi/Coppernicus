"""The committed examples are complete: each project's board file is in the repository."""

import subprocess
from pathlib import Path

import pytest

from coppernicus.project import Project

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = sorted((ROOT / "examples").glob("*/*.coppernicus.yml"))


def _tracked():
    try:
        out = subprocess.run(["git", "ls-files", "examples"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return set(out.split())


def test_every_example_board_is_committed():
    assert PROJECTS, "no example projects found"
    tracked = _tracked()
    for path in PROJECTS:
        board = Project.load(path).board_path().resolve().relative_to(ROOT).as_posix()
        assert board in tracked, f"{board} (board of {path.name}) is not in git"
