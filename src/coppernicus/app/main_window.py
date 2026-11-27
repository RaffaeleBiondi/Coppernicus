"""Main window: one loop — open a board, pick a net, add sources and loads, solve, read the
maps and the ranked problems, click a problem to go there.

Layout: nets on the left, the 2D board view in the centre (layer and field pickers on
top), the rail setup on the right, problems and notes at the bottom.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QLocale, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from coppernicus import __version__, bootstrap_triangle
from coppernicus import board_fields as BF
from coppernicus.app import fields_view, label_text, theme
from coppernicus.app.ac_panel import AcPanel
from coppernicus.app.board_view import FIELDS, BoardView, copper_rings
from coppernicus.app.comparing import Run, compare_runs
from coppernicus.app.noise_panel import NoisePanel
from coppernicus.app.panels import (
    ComparePanel,
    LossChart,
    NetPanel,
    ProblemsPanel,
    RailPanel,
    TreePanel,
)
from coppernicus.app.picking import add_picked_pin, pick_nets
from coppernicus.app.thermal_panel import CANCELLING, NO_MAP, ThermalPanel, record_solve
from coppernicus.app.tolerance_panel import TolerancePanel, read_misspelt
from coppernicus.app.worker import CANCELLED, ReportWorker, SolveWorker, TreeWorker
from coppernicus.compare import difference_layer, same_mesh
from coppernicus.engine import Board, EngineError, tied_nets
from coppernicus.engine.board import board_file_filter
from coppernicus.engine.kicad_fields import read_board_fields
from coppernicus.engine.pisetup import ground_nets
from coppernicus.power_tree import (
    apply_limits,
    group_limits,
    group_verdict,
    has_limits,
    not_judged_text,
    rating_origins,
    tree_summary,
    tree_verdicts,
    unsolved_limits,
)
from coppernicus.problems import ATTENTION, FAIL, find_problems, max_drop_text
from coppernicus.project import SOURCE_ACCURACY_KEYS, Limits, Project, ProjectError, Rail
from coppernicus.results import RailResult, TreeResult
from coppernicus.service.convert import not_judged_items
from coppernicus.service.session import FIELD_HINT
from coppernicus.temperature import apply_thermal_limits, thermal_verdict

PROJECT_FILTER = "Coppernicus project (*.coppernicus.yml *.yml *.yaml)"
USER_RESULTS = ("Documents", "Coppernicus", "results")     # under the home folder
REMOVE_MESHER = "Remove downloaded mesher (Triangle)…"
THEME_KEY = "Ctrl+Shift+L"                              # dark <-> light (View menu, toolbar)
# + / − step the Layer selector, as in KiCad: (key, modifiers, step); "=" is "+" without Shift.
LAYER_KEYS = ((Qt.Key_Plus, Qt.NoModifier, 1), (Qt.Key_Equal, Qt.NoModifier, 1),
              (Qt.Key_Plus, Qt.KeypadModifier, 1), (Qt.Key_Minus, Qt.NoModifier, -1),
              (Qt.Key_Minus, Qt.KeypadModifier, -1))
LAYER_TIP = "The copper layer shown. + / − (also = and the keypad's + / −): next / previous layer"


def remove_mesher_shown() -> bool:
    """Help → Remove downloaded mesher (Triangle)… exists only in the packaged app, which
    downloaded Triangle into a per-user folder itself (coppernicus.bootstrap_triangle).
    Elsewhere triangle comes from the Python environment and is not the app's to delete."""
    return bootstrap_triangle.frozen()


def _writable_folder(folder: Path) -> bool:
    """``folder`` exists (created if missing) and can be written."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(folder, os.W_OK)


def report_folder(project_dir: Path, home: Optional[Path] = None) -> Tuple[Path, str]:
    """Where a project's report goes by default, and a note when it is not the usual place
    ("" otherwise): results/ next to the project (the CLI's folder), created if missing; when
    that cannot be created or written (a project opened from a read-only disk image), the
    per-user ~/Documents/Coppernicus/results, created on demand."""
    folder = Path(project_dir) / "results"
    if _writable_folder(folder):
        return folder, ""
    user = (Path.home() if home is None else Path(home)).joinpath(*USER_RESULTS)
    _writable_folder(user)                     # if even this fails, the Save dialog says so
    return user, f"{folder} cannot be written, so reports go to {user}"


def tree_report_path(path: Path) -> Path:
    """The power tree's page beside the rails' report ``path``, with the CLI's "_tree_report"
    suffix: "demo_report.html" → "demo_tree_report.html", any other "name.html" →
    "name_tree_report.html" (never ``path`` itself)."""
    path = Path(path)
    stem = path.stem[: -len("_report")] if path.stem.endswith("_report") else path.stem
    return path.with_name(f"{stem}_tree_report{path.suffix or '.html'}")


def typing_widget(widget) -> bool:
    """Whether keys pressed in ``widget`` are typed into it: an editable text field, spin box or
    combo box, or a table whose cells start editing on a key (or one being edited). The layer
    keys (:data:`LAYER_KEYS`) are off there, so the key reaches it."""
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return not widget.isReadOnly()
    if isinstance(widget, QComboBox):
        return widget.isEditable()
    if isinstance(widget, QAbstractItemView):
        return (widget.state() == QAbstractItemView.EditingState
                or bool(widget.editTriggers() & QAbstractItemView.AnyKeyPressed))
    return False


def initial_size(avail_w: Optional[int], avail_h: Optional[int]) -> Tuple[int, int]:
    """The window's first size: 1500 × 920, shrunk to the screen's available area (a 1440 ×
    900 laptop) when one is known."""
    return (min(1500, avail_w) if avail_w else 1500, min(920, avail_h) if avail_h else 920)


def tree_i_max(tree: TreeResult) -> Dict[str, Optional[float]]:
    """Each VRM's rated output current (I max) by name: the value the tree judged
    (``VrmReport.current_limit_a``, from the merged project's ``Source.current_limit_a``: KiCad
    field PI_IMAX or the project file; None where not given), also for a label used on several
    rails ("U1 (+3V3)")."""
    return {v.name: v.current_limit_a for v in tree.vrms}


def verdict_html(verdict: str, fails: int, not_judged, has_limits: bool) -> str:
    """A power-tree verdict for the summary lines: what failed, and what a limit set could not
    judge (a group passes only when everything was judged)."""
    missing = (theme.span("warn", f"<b>not judged</b>: {not_judged_text(not_judged)}")
               if not_judged else "")
    if verdict == "fail":
        text = theme.span("fail", f"<b>{fails} limit(s) exceeded</b>")
        return f"{text} · {missing}" if missing else text
    if verdict == "pass":
        return "no limits exceeded"
    if has_limits:
        return missing or theme.span("warn", "<b>limits set, nothing judged</b>")
    return "no limits set: nothing is judged"


def attention_html(problems) -> str:
    """The count of attention items (spots above Coppernicus's current-density default), apart
    from the verdict: " · 2 attention (…)", or "" when there are none."""
    n = sum(p.severity == ATTENTION for p in problems)
    if not n:
        return ""
    return " · " + theme.span("warn", f"<b>{n} attention</b> (current density above Coppernicus's "
                              "default, not a verdict)")


def tree_header_html(key: str, res: RailResult, gv) -> str:
    """The Problems header of group ``key`` of a power tree (``gv``: its GroupVerdict)."""
    verdict = verdict_html(gv.verdict, gv.failed, gv.not_judged, has_limits(gv))
    return (f"<b>{key}</b> ({res.kind} of the power tree: {' + '.join(res.nets)}): {max_drop_text(res)} "
            f"· copper loss {res.power_loss * 1e3:.3g} mW · {verdict}{attention_html(gv.problems)}")


def rail_header_html(res: RailResult, limits: Limits, problems) -> str:
    """The Problems header of a rail solved alone, judged against ``limits`` (``problems``:
    its find_problems list)."""
    fails = sum(p.severity == FAIL for p in problems)
    shares = res.loss_breakdown()
    total = sum(shares.values()) or 1.0
    split = ", ".join(f"{k} {v / total:.0%}" for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                      if v / total >= 0.01)
    worst = max(res.loads, key=res.drop) if res.loads else None     # by mV: a % is NaN at 0 V
    pct = res.drop_pct(worst) if worst else float("nan")
    share = f", {pct:.2f} %" if pct == pct else ""
    worst_txt = (f"worst load <b>{worst.name}</b> {res.worst_v(worst):.4f} V "
                 f"({res.drop(worst) * 1e3:.2f} mV{share}) · ") if worst else ""
    missing = not_judged_items(res, limits)      # the per-rail rule (service), one source
    set_any = any(v is not None for v in vars(limits).values())
    verdict = verdict_html("fail" if fails else "not_judged" if missing or not set_any else "pass",
                           fails, missing, set_any)
    return (f"<b>{res.net}</b>: {worst_txt}{max_drop_text(res)} · copper loss "
            f"{res.power_loss * 1e3:.3g} mW ({split}) · {verdict}{attention_html(problems)}")


def saved_rail(rail: Rail) -> bool:
    """Whether a project rail is written to the file: one picked but never set up is not. A
    rail is kept for anything it says: sources or loads, a limit (a ground net's ground-rise
    limit, or limits added to a rail from the KiCad fields), the net-tie option off, or
    override_board_fields (an override emptied of its sources and loads too)."""
    return bool(rail.sources or rail.loads or any(v is not None for v in vars(rail.limits).values())
                or not rail.follow_net_ties or rail.override_board_fields)


class MainWindow(QMainWindow):
    def __init__(self):
        # One number format whatever the system locale: a dot for decimals and no group
        # separators, as in the limits, the problems list and the project file. Must be set
        # before any numeric widget exists.
        QLocale.setDefault(QLocale.c())
        super().__init__()
        theme.apply(theme.saved())                   # dark unless the user chose light
        self._theme = theme.current()
        self.setWindowTitle(f"Coppernicus {__version__}")
        screen = self.screen()
        free = screen.availableGeometry() if screen is not None else None
        self.resize(*initial_size(free.width() if free else None, free.height() if free else None))
        self.project = Project()
        self.board: Optional[Board] = None
        self.net: Optional[str] = None
        self.results: Dict[str, RailResult] = {}
        self.worker: Optional[SolveWorker] = None
        self.report_worker: Optional[ReportWorker] = None
        self._report_fallback: Optional[Tuple[Path, str]] = None    # (folder, note): see report_folder
        self.dirty = False
        self.last_render_s = 0.0
        self._candidates: Dict[Tuple, List[str]] = {}      # pickable pad ids per set of nets
        self._generation = 0          # +1 per opened board: a solve started before is stale
        self.runs: Dict[str, Run] = {}            # the latest solve per net, with its inputs
        self.baseline: Optional[Run] = None       # pinned "before"; survives opening a board
        self._board_stamp = ""                    # which board file, and which version of it
        self._diff_cache: Optional[Tuple] = None      # (baseline, run, layer, (Δ mV, note))
        self._fallback_reason = ""                     # why the Δ map was not shown, last draw
        self.tree: Optional[TreeResult] = None          # the last power-tree solve, if still valid
        self.tree_i_max: Dict[str, Optional[float]] = {}    # its VRMs' I max (tree_i_max)
        self._revision = 0            # +1 per edit of the setup: a solve started before is stale
        self.tree_worker: Optional[TreeWorker] = None
        self._rail_draft: Optional[Rail] = None       # the shown net's rail while it is set up
        self._kicad_shown = False                     # the panel shows a rail from KiCad fields
        # The board's KiCad footprint fields (read at open) and the project merged with them,
        # as the CLI merges them: what is solved, drawn and judged. self.project stays the
        # user's own project, the only thing saved.
        self.board_fields: Optional[BF.BoardFields] = None
        self._fields_read_error: Optional[str] = None
        self._tie_pairs: List[Tuple[str, str]] = []     # the board's net ties, for the merge
        self.fields_error: Optional[str] = None      # set: solves refuse, with this text
        self.merged = BF.Merged(self.project)
        self.last_remerge_s = 0.0
        self.last_fields_read_s = 0.0
        # The misspelt tolerance keys of the project file as loaded, (warning, not-judged entry)
        # each: loading ignores them and a save drops them, so the Tolerance tab keeps them.
        self.misspelt: List[Tuple[str, dict]] = []

        # Centre: pickers + view.
        self.view = BoardView()
        self.group_box = QComboBox()
        self.group_box.addItem("Rail solved alone", None)
        self.group_box.setToolTip("After Solve tree: show a rail or ground group of the tree")
        self.layer_box = QComboBox()
        self.layer_box.setToolTip(LAYER_TIP)
        self.field_box = QComboBox()
        for key, (label, _cmap) in FIELDS.items():
            self.field_box.addItem(label, key)
        self.show_vias = QCheckBox("Vias")
        self.show_vias.setChecked(True)
        self.show_isolines = QCheckBox("Isolines")
        self.show_isolines.setToolTip("Lines of equal voltage drop, the same levels on every layer")
        self.show_arrows = QCheckBox("Arrows")
        self.show_arrows.setToolTip("Direction of the current; arrow length grows with √|J|")
        self.measure = QPushButton("Measure ΔV")
        self.measure.setCheckable(True)
        self.measure.setToolTip("Click A, then B: ΔV A→B = V(A) − V(B). Esc stops measuring.")
        fit = QPushButton("Fit")
        fit.clicked.connect(self.view.reset_view)
        bar = QHBoxLayout()                     # two rows: the window must fit a laptop screen
        bar.addWidget(QLabel("Group"))
        bar.addWidget(self.group_box)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Layer"))
        bar.addWidget(self.layer_box)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Show"))
        bar.addWidget(self.field_box)
        bar.addStretch(1)
        bar2 = QHBoxLayout()
        bar2.addWidget(self.show_vias)
        bar2.addWidget(self.show_isolines)
        bar2.addWidget(self.show_arrows)
        bar2.addSpacing(12)
        bar2.addWidget(self.measure)
        hint = QLabel("<small>double-click: read values</small>")
        hint.setToolTip("Drag to pan, wheel to zoom, double-click to read V, drop and |J|")
        hint.setWordWrap(True)                  # may wrap: the bar must fit a laptop screen
        hint.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hint.setMinimumWidth(80)                # at most two lines
        bar2.addWidget(hint, 1)
        bar2.addWidget(fit)
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(4, 4, 4, 0)
        cl.addLayout(bar)
        cl.addLayout(bar2)
        cl.addWidget(self.view, 1)
        self.setCentralWidget(centre)

        # Docks.
        self.nets = NetPanel()
        self.rail_panel = RailPanel()
        self.problems = ProblemsPanel()
        self.losses = LossChart()
        self.compare = ComparePanel()
        self.tree_panel = TreePanel()
        self.results_tabs = QTabWidget()
        self.results_tabs.addTab(self.problems, "Problems")
        self.results_tabs.addTab(self.losses, "Loss by layer")
        self.thermal_panel = ThermalPanel()               # the board temperature (power tree)
        self.results_tabs.addTab(self.thermal_panel, "Temperature")
        self.thermal_panel.options_changed.connect(self._thermal_edited)
        self.thermal_panel.limits_changed.connect(self._thermal_limits_edited)
        self.thermal_panel.layer_requested.connect(self.show_temperature_at)
        self.thermal_panel.cancel_requested.connect(self.cancel_tree)
        self.results_tabs.addTab(self.compare, "Compare")
        self.results_tabs.addTab(self.tree_panel, "Power tree")
        self.ac_panel = AcPanel(self)
        self.results_tabs.addTab(self.ac_panel, "Impedance")
        self.noise_panel = NoisePanel(self)              # DC-DC ripple and coupled noise
        self.results_tabs.addTab(self.noise_panel, label_text("Ripple & noise"))   # "&&": not a mnemonic
        self.tolerance_panel = TolerancePanel(self)      # each load's stack-up, from the results held
        self.results_tabs.addTab(self.tolerance_panel, "Tolerance")
        for panel in (self.ac_panel, self.noise_panel):
            panel.results_changed.connect(self.tolerance_panel.schedule_refresh)
        self._dock("Nets", self.nets, Qt.LeftDockWidgetArea)
        rail_scroll = QScrollArea()             # the rail setup is taller than a laptop screen
        rail_scroll.setWidget(self.rail_panel)
        rail_scroll.setWidgetResizable(True)
        rail_scroll.setFrameShape(QScrollArea.NoFrame)
        rail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)     # scroll down only
        rail_scroll.setMinimumWidth(self.rail_panel.minimumSizeHint().width()
                                    + rail_scroll.verticalScrollBar().sizeHint().width())
        self._dock("Rail setup", rail_scroll, Qt.RightDockWidgetArea)
        problems_dock = self._dock("Results", self.results_tabs, Qt.BottomDockWidgetArea)
        self.resizeDocks([problems_dock], [300], Qt.Vertical)

        self._actions()
        self.statusBar().showMessage("Open a board (.kicad_pcb) or a project to start")

        self.nets.net_selected.connect(self.select_net)
        self.nets.marked_changed.connect(self._marked_edited)
        self.rail_panel.rail_changed.connect(self._rail_edited)
        self.rail_panel.limits_changed.connect(self._limits_edited)
        self.rail_panel.settings_changed.connect(self._settings_edited)
        self.rail_panel.solve_requested.connect(self.solve)
        self.problems.problem_activated.connect(self.go_to_problem)
        self.layer_box.currentIndexChanged.connect(lambda *_: self.refresh_view())
        self.group_box.currentIndexChanged.connect(lambda *_: self._group_changed())
        self.tree_panel.group_requested.connect(self.show_group)
        self.tree_panel.reference_requested.connect(self._tree_reference_requested)     # "0 V at"
        self.tree_panel.pick_toggled.connect(self._reference_pick_toggled)
        self.field_box.currentIndexChanged.connect(lambda *_: self.refresh_view())
        self.show_vias.toggled.connect(lambda *_: self.refresh_view())
        self.show_isolines.toggled.connect(lambda *_: self.refresh_view())
        self.show_arrows.toggled.connect(lambda *_: self.refresh_view())
        self.measure.toggled.connect(self._measure_toggled)
        self.view.probed.connect(lambda text: self.statusBar().showMessage(text))
        self.view.pad_picked.connect(self.pick_pad)
        self.view_menu = self.menuBar().addMenu("View")
        self.view.add_hover_menu(self.view_menu)                       # View → Hover shows
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.theme_action)
        theme.signals.changed.connect(self._theme_changed)
        self.compare.pin_requested.connect(self.pin_baseline)
        self.compare.clear_requested.connect(self.clear_baseline)
        self.rail_panel.pick_changed.connect(self._pick_mode_changed)
        self.rail_panel.banner.action_requested.connect(self._banner_action)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.stop_clicking)
        self._layer_keys = [QShortcut(QKeySequence(mods | key), self,
                                      activated=lambda s=step: self.step_layer(s))
                            for key, mods, step in LAYER_KEYS]
        QApplication.instance().focusChanged.connect(self._focus_changed)

    def step_layer(self, step: int):
        """+ / − (as in KiCad): the next (``step`` 1) or previous (−1) copper layer in the Layer
        selector, round from the last to the first; nothing without layers."""
        n = self.layer_box.count()
        if n:
            self.layer_box.setCurrentIndex((self.layer_box.currentIndex() + step) % n)

    def _focus_changed(self, _old, new):
        """The layer keys are off while the focus is where keys are typed (:func:`typing_widget`):
        a disabled shortcut leaves the key to the widget, so "+3V3" can be typed in a cell."""
        on = not typing_widget(new)
        for shortcut in self._layer_keys:
            shortcut.setEnabled(on)

    def _dock(self, title, widget, area):
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(area, dock)
        return dock

    def _actions(self):
        tb = self.addToolBar("Main")
        tb.setObjectName("Main")
        tb.setMovable(False)
        file_actions = []
        for text, shortcut, slot in (("Open board…", QKeySequence.Open, self.open_board_dialog),
                                     ("Open project…", "Ctrl+Shift+O", self.open_project_dialog),
                                     ("Save project", QKeySequence.Save, self.save_project),
                                     ("Save project as…", QKeySequence.SaveAs, self.save_project_as),
                                     (None, None, None),
                                     ("Solve rail", "F5", self.solve),
                                     ("Solve tree", "Ctrl+T", self.solve_tree),
                                     ("Pin baseline", "Ctrl+B", self.pin_baseline),
                                     ("Export maps…", "Ctrl+E", self.export_maps),
                                     ("Export report…", "Ctrl+R", self.export_report)):
            if text is None:
                tb.addSeparator()
                continue
            act = QAction(text, self)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            tb.addAction(act)
            if text.startswith(("Open", "Save")):
                file_actions.append(act)
        self.ac_panel.add_solve_action(tb)            # "Solve impedance" (Ctrl+I), the Impedance tab's
        # Menus: the toolbar's file actions (the same actions, so one shortcut each), and the
        # KiCad footprint fields.
        self.file_menu = self.menuBar().addMenu("&File")
        self.file_menu.addActions(file_actions)
        self.file_menu.addSeparator()
        self.reload_action = QAction("Reload board", self)
        self.reload_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.reload_action.setToolTip("Read the board file again (and its KiCad footprint fields), "
                                      "keeping the project; results are dropped")
        self.reload_action.triggered.connect(self.reload_board)
        self.file_menu.addAction(self.reload_action)
        self.project_menu = self.menuBar().addMenu("&Project")
        self.use_fields_action = QAction("Use KiCad footprint fields", self)
        self.use_fields_action.setCheckable(True)
        self.use_fields_action.setChecked(True)
        self.use_fields_action.setToolTip("Take rails from the board's PI_* footprint fields "
                                          "(use_board_fields in the project file)")
        self.use_fields_action.toggled.connect(self._use_fields_toggled)
        self.project_menu.addAction(self.use_fields_action)
        if remove_mesher_shown():
            self.help_menu = self.menuBar().addMenu("&Help")
            remove = QAction(REMOVE_MESHER, self)
            remove.setMenuRole(QAction.NoRole)          # macOS: stays in Help
            remove.setToolTip("Delete the Triangle mesher this app downloaded; it is downloaded "
                              "again on the next start")
            remove.triggered.connect(self.remove_mesher)
            self.help_menu.addAction(remove)
        self.noise_panel.add_solve_action(tb)         # "Solve noise" (Ctrl+Shift+N), the Ripple & noise tab's
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.theme_action = QAction(self)
        self.theme_action.setShortcut(QKeySequence(THEME_KEY))
        self.theme_action.triggered.connect(self.toggle_theme)
        self._show_theme_action()
        tb.addAction(self.theme_action)               # at the right end, as the report's button

    # ------------------------------------------------------------------ theme
    def toggle_theme(self):
        """Switch dark <-> light and keep the choice for the next start."""
        new = "light" if theme.current() == "dark" else "dark"
        theme.save(new)
        theme.apply(new)

    def _theme_changed(self, new: str):
        """Colours already written in cells and rich-text labels follow the new theme."""
        theme.recolor(self, self._theme, new)
        self._theme = new
        self._show_theme_action()

    def _show_theme_action(self):
        light = theme.current() == "dark"            # what the button switches to
        self.theme_action.setText("☀ Light" if light else "☾ Dark")
        self.theme_action.setToolTip(f"Switch to the {'light' if light else 'dark'} theme "
                                     f"({QKeySequence(THEME_KEY).toString(QKeySequence.NativeText)})")

    # ------------------------------------------------------------------ files
    def open_path(self, path):
        path = Path(path)
        if path.name.endswith((".coppernicus.yml", ".yml", ".yaml")):
            self.open_project(path)
        else:
            self.open_board(path)

    def open_board_dialog(self):
        if not self.confirm_discard("opening another board"):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open board", "", board_file_filter())
        if path:
            self.open_board(path)

    def open_project_dialog(self):
        if not self.confirm_discard("opening another project"):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", PROJECT_FILTER)
        if path:
            self.open_project(path)

    def confirm_discard(self, before: str = "closing") -> bool:
        """Before the project is dropped (closing, opening another board or project): offer
        to save unsaved edits. True to go on; False to stay, on Cancel or when the save did
        not happen (its dialog was cancelled or it failed)."""
        if not (self.dirty and (self.project.rails or self.project.path or self.project.marked_rails)):
            return True
        answer = QMessageBox.question(self, "Unsaved changes", f"Save the project before {before}?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            self.save_project()
            return not self.dirty
        return True

    def open_board(self, path, project: Optional[Project] = None,
                   misspelt: Optional[List[Tuple[str, dict]]] = None) -> bool:
        """Open the board at ``path``, with ``project`` (None: a new one). ``misspelt``: the
        project file's misspelt tolerance keys (None with a project: kept, as a reload does)."""
        self.statusBar().showMessage(f"Reading {Path(path).name}…")
        self.repaint()
        try:
            board = Board.open(path)
        except EngineError as e:
            QMessageBox.critical(self, "Cannot open board", str(e))
            return False
        self.board = board
        if project is None or misspelt is not None:
            self.misspelt = list(misspelt or [])
        self.project = project or Project(board=str(Path(path).resolve()))
        self._read_board_fields(board)
        self._remerge()
        self._show_fields_error()
        self.use_fields_action.blockSignals(True)
        self.use_fields_action.setChecked(self.project.use_board_fields)
        self.use_fields_action.blockSignals(False)
        resolved = Path(path).resolve()
        modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(resolved.stat().st_mtime))
        self._board_stamp = f"{resolved.name} (file modified {modified})"
        self.results.clear()
        self.runs.clear()
        self._diff_cache = None
        self._candidates.clear()
        self.rail_panel.set_pick("")
        self.measure.setChecked(False)
        self.net, self._rail_draft = None, None          # nothing of the previous board survives
        self._generation += 1
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()                         # and _solved ignores it if it ends
        if self.tree_worker is not None and self.tree_worker.isRunning():
            self.tree_worker.cancel()
        self._drop_tree("")
        self.rail_panel.clear_rail()
        self._kicad_shown = False
        self._show_banner(None)
        self.view.reset()
        self.layer_box.blockSignals(True)
        self.layer_box.clear()
        self.layer_box.blockSignals(False)
        self._refresh_results()                       # no net yet: nothing of the old board shown
        self.rail_panel.set_settings(self.project.settings)
        self.thermal_panel.set_options(self.project.thermal)
        self.noise_panel.board_changed()
        self.nets.set_board(board, self._configured_nets(), self.project.marked_rails, self._kicad_nets())
        self.problems.clear()
        self.ac_panel.board_changed()
        self.tolerance_panel.board_changed()
        self.dirty = project is None
        self._update_title()
        warn = f" — {len(board.warnings)} import warning(s), see Problems notes" if board.warnings else ""
        self.statusBar().showMessage(f"{board.name}: {len(board.nets())} nets, "
                                     f"{len(board.layers())} copper layers{warn}")
        if board.warnings:
            self.problems.set_notes(["Board import:", *board.warnings])
        first = next((r.net for r in self.merged.project.rails), None)
        if first:
            self.nets.select(first)
        else:
            self.view.show_board(board.outline(), fit=True)
        return True

    def reload_board(self) -> bool:
        """Read the board file again, and its KiCad footprint fields, keeping the project as it
        is (unsaved edits too) and the shown net; what was solved is dropped."""
        if self.board is None or self.board.path is None:
            return False
        net, dirty = self.net, self.dirty
        if not self.open_board(self.board.path, self.project):
            return False
        self.dirty = dirty
        self._update_title()
        if net and net in self.board.nets():
            self.nets.select(net)
        self.statusBar().showMessage(f"Reloaded {self.board.name}" + (
            " — a KiCad footprint field cannot be used, see Problems" if self.fields_error else ""))
        return True

    def _use_fields_toggled(self, on: bool):
        """Project → Use KiCad footprint fields (``use_board_fields``): off, every rail comes
        from the project file. What was solved is dropped."""
        if self.project.use_board_fields == on:
            return
        self.project.use_board_fields = on
        self._merge_again(reshow=True)
        self.noise_panel._setup_changed()            # the switchers, and the shown results flagged
        self.ac_panel._setup_changed()
        self.dirty = True
        self._update_title()
        if self.board is None:
            return
        self._revision += 1
        self._drop_tree("KiCad footprint fields turned " + ("on" if on else "off")
                        + ": solve the power tree again (Ctrl+T)")
        self.results.clear()
        self.runs.clear()
        self._diff_cache = None
        if self.net:
            self.problems.clear(f"<b>{self.net}</b>: KiCad footprint fields turned "
                                f"{'on' if on else 'off'} — press Solve rail (F5)")
            self._refresh_results()
            self.refresh_view()

    def open_project(self, path) -> bool:
        try:
            project = Project.load(path)
        except (OSError, ProjectError) as e:
            QMessageBox.critical(self, "Cannot open project", str(e))
            return False
        return self.open_board(project.board_path(), project, misspelt=read_misspelt(path))

    # ------------------------------------------------------------------ KiCad footprint fields
    def _read_board_fields(self, board: Board):
        """Read the board's KiCad footprint fields (``.kicad_pcb`` only: another format has
        none), and its net ties. An unreadable file is kept as the error the solves refuse with."""
        t0 = time.perf_counter()
        self.board_fields, self._fields_read_error = None, None
        try:
            self.board_fields = read_board_fields(board.path) if board.path is not None else None
        except EngineError as e:
            self._fields_read_error = f"{e} ({FIELD_HINT})"       # as the CLI prints it
        self.last_fields_read_s = time.perf_counter() - t0
        # The nets the board's net ties join: the merge follows them, as the CLI's does.
        self._tie_pairs = [(t.net_a, t.net_b) for t in board.net_ties()]

    def _remerge(self):
        """Merge the project with the board's fields into ``self.merged``, as the CLI does
        (``board_fields.apply``: off, or a board without fields, leaves the project as it
        is). Called after every change of the project. A field that cannot be used leaves
        the project unmerged and sets ``fields_error``: the solves then refuse."""
        t0 = time.perf_counter()
        self.fields_error = None
        merged = BF.Merged(self.project)
        if self.project.use_board_fields and self._fields_read_error:
            self.fields_error = self._fields_read_error
        elif self.board_fields is not None:
            try:
                merged = BF.apply(self.project, lambda: self.board_fields, ties=self._tie_pairs)
            except BF.FieldError as e:          # as `coppernicus solve` prints it
                where = f"{self.project.path.name}: " if self.project.path else ""
                self.fields_error = f"{where}{e} ({FIELD_HINT})"
        self.merged = merged
        self.last_remerge_s = time.perf_counter() - t0
        self._tolerance_stale()
        self._impedance_stale()

    def _tolerance_stale(self):
        """The project, the power tree or a result held changed: the Tolerance tab recomposes
        (once, after the events queued; at once while shown, else when shown)."""
        panel = getattr(self, "tolerance_panel", None)
        if panel is not None:
            panel.schedule_refresh()

    def _impedance_stale(self):
        """The project changed: the Impedance tab's capacitor models from KiCad fields and its
        shown Q9 warnings follow it, after the events queued (a tolerance edit keeps its result:
        no |Z| depends on a tolerance)."""
        panel = getattr(self, "ac_panel", None)
        if panel is not None:
            panel.schedule_refresh()

    def _merge_again(self, reshow: bool = False):
        """Merge again after the project changed (or its file name, which the red line
        names), and show what that changes: the red line and the pinned note, the net list's
        marks, and the shown rail. The rail is shown anew (or with ``reshow``) when its origin
        changed between the KiCad fields (read-only, a copy of the merged rail) and any other
        (the project's own rail, edited in place); any other change of origin shows the same
        rail, so only its banner and drop-limit lock follow, and an edit is never interrupted
        by the panel being filled again."""
        self._remerge()
        self._show_fields_error()
        if self.board is None:
            return
        self.nets.mark_configured(self._configured_nets(), self._kicad_nets())
        if not self.net:
            return
        origin = self.merged.origins.get(self.net)
        from_fields = origin is not None and origin.source == "fields"
        if reshow or from_fields != self._kicad_shown:
            rail = self._show_rail(self.net)
            self.rail_panel.set_net_kind(self._net_at_0v(rail))
        else:
            self._lock_drop(self.net)             # after a pad picked, the panel was filled again
            self._show_banner(self.net)

    def _fields_refusal(self) -> str:
        """What a solve says when a KiCad field cannot be used: the CLI's text, and the
        app's own off switch."""
        return f"{self.fields_error}; or turn them off: Project → Use KiCad footprint fields"

    def _show_fields_error(self):
        """The fields error (or none) in the rail panel's red line and the Problems notes."""
        text = self._fields_refusal() if self.fields_error else ""
        self.rail_panel.banner.set_error(text)
        self.problems.pin_note(f"KiCad footprint fields: {text}" if text else "")

    def _refuse_fields(self, title: str) -> bool:
        """True (and say why) when a KiCad field cannot be used: never solve without them."""
        if not self.fields_error:
            return False
        self.statusBar().showMessage("Cannot solve: a KiCad footprint field cannot be used (see Problems)")
        QMessageBox.warning(self, title, self._fields_refusal())
        return True

    def _configured_nets(self) -> List[str]:
        return [r.net for r in self.merged.project.rails if r.sources or r.loads]

    def _kicad_nets(self) -> Dict[str, str]:
        """The rails from the KiCad footprint fields, each with its net-list tooltip."""
        return {net: f"rail from KiCad footprint fields (parts {', '.join(o.board_fields['parts'])})"
                for net, o in self.merged.origins.items() if o.source == "fields" and o.board_fields}

    def _solved_rail(self, net: str) -> Rail:
        """The rail of ``net`` that is solved, drawn and judged: the merged one, or the draft
        being set up."""
        draft = self._rail_draft if self._rail_draft is not None and self._rail_draft.net == net else None
        return self.merged.project.rail(net) or self.project.rail(net) or draft or Rail(net)

    def _rail_warnings(self, net: str) -> Tuple[List[str], List[str]]:
        """(notes, warnings) of where the rail of ``net`` came from, for its result and its
        banner: the origin's, and the warnings about links on that net (the merge files each
        under both nets of the link, a net that is no rail too)."""
        origin = self.merged.origins.get(net)
        links = list(self.merged.link_warnings.get(net, []))
        if origin is None:
            return [], links
        return list(origin.notes), list(origin.warnings) + links

    def save_project(self):
        if self.project.path is None:
            return self.save_project_as()
        self._save(self.project.path)

    def save_project_as(self):
        start = str(self.board.path.with_suffix(".coppernicus.yml")) if self.board and self.board.path else ""
        path, _ = QFileDialog.getSaveFileName(self, "Save project", start, PROJECT_FILTER)
        if path:
            self._save(Path(path))

    def _save(self, path: Path):
        # A copy is written, with the rails the project says something about: the rails on
        # screen stay the project's, an emptied one too (it is simply not written).
        out = copy.copy(self.project)
        out.rails = [r for r in self.project.rails if saved_rail(r)]
        try:
            out.save(path)
        except (OSError, ProjectError) as e:
            QMessageBox.critical(self, "Cannot save project", str(e))
            return
        self.project.path, self.project.board = out.path, out.board     # as saved: relative to it
        self._merge_again()             # its path and board path; the red line names the file
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}")

    def export_maps(self):
        if not self.net or self.net not in self.results or self.board is None:
            QMessageBox.information(self, "Export maps", "Solve the rail first.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Export maps to")
        if not folder:
            return
        from coppernicus.maps import save_layer_maps
        res = self.results[self.net]
        rail = self._solved_rail(self.net)
        written = save_layer_maps(res, self.board.outline(), folder, find_problems(res, rail.limits))
        self.statusBar().showMessage(f"Wrote {len(written)} maps to {folder}")

    def export_report(self):
        """Write the offline HTML report of every solved rail (WS1's coppernicus.report), off
        the UI thread, to a file chosen in a Save dialog that starts in results/ next to the
        project (the CLI's folder), or in the home folder for an unsaved project, never next to
        a board elsewhere. With the power tree solved, also its page, the one ``solve --tree
        --html`` writes, beside it (:func:`tree_report_path`); with only the tree solved, the
        dialog is for the tree's page alone."""
        if any(w is not None and w.isRunning() for w in (self.worker, self.tree_worker)):
            self.statusBar().showMessage("A solve is running: export the report when it has finished")
            return
        if self.board is None or not (self.results or self.tree is not None):
            self.statusBar().showMessage("Solve a rail or the power tree first: the report shows what "
                                         "is solved")
            return
        if self.report_worker is not None and self.report_worker.isRunning():
            self.statusBar().showMessage("A report is already being written")
            return
        start = self._report_start_path()
        if not self.results:
            start = tree_report_path(start)
        path, _ = QFileDialog.getSaveFileName(self, "Export report", str(start), "HTML report (*.html)")
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() not in (".html", ".htm"):
            path = path.with_name(path.name + ".html")
        tree_job, what = None, [f"{len(self.results)} rail(s)"] if self.results else []
        if self.tree is not None:
            # As the CLI writes it, judged as the Power tree tab shows it now: the records a limit
            # edit judges again are copied (the solved groups are not: nothing changes them).
            tree = dataclasses.replace(self.tree, limits=dict(self.tree.limits),
                                       loads=copy.deepcopy(self.tree.loads),
                                       vrms=copy.deepcopy(self.tree.vrms))
            tree_job = (tree, tree_summary(tree), tree_report_path(path) if self.results else path,
                        Path(self.project.path).name if self.project.path else "",
                        dict(vars(self.merged.project.settings)))
            what.append("the power tree")
        self.report_worker = ReportWorker(self.results.values(), copy.deepcopy(self.merged.project),
                                          path if self.results else None, self.board.outline(),
                                          self.board.name, self, tree=tree_job)
        self.report_worker.succeeded.connect(self._report_written)
        self.report_worker.failed.connect(self._report_failed)
        self.statusBar().showMessage(f"Writing the report of {' and '.join(what)}… "
                                     "(a few seconds on big boards)")
        self.report_worker.start()

    def _report_start_path(self) -> Path:
        if self.project.path:
            project = Path(self.project.path)
            stem = project.name
            for suffix in (".coppernicus.yml", ".yml", ".yaml"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            folder, note = report_folder(project.parent)
            self._report_fallback = (folder, note) if note else None
        else:
            stem, folder = (self.board.name.split(".")[0] or "coppernicus"), Path.home()
            self._report_fallback = None
        return folder / f"{stem}_report.html"

    def _report_written(self, paths):
        """``paths``: the page written, or the pages (the rails', then the power tree's)."""
        paths = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
        sizes = [p.stat().st_size / 1e6 for p in paths]
        fallback = self._report_fallback
        note = fallback[1] if fallback and paths[0].parent.resolve() == fallback[0].resolve() else ""
        one = len(paths) == 1
        names = " and ".join(f"{p.name} ({mb:.2f} MB)" for p, mb in zip(paths, sizes))
        status = f"{'Report' if one else 'Reports'} written: {names} in {paths[0].parent}"
        self.statusBar().showMessage(f"{status} ({note})" if note else status)
        answer = QMessageBox.question(self, "Report written" if one else "Reports written",
                                      "\n".join(f"{p}\n{mb:.2f} MB." for p, mb in zip(paths, sizes))
                                      + (f"\n{note}." if note else "")
                                      + (" Open it in the browser?" if one else " Open them in the browser?"),
                                      QMessageBox.Open | QMessageBox.Close)
        if answer == QMessageBox.Open:
            for p in paths:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def remove_mesher(self):
        """Help → Remove downloaded mesher (Triangle)… (packaged app): after a confirmation,
        delete the Triangle mesher the app downloaded; the next start downloads it again."""
        site = bootstrap_triangle.site_dir()
        answer = QMessageBox.question(self, "Remove downloaded mesher",
                                      f"Delete the Triangle mesher downloaded into {site}?\n"
                                      "Coppernicus downloads it again on its next start.",
                                      QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        try:
            size = bootstrap_triangle.remove(site)
        except bootstrap_triangle.RemovalDeferred:
            # In use (mostly on Windows: this app has it loaded): not a failure, the next start
            # deletes it before using it.
            self.statusBar().showMessage("The mesher is in use: Coppernicus removes it at its next start")
            QMessageBox.information(self, "Remove downloaded mesher",
                                    f"The downloaded Triangle mesher in {site} is in use: Coppernicus "
                                    "removes it at its next start, then downloads it again.")
            return
        except bootstrap_triangle.BootstrapError as e:
            self.statusBar().showMessage("The downloaded mesher could not be removed")
            QMessageBox.warning(self, "Cannot remove the mesher", f"{e.message}\n{e.hint}")
            return
        if size:
            text = (f"Removed the downloaded Triangle mesher from {site} ({size / 1e6:.1f} MB). "
                    "Coppernicus downloads it again on its next start.")
            self.statusBar().showMessage("Removed the downloaded mesher: Coppernicus downloads it again "
                                         "on its next start")
        else:
            text = f"Nothing to remove: no downloaded Triangle mesher in {site}."
            self.statusBar().showMessage("Nothing to remove: no downloaded mesher")
        QMessageBox.information(self, "Remove downloaded mesher", text)

    def _report_failed(self, message: str):
        self.statusBar().showMessage("The report could not be written")
        QMessageBox.warning(self, "Cannot write the report", message)

    def _update_title(self):
        name = self.project.path.name if self.project.path else "unsaved project"
        board = f" — {self.board.name}" if self.board else ""
        self.setWindowTitle(f"Coppernicus {__version__} — {name}{'*' if self.dirty else ''}{board}")

    # ------------------------------------------------------------------ nets and layers
    def select_net(self, net: str):
        if self.board is None:
            return
        self.rail_panel.set_pick("")
        self.net = net
        self._set_group(None)                    # a net picked in the list: the rail solved alone
        rail = self._show_rail(net)
        self.rail_panel.set_net_kind(self._net_at_0v(rail))
        self.layer_box.blockSignals(True)
        self.layer_box.clear()
        for lid in self.board.net_layers(net):
            self.layer_box.addItem(self.board.layer_name(lid), lid)
        self.layer_box.blockSignals(False)
        res = self.results.get(net)
        if res is not None:
            self._show_problems(res)
        else:
            self.problems.clear(self._unsolved_text())
        self._refresh_results()
        self.refresh_view(fit=True)

    def _unsolved_text(self) -> str:
        """The Problems line of the shown net before it is solved: a rail from the KiCad
        fields is set up already."""
        if self._kicad_shown:
            return f"<b>{self.net}</b>: rail from KiCad footprint fields — press Solve rail (F5)"
        return f"<b>{self.net}</b>: add a source and a load, then Solve rail (F5)"

    def _show_rail(self, net: str) -> Rail:
        """Fill the rail panel for ``net`` as its origin says: a rail from the KiCad fields is
        shown read-only (a copy of the merged rail: its limits and net-tie option are written
        to the project's own rail of the net); any other is the project's rail, edited in
        place. Returns the rail the panel edits."""
        origin = self.merged.origins.get(net)
        self._kicad_shown = origin is not None and origin.source == "fields"
        rail = (copy.deepcopy(self.merged.project.rail(net)) if self._kicad_shown
                else self.project.rail(net) or Rail(net))
        _, ties = tied_nets(self.board, net)
        self.rail_panel.set_rail(self.board, rail, ties)
        self._rail_draft = rail
        if self._kicad_shown:
            listing = origin.board_fields or {}
            self.rail_panel.set_read_only(
                fields_view.read_only_reason(net),
                [fields_view.row_tip(e) for e in listing.get("sources", [])],
                [fields_view.row_tip(e) for e in listing.get("loads", [])])
        self._lock_drop(net)
        self._show_banner(net)
        return rail

    def _lock_drop(self, net: str):
        """A drop limit from a KiCad field (PI_LIMIT_DROP) applies to its net whatever its
        origin (the merge keeps it under an override and over incomplete fields): the rail
        panel shows it read-only, at the merged value, and never writes it to the project."""
        origin = self.merged.origins.get(net)
        limit = (origin.board_fields or {}).get("limits") if origin is not None else None
        merged = self.merged.project.rail(net)
        if limit and merged is not None:
            self.rail_panel.lock_drop(merged.limits.max_drop_pct, fields_view.drop_tip(limit))
        else:
            self.rail_panel.lock_drop(None)

    def _show_banner(self, net: Optional[str]):
        """The banner of the rail panel: where the shown rail comes from, its warnings."""
        if not net:
            self.rail_panel.banner.show_banner(fields_view.Banner())
            return
        origin = self.merged.origins.get(net)
        values = ""
        if origin is not None and origin.source == "overridden" and self.board_fields is not None:
            values = BF.field_rails(self.board_fields.fields)[net].values_text()
        self.rail_panel.banner.show_banner(
            fields_view.describe(origin, values, self._rail_warnings(net)[1]))

    def _banner_action(self, action: str):
        if self.net and action == fields_view.OVERRIDE:
            self.override_fields(self.net)
        elif self.net and action == fields_view.USE_FIELDS:
            self.use_fields(self.net)

    def override_fields(self, net: str) -> bool:
        """Override in project: the project's rail of ``net`` replaces the KiCad fields'
        (``override_board_fields``). With no sources or loads of its own, it takes the KiCad
        rail's (keeping its limits), so the same values are shown, now editable; its own
        rail, when it differs, is kept or replaced as the user chooses. False: nothing done."""
        origin = self.merged.origins.get(net)
        kicad = self.merged.project.rail(net)
        if origin is None or origin.source != "fields" or kicad is None:
            return False
        own = self.project.rail(net)
        if own is not None and (own.sources or own.loads):
            choice = fields_view.USE_MINE
            # Its sources or loads differ from the KiCad rail's: ask. (A max_drop_pct that
            # differs from PI_LIMIT_DROP warns too, but the KiCad limit stays in force.)
            differences = [w for w in origin.warnings if w.startswith(f"project file rail {net} ignored")]
            if differences:
                choice = fields_view.ask_override(self, net, differences)
                if choice is None:
                    return False
        else:
            choice = fields_view.START_FROM_KICAD
            if own is None:
                own = Rail(net)
                self.project.rails.append(own)
        if choice == fields_view.START_FROM_KICAD:   # the merged rail's: with any sense pins
            own.sources, own.loads = copy.deepcopy(kicad.sources), copy.deepcopy(kicad.loads)
        own.override_board_fields = True
        self._origin_changed(net)
        return True

    def use_fields(self, net: str) -> bool:
        """Use KiCad fields (the way back from an override), once confirmed: the project's
        rail of ``net`` keeps its limits and net-tie option and loses its sources and loads,
        so it is not an ignored rail (remote sense pins go with them: the confirmation names
        their parts). False: nothing done."""
        own = self.project.rail(net)
        if own is None or not own.override_board_fields:
            return False
        sensed = [s.ref for s in own.sources if s.sense_net or s.ground_sense_net]    # as the merge
        kept, lost = self._tolerance_holders(net)
        if not fields_view.confirm_use_fields(self, net, sensed, lost):
            return False
        own.override_board_fields = False
        own.sources, own.loads = kept if kept is not None else ([], [])
        self._origin_changed(net)
        return True

    def _tolerance_holders(self, net: str) -> Tuple[Optional[Tuple[list, list]], List[str]]:
        """Before Use KiCad fields on ``net``: the project rail's tolerances, entered parts and
        accuracies (where the parts' KiCad fields give none) kept by ref in a copy of the fields' sources
        and loads, which the merge sees as no difference (None when there is nothing to keep),
        and the parts whose ones go (the fields' rail has no part of their reference)."""
        own = self.project.rail(net)
        sides = ((own.sources, SOURCE_ACCURACY_KEYS), (own.loads, BF.TOLERANCE_KEYS))
        holding = [t for ts, keys in sides for t in ts if any(getattr(t, k) is not None for k in keys)]
        if not holding:
            return None, []
        trial = copy.deepcopy(self.project)       # the merge as it will be, the rail emptied
        rail = trial.rail(net)
        rail.override_board_fields, rail.sources, rail.loads = False, [], []
        try:
            merged = (BF.apply(trial, lambda: self.board_fields, ties=self._tie_pairs)
                      if self.board_fields is not None else BF.Merged(trial))
        except BF.FieldError:
            return None, [t.label for t in holding]
        origin, fields = merged.origins.get(net), merged.project.rail(net)
        if origin is None or origin.source != "fields" or fields is None:
            return None, [t.label for t in holding]
        out, lost = [], []
        for terms, (own_terms, keys) in ((copy.deepcopy(fields.sources), sides[0]),
                                         (copy.deepcopy(fields.loads), sides[1])):
            pairs, gone = BF.carried_by_ref(terms, own_terms, keys)
            for t, p in pairs:
                i = next(i for i, x in enumerate(terms) if x is t)
                carried = {k: getattr(p, k) for k in keys if getattr(p, k) is not None}
                terms[i] = dataclasses.replace(t, **carried)        # its own checks run
            out.append(terms)
            lost += [t.label for t in gone]
        return (out[0], out[1]), lost

    def _origin_changed(self, net: str):
        """The project now says otherwise where the rail of ``net`` comes from: merge again,
        show the rail anew, and drop what was solved with the old one."""
        self._merge_again(reshow=net == self.net)
        self.dirty = True
        self._update_title()
        self._setup_changed(net)

    def _adopt_draft(self):
        rail = self._rail_draft
        if self._kicad_shown:
            self._adopt_kicad_limits(rail)
        else:
            has_content = rail.sources or rail.loads or any(v is not None for v in vars(rail.limits).values())
            if self.project.rail(rail.net) is None and has_content:
                self.project.rails.append(rail)
        self._merge_again()
        self.dirty = True
        self._update_title()
        return rail

    def _adopt_kicad_limits(self, shown: Rail):
        """The limits and net-tie option edited on a rail from the KiCad fields go to the
        project's own rail of that net, a limits-only one when it has none (merge takes them
        from there, silently). A drop limit a KiCad field gives is not copied."""
        own = self.project.rail(shown.net)
        origin = self.merged.origins.get(shown.net)
        locked = bool(origin is not None and (origin.board_fields or {}).get("limits"))
        mine = own.limits.max_drop_pct if own is not None else None
        limits = dataclasses.replace(shown.limits, max_drop_pct=mine) if locked else copy.copy(shown.limits)
        if own is None:
            if all(v is None for v in vars(limits).values()) and shown.follow_net_ties:
                return                            # nothing of the project's own to keep
            own = Rail(shown.net)
            self.project.rails.append(own)
        own.limits = limits
        own.follow_net_ties = shown.follow_net_ties

    def _net_at_0v(self, rail: Rail) -> Optional[bool]:
        """True for a net at 0 V (named as a ground, or driven at 0 V), False for one with a
        nonzero nominal, None while it has neither: which limits apply to it."""
        if rail.net in ground_nets(self.merged.project):
            return True
        return (rail.sources[0].voltage == 0) if rail.sources else None

    def _rail_edited(self):
        rail = self._adopt_draft()
        self.rail_panel.set_net_kind(self._net_at_0v(rail))
        self._setup_changed(rail.net)

    def _setup_changed(self, changed: str):
        """The rail of net ``changed`` is not what was solved any more."""
        self._revision += 1
        self._drop_tree("The setup changed: solve the power tree again (Ctrl+T)")
        # Rails solved alone that drew an estimate from the project may depend on this rail.
        for net, res in list(self.results.items()):
            if net != changed and any(t.name.endswith("estimated)") for t in res.loads):
                self.results.pop(net)
                self.runs.pop(net, None)
        self.runs.pop(changed, None)
        self._diff_cache = None
        if self.results.pop(changed, None) is not None:          # the old result no longer applies
            self.problems.clear(f"<b>{changed}</b>: setup changed since the last solve — press "
                                "Solve rail (F5)")
            self._refresh_results()
        self.refresh_view()

    def _limits_edited(self):
        rail = self._adopt_draft()
        self.rail_panel.set_net_kind(self._net_at_0v(rail))       # a cleared field may close
        if self.tree is not None:                                  # re-judge, no re-solve
            apply_limits(self.tree, self.merged.project)
            self._show_tree()
        group = self.group_box.currentData()
        if group and self.tree is not None and group in self.tree.groups:
            self._show_tree_problems(group)                        # what the view shows
        elif (res := self.results.get(rail.net)) is not None:      # re-rank; the solve is still valid
            self._show_problems(res)

    def _marked_edited(self, nets):
        self.project.marked_rails = list(nets)
        self.dirty = True
        self._update_title()

    def _settings_edited(self):
        self._remerge()                           # the merged project holds a copy of them
        self.dirty = True
        self._update_title()
        self._revision += 1
        self._drop_tree("Solve settings changed: solve the power tree again (Ctrl+T)")
        if self.results:                                           # every result used the old settings
            self.results.clear()
            self.runs.clear()
            self._diff_cache = None
            self.problems.clear("Solve settings changed — press Solve rail (F5) to recompute")
            self._refresh_results()
            self.refresh_view()

    def refresh_view(self, fit: bool = False):
        if self.board is None or not self.net:
            return
        layer_name = self.layer_box.currentText()
        lid = self.layer_box.currentData()
        group = self.group_box.currentData()
        res = self.tree.groups.get(group) if group and self.tree else self.results.get(self.net)
        rail = None if group else self._solved_rail(self.net)
        t0 = time.perf_counter()
        self._fallback_reason = ""
        if self._draw_temperature(layer_name, fit):          # needs no rail result
            self.last_render_s = time.perf_counter() - t0
            return
        if res is not None and res.layer(layer_name) is not None:
            pads = [("source", [i for t in res.sources for i in t.pad_ids]),
                    ("load", [i for t in res.loads for i in t.pad_ids])]
            field, diff_mv, diff_note = self.field_box.currentData(), None, ""
            if field == "difference":
                diff_mv, diff_note, why = ((None, "", "The difference map is for a rail solved alone, "
                                            "not yet for a power-tree group") if group
                                           else self._difference(res, layer_name))
                if diff_mv is None:                  # not available: say why, show the drop
                    self._fallback_reason = why
                    self.statusBar().showMessage(why)
                    field = "drop"
                    self.field_box.blockSignals(True)
                    self.field_box.setCurrentIndex(self.field_box.findData("drop"))
                    self.field_box.blockSignals(False)
            self.view.show_field(res, res.layer(layer_name), field,
                                 self.board.outline(), self.show_vias.isChecked(), fit=fit,
                                 pad_outlines=self._pad_outlines(rail, pads, lid),
                                 isolines=self.show_isolines.isChecked(),
                                 arrows=self.show_arrows.isChecked(),
                                 difference_mv=diff_mv, difference_note=diff_note,
                                 **self._view_reference(group))
        else:
            rings = copper_rings(self.board.copper(self.net, lid)) if lid is not None else []
            src, lds = self._terminal_pads(rail)
            pads = [("source", [p.id for _, ps in src for p in ps]),
                    ("load", [p.id for _, ps in lds for p in ps])]
            self.view.show_board(self.board.outline(), rings,
                                 [(name, [tuple(p.position) for p in ps]) for name, ps in src],
                                 [(name, [tuple(p.position) for p in ps]) for name, ps in lds],
                                 fit=fit, pad_outlines=self._pad_outlines(rail, pads, lid))
        self.last_render_s = time.perf_counter() - t0

    def _rail_nets(self, rail: Rail) -> List[str]:
        """The rail's net plus, when it follows them, the nets joined by net ties."""
        return sorted(pick_nets(rail, tied_nets(self.board, rail.net)[0]))

    def _terminal_pads(self, rail: Optional[Rail]):
        """(label, pads) of the rail's sources and loads; a terminal that does not resolve
        (a part not on the net, a wrong pin) has no pads."""
        if rail is None or self.board is None:
            return [], []
        nets = self._rail_nets(rail)

        def pads(t):
            try:
                return self.board.pads(nets, t.ref, t.pins)
            except EngineError:
                return []
        return ([(s.label, pads(s)) for s in rail.sources], [(ld.label, pads(ld)) for ld in rail.loads])

    def _pad_outlines(self, rail: Optional[Rail], terminal_ids, layer_id):
        """Pad rings on the shown layer: every pickable pad while picking, and the pads of
        the sources and loads (drawn on top)."""
        if layer_id is None or self.board is None:
            return []
        out = []
        if self.rail_panel.pick_kind and rail is not None:
            out.append(("candidate", self.board.pad_rings(self._pickable_pads(rail), layer_id)))
        out.extend((role, self.board.pad_rings(ids, layer_id)) for role, ids in terminal_ids)
        return out

    def _pickable_pads(self, rail: Rail) -> List[str]:
        key = tuple(self._rail_nets(rail))
        if key not in self._candidates:
            self._candidates[key] = [p.id for net in key for part in self.board.parts_on_net(net)
                                     for p in self.board.pads(net, part.ref)]
        return self._candidates[key]

    # ------------------------------------------------------------------ clicks on the view
    def stop_clicking(self):
        """Esc: leave pick and measure modes."""
        self.rail_panel.set_pick("")
        self.tree_panel.stop_pick()
        self.measure.setChecked(False)
        self.ac_panel.stop_pick()

    def _measure_toggled(self, on: bool):
        if on:
            self.rail_panel.set_pick("")                  # one click mode at a time
            self.tree_panel.stop_pick()
            self.view.set_click_mode("measure")
            self.statusBar().showMessage("Click point A, then point B · Esc to stop")
        elif self.view.click_mode == "measure":
            self.view.set_click_mode(None)

    def _pick_mode_changed(self, kind: str):
        if kind:
            self.tree_panel.stop_pick()
            self.measure.blockSignals(True)
            self.measure.setChecked(False)
            self.measure.blockSignals(False)
            self.view.set_click_mode("pick")
            self.statusBar().showMessage(f"Click pads to add them to the {kind}s · Esc to stop")
        elif self.view.click_mode == "pick":
            self.view.set_click_mode(None)
        self.refresh_view()

    def pick_pad(self, x: float, y: float) -> str:
        """Add the pad at board point (x, y) mm to the sources or loads being picked. A pad
        on the shown layer is preferred; one on another layer is used when none is there.
        Returns the status-bar message."""
        if self.tree_panel.picking():             # "0 V at" a pad of the power tree (S2)
            return self._pick_reference(x, y)
        kind = self.rail_panel.pick_kind
        if self.board is None or not self.net or not kind:
            return ""
        if self._kicad_shown:                     # read-only: say how to edit it
            msg = fields_view.read_only_reason(self.net)
            self.statusBar().showMessage(msg)
            return msg
        lid = self.layer_box.currentData()
        hit = (self.board.pad_at(x, y, lid) if lid is not None else None) or self.board.pad_at(x, y)
        rail = self.project.rail(self.net) or self._rail_draft
        if hit is None:
            msg = f"No pad at ({x:.2f}, {y:.2f}) mm"
        elif hit.net not in self._rail_nets(rail):
            msg = f"{hit.ref or 'pad'}.{hit.pin} is on {hit.net or 'no net'}, not on {rail.net}"
        elif not hit.ref:
            msg = f"The pad at ({x:.2f}, {y:.2f}) mm belongs to no part"
        else:
            msg = add_picked_pin(rail, kind, hit.ref, hit.pin)
            if hit.layer_id != lid:
                msg += f" (pad on {self.board.layer_name(hit.layer_id)})"
            _, ties = tied_nets(self.board, rail.net)
            self.rail_panel.set_rail(self.board, rail, ties)
            self._rail_edited()
        self.statusBar().showMessage(msg)
        return msg

    # ------------------------------------------------------------------ solve
    def solve(self):
        if self.board is None or not self.net:
            return
        if self._refuse_fields("Cannot solve"):
            return
        if self.worker is not None and self.worker.isRunning():
            self.statusBar().showMessage("A solve is already running")
            return
        rail = self._solved_rail(self.net)
        project = self.merged.project
        notes, warnings = self._rail_warnings(self.net)
        self.rail_panel.solve.setEnabled(False)
        self.problems.clear(f"<b>{self.net}</b>: solving…")
        self.worker = SolveWorker(self.board, rail, project.settings, self, project=project,
                                  notes=notes, warnings=warnings)
        self.worker.generation = self._generation
        self.worker.revision = self._revision
        # What produced the result, as it is now (the user may edit while it runs).
        self.worker.run_inputs = (copy.deepcopy(rail), copy.deepcopy(project.settings),
                                  self._board_stamp)
        self.worker.message.connect(lambda m: self.statusBar().showMessage(m))
        self.worker.succeeded.connect(self._solved)
        self.worker.failed.connect(self._solve_failed)
        self.worker.finished.connect(lambda: self.rail_panel.solve.setEnabled(True))
        self.worker.start()

    def _is_stale(self) -> bool:
        """True in a solve slot when the solve was started on a board that is closed now."""
        return getattr(self.sender(), "generation", self._generation) != self._generation

    def _solved(self, res: RailResult):
        if self._is_stale():
            return
        if getattr(self.sender(), "revision", self._revision) != self._revision:
            msg = f"The setup changed while {res.net} was solving: press Solve rail (F5) again"
            if res.net == self.net:
                self.problems.clear(msg)
            self.statusBar().showMessage(msg)
            return
        self.results[res.net] = res
        inputs = getattr(self.sender(), "run_inputs", None)
        rail, settings, board = inputs or (self._solved_rail(res.net), self.merged.project.settings,
                                           self._board_stamp)
        self.runs[res.net] = Run(res, rail, settings, board)
        if res.net == self.net:
            self._refresh_results()
        if res.net == self.net:
            self._show_problems(res)
            # Open on the layer where the rail loses the most power: where the drop happens.
            busiest = max(res.layers, key=lambda lf: lf.power, default=None)
            i = self.layer_box.findText(busiest.name) if busiest else -1
            if i >= 0:
                self.layer_box.blockSignals(True)
                self.layer_box.setCurrentIndex(i)
                self.layer_box.blockSignals(False)
            self.refresh_view(fit=True)
        reason = f" · {self._fallback_reason}" if res.net == self.net and self._fallback_reason else ""
        self.statusBar().showMessage(
            f"{res.net} solved in {res.stats['seconds']:.1f} s · {int(res.stats['triangles']):,} triangles · "
            f"drawn in {self.last_render_s:.2f} s{reason}")

    # ------------------------------------------------------------------ power tree
    def solve_tree(self):
        """Solve every rail and ground of the project together (Ctrl+T)."""
        if self.board is None:
            return
        if self._refuse_fields("Cannot solve the power tree"):
            return
        if self.tree_worker is not None and self.tree_worker.isRunning():
            self.statusBar().showMessage("The power tree is already being solved")
            return
        thermal = self.merged.project.thermal.enabled   # the Temperature tab holds Cancel
        if thermal and not self._thermal_go():
            return
        self.tree_panel.clear("<b>Power tree</b>: solving… · Cancel: Temperature tab")
        self.thermal_panel.clear("<b>Board temperature</b>: solving…" if thermal else "")
        self.results_tabs.setCurrentWidget(self.thermal_panel if thermal else self.tree_panel)
        self.tree_worker = TreeWorker(self.board, self.merged.project, self)
        self.tree_worker.merged = self.merged          # its notes and warnings, for the result
        self.tree_worker.thermal = thermal
        self.tree_worker.generation = self._generation
        self.tree_worker.revision = self._revision
        self.tree_worker.message.connect(self._tree_message)
        self.tree_worker.succeeded.connect(self._tree_solved)
        self.tree_worker.failed.connect(self._tree_failed)
        self.thermal_panel.set_solving(True)
        self.tree_worker.start()

    def _tree_solved(self, tree: TreeResult):
        self.thermal_panel.set_solving(False)
        if self._is_stale():
            return
        if getattr(self.sender(), "revision", self._revision) != self._revision:
            msg = "The setup changed while the power tree was solving: solve it again (Ctrl+T)"
            self.tree_panel.clear(msg)
            self.thermal_panel.clear(msg)
            self.statusBar().showMessage(msg)
            return
        merged = getattr(self.sender(), "merged", None)
        if merged is not None:
            merged.annotate(tree)                   # where the rails came from, as the CLI says
            rating_origins(tree, merged)            # ... and each regulator's rating
        # Limits do not change the solve: judge it by the ones set now (one may have been
        # edited while it solved, on the copy the worker took).
        apply_limits(tree, self.merged.project)         # the board temperature's too
        if tree.thermal is not None:
            record_solve(tree)                           # its time, for the next estimate
        self.tree = tree
        self.tree_i_max = tree_i_max(tree)
        self._tolerance_stale()
        self._apply_tree_reference(self.tree_panel.reference_spec() if self.tree_panel.asked
                                   else self.merged.project.tree_reference)
        self._show_tree()
        self.thermal_panel.set_tree(tree, self._thermal_verdict_html())
        self.group_box.blockSignals(True)
        while self.group_box.count() > 1:
            self.group_box.removeItem(1)
        for key, res in tree.groups.items():
            self.group_box.addItem(f"{key} ({res.kind})" if res.kind == "ground" else key, key)
        self.group_box.blockSignals(False)
        reason = ""
        if self.field_box.currentData() == "temperature":
            self.refresh_view()                          # the map showed the last tree's
            reason = f" · {self._fallback_reason}" if self._fallback_reason else ""
        peak = (f" · board peak {tree.thermal.peak_c:.2f} °C (Temperature tab, Board temperature map)"
                if tree.thermal is not None else "")
        self.statusBar().showMessage(f"Power tree solved in {tree.stats['seconds']:.1f} s: "
                                     f"{len(tree.groups)} groups · pick one in Group to see it{peak}{reason}")

    def _tree_failed(self, message: str):
        self.thermal_panel.set_solving(False)
        if self._is_stale():
            return
        # Cancel is expected (no box), but only its own report: an error after it is shown.
        cancelled = getattr(self.sender(), "cancelled", False) and message == CANCELLED
        if self.tree is not None:          # the previous tree is still in use: show it again
            self._show_tree()
            self.thermal_panel.set_tree(self.tree, self._thermal_verdict_html())
            self.statusBar().showMessage(f"Power tree solve {'cancelled' if cancelled else 'failed'}: "
                                         "the previous result is shown")
        else:
            what = "solve cancelled" if cancelled else "cannot solve"
            self.tree_panel.clear(f"<b>Power tree</b>: {what}")
            self.thermal_panel.clear(f"<b>Board temperature</b>: {what}")
            if cancelled:
                self.statusBar().showMessage("Power tree solve cancelled")
        if not cancelled:
            QMessageBox.warning(self, "Cannot solve the power tree", message)

    def _show_tree(self):
        """The Power tree tab: the tree, its verdict and its VRMs' I max."""
        self.tree_panel.set_tree(self.tree, self._tree_verdict(), self.tree_i_max)

    # ------------------------------------------------------------------ "0 V at" (S2)
    def _apply_tree_reference(self, spec) -> bool:
        """Show the power tree from 0 V at ``spec`` (a ``reference_spec``; None: the solve's own
        0 V): read off the tree (:func:`tree_reference`), which is not solved again and whose
        values never move; only its ``reference`` (a display setting) is replaced. A spec that
        does not resolve leaves the reference as it was and says why."""
        from coppernicus.app.panels import reference_note
        from coppernicus.engine.tree import reference_parts, tree_reference
        if self.tree is None or self.board is None:
            return False
        try:
            ref = tree_reference(self.board, self.tree, spec)
        except EngineError as e:
            self.tree_panel.show_reference(getattr(self.tree.reference, "given", None),
                                           reference_parts(self.tree))
            self.tree_panel.set_reference_note(str(e), error=True)
            self.statusBar().showMessage(f"0 V at: {e}")
            return False
        self.tree.reference = ref
        self.tree_panel.show_reference(ref.given, reference_parts(self.tree))
        self.tree_panel.set_reference_note(reference_note(ref))
        return True

    def _tree_reference_requested(self, spec):
        """"0 V at" chosen in the Power tree tab: the tables, the voltage map and the readouts
        from it, with no re-solve."""
        if self._apply_tree_reference(spec):
            self._show_tree()
            if self.group_box.currentData() is not None:
                self.refresh_view()
            self.statusBar().showMessage(f"Voltages shown from 0 V at {self.tree.reference.label}")

    def _view_reference(self, group) -> dict:
        """The view's voltage offset for a power-tree group shown from a reference the user
        chose; nothing for a rail solved alone or the solve's own 0 V."""
        ref = getattr(self.tree, "reference", None) if group and self.tree is not None else None
        if ref is None or ref.given is None:
            return {}
        return {"v_offset": ref.offset_v, "v_offset_label": ref.label}

    def _reference_pick_toggled(self, on: bool):
        if on:
            self.rail_panel.set_pick("")                  # one click mode at a time
            self.measure.blockSignals(True)
            self.measure.setChecked(False)
            self.measure.blockSignals(False)
            self.view.set_click_mode("pick")
            self.statusBar().showMessage("Click a pad: the power tree shown from 0 V at it · Esc to stop")
        elif self.view.click_mode == "pick" and not self.rail_panel.pick_kind:
            self.view.set_click_mode(None)

    def _pick_reference(self, x: float, y: float) -> str:
        """The pad at board point (x, y) mm as "0 V at" (Pick in the Power tree tab)."""
        lid = self.layer_box.currentData()
        hit = None
        if self.board is not None:
            hit = (self.board.pad_at(x, y, lid) if lid is not None else None) or self.board.pad_at(x, y)
        if self.tree is None:
            msg = "Solve the power tree first (Ctrl+T)"
        elif hit is None or not hit.ref:
            msg = f"No pad of a part at ({x:.2f}, {y:.2f}) mm"
        else:
            self.tree_panel.picked(f"{hit.ref}.{hit.pin}")
            return self.statusBar().currentMessage()
        self.statusBar().showMessage(msg)
        return msg

    def _tree_verdict(self) -> str:
        if self.tree is None:
            return ""
        overall, groups = tree_verdicts(self.tree)
        unsolved = unsolved_limits(self.tree)
        tv = thermal_verdict(self.tree)                         # the board temperature counts too
        return verdict_html(overall, sum(v.failed for v in groups.values()) + tv.failed,
                            [x for v in groups.values() for x in v.not_judged] + unsolved + tv.not_judged,
                            any(has_limits(v) for v in groups.values()) or bool(unsolved) or bool(tv.limits))

    def _drop_tree(self, reason: str):
        """The power-tree result no longer matches the project (or the board)."""
        had = self.tree is not None
        self.tree = None
        self._tolerance_stale()
        shown = self.group_box.currentData() is not None
        self.group_box.blockSignals(True)
        while self.group_box.count() > 1:
            self.group_box.removeItem(1)
        self.group_box.setCurrentIndex(0)
        self.group_box.blockSignals(False)
        if had or reason == "":
            self.tree_panel.clear(reason)
            self.thermal_panel.clear(reason)
        if shown and self.net:
            self._show_net_view()
        elif self.field_box.currentData() == "temperature" and self.net:
            self.refresh_view()                          # the map showed this tree's temperature

    def _set_group(self, key: Optional[str]):
        self.group_box.blockSignals(True)
        self.group_box.setCurrentIndex(max(0, self.group_box.findData(key)))
        self.group_box.blockSignals(False)

    def show_group(self, key: str):
        """Show group ``key`` of the power tree in the view."""
        i = self.group_box.findData(key)
        if i >= 0:
            self.group_box.setCurrentIndex(i)

    def _group_changed(self):
        """Fill the layer box for the shown group (or the net), show its problems and draw it."""
        if self.board is None:
            return
        group = self.group_box.currentData()
        res = self.tree.groups.get(group) if group and self.tree else None
        if res is not None:
            keep = self.layer_box.currentText()
            self.layer_box.blockSignals(True)
            self.layer_box.clear()
            for lf in res.layers:
                self.layer_box.addItem(lf.name, lf.layer_id)
            busiest = max(res.layers, key=lambda lf: lf.power).name if res.layers else ""
            i = self.layer_box.findText(keep if self.layer_box.findText(keep) >= 0 else busiest)
            self.layer_box.setCurrentIndex(max(0, i))
            self.layer_box.blockSignals(False)
            self._show_tree_problems(group)
            self.refresh_view(fit=True)
        elif self.net:
            self._show_net_view()

    def _show_net_view(self):
        """Back to the shown net solved alone: its layers, problems and map. The rail panel is
        left as it is (it may be in the middle of an edit)."""
        self.layer_box.blockSignals(True)
        self.layer_box.clear()
        for lid in self.board.net_layers(self.net):
            self.layer_box.addItem(self.board.layer_name(lid), lid)
        self.layer_box.blockSignals(False)
        res = self.results.get(self.net)
        if res is not None:
            self._show_problems(res)
        else:
            self.problems.clear(self._unsolved_text())
        self._refresh_results()
        self.refresh_view(fit=True)

    def _show_tree_problems(self, key: str):
        res = self.tree.groups[key]
        gv = group_verdict(self.tree, key)
        self.problems.set_result(tree_header_html(key, res, gv), gv.problems, res.warnings + res.assumptions)
        self.view.set_problems(res, gv.problems,          # the current map's peak and verdict
                               group_limits(self.tree, key)[0])

    # ------------------------------------------------------------------ board temperature
    def _thermal_edited(self, key: str = ""):
        """An electro-thermal option ``key`` changed (Temperature tab). Ticking or unticking
        Electro-thermal, or any option while it is ticked (or while the tree holds a board
        temperature), makes the power tree stale; with it off, the other options change no
        solve. The rails solved alone stay: Solve rail (F5) does not heat copper."""
        self._remerge()                           # the merged project holds a copy of them
        self.dirty = True
        self._update_title()
        heated = self.merged.project.thermal.enabled or getattr(self.tree, "thermal", None) is not None
        if key != "enabled" and not heated:
            return
        self._revision += 1
        self._drop_tree("Electro-thermal options changed: solve the power tree again (Ctrl+T)")

    def _thermal_limits_edited(self):
        """A board-temperature limit changed: the power tree is judged again, not solved."""
        self._remerge()                           # the merged project holds a copy of them
        self.dirty = True
        self._update_title()
        if self.tree is not None:
            apply_thermal_limits(self.tree, self.merged.project.thermal)
            self._show_tree()
            self.thermal_panel.set_tree(self.tree, self._thermal_verdict_html())

    def _thermal_go(self) -> bool:
        """Before an electro-thermal solve: its rough cost in the Temperature tab and, when its
        model could exceed this machine's memory, "Solve anyway?" (No by default). Nothing
        else changes before the answer."""
        text, risk = self.thermal_panel.show_estimate(self.board, self.merged.project.thermal)
        if not risk:
            return True
        answer = QMessageBox.warning(
            self, "This solve could exceed the memory",
            f"{text}\n\nSolve anyway? It could make this computer swap; a coarser grid "
            "(Temperature tab → Grid) lowers it.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            return True
        self.statusBar().showMessage("Not solved: the board temperature's model could exceed this "
                                     "machine's memory (see the estimate in the Temperature tab)")
        return False

    def _thermal_verdict_html(self) -> str:
        tv = thermal_verdict(self.tree)
        return verdict_html(tv.verdict, tv.failed, tv.not_judged, bool(tv.limits))

    def _draw_temperature(self, layer_name: str, fit: bool) -> bool:
        """With "Board temperature" chosen, draw the shown layer's map from the power tree's
        electro-thermal solve and return True; without one, say why and show the drop. The
        map has no isolines, arrows, vias or ΔV: they need a rail's copper."""
        shown = False
        if self.field_box.currentData() == "temperature":
            temp = self.tree.thermal if self.tree is not None else None
            if temp is not None and temp.map(layer_name) is not None:
                self.measure.setChecked(False)
                self.view.show_temperature(temp, layer_name, self.board.outline(), fit=fit)
                shown = True
            else:
                self._fallback_reason = NO_MAP if temp is None else f"No board temperature on {layer_name}"
                self.statusBar().showMessage(self._fallback_reason)
                self.field_box.blockSignals(True)
                self.field_box.setCurrentIndex(self.field_box.findData("drop"))
                self.field_box.blockSignals(False)
        for widget in (self.show_vias, self.show_isolines, self.show_arrows, self.measure):
            widget.setEnabled(not shown)
        return shown

    def show_temperature_at(self, layer: str, x: float, y: float, group: str):
        """A layer row of the Temperature tab double-clicked: that layer's board temperature,
        zoomed to the row's copper. A layer the shown net has no copper on is reached through
        the row's group of the power tree."""
        if self.board is None or self.tree is None or self.tree.thermal is None or not self.net:
            return
        if self.layer_box.findText(layer) < 0 and group in self.tree.groups:
            self.show_group(group)
        i = self.layer_box.findText(layer)
        if i < 0:
            self.statusBar().showMessage(f"{layer} is not a layer of the shown net or group")
            return
        for box, index in ((self.field_box, self.field_box.findData("temperature")), (self.layer_box, i)):
            box.blockSignals(True)
            box.setCurrentIndex(index)
            box.blockSignals(False)
        self.refresh_view()
        QTimer.singleShot(0, lambda: self.view.zoom_to(x, y))

    def cancel_tree(self):
        """Cancel (Temperature tab): the power-tree solve stops at its next check, once per
        iteration (never inside a factorization)."""
        if self.tree_worker is not None and self.tree_worker.isRunning():
            self.tree_worker.cancel()
            self.thermal_panel.set_cancelling()
            self.statusBar().showMessage(CANCELLING)

    def _tree_message(self, text: str):
        """A progress line of the power-tree solve; after Cancel, led by the cancelling note."""
        cancelled = getattr(self.sender(), "cancelled", False)
        self.statusBar().showMessage(f"{CANCELLING} · {text}" if cancelled else text)

    # ------------------------------------------------------------------ before / after
    def pin_baseline(self):
        """Keep the shown rail's latest result as the baseline ("before")."""
        run = self.runs.get(self.net) if self.net else None
        if run is None:
            self.statusBar().showMessage("Solve the rail first, then pin its result as the baseline")
            return
        self.baseline = run
        self._diff_cache = None
        self._refresh_results()
        self.refresh_view()                          # a Δ map against the old baseline is stale
        self.statusBar().showMessage(f"Baseline: {self.net} on {run.board}. Change something and solve again "
                                     "to compare (Compare tab, 'Δ drop vs baseline' map)")

    def clear_baseline(self):
        self.baseline = None
        self._diff_cache = None
        if self.field_box.currentData() == "difference":
            self.field_box.setCurrentIndex(self.field_box.findData("drop"))
        self._refresh_results()

    def _comparable(self):
        """(baseline, current run) of the shown net, or a reason why there is nothing to compare."""
        run = self.runs.get(self.net) if self.net else None
        base = self.baseline
        if base is None:
            return None, None, ("No baseline yet. Solve the rail, press 'Pin baseline', change the loads, "
                                "the settings or the board, and solve again.")
        if base.rail.net != self.net:
            return None, None, f"The baseline is a result of {base.rail.net}: select that net to compare."
        if run is None:
            return None, None, f"Baseline pinned (solved on {base.board}). Solve {self.net} to compare."
        if run is base:
            return None, None, "The shown result is the baseline. Change something and solve again."
        return base, run, ""

    def _refresh_results(self):
        """The 'Loss by layer' chart and the 'Compare' tab, for the shown net."""
        run = self.runs.get(self.net) if self.net else None
        base, current, why = self._comparable()
        self.compare.pin.setEnabled(run is not None)
        self.compare.clear_baseline.setEnabled(self.baseline is not None)
        self.losses.set_losses(run.result.loss_breakdown() if run else None,
                               base.result.loss_breakdown() if base else None,
                               f"{self.net}: copper loss by layer" if run else "")
        if base is None:
            self.compare.show_message(why)
            return
        notes = []
        if base.board != current.board:
            notes.append("The board file changed between the two solves: loads are matched by name; "
                         "the difference map is not available.")
        self.compare.show_comparison(
            compare_runs(base, current),
            f"<b>{self.net}</b>: current − baseline. Baseline solved on {base.board}; current on "
            f"{current.board}.", notes)

    def _difference(self, res: RailResult, layer_name: str):
        """(Δ drop per node in mV on the shown layer, how it was obtained, why not) for the
        'Δ drop vs baseline' map; the values are None when it is not available."""
        base, run, why = self._comparable()
        if base is None:
            return None, "", why
        if base.board != run.board:
            return None, "", ("The board file changed between the two solves: the difference map needs the "
                              "same copper. The Compare tab still compares the loads.")
        base_layer, layer = base.result.layer(layer_name), res.layer(layer_name)
        if base_layer is None or layer is None:
            return None, "", f"The baseline has no copper of {self.net} on {layer_name}"
        c = self._diff_cache
        if not (c and c[0] is base and c[1] is run and c[2] == layer_name):
            diff = difference_layer(layer, base_layer)          # V_base − V_current per node
            # Δ drop = drop_sign · ((V_ref − V)_current − (V_ref − V)_baseline)
            mv = res.sign * (diff.voltage + (res.v_ref - base.result.v_ref)) * 1e3
            note = ("exact: same mesh" if same_mesh(layer, base_layer)
                    else "interpolated: the two solves have different meshes")
            self._diff_cache = (base, run, layer_name, (mv, note))
        mv, note = self._diff_cache[3]
        return mv, note, ""

    def _solve_failed(self, message: str):
        if self._is_stale():
            return
        self.problems.clear(f"<b>{self.net}</b>: cannot solve")
        self.problems.set_notes([message])
        QMessageBox.warning(self, "Cannot solve", message)

    def _show_problems(self, res: RailResult):
        rail = self._solved_rail(res.net)
        problems = find_problems(res, rail.limits)
        self.problems.set_result(rail_header_html(res, rail.limits, problems), problems,
                                 res.warnings + res.assumptions)
        self.view.set_problems(res, problems)             # the current map's peak and verdict

    def go_to_problem(self, problem):
        if problem.layer:
            i = self.layer_box.findText(problem.layer)
            if i >= 0 and i != self.layer_box.currentIndex():
                self.layer_box.setCurrentIndex(i)
        if problem.kind == "hotspot" and self.field_box.currentData() != "current":
            self.field_box.setCurrentIndex(self.field_box.findData("current"))
        QTimer.singleShot(0, lambda: self.view.zoom_to(problem.x, problem.y))

    # ------------------------------------------------------------------ closing
    def closeEvent(self, event):
        if not self.confirm_discard("closing"):
            event.ignore()
            return
        if self.worker is not None and self.worker.isRunning():
            # Cancelled, it reports "Solve cancelled": no box or drawing after the window has gone.
            for signal in (self.worker.message, self.worker.succeeded, self.worker.failed,
                           self.worker.finished):
                signal.disconnect()
            self.worker.cancel()
            self.worker.wait(5000)
        if self.tree_worker is not None and self.tree_worker.isRunning():
            for signal in (self.tree_worker.message, self.tree_worker.succeeded, self.tree_worker.failed):
                signal.disconnect()
            self.tree_worker.cancel()
            # KiPI checks Cancel once per iteration, never inside a factorization (about 30 s on a
            # big board): wait for the thread to end rather than destroy it running.
            what = "board temperature" if getattr(self.tree_worker, "thermal", False) else "power-tree"
            self.statusBar().showMessage(f"Finishing the {what} solve before closing…")
            self.statusBar().repaint()
            self.tree_worker.wait()
        self.noise_panel.close_workers()
        if self.report_worker is not None and self.report_worker.isRunning():
            # A report cannot be cancelled halfway: finish the file, without asking to open it.
            self.report_worker.succeeded.disconnect()
            self.report_worker.failed.disconnect()
            self.statusBar().showMessage("Finishing the report before closing…")
            self.report_worker.wait()
        self.ac_panel.close_workers()
        self.view.close()
        super().closeEvent(event)
