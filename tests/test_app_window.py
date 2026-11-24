"""The main window itself, offscreen: what only a window can show."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


class _RunningTreeWorker(QtCore.QObject):
    """A power-tree solve still running when the window closes: its signals, nothing else."""
    message = QtCore.Signal(str)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def isRunning(self):
        return True

    def cancel(self):
        pass

    def wait(self, _ms=None):
        return True


def test_closing_during_a_tree_solve_leaves_none_of_its_signals_connected():
    from coppernicus.app.main_window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow()
    worker = window.tree_worker = _RunningTreeWorker()
    heard = []
    for signal in (worker.message, worker.succeeded, worker.failed):
        signal.connect(heard.append)
    window.dirty = False
    window.close()
    app.processEvents()
    worker.message.emit("meshing")           # a late progress line from the solve thread
    worker.succeeded.emit(None)
    worker.failed.emit("cancelled")
    assert heard == []
