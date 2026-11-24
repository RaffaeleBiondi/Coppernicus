"""The desktop app (PySide6). Start it with ``coppernicus`` or ``coppernicus gui [path]``."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

os.environ.setdefault("QT_API", "pyside6")

ICON = Path(__file__).resolve().parent / "resources" / "icon.png"
APP_NAME = "Coppernicus"


def set_macos_app_name(name: str = APP_NAME) -> bool:
    """Make the macOS menu bar show ``name`` instead of "Python": set ``CFBundleName`` in the
    main bundle's info dictionary, which must happen before the QApplication exists.

    Uses pyobjc's Foundation, which pyvista already installs on macOS. Purely cosmetic: on
    other systems, or if anything fails, it returns ``False`` and changes nothing."""
    if sys.platform != "darwin":
        return False
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is None:
            return False
        info["CFBundleName"] = name
        return True
    except Exception:                                   # noqa: BLE001 - never block start-up
        return False


def run(path: Optional[str] = None, screenshot: Optional[str] = None, solve_net: Optional[str] = None,
        layer: Optional[str] = None, field: Optional[str] = None) -> int:
    """Open the main window, optionally on ``path``. ``solve_net`` solves that rail at start and
    shows ``field`` ('drop', 'voltage', 'current') on ``layer``. ``screenshot`` is a test hook:
    solve, save window and view images, then quit."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from coppernicus.app.main_window import MainWindow

    if QApplication.instance() is None:
        set_macos_app_name()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(ICON)))
    window = MainWindow()
    window.show()
    if path:
        QTimer.singleShot(0, lambda: window.open_path(path))
    if screenshot:
        QTimer.singleShot(200, lambda: _screenshot_run(window, Path(screenshot), solve_net, layer, field))
    elif solve_net:
        QTimer.singleShot(200, lambda: _auto_solve(window, solve_net, layer, field))
    return app.exec()


def _auto_solve(window, net: str, layer: Optional[str], field: Optional[str]):
    """Select ``net``, solve it, then show ``field`` on ``layer`` (default: the busiest layer)."""
    from PySide6.QtWidgets import QApplication

    def show(_result):
        if field:
            window.field_box.setCurrentIndex(max(0, window.field_box.findData(field)))
        if layer and window.layer_box.findText(layer) >= 0:
            window.layer_box.setCurrentIndex(window.layer_box.findText(layer))

    window.nets.select(net)
    QApplication.processEvents()
    window.solve()
    if window.worker is not None:
        window.worker.succeeded.connect(show)


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
