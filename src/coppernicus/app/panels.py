"""Side panels: the net list, the rail setup (sources, loads, limits, settings) and the
problems list. They edit plain :mod:`coppernicus.project` objects and never touch the
engine directly; the main window wires them together."""

from __future__ import annotations

from typing import Iterable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from coppernicus.engine import Board, NetTie, PartPins
from coppernicus.problems import FAIL, Problem
from coppernicus.project import Limits, Load, Rail, Settings, Source, carry_over, parse_pins, pins_text

# ---------------------------------------------------------------------- numbers


class DecimalSpinBox(QDoubleSpinBox):
    """A spin box that reads a typed comma as the decimal sign. Numbers are shown with a dot
    (the app uses the C locale), where a comma would otherwise be taken as a thousands
    separator and dropped, turning 3,3 into 33."""

    def validate(self, text, pos):
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text):
        return super().valueFromText(text.replace(",", "."))


class DecimalValidator(QDoubleValidator):
    """A validator for decimal fields that reads a typed comma as the decimal sign (see
    :class:`DecimalSpinBox`)."""

    def validate(self, text, pos):
        return super().validate(text.replace(",", "."), pos)


# ---------------------------------------------------------------------- nets


def rail_nets(nets: Iterable[str], heuristic: Iterable[str], marked: Iterable[str],
              configured: Iterable[str]) -> List[str]:
    """The nets the "Rails only" filter shows, in board order: power and ground nets found by
    the name/zone heuristic, nets marked as rails by hand, and nets with sources or loads."""
    keep = set(heuristic) | set(marked) | set(configured)
    return [n for n in nets if n in keep]


class NetPanel(QWidget):
    net_selected = Signal(str)
    marked_changed = Signal(list)       # the nets marked as rails by hand

    def __init__(self, parent=None):
        super().__init__(parent)
        self._board: Optional[Board] = None
        self._configured: set = set()
        self._marked: List[str] = []
        self.filter = QLineEdit(placeholderText="Filter nets…")
        self.power_only = QCheckBox("Rails only")
        self.power_only.setToolTip("Power and ground nets found by name and zones, nets you marked "
                                   "as rails (right-click a net), and nets with sources or loads")
        self.power_only.setChecked(True)
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.filter)
        layout.addWidget(self.power_only)
        layout.addWidget(self.list)
        self.filter.textChanged.connect(self._refill)
        self.power_only.toggled.connect(self._refill)
        self.list.currentTextChanged.connect(self._emit)

    def set_board(self, board: Optional[Board], configured: List[str] = (), marked: List[str] = ()):
        """A new board: nothing is selected, so selecting any net afterwards (even one with
        the same name as before) emits :attr:`net_selected`."""
        self._board = board
        self._configured = set(configured)
        self._marked = list(marked)
        self._refill(keep=False)

    @property
    def marked(self) -> List[str]:
        return list(self._marked)

    def set_marked(self, net: str, on: bool):
        """Mark ``net`` as a rail by hand, or unmark it; emits :attr:`marked_changed`."""
        if on == (net in self._marked):
            return
        self._marked = [*self._marked, net] if on else [n for n in self._marked if n != net]
        self._refill(keep=True)
        self.marked_changed.emit(list(self._marked))

    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None or self._board is None:
            return
        net = item.text()
        menu = QMenu(self)
        if net in self._marked:
            menu.addAction("Unmark as rail", lambda: self.set_marked(net, False))
        elif self._board.net_kind(net) in ("power", "ground"):
            found = menu.addAction(f"Rail ({self._board.net_kind(net)}, found by name and zones)")
            found.setEnabled(False)
        else:
            menu.addAction("Mark as rail", lambda: self.set_marked(net, True))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def mark_configured(self, nets: List[str]):
        self._configured = set(nets)
        self._refill(keep=True)

    def select(self, net: str):
        items = self.list.findItems(net, Qt.MatchExactly)
        if not items:
            self.power_only.setChecked(False)
            self.filter.clear()
            items = self.list.findItems(net, Qt.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])

    def _refill(self, *_args, keep: bool = True):
        current = self.list.currentItem().text() if keep and self.list.currentItem() else None
        self.list.blockSignals(True)
        self.list.clear()
        if self._board is not None:
            nets = self._board.nets()
            if self.power_only.isChecked():
                nets = rail_nets(nets, self._board.power_nets(), self._marked, self._configured)
            text = self.filter.text().strip().lower()
            for n in nets:
                if text and text not in n.lower():
                    continue
                self.list.addItem(n)
                item = self.list.item(self.list.count() - 1)
                f = item.font()
                tips = []
                if n in self._configured:
                    f.setBold(True)
                    tips.append("has sources/loads in this project")
                if n in self._marked:
                    f.setItalic(True)
                    tips.append("marked as a rail by hand")
                item.setFont(f)
                if tips:
                    item.setToolTip("; ".join(tips).capitalize())
            if current:
                found = self.list.findItems(current, Qt.MatchExactly)
                if found:
                    self.list.setCurrentItem(found[0])
        self.list.blockSignals(False)

    def _emit(self, text: str):
        if text:
            self.net_selected.emit(text)


# ---------------------------------------------------------------------- rail setup


class TerminalTable(QWidget):
    """Sources or loads of a rail: name, part (picked from the parts on the net), pins, value.
    The "Pick on board" toggle asks the main window to add pads clicked in the view."""
    changed = Signal()
    pick_toggled = Signal(bool)

    def __init__(self, title: str, value_label: str, unit: str, parent=None):
        super().__init__(parent)
        self._unit = unit
        self._parts: List[PartPins] = []
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Part", "Pins", f"{value_label} ({unit})"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(110)
        add, remove = QPushButton("Add"), QPushButton("Remove")
        add.clicked.connect(self.add_row)
        remove.clicked.connect(self.remove_row)
        self.pick = QPushButton("Pick on board")
        self.pick.setCheckable(True)
        self.pick.setToolTip("Click pads in the view to add them here; a pin joins the row of its "
                             "part. Esc or a second press stops picking.")
        self.pick.toggled.connect(self.pick_toggled.emit)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel(f"<b>{title}</b>"))
        buttons.addStretch(1)
        buttons.addWidget(self.pick)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self.table)
        self.table.itemChanged.connect(lambda *_: self.changed.emit())

    def set_parts(self, parts: List[PartPins]):
        self._parts = parts

    def set_rows(self, rows):
        """``rows``: (name, ref, pins, value) tuples."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for row in rows:
            self._append(*row)
        self.table.blockSignals(False)

    def rows(self):
        out = []
        for r in range(self.table.rowCount()):
            part, pins_edit, spin = (self.table.cellWidget(r, c) for c in (1, 2, 3))
            if part is None or pins_edit is None or spin is None:
                continue
            name = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            out.append((name, part.currentData() or "", parse_pins(pins_edit.text()), spin.value()))
        return out

    def add_row(self):
        used = {row[1] for row in self.rows()}
        fallback = self._parts[0].ref if self._parts else ""
        ref = next((p.ref for p in self._parts if p.ref not in used), fallback)
        self._append("", ref, None, 0.0)
        self.changed.emit()

    def remove_row(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for r in rows:
            self.table.removeRow(r)
        self.changed.emit()

    def _append(self, name, ref, pins, value):
        blocked = self.table.blockSignals(True)     # no itemChanged before the row is complete
        try:
            self._build_row(name, ref, pins, value)
        finally:
            self.table.blockSignals(blocked)

    def _build_row(self, name, ref, pins, value):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        part = QComboBox()
        for p in self._parts:
            part.addItem(f"{p.ref}  {p.value}".strip(), p.ref)
            part.setItemData(part.count() - 1, f"pins on this net: {', '.join(p.pins)}", Qt.ToolTipRole)
        if ref and part.findData(ref) < 0:
            part.addItem(f"{ref} (not on this net)", ref)
        part.setCurrentIndex(max(0, part.findData(ref)))
        part.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.table.setCellWidget(r, 1, part)
        pins_edit = QLineEdit(pins_text(pins))
        pins_edit.setToolTip("Pad numbers, comma separated; globs like A* work; 'all' = every pin "
                             "of the part on this net")
        pins_edit.editingFinished.connect(self.changed.emit)
        self.table.setCellWidget(r, 2, pins_edit)
        spin = DecimalSpinBox()
        spin.setDecimals(4)
        spin.setRange(0.0, 1e4)
        spin.setValue(float(value))
        spin.setSuffix(f" {self._unit}")
        spin.valueChanged.connect(lambda *_: self.changed.emit())
        self.table.setCellWidget(r, 3, spin)


def _optional_float(edit: QLineEdit) -> Optional[float]:
    text = edit.text().strip().replace(",", ".")
    return float(text) if text else None


class RailPanel(QWidget):
    """Setup of the selected net's rail and the project's solve settings."""
    rail_changed = Signal()          # sources, loads, pins, net-tie option: the result is stale
    limits_changed = Signal()        # limits only: re-rank the problems, no solve needed
    settings_changed = Signal()
    solve_requested = Signal()
    pick_changed = Signal(str)       # "source", "load" or "" (no picking)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rail: Optional[Rail] = None
        self._loading = False
        self.title = QLabel("<i>Open a board and pick a net</i>")
        self.sources = TerminalTable("Sources (regulator outputs)", "Voltage", "V")
        self.loads = TerminalTable("Loads (current split equally across pins)", "Current", "A")
        self.ties = QLabel()
        self.ties.setWordWrap(True)
        self.follow_ties = QCheckBox("Solve nets joined by net ties as one (tie = short)")

        limits = QGroupBox("Limits (empty = not checked; problems are still ranked)")
        self.max_drop = QLineEdit(placeholderText="e.g. 3")
        self.max_j = QLineEdit(placeholderText="A/mm²")
        self.max_via = QLineEdit(placeholderText="A")
        for e in (self.max_drop, self.max_j, self.max_via):
            e.setValidator(DecimalValidator(0.0, 1e9, 6))
        form = QFormLayout(limits)
        form.addRow("Max drop (%)", self.max_drop)
        form.addRow("Max current density (A/mm²)", self.max_j)
        form.addRow("Max via current (A)", self.max_via)

        settings = QGroupBox("Solve settings (whole project)")
        self.max_edge = DecimalSpinBox(decimals=3, minimum=0.02, maximum=5.0, singleStep=0.1, suffix=" mm")
        self.plating = QLineEdit(placeholderText="engine defaults (IPC-6012 Class 2)")
        self.plating.setValidator(DecimalValidator(0.1, 1000.0, 3))
        self.plating.setToolTip("Via barrel plating in µm for every plated hole. Empty: the engine's "
                                "table (micro 12, buried 13, others 20 µm). Ask your fabricator.")
        self.temp = DecimalSpinBox(decimals=1, minimum=-55.0, maximum=250.0, suffix=" °C")
        self.temp.setToolTip("Copper temperature; resistivity is referenced to 20 °C")
        sform = QFormLayout(settings)
        sform.addRow("Largest mesh edge", self.max_edge)
        sform.addRow("Via plating (µm)", self.plating)
        sform.addRow("Copper temperature", self.temp)

        self.solve = QPushButton("Solve  (F5)")
        self.solve.setMinimumHeight(34)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        for w in (self.title, self.sources, self.loads, self.ties, self.follow_ties, limits,
                  settings, self.solve):
            layout.addWidget(w)
        layout.addStretch(1)

        self.sources.changed.connect(self._write_rail)
        self.loads.changed.connect(self._write_rail)
        self.sources.pick_toggled.connect(lambda on: self._pick_toggled("source", on))
        self.loads.pick_toggled.connect(lambda on: self._pick_toggled("load", on))
        self.follow_ties.toggled.connect(self._write_rail)
        for e in (self.max_drop, self.max_j, self.max_via):
            e.editingFinished.connect(self._write_limits)
        self.max_edge.valueChanged.connect(self._write_settings)
        self.temp.valueChanged.connect(self._write_settings)
        self.plating.editingFinished.connect(self._write_settings)
        self.solve.clicked.connect(self.solve_requested.emit)
        self._settings: Optional[Settings] = None
        self.setEnabled(False)

    # ------------------------------------------------------------------ picking
    @property
    def pick_kind(self) -> str:
        """"source", "load" or "" — which table the pads clicked in the view go to."""
        return "source" if self.sources.pick.isChecked() else "load" if self.loads.pick.isChecked() else ""

    def set_pick(self, kind: str):
        """Turn picking on for "source" or "load", or off with ""."""
        for k, table in (("source", self.sources), ("load", self.loads)):
            if table.pick.isChecked() != (k == kind):
                table.pick.setChecked(k == kind)

    def _pick_toggled(self, kind: str, on: bool):
        other = self.loads if kind == "source" else self.sources
        if on and other.pick.isChecked():
            other.pick.blockSignals(True)              # one table at a time
            other.pick.setChecked(False)
            other.pick.blockSignals(False)
        self.pick_changed.emit(self.pick_kind)

    # ------------------------------------------------------------------ fill
    def set_settings(self, settings: Settings):
        self._loading = True
        self._settings = settings
        self.max_edge.setValue(settings.max_edge_mm)
        self.plating.setText("" if settings.via_plating_um is None else f"{settings.via_plating_um:g}")
        self.temp.setValue(settings.copper_temp_c)
        self._loading = False

    def set_rail(self, board: Board, rail: Rail, ties: List[NetTie]):
        self._loading = True
        self._rail = rail
        self.setEnabled(True)
        parts = board.parts_on_net(rail.net)
        self.title.setText(f"<h3>{rail.net}</h3>{len(parts)} parts on this net")
        self.sources.set_parts(parts)
        self.loads.set_parts(parts)
        self.sources.set_rows([(s.name, s.ref, s.pins, s.voltage) for s in rail.sources])
        self.loads.set_rows([(ld.name, ld.ref, ld.pins, ld.current) for ld in rail.loads])
        self.ties.setText("Net ties: " + ", ".join(f"{t.ref} ({t.net_a} ↔ {t.net_b})" for t in ties)
                          if ties else "")
        self.ties.setVisible(bool(ties))
        self.follow_ties.setVisible(bool(ties))
        self.follow_ties.setChecked(rail.follow_net_ties)
        lim = rail.limits
        for edit, value in ((self.max_drop, lim.max_drop_pct), (self.max_j, lim.max_current_density),
                            (self.max_via, lim.max_via_current)):
            edit.setText("" if value is None else f"{value:g}")
        self._loading = False

    def clear_rail(self):
        """No net selected (a new board was opened)."""
        self._loading = True
        self._rail = None
        self.title.setText("<i>Pick a net</i>")
        self.sources.set_rows([])
        self.loads.set_rows([])
        for edit in (self.max_drop, self.max_j, self.max_via):
            edit.clear()
        self.ties.setVisible(False)
        self.follow_ties.setVisible(False)
        self.setEnabled(False)
        self._loading = False

    # ------------------------------------------------------------------ write back
    def _write_rail(self, *_):
        if self._loading or self._rail is None:
            return
        # The tables show name, part, pins and value; every other field (a regulator's kind,
        # input, ground and sense pins, a load's ground pins) is kept from the row's part.
        self._rail.sources = carry_over(self._rail.sources, [
            Source(ref, value, pins, name) for name, ref, pins, value in self.sources.rows() if ref])
        self._rail.loads = carry_over(self._rail.loads, [
            Load(ref, value, pins, name) for name, ref, pins, value in self.loads.rows() if ref])
        self._rail.follow_net_ties = self.follow_ties.isChecked()
        self.rail_changed.emit()

    def _write_limits(self, *_):
        if self._loading or self._rail is None:
            return
        limits = Limits(_optional_float(self.max_drop), _optional_float(self.max_j),
                        _optional_float(self.max_via))
        if limits != self._rail.limits:
            self._rail.limits = limits
            self.limits_changed.emit()

    def _write_settings(self, *_):
        if self._loading or self._settings is None:
            return
        self._settings.max_edge_mm = self.max_edge.value()
        self._settings.via_plating_um = _optional_float(self.plating)
        self._settings.copper_temp_c = self.temp.value()
        self.settings_changed.emit()


# ---------------------------------------------------------------------- problems


class ProblemsPanel(QWidget):
    problem_activated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = QLabel("<i>No results yet</i>")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Problem", "Layer", "Where (mm)", "Detail"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(170)
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMaximumHeight(72)
        self.notes.setPlaceholderText("Assumptions and warnings of the last solve")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.summary)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.notes)
        self.tree.itemClicked.connect(self._activated)
        self._problems: List[Problem] = []

    def clear(self, text: str = "<i>No results yet</i>"):
        self.summary.setText(text)
        self.tree.clear()
        self.notes.clear()
        self._problems = []

    def set_result(self, summary: str, problems: List[Problem], notes: List[str]):
        self.summary.setText(summary)
        self.tree.clear()
        self._problems = list(problems)
        for i, p in enumerate(problems):
            item = QTreeWidgetItem(["FAIL" if p.severity == FAIL else "", p.title, p.layer or "",
                                    f"{p.x:.2f}, {p.y:.2f}", p.detail])
            item.setData(0, Qt.UserRole, i)
            item.setToolTip(1, p.detail)
            if p.severity == FAIL:
                for col in range(5):
                    item.setForeground(col, QBrush(QColor("#c0392b")))
                f = item.font(0)
                f.setBold(True)
                item.setFont(0, f)
            self.tree.addTopLevelItem(item)
        self.notes.setPlainText("\n".join(notes))

    def _activated(self, item: QTreeWidgetItem, _col: int):
        i = item.data(0, Qt.UserRole)
        if i is not None:
            self.problem_activated.emit(self._problems[int(i)])


# ---------------------------------------------------------------------- losses and comparison

# Chart colours (the dataviz reference palette, validated for colour-vision deficiency):
# the current result is always categorical slot 1, the baseline always slot 2, so a colour
# never changes meaning when the baseline appears.
CURRENT_COLOR, BASELINE_COLOR = "#2a78d6", "#eb6834"
CHART_SURFACE, INK, INK_2, INK_MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"


class LossChart(QWidget):
    """Copper loss per layer (and vias, series links) as horizontal bars in mW, with the
    baseline beside the current result when one is pinned for the same rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        # "constrained" re-fits the labels at every draw, so they survive a resized dock.
        self.figure = Figure(figsize=(6, 2.6), facecolor=CHART_SURFACE, layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)
        self.series: List[str] = []
        self.set_losses(None)

    def set_losses(self, current, baseline=None, title: str = ""):
        """``current`` / ``baseline``: {name: W} as :meth:`RailResult.loss_breakdown` gives."""
        import numpy as np
        fig = self.figure
        fig.clear()
        ax = fig.add_subplot()
        ax.set_facecolor(CHART_SURFACE)
        if not current:
            ax.axis("off")
            ax.text(0.5, 0.5, "Solve a rail to see where its copper loss is", ha="center",
                    va="center", color=INK_MUTED, transform=ax.transAxes)
            self.series = []
            self.canvas.draw_idle()
            return
        names = list(current) + [k for k in (baseline or {}) if k not in current]
        y = np.arange(len(names))
        cur = np.array([current.get(n, 0.0) * 1e3 for n in names])
        bars = []
        if baseline:
            base = np.array([baseline.get(n, 0.0) * 1e3 for n in names])
            h = 0.36
            bars.append(ax.barh(y - h / 2 - 0.02, base, height=h, color=BASELINE_COLOR, label="baseline"))
            bars.append(ax.barh(y + h / 2 + 0.02, cur, height=h, color=CURRENT_COLOR, label="current"))
            self.series = ["baseline", "current"]
        else:
            bars.append(ax.barh(y, cur, height=0.5, color=CURRENT_COLOR))
            self.series = ["current"]
        top = max(float(max((b.datavalues.max() for b in bars), default=0.0)), 1e-12)
        for b in bars:
            ax.bar_label(b, labels=[f"{v:.3g}" for v in b.datavalues], padding=3, color=INK_2, fontsize=8)
        ax.set_yticks(y, names, color=INK_2, fontsize=8)
        ax.invert_yaxis()                                   # layers top to bottom, as stacked
        ax.set_xlim(0, top * 1.18)
        ax.set_xlabel("Copper loss (mW)", color=INK_MUTED, fontsize=8)
        ax.tick_params(axis="x", colors=INK_MUTED, labelsize=8)
        ax.tick_params(axis="y", length=0)
        ax.grid(axis="x", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right", "bottom"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(AXIS)
        if title:
            ax.set_title(title, loc="left", color=INK, fontsize=9)
        if baseline:
            ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="lower right")
        self.canvas.draw_idle()


class ComparePanel(QWidget):
    """Before/after of a rail: pin a solved result as the baseline, solve again, read the
    deltas (current − baseline) per load, for the whole rail and per layer."""
    pin_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pin = QPushButton("Pin as baseline")
        self.pin.setToolTip("Keep the shown result as the 'before' to compare later solves with")
        self.clear_baseline = QPushButton("Clear baseline")
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Baseline", "Current", "Δ current − baseline"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMaximumHeight(72)
        buttons = QHBoxLayout()
        buttons.addWidget(self.pin)
        buttons.addWidget(self.clear_baseline)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(buttons)
        layout.addWidget(self.info)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.notes)
        self.pin.clicked.connect(self.pin_requested.emit)
        self.clear_baseline.clicked.connect(self.clear_requested.emit)
        self.show_message("")

    def show_message(self, text: str):
        self.info.setText(text)
        self.table.setRowCount(0)
        self.notes.clear()

    def rows(self):
        """The table as (label, baseline, current, delta) strings, for scripts and tests."""
        return [tuple(self.table.item(r, c).text() if self.table.item(r, c) else ""
                      for c in range(4)) for r in range(self.table.rowCount())]

    def show_comparison(self, cmp_: dict, info: str, notes: List[str] = ()):
        """``cmp_``: what :func:`coppernicus.service.session.compare_summaries` returns."""
        def num(v, scale=1.0, fmt="{:.3f}"):
            return "" if v is None else fmt.format(v * scale)

        rows = []
        for d in cmp_["loads"]:
            rows.append((f"{d['name']}: drop at its worst pin (mV)", num(d["drop_mv_a"]),
                         num(d["drop_mv_b"]), num(d.get("delta_drop_mv"), fmt="{:+.3f}")))
        md, pl = cmp_["max_drop_mv"], cmp_["power_loss_w"]
        rows.append(("Largest drop on the rail (mV)", num(md["a"]), num(md["b"]),
                     num(md["delta"], fmt="{:+.3f}")))
        rows.append(("Copper loss, total (mW)", num(pl["a"], 1e3), num(pl["b"], 1e3),
                     num(pl["delta"], 1e3, "{:+.3f}")))
        for d in cmp_["losses"]:
            rows.append((f"  {d['name']} (mW)", num(d["power_w_a"], 1e3), num(d["power_w_b"], 1e3),
                         num(d["delta_power_w"], 1e3, "{:+.3f}")))
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.info.setText(info)
        changed = [f"Changed setting {k}: {v['a']} → {v['b']}" for k, v in cmp_["settings_changed"].items()]
        self.notes.setPlainText("\n".join([*notes, *changed, *cmp_["warnings"], *cmp_["assumptions"]]))
