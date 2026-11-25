"""Editing a project file for an agent, on the file's own mapping.

The YAML mapping is edited in place, rail by rail, so keys Coppernicus does not know (a
team's notes, fields of a later version) survive an edit; a replaced rail keeps its own
unknown keys too. New rails and links are written in the file's form (``voltage``,
``current``, ``a``/``b`` pin references) and the whole mapping goes through the project
loader before anything is written, so every check the loader makes applies.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence

from coppernicus.service import errors as E
from coppernicus.service.convert import LIMIT_KEYS, SETTING_KEYS
from coppernicus.service.errors import ServiceError

RAIL_KEYS = ("net", "sources", "loads", "limits", "follow_net_ties", "override_board_fields")
SOURCE_KEYS = ("ref", "voltage_v", "pins", "name", "kind", "efficiency", "quiescent_a", "input",
               "ground", "sense", "ground_sense")
LOAD_KEYS = ("ref", "current_a", "pins", "name", "ground")
PIN_REF_KEYS = ("net", "pins", "ref")
LINK_KEYS = ("name", "ref", "a", "b", "resistance_ohm")
# What a rail in the file holds that this module writes; anything else is kept on replace.
_FILE_RAIL_KEYS = ("net", "sources", "loads", "limits", "follow_net_ties", "override_board_fields")


def _keys(data: Any, allowed: Sequence[str], what: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ServiceError(E.INVALID_INPUT, f"{what} must be an object, not {data!r}")
    unknown = [k for k in data if k not in allowed]
    if unknown:
        raise ServiceError(E.INVALID_INPUT, f"unknown {what} key(s): {', '.join(map(str, unknown))}",
                           f"allowed keys: {', '.join(allowed)}")
    return data


def _pins(value: Any):
    return "all" if value is None else value


def _pin_ref(data: Any, what: str) -> Dict[str, Any]:
    d = _keys(data, PIN_REF_KEYS, what)
    if not d.get("net"):
        raise ServiceError(E.INVALID_INPUT, f"{what} needs its net", "a pin reference is {net, pins?, ref?}")
    out: Dict[str, Any] = {"ref": d["ref"]} if d.get("ref") else {}
    out["net"] = d["net"]
    out["pins"] = _pins(d.get("pins"))
    return out


def source_to_file(spec: Any) -> Dict[str, Any]:
    s = _keys(spec, SOURCE_KEYS, "source")
    for key in ("ref", "voltage_v"):
        if s.get(key) is None:
            raise ServiceError(E.INVALID_INPUT, f"a source needs {key}",
                               "a source is {ref, voltage_v, pins?, ...}")
    out: Dict[str, Any] = {"name": s["name"]} if s.get("name") else {}
    out.update(ref=s["ref"], pins=_pins(s.get("pins")), voltage=s["voltage_v"])
    for key in ("kind", "efficiency", "quiescent_a"):
        if s.get(key) is not None:
            out[key] = s[key]
    for key in ("input", "ground", "sense", "ground_sense"):
        if s.get(key) is not None:
            out[key] = _pin_ref(s[key], f"source {s['ref']} {key}")
    return out


def load_to_file(spec: Any) -> Dict[str, Any]:
    ld = _keys(spec, LOAD_KEYS, "load")
    for key in ("ref", "current_a"):
        if ld.get(key) is None:
            raise ServiceError(E.INVALID_INPUT, f"a load needs {key}",
                               "a load is {ref, current_a, pins?, ...}")
    out: Dict[str, Any] = {"name": ld["name"]} if ld.get("name") else {}
    out.update(ref=ld["ref"], pins=_pins(ld.get("pins")), current=ld["current_a"])
    if ld.get("ground") is not None:
        out["ground"] = _pin_ref(ld["ground"], f"load {ld['ref']} ground")
    return out


def rail_to_file(spec: Any, old: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A rail in the file's form; with ``old`` (the rail it replaces), that rail's keys this
    module does not write are kept."""
    r = _keys(spec, RAIL_KEYS, "rail")
    if not r.get("net"):
        raise ServiceError(E.INVALID_INPUT, "a rail needs its net",
                           "a rail is {net, sources, loads, limits?}")
    out: Dict[str, Any] = {"net": str(r["net"]),
                           "sources": [source_to_file(s) for s in r.get("sources") or []],
                           "loads": [load_to_file(ld) for ld in r.get("loads") or []]}
    limits = _keys(r.get("limits") or {}, tuple(LIMIT_KEYS), "limits")
    file_limits = {LIMIT_KEYS[k]: v for k, v in limits.items() if v is not None}
    if file_limits:
        out["limits"] = file_limits
    if r.get("follow_net_ties") is False:
        out["follow_net_ties"] = False
    if r.get("override_board_fields"):
        out["override_board_fields"] = True
    for key, value in (old or {}).items():
        if key not in _FILE_RAIL_KEYS:
            out[key] = copy.deepcopy(value)
    return out


def link_to_file(spec: Any) -> Dict[str, Any]:
    k = _keys(spec, LINK_KEYS, "link")
    for key in ("name", "ref", "a", "b", "resistance_ohm"):
        if k.get(key) is None:
            raise ServiceError(E.INVALID_INPUT, f"a link needs {key}",
                               "a link is {name, ref, a: {net, pins?}, b: {net, pins?}, resistance_ohm}")
    return {"name": k["name"], "ref": k["ref"], "a": _pin_ref(k["a"], f"link {k['name']} a"),
            "b": _pin_ref(k["b"], f"link {k['name']} b"), "resistance_ohm": k["resistance_ohm"]}


def edit(mapping: Dict[str, Any], *, rails: Optional[Sequence[Any]] = None,
         remove_rails: Optional[Sequence[str]] = None, links: Optional[Sequence[Any]] = None,
         remove_links: Optional[Sequence[str]] = None, settings: Optional[Mapping[str, Any]] = None,
         use_board_fields: Optional[bool] = None) -> Dict[str, Any]:
    """The project mapping with the edits applied (a copy): rails added or replaced by net,
    rails removed by net, links added or replaced by name, links removed by name, settings
    keys set (None removes a key: its default again), the KiCad fields switched."""
    out = copy.deepcopy(mapping)
    file_rails: List[Dict[str, Any]] = list(out.get("rails") or [])
    for net in remove_rails or []:
        kept = [r for r in file_rails if r.get("net") != net]
        if len(kept) == len(file_rails):
            names = ", ".join(str(r.get("net")) for r in file_rails) or "none"
            raise ServiceError(E.INVALID_INPUT, f"no rail {net} to remove", f"rails in the project: {names}")
        file_rails = kept
    for spec in rails or []:
        net = spec.get("net") if isinstance(spec, Mapping) else None
        at = next((i for i, r in enumerate(file_rails) if r.get("net") == net), None)
        new = rail_to_file(spec, file_rails[at] if at is not None else None)
        if at is None:
            file_rails.append(new)
        else:
            file_rails[at] = new
    out["rails"] = file_rails
    file_links: List[Dict[str, Any]] = list(out.get("links") or [])
    for name in remove_links or []:
        kept = [k for k in file_links if k.get("name") != name]
        if len(kept) == len(file_links):
            names = ", ".join(str(k.get("name")) for k in file_links) or "none"
            raise ServiceError(E.INVALID_INPUT, f"no link {name} to remove", f"links in the project: {names}")
        file_links = kept
    for spec in links or []:
        new = link_to_file(spec)
        at = next((i for i, k in enumerate(file_links) if k.get("name") == new["name"]), None)
        if at is None:
            file_links.append(new)
        else:
            file_links[at] = new
    if file_links:
        out["links"] = file_links
    else:
        out.pop("links", None)
    if settings:
        _keys(settings, SETTING_KEYS, "settings")
        file_settings = dict(out.get("settings") or {})
        for key, value in settings.items():
            if value is None:
                file_settings.pop(key, None)
            else:
                file_settings[key] = copy.deepcopy(value)
        out["settings"] = file_settings
    if use_board_fields is False:
        out["use_board_fields"] = False
    elif use_board_fields is True:
        out.pop("use_board_fields", None)
    return out
