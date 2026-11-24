"""The desktop app (PySide6). Start it with ``coppernicus`` or ``coppernicus gui [path]``."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

os.environ.setdefault("QT_API", "pyside6")

ICON = Path(__file__).resolve().parent / "resources" / "icon.png"


def run(path: Optional[str] = None, screenshot: Optional[str] = None, solve_net: Optional[str] = None,
        layer: Optional[str] = None, field: Optional[str] = None) -> int:
    """Open the main window. ``screenshot`` (with optional ``solve_net``, ``layer``, ``field``)
    is a test hook: open ``path``, solve, save window and view images, then quit."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from coppernicus.app.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Coppernicus")
    app.setWindowIcon(QIcon(str(ICON)))
    window = MainWindow()
    window.show()
    if path:
        QTimer.singleShot(0, lambda: window.open_path(path))
    if screenshot:
        QTimer.singleShot(200, lambda: _screenshot_run(window, Path(screenshot), solve_net, layer, field))
    return app.exec()


def _screenshot_run(window, out: Path, solve_net, layer, field):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def capture():
        if layer:
            i = window.layer_box.findText(layer)
            if i >= 0:
                window.layer_box.setCurrentIndex(i)
        if field:
            window.field_box.setCurrentIndex(max(0, window.field_box.findData(field)))
        QApplication.processEvents()
        window.view.reset_view()
        QTimer.singleShot(800, finish)

    def finish():
        out.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(out))
        window.view.screenshot(out.with_name(out.stem + "_view.png"))
        print(f"screenshot: {out}  last draw {window.last_render_s:.2f} s", flush=True)
        window.dirty = False
        window.close()

    if solve_net:
        window.nets.select(solve_net)
        QApplication.processEvents()
        window.worker = None
        window.solve()
        window.worker.succeeded.connect(lambda *_: QTimer.singleShot(300, capture))
        window.worker.failed.connect(lambda msg: (print("solve failed:", msg, flush=True), window.close()))
    else:
        capture()
