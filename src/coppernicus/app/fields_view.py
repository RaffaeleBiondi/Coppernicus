"""Where the shown rail comes from, when the board's parts carry KiCad footprint fields: the
banner of the rail panel (and its red line when a field cannot be used), and the texts the
read-only rows show. The origins are those of ``coppernicus.board_fields.merge``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

ERROR_STYLE = "color: #c0392b;"
WARNING_STYLE = ("color: #6e4a07; background: #fdf2e0; border: 1px solid #b9770e; "
                 "border-radius: 3px; padding: 4px;")
OVERRIDE, USE_FIELDS = "override", "use_fields"          # what the banner's button asks for


def read_only_reason(net: str) -> str:
    return f"{net} comes from KiCad fields: Override in project to edit it"


def row_tip(entry: Mapping[str, Any]) -> str:
    """A read-only row's tooltip: the KiCad fields its values come from (one terminal of a
    ``board_fields`` listing: its ``ref``, ``from`` and ``fields``)."""
    fields = ", ".join(f'{name} "{text}"' for name, text in entry.get("fields", {}).items())
    return (f"From KiCad footprint fields on {entry.get('ref', '')}: {fields} "
            f"({entry.get('from', '')}). Override in project to edit it.")


@dataclass
class Banner:
    """What the banner says about one rail."""
    text: str = ""
    tooltip: str = ""
    action: str = ""                   # OVERRIDE, USE_FIELDS or "" (no button)
    warnings: List[str] = field(default_factory=list)


def describe(origin, values_text: str = "", warnings: Optional[List[str]] = None) -> Banner:
    """The banner for a rail of origin ``origin`` (a ``board_fields.Origin``, or None for a
    net the fields say nothing about). ``values_text``: what the KiCad fields give for the
    net (for an overridden rail); ``warnings``: the origin's and those of links on the net."""
    warnings = list(origin.warnings if origin is not None else []) if warnings is None else warnings
    if origin is None:
        return Banner(warnings=warnings)
    notes = "\n".join(origin.notes)
    if origin.source == "fields":
        parts = ", ".join((origin.board_fields or {}).get("parts", []))
        return Banner(f"From KiCad footprint fields (parts {parts}) — read-only", notes, OVERRIDE, warnings)
    if origin.source == "overridden":
        return Banner(f"Project rail overrides the KiCad fields (KiCad: {values_text})", notes,
                      USE_FIELDS, warnings)
    if origin.board_fields and origin.warnings:          # the fields leave the net incomplete
        return Banner("\n".join(origin.warnings), notes,
                      warnings=[w for w in warnings if w not in origin.warnings])
    return Banner(warnings=warnings)


class FieldsBanner(QWidget):
    """The top of the rail panel: a red line when the KiCad fields cannot be used, then where
    the rail comes from, with its button (Override in project, Use KiCad fields) and its
    warnings in an amber box."""
    action_requested = Signal(str)      # OVERRIDE or USE_FIELDS

    def __init__(self, parent=None):
        super().__init__(parent)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(ERROR_STYLE)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.button = QPushButton()
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet(WARNING_STYLE)
        self._action = ""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.text, 1)
        row.addWidget(self.button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.error)
        layout.addLayout(row)
        layout.addWidget(self.warnings)
        self.button.clicked.connect(lambda: self.action_requested.emit(self._action))
        self.set_error("")
        self.show_banner(Banner())

    def set_error(self, text: str):
        """The red line: why the KiCad fields cannot be used ("" for none)."""
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def show_banner(self, banner: Banner):
        self._action = banner.action
        self.text.setText(banner.text)
        self.text.setToolTip(banner.tooltip)
        self.text.setVisible(bool(banner.text))
        self.button.setText({OVERRIDE: "Override in project", USE_FIELDS: "Use KiCad fields"}.get(
            banner.action, ""))
        self.button.setToolTip({
            OVERRIDE: "Copy this rail into the project file and edit it there (override_board_fields)",
            USE_FIELDS: "Go back to the KiCad fields: the project rail keeps only its limits"}.get(
            banner.action, ""))
        self.button.setVisible(bool(banner.action))
        self.warnings.setText("\n".join(banner.warnings))
        self.warnings.setVisible(bool(banner.warnings))
