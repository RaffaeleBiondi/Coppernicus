"""The board temperature of an electro-thermal power-tree solve, read from
:mod:`coppernicus.results` alone (no engine): its verdict against the user's limits, its
JSON summary, its text and its PNG maps.

What is judged (coordinator's rulings): the hottest **solved** copper of each layer (KiPI's
``layer_temps`` rows), against ``max_temp_c`` and ``max_rise_c`` (over the ambient), only
the user's. A row over a limit fails even when the solve has not settled (the last iteration
still underestimates while the temperature rises), and the fail says so. Otherwise the
limits are not judged when the solve did not settle, ran one iteration, or a group the rows
come from did not converge. The grid peak is shown, never judged. Limits set while the tree
was solved without the temperature are listed as not judged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

THERMAL_LIMITS = ("max_temp_c", "max_rise_c")
ITEM = "board temperature"
NOT_SOLVED = "board temperature not solved (thermal disabled in the project)"
CMAP = "inferno"


def thermal_limits(options) -> Optional[Dict[str, float]]:
    """``{max_temp_c, max_rise_c}`` of thermal options (or a mapping), only those set;
    ``None`` when none is."""
    if options is None:
        return None
    get = options.get if isinstance(options, Mapping) else (lambda k: getattr(options, k, None))
    out = {k: float(get(k)) for k in THERMAL_LIMITS if get(k) is not None}
    return out or None


def apply_thermal_limits(tree, options) -> None:
    """Read the project's board-temperature limits into ``tree.thermal_limits`` and judge the
    board temperature again, if it was solved; no solve needed (as ``apply_limits``)."""
    tree.thermal_limits = thermal_limits(options)
    if tree.thermal is not None:
        judge(tree.thermal, tree.thermal_limits, tree.groups)


def temp_passed(t_c: float, ambient_c: float, limits: Optional[Mapping[str, float]]) -> Optional[bool]:
    """KiPI's rule (``kipi/dc/thermal/model.py:114-120`` ``temp_passed``): within
    ``max_temp_c`` and with a rise over ``ambient_c`` within ``max_rise_c``; None without
    limits."""
    if not limits:
        return None
    t_max, rise = limits.get("max_temp_c"), limits.get("max_rise_c")
    return bool((t_max is None or t_c <= t_max) and (rise is None or t_c - ambient_c <= rise))


@dataclass
class ThermalVerdict:
    """``fail`` when a layer's hottest solved copper exceeds a limit; ``pass`` when every
    row is judged and within; ``not_judged`` otherwise (no limit, not solved, not settled,
    one iteration, a group that did not converge, nothing to judge)."""
    verdict: str
    failed: int
    limits: Dict[str, float]
    not_judged: List[Dict[str, str]] = field(default_factory=list)
    note: str = ""
    problems: List[Dict[str, Any]] = field(default_factory=list)


def _limits_text(limits: Mapping[str, float]) -> str:
    return ", ".join(f"{k} = {v:g} °C" for k, v in limits.items())


def _pad_text(ref: str, number: str, distance: Optional[float]) -> str:
    if not ref or distance is None:
        return "no pad on the board"
    return f"nearest pad {ref}.{number} ({distance:.2f} mm)"


def _not_settled(temp) -> str:
    """'' when settled; else what the last iteration did."""
    if temp.settled is None:
        return "one iteration: settling not checked"
    if temp.settled is False:
        return f"not settled: last change {temp.history[-1]['dt_max_c']:.3g} °C"
    return ""


def judge(temp, limits, groups: Optional[Mapping[str, Any]] = None) -> ThermalVerdict:
    """Judge a :class:`~coppernicus.results.BoardTemperature` against ``limits`` (thermal
    options or ``{max_temp_c, max_rise_c}``): sets each layer row's ``passed`` and returns
    the verdict. ``groups`` (``TreeResult.groups``) tells which rows come from a group that
    did not converge."""
    limits = thermal_limits(limits) or {}
    for row in temp.copper:
        row.passed = temp_passed(row.t_max_c, temp.ambient_c, limits)
    unsettled = _not_settled(temp)
    problems = []
    for row in temp.copper:
        fail = row.passed is False
        title = (f"Hottest copper on {row.layer_name}: {row.t_max_c:.2f} °C (rise {row.rise_c:.2f} °C) "
                 f"at ({row.x_mm:.2f}, {row.y_mm:.2f}) mm, "
                 f"{_pad_text(row.pad_ref, row.pad_number, row.pad_distance_mm)}")
        if fail:
            title += f" (limit {_limits_text(limits)}" + (f"; {unsettled})" if unsettled else ")")
        problems.append({"severity": "FAIL" if fail else "INFO", "kind": "temperature", "title": title,
                         "layer": row.layer_name, "x_mm": row.x_mm, "y_mm": row.y_mm,
                         "t_max_c": row.t_max_c, "rise_c": row.rise_c, "group": row.group,
                         "nearest_pad": _pad(row.pad_ref, row.pad_number, row.pad_distance_mm)})
    problems.sort(key=lambda p: (p["severity"] != "FAIL", -p["t_max_c"]))
    if not limits:
        return ThermalVerdict("not_judged", 0, {}, [], "No limits set: nothing is judged.", problems)
    failed = [r for r in temp.copper if r.passed is False]
    text = _limits_text(limits)
    if failed:
        rows = ", ".join(f"{r.layer_name} {r.t_max_c:.2f} °C (rise {r.rise_c:.2f} °C, "
                         f"{_pad_text(r.pad_ref, r.pad_number, r.pad_distance_mm)})" for r in failed)
        note = f"Board temperature against {text}: {rows} over the limit"
        return ThermalVerdict("fail", len(failed), dict(limits), [],
                              note + (f"; {unsettled}." if unsettled else "."), problems)
    reasons = []
    if not temp.copper:
        reasons.append("no solved copper to judge")
    if temp.settled is False:
        last = temp.history[-1]
        reasons.append(f"last change {last['dt_max_c']:.3g} °C over a "
                       f"{last['t_max_c'] - temp.ambient_c:.3g} °C rise: run more iterations")
    elif temp.settled is None:
        reasons.append("one iteration: settling not checked")
    stuck = sorted({r.group for r in temp.copper if groups is not None and r.group in groups
                    and not groups[r.group].converged})
    if stuck:
        reasons.append(f"group {', '.join(stuck)} did not converge")
    peak = ""
    if temp_passed(temp.peak_c, temp.ambient_c, limits) is False:
        near = _pad_text(temp.peak_pad_ref, temp.peak_pad_number, temp.peak_pad_distance_mm)
        peak = (f" The grid peak {temp.peak_c:.2f} °C on {temp.peak_layer} at ({temp.peak_x_mm:.2f}, "
                f"{temp.peak_y_mm:.2f}) mm, {near}, exceeds it but is not judged: only each layer's "
                "hottest solved copper is.")
    if reasons:
        reason = "; ".join(reasons)
        return ThermalVerdict("not_judged", 0, dict(limits),
                              [{"item": ITEM, "net": "", "limit": k, "reason": reason} for k in limits],
                              f"Board temperature not judged against {text}: {reason}.{peak}", problems)
    return ThermalVerdict("pass", 0, dict(limits), [],
                          f"Board temperature against {text}: every layer's hottest solved copper is "
                          f"within it.{peak}", problems)


def thermal_verdict(tree) -> ThermalVerdict:
    """The board-temperature verdict of a power tree: judged when it was solved, its limits
    listed as not judged when it was not (``tree.thermal_limits``)."""
    limits = getattr(tree, "thermal_limits", None) or {}
    temp = getattr(tree, "thermal", None)
    if temp is not None:
        return judge(temp, limits, tree.groups)
    if not limits:
        return ThermalVerdict("not_judged", 0, {}, [], "")
    return ThermalVerdict("not_judged", 0, dict(limits),
                          [{"item": ITEM, "net": "", "limit": k, "reason": NOT_SOLVED} for k in limits],
                          f"Board temperature not judged against {_limits_text(limits)}: {NOT_SOLVED}.")


# ---------------------------------------------------------------------- summary and text

def _pad(ref: str, number: str, distance: Optional[float]) -> Optional[Dict[str, Any]]:
    if not ref or distance is None:
        return None
    return {"ref": ref, "pad": number, "distance_mm": float(distance)}


def _change_pct(before: float, after: float) -> Optional[float]:
    if not (math.isfinite(before) and math.isfinite(after)) or before == 0:
        return None
    return 100.0 * (after - before) / abs(before)


def thermal_summary(tree) -> Dict[str, Any]:
    """``tree.thermal`` as a JSON-ready dict (units in every key, no arrays), with its
    verdict; NaN is left for the caller's cleaning (``tree_summary`` turns it into null)."""
    temp = tree.thermal
    tv = thermal_verdict(tree)
    return {
        "verdict": tv.verdict, "verdict_note": tv.note, "not_judged": list(tv.not_judged),
        "limits": dict(tv.limits), "failed": tv.failed,
        "ambient_c": temp.ambient_c, "airflow_m_s": temp.airflow_m_s, "h_w_m2k": temp.h_w_m2k,
        "grid_mm": temp.grid_mm, "grid_nx": temp.grid_nx, "grid_ny": temp.grid_ny,
        "grid_cells": temp.grid_cells, "iterations": temp.iterations, "settled": temp.settled,
        "history": [dict(h) for h in temp.history],
        "load_heat": temp.load_heat, "regulator_heat": temp.regulator_heat,
        "heat_w": temp.heat_w, "heat_by_kind_w": dict(temp.heat_by_kind_w),
        "peak_c": temp.peak_c, "peak_rise_c": temp.peak_rise_c, "peak_x_mm": temp.peak_x_mm,
        "peak_y_mm": temp.peak_y_mm, "peak_layer": temp.peak_layer,
        "peak_nearest_pad": _pad(temp.peak_pad_ref, temp.peak_pad_number, temp.peak_pad_distance_mm),
        "layers": [{"layer": r.layer_name, "t_max_c": r.t_max_c, "rise_c": r.rise_c, "x_mm": r.x_mm,
                    "y_mm": r.y_mm, "group": r.group,
                    "exceeds_limit": None if r.passed is None else not r.passed,
                    "nearest_pad": _pad(r.pad_ref, r.pad_number, r.pad_distance_mm)}
                   for r in temp.copper],
        "loads": [{"name": ld["name"], "group": ld["group"], "drop_ambient_mv": ld["drop_ambient_v"] * 1e3,
                   "drop_mv": ld["drop_v"] * 1e3,
                   "change_pct": _change_pct(ld["drop_ambient_v"], ld["drop_v"])} for ld in temp.loads],
        "problems": tv.problems,
        "assumptions": list(temp.assumptions), "warnings": list(temp.warnings),
    }


def _num(x: Optional[float], spec: str, unit: str = "") -> str:
    return "—" if x is None or not math.isfinite(x) else f"{x:{spec}}{unit}"


def temperature_text(doc: Mapping[str, Any], skip: Sequence[str] = ()) -> List[str]:
    """The lines ``solve --tree`` prints for the board temperature, from
    :func:`thermal_summary` (``doc["thermal"]``); assumptions in ``skip`` (already printed
    with the tree's) are left out."""
    settled = {True: "settled", False: "not settled", None: "one iteration"}[doc.get("settled")]
    n = doc["iterations"]
    lines = [f"Board temperature (electro-thermal: {n} iteration{'s' if n != 1 else ''}, {settled}; "
             f"ambient {doc['ambient_c']:g} °C, airflow {doc['airflow_m_s']:g} m/s; grid "
             f"{doc['grid_nx']} × {doc['grid_ny']} cells of {doc['grid_mm']:.3g} mm)"]
    pad = doc.get("peak_nearest_pad")
    near = _pad_text(pad["ref"], pad["pad"], pad["distance_mm"]) if pad else "no pad on the board"
    lines.append(f"  grid peak {_num(doc['peak_c'], '.2f', ' °C')} "
                 f"(rise {_num(doc['peak_rise_c'], '.2f', ' °C')}) on {doc['peak_layer']} at "
                 f"({doc['peak_x_mm']:.2f}, {doc['peak_y_mm']:.2f}) mm, {near}: shown, not judged")
    hist = doc.get("history") or []
    if hist:
        lines.append("  history: " + " → ".join(_num(h["t_max_c"], ".2f") for h in hist)
                     + f" °C, last change {_num(hist[-1]['dt_max_c'], '.3g', ' °C')}")
    by_kind = sorted((doc.get("heat_by_kind_w") or {}).items(), key=lambda kv: -(kv[1] or 0))
    lines.append(f"  heat {_num(doc['heat_w'], '.3g', ' W')} ("
                 + ", ".join(f"{k} {_num(w, '.3g', ' W')}" for k, w in by_kind) + ")")
    lines.append(f"  {'layer':<10} {'hottest':>10} {'rise':>10}  {'at (x, y) mm':<18} {'group':<10} "
                 f"{'nearest pad':<20} verdict")
    for r in doc.get("layers") or []:
        p = r.get("nearest_pad")
        near = f"{p['ref']}.{p['pad']} {p['distance_mm']:.2f} mm" if p else "—"
        verdict = "FAIL" if r["exceeds_limit"] else "pass" if r["exceeds_limit"] is False else "not judged"
        at = "({:.2f}, {:.2f})".format(r["x_mm"], r["y_mm"])
        lines.append(f"  {r['layer']:<10} {_num(r['t_max_c'], '.2f', ' °C'):>10} "
                     f"{_num(r['rise_c'], '.2f', ' °C'):>10}  {at:<18} {r['group']:<10} {near:<20} {verdict}")
    loads = doc.get("loads") or []
    if loads:
        lines.append("  load drops, copper at the ambient (iteration 1) → electro-thermal:")
        for ld in loads:
            pct = "" if ld.get("change_pct") is None else f" ({ld['change_pct']:+.1f} %)"
            lines.append(f"    {ld['name']:<14} {ld['group']:<10} {_num(ld['drop_ambient_mv'], '.3f')} → "
                         f"{_num(ld['drop_mv'], '.3f', ' mV')}{pct}")
    lines.append(f"  board temperature: {doc['verdict']}. {doc['verdict_note']}")
    lines += [f"  thermal assumption: {a}" for a in doc.get("assumptions") or [] if a not in skip]
    lines += [f"  thermal warning: {w}" for w in doc.get("warnings") or []]
    return lines


# ---------------------------------------------------------------------- maps

def write_png(temp, layer: str, path, outline: Sequence[np.ndarray] = (), dpi: int = 150) -> Dict[str, Any]:
    """The board temperature on copper layer ``layer`` as a PNG at ``path``: one colour per
    grid cell (grey outside the board), every layer on one scale from the ambient to the
    board's peak; the layer's hottest cell (×) and its hottest solved copper (○) marked.
    Returns the scale ``{"unit", "min", "max"}``."""
    from matplotlib import colormaps
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    m = temp.map(layer)
    if m is None:
        raise ValueError(f"no board temperature on layer {layer!r} "
                         f"(layers: {', '.join(t.layer_name for t in temp.maps)})")
    lo, hi = float(temp.ambient_c), max(float(temp.peak_c), float(temp.ambient_c) + 1e-9)
    d = m.cell_mm
    fig = Figure(figsize=(8, 6), dpi=dpi, layout="constrained")
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    im = ax.imshow(np.ma.masked_invalid(m.t_c), cmap=colormaps[CMAP].with_extremes(bad="0.75"),
                   vmin=lo, vmax=hi, origin="upper", interpolation="nearest",
                   extent=(m.x_mm[0] - d / 2, m.x_mm[-1] + d / 2, m.y_mm[-1] + d / 2, m.y_mm[0] - d / 2))
    fig.colorbar(im, ax=ax, label="board temperature (°C)")
    for ring in outline:
        closed = np.vstack([ring, ring[:1]])
        ax.plot(closed[:, 0], closed[:, 1], color="0.35", lw=0.8)
    x, y, t = m.hottest()
    ax.scatter([x], [y], marker="x", s=40, c="#00bfff", zorder=5)
    row = next((r for r in temp.copper if r.layer_name == m.layer_name), None)
    if row is not None:
        ax.scatter([row.x_mm], [row.y_mm], marker="o", s=36, facecolors="none", edgecolors="#00bfff",
                   zorder=5)
    ax.set_title(f"Board temperature — {m.layer_name}: hottest cell {t:.2f} °C ({d:.3g} mm cells)")
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    fig.savefig(path)
    return {"unit": "°C", "min": lo, "max": hi}
