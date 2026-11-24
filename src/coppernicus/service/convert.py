"""Plain-data views of projects, results and problems: dictionaries whose keys carry their
units (``_v``, ``_mv``, ``_a``, ``_a_mm2``, ``_mm``, ``_w``, ``_pct``), safe to dump as JSON.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from coppernicus.problems import FAIL, Problem
from coppernicus.project import Limits, Settings, parse_pins, pins_text
from coppernicus.results import RailResult
from coppernicus.service import errors as E
from coppernicus.service.errors import ServiceError

SIG_DIGITS = 7

# Caveats that belong next to every rail result (see docs/agent-interface-brief.md §8).
RAIL_CAVEATS = [
    "A rail solve covers this net only (and nets joined by net ties); the ground return is "
    "not included. Solve the ground net as its own rail to see its drop.",
    "Point maxima of |J| (j_point_max_a_mm2, peak_j_a_mm2) at sharp inner corners depend on "
    "the mesh; use the hotspot means and cut_current instead.",
]
NET_KIND_CAVEAT = ("Net kind (power / ground / signal) is a name-and-zone heuristic, not a "
                   "certainty.")

# Limits: our unit-keyed names <-> project.Limits fields.
LIMIT_KEYS = {"max_drop_pct": "max_drop_pct",
              "max_current_density_a_mm2": "max_current_density",
              "max_via_current_a": "max_via_current"}
SETTING_KEYS = ("max_edge_mm", "via_plating_um", "copper_temp_c", "copper_thickness_mm")


# ---------------------------------------------------------------------- JSON safety

def _round(x: float) -> Optional[float]:
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    return float(f"{x:.{SIG_DIGITS}g}")


def jsonable(obj: Any) -> Any:
    """``obj`` with numpy scalars and arrays made plain, NaN/inf made ``None``, floats
    rounded to 7 significant digits, tuples made lists and paths made strings."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return _round(float(obj))
    if isinstance(obj, Path):
        return str(obj)
    return obj


# ---------------------------------------------------------------------- inputs

def _number(value: Any, what: str, positive: bool = False, allow_zero: bool = True) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ServiceError(E.INVALID_INPUT, f"{what} must be a number, not {value!r}") from None
    if not math.isfinite(x):
        raise ServiceError(E.INVALID_INPUT, f"{what} must be finite, not {value!r}")
    if positive and (x < 0 or (x == 0 and not allow_zero)):
        raise ServiceError(E.INVALID_INPUT, f"{what} must be {'≥' if allow_zero else '>'} 0, not {x:g}")
    return x


def _check_keys(data: Mapping[str, Any], allowed, what: str):
    unknown = [k for k in data if k not in allowed]
    if unknown:
        raise ServiceError(E.INVALID_INPUT, f"unknown {what} key(s): {', '.join(map(str, unknown))}",
                           f"allowed keys: {', '.join(allowed)}")


def limits_from_dict(data: Optional[Mapping[str, Any]]) -> Limits:
    data = dict(data or {})
    _check_keys(data, LIMIT_KEYS, "limits")
    kw = {}
    for key, attr in LIMIT_KEYS.items():
        if data.get(key) is not None:
            kw[attr] = _number(data[key], key, positive=True, allow_zero=False)
    return Limits(**kw)


def limits_to_dict(limits: Limits) -> Dict[str, float]:
    return {key: getattr(limits, attr) for key, attr in LIMIT_KEYS.items()
            if getattr(limits, attr) is not None}


def settings_from_dict(data: Optional[Mapping[str, Any]], base: Optional[Settings] = None
                       ) -> Tuple[Settings, Dict[str, float]]:
    """Solve settings and the copper-thickness overrides (layer name → mm)."""
    data = dict(data or {})
    _check_keys(data, SETTING_KEYS, "settings")
    base = base or Settings()
    s = Settings(max_edge_mm=base.max_edge_mm, via_plating_um=base.via_plating_um,
                 copper_temp_c=base.copper_temp_c)
    if data.get("max_edge_mm") is not None:
        s.max_edge_mm = _number(data["max_edge_mm"], "max_edge_mm", positive=True, allow_zero=False)
    if data.get("via_plating_um") is not None:
        s.via_plating_um = _number(data["via_plating_um"], "via_plating_um", positive=True,
                                   allow_zero=False)
    if data.get("copper_temp_c") is not None:
        s.copper_temp_c = _number(data["copper_temp_c"], "copper_temp_c")
    thickness = {}
    for layer, mm in (data.get("copper_thickness_mm") or {}).items():
        thickness[str(layer)] = _number(mm, f"copper_thickness_mm[{layer}]", positive=True,
                                        allow_zero=False)
    return s, thickness


def settings_to_dict(s: Settings, thickness: Mapping[str, float]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"max_edge_mm": s.max_edge_mm, "via_plating_um": s.via_plating_um,
                           "copper_temp_c": s.copper_temp_c}
    if thickness:
        out["copper_thickness_mm"] = dict(thickness)
    return out


def terminal_from_dict(data: Mapping[str, Any], kind: str, base=None):
    """A source (``voltage_v``) or load (``current_a``) from a plain dict.

    With ``base`` (an existing Source or Load), the result is ``base`` with the dict's
    fields replaced: every other field of ``base`` (and its exact class) is carried over,
    so fields this function does not know about are never dropped."""
    from coppernicus.project import Load, Source
    if not isinstance(data, Mapping):
        raise ServiceError(E.INVALID_INPUT, f"each {kind} must be an object, not {data!r}")
    value_key = "voltage_v" if kind == "source" else "current_a"
    _check_keys(data, ("ref", value_key, "pins", "name"), kind)
    ref = str(data.get("ref") or "").strip()
    if not ref:
        raise ServiceError(E.INVALID_INPUT, f"a {kind} needs a part reference ('ref')",
                           "use net_parts to list the parts on the net")
    if data.get(value_key) is None:
        raise ServiceError(E.INVALID_INPUT, f"{kind} {ref} needs '{value_key}'")
    try:
        pins = parse_pins(data.get("pins"))
    except ValueError as e:
        raise ServiceError(E.INVALID_INPUT, f"{kind} {ref}: {e}") from None
    name = str(data.get("name") or "")
    if kind == "source":
        values = {"ref": ref, "voltage": _number(data[value_key], f"{ref} voltage_v"),
                  "pins": pins, "name": name}
        cls = Source
    else:
        values = {"ref": ref, "current": _number(data[value_key], f"{ref} current_a", positive=True),
                  "pins": pins, "name": name}
        cls = Load
    if base is not None:
        return dataclasses.replace(base, **values)
    return cls(**values)


def rail_from_dicts(net: str, sources: Sequence[Mapping[str, Any]], loads: Sequence[Mapping[str, Any]],
                    limits: Optional[Mapping[str, Any]] = None, follow_net_ties: bool = True,
                    base=None):
    """A Rail from plain dicts. With ``base`` (an existing Rail), the result keeps every
    field of ``base`` it does not set (and its exact class), and each terminal starts from
    the matching terminal of ``base``: the one in the same row when its ref matches, else
    the first unused one with that ref. Fields added to the project model later are thus
    carried through without changes here."""
    from coppernicus.project import Rail

    def build(items, old, kind):
        used, out = set(), []
        for i, data in enumerate(items):
            ref = str(data.get("ref") or "").strip() if isinstance(data, Mapping) else ""
            match = None
            if i < len(old) and old[i].ref == ref and i not in used:
                match = i
            else:
                match = next((j for j, t in enumerate(old) if t.ref == ref and j not in used), None)
            if match is not None:
                used.add(match)
            out.append(terminal_from_dict(data, kind, old[match] if match is not None else None))
        return out

    old_sources = list(base.sources) if base is not None else []
    old_loads = list(base.loads) if base is not None else []
    values = {"net": str(net), "sources": build(sources, old_sources, "source"),
              "loads": build(loads, old_loads, "load"), "limits": limits_from_dict(limits),
              "follow_net_ties": bool(follow_net_ties)}
    return dataclasses.replace(base, **values) if base is not None else Rail(**values)


# ---------------------------------------------------------------------- outputs

def problem_dict(p: Problem) -> Dict[str, Any]:
    out: Dict[str, Any] = {"severity": p.severity, "kind": p.kind, "title": p.title,
                           "detail": p.detail, "layer": p.layer, "x_mm": p.x, "y_mm": p.y}
    out.update(p.data)
    return out


def verdict(problems: List[Problem], limits: Limits) -> Dict[str, Any]:
    """``fail`` when a limit the user set is exceeded, ``pass`` when limits are set and none
    is, ``not_judged`` without limits (problems are then only ranked)."""
    checked = limits_to_dict(limits)
    failed = [p for p in problems if p.severity == FAIL]
    if failed:
        v = "fail"
    elif checked:
        v = "pass"
    else:
        v = "not_judged"
    note = ("Judged only against the limits you set: " + ", ".join(f"{k} = {val:g}"
                                                                   for k, val in checked.items())
            if checked else "No limits set: nothing is judged, problems are ranked only.")
    return {"verdict": v, "limits": checked, "failed": len(failed), "verdict_note": note}


def rail_summary(result_id: str, board_id: str, res: RailResult, rail, problems: List[Problem],
                 settings: Settings, thickness: Mapping[str, float],
                 layer_thickness: Mapping[str, float], top: int = 5) -> Dict[str, Any]:
    """The compact summary of a solved rail. ``problems`` are listed in full: every load,
    then the ``top`` current-crowding spots and the ``top`` vias (more when they exceed a
    limit), as :func:`coppernicus.problems.find_problems` ranked them."""
    v_ref = res.v_ref
    pct = (lambda volts: 100.0 * volts / v_ref) if v_ref else (lambda volts: float("nan"))
    limits = rail.limits
    loads = []
    for t, spec in zip(res.loads, rail.loads):
        drop = res.drop(t)
        exceeds = (None if limits.max_drop_pct is None
                   else bool(res.drop_pct(t) > limits.max_drop_pct))
        x, y = t.positions[0] if t.positions else (float("nan"), float("nan"))
        loads.append({"name": t.name, "ref": t.ref, "pins": pins_text(spec.pins),
                      "pin_count": len(t.pad_ids), "current_a": t.current, "v_min_v": t.v_min,
                      "v_max_v": t.v_max, "drop_mv": drop * 1e3, "drop_pct": res.drop_pct(t),
                      "exceeds_drop_limit": exceeds, "x_mm": x, "y_mm": y})
    sources = []
    for t, spec in zip(res.sources, rail.sources):
        x, y = t.positions[0] if t.positions else (float("nan"), float("nan"))
        sources.append({"name": t.name, "ref": t.ref, "pins": pins_text(spec.pins),
                        "pin_count": len(t.pad_ids), "voltage_v": spec.voltage,
                        "current_a": t.current, "x_mm": x, "y_mm": y})
    total = res.power_loss or 1.0
    layers = []
    for lf in res.layers:
        finite = lf.voltage[np.isfinite(lf.voltage)]
        layers.append({"name": lf.name, "power_w": lf.power, "loss_share_pct": 100.0 * lf.power / total,
                       "max_drop_mv": (v_ref - finite.min()) * 1e3 if finite.size else None,
                       "copper_area_mm2": float(lf.tri_area.sum()),
                       "thickness_mm": layer_thickness.get(lf.name), "triangles": int(len(lf.tris))})
    busiest = max(res.layers, key=lambda lf: lf.power).name if res.layers else None
    out = {
        "result_id": result_id, "board_id": board_id, "net": res.net, "nets_solved": res.nets,
        "v_ref_v": v_ref, "sources": sources, "loads": loads,
        "max_drop_mv": res.max_drop * 1e3, "max_drop_pct": pct(res.max_drop),
        "worst_load": max(loads, key=lambda d: d["drop_mv"])["name"] if loads else None,
        "power_loss_w": res.power_loss, "loss_breakdown_w": res.loss_breakdown(),
        "busiest_layer": busiest, "layers": layers,
        "via_count": len(res.vias), "via_current_max_a": max((v.current for v in res.vias), default=0.0),
        "j_point_max_a_mm2": res.j_max,
        **verdict(problems, limits),
        "problems": [problem_dict(p) for p in problems],
        "problems_total": len(problems),
        "problems_note": f"Every load, then the top {top} current-crowding spots and the top {top} "
                         "vias by current (more when they exceed a limit you set).",
        "settings": settings_to_dict(settings, thickness),
        "energy_balance_rel_error": res.energy_balance_error,
        "stats": {"triangles": int(res.stats.get("triangles", 0)),
                  "unknowns": int(res.stats.get("unknowns", 0)),
                  "solve_s": res.stats.get("seconds")},
        "assumptions": list(res.assumptions) + RAIL_CAVEATS,
        "warnings": list(res.warnings),
    }
    return out
