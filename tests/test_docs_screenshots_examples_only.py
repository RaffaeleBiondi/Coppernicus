"""tools/make_docs_screenshots.py: the docs' images come from the repository's synthetic
examples only. A window's open_project / open_board and every file a ``coppernicus`` command
is given must be in examples/, and so must the board a project names; anything else stops the
run before it is read."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from coppernicus.project import Project

TOOL = Path(__file__).resolve().parents[1] / "tools" / "make_docs_screenshots.py"


def _tool():
    spec = importlib.util.spec_from_file_location("make_docs_screenshots", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_input_of_the_run_is_an_example():
    tool = _tool()
    for path in tool.INPUTS:
        assert tool.example_only(path) == path.resolve()


def test_a_board_or_project_outside_the_examples_is_refused(tmp_path):
    tool = _tool()
    board = tmp_path / "private" / "board.kicad_pcb"
    board.parent.mkdir()
    board.write_text("(kicad_pcb)")
    project = Project(board=str(board)).save(tmp_path / "private" / "p.coppernicus.yml")
    for path in (board, project, tool.ROOT / "README.md", tool.EXAMPLES / ".." / "pyproject.toml"):
        with pytest.raises(tool.Unfinished, match="examples only"):
            tool.example_only(path)


def test_a_project_in_the_examples_naming_a_board_elsewhere_is_refused(tmp_path, monkeypatch):
    tool = _tool()
    monkeypatch.setattr(tool, "EXAMPLES", tmp_path / "examples")
    (tmp_path / "examples").mkdir()
    (tmp_path / "private").mkdir()
    board = tmp_path / "private" / "board.kicad_pcb"
    board.write_text("(kicad_pcb)")
    project = Project(board=str(board)).save(tmp_path / "examples" / "x.coppernicus.yml")
    with pytest.raises(tool.Unfinished, match="names the board"):
        tool.example_only(project)


def test_a_window_and_a_command_refuse_before_reading(tmp_path):
    tool = _tool()
    opened = []
    window = tool.guard_window(SimpleNamespace(open_project=opened.append, open_board=opened.append))
    window.open_project(tool.DEMO)
    with pytest.raises(tool.Unfinished):
        window.open_board(tmp_path / "board.kicad_pcb")
    assert opened == [tool.DEMO.resolve()]
    with pytest.raises(tool.Unfinished):                       # before any child process starts
        tool.coppernicus(("solve", tmp_path / "p.coppernicus.yml"), tmp_path / "out", 10)
    assert not (tmp_path / "out").exists()


def test_the_runs_own_copy_of_an_example_is_allowed(tmp_path):
    tool = _tool()
    copy = tool.chain_with_limits(tmp_path / "chain")
    assert tool.example_only(copy) == Path(copy).resolve()
