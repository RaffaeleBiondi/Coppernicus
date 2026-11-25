"""Where and why a rail fails: a ranked list of problems, each with a place on the board.

No limits are invented here. Every load, current-crowding spot and via is *ranked*; a
problem is a ``fail`` only when the user set the matching limit in the rail's
:class:`~coppernicus.project.Limits` and the result exceeds it.

Current crowding is reported per *spot*: connected triangles in the top share of current
density on a layer, summarised by their area-weighted mean |J|. A point maximum is not
used because at a sharp inner corner the 2D current density is singular, so its value
depends on the mesh.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from coppernicus.project import Limits
from coppernicus.results import DISCONNECTED, NOT_CONVERGED, LayerField, RailResult, TreeResult

FAIL, INFO = "fail", "info"
HOTSPOT_TOP_SHARE = 0.01      # without a limit: the top 1 % of a layer's copper area by |J|
HOTSPOT_GROW = 0.8            # ... grown through neighbours above 80 % of that threshold
VIA_NOISE_SHARE = 1e-6        # vias below this share of the rail current carry nothing (round-off)


@dataclass
class Problem:
    severity: str                 # FAIL or INFO
    kind: str                     # "load", "hotspot" or "via"
    title: str
    detail: str
    value: float                  # the ranking: drop % (|mV| at 0 V nominal), mean A/mm^2, A or mV, by kind
    x: float
    y: float
    layer: Optional[str] = None
    # The numbers behind the title, keyed with their units (``drop_mv``, ``mean_j_a_mm2``,
    # ``current_a`` ...), for reports and the agent interface.
    data: Dict[str, Any] = field(default_factory=dict)


def find_problems(result: RailResult, limits: Optional[Limits] = None,
                  top: int = 5) -> List[Problem]:
    """Every load, then at most ``top`` current-crowding spots and ``top`` vias (more when
    they exceed a limit). Fails first."""
    limits = limits or Limits()
    rise = _rise(result, limits) if not result.v_ref else []    # a ground-rise limit: 0 V nets only
    out = rise + _loads(result, limits) + _hotspots(result, limits, top) + _vias(result, limits, top)
    order = {"rise": 0, "load": 0, "hotspot": 1, "via": 2}
    return sorted(out, key=lambda p: (p.severity != FAIL, order[p.kind], -p.value))


def _loads(result: RailResult, limits: Limits) -> List[Problem]:
    out = []
    for t in result.loads:
        pct, mv = result.drop_pct(t), result.drop(t) * 1e3
        fail = limits.max_drop_pct is not None and pct > limits.max_drop_pct
        x, y = t.positions[0] if t.positions else (np.nan, np.nan)
        limit = f"limit {limits.max_drop_pct:g} %" if limits.max_drop_pct is not None else "no limit set"
        share = f", {pct:.2f} %" if np.isfinite(pct) else ""        # none at 0 V
        word = "below" if result.sign > 0 else "above"
        out.append(Problem(
            FAIL if fail else INFO, "load",
            f"{t.name}: {result.worst_v(t):.4f} V at its worst pin ({mv:.2f} mV{share} {word} "
            f"{result.v_ref:g} V)",
            f"{t.ref}, {len(t.pad_ids)} pin(s), {t.current:g} A; {limit}.",
            _rank(pct, mv), float(x), float(y),
            data={"name": t.name, "ref": t.ref, "drop_mv": mv, "drop_pct": pct,
                  "v_min_v": t.v_min, "v_worst_v": result.worst_v(t), "current_a": t.current}))
    return out


def _spots(layer: LayerField, threshold: float, grow: float = 1.0):
    """Connected groups of triangles around those with |J| ≥ ``threshold``: each group is
    grown through neighbours with |J| ≥ ``grow`` · threshold (hysteresis, so a uniform track
    near the threshold is one spot, not many). Returns (indices, mean J, max J, area)."""
    j_all = layer.current_density
    cand = np.flatnonzero(j_all >= grow * threshold)
    if cand.size == 0:
        return []
    tris = layer.tris[cand]
    # Two candidate triangles are neighbours when they share a node.
    node_tri = np.repeat(np.arange(len(cand)), 3)
    inc = coo_matrix((np.ones(tris.size), (node_tri, tris.ravel())),
                     shape=(len(cand), int(layer.points.shape[0]))).tocsr()
    n, labels = connected_components(inc @ inc.T, directed=False)
    spots = []
    for k in range(n):
        idx = cand[labels == k]
        j = j_all[idx]
        if j.max() < threshold:          # grown region without a seed above the threshold
            continue
        area = layer.tri_area[idx]
        spots.append((idx, float((j * area).sum() / area.sum()), float(j.max()), float(area.sum())))
    return spots


def _top_share_threshold(layer: LayerField, share: float) -> float:
    """|J| above which the top ``share`` of the layer's copper area lies."""
    order = np.argsort(layer.current_density)[::-1]
    cum = np.cumsum(layer.tri_area[order])
    k = int(np.searchsorted(cum, share * cum[-1]))
    return float(layer.current_density[order[min(k, len(order) - 1)]])


def _against(title: str, fail: bool, value: Optional[float], unit: str, source: str) -> str:
    """A copper fail's title, naming the limit it used and the net it came from."""
    return f"{title} (limit {value:g} {unit} {source})" if fail and source else title


def _hotspots(result: RailResult, limits: Limits, top: int, source: str = "") -> List[Problem]:
    found = []
    for layer in result.layers:
        if layer.current_density.size == 0 or not np.any(layer.current_density > 0):
            continue
        if limits.max_current_density is not None:
            threshold, grow = limits.max_current_density, 1.0
            why = f"above the {limits.max_current_density:g} A/mm² limit"
        else:
            threshold, grow = _top_share_threshold(layer, HOTSPOT_TOP_SHARE), HOTSPOT_GROW
            why = f"top {HOTSPOT_TOP_SHARE:.0%} of this layer's copper by current density"
        cents = layer.centroids()
        for idx, mean_j, max_j, area in _spots(layer, threshold, grow):
            w = layer.tri_area[idx]
            x, y = (cents[idx] * w[:, None]).sum(axis=0) / w.sum()
            fail = limits.max_current_density is not None and mean_j > limits.max_current_density
            found.append(Problem(
                FAIL if fail else INFO, "hotspot",
                _against(f"Current crowding on {layer.name}: {mean_j:.1f} A/mm² mean over {area:.3g} mm²",
                         fail, limits.max_current_density, "A/mm²", source),
                f"Peak {max_j:.1f} A/mm² (mesh-dependent at sharp corners); {why}.",
                mean_j, float(x), float(y), layer.name,
                data={"mean_j_a_mm2": mean_j, "peak_j_a_mm2": max_j, "area_mm2": area}))
    found.sort(key=lambda p: -p.value)
    fails = [p for p in found if p.severity == FAIL]
    return fails + [p for p in found if p.severity != FAIL][:max(0, top - len(fails))]


def _vias(result: RailResult, limits: Limits, top: int, source: str = "") -> List[Problem]:
    vias = sorted(result.vias, key=lambda v: -v.current)
    # Vias carrying a numerically zero current (solver round-off, ~1e-21 W) are not listed.
    # This is display noise, not a verdict: below VIA_NOISE_SHARE of the rail's current.
    floor = VIA_NOISE_SHARE * sum(abs(t.current) for t in result.sources)
    out = []
    for v in vias:
        fail = limits.max_via_current is not None and v.current > limits.max_via_current
        if not fail and len(out) >= top:
            continue
        if v.current <= floor:
            continue
        what = "Via" if v.kind == "via" else "Plated hole"
        limit = (f"limit {limits.max_via_current:g} A" if limits.max_via_current is not None
                 else "no limit set")
        out.append(Problem(
            FAIL if fail else INFO, "via",
            _against(f"{what} carrying {v.current:.3f} A (drill {v.drill_mm:g} mm)",
                     fail, limits.max_via_current, "A", source),
            f"{v.power * 1e3:.3g} mW in the barrel; {limit}.",
            v.current, float(v.position[0]), float(v.position[1]),
            data={"via_id": v.owner_id, "via_kind": v.kind, "current_a": v.current,
                  "power_w": v.power, "drill_mm": v.drill_mm}))
    return out


# ---------------------------------------------------------------------- power tree

# Limit keys as the agent interface names them (units in the key).
LIMIT_KEYS = {"max_drop_pct": "max_drop_pct", "max_current_density_a_mm2": "max_current_density",
              "max_via_current_a": "max_via_current", "max_ground_rise_mv": "max_ground_rise_mv"}


GROUND_ONLY = "applies to nets at 0 V (grounds) only"
SUPPLY_ONLY = "0 V nominal: a % drop limit does not apply (set max_ground_rise_mv)"


def limits_dict(limits: Optional[Limits], kind: str = "rail",
                v_ref: Optional[float] = None) -> Dict[str, float]:
    """The limits that apply to a net or group, keyed with their units. One rule, by the
    reference voltage: ``max_ground_rise_mv`` applies to a net at 0 V (a ground group, or a
    rail driven at 0 V: ``v_ref`` 0), ``max_drop_pct`` to a net with a nonzero nominal; a
    ground group has no nominal at all. Copper limits apply to both."""
    limits = limits or Limits()
    out = {key: getattr(limits, attr) for key, attr in LIMIT_KEYS.items()
           if getattr(limits, attr) is not None}
    if kind == "ground":
        out.pop("max_drop_pct", None)
    elif v_ref is None or v_ref != 0:
        out.pop("max_ground_rise_mv", None)
    return out


def inapplicable_limits(net: str, limits: Optional[Limits], v_ref: float,
                        kind: str = "rail") -> List[Dict[str, str]]:
    """The limits set on ``net`` that do not apply to it (:func:`limits_dict`), as not-judged
    entries ``{item, limit, reason}``: a ground-rise limit on a net with a nonzero nominal, a
    drop limit on a ground group. (A drop limit on a rail driven at 0 V is listed load by
    load: "0 V nominal".)"""
    limits = limits or Limits()
    out = []
    if limits.max_ground_rise_mv is not None and kind != "ground" and v_ref != 0:
        out.append({"item": net, "limit": "max_ground_rise_mv", "reason": GROUND_ONLY})
    if limits.max_drop_pct is not None and kind == "ground":
        out.append({"item": net, "limit": "max_drop_pct", "reason": SUPPLY_ONLY})
    return out


NetLimits = Callable[..., Tuple[Limits, Dict[str, str]]]      # (net, rail=None)


def find_tree_problems(tree: TreeResult, group: str, limits: Optional[Limits] = None,
                       top: int = 5) -> List[Problem]:
    """The problems of one group of a power-tree solve, fails first. On a rail: its loads
    and the inputs of the regulators it feeds, ranked by the drop across their supply (input)
    and ground pins. On a ground: its return pins, ranked by their rise from 0 V (no drop
    limit applies to a ground). Then current-crowding spots and vias, as on a single rail.

    Limits per net (:data:`coppernicus.power_tree.LIMITS_RULE`): each pin by its net's,
    the copper by the strictest of the group's. ``limits``, if given, stands for the limits
    set on the group's driving net (its key) in this call. A group whose iteration did not
    converge is not judged."""
    from coppernicus.power_tree import group_limits, net_limits
    if limits is not None:
        tree = replace(tree, limits={**tree.limits, group: limits})
    res = tree.groups[group]
    if res.converged:
        glim, gorigin = group_limits(tree, group)

        def per_net(net, rail=None):
            return net_limits(tree, net or group, group, rail)
    else:
        glim, gorigin = Limits(), {}

        def per_net(net, rail=None):
            return Limits(), {}

    def source(attr):        # where a copper limit came from, for the titles of its fails
        if attr not in gorigin:
            return ""
        return f"from {gorigin[attr]}" + (", strictest in group" if len(res.nets) > 1 else "")
    if res.kind == "ground":
        first = (_rise(res, glim, source("max_ground_rise_mv"))
                 + _returns(res, None, per_net))
    else:
        first = _tree_loads(tree, group, res, per_net) + _tree_inputs(tree, group, res, per_net)
        if not res.v_ref:                                   # a rail driven at 0 V
            first += _rise(res, glim, source("max_ground_rise_mv"))
    out = (first + _hotspots(res, glim, top, source("max_current_density"))
           + _vias(res, glim, top, source("max_via_current")))
    order = {"rise": 0, "load": 0, "return": 0, "hotspot": 1, "via": 2}
    return sorted(out, key=lambda p: (p.severity != FAIL, order[p.kind], -p.value))


def _limit_text(value: Optional[float], unit: str, origin: str, own: str) -> str:
    """"limit 1 %", "limit 1 % (from +3V3)" when it comes from another net, or "no limit set"."""
    if value is None:
        return "no limit set"
    return f"limit {value:g} {unit}" + (f" (from {origin})" if origin and origin != own else "")


def _share(pct: float) -> str:
    """", 1.23 %" after a drop in mV; nothing at 0 V nominal, where there is no %."""
    return f", {abs(pct):.2f} %" if np.isfinite(pct) else ""


def _rank(pct: float, mv: float) -> float:
    """A load's ranking value: its drop %, or at 0 V nominal (no %) its |drop| in mV."""
    return pct if np.isfinite(pct) else abs(mv)


def _word(drop: float, nominal: float) -> str:
    """Which side of the nominal a pin that lost ``drop`` toward 0 V sits on."""
    return "below" if (drop >= 0) == (nominal >= 0) else "above"


def _tree_loads(tree: TreeResult, group: str, res: RailResult, per_net: NetLimits) -> List[Problem]:
    terms = {t.name: t for t in res.loads}
    out = []
    for ld in (ld for ld in tree.loads if ld.group == group):
        t = terms.get(ld.name)
        x, y = t.positions[0] if t is not None and t.positions else (np.nan, np.nan)
        lim, org = per_net(ld.net, ld.rail)
        limit = _limit_text(lim.max_drop_pct, "%", org.get("max_drop_pct", ""), ld.net or group)
        if ld.status in (DISCONNECTED, NOT_CONVERGED):
            title, value = f"{ld.name}: {ld.status}", float("inf")
        else:
            title = (f"{ld.name}: {ld.v_worst_v:.4f} V across its supply and ground pins "
                     f"({abs(ld.drop_v) * 1e3:.2f} mV{_share(ld.drop_pct)} "
                     f"{_word(ld.drop_v, ld.nominal_v)} {ld.nominal_v:g} V)")
            value = _rank(ld.drop_pct, ld.drop_v * 1e3)
        ground = ("no ground pins" if ld.v_ground_v is None
                  else f"ground at {ld.v_ground_v * 1e3:.2f} mV")
        status = f" {ld.status}." if ld.status and ld.status not in title else ""
        out.append(Problem(
            FAIL if ld.passed is False else INFO, "load", title,
            f"{ld.current_a:g} A, supply {ld.v_supply_v:.4f} V, {ground}; {limit}.{status}",
            value, float(x), float(y),
            data={"name": ld.name, "drop_mv": ld.drop_v * 1e3, "drop_pct": ld.drop_pct,
                  "v_across_v": ld.v_across_v, "v_worst_v": ld.v_worst_v,
                  "current_a": ld.current_a, "status": ld.status}))
    return out


def _tree_inputs(tree: TreeResult, group: str, res: RailResult, per_net: NetLimits) -> List[Problem]:
    """The inputs of the regulators this rail feeds, judged as its loads."""
    terms = {t.name: t for t in res.loads}
    out = []
    for v in (v for v in tree.vrms if v.input_group == group):
        name = f"{v.name} (input)"
        t = terms.get(name)
        x, y = t.positions[0] if t is not None and t.positions else (np.nan, np.nan)
        lim, org = per_net(v.input_net)
        limit = _limit_text(lim.max_drop_pct, "%", org.get("max_drop_pct", ""), v.input_net)
        if v.input_status in (DISCONNECTED, NOT_CONVERGED) or v.input_drop_v is None:
            title, value = f"{name}: {v.input_status or 'not solved'}", float("inf")
        else:
            pct = v.input_drop_pct if v.input_drop_pct is not None else float("nan")
            title = (f"{name}: {v.v_in_worst_v:.4f} V across its input and ground pins "
                     f"({abs(v.input_drop_v) * 1e3:.2f} mV{_share(pct)} "
                     f"{_word(v.input_drop_v, v.input_nominal_v or 0.0)} {v.input_nominal_v:g} V)")
            value = _rank(pct, v.input_drop_v * 1e3)
        status = f" {v.input_status}." if v.input_status and v.input_status not in title else ""
        amps = f"{v.i_in_a:g} A in" if v.i_in_a is not None else "input current not known"
        out.append(Problem(
            FAIL if v.input_passed is False else INFO, "load", title,
            f"Input of {v.kind} {v.name} on {v.input_net}, {amps}; {limit}.{status}",
            value, float(x), float(y),
            data={"name": name, "drop_mv": None if v.input_drop_v is None else v.input_drop_v * 1e3,
                  "drop_pct": v.input_drop_pct, "v_across_v": v.v_in_worst_v,
                  "v_worst_v": v.v_in_worst_v, "current_a": v.i_in_a, "status": v.input_status}))
    return out


def _returns(res: RailResult, limits: Optional[Limits] = None,
             per_net: Optional[NetLimits] = None) -> List[Problem]:
    """Ground pins ranked by rise: where the return current lifts the ground most, judged
    only against the user's ``max_ground_rise_mv`` (a drop limit does not apply): each pin
    by its net's (``per_net``), else by ``limits``."""
    out = []
    for t in res.loads:
        rise = _signed_max(t.v_max - res.v_ref, t.v_min - res.v_ref)   # its worst pin
        if not np.isfinite(rise):
            continue
        x, y = t.positions[0] if t.positions else (np.nan, np.nan)
        lim, org = per_net(t.net) if per_net else (limits or Limits(), {})
        limit = lim.max_ground_rise_mv
        fail = limit is not None and abs(rise) * 1e3 > limit
        note = (_limit_text(limit, "mV", org.get("max_ground_rise_mv", ""), t.net or res.net)
                if limit is not None else "no ground-rise limit set")
        out.append(Problem(
            FAIL if fail else INFO, "return", f"{t.name}: ground {from_0v(rise)}",
            f"{t.ref}, {len(t.pad_ids)} pin(s), {abs(t.current):g} A; {note}.",
            abs(rise) * 1e3, float(x), float(y),
            data={"name": t.name, "ref": t.ref, "rise_mv": rise * 1e3, "current_a": t.current}))
    return out


def _signed_max(a: float, b: float) -> float:
    """Whichever of ``a`` and ``b`` is larger in magnitude, with its sign."""
    return a if abs(a) >= abs(b) else b


def from_0v(volts: float) -> str:
    """A ground's rise in words: "12.34 mV above 0 V" or "12.34 mV below 0 V"."""
    return f"{abs(volts) * 1e3:.2f} mV {'above' if volts >= 0 else 'below'} 0 V"


def drop_words(volts: float, v_ref: float, sign: float, rise: bool) -> Tuple[str, str]:
    """What a drop of ``volts`` is called and which side of its reference it sits on, from
    the net's ``v_ref``, ``sign`` (:attr:`RailResult.sign`) and whether it is read as a rise
    (:meth:`RailResult.is_rise`): ("drop", "below 3.3 V"), ("drop", "above -3.3 V (toward
    0 V)"), or on a ground ("ground rise", "above 0 V") and, below 0 V (a ground under a
    negative rail), ("ground shift", "below 0 V"). A NaN is a rise, as for
    :meth:`RailResult.is_shift`."""
    if rise:
        return ("ground shift", "below 0 V") if volts < 0 else ("ground rise", "above 0 V")
    if sign < 0:
        return "drop", f"above {v_ref:g} V (toward 0 V)"
    return "drop", f"below {v_ref:g} V"


def max_drop_text(res: RailResult) -> str:
    """The worst of the copper for a summary line: "max drop 12.34 mV below 3.3 V", "max
    drop 12.34 mV above -3.3 V (toward 0 V)", or, on a ground (a ground group, a net at 0 V
    solved alone), "max ground rise 5.67 mV above 0 V" or "max ground shift 5.67 mV below
    0 V" (:func:`drop_words`)."""
    what, where = drop_words(res.max_drop, res.v_ref, res.sign, res.is_rise())
    mv = abs(res.max_drop) * 1e3 if res.is_rise() else res.max_drop * 1e3
    return f"max {what} {mv:.2f} mV {where}"


def _rise(res: RailResult, limits: Limits, source: str = "") -> List[Problem]:
    """With a ``max_ground_rise_mv`` limit: the largest deviation of the net's copper from
    its reference, and where it is, signed (a ground below 0 V under a negative rail) and
    judged by its magnitude. Where the load current is drawn out of the copper
    (``drop_sign`` +1: a rail at 0 V inside a power tree, whose sign is the opposite of a
    ground's), it is the magnitude."""
    limit = limits.max_ground_rise_mv
    if limit is None:
        return []
    best = (0.0, np.nan, np.nan, None)
    for lf in res.layers:
        dev = lf.voltage - res.v_ref
        if not np.any(np.isfinite(dev)):
            continue
        i = int(np.nanargmax(np.abs(dev)))
        if abs(dev[i]) > abs(best[0]):
            best = (float(dev[i]), float(lf.points[i, 0]), float(lf.points[i, 1]), lf.name)
    volts, x, y, layer = best
    what, side = drop_words(volts, res.v_ref, res.sign, True)   # its side, from the signed value
    if res.sign > 0:
        volts = abs(volts)
    mv = volts * 1e3
    where = f" on {layer}" if layer else ""
    title = f"{res.net}: largest {what} {abs(mv):.2f} mV {side}{where}"
    return [Problem(
        FAIL if abs(mv) > limit else INFO, "rise", _against(title, abs(mv) > limit, limit, "mV", source),
        f"Copper furthest from the {res.v_ref:g} V reference; limit {limit:g} mV.",
        abs(mv), x, y, layer, data={"rise_mv": mv, "limit_mv": limit})]
