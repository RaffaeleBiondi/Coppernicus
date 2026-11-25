"""The desktop app reads the KiCad footprint fields as the CLI does, on the synthetic strip
.kicad_pcb of tests/kicad_fixture.py. The window's project stays the user's (the only thing
saved); what it solves, draws and judges is the merged project.

Each test runs its window in a child process with ``QT_QPA_PLATFORM=offscreen``, as
tests/test_app_window.py does: a Qt abort fails that test only, a hang is cut by the timeout,
and nothing of Qt's state leaks into the other tests. Every dialog is answered by the child
(recorded in ``DIALOGS``); one nobody expected fails the test, and so does an exception raised
inside a Qt slot. The CLI runs here, in the pytest process, and its results go to the child."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from kicad_fixture import write_strip_board

from coppernicus.cli import EXIT_LIMIT, main
from coppernicus.project import Project

pytest.importorskip("PySide6")

TESTS = Path(__file__).resolve().parent
FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "1V"},
          "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1000mA", "PI_PINS": "1", "PI_LIMIT_DROP": "2%"}}
TIMEOUT_S = 180
OFF_SWITCH = "; or turn them off: Project → Use KiCad footprint fields"

PRELUDE = '''
import math
import sys
import traceback
from pathlib import Path

sys.path.insert(0, TESTS)
TMP = Path(TMP)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMenu, QMessageBox

ERRORS = []                       # exceptions raised anywhere, Qt slots included


def _hook(kind, value, tb):
    ERRORS.append(value)
    traceback.print_exception(kind, value, tb)


sys.excepthook = _hook

DIALOGS = []        # (kind, title, text) of every dialog, answered here instead of shown
ANSWERS = {}        # what a QMessageBox.<kind>(...) returns (default: Discard)
CLICKS = []         # the button text each QMessageBox.exec() clicks, in turn
FILES = []          # what each file dialog answers, in turn


def _static_box(kind):
    def show(_parent, title, text, *_a, **_k):
        DIALOGS.append((kind, title, text))
        return ANSWERS.get(kind, QMessageBox.Discard)
    return staticmethod(show)


for _kind in ("critical", "warning", "information", "question"):
    setattr(QMessageBox, _kind, _static_box(_kind))


def _exec_box(box):
    DIALOGS.append(("exec", box.windowTitle(), box.text() + "\\n" + box.informativeText()))
    assert CLICKS, f"unexpected dialog: {box.windowTitle()}: {box.text()}"
    want = CLICKS.pop(0)
    button = next((b for b in box.buttons() if b.text() == want), None)
    assert button is not None, f"no button {want!r} in {[b.text() for b in box.buttons()]}"
    button.click()
    return 0


def _unexpected(*_a, **_k):
    raise AssertionError("unexpected dialog or menu")


QMessageBox.exec = _exec_box
QDialog.exec = _unexpected
QMenu.exec = _unexpected


def _file_dialog(kind):
    def ask(*_a, **_k):
        DIALOGS.append(("file", kind, ""))
        assert FILES, f"unexpected file dialog: {kind}"
        return FILES.pop(0) if kind == "getExistingDirectory" else (str(FILES.pop(0)), "")
    return staticmethod(ask)


for _kind in ("getOpenFileName", "getSaveFileName", "getExistingDirectory"):
    setattr(QFileDialog, _kind, _file_dialog(_kind))

from kicad_fixture import write_strip_board

from coppernicus.app import fields_view
from coppernicus.app.main_window import MainWindow
from coppernicus.project import Limits, Load, Project, Rail, Source

app = QApplication.instance() or QApplication([])
window = MainWindow()
window.dirty = False
rp = window.rail_panel
CHAIN = Path(TESTS).parent / "examples" / "power_chain" / "power_chain.coppernicus.yml"
READ_ONLY = "VCC comes from KiCad fields: Override in project to edit it"


def strip(folder, fields=FIELDS, rails=(), extra_parts=(), **kw):
    """The strip board with ``fields`` in ``folder``, and a project file next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    write_strip_board(folder / "strip.kicad_pcb", fields, extra_parts=extra_parts)
    return Project(board="strip.kicad_pcb", rails=list(rails), **kw).save(folder / "strip.coppernicus.yml")


def open_net(project, net="VCC"):
    """Open ``project`` (unsaved edits of the one before are dropped) and select ``net``."""
    window.dirty = False
    assert window.open_project(project), f"cannot open {project}: {DIALOGS[-1:]}"
    window.nets.select(net)
    app.processEvents()
    assert window.net == net, window.net


def _wait(worker):
    worker.wait(SOLVE_MS)
    app.processEvents()
    app.processEvents()


def solve():
    """F5 on the shown net: its result, or None when the window refused to solve."""
    before = window.worker
    window.solve()
    if window.worker is None or window.worker is before:
        return None
    _wait(window.worker)
    return window.results.get(window.net)


def solve_tree():
    """Ctrl+T: the power tree, or None when the window refused to solve."""
    before = window.tree_worker
    window.solve_tree()
    if window.tree_worker is None or window.tree_worker is before:
        return None
    _wait(window.tree_worker)
    return window.tree


def net_item(net):
    found = [window.nets.list.item(i) for i in range(window.nets.list.count())
             if window.nets.list.item(i).data(Qt.UserRole) == net]
    return found[0] if found else None


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-12)
'''

EPILOGUE = '''
window.dirty = False
window.close()
app.processEvents()
assert not ERRORS, ERRORS
'''


def run_in_window(body: str, tmp_path: Path, **values) -> None:
    """Run ``body`` in a child process after PRELUDE: a MainWindow offscreen (``window``,
    its rail panel ``rp``), ``TMP`` (the test's tmp_path), ``VALUES`` (``values``, through
    JSON) and ``FIELDS``."""
    head = (f"import json\nTESTS = {str(TESTS)!r}\nTMP = {str(tmp_path)!r}\n"
            f"VALUES = json.loads({json.dumps(values)!r})\nFIELDS = json.loads({json.dumps(FIELDS)!r})\n"
            f"SOLVE_MS = {TIMEOUT_S * 1000 // 2}\n")
    script = head + PRELUDE + textwrap.dedent(body) + EPILOGUE
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                         timeout=TIMEOUT_S)
    assert out.returncode == 0, out.stdout + out.stderr


def strip(tmp_path, fields=FIELDS, rails=(), **kw):
    """The strip board with ``fields`` and a project file next to it (for the CLI)."""
    write_strip_board(tmp_path / "strip.kicad_pcb", fields)
    return Project(board="strip.kicad_pcb", rails=list(rails), **kw).save(tmp_path / "strip.coppernicus.yml")


def cli_rails(project, tmp_path, capsys, *extra):
    code = main(["solve", str(project), "--json", "--no-maps", "--out", str(tmp_path / "cli"), *extra])
    return code, json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------- the net list

def test_the_net_list_marks_a_kicad_rail_with_its_parts_and_keeps_every_rail(tmp_path):
    run_in_window('''
        assert window.open_project(strip(TMP / "a"))
        app.processEvents()
        item = net_item("VCC")
        assert item is not None and "KiCad" in item.text() and item.font().bold(), item and item.text()
        assert "KiCad footprint fields" in item.toolTip() and "J1, U1" in item.toolTip(), item.toolTip()
        assert window.net == "VCC", window.net             # the first rail, selected at open
        window.nets.select("VCC")
        assert window.net == "VCC"

        # The net-list items carry their net as data: every configured rail is still listed.
        window.dirty = False
        assert window.open_project(CHAIN)
        listed = {window.nets.list.item(i).data(Qt.UserRole) for i in range(window.nets.list.count())}
        assert {"VIN", "+3V3", "+1V8", "GND"} <= listed and window.net == "VIN", (listed, window.net)
    ''', tmp_path)


# ---------------------------------------------------------------------- solving

def test_f5_and_ctrl_t_on_a_kicad_rail_give_the_cli_numbers_limits_and_notes(tmp_path, capsys):
    project = strip(tmp_path)
    code, doc = cli_rails(project, tmp_path, capsys)
    [cli] = doc["rails"]
    assert code == EXIT_LIMIT and cli["from_board_fields"] and cli["verdict"] == "fail"
    tree = cli_rails(project, tmp_path, capsys, "--tree")[1]
    run_in_window('''
        from coppernicus.power_tree import tree_summary
        cli = VALUES["rail"]
        open_net(VALUES["project"])
        res = solve()
        assert res is not None, DIALOGS
        assert [t.name for t in res.loads] == [d["name"] for d in cli["loads"]]
        for t, d in zip(res.loads, cli["loads"]):
            assert close(t.current, d["current_a"]), (t.current, d)
            assert close(res.drop(t) * 1e3, d["drop_mv"]), (res.drop(t), d)
        assert close(res.max_drop * 1e3, cli["max_drop_mv"]), (res.max_drop, cli["max_drop_mv"])
        note = next(a for a in cli["assumptions"] if a.startswith("Rail VCC from KiCad footprint fields"))
        assert note in res.assumptions, res.assumptions
        header = window.problems.summary.text()
        assert "limit(s) exceeded" in header, header     # judged by PI_LIMIT_DROP, as the CLI: 3.8 % > 2 %
        assert window.project.rails == [], window.project.rails      # the user's project is as it was

        tree = solve_tree()                              # Ctrl+T: the merged project, as solve --tree
        assert tree is not None, DIALOGS
        doc = json.loads(json.dumps(tree_summary(tree)))
        for key in ("verdict", "loads", "vrms", "assumptions", "warnings"):
            assert doc[key] == VALUES["tree"][key], (key, doc[key], VALUES["tree"][key])
        assert any(a.startswith("Rail VCC from KiCad footprint fields") for a in doc["assumptions"])
    ''', tmp_path, project=str(project), rail=cli, tree=tree)


def test_an_ignored_project_rail_warns_and_the_kicad_values_are_shown_and_solved(tmp_path):
    run_in_window('''
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])
        open_net(strip(TMP / "a", rails=[own]))
        assert rp.loads.rows() == [("", "U1", ["1"], 1.0)], rp.loads.rows()   # KiCad's, not 3 A
        assert not rp.banner.warnings.isHidden()
        assert "project file rail VCC ignored" in rp.banner.warnings.text(), rp.banner.warnings.text()
        assert "KiCad U1 1 A pins 1, project U1 3 A" in rp.banner.warnings.text()
        res = solve()
        assert res is not None and close(res.loads[0].current, 1.0), res and res.loads    # KiCad wins
        [warning] = [w for w in res.warnings if w.startswith("project file rail VCC ignored")]
        assert "KiCad U1 1 A pins 1, project U1 3 A" in warning, warning
        assert warning in window.problems.notes.toPlainText()
    ''', tmp_path)


# ---------------------------------------------------------------------- a field that cannot be used

def test_a_field_error_refuses_f5_and_ctrl_t_with_the_cli_text_until_the_fields_are_off(tmp_path, capsys):
    cases = []
    for name, fields, extra in (("merge", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}, ()),     # no PI_SOURCE
                                ("read", FIELDS, [("U1", 20.0, 3.0, "VCC")])):                 # two U1
        folder = tmp_path / name
        folder.mkdir()
        write_strip_board(folder / "strip.kicad_pcb", fields, extra_parts=extra)
        project = Project(board="strip.kicad_pcb").save(folder / "strip.coppernicus.yml")
        assert main(["solve", str(project), "--no-maps", "--out", str(folder / "cli")]) == 2
        printed = capsys.readouterr().err.strip()
        assert printed.startswith("error: ")
        cases.append((str(project), printed.removeprefix("error: ") + OFF_SWITCH))
    run_in_window('''
        for project, refusal in VALUES["cases"]:
            open_net(project)
            assert solve() is None and window.results == {}, window.results
            assert DIALOGS[-1][2] == refusal, (DIALOGS[-1], refusal)
            assert solve_tree() is None and window.tree is None
            assert DIALOGS[-1][2] == refusal, (DIALOGS[-1], refusal)
            assert refusal in rp.banner.error.text(), rp.banner.error.text()       # the red line
            assert refusal in window.problems.notes.toPlainText()
            window.nets.select("GND" if "GND" in window.board.nets() else "VCC")   # survives a net change
            assert refusal in window.problems.notes.toPlainText()

        # Turning the fields off lifts the error: the project's own rail is solved.
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])
        open_net(strip(TMP / "off", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_LIMIT_DROP": "0%"}},
                       rails=[own]))
        assert window.fields_error and not rp.banner.error.isHidden(), window.fields_error
        window.use_fields_action.trigger()
        assert window.fields_error is None and rp.banner.error.isHidden(), window.fields_error
        assert "KiCad footprint fields" not in window.problems.notes.toPlainText()
        assert solve() is not None, DIALOGS
    ''', tmp_path, cases=cases)


# ---------------------------------------------------------------------- the rail panel

def test_a_kicad_rail_is_read_only_each_value_naming_its_field(tmp_path):
    run_in_window('''
        open_net(strip(TMP / "a"))
        assert not window.dirty                              # showing it read-only edits nothing
        assert rp.banner.text.text() == "From KiCad footprint fields (parts J1, U1) — read-only"
        assert rp.banner.button.text() == "Override in project" and not rp.banner.button.isHidden()
        assert "Rail VCC from KiCad footprint fields" in rp.banner.text.toolTip()     # the notes
        assert rp.sources.rows() == [("", "J1", None, 1.0)], rp.sources.rows()
        assert rp.loads.rows() == [("", "U1", ["1"], 1.0)], rp.loads.rows()
        for table in (rp.sources, rp.loads):
            assert not table.add_button.isEnabled() and not table.remove_button.isEnabled()
            part, pins, value = (table.table.cellWidget(0, c) for c in (1, 2, 3))
            assert not part.isEnabled() and pins.isReadOnly() and value.isReadOnly()
            assert not table.table.item(0, 0).flags() & Qt.ItemIsEditable
        assert 'PI_SOURCE "1V"' in rp.sources.table.cellWidget(0, 3).toolTip()
        tip = rp.loads.table.cellWidget(0, 2).toolTip()
        assert 'PI_LOAD "1000mA"' in tip and 'PI_PINS "1"' in tip and "U1" in tip, tip
        assert not rp.source_box.isEnabled() and not rp.load_box.isEnabled()
        assert rp.max_drop.text() == "2" and rp.max_drop.isReadOnly(), rp.max_drop.text()
        tip = rp.max_drop.toolTip()
        assert "PI_LIMIT_DROP" in tip and "U1" in tip, tip
        assert not rp.max_j.isReadOnly() and not rp.max_via.isReadOnly()
        rp.set_pick("load")                                  # a click on a pad picks nothing
        assert window.pick_pad(40.0, 0.0) == READ_ONLY and window.project.rails == []
        rp.set_pick("")

        # Leaving a read-only cell is not an edit.
        assert solve() is not None, DIALOGS
        rp.loads.table.cellWidget(0, 2).editingFinished.emit()      # focus left the pins
        assert "VCC" in window.results and not window.dirty and window.project.rails == []
    ''', tmp_path)


def test_the_limits_and_net_tie_option_of_a_kicad_rail_save_as_a_project_rail_of_their_own(tmp_path):
    run_in_window('''
        open_net(strip(TMP / "a"))
        rp.max_via.setText("2")
        rp.max_via.editingFinished.emit()
        rp.max_drop.setText("5")             # read-only on screen: a script typing it changes nothing
        rp.max_drop.editingFinished.emit()
        assert window.project.rails == [Rail("VCC", limits=Limits(max_via_current=2.0))], window.project.rails
        merged = window.merged.project.rail("VCC")
        assert merged.limits == Limits(max_drop_pct=2.0, max_via_current=2.0) and merged.loads[0].ref == "U1"
        assert window.merged.warnings == [] and window.dirty, window.merged.warnings
        window._save(TMP / "saved.coppernicus.yml")
        [saved] = Project.load(TMP / "saved.coppernicus.yml").rails
        assert saved == Rail("VCC", limits=Limits(max_via_current=2.0)), saved

        # Without PI_LIMIT_DROP, the drop limit is the project's.
        fields = {"J1": FIELDS["J1"], "U1": {k: v for k, v in FIELDS["U1"].items() if k != "PI_LIMIT_DROP"}}
        open_net(strip(TMP / "b", fields))
        assert rp.max_drop.text() == "" and not rp.max_drop.isReadOnly()
        rp.max_drop.setText("3")
        rp.max_drop.editingFinished.emit()
        assert window.project.rails == [Rail("VCC", limits=Limits(max_drop_pct=3.0))], window.project.rails
        assert window.merged.project.rail("VCC").limits.max_drop_pct == 3.0

        # The net-tie option (shown only with net ties) is saved on its own.
        open_net(strip(TMP / "c"))
        rp.follow_ties.setChecked(False)
        assert window.project.rails == [Rail("VCC", follow_net_ties=False)], window.project.rails
        assert window.merged.project.rail("VCC").follow_net_ties is False
        assert window.merged.project.rail("VCC").loads[0].ref == "U1"
        window._save(TMP / "ties.coppernicus.yml")
        rails = Project.load(TMP / "ties.coppernicus.yml").rails
        assert rails == [Rail("VCC", follow_net_ties=False)], rails
    ''', tmp_path)


def test_a_project_rail_over_incomplete_fields_is_editable_and_a_board_without_fields_has_no_banner(tmp_path):
    run_in_window('''
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])
        open_net(strip(TMP / "a", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "2A"}}, rails=[own]))
        assert window.fields_error is None and window.merged.origins["VCC"].source == "project"
        assert rp.banner.text.text().startswith("KiCad fields on VCC are incomplete"), rp.banner.text.text()
        assert rp.banner.button.isHidden() and not rp.read_only
        assert rp.loads.rows() == [("", "U1", None, 1.0)], rp.loads.rows()
        assert net_item("VCC").text() == "VCC"

        open_net(strip(TMP / "b"))
        write_strip_board(TMP / "plain.kicad_pcb")
        window.dirty = False
        window.open_board(TMP / "plain.kicad_pcb")
        app.processEvents()
        banner = rp.banner
        assert window.board_fields.fields == {} and window.merged.origins == {}
        assert banner.text.isHidden() and banner.button.isHidden() and banner.warnings.isHidden()
    ''', tmp_path)


# ---------------------------------------------------------------------- override, and back

def test_override_copies_the_kicad_rail_into_the_project_and_the_way_back_keeps_its_limits(tmp_path):
    run_in_window('''
        open_net(strip(TMP / "a"))
        rp.max_via.setText("2")                                   # a limits-only rail to start with
        rp.max_via.editingFinished.emit()
        assert solve() is not None and "VCC" in window.results
        rp.banner.button.click()                                  # Override in project
        own = window.project.rail("VCC")
        assert own.override_board_fields
        assert [(s.ref, s.voltage, s.pins) for s in own.sources] == [("J1", 1.0, None)], own.sources
        assert [(ld.ref, ld.current, ld.pins) for ld in own.loads] == [("U1", 1.0, ["1"])], own.loads
        assert own.limits == Limits(max_via_current=2.0), own.limits          # its own limits, kept
        assert window.merged.origins["VCC"].source == "overridden" and "VCC" not in window.results
        assert not rp.read_only and rp.loads.add_button.isEnabled() and not rp.max_drop.isReadOnly()
        text = rp.banner.text.text()
        assert text.startswith('Project rail overrides the KiCad fields (KiCad: source J1 1 V '
                               '(from KiCad field PI_SOURCE "1V"'), text
        assert rp.banner.button.text() == "Use KiCad fields"
        assert net_item("VCC").text() == "VCC"                    # no longer marked [KiCad]
        rp.loads.table.cellWidget(0, 3).setValue(0.5)             # now editable
        assert window.project.rail("VCC").loads[0].current == 0.5
        res = solve()
        assert close(res.loads[0].current, 0.5), res.loads
        assert any("override_board_fields is set" in a for a in res.assumptions), res.assumptions
        window._save(TMP / "saved.coppernicus.yml")
        saved = (TMP / "saved.coppernicus.yml").read_text(encoding="utf-8")
        assert "override_board_fields: true" in saved and "current: 0.5" in saved, saved

        asked = []
        fields_view.confirm_use_fields = lambda _w, net: asked.append(net) or True
        rp.banner.button.click()                                  # Use KiCad fields
        assert asked == ["VCC"], asked
        assert window.project.rails == [Rail("VCC", limits=Limits(max_via_current=2.0))], window.project.rails
        assert window.merged.origins["VCC"].source == "fields" and window.merged.warnings == []
        assert rp.read_only and rp.loads.rows() == [("", "U1", ["1"], 1.0)], rp.loads.rows()
        assert "KiCad" in net_item("VCC").text() and "VCC" not in window.results

        # The way back declined changes nothing.
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 0.5)], override_board_fields=True)
        open_net(strip(TMP / "b", rails=[own]))
        fields_view.confirm_use_fields = lambda _w, _net: False
        rp.banner.button.click()
        assert window.project.rails == [own] and not window.dirty, window.project.rails
    ''', tmp_path)


def test_override_over_an_ignored_project_rail_asks_which_to_keep_and_not_when_identical(tmp_path):
    run_in_window('''
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)], limits=Limits(max_via_current=2.0))
        project = strip(TMP / "a", rails=[own])
        for choice, current in (("project", 3.0), ("kicad", 1.0), (None, None)):
            open_net(project)
            asked = []
            fields_view.ask_override = lambda _w, net, differences: asked.append((net, differences)) or choice
            rp.banner.button.click()
            [(net, differences)] = asked
            assert net == "VCC", asked
            assert any("KiCad U1 1 A pins 1, project U1 3 A" in d for d in differences), asked
            rail = window.project.rail("VCC")
            if choice is None:
                assert rail == own and window.merged.origins["VCC"].source == "fields", (choice, rail)
                continue
            assert rail.override_board_fields and rail.loads[0].current == current, (choice, rail)
            assert rail.limits == Limits(max_via_current=2.0), (choice, rail.limits)
            assert window.merged.origins["VCC"].source == "overridden", choice
            assert window.merged.warnings == [], (choice, window.merged.warnings)

        # An identical project rail: nothing to ask.
        open_net(strip(TMP / "b", rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0, ["1"])])]))
        def never(*_a):
            raise AssertionError("asked")
        fields_view.ask_override = never
        rp.banner.button.click()
        assert window.project.rail("VCC").override_board_fields
        assert window.merged.origins["VCC"].source == "overridden"
    ''', tmp_path)


# ---------------------------------------------------------------------- saving

def test_saving_keeps_every_rail_the_project_says_something_about_and_edits_none(tmp_path):
    run_in_window('''
        # The net-tie option of a KiCad rail, saved with the fields turned off.
        open_net(strip(TMP / "a"))
        rp.follow_ties.setChecked(False)
        window.use_fields_action.trigger()                       # off
        window._save(TMP / "a" / "off.coppernicus.yml")
        rails = Project.load(TMP / "a" / "off.coppernicus.yml").rails
        assert rails == [Rail("VCC", follow_net_ties=False)], rails
        assert window.project.rails == [Rail("VCC", follow_net_ties=False)], window.project.rails
        window.use_fields_action.trigger()                       # on again: still not followed
        assert window.merged.project.rail("VCC").follow_net_ties is False

        # ... and while a field elsewhere cannot be used.
        other = {**FIELDS, "U2": {"PI_RAIL": "OTHER", "PI_LOAD": "1A"}}
        project = strip(TMP / "b", other, rails=[Rail("VCC", follow_net_ties=False)],
                        extra_parts=[("U2", 20.0, 3.0, "OTHER")])
        open_net(project)
        assert window.fields_error, "a field elsewhere should not be usable"
        rp.temp.setValue(25.0)                                   # any edit, then Save
        window.save_project()
        rails = Project.load(project).rails
        assert rails == [Rail("VCC", follow_net_ties=False)], rails

        # An override emptied of its sources and loads stays an override: saved, and shown.
        open_net(strip(TMP / "c"))
        rp.banner.button.click()                                 # Override in project
        for table in (rp.sources, rp.loads):
            while table.table.rowCount():
                table.table.selectRow(0)
                table.remove_row()
        assert window.project.rails == [Rail("VCC", override_board_fields=True)], window.project.rails
        window._save(TMP / "c" / "emptied.coppernicus.yml")
        rails = Project.load(TMP / "c" / "emptied.coppernicus.yml").rails
        assert rails == [Rail("VCC", override_board_fields=True)], rails
        assert window.merged.origins["VCC"].source == "overridden", window.merged.origins["VCC"]
        assert not rp.read_only and rp.banner.button.text() == "Use KiCad fields", rp.banner.button.text()
        assert window._rail_draft is window.project.rail("VCC")     # the panel edits the project's rail

        # A rail emptied by hand is not written, and saving leaves the one on screen alone.
        open_net(strip(TMP / "d", fields={}, rails=[Rail("VCC", [Source("J1", 1.0)], [Load("U1", 1.0)])]))
        shown = window.project.rail("VCC")
        for table in (rp.sources, rp.loads):
            while table.table.rowCount():
                table.table.selectRow(0)
                table.remove_row()
        window._save(TMP / "d" / "empty.coppernicus.yml")
        assert Project.load(TMP / "d" / "empty.coppernicus.yml").rails == []
        assert window.project.rails == [shown] and window._rail_draft is shown, window.project.rails
    ''', tmp_path)


def test_save_as_names_the_new_file_where_a_field_cannot_be_used(tmp_path):
    run_in_window('''
        open_net(strip(TMP / "a", {"U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}))       # no PI_SOURCE
        assert rp.banner.error.text().startswith("strip.coppernicus.yml: "), rp.banner.error.text()
        FILES.append(TMP / "a" / "renamed.coppernicus.yml")
        window.save_project_as()
        assert window.project.path.name == "renamed.coppernicus.yml", window.project.path
        error = rp.banner.error.text()
        assert error.startswith("renamed.coppernicus.yml: ") and window.fields_error in error, error
        notes = window.problems.notes.toPlainText()
        assert "renamed.coppernicus.yml: " in notes and "strip.coppernicus.yml" not in notes, notes
    ''', tmp_path)


# ---------------------------------------------------------------------- menus

def test_use_kicad_fields_off_takes_every_rail_from_the_project_and_is_saved(tmp_path):
    run_in_window('''
        menus = [a.text() for a in window.menuBar().actions()]
        assert menus == ["&File", "&Project", "View"], menus      # File first, View (hover) last
        own = Rail("VCC", [Source("J1", 1.0)], [Load("U1", 3.0)])
        open_net(strip(TMP / "a", rails=[own]))
        action = window.use_fields_action
        assert action.isCheckable() and action.isChecked() and action.text() == "Use KiCad footprint fields"
        assert action in window.project_menu.actions()
        action.trigger()                                          # off
        assert window.project.use_board_fields is False and window.dirty
        assert window.merged.origins == {} and net_item("VCC").text() == "VCC"
        assert not rp.read_only and rp.loads.rows() == [("", "U1", None, 3.0)], rp.loads.rows()
        assert rp.banner.text.isHidden() and rp.banner.warnings.isHidden()
        res = solve()
        assert close(res.loads[0].current, 3.0), res.loads
        assert not any("ignored" in w for w in res.warnings), res.warnings
        window._save(TMP / "off.coppernicus.yml")
        assert "use_board_fields: false" in (TMP / "off.coppernicus.yml").read_text(encoding="utf-8")
        action.trigger()                                          # on again
        assert window.project.use_board_fields and rp.read_only and "VCC" not in window.results
        assert "KiCad" in net_item("VCC").text()
        window.dirty = False
        window.open_project(TMP / "off.coppernicus.yml")
        assert not window.use_fields_action.isChecked() and window.merged.origins == {}
    ''', tmp_path)


def test_reload_board_rereads_the_fields_keeping_the_project_and_save_as_merges_again(tmp_path):
    run_in_window('''
        project = strip(TMP / "a")
        open_net(project)
        assert solve() is not None, DIALOGS
        rp.max_via.setText("2")                                   # an unsaved edit
        rp.max_via.editingFinished.emit()
        changed = dict(FIELDS, U1=dict(FIELDS["U1"], PI_LOAD="500mA"))
        write_strip_board(TMP / "a" / "strip.kicad_pcb", changed)
        assert window.reload_action.shortcut().toString() == "Ctrl+Shift+R"
        assert window.reload_action in window.file_menu.actions()
        window.reload_action.trigger()
        assert window.results == {} and window.dirty and window.net == "VCC"
        assert window.project.rails == [Rail("VCC", limits=Limits(max_via_current=2.0))], window.project.rails
        assert close(window.merged.project.rail("VCC").loads[0].current, 0.5)
        assert rp.loads.rows() == [("", "U1", ["1"], 0.5)], rp.loads.rows()

        # Save As merges again: the merged project has the new path.
        open_net(project)
        moved = TMP / "elsewhere" / "moved.coppernicus.yml"
        moved.parent.mkdir()
        window._save(moved)
        assert window.merged.project.path == window.project.path == moved, window.merged.project.path
        board = window.merged.project.board
        assert board == window.project.board == "../a/strip.kicad_pcb", (board, window.project.board)
    ''', tmp_path)
