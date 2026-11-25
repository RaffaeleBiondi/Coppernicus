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


class ProjectError(ValueError):
    """The project file is malformed."""


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

    @property
    def label(self) -> str:
        return self.name or self.ref


@dataclass
class Load:
    """Pins of ``ref`` drawing ``current`` amps in total, split equally across the pins;
    with ``ground_net`` set, the current returns through those ground pins."""
    ref: str
    current: float
    pins: PinSpec = None
    name: str = ""
    ground_ref: str = ""
    ground_pins: PinSpec = None
    ground_net: str = ""

    @property
    def label(self) -> str:
        return self.name or self.ref


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

    def __post_init__(self):
        self.copper_thickness_mm = check_copper_thickness(self.copper_thickness_mm)


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


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Project:
    board: str = ""
    rails: List[Rail] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    path: Optional[Path] = None               # where it was loaded from / saved to; not saved
    # Nets the user marked as rails by hand, where the name/zone heuristic missed them.
    # Saved only when not empty, so files written before this field stay identical.
    marked_rails: List[str] = field(default_factory=list)
    links: List[Link] = field(default_factory=list)
    # Engine-specific options, passed through to the engine and checked there (KiPI:
    # ``excluded_nets``). Saved only when not empty.
    engine_options: Dict[str, Any] = field(default_factory=dict)
    # Take rails from the board's KiCad footprint fields (PI_RAIL, PI_SOURCE, ...; see
    # coppernicus.board_fields) when it has any. On by default; saved only when off.
    use_board_fields: bool = True

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
            for what in (SOURCE_PIN_REFS if value_key == "voltage" else LOAD_PIN_REFS):
                if getattr(t, f"{what}_net"):
                    d[what] = _pin_ref_out(getattr(t, f"{what}_ref"), getattr(t, f"{what}_pins"),
                                           getattr(t, f"{what}_net"))
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
        if self.links:
            out["links"] = [{"name": k.name, "ref": k.ref, "a": _pin_ref_out("", k.pins_a, k.net_a),
                             "b": _pin_ref_out("", k.pins_b, k.net_b),
                             "resistance_ohm": k.resistance_ohm} for k in self.links]
        if self.engine_options:
            out["engine_options"] = dict(self.engine_options)
        if self.marked_rails:
            out["marked_rails"] = list(self.marked_rails)
        if not self.use_board_fields:
            out["use_board_fields"] = False
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any], path: Optional[Path] = None) -> "Project":
        if not isinstance(data, dict):
            raise ProjectError("the project file must be a mapping")
        try:
            settings = Settings(**(data.get("settings") or {}))
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
            use_fields = _flag(data, "use_board_fields", True)
        except (KeyError, TypeError, ValueError) as e:
            raise ProjectError(f"invalid project file: {e}") from e
        return cls(board=str(data.get("board", "")), rails=rails, settings=settings, path=path,
                   marked_rails=[str(n) for n in marked], links=links, engine_options=dict(options),
                   use_board_fields=use_fields)

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
    """The settings as written to the file; copper_thickness_mm only when set, so a project
    without it is written exactly as before."""
    out = dict(vars(settings))
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
    refs: Dict[str, Any] = {}
    for what in SOURCE_PIN_REFS:
        refs.update(_pin_ref_in(s, what, f"source {ref}"))
    return Source(ref=ref, voltage=_number(s["voltage"], f"source {ref}: voltage"),
                  pins=parse_pins(s.get("pins")),
                  name=str(s.get("name", "")), kind=kind, efficiency=efficiency,
                  quiescent_a=quiescent, **refs)


def _load_in(ld: Dict[str, Any]) -> Load:
    ref = str(ld["ref"])
    refs: Dict[str, Any] = {}
    for what in LOAD_PIN_REFS:
        refs.update(_pin_ref_in(ld, what, f"load {ref}"))
    return Load(ref=ref, current=_number(ld["current"], f"load {ref}: current"),
                pins=parse_pins(ld.get("pins")),
                name=str(ld.get("name", "")), **refs)


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
