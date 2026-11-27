"""Where and why a rail fails: a ranked list of problems, each with a place on the board.

Every load, current-crowding spot and via is *ranked*; a problem is a ``fail`` only when the
user set the matching limit in the rail's :class:`~coppernicus.project.Limits` and the
result exceeds it. Verdicts are only against the user's limits.

One documented default (docs/technical.md#current-density-default): without a
``max_current_density``, a current-crowding spot whose mean |J| is above
:func:`density_default` for its layer is an ``attention`` item. It is shown between the fails
and the ranked items and never changes a verdict. A spot of fewer than
:data:`MIN_SPOT_ELEMENTS` mesh elements is below the mesh's resolution and is not one. Each
spot is listed once: a ranked spot on the copper of an attention or failed spot is not listed
again.

Current crowding is reported per *spot*: connected triangles in the top share of current
density on a layer, summarised by their area-weighted mean |J|. A point maximum is not
used because at a sharp inner corner the 2D current density is singular, so its value
depends on the mesh. A degenerate triangle (an edge shorter than 1 nm, the resolution of
KiPI's geometry snap) is in no spot (:func:`_spots`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from coppernicus.project import Limits
from coppernicus.results import DISCONNECTED, NOT_CONVERGED, LayerField, RailResult, TreeResult

FAIL, ATTENTION, INFO = "fail", "attention", "info"
SEVERITY_RANK = {FAIL: 0, ATTENTION: 1, INFO: 2}     # the order problems are listed in
HOTSPOT_TOP_SHARE = 0.01      # without a limit: the top 1 % of a layer's copper area by |J|
HOTSPOT_GROW = 0.8            # ... grown through neighbours above 80 % of that threshold
VIA_NOISE_SHARE = 1e-6        # vias below this share of the rail current carry nothing (round-off)
DENSITY_NOISE_SHARE = 1e-6    # copper below this share of the rail's peak |J| carries nothing (round-off)

# The current-density default (docs/technical.md#current-density-default): A/mm² at 1 oz
# (35 µm), outer and inner layers apart so they can diverge; scaled ×√(35 µm / t).
DENSITY_DEFAULT_EXTERNAL_A_MM2 = 60.0
DENSITY_DEFAULT_INTERNAL_A_MM2 = 60.0
DENSITY_DEFAULT_REF_MM = 0.035
DENSITY_DEFAULT_SOURCE = "docs/technical.md#current-density-default"
# A numerical-resolution rule of the default, not a physical limit: a spot of fewer mesh
# elements than this is not an attention item (docs/technical.md#current-density-default:
# 1-4 element slivers at the polygon vertices of via walls, a few % above the default, that
# do not converge when the mesh is refined). A user's max_current_density judges every spot.
MIN_SPOT_ELEMENTS = 3
# What a failed spot smaller than that says: it still fails (the user's limit), and how to check it.
SMALL_SPOT_CHECK = ("A spot this small may be a mesh artefact at a via wall: to check it, solve again "
                    "with a smaller min_edge_mm and grading (the mesh near plated holes); if it stays, "
                    "widen the copper there or add a via.")


#: The assumption line of a result whose current crowding a user's max_current_density judges
#: (:func:`density_mesh_note`): never with the default alone (attention items only).
DENSITY_MESH_NOTE = ("Current crowding judged against max_current_density: a crowding spot's mean "
                     "|J| depends on the mesh, with either mesher: typically 1-2 % on detailed "
                     "boards, up to about 6 %. On a spot of only a few elements at a sharp copper "
                     "corner neither mesher converges, and the two can differ by more (15 % seen on "
                     "a real board). Read a spot within a few percent of the limit, or one of only "
                     "a few elements, with care.")


def density_mesh_note(limits: Iterable[Optional[Limits]]) -> Optional[str]:
    """:data:`DENSITY_MESH_NOTE` when one of ``limits`` (a rail's, or a tree's by net) sets a
    max_current_density, else None."""
    return DENSITY_MESH_NOTE if any(x is not None and x.max_current_density is not None
                                    for x in limits) else None


def density_default(thickness_mm: float, outer: bool) -> float:
    """Coppernicus's current-density default for copper ``thickness_mm`` thick, A/mm²: the
    constant for outer or inner layers at 1 oz, × √(35 µm / t) (TI SLUA366: the current ×√2
    when the thickness doubles)."""
    return _base(outer) * float(np.sqrt(DENSITY_DEFAULT_REF_MM / thickness_mm))


def _base(outer: bool) -> float:
    """The default at 1 oz: the outer or the inner layers' constant."""
    return DENSITY_DEFAULT_EXTERNAL_A_MM2 if outer else DENSITY_DEFAULT_INTERNAL_A_MM2


def _um(mm: float) -> str:
    return f"{mm * 1e3:.3g} µm"


def density_default_note(layers) -> str:
    """The assumption line of every rail and power-tree result: the default, its scaling and
    each layer's value. ``layers``: (name, thickness_mm, outer) in stack order."""
    ext, inn = DENSITY_DEFAULT_EXTERNAL_A_MM2, DENSITY_DEFAULT_INTERNAL_A_MM2
    base = (f"{ext:g} A/mm²" if ext == inn else
            f"{ext:g} A/mm² on outer layers and {inn:g} A/mm² on inner layers")
    each = ", ".join(f"{name} {density_default(t, outer):.3g}" for name, t, outer in layers)
    return (f"Current-density default (Coppernicus, attention only, not a verdict): {base} at "
            f"1 oz, scaled ×√({_um(DENSITY_DEFAULT_REF_MM)} / t): {each} A/mm²; set "
            "max_current_density to judge it (it then replaces this default). Sources: "
            f"{DENSITY_DEFAULT_SOURCE}.")


@dataclass
class Problem:
    severity: str                 # FAIL, ATTENTION (the current-density default) or INFO
    kind: str                     # "load", "hotspot", "via"; in a tree also "rise", "return", "regulator"
    title: str
    detail: str
    value: float                  # the ranking: drop % (|mV| at 0 V nominal), mean A/mm^2, A or mV, by kind
    x: float
    y: float
    layer: Optional[str] = None
    # The numbers behind the title, keyed with their units (``drop_mv``, ``mean_j_a_mm2``,
    # ``current_a`` ...), for reports and the agent interface.
    data: Dict[str, Any] = field(default_factory=dict)


class Problems(list):
    """The problems of a rail or a tree group, as :func:`find_problems` and
    :func:`find_tree_problems` return them: a plain list that also keeps what judged it.
    ``limits`` are the limits its copper was judged against (a rail's own, a tree group's
    strictest; none for a group that did not converge) and ``default`` whether Coppernicus's
    current-density default applied where no ``max_current_density`` is set. The |J| maps
    read them for the label above their colour scale (:func:`density_check`). A copy
    (``list(p)``, a slice, ``sorted``) is a plain list without them."""

    def __init__(self, items: Iterable[Problem] = (), limits: Optional[Limits] = None,
                 default: bool = True):
        super().__init__(items)
        self.limits = limits if limits is not None else Limits()
        self.default = default


def find_problems(result: RailResult, limits: Optional[Limits] = None,
                  top: int = 5) -> Problems:
    """Every load, then at most ``top`` current-crowding spots and ``top`` vias (more when
    they exceed a limit), and every spot above the current-density default (attention), each
    spot once (:func:`_hotspots`). Fails first, then attention items, then the ranked ones."""
    limits = limits or Limits()
    rise = _rise(result, limits) if not result.v_ref else []    # a ground-rise limit: 0 V nets only
    out = rise + _loads(result, limits) + _hotspots(result, limits, top) + _vias(result, limits, top)
    order = {"rise": 0, "load": 0, "hotspot": 1, "via": 2}
    return Problems(sorted(out, key=lambda p: (SEVERITY_RANK[p.severity], order[p.kind], -p.value)),
                    limits, True)


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
    near the threshold is one spot, not many). Returns (indices, mean J, max J, area).

    Degenerate triangles (an edge shorter than the input's 1 nm resolution,
    :meth:`~coppernicus.results.LayerField.resolved`) are neither seeds nor members: one alone
    would be a spot of its own, and on a spot it would raise the peak and count toward
    :data:`MIN_SPOT_ELEMENTS`. Its area (~1e-14 mm²) would not move the mean anyway."""
    j_all = layer.current_density
    cand = np.flatnonzero((j_all >= grow * threshold) & layer.resolved())
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


def _hotspots(result: RailResult, limits: Limits, top: int, source: str = "",
              default: bool = True) -> List[Problem]:
    """Current-crowding spots: against the user's ``max_current_density`` when set (fails),
    else the top ones ranked, plus every spot above the current-density default (attention,
    measured as a limit is: mean |J| over the spot; unless ``default`` is False).

    Each spot is listed once: a ranked spot that shares copper (one triangle is enough) with
    a failed or attention spot on its layer is not listed again. The ``top`` ranked spots
    are counted after that, so the attention items come on top of them, not out of them."""
    fails, attention, ranked = [], [], []
    peak = max((float(lay.current_density[lay.resolved()].max(initial=0.0)) for lay in result.layers),
               default=0.0)
    for layer in result.layers:
        if layer.current_density.size == 0 or not np.any(layer.current_density > DENSITY_NOISE_SHARE * peak):
            continue
        listed = np.zeros(layer.current_density.shape, dtype=bool)   # copper of the spots above
        if limits.max_current_density is not None:
            threshold, grow = limits.max_current_density, 1.0
            why = f"above the {limits.max_current_density:g} A/mm² limit"
        else:
            threshold, grow = _top_share_threshold(layer, HOTSPOT_TOP_SHARE), HOTSPOT_GROW
            why = f"top {HOTSPOT_TOP_SHARE:.0%} of this layer's copper by current density"
            if default and layer.thickness_mm:
                for problem, idx in _default_spots(layer):
                    attention.append(problem)
                    listed[idx] = True
        cents = layer.centroids()
        found = []
        for idx, mean_j, max_j, area in _spots(layer, threshold, grow):
            w = layer.tri_area[idx]
            x, y = (cents[idx] * w[:, None]).sum(axis=0) / w.sum()
            fail = limits.max_current_density is not None and mean_j > limits.max_current_density
            check = (f" {SMALL_SPOT_CHECK}" if fail and len(idx) < MIN_SPOT_ELEMENTS else "")
            found.append((Problem(
                FAIL if fail else INFO, "hotspot",
                _against(f"Current crowding on {layer.name}: {mean_j:.1f} A/mm² mean over {area:.3g} mm²",
                         fail, limits.max_current_density, "A/mm²", source),
                f"{_elements(idx)}; peak {max_j:.1f} A/mm² (mesh-dependent at sharp corners); {why}.{check}",
                mean_j, float(x), float(y), layer.name,
                data={"mean_j_a_mm2": mean_j, "peak_j_a_mm2": max_j, "area_mm2": area,
                      "elements": len(idx)}), idx))
        for problem, idx in found:
            if problem.severity == FAIL:
                fails.append(problem)
                listed[idx] = True
        ranked += [problem for problem, idx in found
                   if problem.severity != FAIL and not listed[idx].any()]
    for group in (fails, attention, ranked):
        group.sort(key=lambda p: -p.value)
    return fails + attention + ranked[:max(0, top - len(fails))]


def _elements(idx) -> str:
    return f"{len(idx)} mesh element{'s' if len(idx) != 1 else ''}"


def _default_spots(layer: LayerField) -> List[Tuple[Problem, np.ndarray]]:
    """The spots of ``layer`` whose mean |J| is above the current-density default for its
    thickness, found and measured as against a ``max_current_density``: attention items,
    each with the indices of its triangles. A spot of fewer than :data:`MIN_SPOT_ELEMENTS`
    elements is not one (nor is its copper taken as listed)."""
    t, outer = float(layer.thickness_mm), layer.outer is not False
    limit = density_default(t, outer)
    cents = layer.centroids()
    out = []
    for idx, mean_j, max_j, area in _spots(layer, limit, 1.0):
        if mean_j <= limit or len(idx) < MIN_SPOT_ELEMENTS:
            continue
        w = layer.tri_area[idx]
        x, y = (cents[idx] * w[:, None]).sum(axis=0) / w.sum()
        out.append((Problem(
            ATTENTION, "hotspot",
            f"Current crowding on {layer.name}: {mean_j:.1f} A/mm² mean over {area:.3g} mm², above "
            f"the {limit:.3g} A/mm² default for {_um(t)} copper",
            f"{_elements(idx)}; peak {max_j:.1f} A/mm² (mesh-dependent at sharp corners). Attention "
            f"only, not a verdict: Coppernicus's current-density default, {_base(outer):g} A/mm² at "
            f"1 oz scaled ×√({_um(DENSITY_DEFAULT_REF_MM)} / t) to {limit:.3g} A/mm² at {_um(t)} "
            f"({DENSITY_DEFAULT_SOURCE}); set max_current_density to judge it.",
            mean_j, float(x), float(y), layer.name,
            data={"mean_j_a_mm2": mean_j, "peak_j_a_mm2": max_j, "area_mm2": area,
                  "elements": len(idx), "default_j_a_mm2": limit, "thickness_mm": t}), idx))
    return out


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


# ---------------------------------------------------------------------- the |J| maps' label

PEAK_NOTE = "mesh-dependent"
PEAK_TIP = ("The point maximum |J| on this layer, degenerate triangles left out. At a sharp corner or "
            "a hole edge it depends on the mesh, so it is never judged: the verdict uses the spot "
            "means, as the problem list does.")


def _jtext(value: float) -> str:
    """A |J| in A/mm² for the label: 3 significant figures, never an exponent ("220", "2500")."""
    return f"{value:.3g}" if abs(value) < 999.5 else f"{value:.0f}"


@dataclass
class DensityCheck:
    """What every |J| map writes above its colour scale for one layer (the HTML report, the
    PNG maps, the app): :meth:`lines`. Line 1, the layer's peak |J|: the point maximum with
    degenerate triangles left out, mesh-dependent, never judged. Line 2, when a limit applies,
    the verdict of the current density on the layer as the problem list judged it (the spot
    means, :func:`_hotspots`), read off the list, never judged again: against the user's
    ``max_current_density``, or without one against Coppernicus's attention default for the
    layer's thickness. Made by :func:`density_check`."""
    layer: str
    peak_j_a_mm2: float                     # 0 on a layer with no current
    limit_j_a_mm2: Optional[float] = None
    limit_kind: Optional[str] = None        # "limit" (the user's), "default", or None: none applies
    # "fail" / "pass" against a limit, "attention" / "below" against the default, "not_judged"
    # (a tree group that did not converge), None when no limit applies.
    verdict: Optional[str] = None
    spots: int = 0                          # the failed (limit) or attention (default) spots on the layer
    worst: Optional[Dict[str, Any]] = None  # the strongest of them: mean_j_a_mm2, area_mm2, ...
    peak_above: bool = False                # the peak is above the limit (no spot above: a corner)
    small_only: bool = False                # every failed spot is under MIN_SPOT_ELEMENTS elements

    def peak_text(self) -> str:
        """"Peak 220 A/mm²"."""
        return f"Peak {_jtext(self.peak_j_a_mm2)} A/mm²"

    def verdict_text(self) -> str:
        """Line 2: "limit 60 A/mm²: FAIL · worst spot 89.1 A/mm² over 3.02 mm²", "limit 60
        A/mm²: pass", "default 60 A/mm² (attention): 3 spots above · worst ..." or "default 60
        A/mm² (attention): below"; empty when no limit applies."""
        if self.limit_kind is None:
            return ""
        worst = self.worst or {}
        spot = (f"{worst.get('mean_j_a_mm2', 0.0):.1f} A/mm² over "
                f"{worst['area_mm2']:.3g} mm²" if worst.get("area_mm2") is not None else "")
        if self.limit_kind == "limit":
            head = f"limit {self.limit_j_a_mm2:g} A/mm²"
            if self.verdict == "not_judged":
                return f"{head}: not judged (did not converge)"
            if self.verdict == "fail":
                text = f"{head}: FAIL · worst spot {spot}"
                if self.spots > 1:
                    text += f" · {self.spots} spots above"
                if self.small_only:
                    text += f" · under {MIN_SPOT_ELEMENTS} mesh elements: may be a mesh artefact"
                return text
            corner = " · peak above the limit at a corner; spots pass" if self.peak_above else ""
            return f"{head}: pass{corner}"
        head = f"default {self.limit_j_a_mm2:.3g} A/mm² (attention)"
        if self.verdict == "attention":
            return f"{head}: {self.spots} spot{'s' if self.spots != 1 else ''} above · worst {spot}"
        corner = " · peak above the default at a corner; spots below" if self.peak_above else ""
        return f"{head}: below{corner}"

    def lines(self) -> List[str]:
        """The label: the peak, marked mesh-dependent, and the verdict when a limit applies."""
        return [f"{self.peak_text()} ({PEAK_NOTE})"] + ([self.verdict_text()] if self.limit_kind else [])

    def as_dict(self) -> Dict[str, Any]:
        """As plain data, keyed with units (the HTML report's ``j_check`` of each layer)."""
        return {"peak_j_a_mm2": self.peak_j_a_mm2, "peak_text": self.peak_text(), "peak_note": PEAK_NOTE,
                "peak_tip": PEAK_TIP, "limit_j_a_mm2": self.limit_j_a_mm2, "limit_kind": self.limit_kind,
                "verdict": self.verdict, "verdict_text": self.verdict_text(), "spots_above": self.spots,
                "worst_spot": self.worst, "peak_above_limit": self.peak_above, "lines": self.lines()}


def density_check(result: RailResult, layer: LayerField, problems: Optional[List[Problem]] = None,
                  limits: Optional[Limits] = None) -> DensityCheck:
    """The label above ``layer``'s |J| colour scale (:class:`DensityCheck`). The verdict is
    read off ``problems``, judged against what judged them: a :class:`Problems` list keeps it;
    a plain list is taken as judged by ``limits``; with no list, ``limits`` judge the rail
    here (:func:`find_problems`). With neither, only the peak is known. A tree group that did
    not converge is not judged: with a ``max_current_density`` (in ``limits`` or the list's)
    line 2 says so."""
    j = layer.current_density
    kept = j[np.isfinite(j) & layer.resolved()] if j.size else j
    out = DensityCheck(layer.name, float(kept.max()) if kept.size else 0.0)
    if problems is None and limits is not None:
        problems = find_problems(result, limits)
    judged, default = getattr(problems, "limits", None), getattr(problems, "default", True)
    if judged is None:
        if problems is None or limits is None:          # nothing says what judged the list
            return out
        judged = limits
    user = judged.max_current_density
    if not getattr(result, "converged", True):
        named = user if user is not None else (limits.max_current_density if limits else None)
        if named is not None:
            out.limit_j_a_mm2, out.limit_kind, out.verdict = float(named), "limit", "not_judged"
            return out
    mine = [p for p in problems if p.kind == "hotspot" and p.layer == layer.name]
    if user is not None:
        above = [p for p in mine if p.severity == FAIL]
        out.limit_j_a_mm2, out.limit_kind, out.verdict = float(user), "limit", "fail" if above else "pass"
    elif default and layer.thickness_mm:
        above = [p for p in mine if p.severity == ATTENTION]
        out.limit_j_a_mm2 = density_default(float(layer.thickness_mm), layer.outer is not False)
        out.limit_kind, out.verdict = "default", "attention" if above else "below"
    else:
        return out
    out.spots = len(above)
    out.peak_above = out.peak_j_a_mm2 > out.limit_j_a_mm2
    if above:
        w = max(above, key=lambda p: p.value)
        out.worst = {"mean_j_a_mm2": w.data.get("mean_j_a_mm2", w.value), "area_mm2": w.data.get("area_mm2"),
                     "peak_j_a_mm2": w.data.get("peak_j_a_mm2"), "elements": w.data.get("elements"),
                     "x_mm": w.x, "y_mm": w.y}
        out.small_only = out.verdict == "fail" and all(
            (p.data.get("elements") or MIN_SPOT_ELEMENTS) < MIN_SPOT_ELEMENTS for p in above)
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
                       top: int = 5) -> Problems:
    """The problems of one group of a power-tree solve, fails first. On a rail: its loads
    and the inputs of the regulators it feeds, ranked by the drop across their supply (input)
    and ground pins. On a ground: its return pins, ranked by their rise from 0 V (no drop
    limit applies to a ground). The regulators driving it that have a rated output current,
    against it. Then current-crowding spots and vias, as on a single rail.

    Limits per net (:data:`coppernicus.power_tree.LIMITS_RULE`): each pin by its net's,
    the copper by the strictest of the group's. ``limits``, if given, stands for the limits
    set on the group's driving net (its key) in this call. A group whose iteration did not
    converge is not judged. Without a ``max_current_density`` on any of its nets, its spots
    above the current-density default are attention items (a non-converged group's too)."""
    from coppernicus.power_tree import group_limits, net_limits
    if limits is not None:
        tree = replace(tree, limits={**tree.limits, group: limits})
    res = tree.groups[group]
    default = group_limits(tree, group)[0].max_current_density is None   # the user's replaces it
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
    out = (first + _ratings(tree, group, res)
           + _hotspots(res, glim, top, source("max_current_density"), default)
           + _vias(res, glim, top, source("max_via_current")))
    order = {"rise": 0, "load": 0, "return": 0, "regulator": 0, "hotspot": 1, "via": 2}
    return Problems(sorted(out, key=lambda p: (SEVERITY_RANK[p.severity], order[p.kind], -p.value)),
                    glim, default)


def _limit_text(value: Optional[float], unit: str, origin: str, own: str) -> str:
    """"limit 1 %", "limit 1 % (from +3V3)" when it comes from another net, or "no limit set"."""
    if value is None:
        return "no limit set"
    return f"limit {value:g} {unit}" + (f" (from {origin})" if origin and origin != own else "")


def _limit_data(value: Optional[float], key: str, origin: str) -> Dict[str, Any]:
    """The limit that judged a pin, as data: ``key`` (``limit_pct``, ``limit_mv``) its value
    and ``limit_from`` the net it came from, both None when no limit is set."""
    return {key: value, "limit_from": (origin or None) if value is not None else None}


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
        on = f" on {ld.net}" if ld.net and ld.net != group else ""     # past a net tie
        out.append(Problem(
            FAIL if ld.passed is False else INFO, "load", title,
            f"{ld.current_a:g} A{on}, supply {ld.v_supply_v:.4f} V, {ground}; {limit}.{status}",
            value, float(x), float(y),
            data={"name": ld.name, "drop_mv": ld.drop_v * 1e3, "drop_pct": ld.drop_pct,
                  "v_across_v": ld.v_across_v, "v_worst_v": ld.v_worst_v,
                  "current_a": ld.current_a, "status": ld.status,
                  **_limit_data(lim.max_drop_pct, "limit_pct", org.get("max_drop_pct", ""))}))
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
                  "v_worst_v": v.v_in_worst_v, "current_a": v.i_in_a, "status": v.input_status,
                  **_limit_data(lim.max_drop_pct, "limit_pct", org.get("max_drop_pct", ""))}))
    return out


def _ratings(tree: TreeResult, group: str, res: RailResult) -> List[Problem]:
    """The regulators driving this rail that have a rated output current, against it
    (:func:`coppernicus.power_tree.judge_ratings`), at their output pins. One without a rating
    is not listed: no limit was set."""
    from coppernicus.power_tree import RATING_KEY, rated_from, rating_title, rating_why_not
    terms = {t.name: t for t in res.sources}
    out = []
    for v in (v for v in tree.vrms if v.group == group and v.current_limit_a is not None):
        t = terms.get(v.name)
        x, y = t.positions[0] if t is not None and t.positions else (np.nan, np.nan)
        used = 100.0 * abs(v.i_out_a) / v.current_limit_a
        value = used if np.isfinite(used) else float("inf")
        why = "" if v.current_passed is not None else f" Not judged: {rating_why_not(tree, v)}."
        out.append(Problem(
            FAIL if v.current_passed is False else INFO, "regulator", rating_title(v),
            f"Output of {v.kind} {v.name} on {group}: {used:.1f} % of its rated output current.{why}",
            value, float(x), float(y),
            data={"name": v.name, "i_out_a": v.i_out_a, RATING_KEY: v.current_limit_a,
                  "current_limit_from": rated_from(v), "rating_used_pct": used}))
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
            data={"name": t.name, "ref": t.ref, "rise_mv": rise * 1e3, "current_a": t.current,
                  **_limit_data(limit, "limit_mv", org.get("max_ground_rise_mv", ""))}))
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
