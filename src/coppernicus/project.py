"""The project file: which board, which rails, their sources, loads and limits.

Engine-independent plain dataclasses, saved as YAML (``*.coppernicus.yml``). The board path
is stored relative to the project file so a project folder can be moved as a whole.

A *rail* is one power net. Its *sources* are parts whose pins are held at a fixed voltage
(a regulator output); its *loads* are parts whose pins draw a current, split equally across
the pins (that is what the engine solves). Pins are given as a list of pad numbers, glob
patterns (``"A*"``), or ``None`` / ``"all"`` for every pin of the part on the rail's net.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PinSpec = Optional[List[str]]


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
    """Pins of ``ref`` held at ``voltage`` (a regulator output, a connector supply)."""
    ref: str
    voltage: float
    pins: PinSpec = None
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.ref


@dataclass
class Load:
    """Pins of ``ref`` drawing ``current`` amps in total, split equally across the pins."""
    ref: str
    current: float
    pins: PinSpec = None
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or self.ref


@dataclass
class Limits:
    """Pass/fail limits set by the user. ``None`` = not checked: problems are then ranked,
    never judged."""
    max_drop_pct: Optional[float] = None
    max_current_density: Optional[float] = None    # A/mm^2
    max_via_current: Optional[float] = None         # A


@dataclass
class Rail:
    net: str
    sources: List[Source] = field(default_factory=list)
    loads: List[Load] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    # Nets joined to this one by net-tie footprints are solved with it, the tie a short.
    follow_net_ties: bool = True


@dataclass
class Settings:
    max_edge_mm: float = 0.5                  # largest mesh edge on planes
    # None: the engine's default plating table (IPC-6012 Class 2 minimum average, by hole
    # type); a number: that plating for every plated hole.
    via_plating_um: Optional[float] = None
    copper_temp_c: float = 20.0               # resistivity is referenced to 20 °C


@dataclass
class Project:
    board: str = ""
    rails: List[Rail] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    path: Optional[Path] = None               # where it was loaded from / saved to; not saved

    # ------------------------------------------------------------------ queries
    def board_path(self) -> Path:
        p = Path(self.board).expanduser()
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
            d["pins"] = "all" if t.pins is None else list(t.pins)
            d[value_key] = t.voltage if value_key == "voltage" else t.current
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
            rails.append(rd)
        return {"board": self.board, "settings": dict(vars(self.settings)), "rails": rails}

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
                    sources=[Source(ref=str(s["ref"]), voltage=float(s["voltage"]),
                                    pins=parse_pins(s.get("pins")), name=str(s.get("name", "")))
                             for s in rd.get("sources") or []],
                    loads=[Load(ref=str(ld["ref"]), current=float(ld["current"]),
                                pins=parse_pins(ld.get("pins")), name=str(ld.get("name", "")))
                           for ld in rd.get("loads") or []],
                    limits=Limits(**(rd.get("limits") or {})),
                    follow_net_ties=bool(rd.get("follow_net_ties", True)),
                ))
        except (KeyError, TypeError, ValueError) as e:
            raise ProjectError(f"invalid project file: {e}") from e
        return cls(board=str(data.get("board", "")), rails=rails, settings=settings, path=path)

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
            board = Path(self.board).expanduser()
            if not board.is_absolute():   # relative to where the project was, or will be
                board = (Path(self.path).parent if self.path else Path(path).parent) / board
            board = board.resolve()
            try:
                self.board = os.path.relpath(board, Path(path).resolve().parent)
            except ValueError:            # another drive (Windows): keep it absolute
                self.board = str(board)
        self.path = path
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.to_dict(), f, sort_keys=False, allow_unicode=True)
        return path
