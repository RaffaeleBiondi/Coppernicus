"""The mesher of the app's solves: chosen in the setup panel's Mesher box, kept in the app's
settings, overruled by a project file's ``settings.mesher`` (which the app never changes).

Every solve the app starts (rail, tree with its board temperature, impedance, noise, the
tolerance tab's solves through those, and the estimates that mesh) runs inside
:func:`coppernicus.engine.meshing.session.using` with :func:`effective`'s name, in its worker
thread; a mesher that cannot run, or Triangle missing behind it, reaches the user as the
error's message and hint (:func:`failure`). The box shows the mesher each solve will use,
whatever the default is (``session.DEFAULT``), and, while only Coppernicus's stand-in takes
Triangle's place, "Triangle (download…)": choosing it shows Triangle's licence and downloads
it (:func:`coppernicus.bootstrap_triangle.fetch_for_mesher`).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QWidget

from coppernicus.app import settings as app_settings
from coppernicus.engine.meshing import registry
from coppernicus.engine.meshing import session as meshing
from coppernicus.triangle_standin import TriangleNotInstalled

SETTING = "solve/mesher"            # the app's choice ("" or missing: the default)
LABELS = {"triangle": "Triangle", "rust": "Rust"}


def app_choice() -> Optional[str]:
    """The mesher chosen in the app, or None (the default)."""
    value = app_settings().value(SETTING, "")
    return str(value).strip().lower() or None


def save_choice(name: Optional[str]) -> None:
    app_settings().setValue(SETTING, name or "")


def effective(project) -> str:
    """The mesher a solve of ``project`` uses: its ``settings.mesher``, else the app's choice,
    else the default (:func:`coppernicus.engine.meshing.session.resolve`)."""
    return meshing.resolve(None, getattr(getattr(project, "settings", None), "mesher", None), app_choice())


def triangle_missing() -> bool:
    """Whether Coppernicus's stand-in takes Triangle's place (the packaged app before a download)."""
    from coppernicus.triangle_standin import standin_in_use
    return standin_in_use()


def failure(error: BaseException) -> Optional[str]:
    """The text a worker shows for a mesher error (its message, then its hint), or None for
    any other exception."""
    if isinstance(error, (TriangleNotInstalled, meshing.MesherUnavailable)):
        return f"{error.message}\n{error.hint}"
    return None


def shown(name: str) -> str:
    """How the status bar and the box name a mesher: "Rust", "Triangle"; a backend
    added with ``registry.register`` gets "(experimental)"."""
    info = meshing.describe(name)
    label = LABELS.get(name, name)
    return f"{label} (experimental)" if info["experimental"] else label


def entries() -> List[Tuple[str, str, bool, str]]:
    """(name, label, can run here, why not) for every mesher the app knows, default first."""
    here = set(registry.available())
    names = sorted(set(registry.KNOWN) | here, key=lambda n: (n != meshing.DEFAULT, n))
    out = []
    for name in names:
        label = shown(name)
        if name == meshing.DEFAULT:
            label += " — default"
        if name == "triangle" and triangle_missing():
            out.append((name, f"{LABELS['triangle']} (download…)", True, ""))
            continue
        why = "" if name in here else meshing.HINTS.get(name, "not available here")
        out.append((name, label, name in here, why))
    return out


class MesherBox(QWidget):
    """"Mesher [box]": the app's choice, or the project's (shown, and the box then locked)."""

    changed = Signal(str)            # the mesher solves now use

    def __init__(self, parent=None):
        super().__init__(parent)
        self.box = QComboBox()
        self.box.setObjectName("mesherBox")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 8, 4)
        layout.addWidget(QLabel("Mesher"))
        layout.addWidget(self.box, 1)
        self._project_mesher: Optional[str] = None
        self._name = ""
        self.box.activated.connect(self._chosen)
        self.refresh()

    def set_project(self, project) -> None:
        """The project's ``settings.mesher`` overrules the app's choice: shown, box locked."""
        self._project_mesher = getattr(getattr(project, "settings", None), "mesher", None) or None
        self.refresh()

    def current(self) -> str:
        """The mesher a solve started now uses."""
        return meshing.resolve(None, self._project_mesher, app_choice())

    def refresh(self) -> None:
        name = self.current()
        self.box.blockSignals(True)
        self.box.clear()
        index: Dict[str, int] = {}
        for i, (key, label, ok, why) in enumerate(entries()):
            self.box.addItem(label, key)
            index[key] = i
            if not ok:
                item = self.box.model().item(i)
                item.setEnabled(False)
                item.setToolTip(why)
        if name not in index:                              # a project's mesher the app does not know
            self.box.addItem(name, name)
            index[name] = self.box.count() - 1
        self.box.setCurrentIndex(index[name])
        self.box.blockSignals(False)
        locked = self._project_mesher is not None
        self.box.setEnabled(not locked)
        self.box.setToolTip(f"Set by the project file (settings.mesher: {self._project_mesher}); "
                            "change it there" if locked else
                            "The mesher every solve uses (rail, tree, impedance, noise, tolerance); "
                            "kept for the next start. A project file's settings.mesher wins.")
        self._name = name

    def _chosen(self, index: int) -> None:
        name = self.box.itemData(index)
        if name == self._name:
            return
        if name == "triangle" and triangle_missing():
            from coppernicus import bootstrap_triangle
            try:
                ok = bootstrap_triangle.fetch_for_mesher("triangle", gui=True)
            except bootstrap_triangle.BootstrapError as e:
                QMessageBox.warning(self, "Triangle", f"{e}")
                ok = False
            if not ok:
                self.refresh()                             # back to the mesher in use
                return
        save_choice(name)              # chosen, kept: a later change of the default leaves it
        self.refresh()
        self.changed.emit(self.current())
