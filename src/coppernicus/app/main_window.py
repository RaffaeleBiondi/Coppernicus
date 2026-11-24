"""Main window: one loop — open a board, pick a net, add sources and loads, solve, read the
maps and the ranked problems, click a problem to go there.

Layout: nets on the left, the 2D board view in the centre (layer and field pickers on
top), the rail setup on the right, problems and notes at the bottom.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from coppernicus import __version__
from coppernicus.app.board_view import FIELDS, BoardView, copper_rings
from coppernicus.app.comparing import Run, compare_runs
from coppernicus.app.panels import ComparePanel, LossChart, NetPanel, ProblemsPanel, RailPanel
from coppernicus.app.picking import add_picked_pin, pick_nets
from coppernicus.app.worker import SolveWorker
from coppernicus.compare import difference_layer, same_mesh
from coppernicus.engine import Board, EngineError, tied_nets
from coppernicus.engine.board import board_file_filter
from coppernicus.problems import FAIL, find_problems
from coppernicus.project import Project, ProjectError, Rail
from coppernicus.results import RailResult

PROJECT_FILTER = "Coppernicus project (*.coppernicus.yml *.yml *.yaml)"


class MainWindow(QMainWindow):
    def __init__(self):
        # One number format whatever the system locale: a dot for decimals and no group
        # separators, as in the limits, the problems list and the project file. Must be set
        # before any numeric widget exists.
        QLocale.setDefault(QLocale.c())
        super().__init__()
        self.setWindowTitle(f"Coppernicus {__version__}")
        self.resize(1500, 920)
        self.project = Project()
        self.board: Optional[Board] = None
        self.net: Optional[str] = None
        self.results: Dict[str, RailResult] = {}
        self.worker: Optional[SolveWorker] = None
        self.dirty = False
        self.last_render_s = 0.0
        self._candidates: Dict[Tuple, List[str]] = {}      # pickable pad ids per set of nets
        self._generation = 0          # +1 per opened board: a solve started before is stale
        self.runs: Dict[str, Run] = {}            # the latest solve per net, with its inputs
        self.baseline: Optional[Run] = None       # pinned "before"; survives opening a board
        self._board_stamp = ""                    # which board file, and which version of it
        self._diff_cache: Tuple = ((), None)

        # Centre: pickers + view.
        self.view = BoardView()
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
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Layer"))
        bar.addWidget(self.layer_box)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Show"))
        bar.addWidget(self.field_box)
        bar.addWidget(self.show_vias)
        bar.addWidget(self.show_isolines)
        bar.addWidget(self.show_arrows)
        bar.addSpacing(12)
        bar.addWidget(self.measure)
        bar.addStretch(1)
        bar.addWidget(QLabel("<small>drag: pan · wheel: zoom · double-click: read values</small>"))
        bar.addWidget(fit)
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(4, 4, 4, 0)
        cl.addLayout(bar)
        cl.addWidget(self.view, 1)
        self.setCentralWidget(centre)

        # Docks.
        self.nets = NetPanel()
        self.rail_panel = RailPanel()
        self.problems = ProblemsPanel()
        self.losses = LossChart()
        self.compare = ComparePanel()
        self.results_tabs = QTabWidget()
        self.results_tabs.addTab(self.problems, "Problems")
        self.results_tabs.addTab(self.losses, "Loss by layer")
        self.results_tabs.addTab(self.compare, "Compare")
        self._dock("Nets", self.nets, Qt.LeftDockWidgetArea)
        self._dock("Rail setup", self.rail_panel, Qt.RightDockWidgetArea)
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
        self.field_box.currentIndexChanged.connect(lambda *_: self.refresh_view())
        self.show_vias.toggled.connect(lambda *_: self.refresh_view())
        self.show_isolines.toggled.connect(lambda *_: self.refresh_view())
        self.show_arrows.toggled.connect(lambda *_: self.refresh_view())
        self.measure.toggled.connect(self._measure_toggled)
        self.view.probed.connect(lambda text: self.statusBar().showMessage(text))
        self.view.pad_picked.connect(self.pick_pad)
        self.compare.pin_requested.connect(self.pin_baseline)
        self.compare.clear_requested.connect(self.clear_baseline)
        self.rail_panel.pick_changed.connect(self._pick_mode_changed)
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
        for text, shortcut, slot in (("Open board…", QKeySequence.Open, self.open_board_dialog),
                                     ("Open project…", "Ctrl+Shift+O", self.open_project_dialog),
                                     ("Save project", QKeySequence.Save, self.save_project),
                                     ("Save project as…", QKeySequence.SaveAs, self.save_project_as),
                                     (None, None, None),
                                     ("Solve", "F5", self.solve),
                                     ("Pin as baseline", "Ctrl+B", self.pin_baseline),
                                     ("Export maps…", "Ctrl+E", self.export_maps)):
            if text is None:
                tb.addSeparator()
                continue
            act = QAction(text, self)
            act.setShortcut(QKeySequence(shortcut))
            act.triggered.connect(slot)
            tb.addAction(act)

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
        resolved = Path(path).resolve()
        modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(resolved.stat().st_mtime))
        self._board_stamp = f"{resolved.name} (file modified {modified})"
        self.results.clear()
        self.runs.clear()
        self._candidates.clear()
        self.rail_panel.set_pick("")
        self.measure.setChecked(False)
        self.net, self._rail_draft = None, None          # nothing of the previous board survives
        self._generation += 1
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()                         # and _solved ignores it if it ends
        self.rail_panel.clear_rail()
        self.view.reset()
        self.layer_box.blockSignals(True)
        self.layer_box.clear()
        self.layer_box.blockSignals(False)
        self.rail_panel.set_settings(self.project.settings)
        self.nets.set_board(board, [r.net for r in self.project.rails if r.sources or r.loads],
                            self.project.marked_rails)
        self.problems.clear()
        self.dirty = project is None
        self._update_title()
        warn = f" — {len(board.warnings)} import warning(s), see Problems notes" if board.warnings else ""
        self.statusBar().showMessage(f"{board.name}: {len(board.nets())} nets, "
                                     f"{len(board.layers())} copper layers{warn}")
        if board.warnings:
            self.problems.notes.setPlainText("Board import:\n" + "\n".join(board.warnings))
        first = next((r.net for r in self.project.rails), None)
        if first:
            self.nets.select(first)
        else:
            self.view.show_board(board.outline(), fit=True)
        return True

    def open_project(self, path) -> bool:
        try:
            project = Project.load(path)
        except (OSError, ProjectError) as e:
            QMessageBox.critical(self, "Cannot open project", str(e))
            return False
        return self.open_board(project.board_path(), project)

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
        self.project.rails = [r for r in self.project.rails if r.sources or r.loads]
        try:
            self.project.save(path)
        except (OSError, ProjectError) as e:
            QMessageBox.critical(self, "Cannot save project", str(e))
            return
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
        rail = self.project.rail(self.net) or Rail(self.net)
        written = save_layer_maps(res, self.board.outline(), folder, find_problems(res, rail.limits))
        self.statusBar().showMessage(f"Wrote {len(written)} maps to {folder}")

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
        rail = self.project.rail(net) or Rail(net)
        _, ties = tied_nets(self.board, net)
        self.rail_panel.set_rail(self.board, rail, ties)
        self._rail_draft = rail
        self.layer_box.blockSignals(True)
        self.layer_box.clear()
        for lid in self.board.net_layers(net):
            self.layer_box.addItem(self.board.layer_name(lid), lid)
        self.layer_box.blockSignals(False)
        res = self.results.get(net)
        if res is not None:
            self._show_problems(res)
        else:
            self.problems.clear(f"<b>{net}</b>: add a source and a load, then Solve (F5)")
        self._refresh_results()
        self.refresh_view(fit=True)

    def _adopt_draft(self):
        rail = self._rail_draft
        has_content = rail.sources or rail.loads or any(v is not None for v in vars(rail.limits).values())
        if self.project.rail(rail.net) is None and has_content:
            self.project.rails.append(rail)
        self.nets.mark_configured([r.net for r in self.project.rails if r.sources or r.loads])
        self.dirty = True
        self._update_title()
        return rail

    def _rail_edited(self):
        rail = self._adopt_draft()
        self.runs.pop(rail.net, None)
        if self.results.pop(rail.net, None) is not None:          # the old result no longer applies
            self.problems.clear(f"<b>{rail.net}</b>: setup changed since the last solve — press Solve (F5)")
            self._refresh_results()
        self.refresh_view()

    def _limits_edited(self):
        rail = self._adopt_draft()
        res = self.results.get(rail.net)
        if res is not None:                                        # re-rank; the solve is still valid
            self._show_problems(res)

    def _marked_edited(self, nets):
        self.project.marked_rails = list(nets)
        self.dirty = True
        self._update_title()

    def _settings_edited(self):
        self.dirty = True
        self._update_title()
        if self.results:                                           # every result used the old settings
            self.results.clear()
            self.runs.clear()
            self.problems.clear("Solve settings changed — press Solve (F5) to recompute")
            self._refresh_results()
            self.refresh_view()

    def refresh_view(self, fit: bool = False):
        if self.board is None or not self.net:
            return
        layer_name = self.layer_box.currentText()
        lid = self.layer_box.currentData()
        res = self.results.get(self.net)
        rail = self.project.rail(self.net) or getattr(self, "_rail_draft", None)
        t0 = time.perf_counter()
        if res is not None and res.layer(layer_name) is not None:
            pads = [("source", [i for t in res.sources for i in t.pad_ids]),
                    ("load", [i for t in res.loads for i in t.pad_ids])]
            field, diff_mv, diff_note = self.field_box.currentData(), None, ""
            if field == "difference":
                diff_mv, diff_note, why = self._difference(res, layer_name)
                if diff_mv is None:                  # not available: say why, show the drop
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
        if self.worker is not None and self.worker.isRunning():
            self.statusBar().showMessage("A solve is already running")
            return
        rail = self.project.rail(self.net) or self._rail_draft
        self.rail_panel.solve.setEnabled(False)
        self.problems.clear(f"<b>{self.net}</b>: solving…")
        self.worker = SolveWorker(self.board, rail, self.project.settings, self)
        self.worker.generation = self._generation
        # What produced the result, as it is now (the user may edit while it runs).
        self.worker.run_inputs = (copy.deepcopy(rail), copy.deepcopy(self.project.settings),
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
        rail, settings, board = inputs or (self.project.rail(res.net) or Rail(res.net),
                                           self.project.settings, self._board_stamp)
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
        self.statusBar().showMessage(
            f"{res.net} solved in {res.stats['seconds']:.1f} s · {int(res.stats['triangles']):,} triangles · "
            f"drawn in {self.last_render_s:.2f} s")

    # ------------------------------------------------------------------ before / after
    def pin_baseline(self):
        """Keep the shown rail's latest result as the baseline ("before")."""
        run = self.runs.get(self.net) if self.net else None
        if run is None:
            self.statusBar().showMessage("Solve the rail first, then pin its result as the baseline")
            return
        self.baseline = run
        self._refresh_results()
        self.statusBar().showMessage(f"Baseline: {self.net} on {run.board}. Change something and solve again "
                                     "to compare (Compare tab, 'Δ drop vs baseline' map)")

    def clear_baseline(self):
        self.baseline = None
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
        key = (id(base), id(run), layer_name)
        if self._diff_cache[0] != key:
            diff = difference_layer(layer, base_layer)          # V_base − V_current per node
            # Δ drop = (V_ref − V)_current − (V_ref − V)_baseline
            mv = (diff.voltage + (res.v_ref - base.result.v_ref)) * 1e3
            note = ("exact: same mesh" if same_mesh(layer, base_layer)
                    else "interpolated: the two solves have different meshes")
            self._diff_cache = (key, (mv, note))
        mv, note = self._diff_cache[1]
        return mv, note, ""

    def _solve_failed(self, message: str):
        if self._is_stale():
            return
        self.problems.clear(f"<b>{self.net}</b>: cannot solve")
        self.problems.notes.setPlainText(message)
        QMessageBox.warning(self, "Cannot solve", message)

    def _show_problems(self, res: RailResult):
        rail = self.project.rail(res.net) or Rail(res.net)
        problems = find_problems(res, rail.limits)
        fails = sum(p.severity == FAIL for p in problems)
        shares = res.loss_breakdown()
        total = sum(shares.values()) or 1.0
        split = ", ".join(f"{k} {v / total:.0%}" for k, v in sorted(shares.items(), key=lambda kv: -kv[1])
                          if v / total >= 0.01)
        worst = max(res.loads, key=res.drop_pct) if res.loads else None
        worst_txt = (f"worst load <b>{worst.name}</b> {worst.v_min:.4f} V "
                     f"({res.drop(worst) * 1e3:.2f} mV, {res.drop_pct(worst):.2f} %) · ") if worst else ""
        verdict = (f"<span style='color:#c0392b'><b>{fails} limit(s) exceeded</b></span>" if fails
                   else ("no limits exceeded" if any(vars(rail.limits).values()) else "no limits set"))
        self.problems.set_result(
            f"<b>{res.net}</b>: {worst_txt}max drop {res.max_drop * 1e3:.2f} mV · copper loss "
            f"{res.power_loss * 1e3:.3g} mW ({split}) · {verdict}",
            problems, res.warnings + res.assumptions)

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
            self.worker.cancel()
            self.worker.wait(5000)
        self.view.close()
        super().closeEvent(event)
