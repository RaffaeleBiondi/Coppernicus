"""Adding a pad picked on the board to a rail: plain project logic, no window or engine."""

from __future__ import annotations

from typing import Iterable, Set

from coppernicus.project import Load, Rail, Source


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
