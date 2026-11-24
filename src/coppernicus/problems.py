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

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    value: float                  # drop %, mean A/mm^2 or A, by kind
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
    out = (_rise(result, limits) + _loads(result, limits) + _hotspots(result, limits, top)
           + _vias(result, limits, top))
    order = {"rise": 0, "load": 0, "hotspot": 1, "via": 2}
    return sorted(out, key=lambda p: (p.severity != FAIL, order[p.kind], -p.value))


def _loads(result: RailResult, limits: Limits) -> List[Problem]:
    out = []
    for t in result.loads:
        pct, mv = result.drop_pct(t), result.drop(t) * 1e3
        fail = limits.max_drop_pct is not None and pct > limits.max_drop_pct
        x, y = t.positions[0] if t.positions else (np.nan, np.nan)
        limit = f"limit {limits.max_drop_pct:g} %" if limits.max_drop_pct is not None else "no limit set"
        out.append(Problem(
            FAIL if fail else INFO, "load",
            f"{t.name}: {t.v_min:.4f} V at its worst pin ({mv:.2f} mV, {pct:.2f} % below "
            f"{result.v_ref:g} V)",
            f"{t.ref}, {len(t.pad_ids)} pin(s), {t.current:g} A; {limit}.",
            pct, float(x), float(y),
            data={"name": t.name, "ref": t.ref, "drop_mv": mv, "drop_pct": pct,
                  "v_min_v": t.v_min, "current_a": t.current}))
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


def _hotspots(result: RailResult, limits: Limits, top: int) -> List[Problem]:
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
                f"Current crowding on {layer.name}: {mean_j:.1f} A/mm² mean over {area:.3g} mm²",
                f"Peak {max_j:.1f} A/mm² (mesh-dependent at sharp corners); {why}.",
                mean_j, float(x), float(y), layer.name,
                data={"mean_j_a_mm2": mean_j, "peak_j_a_mm2": max_j, "area_mm2": area}))
    found.sort(key=lambda p: -p.value)
    fails = [p for p in found if p.severity == FAIL]
    return fails + [p for p in found if p.severity != FAIL][:max(0, top - len(fails))]


def _vias(result: RailResult, limits: Limits, top: int) -> List[Problem]:
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
            f"{what} carrying {v.current:.3f} A (drill {v.drill_mm:g} mm)",
            f"{v.power * 1e3:.3g} mW in the barrel; {limit}.",
            v.current, float(v.position[0]), float(v.position[1]),
            data={"via_id": v.owner_id, "via_kind": v.kind, "current_a": v.current,
                  "power_w": v.power, "drill_mm": v.drill_mm}))
    return out


# ---------------------------------------------------------------------- power tree

# Limit keys as the agent interface names them (units in the key).
LIMIT_KEYS = {"max_drop_pct": "max_drop_pct", "max_current_density_a_mm2": "max_current_density",
              "max_via_current_a": "max_via_current", "max_ground_rise_mv": "max_ground_rise_mv"}


def limits_dict(limits: Optional[Limits], kind: str = "rail") -> Dict[str, float]:
    """The limits that apply to a group of ``kind``, keyed with their units. A ground has
    no nominal voltage, so a drop limit does not apply to it."""
    limits = limits or Limits()
    out = {key: getattr(limits, attr) for key, attr in LIMIT_KEYS.items()
           if getattr(limits, attr) is not None}
    # A ground has no nominal voltage for a drop limit; a supply rail of a tree has no rise.
    out.pop("max_drop_pct" if kind == "ground" else "max_ground_rise_mv", None)
    return out


def find_tree_problems(tree: TreeResult, group: str, limits: Optional[Limits] = None,
                       top: int = 5) -> List[Problem]:
    """The problems of one group of a power-tree solve, fails first. On a rail: its loads,
    ranked by the drop across their supply and ground pins (``LoadReport.drop_pct``). On a
    ground: its return pins, ranked by their rise above 0 V (no drop limit applies to a
    ground). Then current-crowding spots and vias, as on a single rail."""
    res = tree.groups[group]
    limits = limits or Limits()
    if res.kind == "ground":
        first = _rise(res, limits) + _returns(res, limits)
    else:
        first = _tree_loads(tree, group, res, limits)
    out = first + _hotspots(res, limits, top) + _vias(res, limits, top)
    order = {"rise": 0, "load": 0, "return": 0, "hotspot": 1, "via": 2}
    return sorted(out, key=lambda p: (p.severity != FAIL, order[p.kind], -p.value))


def _tree_loads(tree: TreeResult, group: str, res: RailResult, limits: Limits) -> List[Problem]:
    terms = {t.name: t for t in res.loads}
    out = []
    for ld in (ld for ld in tree.loads if ld.group == group):
        t = terms.get(ld.name)
        x, y = t.positions[0] if t is not None and t.positions else (np.nan, np.nan)
        limit = f"limit {limits.max_drop_pct:g} %" if limits.max_drop_pct is not None else "no limit set"
        if ld.status in (DISCONNECTED, NOT_CONVERGED):
            title, value = f"{ld.name}: {ld.status}", float("inf")
        else:
            word = "below" if ld.drop_v >= 0 else "above"
            title = (f"{ld.name}: {ld.v_worst_v:.4f} V across its supply and ground pins "
                     f"({abs(ld.drop_v) * 1e3:.2f} mV, {abs(ld.drop_pct):.2f} % {word} "
                     f"{ld.nominal_v:g} V)")
            value = ld.drop_pct
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


def _returns(res: RailResult, limits: Optional[Limits] = None) -> List[Problem]:
    """Ground pins ranked by rise: where the return current lifts the ground most, judged
    only against the user's ``max_ground_rise_mv`` (a drop limit does not apply)."""
    limit = (limits or Limits()).max_ground_rise_mv
    out = []
    for t in res.loads:
        rise = _signed_max(t.v_max - res.v_ref, t.v_min - res.v_ref)   # its worst pin
        if not np.isfinite(rise):
            continue
        x, y = t.positions[0] if t.positions else (np.nan, np.nan)
        fail = limit is not None and abs(rise) * 1e3 > limit
        note = f"limit {limit:g} mV" if limit is not None else "no ground-rise limit set"
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


def _rise(res: RailResult, limits: Limits) -> List[Problem]:
    """With a ``max_ground_rise_mv`` limit: the largest deviation of the net's copper from
    its reference, and where it is. On a ground group of a power tree it is signed (below
    0 V under a negative rail), judged by its magnitude; on a net solved alone it is the
    magnitude (that solve draws every load current out of the copper, so its sign is not
    the physical one)."""
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
    if res.kind != "ground":
        volts = abs(volts)
    mv = volts * 1e3
    where = f" on {layer}" if layer else ""
    title = (f"{res.net}: largest ground rise {from_0v(volts)}{where}" if res.kind == "ground"
             else f"{res.net}: largest ground rise {mv:.2f} mV{where}")
    return [Problem(
        FAIL if abs(mv) > limit else INFO, "rise", title,
        f"Copper furthest from the {res.v_ref:g} V reference; limit {limit:g} mV.",
        abs(mv), x, y, layer, data={"rise_mv": mv, "limit_mv": limit})]
