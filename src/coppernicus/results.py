"""Results of a DC solve, as plain arrays: what the viewer, the maps and the reports read.

Nothing here depends on the solver engine; :mod:`coppernicus.engine` fills these in.
Coordinates are millimetres in KiCad's frame (x right, **y down**).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class LayerField:
    """The solved copper of one layer: a triangle mesh with a voltage per node and a
    current density, power and area per triangle."""
    layer_id: int
    name: str
    points: np.ndarray             # (N, 2) mm
    tris: np.ndarray               # (M, 3) indices into points
    voltage: np.ndarray            # (N,) V; NaN on copper not connected to a source
    current_density: np.ndarray    # (M,) A/mm^2, uniform through the copper thickness
    tri_power: np.ndarray          # (M,) W dissipated in each triangle
    tri_area: np.ndarray           # (M,) mm^2

    @property
    def power(self) -> float:
        return float(np.nansum(self.tri_power))

    def centroids(self) -> np.ndarray:
        return self.points[self.tris].mean(axis=1)


@dataclass
class TerminalResult:
    """A source or a load as solved."""
    name: str
    kind: str                      # "source" or "load"
    ref: str
    current: float                 # A: delivered (source) or drawn (load)
    v_min: float                   # V over its pads
    v_max: float
    pad_ids: List[str] = field(default_factory=list)
    positions: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class ViaCurrent:
    owner_id: str                  # via id, or pad id for a component's plated hole
    kind: str                      # "via" or "pth"
    position: Tuple[float, float]
    drill_mm: float
    current: float                 # largest |I| through any barrel segment, A
    power: float                   # W dissipated in the barrel


@dataclass
class RailResult:
    net: str
    nets: List[str]                # every net solved: the rail plus nets joined by net ties
    v_ref: float                   # the nominal voltage drops are measured from
    layers: List[LayerField]
    sources: List[TerminalResult]
    loads: List[TerminalResult]
    vias: List[ViaCurrent]
    max_drop: float                # V, largest drop below v_ref anywhere on the copper
    j_max: float                   # A/mm^2, point maximum (mesh-dependent at sharp corners)
    power_loss: float              # W, total in copper, barrels and series links
    energy_balance_error: float    # relative; ~0 for a sound solve
    loss_vias: float = 0.0         # W in via and plated-hole barrels
    loss_series: float = 0.0       # W in series links (net-tie shorts carry none)
    stats: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)   # inputs the engine filled in

    # ------------------------------------------------------------------ helpers
    def layer(self, name: str) -> Optional[LayerField]:
        return next((lf for lf in self.layers if lf.name == name), None)

    def drop(self, t: TerminalResult) -> float:
        """Volts below ``v_ref`` at the terminal's worst pad."""
        return self.v_ref - t.v_min

    def drop_pct(self, t: TerminalResult) -> float:
        return 100.0 * self.drop(t) / self.v_ref if self.v_ref else float("nan")

    def loss_breakdown(self) -> Dict[str, float]:
        """Watts per copper layer, plus vias and series links: where the voltage is lost."""
        out = {lf.name: lf.power for lf in self.layers}
        out["vias"] = self.loss_vias
        if self.loss_series:
            out["series links"] = self.loss_series
        return out
