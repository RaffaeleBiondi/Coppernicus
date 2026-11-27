"""Side panels: the net list, the rail setup (sources, loads, limits, settings) and the
problems list. They edit plain :mod:`coppernicus.project` objects and never touch the
engine directly; the main window wires them together."""

from __future__ import annotations

import copy
import html
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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

from coppernicus.app import theme
from coppernicus.app.copper_thickness import CopperThicknessDialog, thickness_summary
from coppernicus.app.fields_view import FieldsBanner
from coppernicus.app.units import PLAIN_SPELLINGS, UNIT_SPELLINGS, UNIT_TIP, Unit, UnitEdit
from coppernicus.app.verdict import BADGE_ROLE, BadgeDelegate, CardData, VerdictHeader
from coppernicus.engine import Board, NetTie, PartPins
from coppernicus.power_tree import ACROSS_NOTE
from coppernicus.problems import ATTENTION, FAIL, Problem
from coppernicus.project import (
    GRADING_RANGE,
    LOAD_PART_KEYS,
    LOAD_TOLERANCE_KEYS,
    SOURCE_ACCURACY_KEYS,
    SOURCE_KINDS,
    Limits,
    Load,
    PinSpec,
    Rail,
    Settings,
    Source,
    carry_over,
    parse_pins,
    pins_text,
)

# Attention rows (a spot above Coppernicus's current-density default) and their count in the
# Problems header: a dark amber, 4.9:1 on white.


def without_tolerances(d: Dict) -> Dict:
    """A project's ``to_dict()`` without what no solve reads (Task 11): the loads' tolerances
    and entered parts, the sources' DC accuracy and the ``tolerance:`` options; its rails in
    net order (the merge lists the project's rails before the fields-only ones, so a copy of
    the KiCad fields' parts holding a tolerance moves its rail). For the keys that tell when a
    held result no longer matches the project: typing a tolerance never makes a result stale.
    Changes ``d`` and returns it."""
    d.pop("tolerance", None)
    if "rails" in d:
        d["rails"] = sorted(d["rails"], key=lambda r: str(r.get("net", "")))
    for rail in d.get("rails", []):
        for key, names in (("sources", SOURCE_ACCURACY_KEYS),
                           ("loads", LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS)):
            for t in rail.get(key, []):
                for name in names:
                    t.pop(name, None)
    return d

# ---------------------------------------------------------------------- numbers


class WheelWhenFocused:
    """A form field that takes the mouse wheel only once focused (a click or Tab): turning the
    wheel to scroll the Rail setup never changes a value under the pointer (and drops the
    results). Unfocused, the wheel goes on to the scroll area; and a wheel turn does not focus
    it first, as Qt's default WheelFocus policy would."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class DecimalSpinBox(WheelWhenFocused, QDoubleSpinBox):
    """A spin box that reads a typed comma as the decimal sign. Numbers are shown with a dot
    (the app uses the C locale), where a comma would otherwise be taken as a thousands
    separator and dropped, turning 3,3 into 33. It takes the wheel only when focused
    (:class:`WheelWhenFocused`)."""

    def _unit(self) -> Optional[Unit]:
        """The unit its suffix names (" V", " A", " mm", " °C"); None for one Unit does not know
        (" m/s") or no suffix: then a plain number, as Qt reads it."""
        symbol = self.suffix().strip()
        return Unit(symbol) if symbol in UNIT_SPELLINGS or symbol in PLAIN_SPELLINGS else None

    def _parse(self, text: str) -> Optional[float]:
        """The value typed, with its unit or an SI prefix ("500m", "500 mA", "0.5 A"); None
        when it is not one (yet). The suffix Qt shows after the number is read away."""
        unit = self._unit()
        if unit is None:
            return None
        t = text.strip()
        candidates = [t]
        if t.endswith(self.suffix().strip()):
            candidates.append(t[: -len(self.suffix().strip())])
        for c in candidates:
            try:
                value = unit.parse(c)
            except ValueError:
                continue
            if value is not None:
                return value
        return None

    def validate(self, text, pos):
        value = self._parse(text)
        if value is not None:
            ok = self.minimum() <= value <= self.maximum()
            return (QValidator.Acceptable if ok else QValidator.Intermediate), text, pos
        return super().validate(text.replace(",", "."), pos)

    def valueFromText(self, text):
        value = self._parse(text)
        return value if value is not None else super().valueFromText(text.replace(",", "."))


class FieldComboBox(WheelWhenFocused, QComboBox):
    """A combo box of the Rail setup: it takes the wheel only when focused
    (:class:`WheelWhenFocused`)."""

    def setEditable(self, editable: bool):
        super().setEditable(editable)
        self.setFocusPolicy(Qt.StrongFocus)          # Qt on macOS gives an editable one WheelFocus


class DecimalValidator(QDoubleValidator):
    """A validator for decimal fields that reads a typed comma as the decimal sign (see
    :class:`DecimalSpinBox`)."""

    def validate(self, text, pos):
        return super().validate(text.replace(",", "."), pos)


class OptionalDecimalValidator(DecimalValidator):
    """A number, or empty for "not set" (each field says what that means: no limit, the
    engine's via plating, no efficiency, no quiescent current). A plain number validator calls
    an empty field unfinished, and a line edit then never emits editingFinished (on Return or
    on focus loss), so a cleared field would never reach the project."""

    def validate(self, text, pos):
        if not text.strip():
            return QValidator.Acceptable, text, pos
        return super().validate(text, pos)


# ---------------------------------------------------------------------- nets


def rail_nets(nets: Iterable[str], heuristic: Iterable[str], marked: Iterable[str],
              configured: Iterable[str]) -> List[str]:
    """The nets the "Rails only" filter shows, in board order: power and ground nets found by
    the name/zone heuristic, nets marked as rails by hand, and nets with sources or loads."""
    keep = set(heuristic) | set(marked) | set(configured)
    return [n for n in nets if n in keep]


KICAD_MARK = "[KiCad]"


class NetPanel(QWidget):
    """The board's nets. Each item's text is the net, with a "[KiCad]" mark on a rail from
    the KiCad footprint fields; the net itself is the item's ``Qt.UserRole`` data."""
    net_selected = Signal(str)
    marked_changed = Signal(list)       # the nets marked as rails by hand

    def __init__(self, parent=None):
        super().__init__(parent)
        self._board: Optional[Board] = None
        self._configured: set = set()
        self._marked: List[str] = []
        self._kicad: Dict[str, str] = {}        # rails from KiCad fields: net -> tooltip
        self.filter = QLineEdit(placeholderText="Filter nets…")
        self.power_only = QCheckBox("Rails only")
        self.power_only.setToolTip("Power and ground nets found by name and zones, nets you marked "
                                   "as rails (right-click a net), and nets with sources or loads")
        self.power_only.setChecked(True)
        self.list = QListWidget()
        self.list.setItemDelegate(BadgeDelegate(self.list))
        self._badges: Dict[str, tuple] = {}           # net -> (kind, text): set_badges
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.filter)
        layout.addWidget(self.power_only)
        layout.addWidget(self.list)
        self.filter.textChanged.connect(self._refill)
        self.power_only.toggled.connect(self._refill)
        self.list.currentItemChanged.connect(
            lambda item, _previous: self._emit(item.data(Qt.UserRole) if item is not None else ""))

    def set_board(self, board: Optional[Board], configured: List[str] = (), marked: List[str] = (),
                  kicad: Optional[Mapping[str, str]] = None):
        """A new board: nothing is selected, so selecting any net afterwards (even one with
        the same name as before) emits :attr:`net_selected`. ``kicad``: the rails from the
        KiCad footprint fields, each with its tooltip (the parts)."""
        self._board = board
        self._configured = set(configured)
        self._marked = list(marked)
        self._kicad = dict(kicad or {})
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
        net = item.data(Qt.UserRole)
        menu = QMenu(self)
        if net in self._marked:
            menu.addAction("Unmark as rail", lambda: self.set_marked(net, False))
        elif self._board.net_kind(net) in ("power", "ground"):
            found = menu.addAction(f"Rail ({self._board.net_kind(net)}, found by name and zones)")
            found.setEnabled(False)
        else:
            menu.addAction("Mark as rail", lambda: self.set_marked(net, True))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def mark_configured(self, nets: List[str], kicad: Optional[Mapping[str, str]] = None):
        """The nets with sources or loads; with ``kicad``, also the rails from the KiCad
        footprint fields (net -> tooltip)."""
        self._configured = set(nets)
        if kicad is not None:
            self._kicad = dict(kicad)
        self._refill(keep=True)

    def set_badges(self, badges: Mapping[str, tuple]) -> None:
        """Each net's badge, (kind, text), kept through the list's refills; nets not named
        carry none (a net that is not a rail)."""
        self._badges = dict(badges)
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setData(BADGE_ROLE, self._badges.get(item.data(Qt.UserRole)))

    def badge_of(self, net: str):
        item = self._find(net)
        return item.data(BADGE_ROLE) if item is not None else None

    def _find(self, net: str) -> Optional[QListWidgetItem]:
        return next((self.list.item(i) for i in range(self.list.count())
                     if self.list.item(i).data(Qt.UserRole) == net), None)

    def select(self, net: str):
        item = self._find(net)
        if item is None:
            self.power_only.setChecked(False)
            self.filter.clear()
            item = self._find(net)
        if item is not None:
            self.list.setCurrentItem(item)

    def _refill(self, *_args, keep: bool = True):
        current = self.list.currentItem().data(Qt.UserRole) if keep and self.list.currentItem() else None
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
                # Made with the list as its parent, so the list owns it.
                item = QListWidgetItem(f"{n}  {KICAD_MARK}" if n in self._kicad else n, self.list)
                item.setData(Qt.UserRole, n)
                item.setData(BADGE_ROLE, self._badges.get(n))
                f = item.font()
                tips = [self._kicad[n]] if n in self._kicad else []
                if n in self._configured and n not in self._kicad:
                    f.setBold(True)
                    tips.append("has sources/loads in this project")
                if n in self._marked:
                    f.setItalic(True)
                    tips.append("marked as a rail by hand")
                if n in self._kicad:
                    f.setBold(True)
                item.setFont(f)
                if tips:
                    tip = "; ".join(tips)
                    item.setToolTip(tip[:1].upper() + tip[1:])
            if current:
                found = self._find(current)
                if found is not None:
                    self.list.setCurrentItem(found)
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
    row_selected = Signal(int)        # the current row (-1: none)
    PICK_TIP = ("Click pads in the view to add them here; a pin joins the row of its part. Esc or "
                "a second press stops picking.")

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
        self.add_button, self.remove_button = QPushButton("Add"), QPushButton("Remove")
        self.add_button.clicked.connect(self.add_row)
        self.remove_button.clicked.connect(self.remove_row)
        self.pick = QPushButton("Pick on board")
        self.pick.setCheckable(True)
        self.pick.setToolTip(self.PICK_TIP)
        self.pick.toggled.connect(self.pick_toggled.emit)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel(f"<b>{title}</b>"))
        buttons.addStretch(1)
        buttons.addWidget(self.pick)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self.table)
        self.table.itemChanged.connect(lambda *_: self.changed.emit())
        self.table.currentCellChanged.connect(lambda row, *_: self.row_selected.emit(row))

    def set_parts(self, parts: List[PartPins]):
        self._parts = parts

    def set_rows(self, rows):
        """``rows``: (name, ref, pins, value) tuples. Each row remembers its position in
        ``rows`` (see :meth:`origins`)."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for i, row in enumerate(rows):
            self._append(*row, origin=i)
        self.table.blockSignals(False)

    def set_read_only(self, why: str = "", tips: List[str] = ()):
        """Rows that cannot be edited (``why``: the reason, "" to edit again), each with its
        tooltip from ``tips``. Picking stays on, so a click on a pad can say why."""
        for button in (self.add_button, self.remove_button):
            button.setEnabled(not why)
            button.setToolTip(why)
        self.pick.setToolTip(why or self.PICK_TIP)
        blocked = self.table.blockSignals(True)      # flags and tooltips are not an edit
        try:
            self._set_rows_read_only(why, tips)
        finally:
            self.table.blockSignals(blocked)

    def _set_rows_read_only(self, why: str, tips: List[str]):
        for r in range(self.table.rowCount()):
            tip = tips[r] if r < len(tips) else why
            item = self.table.item(r, 0)
            part, pins, spin = (self.table.cellWidget(r, c) for c in (1, 2, 3))
            if item is not None:
                flags = item.flags()
                item.setFlags(flags & ~Qt.ItemIsEditable if why else flags | Qt.ItemIsEditable)
                item.setToolTip(tip)
            if part is not None:
                part.setEnabled(not why)
                part.setToolTip(tip)
            if pins is not None:
                pins.setReadOnly(bool(why))
                if why:
                    pins.setToolTip(tip)
            if spin is not None:
                spin.setReadOnly(bool(why))
                spin.setButtonSymbols(QDoubleSpinBox.NoButtons if why else QDoubleSpinBox.UpDownArrows)
                spin.setToolTip(tip)

    def part_flags(self) -> List[bool]:
        """For every table row, whether it is complete and names a part (is in the rail)."""
        out = []
        for r in range(self.table.rowCount()):
            part = self.table.cellWidget(r, 1)
            out.append(part is not None and all(self.table.cellWidget(r, c) is not None for c in (2, 3))
                       and bool(part.currentData()))
        return out

    def renumber(self):
        """Each row in the rail becomes its own origin (see :meth:`origins`): after a change
        made outside the table, the rail's list is the new reference."""
        k = 0
        for r, in_rail in enumerate(self.part_flags()):
            item = self.table.item(r, 0)
            if item is not None:
                item.setData(Qt.UserRole, k if in_rail else None)
            k += in_rail

    def origins(self) -> List[Optional[int]]:
        """For each row :meth:`rows` returns, its position in the last :meth:`set_rows`
        (``None`` for a row added since)."""
        out = []
        for r in range(self.table.rowCount()):
            if any(self.table.cellWidget(r, c) is None for c in (1, 2, 3)):
                continue
            item = self.table.item(r, 0)
            origin = item.data(Qt.UserRole) if item is not None else None
            out.append(origin if isinstance(origin, int) else None)
        return out

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

    def _append(self, name, ref, pins, value, origin: Optional[int] = None):
        blocked = self.table.blockSignals(True)     # no itemChanged before the row is complete
        try:
            self._build_row(name, ref, pins, value, origin)
        finally:
            self.table.blockSignals(blocked)

    def _build_row(self, name, ref, pins, value, origin: Optional[int] = None):
        r = self.table.rowCount()
        self.table.insertRow(r)
        item = QTableWidgetItem(name)
        item.setData(Qt.UserRole, origin)
        self.table.setItem(r, 0, item)
        part = FieldComboBox()
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
        spin.setRange(-1e4 if self._unit == "V" else 0.0, 1e4)   # a negative rail's source
        spin.setValue(float(value))
        spin.setSuffix(f" {self._unit}")
        spin.valueChanged.connect(lambda *_: self.changed.emit())
        self.table.setCellWidget(r, 3, spin)


def rail_index(has_part: List[bool], row: int) -> Optional[int]:
    """The index in the rail's list of the terminal shown in table row ``row``: rows without
    a part are not in the rail (see ``RailPanel._write_rail``)."""
    if not 0 <= row < len(has_part) or not has_part[row]:
        return None
    return sum(has_part[:row])


def _optional_float(edit: UnitEdit) -> Optional[float]:
    return edit.value()


class PinRefEdit(QWidget):
    """A pin reference: the part (empty = the row's own part), its net and its pins."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.part = QLineEdit(placeholderText="this part")
        self.part.setToolTip("Part reference; empty means the part of the selected row")
        self.net = FieldComboBox()
        self.net.setEditable(True)
        self.net.setInsertPolicy(QComboBox.NoInsert)
        self.net.setToolTip("Net of these pins; empty means not set")
        self.pins = QLineEdit(placeholderText="all")
        self.pins.setToolTip("Pad numbers, comma separated; globs like A* work; 'all' = every pin "
                             "of the part on this net")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.part, 2)
        row.addWidget(self.net, 3)
        row.addWidget(self.pins, 2)
        self.part.editingFinished.connect(self.changed.emit)
        self.net.activated.connect(lambda *_: self.changed.emit())
        self.net.lineEdit().editingFinished.connect(self.changed.emit)
        self.pins.editingFinished.connect(self.changed.emit)

    def set_nets(self, nets: List[str]):
        text = self.net.currentText()
        self.net.blockSignals(True)
        self.net.clear()
        self.net.addItems([""] + list(nets))
        self.net.setEditText(text)
        self.net.blockSignals(False)

    def set(self, ref: str, pins: PinSpec, net: str):
        for w in (self.part, self.net, self.pins):
            w.blockSignals(True)
        self.part.setText(ref)
        self.net.setEditText(net)
        self.pins.setText("" if net == "" and pins is None else pins_text(pins))
        for w in (self.part, self.net, self.pins):
            w.blockSignals(False)

    def get(self) -> Tuple[str, PinSpec, str]:
        """(part, pins, net); the net is empty when the reference is not set."""
        try:
            pins = parse_pins(self.pins.text())
        except ValueError:
            pins = None
        return self.part.text().strip(), pins, self.net.currentText().strip()


KIND_LABELS = {"source": "Source (ideal supply)", "ldo": "LDO", "smps": "Switcher (SMPS)"}


class RegulatorBox(QGroupBox):
    """The power-tree fields of the selected source: what kind of regulator it is, and the
    pins of its input, ground and remote sense."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__("Regulator: selected source (used by Solve tree)", parent)
        self.kind = FieldComboBox()
        for k in SOURCE_KINDS:
            self.kind.addItem(KIND_LABELS[k], k)
        self.efficiency = UnitEdit(Unit(), 0.001, 1.0, placeholderText="switcher: a fraction 0–1, e.g. 0.9")
        self.quiescent = UnitEdit(Unit("A", None), 0.0, 1e3, placeholderText="0")     # empty: 0 A
        self.quiescent.setToolTip(UNIT_TIP)
        self.input, self.ground = PinRefEdit(), PinRefEdit()
        self.sense, self.ground_sense = PinRefEdit(), PinRefEdit()
        form = QFormLayout(self)
        form.addRow("Kind", self.kind)
        form.addRow("Efficiency", self.efficiency)
        form.addRow("Quiescent current (A)", self.quiescent)
        form.addRow(QLabel("<small>pins: part (empty = this one) · net · pins</small>"))
        form.addRow("Input", self.input)
        form.addRow("Ground", self.ground)
        form.addRow("Remote sense", self.sense)
        form.addRow("Ground sense", self.ground_sense)
        self.kind.currentIndexChanged.connect(lambda *_: self._kind_changed())
        self.efficiency.editingFinished.connect(self.changed.emit)
        self.quiescent.editingFinished.connect(self.changed.emit)
        for w in (self.input, self.ground, self.sense, self.ground_sense):
            w.changed.connect(self.changed.emit)
        self.setEnabled(False)

    def _kind_changed(self):
        self._enable()
        self.changed.emit()

    def _enable(self):
        kind = self.kind.currentData()
        self.efficiency.setEnabled(kind == "smps")
        self.quiescent.setEnabled(kind != "source")
        self.input.setEnabled(kind != "source")

    def show_source(self, s: Optional[Source], nets: List[str]):
        """Fill the box from source ``s`` (None: empty and disabled)."""
        widgets = (self.kind, self.efficiency, self.quiescent)
        for w in widgets:
            w.blockSignals(True)
        self.setEnabled(s is not None)
        s = s or Source("", 0.0)
        self.kind.setCurrentIndex(max(0, self.kind.findData(s.kind)))
        self.efficiency.set_value(s.efficiency)
        self.quiescent.set_value(s.quiescent_a or None)
        for what in ("input", "ground", "sense", "ground_sense"):
            edit = getattr(self, what)
            edit.set_nets(nets)
            edit.set(getattr(s, f"{what}_ref"), getattr(s, f"{what}_pins"), getattr(s, f"{what}_net"))
        for w in widgets:
            w.blockSignals(False)
        self._enable()

    def write(self, s: Source):
        """Copy the box into source ``s``."""
        s.kind = self.kind.currentData()
        s.efficiency = self.efficiency.value()
        s.quiescent_a = self.quiescent.value() or 0.0
        for what in ("input", "ground", "sense", "ground_sense"):
            ref, pins, net = getattr(self, what).get()
            setattr(s, f"{what}_ref", ref if net else "")
            setattr(s, f"{what}_pins", pins if net else None)
            setattr(s, f"{what}_net", net)
        if not s.ground_net:          # more pads of a ground terminal that is gone (the file
            s.extra_ground_pins = None    # keeps them only with their ground)


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
        self._loaded = ([], [])        # the rail's sources and loads as set_rail got them
        self._loading = False
        self._read_only = False         # a rail from the KiCad fields: only limits are edited
        self._drop_tip = ""             # set: max_drop comes from a KiCad field (PI_LIMIT_DROP)
        self.title = QLabel("<i>Open a board and pick a net</i>")
        self.banner = FieldsBanner()           # where the rail comes from (KiCad fields)
        self.sources = TerminalTable("Sources (regulator outputs)", "Voltage", "V")
        self.loads = TerminalTable("Loads (current split equally across pins)", "Current", "A")
        self.ties = QLabel()
        self.ties.setWordWrap(True)
        self.follow_ties = QCheckBox("Follow net ties")
        self.follow_ties.setToolTip("Solve nets joined by net ties as one (tie = short)")
        self._board: Optional[Board] = None
        self.source_box = RegulatorBox()
        self.load_box = QGroupBox("Ground pins: selected load (used by Solve tree)")
        self.load_ground = PinRefEdit()
        QVBoxLayout(self.load_box).addWidget(self.load_ground)
        self.load_box.setEnabled(False)

        limits = QGroupBox("Limits (empty = not checked; problems are still ranked)")
        # Empty: no limit.
        self.max_drop = UnitEdit(Unit("%"), 0.0, 1e9, placeholderText="e.g. 3 %")
        self.max_j = UnitEdit(Unit("A/mm²"), 0.0, 1e9, placeholderText="A/mm²")
        self.max_via = UnitEdit(Unit("A", None), 0.0, 1e9, placeholderText="A")
        self.max_via.setToolTip(UNIT_TIP)
        self.max_rise = UnitEdit(Unit("V", -3, stored=-3), 0.0, 1e9, placeholderText="mV, for a ground net")
        self.max_rise.setToolTip("For a ground net: the largest rise above its 0 V reference. Judged "
                                 "on its group in Solve tree, and when this net is solved alone. "
                                 + UNIT_TIP)
        self.max_drop.setToolTip("For a supply net: the largest load drop, % of the source voltage.")
        self._tips = {e: e.toolTip() for e in (self.max_drop, self.max_rise)}
        form = QFormLayout(limits)
        form.addRow("Max drop (%)", self.max_drop)
        form.addRow("Max current density (A/mm²)", self.max_j)
        form.addRow("Max via current (A)", self.max_via)
        form.addRow("Max ground rise (mV)", self.max_rise)

        settings = QGroupBox("Solve settings (whole project)")
        self.max_edge = DecimalSpinBox(decimals=3, minimum=0.02, maximum=5.0, singleStep=0.1, suffix=" mm")
        self.plating = UnitEdit(Unit("µm"), 0.1, 1000.0,                             # empty: the engine's
                                placeholderText="engine defaults (IPC-6012 Class 2)")
        self.plating.setToolTip("Via barrel plating in µm for every plated hole. Empty: the engine's "
                                "table (micro 12, buried 13, others 20 µm). Ask your fabricator.")
        self.temp = DecimalSpinBox(decimals=1, minimum=-55.0, maximum=250.0, suffix=" °C")
        self.temp.setToolTip("Copper temperature; resistivity is referenced to 20 °C")
        # Advanced, the mesh near plated holes (KiPI's): empty = KiPI's default.
        self.min_edge = UnitEdit(Unit("mm"), 0.0001, 5.0, placeholderText="engine default 0.02 mm")
        self.min_edge.setToolTip("Advanced: the smallest mesh element, at a plated hole's wall, in mm. "
                                 "The largest mesh edge does not refine there; a smaller value here "
                                 "does, to check a small current-density spot at a via. Empty: the "
                                 "engine's 0.02 mm.")
        self.grading = UnitEdit(Unit(), GRADING_RANGE[0], GRADING_RANGE[1],
                                placeholderText="engine default 0.2")
        self.grading.setToolTip("Advanced: how fast mesh elements grow away from a plated hole, mm of "
                                "edge per mm of distance, 0 (off) to 2. Lower keeps the mesh fine "
                                "further from the hole. Empty: the engine's 0.2.")
        self.copper_summary = QLabel("board file")
        self.copper_summary.setWordWrap(True)            # many layers: wrap, never widen the panel
        self.copper_summary.setToolTip("What-if copper thickness by layer, for every solve. "
                                       "The board file is never changed.")
        self.copper_edit = QPushButton("Edit…")
        copper = QWidget()      # a widget, not a layout: a layout field kept one line and clipped
        row = QHBoxLayout(copper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.copper_summary, 1)
        row.addWidget(self.copper_edit)
        sform = QFormLayout(settings)
        sform.addRow("Largest mesh edge", self.max_edge)
        sform.addRow("Via plating (µm)", self.plating)
        sform.addRow("Copper temperature", self.temp)
        sform.addRow("Copper thickness", copper)
        sform.addRow("Mesh at holes, min edge (mm)", self.min_edge)
        sform.addRow("Mesh at holes, grading", self.grading)

        self.solve = QPushButton("Solve rail  (F5)")
        self.solve.setMinimumHeight(34)
        self._solve_free = True          # no rail solve running (set_solve_free)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        for w in (self.title, self.banner, self.sources, self.source_box, self.loads, self.load_box,
                  self.ties, self.follow_ties, limits, settings, self.solve):
            layout.addWidget(w)
        layout.addStretch(1)

        self.sources.changed.connect(self._write_rail)
        self.loads.changed.connect(self._write_rail)
        self.sources.row_selected.connect(lambda row: self._show_source(row))
        self.loads.row_selected.connect(lambda row: self._show_load(row))
        self.source_box.changed.connect(self._write_source_box)
        self.load_ground.changed.connect(self._write_load_box)
        self.sources.pick_toggled.connect(lambda on: self._pick_toggled("source", on))
        self.loads.pick_toggled.connect(lambda on: self._pick_toggled("load", on))
        self.follow_ties.toggled.connect(self._write_rail)
        for e in (self.max_drop, self.max_j, self.max_via, self.max_rise):
            e.editingFinished.connect(self._write_limits)
        self.max_edge.valueChanged.connect(self._write_settings)
        self.temp.valueChanged.connect(self._write_settings)
        self.plating.editingFinished.connect(self._write_settings)
        self.min_edge.editingFinished.connect(self._write_settings)
        self.grading.editingFinished.connect(self._write_settings)
        self.copper_edit.clicked.connect(self._edit_copper)
        self.solve.clicked.connect(self.solve_requested.emit)
        self._settings: Optional[Settings] = None
        self.setEnabled(False)

    def set_solve_free(self, free: bool):
        """Solve rail is offered when no rail solve runs and the panel is enabled (a board, a
        net): the button may sit outside the panel (the setup panel's foot), so both count."""
        self._solve_free = free
        self.solve.setEnabled(free and self.isEnabled())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange and hasattr(self, "_solve_free"):
            self.solve.setEnabled(self._solve_free and self.isEnabled())

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
        self.plating.set_value(settings.via_plating_um)
        self.temp.setValue(settings.copper_temp_c)
        self.min_edge.set_value(settings.min_edge_mm)
        self.grading.set_value(settings.grading)
        self.copper_summary.setText(thickness_summary(settings.copper_thickness_mm))
        self._loading = False

    def set_rail(self, board: Board, rail: Rail, ties: List[NetTie]):
        self._loading = True
        self._rail, self._board = rail, board
        # The terminals as loaded: each table row takes the fields the table does not show
        # from its own row here (carry_over), so they survive any edit of the row.
        self._loaded = (copy.deepcopy(rail.sources), copy.deepcopy(rail.loads))
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
                            (self.max_via, lim.max_via_current), (self.max_rise, lim.max_ground_rise_mv)):
            edit.set_value(value)
        self._loading = False
        self.set_read_only()
        self._drop_tip = ""                       # editable again, until lock_drop says otherwise
        self.max_drop.setReadOnly(False)
        self.max_drop.setToolTip(self._tips[self.max_drop])
        # The first row of each table is the current one, so the boxes below edit it.
        for table, items in ((self.sources, rail.sources), (self.loads, rail.loads)):
            if items:
                table.table.setCurrentCell(0, 0)
        self._show_source(0 if rail.sources else -1)
        self._show_load(0 if rail.loads else -1)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, why: str = "", source_tips: List[str] = (), load_tips: List[str] = ()):
        """Show the rail read-only (``why``: the reason; "" to edit it again): sources, loads
        and their regulator and ground pins; the limits and the net-tie option stay editable
        (see :meth:`lock_drop`). Call it after :meth:`set_rail`, which makes everything
        editable again."""
        self._read_only = bool(why)
        self.sources.set_read_only(why, source_tips)
        self.loads.set_read_only(why, load_tips)
        for box in (self.source_box, self.load_box):
            box.setToolTip(why)
            if why:
                box.setEnabled(False)

    def lock_drop(self, pct: Optional[float], tip: str = ""):
        """The drop limit given by a KiCad field (``tip`` says which; "" for none): shown at
        ``pct``, read-only, and never written to the rail, which keeps its own value. Call it
        after :meth:`set_rail`, which unlocks it."""
        if not tip and not self._drop_tip:
            return                                # nothing locked: as set_rail and set_net_kind left it
        own = self._rail.limits.max_drop_pct if self._rail is not None else None
        value = pct if tip else own
        self.max_drop.set_value(value)
        self._drop_tip = tip
        self.max_drop.setReadOnly(bool(tip))
        self.max_drop.setToolTip(tip or self._tips[self.max_drop])

    def set_net_kind(self, at_0v: Optional[bool]):
        """Disable the limit that does not apply to the shown net (one rule, by its reference
        voltage): the ground rise on a net with a nonzero nominal, the % drop on a net at 0 V.
        ``None`` (not set up yet): both stay editable. A value read from the file stays shown
        and editable, so it can be cleared; it is reported as not judged."""
        off = {True: self.max_drop, False: self.max_rise}.get(at_0v)
        why = {True: "Applies to nets with a nonzero nominal only: this net is at 0 V (a ground), "
                     "so a value here is not judged. Use Max ground rise.",
               False: "Applies to nets at 0 V (grounds) only: this net has a nonzero nominal, so "
                      "a value here is not judged."}.get(at_0v, "")
        for edit in (self.max_drop, self.max_rise):
            edit.setEnabled(edit is not off or bool(edit.text().strip()))
            edit.setToolTip(why if edit is off else self._tips[edit])
        if self._drop_tip:                        # given by a KiCad field: say which
            self.max_drop.setToolTip(self._drop_tip)

    # ------------------------------------------------------------------ power-tree fields
    def select_source(self, row: int):
        """Select a source row and show its regulator fields."""
        self.sources.table.setCurrentCell(row, 0)
        self._show_source(row)

    def select_load(self, row: int):
        """Select a load row and show its ground pins."""
        self.loads.table.setCurrentCell(row, 0)
        self._show_load(row)

    def _terminal(self, table: "TerminalTable", items, row: int):
        i = rail_index(table.part_flags(), row)
        return items[i] if i is not None and i < len(items) else None

    def _show_source(self, row: int):
        s = self._terminal(self.sources, self._rail.sources, row) if self._rail else None
        nets = self._board.part_nets(s.ref) if s is not None and self._board is not None else []
        self.source_box.show_source(s, nets)
        if self._read_only:
            self.source_box.setEnabled(False)

    def _show_load(self, row: int):
        ld = self._terminal(self.loads, self._rail.loads, row) if self._rail else None
        self.load_box.setEnabled(ld is not None and not self._read_only)
        nets = self._board.part_nets(ld.ref) if ld is not None and self._board is not None else []
        self.load_ground.set_nets(nets)
        self.load_ground.set(*((ld.ground_ref, ld.ground_pins, ld.ground_net) if ld else ("", None, "")))

    def _write_source_box(self):
        s = self._terminal(self.sources, self._rail.sources, self.sources.table.currentRow()) \
            if self._rail else None
        if s is None:
            return
        self.source_box.write(s)
        self._rebase()

    def _write_load_box(self):
        ld = self._terminal(self.loads, self._rail.loads, self.loads.table.currentRow()) \
            if self._rail else None
        if ld is None:
            return
        ref, pins, net = self.load_ground.get()
        ld.ground_ref, ld.ground_pins, ld.ground_net = (ref if net else ""), (pins if net else None), net
        self._rebase()

    def _rebase(self):
        """A field outside the tables changed: the rail as it is now becomes the reference the
        table rows take their hidden fields from."""
        self._loaded = (copy.deepcopy(self._rail.sources), copy.deepcopy(self._rail.loads))
        self.sources.renumber()
        self.loads.renumber()
        self.rail_changed.emit()

    def clear_rail(self):
        """No net selected (a new board was opened)."""
        self._loading = True
        self._rail = None
        self._loaded = ([], [])
        self.source_box.show_source(None, [])
        self.load_box.setEnabled(False)
        self.title.setText("<i>Pick a net</i>")
        self.sources.set_rows([])
        self.loads.set_rows([])
        for edit in (self.max_drop, self.max_j, self.max_via, self.max_rise):
            edit.clear()
        self.ties.setVisible(False)
        self.follow_ties.setVisible(False)
        self.set_read_only()
        self.lock_drop(None)
        self.setEnabled(False)
        self._loading = False

    # ------------------------------------------------------------------ write back
    def _write_rail(self, *_):
        if self._loading or self._rail is None:
            return
        if self._read_only:                       # a KiCad rail: only the net-tie option
            if self._rail.follow_net_ties != self.follow_ties.isChecked():
                self._rail.follow_net_ties = self.follow_ties.isChecked()
                self.rail_changed.emit()
            return
        # The tables show name, part, pins and value; every other field (a regulator's kind,
        # input, ground and sense pins, a load's ground pins) is kept from the row's part.
        for table, attr, kind, loaded in ((self.sources, "sources", Source, self._loaded[0]),
                                          (self.loads, "loads", Load, self._loaded[1])):
            rows = [(row, origin) for row, origin in zip(table.rows(), table.origins()) if row[1]]
            setattr(self._rail, attr, carry_over(
                loaded, [kind(ref, value, pins, name) for (name, ref, pins, value), _ in rows],
                [origin for _, origin in rows]))
        self._show_source(self.sources.table.currentRow())
        self._show_load(self.loads.table.currentRow())
        self._rail.follow_net_ties = self.follow_ties.isChecked()
        self.rail_changed.emit()

    def _write_limits(self, *_):
        if self._loading or self._rail is None:
            return
        # A drop limit locked by a KiCad field is shown, not the rail's: the rail keeps its own.
        drop = self._rail.limits.max_drop_pct if self._drop_tip else _optional_float(self.max_drop)
        limits = Limits(drop, _optional_float(self.max_j), _optional_float(self.max_via),
                        _optional_float(self.max_rise))
        if limits != self._rail.limits:
            self._rail.limits = limits
            self.limits_changed.emit()

    def _write_settings(self, *_):
        if self._loading or self._settings is None:
            return
        self._settings.max_edge_mm = self.max_edge.value()
        self._settings.via_plating_um = _optional_float(self.plating)
        self._settings.copper_temp_c = self.temp.value()
        # Only a value its validator accepts: a field left at "0" or "2.5" keeps the setting.
        for key, edit in (("min_edge_mm", self.min_edge), ("grading", self.grading)):
            if edit.hasAcceptableInput():
                setattr(self._settings, key, _optional_float(edit))
        self.settings_changed.emit()

    def _edit_copper(self):
        """Edit…: the copper thickness by layer of the board shown, in a dialog. Only a change
        reaches the settings (and drops the results)."""
        if self._settings is None or self._board is None:
            return
        now = self._settings.copper_thickness_mm
        dialog = CopperThicknessDialog(self._board.file_thickness_mm, now, self)
        if dialog.exec() != QDialog.Accepted or dialog.values() == now:
            return
        self._settings.copper_thickness_mm = dialog.values()        # checked by the dialog
        self.copper_summary.setText(thickness_summary(self._settings.copper_thickness_mm))
        self._write_settings()


# ---------------------------------------------------------------------- problems


class ProblemsPanel(QWidget):
    """The problems of the shown DC result, one line each, the selected one's detail in a strip
    under the list. Its verdict line (:attr:`header`: the badge, :attr:`summary` and the cards)
    is placed by the window at the top of the page column (layout C)."""
    problem_activated = Signal(object)          # a row clicked: zoom the map there
    problem_selected = Signal(object)           # the current row (None: none): ring it on the map

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = QLabel("<i>No results yet</i>")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.header = VerdictHeader(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Problem", "Layer", "Where (mm)"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(120)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (0, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.detail = QLabel()                 # the selected row's detail, one strip
        self.detail.setObjectName("detailStrip")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMaximumHeight(72)
        self.notes.setPlaceholderText("Assumptions and warnings of the last solve")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.detail)
        layout.addWidget(self.notes)
        self.tree.itemClicked.connect(self._activated)
        self.tree.currentItemChanged.connect(self._current_changed)
        self._problems: List[Problem] = []
        self._pinned = ""
        self._notes: List[str] = []
        self._show_detail(None)

    def pin_note(self, text: str):
        """A note kept above the others, whatever is shown, until it is changed ("" for
        none): a KiCad footprint field the solves cannot use."""
        self._pinned = text
        self.set_notes(self._notes)

    def set_notes(self, notes: List[str]):
        self._notes = list(notes)
        self.notes.setPlainText("\n".join(([self._pinned] if self._pinned else []) + self._notes))

    def clear(self, text: str = "<i>No results yet</i>"):
        self.summary.setText(text)
        self.header.show_verdict(None, [])
        self.tree.clear()
        self.set_notes([])
        self._problems = []
        self._show_detail(None)

    def set_result(self, summary: str, problems: List[Problem], notes: List[str],
                   verdict: Optional[str] = None, cards: Sequence[CardData] = ()):
        """``summary`` (the line beside the badge of ``verdict``: "fail", "pass",
        "not_judged"), the ``cards`` under it, ``problems`` one per row and the ``notes``."""
        self.summary.setText(summary)
        self.header.show_verdict(verdict, cards)
        self.tree.clear()
        self._problems = list(problems)
        for i, p in enumerate(problems):
            tag = {FAIL: "FAIL", ATTENTION: "ATTENTION"}.get(p.severity, "")
            item = QTreeWidgetItem([tag, p.title, p.layer or "", f"{p.x:.2f}, {p.y:.2f}"])
            item.setData(0, Qt.UserRole, i)
            item.setToolTip(1, p.detail)
            if tag:         # a fail red; an attention item (the current-density default) amber
                color = theme.brush("fail" if p.severity == FAIL else "warn")
                for col in range(4):
                    item.setForeground(col, color)
                f = item.font(0)
                f.setBold(True)
                item.setFont(0, f)
            self.tree.addTopLevelItem(item)
        self.set_notes(notes)
        self._show_detail(None)

    def current_problem(self) -> Optional[Problem]:
        item = self.tree.currentItem()
        i = item.data(0, Qt.UserRole) if item is not None else None
        return self._problems[int(i)] if i is not None and int(i) < len(self._problems) else None

    def _current_changed(self, item: Optional[QTreeWidgetItem], _previous):
        problem = self.current_problem() if item is not None else None
        self._show_detail(problem)
        self.problem_selected.emit(problem)

    def _show_detail(self, problem: Optional[Problem]):
        if problem is None:
            self.detail.setText("Select a row for its detail; a click also zooms the map there."
                                if self._problems else "")
            self.detail.setVisible(bool(self._problems))
            return
        self.detail.setText(f"<b>{html.escape(problem.title)}</b>. {html.escape(problem.detail)} "
                            f"Ringed on the map; a click zooms there.")
        self.detail.setVisible(True)

    def _activated(self, item: QTreeWidgetItem, _col: int):
        i = item.data(0, Qt.UserRole)
        if i is not None:
            self.problem_activated.emit(self._problems[int(i)])


# ---------------------------------------------------------------------- losses and comparison

# Chart colours: the theme's (the dataviz reference palette, validated for colour-vision
# deficiency in both themes): the current result is always series 1, the baseline always
# series 2, so a colour never changes meaning when the baseline appears.


class LossChart(QWidget):
    """Copper loss per layer (and vias, series links) as horizontal bars in mW, with the
    baseline beside the current result when one is pinned for the same rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        # "constrained" re-fits the labels at every draw, so they survive a resized dock.
        self.figure = Figure(figsize=(6, 2.6), facecolor=theme.color("panel"), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.canvas)
        self.series: List[str] = []
        self._shown = (None, None, "")
        self.set_losses(None)
        theme.signals.changed.connect(lambda *_: self.set_losses(*self._shown))

    def set_losses(self, current, baseline=None, title: str = ""):
        """``current`` / ``baseline``: {name: W} as :meth:`RailResult.loss_breakdown` gives."""
        import numpy as np
        self._shown = (current, baseline, title)
        c = theme.tokens(theme.current())
        fig = self.figure
        fig.clear()
        fig.set_facecolor(c["panel"])
        ax = fig.add_subplot()
        ax.set_facecolor(c["panel"])
        if not current:
            ax.axis("off")
            ax.text(0.5, 0.5, "Solve a rail to see where its copper loss is", ha="center",
                    va="center", color=c["muted"], transform=ax.transAxes)
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
            bars.append(ax.barh(y - h / 2 - 0.02, base, height=h, color=c["series-2"], label="baseline"))
            bars.append(ax.barh(y + h / 2 + 0.02, cur, height=h, color=c["series-1"], label="current"))
            self.series = ["baseline", "current"]
        else:
            bars.append(ax.barh(y, cur, height=0.5, color=c["series-1"]))
            self.series = ["current"]
        top = max(float(max((b.datavalues.max() for b in bars), default=0.0)), 1e-12)
        for b in bars:
            ax.bar_label(b, labels=[f"{v:.3g}" for v in b.datavalues], padding=3, color=c["fg"], fontsize=8)
        ax.set_yticks(y, names, color=c["fg"], fontsize=8)
        ax.invert_yaxis()                                   # layers top to bottom, as stacked
        ax.set_xlim(0, top * 1.18)
        ax.set_xlabel("Copper loss (mW)", color=c["muted"], fontsize=8)
        ax.tick_params(axis="x", colors=c["muted"], labelsize=8)
        ax.tick_params(axis="y", length=0)
        ax.grid(axis="x", color=c["line"], linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right", "bottom"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(c["outline"])
        if title:
            ax.set_title(title, loc="left", color=c["fg"], fontsize=9)
        if baseline:
            ax.legend(frameon=False, fontsize=8, labelcolor=c["fg"], loc="lower right")
        self.canvas.draw_idle()


class ComparePanel(QWidget):
    """Before/after of a rail: pin a solved result as the baseline, solve again, read the
    deltas (current − baseline) per load, for the whole rail and per layer."""
    pin_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pin = QPushButton("Pin baseline")
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
        rows.append((cmp_.get("max_drop_label", "Largest drop on the rail (mV)"), num(md["a"]), num(md["b"]),
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


# ---------------------------------------------------------------------- power tree

def _cell(x: Optional[float], spec: str, scale: float = 1.0) -> str:
    return "—" if x is None or x != x else f"{x * scale:{spec}}"


def reference_note(ref) -> str:
    """The line under "0 V at" for a tree's reference (:class:`~coppernicus.results.TreeReference`):
    what the voltages are shown from, and that drops, across and verdicts do not move."""
    if ref is None or ref.given is None:
        return ("Voltages from the solve's 0 V, held at the sources' ground pins: every drop, rise and "
                "verdict is taken there.")
    pads = ", ".join(ref.pads[:4]) + (f" (+{len(ref.pads) - 4})" if len(ref.pads) > 4 else "")
    spread = f", spread {ref.spread_v * 1e3:.3f} mV" if ref.spread_v else ""
    return (f"Voltages shown from 0 V at {ref.label} ({pads}), {ref.offset_v * 1e3:+.3f} mV in the solve's "
            f"frame{spread}: V out, V in, supply, ground, the voltage map and the readouts move by it. "
            "Drops, across and every verdict (the ground rise too) stay from the solve's 0 V.")


#: The "0 V at" choice that is the solve's own 0 V (the sources' ground pins), and a pad's.
SOLVE_ZERO = "the sources' ground pins (the solve's 0 V)"
A_PAD = "a pad (REF.PAD, or Pick)"


class TreePanel(QWidget):
    """The Power tree tab: every source and regulator, every load with the voltage across its
    supply and ground pins, and the links. Double-click a row to show its group.

    "0 V at" (S2): where the voltages are shown from, a display shift with no re-solve: the
    solve's own 0 V, a part's ground pins, or a pad typed as REF.PAD or picked on the map.
    It asks for it (:attr:`reference_requested`: a ``reference_spec``, None for the solve's);
    the window reads it off the tree and shows the tables, the voltage map and the readouts
    from it. Drops, across and verdicts never move."""
    group_requested = Signal(str)
    reference_requested = Signal(object)         # {"ground_of": REF} / {"pads": [...]} / None
    pick_toggled = Signal(bool)                  # Pick a pad on the map as 0 V

    VRM_HEADERS = ["Name", "Kind", "Group", "V set", "V out", "Across (V)", "I out (A)", "V in",
                   "I in (A)", "P loss (mW)", "Ground (mV)", "Regulation"]
    LOAD_HEADERS = ["Name", "Group", "I (A)", "Nominal", "Supply (V)", "Ground (mV)", "Across, mean (V)",
                    "Worst pins (V)", "Drop (mV)", "Drop (%)", "Verdict"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summary = QLabel("<i>Solve tree (Ctrl+T) solves every rail and ground "
                              "together</i>")
        self.summary.setWordWrap(True)
        self.reference_box = FieldComboBox()
        self.reference_box.setToolTip("Where the voltages are shown from: a display shift, nothing is "
                                      "solved again. Drops, across and every verdict (the ground rise "
                                      "too) stay from the solve's 0 V, at the sources' ground pins.")
        self.reference_box.addItem(SOLVE_ZERO, None)
        self.reference_box.activated.connect(self._reference_chosen)
        self.reference_pad = QLineEdit()
        self.reference_pad.setPlaceholderText("REF.PAD, e.g. U3.2 or R12.2, NT1.*")
        self.reference_pad.returnPressed.connect(self._pad_entered)
        self.reference_pick = QPushButton("Pick")
        self.reference_pick.setCheckable(True)
        self.reference_pick.setToolTip("Click a pad on the map: 0 V at it · Esc to stop")
        self.reference_pick.toggled.connect(self.pick_toggled)
        self.reference_note = QLabel()            # what 0 V is now, or why a choice was refused
        self.reference_note.setWordWrap(True)
        self.asked = False                        # "0 V at" chosen here (else the project's)
        self.vrms = self._table(self.VRM_HEADERS)
        self.loads = self._table(self.LOAD_HEADERS)
        self.links = QLabel()
        self.links.setWordWrap(True)
        self.notes = QLabel()                    # the tree's warnings, e.g. a 0 V rail's sign
        self.notes.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(QLabel("0 V at:"))
        row.addWidget(self.reference_box, 2)
        row.addWidget(self.reference_pad, 2)
        row.addWidget(self.reference_pick)
        layout.addLayout(row)
        layout.addWidget(self.reference_note)
        layout.addWidget(QLabel("<b>Sources and regulators</b>"))
        layout.addWidget(self.vrms, 2)
        layout.addWidget(QLabel("<b>Loads</b> (the voltage across their supply and ground pins)"))
        layout.addWidget(self.loads, 3)
        self.loads_note = QLabel(f"<small>{html.escape(ACROSS_NOTE[0].upper() + ACROSS_NOTE[1:])}</small>")
        self.loads_note.setWordWrap(True)
        layout.addWidget(self.loads_note)
        layout.addWidget(self.links)
        layout.addWidget(self.notes)

    def _table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.cellDoubleClicked.connect(lambda r, _c, t=table: self._activated(t, r))
        return table

    def _activated(self, table, row: int):
        item = table.item(row, 2 if table is self.vrms else 1)
        if item is not None:
            self.group_requested.emit(item.text())

    @staticmethod
    def _fill(table, rows, fails=()):
        table.setRowCount(0)
        for r, row in enumerate(rows):
            table.insertRow(r)
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if c > 1 and text[:1] in "-0123456789":
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if r in fails:
                    item.setForeground(theme.brush("fail"))
                table.setItem(r, c, item)

    # ------------------------------------------------------------------ "0 V at" (S2)
    def _reference_chosen(self, index: int):
        self.asked = True
        data = self.reference_box.itemData(index)
        if data == "pad":                        # a pad: typed (Enter) or picked
            self.reference_pad.setFocus()
            if self.reference_pad.text().strip():
                self._pad_entered()
            return
        self.reference_requested.emit(None if data is None else {"ground_of": data})

    def _pad_entered(self):
        text = self.reference_pad.text().strip()
        if not text:
            return
        self.asked = True
        self._select_reference("pad")
        self.reference_requested.emit({"pads": [p.strip() for p in text.split(",") if p.strip()]})

    def _select_reference(self, data):
        i = self.reference_box.findData(data)
        self.reference_box.setCurrentIndex(max(0, i))

    def picking(self) -> bool:
        """Whether Pick is on: the next click on the map picks the pad that is 0 V."""
        return self.reference_pick.isChecked()

    def stop_pick(self):
        self.reference_pick.setChecked(False)

    def picked(self, pad: str):
        """``pad`` ("REF.PAD") was picked on the map: ask for 0 V at it, and stop picking."""
        self.reference_pad.setText(pad)
        self.stop_pick()
        self._pad_entered()

    def reference_spec(self) -> Optional[Dict]:
        """The choice shown now, as a ``reference_spec`` (None: the solve's own 0 V)."""
        data = self.reference_box.currentData()
        if data == "pad":
            pads = [p.strip() for p in self.reference_pad.text().split(",") if p.strip()]
            return {"pads": pads} if pads else None
        return None if data is None else {"ground_of": data}

    def show_reference(self, spec: Optional[Dict], parts: Iterable[str] = ()):
        """List ``parts`` (each a part whose ground pins are solved) as choices and show
        ``spec`` chosen (as the window applied it), without asking for it again."""
        self.reference_box.blockSignals(True)
        self.reference_box.clear()
        self.reference_box.addItem(SOLVE_ZERO, None)
        for ref in parts:
            self.reference_box.addItem(f"{ref}'s ground pins", ref)
        self.reference_box.addItem(A_PAD, "pad")
        if spec and "pads" in spec:
            self.reference_pad.setText(", ".join(spec["pads"]))
            self._select_reference("pad")
        elif spec and self.reference_box.findData(spec.get("ground_of")) < 0:
            self.reference_box.addItem(f"{spec['ground_of']}'s ground pins", spec["ground_of"])
            self._select_reference(spec["ground_of"])
        else:
            self._select_reference(spec.get("ground_of") if spec else None)
        self.reference_box.blockSignals(False)

    def set_reference_note(self, text: str, error: bool = False):
        text = html.escape(text, quote=False)            # "U1's" stays readable in .text()
        self.reference_note.setText(theme.span("fail", text) if error else text)

    def clear(self, text: str = ""):
        self.summary.setText(text or "<i>No power-tree result</i>")
        self.vrms.setColumnCount(len(self.VRM_HEADERS))       # no I max column left behind
        self.vrms.setHorizontalHeaderLabels(self.VRM_HEADERS)
        self._fill(self.vrms, [])
        self._fill(self.loads, [])
        self.links.clear()
        self.notes.clear()
        self.reference_note.clear()

    def set_tree(self, tree, verdict_html: str = "", i_max: Optional[Mapping[str, Optional[float]]] = None):
        """Show ``tree``. ``i_max``: each VRM's rated output current by name (I max, see
        ``MainWindow``'s ``tree_i_max``), in a column after I out only when one has a value. A
        regulator over its rating is in red, as a load over its limit."""
        rated = any(a is not None for a in (i_max or {}).values())
        headers = list(self.VRM_HEADERS)
        if rated:
            headers.insert(headers.index("I out (A)") + 1, "I max (A)")
        self.vrms.setColumnCount(len(headers))
        self.vrms.setHorizontalHeaderLabels(headers)
        # S2: absolute voltages from the reference chosen ("0 V at"); across, drops and
        # verdicts never move. The solve's own 0 V shifts nothing.
        ref = getattr(tree, "reference", None)
        shown = ref.shown if ref is not None and ref.given is not None else (lambda v: v)
        vrms = [(v.name, v.kind, v.group, f"{v.v_set_v:g}", _cell(shown(v.v_out_v), ".5f"),
                 _cell(getattr(v, "v_out_across_v", None), ".5f"), _cell(v.i_out_a, ".4f"))
                + ((_cell(i_max.get(v.name), ".4g"),) if rated else ())
                + (_cell(shown(v.v_in_v), ".5f"), _cell(v.i_in_a, ".4f"), _cell(v.p_loss_w, ".1f", 1e3),
                   _cell(shown(v.v_ground_v), ".2f", 1e3), v.regulation)
                for v in tree.vrms]
        over = {r for r, v in enumerate(tree.vrms) if v.current_passed is False}   # over its rating
        loads, fails = [], set()
        for ld in tree.loads:
            verdict = ld.status or ("FAIL" if ld.passed is False else "pass" if ld.passed else "not judged")
            if ld.passed is False:
                fails.add(len(loads))
            loads.append((ld.name, ld.group, _cell(ld.current_a, ".4g"), f"{ld.nominal_v:g}",
                          _cell(shown(ld.v_supply_v), ".5f"), _cell(shown(ld.v_ground_v), ".2f", 1e3),
                          _cell(ld.v_across_v, ".5f"), _cell(ld.v_worst_v, ".5f"),
                          _cell(ld.drop_v, ".2f", 1e3), _cell(ld.drop_pct, ".2f"), verdict))
        # A regulator's input is a load on the rail feeding it, judged as one.
        for v in (v for v in tree.vrms if v.input_group):
            verdict = v.input_status or ("FAIL" if v.input_passed is False
                                         else "pass" if v.input_passed else "not judged")
            if v.input_passed is False:
                fails.add(len(loads))
            across = (v.v_in_v - (v.v_ground_v or 0.0)) if v.v_in_v is not None else None
            loads.append((f"{v.name} (input)", v.input_group, _cell(v.i_in_a, ".4g"),
                          _cell(v.input_nominal_v, "g"), _cell(shown(v.v_in_v), ".5f"),
                          _cell(shown(v.v_ground_v), ".2f", 1e3), _cell(across, ".5f"),
                          _cell(v.v_in_worst_v, ".5f"),
                          _cell(v.input_drop_v, ".2f", 1e3), _cell(v.input_drop_pct, ".2f"), verdict))
        self._fill(self.vrms, vrms, over)
        self._fill(self.loads, loads, fails)
        self.links.setText("Links (edit them in the project file): " + "; ".join(
            f"{k['name']} {k['resistance_ohm']:g} Ω, {' ↔ '.join(k['nets'])}: {k['current_a']:.4f} A, "
            f"{k['power_w'] * 1e3:.3g} mW" for k in tree.links if not k["name"].startswith("net tie "))
            if any(not k["name"].startswith("net tie ") for k in tree.links) else "")
        self.notes.setText(theme.span("warn", "<b>Warnings</b>") + ": " + " · ".join(
            html.escape(w) for w in tree.warnings) if tree.warnings else "")
        groups = ", ".join(f"{k} ({r.kind})" for k, r in tree.groups.items())
        self.summary.setText(f"<b>Power tree</b>: {len(tree.groups)} groups ({groups}) solved in "
                             f"{tree.stats.get('seconds', 0.0):.1f} s · {verdict_html}")

    def vrm_rows(self):
        return [tuple(self.vrms.item(r, c).text() for c in range(self.vrms.columnCount()))
                for r in range(self.vrms.rowCount())]

    def load_rows(self):
        return [tuple(self.loads.item(r, c).text() for c in range(self.loads.columnCount()))
                for r in range(self.loads.rowCount())]
