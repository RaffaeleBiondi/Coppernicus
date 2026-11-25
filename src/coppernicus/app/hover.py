"""The board view's hover readout: which values it can show and their text. Plain logic, no
window: the view probes the point under the pointer with :func:`coppernicus.fields.probe`,
as the double-click does, and shows what :func:`hover_text` returns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np

from coppernicus.fields import Probe
from coppernicus.results import RailResult

# (key, menu label) of every value the readout can show, in the order it shows them.
HOVER_VALUES = (("v", "V"), ("drop", "drop"), ("j", "|J| (A/mm²)"), ("layer", "layer"), ("net", "net"))
HOVER_DEFAULT = ("v", "drop", "layer")
LABEL_OFFSET_PX = 14           # from the cursor to the label's corner


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


def hover_values(result: RailResult, layer: str, p: Probe) -> HoverValues:
    """The values at probe ``p`` on ``layer`` of ``result``, with the result's wording."""
    return HoverValues(p.voltage_v, float(result.shown_drop_at(p.voltage_v)) * 1e3,
                       result.drop_word(), result.shown_drop_unit(),
                       p.current_density_a_mm2, layer, " + ".join(result.nets) or result.net)


def hover_text(values: Optional[HoverValues], chosen: Iterable[str]) -> str:
    """The readout's text: one line per chosen value ("v", "drop", "j"), then the layer and
    the net on one line ("layer", "net"), always in that order. Copper not connected to a
    source reads "not connected" in place of the numbers. Empty when nothing is chosen or
    there is no copper (``values`` None): the readout is then hidden."""
    chosen = set(chosen)
    if values is None or not chosen:
        return ""
    lines = []
    if not np.isfinite(values.voltage_v):
        lines.append("not connected")
    else:
        if "v" in chosen:
            lines.append(f"V = {values.voltage_v:.5f} V")
        if "drop" in chosen:
            lines.append(f"{values.drop_word} = {values.drop_mv:.3f} {values.drop_unit}")
        if "j" in chosen:
            lines.append(f"|J| = {values.current_density_a_mm2:.2f} A/mm²")
    where = [text for key, text in (("layer", values.layer), ("net", values.nets)) if key in chosen]
    if where:
        lines.append(" · ".join(where))
    return "\n".join(lines)


def chosen_from_setting(text: Optional[str]) -> Tuple[str, ...]:
    """The chosen values from their stored setting ("v,drop"): ``None`` (never set) gives
    :data:`HOVER_DEFAULT`, an empty string none. Unknown keys are dropped."""
    if text is None:
        return HOVER_DEFAULT
    keys = set(str(text).split(","))
    return tuple(key for key, _label in HOVER_VALUES if key in keys)


def setting_from_chosen(chosen: Iterable[str]) -> str:
    """The setting that stores ``chosen``: the keys, comma-separated, in menu order. A
    string, not a list: an empty list may read back as unset."""
    chosen = set(chosen)
    return ",".join(key for key, _label in HOVER_VALUES if key in chosen)


def label_position(cursor: Sequence[float], size: Sequence[float], viewport: Sequence[float],
                   offset: float = LABEL_OFFSET_PX) -> Tuple[int, int]:
    """Top-left corner of a label of ``size`` (w, h) beside the cursor, in a viewport of
    ``viewport`` (w, h) pixels (y down): ``offset`` right of and below the cursor, flipped to
    its left or above it where it would leave the viewport, then clamped inside, so it never
    sits under the cursor while it fits beside it."""
    (cx, cy), (w, h), (vw, vh) = cursor, size, viewport
    x = cx + offset if cx + offset + w <= vw else cx - offset - w
    y = cy + offset if cy + offset + h <= vh else cy - offset - h
    return int(min(max(x, 0), max(vw - w, 0))), int(min(max(y, 0), max(vh - h, 0)))
