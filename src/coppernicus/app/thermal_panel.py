"""The Temperature results tab: the project's electro-thermal options, written to
``project.thermal``, and the board temperature of the last power-tree solve: its verdict
(only against the user's limits), the hottest solved copper per layer with the pad nearest
to it, how the iterations settled, where the heat came from, the load drops at the ambient
against the electro-thermal ones, and the assumptions. Double-click a layer row to see it on
the map. The text comes from :func:`coppernicus.temperature.thermal_summary` through the pure
functions below, so it is the same the CLI prints."""

from __future__ import annotations

import dataclasses
import html
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app import settings as app_settings
from coppernicus.app import theme
from coppernicus.app.panels import DecimalSpinBox, FieldComboBox, OptionalDecimalValidator, WheelWhenFocused
from coppernicus.engine.ac import total_memory_bytes
from coppernicus.engine.thermal import thermal_cost_text, thermal_ram_estimate, thermal_size
from coppernicus.project import ThermalOptions
from coppernicus.temperature import THERMAL_LIMITS, thermal_summary, thermal_verdict

LAYER_HEADERS = ["Layer", "Hottest copper (°C)", "Rise (°C)", "x (mm)", "y (mm)", "Group", "Nearest pad",
                 "Distance (mm)", "Verdict"]
LOAD_HEADERS = ["Load", "Group", "Drop at ambient (mV)", "Electro-thermal (mV)", "Change (%)"]
REGULATOR_HEAT_LABELS = (("output_pads", "output pads"), ("part_pads", "all the part's pads"))
CANCELLING = "Cancelling: stops at the next iteration"
LAST_SOLVE_SETTING = "thermal/last_solve"   # the last electro-thermal solve's time and size (QSettings)
NO_MAP = ("No board temperature: tick Electro-thermal in the Temperature tab and solve the power tree "
          "(Ctrl+T)")
NOT_SOLVED_TEXT = ("<b>Board temperature</b>: not solved. Tick Electro-thermal and solve the power "
                   "tree (Ctrl+T).")


# ---------------------------------------------------------------------- text (pure)

def _num(x: Optional[float], spec: str) -> str:
    return "—" if x is None or not math.isfinite(x) else f"{x:{spec}}"


def _change(dt_c: float) -> str:
    """A temperature change: two decimals, or two significant digits below 0.005 °C."""
    return f"{dt_c:.2f}" if abs(dt_c) >= 0.005 else f"{dt_c:.2g}"


def history_text(doc: Mapping[str, Any]) -> str:
    """Each iteration's peak and how the solve settled, from a ``thermal_summary``:
    "41.33 → 42.26 → 42.31 °C, last change 0.05 °C: settled"."""
    hist = doc.get("history") or []
    if not hist:
        return ""
    peaks = " → ".join(_num(h["t_max_c"], ".2f") for h in hist)
    if doc.get("settled") is None:
        return f"{peaks} °C, one iteration: settling not checked"
    state = "settled" if doc["settled"] else "not settled, run more iterations"
    return f"{peaks} °C, last change {_change(hist[-1]['dt_max_c'])} °C: {state}"


def heat_text(doc: Mapping[str, Any]) -> str:
    """"Heat 0.591 W: copper 0.414 W, vias 0.177 W", largest first."""
    kinds = sorted((doc.get("heat_by_kind_w") or {}).items(), key=lambda kv: -(kv[1] or 0.0))
    return f"Heat {_num(doc['heat_w'], '.3g')} W: " + ", ".join(f"{k} {_num(w, '.3g')} W" for k, w in kinds)


def _pad(pad: Optional[Mapping[str, Any]]) -> str:
    if not pad:
        return "no pad on the board"
    return f"nearest pad {pad['ref']}.{pad['pad']} ({pad['distance_mm']:.2f} mm)"


def peak_text(doc: Mapping[str, Any]) -> str:
    """The hottest grid cell, any layer: shown, never judged."""
    return (f"Grid peak {_num(doc['peak_c'], '.2f')} °C (rise {_num(doc['peak_rise_c'], '.2f')} °C) on "
            f"{doc['peak_layer']} at ({doc['peak_x_mm']:.2f}, {doc['peak_y_mm']:.2f}) mm, "
            f"{_pad(doc.get('peak_nearest_pad'))}: shown, not judged")


def layer_rows(doc: Mapping[str, Any], stuck: Iterable[str] = ()) -> List[Tuple[str, ...]]:
    """The per-layer table (:data:`LAYER_HEADERS`). A row over a limit FAILs, settled or not;
    one within the limits passes only when it is judged: the solve settled and its group
    (``stuck``: groups that did not converge) converged, as
    :func:`coppernicus.temperature.judge` rules."""
    stuck = set(stuck)
    rows = []
    for r in doc.get("layers") or []:
        exceeds = r.get("exceeds_limit")
        if exceeds:
            verdict = "FAIL"
        elif exceeds is False and doc.get("settled") is True and r["group"] not in stuck:
            verdict = "pass"
        else:
            verdict = "not judged"
        p = r.get("nearest_pad")
        rows.append((r["layer"], _num(r["t_max_c"], ".2f"), _num(r["rise_c"], ".2f"), _num(r["x_mm"], ".2f"),
                     _num(r["y_mm"], ".2f"), r["group"], f"{p['ref']}.{p['pad']}" if p else "—",
                     _num(p["distance_mm"], ".2f") if p else "—", verdict))
    return rows


def load_rows(doc: Mapping[str, Any]) -> List[Tuple[str, ...]]:
    """The load-drop table (:data:`LOAD_HEADERS`): copper at the ambient (iteration 1)
    against the electro-thermal drop."""
    return [(ld["name"], ld["group"], _num(ld["drop_ambient_mv"], ".3f"), _num(ld["drop_mv"], ".3f"),
             _num(ld.get("change_pct"), "+.1f")) for ld in doc.get("loads") or []]


def estimate_text(cost: str, ram) -> str:
    """The pre-solve estimate: KiPI's grid and the last solve's time (``cost``), and the
    memory of the model (:func:`coppernicus.engine.thermal.thermal_ram_estimate`)."""
    risk = "It could exceed this machine's memory. " if ram.risk else ""
    return f"Before solving: {cost}. {risk}{ram.text}"


def stats_text(st: Mapping[str, Any], cells: int, layers: int) -> str:
    """One line of an electro-thermal solve's stats, with the memory peak and whether this
    solve raised it (the AC tab's wording)."""
    parts = [f"solved in {float(st.get('seconds', 0.0)):.1f} s",
             f"{cells * layers:,} unknowns ({cells:,} cells × {layers} layers)"]
    mb = st.get("memory_peak_mb")
    if mb is None:
        parts.append("memory peak cannot be read on this system")
    elif st.get("memory_peak_raised"):
        parts.append(f"memory peak {mb:.0f} MB (this solve raised the process's peak)")
    else:
        parts.append(f"memory peak {mb:.0f} MB: the process's peak so far; this solve stayed at or below it")
    return " · ".join(parts)


def _settings() -> QSettings:
    return app_settings()


def last_solve() -> Optional[Dict[str, float]]:
    """The last electro-thermal solve on this computer: ``seconds`` and ``unknowns``; None
    without one (or a damaged setting)."""
    try:
        text = _settings().value(LAST_SOLVE_SETTING, "")
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def record_solve(tree) -> None:
    """Keep an electro-thermal solve's time and size for the next estimate."""
    try:
        t = tree.thermal
        _settings().setValue(LAST_SOLVE_SETTING, json.dumps(
            {"seconds": float(tree.stats["seconds"]), "unknowns": float(t.grid_cells * len(t.maps))}))
    except (AttributeError, KeyError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------- the form (pure)

# output_part_heat_pct is set in the project file only: the form edits the project's own
# options object, so it is kept as it is.
FORM_KEYS = tuple(f.name for f in dataclasses.fields(ThermalOptions) if f.name != "output_part_heat_pct")


def form_values(options: ThermalOptions) -> Dict[str, Any]:
    """What the form shows for ``options``: the limits as text, empty when not checked."""
    out = {k: v for k, v in vars(options).items() if k in FORM_KEYS}
    for key in THERMAL_LIMITS:
        out[key] = "" if out[key] is None else f"{out[key]:g}"
    return out


def field_value(key: str, raw: Any) -> Any:
    """The project value of form field ``key`` (a ``ThermalOptions`` name) from what the
    form holds: a limit's text (empty = not checked; a comma reads as the decimal sign), a
    number, a tick or the regulator-heat choice."""
    if key in ("enabled", "load_heat"):
        return bool(raw)
    if key == "iterations":
        return int(raw)
    if key == "regulator_heat":
        return str(raw)
    if key in THERMAL_LIMITS:
        text = str(raw).strip().replace(",", ".")
        return float(text) if text else None
    if key in ("ambient_c", "airflow_m_s", "grid_mm"):
        return float(raw)
    raise KeyError(key)


def is_limit(key: str) -> bool:
    """A limit only re-judges the solved board temperature; any other option makes it stale."""
    return key in THERMAL_LIMITS


class _IntSpin(WheelWhenFocused, QSpinBox):
    """The iterations field: it takes the wheel only when focused, as the form's other fields
    (:class:`coppernicus.app.panels.WheelWhenFocused`): any change of an option drops the solved
    power tree."""


# ---------------------------------------------------------------------- the tab

class ThermalPanel(QWidget):
    """The Temperature tab (see the module)."""
    options_changed = Signal(str)                    # the option's name: may make the tree stale
    limits_changed = Signal()                        # re-judge only, no solve
    layer_requested = Signal(str, float, float, str)    # layer, x mm, y mm, group
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._options: Optional[ThermalOptions] = None
        self._loading = False
        self._points: List[Tuple[str, float, float, str]] = []

        self.enabled = QCheckBox("Electro-thermal: Solve tree heats the copper")
        self.enabled.setToolTip("Solve tree (Ctrl+T) also solves the board temperature: the copper "
                                "heated by its own losses, solved again at that temperature (KiPI's "
                                "co-simulation). Solve rail (F5) is not heated.")
        self.ambient = DecimalSpinBox(decimals=2, minimum=-100.0, maximum=1000.0, singleStep=1.0,
                                      suffix=" °C")
        self.ambient.setToolTip("The air and the board without current")
        self.airflow = DecimalSpinBox(decimals=2, minimum=0.0, maximum=1000.0, singleStep=0.5, suffix=" m/s")
        self.airflow.setToolTip("Air speed over both faces: convection h = 10 + 3.8·v W/m²K per face (KiPI)")
        self.iterations = _IntSpin(minimum=1, maximum=10000)
        self.iterations.setToolTip("Electro-thermal passes. Settled when the last one changed no cell by "
                                   "more than 5 % of the peak rise.")
        self.grid = DecimalSpinBox(decimals=3, minimum=0.0, maximum=1000.0, singleStep=0.05)
        self.grid.setSpecialValueText("automatic")
        self.grid.setToolTip("Square cell of the heat grid; 0 = automatic (about 250 cells across the board)")
        self.load_heat = QCheckBox("Heat from the loads")
        self.load_heat.setToolTip("Also each load's own power (V·I), at its pins")
        self.regulator_heat = FieldComboBox()
        for key, label in REGULATOR_HEAT_LABELS:
            self.regulator_heat.addItem(label, key)
        self.regulator_heat.setToolTip("Where a regulator's conversion loss (input − output power) goes: "
                                       "its output pads (KiPI's placement) or spread evenly over every pad "
                                       "of its own part, the one with its input pins (an external-"
                                       "inductor buck's IC); output_part_heat_pct in the project file "
                                       "moves a share you enter to its inductor")
        self.max_temp = QLineEdit(placeholderText="empty = not checked")
        self.max_temp.setValidator(OptionalDecimalValidator(-273.15, 1e4, 3))
        self.max_temp.setToolTip("Fail when a layer's hottest solved copper is hotter")
        self.max_rise = QLineEdit(placeholderText="above the ambient")
        self.max_rise.setValidator(OptionalDecimalValidator(0.0, 1e4, 3))
        self.max_rise.setToolTip("Fail when a layer's hottest solved copper rises more above the ambient")
        self.options_box = QGroupBox("Electro-thermal options (whole project)")
        form = QFormLayout(self.options_box)
        form.addRow(self.enabled)
        form.addRow("Ambient", self.ambient)
        form.addRow("Airflow", self.airflow)
        form.addRow("Iterations", self.iterations)
        form.addRow("Grid (mm, 0 = automatic)", self.grid)
        form.addRow(self.load_heat)
        form.addRow("Regulator heat", self.regulator_heat)
        form.addRow("Max copper temperature (°C)", self.max_temp)
        form.addRow("Max rise (°C)", self.max_rise)
        self.options_box.setEnabled(False)
        self.cancel = QPushButton("Cancel solve")
        self.cancel.setToolTip("Stop the power-tree solve (checked once per iteration)")
        self.cancel.setEnabled(False)
        self.state = QLabel()
        self.state.setWordWrap(True)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.options_box)
        row = QHBoxLayout()
        row.addWidget(self.cancel)
        row.addWidget(self.state, 1)
        ll.addLayout(row)
        self.estimate = QLabel()           # before an electro-thermal solve: its cost and memory
        self.estimate.setWordWrap(True)
        self.estimate.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ll.addWidget(self.estimate)
        ll.addStretch(1)
        scroll = QScrollArea()                       # the form is taller than the dock
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(left.sizeHint().width() + scroll.verticalScrollBar().sizeHint().width())

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.layers = self._table(LAYER_HEADERS)
        self.layers.setToolTip("Double-click a row: its layer's board temperature, zoomed to that copper")
        self.layers.cellDoubleClicked.connect(self._activated)
        self.loads = self._table(LOAD_HEADERS)
        tables = QSplitter(Qt.Horizontal)
        for title, table in (("<b>Hottest solved copper per layer</b> (judged; double-click: show it)",
                              self.layers),
                             ("<b>Load drops</b>: copper at the ambient (iteration 1) → electro-thermal",
                              self.loads)):
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            label = QLabel(title)
            label.setWordWrap(True)
            bl.addWidget(label)
            bl.addWidget(table, 1)
            tables.addWidget(box)
        tables.setStretchFactor(0, 3)
        tables.setStretchFactor(1, 2)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMaximumHeight(72)
        self.notes.setPlaceholderText("Assumptions and warnings of the board temperature")
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.summary)
        rl.addWidget(self.details)
        rl.addWidget(tables, 1)
        rl.addWidget(self.notes)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(scroll)
        self.setup_area = scroll
        layout.addWidget(right, 1)

        self.enabled.toggled.connect(lambda on: self._edited("enabled", on))
        self.ambient.valueChanged.connect(lambda v: self._edited("ambient_c", v))
        self.airflow.valueChanged.connect(lambda v: self._edited("airflow_m_s", v))
        self.iterations.valueChanged.connect(lambda v: self._edited("iterations", v))
        self.grid.valueChanged.connect(lambda v: self._edited("grid_mm", v))
        self.load_heat.toggled.connect(lambda on: self._edited("load_heat", on))
        self.regulator_heat.currentIndexChanged.connect(
            lambda _i: self._edited("regulator_heat", self.regulator_heat.currentData()))
        self.max_temp.editingFinished.connect(lambda: self._edited("max_temp_c", self.max_temp.text()))
        self.max_rise.editingFinished.connect(lambda: self._edited("max_rise_c", self.max_rise.text()))
        self.cancel.clicked.connect(self.cancel_requested.emit)
        self.clear()

    def take_setup(self) -> QWidget:
        """The editor, handed to the window's setup panel (layout C): the page keeps
        the per-layer and load tables."""
        self.setup_area.setParent(None)
        return self.setup_area

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        return table

    # ------------------------------------------------------------------ options
    def set_options(self, options: ThermalOptions):
        """Show (and from now on edit) ``options``, the project's own object; writes nothing."""
        self._loading = True
        self._options = options
        values = form_values(options)
        self.enabled.setChecked(values["enabled"])
        self.ambient.setValue(values["ambient_c"])
        self.airflow.setValue(values["airflow_m_s"])
        self.iterations.setValue(values["iterations"])
        self.grid.setValue(values["grid_mm"])
        self.load_heat.setChecked(values["load_heat"])
        self.regulator_heat.setCurrentIndex(max(0, self.regulator_heat.findData(values["regulator_heat"])))
        self.max_temp.setText(values["max_temp_c"])
        self.max_rise.setText(values["max_rise_c"])
        self.options_box.setEnabled(True)
        self._loading = False

    def _edited(self, key: str, raw):
        """One field edited: written to the options only when its value changed (tabbing
        through the form changes nothing), then the matching signal."""
        if self._loading or self._options is None:
            return
        value = field_value(key, raw)
        if value == getattr(self._options, key):
            return
        setattr(self._options, key, value)
        if is_limit(key):
            self.limits_changed.emit()
        else:
            self.options_changed.emit(key)

    # ------------------------------------------------------------------ solve state
    def set_solving(self, on: bool):
        """A power-tree solve is running (Cancel enabled) or not."""
        self.cancel.setEnabled(on)
        self.state.setText("Solving the power tree…" if on else "")

    def set_cancelling(self):
        self.cancel.setEnabled(False)
        self.state.setText(CANCELLING)

    def show_estimate(self, board, options: ThermalOptions) -> Tuple[str, bool]:
        """Before an electro-thermal solve: its rough cost and memory, shown in the tab; True
        when the model could exceed this machine's memory. An estimate never stops a solve."""
        try:
            size = thermal_size(board, options)
        except Exception as e:                       # noqa: BLE001 - shown, never raised
            text = f"Before solving: no estimate ({e})"
            self.estimate.setText(text)
            return text, False
        ram = thermal_ram_estimate(size.unknowns, size.layers, total_memory_bytes())
        text = estimate_text(thermal_cost_text(size, last_solve()), ram)
        self.estimate.setText(text)
        return text, ram.risk

    # ------------------------------------------------------------------ results
    def clear(self, text: str = ""):
        self.summary.setText(text or NOT_SOLVED_TEXT)
        self.details.clear()
        self._fill(self.layers, [])
        self._fill(self.loads, [])
        self.notes.clear()
        self._points = []

    def set_tree(self, tree, verdict_html: str = ""):
        """The board temperature of power tree ``tree``, judged; ``verdict_html`` is its
        verdict line (the main window's ``verdict_html``). A tree solved without it lists
        the limits set as not judged."""
        if tree.thermal is None:
            tv = thermal_verdict(tree)
            self.clear(NOT_SOLVED_TEXT + (f" · {verdict_html}" if tv.limits else ""))
            return
        doc = thermal_summary(tree)
        stuck = {k for k, g in tree.groups.items() if not g.converged}
        n = doc["iterations"]
        head = (f"<b>Board temperature</b> (electro-thermal, {n} iteration{'s' if n != 1 else ''}; ambient "
                f"{doc['ambient_c']:g} °C, airflow {doc['airflow_m_s']:g} m/s; {doc['grid_nx']} × "
                f"{doc['grid_ny']} cells of {doc['grid_mm']:.3g} mm): {verdict_html}")
        if doc["limits"]:
            head += f"<br>{html.escape(doc['verdict_note'])}"
        self.summary.setText(head)
        temp = tree.thermal
        self.details.setText("<br>".join(html.escape(t) for t in (
            peak_text(doc), history_text(doc), heat_text(doc),
            stats_text(tree.stats, temp.grid_cells, len(temp.maps)))))
        rows = layer_rows(doc, stuck)
        self._fill(self.layers, rows, {i for i, r in enumerate(rows) if r[-1] == "FAIL"})
        self._points = [(r["layer"], float(r["x_mm"]), float(r["y_mm"]), r["group"]) for r in doc["layers"]]
        self._fill(self.loads, load_rows(doc))
        self.notes.setPlainText("\n".join([*(f"Warning: {w}" for w in doc["warnings"]), *doc["assumptions"]]))

    @staticmethod
    def _fill(table, rows, fails=()):
        table.setRowCount(0)
        for r, row in enumerate(rows):
            table.insertRow(r)
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if text[:1] in "-+0123456789" and c > 0:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if r in fails:
                    item.setForeground(theme.brush("fail"))
                table.setItem(r, c, item)

    def _activated(self, row: int, _col: int = 0):
        if 0 <= row < len(self._points):
            self.layer_requested.emit(*self._points[row])

    def shown_layer_rows(self):
        """The per-layer table as strings, for scripts and tests."""
        return [tuple(self.layers.item(r, c).text() for c in range(self.layers.columnCount()))
                for r in range(self.layers.rowCount())]

    def shown_load_rows(self):
        return [tuple(self.loads.item(r, c).text() for c in range(self.loads.columnCount()))
                for r in range(self.loads.rowCount())]
