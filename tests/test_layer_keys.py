"""The + / − keys of the app, as in KiCad: + (also = and the keypad's +) shows the next copper
layer in the Layer selector, − (also the keypad's −) the previous one, round from the last to
the first; while the focus is where keys are typed (a text field, a spin box, an editable
table cell) the key goes there and the layer stays. Offscreen, in a child process as in
test_app_window."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo" / "demo.coppernicus.yml"

PRELUDE = '''
from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from coppernicus.app.main_window import LAYER_TIP, MainWindow, typing_widget

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MainWindow()
window.dirty = False
window.show()
app.processEvents()


def press(widget, key, modifiers=Qt.NoModifier):
    """Focus ``widget`` and press ``key`` in it, as a user would."""
    window.activateWindow()
    widget.setFocus()
    app.processEvents()
    assert app.focusWidget() is widget, app.focusWidget()
    QTest.keyClick(widget, key, modifiers)
    app.processEvents()


def layer():
    return window.layer_box.currentText()
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_plus_and_minus_step_through_the_layers_round_the_ends():
    run_in_window('''
        box = window.layer_box
        press(box, Qt.Key_Plus)                       # no layer yet: nothing to do
        assert box.currentIndex() == -1
        box.addItems(["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        box.setCurrentIndex(0)
        seen = []
        box.currentIndexChanged.connect(lambda i: seen.append(box.itemText(i)))
        press(box, Qt.Key_Plus)
        press(box, Qt.Key_Equal)                      # "+" without Shift
        press(box, Qt.Key_Plus, Qt.KeypadModifier)
        assert layer() == "B.Cu" and seen == ["In1.Cu", "In2.Cu", "B.Cu"], seen
        press(box, Qt.Key_Plus)                       # round the end, as KiCad does
        assert layer() == "F.Cu", layer()
        press(box, Qt.Key_Minus)
        assert layer() == "B.Cu", layer()
        press(box, Qt.Key_Minus, Qt.KeypadModifier)
        assert layer() == "In2.Cu", layer()
        assert "+" in LAYER_TIP and "−" in LAYER_TIP and box.toolTip() == LAYER_TIP
    ''')


def test_the_keys_are_typed_into_text_fields_spin_boxes_and_table_cells():
    run_in_window(f'''
        from pathlib import Path
        window.open_project(Path({str(DEMO)!r}))
        app.processEvents()
        window.nets.select("+3V3")
        app.processEvents()
        box, panel = window.layer_box, window.rail_panel
        first = layer()
        panel.max_drop.setValidator(None)             # the field takes any text here
        panel.max_drop.clear()
        for key in (Qt.Key_Plus, Qt.Key_Minus, Qt.Key_Equal):
            press(panel.max_drop, key)
        assert panel.max_drop.text() == "+-=" and layer() == first, (panel.max_drop.text(), layer())
        assert typing_widget(panel.max_edge)          # a spin box
        press(panel.max_edge, Qt.Key_Minus)
        press(panel.max_edge, Qt.Key_Plus, Qt.KeypadModifier)
        assert layer() == first, layer()
        table = panel.loads.table                     # an editable table: the key starts the edit
        table.setCurrentCell(0, 0)
        assert typing_widget(table) and table.item(0, 0).flags() & Qt.ItemIsEditable
        press(table, Qt.Key_Plus)
        editor = app.focusWidget()
        assert isinstance(editor, QtWidgets.QLineEdit) and editor.text() == "+", editor
        assert layer() == first and typing_widget(editor), layer()
        QTest.keyClick(editor, Qt.Key_Escape)        # the edit is dropped
        press(box, Qt.Key_Plus)                       # off the table, the keys step again
        assert layer() != first, layer()
        assert not typing_widget(window.problems.tree) and not typing_widget(None)
    ''')


def test_the_keys_step_the_layer_with_the_board_view_focused():
    """The common case: the pointer was on the board. The view (pyvista binds + and − to the
    point size) never gets the key: the layer steps and the map is drawn on it."""
    run_in_window(f'''
        from pathlib import Path
        window.open_project(Path({str(DEMO)!r}))
        app.processEvents()
        window.nets.select("+3V3")
        app.processEvents()
        names = [window.layer_box.itemText(i) for i in range(window.layer_box.count())]
        assert len(names) >= 2, names
        first = layer()
        view = window.view.plotter.interactor
        view.setFocusPolicy(Qt.StrongFocus)
        drawn = []
        real = window.refresh_view
        window.refresh_view = lambda *a, **k: (drawn.append(layer()), real(*a, **k))
        press(view, Qt.Key_Plus)
        second = names[(names.index(first) + 1) % len(names)]
        assert layer() == second and drawn == [second], (first, layer(), drawn)
        press(view, Qt.Key_Minus)
        assert layer() == first, layer()
    ''')
