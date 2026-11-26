"""Where the shown rail comes from, when the board's parts carry KiCad footprint fields: the
banner of the rail panel (and its red line when a field cannot be used), the texts the
read-only rows show (the rail panel's, and the Impedance tab's capacitor models from KiCad
fields), and the two questions of an override (which rail to keep, going back to the
fields). The origins are those of ``coppernicus.board_fields.merge``.

The questions are module functions, so a scripted run can answer them instead of a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

ERROR_STYLE = "color: #c0392b;"
WARNING_STYLE = ("color: #6e4a07; background: #fdf2e0; border: 1px solid #b9770e; "
                 "border-radius: 3px; padding: 4px;")
OVERRIDE, USE_FIELDS = "override", "use_fields"          # what the banner's button asks for
USE_MINE, START_FROM_KICAD = "project", "kicad"          # the answers of ask_override


def read_only_reason(net: str) -> str:
    return f"{net} comes from KiCad fields: Override in project to edit it"


def row_tip(entry: Mapping[str, Any]) -> str:
    """A read-only row's tooltip: the KiCad fields its values come from (one terminal of a
    ``board_fields`` listing: its ``ref``, ``from`` and ``fields``)."""
    fields = ", ".join(f'{name} "{text}"' for name, text in entry.get("fields", {}).items())
    return (f"From KiCad footprint fields on {entry.get('ref', '')}: {fields} "
            f"({entry.get('from', '')}). Override in project to edit it.")


CAP_KEY_FIELDS = {"esl_h": "PI_ESL", "esr_ohm": "PI_ESR", "dielectric": "PI_DIELECTRIC"}   # one field each


def _use_project_cap(ref: str) -> str:
    return f"set override_board_fields: true on a project capacitor model naming {ref} to use the project's"


def cap_field_tip(cap, key: str) -> str:
    """The tooltip of a capacitor model from KiCad fields, read-only in the Impedance tab, in
    its column ``key`` (``cap``: a ``board_fields.FieldCap`` with its model): where the value
    lives, and how a project model takes its place."""
    ref, given, m = cap.ref, cap.fields, cap.model

    def field(name: str) -> str:
        return f'KiCad field {name} "{given[name]}" on {ref}'

    if key == "name":
        listed = ", ".join(f'{name} "{text}"' for name, text in given.items())
        value = "" if "PI_C" in given else f"; its capacitance read from the {cap.capacitance_from}"
        where = f"From KiCad footprint fields on {ref}, read-only (what the solve uses): {listed}{value}"
    elif key == "capacitance_f":
        where = (f"set in {field('PI_C')}" if "PI_C" in given
                 else f"read from the {cap.capacitance_from} of {ref} (no PI_C)")
    elif key in CAP_KEY_FIELDS:
        where = "set in " + field(CAP_KEY_FIELDS[key])
    elif key == "rated_v":
        if m.rated_v is not None:
            where = f"set in {field('PI_VRATED')}"
        elif "PI_VRATED" in given:
            where = (f"{field('PI_VRATED')} is not passed on: a {m.dielectric} capacitor is not a ceramic, "
                     "so it is not derated for DC bias")
        else:
            where = f"no PI_VRATED on {ref}: not derated for DC bias ({m.dielectric})"
    elif key == "kept_at_rated_pct":
        where = ("no KiCad field gives it (empty: KiPI's typical class II value)" if m.rated_v is not None
                 else "no KiCad field gives it, and without a voltage rating it is not derated")
    else:
        where = f"{ref}: the part whose KiCad fields give this model"
    return f"{where}; {_use_project_cap(ref)}"


def cap_lost_tip(lost) -> str:
    """The tooltip of a project capacitor model's parts when some of them went to the models of
    their KiCad fields (``lost``: (ref, that model's name) pairs), as the merge gives them."""
    parts = "; ".join(f"{ref} is solved with {name}, from its KiCad fields, not with this model"
                      for ref, name in lost)
    return (f"{parts}: KiCad fields win over a project model; set override_board_fields: true on this "
            "model in the project file to use its values")


def drop_tip(limit: Mapping[str, Any]) -> str:
    """The locked max drop's tooltip (``limit``: the ``limits`` of a ``board_fields`` listing:
    its ``max_drop_pct`` and ``from``, which names the field and the part)."""
    return (f"Max drop {limit['max_drop_pct']:g} % {limit.get('from', '')}: a KiCad limit always "
            "applies to its net, even to a project rail; change it in KiCad.")


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


def ask_override(parent, net: str, differences: List[str]) -> Optional[str]:
    """The project has its own rail on ``net``, which the KiCad fields replace and which
    differs from them (``differences``: the merge's warnings): keep it (``USE_MINE``), or
    start from the KiCad values (``START_FROM_KICAD``)? None: cancelled."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Question)
    box.setWindowTitle("Override in project")
    box.setText(f"The project file has its own rail on {net}, ignored so far because the KiCad "
                "footprint fields define it. They differ:")
    box.setInformativeText("\n".join(f"• {d}" for d in differences))
    mine = box.addButton("Use my project rail", QMessageBox.AcceptRole)
    kicad = box.addButton("Start from the KiCad values", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.exec()
    clicked = box.clickedButton()
    return USE_MINE if clicked is mine else START_FROM_KICAD if clicked is kicad else None


def confirm_use_fields(parent, net: str, sense_parts: List[str] = (),
                       tolerance_parts: List[str] = ()) -> bool:
    """Go back to the KiCad fields on ``net``: its project rail loses its sources and loads,
    and with them the remote sense pins of ``sense_parts`` and the tolerances of
    ``tolerance_parts`` (parts the fields' rail lacks; each named only when there are some)."""
    sense = (f" The remote sense pins of {', '.join(sense_parts)} are removed too: KiCad fields "
             "cannot give them yet." if sense_parts else "")
    tolerances = (f" The tolerances, accuracy and entered parts of {', '.join(tolerance_parts)} are removed "
                  "too: the KiCad fields' rail has no part of that reference (those of its own parts are "
                  "kept)." if tolerance_parts else "")
    answer = QMessageBox.question(
        parent, "Use KiCad fields",
        f"Use the KiCad footprint fields for {net} again? The project rail's sources and loads "
        f"are removed; its limits and net-tie option are kept.{sense}{tolerances}",
        QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
    return answer == QMessageBox.Yes
