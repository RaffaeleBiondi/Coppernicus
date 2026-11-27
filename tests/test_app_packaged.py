"""What the packaged app needs from the window: Help → Remove downloaded mesher (Triangle)…
in the frozen app only, and reports that go to a per-user folder when results/ next to the
project cannot be written (an example opened from inside the read-only disk image).

Window checks run in a child process, offscreen (the pattern of tests/test_app_window.py)."""

import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6")

from coppernicus.app import main_window as mw  # noqa: E402

ITEM = "Remove downloaded mesher (Triangle)…"
needs_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX folder permissions that apply to this user")

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

def test_help_remove_mesher_asks_first_then_says_when_it_is_downloaded_again(tmp_path):
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
        assert "downloads it again only when the triangle mesher is chosen" in text, text
        status = window.statusBar().currentMessage()
        assert "only when the triangle mesher is chosen" in status, status

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

def test_help_remove_mesher_in_use_says_the_next_start_removes_it_not_that_it_failed(tmp_path):
    """A mesher in use (mostly on Windows, where the running app has it loaded) cannot be
    deleted now: remove() defers it to the next start (RemovalDeferred). That is not a failure:
    an information box says so, not a "Cannot remove" warning."""
    run_in_window(f'''
        sys.frozen = True
        window = MainWindow()
        window.dirty = False

        def in_use(site):
            raise bootstrap_triangle.RemovalDeferred(
                "Triangle will be removed the next time Coppernicus starts (the running app has it "
                "loaded)", "close every Coppernicus window: the next start deletes it before using it, "
                "then downloads it again")
        bootstrap_triangle.remove = in_use
        ANSWERS["question"] = QMessageBox.Yes
        items(window)[{ITEM!r}].trigger()
        assert [d[0] for d in DIALOGS] == ["question", "information"], DIALOGS
        _kind, title, text = DIALOGS[-1]
        assert "in use" in text and "removes it at its next start" in text, text
        assert str(bootstrap_triangle.site_dir()) in text, text
        status = window.statusBar().currentMessage()
        assert "in use" in status and "next start" in status, status
    ''', env={"COPPERNICUS_SITE_PACKAGES": str(tmp_path / "site-packages")})

@needs_posix_permissions
def test_reports_go_to_a_per_user_folder_when_results_next_to_the_project_cannot_be_written(tmp_path):
    project_dir, home = tmp_path / "volume", tmp_path / "home"
    project_dir.mkdir()
    user = home / "Documents" / "Coppernicus" / "results"
    os.chmod(project_dir, 0o555)                       # results/ cannot be created
    try:
        folder, note = mw.report_folder(project_dir, home)
        assert folder == user and folder.is_dir()      # created on demand
        assert str(project_dir / "results") in note and str(user) in note, note
        assert not (project_dir / "results").exists()
    finally:
        os.chmod(project_dir, 0o755)
    folder, note = mw.report_folder(project_dir, home)
    assert (folder, note) == (project_dir / "results", "") and folder.is_dir()
    os.chmod(folder, 0o555)                            # results/ is there but read-only
    try:
        assert mw.report_folder(project_dir, home)[0] == user
    finally:
        os.chmod(folder, 0o755)

@needs_posix_permissions
def test_the_window_says_where_the_report_went_when_it_falls_back(tmp_path):
    project_dir, home = tmp_path / "volume", tmp_path / "home"
    project_dir.mkdir()
    os.chmod(project_dir, 0o555)
    try:
        run_in_window(f'''
            window = MainWindow()
            window.dirty = False
            window.project.path = Path({str(project_dir / "demo.coppernicus.yml")!r})
            start = window._report_start_path()
            user = Path({str(home)!r}) / "Documents" / "Coppernicus" / "results"
            assert start == user / "demo_report.html", start
            start.write_text("<html></html>")
            window._report_written(start)
            kind, _title, text = DIALOGS[-1]
            assert kind == "question" and str(start) in text and "cannot be written" in text, text
            status = window.statusBar().currentMessage()
            assert "cannot be written" in status and str(user) in status, status

            elsewhere = Path({str(tmp_path / "chosen.html")!r})     # the user picked another folder
            elsewhere.write_text("<html></html>")
            window._report_written(elsewhere)
            assert "cannot be written" not in DIALOGS[-1][2], DIALOGS[-1]
        ''', env={"HOME": str(home)})
    finally:
        os.chmod(project_dir, 0o755)
