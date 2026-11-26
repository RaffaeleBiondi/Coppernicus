"""The board view's hover readout: which values it can show and their text. Plain logic, no
window: the view probes the point under the pointer with :func:`coppernicus.fields.probe`,
as the double-click does, and shows what :func:`hover_readout` returns: the value of the map
shown (the Show selector) as one large number, then the values chosen in View → Hover shows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

from coppernicus.fields import Probe
from coppernicus.results import RailResult

# (key, menu label) of every value the readout can show, in the order it shows them. "shown"
# is the value of the map shown, large, first; the others are the extras under it, each left
# out where it is that value. None checked turns the readout off.
HOVER_VALUES = (("shown", "map value (large)"), ("v", "V"), ("drop", "drop"), ("j", "|J| (A/mm²)"),
                ("layer", "layer"), ("net", "net"))
HOVER_DEFAULT = ("shown", "layer")
OLD_DEFAULT = ("v", "drop", "layer")     # the default up to 0.3.1, before the map's value led
# The extra each map shows as its large value, so the label never repeats it.
MAP_VALUE = {"drop": "drop", "voltage": "v", "current": "j"}
DELTA = "Δ drop vs baseline"             # the Δ map's value, worded as the double-click words it
LABEL_OFFSET_PX = (16, 12)     # from the cursor to the label's corner: right, down


@dataclass(frozen=True)
class HoverValues:
    """What the readout can show at one point of a solved layer."""
    voltage_v: float               # NaN on copper not connected to a source
    drop_mv: float                 # RailResult.shown_drop_at, in mV
    drop_word: str                 # RailResult.drop_word: "drop", "rise" or "shift"
    drop_unit: str                 # RailResult.shown_drop_unit: "mV below 3.3 V", ...
    current_density_a_mm2: float
    layer: str
    nets: str                      # every net of the result: the mesh has no net per point
    delta_mv: float = float("nan")     # the Δ map's change of drop (mV); NaN: no baseline copper
    drop_pct: float = float("nan")     # the drop, % of |v_ref| (RailResult.drop_pct); NaN at 0 V


@dataclass(frozen=True)
class HoverReadout:
    """What the hover label shows at one point. ``lead``: the value of the map shown with its
    unit ("7.263 mV"), in large type, and ``secondary``: a smaller figure beside it where one
    is natural (the drop's share of the reference, "0.22 %"); ``caption``: what the lead is,
    where its unit does not say it ("drop below 3.3 V"); ``lines``: the chosen extras.
    ``lead_line`` is the lead as the status-bar readout writes it ("drop = 7.263 mV below
    3.3 V"), for :meth:`text`. Empty (false) when there is nothing to show: the label is then
    hidden."""
    lead: str = ""
    caption: str = ""
    lead_line: str = ""
    lines: Tuple[str, ...] = ()
    secondary: str = ""

    def __bool__(self) -> bool:
        return bool(self.lead_line or self.lines)

    def text(self) -> str:
        """The readout as plain text, one value per line, the lead first in the status bar's
        words: what :meth:`BoardView.hover_at` returns and the label's ``text()``."""
        return "\n".join(([self.lead_line] if self.lead_line else []) + list(self.lines))

    def label_lines(self) -> Tuple[str, ...]:
        """The small lines under the lead, as the label shows them: the caption, then the
        extras without their " = " ("V 3.29274 V", "|J| 2.65 A/mm²")."""
        extras = [line.replace(" = ", " ", 1) for line in self.lines]
        return tuple(([self.caption] if self.caption else []) + extras)


def hover_values(result: RailResult, layer: str, p: Probe, delta_mv: float = float("nan")) -> HoverValues:
    """The values at probe ``p`` on ``layer`` of ``result``, with the result's wording;
    ``delta_mv``: the change of drop there on the Δ map."""
    drop_mv = float(result.shown_drop_at(p.voltage_v)) * 1e3
    pct = drop_mv / (10.0 * abs(result.v_ref)) if result.v_ref else float("nan")
    return HoverValues(p.voltage_v, drop_mv, result.drop_word(), result.shown_drop_unit(),
                       p.current_density_a_mm2, layer, " + ".join(result.nets) or result.net, delta_mv, pct)


def _value(values: HoverValues, key: str) -> Tuple[str, str, str]:
    """(number with its unit, caption, readout line) of ``key`` ("v", "drop" or "j") at a point
    joined to a source, at the double-click readout's precision."""
    if key == "v":
        number = f"{values.voltage_v:.5f} V"
        return number, "", f"V = {number}"
    if key == "drop":                     # "mV below 3.3 V": the unit, then the reference
        unit, _, ref = values.drop_unit.partition(" ")
        number = f"{values.drop_mv:.3f} {unit}"
        return number, f"{values.drop_word} {ref}".strip(), f"{values.drop_word} = {number} {ref}".strip()
    number = f"{values.current_density_a_mm2:.2f} A/mm²"
    return number, "", f"|J| = {number}"


def hover_readout(values: Optional[HoverValues], chosen: Iterable[str], field: Optional[str]) -> HoverReadout:
    """The readout on the map of ``field`` ("drop", "voltage", "current" or "difference", as
    the view names its maps): with "shown" chosen, the map's value as the lead (the drop with
    its word and reference, V, |J|, or the change of drop on the Δ map); then one line per
    other chosen value ("v", "drop", "j"), then the layer and the net on one line ("layer",
    "net"), always in that order. Copper not connected to a source reads "not connected" in
    place of the numbers. Empty when nothing is chosen or there is no copper (``values``
    None)."""
    chosen = set(chosen)
    if values is None or not chosen:
        return HoverReadout()
    where = [text for key, text in (("layer", values.layer), ("net", values.nets)) if key in chosen]
    tail = [" · ".join(where)] if where else []
    if not np.isfinite(values.voltage_v):
        return HoverReadout(lines=tuple(["not connected"] + tail))
    lead = caption = lead_line = led = ""
    lines = []
    if "shown" in chosen and field == "difference":
        if np.isfinite(values.delta_mv):
            lead = f"{values.delta_mv:+.3f} mV"
            caption, lead_line = DELTA, f"{DELTA} = {lead}"
        else:
            lines.append(f"{DELTA}: no baseline copper here")
    elif "shown" in chosen and field in MAP_VALUE:
        led = MAP_VALUE[field]
        lead, caption, lead_line = _value(values, led)
    share = f"{values.drop_pct:.2f} %" if led == "drop" and np.isfinite(values.drop_pct) else ""
    lines += [_value(values, key)[2] for key in ("v", "drop", "j") if key in chosen and key != led]
    return HoverReadout(lead, caption, lead_line, tuple(lines + tail), share)


def hover_text(values: Optional[HoverValues], chosen: Iterable[str], field: Optional[str] = "drop") -> str:
    """:func:`hover_readout` as plain text (:meth:`HoverReadout.text`); "" hides the readout."""
    return hover_readout(values, chosen, field).text()


def temperature_reading(t_c: float, cell_mm: float) -> str:
    """The board temperature of one grid cell, as the double-click and the hover read it on
    the "Board temperature" map: "T = 41.8 °C (0.24 mm cell)"; empty outside the board (NaN)."""
    if not np.isfinite(t_c):
        return ""
    return f"T = {t_c:.1f} °C ({cell_mm:.3g} mm cell)"


def temperature_readout(t_c: float, cell_mm: float, layer: str, chosen: Iterable[str]) -> HoverReadout:
    """The readout on the board-temperature map: the cell's temperature whenever the readout
    is on (it replaces V, drop and |J|, which this map does not show), as the lead ("41.8 °C")
    with "shown" chosen, else as a line; then the layer if chosen. Empty when nothing is
    chosen or outside the board."""
    chosen = set(chosen)
    reading = temperature_reading(t_c, cell_mm)
    if not chosen or not reading:
        return HoverReadout()
    tail = (layer,) if "layer" in chosen else ()
    if "shown" in chosen:
        return HoverReadout(f"{t_c:.1f} °C", "", reading, tail)
    return HoverReadout(lines=(reading,) + tail)


def temperature_hover_text(t_c: float, cell_mm: float, layer: str, chosen: Iterable[str]) -> str:
    """:func:`temperature_readout` as plain text: "T = 41.8 °C (0.24 mm cell)", then the layer."""
    return temperature_readout(t_c, cell_mm, layer, chosen).text()


def chosen_from_setting(text, old=None) -> Tuple[str, ...]:
    """The chosen values from their stored setting ("shown,layer"): an empty string gives none,
    unknown keys are dropped. Never set (``None``): from ``old``, the setting of View → Hover
    shows up to 0.3.1 (no "shown" then), when there is one: its default becomes
    :data:`HOVER_DEFAULT`, a choice of its own keeps its values under the map's value, none
    checked stays off. Neither set: :data:`HOVER_DEFAULT`."""
    if text is None:
        if old is None:
            return HOVER_DEFAULT
        kept = chosen_from_setting(old)
        if not kept or kept == OLD_DEFAULT:
            return HOVER_DEFAULT if kept else ()
        return chosen_from_setting(setting_from_chosen(("shown",) + kept))
    keys = set(map(str, text)) if isinstance(text, (list, tuple)) else set(str(text).split(","))
    return tuple(key for key, _label in HOVER_VALUES if key in keys)


def setting_from_chosen(chosen: Iterable[str]) -> str:
    """The setting that stores ``chosen``: the keys, comma-separated, in menu order. A
    string, not a list: an empty list may read back as unset."""
    chosen = set(chosen)
    return ",".join(key for key, _label in HOVER_VALUES if key in chosen)


def label_position(cursor: Sequence[float], size: Sequence[float], viewport: Sequence[float],
                   offset=LABEL_OFFSET_PX) -> Tuple[int, int]:
    """Top-left corner of a label of ``size`` (w, h) beside the cursor, in a viewport of
    ``viewport`` (w, h) pixels (y down): ``offset`` (right, down; one number for both) from
    the cursor, flipped to its left or above it where it would leave the viewport, then
    clamped inside, so it never sits under the cursor while it fits beside it."""
    (cx, cy), (w, h), (vw, vh) = cursor, size, viewport
    ox, oy = (offset, offset) if isinstance(offset, (int, float)) else offset
    x = cx + ox if cx + ox + w <= vw else cx - ox - w
    y = cy + oy if cy + oy + h <= vh else cy - oy - h
    return int(min(max(x, 0), max(vw - w, 0))), int(min(max(y, 0), max(vh - h, 0)))
