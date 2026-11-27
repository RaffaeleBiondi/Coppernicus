"""Screenshots for the README and the docs, made from the synthetic examples only.

The real capture, which replaces the images in docs/images (about two minutes):

    uv run python tools/make_docs_screenshots.py --set all        # every PNG below but report-*
    uv run python tools/make_docs_screenshots.py --set reports    # the pages for report-*.png,
                                                                  # captured in a browser by hand

One set at a time: ``--set guide`` (the default), ``features``, ``analyses``, ``cli``.
``--out DIR`` writes somewhere else, e.g. to review. ``--offscreen --out DIR`` runs every
scenario without a window and checks that it reaches its state (tabs shown, solves finished,
rows present); the board view is blank there, so such images never go into docs/images.

The sets, and the docs that show their images:

- ``guide``: the README's six ``guide-*.png``: pads picked on the bare demo board, the demo
  project's rail setup, the solved maps, a probe readout and the problems.
- ``features``: ``tree-*.png`` (the power chain's power tree and ground rise,
  docs/power-tree.md), ``fields-*.png`` (the fields example's rail from KiCad footprint fields,
  docs/kicad-fields.md), ``whatif-*.png`` (the copper thickness editor, Pin baseline and
  Compare on the demo) and ``revisions-delta.png`` (two revisions of the demo board, solved in
  a temporary folder), both docs/what-if-and-compare.md.
- ``analyses``: a window per analysis tab, each solved on its example: ``thermal-1-window.png``
  (the thermal demo's power tree, F.Cu's board temperature and the Temperature tab;
  docs/thermal.md), ``ac-1-window.png`` (Solve impedance on examples/pdn; docs/ac-impedance.md),
  ``noise-1-window.png`` (Solve noise on noise-tight; docs/noise.md),
  ``tolerance-1-window.png`` (the Tolerance tab's Solve on tolerance-tight; docs/tolerance.md)
  and ``results-attention.png`` (the Results panel: the thermal demo's +3V3 solved alone,
  amber ATTENTION rows; docs/technical.md#current-density-default and the README's step 6).
- ``cli``: the command line's own PNGs, written by ``coppernicus`` into a temporary folder:
  ``thermal-map.png`` (docs/thermal.md), ``ac-impedance.png`` (docs/ac-impedance.md) and
  ``noise-waveforms.png`` (docs/noise.md). No window.
- ``reports``: no PNG. The HTML pages behind ``report-*.png`` (demo, power tree, thermal,
  impedance, noise, tolerance; docs/html-report.md and the pages listed) go into
  ``--html-dir``; the run prints each page's URL, the browser width of the image and the part
  to capture. The repository has no headless browser.

A real window never takes the focus, as with ``tools/gui_check.py --window``: it opens without
activating, behind the other applications' windows, and lets mouse clicks through to whatever is
behind it (the run sends no real input), and Qt is told never to activate the process when a
window is raised. Every window shown is checked, offscreen too (none modal, none raised); with
a real display the application in front is also read every second, around each ``coppernicus``
command too, and the run fails if this process or a command came to the front, saying in which
step and, for each activation, whether a click caused it. The app's settings live in a temporary
folder for the run, so the estimates' "last solve" lines are the same on every machine.

It never saves a project and takes no board argument: only the projects in ``examples/`` are
used (the power tree page's limits go into a copy in the pages' folder), so no real board can
end up in the docs. Message boxes are answered in code, so a scripted run never waits on one.

Numbers are formatted with the C locale (dot decimals), whatever the machine's locale. Images
are scaled down to at most ``--width`` pixels so the repository stays small. Regenerate them
only when the UI changes, since every PNG stays in the git history.
"""

from __future__ import annotations

import argparse
import faulthandler
import functools
import importlib.util
import multiprocessing
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
DEMO = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
DEMO_BOARD = ROOT / "examples" / "demo" / "demo.kipi.json.gz"
CHAIN = ROOT / "examples" / "power_chain" / "power_chain.coppernicus.yml"
FIELDS = ROOT / "examples" / "fields" / "fields.coppernicus.yml"
DEMO_THERMAL = ROOT / "examples" / "demo" / "demo-thermal.coppernicus.yml"
PDN = ROOT / "examples" / "pdn" / "pdn.coppernicus.yml"
NOISE_TIGHT = ROOT / "examples" / "noise" / "noise-tight.coppernicus.yml"
TOLERANCE_TIGHT = ROOT / "examples" / "tolerance" / "tolerance-tight.coppernicus.yml"
NET = "+3V3"


class Unfinished(Exception):
    """A scenario did not reach the state its image is meant to show."""


@functools.cache
def gui_check():
    """tools/gui_check.py, beside this script: its no-focus rule for real windows and its
    solve-and-wait helpers. Imported by path, so this script runs from anywhere."""
    spec = importlib.util.spec_from_file_location("gui_check", Path(__file__).with_name("gui_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help=f"output folder (default: {IMAGES.relative_to(ROOT)})")
    ap.add_argument("--set", choices=("guide", "features", "analyses", "cli", "reports", "all"),
                    default="guide",
                    help="guide: the README's guide-*.png (default); features: the images of the "
                         "feature pages in docs/; analyses: the Temperature, Impedance, Ripple & noise "
                         "and Tolerance tabs and the Results panel; cli: the command line's PNGs "
                         "(no window); all: those four sets; reports: the HTML pages to capture "
                         "report-*.png from in a browser (no window, no PNG)")
    ap.add_argument("--html-dir", help="--set reports: folder for the pages (default: a new temporary "
                                       "folder, kept and printed)")
    ap.add_argument("--offscreen", action="store_true",
                    help="no window (Qt's offscreen platform), to check that every scenario runs and "
                         "reaches its state; the board view stays blank, so --out is required")
    ap.add_argument("--width", type=int, default=1600, help="largest image width, px")
    ap.add_argument("--timeout", type=float, default=900.0, help="hard limit for the whole run, s")
    args = ap.parse_args(argv)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    on_screen = os.environ.get("QT_QPA_PLATFORM") != "offscreen"
    if (not on_screen and args.set not in ("cli", "reports")
            and (not args.out or Path(args.out).resolve() == IMAGES)):
        ap.error(f"offscreen (--offscreen or QT_QPA_PLATFORM=offscreen) the board view stays blank: "
                 f"give --out a folder other than {IMAGES}")
    gc = gui_check()
    for key, value in gc.background_env(on_screen).items():   # macOS: never the app in front
        os.environ.setdefault(key, value)
    front_before = gc.front_app() if on_screen else None
    faulthandler.dump_traceback_later(args.timeout, exit=True)
    out = Path(args.out) if args.out else IMAGES
    out.mkdir(parents=True, exist_ok=True)
    if args.set == "cli":                    # no window: the command line draws these itself
        try:
            written = cli_pngs(out, args.timeout, watch_front=on_screen)
        except Unfinished as e:
            print(f"cli: {e}", file=sys.stderr)
            return 1
        for path in written:
            print(path)
        return 0
    if args.set == "reports":                # no window: pages for a browser capture by hand
        folder = Path(args.html_dir or tempfile.mkdtemp(prefix="coppernicus-reports-"))
        try:
            pages = reports(folder, args.timeout, watch_front=on_screen)
        except Unfinished as e:
            print(f"reports: {e}", file=sys.stderr)
            return 1
        print(f"Pages in {folder} (kept). Open each in a browser window of the width given, one "
              f"colour scheme for all, and save the part named as {out}/<image>:")
        for name, url, what, width, docs in pages:
            print(f"{name}  ({docs})\n    {url}\n    {width} px wide: {what}")
        return 0

    from PySide6.QtCore import QLocale, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    QLocale.setDefault(QLocale.c())
    from coppernicus.app import ICON
    from coppernicus.app.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Coppernicus")
    app.setWindowIcon(QIcon(str(ICON)))
    # The app's settings (the last solve times the estimates quote, the hover choice) in a
    # fresh folder: the same images on every machine, and the user's settings untouched.
    from coppernicus.app import SETTINGS_DIR_ENV
    settings = os.environ[SETTINGS_DIR_ENV] = tempfile.mkdtemp(prefix="coppernicus-shots-settings-")
    # As gui_check --window: every window opens behind the others without activating, and here
    # it also lets clicks through (the run sends no real mouse input), so a click meant for
    # another application cannot land on it. Offscreen too, where audit() checks every window.
    watch = gc.FrontWatch(app, front_before) if on_screen else None
    background = gc.show_in_background(app, input_through=True)
    answer_dialogs()
    windows = []
    written = []

    def step(text: str):
        if watch is not None:                # what the run is doing, in the focus report
            watch.step = text

    def new_window():
        w = MainWindow()
        w.resize(1500, 920)
        step(f"window {len(windows) + 1}")
        w.show()
        windows.append(w)
        settle()
        if watch is not None:                # once Qt has made its platform window, as gui_check
            watch.shown(w)
        return w

    def settle(n: int = 6):
        for _ in range(n):
            app.processEvents()

    def save(widget, name: str, prefix: str = "guide-"):
        step(f"{prefix}{name}.png")
        settle()
        owner = widget.window()
        if on_screen and isinstance(owner, MainWindow):
            owner.view.plotter._render()     # pyvistaqt defers its draws on macOS: draw the map now
            settle()
        pix = widget.grab()
        if pix.width() > args.width:
            pix = pix.scaledToWidth(args.width, Qt.SmoothTransformation)
        path = out / f"{prefix}{name}.png"
        pix.save(str(path))
        written.append(path)

    def solved(window) -> bool:
        window.solve()
        if window.worker is not None:
            window.worker.wait(int(args.timeout * 1000))
        settle(10)
        return window.results.get(window.net) is not None

    def guide() -> int:
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
        window.dirty = False
        window.close()                       # no window stays on screen for the later sets
        return 0

    def features() -> int:
        from coppernicus.app.copper_thickness import CopperThicknessDialog

        # The power tree of the power chain: the regulator box of a rail, then the whole tree
        # solved at once, the ground net's rise on the board and the tree's tables.
        window = new_window()
        if not window.open_project(CHAIN):
            print("could not open the power-chain project", file=sys.stderr)
            return 1
        window.nets.select(NET)
        settle()
        save(window.rail_panel, "3-regulator", "tree-")
        window.solve_tree()
        if window.tree_worker is not None:
            window.tree_worker.wait(int(args.timeout * 1000))
        settle(10)
        if window.tree is None:
            print("the power tree did not solve", file=sys.stderr)
            return 1
        window.group_box.setCurrentIndex(window.group_box.findData("GND"))
        window.results_tabs.setCurrentWidget(window.tree_panel)
        settle()
        window.view.reset_view()
        window.statusBar().showMessage("Power tree solved: GND shows how far the ground rises above 0 V")
        save(window, "1-ground", "tree-")
        save(window.tree_panel, "2-tables", "tree-")
        window.dirty = False
        window.close()

        # The fields example: its rail comes from KiCad footprint fields, shown read-only, with
        # the project file's own rail of the net ignored (a warning in the banner).
        window = new_window()
        if not window.open_project(FIELDS):
            print("could not open the fields example", file=sys.stderr)
            return 1
        window.nets.select("VCC")
        settle()
        if not solved(window):
            print("the fields example did not solve", file=sys.stderr)
            return 1
        window.view.reset_view()
        window.statusBar().showMessage("VCC from KiCad footprint fields: J1 holds 3.3 V, U1 draws 1 A")
        save(window, "1-window", "fields-")
        save(window.rail_panel, "2-panel", "fields-")
        window.dirty = False
        window.close()

        # What-if on the demo: pin the result as the baseline, set B.Cu to 2 oz in the copper
        # thickness editor, solve again, then the Compare tab and the Δ drop map.
        window = new_window()
        if not window.open_project(DEMO):
            print("could not open the demo project", file=sys.stderr)
            return 1
        window.nets.select(NET)
        settle()
        if not solved(window):
            print("the demo did not solve", file=sys.stderr)
            return 1
        window.pin_baseline()

        def typed(dialog):                   # plays the user in place of the modal loop
            dialog.fields["B.Cu"].setText("0.07")
            dialog.show()
            save(dialog, "1-copper", "whatif-")
            dialog.accept()
            return dialog.result()
        real_exec = CopperThicknessDialog.exec
        CopperThicknessDialog.exec = typed
        try:
            window.rail_panel.copper_edit.click()
        finally:
            CopperThicknessDialog.exec = real_exec
        settle()
        if window.project.settings.copper_thickness_mm != {"B.Cu": 0.07} or not solved(window):
            print("the copper what-if did not solve", file=sys.stderr)
            return 1
        window.results_tabs.setCurrentWidget(window.compare)
        window.field_box.setCurrentIndex(window.field_box.findData("drop"))
        settle()
        window.view.reset_view()
        window.statusBar().showMessage("B.Cu at 0.07 mm (2 oz) against the pinned baseline "
                                       "(the board file's 1 oz)")
        save(window, "2-compare", "whatif-")
        window.field_box.setCurrentIndex(window.field_box.findData("difference"))
        settle()
        window.view.reset_view()
        window.statusBar().showMessage("Δ drop vs baseline: blue where the drop shrank")
        save(window, "3-delta", "whatif-")
        window.dirty = False
        window.close()

        # Two revisions of the demo board (no window): the slot shortened in the second.
        revisions_delta(out / "revisions-delta.png")
        written.append(out / "revisions-delta.png")
        return 0

    def analyses() -> int:
        """A window per analysis tab, each solved on its example, and the Results panel with
        the current-density default's attention rows. Tabs are found by their names, and every
        step checks that it got where it should (Unfinished stops the run)."""
        gc = gui_check()

        def need(ok, what: str):
            if not ok:
                raise Unfinished(what)

        def opened(project: Path, net: str):
            window = new_window()
            need(window.open_project(project), f"could not open {project.name}")
            window.nets.select(net)
            settle()
            need(window.net == net, f"{project.name}: no net {net}")
            return window

        def tab(window, text: str):
            """The Results tab named ``text`` (as the screen shows it: "Ripple & noise"), shown,
            wherever it is in the row of tabs."""
            from coppernicus.app import shown_label
            tabs = window.results_tabs
            names = [shown_label(tabs.tabText(i)) for i in range(tabs.count())]
            need(text in names, f"no {text!r} tab in the Results panel: {names}")
            tabs.setCurrentIndex(names.index(text))
            settle()
            return tabs.currentWidget()

        def results_height(window, px: int):
            """The results page made ``px`` tall, under the map: the pages' plots and tables
            need more than the app's default."""
            split = window.centre_split
            split.setSizes([max(split.height() - px, 100), px])
            settle()
            return split

        def closed(window):
            window.dirty = False
            window.close()

        # Temperature: the thermal demo's power tree solved electro-thermally (Ctrl+T), the board
        # temperature of F.Cu with a double-click's readout on its hottest cell.
        window = opened(DEMO_THERMAL, NET)
        results_height(window, 330)
        tree = gc.solve_tree_and_wait(window, args.timeout)
        need(tree is not None and tree.thermal is not None, "the thermal demo's power tree did not solve")
        panel = tab(window, "Temperature")
        need(panel.shown_layer_rows() and panel.shown_load_rows(), "the Temperature tab shows no rows")
        window.layer_box.setCurrentIndex(window.layer_box.findText("F.Cu"))
        window.field_box.setCurrentIndex(window.field_box.findData("temperature"))
        settle()
        need(window.field_box.currentData() == "temperature" and window.layer_box.currentText() == "F.Cu",
             "the board temperature of F.Cu is not shown (the app fell back to the drop)")
        window.view.reset_view()
        hx, hy, _ = tree.thermal.map("F.Cu").hottest()
        window.view.probe_at(hx, hy)
        save(window, "thermal-1-window", "")
        closed(window)

        # Impedance: Solve impedance on the pdn example (its +3V3 has no DC load, so the view
        # shows the copper): |Z|(f) at probe U1 against its target, and the probe's row.
        window = opened(PDN, NET)
        panel = tab(window, "Impedance")
        results_height(window, 470)
        res, _ = gc.solve_ac_and_wait(window, args.timeout)
        need(res is not None and panel.figure.axes and panel.figure.axes[0].get_lines()
             and panel.table.rowCount() > 0, f"the impedance did not solve: {panel.status.text()}")
        save(window, "ac-1-window", "")
        closed(window)

        # Ripple & noise: noise-tight's VOUT solved (the drop map behind), then Solve noise: the
        # waveforms, the ripple table and the victims, SIG_LOOP over its margin.
        window = opened(NOISE_TIGHT, "VOUT")
        need(solved(window), "noise-tight's VOUT did not solve")
        panel = tab(window, "Ripple & noise")
        results_height(window, 470)
        res, _, _ = gc.solve_noise_and_wait(window, args.timeout)
        need(res is not None and panel.ripple_table.rowCount() > 0 and panel.victim_table.rowCount() > 0
             and panel.wave_figure.axes and all(ax.get_lines() for ax in panel.wave_figure.axes),
             f"the noise did not solve: {panel.status.text()}")
        plots = [panel.plot_tabs.tabText(i) for i in range(panel.plot_tabs.count())]
        need("Waveforms" in plots, f"no Waveforms plot: {plots}")
        panel.plot_tabs.setCurrentIndex(plots.index("Waveforms"))
        save(window, "noise-1-window", "")
        closed(window)

        # Tolerance: tolerance-tight, the tab's Solve pressed until nothing is missing (the power
        # tree, the noise of U1, the impedance with KiPI's transient, each in its own tab), then
        # U2's stack-up and its verdict.
        window = opened(TOLERANCE_TIGHT, "VOUT")
        panel = tab(window, "Tolerance")
        results_height(window, 470)
        for _ in range(6):
            if not panel.solve_button.isEnabled():
                break
            step = panel.solve_button.text()
            panel.solve_next()
            if window.tree_worker is not None:
                window.tree_worker.wait(int(args.timeout * 1000))
            for busy in (window.noise_panel, window.ac_panel):
                gc.wait_idle(busy, args.timeout)
            settle()
            panel = tab(window, "Tolerance")
            settle()
            need(not panel.solve_button.isEnabled() or panel.solve_button.text() != step,
                 f"the Tolerance tab's {step!r} solved nothing: {panel.status.text()}")
        res = panel.result
        need(res is not None and res.verdict in ("pass", "fail") and panel.table.rowCount() > 0
             and not panel.solve_button.isEnabled(), f"the stack-up is not judged: {panel.header.text()}")
        loads = [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]
        need("U2" in loads, f"no U2 row in the stack-up: {loads}")
        said = panel.go_to_row(loads.index("U2"))   # a click on the row: U2 on the board, its verdict
        settle()
        need(said.startswith("U2 on VOUT"), f"the U2 row did not show U2: {said!r}")
        from coppernicus.app.tolerance_panel import RESULT_HEADERS
        head, verdict = panel.table.horizontalHeader(), RESULT_HEADERS.index("Verdict")
        left = head.sectionViewportPosition(verdict)
        need(0 <= left and left + head.sectionSize(verdict) <= panel.table.viewport().width(),
             "the stack-up's Verdict column is not in view")
        save(window, "tolerance-1-window", "")
        closed(window)

        # The Results panel: the thermal demo's +3V3 solved alone (F5), 15.2 A through the demo
        # board: crowding above the current-density default, amber ATTENTION rows.
        window = opened(DEMO_THERMAL, NET)
        need(solved(window), "the thermal demo's +3V3 did not solve")
        tab(window, "Problems")
        dock = results_height(window, 420)
        rows = window.problems.tree
        attention = [rows.topLevelItem(i) for i in range(rows.topLevelItemCount())
                     if rows.topLevelItem(i).text(0) == "ATTENTION"]
        need(attention, "no ATTENTION row in the Problems tab")
        rows.scrollToItem(attention[0])
        settle()
        need(rows.viewport().rect().contains(rows.visualItemRect(attention[0])),
             "the first ATTENTION row is not in view")
        save(dock, "results-attention", "")
        closed(window)
        return 0

    try:
        status = guide() if args.set in ("guide", "all") else 0
        if status == 0 and args.set in ("features", "all"):
            status = features()
        if status == 0 and args.set in ("analyses", "all"):
            try:
                status = analyses()
            except Unfinished as e:
                print(f"analyses: {e}", file=sys.stderr)
                status = 1
        check = gc.Checks()
        background.audit(check)
        if watch is not None:
            watch.finish(check)
        if check.failed:
            print("a window could take the focus, or the run came to the front: see the FAIL lines "
                  "above", file=sys.stderr)
            status = status or 1
        if status:
            return status
    finally:
        for w in windows:
            w.dirty = False                  # never ask to save
            w.close()
        settle()
        shutil.rmtree(settings, ignore_errors=True)

    if args.set == "all":
        try:
            written.extend(cli_pngs(out, args.timeout, watch_front=on_screen))
        except Unfinished as e:
            print(f"cli: {e}", file=sys.stderr)
            return 1
    for path in written:
        print(path)
    return 0


# The command line's own PNGs that the docs show: (the image, the arguments of `coppernicus`, the
# file it writes into --out, as a glob). Each example fails a limit on purpose: exit code 1.
CLI_PNGS = (
    ("thermal-map.png", ("solve", DEMO_THERMAL, "--thermal"), "*_temperature_F.Cu.png"),
    ("ac-impedance.png", ("ac", PDN), "*_P3V3_impedance.png"),
    ("noise-waveforms.png", ("noise", NOISE_TIGHT), "*_U1_noise.png"),
)


def run_child(cmd, timeout: float, watch_front: bool = False):
    """Run ``cmd`` with matplotlib's file-only backend (Agg) and the run's environment (on
    macOS with a real display, Qt's no-focus switches). With ``watch_front`` the application in
    front is read every half second while it runs: the run stops (:class:`Unfinished`) if the
    child or this process ever came to the front. Returns (exit code, stdout, stderr, the
    number of front samples read)."""
    import subprocess
    import time
    front_app = gui_check().front_app
    what = " ".join(map(str, cmd[1:]))
    env = dict(os.environ, MPLBACKEND="Agg")
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True, env=env)
        fronts, t0 = [], time.monotonic()
        while True:
            try:
                proc.wait(timeout=0.5 if watch_front else timeout)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - t0 > timeout:
                    proc.kill()
                    proc.wait()
                    raise Unfinished(f"{what} took more than {timeout:.0f} s") from None
                if watch_front:
                    fronts.append(front_app())
        if watch_front:
            fronts.append(front_app())
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(), err.read()
    came = [f for f in fronts if f and f[1] in (proc.pid, os.getpid())]
    if came:
        raise Unfinished(f"{what} came to the front: {came[0][0]} (pid {came[0][1]}) in {len(came)} of "
                         f"{len(fronts)} sample(s)")
    return proc.returncode, stdout, stderr, sum(1 for f in fronts if f)


def coppernicus(args, out_dir: Path, timeout: float, watch_front: bool = False) -> int:
    """``coppernicus ARGS --out OUT_DIR`` in a child process, as a user runs it (see
    :func:`run_child`). Exit codes 0, 1 (a limit exceeded) and 3 (not judged) are results;
    anything else stops the run. Returns the number of front samples read."""
    cmd = [sys.executable, "-m", "coppernicus", *map(str, args), "--out", str(out_dir)]
    code, _, stderr, read = run_child(cmd, timeout, watch_front)
    if code not in (0, 1, 3):
        raise Unfinished(f"coppernicus {' '.join(map(str, args))} exited with {code}: "
                         f"{stderr.strip()[-800:]}")
    return read


def _never_in_front(commands: int, read: int, watch_front: bool):
    if watch_front:
        print(f"[INFO] the {commands} coppernicus command(s) never came to the front "
              f"({read} front sample(s) read while they ran)", flush=True)


def cli_pngs(out: Path, timeout: float, watch_front: bool = False) -> list:
    """The PNGs of :data:`CLI_PNGS`, each written by its command into a temporary folder (never
    ``results/`` beside an example) and copied to ``out``."""
    written, read = [], 0
    with tempfile.TemporaryDirectory(prefix="coppernicus-shots-cli-") as tmp:
        for name, args, pattern in CLI_PNGS:
            folder = Path(tmp) / Path(name).stem
            read += coppernicus(args, folder, timeout, watch_front)
            found = sorted(folder.glob(pattern))
            if len(found) != 1:
                raise Unfinished(f"{name}: expected one {pattern} from coppernicus {args[0]}, found "
                                 f"{sorted(p.name for p in folder.iterdir())}")
            shutil.copyfile(found[0], out / name)
            written.append(out / name)
    _never_in_front(len(CLI_PNGS), read, watch_front)
    return written


# The HTML pages behind the report-*.png images: (the image, the arguments of `coppernicus`, the
# fragment to open the page at, what to capture, the browser width of the image in the docs, the
# docs that show it). The repository has no headless browser: the pages are captured by hand.
REPORTS = (
    ("report-demo.png", ("solve", DEMO, "--html"), "",
     "the top of the page, with a hover readout on the B.Cu drop map", 1360,
     "README.md, docs/html-report.md, docs/agents.md"),
    ("report-tree.png", ("solve", "CHAIN_WITH_LIMITS", "--tree", "--html"), "#group=+3V3",
     "the top of the page down to the Problems table", 1280, "docs/html-report.md"),
    ("report-thermal.png", ("solve", DEMO_THERMAL, "--thermal", "--html"), "",
     "the Board temperature section, from its heading to 'Assumptions of the board temperature'",
     1280, "docs/html-report.md"),
    ("report-ac.png", ("ac", PDN, "--html"), "", "the whole page", 1200, "docs/html-report.md"),
    ("report-noise.png", ("noise", NOISE_TIGHT, "--html"), "",
     "from the title down to the radiated estimate", 900, "docs/html-report.md, docs/noise.md"),
    ("report-tolerance.png", ("tolerance", TOLERANCE_TIGHT, "--html"), "", "the whole page", 1150,
     "docs/html-report.md, docs/tolerance.md"),
)


def chain_with_limits(folder: Path) -> Path:
    """The power chain with the limits docs/power-tree.md gives it for the example (1 % drop on
    +3V3 and +1V8, 30 mV of ground rise on GND), saved in ``folder`` under the example's name,
    beside a copy of its board, so that the page names the example's files."""
    from coppernicus.project import Project
    project = Project.load(CHAIN)
    project.rail("+3V3").limits.max_drop_pct = 1.0
    project.rail("+1V8").limits.max_drop_pct = 1.0
    project.ensure_rail("GND").limits.max_ground_rise_mv = 30.0
    folder.mkdir(parents=True, exist_ok=True)
    board = folder / project.board_path().name
    shutil.copyfile(project.board_path(), board)
    project.board = str(board)
    return project.save(folder / CHAIN.name)


def reports(folder: Path, timeout: float, watch_front: bool = False) -> list:
    """Each page of :data:`REPORTS` written into its own subfolder of ``folder``. Returns
    (image, page URL with its fragment, what to capture, width, docs) per page. A page that
    names a local folder is refused: it would end up in a public image."""
    pages, read = [], 0
    private = [str(p) for p in (Path.home(), folder.resolve(), ROOT)]
    for name, args, fragment, what, width, docs in REPORTS:
        sub = folder / Path(name).stem
        if "CHAIN_WITH_LIMITS" in args:
            args = tuple(chain_with_limits(sub / "project") if a == "CHAIN_WITH_LIMITS" else a for a in args)
        read += coppernicus(args, sub, timeout, watch_front)
        found = sorted(sub.glob("*.html"))
        if len(found) != 1:
            raise Unfinished(f"{name}: expected one page from coppernicus {args[0]}, found "
                             f"{[p.name for p in found]}")
        text = found[0].read_text(encoding="utf-8")
        named = [p for p in private if p in text]
        if named:
            raise Unfinished(f"{found[0]} names a local folder ({named[0]}): not for a public image")
        pages.append((name, found[0].resolve().as_uri() + fragment, what, width, docs))
    _never_in_front(len(REPORTS), read, watch_front)
    return pages


def answer_dialogs():
    """Message boxes would block a scripted run: print them and answer in code."""
    from PySide6.QtWidgets import QMessageBox

    def say(kind):
        def box(_parent, title, text, *_a, **_k):
            print(f"{kind}: {title}: {text}", file=sys.stderr)
            return QMessageBox.Discard
        return staticmethod(box)
    for kind in ("critical", "warning", "information", "question"):
        setattr(QMessageBox, kind, say(kind))


def demo_after():
    """The demo board's next revision: the slot in the B.Cu plane ends 7 mm earlier, so the
    neck below it is wider. Everything else is the demo board's (``demo_board``)."""
    from shapely.geometry import box

    from coppernicus.engine.synthetic import B, F, SyntheticBoard, two_layer_stackup
    b = SyntheticBoard("demo", two_layer_stackup())
    b.zone("+3V3", B, box(2, 2, 58, 38).difference(box(28, 2, 31, 24)))
    b.square_pad("J1", "1", "+3V3", 6.0, 20.0, 4.0, value="SUPPLY")
    for dx, dy in ((-1.2, -1.2), (1.2, -1.2), (-1.2, 1.2), (1.2, 1.2)):
        b.via("+3V3", 6.0 + dx, 20.0 + dy)
    for ref, (x, y) in (("U1", (50.0, 10.0)), ("U2", (50.0, 28.0))):
        b.square_pad(ref, "1", "+3V3", x, y, 2.5, value="LOAD")
        b.via("+3V3", x - 0.7, y)
        b.via("+3V3", x + 0.7, y)
    b.square_pad("U3", "1", "+3V3", 18.0, 33.0, 1.5, value="LOAD")
    b.via("+3V3", 18.0, 26.0)
    b.track("+3V3", F, 0.25, (18.0, 26.0), (18.0, 33.0))
    return b.snapshot(outline=box(0, 0, 60, 40))


def revisions_delta(path: Path) -> None:
    """compare_revisions on the demo board and :func:`demo_after`, with the demo project's
    +3V3 rail: the B.Cu map of the drop change, copied to ``path``. The boards, results and
    maps live in a temporary folder that is deleted afterwards."""
    from coppernicus.engine.synthetic import demo_board, save_snapshot
    from coppernicus.project import Project
    from coppernicus.service import Session
    rail = Project.load(DEMO).rail(NET)
    sources = [{"ref": s.ref, "voltage_v": s.voltage} for s in rail.sources]
    loads = [{"ref": ld.ref, "current_a": ld.current} for ld in rail.loads]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        session = Session(out_dir=tmp / "out")
        ids = []
        for name, board in (("before", demo_board()), ("after", demo_after())):
            save_snapshot(board, tmp / f"{name}.kipi.json.gz")
            board_id = session.open_board(tmp / f"{name}.kipi.json.gz")["board_id"]
            ids.append(session.solve_rail(board_id, NET, sources, loads)["result_id"])
        doc = session.compare_revisions(*ids, out_dir=tmp / "maps")
        [b_cu] = [m for m in doc["maps"] if "B_Cu" in Path(m).name or "B.Cu" in Path(m).name]
        shutil.copyfile(b_cu, path)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
