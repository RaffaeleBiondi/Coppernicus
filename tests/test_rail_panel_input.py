"""Typing and the mouse wheel in the Rail setup panel: a limit cleared from the keyboard means
"no limit" and reaches the project (a comma still reads as the decimal sign), and turning
the wheel over a spin box that does not have the focus scrolls the panel instead of
changing the value.

Child process, offscreen (the pattern of tests/test_app_window.py)."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"

PRELUDE = '''
import shutil
from pathlib import Path

import yaml
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

for _kind in ("critical", "warning", "information", "question"):            # never block
    setattr(QMessageBox, _kind, staticmethod(lambda *_a, **_k: QMessageBox.Discard))

from coppernicus.app.main_window import MainWindow

app = QApplication.instance() or QApplication([])
window = MainWindow()
window.dirty = False
rp = window.rail_panel
'''


def run_in_window(body: str, tmp_path: Path) -> None:
    setup = textwrap.dedent(f'''
        TMP = Path({str(tmp_path)!r})
        for name in ("demo.coppernicus.yml", "demo.kipi.json.gz"):
            shutil.copy(Path({str(DEMO)!r}) / name, TMP / name)
        PROJECT = TMP / "demo.coppernicus.yml"
        window.open_project(PROJECT)
        window.nets.select("+3V3")
        app.processEvents()
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + setup + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_a_limit_cleared_from_the_keyboard_means_no_limit_and_is_saved_so(tmp_path):
    run_in_window('''
        def limits():
            return window.project.rail("+3V3").limits

        def clear(edit):
            QTest.keyClick(edit, Qt.Key_End)
            for _ in range(len(edit.text())):
                QTest.keyClick(edit, Qt.Key_Backspace)
            assert edit.text() == ""

        assert limits().max_drop_pct == 0.25
        clear(rp.max_drop)
        QTest.keyClick(rp.max_drop, Qt.Key_Return)                      # Return
        assert limits().max_drop_pct is None, limits()

        QTest.keyClicks(rp.max_j, "2,5")                                # the comma still works
        QTest.keyClick(rp.max_j, Qt.Key_Return)
        assert limits().max_current_density == 2.5, limits()
        clear(rp.max_j)
        QApplication.sendEvent(rp.max_j, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))   # focus away
        assert limits().max_current_density is None, limits()

        window._save(PROJECT)
        saved = yaml.safe_load(PROJECT.read_text(encoding="utf-8"))["rails"][0]
        assert not saved.get("limits"), saved
    ''', tmp_path)


def test_the_wheel_over_an_unfocused_spin_box_changes_nothing_and_goes_on_to_the_panel(tmp_path):
    """A wheel event sent from code reaches only its target (Qt propagates only real ones), so
    each spin box gets it directly: it must pass it on (ignore it), and a wheel turn must never
    focus it first, as Qt's default WheelFocus policy would."""
    run_in_window('''
        window.show()
        app.processEvents()

        def wheel(spin, notches):
            """True when the spin box took the wheel; False: it goes on to the scroll area."""
            at = QPointF(spin.width() / 2, spin.height() / 2)
            event = QWheelEvent(at, QPointF(spin.mapToGlobal(at.toPoint())), QPoint(),
                                QPoint(0, 120 * notches), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(spin, event)
            app.processEvents()
            return event.isAccepted()

        def wheel_focus(widget):
            return widget.focusPolicy().value & Qt.WheelFocus.value == Qt.WheelFocus.value

        settings, rail = window.project.settings, window.project.rail("+3V3")
        spins = {"mesh edge": rp.max_edge, "temperature": rp.temp,
                 "source voltage": rp.sources.table.cellWidget(0, 3),
                 "load current": rp.loads.table.cellWidget(0, 3)}
        before = {name: spin.value() for name, spin in spins.items()}
        currents = [ld.current for ld in rail.loads]
        for name, spin in spins.items():
            assert not spin.hasFocus(), name
            assert not wheel_focus(spin) and not wheel_focus(spin.lineEdit()), f"{name}: the wheel focuses it"
            assert not wheel(spin, -1), f"{name}: took the wheel instead of passing it on"
        assert {name: spin.value() for name, spin in spins.items()} == before
        assert (settings.max_edge_mm, settings.copper_temp_c) == (0.5, 20.0), settings
        assert [ld.current for ld in rail.loads] == currents

        window.activateWindow()
        rp.temp.setFocus()
        app.processEvents()
        assert rp.temp.hasFocus()
        assert wheel(rp.temp, 1)                                         # focused: the wheel steps it
        assert rp.temp.value() == 21.0 and settings.copper_temp_c == 21.0, rp.temp.value()
    ''', tmp_path)
