"""Export report in the app writes the power tree's page too, when the tree is solved: the page
`coppernicus solve --tree --html` writes, with the CLI's suffix (``<name>_tree_report.html``,
next to the rails' page; alone when only the tree is solved). The window checks run offscreen
in a child process, as in test_app_window."""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from coppernicus.app.main_window import tree_report_path  # noqa: E402
from coppernicus.cli import EXIT_OK, main  # noqa: E402

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"

PRELUDE = '''
from pathlib import Path

from PySide6 import QtWidgets
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox

from coppernicus.app.main_window import MainWindow

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
TARGET, CHOSEN, OPENED, ASKED = [None], [], [], []


def save(*args, **kwargs):
    CHOSEN.append(args[2])                      # where the Save dialog starts
    return str(TARGET[0]), ""


QFileDialog.getSaveFileName = staticmethod(save)
QDesktopServices.openUrl = staticmethod(lambda url: OPENED.append(url.toLocalFile()) or True)
QMessageBox.question = staticmethod(lambda *a, **k: ASKED.append(a[2]) or QMessageBox.Open)
WARNED = []
QMessageBox.warning = staticmethod(lambda *a, **k: WARNED.append(a[2]))
window = MainWindow()
window.dirty = False


def settle(worker):
    if worker is not None:
        worker.wait(170000)
    app.processEvents()
    app.processEvents()
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stdout + out.stderr


def meta(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    return json.loads(re.search(r'id="meta">(.*?)</script>', text, re.S).group(1))


def test_the_tree_page_is_named_after_the_rails_page_as_the_cli_names_it():
    assert tree_report_path(Path("r/demo_report.html")) == Path("r/demo_tree_report.html")
    assert tree_report_path(Path("r/rev2.html")) == Path("r/rev2_tree_report.html")
    assert tree_report_path(Path("r/x_tree_report.html")) != Path("r/x_tree_report.html")   # never itself


def test_the_worker_writes_a_board_temperature_tree_page_in_its_thread(tmp_path):
    """An electro-thermal tree: its page carries the board temperature, whose maps are drawn
    inside the report's thread."""
    demo = CHAIN.parent / "demo" / "demo-thermal.coppernicus.yml"
    page = tmp_path / "demo-thermal_tree_report.html"
    run_in_window(f'''
        from coppernicus.app.worker import ReportWorker
        from coppernicus.engine import Board
        from coppernicus.engine.tree import solve_project_tree
        from coppernicus.power_tree import tree_summary
        from coppernicus.project import Project
        project = Project.load({str(demo)!r})
        board = Board.open(project.board_path())
        tree = solve_project_tree(board, project)
        assert tree.thermal is not None
        done, failed = [], []
        worker = ReportWorker([], project, None, board.outline(), board.name,
                              tree=(tree, tree_summary(tree), Path({str(page)!r}),
                                    "demo-thermal.coppernicus.yml", dict(vars(project.settings))))
        worker.succeeded.connect(done.append)
        worker.failed.connect(failed.append)
        worker.start()
        settle(worker)
        assert not failed and done == [Path({str(page)!r})], (done, failed)
    ''')
    thermal = meta(page)["tree"]["thermal"]
    assert thermal["maps"] and all(m["png"].startswith("data:image/png;base64,") for m in thermal["maps"])


def test_export_report_writes_the_tree_page_alone_then_beside_the_rails(tmp_path):
    shutil.copytree(CHAIN, tmp_path / "c")
    project = tmp_path / "c" / "power_chain.coppernicus.yml"
    alone, rails = tmp_path / "out" / "alone.html", tmp_path / "out" / "chain_report.html"
    run_in_window(f'''
        window.open_project(Path({str(project)!r}))
        app.processEvents()
        window.export_report()                       # nothing solved: refused, no dialog
        assert not CHOSEN and "Solve" in window.statusBar().currentMessage()
        window.solve_tree()
        window.export_report()                       # the tree still solving: refused
        assert not CHOSEN and "solve is running" in window.statusBar().currentMessage()
        settle(window.tree_worker)
        assert window.tree is not None and not window.results
        TARGET[0] = Path({str(alone)!r})
        window.export_report()                       # only the tree: its page alone
        start = Path(CHOSEN[-1])
        assert start == Path({str(project.parent)!r}) / "results" / "power_chain_tree_report.html", start
        settle(window.report_worker)
        assert TARGET[0].is_file() and OPENED == [str(TARGET[0])], OPENED
        assert "alone.html" in window.statusBar().currentMessage()

        window.nets.select("+3V3")                  # a rail too: its page, and the tree's beside it
        app.processEvents()
        window.solve()
        settle(window.worker)
        assert "+3V3" in window.results
        TARGET[0] = Path({str(rails)!r})
        window.export_report()
        assert Path(CHOSEN[-1]).name == "power_chain_report.html", CHOSEN
        settle(window.report_worker)
        tree = TARGET[0].with_name("chain_tree_report.html")
        assert TARGET[0].is_file() and tree.is_file()
        assert OPENED[1:] == [str(TARGET[0]), str(tree)], OPENED
        status = window.statusBar().currentMessage()
        assert "chain_report.html" in status and "chain_tree_report.html" in status, status
        assert str(TARGET[0]) in ASKED[-1] and str(tree) in ASKED[-1], ASKED[-1]
        assert not WARNED, WARNED
    ''')
    # The tree's page is the page `solve --tree --html` writes (but for the time it was written).
    cli = tmp_path / "cli.html"
    assert main(["solve", str(project), "--tree", "--out", str(tmp_path / "o"),
                 "--html", str(cli)]) == EXIT_OK
    want = {k: v for k, v in meta(cli).items() if k != "generated"}
    for page in (alone, rails.with_name("chain_tree_report.html")):
        got = {k: v for k, v in meta(page).items() if k != "generated"}
        assert got == want
        assert "tree" in got and [v["name"] for v in got["tree"]["vrms"]] == ["J1", "U1", "U2"]
    assert "tree" not in meta(rails) and [r["net"] for r in meta(rails)["rails"]] == ["+3V3"]
