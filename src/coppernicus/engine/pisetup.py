"""Map a Coppernicus project onto KiPI's power-integrity setup (``PISetup``).

Field by field: ``docs/engine-mapping.md`` (Task 6). :func:`to_pisetup` only maps the
project; KiPI's ``validate`` checks the result (:func:`validate_setup`). Problems that can
only be seen while mapping (pins not on the board, two sources on a rail, a switcher
without its efficiency, a load counted twice, an unknown engine option) raise
:class:`EngineError` naming the part.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from kipi.dc.pi.model import DcLink, PinRef, PISetup, Vrm, validate
from kipi.dc.pi.model import Load as KipiLoad

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.solve import _unique, engine_settings, tied_nets
from coppernicus.project import PinSpec, Project, Rail, pin_ref

SWITCHING_KINDS = ("smps",)          # input power = output power / efficiency
# KiPI's DC solve reads a VRM's efficiency only for switchers; other kinds get this.
UNUSED_EFFICIENCY = 1.0
ENGINE_OPTIONS = ("excluded_nets",)

# ("source" | "load" | "link", the rail's net ("" for a link), index in its list)
Key = Tuple[str, str, int]


def _mapped(project: Project) -> List[Rail]:
    """The rails that go into the setup: every one with a source or a load."""
    return [r for r in project.rails if r.sources or r.loads]


def terminal_names(project: Project) -> Dict[Key, str]:
    """The unique name each source, load and link gets in the engine's setup (KiPI wants
    every VRM, load and link name unique). A label used once is kept; a source or load label
    used more than once becomes ``"<label> (<net>)"``; any duplicate left is numbered."""
    rails = _mapped(project)
    entries = [(("source", r.net, i), s.label, r.net) for r in rails for i, s in enumerate(r.sources)]
    entries += [(("load", r.net, i), ld.label, r.net) for r in rails for i, ld in enumerate(r.loads)]
    entries += [(("link", "", i), k.name, "") for i, k in enumerate(project.links)]
    count = Counter(label for _, label, _ in entries)
    names = _unique([f"{label} ({net})" if net and count[label] > 1 else label
                     for _, label, net in entries])
    return {key: name for (key, _, _), name in zip(entries, names)}


def _numbers(pads, pins: PinSpec) -> List[str]:
    """KiPI's pin list: empty for every pad of the part on the net, else pad numbers."""
    return [] if pins is None else list(dict.fromkeys(p.number for p in pads))


def _pads(board: Board, nets: Sequence[str], ref: str, pins: PinSpec, owner: str):
    try:
        return board.pads(list(nets), ref, pins)
    except EngineError as e:
        raise EngineError(f"{owner}: {e}") from None


def _on_rail(board: Board, nets: Sequence[str], ref: str, pins: PinSpec, owner: str) -> PinRef:
    """A source's output or a load's supply pins: on the rail's net or a net tied to it."""
    pads = _pads(board, nets, ref, pins, owner)
    on = sorted({p.net for p in pads}, key=list(nets).index)
    if len(on) > 1:
        raise EngineError(f"{owner}: its pins are on {', '.join(on)}, nets joined by net ties; "
                          "a power-tree solve needs the pins of one net: set its pins")
    return PinRef(ref, on[0], _numbers(pads, pins))


def _pin_ref(board: Board, terminal, what: str, owner: str) -> Optional[PinRef]:
    found = pin_ref(terminal, what)
    if found is None:
        return None
    ref, pins, net = found
    pads = _pads(board, [net], ref, pins, f"{owner} ({what.replace('_', ' ')} pins)")
    return PinRef(ref, net, _numbers(pads, pins))


def _overlap(a: PinRef, b: PinRef) -> bool:
    """Whether two pin references share a pad (an empty list is every pad)."""
    if (a.ref, a.net) != (b.ref, b.net):
        return False
    return not a.pins or not b.pins or bool(set(a.pins) & set(b.pins))


def _excluded_nets(options: Dict) -> List[str]:
    unknown = sorted(set(options) - set(ENGINE_OPTIONS))
    if unknown:
        raise EngineError(f"Unknown engine option {', '.join(unknown)}; the engine accepts "
                          f"{', '.join(ENGINE_OPTIONS)}")
    nets = options.get("excluded_nets", [])
    if not isinstance(nets, list) or not all(isinstance(n, str) for n in nets):
        raise EngineError("Engine option excluded_nets must be a list of net names")
    return list(nets)


def _tie_links(board: Board, nets: Sequence[str], skip: set, taken: set) -> List[DcLink]:
    """Net-tie footprints as 0 Ω links, as the per-rail solve has them, for the ties reached
    from ``nets`` (the nets of rails that do not follow ties, ``skip``, start no search)."""
    out, seen = [], set()
    for net in nets:
        if net in skip:
            continue
        for t in tied_nets(board, net)[1]:
            key = (t.ref, t.pad_a, t.pad_b)
            if key in seen:
                continue
            seen.add(key)
            name = _unique([*taken, f"net tie {t.ref}"])[-1]
            taken.add(name)
            out.append(DcLink(name, PinRef(t.ref, t.net_a, [board.pad(t.pad_a).number]),
                              PinRef(t.ref, t.net_b, [board.pad(t.pad_b).number]), 0.0))
    return out


def to_pisetup(board: Board, project: Project) -> PISetup:
    """KiPI's setup for the whole project: one VRM per source, its loads and links, the
    project's solve settings (as the per-rail solve), no pass/fail limits."""
    rails = _mapped(project)
    nets_seen = Counter(r.net for r in rails)
    twice = sorted(n for n, c in nets_seen.items() if c > 1)
    if twice:
        raise EngineError(f"Rail {', '.join(twice)} is in the project more than once")
    names = terminal_names(project)
    vrms: List[Vrm] = []
    loads: List[KipiLoad] = []
    labels: Dict[str, str] = {}
    for r in rails:
        if len(r.sources) > 1:
            raise EngineError(
                f"Rail {r.net} has {len(r.sources)} sources ({', '.join(s.label for s in r.sources)}): "
                "a power-tree solve drives each net group from one source. Keep one, or solve "
                "the rail on its own")
        nets = tied_nets(board, r.net)[0] if r.follow_net_ties else [r.net]
        for i, s in enumerate(r.sources):
            owner = f"Source {s.label}"
            if s.kind in SWITCHING_KINDS and s.efficiency is None:
                raise EngineError(f"{owner} on {r.net}: a switcher needs its efficiency (a "
                                  "fraction 0–1); there is no default")
            vrms.append(Vrm(
                name=names[("source", r.net, i)],
                output=_on_rail(board, nets, s.ref, s.pins, owner),
                voltage=float(s.voltage), kind=s.kind,
                efficiency=float(s.efficiency) if s.kind in SWITCHING_KINDS else UNUSED_EFFICIENCY,
                quiescent_a=float(s.quiescent_a),
                input=None if s.kind == "source" else _pin_ref(board, s, "input", owner),
                ground=_pin_ref(board, s, "ground", owner),
                sense=_pin_ref(board, s, "sense", owner),
                ground_sense=_pin_ref(board, s, "ground_sense", owner)))
        for i, ld in enumerate(r.loads):
            owner = f"Load {ld.label}"
            name = names[("load", r.net, i)]
            labels[name] = ld.label
            loads.append(KipiLoad(name=name, supply=_on_rail(board, nets, ld.ref, ld.pins, owner),
                                  current=float(ld.current),
                                  ground=_pin_ref(board, ld, "ground", owner)))

    for v in vrms:
        for ld in loads:
            if v.input is not None and _overlap(v.input, ld.supply):
                raise EngineError(
                    f"Load {labels[ld.name]} on {ld.supply.net} draws from {ld.supply.label()}, "
                    f"which is also the input of VRM {v.name} ({v.input.label()}): its current "
                    "would be counted twice; remove the load or the VRM input")

    links = []
    for i, k in enumerate(project.links):
        owner = f"Link {k.name}"
        a = _pads(board, [k.net_a], k.ref, k.pins_a, owner)
        b = _pads(board, [k.net_b], k.ref, k.pins_b, owner)
        links.append(DcLink(names[("link", "", i)], PinRef(k.ref, k.net_a, _numbers(a, k.pins_a)),
                            PinRef(k.ref, k.net_b, _numbers(b, k.pins_b)), float(k.resistance_ohm)))

    used = [p.net for v in vrms for p in v.pin_refs()]
    used += [p.net for ld in loads for p in (ld.supply, ld.ground) if p]
    used += [p.net for k in links for p in (k.side_a, k.side_b)]
    skip = {r.net for r in project.rails if not r.follow_net_ties}
    links += _tie_links(board, list(dict.fromkeys(used)), skip, set(names.values()))

    settings, _assumptions = engine_settings(project.settings)
    return PISetup(vrms=vrms, loads=loads, links=links, settings=settings,
                   via_current_limit_a=None,      # KiPI's 1 A is a placeholder, not a user limit
                   excluded_nets=_excluded_nets(project.engine_options))


def validate_setup(setup: PISetup) -> List[str]:
    """KiPI's checks of a setup (an LDO without input pins, a ground with no 0 V reference,
    a loop in the power tree…): one message per problem, empty when it can be solved."""
    return validate(setup)
