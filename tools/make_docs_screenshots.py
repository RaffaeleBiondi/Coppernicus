"""Screenshots for the README and the docs, made from the synthetic demo board only.

    uv run python tools/make_docs_screenshots.py              # writes docs/images/guide-*.png
    uv run python tools/make_docs_screenshots.py --out DIR    # somewhere else, e.g. to review

It opens a real window, which closes itself, and drives the app from code: it picks pads on
the bare demo board, opens the demo project's rail setup, solves, and shows the maps, a probe
readout and the problems. It never saves a project and takes no board argument: only
``examples/demo`` is used, so no real board can end up in the docs.

Numbers are formatted with the C locale (dot decimals), whatever the machine's locale. Images
are scaled down to at most ``--width`` pixels so the repository stays small. Regenerate them
only when the UI changes, since every PNG stays in the git history.
"""

from __future__ import annotations

import argparse
import faulthandler
import multiprocessing
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
DEMO_BOARD = ROOT / "examples" / "demo" / "demo.kipi.json.gz"
NET = "+3V3"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"), help="output folder")
    ap.add_argument("--width", type=int, default=1600, help="largest image width, px")
    ap.add_argument("--timeout", type=float, default=180.0, help="hard limit for the whole run, s")
    args = ap.parse_args(argv)
    faulthandler.dump_traceback_later(args.timeout, exit=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtCore import QLocale, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    QLocale.setDefault(QLocale.c())
    from coppernicus.app import ICON
    from coppernicus.app.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Coppernicus")
    app.setWindowIcon(QIcon(str(ICON)))
    windows = []
    written = []

    def new_window():
        w = MainWindow()
        w.resize(1500, 920)
        w.show()
        windows.append(w)
        settle()
        return w

    def settle(n: int = 6):
        for _ in range(n):
            app.processEvents()

    def save(widget, name: str):
        settle()
        pix = widget.grab()
        if pix.width() > args.width:
            pix = pix.scaledToWidth(args.width, Qt.SmoothTransformation)
        path = out / f"guide-{name}.png"
        pix.save(str(path))
        written.append(path)

    try:
        # 1. The bare board (no project yet): pick the source and a load by clicking pads.
        #    The pads are on F.Cu; candidate pads are outlined while picking.
        window = new_window()
        if not window.open_board(DEMO_BOARD):
            print("could not open the demo board", file=sys.stderr)
            return 1
        window.nets.select(NET)
        window.layer_box.setCurrentIndex(max(0, window.layer_box.findText("F.Cu")))
        settle()
        window.view.reset_view()
        window.rail_panel.set_pick("source")
        window.pick_pad(6.0, 20.0)                                   # J1
        window.rail_panel.set_pick("load")
        msg = window.pick_pad(50.0, 10.0)                            # U1
        window.statusBar().showMessage(f"{msg} · click more pads, Esc to stop")
        save(window, "1-pick")
        window.rail_panel.set_pick("")
        window.dirty = False
        window.close()

        # 2. The demo project, in a fresh window: voltages, currents and an optional limit
        #    as typed in the rail setup.
        window = new_window()
        if not window.open_project(DEMO):
            print("could not open the demo project", file=sys.stderr)
            return 1
        window.nets.select(NET)
        settle()
        save(window.rail_panel, "2-setup")

        # 3. Solve, then the voltage-drop map on the busiest layer.
        window.solve()
        if window.worker is not None:
            window.worker.wait(int(args.timeout * 1000))
        settle(10)
        if window.results.get(NET) is None:
            print("the demo did not solve", file=sys.stderr)
            return 1
        #    Isolines (equal drop) and arrows (current direction) on.
        window.field_box.setCurrentIndex(window.field_box.findData("drop"))
        window.show_isolines.setChecked(True)
        window.show_arrows.setChecked(True)
        window.view.reset_view()
        window.statusBar().showMessage("Solved: the colours show how many mV each point is below the source")
        save(window, "3-drop")

        # 4. Current density with the arrows, and a probe readout as after a double-click.
        window.show_isolines.setChecked(False)
        window.field_box.setCurrentIndex(window.field_box.findData("current"))
        window.view.reset_view()
        settle()
        window.view._on_double_click((29.5, -32.0, 0.0))      # just below the slot, where current crowds
        save(window, "4-current")

        # 5. The problems panel on its own.
        save(window.problems, "5-problems")

        # 6. ΔV between two points: the source pad and U1's pad, on F.Cu where the pads are.
        window.show_arrows.setChecked(False)
        window.field_box.setCurrentIndex(window.field_box.findData("drop"))
        window.layer_box.setCurrentIndex(max(0, window.layer_box.findText("F.Cu")))
        settle()
        window.view.reset_view()
        window.measure.setChecked(True)
        window.view.measure_at(6.0, 20.0)                            # A: J1
        window.view.measure_at(50.0, 10.0)                           # B: U1
        save(window, "6-measure")
        window.measure.setChecked(False)
    finally:
        for w in windows:
            w.dirty = False                  # never ask to save
            w.close()
        settle()

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
