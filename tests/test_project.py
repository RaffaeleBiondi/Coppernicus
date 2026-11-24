"""Project files: YAML round trip, pin parsing, relative board paths."""

import pytest

from coppernicus.project import Limits, Load, Project, ProjectError, Rail, Settings, Source, parse_pins


def test_parse_pins():
    assert parse_pins(None) is None
    assert parse_pins("all") is None
    assert parse_pins(" * ") is None
    assert parse_pins("1, 2,A3") == ["1", "2", "A3"]
    assert parse_pins([1, "2"]) == ["1", "2"]
    assert parse_pins(5) == ["5"]
    with pytest.raises(ProjectError):
        parse_pins({"bad": 1})


def test_round_trip_keeps_everything(tmp_path):
    (tmp_path / "boards").mkdir()
    board = tmp_path / "boards" / "b.kicad_pcb"
    board.write_text("")
    p = Project(board=str(board), settings=Settings(max_edge_mm=0.3, via_plating_um=25.0, copper_temp_c=60),
                rails=[Rail("+3V3", sources=[Source("L2", 3.3, ["2"], name="Buck")],
                            loads=[Load("U1", 0.5), Load("J6", 0.05, ["24", "26"])],
                            limits=Limits(max_drop_pct=2.0), follow_net_ties=False)])
    (tmp_path / "proj").mkdir()
    path = p.save(tmp_path / "proj" / "x.coppernicus.yml")
    text = path.read_text()
    assert "board: ../boards/b.kicad_pcb" in text           # stored relative to the project
    q = Project.load(path)
    assert q.board_path().resolve() == board.resolve()
    assert q.settings == p.settings
    assert q.rails == p.rails


def test_new_project_with_relative_board_saves_relative_to_its_own_folder(tmp_path):
    p = Project(board="demo.json.gz", rails=[Rail("VCC")])
    path = p.save(tmp_path / "demo.coppernicus.yml")
    assert "board: demo.json.gz" in path.read_text()
    assert Project.load(path).board_path() == tmp_path / "demo.json.gz"


def test_malformed_file_is_a_project_error(tmp_path):
    path = tmp_path / "bad.coppernicus.yml"
    path.write_text("rails:\n  - sources: []\n")                 # no net
    with pytest.raises(ProjectError):
        Project.load(path)


def test_ensure_rail():
    p = Project()
    r = p.ensure_rail("+5V")
    assert p.ensure_rail("+5V") is r and p.rail("+5V") is r and p.rail("GND") is None


def test_marked_rails_round_trip(tmp_path):
    p = Project(board="b.kicad_pcb", marked_rails=["VBUS_X"])
    path = p.save(tmp_path / "m.coppernicus.yml")
    assert "marked_rails" in path.read_text()
    assert Project.load(path).marked_rails == ["VBUS_X"]
    # Without the key (every file written before it existed) the list is empty, and an
    # empty list is not written, so old files stay byte-identical on save.
    old = Project(board="b.kicad_pcb", rails=[Rail("VCC")])
    path = old.save(tmp_path / "old.coppernicus.yml")
    assert "marked_rails" not in path.read_text()
    assert Project.load(path).marked_rails == []
