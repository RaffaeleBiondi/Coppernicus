"""The test suite never touches the user's own app settings: every setting the app keeps goes
through coppernicus.app.settings(), which tests/conftest.py points at a folder of the run
(COPPERNICUS_SETTINGS_DIR), and every child process a test starts inherits that environment."""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coppernicus.app import SETTINGS_DIR_ENV

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "coppernicus" / "app"


def test_the_suite_keeps_the_settings_in_a_folder_of_its_own(app_settings_kept_apart):
    folder = Path(os.environ[SETTINGS_DIR_ENV])
    assert folder == app_settings_kept_apart and folder.is_dir()
    assert not str(folder).startswith(str(Path.home() / "Library" / "Preferences"))


def test_only_the_helper_builds_a_qsettings():
    """A QSettings built anywhere else in the app would bypass COPPERNICUS_SETTINGS_DIR."""
    found = [f"{p.relative_to(ROOT)}:{i}" for p in APP.rglob("*.py")
             for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if "QSettings(" in line and p.name != "__init__.py"]
    assert not found, found


def test_every_window_test_starts_its_children_with_the_suites_environment():
    """A child given an environment of its own (not built from os.environ) would lose
    COPPERNICUS_SETTINGS_DIR and write the user's settings."""
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "MainWindow(" not in text or "subprocess" not in text:
            continue
        for call in re.findall(r"subprocess\.(?:run|Popen)\((.*?)\)\n", text, re.S):
            if "env=" in call:
                assert "env=env" in call or "os.environ" in call, (path.name, call)
        for built in re.findall(r"^\s*env = ((?:dict\(|\{).+)$", text, re.M):     # an environment built
            assert "os.environ" in built, (path.name, built)


def test_a_window_in_a_child_writes_only_into_the_suites_folder():
    pytest.importorskip("PySide6")
    body = textwrap.dedent('''
        from PySide6 import QtWidgets
        from coppernicus.app import settings
        from coppernicus.app.main_window import MainWindow
        app = QtWidgets.QApplication([])
        window = MainWindow()
        window.dirty = False
        window.toggle_theme()
        print(settings().fileName(), flush=True)
    ''')                                  # a normal exit: os._exit with Qt alive crashed on Windows
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    written = Path(out.stdout.strip().splitlines()[-1])
    assert written.parent == Path(os.environ[SETTINGS_DIR_ENV]), written
    assert written.is_file() and "theme" in written.read_text(encoding="utf-8")


def test_the_app_exits_cleanly_after_a_theme_switch():
    """The packaged app's own way out (coppernicus.app.run: the window shown, the theme switched,
    the window closed, app.exec() returns, Python exits normally) ends with code 0 on every
    system: on Windows a crash there would show as 0xC0000005."""
    pytest.importorskip("PySide6")
    body = textwrap.dedent('''
        import sys
        from PySide6 import QtCore, QtWidgets
        from coppernicus.app import run
        from coppernicus.app.main_window import MainWindow

        def act():
            window = next(w for w in QtWidgets.QApplication.topLevelWidgets() if isinstance(w, MainWindow))
            window.dirty = False
            window.toggle_theme()
            window.toggle_theme()
            window.close()

        app = QtWidgets.QApplication(sys.argv[:1])
        QtCore.QTimer.singleShot(300, act)
        sys.exit(run())
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", body], env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, (out.returncode, out.stderr[-2000:])
