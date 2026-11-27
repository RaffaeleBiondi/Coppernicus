"""The copper-thickness what-if of the solve settings (``Settings.copper_thickness_mm``,
layer name → mm): its summary line and the dialog that edits it, one field per copper layer
of the open board. An empty field means the board file's thickness, so its key is removed;
a typed value keeps its key even when it equals the file's. The board file is never
changed."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app.units import Unit
from coppernicus.project import ProjectError, check_copper_thickness

MM = Unit("mm")


def thickness_summary(values: Mapping[str, float]) -> str:
    """The settings row's text: "board file" when nothing is set, else each layer with its
    thickness ("B.Cu 0.07 mm")."""
    if not values:
        return "board file"
    return ", ".join(f"{layer} {mm:g} mm" for layer, mm in values.items())


def off_board(values: Mapping[str, float], layers: Iterable[str]) -> List[str]:
    """The keys of ``values`` naming no copper layer of the board (each would stop every
    solve), in their order."""
    on_board = set(layers)
    return [layer for layer in values if layer not in on_board]


def _mm(text: str) -> object:
    """A field's thickness, "0.035", "0,035" or "0.035 mm"; text that is no number is passed
    on as typed, for the check to name it."""
    try:
        return MM.parse(text)
    except ValueError:
        return text.strip()


def thickness_from_texts(texts: Mapping[str, str], kept: Mapping[str, float]) -> Dict[str, float]:
    """The settings from the dialog: ``texts`` is each board layer's field (empty: the board
    file's value, no key), ``kept`` the keys for layers not on the board still listed. A
    comma reads as the decimal sign. Checked by :func:`check_copper_thickness`, which raises
    :class:`ProjectError` naming the layer."""
    values: Dict[str, object] = {layer: _mm(text) for layer, text in texts.items() if text.strip()}
    values.update(kept)
    return check_copper_thickness(values)


class CopperThicknessDialog(QDialog):
    """One field per copper layer of the board (``file_mm``: the board file's thickness by
    layer, in stack order), filled from ``values`` (the settings). Keys for layers the board
    does not have are listed, each with a Remove button. OK checks the fields and stays open
    on an error; :meth:`values` is then the new settings."""

    def __init__(self, file_mm: Mapping[str, float], values: Mapping[str, float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Copper thickness")
        self._values = dict(values)
        self._kept = {layer: values[layer] for layer in off_board(values, file_mm)}
        intro = QLabel("What-if copper thickness for every solve, in mm (1 oz = 0.035). Empty: the "
                       "board file's thickness. The board file is never changed; real changes of "
                       "copper weight are made in KiCad.")
        intro.setWordWrap(True)
        self.fields: Dict[str, QLineEdit] = {}
        form = QFormLayout()
        for layer, mm in file_mm.items():
            edit = QLineEdit(placeholderText=f"{mm:g} mm (board file)")
            edit.setText(MM.exact(float(values[layer])) if layer in values else "")   # every digit
            self.fields[layer] = edit
            form.addRow(layer, edit)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        self.remove_buttons: Dict[str, QPushButton] = {}
        self.off_board_box = QGroupBox("Not on this board (every solve stops on these)")
        rows = QVBoxLayout(self.off_board_box)
        for layer, mm in self._kept.items():
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(QLabel(f"{layer} {mm:g} mm"), 1)
            button = QPushButton("Remove")
            button.clicked.connect(lambda _=False, layer=layer, row=row: self._remove(layer, row))
            line.addWidget(button)
            self.remove_buttons[layer] = button
            rows.addWidget(row)
        self.off_board_box.setVisible(bool(self._kept))
        layout.addWidget(self.off_board_box)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty("role", "fail")        # the theme's style sheet
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _remove(self, layer: str, row: QWidget):
        self._kept.pop(layer, None)
        row.hide()
        self.off_board_box.setVisible(bool(self._kept))

    def accept(self):
        try:
            self._values = thickness_from_texts({layer: e.text() for layer, e in self.fields.items()},
                                                self._kept)
        except ProjectError as e:
            self.error.setText(str(e))
            self.error.show()
            self.adjustSize()                 # make room for the error: no row squashed
            return
        super().accept()

    def values(self) -> Dict[str, float]:
        """The settings as the dialog has them (after OK: the fields, checked)."""
        return dict(self._values)
