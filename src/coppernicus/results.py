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
    # Which way the load current flows, so drop = drop_sign · (v_ref − V) at any point:
    # +1, out of the copper (a positive rail: the copper falls below v_ref); −1, into it (a
    # negative rail, where the copper moves toward 0 V, a net at 0 V solved alone, and a
    # ground group, where it rises). Not derivable from v_ref: a rail group at 0 V inside a
    # power tree draws (+1), a net at 0 V solved alone does not.
    drop_sign: float = 1.0

    # ------------------------------------------------------------------ helpers
    def layer(self, name: str) -> Optional[LayerField]:
        return next((lf for lf in self.layers if lf.name == name), None)

    @property
    def sign(self) -> float:
        """``drop_sign``; always -1 on a ground group (its drop is its signed rise)."""
        return -1.0 if self.kind == "ground" else self.drop_sign

    def worst_v(self, t: TerminalResult) -> float:
        """The voltage of the terminal's worst pad: its lowest when the load current flows
        out of the copper, its highest when it flows in (``drop_sign``)."""
        return t.v_max if self.sign < 0 else t.v_min

    def drop(self, t: TerminalResult) -> float:
        """Volts lost from ``v_ref`` at the terminal's worst pad: below it on a positive rail,
        toward 0 V on a negative one, the rise of a net at 0 V."""
        return self.sign * (self.v_ref - self.worst_v(t))

    def drop_at(self, v):
        """The drop (V) at voltages ``v`` (a number or an array): ``drop_sign · (v_ref − v)``."""
        return self.sign * (self.v_ref - np.asarray(v, dtype=float))

    def is_rise(self) -> bool:
        """Whether the drop is a ground's rise above 0 V (a ground group, or a net at 0 V
        solved alone)."""
        return self.kind == "ground" or (self.v_ref == 0 and self.drop_sign < 0)

    def is_shift(self) -> bool:
        """Whether a rise (:meth:`is_rise`) lies below 0 V: a ground under a negative rail.
        Decided once for the whole result, by the sign of ``max_drop``, its deviation from
        0 V with the largest magnitude over all its copper (on a ground group the signed
        rise :func:`coppernicus.problems._rise` finds on the mesh, and the number
        :func:`coppernicus.problems.max_drop_text` words): negative is a shift. Zero (nothing
        solved yet) stays a rise."""
        return self.is_rise() and self.max_drop < 0

    def drop_word(self) -> str:
        """What the readouts call the drop: "drop", "rise" (:meth:`is_rise`) or "shift"
        (:meth:`is_shift`)."""
        return "shift" if self.is_shift() else "rise" if self.is_rise() else "drop"

    def shown_drop_at(self, v):
        """The drop (V) at voltages ``v`` as the app's maps and readouts show it, measured the
        way :meth:`shown_drop_unit` says: :meth:`drop_at`, negated on a ground shift, where it
        is the depth below 0 V (a point above 0 V on it reads negative, as a boost does)."""
        drop = self.drop_at(v)
        return -drop if self.is_shift() else drop

    def shown_drop_unit(self) -> str:
        """The unit of :meth:`shown_drop_at`, for the app's maps and readouts (the side
        :func:`coppernicus.problems.drop_words` names): :meth:`drop_unit`, or "mV below 0 V
        (ground shift)" on a ground shift (:meth:`is_shift`)."""
        return "mV below 0 V (ground shift)" if self.is_shift() else self.drop_unit()

    def drop_unit(self) -> str:
        """What the drop is measured as, for map labels: "mV below 3.3 V", "mV above
        -3.3 V (toward 0 V)", "mV above 0 V (ground rise)"."""
        if self.is_rise():
            return "mV above 0 V (ground rise)"
        if self.sign < 0:
            return f"mV above {self.v_ref:g} V (toward 0 V)"
        return f"mV below {self.v_ref:g} V"

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


# ---------------------------------------------------------------------- board temperature

@dataclass
class TemperatureMap:
    """The board temperature on one copper layer: square grid cells, centres ``x_mm`` and
    ``y_mm`` (KiCad mm), one temperature each, NaN outside the board. A cell holds its
    layer's copper and laminate together (KiPI's layered heat model)."""
    layer_id: int
    layer_name: str
    x_mm: np.ndarray               # (nx,) cell centres
    y_mm: np.ndarray               # (ny,)
    t_c: np.ndarray                # (ny, nx) °C, NaN outside the board

    @property
    def cell_mm(self) -> float:
        return float(self.x_mm[1] - self.x_mm[0]) if len(self.x_mm) > 1 else 0.0

    @property
    def peak_c(self) -> float:
        return float(np.nanmax(self.t_c))

    def hottest(self) -> Tuple[float, float, float]:
        """``(x, y, °C)`` of the hottest cell."""
        i, j = np.unravel_index(np.nanargmax(self.t_c), self.t_c.shape)
        return float(self.x_mm[j]), float(self.y_mm[i]), float(self.t_c[i, j])

    def at(self, x: float, y: float) -> float:
        """The temperature of the cell holding (x, y); NaN off the grid or outside the board."""
        d = self.cell_mm
        if d <= 0:
            return float("nan")
        j = int(np.floor((x - self.x_mm[0]) / d + 0.5))
        i = int(np.floor((y - self.y_mm[0]) / d + 0.5))
        if not (0 <= i < len(self.y_mm) and 0 <= j < len(self.x_mm)):
            return float("nan")
        return float(self.t_c[i, j])


@dataclass
class LayerTemperature:
    """The hottest solved copper of one layer (KiPI's ``layer_temps`` row: the temperature
    read at the copper's mesh nodes), with the pad nearest to it, on any layer."""
    layer_name: str
    t_max_c: float
    rise_c: float                  # above the ambient
    x_mm: float
    y_mm: float
    group: str                     # the TreeResult.groups key of that copper
    passed: Optional[bool] = None  # against the user's max_temp_c / max_rise_c; None without
    pad_ref: str = ""              # the nearest pad: its part, its number, and the distance
    pad_number: str = ""           # from the point to its copper (0: on the pad)
    pad_distance_mm: Optional[float] = None


@dataclass
class BoardTemperature:
    """The board temperature of an electro-thermal power-tree solve: one map per copper
    layer, the hottest solved copper per layer (what is judged), the grid peak (shown only),
    how the solve settled, where the heat came from, and the load drops at the ambient
    (iteration 1) against the final ones."""
    maps: List[TemperatureMap]
    copper: List[LayerTemperature]
    ambient_c: float
    airflow_m_s: float
    h_w_m2k: float                 # convection per face (KiPI: 10 + 3.8·v W/m²K)
    grid_mm: float                 # the cell size used
    grid_nx: int
    grid_ny: int
    grid_cells: int                # cells inside the board
    iterations: int
    settled: Optional[bool]        # KiPI's rule on the last change; None after one iteration
    history: List[Dict[str, float]]    # per iteration: iteration, heat_w, t_max_c, dt_max_c
    heat_w: float
    heat_by_kind_w: Dict[str, float]
    peak_c: float                  # the hottest grid cell, any layer
    peak_x_mm: float
    peak_y_mm: float
    peak_layer: str
    peak_pad_ref: str = ""
    peak_pad_number: str = ""
    peak_pad_distance_mm: Optional[float] = None
    loads: List[Dict[str, Any]] = field(default_factory=list)   # name, group, drop_ambient_v, drop_v
    load_heat: bool = False
    regulator_heat: str = "output_pads"
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def peak_rise_c(self) -> float:
        return self.peak_c - self.ambient_c

    def map(self, layer_name: str) -> Optional[TemperatureMap]:
        return next((m for m in self.maps if m.layer_name == layer_name), None)


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
    # The board temperature, when the tree was solved electro-thermally; else None.
    thermal: Optional[BoardTemperature] = None
    # The project's board-temperature limits (max_temp_c / max_rise_c, only those set; None
    # when none is): judged on ``thermal``, or listed as not judged when it was not solved.
    thermal_limits: Optional[Dict[str, float]] = None
    # net -> project Limits, for every solved net the project has a rail for (a group's
    # key is its driving net; see coppernicus.power_tree.net_limits)
    limits: Dict[str, Any] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, float] = field(default_factory=dict)

    def group_of(self, net: str) -> Optional[str]:
        """The key of the group ``net`` was solved in, or None."""
        return next((k for k, r in self.groups.items() if net in r.nets), None)
