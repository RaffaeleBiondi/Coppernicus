"""Main window: one loop — open a board, pick a net, add sources and loads, solve, read the
maps and the ranked problems, click a problem to go there.

Layout: nets on the left, the 2D board view in the centre (layer and field pickers on
top), the rail setup on the right, problems and notes at the bottom.
"""

from __future__ import annotations

import copy
import dataclasses
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QLocale, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from coppernicus import __version__
from coppernicus import board_fields as BF
from coppernicus.app import fields_view
from coppernicus.app.board_view import FIELDS, BoardView, copper_rings
from coppernicus.app.comparing import Run, compare_runs
from coppernicus.app.panels import ComparePanel, LossChart, NetPanel, ProblemsPanel, RailPanel, TreePanel
from coppernicus.app.picking import add_picked_pin, pick_nets
from coppernicus.app.worker import ReportWorker, SolveWorker, TreeWorker
from coppernicus.compare import difference_layer, same_mesh
from coppernicus.engine import Board, EngineError, tied_nets
from coppernicus.engine.board import board_file_filter
from coppernicus.engine.kicad_fields import read_board_fields
from coppernicus.engine.pisetup import ground_nets
from coppernicus.power_tree import (
    apply_limits,
    group_verdict,
    has_limits,
    not_judged_text,
    tree_verdicts,
    unsolved_limits,
)
from coppernicus.problems import FAIL, find_problems, max_drop_text
from coppernicus.project import Limits, Project, ProjectError, Rail
from coppernicus.results import RailResult, TreeResult
from coppernicus.service.convert import not_judged_items
from coppernicus.service.session import FIELD_HINT

PROJECT_FILTER = "Coppernicus project (*.coppernicus.yml *.yml *.yaml)"


def initial_size(avail_w: Optional[int], avail_h: Optional[int]) -> Tuple[int, int]:
    """The window's first size: 1500 × 920, shrunk to the screen's available area (a 1440 ×
    900 laptop) when one is known."""
    return (min(1500, avail_w) if avail_w else 1500, min(920, avail_h) if avail_h else 920)


def verdict_html(verdict: str, fails: int, not_judged, has_limits: bool) -> str:
    """A power-tree verdict for the summary lines: what failed, and what a limit set could not
    judge (a group passes only when everything was judged)."""
    missing = (f"<span style='color:#b9770e'><b>not judged</b>: {not_judged_text(not_judged)}</span>"
               if not_judged else "")
    if verdict == "fail":
        text = f"<span style='color:#c0392b'><b>{fails} limit(s) exceeded</b></span>"
        return f"{text} · {missing}" if missing else text
    if verdict == "pass":
        return "no limits exceeded"
    if has_limits:
        return missing or "<span style='color:#b9770e'><b>limits set, nothing judged</b></span>"
    return "no limits set: nothing is judged"


def tree_header_html(key: str, res: RailResult, gv) -> str:
    """The Problems header of group ``key`` of a power tree (``gv``: its GroupVerdict)."""
    verdict = verdict_html(gv.verdict, gv.failed, gv.not_judged, has_limits(gv))
    return (f"<b>{key}</b> ({res.kind} of the power tree: {' + '.join(res.nets)}): {max_drop_text(res)} "
            f"· copper loss {res.power_loss * 1e3:.3g} mW · {verdict}")


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
            f"{res.power_loss * 1e3:.3g} mW ({split}) · {verdict}")


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
        self._revision = 0            # +1 per edit of the setup: a tree solved before is stale
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

        # Centre: pickers + view.
        self.view = BoardView()
        self.group_box = QComboBox()
        self.group_box.addItem("Rail solved alone", None)
        self.group_box.setToolTip("After Solve power tree: show a rail or ground group of the tree")
        self.layer_box = QComboBox()
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
        self.results_tabs.addTab(self.compare, "Compare")
        self.results_tabs.addTab(self.tree_panel, "Power tree")
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
        self.field_box.currentIndexChanged.connect(lambda *_: self.refresh_view())
        self.show_vias.toggled.connect(lambda *_: self.refresh_view())
        self.show_isolines.toggled.connect(lambda *_: self.refresh_view())
        self.show_arrows.toggled.connect(lambda *_: self.refresh_view())
        self.measure.toggled.connect(self._measure_toggled)
        self.view.probed.connect(lambda text: self.statusBar().showMessage(text))
        self.view.pad_picked.connect(self.pick_pad)
        self.view.add_hover_menu(self.menuBar().addMenu("View"))      # View → Hover shows
        self.compare.pin_requested.connect(self.pin_baseline)
        self.compare.clear_requested.connect(self.clear_baseline)
        self.rail_panel.pick_changed.connect(self._pick_mode_changed)
        self.rail_panel.banner.action_requested.connect(self._banner_action)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.stop_clicking)

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
                                     ("Solve", "F5", self.solve),
                                     ("Solve power tree", "Ctrl+T", self.solve_tree),
                                     ("Pin as baseline", "Ctrl+B", self.pin_baseline),
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

    def open_board(self, path, project: Optional[Project] = None) -> bool:
        self.statusBar().showMessage(f"Reading {Path(path).name}…")
        self.repaint()
        try:
            board = Board.open(path)
        except EngineError as e:
            QMessageBox.critical(self, "Cannot open board", str(e))
            return False
        self.board = board
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
        self.nets.set_board(board, self._configured_nets(), self.project.marked_rails, self._kicad_nets())
        self.problems.clear()
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
                                f"{'on' if on else 'off'} — press Solve (F5)")
            self._refresh_results()
            self.refresh_view()

    def open_project(self, path) -> bool:
        try:
            project = Project.load(path)
        except (OSError, ProjectError) as e:
            QMessageBox.critical(self, "Cannot open project", str(e))
            return False
        return self.open_board(project.board_path(), project)

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

    def _merge_again(self, reshow: bool = False):
        """Merge again after the project changed (or its file name, which the red line
        names), and show what that changes: the red line and the pinned note, the net list's
        marks, and the shown rail: anew when its origin changed (or ``reshow``), read-only
        or editable as the new origin says; else its banner only."""
        before = self._shown_origin()
        self._remerge()
        self._show_fields_error()
        if self.board is None:
            return
        self.nets.mark_configured(self._configured_nets(), self._kicad_nets())
        if not self.net:
            return
        if reshow or self._shown_origin() != before:
            rail = self._show_rail(self.net)
            self.rail_panel.set_net_kind(self._net_at_0v(rail))
        else:
            self._lock_drop(self.net)             # after a pad picked, the panel was filled again
            self._show_banner(self.net)

    def _shown_origin(self) -> Optional[str]:
        """Where the shown net's rail comes from (an Origin's source), None for no origin."""
        origin = self.merged.origins.get(self.net) if self.net else None
        return origin.source if origin is not None else None

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
        """(notes, warnings) of where the rail of ``net`` came from, for its result: the
        origin's, and the warnings about links on that net."""
        origin = self.merged.origins.get(net)
        if origin is None:
            return [], []
        own = {w for o in self.merged.origins.values() for w in o.warnings}
        refs = [k.ref for k in self.merged.project.links if net in (k.net_a, k.net_b)]
        links = [w for w in self.merged.warnings
                 if w not in own and any(f"link {ref} " in w for ref in refs)]
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
        a board elsewhere."""
        if self.worker is not None and self.worker.isRunning():
            self.statusBar().showMessage("A solve is running: export the report when it has finished")
            return
        if self.board is None or not self.results:
            self.statusBar().showMessage("Solve at least one rail first: the report shows the solved rails")
            return
        if self.report_worker is not None and self.report_worker.isRunning():
            self.statusBar().showMessage("A report is already being written")
            return
        start = self._report_start_path()
        path, _ = QFileDialog.getSaveFileName(self, "Export report", str(start), "HTML report (*.html)")
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() not in (".html", ".htm"):
            path = path.with_name(path.name + ".html")
        self.report_worker = ReportWorker(self.results.values(), copy.deepcopy(self.merged.project), path,
                                          self.board.outline(), self.board.name, self)
        self.report_worker.succeeded.connect(self._report_written)
        self.report_worker.failed.connect(self._report_failed)
        self.statusBar().showMessage(f"Writing the report of {len(self.results)} rail(s)… "
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
            folder = project.parent / "results"
            folder.mkdir(exist_ok=True)
        else:
            stem, folder = (self.board.name.split(".")[0] or "coppernicus"), Path.home()
        return folder / f"{stem}_report.html"

    def _report_written(self, path):
        path = Path(path)
        size_mb = path.stat().st_size / 1e6
        self.statusBar().showMessage(f"Report written: {path.name} ({size_mb:.2f} MB) in {path.parent}")
        answer = QMessageBox.question(self, "Report written",
                                      f"{path}\n{size_mb:.2f} MB. Open it in the browser?",
                                      QMessageBox.Open | QMessageBox.Close)
        if answer == QMessageBox.Open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

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
            return f"<b>{self.net}</b>: rail from KiCad footprint fields — press Solve (F5)"
        return f"<b>{self.net}</b>: add a source and a load, then Solve (F5)"

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
        if not fields_view.confirm_use_fields(self, net, sensed):
            return False
        own.override_board_fields = False
        own.sources, own.loads = [], []
        self._origin_changed(net)
        return True

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
            self.problems.clear(f"<b>{changed}</b>: setup changed since the last solve — press Solve (F5)")
            self._refresh_results()
        self.refresh_view()

    def _limits_edited(self):
        rail = self._adopt_draft()
        self.rail_panel.set_net_kind(self._net_at_0v(rail))       # a cleared field may close
        if self.tree is not None:                                  # re-judge, no re-solve
            apply_limits(self.tree, self.merged.project)
            self.tree_panel.set_tree(self.tree, self._tree_verdict())
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
            self.problems.clear("Solve settings changed — press Solve (F5) to recompute")
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
                                 difference_mv=diff_mv, difference_note=diff_note)
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
        self.measure.setChecked(False)

    def _measure_toggled(self, on: bool):
        if on:
            self.rail_panel.set_pick("")                  # one click mode at a time
            self.view.set_click_mode("measure")
            self.statusBar().showMessage("Click point A, then point B · Esc to stop")
        elif self.view.click_mode == "measure":
            self.view.set_click_mode(None)

    def _pick_mode_changed(self, kind: str):
        if kind:
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
        self.tree_panel.clear("<b>Power tree</b>: solving…")
        self.results_tabs.setCurrentWidget(self.tree_panel)
        self.tree_worker = TreeWorker(self.board, self.merged.project, self)
        self.tree_worker.merged = self.merged          # its notes and warnings, for the result
        self.tree_worker.generation = self._generation
        self.tree_worker.revision = self._revision
        self.tree_worker.message.connect(lambda m: self.statusBar().showMessage(m))
        self.tree_worker.succeeded.connect(self._tree_solved)
        self.tree_worker.failed.connect(self._tree_failed)
        self.tree_worker.start()

    def _tree_solved(self, tree: TreeResult):
        if self._is_stale():
            return
        if getattr(self.sender(), "revision", self._revision) != self._revision:
            msg = "The setup changed while the power tree was solving: solve it again (Ctrl+T)"
            self.tree_panel.clear(msg)
            self.statusBar().showMessage(msg)
            return
        merged = getattr(self.sender(), "merged", None)
        if merged is not None:
            merged.annotate(tree)                   # where the rails came from, as the CLI says
        # Limits do not change the solve: judge it by the ones set now (one may have been
        # edited while it solved, on the copy the worker took).
        apply_limits(tree, self.merged.project)
        self.tree = tree
        self.tree_panel.set_tree(tree, self._tree_verdict())
        self.group_box.blockSignals(True)
        while self.group_box.count() > 1:
            self.group_box.removeItem(1)
        for key, res in tree.groups.items():
            self.group_box.addItem(f"{key} ({res.kind})" if res.kind == "ground" else key, key)
        self.group_box.blockSignals(False)
        self.statusBar().showMessage(f"Power tree solved in {tree.stats['seconds']:.1f} s: "
                                     f"{len(tree.groups)} groups · pick one in Group to see it")

    def _tree_failed(self, message: str):
        if self._is_stale():
            return
        self.tree_panel.clear("<b>Power tree</b>: cannot solve")
        QMessageBox.warning(self, "Cannot solve the power tree", message)

    def _tree_verdict(self) -> str:
        if self.tree is None:
            return ""
        overall, groups = tree_verdicts(self.tree)
        unsolved = unsolved_limits(self.tree)
        return verdict_html(overall, sum(v.failed for v in groups.values()),
                            [x for v in groups.values() for x in v.not_judged] + unsolved,
                            any(has_limits(v) for v in groups.values()) or bool(unsolved))

    def _drop_tree(self, reason: str):
        """The power-tree result no longer matches the project (or the board)."""
        had = self.tree is not None
        self.tree = None
        shown = self.group_box.currentData() is not None
        self.group_box.blockSignals(True)
        while self.group_box.count() > 1:
            self.group_box.removeItem(1)
        self.group_box.setCurrentIndex(0)
        self.group_box.blockSignals(False)
        if had or reason == "":
            self.tree_panel.clear(reason)
        if shown and self.net:
            self._show_net_view()

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
            return None, None, ("No baseline yet. Solve, press 'Pin as baseline', change the loads, the "
                                "settings or the board, and solve again.")
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
            self.tree_worker.wait(5000)
        if self.report_worker is not None and self.report_worker.isRunning():
            # A report cannot be cancelled halfway: finish the file, without asking to open it.
            self.report_worker.succeeded.disconnect()
            self.report_worker.failed.disconnect()
            self.statusBar().showMessage("Finishing the report before closing…")
            self.report_worker.wait()
        self.view.close()
        super().closeEvent(event)
