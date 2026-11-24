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
