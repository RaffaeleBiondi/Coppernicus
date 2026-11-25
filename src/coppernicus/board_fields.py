"""Rails, converters and links from KiCad footprint fields.

A part's power data can live on its symbol in the schematic, as fields that KiCad 8 and
later copy onto the footprint, where the board file keeps them as ``(property "NAME"
"TEXT")``. ``coppernicus.engine.kicad_fields`` reads them; this module (no engine, plain
data) turns them into the project's own v2 objects (:class:`Source`, :class:`Load`,
:class:`Link`) and merges them with a project, so the engine is unchanged. See
``docs/kicad-fields-proposal.md`` for the decisions, ``docs/agents.md`` for the use.

Fields, on the part (every one optional; ``PI_RAIL`` is required with any per-rail field):

==================  ===========  ===========================================================
``PI_RAIL``         ``+3V3``     The net the part is a source or load on. Several:
                                 ``VIN;+3V3``; every per-rail field below then gives one
                                 value per rail, by position (an empty one skips that rail).
``PI_SOURCE``       ``3.3V``     The part holds the rail at this voltage.
``PI_LOAD``         ``2A``       The part draws this current, split across its pins.
``PI_PINS``         ``1,2``      Which pads take part (numbers or globs); default all.
``PI_LIMIT_DROP``   ``2%``       A drop limit for the rail (the strictest one wins).
``PI_KIND``         ``ldo``      A source's kind: ``source`` (default), ``ldo`` or ``smps``.
``PI_EFF``          ``90%``      A switcher's efficiency.
``PI_IQ``           ``2mA``      A regulator's quiescent current (A, mA, uA/µA).
``PI_INPUT``        ``VIN``      A regulator's input net (its pads there; ``PI_INPUT_PINS``
                                 to choose them).
``PI_GND``          ``GND``      The ground net of a source's or load's ground pins (the
                                 return path; ``PI_GND_PINS`` to choose them).
``PI_LINK``         ``50mOhm``   On a two-net part (ferrite, sense resistor, fuse): a link
                                 of this resistance between the nets of its pads. Not with
                                 ``PI_RAIL``.
``PI_LINK_PINS``    ``1,2;5``    The two sides of a link on a part with more than two nets
                                 (load switch, MOSFET, 3-pin fuse): side A's pads ; side B's.
``PI_VOUT``         ``3.3V``     A fixed-output part's voltage: the source voltage when the
                                 instance has ``PI_RAIL`` and no ``PI_SOURCE``.
``PI_IMAX``         ``2A``       A source's rated output current (shown, not judged yet).
``PI_FSW``          ``1.2MHz``   A switcher's fixed switching frequency (stored, not used yet).
``PI_SOURCE_REF``   ``L1``       The part whose pads hold the rail (an external-inductor
                                 buck's inductor); ``PI_PINS`` then selects that part's pads,
                                 while input, ground and kind stay on this part. A list,
                                 ``BT1,BT5``: the pads of every listed part, one terminal at
                                 the source voltage (cell holders in parallel); ``PI_PINS``
                                 then takes ``REF.PAD`` entries, or a pad for every listed part.
``PI_GND_REF``      ``BT4``      The part whose pads are the source's (or load's) ground pins
                                 on ``PI_GND``, when not this part (one part).
==================  ===========  ===========================================================

**Library and instance.** ``PI_KIND``, ``PI_EFF``, ``PI_IQ``, ``PI_VOUT``, ``PI_IMAX`` and
``PI_FSW`` say what the part is (its library symbol): they need no ``PI_RAIL``, and a part
that carries only them is part data, used once its instance has ``PI_RAIL``. With
``PI_RAIL``, one such value applies to every rail where the part is a source (on a rail where
it is a load it is not used), or one per rail with ``;``. The other fields say where the part
sits (the schematic instance) and need ``PI_RAIL``.

Remote sense stays in the project file for now: when the fields replace a project rail, the
sense pins of its sources are carried over by part.

Every value carries its unit: a bare number is refused, so "2" can never be read as 2 A
when 2 mA was meant. Nothing is assumed: a part with a rail but no voltage or current, a
regulator without its input or a switcher without its efficiency, a rail with no source,
all are errors that name the part, the field and its text.

**KiCad wins.** For a net the fields define, the fields give the design data (sources,
loads, pins, converter data); a project-file rail of that net is ignored, with a warning
listing the differences, unless it sets ``override_board_fields: true``. ``PI_LIMIT_DROP``
wins over the project's ``max_drop_pct``; the other limits come from the project file.
"""

from __future__ import annotations

import copy
import dataclasses
import fnmatch
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from coppernicus.project import SOURCE_KINDS, Limits, Link, Load, PinSpec, Project, Rail, Source, parse_pins

PREFIX = "PI_"
# What the part is (its library symbol): no PI_RAIL needed; with PI_RAIL, one value for every
# rail where the part is a source, or one per rail.
LIBRARY_FIELDS = ("PI_KIND", "PI_EFF", "PI_IQ", "PI_VOUT", "PI_IMAX", "PI_FSW")
# Where it sits (the schematic instance): one value per rail of PI_RAIL.
RAIL_FIELDS = ("PI_SOURCE", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP", "PI_INPUT", "PI_INPUT_PINS",
               "PI_GND", "PI_GND_PINS", "PI_SOURCE_REF", "PI_GND_REF")
PER_RAIL = (*RAIL_FIELDS, *LIBRARY_FIELDS)
FIELDS = ("PI_RAIL", *PER_RAIL, "PI_LINK", "PI_LINK_PINS")
INSTANCE_FIELDS = ("PI_RAIL", "PI_SOURCE", "PI_SOURCE_REF", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP",
                   "PI_INPUT", "PI_INPUT_PINS", "PI_GND", "PI_GND_PINS", "PI_GND_REF", "PI_LINK",
                   "PI_LINK_PINS")
SOURCE_ONLY = ("PI_INPUT", "PI_INPUT_PINS", "PI_SOURCE_REF")
_OHM = ("Ohm", "Ω", "Ω")                    # Ohm, Greek capital omega, the Ohm sign
_MICRO = ("u", "µ", "μ")                     # u, the micro sign, Greek small mu
# Units by field: unit text -> divisor (value = number / divisor, exactly rounded).
UNITS: Dict[str, Dict[str, float]] = {
    "PI_SOURCE": {"V": 1.0},
    "PI_LOAD": {"A": 1.0, "mA": 1e3},
    "PI_LIMIT_DROP": {"%": 1.0},
    "PI_EFF": {"%": 100.0},
    "PI_IQ": {"A": 1.0, "mA": 1e3, **{f"{m}A": 1e6 for m in _MICRO}},
    "PI_LINK": {**{o: 1.0 for o in _OHM}, **{f"m{o}": 1e3 for o in _OHM}},
    "PI_VOUT": {"V": 1.0},
    "PI_IMAX": {"A": 1.0, "mA": 1e3},
    "PI_FSW": {"Hz": 1.0, "kHz": 1e-3, "MHz": 1e-6},
}
SHOWN = {"PI_SOURCE": "V", "PI_LOAD": "A or mA", "PI_LIMIT_DROP": "%", "PI_EFF": "%",
         "PI_IQ": "A, mA or uA", "PI_LINK": "Ohm or mOhm", "PI_VOUT": "V", "PI_IMAX": "A or mA",
         "PI_FSW": "Hz, kHz or MHz"}
EXAMPLES = {"PI_SOURCE": "3.3V or -5V", "PI_LOAD": "2A or 250mA", "PI_LIMIT_DROP": "2%",
            "PI_EFF": "90%", "PI_IQ": "2mA or 50uA", "PI_LINK": "50mOhm", "PI_VOUT": "3.3V or -5V",
            "PI_IMAX": "2A or 500mA", "PI_FSW": "500kHz or 2.2MHz"}
_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s?(\S*)$")


class FieldError(ValueError):
    """A footprint field Coppernicus cannot use: the message names the part, the field and
    its text, and says what to write instead."""


@dataclass
class BoardFields:
    """What a board file says in its footprints: ``fields`` (reference → {field name:
    text}, ``PI_*`` only) and ``pad_nets`` (reference → {pad number: net}) for those parts.
    Plain data, so the app can pass what it read."""
    fields: Dict[str, Dict[str, str]] = field(default_factory=dict)
    pad_nets: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class FieldValue:
    """One value read from a field, kept with its text for the assumptions."""
    ref: str
    field: str
    text: str

    def origin(self) -> str:
        return f'from KiCad field {self.field} "{self.text}" on {self.ref}'


@dataclass
class FieldTerm:
    """A source or load built from fields, with every field value it used."""
    term: Union[Source, Load]
    values: List[Tuple[str, FieldValue]]          # (what it set, e.g. "3.3 V", the value)
    owner: str = ""             # the part carrying the fields, when not the term's ref
    warnings: List[str] = field(default_factory=list)

    @property
    def part(self) -> str:
        """The part whose fields made it (a source held at another part's pads, through
        ``PI_SOURCE_REF``, is the part that carries the fields)."""
        return self.owner or self.term.ref

    def text(self) -> str:
        kind = "source" if isinstance(self.term, Source) else "load"
        items = [f"{what} ({fv.origin()})" for what, fv in self.values]
        if not any(fv.field == "PI_PINS" for _, fv in self.values):
            items.insert(1, "every pin on the net")
        return f"{kind} {self.part} " + ", ".join(items)


@dataclass
class FieldRail:
    """What the fields say about one net."""
    net: str
    sources: List[FieldTerm] = field(default_factory=list)
    loads: List[FieldTerm] = field(default_factory=list)
    limit: Optional[Tuple[float, FieldValue]] = None           # the strictest PI_LIMIT_DROP
    parts: List[str] = field(default_factory=list)              # refs with PI_RAIL on it

    def missing(self) -> Optional[str]:
        """Why it is not a rail by itself (no source, no load), or None."""
        if not self.sources:
            return (f"{self.net}: loads {', '.join(t.part for t in self.loads)} have PI_LOAD "
                    "but no part on this net has PI_SOURCE (the voltage that holds the rail), and "
                    "no link or net tie joins it to a net that has one")
        if not self.loads:
            return (f"{self.net}: {', '.join(t.part for t in self.sources)} has PI_SOURCE but "
                    "no part on this net has PI_LOAD (a current drawn from the rail), and it feeds "
                    "no regulator input or link")
        return None

    @property
    def warnings(self) -> List[str]:
        return [w for t in self.sources + self.loads for w in t.warnings]

    def rail(self) -> Rail:
        return Rail(self.net, sources=[copy.deepcopy(t.term) for t in self.sources],
                    loads=[copy.deepcopy(t.term) for t in self.loads],
                    limits=Limits(max_drop_pct=self.limit[0] if self.limit else None))

    def values_text(self) -> str:
        items = [t.text() for t in self.sources + self.loads]
        if self.limit is not None:
            items.append(f"limit max_drop_pct {self.limit[0]:g} ({self.limit[1].origin()})")
        return "; ".join(items)

    def note(self) -> str:
        """The assumption line: every value of this rail, each with the field it came from."""
        return (f"Rail {self.net} from KiCad footprint fields (from KiCad field PI_RAIL on "
                f"{', '.join(self.parts)}): {self.values_text()}.")

    def listing(self) -> Dict[str, Any]:
        """As plain data, each terminal with the fields it came from."""
        def term(t: FieldTerm) -> Dict[str, Any]:
            d = {k: v for k, v in dataclasses.asdict(t.term).items()
                 if v not in (None, "", 0.0) or k in ("voltage", "current")}
            d.pop("name", None)
            if t.part != t.term.ref:
                d["fields_on"] = t.part
            d["fields"] = {fv.field: fv.text for _, fv in t.values}
            return d
        return {"net": self.net, "parts": list(self.parts), "complete": self.missing() is None,
                "sources": [{**term(t), "voltage_v": t.term.voltage, "from": t.values[0][1].origin()}
                            for t in self.sources],
                "loads": [{**term(t), "current_a": t.term.current, "from": t.values[0][1].origin()}
                          for t in self.loads],
                "limits": ({"max_drop_pct": self.limit[0], "from": self.limit[1].origin()}
                           if self.limit else {}),
                "warnings": self.warnings}


def quantity(ref: str, name: str, text: str) -> float:
    """A field value with its unit, as a number in V, A, Ω, % (a limit) or a fraction (an
    efficiency). Raises FieldError."""
    units = UNITS[name]
    shown = f'{ref}: {name} = "{text}"'
    if "," in text:
        raise FieldError(f"{shown}: use a dot for decimals, e.g. {EXAMPLES[name]}")
    m = _QUANTITY.match(text.strip())
    if m is None or any(c.isdigit() for c in m.group(2)):
        raise FieldError(f"{shown} is not a value with its unit; write it as {EXAMPLES[name]}")
    number, unit = m.groups()
    if not unit:
        raise FieldError(f"{shown}: give the unit ({SHOWN[name]}), e.g. {EXAMPLES[name]}; a bare "
                         "number is refused so that no unit is guessed")
    if unit not in units:
        raise FieldError(f"{shown}: {name} takes {SHOWN[name]}, not {unit}; e.g. {EXAMPLES[name]}")
    div = units[unit]
    x = float(number) / div if div >= 1 else float(Decimal(number) / Decimal(repr(div)))
    if name in ("PI_IMAX", "PI_FSW") and x <= 0:
        raise FieldError(f"{shown}: the value is > 0")
    if name in ("PI_LOAD", "PI_IQ", "PI_LINK") and x < 0:
        raise FieldError(f"{shown}: the value is ≥ 0" + (" (a part that feeds the rail is a "
                                                          "PI_SOURCE)" if name == "PI_LOAD" else ""))
    if name == "PI_LIMIT_DROP" and x <= 0:
        raise FieldError(f"{shown}: a drop limit is > 0")
    if name == "PI_EFF" and not 0 < x <= 1:
        raise FieldError(f"{shown}: an efficiency is above 0 and at most 100 %")
    return x


def _split(text: Optional[str]) -> Optional[List[str]]:
    """The values of a field, one per rail; None when the field is absent or blank."""
    if text is None or not text.strip():
        return None
    return [v.strip() for v in text.split(";")]


def _at(values: Optional[List[str]], i: int) -> Optional[str]:
    return values[i] if values is not None and values[i] else None


def _pins(ref: str, text: Optional[str], name: str, values: List) -> PinSpec:
    if not text:
        return None
    label = {"PI_PINS": "pins", "PI_INPUT_PINS": "input pins", "PI_GND_PINS": "ground pins"}[name]
    values.append((f"{label} {text}", FieldValue(ref, name, text)))
    return parse_pins(text)


def _check_names(ref: str, f: Mapping[str, str]) -> None:
    unknown = sorted(n for n in f if n.upper().startswith(PREFIX) and n not in FIELDS)
    if unknown:
        raise FieldError(f"{ref}: unknown field {', '.join(unknown)} (the fields are "
                         f"{', '.join(FIELDS)}, in upper case)")


def field_rails(fields: Mapping[str, Mapping[str, str]],
                pad_nets: Optional[Mapping[str, Mapping[str, str]]] = None) -> Dict[str, FieldRail]:
    """The rails the parts' fields describe (``fields``: reference → {field name: text}), by
    net, complete or not. ``pad_nets`` (reference → {pad: net}, as read from the board) checks
    that a ``PI_SOURCE_REF`` part has a pad on its rail. Raises FieldError for a field that
    cannot be used."""
    rails: Dict[str, FieldRail] = {}
    inputs: List[Tuple[str, str]] = []               # (ref, input net) of field regulators
    for ref in sorted(fields, key=_ref_key):
        f = fields[ref]
        _check_names(ref, f)
        nets = _split(f.get("PI_RAIL"))
        values = {n: _split(f.get(n)) for n in PER_RAIL}
        placed = [n for n in RAIL_FIELDS if values[n] is not None]    # instance data
        link = _split(f.get("PI_LINK")) is not None
        if _split(f.get("PI_LINK_PINS")) is not None and not link:
            raise FieldError(f"{ref}: PI_LINK_PINS without PI_LINK: the sides belong to a link "
                             "(PI_LINK, e.g. 20mOhm)")
        if link:
            if nets is not None or placed:
                raise FieldError(f"{ref}: PI_LINK with other fields: a link part has no PI_RAIL; "
                                 "its two nets are those of its pads")
            continue                          # library fields on a link part: part data
        if nets is None:
            if placed:
                raise FieldError(f"{ref}: {', '.join(placed)} without PI_RAIL: set PI_RAIL to the "
                                 "net it is on (e.g. +3V3)")
            continue                          # library fields only: part data, no rail yet
        if any(not n for n in nets):
            raise FieldError(f'{ref}: PI_RAIL = "{f["PI_RAIL"]}" has an empty net name')
        if values["PI_SOURCE"] is None and values["PI_LOAD"] is None and values["PI_VOUT"] is None:
            raise FieldError(f"{ref}: PI_RAIL {f['PI_RAIL']} but no PI_SOURCE or PI_LOAD: give the "
                             "voltage it holds (PI_SOURCE, e.g. 3.3V, or PI_VOUT on a fixed-output "
                             "part) or the current it draws (PI_LOAD, e.g. 2A)")
        for name, vals in values.items():
            if vals is None or len(vals) == len(nets) or (name in LIBRARY_FIELDS and len(vals) == 1):
                continue
            rule = ("give one value for every rail, or one per rail separated by ';'"
                    if name in LIBRARY_FIELDS else
                    "give one per rail, separated by ';' (leave one empty to skip that rail)")
            raise FieldError(f'{ref}: {name} = "{f[name]}" has {len(vals)} value(s) for '
                             f'{len(nets)} rail(s) (PI_RAIL = "{f["PI_RAIL"]}"): {rule}')
        positions = []
        for i in range(len(nets)):
            v: Dict[str, Optional[str]] = {}
            explicit = set()                  # library fields given rail by rail
            for n in PER_RAIL:
                vals = values[n]
                if vals is not None and n in LIBRARY_FIELDS and len(vals) == 1:
                    v[n] = vals[0] or None    # one value for every rail of the part
                else:
                    v[n] = _at(vals, i)
                    if vals is not None and n in LIBRARY_FIELDS:
                        explicit.add(n)
            positions.append((v, explicit))
        if not any(v["PI_SOURCE"] or (v["PI_VOUT"] and not v["PI_LOAD"]) for v, _ in positions):
            # A source nowhere: its library data cannot be part data for a source rail, so it
            # is a mistake (a regulator placed as a load) and is refused, rail by rail.
            positions = [(v, set(LIBRARY_FIELDS)) for v, _ in positions]
        for i, (net, (v, explicit)) in enumerate(zip(nets, positions)):
            term, limit = _term(ref, net, i, v, explicit)
            rail = rails.setdefault(net, FieldRail(net))
            rail.parts.append(ref)
            if isinstance(term.term, Source):
                rail.sources.append(term)
                if term.term.input_net:
                    inputs.append((ref, term.term.input_net))
            else:
                rail.loads.append(term)
            if limit is not None and (rail.limit is None or limit[0] < rail.limit[0]):
                rail.limit = limit
    for ref, net in inputs:              # a regulator that is also a load on its input net
        if net in rails and any(t.term.ref == ref for t in rails[net].loads):
            raise FieldError(f"{ref}: PI_LOAD on {net} and PI_INPUT {net}: its input current would "
                             f"be counted twice; remove the PI_LOAD on {net}, PI_INPUT draws it")
    if pad_nets:            # the parts PI_SOURCE_REF and PI_GND_REF name have pads where they hold
        for net, fr in rails.items():
            for t in fr.sources:
                held = ([t.term.ref] if t.part != t.term.ref else []) + list(dict.fromkeys(
                    e.split(".", 1)[0] for e in (t.term.extra_pins or ())))
                for other in held:
                    _check_part(t.part, "PI_SOURCE_REF", other, net, pad_nets,
                                "name the part whose pads hold the rail (e.g. the inductor of an "
                                "external-inductor buck, or every cell holder)")
            for t in fr.sources + fr.loads:
                gref = t.term.ground_ref
                if gref and gref != t.part and any(fv.field == "PI_GND_REF" for _, fv in t.values):
                    _check_part(t.part, "PI_GND_REF", gref, t.term.ground_net, pad_nets,
                                "name the part whose pads are the ground pins")
    return rails


def _check_part(owner: str, field_name: str, ref: str, net: str,
                pad_nets: Mapping[str, Mapping[str, str]], hint: str) -> None:
    pads = pad_nets.get(ref)
    if pads is None:
        raise FieldError(f"{owner}: {field_name} {ref}: no part {ref} on the board; {hint}")
    if net not in pads.values():
        on = ", ".join(sorted({n for n in pads.values() if n})) or "no net"
        raise FieldError(f"{owner}: {field_name} {ref} has no pad on {net} (its pads are on {on}); "
                         f"{hint}")


def _term(ref: str, net: str, i: int, v: Mapping[str, Optional[str]], explicit=frozenset()
          ) -> Tuple[FieldTerm, Optional[Tuple[float, FieldValue]]]:
    """The source or load at rail position ``i`` of a part, and its drop limit. ``explicit``:
    the library fields given rail by rail (the others apply to every rail where the part is a
    source, and are not used where it is a load)."""
    src, load, vout = v["PI_SOURCE"], v["PI_LOAD"], v["PI_VOUT"]
    if src and load:
        raise FieldError(f"{ref}: both PI_SOURCE and PI_LOAD for rail {net}: a part is either the "
                         "source of a rail or a load on it")
    if not src and not load and not vout:
        raise FieldError(f"{ref}: rail {net} (PI_RAIL position {i + 1}) has neither PI_SOURCE nor "
                         "PI_LOAD")
    for name, of in (("PI_INPUT_PINS", "PI_INPUT"), ("PI_GND_PINS", "PI_GND"), ("PI_GND_REF", "PI_GND")):
        if v[name] and not v[of]:
            raise FieldError(f"{ref}: {name} without {of} for rail {net}: set {of} to the net")
    values: List[Tuple[str, FieldValue]] = []
    warnings: List[str] = []
    if load:
        extra = [n for n in SOURCE_ONLY if v[n]] + [n for n in LIBRARY_FIELDS if n in explicit and v[n]]
        if extra:
            raise FieldError(f"{ref}: {extra[0]} is for a source (PI_SOURCE or PI_VOUT), not a load "
                             f"on {net}" + ("; a regulator holds its output rail and draws its input "
                                            "through PI_INPUT" if extra[0] in LIBRARY_FIELDS else ""))
        current = quantity(ref, "PI_LOAD", load)
        values.append((f"{current:g} A", FieldValue(ref, "PI_LOAD", load)))
        pins = _pins(ref, v["PI_PINS"], "PI_PINS", values)
        ground, gpins, gref = _ground(ref, v, values)
        term: Union[Source, Load] = Load(ref, current, pins, ground_ref=gref if gref != ref else "",
                                         ground_pins=gpins, ground_net=ground)
    else:
        if src:
            voltage = quantity(ref, "PI_SOURCE", src)
            values.append((f"{voltage:g} V", FieldValue(ref, "PI_SOURCE", src)))
            if vout and not math.isclose(quantity(ref, "PI_VOUT", vout), voltage, rel_tol=1e-12):
                warnings.append(f'{ref}: PI_SOURCE "{src}" and PI_VOUT "{vout}" differ on {net}: '
                                "PI_SOURCE (the placed part's value) is used, not the library's "
                                "PI_VOUT")
        else:
            voltage = quantity(ref, "PI_VOUT", vout)
            values.append((f"{voltage:g} V", FieldValue(ref, "PI_VOUT", vout)))
        # PI_SOURCE_REF: the part(s) whose pads hold the rail; this part first when listed.
        refs = list(dict.fromkeys(r.strip() for r in (v["PI_SOURCE_REF"] or "").split(",") if r.strip()))
        if ref in refs:
            refs.remove(ref)
            refs.insert(0, ref)
        out_ref = refs[0] if refs and refs[0] != ref else ""
        if refs and refs != [ref]:
            values.append((f"output on {', '.join(refs)}",
                           FieldValue(ref, "PI_SOURCE_REF", v["PI_SOURCE_REF"])))
        if len(refs) > 1:
            pins, extra = _terminal_pins(ref, refs, v["PI_PINS"], values)
        else:
            pins, extra = _pins(ref, v["PI_PINS"], "PI_PINS", values), None
        kind = (v["PI_KIND"] or "source").lower()
        if kind not in SOURCE_KINDS:
            raise FieldError(f'{ref}: PI_KIND = "{v["PI_KIND"]}": PI_KIND is one of '
                             f"{', '.join(SOURCE_KINDS)}")
        if v["PI_KIND"]:
            values.append((f"kind {kind}", FieldValue(ref, "PI_KIND", v["PI_KIND"])))
        regulator = kind in ("ldo", "smps")
        efficiency = None
        if v["PI_EFF"]:
            if kind != "smps":
                raise FieldError(f"{ref}: PI_EFF is for a switcher (PI_KIND smps), not a {kind}")
            efficiency = quantity(ref, "PI_EFF", v["PI_EFF"])
            values.append((f"η {efficiency:g}", FieldValue(ref, "PI_EFF", v["PI_EFF"])))
        elif kind == "smps":
            raise FieldError(f"{ref}: PI_KIND smps needs PI_EFF (its efficiency, e.g. 90%); there "
                             "is no default")
        iq = 0.0
        if v["PI_IQ"]:
            if not regulator:
                raise FieldError(f"{ref}: PI_IQ is for a regulator (PI_KIND ldo or smps)")
            iq = quantity(ref, "PI_IQ", v["PI_IQ"])
            values.append((f"I_q {iq:g} A", FieldValue(ref, "PI_IQ", v["PI_IQ"])))
        if v["PI_INPUT"] and not regulator:
            raise FieldError(f"{ref}: PI_INPUT is for a regulator (PI_KIND ldo or smps)")
        if regulator and not v["PI_INPUT"]:
            raise FieldError(f"{ref}: PI_KIND {kind} needs PI_INPUT (the net it draws from)")
        ipins = None
        if v["PI_INPUT"]:
            values.append((f"input {v['PI_INPUT']}", FieldValue(ref, "PI_INPUT", v["PI_INPUT"])))
            ipins = _pins(ref, v["PI_INPUT_PINS"], "PI_INPUT_PINS", values)
        imax = fsw = None
        if v["PI_IMAX"]:
            imax = quantity(ref, "PI_IMAX", v["PI_IMAX"])
            values.append((f"I_max {imax:g} A (shown, not judged yet)",
                           FieldValue(ref, "PI_IMAX", v["PI_IMAX"])))
        if v["PI_FSW"]:
            if kind != "smps":
                raise FieldError(f"{ref}: PI_FSW is for a switcher (PI_KIND smps), not a {kind}")
            fsw = quantity(ref, "PI_FSW", v["PI_FSW"])
            values.append((f"f_sw {fsw:g} Hz (stored, not used yet)",
                           FieldValue(ref, "PI_FSW", v["PI_FSW"])))
        ground, gpins, gref = _ground(ref, v, values)
        # PI_SOURCE_REF: the rail is held at that part's pads; input and ground stay this part's,
        # unless PI_GND_REF names the part with the ground pins.
        anchor = out_ref or ref
        gref = gref or (ref if out_ref and ground else "")
        term = Source(anchor, voltage, pins, name=ref if out_ref else "", kind=kind,
                      efficiency=efficiency, quiescent_a=iq,
                      input_ref=ref if out_ref and v["PI_INPUT"] else "", input_pins=ipins,
                      input_net=v["PI_INPUT"] or "", ground_ref=gref if gref != anchor else "",
                      ground_pins=gpins, ground_net=ground, current_limit_a=imax, fsw_hz=fsw,
                      extra_pins=extra)
        if out_ref:
            return _limited(FieldTerm(term, values, owner=ref, warnings=warnings), ref, v)
    return _limited(FieldTerm(term, values, warnings=warnings), ref, v)


def _limited(term: FieldTerm, ref: str, v: Mapping[str, Optional[str]]
             ) -> Tuple[FieldTerm, Optional[Tuple[float, FieldValue]]]:
    """``term`` with the drop limit of its rail position."""
    limit = None
    if v["PI_LIMIT_DROP"]:
        limit = (quantity(ref, "PI_LIMIT_DROP", v["PI_LIMIT_DROP"]),
                 FieldValue(ref, "PI_LIMIT_DROP", v["PI_LIMIT_DROP"]))
    return term, limit


def _ground(ref: str, v: Mapping[str, Optional[str]], values: List) -> Tuple[str, PinSpec, str]:
    """The ground net, pins and part (PI_GND, PI_GND_PINS, PI_GND_REF; "" for this part)."""
    if not v["PI_GND"]:
        return "", None, ""
    values.append((f"ground {v['PI_GND']}", FieldValue(ref, "PI_GND", v["PI_GND"])))
    gref = ""
    if v["PI_GND_REF"]:
        refs = [r.strip() for r in v["PI_GND_REF"].split(",") if r.strip()]
        if len(refs) != 1:
            raise FieldError(f'{ref}: PI_GND_REF = "{v["PI_GND_REF"]}": give one part (the power tree '
                             "takes a terminal's ground pins on one part)")
        gref = refs[0]
        values.append((f"ground pins on {gref}", FieldValue(ref, "PI_GND_REF", v["PI_GND_REF"])))
    return v["PI_GND"], _pins(ref, v["PI_GND_PINS"], "PI_GND_PINS", values), gref


def _terminal_pins(ref: str, refs: Sequence[str], text: Optional[str], values: List
                   ) -> Tuple[PinSpec, Optional[List[str]]]:
    """The pins of a terminal on several parts (PI_SOURCE_REF ``refs``, the first holding the
    rail): those of ``refs[0]``, and the others' as "REF.PAD" extra pins. PI_PINS ``text``
    takes "REF.PAD" entries (REF one of ``refs``) and plain pads, which apply to every listed
    part; none: every pad of each part on the rail."""
    if not text:
        return None, [f"{r}.*" for r in refs[1:]]
    values.append((f"pins {text}", FieldValue(ref, "PI_PINS", text)))
    per: Dict[str, List[str]] = {r: [] for r in refs}
    for entry in (e.strip() for e in text.split(",") if e.strip()):
        head, dot, pad = entry.partition(".")
        for r in ([head] if dot and head in per else refs):
            per[r].append(pad if dot and head in per else entry)
    empty = [r for r in refs if not per[r]]
    if empty:
        raise FieldError(f'{ref}: PI_PINS = "{text}": {empty[0]} gets no pad; with PI_SOURCE_REF '
                         f"{', '.join(refs)}, write REF.PAD entries (e.g. {refs[0]}.1,{empty[0]}.1) "
                         "or plain pads, which apply to every listed part")
    return per[refs[0]], [f"{r}.{p}" for r in refs[1:] for p in per[r]]


def field_links(fields: Mapping[str, Mapping[str, str]],
                pad_nets: Mapping[str, Mapping[str, str]]) -> List[Tuple[Link, FieldValue]]:
    """The links the ``PI_LINK`` fields describe: each between the two nets of its part's
    pads (``pad_nets``: reference → {pad number: net}), with those pads."""
    out = []
    for ref in sorted(fields, key=_ref_key):
        text = fields[ref].get("PI_LINK")
        if text is None or not text.strip():
            continue
        ohm = quantity(ref, "PI_LINK", text)
        pads = pad_nets.get(ref)
        if pads is None:
            raise FieldError(f"{ref}: PI_LINK needs the nets of the part's pads (read from the board file)")
        nets: Dict[str, List[str]] = {}
        for number in sorted(pads, key=_pin_key):
            if pads[number]:
                nets.setdefault(pads[number], []).append(number)
        sides = fields[ref].get("PI_LINK_PINS")
        if sides is not None and sides.strip():
            (net_a, pins_a), (net_b, pins_b) = _link_sides(ref, sides, pads)
        elif len(nets) != 2:
            raise FieldError(f"{ref}: PI_LINK on a part whose pads are on {len(nets)} net(s) "
                             f"({', '.join(nets) or 'none'}); a link joins exactly two nets: on a "
                             'part with more, choose its two sides with PI_LINK_PINS (e.g. "1,2;5": '
                             "side A's pads ; side B's)")
        else:
            (net_a, pins_a), (net_b, pins_b) = nets.items()
        out.append((Link(ref, ref, pins_a, net_a, pins_b, net_b, ohm), FieldValue(ref, "PI_LINK", text)))
    return out


def _link_sides(ref: str, text: str, pads: Mapping[str, str]) -> List[Tuple[str, List[str]]]:
    """The two sides PI_LINK_PINS chooses ("1,2;5": side A's pads ; side B's), each its net
    and its pads, checked against the part's pads."""
    shown = f'{ref}: PI_LINK_PINS = "{text}"'
    parts = [p.strip() for p in text.split(";")]
    if len(parts) != 2 or not all(parts):
        raise FieldError(f"{shown}: give the two sides as side A ; side B, e.g. 1,2;5")
    out = []
    for label, part in zip("AB", parts):
        patterns = parse_pins(part)
        if patterns is None:
            raise FieldError(f"{shown}: name side {label}'s pads (numbers or globs), not all")
        chosen = [n for n in sorted(pads, key=_pin_key) if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
        if not chosen:
            raise FieldError(f"{shown}: side {label} ({part}) matches no pad of {ref} (its pads: "
                             f"{', '.join(sorted(pads, key=_pin_key))})")
        on = sorted({pads[n] for n in chosen if pads[n]})
        if len(on) != 1:
            raise FieldError(f"{shown}: side {label}'s pads are on {len(on)} net(s) "
                             f"({', '.join(on) or 'none'}); each side's pads are on one net, so two "
                             "nets are not one side")
        out.append((on[0], chosen))
    if out[0][0] == out[1][0]:
        raise FieldError(f"{shown}: both sides are on the same net {out[0][0]}; a link joins two "
                         "different nets")
    return out


# ---------------------------------------------------------------------- merging

@dataclass
class Origin:
    """Where a rail of the merged project comes from: ``"fields"`` (the KiCad fields, with
    the project's other limits), ``"project"`` (the project file; the fields do not define
    the net, or leave it incomplete) or ``"overridden"`` (the project file, with
    ``override_board_fields``, over the fields' rail in ``board_fields``)."""
    net: str
    source: str
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    board_fields: Optional[Dict[str, Any]] = None


@dataclass
class Merged:
    """The project with the fields merged in (a copy: the loaded one is never changed), where
    each rail came from, and every warning (the per-rail ones and those about links)."""
    project: Project
    origins: Dict[str, Origin] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    link_notes: List[str] = field(default_factory=list)
    # The warnings about each link, under both of its nets (a net need not be a rail).
    link_warnings: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def from_fields(self) -> List[str]:
        return [n for n, o in self.origins.items() if o.source == "fields"]

    def notes(self) -> List[str]:
        """Every assumption line, once (rails, then links)."""
        lines = [n for o in self.origins.values() for n in o.notes] + self.link_notes
        return list(dict.fromkeys(lines))

    def annotate(self, result) -> None:
        """Add the notes and warnings to a result's ``assumptions`` and ``warnings`` (a
        power-tree result, whose rails are not reported one by one)."""
        result.assumptions.extend(n for n in self.notes() if n not in result.assumptions)
        result.warnings.extend(w for w in self.warnings if w not in result.warnings)


def apply(project: Project, read: Callable[[], BoardFields], use: Optional[bool] = None,
          ties: Sequence[Tuple[str, str]] = ()) -> Merged:
    """The project merged with the board's fields, read by ``read()`` only when they are
    on: ``use`` (None: the project's ``use_board_fields``, on by default). ``ties``: the
    board's net-tie pairs (see :func:`merge`)."""
    if not (project.use_board_fields if use is None else use):
        return Merged(project)
    found = read()
    if not found.fields:
        return Merged(project)
    return merge(project, found.fields, found.pad_nets, ties)


def merge(project: Project, fields: Mapping[str, Mapping[str, str]],
          pad_nets: Optional[Mapping[str, Mapping[str, str]]] = None,
          ties: Sequence[Tuple[str, str]] = ()) -> Merged:
    """The project with the KiCad fields merged in, as the owner decided:

    * a net the fields define completely takes its sources, loads, pins and converter data
      from them; a project-file rail of that net is ignored (a warning lists the differences;
      none when they agree), unless it sets ``override_board_fields``, which keeps it;
    * on a field rail, ``PI_LIMIT_DROP`` wins over the project's ``max_drop_pct`` (a warning
      when they differ); its other limits and ``follow_net_ties`` come from the project rail;
      remote sense pins are carried over from it by part;
    * a rail with a source and no load is complete when it feeds a regulator's input or a
      link (it draws their estimated current), through the board's net ties (``ties``: pairs
      of nets a net-tie footprint joins) as the solve does; a net the fields leave incomplete keeps the
      project's rail (a warning) or, with none, is an error naming the missing field;
    * loads on a net tied to a driven net go into the driven rail; a project rail of the tied
      net that holds only limits stays as it is (with a ``PI_LIMIT_DROP`` there applied to
      it); with none, a ``PI_LIMIT_DROP`` there stays on that net, in a rail that holds only
      the limit;
    * ``PI_LINK`` parts replace project links on the same part (a warning when they differ).
    """
    rails = field_rails(fields, pad_nets)
    links = field_links(fields, pad_nets or {})
    out = copy.deepcopy(project)
    merged = Merged(out)
    link_notes: Dict[str, List[str]] = {}
    for link, fv in links:
        note = (f"Link {link.ref} {link.resistance_ohm:g} Ω between {link.net_a} (pins "
                f"{', '.join(link.pins_a)}) and {link.net_b} (pins {', '.join(link.pins_b)}) "
                f"({fv.origin()}).")
        old = next((k for k in out.links if k.ref == link.ref), None)
        if old is None:
            out.links.append(link)
        else:
            if not _same_link(old, link):
                warning = (f"project file link {link.ref} ignored: KiCad field PI_LINK defines it "
                           f"(KiCad {_link_text(link)}, project {_link_text(old)})")
                merged.warnings.append(warning)
                for net in dict.fromkeys((link.net_a, link.net_b, old.net_a, old.net_b)):
                    merged.link_warnings.setdefault(net, []).append(warning)
            out.links[out.links.index(old)] = link
        merged.link_notes.append(note)
        for net in (link.net_a, link.net_b):
            link_notes.setdefault(net, []).append(note)

    own = {r.net: r for r in out.rails}
    # Loads on a net that a net tie joins to a driven net go into that rail: the per-rail
    # solve joins tied copper, and finds their pads on the tied net. A project rail of the
    # tied net that holds only limits stays, limits only.
    tie_notes: Dict[str, List[str]] = {}
    folded: Dict[str, Tuple[FieldRail, str]] = {}       # tied net -> (its fields, the driver)
    for net in sorted(rails):
        fr = rails[net]
        if fr.sources or not fr.loads or (net in own and (own[net].sources or own[net].loads
                                                          or own[net].override_board_fields)):
            continue
        drivers = sorted(n for n in _group(net, ties) - {net}
                         if (n in rails and rails[n].sources) or (n in own and own[n].sources))
        if not drivers:
            continue
        driver = drivers[0]
        if driver in rails and rails[driver].sources:
            rails[driver].loads += fr.loads
            rails[driver].parts += fr.parts
        else:
            own[driver].loads += [copy.deepcopy(t.term) for t in fr.loads]
        tie_notes.setdefault(driver, []).append(
            f"Rail {driver}: loads {', '.join(t.term.ref for t in fr.loads)} on {net}, joined to it "
            f"by a net tie (from KiCad field PI_RAIL {net}).")
        folded[net] = (fr, driver)
        del rails[net]
    trial = copy.deepcopy(out)                # every field rail with a source in place, to
    for net, fr in rails.items():             # see what a source-only rail feeds
        if fr.sources and not (net in own and own[net].override_board_fields):
            trial.rails = [r for r in trial.rails if r.net != net] + [fr.rail()]
    # Complete: a source and a load; a source feeding a regulator or a link; or loads on a
    # net that links and net ties join to a driven net (the loads behind a ferrite).
    joins = [(k.net_a, k.net_b) for k in trial.links] + list(ties)
    driven = {r.net for r in trial.rails if r.sources}

    def complete(net: str, fr: FieldRail) -> bool:
        if fr.sources:
            return bool(fr.loads) or _feeds(trial, net, ties)
        return bool(fr.loads) and bool((_group(net, joins) - {net}) & driven)
    missing = {net: None if complete(net, fr) else fr.missing() for net, fr in rails.items()}

    new_rails: List[Rail] = []
    for r in out.rails:
        fr = rails.get(r.net)
        if fr is None and r.net in folded:            # limits only, its loads tied into a rail
            tied, driver = folded[r.net]
            new_rails.append(r)
            origin = merged.origins[r.net] = Origin(
                r.net, "project",
                [f"Rail {r.net}: limits only, from the project file; its loads "
                 f"{', '.join(t.term.ref for t in tied.loads)} (from KiCad field PI_RAIL {r.net}) "
                 f"are in rail {driver}, joined to it by a net tie."] + link_notes.get(r.net, []))
            _kicad_drop_limit(tied, r, origin, f"on a net tied into rail {driver}")
        elif fr is None:
            new_rails.append(r)
            merged.origins[r.net] = Origin(r.net, "project", list(link_notes.get(r.net, [])))
        elif r.override_board_fields:
            new_rails.append(r)
            origin = merged.origins[r.net] = Origin(
                r.net, "overridden",
                [f"Rail {r.net} from the project file: override_board_fields is set, so it replaces "
                 f"the rail of the KiCad footprint fields ({fr.values_text()})."]
                + link_notes.get(r.net, []), board_fields=fr.listing())
            _kicad_drop_limit(fr, r, origin, "even under override_board_fields")
        elif missing[r.net] is not None:
            new_rails.append(r)
            origin = merged.origins[r.net] = Origin(
                r.net, "project", list(link_notes.get(r.net, [])),
                [f"KiCad fields on {r.net} are incomplete ({missing[r.net]}): the project file's "
                 "rail is used"], board_fields=fr.listing())
            _kicad_drop_limit(fr, r, origin, "even though the fields' rail is incomplete")
        else:
            rail, origin = _from_fields(fr, r, pad_nets)
            origin.notes += link_notes.get(r.net, [])
            new_rails.append(rail)
            merged.origins[r.net] = origin
    errors = []
    for net in sorted(n for n in rails if n not in own):
        if missing[net] is not None:
            errors.append(missing[net])
            continue
        rail, origin = _from_fields(rails[net], None, pad_nets)
        origin.notes += link_notes.get(net, [])
        new_rails.append(rail)
        merged.origins[net] = origin
    if errors:
        raise FieldError("Incomplete rails in the KiCad footprint fields: " + "; ".join(errors)
                         + ". Add the missing field, or define the rail in the project file")
    for net, (tied, driver) in folded.items():   # a KiCad limit stays on its net, limits only
        if net in own or tied.limit is None:
            continue
        new_rails.append(Rail(net, limits=Limits(max_drop_pct=tied.limit[0])))
        merged.origins[net] = Origin(
            net, "fields",
            [f"Rail {net}: limit max_drop_pct {tied.limit[0]:g} ({tied.limit[1].origin()}), limits "
             f"only: its loads {', '.join(t.term.ref for t in tied.loads)} are in rail {driver}, "
             "joined to it by a net tie; a KiCad limit always applies to its net."]
            + link_notes.get(net, []))
    out.rails = new_rails
    for driver, notes in tie_notes.items():
        merged.origins[driver].notes += notes
    merged.warnings = [w for o in merged.origins.values() for w in o.warnings] + merged.warnings
    return merged


def _group(net: str, joins: Sequence[Tuple[str, str]]) -> set:
    """``net`` and every net the pairs in ``joins`` (links, net ties) join to it."""
    nets, todo = {net}, [net]
    while todo:
        n = todo.pop()
        for a, b in joins:
            for x, y in ((a, b), (b, a)):
                if x == n and y not in nets:
                    nets.add(y)
                    todo.append(y)
    return nets


def _feeds(project: Project, net: str, ties: Sequence[Tuple[str, str]] = ()) -> bool:
    """Whether a rail on ``net`` (and the nets tied to it) delivers current to a regulator
    input or a link, at nominal voltages, as the per-rail solve estimates it. An estimate
    that cannot be made counts as yes: the solve reports it."""
    from coppernicus.power_tree import EstimateError, estimated_draws
    try:
        return bool(estimated_draws(project, sorted(_group(net, ties)), list(ties)))
    except EstimateError:
        return True


def _kicad_drop_limit(fr: FieldRail, rail: Rail, origin: Origin, why: str = "") -> None:
    """Set the fields' PI_LIMIT_DROP on ``rail``: a KiCad limit always applies to its net (a
    project rail cannot loosen it), with a warning when the project's max_drop_pct differs."""
    if fr.limit is None:
        return
    pct = rail.limits.max_drop_pct
    if pct is not None and not math.isclose(pct, fr.limit[0], rel_tol=1e-9):
        origin.warnings.append(f"KiCad field PI_LIMIT_DROP gives max_drop_pct {fr.limit[0]:g} on "
                               f"{fr.net} ({fr.limit[1].origin()}); the project file's {pct:g} is "
                               "not used")
    rail.limits = dataclasses.replace(rail.limits, max_drop_pct=fr.limit[0])
    if why:
        origin.notes.append(f"Rail {fr.net}: limit max_drop_pct {fr.limit[0]:g} "
                            f"({fr.limit[1].origin()}), {why}: a KiCad limit always applies to "
                            "its net.")


def _from_fields(fr: FieldRail, own: Optional[Rail], pads: Optional[PadNets] = None
                 ) -> Tuple[Rail, Origin]:
    """The fields' rail, with what it keeps from the project rail ``own`` of its net, and its
    origin (notes and warnings)."""
    rail = fr.rail()
    origin = Origin(fr.net, "fields", [fr.note()], list(fr.warnings), board_fields=fr.listing())
    if own is None:
        return rail, origin
    # A project rail with no sources and no loads only adds limits: nothing to compare.
    diffs = _differences(rail, own, pads) if own.sources or own.loads else []
    if diffs:
        origin.warnings.append(f"project file rail {fr.net} ignored: KiCad fields define it "
                               f"({'; '.join(diffs)}); set override_board_fields: true on the rail "
                               "to use the project's values")
    pct = own.limits.max_drop_pct
    rail.limits = copy.deepcopy(own.limits)
    _kicad_drop_limit(fr, rail, origin)
    if fr.limit is None and pct is not None:
        origin.notes.append(f"Rail {fr.net}: limit max_drop_pct {pct:g} from the project file (no "
                            "PI_LIMIT_DROP on this net).")
    others = [k for k in ("max_current_density", "max_via_current", "max_ground_rise_mv")
              if getattr(own.limits, k) is not None]
    if others:
        origin.notes.append(f"Rail {fr.net}: limits {', '.join(others)} from the project file.")
    rail.follow_net_ties = own.follow_net_ties
    for s in rail.sources:                    # remote sense stays project-only for now
        p = next((p for p in own.sources if p.ref == s.ref and (p.sense_net or p.ground_sense_net)),
                 None)
        if p is not None:
            for k in ("sense_ref", "sense_pins", "sense_net", "ground_sense_ref", "ground_sense_pins",
                      "ground_sense_net"):
                setattr(s, k, copy.deepcopy(getattr(p, k)))
            origin.notes.append(f"Rail {fr.net}: remote sense pins of {s.ref} from the project file "
                                "(sense is not a KiCad field yet).")
    return rail, origin


# ---------------------------------------------------------------------- comparing

_SOURCE_KEYS = ("voltage", "pins", "kind", "efficiency", "quiescent_a", "input_ref", "input_pins",
                "input_net", "ground_ref", "ground_pins", "ground_net", "current_limit_a", "fsw_hz",
                "extra_pins")
_LOAD_KEYS = ("current", "pins", "ground_ref", "ground_pins", "ground_net")


PadNets = Mapping[str, Mapping[str, str]]


def _key(t: Union[Source, Load], net: str = "", pads: Optional[PadNets] = None) -> Tuple:
    """What is compared of a terminal on ``net``: its values, and its pins as the set of pads
    they select when the part's pads are known (``pads``: reference → {pad: net}), so that
    "all" and a list of the same pads agree."""
    keys = _SOURCE_KEYS if isinstance(t, Source) else _LOAD_KEYS
    out = []
    for k in keys:
        v = getattr(t, k)
        if k == "extra_pins":                          # REF.PAD entries, in any order
            out.append(tuple(sorted(v or ())))
            continue
        if k.endswith("_ref") and v == t.ref:
            v = ""                                     # the part's own pins either way
        if k.endswith("pins"):
            where = {"pins": net, "input_pins": getattr(t, "input_net", ""),
                     "ground_pins": t.ground_net}[k]
            ref = t.ref if k == "pins" else (getattr(t, k.replace("pins", "ref")) or t.ref)
            v = _pad_set(ref, v, where, pads)
        if isinstance(v, float):
            v = round(v, 12)
        out.append(v)
    return tuple(out)


def _pad_set(ref: str, pins: PinSpec, net: str, pads: Optional[PadNets]):
    """The pads of ``ref`` on ``net`` that ``pins`` selects (None: all of them), when the
    part's pads are known; else the pins as written."""
    known = (pads or {}).get(ref)
    if known is None or not net:
        return tuple(pins) if pins is not None else None
    on_net = [n for n, pad_net in known.items() if pad_net == net]
    return frozenset(n for n in on_net if pins is None or any(fnmatch.fnmatchcase(n, p) for p in pins))


def _term_text(t: Union[Source, Load]) -> str:
    def pins(p):
        return f" pins {', '.join(p)}" if p else ""
    if isinstance(t, Source):
        s = f"{t.ref} {t.voltage:g} V{pins(t.pins)}"
        if t.kind != "source":
            s += f" {t.kind}"
        if t.efficiency is not None:
            s += f" η {t.efficiency:g}"
        if t.quiescent_a:
            s += f" I_q {t.quiescent_a:g} A"
        if t.input_net:
            s += f" input {t.input_net}{pins(t.input_pins)}"
        if t.current_limit_a is not None:
            s += f" I_max {t.current_limit_a:g} A"
        if t.fsw_hz is not None:
            s += f" f_sw {t.fsw_hz:g} Hz"
        if t.extra_pins:
            s += f" + {', '.join(t.extra_pins)}"
    else:
        s = f"{t.ref} {t.current:g} A{pins(t.pins)}"
    if t.ground_net:
        s += f" ground {t.ground_net}{pins(t.ground_pins)}"
    return s


def _differences(kicad: Rail, project: Rail, pads: Optional[PadNets] = None) -> List[str]:
    """What differs between the fields' rail and the project's, part by part (names, limits
    and remote sense are not compared; pins as the pads they select, when known)."""
    out = []

    def key(t):
        return _key(t, kicad.net, pads)
    for what, ks, ps in (("source", kicad.sources, project.sources), ("load", kicad.loads, project.loads)):
        for ref in sorted({t.ref for t in ks} | {t.ref for t in ps}, key=_ref_key):
            a = sorted((t for t in ks if t.ref == ref), key=lambda t: repr(key(t)))
            b = sorted((t for t in ps if t.ref == ref), key=lambda t: repr(key(t)))
            for x, y in zip(a, b):
                if key(x) != key(y):
                    out.append(f"KiCad {_term_text(x)}, project {_term_text(y)}")
            out += [f"KiCad has {what} {_term_text(x)}, project does not" for x in a[len(b):]]
            out += [f"project has {what} {_term_text(y)}, KiCad does not" for y in b[len(a):]]
    return out


def _same_link(a: Link, b: Link) -> bool:
    """The same link, whichever side is called a, and with "all" pins (None) equal to any
    list: a field link names all of its part's pads on each net."""
    def side(net, pins, other_net, other_pins):
        return net == other_net and (pins is None or other_pins is None
                                     or sorted(pins) == sorted(other_pins))
    ab = side(a.net_a, a.pins_a, b.net_a, b.pins_a) and side(a.net_b, a.pins_b, b.net_b, b.pins_b)
    ba = side(a.net_a, a.pins_a, b.net_b, b.pins_b) and side(a.net_b, a.pins_b, b.net_a, b.pins_a)
    return (ab or ba) and math.isclose(a.resistance_ohm, b.resistance_ohm, rel_tol=1e-9,
                                       abs_tol=1e-15)


def _link_text(k: Link) -> str:
    return f"{k.resistance_ohm:g} Ω {k.net_a} ↔ {k.net_b}"


# ---------------------------------------------------------------------- listing

def listing(fields: Mapping[str, Mapping[str, str]],
            pad_nets: Optional[Mapping[str, Mapping[str, str]]] = None) -> Dict[str, Any]:
    """The fields' rails and links as plain data, each value with the field it came from
    (for the MCP tool and the CLI). Raises FieldError like :func:`field_rails`."""
    rails = field_rails(fields, pad_nets)
    return {
        "rails": [{**fr.listing(), "missing": fr.missing(),
                   "note": fr.note() if fr.missing() is None else None}
                  for _, fr in sorted(rails.items())],
        "links": [{"ref": k.ref, "net_a": k.net_a, "pins_a": k.pins_a, "net_b": k.net_b,
                   "pins_b": k.pins_b, "resistance_ohm": k.resistance_ohm, "from": fv.origin()}
                  for k, fv in field_links(fields, pad_nets or {})],
    }


def _ref_key(ref: str):
    head = ref.rstrip("0123456789")
    tail = ref[len(head):]
    return (head, int(tail) if tail else -1, ref)


def _pin_key(number: str):
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


# ---------------------------------------------------------------------- proposing fields

# The unit a value is written in, by size: (below this, unit, scale), the first that fits.
_WRITE_UNITS = {"PI_SOURCE": [(math.inf, "V", 1)],
                "PI_LOAD": [(1.0, "mA", 1000), (math.inf, "A", 1)],
                "PI_IQ": [(1e-3, "uA", 10 ** 6), (1.0, "mA", 1000), (math.inf, "A", 1)],
                "PI_LINK": [(1.0, "mOhm", 1000), (math.inf, "Ohm", 1)],
                "PI_EFF": [(math.inf, "%", 100)], "PI_LIMIT_DROP": [(math.inf, "%", 1)],
                "PI_VOUT": [(math.inf, "V", 1)],
                "PI_IMAX": [(1.0, "mA", 1000), (math.inf, "A", 1)],
                "PI_FSW": [(1e3, "Hz", 1), (1e6, "kHz", Decimal("0.001")),
                           (math.inf, "MHz", Decimal("0.000001"))]}


def field_text(name: str, value: float) -> str:
    """``value`` (V, A, Ω, a fraction for PI_EFF, % for PI_LIMIT_DROP) as field text with its
    unit, in plain decimals (the grammar has no exponent), read back exactly by
    :func:`quantity`."""
    units = _WRITE_UNITS[name]
    preferred = next(i for i, (below, _, _) in enumerate(units) if abs(value) < below)
    texts = []
    for _, unit, scale in units[preferred:] + units[:preferred]:
        number = format((Decimal(repr(float(value))) * scale).normalize(), "f")
        texts.append(f"{number}{unit}")
        if quantity("", name, texts[-1]) == value:
            return texts[-1]
    # A percentage read back is divided by 100, which can be one unit in the last place off
    # (33.3 % gives 0.33299999999999996): the preferred text, which is that close.
    if math.isclose(quantity("", name, texts[0]), value, rel_tol=1e-12):
        return texts[0]
    raise ValueError(f"{name}: {value!r} cannot be written")


def _source_part(s: Source) -> str:
    """The part whose fields describe source ``s``: its own, or, when its input is on another
    part (an external-inductor buck: the input on the IC, the output at the inductor), that
    part, written with PI_SOURCE_REF; ground pins elsewhere are PI_GND_REF."""
    if s.input_net and s.input_ref and s.input_ref != s.ref:
        return s.input_ref
    return s.ref


def _source_fields(s: Source, net: str, blocked: List[str], notes: List[str]) -> Dict[str, str]:
    part = _source_part(s)
    out = {"PI_SOURCE": field_text("PI_SOURCE", s.voltage)}
    refs = list(dict.fromkeys([s.ref] + [e.split(".", 1)[0] for e in (s.extra_pins or ())]))
    if part != s.ref or len(refs) > 1:
        out["PI_SOURCE_REF"] = ",".join(refs)
    if len(refs) > 1:            # REF.PAD for each part, none when every pad of each is used
        own = [f"{s.ref}.{p}" for p in s.pins] if s.pins else [f"{s.ref}.*"]
        if s.pins or any(not e.endswith(".*") for e in s.extra_pins):
            out["PI_PINS"] = ",".join(own + list(s.extra_pins))
    elif s.pins:
        out["PI_PINS"] = ",".join(s.pins)
    regulator = s.kind in ("ldo", "smps")
    if s.kind != "source":
        out["PI_KIND"] = s.kind
    if s.kind == "smps":
        if s.efficiency is None:
            blocked.append("a switcher without its efficiency")
        else:
            out["PI_EFF"] = field_text("PI_EFF", s.efficiency)
    elif s.efficiency is not None:
        notes.append(f"efficiency on a {s.kind} (not used by the solve)")
    if s.quiescent_a:
        if regulator:
            out["PI_IQ"] = field_text("PI_IQ", s.quiescent_a)
        else:
            notes.append("quiescent current on a plain source (not used by the solve)")
    if s.current_limit_a is not None:
        out["PI_IMAX"] = field_text("PI_IMAX", s.current_limit_a)
    if s.fsw_hz is not None:
        if s.kind == "smps":
            out["PI_FSW"] = field_text("PI_FSW", s.fsw_hz)
        else:
            notes.append(f"switching frequency on a {s.kind} (PI_FSW is for a switcher)")
    if s.input_net:
        if s.input_ref and s.input_ref not in (s.ref, part):
            blocked.append(f"input on another part ({s.input_ref})")
        out["PI_INPUT"] = s.input_net
        if s.input_pins:
            out["PI_INPUT_PINS"] = ",".join(s.input_pins)
    elif regulator:
        blocked.append(f"a {s.kind} without its input")
    if s.sense_net or s.ground_sense_net:
        notes.append("remote sense (sense, ground_sense): keep it in the project file")
    _ground_fields(s, out, blocked, part)
    return out


def _load_fields(ld: Load, blocked: List[str]) -> Dict[str, str]:
    out = {"PI_LOAD": field_text("PI_LOAD", ld.current)}
    if ld.pins:
        out["PI_PINS"] = ",".join(ld.pins)
    _ground_fields(ld, out, blocked)
    return out


def _ground_fields(t, out: Dict[str, str], blocked: List[str], part: str = "") -> None:
    if not t.ground_net:
        return
    on = t.ground_ref or t.ref                   # the part with the ground pins
    out["PI_GND"] = t.ground_net
    if on != (part or t.ref):
        out["PI_GND_REF"] = on
    if t.ground_pins:
        out["PI_GND_PINS"] = ",".join(t.ground_pins)


def propose(project: Project, pad_nets: Optional[Mapping[str, Mapping[str, str]]] = None
            ) -> Dict[str, Any]:
    """The PI_* field text that describes ``project``'s rails and links, part by part, split
    between the library symbol (``PI_KIND``, ``PI_EFF``, ``PI_IQ``: what the part is) and the
    schematic instance (the rest: where it sits). A part on several rails gets one value per
    rail, by position. What fields cannot carry is listed in ``not_expressed``; a rail that
    needs it is not proposed (a wrong field would fail every run). The proposal is checked by
    parsing it back with :func:`merge`. ``pad_nets``: reference → {pad: net}, to check that a
    link's pins are all its pads on each net."""
    not_expressed: List[Dict[str, Any]] = []
    staged: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}       # rail net -> [(ref, fields)]
    for rail in project.rails:
        positions, blocking = [], []
        for t in [*rail.sources, *rail.loads]:
            blocked: List[str] = []
            notes: List[str] = []
            fields = (_source_fields(t, rail.net, blocked, notes) if isinstance(t, Source)
                      else _load_fields(t, blocked))
            part = _source_part(t) if isinstance(t, Source) else t.ref
            if t.name and t.name not in (t.ref, part):
                notes.append(f"its name ({t.name}): fields name a terminal by its part")
            not_expressed += [{"rail": rail.net, "part": part, "what": n,
                               "reason": "fields cannot carry it; it is not proposed"} for n in notes]
            blocking += [(part, b) for b in blocked]
            positions.append((part, fields))
        if blocking:
            not_expressed += [{"rail": rail.net, "part": ref, "what": what,
                               "reason": "fields cannot carry it, so this rail is not proposed"}
                              for ref, what in blocking]
            continue
        if rail.limits.max_drop_pct is not None and positions:
            positions[0][1]["PI_LIMIT_DROP"] = field_text("PI_LIMIT_DROP", rail.limits.max_drop_pct)
        others = [k for k in ("max_current_density", "max_via_current", "max_ground_rise_mv")
                  if getattr(rail.limits, k) is not None]
        if others:
            not_expressed.append({"rail": rail.net, "part": None, "what": ", ".join(others),
                                  "reason": "limits other than the drop stay in the project file"})
        if not rail.follow_net_ties:
            not_expressed.append({"rail": rail.net, "part": None, "what": "follow_net_ties: false",
                                  "reason": "it stays in the project file"})
        staged[rail.net] = positions
    links: Dict[str, Dict[str, str]] = {}
    for k in project.links:
        pads = (pad_nets or {}).get(k.ref)
        sides = None                         # PI_LINK_PINS, when the link needs its sides named
        if pads is not None:
            on = {net: sorted((p for p, n in pads.items() if n == net), key=_pin_key)
                  for net in {n for n in pads.values() if n}}
            side_a = sorted(k.pins_a or on.get(k.net_a, []), key=_pin_key)
            side_b = sorted(k.pins_b or on.get(k.net_b, []), key=_pin_key)
            wrong = (k.net_a not in on or k.net_b not in on
                     or any(p not in on[net] for net, side in ((k.net_a, side_a), (k.net_b, side_b))
                            for p in side))
            if wrong:
                not_expressed.append({"rail": None, "part": k.ref, "what": f"link {k.name}",
                                      "reason": "its pins are not the part's pads on its two nets, "
                                                "so it is not proposed"})
                continue
            if set(on) != {k.net_a, k.net_b} or side_a != on[k.net_a] or side_b != on[k.net_b]:
                sides = f"{','.join(side_a)};{','.join(side_b)}"
        else:
            not_expressed.append({"rail": None, "part": k.ref, "what": f"link {k.name} pins",
                                  "reason": "the board's pads were not available to check them: a "
                                            "PI_LINK uses all of the part's pads on each of its two "
                                            "nets; check that this part has no others"})
        if k.name != k.ref:
            not_expressed.append({"rail": None, "part": k.ref, "what": f"link name {k.name}",
                                  "reason": "a PI_LINK is named by its part"})
        links[k.ref] = {"PI_LINK": field_text("PI_LINK", k.resistance_ohm),
                        **({"PI_LINK_PINS": sides} if sides else {})}
    check_pads = dict(pad_nets or {})        # a link without known pads: checked with its own
    for k in project.links:
        if k.ref in links and k.ref not in check_pads:
            check_pads[k.ref] = {**{p: k.net_a for p in (k.pins_a or ["a"])},
                                 **{p: k.net_b for p in (k.pins_b or ["b"])}}
    for _ in range(len(staged) + 1):          # drop the rails that are incomplete as fields
        parts = _assemble(staged, links)
        try:
            merge(Project(), {ref: {**p["library_symbol"], **p["schematic_instance"]} for ref, p in
                              ((p["ref"], p) for p in parts)}, check_pads)
            break
        except FieldError as e:
            bad = [net for net in staged if f"{net}: " in str(e) or
                   any(str(e).startswith(f"{ref}: ") for ref, _ in staged[net])]
            if not bad:
                raise
            for net in bad:
                not_expressed.append({"rail": net, "part": None, "what": f"rail {net}",
                                      "reason": f"incomplete as fields without the rest: {e}"})
                del staged[net]
    return {"parts": _assemble(staged, links), "rails": list(staged), "not_expressed": not_expressed}


def _assemble(staged: Mapping[str, List[Tuple[str, Dict[str, str]]]],
              links: Mapping[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Per part: its positions joined by ';' (PI_RAIL in rail order), split into library
    and instance fields."""
    by_ref: Dict[str, List[Tuple[str, Dict[str, str]]]] = {}
    for net, positions in staged.items():
        for ref, fields in positions:
            by_ref.setdefault(ref, []).append((net, fields))
    out = []
    for ref in sorted(set(by_ref) | set(links), key=_ref_key):
        joined: Dict[str, str] = dict(links.get(ref, {}))
        positions = by_ref.get(ref, [])
        if positions:
            joined["PI_RAIL"] = ";".join(net for net, _ in positions)
            for name in [*INSTANCE_FIELDS, *LIBRARY_FIELDS]:
                if name != "PI_RAIL" and any(name in f for _, f in positions):
                    joined[name] = ";".join(f.get(name, "") for _, f in positions)
        out.append({"ref": ref,
                    "library_symbol": {k: joined[k] for k in LIBRARY_FIELDS if k in joined},
                    "schematic_instance": {k: joined[k] for k in INSTANCE_FIELDS if k in joined}})
    return out


__all__: Sequence[str] = ("FIELDS", "BoardFields", "FieldError", "FieldRail", "Merged", "Origin",
                          "field_text", "propose",
                          "apply", "field_links", "field_rails", "listing", "merge", "quantity")
