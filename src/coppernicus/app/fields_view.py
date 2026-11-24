"""Where the shown rail comes from, when the board's parts carry KiCad footprint fields: the
banner of the rail panel (and its red line when a field cannot be used)."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

ERROR_STYLE = "color: #c0392b;"


class FieldsBanner(QWidget):
    """The top of the rail panel: a red line when the KiCad fields cannot be used."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(ERROR_STYLE)
        self.error.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.error)

    def set_error(self, text: str):
        """The red line: why the KiCad fields cannot be used ("" for none)."""
        self.error.setText(text)
        self.error.setVisible(bool(text))
