"""The 2D board view: the only class in the app that knows the renderer (VTK via pyvistaqt).

Shows one copper layer at a time: the board outline, the net's copper outline, and after a
solve a colour map (voltage drop, voltage or current density) with the sources, loads and
vias on top. Left-drag pans, the wheel zooms, a double-click reads the values under the
pointer and, as the pointer moves, a readout beside it shows the values chosen in View →
Hover shows. In pick mode a single click (a press and release less than 3 px apart, so a pan
is never a click) reports the board point under it; in measure mode two clicks give the
voltage difference between two points. Drop isolines and current-flow arrows can be laid
over any map. KiCad's y axis points down; the view plots (x, −y) so the board reads as in
KiCad.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

os.environ.setdefault("QT_API", "pyside6")

import numpy as np  # noqa: E402
import pyvista as pv  # noqa: E402
from matplotlib import colormaps  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QWidget  # noqa: E402
from pyvistaqt import QtInteractor  # noqa: E402

from coppernicus.app import APP_NAME  # noqa: E402
from coppernicus.app.hover import (  # noqa: E402
    HOVER_DEFAULT,
    HOVER_VALUES,
    chosen_from_setting,
    hover_text,
    hover_values,
    label_position,
    setting_from_chosen,
)

# TriangleLocator is also re-exported here for code that imported it from this module.
from coppernicus.fields import TriangleLocator, drop_levels, probe, sample_flow  # noqa: E402
from coppernicus.fields import isolines as isoline_paths  # noqa: E402
from coppernicus.results import LayerField, RailResult  # noqa: E402

FIELDS = {
    "drop": ("Voltage drop (mV)", "viridis"),
    "voltage": ("Voltage (V)", "viridis"),
    "current": ("Current density (A/mm²)", "magma"),
    # Before/after: two hues and a light neutral midpoint (no change).
    "difference": ("Δ drop vs baseline (mV)", "RdBu_r"),
}
J_CLIP_PERCENTILE = 99.5
SOURCE_COLOR, LOAD_COLOR, OUTLINE_COLOR, COPPER_COLOR = "#1a9850", "#d73027", "#555555", "#b87333"
CANDIDATE_COLOR = "#7a4a1d"       # outlines of the pads that can be picked
PAD_STYLE = {"source": (SOURCE_COLOR, 3.0), "load": (LOAD_COLOR, 3.0), "candidate": (CANDIDATE_COLOR, 1.0)}
CLICK_SLOP_PX = 3                 # a press and release closer than this is a click, not a pan
# Isolines: dark on the drop/voltage map (viridis), light on the current map (magma, dark).
ISOLINE_COLOR = {"drop": "#111111", "voltage": "#111111", "current": "#e8e8e8", "difference": "#111111"}
ARROW_COLOR, ARROW_HALO, MEASURE_COLOR = "#ffffff", "#111111", "#0b62d6"
ISOLINE_COUNT = 10
MAX_ARROWS = 2000
ARROW_CELLS = 36                  # arrows across the layer's larger side, before thinning
ARROW_FULL_PERCENTILE = 95        # arrows at or above this percentile of |J| get full length
HOVER_MS = 40                     # at most one hover probe per 40 ms while the pointer moves
HOVER_SETTING = "view/hover_shows"    # the values the hover readout shows (QSettings)
HOVER_STYLE = ("QLabel { background: #fffff0; color: #111111; border: 1px solid #888888;"
               " border-radius: 3px; padding: 2px 5px; }")


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
    """A vertical colour scale drawn by Qt beside the render view, so it never covers the board."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(104)
        self._cmap, self._lo, self._hi, self._title = None, 0.0, 1.0, ""

    def set_scale(self, cmap: Optional[str], lo: float = 0.0, hi: float = 1.0, title: str = ""):
        self._cmap, self._lo, self._hi, self._title = cmap, lo, hi, title
        self.setVisible(cmap is not None)
        self.update()

    def paintEvent(self, _event):
        if self._cmap is None:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("white"))
        flags = Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap
        title_h = p.fontMetrics().boundingRect(0, 0, self.width() - 6, 1000, flags, self._title).height()
        top, bottom, x0, w = title_h + 18, self.height() - 16, 10, 18      # the title sets the top
        h = max(bottom - top, 10)
        rgba = (colormaps[self._cmap](np.linspace(1, 0, h))[:, :3] * 255).astype(np.uint8)
        img = np.ascontiguousarray(np.repeat(rgba[:, None, :], w, axis=1))
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888)
        p.drawPixmap(x0, top, QPixmap.fromImage(qimg))
        p.setPen(QColor("black"))
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
    under a negative rail (``RailResult.shown_drop_at``, as ``drop_unit`` names it)."""
    return result.shown_drop_at(v) * 1e3


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
        self.plotter.set_background("white")
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
        # Hover readout: a move stores the pointer and starts the timer if it is idle, so a
        # moving pointer is probed every HOVER_MS, at its latest position.
        self.hover_label = QLabel(self.plotter.interactor)
        self.hover_label.setStyleSheet(HOVER_STYLE)
        self.hover_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hover_label.hide()
        self.hover_actions = {}
        self._hover_chosen: Tuple[str, ...] = HOVER_DEFAULT
        self._hover_at: Optional[QPoint] = None                       # widget pixels, y down
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

    def clear(self):
        self._hide_hover()
        self.plotter.clear()
        self.colorbar.set_scale(None)
        self._layer, self._result, self._finder = None, None, None

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
                   difference_mv: Optional[np.ndarray] = None, difference_note: str = ""):
        """A colour map of ``field`` ('drop', 'voltage', 'current' or 'difference') on
        ``layer``, with drop isolines (the same levels on every layer) and current-flow arrows
        if asked. For 'difference', ``difference_mv`` holds the change of drop per node of
        ``layer`` (current − baseline, mV) and ``difference_note`` how it was obtained."""
        self.clear()
        if result is not self._measure_result:          # a measurement belongs to one result
            self._measure, self._measure_result = [], result
        self._result, self._layer = result, layer
        self._difference_mv = np.asarray(difference_mv, float) if field == "difference" else None
        title, cmap = FIELDS[field]
        faces = np.column_stack([np.full(len(layer.tris), 3), layer.tris]).ravel()
        mesh = pv.PolyData(_xyz(layer.points), faces=faces)
        if field == "current":
            values = layer.current_density
            mesh.cell_data[title] = values
            top = float(np.percentile(values, J_CLIP_PERCENTILE)) if values.size else 1.0
            clim = [0.0, max(top, 1e-12)]
        elif field == "difference":
            values = np.asarray(difference_mv, dtype=float)
            mesh.point_data[title] = values
            finite = np.abs(values[np.isfinite(values)])
            top = float(finite.max()) if finite.size else 0.0
            clim = [-max(top, 1e-9), max(top, 1e-9)]             # symmetric: white = no change
        else:
            v = layer.voltage
            values = drop_mv(result, v) if field == "drop" else v
            mesh.point_data[title] = values
            # Same scale on every layer: the rail's full range.
            all_v = np.concatenate([lf.voltage for lf in result.layers])
            all_v = all_v[np.isfinite(all_v)]
            lo, hi = (all_v.min(), all_v.max()) if all_v.size else (0.0, 1.0)
            if field == "drop":            # a boost (remote sense) shows below zero
                d = drop_mv(result, np.array([lo, hi]))
                clim = [min(0.0, float(d.min())), max(float(d.max()), 1e-9)]
            else:
                clim = [lo, hi]
        self.plotter.add_mesh(mesh, scalars=title, cmap=cmap, clim=clim, nan_color="#dddddd",
                              show_edges=False, name="field", show_scalar_bar=False)
        units = {"drop": result.drop_unit(), "voltage": "V", "current": "A/mm²",
                 "difference": "mV of drop, current − baseline (red: more)"}[field]
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
        self._add_outline(outline)
        if show_vias and result.vias:
            self._add_vias(result)
        self._add_pad_outlines(pad_outlines)
        self._add_terminals([(t.name, t.positions) for t in result.sources],
                            [(t.name, t.positions) for t in result.loads])
        self._finder = TriangleLocator(layer.points, layer.tris)
        self._add_measure()
        self._finish(fit)

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

    def _add_outline(self, outline):
        mesh = _polyline_mesh(outline, 0.01)
        if mesh is not None:
            self.plotter.add_mesh(mesh, color=OUTLINE_COLOR, line_width=1.5, name="outline")

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

    # ------------------------------------------------------------------ navigation
    def zoom_to(self, x: float, y: float, half_width_mm: float = 3.0):
        cam = self.plotter.camera
        fx, fy, fz = cam.focal_point
        px, py, pz = cam.position
        cam.focal_point = (x, -y, fz)
        cam.position = (x, -y, pz)
        cam.parallel_scale = half_width_mm
        self.plotter.render()

    def reset_view(self):
        self.plotter.view_xy()
        self.plotter.reset_camera()
        self.plotter.render()

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
                if len(self._measure) == 1:
                    msg = (f"A: {p.voltage_v:.5f} V at ({x:.3f}, {y:.3f}) mm on {self._layer.name}"
                           " — click B")
                else:
                    (_, _, _, va), (_, _, _, vb) = self._measure
                    msg = f"ΔV A→B = {(va - vb) * 1e3:.3f} mV (A: {va:.5f} V, B: {vb:.5f} V)"
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
        drop and |J| (and the change of drop on the Δ map), emitted as :attr:`probed`.
        Empty, and not emitted, before a solve."""
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
                nodes = self._layer.tris[p.triangle]
                d = float(self._finder.weights(p.triangle, x, y) @ self._difference_mv[nodes])
                delta = (f", Δ drop vs baseline = {d:+.3f} mV" if np.isfinite(d)
                         else ", Δ drop vs baseline: no baseline copper here")
            msg = (f"({x:.3f}, {y:.3f}) mm on {self._layer.name}: V = {p.voltage_v:.5f} V, "
                   f"{self._result.drop_word()} = {float(drop_mv(self._result, p.voltage_v)):.3f} mV, "
                   f"|J| = {p.current_density_a_mm2:.2f} A/mm²{delta}")
        self.probed.emit(msg)
        return msg

    # ------------------------------------------------------------------ hover
    def hover_at(self, x: float, y: float) -> str:
        """The hover readout at board point (x, y) mm on the shown layer: the double-click's
        probe on the result it reads (a power-tree group when one is shown). Empty off the
        copper, before a solve and with no value chosen."""
        if self._layer is None or self._finder is None or self._result is None or not self._hover_chosen:
            return ""
        p = probe(self._layer, self._finder, x, y)
        return hover_text(None if p is None else hover_values(self._result, self._layer.name, p),
                          self._hover_chosen)

    def set_hover_values(self, chosen: Sequence[str]):
        """The values the readout shows (keys of :data:`HOVER_VALUES`); none turns it off."""
        self._hover_chosen = tuple(chosen)
        if not self._hover_chosen:
            self._hide_hover()

    def add_hover_menu(self, menu: QMenu) -> QMenu:
        """Add the "Hover shows" submenu to ``menu``: one checkable action per value, none
        checked turns the readout off. The choice is kept in the user's settings."""
        settings = _settings()
        self.set_hover_values(chosen_from_setting(settings.value(HOVER_SETTING)))
        sub = menu.addMenu("Hover shows")
        for key, label in HOVER_VALUES:
            act = sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(key in self._hover_chosen)
            act.toggled.connect(self._hover_menu_toggled)
            self.hover_actions[key] = act
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
            elif kind == QEvent.Wheel and self._hover_at is not None and not self._hover_timer.isActive():
                self._hover_timer.start()                   # a zoom moves the board under it
        return super().eventFilter(watched, event)

    def _hover_tick(self):
        if self._hover_at is None:
            return
        # The pointer's display point as VTK received it, as a click in pick mode reads it.
        text = self.hover_at(*self.display_to_board(*self._event_xy()))
        label = self.hover_label
        if not text:
            label.hide()
            return
        if text != label.text():
            label.setText(text)
            label.adjustSize()
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
        self._hover_timer.stop()
        self.plotter.close()
        super().closeEvent(event)


def _settings() -> QSettings:
    """The user's settings for the app (the default format, so a test can redirect them)."""
    return QSettings(QSettings.defaultFormat(), QSettings.UserScope, APP_NAME, APP_NAME)


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
