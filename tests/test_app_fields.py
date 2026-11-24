"""The desktop app reads the KiCad footprint fields as the CLI does: an offscreen MainWindow on
the synthetic strip .kicad_pcb of tests/kicad_fixture.py. The window's project stays the
user's (the only thing saved); what it solves, draws and judges is the merged project."""

import json
import os

import pytest
from kicad_fixture import write_strip_board

from coppernicus.cli import EXIT_LIMIT, main
from coppernicus.project import Limits, Load, Project, Rail, Source

FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"},
          "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1000mA", "PI_PINS": "1", "PI_LIMIT_DROP": "2%"}}
TIMEOUT_S = 120


@pytest.fixture(scope="module")
def qapp():
    """A QApplication on the offscreen platform, set only when nothing else chose one, and
    taken back out of the environment afterwards."""
    added = "QT_QPA_PLATFORM" not in os.environ
    if added:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    yield app
    if added:
        os.environ.pop("QT_QPA_PLATFORM", None)


@pytest.fixture()
def dialogs(monkeypatch):
    """Message boxes recorded instead of shown: (kind, title, text)."""
    from PySide6.QtWidgets import QMessageBox
    shown = []

    def box(kind):
        def show(_parent, title, text, *_a, **_k):
            shown.append((kind, title, text))
            return QMessageBox.Discard
        return show
    for kind in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(box(kind)))
    return shown


@pytest.fixture()
def window(qapp, dialogs):
    from coppernicus.app.main_window import MainWindow
    w = MainWindow()
    yield w
    w.dirty = False
    w.close()
    qapp.processEvents()


def strip(tmp_path, fields=FIELDS, rails=(), **kw):
    """The strip board with ``fields`` and a project file next to it."""
    write_strip_board(tmp_path / "strip.kicad_pcb", fields)
    return Project(board="strip.kicad_pcb", rails=list(rails), **kw).save(tmp_path / "strip.coppernicus.yml")


def open_net(window, project, net="VCC"):
    from PySide6.QtWidgets import QApplication
    assert window.open_project(project)
    window.nets.select(net)
    QApplication.processEvents()
    assert window.net == net


def solve(window):
    from PySide6.QtWidgets import QApplication
    before = window.worker
    window.solve()
    if window.worker is None or window.worker is before:
        return None
    window.worker.wait(TIMEOUT_S * 1000)
    QApplication.processEvents()
    QApplication.processEvents()
    return window.results.get(window.net)


def cli_rails(project, tmp_path, capsys, *extra):
    code = main(["solve", str(project), "--json", "--no-maps", "--out", str(tmp_path / "cli"), *extra])
    return code, json.loads(capsys.readouterr().out)


def QtCore():
    from PySide6 import QtCore as core
    return core


def net_item(window, net):
    from PySide6.QtCore import Qt
    found = [window.nets.list.item(i) for i in range(window.nets.list.count())
             if window.nets.list.item(i).data(Qt.UserRole) == net]
    return found[0] if found else None


# ---------------------------------------------------------------------- the net list

def test_a_kicad_rail_is_listed_as_configured_with_a_marker_naming_its_parts(window, tmp_path):
    from PySide6.QtWidgets import QApplication
    window.open_project(strip(tmp_path))
    QApplication.processEvents()
    item = net_item(window, "VCC")
    assert item is not None and "KiCad" in item.text() and item.font().bold()
    assert "KiCad footprint fields" in item.toolTip() and "J1, U1" in item.toolTip()
    assert window.net == "VCC"                       # the first rail, selected at open
    window.nets.select("VCC")
    assert window.net == "VCC"


def test_the_net_list_keeps_every_rail_after_one_with_a_tooltip(window):
    """The net-list items carry their net as data: every configured rail is still listed."""
    from pathlib import Path

    from PySide6.QtCore import Qt
    chain = Path(__file__).resolve().parents[1] / "examples" / "power_chain" / "power_chain.coppernicus.yml"
    window.open_project(chain)
    listed = {window.nets.list.item(i).data(Qt.UserRole) for i in range(window.nets.list.count())}
    assert {"VIN", "+3V3", "+1V8", "GND"} <= listed and window.net == "VIN"


# ---------------------------------------------------------------------- solving

def test_f5_on_a_kicad_rail_gives_the_cli_numbers_limits_and_notes(window, tmp_path, capsys):
    project = strip(tmp_path)
    code, doc = cli_rails(project, tmp_path, capsys)
    [cli] = doc["rails"]
    assert code == EXIT_LIMIT and cli["from_board_fields"] and cli["verdict"] == "fail"
    open_net(window, project)
    res = solve(window)
    assert res is not None
    assert [t.name for t in res.loads] == [d["name"] for d in cli["loads"]]
    for t, d in zip(res.loads, cli["loads"]):
        assert t.current == pytest.approx(d["current_a"], rel=1e-12)
        assert res.drop(t) * 1e3 == pytest.approx(d["drop_mv"], rel=1e-12)
    assert res.max_drop * 1e3 == pytest.approx(cli["max_drop_mv"], rel=1e-12)
    note = next(a for a in cli["assumptions"] if a.startswith("Rail VCC from KiCad footprint fields"))
    assert note in res.assumptions
    header = window.problems.summary.text()
    assert "limit(s) exceeded" in header          # judged by PI_LIMIT_DROP, as the CLI: 3.8 % > 2 %
    assert window.project.rails == []              # the user's project is left as it was


def solve_tree(window):
    from PySide6.QtWidgets import QApplication
    before = window.tree_worker
    window.solve_tree()
    if window.tree_worker is None or window.tree_worker is before:
        return None
    window.tree_worker.wait(TIMEOUT_S * 1000)
    QApplication.processEvents()
    QApplication.processEvents()
    return window.tree


def test_ctrl_t_solves_the_merged_project_as_the_cli_tree(window, tmp_path, capsys):
    from coppernicus.power_tree import tree_summary
    project = strip(tmp_path)
    cli = json.loads(json.dumps(cli_rails(project, tmp_path, capsys, "--tree")[1]))
    open_net(window, project)
    tree = solve_tree(window)
    assert tree is not None
    doc = json.loads(json.dumps(tree_summary(tree)))
    for key in ("verdict", "loads", "vrms", "assumptions", "warnings"):
        assert doc[key] == cli[key], key
    assert any(a.startswith("Rail VCC from KiCad footprint fields") for a in doc["assumptions"])


def test_an_ignored_project_rail_warns_in_the_result_and_the_kicad_values_are_solved(window, tmp_path):
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])
    open_net(window, strip(tmp_path, rails=[own]))
    res = solve(window)
    assert res is not None and res.loads[0].current == pytest.approx(1.0)        # KiCad wins
    [warning] = [w for w in res.warnings if w.startswith("project file rail VCC ignored")]
    assert "KiCad U1 1 A pins 1, project U1 3 A" in warning
    assert warning in window.problems.notes.toPlainText()


# ---------------------------------------------------------------------- a field that cannot be used

OFF_SWITCH = "; or turn them off: Project → Use KiCad footprint fields"


@pytest.mark.parametrize("fields, extra", [
    ({"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}, ()),                       # merge: no PI_SOURCE
    ({"J1": FIELDS["J1"], "U1": FIELDS["U1"]}, [("U1", 20.0, 3.0, "VCC")])])  # read: two U1
def test_a_field_error_refuses_f5_and_ctrl_t_with_the_cli_text(window, dialogs, tmp_path, capsys,
                                                              fields, extra):
    write_strip_board(tmp_path / "strip.kicad_pcb", fields, extra_parts=extra)
    project = Project(board="strip.kicad_pcb").save(tmp_path / "strip.coppernicus.yml")
    assert main(["solve", str(project), "--no-maps", "--out", str(tmp_path / "cli")]) == 2
    printed = capsys.readouterr().err.strip()
    assert printed.startswith("error: ")
    refusal = printed.removeprefix("error: ") + OFF_SWITCH
    open_net(window, project)
    assert solve(window) is None and window.results == {}
    assert dialogs[-1][2] == refusal
    assert solve_tree(window) is None and window.tree is None
    assert dialogs[-1][2] == refusal
    assert refusal in window.rail_panel.banner.error.text()                  # the red line
    assert refusal in window.problems.notes.toPlainText()
    window.nets.select("GND" if "GND" in window.board.nets() else "VCC")     # survives a net change
    assert refusal in window.problems.notes.toPlainText()


# ---------------------------------------------------------------------- the rail panel

READ_ONLY = "VCC comes from KiCad fields: Override in project to edit it"


def test_a_kicad_rail_is_shown_read_only_each_value_naming_its_field(window, tmp_path):
    open_net(window, strip(tmp_path))
    assert not window.dirty                              # showing it read-only edits nothing
    rp = window.rail_panel
    assert rp.banner.text.text() == "From KiCad footprint fields (parts J1, U1) — read-only"
    assert rp.banner.button.text() == "Override in project" and not rp.banner.button.isHidden()
    assert "Rail VCC from KiCad footprint fields" in rp.banner.text.toolTip()     # the notes
    assert rp.sources.rows() == [("", "J1", None, 1.0)] and rp.loads.rows() == [("", "U1", ["1"], 1.0)]
    for table in (rp.sources, rp.loads):
        assert not table.add_button.isEnabled() and not table.remove_button.isEnabled()
        part, pins, value = (table.table.cellWidget(0, c) for c in (1, 2, 3))
        assert not part.isEnabled() and pins.isReadOnly() and value.isReadOnly()
        assert not table.table.item(0, 0).flags() & QtCore().Qt.ItemIsEditable
    assert 'PI_SOURCE "1V"' in rp.sources.table.cellWidget(0, 3).toolTip()
    tip = rp.loads.table.cellWidget(0, 2).toolTip()
    assert 'PI_LOAD "1000mA"' in tip and 'PI_PINS "1"' in tip and "U1" in tip
    assert not rp.source_box.isEnabled() and not rp.load_box.isEnabled()
    assert rp.max_drop.text() == "2" and rp.max_drop.isReadOnly()
    assert "PI_LIMIT_DROP" in rp.max_drop.toolTip() and "U1" in rp.max_drop.toolTip()
    assert not rp.max_j.isReadOnly() and not rp.max_via.isReadOnly()
    rp.set_pick("load")                                  # a click on a pad picks nothing
    assert window.pick_pad(40.0, 0.0) == READ_ONLY and window.project.rails == []


def test_a_limit_typed_on_a_kicad_rail_saves_as_a_limits_only_project_rail(window, tmp_path):
    open_net(window, strip(tmp_path))
    rp = window.rail_panel
    rp.max_via.setText("2")
    rp.max_via.editingFinished.emit()
    rp.max_drop.setText("5")                  # read-only on screen: a script typing it changes nothing
    rp.max_drop.editingFinished.emit()
    assert window.project.rails == [Rail("VCC", limits=Limits(max_via_current=2.0))]
    merged = window.merged.project.rail("VCC")
    assert merged.limits == Limits(max_drop_pct=2.0, max_via_current=2.0) and merged.loads[0].ref == "U1"
    assert window.merged.warnings == [] and window.dirty
    window._save(tmp_path / "saved.coppernicus.yml")
    [saved] = Project.load(tmp_path / "saved.coppernicus.yml").rails
    assert saved == Rail("VCC", limits=Limits(max_via_current=2.0))


def test_without_pi_limit_drop_the_drop_limit_of_a_kicad_rail_is_the_projects(window, tmp_path):
    fields = {"J1": FIELDS["J1"], "U1": {k: v for k, v in FIELDS["U1"].items() if k != "PI_LIMIT_DROP"}}
    open_net(window, strip(tmp_path, fields))
    rp = window.rail_panel
    assert rp.max_drop.text() == "" and not rp.max_drop.isReadOnly()
    rp.max_drop.setText("3")
    rp.max_drop.editingFinished.emit()
    assert window.project.rails == [Rail("VCC", limits=Limits(max_drop_pct=3.0))]
    assert window.merged.project.rail("VCC").limits.max_drop_pct == 3.0


def test_an_ignored_project_rail_is_shown_with_its_warning_and_the_kicad_values(window, tmp_path):
    own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])
    open_net(window, strip(tmp_path, rails=[own]))
    rp = window.rail_panel
    assert rp.loads.rows() == [("", "U1", ["1"], 1.0)]               # KiCad's, not the project's 3 A
    assert not rp.banner.warnings.isHidden()
    assert "project file rail VCC ignored" in rp.banner.warnings.text()
    assert "KiCad U1 1 A pins 1, project U1 3 A" in rp.banner.warnings.text()


def test_the_net_tie_option_of_a_kicad_rail_is_saved_on_its_own(window, tmp_path):
    open_net(window, strip(tmp_path))
    window.rail_panel.follow_ties.setChecked(False)        # shown only with net ties; edited here
    assert window.project.rails == [Rail("VCC", follow_net_ties=False)]
    assert window.merged.project.rail("VCC").follow_net_ties is False
    assert window.merged.project.rail("VCC").loads[0].ref == "U1"
    window._save(tmp_path / "saved.coppernicus.yml")
    assert Project.load(tmp_path / "saved.coppernicus.yml").rails == [Rail("VCC", follow_net_ties=False)]
