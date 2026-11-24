"""Results of a DC solve, as plain arrays: what the viewer, the maps and the reports read.

Nothing here depends on the solver engine; :mod:`coppernicus.engine` fills these in.
Coordinates are millimetres in KiCad's frame (x right, **y down**).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
    net: str = ""                  # the net of its pads, when known (power-tree groups)


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
    max_drop: float                # V, largest drop below v_ref anywhere on the copper (a
                                   # ground of a power tree: its rise, see ``kind``)
    j_max: float                   # A/mm^2, point maximum (mesh-dependent at sharp corners)
    power_loss: float              # W, total in copper, barrels and series links
    energy_balance_error: float    # relative; ~0 for a sound solve
    loss_vias: float = 0.0         # W in via and plated-hole barrels
    loss_series: float = 0.0       # W in series links (net-tie shorts carry none)
    stats: Dict[str, float] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)   # inputs the engine filled in
    # "rail", or "ground" for a ground net of a power-tree solve: v_ref is then its 0 V
    # reference and max_drop the rise with the largest magnitude, signed (negative: the
    # ground sinks below 0 V, as under a negative rail).
    kind: str = "rail"
    converged: bool = True         # False: a power-tree group whose iteration did not converge

    # ------------------------------------------------------------------ helpers
    def layer(self, name: str) -> Optional[LayerField]:
        return next((lf for lf in self.layers if lf.name == name), None)

    def drop(self, t: TerminalResult) -> float:
        """Volts below ``v_ref`` at the terminal's worst pad."""
        return self.v_ref - t.v_min

    def drop_pct(self, t: TerminalResult) -> float:
        """The drop as a share of |v_ref| (positive on a negative rail too)."""
        return 100.0 * self.drop(t) / abs(self.v_ref) if self.v_ref else float("nan")

    def loss_breakdown(self) -> Dict[str, float]:
        """Watts per copper layer, plus vias and series links: where the voltage is lost."""
        out = {lf.name: lf.power for lf in self.layers}
        out["vias"] = self.loss_vias
        if self.loss_series:
            out["series links"] = self.loss_series
        return out


# ---------------------------------------------------------------------- power tree

DISCONNECTED = "DISCONNECTED"                     # no copper path to its source: not solved
NOT_CONVERGED = "NOT CONVERGED"
OUT_OF_REGULATION = "VRM OUT OF REGULATION"       # its regulator cannot hold its voltage


@dataclass
class VrmReport:
    """A source or regulator after a power-tree solve. Powers are across its own pins
    (from its ground pins); input values are ``None`` for a plain source."""
    name: str
    kind: str                      # "source", "ldo", "smps"
    group: str                     # the TreeResult.groups key of the rail it drives
    v_set_v: float
    v_out_v: float                 # mean over its output pins
    i_out_a: float
    v_in_v: Optional[float] = None
    i_in_a: Optional[float] = None
    p_out_w: float = 0.0
    p_in_w: Optional[float] = None
    p_loss_w: Optional[float] = None
    v_ground_v: Optional[float] = None
    v_sense_v: Optional[float] = None
    v_ground_sense_v: Optional[float] = None
    regulation: str = ""           # why it cannot hold its voltage; "" when it can
    # Its input, judged as a load on the rail that feeds it (regulators only):
    input_net: str = ""
    input_group: str = ""          # the TreeResult.groups key of that rail
    input_nominal_v: Optional[float] = None    # the set voltage of the source driving it
    v_in_worst_v: Optional[float] = None       # lowest input pin - highest ground pin
    input_drop_v: Optional[float] = None       # input_nominal_v - v_in_worst_v (toward 0 V)
    input_drop_pct: Optional[float] = None
    input_status: str = ""         # DISCONNECTED, NOT_CONVERGED or OUT_OF_REGULATION
    input_passed: Optional[bool] = None        # against its input net's max_drop_pct


@dataclass
class LoadReport:
    """A load after a power-tree solve: the voltage it sees is across its supply and ground
    pins, so the ground's rise counts in its drop."""
    name: str
    group: str
    current_a: float
    nominal_v: float               # the set voltage of the source driving its rail
    v_supply_v: float              # mean over its supply pins
    v_supply_min_v: float
    v_ground_v: Optional[float]    # mean over its ground pins; None without ground pins
    v_across_v: float              # v_supply_v - (v_ground_v or 0)
    v_worst_v: float               # lowest supply pin - highest ground pin (or the boost side)
    drop_v: float                  # nominal_v - v_worst_v (negative: a boost)
    drop_pct: float
    status: str = ""               # DISCONNECTED, NOT_CONVERGED or OUT_OF_REGULATION
    passed: Optional[bool] = None  # against its net's max_drop_pct; None without a limit
    net: str = ""                  # its supply net


@dataclass
class TreeResult:
    """A whole power tree: every rail and ground group, its sources and regulators, its
    loads, and the limits each group is judged against (only the user's)."""
    groups: Dict[str, RailResult] = field(default_factory=dict)
    vrms: List[VrmReport] = field(default_factory=list)
    loads: List[LoadReport] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)   # name, nets, resistance, current
    # net -> project Limits, for every solved net the project has a rail for (a group's
    # key is its driving net; see coppernicus.power_tree.net_limits)
    limits: Dict[str, Any] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, float] = field(default_factory=dict)

    def group_of(self, net: str) -> Optional[str]:
        """The key of the group ``net`` was solved in, or None."""
        return next((k for k, r in self.groups.items() if net in r.nets), None)
