"""What the packaged app needs from the window: Help → Remove downloaded mesher (Triangle)…
in the frozen app only.

Window checks run in a child process, offscreen (the pattern of tests/test_app_window.py)."""

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

from coppernicus.app import main_window as mw  # noqa: E402

ITEM = "Remove downloaded mesher (Triangle)…"

PRELUDE = '''
import sys
from pathlib import Path

from PySide6 import QtWidgets
from PySide6.QtWidgets import QMessageBox

DIALOGS, ANSWERS = [], {}          # (kind, title, text) of every message box; what each returns


def _box(kind):
    def show(_parent, title, text, *_a, **_k):
        DIALOGS.append((kind, title, text))
        return ANSWERS.get(kind, QMessageBox.Discard)
    return staticmethod(show)


for _kind in ("critical", "warning", "information", "question"):
    setattr(QMessageBox, _kind, _box(_kind))

from coppernicus import bootstrap_triangle
from coppernicus.app.main_window import MainWindow

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def items(window):
    return {a.text(): a for m in window.menuBar().findChildren(QtWidgets.QMenu) for a in m.actions()}
'''


def run_in_window(body: str, env=None) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", **(env or {}))
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_remove_mesher_item_is_offered_only_in_the_packaged_app(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert mw.remove_mesher_shown() is False           # triangle comes from the environment
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert mw.remove_mesher_shown() is True


def test_help_remove_mesher_asks_first_then_says_the_next_start_downloads_it_again(tmp_path):
    run_in_window(f'''
        ITEM = {ITEM!r}
        plain = MainWindow()
        plain.dirty = False
        assert ITEM not in items(plain), sorted(items(plain))      # not the frozen app

        sys.frozen = True
        window = MainWindow()
        window.dirty = False
        removed = []

        def remove(site):
            removed.append(site)
            return 12_300_000
        bootstrap_triangle.remove = remove

        ANSWERS["question"] = QMessageBox.Cancel
        items(window)[ITEM].trigger()
        assert removed == [] and DIALOGS[-1][0] == "question", DIALOGS

        ANSWERS["question"] = QMessageBox.Yes
        items(window)[ITEM].trigger()
        site = bootstrap_triangle.site_dir()
        assert removed == [site], removed
        kind, _title, text = DIALOGS[-1]
        assert kind == "information" and "12.3 MB" in text and str(site) in text, DIALOGS[-1]
        assert "downloads it again on its next start" in text, text
        assert "next start" in window.statusBar().currentMessage(), window.statusBar().currentMessage()

        bootstrap_triangle.remove = lambda site: 0
        items(window)[ITEM].trigger()
        assert DIALOGS[-1][0] == "information" and "Nothing to remove" in DIALOGS[-1][2], DIALOGS[-1]

        def busy(site):
            raise bootstrap_triangle.BootstrapError("cannot delete the downloaded Triangle in X: busy",
                                                    "close every Coppernicus window, then try again")
        bootstrap_triangle.remove = busy
        items(window)[ITEM].trigger()
        kind, _title, text = DIALOGS[-1]
        assert kind == "warning" and "busy" in text and "close every Coppernicus window" in text, DIALOGS[-1]
    ''', env={"COPPERNICUS_SITE_PACKAGES": str(tmp_path / "site-packages")})

