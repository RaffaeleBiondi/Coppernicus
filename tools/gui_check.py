"""Scripted check of the desktop app: drives MainWindow from code on the synthetic demo board.

    uv run python tools/gui_check.py --out DIR        # offscreen: no window on screen
    uv run python tools/gui_check.py --window --out DIR   # a real window (closes itself):
                                                      # also simulated mouse clicks
    uv run python tools/gui_check.py --local PROJECT --rail NET --out DIR   # + time a real rail

Offscreen, Qt has no OpenGL context, so the app's own view draws nothing: the check covers
the app logic (pads picked, rows, solve, ΔV, arrow geometry) and renders the view's scene
with a separate off-screen VTK window into ``--out`` (``*_scene.png``). With ``--window`` it
also sends mouse presses and releases to the view. It saves projects only into ``--out``
and never writes next to a board. Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import faulthandler
import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_BOARD = ROOT / "examples" / "demo" / "demo.kipi.json.gz"


class Checks:
    def __init__(self):
        self.failed = 0

    def __call__(self, name: str, ok: bool, detail: str = ""):
        self.failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


DIALOGS = []                                  # (kind, title) of every message box shown


def _quiet_dialogs():
    """Modal boxes would block a scripted run: print and record them instead."""
    from PySide6.QtWidgets import QMessageBox

    def say(kind):
        def box(_parent, title, text, *_a, **_k):
            print(f"{kind}: {title}: {text}", flush=True)
            DIALOGS.append((kind, title))
            return QMessageBox.Discard
        return staticmethod(box)
    for kind in ("critical", "warning", "information", "question"):
        setattr(QMessageBox, kind, say(kind))


def solve_and_wait(window, timeout_s: float):
    from PySide6.QtWidgets import QApplication
    window.solve()
    if window.worker is None:
        return None
    window.worker.wait(int(timeout_s * 1000))
    QApplication.processEvents()          # deliver the worker's queued signals
    QApplication.processEvents()
    return window.results.get(window.net)


def click_view(window, x_mm: float, y_mm: float, drag_px: int = 0):
    """Send a left press and release to the board view at a board point (real window only)."""
    from PySide6.QtWidgets import QApplication
    view = window.view
    ren = view.plotter.renderer
    ren.SetWorldPoint(x_mm, -y_mm, 0.0, 1.0)
    ren.WorldToDisplay()
    dx, dy, _ = ren.GetDisplayPoint()
    iren = view.plotter.iren.interactor
    iren.SetEventPosition(int(round(dx)), int(round(dy)))
    iren.InvokeEvent("LeftButtonPressEvent")
    iren.SetEventPosition(int(round(dx)) + drag_px, int(round(dy)))
    iren.InvokeEvent("LeftButtonReleaseEvent")
    QApplication.processEvents()


def qt_click_view(window, x_mm: float, y_mm: float):
    """A left click sent to the view widget as a Qt mouse event, so it goes through
    pyvistaqt's Qt → VTK translation with the screen's device-pixel ratio (Retina) — the path
    a real mouse takes. Real window only. Returns (dpr, logical x, logical y)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    view = window.view
    ren = view.plotter.renderer
    ren.SetWorldPoint(x_mm, -y_mm, 0.0, 1.0)
    ren.WorldToDisplay()
    dx, dy, _ = ren.GetDisplayPoint()                     # device pixels, origin bottom-left
    widget = view.plotter.interactor
    dpr = widget.devicePixelRatioF()
    height_px = view.plotter.ren_win.GetSize()[1]
    qx, qy = dx / dpr, (height_px - 1 - dy) / dpr
    QTest.mouseClick(widget, Qt.LeftButton, Qt.NoModifier, QPoint(round(qx), round(qy)))
    QApplication.processEvents()
    return dpr, qx, qy


def run_demo(window, check: Checks, args, out: Path):
    from PySide6.QtWidgets import QApplication
    ok = window.open_board(DEMO_BOARD)
    check("open the demo board", ok)
    window.nets.select("+3V3")
    QApplication.processEvents()
    check("select +3V3", window.net == "+3V3")

    # --- Task 1: pick J1 as the source, U1 and U3 as loads, by board coordinates.
    panel = window.rail_panel
    panel.set_pick("source")
    check("pick mode on the view", window.view.click_mode == "pick")
    msg = window.pick_pad(6.0, 20.0)
    check("pick J1.1 as a source", "J1" in msg, msg)
    panel.set_pick("load")
    check("one table picks at a time", panel.pick_kind == "load" and not panel.sources.pick.isChecked())
    for x, y in ((50.0, 10.0), (18.0, 33.0)):
        msg = window.pick_pad(x, y)
        check(f"pick the pad at ({x:g}, {y:g})", "New load" in msg, msg)
    msg = window.pick_pad(18.0, 33.0)
    check("picking the same pin again changes nothing", "already" in msg, msg)
    msg = window.pick_pad(40.0, 2.0)
    check("a click away from pads adds nothing", msg.startswith("No pad"), msg)
    rail = window._rail_draft
    rows = ([(s.ref, s.pins) for s in rail.sources], [(ld.ref, ld.pins) for ld in rail.loads])
    check("rows J1 / U1 / U3", rows == ([("J1", ["1"])], [("U1", ["1"]), ("U3", ["1"])]), str(rows))
    check("the table shows them", [r[1] for r in panel.loads.rows()] == ["U1", "U3"])

    if args.window:
        # The view's own click path: a click picks, a drag does not.
        panel.set_pick("load")
        window.view.reset_view()
        QApplication.processEvents()
        click_view(window, 50.0, 28.0)
        refs = [ld.ref for ld in window._rail_draft.loads]
        check("a mouse click on U2 picks it", "U2" in refs, str(refs))
        n = len(window._rail_draft.loads)
        click_view(window, 50.0, 28.0, drag_px=12)
        check("a drag is not a click", len(window._rail_draft.loads) == n)
        window._rail_draft.loads = [ld for ld in window._rail_draft.loads if ld.ref != "U2"]
        window.refresh_view()
        QApplication.processEvents()
        dpr, qx, qy = qt_click_view(window, 50.0, 28.0)
        refs = [ld.ref for ld in window._rail_draft.loads]
        check("a Qt mouse click on U2 picks it (device-pixel ratio path)", "U2" in refs,
              f"dpr {dpr:g}, widget point ({qx:.0f}, {qy:.0f}); loads {refs}")
        window.grab().save(str(out / "t1_picked_window.png"))
        window.view.screenshot(out / "t1_picked_view.png")
        window._rail_draft.loads = [ld for ld in window._rail_draft.loads if ld.ref != "U2"]
    panel.set_pick("")
    check("Esc/toggle leaves pick mode", window.view.click_mode is None)

    # Values the user would type, then solve.
    rail.sources[0].voltage = 3.3
    rail.loads[0].current, rail.loads[1].current = 2.0, 0.3
    from coppernicus.engine import tied_nets
    panel.set_rail(window.board, rail, tied_nets(window.board, rail.net)[1])
    window._rail_edited()
    res = solve_and_wait(window, args.timeout)
    check("solve with the picked pads", res is not None and len(res.loads) == 2,
          f"max drop {res.max_drop * 1e3:.2f} mV" if res else "no result")
    if args.window and res is not None:
        QApplication.processEvents()
        window.grab().save(str(out / "t1_solved_window.png"))
        window.view.screenshot(out / "t1_solved_view.png")
    return res


def render_scene(view, path: Path, size=(1100, 800)) -> float:
    """Render the view's current scene (its actors and camera) in an off-screen VTK window
    and save it; returns the render time in seconds. Needs no window on screen."""
    import time

    import pyvista as pv
    off = pv.Plotter(off_screen=True, window_size=size)
    off.set_background("white")
    off.enable_parallel_projection()
    for name, actor in view.plotter.renderer.actors.items():
        off.add_actor(actor, name=name, reset_camera=False)
    off.view_xy()
    off.reset_camera()
    t0 = time.perf_counter()
    off.screenshot(str(path))         # the first render (geometry upload) plus a pixel read-back
    seconds = time.perf_counter() - t0
    off.close()
    return seconds


def timed_refresh(window, on_screen: bool = False) -> float:
    """Seconds to redraw the view. pyvistaqt defers render() to the event loop on macOS, so
    on screen the draw is forced synchronously (``_render``) inside the timing; offscreen Qt
    has no GL context and only the scene build is timed."""
    import time
    t0 = time.perf_counter()
    window.refresh_view()
    if on_screen:
        window.view.plotter._render()
    return time.perf_counter() - t0


def run_flow_demo(window, check: Checks, args, out: Path):
    """Task 3 on the demo project: isolines, arrows, ΔV between J1 and U1."""
    from PySide6.QtWidgets import QApplication
    window.open_project(ROOT / "examples" / "demo" / "demo.coppernicus.yml")
    QApplication.processEvents()
    # The demo board was open with +3V3 selected: the project's own +3V3 rail must replace it.
    shown = [(r[1], r[2]) for r in window.rail_panel.loads.rows()]
    check("opening a project shows its rail, not the previous one",
          shown == [("U1", None), ("U2", None), ("U3", None)]
          and window._rail_draft is window.project.rail("+3V3"), str(shown))
    max_drop = window.rail_panel.max_drop.text()
    check("... with its limits", max_drop == "0.25", max_drop)
    layers = [window.layer_box.itemText(i) for i in range(window.layer_box.count())]
    check("... and the new board's layers", layers == ["F.Cu", "B.Cu"], str(layers))
    # One number format whatever the system locale: dots, as in the limits and the problems.
    spin_texts = [window.rail_panel.max_edge.text(), window.rail_panel.temp.text(),
                  window.rail_panel.sources.table.cellWidget(0, 3).text(),
                  window.rail_panel.loads.table.cellWidget(0, 3).text()]
    check("numbers use dots in every spin box", all("," not in t for t in spin_texts), str(spin_texts))
    res = solve_and_wait(window, args.timeout)
    check("solve the demo project", res is not None)
    if res is None:
        return
    window.show_isolines.setChecked(True)
    window.show_arrows.setChecked(True)
    for name in ("B.Cu", "F.Cu"):
        window.layer_box.setCurrentIndex(window.layer_box.findText(name))
        QApplication.processEvents()
        actors = window.view.plotter.renderer.actors
        check(f"isolines and arrows drawn on {name}", "isolines" in actors and "arrows" in actors)
        what = "scene build and on-screen draw" if args.window else "scene build only, offscreen Qt"
        print(f"  redraw {name} with isolines + arrows: {timed_refresh(window, args.window):.3f} s "
              f"({what})", flush=True)
        for field in ("drop", "current"):
            window.field_box.setCurrentIndex(window.field_box.findData(field))
            QApplication.processEvents()
            secs = render_scene(window.view, out / f"t3_{name.replace('.', '')}_{field}_scene.png")
            print(f"  off-screen VTK render {name} {field}: {secs:.3f} s", flush=True)
    window.field_box.setCurrentIndex(window.field_box.findData("drop"))

    # Arrows on U3's feeder track (F.Cu, via at y=26 → pad at y=33) point toward U3. The view
    # plots (x, −y), so toward U3 is toward smaller display y.
    import numpy as np
    window.layer_box.setCurrentIndex(window.layer_box.findText("F.Cu"))
    QApplication.processEvents()
    pts = np.asarray(window.view.plotter.renderer.actors["arrows"].mapper.dataset.points)[:, :2]
    shafts = pts.reshape(-1, 3, 2, 2)[:, 0]                 # every arrow: shaft, head, head
    on_track = (np.abs(shafts[:, :, 0].mean(axis=1) - 18.0) < 0.3) & \
               (shafts[:, :, 1].mean(axis=1) < -27) & (shafts[:, :, 1].mean(axis=1) > -32)
    toward_u3 = shafts[on_track, 1, 1] < shafts[on_track, 0, 1]
    check("arrows on U3's track point toward U3", on_track.any() and toward_u3.all(),
          f"{toward_u3.sum()}/{on_track.sum()} arrows")

    # ΔV between J1 and U1 on F.Cu equals the drop at U1 (single-pad load) within 0.5 mV.
    window.measure.setChecked(True)
    check("measure mode on the view", window.view.click_mode == "measure")
    msg_a = window.view.measure_at(6.0, 20.0)
    msg_b = window.view.measure_at(50.0, 10.0)
    u1 = next(t for t in res.loads if t.ref == "U1")
    try:
        dv = float(msg_b.split("=")[1].split("mV")[0]) / 1e3
    except (IndexError, ValueError):
        dv = float("nan")
    check("ΔV J1→U1 within 0.5 mV of drop(U1)", abs(dv - res.drop(u1)) < 0.5e-3,
          f"{msg_b}; drop(U1) = {res.drop(u1) * 1e3:.3f} mV")
    check("A is reported first", msg_a.startswith("A: "), msg_a)
    msg = window.view.measure_at(30.0, 1.0)
    check("a point off this net's copper says so", "no copper of this net here" in msg, msg)
    render_scene(window.view, out / "t3_FCu_measure_scene.png")
    window.stop_clicking()
    check("Esc leaves measure mode", window.view.click_mode is None and not window.measure.isChecked())
    window.grab().save(str(out / "t3_window.png"))
    if args.window:
        window.view.screenshot(out / "t3_view.png")


def run_local(window, check: Checks, args, out: Path):
    """Time the redraw on a local project rail (read-only: nothing is saved)."""
    from PySide6.QtWidgets import QApplication
    window.open_project(args.local)
    window.nets.select(args.rail)
    QApplication.processEvents()
    res = solve_and_wait(window, args.timeout)
    check(f"solve {args.rail} of the local project", res is not None)
    if res is None:
        return
    window.show_isolines.setChecked(True)
    window.show_arrows.setChecked(True)
    for lf in sorted(res.layers, key=lambda lf: -len(lf.tris))[:2]:
        window.layer_box.setCurrentIndex(window.layer_box.findText(lf.name))
        QApplication.processEvents()
        for field in ("drop", "current"):
            window.field_box.setCurrentIndex(window.field_box.findData(field))
            QApplication.processEvents()
            arrows = None
            name = f"local_{lf.name.replace('.', '')}_{field}"
            if args.window:
                total = timed_refresh(window, on_screen=True)
                window.view.screenshot(out / f"{name}_view.png")
                detail = f"{total:.3f} s in the window"
            else:
                build = timed_refresh(window)
                secs = render_scene(window.view, out / f"{name}_scene.png")
                total = build + secs
                detail = f"scene {build:.3f} s + off-screen render {secs:.3f} s"
            arrows = window.view.plotter.renderer.actors.get("arrows")
            n_arrows = arrows.mapper.dataset.n_lines // 3 if arrows is not None else 0
            check(f"{lf.name} {field} ({len(lf.tris):,} triangles): redraw under 1 s",
                  total < 1.0, f"{detail}, {n_arrows} arrows")
            check(f"{lf.name} {field}: at most 2000 arrows", n_arrows <= 2000)


def run_review_fixes(window, check: Checks, args, out: Path):
    """Findings of the Part A review: comma input, a stale solve, state left by open_board,
    a double-click while measuring."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    demo = ROOT / "examples" / "demo" / "demo.coppernicus.yml"

    # A comma typed as the decimal sign is read as one, never as a thousands separator.
    window.open_project(demo)
    QApplication.processEvents()
    spin = window.rail_panel.sources.table.cellWidget(0, 3)
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin, "3,3")
    QTest.keyClick(spin, Qt.Key_Return)
    rail = window.project.rail("+3V3")
    check("typing 3,3 in a voltage gives 3.3 V", abs(spin.value() - 3.3) < 1e-9
          and abs(rail.sources[0].voltage - 3.3) < 1e-9, f"{spin.text()!r} -> {rail.sources[0].voltage}")
    edit = window.rail_panel.max_drop
    edit.selectAll()
    QTest.keyClicks(edit, "1,5")
    QTest.keyClick(edit, Qt.Key_Return)
    check("typing 1,5 in a limit gives 1.5 %", rail.limits.max_drop_pct == 1.5,
          f"{edit.text()!r} -> {rail.limits.max_drop_pct}")
    edge = window.rail_panel.max_edge
    edge.lineEdit().selectAll()
    QTest.keyClicks(edge, "0,3")
    QTest.keyClick(edge, Qt.Key_Return)
    check("typing 0,3 in the mesh edge gives 0.3 mm", abs(window.project.settings.max_edge_mm - 0.3) < 1e-9,
          f"{edge.text()!r} -> {window.project.settings.max_edge_mm}")

    # A solve still running when another board is opened does not land on it.
    window.open_project(demo)
    QApplication.processEvents()
    window.solve()
    old = window.worker
    window.open_board(DEMO_BOARD)                       # a fresh project: +3V3 has no rail
    window.nets.select("+3V3")
    if old is not None:
        old.wait(int(args.timeout * 1000))
    QApplication.processEvents()
    QApplication.processEvents()
    check("a solve of the previous board is dropped", "+3V3" not in window.results
          and "worst load" not in window.problems.summary.text(), window.problems.summary.text())

    # open_board leaves nothing of the previous board: measure mode, layers, limits.
    window.open_project(demo)
    QApplication.processEvents()
    solve_and_wait(window, args.timeout)
    window.measure.setChecked(True)
    window.view.measure_at(6.0, 20.0)
    window.open_board(out / "pins.kipi.json.gz")        # no rails: nothing selected
    QApplication.processEvents()
    check("open_board leaves measure mode", not window.measure.isChecked() and window.view.click_mode is None)
    check("... and drops the old result held by the view",
          window.view._measure_result is None and not window.view._measure)
    check("... and empties the layer box", window.layer_box.count() == 0,
          str([window.layer_box.itemText(i) for i in range(window.layer_box.count())]))
    left = window.rail_panel.max_drop.text()
    check("... and the limit fields", left == "", left)

    if args.window:
        # A double-click while measuring reads the value and keeps point A.
        window.open_project(demo)
        QApplication.processEvents()
        solve_and_wait(window, args.timeout)
        window.layer_box.setCurrentIndex(window.layer_box.findText("F.Cu"))
        window.view.reset_view()
        QApplication.processEvents()
        window.measure.setChecked(True)
        window.view.measure_at(6.0, 20.0)
        messages = []
        window.view.probed.connect(messages.append)
        widget = window.view.plotter.interactor
        ren = window.view.plotter.renderer
        ren.SetWorldPoint(50.0, -10.0, 0.0, 1.0)
        ren.WorldToDisplay()
        dx, dy, _ = ren.GetDisplayPoint()
        dpr = widget.devicePixelRatioF()
        from PySide6.QtCore import QPoint
        pos = QPoint(round(dx / dpr), round((window.view.plotter.ren_win.GetSize()[1] - 1 - dy) / dpr))
        # What a real mouse sends for a double-click: press, release, double-click press,
        # release. (QTest.mouseDClick alone sends only the double-click event.)
        QTest.mouseClick(widget, Qt.LeftButton, Qt.NoModifier, pos)
        QTest.mouseDClick(widget, Qt.LeftButton, Qt.NoModifier, pos)
        QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, pos)
        QApplication.processEvents()
        a = window.view._measure[0][1:3] if window.view._measure else None
        last = messages[-1] if messages else ""
        check("a double-click while measuring keeps A and does not read 0 mV",
              a == (6.0, 20.0) and "= 0.000 mV" not in last, f"A {a}; messages {messages}")
        window.measure.setChecked(False)


def run_close_asks(window, check: Checks, out: Path):
    """Closing with only hand-marked rails asks to save (the dialog answers Discard)."""
    from PySide6.QtWidgets import QApplication
    window.open_board(out / "pins.kipi.json.gz")
    QApplication.processEvents()
    window.nets.set_marked("OTHER", True)
    before = len(DIALOGS)
    window.close()
    QApplication.processEvents()
    check("closing with marked rails asks to save", any(k == "question" for k, _ in DIALOGS[before:]),
          str(DIALOGS[before:]))


def run_other_net(window, check: Checks, out: Path):
    """A click on a pad of another net says so and adds nothing (synthetic pins board)."""
    from PySide6.QtWidgets import QApplication

    from coppernicus.engine.synthetic import pins_board, save_snapshot
    path = out / "pins.kipi.json.gz"
    save_snapshot(pins_board(), path)
    window.open_board(path)
    window.nets.select("VCC")
    QApplication.processEvents()
    window.rail_panel.set_pick("load")
    msg = window.pick_pad(16.0, 0.0)
    check("a pad of another net is refused", msg == "U1.4 is on OTHER, not on VCC", msg)
    check("... and adds nothing", window._rail_draft.loads == [])
    msg = window.pick_pad(12.0, 0.0)
    check("a pad of the rail is added", window._rail_draft.loads[0].pins == ["2"], msg)
    window.rail_panel.set_pick("")


def run_marked(window, check: Checks, out: Path):
    """Task 4: mark a signal net as a rail, save the project in ``out``, reopen it."""
    from PySide6.QtWidgets import QApplication

    def listed():
        return [window.nets.list.item(i).text() for i in range(window.nets.list.count())]
    window.open_board(out / "pins.kipi.json.gz")             # written by run_other_net
    QApplication.processEvents()
    check("OTHER is not a rail by the heuristic", "OTHER" not in listed(), str(listed()))
    window.nets.set_marked("OTHER", True)
    check("marking OTHER lists it", "OTHER" in listed() and window.project.marked_rails == ["OTHER"])
    check("marking makes the project dirty", window.dirty)
    path = out / "marked.coppernicus.yml"
    window._save(path)
    check("saved with marked_rails", "marked_rails" in path.read_text())
    window.open_project(path)
    QApplication.processEvents()
    check("reopened: OTHER still marked and listed", window.nets.marked == ["OTHER"] and "OTHER" in listed())
    window.nets.set_marked("OTHER", False)
    check("unmarking hides it again", "OTHER" not in listed() and window.project.marked_rails == [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window", action="store_true", help="use a real window (it closes itself)")
    ap.add_argument("--out", help="folder for screenshots (default: a new temporary folder)")
    ap.add_argument("--timeout", type=float, default=300.0, help="hard limit for the whole run, s")
    ap.add_argument("--local", help="a local project to time (read-only)")
    ap.add_argument("--rail", help="with --local: the rail to solve and time")
    args = ap.parse_args(argv)
    if not args.window:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    faulthandler.dump_traceback_later(args.timeout, exit=True)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="coppernicus-gui-"))
    out.mkdir(parents=True, exist_ok=True)

    from PySide6.QtWidgets import QApplication

    from coppernicus.app.main_window import MainWindow
    app = QApplication.instance() or QApplication(sys.argv[:1])
    _quiet_dialogs()
    window = MainWindow()
    window.show()
    app.processEvents()
    check = Checks()
    try:
        run_other_net(window, check, out)
        run_marked(window, check, out)
        run_demo(window, check, args, out)
        run_flow_demo(window, check, args, out)
        if args.local and args.rail:
            run_local(window, check, args, out)
        run_review_fixes(window, check, args, out)
        run_close_asks(window, check, out)
    finally:
        if window.isVisible():
            window.dirty = False          # never ask to save
            window.close()
        app.processEvents()
    print(f"{'all checks passed' if not check.failed else f'{check.failed} check(s) failed'}"
          + f"; images in {out}", flush=True)
    return 1 if check.failed else 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
