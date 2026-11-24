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
from coppernicus.results import LayerField, RailResult

FAIL, INFO = "fail", "info"
HOTSPOT_TOP_SHARE = 0.01      # without a limit: the top 1 % of a layer's copper area by |J|
HOTSPOT_GROW = 0.8            # ... grown through neighbours above 80 % of that threshold


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
    out = _loads(result, limits) + _hotspots(result, limits, top) + _vias(result, limits, top)
    order = {"load": 0, "hotspot": 1, "via": 2}
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
    out = []
    for v in vias:
        fail = limits.max_via_current is not None and v.current > limits.max_via_current
        if not fail and len(out) >= top:
            continue
        if v.current <= 0:
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
