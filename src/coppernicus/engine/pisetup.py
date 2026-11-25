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

from kipi.dc.pi.model import DcLink, PinRef, PISetup, SensePoint, Vrm, validate
from kipi.dc.pi.model import Load as KipiLoad

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.solve import engine_settings, tied_nets
from coppernicus.project import PinSpec, Project, Rail, pin_ref

SWITCHING_KINDS = ("smps",)          # input power = output power / efficiency
# KiPI's DC solve reads a VRM's efficiency only for switchers; other kinds get this.
UNUSED_EFFICIENCY = 1.0
ENGINE_OPTIONS = ("excluded_nets",)

# ("source" | "load" | "link", the rail's net ("" for a link), index in its list)
Key = Tuple[str, str, int]


def ground_nets(project: Project) -> List[str]:
    """Ground nets the project names, in order (sources' ground and ground-sense pins, then
    loads' ground pins)."""
    out: List[str] = []
    for r in project.rails:
        for s in r.sources:
            out += [n for n in (s.ground_net, s.ground_sense_net) if n]
        out += [ld.ground_net for ld in r.loads if ld.ground_net]
    return list(dict.fromkeys(out))


def ground_rails(project: Project) -> List[Rail]:
    """Rails on a net the tree uses as a ground: set up to solve that ground on its own (a
    0 V source, its return pins as loads). The tree solves the net as a ground instead."""
    grounds = set(ground_nets(project))
    return [r for r in project.rails if r.net in grounds and (r.sources or r.loads)]


def _mapped(project: Project) -> List[Rail]:
    """The rails that go into the setup: every one with a source or a load, except those on
    a net used as a ground (see :func:`ground_rails`)."""
    grounds = set(ground_nets(project))
    return [r for r in project.rails if (r.sources or r.loads) and r.net not in grounds]


def _fresh(base: str, taken: set) -> str:
    """``base``, or ``base (2)``, ``base (3)``… the first one not in ``taken`` (added to it)."""
    name, k = base, 1
    while name in taken:
        k += 1
        name = f"{base} ({k})"
    taken.add(name)
    return name


def terminal_names(project: Project) -> Dict[Key, str]:
    """The unique name each source, load and link gets in the engine's setup (KiPI wants
    every VRM, load and link name unique). A label used once is kept; a source or load label
    used more than once becomes ``"<label> (<net>)"``; any duplicate left is numbered."""
    rails = _mapped(project)
    entries = [(("source", r.net, i), s.label, r.net) for r in rails for i, s in enumerate(r.sources)]
    entries += [(("load", r.net, i), ld.label, r.net) for r in rails for i, ld in enumerate(r.loads)]
    entries += [(("link", "", i), k.name, "") for i, k in enumerate(project.links)]
    count = Counter(label for _, label, _ in entries)
    wanted = [f"{label} ({net})" if net and count[label] > 1 else label for _, label, net in entries]
    # A name used once keeps it; repeats are numbered, never onto a name already in use.
    once = {n for n, c in Counter(wanted).items() if c == 1}
    taken = set(once)
    names = [n if n in once else _fresh(n, taken) for n in wanted]
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


def _extra_terminal(board: Board, nets: Sequence[str], source, owner: str) -> List[SensePoint]:
    """A source's ``extra_pins`` ("REF.PAD") as KiPI mesh-sense points, one per other part, each
    held at the source's voltage; empty without them. Refused with a remote sense of its own."""
    by_ref: Dict[str, List[str]] = {}
    for entry in getattr(source, "extra_pins", None) or ():
        ref, _, pad = entry.partition(".")
        by_ref.setdefault(ref, []).append(pad)
    if not by_ref:
        return []
    if source.sense_net or source.ground_sense_net:
        raise EngineError(f"{owner}: extra_pins (a terminal on several parts) and remote sense "
                          "cannot be combined: remove one")
    return [SensePoint(sense=_on_rail(board, nets, ref, pins, f"{owner} (extra pins)"))
            for ref, pins in by_ref.items()]


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
    from ``nets``. A tie touching a net in ``skip`` (a rail with ``follow_net_ties: false``)
    is left out, so that rail stays apart."""
    out, seen = [], set()
    for net in nets:
        if net in skip:
            continue
        for t in tied_nets(board, net)[1]:
            key = (t.ref, t.pad_a, t.pad_b)
            if key in seen or t.net_a in skip or t.net_b in skip:
                continue
            seen.add(key)
            name = _fresh(f"net tie {t.ref}", taken)
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
            output = _on_rail(board, nets, s.ref, s.pins, owner)
            held = _extra_terminal(board, nets, s, owner)
            vrms.append(Vrm(
                name=names[("source", r.net, i)],
                output=output,
                voltage=float(s.voltage), kind=s.kind,
                efficiency=float(s.efficiency) if s.kind in SWITCHING_KINDS else UNUSED_EFFICIENCY,
                quiescent_a=float(s.quiescent_a),
                input=None if s.kind == "source" else _pin_ref(board, s, "input", owner),
                ground=_pin_ref(board, s, "ground", owner),
                # extra_pins: KiPI's mesh sense holds its own pins and each other part's at the
                # voltage, the current entering there (one terminal on several parts).
                sense=output if held else _pin_ref(board, s, "sense", owner),
                ground_sense=_pin_ref(board, s, "ground_sense", owner),
                **({"mesh_sense": True, "sense_points": held} if held else {})))
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
    for k in links:
        for side, ref in (("a", k.side_a), ("b", k.side_b)):
            for ld in loads:
                if _overlap(ref, ld.supply):
                    raise EngineError(
                        f"Load {labels[ld.name]} on {ld.supply.net} draws from {ld.supply.label()}, "
                        f"which is also side {side} of link {k.name}: the current through the "
                        "link is already drawn by what is on its other side, so it would be "
                        "counted twice; remove the load or the link")

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
