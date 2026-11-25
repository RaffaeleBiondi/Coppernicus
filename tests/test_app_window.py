"""The main window itself, offscreen: what only a window can show.

Each check runs in a child process with ``QT_QPA_PLATFORM=offscreen``: a Qt or VTK abort
there fails that test instead of ending the whole run, a hang is cut by the timeout, and
nothing of Qt's state or environment leaks into the other tests."""

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

PRELUDE = '''
from PySide6 import QtCore, QtWidgets


class RunningWorker(QtCore.QObject):
    """A solve still running when the window closes: its signals, nothing else."""
    message = QtCore.Signal(str)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    finished = QtCore.Signal()

    def isRunning(self):
        return True

    def cancel(self):
        pass

    def wait(self, _ms=None):
        return True


from coppernicus.app.main_window import MainWindow

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MainWindow()
window.dirty = False
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_closing_during_a_tree_solve_leaves_none_of_its_signals_connected():
    run_in_window('''
        worker = window.tree_worker = RunningWorker()
        heard = []
        for signal in (worker.message, worker.succeeded, worker.failed):
            signal.connect(heard.append)
        window.close()
        app.processEvents()
        worker.message.emit("meshing")           # a late progress line from the solve thread
        worker.succeeded.emit(None)
        worker.failed.emit("cancelled")
        assert heard == [], heard
    ''')


def test_closing_during_a_rail_solve_leaves_none_of_its_signals_connected():
    """Cancelling a rail solve makes it report "Solve cancelled": that must not open a
    "Cannot solve" box, or draw into the closed view, after the window has gone."""
    run_in_window('''
        worker = window.worker = RunningWorker()
        heard = []
        for signal in (worker.message, worker.succeeded, worker.failed):
            signal.connect(heard.append)
        worker.finished.connect(lambda: heard.append("finished"))
        window.close()
        app.processEvents()
        worker.message.emit("meshing")
        worker.succeeded.emit(None)
        worker.failed.emit("Solve cancelled")
        worker.finished.emit()
        assert heard == [], heard
    ''')


def test_the_compare_tab_labels_the_largest_drop_with_its_side():
    run_in_window('''
        from coppernicus.app.comparing import Run, compare_runs
        from coppernicus.app.panels import ComparePanel
        from coppernicus.engine import Board, solve_rail
        from coppernicus.engine.synthetic import Strip, strip_board
        from coppernicus.project import Load, Rail, Settings, Source

        board = Board(strip_board())
        rail = Rail(Strip.NET, sources=[Source("J1", -1.0)], loads=[Load("U1", 1.0)])
        one, two = solve_rail(board, rail), solve_rail(board, rail)
        panel = ComparePanel()
        cmp_ = compare_runs(Run(one, rail, Settings(), "s"), Run(two, rail, Settings(), "s"))
        panel.show_comparison(cmp_, "")
        labels = [row[0] for row in panel.rows()]
        assert "Largest drop on the rail, mV above -1 V (toward 0 V)" in labels, labels
        assert "U1: drop at its worst pin (mV)" in labels, labels
    ''')


def test_a_rail_solve_that_finishes_after_the_setup_changed_is_dropped():
    """A rail solve started before a settings change or a rail edit ran on the old setup: the
    window drops its result with a status line, as it drops such a power tree. A solve with
    no change while it ran is kept."""
    run_in_window('''
        from pathlib import Path

        import coppernicus
        import coppernicus.app.main_window as mw

        for kind in ("critical", "warning", "information", "question"):     # never block
            setattr(QtWidgets.QMessageBox, kind,
                    staticmethod(lambda *_a, **_k: QtWidgets.QMessageBox.Discard))

        class HeldWorker(mw.SolveWorker):
            """The real rail solve, run when the test says so."""
            def start(self):
                pass

        mw.SolveWorker = HeldWorker
        root = Path(coppernicus.__file__).resolve().parents[2]
        window.open_project(root / "examples" / "demo" / "demo.coppernicus.yml")
        window.nets.select("+3V3")
        app.processEvents()

        def solve_then(change):
            window.solve()
            worker = window.worker
            change()                          # while it runs, on the setup it copied
            worker.run()
            app.processEvents()
            return window.statusBar().currentMessage()

        msg = solve_then(lambda: None)
        assert "+3V3" in window.results and "+3V3 solved in" in msg, msg
        temp = window.rail_panel.temp
        msg = solve_then(lambda: temp.setValue(temp.value() + 40.0))
        assert "+3V3" not in window.results and "+3V3" not in window.runs, msg
        assert msg == "The setup changed while +3V3 was solving: press Solve (F5) again", msg
        assert "press Solve (F5) again" in window.problems.summary.text()
        msg = solve_then(lambda: window.rail_panel.loads.table.cellWidget(0, 3).setValue(0.75))
        assert "+3V3" not in window.results and "changed while +3V3 was solving" in msg, msg
        msg = solve_then(lambda: None)
        assert "+3V3" in window.results and "+3V3 solved in" in msg, msg
    ''')
