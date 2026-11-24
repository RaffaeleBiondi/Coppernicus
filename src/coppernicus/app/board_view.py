"""The 2D board view: the only class in the app that knows the renderer (VTK via pyvistaqt).

Shows one copper layer at a time: the board outline, the net's copper outline, and after a
solve a colour map (voltage drop, voltage or current density) with the sources, loads and
vias on top. Left-drag pans, the wheel zooms, a double-click reads the values under the
pointer. In pick mode a single click (a press and release less than 3 px apart, so a pan
is never a click) reports the board point under it. KiCad's y axis points down; the view
plots (x, −y) so the board reads as in KiCad.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence, Tuple

os.environ.setdefault("QT_API", "pyside6")

import numpy as np  # noqa: E402
import pyvista as pv  # noqa: E402
from matplotlib import colormaps  # noqa: E402
from PySide6.QtCore import Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QHBoxLayout, QWidget  # noqa: E402
from pyvistaqt import QtInteractor  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402

from coppernicus.results import LayerField, RailResult  # noqa: E402

FIELDS = {
    "drop": ("Voltage drop (mV)", "viridis"),
    "voltage": ("Voltage (V)", "viridis"),
    "current": ("Current density (A/mm²)", "magma"),
}
J_CLIP_PERCENTILE = 99.5
SOURCE_COLOR, LOAD_COLOR, OUTLINE_COLOR, COPPER_COLOR = "#1a9850", "#d73027", "#555555", "#b87333"
CANDIDATE_COLOR = "#7a4a1d"       # outlines of the pads that can be picked
PAD_STYLE = {"source": (SOURCE_COLOR, 3.0), "load": (LOAD_COLOR, 3.0), "candidate": (CANDIDATE_COLOR, 1.0)}
CLICK_SLOP_PX = 3                 # a press and release closer than this is a click, not a pan


def _xyz(points: np.ndarray, z: float = 0.0) -> np.ndarray:
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    return np.column_stack([pts[:, 0], -pts[:, 1], np.full(len(pts), z)])


def _polyline_mesh(rings: Sequence[np.ndarray], z: float) -> Optional[pv.PolyData]:
    rings = [np.asarray(r, float) for r in rings if len(r) >= 2]
    if not rings:
        return None
    points, lines, offset = [], [], 0
    for r in rings:
        closed = np.vstack([r, r[:1]])
        points.append(_xyz(closed, z))
        lines.append(np.concatenate([[len(closed)], np.arange(offset, offset + len(closed))]))
        offset += len(closed)
    return pv.PolyData(np.vstack(points), lines=np.concatenate(lines))


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
        top, bottom, x0, w = 58, self.height() - 16, 10, 18
        h = max(bottom - top, 10)
        rgba = (colormaps[self._cmap](np.linspace(1, 0, h))[:, :3] * 255).astype(np.uint8)
        img = np.ascontiguousarray(np.repeat(rgba[:, None, :], w, axis=1))
        qimg = QImage(img.data, w, h, 3 * w, QImage.Format_RGB888)
        p.drawPixmap(x0, top, QPixmap.fromImage(qimg))
        p.setPen(QColor("black"))
        p.drawRect(x0, top, w, h)
        p.drawText(4, 4, self.width() - 6, top - 10, Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap,
                   self._title)
        for k in range(6):
            frac = k / 5
            y = int(bottom - frac * h)
            value = self._lo + frac * (self._hi - self._lo)
            p.drawLine(x0 + w, y, x0 + w + 4, y)
            p.drawText(x0 + w + 7, y + 5, f"{value:.3g}")
        p.end()


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
        self.plotter.track_click_position(self._on_double_click, side="left", double=True)
        self._click_mode: Optional[str] = None
        self._press_at: Optional[Tuple[int, int]] = None
        self.plotter.iren.add_observer("LeftButtonPressEvent", self._on_press)
        self.plotter.iren.add_observer("LeftButtonReleaseEvent", self._on_release)
        self._layer: Optional[LayerField] = None
        self._result: Optional[RailResult] = None
        self._finder = None
        self._has_camera = False

    # ------------------------------------------------------------------ scene
    def clear(self):
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
                   pad_outlines: Sequence[Tuple[str, Sequence[np.ndarray]]] = ()):
        """A colour map of ``field`` ('drop', 'voltage' or 'current') on ``layer``."""
        self.clear()
        self._result, self._layer = result, layer
        title, cmap = FIELDS[field]
        faces = np.column_stack([np.full(len(layer.tris), 3), layer.tris]).ravel()
        mesh = pv.PolyData(_xyz(layer.points), faces=faces)
        if field == "current":
            values = layer.current_density
            mesh.cell_data[title] = values
            top = float(np.percentile(values, J_CLIP_PERCENTILE)) if values.size else 1.0
            clim = [0.0, max(top, 1e-12)]
        else:
            v = layer.voltage
            values = (result.v_ref - v) * 1e3 if field == "drop" else v
            mesh.point_data[title] = values
            # Same scale on every layer: the rail's full range.
            all_v = np.concatenate([lf.voltage for lf in result.layers])
            all_v = all_v[np.isfinite(all_v)]
            lo, hi = (all_v.min(), all_v.max()) if all_v.size else (0.0, 1.0)
            clim = [0.0, max((result.v_ref - lo) * 1e3, 1e-9)] if field == "drop" else [lo, hi]
        self.plotter.add_mesh(mesh, scalars=title, cmap=cmap, clim=clim, nan_color="#dddddd",
                              show_edges=False, name="field", show_scalar_bar=False)
        units = {"drop": "mV below source", "voltage": "V", "current": "A/mm²"}[field]
        note = f"\ntop clipped at {J_CLIP_PERCENTILE:g}th pct" if field == "current" else ""
        self.colorbar.set_scale(cmap, clim[0], clim[1], units + note)
        self._add_outline(outline)
        if show_vias and result.vias:
            self._add_vias(result)
        self._add_pad_outlines(pad_outlines)
        self._add_terminals([(t.name, t.positions) for t in result.sources],
                            [(t.name, t.positions) for t in result.loads])
        self._finder = TriangleLocator(layer.points, layer.tris)
        self._finish(fit)

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
        """``'pick'``: a single click emits :attr:`pad_picked`; ``None``: clicks do nothing
        (the double-click probe works in every mode)."""
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

    def _on_release(self, *_):
        start, self._press_at = self._press_at, None
        if self._click_mode is None or start is None:
            return
        x, y = self._event_xy()
        if abs(x - start[0]) > CLICK_SLOP_PX or abs(y - start[1]) > CLICK_SLOP_PX:
            return                                   # a pan, not a click
        bx, by = self.display_to_board(x, y)
        if self._click_mode == "pick":
            self.pad_picked.emit(bx, by)

    def display_to_board(self, x: float, y: float) -> Tuple[float, float]:
        """Board point (mm, KiCad frame, y down) under display pixel (x, y). The view looks
        straight down with a parallel projection, so depth does not matter."""
        ren = self.plotter.renderer
        ren.SetDisplayPoint(float(x), float(y), 0.0)
        ren.DisplayToWorld()
        wx, wy, _wz, w = ren.GetWorldPoint()
        w = w or 1.0
        return wx / w, -wy / w

    # ------------------------------------------------------------------ probe
    def _on_double_click(self, point):
        if self._layer is None or self._finder is None or self._result is None:
            return
        x, y = float(point[0]), -float(point[1])
        k = self._finder.locate(x, y)
        if k < 0:
            self.probed.emit(f"({x:.3f}, {y:.3f}) mm: no copper of this net on {self._layer.name}")
            return
        nodes = self._layer.tris[k]
        w = self._finder.weights(k, x, y)          # barycentric interpolation at the click
        v = float(w @ self._layer.voltage[nodes])
        j = float(self._layer.current_density[k])
        self.probed.emit(f"({x:.3f}, {y:.3f}) mm on {self._layer.name}: V = {v:.5f} V, "
                         f"drop = {(self._result.v_ref - v) * 1e3:.3f} mV, |J| = {j:.2f} A/mm²")

    def closeEvent(self, event):
        self.plotter.close()
        super().closeEvent(event)


class TriangleLocator:
    """Which triangle contains a point. A KD-tree on centroids narrows the search to nearby
    triangles, then a barycentric test picks the one containing the point. Unlike
    matplotlib's trapezoid map it accepts meshes made of separate pieces with coincident
    points, as a layer of real copper is."""

    def __init__(self, points: np.ndarray, tris: np.ndarray, candidates: int = 24):
        self.points, self.tris, self.k = points, tris, min(candidates, len(tris))
        self.tree = cKDTree(points[tris].mean(axis=1)) if len(tris) else None

    def weights(self, k: int, x: float, y: float) -> np.ndarray:
        p = self.points[self.tris[k]]
        t = np.array([[p[0, 0] - p[2, 0], p[1, 0] - p[2, 0]], [p[0, 1] - p[2, 1], p[1, 1] - p[2, 1]]])
        lam = np.linalg.solve(t, [x - p[2, 0], y - p[2, 1]])
        return np.array([lam[0], lam[1], 1.0 - lam.sum()])

    def locate(self, x: float, y: float, tol: float = 1e-9) -> int:
        if self.tree is None:
            return -1
        _, idx = self.tree.query([x, y], k=self.k)
        for k in np.atleast_1d(idx):
            try:
                if self.weights(int(k), x, y).min() >= -tol:
                    return int(k)
            except np.linalg.LinAlgError:         # degenerate triangle
                continue
        return -1


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
