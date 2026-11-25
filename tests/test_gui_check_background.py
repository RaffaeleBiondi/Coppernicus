"""tools/gui_check.py --window never takes the focus: its windows open without activating,
behind the other applications' windows, and on macOS Qt never makes the process a foreground
app. Only the scripted check does this: the app itself, started by its user, still comes to
the front."""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "gui_check.py"
QT_SWITCH = "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"


def _tool():
    spec = importlib.util.spec_from_file_location("gui_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_a_window_run_on_macos_asks_qt_to_stay_in_the_background():
    env = _tool().background_env
    assert env(window=True, platform="darwin") == {QT_SWITCH: "1"}
    assert env(window=False, platform="darwin") == {}           # offscreen: no window at all
    assert env(window=True, platform="linux") == {}
    assert env(window=True, platform="win32") == {}


def test_the_front_application_is_read_from_lsappinfo():
    parse = _tool().parse_front_app
    text = ('"KiCad" ASN:0x0-0x548548: (in front) \n    bundleID=[ NULL ] \n'
            '    pid = 58254 !cgsConnection !signalled type=[ NULL ]\n')
    assert parse(text) == ("KiCad", 58254)
    assert parse("") is None
    assert parse('"KiCad" ASN:0x0-0x548548: (in front)') is None             # no pid


def test_the_app_start_up_never_keeps_itself_in_the_background():
    """The app the user starts keeps Qt's default: a foreground process that comes to the
    front. Nothing under src/ asks otherwise, and importing its start-up sets nothing."""
    words = (QT_SWITCH, "setActivationPolicy", "ActivationPolicyAccessory", "ActivationPolicyProhibited",
             "WA_ShowWithoutActivating", "_q_showWithoutActivating")
    found = [f"{path.relative_to(ROOT)}: {word}" for path in (ROOT / "src").rglob("*.py")
             for word in words if word in path.read_text(encoding="utf-8")]
    assert found == []
    env = {k: v for k, v in os.environ.items() if k != QT_SWITCH}
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(f'''
        import os
        import coppernicus.app, coppernicus.cli
        print(os.environ.get("{QT_SWITCH}"))
    ''')], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "None"


def test_a_window_run_shows_its_windows_without_activating():
    """Child process, offscreen (the pattern of tests/test_app_window.py): the main window, a
    dialog, and a widget whose native window exists before it is shown (Qt copies the
    attribute to its platform window only when it creates one) all open without activating;
    a child widget is left alone."""
    pytest.importorskip("PySide6")
    body = textwrap.dedent(f'''
        import importlib.util
        spec = importlib.util.spec_from_file_location("gui_check", {str(TOOL)!r})
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QDialog, QWidget
        from coppernicus.app.main_window import MainWindow
        app = QApplication.instance() or QApplication([])
        tool.show_in_background(app)
        window = MainWindow()
        window.dirty = False
        early = QWidget()
        early.winId()                                  # its native window exists already
        dialog = QDialog(window)
        child = QWidget(window)
        for widget in (window, early, dialog):
            widget.show()
        app.processEvents()
        for widget in (window, early, dialog):
            assert widget.testAttribute(Qt.WA_ShowWithoutActivating), widget
            assert widget.windowHandle().property("_q_showWithoutActivating") is True, widget
        assert not child.testAttribute(Qt.WA_ShowWithoutActivating)
        for widget in (dialog, early, window):
            widget.close()
        app.processEvents()
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr
