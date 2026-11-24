"""Rails from KiCad footprint fields: an optional input next to the project file.

A part's power data can live on its symbol in the schematic, as fields that KiCad 8 and
later copy onto the footprint, where the board file keeps them as ``(property "NAME"
"TEXT")``. ``coppernicus.engine.kicad_fields`` reads them; this module (no engine, plain
data) turns them into rails and merges those with a project. See
``docs/kicad-fields-proposal.md``.

========================  ==========  ====================================================
Field                     Example     Meaning
========================  ==========  ====================================================
``PI_RAIL``               ``+3V3``    The net the part is a source or load on. Several:
                                      ``VIN;+3V3``; every other field then gives one value
                                      per rail, by position (an empty one skips that rail).
``PI_SOURCE``             ``3.3V``    The part holds the rail at this voltage.
``PI_LOAD``               ``2A``      The part draws this current, split across its pins.
``PI_PINS``               ``1,2``     Which pads take part (numbers or globs); default all.
``PI_LIMIT_DROP``         ``2%``      A drop limit for the rail (the strictest one wins).
========================  ==========  ====================================================

Every value carries its unit (V; A or mA; %): a bare number is refused, so "2" can never be
read as 2 A when 2 mA was meant. Nothing is assumed: a part with a rail but no voltage or
current, a rail without a source or without a load, a value of the wrong unit, all are
errors that name the part, the field and its text.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from coppernicus.project import Limits, Load, PinSpec, Project, Rail, Source, parse_pins

PREFIX = "PI_"
FIELDS = ("PI_RAIL", "PI_SOURCE", "PI_LOAD", "PI_PINS", "PI_LIMIT_DROP")
UNITS = {"PI_SOURCE": {"V": 1.0}, "PI_LOAD": {"A": 1.0, "mA": 1e-3}, "PI_LIMIT_DROP": {"%": 1.0}}
EXAMPLES = {"PI_SOURCE": "3.3V or -5V", "PI_LOAD": "2A or 250mA", "PI_LIMIT_DROP": "2%"}
_QUANTITY = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s?([A-Za-z%]*)$")


class FieldError(ValueError):
    """A footprint field Coppernicus cannot use: the message names the part, the field and
    its text, and says what to write instead."""


@dataclass
class FieldValue:
    """One value read from a field, kept with its text for the assumptions."""
    ref: str
    field: str
    text: str

    def origin(self) -> str:
        return f'from KiCad field {self.field} "{self.text}" on {self.ref}'


@dataclass
class FieldRail:
    """What the fields say about one net."""
    net: str
    sources: List[Tuple[Source, FieldValue, Optional[FieldValue]]] = field(default_factory=list)
    loads: List[Tuple[Load, FieldValue, Optional[FieldValue]]] = field(default_factory=list)
    limit: Optional[Tuple[float, FieldValue]] = None           # the strictest PI_LIMIT_DROP
    parts: List[str] = field(default_factory=list)              # refs with PI_RAIL on it

    def missing(self) -> Optional[str]:
        """Why it is not a rail yet (no source, no load), or None."""
        if not self.sources:
            return (f"{self.net}: loads {', '.join(ld.ref for ld, _, _ in self.loads)} have PI_LOAD "
                    "but no part on this net has PI_SOURCE (the voltage that holds the rail)")
        if not self.loads:
            return (f"{self.net}: {', '.join(s.ref for s, _, _ in self.sources)} has PI_SOURCE but "
                    "no part on this net has PI_LOAD (a current drawn from the rail)")
        return None

    def rail(self) -> Rail:
        return Rail(self.net, sources=[s for s, _, _ in self.sources],
                    loads=[ld for ld, _, _ in self.loads],
                    limits=Limits(max_drop_pct=self.limit[0] if self.limit else None))

    def note(self) -> str:
        """The assumption line: every value of this rail, each with the field it came from."""
        def pins(fv: Optional[FieldValue]) -> str:
            return f", pins {fv.text} ({fv.origin()})" if fv is not None else ", every pin on the net"
        items = [f"source {s.ref} {s.voltage:g} V ({fv.origin()}){pins(p)}" for s, fv, p in self.sources]
        items += [f"load {ld.ref} {ld.current:g} A ({fv.origin()}){pins(p)}" for ld, fv, p in self.loads]
        if self.limit is not None:
            items.append(f"limit max_drop_pct {self.limit[0]:g} ({self.limit[1].origin()})")
        return (f"Rail {self.net} from KiCad footprint fields (from KiCad field PI_RAIL on "
                f"{', '.join(self.parts)}): " + "; ".join(items) + ".")


def quantity(ref: str, name: str, text: str) -> float:
    """A field value with its unit, as a number in V, A or %. Raises FieldError."""
    units = UNITS[name]
    shown = f'{ref}: {name} = "{text}"'
    if "," in text:
        raise FieldError(f"{shown}: use a dot for decimals, e.g. {EXAMPLES[name]}")
    m = _QUANTITY.match(text.strip())
    if m is None:
        raise FieldError(f"{shown} is not a value with its unit; write it as {EXAMPLES[name]}")
    number, unit = m.groups()
    if not unit:
        raise FieldError(f"{shown}: give the unit ({' or '.join(units)}), e.g. {EXAMPLES[name]}; "
                         "a bare number is refused so that no unit is guessed")
    if unit not in units:
        raise FieldError(f"{shown}: {name} takes {' or '.join(units)}, not {unit}; "
                         f"e.g. {EXAMPLES[name]}")
    x = float(number) * units[unit]
    if name == "PI_LOAD" and x < 0:
        raise FieldError(f"{shown}: a load current is ≥ 0 (a part that feeds the rail is a "
                         "PI_SOURCE)")
    if name == "PI_LIMIT_DROP" and x <= 0:
        raise FieldError(f"{shown}: a drop limit is > 0")
    return x


def _split(text: Optional[str]) -> Optional[List[str]]:
    """The values of a field, one per rail; None when the field is absent or blank."""
    if text is None or not text.strip():
        return None
    return [v.strip() for v in text.split(";")]


def field_rails(fields: Mapping[str, Mapping[str, str]]) -> Dict[str, FieldRail]:
    """The rails the parts' fields describe (``fields``: reference → {field name: text},
    only ``PI_*`` names), by net, complete or not (:meth:`FieldRail.missing`). Raises
    FieldError for a field that cannot be used."""
    rails: Dict[str, FieldRail] = {}
    for ref in sorted(fields, key=_ref_key):
        f = fields[ref]
        unknown = sorted(n for n in f if n.upper().startswith(PREFIX) and n not in FIELDS)
        if unknown:
            raise FieldError(f"{ref}: unknown field {', '.join(unknown)} (the fields are "
                             f"{', '.join(FIELDS)}, in upper case)")
        nets = _split(f.get("PI_RAIL"))
        values = {n: _split(f.get(n)) for n in FIELDS[1:]}
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
            src, load, pins, limit = (_at(values[n], i) for n in FIELDS[1:])
            if src and load:
                raise FieldError(f"{ref}: both PI_SOURCE and PI_LOAD for rail {net}: a part is either "
                                 "the source of a rail or a load on it")
            if not src and not load:
                raise FieldError(f"{ref}: rail {net} (PI_RAIL position {i + 1}) has neither "
                                 "PI_SOURCE nor PI_LOAD")
            rail = rails.setdefault(net, FieldRail(net))
            rail.parts.append(ref)
            spec: PinSpec = parse_pins(pins) if pins else None
            pins_fv = FieldValue(ref, "PI_PINS", pins) if pins else None
            if src:
                rail.sources.append((Source(ref, quantity(ref, "PI_SOURCE", src), spec),
                                     FieldValue(ref, "PI_SOURCE", src), pins_fv))
            else:
                rail.loads.append((Load(ref, quantity(ref, "PI_LOAD", load), spec),
                                   FieldValue(ref, "PI_LOAD", load), pins_fv))
            if limit:
                pct = quantity(ref, "PI_LIMIT_DROP", limit)
                if rail.limit is None or pct < rail.limit[0]:
                    rail.limit = (pct, FieldValue(ref, "PI_LIMIT_DROP", limit))
    return rails


@dataclass
class Merged:
    """A project with the rails from the fields added: ``project`` (a copy; the loaded one
    is never changed), the nets taken from the fields, and the assumption lines per net."""
    project: Project
    from_fields: List[str]
    notes: Dict[str, List[str]]
    parts: Dict[str, List[str]] = field(default_factory=dict)     # net → refs with PI_RAIL on it


def merge(project: Project, fields: Mapping[str, Mapping[str, str]]) -> Merged:
    """The project's rails, then a rail for every other net the fields describe completely.
    A rail in the project file replaces the fields' rail of its net entirely (sources, loads,
    pins and limit), and its note says so. A net the fields leave incomplete, and the project
    does not define, is an error that names the missing field."""
    rails = field_rails(fields)
    out = copy.deepcopy(project)
    own = {r.net for r in out.rails}
    notes: Dict[str, List[str]] = {}
    for net in own & set(rails):
        notes[net] = [f"Rail {net} from the project file; the KiCad footprint fields on this net "
                      f"({', '.join(rails[net].parts)}) are not used: the project file's rail "
                      "replaces them entirely."]
    missing = [rails[n].missing() for n in sorted(rails) if n not in own and rails[n].missing()]
    if missing:
        raise FieldError("Incomplete rails in the KiCad footprint fields: " + "; ".join(missing)
                         + ". Add the missing field, or define the rail in the project file")
    added = sorted(n for n in rails if n not in own)
    for net in added:
        out.rails.append(rails[net].rail())
        notes[net] = [rails[net].note()]
    return Merged(out, added, notes, {n: list(rails[n].parts) for n in added})


def listing(fields: Mapping[str, Mapping[str, str]]) -> List[Dict[str, object]]:
    """The fields' rails as plain data, each value with the field it came from (for the
    MCP tool and the CLI). Raises FieldError like :func:`field_rails`."""
    out = []
    for net, fr in sorted(field_rails(fields).items()):
        out.append({
            "net": net, "parts": list(fr.parts), "complete": fr.missing() is None,
            "missing": fr.missing(),
            "sources": [{"ref": s.ref, "voltage_v": s.voltage, "pins": _pins(s.pins),
                         "from": fv.origin()} for s, fv, _ in fr.sources],
            "loads": [{"ref": ld.ref, "current_a": ld.current, "pins": _pins(ld.pins),
                       "from": fv.origin()} for ld, fv, _ in fr.loads],
            "limits": ({"max_drop_pct": fr.limit[0], "from": fr.limit[1].origin()}
                       if fr.limit else {}),
            "note": fr.note() if fr.missing() is None else None,
        })
    return out


def _at(values: Optional[List[str]], i: int) -> Optional[str]:
    """The value for rail ``i``, or None when the field or that position is empty."""
    return values[i] if values is not None and values[i] else None


def _pins(pins: PinSpec) -> str:
    return "all" if pins is None else ", ".join(pins)


def _ref_key(ref: str):
    head = ref.rstrip("0123456789")
    tail = ref[len(head):]
    return (head, int(tail) if tail else -1, ref)


__all__: Sequence[str] = ("FIELDS", "FieldError", "FieldRail", "Merged", "field_rails", "listing",
                          "merge", "quantity")
