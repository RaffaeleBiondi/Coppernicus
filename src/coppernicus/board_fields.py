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
==================  ===========  ===========================================================

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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from coppernicus.project import SOURCE_KINDS, Limits, Link, Load, PinSpec, Project, Rail, Source, parse_pins

PREFIX = "PI_"
PER_RAIL = ("PI_SOURCE", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP", "PI_KIND", "PI_EFF", "PI_IQ",
            "PI_INPUT", "PI_INPUT_PINS", "PI_GND", "PI_GND_PINS")
FIELDS = ("PI_RAIL", *PER_RAIL, "PI_LINK")
SOURCE_ONLY = ("PI_KIND", "PI_EFF", "PI_IQ", "PI_INPUT", "PI_INPUT_PINS")
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
}
SHOWN = {"PI_SOURCE": "V", "PI_LOAD": "A or mA", "PI_LIMIT_DROP": "%", "PI_EFF": "%",
         "PI_IQ": "A, mA or uA", "PI_LINK": "Ohm or mOhm"}
EXAMPLES = {"PI_SOURCE": "3.3V or -5V", "PI_LOAD": "2A or 250mA", "PI_LIMIT_DROP": "2%",
            "PI_EFF": "90%", "PI_IQ": "2mA or 50uA", "PI_LINK": "50mOhm"}
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

    def text(self) -> str:
        kind = "source" if isinstance(self.term, Source) else "load"
        items = [f"{what} ({fv.origin()})" for what, fv in self.values]
        if not any(fv.field == "PI_PINS" for _, fv in self.values):
            items.insert(1, "every pin on the net")
        return f"{kind} {self.term.ref} " + ", ".join(items)


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
            return (f"{self.net}: loads {', '.join(t.term.ref for t in self.loads)} have PI_LOAD "
                    "but no part on this net has PI_SOURCE (the voltage that holds the rail)")
        if not self.loads:
            return (f"{self.net}: {', '.join(t.term.ref for t in self.sources)} has PI_SOURCE but "
                    "no part on this net has PI_LOAD (a current drawn from the rail), and it feeds "
                    "no regulator input or link")
        return None

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
            d["fields"] = {fv.field: fv.text for _, fv in t.values}
            return d
        return {"net": self.net, "parts": list(self.parts), "complete": self.missing() is None,
                "sources": [{**term(t), "voltage_v": t.term.voltage, "from": t.values[0][1].origin()}
                            for t in self.sources],
                "loads": [{**term(t), "current_a": t.term.current, "from": t.values[0][1].origin()}
                          for t in self.loads],
                "limits": ({"max_drop_pct": self.limit[0], "from": self.limit[1].origin()}
                           if self.limit else {})}


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
    x = float(number) / units[unit]
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


def field_rails(fields: Mapping[str, Mapping[str, str]]) -> Dict[str, FieldRail]:
    """The rails the parts' fields describe (``fields``: reference → {field name: text}), by
    net, complete or not. Raises FieldError for a field that cannot be used."""
    rails: Dict[str, FieldRail] = {}
    inputs: List[Tuple[str, str]] = []               # (ref, input net) of field regulators
    for ref in sorted(fields, key=_ref_key):
        f = fields[ref]
        _check_names(ref, f)
        nets = _split(f.get("PI_RAIL"))
        values = {n: _split(f.get(n)) for n in PER_RAIL}
        if _split(f.get("PI_LINK")) is not None:
            if nets is not None or any(v is not None for v in values.values()):
                raise FieldError(f"{ref}: PI_LINK with other fields: a link part has no PI_RAIL; "
                                 "its two nets are those of its pads")
            continue
        if nets is None:
            given = [n for n, v in values.items() if v is not None]
            if given:
                raise FieldError(f"{ref}: {', '.join(given)} without PI_RAIL: set PI_RAIL to the "
                                 "net it is on (e.g. +3V3)")
            continue
        if any(not n for n in nets):
            raise FieldError(f'{ref}: PI_RAIL = "{f["PI_RAIL"]}" has an empty net name')
        if values["PI_SOURCE"] is None and values["PI_LOAD"] is None:
            raise FieldError(f"{ref}: PI_RAIL {f['PI_RAIL']} but no PI_SOURCE or PI_LOAD: give the "
                             "voltage it holds (PI_SOURCE, e.g. 3.3V) or the current it draws "
                             "(PI_LOAD, e.g. 2A)")
        for name, vals in values.items():
            if vals is not None and len(vals) != len(nets):
                raise FieldError(f'{ref}: {name} = "{f[name]}" has {len(vals)} value(s) for '
                                 f'{len(nets)} rail(s) (PI_RAIL = "{f["PI_RAIL"]}"): give one per '
                                 "rail, separated by ';' (leave one empty to skip that rail)")
        for i, net in enumerate(nets):
            v = {n: _at(values[n], i) for n in PER_RAIL}
            term, limit = _term(ref, net, i, v)
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
    return rails


def _term(ref: str, net: str, i: int, v: Mapping[str, Optional[str]]
          ) -> Tuple[FieldTerm, Optional[Tuple[float, FieldValue]]]:
    """The source or load at rail position ``i`` of a part, and its drop limit."""
    src, load = v["PI_SOURCE"], v["PI_LOAD"]
    if src and load:
        raise FieldError(f"{ref}: both PI_SOURCE and PI_LOAD for rail {net}: a part is either the "
                         "source of a rail or a load on it")
    if not src and not load:
        raise FieldError(f"{ref}: rail {net} (PI_RAIL position {i + 1}) has neither PI_SOURCE nor "
                         "PI_LOAD")
    for name, of in (("PI_INPUT_PINS", "PI_INPUT"), ("PI_GND_PINS", "PI_GND")):
        if v[name] and not v[of]:
            raise FieldError(f"{ref}: {name} without {of} for rail {net}: set {of} to the net")
    values: List[Tuple[str, FieldValue]] = []
    if load:
        extra = [n for n in SOURCE_ONLY if v[n]]
        if extra:
            raise FieldError(f"{ref}: {extra[0]} is for a source (PI_SOURCE), not a load on {net}")
        current = quantity(ref, "PI_LOAD", load)
        values.append((f"{current:g} A", FieldValue(ref, "PI_LOAD", load)))
        pins = _pins(ref, v["PI_PINS"], "PI_PINS", values)
        ground, gpins = _ground(ref, v, values)
        term: Union[Source, Load] = Load(ref, current, pins, ground_pins=gpins, ground_net=ground)
    else:
        voltage = quantity(ref, "PI_SOURCE", src)
        values.append((f"{voltage:g} V", FieldValue(ref, "PI_SOURCE", src)))
        pins = _pins(ref, v["PI_PINS"], "PI_PINS", values)
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
        ground, gpins = _ground(ref, v, values)
        term = Source(ref, voltage, pins, kind=kind, efficiency=efficiency, quiescent_a=iq,
                      input_pins=ipins, input_net=v["PI_INPUT"] or "", ground_pins=gpins,
                      ground_net=ground)
    limit = None
    if v["PI_LIMIT_DROP"]:
        limit = (quantity(ref, "PI_LIMIT_DROP", v["PI_LIMIT_DROP"]),
                 FieldValue(ref, "PI_LIMIT_DROP", v["PI_LIMIT_DROP"]))
    return FieldTerm(term, values), limit


def _ground(ref: str, v: Mapping[str, Optional[str]], values: List) -> Tuple[str, PinSpec]:
    if not v["PI_GND"]:
        return "", None
    values.append((f"ground {v['PI_GND']}", FieldValue(ref, "PI_GND", v["PI_GND"])))
    return v["PI_GND"], _pins(ref, v["PI_GND_PINS"], "PI_GND_PINS", values)


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
        if len(nets) != 2:
            raise FieldError(f"{ref}: PI_LINK on a part whose pads are on {len(nets)} net(s) "
                             f"({', '.join(nets) or 'none'}); a link joins exactly two nets")
        (net_a, pins_a), (net_b, pins_b) = nets.items()
        out.append((Link(ref, ref, pins_a, net_a, pins_b, net_b, ohm), FieldValue(ref, "PI_LINK", text)))
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
    * ``PI_LINK`` parts replace project links on the same part (a warning when they differ).
    """
    rails = field_rails(fields)
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
                merged.warnings.append(f"project file link {link.ref} ignored: KiCad field PI_LINK "
                                       f"defines it (KiCad {_link_text(link)}, project "
                                       f"{_link_text(old)})")
            out.links[out.links.index(old)] = link
        merged.link_notes.append(note)
        for net in (link.net_a, link.net_b):
            link_notes.setdefault(net, []).append(note)

    own = {r.net: r for r in out.rails}
    trial = copy.deepcopy(out)                # every field rail with a source in place, to
    for net, fr in rails.items():             # see what a source-only rail feeds
        if fr.sources and not (net in own and own[net].override_board_fields):
            trial.rails = [r for r in trial.rails if r.net != net] + [fr.rail()]
    missing = {net: (None if fr.sources and (fr.loads or _feeds(trial, net, ties)) else fr.missing())
               for net, fr in rails.items()}

    new_rails: List[Rail] = []
    for r in out.rails:
        fr = rails.get(r.net)
        if fr is None:
            new_rails.append(r)
            merged.origins[r.net] = Origin(r.net, "project", list(link_notes.get(r.net, [])))
        elif r.override_board_fields:
            new_rails.append(r)
            merged.origins[r.net] = Origin(
                r.net, "overridden",
                [f"Rail {r.net} from the project file: override_board_fields is set, so it replaces "
                 f"the rail of the KiCad footprint fields ({fr.values_text()})."]
                + link_notes.get(r.net, []), board_fields=fr.listing())
        elif missing[r.net] is not None:
            new_rails.append(r)
            merged.origins[r.net] = Origin(
                r.net, "project", list(link_notes.get(r.net, [])),
                [f"KiCad fields on {r.net} are incomplete ({missing[r.net]}): the project file's "
                 "rail is used"], board_fields=fr.listing())
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
    out.rails = new_rails
    merged.warnings = [w for o in merged.origins.values() for w in o.warnings] + merged.warnings
    return merged


def _feeds(project: Project, net: str, ties: Sequence[Tuple[str, str]] = ()) -> bool:
    """Whether a rail on ``net`` (and the nets tied to it) delivers current to a regulator
    input or a link, at nominal voltages, as the per-rail solve estimates it. An estimate
    that cannot be made counts as yes: the solve reports it."""
    from coppernicus.power_tree import EstimateError, estimated_draws
    nets, todo = {net}, [net]
    while todo:
        n = todo.pop()
        for a, b in ties:
            for x, y in ((a, b), (b, a)):
                if x == n and y not in nets:
                    nets.add(y)
                    todo.append(y)
    try:
        return bool(estimated_draws(project, sorted(nets), list(ties)))
    except EstimateError:
        return True


def _from_fields(fr: FieldRail, own: Optional[Rail], pads: Optional[PadNets] = None
                 ) -> Tuple[Rail, Origin]:
    """The fields' rail, with what it keeps from the project rail ``own`` of its net, and its
    origin (notes and warnings)."""
    rail = fr.rail()
    origin = Origin(fr.net, "fields", [fr.note()], board_fields=fr.listing())
    if own is None:
        return rail, origin
    # A project rail with no sources and no loads only adds limits: nothing to compare.
    diffs = _differences(rail, own, pads) if own.sources or own.loads else []
    if diffs:
        origin.warnings.append(f"project file rail {fr.net} ignored: KiCad fields define it "
                               f"({'; '.join(diffs)}); set override_board_fields: true on the rail "
                               "to use the project's values")
    pct = own.limits.max_drop_pct
    if fr.limit is not None and pct is not None and not math.isclose(pct, fr.limit[0], rel_tol=1e-9):
        origin.warnings.append(f"KiCad field PI_LIMIT_DROP gives max_drop_pct {fr.limit[0]:g} on "
                               f"{fr.net} ({fr.limit[1].origin()}); the project file's {pct:g} is "
                               "not used")
    rail.limits = dataclasses.replace(own.limits, max_drop_pct=fr.limit[0] if fr.limit else pct)
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
                "input_net", "ground_ref", "ground_pins", "ground_net")
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
    rails = field_rails(fields)
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


__all__: Sequence[str] = ("FIELDS", "BoardFields", "FieldError", "FieldRail", "Merged", "Origin",
                          "apply", "field_links", "field_rails", "listing", "merge", "quantity")
