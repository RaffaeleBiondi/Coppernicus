"""The board view's hover readout: which values it can show and their text. Plain logic, no
window: the view probes the point under the pointer with :func:`coppernicus.fields.probe`,
as the double-click does, and shows what :func:`hover_text` returns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np

from coppernicus.fields import Probe
from coppernicus.results import RailResult


@dataclass(frozen=True)
class HoverValues:
    """What the readout can show at one point of a solved layer."""
    voltage_v: float               # NaN on copper not connected to a source
    drop_mv: float                 # RailResult.drop_at, in mV
    drop_word: str                 # "rise" on a ground (RailResult.is_rise), else "drop"
    drop_unit: str                 # RailResult.drop_unit: "mV below 3.3 V", ...
    current_density_a_mm2: float
    layer: str
    nets: str                      # every net of the result: the mesh has no net per point


def hover_values(result: RailResult, layer: str, p: Probe) -> HoverValues:
    """The values at probe ``p`` on ``layer`` of ``result``, with the result's wording."""
    return HoverValues(p.voltage_v, float(result.drop_at(p.voltage_v)) * 1e3,
                       "rise" if result.is_rise() else "drop", result.drop_unit(),
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
