"""The short names of the app's main actions: Solve rail (F5), Solve tree (Ctrl+T), Pin baseline
(Ctrl+B), and Follow net ties with its long text in the tooltip. Offscreen, in a child process
as in test_app_window."""

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

PRELUDE = '''
from PySide6 import QtGui, QtWidgets

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


def test_the_main_actions_have_short_names_and_keep_their_shortcuts():
    run_in_window('''
        actions = {a.text(): a.shortcut().toString() for a in window.findChildren(QtGui.QAction) if a.text()}
        assert (actions["Solve rail"], actions["Solve tree"], actions["Pin baseline"]) == (
            "F5", "Ctrl+T", "Ctrl+B"), actions
        assert not {"Solve", "Solve power tree", "Pin as baseline"} & set(actions), actions
        assert window.rail_panel.solve.text() == "Solve rail  (F5)"
        assert window.compare.pin.text() == "Pin baseline"
        assert window.tree_panel.summary.text().startswith("<i>Solve tree (Ctrl+T)")
    ''')


def test_the_net_tie_option_is_short_with_its_long_text_in_the_tooltip():
    run_in_window('''
        tie = window.rail_panel.follow_ties
        assert tie.text() == "Follow net ties", tie.text()
        assert tie.toolTip() == "Solve nets joined by net ties as one (tie = short)", tie.toolTip()
    ''')
