"""The desktop app reads the KiCad footprint fields as the CLI does: an offscreen MainWindow on
the synthetic strip .kicad_pcb of tests/kicad_fixture.py. The window's project stays the
user's (the only thing saved); what it solves, draws and judges is the merged project."""

import json
import os

import pytest
from kicad_fixture import write_strip_board

from coppernicus.cli import EXIT_LIMIT, main
from coppernicus.project import Load, Project, Rail, Source

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
