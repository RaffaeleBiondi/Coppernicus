"""Results of a DC solve, as plain arrays: what the viewer, the maps and the reports read.

Nothing here depends on the solver engine; :mod:`coppernicus.engine` fills these in.
Coordinates are millimetres in KiCad's frame (x right, **y down**).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# The resolution of the input: KiPI nodes the board geometry on a 1 nm grid before meshing
# (NODING_GRID_MM = 1e-6 mm, kipi/core/mesh/mesher.py:83 in KiPI 1.0.1). A mesh triangle
# with an edge shorter than that is a degenerate triangle: Steiner points the mesher added
# below the input's resolution. Its |J| is set by the last bits of the node voltages, so
# every reported maximum or peak |J| leaves it out (docs/technical.md#current-density-default).
# Not a physical threshold.
GEOMETRY_RESOLUTION_MM = 1e-6
# An edge of exactly one grid step computes a few 1e-15 mm short of it at board coordinates
# (123.45679 - 123.456789 = 9.99999997e-07): this slack, far above that round-off and far
# below the grid, keeps it resolved.
_ROUND_OFF_MM = 1e-12


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
    # The copper thickness as solved (the board's stackup, or the copper_thickness_mm what-if)
    # and whether the layer is an outer one (top or bottom copper): the current-density
    # default (coppernicus.problems.density_default) needs both. None: not known (a layer
    # built by hand), so no default applies.
    thickness_mm: Optional[float] = None
    outer: Optional[bool] = None
    # The net of each node, aligned with ``points``: an (N,) object array of net names (str).
    # Set on power-tree groups (every node gets one: coppernicus.engine.tree.node_nets), where
    # nets joined by links or net ties are solved as one mesh; None elsewhere (one net, or a
    # rail and the nets its net ties join, solved alone).
    node_net: Optional[np.ndarray] = None

    @property
    def power(self) -> float:
        return float(np.nansum(self.tri_power))

    def centroids(self) -> np.ndarray:
        return self.points[self.tris].mean(axis=1)

    def resolved(self) -> np.ndarray:
        """(M,) bool: the triangles whose every edge is at least
        :data:`GEOMETRY_RESOLUTION_MM` long. The others are degenerate: left out of every
        maximum or peak |J|, kept in the solution, the copper loss and the areas."""
        short = (GEOMETRY_RESOLUTION_MM - _ROUND_OFF_MM) ** 2
        ok = np.ones(len(self.tris), dtype=bool)
        for a, b in ((0, 1), (1, 2), (2, 0)):
            d = self.points[self.tris[:, a]] - self.points[self.tris[:, b]]
            ok &= np.einsum("ij,ij->i", d, d) >= short
        return ok


def degenerate_triangles(result: "RailResult") -> List[Dict[str, Any]]:
    """Per layer with degenerate triangles (:meth:`LayerField.resolved`), in stack order:
    ``{"layer", "triangles", "j_max_a_mm2"}``, the largest |J| among them. Empty when
    there are none."""
    out = []
    for lf in result.layers:
        bad = ~lf.resolved()
        if bad.any():
            out.append({"layer": lf.name, "triangles": int(bad.sum()),
                        "j_max_a_mm2": float(lf.current_density[bad].max())})
    return out


def exclude_degenerate(result: "RailResult") -> Optional[str]:
    """Leave the degenerate triangles out of ``result.j_max`` (the largest |J| of the
    others) and add the warning that says so, once; return it. A result without any is left
    as it was (its j_max is KiPI's, bit for bit) and None is returned."""
    rows = degenerate_triangles(result)
    if not rows:
        return None
    kept = [lf.current_density[lf.resolved()] for lf in result.layers]
    result.j_max = max((float(j.max()) for j in kept if j.size), default=0.0)
    each = "; ".join(f"{r['layer']} {r['triangles']} triangle{'s' if r['triangles'] != 1 else ''}, "
                     f"largest |J| {r['j_max_a_mm2']:.3g} A/mm²" for r in rows)
    note = (f"{result.net}: degenerate mesh triangles (an edge shorter than 1 nm, the resolution "
            "of KiPI's geometry snap) left out of every maximum |J| (j_point_max_a_mm2, the "
            f"current-crowding spots and their peaks, the |J| colour scales): {each}. Their |J| "
            "is a numerical artefact of the mesh, set by the last bits of the node voltages; "
            "they stay in the solution, the copper loss and the areas.")
    if note not in result.warnings:
        result.warnings.append(note)
    return note


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
    # A power-tree group's per-net split (coppernicus.engine.tree): one row per net of
    # ``nets``, in that order; empty elsewhere. Shown, never judged.
    net_split: List["NetSplit"] = field(default_factory=list)
    # How the split was attributed when links in parallel or in a loop give several paths
    # between two nets ("" when every net is reached one way).
    split_note: str = ""

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

@dataclass
class NetSplit:
    """One net of a power-tree group as solved: its lowest and highest voltage (where they are),
    the point it is fed from and its copper loss. Shown, never judged.

    ``entry`` is where the net connects toward its group's driver: on a rail group the driving
    source's output pins (``entry_kind`` "source") or the side of the link it is reached
    through (``"link"``), first from the driver over links and net ties (the highest-current
    path when there are several, :attr:`RailResult.split_note`); on a ground group the pins of
    the source holding it at 0 V (``"reference"``), or the side of the link toward them.
    ``drop_v``: on a rail, V(entry) − its lowest voltage (toward 0 V on a negative rail: its
    highest − V(entry)); on a ground, its rise above V(entry), the one of largest magnitude,
    signed (max − V(entry), or min − V(entry) below it)."""
    net: str
    v_min_v: float                 # NaN: no node with a voltage (floating copper only)
    v_max_v: float
    at_min: Tuple[float, float, str]           # (x mm, y mm, layer name) of the lowest node
    at_max: Tuple[float, float, str]
    copper_loss_w: float           # its triangles
    via_loss_w: float              # its via and plated-hole barrels
    entry_kind: str = ""           # "source", "link", "reference"; "" when not reached
    entry_name: str = ""           # the source, the link (or the sources holding the 0 V)
    entry_pads: List[str] = field(default_factory=list)     # "REF.PAD" labels
    entry_v: Optional[float] = None
    drop_v: Optional[float] = None


@dataclass
class Segment:
    """One part of a load's (or regulator input's) drop, from its group's driver to its worst
    pins (coppernicus.engine.tree). Positive toward 0 V, as the drop: a load's segments add up
    to its ``drop_v`` exactly. Shown, never judged.

    ``what``: "regulation" (nominal − (the driver's output − its ground)), "copper" (a net of
    the rail, from where the current enters it to where it leaves, the last one to the worst
    supply pin), "link" (the voltage across a link or net tie), "ground copper" and "ground
    link" (from the worst ground pin back to the driver's ground pins, along the ground's nets
    and links), or "ground" (the same in one step, when they are not on one ground group)."""
    what: str
    name: str                      # the driver, the net or the link
    frm: str                       # where it starts: "U1.2", "FB1.1", "3.3 V set"
    to: str                        # where it ends: "FB1.1", "U2.1", "U1.2 − U1.3"
    v: float                       # V


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
    # Its rated output current as the user wrote it (Source.current_limit_a: KiCad field
    # PI_IMAX or the project file; None: not given), where it was written ("" when not known:
    # coppernicus.power_tree.rating_origins names it), and |I out| within it (None: no rating,
    # or not judged; coppernicus.power_tree.judge_ratings).
    current_limit_a: Optional[float] = None
    current_limit_from: str = ""
    current_passed: Optional[bool] = None
    # Its input drop (input_drop_v) split along the rail feeding it (Segment); empty without one.
    segments: List[Segment] = field(default_factory=list)


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
    net: str = ""                  # its supply net: the net its supply pins are on
    rail: str = ""                 # the rail it is listed in: another net when a net tie joins them
    # Both worst pins, KiPI's v_low / v_high (Task 11; None where KiPI gives none): the lowest
    # supply pin minus the highest ground pin, and the highest supply pin minus the lowest
    # ground pin. v_worst_v is one of them, the side toward 0 V.
    v_low_v: Optional[float] = None
    v_high_v: Optional[float] = None
    # Its drop (drop_v) split from its rail's driver to its worst pins (Segment); empty when
    # it has none (disconnected).
    segments: List[Segment] = field(default_factory=list)


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
    # A rated source (Source.current_limit_a) on a rail the tree does not solve as a rail
    # (excluded, or solved as a ground): not-judged entries (coppernicus.power_tree.apply_limits).
    unsolved_ratings: List[Dict[str, str]] = field(default_factory=list)
    # KiPI's outer (ground-sense) iteration over the rails and grounds settled; a group's own
    # iteration is RailResult.converged (coppernicus.engine.tree.outer_converged).
    converged: bool = True
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, float] = field(default_factory=dict)

    def group_of(self, net: str) -> Optional[str]:
        """The key of the group ``net`` was solved in, or None."""
        return next((k for k, r in self.groups.items() if net in r.nets), None)


# ---------------------------------------------------------------------- AC impedance (Task 9)

@dataclass
class TransientResult:
    """KiPI's estimated response at a probe to its load step (``kipi/ac/transient.py``): the
    open-loop |Z| with the source put back as a typical PI loop crossing over at the
    bandwidth the user gave (zero at w_c/4, double pole at 8·w_c, about 60° of phase margin
    into a capacitive PDN; not the converter's own). Shown, never judged (Task 11)."""
    bandwidth_hz: float            # as given (ac.bandwidth_hz); never the 50 kHz placeholder
    step_a: float
    slew_a_per_s: float
    t_rise_s: float
    undershoot_v: float            # the largest dip below the settled voltage (positive)
    overshoot_v: float             # the largest rise above it (positive)
    undershoot_bw_v: float         # the same through a single pole at 20 MHz (a scope's limit)
    overshoot_bw_v: float
    t_s: np.ndarray
    i_a: np.ndarray                # the load current: the step up, then back down
    v: np.ndarray                  # the rail's deviation from its DC value, V
    v_bw: np.ndarray               # the same through 20 MHz
    notes: List[str] = field(default_factory=list)      # KiPI's
    edge_l_h: Optional[float] = None   # the inductance the edge sees; None: capacitive there
    edge_v: float = 0.0            # the largest dip while the load ramps up
    load_line_ohm: float = 0.0


@dataclass
class ProbeImpedance:
    """The PDN impedance at one probe after an AC solve, its target where one applies and
    its verdict (only against a target the user gave, or computed from numbers they gave)."""
    name: str
    ref: str
    pins: Optional[List[str]]      # None: every pin of the part on ``net``
    net: str
    ground_net: str
    z_ohm: np.ndarray              # complex Ω at ImpedanceResult.freq_hz
    target_ohm: Optional[float] = None         # flat target (entered, or computed)
    target_hz: Optional[float] = None          # ... up to here (None: the whole sweep)
    target_points: List[Tuple[float, float]] = field(default_factory=list)   # a mask (Hz, Ω)
    # The target at each frequency, NaN where none applies (below the bandwidth, above
    # target_hz); None without a target.
    target_curve_ohm: Optional[np.ndarray] = None
    target_note: str = ""                      # where the target comes from
    judged_from_hz: Optional[float] = None     # the bandwidth above which it is judged
    # KiPI's judged_from note when no frequency is left between the bandwidth and the end of
    # the target: KiPI would then judge the open-loop rise; Coppernicus does not judge.
    fallback_note: str = ""
    worst_z_ohm: Optional[float] = None        # |Z| at the worst point
    worst_hz: Optional[float] = None
    worst_ratio: Optional[float] = None        # |Z| / target there (None without a target)
    passed: Optional[bool] = None              # None: no target, or not judged
    not_judged: str = ""                       # why a target was not judged
    step_a: Optional[float] = None
    slew_a_per_us: Optional[float] = None
    # KiPI's transient estimate (Task 11): only with a step, a slew rate and a bandwidth given.
    transient: Optional[TransientResult] = None

    @property
    def mag_ohm(self) -> np.ndarray:
        return np.abs(self.z_ohm)

    @property
    def phase_deg(self) -> np.ndarray:
        return np.degrees(np.angle(self.z_ohm))

    @property
    def has_target(self) -> bool:
        return bool(self.target_points or self.target_ohm)

    @property
    def limit(self) -> str:
        """The key of the target judged: "target_points" (a mask) or "target_ohm"."""
        return "target_points" if self.target_points else "target_ohm"


@dataclass
class ImpedanceResult:
    """One rail's open-loop PDN impedance |Z|(f) at its probes (KiPI ``kipi/ac``)."""
    rail: str                      # the net its source drives
    rail_nets: List[str]           # every net solved as the rail (joined by links)
    ground: str                    # the ground's nets, " + " between them
    source: str                    # the source left out of the AC model
    # The source's control loop bandwidth as the user gave it (ac.bandwidth_hz); None when
    # not given: then ``assumed_bandwidth_hz`` only marks the band below it, not judged (|Z|
    # does not depend on it: KiPI's AC model has no regulator impedance).
    bandwidth_hz: Optional[float]
    freq_hz: np.ndarray
    field_freq_hz: np.ndarray      # where the copper was solved (interpolated between)
    probes: List[ProbeImpedance] = field(default_factory=list)
    plane_capacitance_f: float = 0.0
    caps: List[str] = field(default_factory=list)           # capacitors in the model
    missing_caps: List[str] = field(default_factory=list)   # rail-to-ground, no model
    # Of those, the ones whose KiCad fields give no model: ref -> what they lack ("C1: PI_ESR missing").
    cap_gaps: Dict[str, str] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)          # links in the model
    cap_derated_f: Dict[str, float] = field(default_factory=dict)   # ref -> derated farads
    load_line_ohm: float = 0.0     # not exposed yet: no load line
    stats: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    assumed_bandwidth_hz: Optional[float] = None   # a placeholder when none is given
    dielectric_assumed: str = ""   # why the dielectric εr is assumed; "" when the board gives it
    # Of ``warnings``, those of Task 11's Q9 (a load's AC tolerance against its probe's
    # ac_tolerance_v): a tolerance changes no |Z|, so the app shows them for the values now.
    ac_tolerance_warnings: List[str] = field(default_factory=list)

    @property
    def bandwidth_assumed(self) -> bool:
        return self.bandwidth_hz is None

    @property
    def open_loop_hz(self) -> Optional[float]:
        """The frequency below which the curve is open loop: the bandwidth, given or assumed."""
        return self.bandwidth_hz if self.bandwidth_hz is not None else self.assumed_bandwidth_hz

    def probe(self, name: str) -> Optional[ProbeImpedance]:
        return next((p for p in self.probes if p.name == name), None)


# ---------------------------------------------------------------------- DC-DC noise (Task 10)

@dataclass
class HotLoop:
    """One phase's hot loop as KiPI traced it: its inductance, the switch node's capacitance
    (Coss + copper), where it rings and how damped. ``coss_entered`` / ``qrr_entered``:
    False where KiPI's stand-ins (200 pF, 5 nC) were used."""
    phase: int
    loop_l_h: float
    package_l_h: float
    c_sw_f: float
    coss_f: float
    coss_entered: bool
    qrr_nc: float
    qrr_entered: bool
    c_copper_f: float
    r_loop_ohm: float
    f_ring_hz: Optional[float]     # None: no hot-loop inductance, no ringing
    zeta: float                    # at least the aggressor's min_ring_zeta
    zeta_loop: float               # what the loop's resistance alone gives
    traced: bool                   # through the copper (True) or straight between the pads
    image_plane: str = ""


@dataclass
class RippleResult:
    """The ripple an aggressor puts between a pair of pins over one switching period: at a
    probe, or at phase 1's device pins ("input", "output"). Never judged in Task 10. The four
    peaks are KiPI's tolerance values (``kipi/tolerance/gather.py`` ``ripple_peaks``): the
    lowest and highest points from the mean, full band and through 20 MHz."""
    name: str
    where: str                     # "probe", "device pins", "capacitor bank" (no board model)
    net: str
    ground_net: str
    ref: str
    pins: Optional[List[str]]
    below_v: float
    above_v: float
    below_bw_v: float
    above_bw_v: float
    pp_v: float
    pp_bw_v: float                 # through a single pole at 20 MHz (a scope's limit)
    rms_v: float
    t_s: np.ndarray                # one period
    v: np.ndarray                  # V about the mean
    v_bw: np.ndarray


@dataclass
class VictimResult:
    """The noise one aggressor couples onto a victim net, at its receiver, and its verdict:
    only against a margin the user gave (``margin_from``), and only when no KiPI stand-in is
    in play (``stand_ins``)."""
    name: str
    net: str
    driver: str                    # pin labels as KiPI shows them
    receiver: str
    ends_entered: bool
    preset: str
    margin_v: Optional[float]      # None: no margin set, ranked only
    margin_from: str               # where the margin comes from ("" without one)
    limit: str                     # the limit's name in a verdict entry ("" without one)
    peak_v: float                  # the receiver's largest |v| over a period, held high or low
    peak_by_v: Dict[str, float]    # capacitive, inductive, common (those solved)
    dominant: str
    ratio: Optional[float]         # peak / margin (None without a margin)
    states: List[Dict[str, Any]]   # name, level_v, threshold_v, driver_ohm, excursion_v, margin_v
    length_mm: float
    coupled_mm: float
    cm_f: Dict[str, float]         # switch net -> mutual capacitance
    m_h: Dict[str, float]          # loop -> mutual inductance (at fsw)
    splits: List[str]              # plane-split crossings, described
    receiver_v: np.ndarray         # one period, driven high, all mechanisms
    stand_ins: List[str] = field(default_factory=list)
    passed: Optional[bool] = None
    not_judged: str = ""
    warnings: List[str] = field(default_factory=list)
    # Why a logic_v the user gave sets no margin (off KiPI's JESD8 table): not judged.
    margin_note: str = ""


@dataclass
class FieldMap:
    """Each point's strongest harmonic (peak amplitude) on a plane over the board:
    ``values`` (ny, nx), rows from y0 (KiCad's y points down), over ``bounds`` (x0, y0, x1,
    y1) mm, the centres of the first and last points."""
    kind: str                      # "H" or "E"
    unit: str                      # "A/m" or "V/m"
    values: np.ndarray
    bounds: Tuple[float, float, float, float]


@dataclass
class NearField:
    """The near field over the top copper and the radiated estimate (KiPI ``kipi/noise/
    fields.py``): an indicator, never judged."""
    height_mm: float
    distance_m: float
    h_map: Optional[FieldMap] = None
    e_map: Optional[FieldMap] = None
    # kind, x_mm, y_mm, value, unit, f_hz, source; the strongest first, per kind
    hotspots: List[Dict[str, Any]] = field(default_factory=list)
    radiated_dbuv_m: Optional[float] = None    # the harmonic nearest the CISPR 32 B limit
    radiated_hz: Optional[float] = None
    cispr32_b_dbuv_m: Optional[float] = None   # the limit there (None below 30 MHz)
    radiated_source: str = ""
    notes: List[str] = field(default_factory=list)


@dataclass
class NoiseResult:
    """One aggressor's ripple and coupled noise (KiPI ``kipi/noise``)."""
    aggressor: str
    vrm: str
    rail: str                      # the output net
    input_net: str
    ground: str
    topology: str
    vin_v: float
    vout_v: float
    iout_a: float
    iin_a: float
    duty: float
    efficiency: float
    phases: int
    operating_notes: List[str]
    fsw_hz: float
    rise_s: float
    fall_s: float
    f_max_hz: float
    harmonics: int
    harmonics_capped: bool
    inductor_ripple_a: List[float]
    hot_loops: List[HotLoop]
    t_s: np.ndarray                # one period
    switch_node_v: List[np.ndarray]    # per phase, one period
    inductor_a: List[np.ndarray]
    edges: List[str]               # per phase, how each edge switches
    ripple: List[RippleResult] = field(default_factory=list)
    victims: List[VictimResult] = field(default_factory=list)
    # used, note, unknowns, field_solves, ports, caps_modelled, caps_missing
    board: Dict[str, Any] = field(default_factory=dict)
    emi: Optional[NearField] = None
    stand_ins: List[str] = field(default_factory=list)   # the aggressor's own (every victim)
    partial: bool = False          # a partial result handed over during the solve
    verdict: str = ""
    verdict_note: str = ""
    failed: List[Dict[str, Any]] = field(default_factory=list)
    not_judged: List[Dict[str, Any]] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def victim(self, name: str) -> Optional[VictimResult]:
        return next((v for v in self.victims if v.name == name), None)

    def ripple_at(self, name: str) -> Optional[RippleResult]:
        return next((r for r in self.ripple if r.name == name), None)


# ---------------------------------------------------------------------- tolerance stack-up (Task 11)

TOLERANCE_PARTS = ("dc", "accuracy", "ripple", "transient")
TOLERANCE_CHECKS = ("dc", "ac", "total")          # KiPI's CHECK_PARTS order (evaluate.py:23)

@dataclass
class TolerancePart:
    """One part of a load's stack-up: how far it takes the load below and above nominal (V,
    never negative), where it came from and the numbers behind it."""
    below_v: float
    above_v: float
    source: str                    # "power tree", "entered (transient_mv)", "accuracy_pct of U1", …
    detail: str = ""


@dataclass
class ToleranceCheck:
    """One check of a load's stack-up (KiPI ``kipi/tolerance/evaluate.py``): the usable parts it
    adds, each side apart, against the allowed deviation. ``passed``: False when a side is
    over (+1e-12 V), even while parts are missing; True only when every part is known; None
    otherwise (or without a tolerance)."""
    kind: str                      # "dc", "ac", "total"
    below_v: float
    above_v: float
    allowed_below_v: Optional[float]
    allowed_above_v: Optional[float]
    complete: bool
    passed: Optional[bool]
    missing: List[str] = field(default_factory=list)   # the parts not known (MISSING)
    # Missing parts whose least value is known (an entered ripple_mv, the usable aggressors, a
    # DC drop not settled thermally): added into below_v / above_v for the FAIL test only.
    floors: List[str] = field(default_factory=list)
    limit: str = ""                # the key set ("tol_pct", "dc_tol_mv", …); "" without one
    reason: str = ""               # why it is not judged ("" when judged or without a limit)

    @property
    def has_limit(self) -> bool:
        return self.allowed_below_v is not None or self.allowed_above_v is not None

    @property
    def margin_below_v(self) -> Optional[float]:
        """Volts to spare below (negative: over); None where not limited."""
        return None if self.allowed_below_v is None else self.allowed_below_v - self.below_v

    @property
    def margin_above_v(self) -> Optional[float]:
        return None if self.allowed_above_v is None else self.allowed_above_v - self.above_v


@dataclass
class ToleranceRow:
    """One load's stack-up: its four parts (None: missing, with a hint saying what to do), the
    three checks against the tolerances set, and its verdict ("" without any tolerance)."""
    load: str                      # its label
    name: str                      # its engine name (TreeResult.loads)
    net: str                       # its supply net
    rail: str                      # the rail it is listed in
    nominal_v: Optional[float]     # the set voltage of the source driving its rail
    source: str                    # that source's label ("" without one)
    probe: str                     # its probe (KiPI's rule; "" without one)
    # dc, accuracy, ripple, transient (None: missing); part -> what to do; dc, ac, total
    parts: Dict[str, Optional[TolerancePart]] = field(default_factory=dict)
    hints: Dict[str, str] = field(default_factory=dict)
    checks: Dict[str, ToleranceCheck] = field(default_factory=dict)
    verdict: str = ""              # "pass", "fail", "not_judged"; "" without any tolerance
    stand_ins: List[str] = field(default_factory=list)   # KiPI stand-ins behind a part not used
    # A missing part's least value, when known: it can make a check fail, never pass.
    floors: Dict[str, TolerancePart] = field(default_factory=dict)
    ripple_from: List[str] = field(default_factory=list)   # the aggressors whose ripple is used
    # KiPI's transient estimate at its probe, shown, never judged: (below, above, the same
    # through 20 MHz) V, already swapped on a negative rail; None without one.
    transient_estimate_v: Optional[Tuple[float, float, float, float]] = None


@dataclass
class ToleranceResult:
    """Every load's stack-up (KiPI ``kipi/tolerance``) and the verdict over them: only against
    the tolerances the user set, and only with every part of a check known."""
    rows: List[ToleranceRow] = field(default_factory=list)
    verdict: str = "not_judged"
    verdict_note: str = ""
    failed: List[Dict[str, Any]] = field(default_factory=list)       # {item, net, limit, reason}
    not_judged: List[Dict[str, Any]] = field(default_factory=list)
    options: Dict[str, Any] = field(default_factory=dict)            # ripple_sum, bw_limit_20mhz
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    estimates: List[Dict[str, Any]] = field(default_factory=list)    # what each solve was to cost
    stats: Dict[str, Any] = field(default_factory=dict)

    def row(self, load: str) -> Optional[ToleranceRow]:
        return next((r for r in self.rows if load in (r.load, r.name)), None)
