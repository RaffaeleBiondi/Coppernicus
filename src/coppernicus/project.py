"""The project file: which board, which rails, their sources, loads and limits.

Engine-independent plain dataclasses, saved as YAML (``*.coppernicus.yml``). The board path
is stored relative to the project file so a project folder can be moved as a whole.

A *rail* is one power net. Its *sources* are parts whose pins are held at a fixed voltage
(a regulator output); its *loads* are parts whose pins draw a current, split equally across
the pins (that is what the engine solves). Pins are given as a list of pad numbers, glob
patterns (``"A*"``), or ``None`` / ``"all"`` for every pin of the part on the rail's net.

Version 2 adds what a whole power tree needs (every field optional, omitted from the file
while at its default, so files written before it load and save unchanged):

* a source's ``kind`` (``"source"``, ``"ldo"``, ``"smps"``), ``efficiency`` (a fraction,
  switchers) and ``quiescent_a``, and the pins of its ``input``, ``ground``, ``sense`` and
  ``ground_sense``;
* a load's ``ground`` pins, where its current returns;
* ``links``: parts that join two nets with a resistance (ferrites, sense resistors, fuses);
* ``engine_options``: an engine-specific passthrough, checked by the engine.

A pin reference is saved as a mapping ``{ref, net, pins}``; ``ref`` is omitted when it is the
terminal's own part. See ``docs/engine-mapping.md`` for how the engine reads these fields.

The AC impedance solve (Task 9) adds ``probes`` (where |Z|(f) is looked into, with an optional
target), ``capacitors`` (RLC models of the parts) and ``ac`` (the sweep, each source's control
loop bandwidth, each link's series inductance), each omitted while empty or at its default.

The DC-DC noise solve (Task 10) adds ``aggressors`` (switchers: switching frequency, edges,
phases, ripple probes), ``victims`` (signal nets, their driver, receiver, type and noise
margin) and ``noise`` (the solve's options), each omitted while empty or at its default.

The tolerance stack-up (Task 11) adds a load's tolerances (``tol_pct``, ``dc_tol_pct``,
``ac_tol_pct``, or the same in mV from nominal as ``_mv``) and entered parts (``ripple_mv``,
``transient_mv``), a source's DC accuracy (``accuracy_pct`` or ``accuracy_mv``), each a number
(± that) or a signed ``[below, above]`` as datasheets write it, and ``tolerance`` (how ripple
adds, full band or 20 MHz), each omitted while unset or at its default.
"""

from __future__ import annotations

import dataclasses
import math
import os
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypeVar

import yaml

PinSpec = Optional[List[str]]

SOURCE_KINDS = ("source", "ldo", "smps")     # ideal supply, linear regulator, switcher
# A source's pin references (input, ground and sense pins) and a load's.
SOURCE_PIN_REFS = ("input", "ground", "sense", "ground_sense")
LOAD_PIN_REFS = ("ground",)
# Task 11: a load's tolerances (each a % of nominal or mV from it, never both), its entered
# parts, a source's DC accuracy; and how several aggressors' ripple adds.
LOAD_TOLERANCE_KEYS = ("tol_pct", "tol_mv", "dc_tol_pct", "dc_tol_mv", "ac_tol_pct", "ac_tol_mv")
LOAD_PART_KEYS = ("ripple_mv", "transient_mv")
SOURCE_ACCURACY_KEYS = ("accuracy_pct", "accuracy_mv")
RIPPLE_SUMS = ("linear", "rss")


class ProjectError(ValueError):
    """The project file is malformed."""


def tolerance_pair(value: Any, what: str) -> Optional[Tuple[float, float]]:
    """A tolerance, an accuracy or an entered part as ``(below, above)``, signed as datasheets
    write it: a number ``n`` is ± n, ``(-n, n)``; a pair ``[below, above]`` needs below ≤ 0 and
    above ≥ 0 (Coppernicus's rule; KiPI has none). 0 is a value; None stays None. Raises
    :class:`ProjectError` naming ``what``."""
    if value is None:
        return None
    shape = f"{what} must be a number (± that) or [below, above], not {value!r}"

    def number(x) -> float:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ProjectError(shape)
        if not math.isfinite(x):
            raise ProjectError(f"{what} must be finite, not {value!r}")
        return float(x)
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise ProjectError(shape)
        below, above = number(value[0]), number(value[1])
    else:
        x = number(value)
        if x < 0:
            raise ProjectError(f"{what}: one number is ± that, so it cannot be negative; give "
                               f"[below, above] (e.g. [{x:g}, 0])")
        below, above = -x, x
    if below > 0:
        raise ProjectError(f"{what}: below ({below:g}) must be ≤ 0 and above ≥ 0, as datasheets "
                           "write −5 / +3: [-5, 3]")
    if above < 0:
        raise ProjectError(f"{what}: above ({above:g}) must be ≥ 0 and below ≤ 0, as datasheets "
                           "write −5 / +3: [-5, 3]")
    return below, above


def _check_tolerances(terminal, keys: Sequence[str], owner: str) -> None:
    """Each of ``keys`` on ``terminal`` as a ``(below, above)`` pair (:func:`tolerance_pair`),
    and never a ``_pct`` with its ``_mv``."""
    for key in keys:
        setattr(terminal, key, tolerance_pair(getattr(terminal, key), f"{owner}: {key}"))
    for key in keys:
        if key.endswith("_pct"):
            mv = key[:-len("_pct")] + "_mv"
            if mv in keys and getattr(terminal, key) is not None and getattr(terminal, mv) is not None:
                raise ProjectError(f"{owner}: {key} and {mv} are both set: give one of them (a % of "
                                   "nominal, or mV from nominal)")


def _pair_out(pair: Tuple[float, float]):
    """A pair as written to the file: one number when symmetric (± that), else [below, above]."""
    below, above = pair
    return above if below == -above else [below, above]


def parse_pins(value: Any) -> PinSpec:
    """``None``/``"all"``/``"*"`` → every pin; ``"1, 2"`` or ``[1, "2"]`` → a list of strings."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("", "all", "*"):
            return None
        return [p.strip() for p in text.split(",") if p.strip()]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, (list, tuple)):
        pins = [str(p).strip() for p in value if str(p).strip()]
        return pins or None
    raise ProjectError(f"pins must be 'all', a comma-separated string or a list, not {value!r}")


def pins_text(pins: PinSpec) -> str:
    return "all" if pins is None else ", ".join(pins)


@dataclass
class Source:
    """Pins of ``ref`` held at ``voltage`` (a regulator output, a connector supply).

    For a power tree: an ``"ldo"`` or ``"smps"`` draws its input current from the pins of
    ``input`` (LDO: I_out + I_q; switcher: V_out·I_out / (efficiency·V_in) + I_q). Pin
    references (``input_*``, ``ground_*``, ``sense_*``, ``ground_sense_*``) are set when their
    ``*_net`` is; an empty ``*_ref`` means this source's own part."""
    ref: str
    voltage: float
    pins: PinSpec = None
    name: str = ""
    kind: str = "source"
    efficiency: Optional[float] = None       # fraction 0–1, switchers only
    quiescent_a: float = 0.0
    input_ref: str = ""
    input_pins: PinSpec = None
    input_net: str = ""
    ground_ref: str = ""
    ground_pins: PinSpec = None
    ground_net: str = ""
    # Remote sense: the voltage is regulated at these pins (above ground_sense, if set).
    sense_ref: str = ""
    sense_pins: PinSpec = None
    sense_net: str = ""
    ground_sense_ref: str = ""
    ground_sense_pins: PinSpec = None
    ground_sense_net: str = ""
    # Part data (neither changes a DC solve): the rated output current (KiCad field PI_IMAX;
    # shown next to I_out, and judged in the power tree: coppernicus.power_tree.judge_ratings)
    # and a switcher's fixed switching frequency (PI_FSW; the switching frequency of a noise
    # aggressor on this source that gives no fsw_hz of its own: engine.noise._source_fsw).
    current_limit_a: Optional[float] = None
    fsw_hz: Optional[float] = None
    # More pads held at the same voltage, on other parts: one terminal, not parallel sources
    # (a battery of cell holders in parallel). "REF.PAD" entries, the pad may be a glob
    # ("BT5.*": every pad of BT5 on the rail). None: the source's own pads only.
    extra_pins: Optional[List[str]] = None
    # The ground's twin: more pads of its ground terminal, on other parts of ``ground_net``
    # (a battery's negative pole on several cell holders), all held at the ground's 0 V
    # reference with its ground pins in the power tree. "REF.PAD" entries, the pad may be a
    # glob. None: its ground pins only (on ``ground_ref``).
    extra_ground_pins: Optional[List[str]] = None
    # Task 11: the DC accuracy of its set voltage, as (below, above) in % of it or in mV from
    # it (one of the two; None: not entered, and the tolerance stack-up is then not judged).
    accuracy_pct: Optional[Tuple[float, float]] = None
    accuracy_mv: Optional[Tuple[float, float]] = None

    def __post_init__(self):
        _check_tolerances(self, SOURCE_ACCURACY_KEYS, f"source {self.ref}")

    @property
    def label(self) -> str:
        return self.name or self.ref


@dataclass
class Load:
    """Pins of ``ref`` drawing ``current`` amps in total, split equally across the pins;
    with ``ground_net`` set, the current returns through those ground pins.

    Task 11: its tolerances, each (below, above) in % of nominal (the set voltage of the source
    driving its rail) or in mV from it, never both: ``tol_*`` the whole band, ``dc_tol_*`` its
    DC share, ``ac_tol_*`` its AC share; and parts entered from a datasheet or a measurement,
    ``ripple_mv`` and ``transient_mv``, (below, above) mV from nominal. None: not set."""
    ref: str
    current: float
    pins: PinSpec = None
    name: str = ""
    ground_ref: str = ""
    ground_pins: PinSpec = None
    ground_net: str = ""
    tol_pct: Optional[Tuple[float, float]] = None
    tol_mv: Optional[Tuple[float, float]] = None
    dc_tol_pct: Optional[Tuple[float, float]] = None
    dc_tol_mv: Optional[Tuple[float, float]] = None
    ac_tol_pct: Optional[Tuple[float, float]] = None
    ac_tol_mv: Optional[Tuple[float, float]] = None
    ripple_mv: Optional[Tuple[float, float]] = None
    transient_mv: Optional[Tuple[float, float]] = None

    def __post_init__(self):
        _check_tolerances(self, LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS, f"load {self.ref}")

    @property
    def label(self) -> str:
        return self.name or self.ref

    def has_tolerance(self) -> bool:
        """Whether any of its tolerances is set (else it is shown, never judged)."""
        return any(getattr(self, k) is not None for k in LOAD_TOLERANCE_KEYS)


@dataclass
class Link:
    """A part joining two nets with a resistance (ferrite, sense resistor, fuse): its pins
    ``pins_a`` on ``net_a`` and ``pins_b`` on ``net_b``. Nets joined by links are solved
    together as one group in a power-tree solve."""
    name: str
    ref: str
    pins_a: PinSpec
    net_a: str
    pins_b: PinSpec
    net_b: str
    resistance_ohm: float


def pin_ref(terminal, what: str) -> Optional[Tuple[str, PinSpec, str]]:
    """``(ref, pins, net)`` of a terminal's pin reference ``what`` (``"input"``, ``"ground"``,
    …), the part defaulting to the terminal's own; ``None`` when its net is not set."""
    net = getattr(terminal, f"{what}_net")
    if not net:
        return None
    return getattr(terminal, f"{what}_ref") or terminal.ref, getattr(terminal, f"{what}_pins"), net


T = TypeVar("T", Source, Load)


def carry_over(old: Sequence[T], new: Sequence[T],
               origins: Optional[Sequence[Optional[int]]] = None) -> List[T]:
    """``new`` terminals, from an editor that shows only name, ref, pins and the value, with
    every other field (a regulator's kind, input, ground and sense pins…) taken from the
    matching ``old`` terminal.

    With ``origins`` (for each new terminal, the index in ``old`` of the row it was edited
    from, ``None`` for a new row) the match is that row, as long as it is still the same
    part: a row keeps its own fields whatever happens to its pins, and gets them back when
    its part is picked again. Without it: the same part with the same pins, else the same
    part. A terminal with no match keeps the defaults."""
    unused = list(old)
    out = []
    for i, n in enumerate(new):
        if origins is not None:
            k = origins[i] if i < len(origins) else None
            match = old[k] if k is not None and 0 <= k < len(old) and old[k].ref == n.ref else None
        else:
            match = (next((o for o in unused if o.ref == n.ref and o.pins == n.pins), None)
                     or next((o for o in unused if o.ref == n.ref), None))
            if match is not None:
                unused.remove(match)
        if match is None:
            out.append(n)
            continue
        value = "voltage" if isinstance(n, Source) else "current"
        out.append(dataclasses.replace(match, ref=n.ref, pins=n.pins, name=n.name,
                                       **{value: getattr(n, value)}))
    return out


@dataclass
class Limits:
    """Pass/fail limits set by the user. ``None`` = not checked: problems are then ranked,
    never judged."""
    max_drop_pct: Optional[float] = None
    max_current_density: Optional[float] = None    # A/mm^2
    max_via_current: Optional[float] = None         # A
    # For a ground net: the largest rise above its 0 V reference, mV (power-tree ground
    # groups, and the net solved alone: the largest deviation from its source voltage).
    max_ground_rise_mv: Optional[float] = None


@dataclass
class Rail:
    net: str
    sources: List[Source] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    # Nets joined to this one by net-tie footprints are solved with it, the tie a short.
    follow_net_ties: bool = True
    # Use this rail even where the board's KiCad footprint fields define the net (which
    # otherwise win; see coppernicus.board_fields). Saved only when on.
    override_board_fields: bool = False


@dataclass
class Settings:
    max_edge_mm: float = 0.5                  # largest mesh edge on planes
    # None: the engine's default plating table (IPC-6012 Class 2 minimum average, by hole
    # type); a number: that plating for every plated hole.
    via_plating_um: Optional[float] = None
    copper_temp_c: float = 20.0               # resistivity is referenced to 20 °C
    # What-if copper thickness by layer name, mm (1 oz = 0.035). Empty: the board file's.
    # The board file is never changed; real changes of copper weight are made in KiCad.
    copper_thickness_mm: Dict[str, float] = field(default_factory=dict)
    # Advanced, the mesh near plated holes (KiPI's SolveSettings): the smallest element, at a
    # hole wall, mm, and how fast elements grow away from it (mm of edge per mm of distance;
    # 0 turns the grading off). None: KiPI's defaults (0.02 mm, 0.2). Saved only when set.
    min_edge_mm: Optional[float] = None
    grading: Optional[float] = None

    def __post_init__(self):
        self.copper_thickness_mm = check_copper_thickness(self.copper_thickness_mm)
        self.min_edge_mm, self.grading = check_mesh_near_holes(self.min_edge_mm, self.grading)


# Where a regulator's conversion loss (input − output power) heats the board: "output_pads",
# KiPI's own placement (its output pins); "part_pads", spread evenly over every copper pad of
# its part, all nets.
REGULATOR_HEAT = ("output_pads", "part_pads")


@dataclass
class ThermalOptions:
    """The board temperature: an electro-thermal power-tree solve (KiPI's co-simulation),
    which heats the copper with its own losses and solves it again at that temperature.
    Defaults are KiPI's (``kipi/dc/thermal/model.py`` ThermalSettings). The limits are the
    user's and are judged by Coppernicus (:mod:`coppernicus.temperature`); ``None`` = not
    checked. Saved as a ``thermal:`` block holding only the keys not at their default."""
    enabled: bool = False          # Ctrl+T, solve --tree and solve_tree also solve the temperature
    ambient_c: float = 25.0
    iterations: int = 3            # electro-thermal passes, at least 1
    airflow_m_s: float = 0.0
    grid_mm: float = 0.0           # square cell size; 0 = automatic (KiPI: ~250 cells across)
    load_heat: bool = False        # also the loads' own power, at their pins (KiPI: load_power)
    max_temp_c: Optional[float] = None
    max_rise_c: Optional[float] = None      # above ambient_c
    regulator_heat: str = "output_pads"     # see REGULATOR_HEAT

    def __post_init__(self):
        """Checked whether made by the app or loaded from a file, with the same messages:
        non-finite numbers, ``iterations`` not a whole number ≥ 1, a negative airflow or
        grid and an unknown ``regulator_heat`` raise :class:`ProjectError`."""
        for k in ("enabled", "load_heat"):
            if not isinstance(getattr(self, k), bool):
                raise ProjectError(f"thermal: {k} must be true or false, not {getattr(self, k)!r}")
        for k in ("ambient_c", "airflow_m_s", "grid_mm", "max_temp_c", "max_rise_c"):
            value = getattr(self, k)
            if value is None and k.startswith("max_"):
                continue
            if isinstance(value, bool):
                raise ProjectError(f"thermal: {k} must be a number, not {value!r}")
            try:
                x = _number(value, f"thermal: {k}")
            except ValueError as e:
                raise ProjectError(str(e)) from None
            if k in ("airflow_m_s", "grid_mm") and x < 0:
                raise ProjectError(f"thermal: {k} cannot be negative (grid_mm 0 = automatic)")
            setattr(self, k, x)
        n = self.iterations
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not float(n).is_integer() or n < 1:
            raise ProjectError(f"thermal: iterations must be a whole number, at least 1, not {n!r}")
        self.iterations = int(n)
        if self.regulator_heat not in REGULATOR_HEAT:
            raise ProjectError(f"thermal: regulator_heat must be one of {', '.join(REGULATOR_HEAT)}, "
                               f"not {self.regulator_heat!r}")


def _thermal_in(data: Any) -> ThermalOptions:
    """The ``thermal:`` block: unknown keys raise; the values are checked by
    :class:`ThermalOptions` itself."""
    if data is None:
        return ThermalOptions()
    if not isinstance(data, dict):
        raise ValueError(f"thermal must be a mapping of option names to values, not {data!r}")
    known = {f.name for f in dataclasses.fields(ThermalOptions)}
    unknown = [str(k) for k in data if k not in known]
    if unknown:
        raise ValueError(f"thermal: unknown option {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    return ThermalOptions(**{str(k): v for k, v in data.items()})


def _thermal_out(options: ThermalOptions) -> Dict[str, Any]:
    """The options not at their default, in field order (empty at the defaults)."""
    default = ThermalOptions()
    return {k: v for k, v in vars(options).items() if v != getattr(default, k)}


@dataclass
class ToleranceOptions:
    """The tolerance stack-up (``tolerance:`` in the project file; Task 11). ``ripple_sum``:
    how several aggressors' ripple at one probe adds, "linear" (the sum of their peaks per
    side: unsynchronised switchers drift through every relative phase, so their peaks line up
    at times) or "rss" (the root sum of squares, KiPI's rule, kipi/tolerance/gather.py:201, for
    such switchers on average). ``bw_limit_20mhz``: judge the ripple through a 20 MHz bandwidth
    limit, as many datasheets measure, instead of the full band (an entered transient has no
    band; KiPI's estimate is shown both ways, never judged). Saved as a block holding only the
    keys not at their default."""
    ripple_sum: str = "linear"
    bw_limit_20mhz: bool = False

    def __post_init__(self):
        if self.ripple_sum not in RIPPLE_SUMS:
            raise ProjectError(f"tolerance: ripple_sum must be one of {', '.join(RIPPLE_SUMS)}, "
                               f"not {self.ripple_sum!r}")
        if not isinstance(self.bw_limit_20mhz, bool):
            raise ProjectError(f"tolerance: bw_limit_20mhz must be true or false, not "
                               f"{self.bw_limit_20mhz!r}")


def _tolerance_in(data: Any) -> ToleranceOptions:
    """The ``tolerance:`` block: unknown keys raise; the values are checked by
    :class:`ToleranceOptions` itself."""
    if data is None:
        return ToleranceOptions()
    if not isinstance(data, dict):
        raise ValueError(f"tolerance must be a mapping of option names to values, not {data!r}")
    known = [f.name for f in dataclasses.fields(ToleranceOptions)]
    unknown = [str(k) for k in data if k not in known]
    if unknown:
        raise ValueError(f"tolerance: unknown option {', '.join(unknown)} (known: {', '.join(known)})")
    return ToleranceOptions(**{str(k): v for k, v in data.items()})


def _tolerance_out(options: ToleranceOptions) -> Dict[str, Any]:
    """The options not at their default (empty at the defaults)."""
    default = ToleranceOptions()
    return {k: v for k, v in vars(options).items() if v != getattr(default, k)}


# ---------------------------------------------------------------------- the power tree's 0 V view

REFERENCE_FORM = ("a reference is {ground_of: REF} (that part's ground pins, e.g. U1) or "
                  "{pads: [REF.PAD, ...]} (e.g. [R12.2, NT1.*]; the pad may be a glob)")


def reference_spec(value: Any, where: str = "tree: reference") -> Optional[Dict[str, Any]]:
    """Where a power tree's voltages are shown from (S2, a display shift only), checked and
    normalised: ``{"ground_of": "U1"}`` or ``{"pads": ["R12.2", "NT1.*"]}``; None for None
    (the solve's own 0 V). Raises ValueError naming ``where``: unknown keys, both or neither
    given, an empty part, a pad that is not REF.PAD (a bare net is refused: its copper is not
    one potential). Whether the part or pads exist is checked against the solved tree
    (:func:`coppernicus.engine.tree.tree_reference`)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping, not {value!r}: {REFERENCE_FORM}")
    unknown = [str(k) for k in value if k not in ("ground_of", "pads")]
    if unknown:
        raise ValueError(f"{where}: unknown key {', '.join(unknown)}: {REFERENCE_FORM}")
    given = [k for k in ("ground_of", "pads") if value.get(k) not in (None, "", [])]
    if len(given) != 1:
        raise ValueError(f"{where}: give {'one of ' if given else ''}ground_of or pads"
                         f"{', not both' if given else ''}: {REFERENCE_FORM}")
    if given == ["ground_of"]:
        ref = value["ground_of"]
        if not isinstance(ref, str) or not ref.strip() or "." in ref.strip():
            raise ValueError(f"{where}: ground_of is a part reference (e.g. U1), not {ref!r}; a pad "
                             "is given as pads: [REF.PAD]")
        return {"ground_of": ref.strip()}
    pads = value["pads"]
    pads = [pads] if isinstance(pads, str) else pads
    if not isinstance(pads, list):
        raise ValueError(f"{where}: pads is a list of REF.PAD entries (e.g. [R12.2, NT1.*]), not {pads!r}")
    out = []
    for e in pads:
        head, dot, pad = str(e).strip().partition(".")
        if not (head and dot and pad):
            raise ValueError(f"{where}: pads entry {e!r} is not REF.PAD (e.g. R12.2, or NT1.* for every "
                             "pad of NT1): a net is not one potential, its copper has many voltages; "
                             "give a pad on it, or ground_of: a part")
        out.append(f"{head}.{pad}")
    return {"pads": list(dict.fromkeys(out))}


def reference_from_text(text: str) -> Dict[str, Any]:
    """``--reference`` as a :func:`reference_spec`: a part reference ("U1": its ground pins),
    or pads as REF.PAD separated by commas ("R12.2,NT1.*"); "ground_of=U1" and
    "pads=R12.2,NT1.*" are accepted too. Raises ValueError saying the form."""
    text = str(text or "").strip()
    key, eq, rest = text.partition("=")
    if eq and key.strip() in ("ground_of", "pads"):
        key, text = key.strip(), rest.strip()
    else:
        key = "pads" if "." in text or "," in text else "ground_of"
    if not text:
        raise ValueError("--reference needs a part (U1: its ground pins) or pads (R12.2,NT1.*)")
    value = [p.strip() for p in text.split(",") if p.strip()] if key == "pads" else text
    return reference_spec({key: value}, "--reference")


def _tree_in(data: Any) -> Optional[Dict[str, Any]]:
    """The ``tree:`` block (the power tree's options): ``reference`` only, so far; unknown
    keys raise."""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"tree must be a mapping (tree: {{reference: ...}}), not {data!r}")
    unknown = [str(k) for k in data if k != "reference"]
    if unknown:
        raise ValueError(f"tree: unknown option {', '.join(unknown)} (known: reference)")
    return reference_spec(data.get("reference"))


# The owner's bound: a thicker value is surely µm or oz written as mm.
MAX_COPPER_MM = 1.0


def check_copper_thickness(values) -> Dict[str, float]:
    """``{layer name: thickness in mm}``, checked: each a finite number, > 0 and at most
    ``MAX_COPPER_MM``. Raises :class:`ProjectError` saying the unit."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ProjectError("copper_thickness_mm must map copper layer names to mm, "
                           f"e.g. {{B.Cu: 0.07}}, not {values!r}")
    out = {}
    for layer, mm in values.items():
        x = None if isinstance(mm, bool) else _float_or_none(mm)
        if x is None or not math.isfinite(x) or x <= 0 or x > MAX_COPPER_MM:
            raise ProjectError(f"copper_thickness_mm[{layer}] = {mm!r}: give the thickness in mm, "
                               f"> 0 and at most {MAX_COPPER_MM:g} mm (1 oz copper is 0.035, not 35)")
        out[str(layer)] = x
    return out


# The range of KiPI's own "Via grading" field (kipi/dc/gui/pi_panel.py, kipi/ac/gui/panel.py):
# 0 (off) to 2 mm of edge per mm of distance.
GRADING_RANGE = (0.0, 2.0)


def check_mesh_near_holes(min_edge_mm, grading) -> Tuple[Optional[float], Optional[float]]:
    """``min_edge_mm`` and ``grading`` checked, each None (KiPI's default) or a finite number:
    ``min_edge_mm`` > 0, ``grading`` within :data:`GRADING_RANGE`. Raises
    :class:`ProjectError` naming the key."""
    out = []
    for key, value, ok, rule in (
            ("min_edge_mm", min_edge_mm, lambda x: x > 0, "a size in mm, > 0 (KiPI's default 0.02)"),
            ("grading", grading, lambda x: GRADING_RANGE[0] <= x <= GRADING_RANGE[1],
             f"from {GRADING_RANGE[0]:g} (off) to {GRADING_RANGE[1]:g} mm of edge per mm of "
             "distance (KiPI's default 0.2)")):
        if value is None:
            out.append(None)
            continue
        x = None if isinstance(value, bool) else _float_or_none(value)
        if x is None or not math.isfinite(x) or not ok(x):
            raise ProjectError(f"settings {key} = {value!r}: give {rule}, or leave it out")
        out.append(x)
    return out[0], out[1]


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- AC (Task 9)

@dataclass
class Probe:
    """Where the PDN impedance |Z|(f) is looked into (AC solve): the supply pins ``pins`` of
    part ``ref`` on ``net``, against its ground pins (``ground_*``; an empty ``ground_ref``
    is ``ref``). A target is optional: ``target_ohm`` flat up to ``target_hz`` (the whole
    sweep if unset), or a mask ``target_points`` (``(Hz, Ω)`` corners joined on the log-log
    plot, applying only between its first and last points: nothing is judged outside them),
    or, with ``target_ohm`` empty, one computed from ``ac_tolerance_v``, ``step_a`` and
    ``slew_a_per_us`` (KiPI: Z = V_ac/I_step up to 1/(π·t_rise), t_rise = I_step/slew).
    Without a target the probe is ranked, never judged."""
    name: str
    ref: str
    net: str
    pins: PinSpec = None
    ground_ref: str = ""
    ground_pins: PinSpec = None
    ground_net: str = ""
    target_ohm: Optional[float] = None
    target_hz: Optional[float] = None
    target_points: List[Tuple[float, float]] = field(default_factory=list)
    ac_tolerance_v: Optional[float] = None
    step_a: Optional[float] = None
    slew_a_per_us: Optional[float] = None


@dataclass
class CapModel:
    """A capacitor model (ESR + jω·ESL + 1/(jω·C)) shared by the parts ``refs``. All three
    values are needed (> 0) before an AC solve uses it; 0 = not entered, there is no default.
    With ``rated_v`` the capacitance is derated for DC bias (``kept_at_rated_pct`` kept at
    the rated voltage; empty: KiPI's typical class II value)."""
    name: str
    capacitance_f: float = 0.0
    esl_h: float = 0.0
    esr_ohm: float = 0.0
    refs: List[str] = field(default_factory=list)
    rated_v: Optional[float] = None
    kept_at_rated_pct: Optional[float] = None
    dielectric: str = ""
    # Keep this model for its refs even where their KiCad library fields (PI_ESR, PI_ESL, ...)
    # give a complete model, which otherwise wins (coppernicus.board_fields). Saved only when on.
    override_board_fields: bool = False


@dataclass
class AcOptions:
    """The AC sweep (``ac:`` in the project file). Defaults are KiPI's ``ACSettings``
    (kipi/ac/model.py:237-257). ``bandwidth_hz`` maps a source (its name, or its ref) to its
    control loop bandwidth; ``link_esl_h`` maps a link (its ref, or its name) to its series
    inductance, without which a link is a plain resistance in AC."""
    f_start_hz: float = 1e3
    f_stop_hz: float = 300e6
    points_per_decade: int = 250
    field_points_per_decade: int = 20     # copper solves per decade; 0 = every sweep point
    max_edge_mm: float = 1.0              # the AC mesh's own (settings.max_edge_mm is DC's)
    loss_tangent: float = 0.02
    default_er: float = 4.3               # where the stackup gives no εr
    bandwidth_hz: Dict[str, float] = field(default_factory=dict)
    link_esl_h: Dict[str, float] = field(default_factory=dict)


_AC_INTS = ("points_per_decade", "field_points_per_decade")
PROBE_KEYS = ("name", "ref", "net", "pins", "ground", "target_ohm", "target_hz", "target_points",
              "ac_tolerance_v", "step_a", "slew_a_per_us")
CAP_KEYS = ("name", "capacitance_f", "esl_h", "esr_ohm", "refs", "rated_v", "kept_at_rated_pct",
            "dielectric", "override_board_fields")


def _known_keys(d: Dict[str, Any], keys: Sequence[str], what: str, name: str) -> None:
    unknown = [str(k) for k in d if k not in keys]
    if unknown:
        raise ValueError(f"unknown {what} key {', '.join(unknown)} in {what} {name} (the keys: "
                         f"{', '.join(keys)})")


def _probe_out(p: Probe) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": p.name, "ref": p.ref, "net": p.net, "pins": _pins_out(p.pins)}
    if p.ground_net:
        d["ground"] = _pin_ref_out(p.ground_ref, p.ground_pins, p.ground_net)
    for key in ("target_ohm", "target_hz"):
        if getattr(p, key) is not None:
            d[key] = getattr(p, key)
    if p.target_points:
        d["target_points"] = [[f, z] for f, z in p.target_points]
    for key in ("ac_tolerance_v", "step_a", "slew_a_per_us"):
        if getattr(p, key) is not None:
            d[key] = getattr(p, key)
    return d


def _cap_out(m: CapModel) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": m.name}
    for key in ("capacitance_f", "esl_h", "esr_ohm"):
        if getattr(m, key):
            d[key] = getattr(m, key)
    if m.refs:
        d["refs"] = list(m.refs)
    for key in ("rated_v", "kept_at_rated_pct"):
        if getattr(m, key) is not None:
            d[key] = getattr(m, key)
    if m.dielectric:
        d["dielectric"] = m.dielectric
    if m.override_board_fields:
        d["override_board_fields"] = True
    return d


def _ac_out(a: AcOptions) -> Dict[str, Any]:
    """The ``ac:`` block: only the keys away from their default (empty when all are)."""
    default = AcOptions()
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in vars(a).items()
           if v != getattr(default, k)}
    return out


def _positive(value: Any, what: str, allow_none: bool = True) -> Optional[float]:
    """A finite number above zero (None when empty and allowed)."""
    if value is None and allow_none:
        return None
    x = _number(value, what) if not isinstance(value, bool) else math.nan
    if not x > 0:
        raise ValueError(f"{what} must be a number above zero, not {value!r}")
    return x


def _probe_in(d: Any) -> Probe:
    if not isinstance(d, dict):
        raise ValueError(f"each probe must be a mapping, not {d!r}")
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("each probe needs a name")
    owner = f"probe {name}"
    _known_keys(d, PROBE_KEYS, "probe", name)
    ref, net = str(d.get("ref") or "").strip(), str(d.get("net") or "").strip()
    if not ref or not net:
        raise ValueError(f"{owner} needs its part (ref) and the net of its supply pins (net)")
    values = {k: _positive(d.get(k), f"{owner}: {k}")
              for k in ("target_ohm", "target_hz", "ac_tolerance_v", "step_a", "slew_a_per_us")}
    raw = d.get("target_points") or []
    if not isinstance(raw, list):
        raise ValueError(f"{owner}: target_points must be a list of [Hz, ohm] pairs")
    points = []
    for pt in raw:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            raise ValueError(f"{owner}: target_points must be [Hz, ohm] pairs, not {pt!r}")
        points.append((_positive(pt[0], f"{owner}: target_points frequency", False),
                       _positive(pt[1], f"{owner}: target_points impedance", False)))
    if points and values["target_ohm"] is not None:
        raise ValueError(f"{owner}: give target_ohm or target_points (a mask), not both")
    if points and values["target_hz"] is not None:
        raise ValueError(f"{owner}: target_points (a mask) takes no target_hz: the mask ends at "
                         "its last point")
    if values["target_hz"] is not None and values["target_ohm"] is None:
        raise ValueError(f"{owner}: target_hz without target_ohm: give the target impedance it "
                         "applies up to")
    freqs = [f for f, _ in points]
    if len(set(freqs)) != len(freqs):
        raise ValueError(f"{owner}: two target_points are at the same frequency")
    return Probe(name=name, ref=ref, net=net, pins=parse_pins(d.get("pins")),
                 target_points=points, **_pin_ref_in(d, "ground", owner), **values)


def _cap_in(d: Any) -> CapModel:
    if not isinstance(d, dict):
        raise ValueError(f"each capacitor model must be a mapping, not {d!r}")
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("each capacitor model needs a name")
    owner = f"capacitor model {name}"
    _known_keys(d, CAP_KEYS, "capacitor model", name)
    values = {}
    for key in ("capacitance_f", "esl_h", "esr_ohm"):
        x = _number(d.get(key, 0.0), f"{owner}: {key}")
        if x < 0:
            raise ValueError(f"{owner}: {key} cannot be negative")
        values[key] = x
    refs = d.get("refs") or []
    if not isinstance(refs, list):
        raise ValueError(f"{owner}: refs must be a list of part references")
    kept = d.get("kept_at_rated_pct")
    if kept is not None:
        kept = _number(kept, f"{owner}: kept_at_rated_pct")
        if not 0 < kept <= 100:
            raise ValueError(f"{owner}: kept_at_rated_pct is the % of the capacitance kept at "
                             f"the rated voltage, above 0 and at most 100, not {kept:g}")
    return CapModel(name=name, refs=[str(r).strip() for r in refs if str(r).strip()],
                    rated_v=_positive(d.get("rated_v"), f"{owner}: rated_v"),
                    kept_at_rated_pct=kept, dielectric=str(d.get("dielectric") or ""),
                    override_board_fields=_flag(d, "override_board_fields", False), **values)


def _ac_options_in(d: Any) -> AcOptions:
    if not isinstance(d, dict):
        raise ValueError("ac must be a mapping of AC options")
    default = AcOptions()
    unknown = sorted(set(d) - set(vars(default)))
    if unknown:
        raise ValueError(f"unknown ac option {', '.join(map(str, unknown))} (the options: "
                         f"{', '.join(vars(default))})")
    out: Dict[str, Any] = {}
    for key in ("f_start_hz", "f_stop_hz", "max_edge_mm"):
        if key in d:
            out[key] = _positive(d[key], f"ac {key}", False)
    for key in _AC_INTS:
        if key in d:
            v = d[key]
            if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    or v != int(v)):
                raise ValueError(f"ac {key} must be a whole number, not {v!r}")
            if v < (1 if key == "points_per_decade" else 0):
                raise ValueError(f"ac {key} must be at least "
                                 f"{1 if key == 'points_per_decade' else 0}, not {v!r}")
            out[key] = int(v)
    if "loss_tangent" in d:
        out["loss_tangent"] = _number(d["loss_tangent"], "ac loss_tangent")
        if out["loss_tangent"] < 0:
            raise ValueError("ac loss_tangent cannot be negative")
    if "default_er" in d:
        out["default_er"] = _number(d["default_er"], "ac default_er")
        if out["default_er"] < 1:
            raise ValueError("ac default_er is a relative permittivity: at least 1")
    a = AcOptions(**out)
    if not a.f_stop_hz > a.f_start_hz:
        raise ValueError(f"ac f_stop_hz ({a.f_stop_hz:g}) must be above f_start_hz ({a.f_start_hz:g})")
    for key, check in (("bandwidth_hz", "above zero"), ("link_esl_h", "not negative")):
        values = d.get(key) or {}
        if not isinstance(values, dict):
            raise ValueError(f"ac {key} must map names to numbers, e.g. {{U1: 1.0e+5}}")
        parsed = {}
        for name, v in values.items():
            x = _number(v, f"ac {key}[{name}]") if not isinstance(v, bool) else math.nan
            if not (x > 0 if check == "above zero" else x >= 0):
                raise ValueError(f"ac {key}[{name}] must be a number {check}, not {v!r}")
            parsed[str(name)] = x
        setattr(a, key, parsed)
    return a


def _ac_in(data: Dict[str, Any]) -> Dict[str, Any]:
    """``probes``, ``capacitors`` and ``ac`` of a project file, checked."""
    probes = data.get("probes") or []
    caps = data.get("capacitors") or []
    if not isinstance(probes, list) or not isinstance(caps, list):
        raise ValueError("probes and capacitors must be lists")
    probes = [_probe_in(p) for p in probes]
    caps = [_cap_in(c) for c in caps]
    for what, names in (("probe", [p.name for p in probes]), ("capacitor model", [c.name for c in caps])):
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"{what} names must be unique: {', '.join(twice)}")
    seen: Dict[str, str] = {}
    for c in caps:
        for ref in c.refs:
            if ref in seen:
                raise ValueError(f"{ref} is in two capacitor models ({seen[ref]}, {c.name})")
            seen[ref] = c.name
    return {"probes": probes, "capacitors": caps, "ac": _ac_options_in(data.get("ac") or {})}


def check_ac(probes: Sequence[Probe], capacitors: Sequence[CapModel],
             ac: Optional[AcOptions] = None) -> None:
    """Raise :class:`ProjectError` when these probes, capacitor models and ``ac`` options
    would not load back from a project file: the checks :meth:`Project.from_dict` makes,
    for an editor that must never write a file the loader refuses."""
    try:
        _ac_in({"probes": [_probe_out(p) for p in probes],
                "capacitors": [_cap_out(c) for c in capacitors],
                "ac": _ac_out(ac) if ac is not None else {}})
    except (KeyError, TypeError, ValueError) as e:
        raise ProjectError(str(e)) from e
    for p in probes:
        if p.ground_net and p.ground_net == p.net:
            raise ProjectError(f"probe {p.name}: its ground net is its supply net {p.net}: its ground "
                               "pins go on the ground")


# ---------------------------------------------------------------------- noise (Task 10)

# KiPI's choices (kipi/noise/model.py:22-25, :153-174); a test checks they match.
NOISE_TOPOLOGIES = ("auto", "buck", "boost")
INDUCTOR_AXES = ("vertical", "along", "across")
VICTIM_PRESETS = ("cmos", "open_drain", "series", "parallel", "lvds", "analog", "hiz")
CUSTOM_PRESET = "custom"
# A victim's terminations: written only for a "custom" victim (a named preset has its own).
TERMINATION_KEYS = ("driver_ohm", "receiver_ohm", "receiver_pf", "termination_ohm", "driver_low_ohm")
CUSTOM_REQUIRED = ("driver_ohm", "receiver_ohm", "receiver_pf")


@dataclass
class Phase:
    """One phase of a switching regulator (an aggressor): its power inductor, the switch
    node, the switching device's pins on the rail its hot loop is fed from (``high_side``:
    VIN for a buck, the output for a boost) and on its power ground (``low_side``), and the
    hot-loop capacitors. The device data are optional: empty, KiPI's stand-ins are used and
    the victims are not judged (Coss, Qrr; ``qrr_nc`` 0 is entered: a Schottky or GaN low
    side). Filled from the board only by an explicit action, never at solve."""
    inductor: str = ""
    inductance_h: float = 0.0                 # > 0 to solve; 0 = not entered
    dcr_ohm: float = 0.0
    switch_net: str = ""
    high_side_ref: str = ""
    high_side_pins: PinSpec = None
    high_side_net: str = ""
    low_side_ref: str = ""
    low_side_pins: PinSpec = None
    low_side_net: str = ""
    input_caps: List[str] = field(default_factory=list)
    output_caps: List[str] = field(default_factory=list)
    coss_f: Optional[float] = None
    qrr_nc: Optional[float] = None            # nC (``_c`` is °C in this file)
    rds_on_hs_ohm: Optional[float] = None
    rds_on_ls_ohm: Optional[float] = None
    package_l_h: float = 0.0
    inductor_axis: str = "vertical"
    inductor_shielded: bool = True


@dataclass
class Aggressor:
    """A switching regulator as a noise source: the switcher source ``vrm`` (its engine
    name, as the source combo lists them), its switching frequency and edges (required to
    solve: the spectrum hangs on them), its phases, and the probes to show its ripple at.
    ``vin_v``, ``duty`` and ``load_a`` empty: from the source feeding its input, the
    voltages and losses, and the loads on its output."""
    name: str
    vrm: str = ""
    topology: str = "auto"
    fsw_hz: Optional[float] = None
    rise_s: Optional[float] = None
    fall_s: Optional[float] = None
    vin_v: Optional[float] = None
    duty: Optional[float] = None
    load_a: Optional[float] = None
    ripple_probes: List[str] = field(default_factory=list)
    phases: List[Phase] = field(default_factory=list)
    min_ring_zeta: float = 0.05               # KiPI's floors (kipi/noise/model.py:87, :91)
    soft_edge_zeta: float = 0.5


@dataclass
class Victim:
    """A signal net the aggressors' noise couples onto, between its driver and receiver
    (pins of a part on ``net``; empty: KiPI guesses them, and it is not judged). ``preset``
    is one of VICTIM_PRESETS or "custom", whose terminations are then given here. Its noise
    margin is ``noise_margin_v``, else the preset's own (lvds, analog), else the logic levels
    of ``logic_v``; with none of them it is ranked, never judged."""
    net: str
    name: str = ""                            # "" = the net's name
    preset: str = ""
    driver_ref: str = ""
    driver_pins: PinSpec = None
    receiver_ref: str = ""
    receiver_pins: PinSpec = None
    driver_ohm: Optional[float] = None
    receiver_ohm: Optional[float] = None
    receiver_pf: Optional[float] = None
    termination_ohm: Optional[float] = None
    driver_low_ohm: Optional[float] = None
    logic_v: Optional[float] = None
    noise_margin_v: Optional[float] = None

    @property
    def label(self) -> str:
        return self.name or self.net


@dataclass
class NoiseOptions:
    """The noise solve (``noise:`` in the project file). Defaults are KiPI's
    ``NoiseSettings`` (kipi/noise/model.py:244-264); keys at their default are omitted."""
    f_max_hz: Optional[float] = None          # None: from the edges and the ringing, ≤ 3 GHz
    max_harmonics: int = 4000
    dead_time_ns: float = 20.0
    segment_mm: float = 0.5
    window_mm: float = 3.0
    board_model: bool = True
    field_per_decade: int = 10


AGGRESSOR_KEYS = ("name", "vrm", "topology", "fsw_hz", "rise_s", "fall_s", "vin_v", "duty", "load_a",
                  "ripple_probes", "phases", "min_ring_zeta", "soft_edge_zeta")
PHASE_KEYS = ("inductor", "inductance_h", "dcr_ohm", "switch_net", "high_side", "low_side",
              "input_caps", "output_caps", "coss_f", "qrr_nc", "rds_on_hs_ohm", "rds_on_ls_ohm",
              "package_l_h", "inductor_axis", "inductor_shielded")
VICTIM_KEYS = ("net", "name", "preset", "driver", "receiver", *TERMINATION_KEYS, "logic_v",
               "noise_margin_v")


def _nested_keys(d: Dict[str, Any], keys: Sequence[str], what: str, owner: str) -> None:
    unknown = [str(k) for k in d if k not in keys]
    if unknown:
        raise ValueError(f"{owner}: unknown {what} key {', '.join(unknown)} (the keys: "
                         f"{', '.join(keys)})")


def _at_least(value: Any, what: str, low: float = 0.0) -> float:
    """A finite number, at least ``low``."""
    x = _number(value, what) if not isinstance(value, bool) else math.nan
    if not x >= low:
        raise ValueError(f"{what} must be a number of at least {low:g}, not {value!r}")
    return x


def _names(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list of names, not {value!r}")
    return [str(x).strip() for x in value if str(x).strip()]


def _part_pins(d: Dict[str, Any], what: str, owner: str, with_net: bool) -> Dict[str, Any]:
    """``{ref, pins}`` (and ``net`` with ``with_net``) of a phase's or victim's pin
    reference, as ``{what}_ref``/``_pins``/``_net`` fields; the part is required."""
    value = d.get(what)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{owner}: {what} must be a mapping {{ref, pins}}, not {value!r}")
    keys = ("ref", "net", "pins") if with_net else ("ref", "pins")
    _nested_keys(value, keys, what, owner)
    ref = str(value.get("ref") or "").strip()
    if not ref:
        raise ValueError(f"{owner}: {what} needs its part (ref)")
    out = {f"{what}_ref": ref, f"{what}_pins": parse_pins(value.get("pins"))}
    if with_net:
        net = str(value.get("net") or "").strip()
        if not net:
            raise ValueError(f"{owner}: {what} needs its net")
        out[f"{what}_net"] = net
    return out


def _phase_in(d: Any, owner: str) -> Phase:
    if not isinstance(d, dict):
        raise ValueError(f"{owner}: each phase must be a mapping, not {d!r}")
    _nested_keys(d, PHASE_KEYS, "phase", owner)
    out: Dict[str, Any] = {}
    for key in ("inductor", "switch_net"):
        out[key] = str(d.get(key) or "").strip()
    for key in ("inductance_h", "dcr_ohm", "package_l_h"):
        if d.get(key) is not None:
            out[key] = _at_least(d[key], f"{owner}: {key}")
    for key in ("coss_f",):
        out[key] = _positive(d.get(key), f"{owner}: {key}")
    for key in ("qrr_nc", "rds_on_hs_ohm", "rds_on_ls_ohm"):
        out[key] = None if d.get(key) is None else _at_least(d[key], f"{owner}: {key}")
    axis = d.get("inductor_axis", "vertical")
    if axis not in INDUCTOR_AXES:
        raise ValueError(f"{owner}: inductor_axis must be one of {', '.join(INDUCTOR_AXES)}, "
                         f"not {axis!r}")
    shielded = d.get("inductor_shielded", True)
    if not isinstance(shielded, bool):
        raise ValueError(f"{owner}: inductor_shielded must be true or false, not {shielded!r}")
    return Phase(**out, input_caps=_names(d.get("input_caps"), f"{owner}: input_caps"),
                 output_caps=_names(d.get("output_caps"), f"{owner}: output_caps"),
                 inductor_axis=axis, inductor_shielded=shielded,
                 **_part_pins(d, "high_side", owner, True), **_part_pins(d, "low_side", owner, True))


def _aggressor_in(d: Any) -> Aggressor:
    if not isinstance(d, dict):
        raise ValueError(f"each aggressor must be a mapping, not {d!r}")
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("each aggressor needs a name")
    owner = f"aggressor {name}"
    _known_keys(d, AGGRESSOR_KEYS, "aggressor", name)
    topology = d.get("topology", "auto")
    if topology not in NOISE_TOPOLOGIES:
        raise ValueError(f"{owner}: topology must be one of {', '.join(NOISE_TOPOLOGIES)}, "
                         f"not {topology!r}")
    values: Dict[str, Any] = {k: _positive(d.get(k), f"{owner}: {k}")
                              for k in ("fsw_hz", "rise_s", "fall_s", "vin_v")}
    duty = d.get("duty")
    if duty is not None:
        duty = _number(duty, f"{owner}: duty") if not isinstance(duty, bool) else math.nan
        if not 0 < duty < 1:
            raise ValueError(f"{owner}: duty is a fraction between 0 and 1 (both excluded), "
                             f"not {d.get('duty')!r}")
    load = None if d.get("load_a") is None else _at_least(d["load_a"], f"{owner}: load_a")
    for key in ("min_ring_zeta", "soft_edge_zeta"):
        if key in d:
            values[key] = _positive(d[key], f"{owner}: {key}", False)
    phases = d.get("phases") or []
    if not isinstance(phases, list):
        raise ValueError(f"{owner}: phases must be a list")
    return Aggressor(name=name, vrm=str(d.get("vrm") or "").strip(), topology=topology,
                     duty=duty, load_a=load,
                     ripple_probes=_names(d.get("ripple_probes"), f"{owner}: ripple_probes"),
                     phases=[_phase_in(ph, f"{owner}, phase {i}") for i, ph in enumerate(phases, 1)],
                     **values)


def _victim_in(d: Any) -> Victim:
    if not isinstance(d, dict):
        raise ValueError(f"each victim must be a mapping, not {d!r}")
    net = str(d.get("net") or "").strip()
    if not net:
        raise ValueError("each victim needs its net")
    name = str(d.get("name") or "").strip()
    owner = f"victim {name or net}"
    _known_keys(d, VICTIM_KEYS, "victim", name or net)
    preset = str(d.get("preset") or "").strip()
    if preset not in (*VICTIM_PRESETS, CUSTOM_PRESET):
        raise ValueError(f"{owner}: preset (the line's type) must be one of "
                         f"{', '.join(VICTIM_PRESETS)} or {CUSTOM_PRESET}, not {preset or 'missing'}")
    given = [k for k in TERMINATION_KEYS if d.get(k) is not None]
    if given and preset != CUSTOM_PRESET:
        raise ValueError(f"{owner}: {', '.join(given)} given with preset {preset}, which has its "
                         f"own terminations: set preset {CUSTOM_PRESET} to enter them")
    missing = [k for k in CUSTOM_REQUIRED if d.get(k) is None]
    if preset == CUSTOM_PRESET and missing:
        raise ValueError(f"{owner}: a {CUSTOM_PRESET} victim needs {', '.join(missing)}")
    values: Dict[str, Any] = {}
    for key in ("driver_ohm", "receiver_ohm", "termination_ohm", "driver_low_ohm", "logic_v",
                "noise_margin_v"):
        values[key] = _positive(d.get(key), f"{owner}: {key}")
    if d.get("receiver_pf") is not None:
        values["receiver_pf"] = _at_least(d["receiver_pf"], f"{owner}: receiver_pf")
    return Victim(net=net, name="" if name == net else name, preset=preset, **values,
                  **_part_pins(d, "driver", owner, False), **_part_pins(d, "receiver", owner, False))


def _noise_options_in(d: Any) -> NoiseOptions:
    if not isinstance(d, dict):
        raise ValueError("noise must be a mapping of noise options")
    default = NoiseOptions()
    unknown = sorted(str(k) for k in set(d) - set(vars(default)))
    if unknown:
        raise ValueError(f"unknown noise option {', '.join(unknown)} (the options: "
                         f"{', '.join(vars(default))})")
    out: Dict[str, Any] = {}
    if "f_max_hz" in d:
        out["f_max_hz"] = _positive(d["f_max_hz"], "noise f_max_hz")
    for key in ("segment_mm", "window_mm"):
        if key in d:
            out[key] = _positive(d[key], f"noise {key}", False)
    if "dead_time_ns" in d:
        out["dead_time_ns"] = _at_least(d["dead_time_ns"], "noise dead_time_ns")
    for key in ("max_harmonics", "field_per_decade"):
        if key in d:
            v = d[key]
            if (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    or v != int(v) or v < 1):
                raise ValueError(f"noise {key} must be a whole number, at least 1, not {v!r}")
            out[key] = int(v)
    if "board_model" in d:
        out["board_model"] = _flag(d, "board_model", True)
    return NoiseOptions(**out)


def _noise_in(data: Dict[str, Any]) -> Dict[str, Any]:
    """``aggressors``, ``victims`` and ``noise`` of a project file, checked."""
    aggressors = data.get("aggressors") or []
    victims = data.get("victims") or []
    if not isinstance(aggressors, list) or not isinstance(victims, list):
        raise ValueError("aggressors and victims must be lists")
    aggressors = [_aggressor_in(a) for a in aggressors]
    victims = [_victim_in(v) for v in victims]
    for what, names in (("aggressor", [a.name for a in aggressors]),
                        ("victim", [v.label for v in victims])):
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(f"{what} names must be unique: {', '.join(twice)} (a victim's name "
                             "is its net unless one is given)" if what == "victim" else
                             f"{what} names must be unique: {', '.join(twice)}")
    return {"aggressors": aggressors, "victims": victims,
            "noise": _noise_options_in(data.get("noise") or {})}


def _phase_out(ph: Phase) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for key in ("inductor", "inductance_h", "dcr_ohm", "switch_net"):
        if getattr(ph, key):
            d[key] = getattr(ph, key)
    for what in ("high_side", "low_side"):
        if getattr(ph, f"{what}_net"):
            d[what] = _pin_ref_out(getattr(ph, f"{what}_ref"), getattr(ph, f"{what}_pins"),
                                   getattr(ph, f"{what}_net"))
    for key in ("input_caps", "output_caps"):
        if getattr(ph, key):
            d[key] = list(getattr(ph, key))
    for key in ("coss_f", "qrr_nc", "rds_on_hs_ohm", "rds_on_ls_ohm"):   # 0 is entered
        if getattr(ph, key) is not None:
            d[key] = getattr(ph, key)
    if ph.package_l_h:
        d["package_l_h"] = ph.package_l_h
    if ph.inductor_axis != "vertical":
        d["inductor_axis"] = ph.inductor_axis
    if not ph.inductor_shielded:
        d["inductor_shielded"] = False
    return d


def _aggressor_out(a: Aggressor) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": a.name}
    if a.vrm:
        d["vrm"] = a.vrm
    if a.topology != "auto":
        d["topology"] = a.topology
    for key in ("fsw_hz", "rise_s", "fall_s", "vin_v", "duty", "load_a"):
        if getattr(a, key) is not None:
            d[key] = getattr(a, key)
    if a.ripple_probes:
        d["ripple_probes"] = list(a.ripple_probes)
    if a.phases:
        d["phases"] = [_phase_out(ph) for ph in a.phases]
    default = Aggressor("")
    for key in ("min_ring_zeta", "soft_edge_zeta"):
        if getattr(a, key) != getattr(default, key):
            d[key] = getattr(a, key)
    return d


def _victim_out(v: Victim) -> Dict[str, Any]:
    d: Dict[str, Any] = {"net": v.net}
    if v.name and v.name != v.net:
        d["name"] = v.name
    d["preset"] = v.preset
    for what in ("driver", "receiver"):
        if getattr(v, f"{what}_ref"):
            d[what] = {"ref": getattr(v, f"{what}_ref"), "pins": _pins_out(getattr(v, f"{what}_pins"))}
    for key in (*TERMINATION_KEYS, "logic_v", "noise_margin_v"):
        if getattr(v, key) is not None:
            d[key] = getattr(v, key)
    return d


def _noise_out(project: "Project") -> Dict[str, Any]:
    """The ``aggressors``, ``victims`` and ``noise`` blocks, each omitted while empty (noise
    keys at their default are omitted)."""
    out: Dict[str, Any] = {}
    if project.aggressors:
        out["aggressors"] = [_aggressor_out(a) for a in project.aggressors]
    if project.victims:
        out["victims"] = [_victim_out(v) for v in project.victims]
    default = NoiseOptions()
    options = {k: v for k, v in vars(project.noise).items() if v != getattr(default, k)}
    if options:
        out["noise"] = options
    return out


@dataclass
class Project:
    board: str = ""
    rails: List[Rail] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    # The board temperature (electro-thermal power tree); saved only when not at the defaults.
    thermal: ThermalOptions = field(default_factory=ThermalOptions)
    path: Optional[Path] = None              # where it was loaded from / saved to; not saved
    # Nets the user marked as rails by hand, where the name/zone heuristic missed them.
    # Saved only when not empty, so files written before this field stay identical.
    marked_rails: List[str] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    # Engine-specific options, passed through to the engine and checked there (KiPI:
    # ``excluded_nets``). Saved only when not empty.
    engine_options: Dict[str, Any] = field(default_factory=dict)
    # AC impedance (Task 9): probes, capacitor models, the sweep. Each saved only when set.
    probes: List[Probe] = field(default_factory=list)
    capacitors: List[CapModel] = field(default_factory=list)
    ac: AcOptions = field(default_factory=AcOptions)
    # Take rails from the board's KiCad footprint fields (PI_RAIL, PI_SOURCE, ...; see
    # coppernicus.board_fields) when it has any. On by default; saved only when off.
    use_board_fields: bool = True
    # DC-DC noise and ripple (Task 10): switchers as aggressors, signal nets as victims, the
    # solve's options. Each saved only when set.
    aggressors: List[Aggressor] = field(default_factory=list)
    victims: List[Victim] = field(default_factory=list)
    noise: NoiseOptions = field(default_factory=NoiseOptions)
    # Tolerance stack-up (Task 11): how ripple adds, full band or 20 MHz. Saved only when set.
    tolerance: ToleranceOptions = field(default_factory=ToleranceOptions)
    # Capacitors whose KiCad fields give no model (partial: PI_ESR missing...), ref -> why, set
    # by the fields merge (coppernicus.board_fields) for the AC and noise reasons. Not saved.
    capacitor_gaps: Dict[str, str] = field(default_factory=dict)
    # Where the power tree's voltages are shown from (``tree: {reference: ...}``; S2, a
    # display shift only: reference_spec). None: the solve's own 0 V. Saved only when set.
    tree_reference: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ queries
    def board_path(self) -> Path:
        p = _portable_path(self.board).expanduser()
        if not p.is_absolute() and self.path is not None:
            p = Path(self.path).parent / p
        return p

    def rail(self, net: str) -> Optional[Rail]:
        return next((r for r in self.rails if r.net == net), None)

    def ensure_rail(self, net: str) -> Rail:
        rail = self.rail(net)
        if rail is None:
            rail = Rail(net)
            self.rails.append(rail)
        return rail

    # ------------------------------------------------------------------ I/O
    def to_dict(self) -> Dict[str, Any]:
        def terminal(t, value_key):
            d: Dict[str, Any] = {}
            if t.name:
                d["name"] = t.name
            d["ref"] = t.ref
            d["pins"] = _pins_out(t.pins)
            d[value_key] = t.voltage if value_key == "voltage" else t.current
            if value_key == "voltage":            # v2 fields, only when not the default
                if t.kind != "source":
                    d["kind"] = t.kind
                if t.efficiency is not None:
                    d["efficiency"] = t.efficiency
                if t.quiescent_a:
                    d["quiescent_a"] = t.quiescent_a
                if t.current_limit_a is not None:
                    d["current_limit_a"] = t.current_limit_a
                if t.fsw_hz is not None:
                    d["fsw_hz"] = t.fsw_hz
                if t.extra_pins:
                    d["extra_pins"] = list(t.extra_pins)
            for what in (SOURCE_PIN_REFS if value_key == "voltage" else LOAD_PIN_REFS):
                if getattr(t, f"{what}_net"):
                    d[what] = _pin_ref_out(getattr(t, f"{what}_ref"), getattr(t, f"{what}_pins"),
                                           getattr(t, f"{what}_net"))
            if value_key == "voltage" and t.extra_ground_pins:
                d["extra_ground_pins"] = list(t.extra_ground_pins)
            # Task 11: tolerances, accuracy and entered parts, only when set (0 is a value).
            for key in (SOURCE_ACCURACY_KEYS if value_key == "voltage"
                        else LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS):
                if getattr(t, key) is not None:
                    d[key] = _pair_out(getattr(t, key))
            return d

        rails = []
        for r in self.rails:
            rd: Dict[str, Any] = {"net": r.net,
                                  "sources": [terminal(s, "voltage") for s in r.sources],
                                  "loads": [terminal(ld, "current") for ld in r.loads]}
            limits = {k: v for k, v in vars(r.limits).items() if v is not None}
            if limits:
                rd["limits"] = limits
            if not r.follow_net_ties:
                rd["follow_net_ties"] = False
            if r.override_board_fields:
                rd["override_board_fields"] = True
            rails.append(rd)
        out: Dict[str, Any] = {"board": self.board, "settings": _settings_out(self.settings), "rails": rails}
        out.update({"thermal": t} if (t := _thermal_out(self.thermal)) else {})
        if self.links:
            out["links"] = [{"name": k.name, "ref": k.ref, "a": _pin_ref_out("", k.pins_a, k.net_a),
                             "b": _pin_ref_out("", k.pins_b, k.net_b),
                             "resistance_ohm": k.resistance_ohm} for k in self.links]
        if self.engine_options:
            out["engine_options"] = dict(self.engine_options)
        if self.probes:
            out["probes"] = [_probe_out(p) for p in self.probes]
        if self.capacitors:
            out["capacitors"] = [_cap_out(c) for c in self.capacitors]
        if _ac_out(self.ac):
            out["ac"] = _ac_out(self.ac)
        if self.marked_rails:
            out["marked_rails"] = list(self.marked_rails)
        if not self.use_board_fields:
            out["use_board_fields"] = False
        out.update(_noise_out(self))
        out.update({"tolerance": t} if (t := _tolerance_out(self.tolerance)) else {})
        if self.tree_reference:
            out["tree"] = {"reference": {k: (list(v) if isinstance(v, list) else v)
                                         for k, v in self.tree_reference.items()}}
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: Optional[Path] = None) -> "Project":
        if not isinstance(data, dict):
            raise ProjectError("the project file must be a mapping")
        try:
            settings = Settings(**(data.get("settings") or {}))
            thermal = _thermal_in(data.get("thermal"))
            rails = []
            for rd in data.get("rails") or []:
                rails.append(Rail(
                    net=str(rd["net"]),
                    sources=[_source_in(s) for s in rd.get("sources") or []],
                    loads=[_load_in(ld) for ld in rd.get("loads") or []],
                    limits=_limits_in(rd.get("limits") or {}, str(rd["net"])),
                    follow_net_ties=bool(rd.get("follow_net_ties", True)),
                    override_board_fields=_flag(rd, "override_board_fields", False),
                ))
            marked = data.get("marked_rails") or []
            if not isinstance(marked, list):
                raise ValueError("marked_rails must be a list of net names")
            links = [_link_in(k) for k in data.get("links") or []]
            options = data.get("engine_options") or {}
            if not isinstance(options, dict):
                raise ValueError("engine_options must be a mapping of option names to values")
            ac = _ac_in(data)
            use_fields = _flag(data, "use_board_fields", True)
            noise = _noise_in(data)
            tolerance = _tolerance_in(data.get("tolerance"))
            tree_reference = _tree_in(data.get("tree"))
        except (KeyError, TypeError, ValueError) as e:
            raise ProjectError(f"invalid project file: {e}") from e
        return cls(board=str(data.get("board", "")), rails=rails, settings=settings, path=path,
                   marked_rails=[str(n) for n in marked], links=links, engine_options=dict(options),
                   use_board_fields=use_fields, thermal=thermal, tolerance=tolerance,
                   tree_reference=tree_reference, **ac, **noise)

    @classmethod
    def load(cls, path) -> "Project":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data, path)

    def save(self, path=None) -> Path:
        path = Path(path) if path is not None else self.path
        if path is None:
            raise ProjectError("no path to save the project to")
        if self.board:                    # keep the board relative to the project file
            board = _portable_path(self.board).expanduser()
            if not board.is_absolute():   # relative to where the project was, or will be
                board = (Path(self.path).parent if self.path else Path(path).parent) / board
            self.board = board_reference(board, path)
        self.path = path
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, sort_keys=False, allow_unicode=True)
        return path


# ---------------------------------------------------------------------- file helpers

def _flag(data: Dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, not {value!r}")
    return value


def _settings_out(settings: Settings) -> Dict[str, Any]:
    """The settings as written to the file; copper_thickness_mm, min_edge_mm and grading only
    when set, so a project without them is written exactly as before."""
    out = dict(vars(settings))
    for key in ("min_edge_mm", "grading"):           # KiPI's defaults: not written
        if out.get(key) is None:
            out.pop(key, None)
    if not out.get("copper_thickness_mm"):
        out.pop("copper_thickness_mm", None)
    else:
        out["copper_thickness_mm"] = dict(out["copper_thickness_mm"])
    return out


def _portable_path(text: str) -> Path:
    """A path written in a project file, with "/" or "\\" as separators (a file saved on
    Windows by an older version has backslashes), as a Path of this system."""
    if "\\" in text and os.sep == "/":
        return Path(PureWindowsPath(text).as_posix())
    return Path(text)


def _limits_in(d: Any, net: str) -> Limits:
    """A rail's limits: each one empty or a finite number (a NaN limit would pass anything)."""
    if not isinstance(d, dict):
        raise ValueError(f"rail {net}: limits must be a mapping of limit names to numbers")
    return Limits(**{k: None if v is None else _number(v, f"rail {net}: limit {k}")
                     for k, v in d.items()})


def _number(value: Any, what: str) -> float:
    """A finite number from the file (NaN and infinity are refused)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number, not {value!r}") from None
    if not math.isfinite(x):
        raise ValueError(f"{what} must be a finite number, not {value!r}")
    return x


def board_reference(board, project_path) -> str:
    """How a project file at ``project_path`` names the board file ``board``: relative to the
    project's folder, always with "/" so that a project saved on Windows opens on macOS and
    Linux; absolute when it is on another drive (Windows)."""
    board = Path(board).resolve()
    try:
        return Path(os.path.relpath(board, Path(project_path).resolve().parent)).as_posix()
    except ValueError:
        return board.as_posix()


def _pins_out(pins: PinSpec):
    return "all" if pins is None else list(pins)


def _pin_ref_out(ref: str, pins: PinSpec, net: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {"ref": ref} if ref else {}
    d["net"] = net
    d["pins"] = _pins_out(pins)
    return d


def _pin_ref_in(data: Dict[str, Any], what: str, owner: str) -> Dict[str, Any]:
    """The ``{what}_ref/_pins/_net`` fields from a nested ``{ref, net, pins}`` mapping."""
    value = data.get(what)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{owner}: {what} must be a mapping {{ref, net, pins}}, not {value!r}")
    net = str(value.get("net") or "").strip()
    if not net:
        raise ValueError(f"{owner}: {what} needs its net")
    return {f"{what}_ref": str(value.get("ref") or "").strip(),
            f"{what}_pins": parse_pins(value.get("pins")), f"{what}_net": net}


def _extra_pins_in(value: Any, ref: str, key: str = "extra_pins", part: str = "BT5",
                   where: str = "the rail") -> Optional[List[str]]:
    """A source's ``extra_pins`` (or ``extra_ground_pins``): a list of "REF.PAD" (the pad may
    be a glob)."""
    if not isinstance(value, list):
        raise ValueError(f"source {ref}: {key} is a list of REF.PAD entries (e.g. [{part}.1]), "
                         f"not {value!r}")
    out = []
    for e in value:
        head, dot, pad = str(e).strip().partition(".")
        if not (head and dot and pad):
            raise ValueError(f"source {ref}: {key} entry {e!r} is not REF.PAD (e.g. {part}.1, or "
                             f"{part}.* for every pad of {part} on {where})")
        out.append(f"{head}.{pad}")
    return out or None


def _source_in(s: Dict[str, Any]) -> Source:
    ref = str(s["ref"])
    kind = str(s.get("kind") or "source")
    if kind not in SOURCE_KINDS:
        raise ValueError(f"source {ref}: kind must be one of {', '.join(SOURCE_KINDS)}, not {kind!r}")
    efficiency = s.get("efficiency")
    if efficiency is not None:
        efficiency = _number(efficiency, f"source {ref}: efficiency")
        if not 0 < efficiency <= 1:
            raise ValueError(f"source {ref}: efficiency is a fraction between 0 and 1 "
                             f"(0.9 for 90 %), not {efficiency:g}")
    quiescent = _number(s.get("quiescent_a", 0.0), f"source {ref}: quiescent_a")
    if quiescent < 0:
        raise ValueError(f"source {ref}: quiescent_a cannot be negative")
    extra: Dict[str, Any] = {}
    for key in ("current_limit_a", "fsw_hz"):           # part data: > 0 when given
        if s.get(key) is not None:
            extra[key] = _number(s[key], f"source {ref}: {key}")
            if not extra[key] > 0:
                raise ValueError(f"source {ref}: {key} is above 0, not {extra[key]:g}")
    if s.get("extra_pins") is not None:
        extra["extra_pins"] = _extra_pins_in(s["extra_pins"], ref)
    refs: Dict[str, Any] = {}
    for what in SOURCE_PIN_REFS:
        refs.update(_pin_ref_in(s, what, f"source {ref}"))
    if s.get("extra_ground_pins") is not None:
        extra["extra_ground_pins"] = _extra_pins_in(s["extra_ground_pins"], ref, "extra_ground_pins",
                                                    "BT8", "the ground net")
        if extra["extra_ground_pins"] and not refs.get("ground_net"):
            raise ValueError(f"source {ref}: extra_ground_pins needs its ground (ground: {{ref, net, "
                             "pins}}): they are more pads of that terminal, on its net")
    extra.update({k: s[k] for k in SOURCE_ACCURACY_KEYS if s.get(k) is not None})
    return Source(ref=ref, voltage=_number(s["voltage"], f"source {ref}: voltage"),
                  pins=parse_pins(s.get("pins")),
                  name=str(s.get("name", "")), kind=kind, efficiency=efficiency,
                  quiescent_a=quiescent, **refs, **extra)


def _load_in(ld: Dict[str, Any]) -> Load:
    ref = str(ld["ref"])
    refs: Dict[str, Any] = {}
    for what in LOAD_PIN_REFS:
        refs.update(_pin_ref_in(ld, what, f"load {ref}"))
    tolerances = {k: ld[k] for k in LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS if ld.get(k) is not None}
    return Load(ref=ref, current=_number(ld["current"], f"load {ref}: current"),
                pins=parse_pins(ld.get("pins")),
                name=str(ld.get("name", "")), **refs, **tolerances)


def _link_in(k: Dict[str, Any]) -> Link:
    if not isinstance(k, dict):
        raise ValueError(f"each link must be a mapping, not {k!r}")
    name = str(k.get("name") or "").strip()
    if not name:
        raise ValueError("each link needs a name")
    ref = str(k.get("ref") or "").strip()
    if not ref:
        raise ValueError(f"link {name} needs its part reference (ref)")
    if k.get("resistance_ohm") is None:
        raise ValueError(f"link {name} needs resistance_ohm")
    resistance = _number(k["resistance_ohm"], f"link {name}: resistance_ohm")
    if resistance < 0:
        raise ValueError(f"link {name}: resistance_ohm cannot be negative")
    sides = []
    for side in ("a", "b"):
        if k.get(side) is None:
            raise ValueError(f"link {name} needs side {side}: {{net, pins}}")
        if isinstance(k[side], dict) and k[side].get("ref"):
            raise ValueError(f"link {name}: side {side} cannot name a part (ref); a link is one "
                             f"part, given once as its ref ({ref})")
        d = _pin_ref_in(k, side, f"link {name}")
        sides += [d[f"{side}_pins"], d[f"{side}_net"]]
    return Link(name, ref, *sides, resistance)
