"""Project files: YAML round trip, pin parsing, relative board paths."""

from pathlib import Path, PureWindowsPath

import pytest
import yaml

from coppernicus.project import (
    Limits,
    Link,
    Load,
    Project,
    ProjectError,
    Rail,
    Settings,
    Source,
    carry_over,
    parse_pins,
)

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo" / "demo.coppernicus.yml"


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


def test_board_path_is_portable_between_windows_and_posix(tmp_path):
    """A project written on Windows by an older version has backslashes in its board path:
    it must open on any system, and saving writes "/" whatever the system."""
    (tmp_path / "boards").mkdir()
    board = tmp_path / "boards" / "b.kicad_pcb"
    board.write_text("")
    (tmp_path / "proj").mkdir()
    windows_style = str(PureWindowsPath("..", "boards", "b.kicad_pcb"))      # "..\\boards\\b.kicad_pcb"
    assert "\\" in windows_style
    path = tmp_path / "proj" / "x.coppernicus.yml"
    path.write_text(f"board: '{windows_style}'\nrails: []\n", encoding="utf-8")
    p = Project.load(path)
    assert p.board_path().resolve() == board.resolve()
    p.save()
    text = path.read_text(encoding="utf-8")
    assert "board: ../boards/b.kicad_pcb" in text and "\\" not in text
    assert Project.load(path).board_path().resolve() == board.resolve()


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


# ---------------------------------------------------------------- project model v2

def test_v1_project_loads_unchanged():
    """A file written before v2 loads, and writes back the same dict and the same text."""
    text = DEMO.read_text(encoding="utf-8")
    p = Project.load(DEMO)
    assert p.to_dict() == yaml.safe_load(text)
    assert yaml.safe_dump(p.to_dict(), sort_keys=False, allow_unicode=True) == text
    assert p.links == [] and p.engine_options == {}


def test_v2_defaults_write_nothing_new():
    assert set(Project().to_dict()) == {"board", "settings", "rails"}
    d = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)], loads=[Load("U1", 1.0)])]).to_dict()
    assert set(d["rails"][0]["sources"][0]) == {"ref", "pins", "voltage"}
    assert set(d["rails"][0]["loads"][0]) == {"ref", "pins", "current"}


def _v2_project():
    return Project(
        board="b.kicad_pcb",
        rails=[
            Rail("+3V3", sources=[Source("U1", 3.3, ["2"], kind="smps", efficiency=0.9,
                                         quiescent_a=0.005, input_pins=["1"], input_net="VIN",
                                         ground_pins=["3"], ground_net="GND")],
                 loads=[Load("U4", 0.5, ["1"], ground_net="GND")]),
            Rail("+1V8", sources=[Source("U2", 1.8, ["2"], name="LDO", kind="ldo",
                                         quiescent_a=0.002, input_ref="U2", input_pins=["1"],
                                         input_net="+3V3_F", ground_pins=["3"], ground_net="GND",
                                         sense_ref="U3", sense_pins=["1"], sense_net="+1V8",
                                         ground_sense_ref="U3", ground_sense_pins=["2"],
                                         ground_sense_net="GND")],
                 loads=[Load("U3", 2.0, ["1"], ground_ref="U3", ground_pins=["2"],
                             ground_net="GND")]),
        ],
        links=[Link("FB1", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", 0.05)],
        engine_options={"excluded_nets": ["VIN"]},
    )


def test_round_trip_v2_fields(tmp_path):
    p = _v2_project()
    path = p.save(tmp_path / "v2.coppernicus.yml")
    q = Project.load(path)
    assert q.rails == p.rails
    assert q.links == p.links
    assert q.engine_options == p.engine_options
    # Pin references are nested mappings in the file, the part omitted when it is the
    # terminal's own.
    d = yaml.safe_load(path.read_text())
    u1 = d["rails"][0]["sources"][0]
    assert u1["input"] == {"net": "VIN", "pins": ["1"]}
    assert d["rails"][0]["loads"][0]["ground"] == {"net": "GND", "pins": "all"}
    assert d["rails"][1]["sources"][0]["sense"] == {"ref": "U3", "net": "+1V8", "pins": ["1"]}
    assert d["links"][0] == {"name": "FB1", "ref": "FB1", "a": {"net": "+3V3", "pins": ["1"]},
                             "b": {"net": "+3V3_F", "pins": ["2"]}, "resistance_ohm": 0.05}


@pytest.mark.parametrize("change, words", [
    (lambda s: s.update(kind="buck"), "kind"),
    (lambda s: s.update(efficiency=90), "fraction"),
    (lambda s: s.update(efficiency=0), "fraction"),
    (lambda s: s.update(quiescent_a=-1), "quiescent_a"),
    (lambda s: s.update(input={"pins": ["1"]}), "net"),
    (lambda s: s.update(ground="GND"), "ground"),
])
def test_v2_source_values_are_checked(change, words):
    src = {"ref": "U1", "voltage": 3.3, "pins": "all"}
    change(src)
    with pytest.raises(ProjectError, match=words):
        Project.from_dict({"rails": [{"net": "+3V3", "sources": [src]}]})


@pytest.mark.parametrize("link, words", [
    ({"name": "FB1", "ref": "FB1", "a": {"net": "A"}, "b": {"net": "B"}}, "resistance_ohm"),
    ({"name": "FB1", "ref": "FB1", "a": {"net": "A"}, "b": {"net": "B"}, "resistance_ohm": -1},
     "resistance_ohm"),
    ({"name": "FB1", "ref": "FB1", "a": {"net": "A"}, "resistance_ohm": 0.1}, "b"),
    ({"ref": "FB1", "a": {"net": "A"}, "b": {"net": "B"}, "resistance_ohm": 0.1}, "name"),
])
def test_link_values_are_checked(link, words):
    with pytest.raises(ProjectError, match=words):
        Project.from_dict({"links": [link]})


def test_engine_options_must_be_a_mapping():
    with pytest.raises(ProjectError, match="engine_options"):
        Project.from_dict({"engine_options": ["excluded_nets"]})


def test_carry_over_keeps_fields_an_editor_does_not_show():
    """The rail editor knows only name, ref, pins and the value: rebuilding the rows from it
    must keep a regulator's kind, input and ground pins (matched by part and pins, then by
    part)."""
    u1 = Source("U1", 3.3, ["2"], kind="smps", efficiency=0.9, input_net="VIN", ground_net="GND")
    u4 = Load("U4", 0.5, ["1"], ground_net="GND")
    edited = carry_over([u1], [Source("J1", 5.0), Source("U1", 3.4, ["2"], name="Buck")])
    assert edited[0] == Source("J1", 5.0)
    assert edited[1] == Source("U1", 3.4, ["2"], name="Buck", kind="smps", efficiency=0.9,
                               input_net="VIN", ground_net="GND")
    # Pins changed: still the same part, so it keeps its ground.
    assert carry_over([u4], [Load("U4", 0.7, ["1", "3"])]) == [
        Load("U4", 0.7, ["1", "3"], ground_net="GND")]
    # A row deleted above it does not shift the fields onto another part.
    u5 = Load("U5", 0.1)
    assert carry_over([u5, u4], [Load("U4", 0.5, ["1"])]) == [u4]


def test_carry_over_by_row_keeps_each_rows_own_fields():
    """With the row each terminal came from (``origins``), a row keeps its own hidden
    fields whatever happens to its pins, and gets them back when its part is picked again."""
    loaded = [Load("U1", 0.1, ["1"], ground_net="GND"), Load("U1", 0.2, ["2"], ground_net="AGND")]
    # Row 0's pins edited to "2": no swap with row 1.
    edited = carry_over(loaded, [Load("U1", 0.1, ["2"]), Load("U1", 0.2, ["2"])], [0, 1])
    assert [ld.ground_net for ld in edited] == ["GND", "AGND"]
    # Row 0 switched to another part: defaults; switched back: its fields again.
    assert carry_over(loaded, [Load("U9", 0.1, ["1"])], [0]) == [Load("U9", 0.1, ["1"])]
    assert carry_over(loaded, [Load("U1", 0.1, ["1"])], [0]) == [loaded[0]]
    # A new row (no origin) starts from the defaults; a deleted row shifts nothing.
    assert carry_over(loaded, [Load("U1", 0.2, ["2"]), Load("U1", 0.3)], [1, None]) == [
        loaded[1], Load("U1", 0.3)]


# ---------------------------------------------------------------------- copper_thickness_mm

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*/*.coppernicus.yml"))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_files_without_thickness_round_trip_byte_identical(example, tmp_path):
    """The optional setting writes nothing while empty: a project file saved again is
    the same bytes (line ends aside: on Windows the checkout and a save may use CRLF)."""
    copy = tmp_path / example.parent.name / example.name
    copy.parent.mkdir()
    copy.write_bytes(example.read_bytes())
    project = Project.load(copy)
    assert project.settings.copper_thickness_mm == {}
    project.save()

    def lf(path):
        return path.read_bytes().replace(b"\r\n", b"\n")

    assert lf(copy) == lf(example)


def test_copper_thickness_round_trip(tmp_path):
    p = Project.load(DEMO)
    p.settings.copper_thickness_mm = {"B.Cu": 0.07, "F.Cu": 0.035}
    out = p.save(tmp_path / "what-if.coppernicus.yml")
    text = out.read_text(encoding="utf-8")
    assert "  copper_thickness_mm:\n    B.Cu: 0.07\n    F.Cu: 0.035\n" in text
    assert Project.load(out).settings.copper_thickness_mm == {"B.Cu": 0.07, "F.Cu": 0.035}
    p.settings.copper_thickness_mm = {}
    p.save()
    assert "copper_thickness_mm" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", [35, 0, -0.035, float("nan"), float("inf"), "thick", True, None])
def test_copper_thickness_values_are_checked(value, tmp_path):
    path = tmp_path / "bad.coppernicus.yml"
    path.write_text(yaml.safe_dump({"board": "x.kicad_pcb",
                                    "settings": {"copper_thickness_mm": {"B.Cu": value}}}),
                    encoding="utf-8")
    with pytest.raises(ProjectError) as e:
        Project.load(path)
    assert "copper_thickness_mm[B.Cu]" in str(e.value) and "1 oz copper is 0.035, not 35" in str(e.value)
    with pytest.raises(ProjectError, match="map copper layer names to mm"):
        Settings(copper_thickness_mm=[0.035])


def test_loader_edge_values():
    """``kind:`` left empty is the default; NaN numbers and a part named inside a link side
    are refused instead of silently kept or dropped."""
    p = Project.from_dict({"rails": [{"net": "+3V3", "sources": [
        {"ref": "U1", "voltage": 3.3, "kind": None}]}]})
    assert p.rails[0].sources[0].kind == "source"
    for bad in ({"ref": "U1", "voltage": ".nan"}, {"ref": "U1", "voltage": 3.3, "quiescent_a": "nan"}):
        with pytest.raises(ProjectError, match="number"):
            Project.from_dict({"rails": [{"net": "+3V3", "sources": [bad]}]})
    with pytest.raises(ProjectError, match="number"):
        Project.from_dict({"rails": [{"net": "+3V3", "loads": [{"ref": "U1", "current": float("nan")}]}]})
    with pytest.raises(ProjectError, match="number"):
        Project.from_dict({"links": [{"name": "FB1", "ref": "FB1", "a": {"net": "A"}, "b": {"net": "B"},
                                      "resistance_ohm": float("nan")}]})
    with pytest.raises(ProjectError, match="FB1.*ref"):
        Project.from_dict({"links": [{"name": "FB1", "ref": "FB1", "a": {"ref": "R5", "net": "A"},
                                      "b": {"net": "B"}, "resistance_ohm": 0.1}]})
    # A limit that is NaN would pass everything (second review, finding 8).
    for key in ("max_ground_rise_mv", "max_drop_pct"):
        with pytest.raises(ProjectError, match=f"{key}.*number"):
            Project.from_dict({"rails": [{"net": "GND", "limits": {key: float("nan")}}]})
    with pytest.raises(ProjectError, match="max_drop_pct.*number"):
        Project.from_dict({"rails": [{"net": "GND", "limits": {"max_drop_pct": "lots"}}]})
    assert Project.from_dict({"rails": [{"net": "GND", "limits": {"max_drop_pct": None}}]}
                             ).rails[0].limits.max_drop_pct is None


def test_ground_rise_limit_round_trip_and_omitted_when_unset(tmp_path):
    p = Project(board="b.kicad_pcb", rails=[Rail("GND", limits=Limits(max_ground_rise_mv=10.0))])
    path = p.save(tmp_path / "g.coppernicus.yml")
    assert "max_ground_rise_mv: 10.0" in path.read_text()
    assert Project.load(path).rails[0].limits.max_ground_rise_mv == 10.0
    plain = Project(board="b.kicad_pcb", rails=[Rail("VCC", limits=Limits(max_drop_pct=1.0))]).to_dict()
    assert plain["rails"][0]["limits"] == {"max_drop_pct": 1.0}
