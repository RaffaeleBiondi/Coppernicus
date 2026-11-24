"""Side panels: the net list, the rail setup (sources, loads, limits, settings) and the
problems list. They edit plain :mod:`coppernicus.project` objects and never touch the
engine directly; the main window wires them together."""

from __future__ import annotations

from typing import List, Optional

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
from coppernicus.project import Limits, Load, Rail, Settings, Source, parse_pins, pins_text

# ---------------------------------------------------------------------- nets


class NetPanel(QWidget):
    net_selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._board: Optional[Board] = None
        self._configured: set = set()
        self.filter = QLineEdit(placeholderText="Filter nets…")
        self.power_only = QCheckBox("Power && ground only")
        self.power_only.setChecked(True)
        self.list = QListWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.filter)
        layout.addWidget(self.power_only)
        layout.addWidget(self.list)
        self.filter.textChanged.connect(self._refill)
        self.power_only.toggled.connect(self._refill)
        self.list.currentTextChanged.connect(self._emit)

    def set_board(self, board: Optional[Board], configured: List[str] = ()):
        self._board = board
        self._configured = set(configured)
        self._refill()

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
            nets = self._board.power_nets() if self.power_only.isChecked() else self._board.nets()
            text = self.filter.text().strip().lower()
            for n in nets:
                if text and text not in n.lower():
                    continue
                self.list.addItem(n)
                item = self.list.item(self.list.count() - 1)
                if n in self._configured:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setToolTip("Has sources/loads in this project")
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
    """Sources or loads of a rail: name, part (picked from the parts on the net), pins, value."""
    changed = Signal()

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
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel(f"<b>{title}</b>"))
        buttons.addStretch(1)
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
        spin = QDoubleSpinBox()
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
            e.setValidator(QDoubleValidator(0.0, 1e9, 6))
        form = QFormLayout(limits)
        form.addRow("Max drop (%)", self.max_drop)
        form.addRow("Max current density (A/mm²)", self.max_j)
        form.addRow("Max via current (A)", self.max_via)

        settings = QGroupBox("Solve settings (whole project)")
        self.max_edge = QDoubleSpinBox(decimals=3, minimum=0.02, maximum=5.0, singleStep=0.1, suffix=" mm")
        self.plating = QLineEdit(placeholderText="engine defaults (IPC-6012 Class 2)")
        self.plating.setValidator(QDoubleValidator(0.1, 1000.0, 3))
        self.plating.setToolTip("Via barrel plating in µm for every plated hole. Empty: the engine's "
                                "table (micro 12, buried 13, others 20 µm). Ask your fabricator.")
        self.temp = QDoubleSpinBox(decimals=1, minimum=-55.0, maximum=250.0, suffix=" °C")
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
        self.follow_ties.toggled.connect(self._write_rail)
        for e in (self.max_drop, self.max_j, self.max_via):
            e.editingFinished.connect(self._write_limits)
        self.max_edge.valueChanged.connect(self._write_settings)
        self.temp.valueChanged.connect(self._write_settings)
        self.plating.editingFinished.connect(self._write_settings)
        self.solve.clicked.connect(self.solve_requested.emit)
        self._settings: Optional[Settings] = None
        self.setEnabled(False)

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

    # ------------------------------------------------------------------ write back
    def _write_rail(self, *_):
        if self._loading or self._rail is None:
            return
        self._rail.sources = [Source(ref, value, pins, name)
                              for name, ref, pins, value in self.sources.rows() if ref]
        self._rail.loads = [Load(ref, value, pins, name)
                            for name, ref, pins, value in self.loads.rows() if ref]
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
