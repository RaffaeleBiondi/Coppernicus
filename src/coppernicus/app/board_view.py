"""The 2D board view: the only class in the app that knows the renderer (VTK via pyvistaqt).

Shows one copper layer at a time: the board outline, the net's copper outline, and after a
solve a colour map (voltage drop, voltage or current density) with the sources, loads and
vias on top, or the board temperature of an electro-thermal power-tree solve. Left-drag
pans, the wheel zooms, a double-click reads the values under the pointer and, as the pointer
moves, a label beside it shows the value of the map shown as one large number, with the
values chosen in View → Hover shows under it. In pick mode a single click (a press and
release less than 3 px apart, so a pan is never a click) reports the board point under it;
in measure mode two clicks give the voltage difference between two points. Drop isolines
and current-flow arrows can be laid over any map. KiCad's y axis points down; the view
plots (x, −y) so the board reads as in KiCad.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

os.environ.setdefault("QT_API", "pyside6")

import numpy as np  # noqa: E402
import pyvista as pv  # noqa: E402
from matplotlib import colormaps  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget  # noqa: E402
from pyvistaqt import QtInteractor  # noqa: E402

from coppernicus.app import settings as app_settings  # noqa: E402
from coppernicus.app import theme  # noqa: E402
from coppernicus.app.hover import (  # noqa: E402
    HOVER_DEFAULT,
    HOVER_VALUES,
    HoverReadout,
    chosen_from_setting,
    hover_readout,
    hover_values,
    label_position,
    setting_from_chosen,
    temperature_reading,
    temperature_readout,
)

# TriangleLocator is also re-exported here for code that imported it from this module.
from coppernicus.fields import TriangleLocator, drop_levels, probe, sample_flow  # noqa: E402
from coppernicus.fields import isolines as isoline_paths  # noqa: E402
from coppernicus.problems import PEAK_NOTE, PEAK_TIP, DensityCheck, density_check  # noqa: E402
from coppernicus.results import LayerField, RailResult  # noqa: E402

FIELDS = {
    "drop": ("Voltage drop (mV)", "viridis"),
    "voltage": ("Voltage (V)", "viridis"),
    "current": ("Current density (A/mm²)", "magma"),
    # Before/after: two hues and a light neutral midpoint (no change).
    "difference": ("Δ drop vs baseline (mV)", "RdBu_r"),
    # The board temperature of the power tree's electro-thermal solve (show_temperature).
    "temperature": ("Board temperature (°C)", "inferno"),
}
J_CLIP_PERCENTILE = 99.5
COLORBAR_WIDTH = 120              # px: the scale, its title and the peak label above it
# The verdict line above the |J| scale: the theme's verdict colours, each at least 4.5:1 on its map.
VERDICT_TOKENS = {"fail": "fail", "pass": "pass", "attention": "warn"}
SOURCE_COLOR, LOAD_COLOR, COPPER_COLOR = "#1a9850", "#d73027", "#b87333"   # the board's outline: theme
CANDIDATE_COLOR = "#7a4a1d"       # outlines of the pads that can be picked
PAD_STYLE = {"source": (SOURCE_COLOR, 3.0), "load": (LOAD_COLOR, 3.0), "candidate": (CANDIDATE_COLOR, 1.0)}
CLICK_SLOP_PX = 3                 # a press and release closer than this is a click, not a pan
# Isolines: dark on the drop/voltage map (viridis), light on the current map (magma, dark).
ISOLINE_COLOR = {"drop": "#111111", "voltage": "#111111", "current": "#e8e8e8", "difference": "#111111"}
ARROW_COLOR, ARROW_HALO, MEASURE_COLOR = "#ffffff", "#111111", "#0b62d6"
# The ring round the selected problem: a light stroke on a dark halo, as the arrows, so it reads
# on every colour scale; its radius a share of the layer's larger side (a marker, not the
# problem's extent).
MARK_STROKE, MARK_HALO, MARK_SHARE = "#ffffff", "#111111", 0.03
OUTSIDE_COLOR, HOTTEST_COLOR = "#bfbfbf", "#00bfff"   # temperature map: as temperature.write_png
ISOLINE_COUNT = 10
MAX_ARROWS = 2000
ARROW_CELLS = 36                  # arrows across the layer's larger side, before thinning
ARROW_FULL_PERCENTILE = 95        # arrows at or above this percentile of |J| get full length
HOVER_MS = 40                     # at most one hover probe per 40 ms while the pointer moves
HOVER_SETTING = "view/hover_label"    # the values the hover readout shows (QSettings)
OLD_HOVER_SETTING = "view/hover_shows"  # the same up to 0.3.1, before the map's value led
# The hover label: an opaque box, so it reads the same on every colour of every map, dark or
# light as the app is: (background, 1 px outline, the large number, the muted small text).
HOVER_COLORS = {"dark": ("#0e1013", "#3d434c", "#f1f3f4", "#9aa0a6"),
                "light": ("#ffffff", "#9aa0a6", "#111111", "#5f6368")}
HOVER_LEAD_PX, HOVER_SMALL_PX = 17, 12   # the map's value (semibold); the rest, muted


def hover_style(scheme: str) -> str:
    """The hover label's style sheet for the app's ``scheme`` ("dark" or "light")."""
    background, outline, text, muted = HOVER_COLORS[scheme]
    return (f"QFrame#hover {{ background: {background}; border: 1px solid {outline}; border-radius: 6px; }}"
            f" QFrame#hover QLabel {{ background: transparent; border: none; color: {muted}; }}"
            f" QFrame#hover QLabel#lead {{ color: {text}; }}")


def color_scheme() -> str:
    """The app's look, "dark" or "light": its theme (:mod:`coppernicus.app.theme`), not the
    platform's colour scheme."""
    return theme.current()


def _xyz(points: np.ndarray, z: float = 0.0) -> np.ndarray:
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    return np.column_stack([pts[:, 0], -pts[:, 1], np.full(len(pts), z)])


def _polyline_mesh(rings: Sequence[np.ndarray], z: float, closed: bool = True) -> Optional[pv.PolyData]:
    rings = [np.asarray(r, float) for r in rings if len(r) >= 2]
    if not rings:
        return None
    points, lines, offset = [], [], 0
    for r in rings:
        line = np.vstack([r, r[:1]]) if closed else r
        points.append(_xyz(line, z))
        lines.append(np.concatenate([[len(line)], np.arange(offset, offset + len(line))]))
        offset += len(line)
    return pv.PolyData(np.vstack(points), lines=np.concatenate(lines))


def arrow_glyphs(points: np.ndarray, vectors: np.ndarray, length_mm: float,
                 j_ref: Optional[float] = None) -> np.ndarray:
    """2D arrows as line segments (3 strokes each: shaft and two head strokes), shape
    (K·3, 2, 2), in the same frame as ``points`` and ``vectors``. Each arrow is centred on its
    point and ``length_mm`` · √(|J| / j_ref) long, capped at ``length_mm`` (KiPI's √|J|
    convention); ``j_ref`` defaults to the largest |J|. Zero vectors are dropped."""
    points = np.asarray(points, float).reshape(-1, 2)
    vectors = np.asarray(vectors, float).reshape(-1, 2)
    mag = np.hypot(vectors[:, 0], vectors[:, 1])
    keep = mag > 0
    if not keep.any():
        return np.zeros((0, 2, 2))
    points, vectors, mag = points[keep], vectors[keep], mag[keep]
    ref = float(j_ref) if j_ref and j_ref > 0 else float(mag.max())
    length = length_mm * np.sqrt(np.minimum(mag / ref, 1.0))
    u = vectors / mag[:, None]
    tip = points + u * (length / 2)[:, None]
    tail = points - u * (length / 2)[:, None]
    head = 0.35 * length
    c, s_ = np.cos(np.radians(25.0)), np.sin(np.radians(25.0))
    back = -u
    left = np.column_stack([back[:, 0] * c - back[:, 1] * s_, back[:, 0] * s_ + back[:, 1] * c])
    right = np.column_stack([back[:, 0] * c + back[:, 1] * s_, -back[:, 0] * s_ + back[:, 1] * c])
    strokes = np.stack([np.stack([tail, tip], axis=1),
                        np.stack([tip, tip + left * head[:, None]], axis=1),
                        np.stack([tip, tip + right * head[:, None]], axis=1)], axis=1)
    return strokes.reshape(-1, 2, 2)


def _segments_mesh(segments: np.ndarray, z: float) -> Optional[pv.PolyData]:
    if len(segments) == 0:
        return None
    n = len(segments)
    lines = np.column_stack([np.full(n, 2), np.arange(0, 2 * n, 2), np.arange(1, 2 * n, 2)]).ravel()
    return pv.PolyData(_xyz(segments.reshape(-1, 2), z), lines=lines)


class ColorBar(QWidget):
    """A vertical colour scale drawn by Qt beside the render view, so it never covers the board.
    On the current map, above its title: the layer's peak |J| and its verdict
    (:meth:`set_check`)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(COLORBAR_WIDTH)
        self._cmap, self._lo, self._hi, self._title = None, 0.0, 1.0, ""
        self._check: Optional[DensityCheck] = None

    def set_scale(self, cmap: Optional[str], lo: float = 0.0, hi: float = 1.0, title: str = ""):
        """A new scale; it has no peak label until :meth:`set_check` gives one."""
        self._cmap, self._lo, self._hi, self._title = cmap, lo, hi, title
        self.set_check(None)
        self.setVisible(cmap is not None)
        self.update()

    def set_check(self, check: Optional[DensityCheck]):
        """The peak and verdict written above the scale (None: none). The tooltip has the
        whole label and why the peak is mesh-dependent."""
        self._check = check
        self.setToolTip("" if check is None else "\n".join(check.lines()) + "\n\n" + PEAK_TIP)
        self.update()

    def header(self) -> List[str]:
        """The label above the scale as text lines, empty when there is none."""
        return self._check.lines() if self._check is not None else []

    def _header_parts(self) -> List[Tuple[str, QFont, str]]:
        """(text, font, colour) of each line of the label, in order."""
        if self._check is None:
            return []
        bold, small = QFont(self.font()), QFont(self.font())
        bold.setWeight(QFont.DemiBold)
        small.setPointSizeF(max(self.font().pointSizeF() * 0.85, 6.0))
        parts = [(self._check.peak_text(), bold, theme.color("fg")),
                 (f"({PEAK_NOTE})", small, theme.color("muted"))]
        if self._check.limit_kind:
            colour = theme.color(VERDICT_TOKENS.get(self._check.verdict, "muted"))
            parts.append((self._check.verdict_text(), small, colour))
        # A value keeps its unit on its line when the narrow bar wraps the text.
        return [(text.replace(" A/mm²", " A⁠/⁠mm²").replace(" mm²", " mm²"), font, colour)
                for text, font, colour in parts]

    def paintEvent(self, _event):
        if self._cmap is None:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), theme.qcolor("panel"))
        flags = Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap
        wrap = Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap
        y = 4
        for text, font, colour in self._header_parts():    # the peak and verdict, on top
            p.setFont(font)
            p.setPen(QColor(colour))
            rect = p.fontMetrics().boundingRect(0, 0, self.width() - 6, 1000, wrap, text)
            p.drawText(4, y, self.width() - 6, rect.height(), wrap, text)
            y += rect.height() + 1
        p.setFont(self.font())
        head = y - 4 + (6 if y > 4 else 0)
        title_h = p.fontMetrics().boundingRect(0, 0, self.width() - 6, 1000, flags, self._title).height()
        top, bottom, x0, w = head + title_h + 18, self.height() - 16, 10, 18   # label and title set the top
        h = max(bottom - top, 10)
        rgba = (colormaps[self._cmap](np.linspace(1, 0, h))[:, :3] * 255).astype(np.uint8)
        img = np.ascontiguousarray(np.repeat(rgba[:, None, :], w, axis=1))
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888)
        p.drawPixmap(x0, top, QPixmap.fromImage(qimg))
        p.setPen(theme.qcolor("fg"))
        p.drawRect(x0, top, w, h)
        p.drawText(4, 4, self.width() - 6, top - 10, flags, self._title)
        for k in range(6):
            frac = k / 5
            y = int(bottom - frac * h)
            value = self._lo + frac * (self._hi - self._lo)
            p.drawLine(x0 + w, y, x0 + w + 4, y)
            p.drawText(x0 + w + 7, y + 5, f"{value:.3g}")
        p.end()


def drop_mv(result: RailResult, v: np.ndarray) -> np.ndarray:
    """The 'drop' map's values in mV: below the source voltage on a positive rail, toward
    0 V on a negative one, the rise above 0 V on a ground, the depth below 0 V on a ground
    under a negative rail (``RailResult.shown_drop_at``, as ``shown_drop_unit`` names it)."""
    return result.shown_drop_at(v) * 1e3


def current_scale(layer: LayerField) -> List[float]:
    """The 'current' map's colour limits: 0 to the layer's :data:`J_CLIP_PERCENTILE`th
    percentile of |J|, degenerate triangles (an edge under 1 nm) left out."""
    kept = layer.current_density[layer.resolved()]
    top = float(np.percentile(kept, J_CLIP_PERCENTILE)) if kept.size else 1.0
    return [0.0, max(top, 1e-12)]


class HoverLabel(QFrame):
    """The hover readout beside the pointer. Line 1: the value of the map shown, large and
    semibold (:attr:`HoverReadout.lead`), with a smaller muted figure beside it where one is
    natural (:attr:`HoverReadout.secondary`); under it, smaller and muted, its caption and the
    chosen extras (:meth:`HoverReadout.label_lines`). Dark or light as the app is.
    :meth:`text` is the readout as plain text, as :meth:`BoardView.hover_at` returns it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hover")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.lead, self.secondary, self.rest = QLabel(self), QLabel(self), QLabel(self)
        self.lead.setObjectName("lead")
        small = QFont(self.font())
        small.setPixelSize(HOVER_SMALL_PX)
        big = QFont(self.font())
        big.setPixelSize(HOVER_LEAD_PX)
        big.setWeight(QFont.DemiBold)
        for label, font in ((self.lead, big), (self.secondary, small), (self.rest, small)):
            label.setTextFormat(Qt.PlainText)        # a net name is never read as markup
            label.setFont(font)
        # Bottom-aligned on line 1, the small figure raised to sit on the number's baseline.
        self.secondary.setContentsMargins(
            0, 0, 0, max(self.lead.fontMetrics().descent() - self.secondary.fontMetrics().descent(), 0))
        line1 = QHBoxLayout()
        line1.setSpacing(6)
        line1.addWidget(self.lead, 0, Qt.AlignBottom)
        line1.addWidget(self.secondary, 0, Qt.AlignBottom)
        line1.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 5)
        layout.setSpacing(1)
        layout.addLayout(line1)
        layout.addWidget(self.rest)
        self._readout = HoverReadout()
        self.restyle()
        theme.signals.changed.connect(self.restyle)

    def restyle(self, *_):
        """Follow the app's colour scheme (:func:`color_scheme`)."""
        self.setStyleSheet(hover_style(color_scheme()))

    def set_readout(self, readout: HoverReadout):
        """Show ``readout``; the label's size follows it."""
        if readout == self._readout:
            return
        self._readout = readout
        rest = "\n".join(readout.label_lines())
        self.lead.setText(readout.lead)
        self.lead.setVisible(bool(readout.lead))
        self.secondary.setText(readout.secondary)
        self.secondary.setVisible(bool(readout.lead and readout.secondary))
        self.rest.setText(rest)
        self.rest.setVisible(bool(rest))
        self.setAccessibleName(readout.text())
        self.layout().activate()
        self.adjustSize()

    def text(self) -> str:
        return self._readout.text()


class BoardView(QWidget):
    probed = Signal(str)            # a readout of the values under a double-click
    pad_picked = Signal(float, float)   # pick mode: board point (mm, KiCad frame) under a click

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plotter = QtInteractor(self)
        self.colorbar = ColorBar(self)
        self.colorbar.set_scale(None)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.plotter.interactor, 1)
        layout.addWidget(self.colorbar)
        self.plotter.set_background(theme.color("map"))
        theme.signals.changed.connect(self._theme_changed)
        self.plotter.enable_parallel_projection()
        self.plotter.enable_2d_style()
        # viewport=True: pyvista hands over the click's display position and runs no picker
        # (its picker snaps to the nearest vertex of any actor, tens of pixels away).
        self.plotter.track_click_position(self._double_clicked, side="left", double=True, viewport=True)
        self._click_mode: Optional[str] = None
        self._press_at: Optional[Tuple[int, int]] = None
        self._press_is_double = False
        self.plotter.iren.add_observer("LeftButtonPressEvent", self._on_press)
        self.plotter.iren.add_observer("LeftButtonReleaseEvent", self._on_release)
        self._layer: Optional[LayerField] = None
        self._result: Optional[RailResult] = None
        self._finder = None
        self._has_camera = False
        self._measure: List[Tuple[str, float, float, float]] = []    # (layer, x, y, V): A, then B
        self._difference_mv: Optional[np.ndarray] = None             # per node, on the Δ map
        self._measure_result: Optional[RailResult] = None
        self._mark = None              # (problem, result): the ring of the selected problem (mark_problem)
        self._temperature = None            # (TemperatureMap, cell mm) while the temperature map is shown
        self._field: Optional[str] = None   # the map shown: a key of FIELDS, None before a solve
        # A power tree shown from another 0 V (S2): subtracted from every voltage shown (the
        # voltage map, readouts, ΔV's A and B), never from a drop; and what that 0 V is.
        self._v_offset, self._v_offset_label = 0.0, ""
        # (result, its problem list), as the Problems panel shows them: the current map's label
        # reads its verdict there (set_problems).
        self._problems: Optional[tuple] = None    # (result, problems, limits)
        # Hover readout: a move stores the pointer and starts the timer if it is idle, so a
        # moving pointer is probed every HOVER_MS, at its latest position.
        self.hover_label = HoverLabel(self.plotter.interactor)
        self.hover_label.hide()
        self.hover_actions = {}
        self._hover_chosen: Tuple[str, ...] = HOVER_DEFAULT
        self._hover_at: Optional[QPoint] = None                       # widget pixels, y down
        self._hover_layer: Optional[str] = None                       # the layer it last read
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_MS)
        self._hover_timer.timeout.connect(self._hover_tick)
        self.plotter.interactor.installEventFilter(self)

    # ------------------------------------------------------------------ scene
    def reset(self):
        """A new board: an empty scene, no measurement and no reference to an old result."""
        self.clear()
        self._measure, self._measure_result = [], None
        self._problems = None
        self._mark = None

    def clear(self):
        # The label goes, the pointer stays: _finish reads the new scene under a still pointer.
        self._hover_timer.stop()
        self.hover_label.hide()
        self.plotter.clear()
        self.colorbar.set_scale(None)
        self._layer, self._result, self._finder = None, None, None
        self._temperature, self._field = None, None

    def show_board(self, outline: Sequence[np.ndarray], copper_rings: Sequence[np.ndarray] = (),
                   sources: Sequence[Tuple[str, Sequence]] = (), loads: Sequence[Tuple[str, Sequence]] = (),
                   fit: bool = False, pad_outlines: Sequence[Tuple[str, Sequence[np.ndarray]]] = ()):
        """Outline, the net's copper outline on the shown layer, and terminal markers.
        ``pad_outlines``: (role, rings) with role 'source', 'load' or 'candidate'."""
        self.clear()
        self._add_outline(outline)
        copper = _polyline_mesh(copper_rings, 0.0)
        if copper is not None:
            self.plotter.add_mesh(copper, color=COPPER_COLOR, line_width=1.2, name="copper")
        self._add_pad_outlines(pad_outlines)
        self._add_terminals(sources, loads)
        self._finish(fit)

    def show_field(self, result: RailResult, layer: LayerField, field: str,
                   outline: Sequence[np.ndarray], show_vias: bool = True, fit: bool = False,
                   pad_outlines: Sequence[Tuple[str, Sequence[np.ndarray]]] = (),
                   isolines: bool = False, arrows: bool = False,
                   difference_mv: Optional[np.ndarray] = None, difference_note: str = "",
                   v_offset: float = 0.0, v_offset_label: str = ""):
        """A colour map of ``field`` ('drop', 'voltage', 'current' or 'difference') on
        ``layer``, with drop isolines (the same levels on every layer) and current-flow arrows
        if asked. For 'difference', ``difference_mv`` holds the change of drop per node of
        ``layer`` (current − baseline, mV) and ``difference_note`` how it was obtained.
        ``v_offset``: a power tree shown from 0 V at ``v_offset_label`` (S2), subtracted from
        the voltages shown (the voltage map, readouts), never from a drop."""
        self.clear()
        if result is not self._measure_result:          # a measurement belongs to one result
            self._measure, self._measure_result = [], result
        self._result, self._layer, self._field = result, layer, field
        self._v_offset, self._v_offset_label = float(v_offset or 0.0), str(v_offset_label or "")
        self._difference_mv = np.asarray(difference_mv, float) if field == "difference" else None
        title, cmap = FIELDS[field]
        faces = np.column_stack([np.full(len(layer.tris), 3), layer.tris]).ravel()
        mesh = pv.PolyData(_xyz(layer.points), faces=faces)
        if field == "current":
            values = layer.current_density
            mesh.cell_data[title] = values
            clim = current_scale(layer)
        elif field == "difference":
            values = np.asarray(difference_mv, dtype=float)
            mesh.point_data[title] = values
            finite = np.abs(values[np.isfinite(values)])
            top = float(finite.max()) if finite.size else 0.0
            clim = [-max(top, 1e-9), max(top, 1e-9)]             # symmetric: white = no change
        else:
            v = layer.voltage
            values = drop_mv(result, v) if field == "drop" else v - self._v_offset
            mesh.point_data[title] = values
            # Same scale on every layer: the rail's full range.
            all_v = np.concatenate([lf.voltage for lf in result.layers])
            all_v = all_v[np.isfinite(all_v)]
            lo, hi = (all_v.min(), all_v.max()) if all_v.size else (0.0, 1.0)
            if field == "drop":            # a boost (remote sense) shows below zero
                d = drop_mv(result, np.array([lo, hi]))
                clim = [min(0.0, float(d.min())), max(float(d.max()), 1e-9)]
            else:                          # from the reference's 0 V, when one is given
                clim = [lo - self._v_offset, hi - self._v_offset]
        self.plotter.add_mesh(mesh, scalars=title, cmap=cmap, clim=clim, nan_color="#dddddd",
                              show_edges=False, name="field", show_scalar_bar=False)
        units = {"drop": result.shown_drop_unit(), "voltage": "V", "current": "A/mm²",
                 "difference": "mV of drop, current − baseline (red: more)"}[field]
        if field == "voltage" and self._v_offset_label:
            units += f" from 0 V at {self._v_offset_label}"
        note = f"\ntop clipped at {J_CLIP_PERCENTILE:g}th pct" if field == "current" else ""
        if field == "difference" and difference_note:
            note += "\n" + difference_note
        if isolines:
            step = self._add_isolines(result, layer, ISOLINE_COLOR[field])
            what = "isolines: current drop" if field == "difference" else "isolines"
            note += f"\n{what} every {step:.3g} mV" if step else ""
        if arrows:
            self._add_arrows(layer)
        self.colorbar.set_scale(cmap, clim[0], clim[1], units + note)
        self._set_density_check()
        self._add_outline(outline)
        if show_vias and result.vias:
            self._add_vias(result)
        self._add_pad_outlines(pad_outlines)
        self._add_terminals([(t.name, t.positions) for t in result.sources],
                            [(t.name, t.positions) for t in result.loads])
        self._finder = TriangleLocator(layer.points, layer.tris)
        self._add_measure()
        self._add_mark()
        self._finish(fit)

    def mark_problem(self, problem) -> None:
        """Ring ``problem`` (a Problem of the shown result; None: no ring) on the map: shown on
        its layer (every layer when it names none) while the map shows the same result."""
        self._mark = (problem, self._result) if problem is not None else None
        self._add_mark()
        self.plotter.render()

    def marked(self):
        """The problem ringed on the map now, or None."""
        names = self.plotter.renderer.actors
        return self._mark[0] if self._mark is not None and "mark" in names else None

    def _add_mark(self):
        for name in ("mark", "mark_halo"):
            self.plotter.remove_actor(name, render=False)
        if self._mark is None or self._layer is None:
            return
        problem, result = self._mark
        if result is not self._result:                  # a ring belongs to one result
            self._mark = None
            return
        if problem.layer and problem.layer != self._layer.name:
            return
        pts = np.asarray(self._layer.points, float)
        span = float(np.ptp(pts, axis=0).max()) if len(pts) else 1.0
        r = max(span * MARK_SHARE, 0.3)
        t = np.linspace(0.0, 2 * np.pi, 73)
        ring = np.column_stack([problem.x + r * np.cos(t), problem.y + r * np.sin(t)])
        mesh = _polyline_mesh([ring], 0.05, closed=False)
        self.plotter.add_mesh(mesh, color=MARK_HALO, line_width=4.5, name="mark_halo", reset_camera=False)
        self.plotter.add_mesh(mesh.copy(), color=MARK_STROKE, line_width=2.0, name="mark", reset_camera=False)

    def set_problems(self, result: RailResult, problems: list, limits=None):
        """The problem list of ``result`` (a rail's or a tree group's, as judged against its
        limits): the current map of ``result`` writes the layer's verdict above its scale
        from it. Shown at once when that map is on screen (the limits were edited).
        ``limits``: a tree group's, so one that did not converge says it was not judged."""
        self._problems = (result, problems, limits)
        self._set_density_check()

    def _set_density_check(self):
        """On the current map: the layer's peak |J| and, from the problem list of the result
        shown, its verdict, above the colour bar (:func:`coppernicus.problems.density_check`)."""
        if self._field != "current" or self._layer is None or self._result is None:
            return
        held = self._problems
        problems, limits = held[1:] if held is not None and held[0] is self._result else (None, None)
        self.colorbar.set_check(density_check(self._result, self._layer, problems, limits))

    def show_temperature(self, temp, layer_name: str, outline: Sequence[np.ndarray], fit: bool = False):
        """The board temperature on copper layer ``layer_name`` of ``temp`` (a
        :class:`~coppernicus.results.BoardTemperature`): one square per grid cell over the
        whole board, grey outside it, every layer on one scale from the ambient to the board's
        peak, the layer's hottest cell marked. The double-click and the hover read the cell
        under the pointer. No isolines, arrows or measuring: they need a rail's copper."""
        self.clear()
        m = temp.map(layer_name)
        d = m.cell_mm or float(temp.grid_mm) or 1.0
        title, cmap = FIELDS["temperature"]
        ny, nx = m.t_c.shape
        # The view plots (x, −y): its first image row is the cell row with the largest y.
        rows = m.t_c[::-1] if m.y_mm[-1] >= m.y_mm[0] else m.t_c
        top = float(max(m.y_mm[0], m.y_mm[-1]))
        grid = pv.ImageData(dimensions=(nx + 1, ny + 1, 1), spacing=(d, d, 1.0),
                            origin=(float(m.x_mm[0]) - d / 2, -(top + d / 2), 0.0))
        grid.cell_data[title] = np.ascontiguousarray(rows, dtype=float).ravel()
        lo = float(temp.ambient_c)
        hi = max(float(temp.peak_c), lo + 1e-9)
        self.plotter.add_mesh(grid, scalars=title, cmap=cmap, clim=[lo, hi], nan_color=OUTSIDE_COLOR,
                              show_edges=False, name="temperature", show_scalar_bar=False)
        x, y, t = m.hottest()
        self.plotter.add_point_labels(_xyz(np.array([[x, y]]), 0.03), [f"hottest {t:.2f} °C"],
                                      point_color=HOTTEST_COLOR, point_size=12, font_size=11,
                                      text_color=HOTTEST_COLOR, shape_opacity=0.0,
                                      render_points_as_spheres=True, always_visible=True, name="hottest")
        self.colorbar.set_scale(cmap, lo, hi, f"°C, all layers: ambient → board peak\n{d:.3g} mm cells")
        self._add_outline(outline)
        self._temperature, self._field = (m, d), "temperature"
        self._finish(fit)

    def temperature_at(self, x: float, y: float) -> str:
        """The double-click's reading on the temperature map at board point (x, y) mm."""
        m, d = self._temperature
        reading = temperature_reading(m.at(x, y), d)
        return f"({x:.3f}, {y:.3f}) mm on {m.layer_name}: {reading or 'outside the board'}"

    def _add_isolines(self, result: RailResult, layer: LayerField, color: str) -> Optional[float]:
        """Drop isolines on ``layer`` at levels spread over the rail's whole drop range.
        Returns the step between levels in mV."""
        all_v = np.concatenate([lf.voltage for lf in result.layers])
        all_v = all_v[np.isfinite(all_v)]
        if not all_v.size:
            return None
        levels = drop_levels(float(drop_mv(result, all_v).max()), ISOLINE_COUNT)
        lines = isoline_paths(layer, drop_mv(result, layer.voltage), levels)
        mesh = _polyline_mesh(lines, 0.012, closed=False)
        if mesh is not None:
            self.plotter.add_mesh(mesh, color=color, line_width=1.2, name="isolines")
        return levels[0] if levels else None

    def _add_arrows(self, layer: LayerField):
        """Current-flow arrows, at most :data:`MAX_ARROWS`, length ∝ √|J|: white over a
        dark halo, so they read on every colour of the map."""
        if not len(layer.points):
            return
        spacing = float(np.ptp(layer.points, axis=0).max()) / ARROW_CELLS
        pts, vec = sample_flow(layer, spacing, MAX_ARROWS)
        # Full length at the 95th percentile of the arrows drawn, not of every triangle: on a
        # real plane the top triangles sit at via rings and would shrink all arrows to dots.
        mag = np.hypot(vec[:, 0], vec[:, 1])
        mag = mag[mag > 0]
        j_ref = float(np.percentile(mag, ARROW_FULL_PERCENTILE)) if mag.size else None
        mesh = _segments_mesh(arrow_glyphs(pts, vec, 0.9 * spacing, j_ref), 0.015)
        if mesh is not None:
            self.plotter.add_mesh(mesh, color=ARROW_HALO, line_width=3.4, name="arrows_halo")
            self.plotter.add_mesh(mesh.copy(), color=ARROW_COLOR, line_width=1.5, name="arrows")

    def _theme_changed(self, *_):
        """The map's background, the board outline and the colour bar in the new theme; the
        maps' own colours (colour scales, pads, isolines, arrows) stay as they are."""
        self.plotter.set_background(theme.color("map"))
        outline = self.plotter.actors.get("outline")
        if outline is not None:
            outline.prop.color = theme.color("outline")
        self.colorbar.update()
        self.plotter.render()

    def _add_outline(self, outline):
        mesh = _polyline_mesh(outline, 0.01)
        if mesh is not None:
            self.plotter.add_mesh(mesh, color=theme.color("outline"), line_width=1.5, name="outline")

    def _add_pad_outlines(self, pad_outlines):
        for role, rings in pad_outlines:
            mesh = _polyline_mesh(rings, 0.025)
            if mesh is not None:
                color, width = PAD_STYLE[role]
                self.plotter.add_mesh(mesh, color=color, line_width=width, name=f"pads_{role}")

    def _add_vias(self, result: RailResult):
        vias = [v for v in result.vias if v.current > 0]
        if not vias:
            return
        pts = pv.PolyData(_xyz(np.array([v.position for v in vias]), 0.02))
        pts["Via current (A)"] = np.array([v.current for v in vias])
        self.plotter.add_mesh(pts, scalars="Via current (A)", cmap="cool", point_size=7,
                              render_points_as_spheres=True, name="vias", show_scalar_bar=False)

    def _add_terminals(self, sources, loads):
        for kind, items, color in (("sources", sources, SOURCE_COLOR), ("loads", loads, LOAD_COLOR)):
            pts, labels = [], []
            for name, positions in items:
                if len(positions):
                    pts.append(np.asarray(positions[0], float))
                    labels.append(name)
            if pts:
                self.plotter.add_point_labels(_xyz(np.array(pts), 0.03), labels, point_color=color,
                                              point_size=12, font_size=11, text_color=color,
                                              shape_opacity=0.0, render_points_as_spheres=True,
                                              always_visible=True, name=kind)

    def _finish(self, fit: bool):
        if fit or not self._has_camera:
            self.plotter.view_xy()
            self.plotter.reset_camera()
            self._has_camera = True
        self.plotter.render()
        self._reread_hover()

    # ------------------------------------------------------------------ navigation
    def zoom_to(self, x: float, y: float, half_width_mm: float = 3.0):
        cam = self.plotter.camera
        fx, fy, fz = cam.focal_point
        px, py, pz = cam.position
        cam.focal_point = (x, -y, fz)
        cam.position = (x, -y, pz)
        cam.parallel_scale = half_width_mm
        self.plotter.render()
        self._rearm_hover()

    def reset_view(self):
        self.plotter.view_xy()
        self.plotter.reset_camera()
        self.plotter.render()
        self._rearm_hover()

    def screenshot(self, path) -> None:
        self.plotter.screenshot(str(path))

    # ------------------------------------------------------------------ single click
    def set_click_mode(self, mode: Optional[str]):
        """``'pick'``: a single click emits :attr:`pad_picked`; ``'measure'``: two clicks
        measure ΔV (see :meth:`measure_at`); ``None``: clicks do nothing (the double-click
        probe works in every mode)."""
        if mode != self._click_mode and self._measure:
            self._measure = []
            self._remove_measure()
        self._click_mode = mode
        self.plotter.interactor.setCursor(Qt.CrossCursor if mode else Qt.ArrowCursor)

    @property
    def click_mode(self) -> Optional[str]:
        return self._click_mode

    def _event_xy(self) -> Tuple[int, int]:
        x, y = self.plotter.iren.interactor.GetEventPosition()
        return int(x), int(y)

    def _on_press(self, *_):
        self._press_at = self._event_xy()
        # Qt delivers the second press of a double-click with VTK's repeat count set: that
        # click belongs to the double-click probe, not to picking or measuring.
        self._press_is_double = self.plotter.iren.interactor.GetRepeatCount() > 0

    def _on_release(self, *_):
        start, self._press_at = self._press_at, None
        if self._click_mode is None or start is None or self._press_is_double:
            return
        x, y = self._event_xy()
        if abs(x - start[0]) > CLICK_SLOP_PX or abs(y - start[1]) > CLICK_SLOP_PX:
            return                                   # a pan, not a click
        bx, by = self.display_to_board(x, y)
        if self._click_mode == "pick":
            self.pad_picked.emit(bx, by)
        elif self._click_mode == "measure":
            self.measure_at(bx, by)

    def display_to_board(self, x: float, y: float) -> Tuple[float, float]:
        """Board point (mm, KiCad frame, y down) under display pixel (x, y). The view looks
        straight down with a parallel projection, so depth does not matter."""
        ren = self.plotter.renderer
        ren.SetDisplayPoint(float(x), float(y), 0.0)
        ren.DisplayToWorld()
        wx, wy, _wz, w = ren.GetWorldPoint()
        w = w or 1.0
        return wx / w, -wy / w

    # ------------------------------------------------------------------ measure ΔV
    def measure_at(self, x: float, y: float) -> str:
        """One click of a ΔV measurement at board point (x, y) mm on the shown layer: the
        first sets A, the second B and reports ΔV A→B = V(A) − V(B); a third starts again.
        The message is also emitted as :attr:`probed`."""
        if self._layer is None or self._finder is None:
            msg = "Solve the rail to measure ΔV"
        else:
            p = probe(self._layer, self._finder, x, y)
            if p is None or not np.isfinite(p.voltage_v):
                why = "" if p is None else " (copper not connected to a source)"
                msg = f"({x:.3f}, {y:.3f}) mm on {self._layer.name}: no copper of this net here{why}"
            else:
                if len(self._measure) >= 2:
                    self._measure = []
                self._measure.append((self._layer.name, p.x_mm, p.y_mm, p.voltage_v))
                off = self._v_offset                  # A and B as shown; ΔV does not move
                if len(self._measure) == 1:
                    msg = (f"A: {p.voltage_v - off:.5f} V at ({x:.3f}, {y:.3f}) mm on {self._layer.name}"
                           " — click B")
                else:
                    (_, _, _, va), (_, _, _, vb) = self._measure
                    msg = f"ΔV A→B = {(va - vb) * 1e3:.3f} mV (A: {va - off:.5f} V, B: {vb - off:.5f} V)"
                self._add_measure()
                self.plotter.render()
        self.probed.emit(msg)
        return msg

    def _remove_measure(self):
        for name in ("measure", "measure_line"):
            self.plotter.remove_actor(name, render=False)

    def _add_measure(self):
        self._remove_measure()
        if not self._measure:
            return
        pts = np.array([(x, y) for _, x, y, _ in self._measure])
        labels = [f"{'AB'[i]} ({layer})" if layer != getattr(self._layer, "name", layer) else "AB"[i]
                  for i, (layer, *_rest) in enumerate(self._measure)]
        self.plotter.add_point_labels(_xyz(pts, 0.04), labels, point_color=MEASURE_COLOR, point_size=15,
                                      font_size=12, text_color=MEASURE_COLOR, shape_opacity=0.0,
                                      render_points_as_spheres=True, always_visible=True, name="measure")
        if len(pts) == 2:
            line = _polyline_mesh([pts], 0.04, closed=False)
            self.plotter.add_mesh(line, color=MEASURE_COLOR, line_width=2.0, name="measure_line")

    # ------------------------------------------------------------------ probe
    def _double_clicked(self, *_):
        """A double-click: the readout of the board point exactly under the pointer, read as
        the hover, pick and Measure read it (:meth:`display_to_board` of the event's display
        position)."""
        self.probe_at(*self.display_to_board(*self._event_xy()))

    def _on_double_click(self, point):
        """The double-click's readout at ``point`` in the view's frame (x, −y), for scripts
        (the docs screenshots, tools/gui_check.py)."""
        self.probe_at(float(point[0]), -float(point[1]))

    def probe_at(self, x: float, y: float) -> str:
        """The double-click's readout at board point (x, y) mm on the shown layer: V, the
        drop and |J| (and the change of drop on the Δ map), emitted as :attr:`probed`; on the
        temperature map, the temperature of the cell holding the point. Empty, and not
        emitted, before a solve."""
        if self._temperature is not None:
            msg = self.temperature_at(x, y)
            self.probed.emit(msg)
            return msg
        if self._layer is None or self._finder is None or self._result is None:
            return ""
        p = probe(self._layer, self._finder, x, y)
        if p is None:
            msg = f"({x:.3f}, {y:.3f}) mm: no copper of this net on {self._layer.name}"
        elif not np.isfinite(p.voltage_v):
            msg = f"({x:.3f}, {y:.3f}) mm on {self._layer.name}: copper not connected to a source"
        else:
            delta = ""
            if self._difference_mv is not None:          # the Δ map is shown: read Δ too
                d = self._difference_at(p.triangle, x, y)
                delta = (f", Δ drop vs baseline = {d:+.3f} mV" if np.isfinite(d)
                         else ", Δ drop vs baseline: no baseline copper here")
            frame = f" from 0 V at {self._v_offset_label}" if self._v_offset_label else ""
            msg = (f"({x:.3f}, {y:.3f}) mm on {self._layer.name}: "
                   f"V = {p.voltage_v - self._v_offset:.5f} V{frame}, "
                   f"{self._result.drop_word()} = {float(drop_mv(self._result, p.voltage_v)):.3f} mV, "
                   f"|J| = {p.current_density_a_mm2:.2f} A/mm²{delta}")
        self.probed.emit(msg)
        return msg

    def _difference_at(self, triangle: int, x: float, y: float) -> float:
        """The Δ map's change of drop (mV) at board point (x, y) in ``triangle`` of the shown
        layer, interpolated as the voltage is; NaN where the baseline had no copper."""
        nodes = self._layer.tris[triangle]
        return float(self._finder.weights(triangle, x, y) @ self._difference_mv[nodes])

    # ------------------------------------------------------------------ hover
    def hover_readout_at(self, x: float, y: float) -> HoverReadout:
        """The hover readout at board point (x, y) mm on the shown layer: the double-click's
        probe on the result it reads (a power-tree group when one is shown), led by the value
        of the map shown. Empty off the copper, before a solve and with no value chosen. On
        the temperature map: the cell's temperature, as the double-click reads it."""
        if self._temperature is not None:
            m, d = self._temperature
            return temperature_readout(m.at(x, y), d, m.layer_name, self._hover_chosen)
        if self._layer is None or self._finder is None or self._result is None or not self._hover_chosen:
            return HoverReadout()
        p = probe(self._layer, self._finder, x, y)
        if p is None:
            return HoverReadout()
        delta = (self._difference_at(p.triangle, x, y)
                 if self._difference_mv is not None and np.isfinite(p.voltage_v) else float("nan"))
        return hover_readout(hover_values(self._result, self._layer.name, p, delta, self._v_offset),
                             self._hover_chosen, self._field)

    def hover_at(self, x: float, y: float) -> str:
        """:meth:`hover_readout_at` as plain text, the map's value first in the double-click
        readout's words ("drop = 7.263 mV below 3.3 V"): what the label's ``text()`` holds."""
        return self.hover_readout_at(x, y).text()

    def set_hover_values(self, chosen: Sequence[str]):
        """The values the readout shows (keys of :data:`HOVER_VALUES`); none turns it off."""
        self._hover_chosen = tuple(chosen)
        if not self._hover_chosen:
            self._hide_hover()

    def add_hover_menu(self, menu: QMenu) -> QMenu:
        """Add the "Hover shows" submenu to ``menu``: one checkable action per value, none
        checked turns the readout off. The choice is kept in the user's settings."""
        settings = _settings()
        self.set_hover_values(chosen_from_setting(settings.value(HOVER_SETTING),
                                                  settings.value(OLD_HOVER_SETTING)))
        sub = menu.addMenu("Hover shows")
        for key, label in HOVER_VALUES:
            act = sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(key in self._hover_chosen)
            act.toggled.connect(self._hover_menu_toggled)
            self.hover_actions[key] = act
            if key == "shown":                 # the map's value, then the extras under it
                sub.addSeparator()
        return sub

    def _hover_menu_toggled(self, *_):
        chosen = [key for key, act in self.hover_actions.items() if act.isChecked()]
        self.set_hover_values(chosen)
        _settings().setValue(HOVER_SETTING, setting_from_chosen(chosen))

    def eventFilter(self, watched, event):
        if watched is self.plotter.interactor:
            kind = event.type()
            if kind == QEvent.MouseMove:
                if event.buttons() != Qt.NoButton:          # panning or dragging
                    self._hide_hover()
                elif self._hover_chosen:
                    self._hover_at = event.position().toPoint()
                    if not self._hover_timer.isActive():
                        self._hover_timer.start()
            elif kind in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
                self._hide_hover()
            elif kind == QEvent.Wheel:
                self._rearm_hover()                         # a zoom moves the board under it
        return super().eventFilter(watched, event)

    def _rearm_hover(self):
        """After the board moved under a still pointer (a wheel zoom, or a camera move from
        code: a Problems row, Fit), read the point now under it, so the readout is never stale."""
        if self._hover_at is not None and not self._hover_timer.isActive():
            self._hover_timer.start()

    def _shown_layer(self) -> Optional[str]:
        """The name of the layer the view shows a map of; None without one."""
        if self._temperature is not None:
            return self._temperature[0].layer_name
        return self._layer.name if self._layer is not None else None

    def _reread_hover(self):
        """After a redraw under a still pointer: on the layer the label was reading (another
        Show, an overlay toggled, a new solve) it reads the new map at once; on another layer,
        or with no map, it stays hidden until the pointer moves."""
        if self._hover_at is None:
            return
        if self._shown_layer() is not None and self._shown_layer() == self._hover_layer:
            self._hover_tick()
        else:
            self._hide_hover()

    def _hover_tick(self):
        if self._hover_at is None or self.plotter.iren is None:     # None: the view has closed
            return
        self._hover_layer = self._shown_layer()
        # The pointer's display point as VTK received it, as a click in pick mode reads it.
        readout = self.hover_readout_at(*self.display_to_board(*self._event_xy()))
        label = self.hover_label
        if not readout:
            label.hide()
            return
        label.set_readout(readout)
        widget = self.plotter.interactor
        corner = QPoint(*label_position((self._hover_at.x(), self._hover_at.y()),
                                        (label.width(), label.height()), (widget.width(), widget.height())))
        if label.pos() != corner:
            label.move(corner)
        if not label.isVisible():
            label.show()

    def _hide_hover(self):
        self._hover_timer.stop()
        self._hover_at = None
        self.hover_label.hide()

    def closeEvent(self, event):
        self.plotter.interactor.removeEventFilter(self)    # a late move starts no readout
        self._hide_hover()
        self.plotter.close()
        super().closeEvent(event)


def _settings() -> QSettings:
    """The user's settings for the app (the default format, so a test can redirect them)."""
    return app_settings()


def copper_rings(geom) -> List[np.ndarray]:
    """Exterior and hole rings of a shapely (Multi)Polygon, for :meth:`BoardView.show_board`."""
    if geom is None or geom.is_empty:
        return []
    polys = getattr(geom, "geoms", [geom])
    rings = []
    for g in polys:
        if g.geom_type != "Polygon":
            continue
        rings.append(np.asarray(g.exterior.coords)[:-1])
        rings.extend(np.asarray(r.coords)[:-1] for r in g.interiors)
    return rings
