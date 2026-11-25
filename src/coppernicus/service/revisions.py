"""Two revisions of a board compared: results from different board files (before and after a
change in KiCad), whose meshes differ.

Loads are matched by part (and name), layer losses by layer name, vias by position and
drill (ids are not trusted to survive a save). B's drop is interpolated onto A's mesh, node by
node, to find where it grew or shrank most; copper that only one of the two has cannot be
compared there and is reported as an area per layer, both ways.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from coppernicus.compare import interpolate_voltage
from coppernicus.fields import TriangleLocator
from coppernicus.results import LayerField, RailResult

MOVED_MM = 0.01          # a part whose first pad moved more than this has moved
VIA_MM = 0.01            # vias closer than this, with the same drill, are the same via
MIN_OVERLAP = 0.5        # below this share of A's copper also in B, no point is named


def _rows(summary: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {ld["name"]: ld for ld in summary.get("loads", [])}


def compare_loads(sa: Mapping[str, Any], sb: Mapping[str, Any]
                  ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[str]]:
    """Per-load drops of two revisions, matched by name (the part, unless named); what was
    removed, added or moved; and warnings when the currents or the sources differ."""
    la, lb = _rows(sa), _rows(sb)
    rows, moved, warnings, currents = [], [], [], []
    for name, da in la.items():
        db = lb.get(name)
        if db is None:
            continue
        rows.append({"name": name, "ref": da["ref"], "estimated": bool(da.get("estimated")),
                     "drop_mv_a": da["drop_mv"], "drop_mv_b": db["drop_mv"],
                     "delta_drop_mv": _delta(db["drop_mv"], da["drop_mv"]),
                     "drop_pct_a": da["drop_pct"], "drop_pct_b": db["drop_pct"],
                     "current_a_a": da["current_a"], "current_a_b": db["current_a"]})
        if not math.isclose(da["current_a"], db["current_a"], rel_tol=1e-6, abs_tol=1e-12):
            currents.append(name)
        if _far(da, db, MOVED_MM):
            moved.append(da["ref"])
    if currents:
        warnings.append(f"Load currents differ ({', '.join(currents)}): the deltas mix copper and "
                        "current; give both revisions the same loads to compare the copper.")
    va = {s["ref"]: s["voltage_v"] for s in sa.get("sources", [])}
    vb = {s["ref"]: s["voltage_v"] for s in sb.get("sources", [])}
    changed = [r for r in va if r in vb and not math.isclose(va[r], vb[r], rel_tol=1e-9)]
    if changed or set(va) != set(vb):
        warnings.append(f"The sources differ (A: {_sources(va)}; B: {_sources(vb)}): the drops are "
                        "measured from different set points.")
    unmatched = {"loads_only_in_a": [n for n in la if n not in lb],
                 "loads_only_in_b": [n for n in lb if n not in la], "moved": sorted(set(moved)),
                 "nets": None if sa["net"] == sb["net"] else {"a": sa["net"], "b": sb["net"]}}
    return rows, unmatched, warnings


def compare_vias(ra: RailResult, rb: RailResult) -> Dict[str, Any]:
    """Via currents of two revisions, matched by position (within ``VIA_MM``) and drill."""
    free = list(rb.vias)
    matched, only_a = [], []
    for v in sorted(ra.vias, key=lambda v: (v.position[0], v.position[1])):
        w = next((w for w in free if math.dist(v.position, w.position) <= VIA_MM
                  and math.isclose(v.drill_mm, w.drill_mm, rel_tol=1e-6)), None)
        if w is None:
            only_a.append(_via(v))
            continue
        free.remove(w)
        matched.append({**_via(v), "current_a_a": v.current, "current_a_b": w.current,
                        "delta_current_a": w.current - v.current})
    only_b = [_via(w) for w in sorted(free, key=lambda w: (w.position[0], w.position[1]))]
    return {"matched": matched, "only_in_a": only_a, "only_in_b": only_b,
            "largest_changes": sorted(matched, key=lambda m: -abs(m["delta_current_a"]))[:5]}


def drop_map(ra: RailResult, rb: RailResult) -> Tuple[Dict[str, Any], Dict[str, np.ndarray], List[str]]:
    """B's drop on A's mesh, minus A's, per common layer (mV per node of A; NaN where B has no
    copper), with where it grew and shrank most and the copper only one of them has."""
    by_b = {lf.name: lf for lf in rb.layers}
    deltas: Dict[str, np.ndarray] = {}
    copper = {"a_mm2": 0.0, "b_mm2": 0.0, "a_not_in_b_mm2": 0.0, "b_not_in_a_mm2": 0.0}
    best_up: Optional[Tuple[float, str, float, float]] = None
    best_down: Optional[Tuple[float, str, float, float]] = None
    for lf in ra.layers:
        copper["a_mm2"] += float(lf.tri_area.sum())
        lb = by_b.get(lf.name)
        if lb is None:
            copper["a_not_in_b_mm2"] += float(lf.tri_area.sum())
            continue
        drop_a = ra.drop_at(lf.voltage)
        drop_b = rb.drop_at(interpolate_voltage(lb, lf.points))
        delta = (drop_b - drop_a) * 1e3
        deltas[lf.name] = delta
        copper["a_not_in_b_mm2"] += _outside(lf, lb)
        finite = np.flatnonzero(np.isfinite(delta))
        if finite.size:
            i, j = finite[np.argmax(delta[finite])], finite[np.argmin(delta[finite])]
            if best_up is None or delta[i] > best_up[0]:
                best_up = (float(delta[i]), lf.name, float(lf.points[i, 0]), float(lf.points[i, 1]))
            if best_down is None or delta[j] < best_down[0]:
                best_down = (float(delta[j]), lf.name, float(lf.points[j, 0]), float(lf.points[j, 1]))
    by_a = {lf.name: lf for lf in ra.layers}
    for lb in rb.layers:
        copper["b_mm2"] += float(lb.tri_area.sum())
        la = by_a.get(lb.name)
        copper["b_not_in_a_mm2"] += float(lb.tri_area.sum()) if la is None else _outside(lb, la)
    warnings = []
    overlap = 1.0 - copper["a_not_in_b_mm2"] / copper["a_mm2"] if copper["a_mm2"] else 0.0
    if overlap < MIN_OVERLAP:
        warnings.append(f"Only {overlap:.0%} of A's copper is also copper in B: the copper changed too "
                        "much to say where the drop changed; compare the loads instead.")
        best_up = best_down = None

    def point(best):
        return None if best is None else {"delta_mv": best[0], "layer": best[1], "x_mm": best[2],
                                          "y_mm": best[3]}
    doc = {"most_increased": point(best_up), "most_decreased": point(best_down),
           "copper": copper, "overlap_share": overlap,
           "layers_only_in_a": [n for n in by_a if n not in by_b],
           "layers_only_in_b": [n for n in by_b if n not in by_a]}
    return doc, deltas, warnings


def _outside(a: LayerField, b: LayerField) -> float:
    """The area of ``a``'s copper whose triangles' centres are not on ``b``'s copper, mm²."""
    if len(a.tris) == 0:
        return 0.0
    if len(b.tris) == 0:
        return float(a.tri_area.sum())
    k = np.asarray(TriangleLocator(b.points, b.tris).locate_many(a.centroids()))
    return float(a.tri_area[k < 0].sum())


def _far(da: Mapping[str, Any], db: Mapping[str, Any], mm: float) -> bool:
    pa, pb = (da.get("x_mm"), da.get("y_mm")), (db.get("x_mm"), db.get("y_mm"))
    if None in pa or None in pb:
        return False
    return math.dist(pa, pb) > mm


def _via(v) -> Dict[str, Any]:
    return {"x_mm": float(v.position[0]), "y_mm": float(v.position[1]), "drill_mm": v.drill_mm,
            "current_a": v.current}


def _sources(volts: Mapping[str, float]) -> str:
    return ", ".join(f"{r} {v:g} V" for r, v in volts.items()) or "none"


def _delta(b, a):
    return None if a is None or b is None else b - a
