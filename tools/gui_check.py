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
CHAIN_PROJECT = ROOT / "examples" / "power_chain" / "power_chain.coppernicus.yml"


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


def run_open_asks(window, check: Checks, out: Path):
    """Opening another board or project from the menu asks before dropping unsaved edits;
    Cancel keeps everything and shows no file dialog."""
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
    window.open_board(out / "pins.kipi.json.gz")
    QApplication.processEvents()
    window.nets.set_marked("OTHER", True)                     # an unsaved edit
    shown = []
    real_open, real_question = QFileDialog.getOpenFileName, QMessageBox.question
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (shown.append(a[1]), ("", ""))[1])
    try:
        for action in (window.open_board_dialog, window.open_project_dialog):
            before = len(DIALOGS)
            action()
            check(f"{action.__name__} asks before dropping unsaved edits",
                  any(k == "question" for k, _ in DIALOGS[before:]), str(DIALOGS[before:]))
        QMessageBox.question = staticmethod(lambda *a, **k: (DIALOGS.append(("question", a[1])),
                                                             QMessageBox.Cancel)[1])
        n = len(shown)
        window.open_board_dialog()
        check("Cancel keeps the edits and shows no file dialog",
              len(shown) == n and window.project.marked_rails == ["OTHER"] and window.dirty)
    finally:
        QFileDialog.getOpenFileName, QMessageBox.question = real_open, real_question
    window.dirty = False


def run_compare(window, check: Checks, args, out: Path):
    """Task 5: pin a baseline, change a load, the settings, then the board; read the deltas,
    the difference map and the loss chart."""
    import shutil

    from PySide6.QtWidgets import QApplication

    from coppernicus.engine import tied_nets
    demo = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
    window.open_project(demo)
    QApplication.processEvents()
    base = solve_and_wait(window, args.timeout)
    check("before a baseline the Compare tab says how to start", "No baseline" in window.compare.info.text())
    check("the loss chart shows the current result", window.losses.series == ["current"])
    window.pin_baseline()
    check("pinning: the shown result is the baseline", "is the baseline" in window.compare.info.text(),
          window.compare.info.text())

    rail = window.project.rail("+3V3")
    rail.loads[0].current = 4.0                              # U1: 2 A → 4 A
    window.rail_panel.set_rail(window.board, rail, tied_nets(window.board, rail.net)[1])
    window._rail_edited()
    now = solve_and_wait(window, args.timeout)
    rows = {r[0]: r for r in window.compare.rows()}
    u1 = rows.get("U1: drop at its worst pin (mV)")
    want = (now.drop(now.loads[0]) - base.drop(base.loads[0])) * 1e3
    check("U1's delta is the change of its drop", u1 is not None and abs(float(u1[3]) - want) < 2e-3,
          f"{u1}; expected {want:+.3f}")
    check("the loss chart shows baseline and current", window.losses.series == ["baseline", "current"])
    if args.window:
        window.results_tabs.setCurrentWidget(window.compare)
        QApplication.processEvents()
        window.grab().save(str(out / "t5_compare_window.png"))
        window.results_tabs.setCurrentWidget(window.losses)
        QApplication.processEvents()
        window.grab().save(str(out / "t5_losses_window.png"))
    window.field_box.setCurrentIndex(window.field_box.findData("difference"))
    QApplication.processEvents()
    title = window.view.colorbar._title
    check("the difference map is exact on the same mesh",
          window.field_box.currentData() == "difference" and "exact" in title, title)
    window.show_isolines.setChecked(True)
    QApplication.processEvents()
    title = window.view.colorbar._title
    check("on the Δ map the isolines say they are the current drop", "current drop" in title, title)
    probed = []
    window.view.probed.connect(probed.append)
    window.view._on_double_click((50.0, -10.0, 0.0))         # U1, display frame (x, −y)
    window.view.probed.disconnect(probed.append)
    check("on the Δ map the probe reads the change of drop", bool(probed) and "Δ drop" in probed[-1],
          str(probed))
    if args.window:
        window.view.screenshot(out / "t5_difference_view.png")
    # Re-pinning while the Δ map is shown: the shown result is now the baseline.
    window.pin_baseline()
    QApplication.processEvents()
    check("re-pinning while the Δ map is shown makes it fall back to the drop",
          window.field_box.currentData() == "drop" and window.view.colorbar._title.startswith("mV below"),
          window.view.colorbar._title)

    window.rail_panel.max_edge.setValue(0.4)                 # another mesh
    QApplication.processEvents()
    solve_and_wait(window, args.timeout)
    window.field_box.setCurrentIndex(window.field_box.findData("difference"))
    QApplication.processEvents()
    title = window.view.colorbar._title
    check("on another mesh the map says it is interpolated", "interpolated" in title, title)
    check("the changed setting is listed", "max_edge_mm" in window.compare.notes.toPlainText())
    window._rail_edited()                                    # the result no longer applies
    check("the Δ cache is dropped when the runs change", not window._diff_cache)
    window.rail_panel.max_edge.setValue(0.5)

    copy = out / "demo_edited.kipi.json.gz"                  # the board "edited": another file
    shutil.copy(ROOT / "examples" / "demo" / "demo.kipi.json.gz", copy)
    window.open_board(copy)
    window.nets.select("+3V3")
    QApplication.processEvents()
    window.rail_panel.set_pick("source")
    window.pick_pad(6.0, 20.0)
    window.rail_panel.set_pick("load")
    window.pick_pad(50.0, 10.0)
    window.rail_panel.set_pick("")
    r = window._rail_draft
    r.sources[0].voltage, r.loads[0].current = 3.3, 2.0
    window.rail_panel.set_rail(window.board, r, [])
    window._rail_edited()
    window.field_box.setCurrentIndex(window.field_box.findData("difference"))   # chosen before solving
    solve_and_wait(window, args.timeout)
    check("after the board changed the loads are still compared",
          any(row[0].startswith("U1") for row in window.compare.rows())
          and "board file changed" in window.compare.notes.toPlainText())
    status = window.statusBar().currentMessage()
    check("... but the difference map is refused, and the solve message keeps the reason",
          window.field_box.currentData() == "drop" and "board file changed" in status, status)
    # 1a: a board with no rails leaves nothing of the comparison on screen.
    window.open_board(out / "pins.kipi.json.gz")
    QApplication.processEvents()
    check("opening a board without rails clears Compare and the loss chart",
          window.compare.rows() == [] and window.losses.series == [] and not window.compare.pin.isEnabled(),
          f"rows {len(window.compare.rows())}, series {window.losses.series}")
    window.clear_baseline()
    check("clearing the baseline", window.baseline is None and "No baseline" in window.compare.info.text())
    window.dirty = False


def run_report(window, check: Checks, args, out: Path):
    """Export report: refused without results or while a solve runs; otherwise a Save dialog
    that starts in results/ next to the project, the file written off the UI thread, its
    size reported, and an offer to open it (QDesktopServices is intercepted here)."""
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from coppernicus.project import Project
    project = Project.load(ROOT / "examples" / "demo" / "demo.coppernicus.yml")
    project.save(out / "demo_copy.coppernicus.yml")           # a project outside the repository
    asked, opened = [], []
    target = out / "chosen" / "demo_report.html"
    real_save, real_open, real_question = (QFileDialog.getSaveFileName, QDesktopServices.openUrl,
                                           QMessageBox.question)
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (asked.append(a[2]), (str(target), ""))[1])
    QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()) or True)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Open)
    try:
        window.open_project(out / "demo_copy.coppernicus.yml")
        QApplication.processEvents()
        window.export_report()
        status = window.statusBar().currentMessage()
        check("export without results is refused", not asked and "Solve" in status, status)
        window.solve()                                       # still running...
        window.export_report()
        check("export while a solve runs is refused", not asked and "solve is running"
              in window.statusBar().currentMessage(), window.statusBar().currentMessage())
        if window.worker is not None:
            window.worker.wait(int(args.timeout * 1000))
        QApplication.processEvents()
        QApplication.processEvents()
        window.export_report()
        check("the Save dialog starts in results/ next to the project",
              asked and Path(asked[-1]) == out / "results" / "demo_copy_report.html", str(asked))
        if window.report_worker is not None:
            window.report_worker.wait(int(args.timeout * 1000))
        QApplication.processEvents()
        QApplication.processEvents()
        text = target.read_text(encoding="utf-8") if target.exists() else ""
        status = window.statusBar().currentMessage()
        check("the report is written", text.lstrip().lower().startswith("<!doctype html") and "+3V3" in text,
              f"{target} ({len(text)} chars)")
        check("its size is reported", "MB" in status and str(target.name) in status, status)
        check("and it is offered to open", opened == [str(target)], str(opened))
        n = len(asked)
        window.solve()                                       # results exist, and a solve runs
        window.export_report()
        status = window.statusBar().currentMessage()
        check("... and with results, a running solve still blocks it", len(asked) == n
              and "solve is running" in status, status)
        if window.worker is not None:
            window.worker.wait(int(args.timeout * 1000))
        QApplication.processEvents()
    finally:
        QFileDialog.getSaveFileName, QDesktopServices.openUrl, QMessageBox.question = (
            real_save, real_open, real_question)
    window.dirty = False


def run_close_asks(window, check: Checks, args, out: Path):
    """Closing with only hand-marked rails asks to save (the dialog answers Discard), and
    closing while a report is being written waits for it."""
    from PySide6.QtWidgets import QApplication, QFileDialog
    window.open_project(out / "demo_copy.coppernicus.yml")        # written by run_report
    QApplication.processEvents()
    solve_and_wait(window, args.timeout)
    target = out / "closing" / "report.html"
    real_save = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))
    try:
        window.export_report()
    finally:
        QFileDialog.getSaveFileName = real_save
    window.nets.set_marked("LED_A", True)                        # an unsaved edit
    before = len(DIALOGS)
    window.close()
    check("closing with marked rails asks to save", any(k == "question" for k, _ in DIALOGS[before:]),
          str(DIALOGS[before:]))
    worker = window.report_worker
    check("closing waits for a report being written", worker is not None and not worker.isRunning()
          and target.exists(), f"running {worker.isRunning() if worker else None}, file {target.exists()}")
    QApplication.processEvents()


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


def run_power_chain(window, check: Checks, args, out: Path):
    """Task 6: the power-chain example (project model v2) opens, its rails solve one at a
    time, and editing rows then saving (into ``out``) keeps every field the rail editor does
    not show: a regulator's kind, efficiency, input, ground and sense pins, a load's ground
    pins, the ferrite link."""
    from PySide6.QtWidgets import QApplication

    from coppernicus.engine.synthetic import power_chain_project
    from coppernicus.project import Project
    ok = window.open_project(CHAIN_PROJECT)
    QApplication.processEvents()
    check("power chain: the v2 example opens", ok and window.board is not None)
    for net in ("+3V3", "+1V8"):
        window.nets.select(net)
        QApplication.processEvents()
        res = solve_and_wait(window, args.timeout)
        # +3V3 also delivers what FB1 passes on to +3V3_F, estimated (Task 7).
        expected = {"+3V3": ["U4", "FB1 (link, estimated)"], "+1V8": ["U3"]}[net]
        check(f"power chain: {net} solves on its own", res is not None
              and [t.name for t in res.loads] == expected,
              "" if res is None else ", ".join(f"{t.name} {res.drop(t) * 1e3:.2f} mV" for t in res.loads))

    window.nets.select("+3V3")
    QApplication.processEvents()
    window.rail_panel.loads.table.cellWidget(0, 3).setValue(0.7)
    QApplication.processEvents()
    rail = window.project.rail("+3V3")
    src = rail.sources[0]
    check("power chain: editing U4's current changes it", rail.loads[0].current == 0.7,
          repr(rail.loads[0]))
    check("... and keeps U1's kind, efficiency, input and ground",
          (src.kind, src.efficiency, src.input_net, src.ground_net) == ("smps", 0.9, "VIN", "GND"),
          repr(src))
    check("... and U4's ground pins", rail.loads[0].ground_net == "GND", repr(rail.loads[0]))
    part = window.rail_panel.loads.table.cellWidget(0, 1)
    u4 = part.currentIndex()
    part.setCurrentIndex(part.findData("U1"))           # picked another part by mistake…
    QApplication.processEvents()
    part.setCurrentIndex(u4)                              # … and back
    QApplication.processEvents()
    load = window.project.rail("+3V3").loads[0]
    check("... another part picked and back: U4 keeps its ground pins",
          (load.ref, load.ground_net, load.ground_pins) == ("U4", "GND", ["2"]), repr(load))
    window.nets.select("+1V8")
    QApplication.processEvents()
    window.rail_panel.sources.table.item(0, 0).setText("LDO")
    QApplication.processEvents()
    u2 = window.project.rail("+1V8").sources[0]
    check("power chain: renaming U2 keeps its sense and ground-sense pins",
          u2.name == "LDO" and (u2.sense_ref, u2.ground_sense_net) == ("U3", "GND"), repr(u2))

    path = out / "power_chain_edited.coppernicus.yml"
    window._save(path)
    saved = Project.load(path)
    expected = power_chain_project()
    expected.rail("+3V3").loads[0].current = 0.7
    expected.rail("+1V8").sources[0].name = "LDO"
    check("power chain: saved and reopened, every v2 field and the link survive",
          saved.rails == expected.rails and saved.links == expected.links,
          f"{saved.rails!r} {saved.links!r}" if saved.rails != expected.rails else "")


def solve_tree_and_wait(window, timeout_s: float):
    from PySide6.QtWidgets import QApplication
    window.solve_tree()
    if window.tree_worker is None:
        return None
    window.tree_worker.wait(int(timeout_s * 1000))
    QApplication.processEvents()
    QApplication.processEvents()
    return window.tree


def run_power_tree(window, check: Checks, args, out: Path):
    """Task 7: on the power-chain example, a rail solved alone draws its regulator's
    estimated input; "Solve power tree" fills the Power tree tab and the group selector; a
    ground group shows its rise; the regulator box edits a source's kind, efficiency and pins
    and survives table edits and saving."""
    from PySide6.QtWidgets import QApplication

    from coppernicus.engine.synthetic import PowerChain as g
    from coppernicus.project import Project
    window.open_project(CHAIN_PROJECT)
    QApplication.processEvents()
    window.nets.select("VIN")
    QApplication.processEvents()
    res = solve_and_wait(window, args.timeout)
    est = [t for t in res.loads if t.name == "U1 (input, estimated)"] if res else []
    check("power tree: VIN solves alone, drawing U1's estimated input",
          len(est) == 1 and abs(est[0].current - (g.V_SMPS * (g.I_U4 + g.I_U3 + g.IQ_LDO)
                                                  / (g.ETA * g.V_SRC) + g.IQ_SMPS)) < 1e-9,
          f"{est[0].current:.4f} A" if est else str(res))

    tree = solve_tree_and_wait(window, args.timeout)
    check("power tree: Solve power tree finishes", tree is not None and set(tree.groups) == {
        "VIN", "+3V3", "+1V8", "GND"}, "" if tree is None else str(sorted(tree.groups)))
    if tree is None:
        return
    vrms, loads = window.tree_panel.vrm_rows(), window.tree_panel.load_rows()
    check("... the VRM table lists J1, U1, U2", [r[0] for r in vrms] == ["J1", "U1", "U2"], str(vrms))
    u3 = next((r for r in loads if r[0] == "U3"), None)
    check("... the load table shows U3 at 1.80000 V across", u3 is not None and "1.80000" in u3, str(u3))
    u1_in = next((r for r in loads if r[0] == "U1 (input)"), None)
    check("... and U1's input as a load on VIN", u1_in is not None and u1_in[1] == "VIN", str(u1_in))
    check("... and the ferrite link", "FB1" in window.tree_panel.links.text(), window.tree_panel.links.text())
    keys = {window.group_box.itemData(i) for i in range(window.group_box.count())}
    check("... the group selector lists the rails and GND", keys == {None, "VIN", "+3V3", "+1V8", "GND"},
          str(keys))
    window.group_box.setCurrentIndex(window.group_box.findData("GND"))
    QApplication.processEvents()
    title = window.view.colorbar._title
    check("... GND shows as a ground-rise map", window.view._result is tree.groups["GND"]
          and "ground rise" in title, title)
    render_scene(window.view, out / "power_tree_gnd_scene.png")
    top = window.problems.tree.topLevelItem(0)
    check("... its Problems list ranks the return pins by rise",
          top is not None and "U3 (return)" in top.text(1), top.text(1) if top else "empty")
    window.view._on_double_click((97.5, -1.0))
    msg = window.statusBar().currentMessage()
    check("... a double-click on GND reads the rise", "rise = 38.4" in msg, msg)
    window.group_box.setCurrentIndex(window.group_box.findData("+1V8"))
    QApplication.processEvents()
    check("... +1V8 shown from the tree", window.view._result is tree.groups["+1V8"])
    spin = window.rail_panel.loads.table.cellWidget(0, 3)            # VIN is still the net
    window.nets.select("+3V3")
    QApplication.processEvents()
    window.group_box.setCurrentIndex(window.group_box.findData("GND"))
    QApplication.processEvents()
    spin = window.rail_panel.loads.table.cellWidget(0, 3)
    spin.setValue(0.55)
    QApplication.processEvents()
    check("... editing the rail while a group is shown keeps the table being edited",
          window.rail_panel.loads.table.cellWidget(0, 3) is spin
          and window.project.rail("+3V3").loads[0].current == 0.55)
    spin.setValue(0.5)
    QApplication.processEvents()
    tree = solve_tree_and_wait(window, args.timeout)

    i_in_before = next(v.i_in_a for v in tree.vrms if v.name == "U1")
    window.nets.select("VIN")
    QApplication.processEvents()
    solve_and_wait(window, args.timeout)                 # VIN alone: U1's input estimated
    window.nets.select("+3V3")
    QApplication.processEvents()
    check("... picking a net goes back to the rail solved alone", window.group_box.currentData() is None)
    box = window.rail_panel.source_box               # the first source is shown and selected
    check("power tree: the regulator box shows U1 as a switcher, η 0.9, input VIN.1, ground GND.3",
          (box.kind.currentData(), box.efficiency.text(), box.input.get(), box.ground.get())
          == ("smps", "0.9", ("", ["1"], "VIN"), ("", ["3"], "GND")),
          f"{box.kind.currentData()} {box.efficiency.text()} {box.input.get()} {box.ground.get()}")
    box.efficiency.setText("0.8")
    box.efficiency.editingFinished.emit()
    QApplication.processEvents()
    check("... editing η writes the project and drops the stale tree",
          window.project.rail("+3V3").sources[0].efficiency == 0.8 and window.dirty and window.tree is None)
    check("... and the result of VIN, whose estimate used U1's η", "VIN" not in window.results,
          str(sorted(window.results)))
    window.rail_panel.select_load(0)
    ground = window.rail_panel.load_ground.get()
    check("... the load box shows U4's ground pins", ground == ("", ["2"], "GND"), str(ground))
    window.rail_panel.loads.table.cellWidget(0, 3).setValue(0.6)
    QApplication.processEvents()
    src = window.project.rail("+3V3").sources[0]
    check("... a table edit after it keeps η 0.8", src.efficiency == 0.8 and src.kind == "smps", repr(src))
    window.solve_tree()                                   # edited while it solves…
    window.project.rail("+1V8").limits.max_drop_pct = None
    window.rail_panel.loads.table.cellWidget(0, 3).setValue(0.61)
    window.tree_worker.wait(int(args.timeout * 1000))
    QApplication.processEvents()
    QApplication.processEvents()
    check("... a tree solved while the setup changed is not shown as current", window.tree is None,
          window.statusBar().currentMessage())
    window.rail_panel.loads.table.cellWidget(0, 3).setValue(0.6)
    QApplication.processEvents()
    tree = solve_tree_and_wait(window, args.timeout)
    i_in = next(v.i_in_a for v in tree.vrms if v.name == "U1") if tree else None
    check("power tree: a lower efficiency draws more input current", i_in is not None and i_in > i_in_before,
          f"{i_in_before:.4f} → {i_in}")
    window.nets.select("+3V3")
    QApplication.processEvents()
    window.solve_tree()                                   # a limit edited while it solves…
    window.rail_panel.max_drop.setText("0.01")
    window.rail_panel.max_drop.editingFinished.emit()
    window.tree_worker.wait(int(args.timeout * 1000))
    QApplication.processEvents()
    QApplication.processEvents()
    u4 = next((r for r in window.tree_panel.load_rows() if r[0] == "U4"), None)
    check("power tree: a limit edited while the tree solves judges the tree it brings",
          window.tree is not None and u4 is not None and u4[-1] == "FAIL"
          and "limit(s) exceeded" in window.tree_panel.summary.text(),
          f"{u4} | {window.tree_panel.summary.text()[-80:]}")
    window.rail_panel.max_drop.setText("")
    window.rail_panel.max_drop.editingFinished.emit()
    QApplication.processEvents()
    u4 = next((r for r in window.tree_panel.load_rows() if r[0] == "U4"), None)
    check("... and clearing it judges nothing again", u4 is not None and u4[-1] == "not judged"
          and "no limits set" in window.tree_panel.summary.text(), str(u4))
    window.nets.select("GND")                             # a ground-rise limit on GND
    QApplication.processEvents()
    window.rail_panel.max_rise.setText("10")
    window.rail_panel.max_rise.editingFinished.emit()
    QApplication.processEvents()
    gnd_rail = window.project.rail("GND")
    check("power tree: a ground-rise limit is set on GND",
          gnd_rail is not None and gnd_rail.limits.max_ground_rise_mv == 10.0)
    tree = solve_tree_and_wait(window, args.timeout)
    window.show_group("GND")
    QApplication.processEvents()
    top = window.problems.tree.topLevelItem(0)
    check("... and judges the ground group: the worst return pin fails",
          "limit(s) exceeded" in window.tree_panel.summary.text() and top is not None
          and top.text(0).upper().startswith("FAIL"),
          f"{window.tree_panel.summary.text()} | {top.text(0) if top else ''} {top.text(1) if top else ''}")
    window.rail_panel.max_rise.setText("100")           # edited while GND is shown
    window.rail_panel.max_rise.editingFinished.emit()
    QApplication.processEvents()
    top = window.problems.tree.topLevelItem(0)
    check("... a looser limit edited while GND is shown re-judges its Problems list",
          top is not None and not top.text(0).upper().startswith("FAIL")
          and "no limits exceeded" in window.tree_panel.summary.text(),
          f"{top.text(0) if top else ''} {top.text(1) if top else ''} | "
          f"{window.tree_panel.summary.text()[-60:]}")
    window.rail_panel.max_rise.setText("10")
    window.rail_panel.max_rise.editingFinished.emit()
    QApplication.processEvents()
    top = window.problems.tree.topLevelItem(0)
    check("... and the tighter one again fails it",
          top is not None and top.text(0).upper().startswith("FAIL"),
          f"{top.text(0) if top else ''} {top.text(1) if top else ''}")
    path = out / "power_tree_edited.coppernicus.yml"
    window._save(path)
    saved = Project.load(path)
    check("... saved: η 0.8 and U4 0.6 A", (saved.rail("+3V3").sources[0].efficiency,
                                             saved.rail("+3V3").loads[0].current) == (0.8, 0.6))
    check("... saved: the limits-only GND rail keeps its ground-rise limit",
          saved.rail("GND") is not None and saved.rail("GND").limits.max_ground_rise_mv == 10.0
          and window.project.rail("GND") is not None)
    assert g.I_U4 == 0.5       # the example itself is unchanged


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
        run_power_chain(window, check, args, out)
        run_power_tree(window, check, args, out)
        run_demo(window, check, args, out)
        run_flow_demo(window, check, args, out)
        if args.local and args.rail:
            run_local(window, check, args, out)
        run_review_fixes(window, check, args, out)
        run_compare(window, check, args, out)
        run_report(window, check, args, out)
        run_open_asks(window, check, out)
        run_close_asks(window, check, args, out)
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
