"""Adding a pad picked on the board to a rail, an AC probe or a capacitor model: plain project
logic, no window or engine."""

from __future__ import annotations

from typing import Iterable, List, Set

from coppernicus.project import CapModel, Load, Probe, Rail, Source


def pick_nets(rail: Rail, tied: Iterable[str]) -> Set[str]:
    """Nets whose pads may join ``rail``: its own net, plus the nets joined to it by net ties
    (``tied``, which includes the rail's net) when the rail solves them as one."""
    return set(tied) | {rail.net} if rail.follow_net_ties else {rail.net}


def add_picked_pin(rail: Rail, kind: str, ref: str, pin: str) -> str:
    """Add pin ``pin`` of part ``ref`` to the rail's sources (``kind="source"``) or loads.

    The pin joins the existing row of the same part; a part without a row gets a new one
    with just that pin. A row that already has the pin, or uses all the part's pins, is
    left as it is. A new source takes the voltage of the rail's first source (0 V when there
    is none, to be set by the user); a new load draws 0 A until the user sets it. Returns a
    message for the status bar."""
    rows = rail.sources if kind == "source" else rail.loads
    what = "source" if kind == "source" else "load"
    row = next((t for t in rows if t.ref == ref), None)
    if row is not None:
        if row.pins is None:
            return f"{ref} already uses all its pins as a {what}"
        if pin in row.pins:
            return f"{ref}.{pin} is already in {what} {row.label}"
        row.pins = [*row.pins, pin]
        return f"Added {ref}.{pin} to {what} {row.label} (pins {', '.join(row.pins)})"
    if kind == "source":
        voltage = rail.sources[0].voltage if rail.sources else 0.0
        rail.sources.append(Source(ref, voltage, [pin]))
        hint = "" if rail.sources[:-1] else ": set its voltage"
        return f"New source {ref} with pin {pin} at {voltage:g} V{hint}"
    rail.loads.append(Load(ref, 0.0, [pin]))
    return f"New load {ref} with pin {pin}: set its current"


def add_probe_pin(probe: Probe, side: str, ref: str, pin: str, net: str) -> str:
    """Add pin ``pin`` of part ``ref`` on ``net`` to the probe's supply pins (``side=
    "supply"``) or its ground pins (``"ground"``). The first pick of a side sets its part and
    net (a probe without a name takes the part's); a pad on another net or another part is
    refused, as is a ground pad on the supply net. Returns a message for the status bar."""
    name = probe.name or ref
    if side == "supply":
        if not probe.ref or not probe.net:
            probe.name, probe.ref, probe.net, probe.pins = name, ref, net, [pin]
            if probe.ground_ref == ref:
                probe.ground_ref = ""                # "" is the probe's own part
            return f"Probe {name}: supply pin {ref}.{pin} on {net}"
        part, own_net, pins = probe.ref, probe.net, probe.pins
    else:
        if net == probe.net:
            return f"{ref}.{pin} is on {net}, the supply net of probe {name}: pick a ground pad"
        if not probe.ground_net:
            probe.name = name
            probe.ground_ref = "" if ref == probe.ref else ref
            probe.ground_net, probe.ground_pins = net, [pin]
            return f"Probe {name}: ground pin {ref}.{pin} on {net}"
        part, own_net, pins = probe.ground_ref or probe.ref, probe.ground_net, probe.ground_pins
    what = "supply" if side == "supply" else "ground"
    if net != own_net:
        return f"{ref}.{pin} is on {net}, not on {own_net}, the {what} net of probe {name}"
    if ref != part:
        return (f"{ref}.{pin} is another part: the {what} pins of probe {name} are {part}'s "
                f"(clear its {what} part to start again)")
    if pins is None:
        return f"Probe {name} already uses all the {what} pins of {part} on {own_net}"
    if pin in pins:
        return f"{ref}.{pin} is already a {what} pin of probe {name}"
    pins.append(pin)
    return f"Probe {name}: added {what} pin {ref}.{pin} ({what} pins {', '.join(pins)})"


def toggle_cap_ref(models: List[CapModel], i: int, ref: str) -> str:
    """Part ``ref`` joins capacitor model ``i``, leaving any other model it was in, or leaves
    model ``i`` when it is already there. Returns a message for the status bar."""
    model = models[i]
    if ref in model.refs:
        model.refs.remove(ref)
        return f"{ref} left capacitor model {model.name}"
    moved = [m.name for m in models if m is not model and ref in m.refs]
    for m in models:
        if m is not model and ref in m.refs:
            m.refs.remove(ref)
    model.refs.append(ref)
    was = f" (moved from {', '.join(moved)})" if moved else ""
    return f"{ref} joined capacitor model {model.name}{was}"
