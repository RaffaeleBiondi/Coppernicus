"""The committed examples are complete: each project's board file is in the repository."""

import runpy
import subprocess
from pathlib import Path

import pytest
from kicad_fixture import write_strip_board

from coppernicus.board_fields import merge
from coppernicus.engine.kicad_fields import read_board_fields
from coppernicus.engine.synthetic import write_strip_kicad_pcb
from coppernicus.project import Project

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = sorted((ROOT / "examples").glob("*/*.coppernicus.yml"))
FIELDS_PROJECT = ROOT / "examples" / "fields" / "fields.coppernicus.yml"


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


def test_the_strip_kicad_pcb_writer_writes_the_test_fixture(tmp_path):
    fields = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"}, "U2": {"PI_RAIL": "GND", "PI_LOAD": "1A"}}
    extra = [("U2", 20.0, 3.0, "GND")]
    ours = write_strip_kicad_pcb(tmp_path / "a.kicad_pcb", fields, net="VCC", extra_parts=extra)
    fixture = write_strip_board(tmp_path / "b.kicad_pcb", fields, net="VCC", extra_parts=extra)
    assert ours.read_bytes() == fixture.read_bytes()
    assert write_strip_kicad_pcb(tmp_path / "c.kicad_pcb").read_bytes() == \
        write_strip_board(tmp_path / "d.kicad_pcb").read_bytes()


def test_the_fields_example_has_a_kicad_rail_over_a_different_project_rail():
    project = Project.load(FIELDS_PROJECT)
    found = read_board_fields(project.board_path())
    assert "PI_SOURCE" in found.fields["J1"]
    assert {"PI_LOAD", "PI_LIMIT_DROP"} <= set(found.fields["U1"])
    merged = merge(project, found.fields, found.pad_nets)
    origin = merged.origins["VCC"]
    assert origin.source == "fields"
    assert any(w.startswith("project file rail VCC ignored") for w in origin.warnings)


def test_make_examples_writes_the_committed_fields_example(tmp_path):
    make = runpy.run_path(str(ROOT / "examples" / "make_examples.py"))
    make["write_fields_example"](tmp_path)
    for name in ("fields.kicad_pcb", "fields.coppernicus.yml"):
        assert (tmp_path / name).read_bytes() == (FIELDS_PROJECT.parent / name).read_bytes(), name
