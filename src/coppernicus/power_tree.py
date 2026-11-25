"""Power-tree helpers that need no engine: plain project and result logic.

* :func:`estimated_draws`: what a rail solved on its own (not as part of a tree) draws for
  the regulators and links it feeds, estimated at nominal voltages without copper losses,
  as KiPI's ``_estimated_input`` and ``link_bias`` estimate it. The power-tree solve computes
  the same currents exactly.
* :func:`apply_limits`, :func:`net_limits`, :func:`group_limits`, :func:`tree_verdicts`:
  which of the user's limits judge what in a power tree, and the verdicts (see
  :data:`LIMITS_RULE`).
* :func:`tree_summary`: a power-tree result as a plain, JSON-ready dict (units in every
  key), for the CLI and the agent interface.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coppernicus.project import Limits, PinSpec, Project, Source
from coppernicus.results import DISCONNECTED, NOT_CONVERGED, OUT_OF_REGULATION

SWITCHING = ("smps",)
REGULATING = ("ldo", "smps")


class EstimateError(ValueError):
    """The draw cannot be estimated (a switcher without its efficiency, no source on its
    input rail): the message names the part."""


@dataclass
class Draw:
    """A current the rail delivers to a regulator's input or through a link, estimated."""
    name: str                      # "U1 (input, estimated)" / "FB1 (link, estimated)"
    ref: str
    pins: PinSpec
    net: str
    current_a: float
    what: str                      # for the assumption line


class _Tree:
    """The project's power tree at nominal voltages: nets joined by links, their sources,
    loads and regulators."""

    def __init__(self, project: Project, ties: Sequence[Tuple[str, str]] = ()):
        self.project = project
        self.ties = list(ties)            # nets joined by net ties: one net, at nominal voltages
        rails = [r for r in project.rails if r.sources or r.loads]
        self.loads = {r.net: sum(ld.current for ld in r.loads) for r in rails}
        self.sources = {r.net: r.sources[0] for r in rails if r.sources}
        self.rail_of = {id(s): r.net for r in rails for s in r.sources}
        self.regulators = [s for r in rails for s in r.sources if s.kind in REGULATING and s.input_net]

    def reach(self, start: str, skip_link=None, stop: Sequence[str] = ()) -> Set[str]:
        """``start`` and the nets joined to it by links (other than ``skip_link``), not
        crossing the nets in ``stop``."""
        seen, todo = {start}, [start]
        while todo:
            n = todo.pop()
            pairs = [(k.net_a, k.net_b) for k in self.project.links if k is not skip_link]
            for x, y in pairs + self.ties:
                for a, b in ((x, y), (y, x)):
                    if a == n and b not in seen and b not in stop:
                        seen.add(b)
                        todo.append(b)
        return seen

    def drawn(self, nets: Set[str], seen: frozenset) -> float:
        """What a group of nets draws: its loads and the input of every regulator on it."""
        amps = sum(self.loads.get(n, 0.0) for n in nets)
        return amps + sum(self.input_a(v, seen) for v in self.regulators if v.input_net in nets)

    def driver_voltage(self, nets: Set[str]) -> Optional[float]:
        return next((self.sources[n].voltage for n in sorted(nets) if n in self.sources), None)

    def input_a(self, v: Source, seen: frozenset = frozenset()) -> float:
        """A regulator's input current at nominal voltages (LDO: I_out + I_q; switcher:
        |V_out|·I_out / (η·|V_in|) + I_q, V_in the voltage of the source on its input)."""
        if id(v) in seen:                  # a loop in the tree: KiPI's checks report it
            return 0.0
        i_out = self.drawn(self.reach(self.rail_of[id(v)]), seen | {id(v)})
        if v.kind not in SWITCHING:
            return i_out + v.quiescent_a
        if v.efficiency is None:
            raise EstimateError(f"Source {v.label}: a switcher needs its efficiency (a fraction "
                                "0–1) to estimate its input current; there is no default")
        v_in = self.driver_voltage(self.reach(v.input_net))
        if not v_in:
            raise EstimateError(f"Source {v.label}: no source drives its input net {v.input_net}, "
                                "so its input current cannot be estimated")
        return abs(v.voltage) * i_out / (v.efficiency * abs(v_in)) + v.quiescent_a


def estimated_draws(project: Project, nets: Sequence[str],
                    ties: Sequence[Tuple[str, str]] = ()) -> List[Draw]:
    """The regulator inputs and links that a rail solved on its own, on ``nets`` (its net
    and the nets tied to it), feeds, each with the current estimated at nominal voltages.
    ``ties``: the board's net-tie pairs, which join nets as links do. A link to nets that
    another source drives carries an unknown current and is left out; links in parallel to
    the same nets share the current in proportion to their conductance."""
    tree, nets = _Tree(project, ties), list(nets)
    out = []
    for v in tree.regulators:
        if v.input_net in nets:
            amps = tree.input_a(v)                 # raises for a switcher without η
            kind = "switcher" if v.kind in SWITCHING else "LDO"
            eff = f"η {v.efficiency:g}, " if v.kind in SWITCHING else ""
            out.append(Draw(f"{v.label} (input, estimated)", v.input_ref or v.ref, v.input_pins,
                            v.input_net, amps, f"{v.label} input ({kind}, {eff}I_q {v.quiescent_a:g} A)"))
    crossing = []                          # (link, near pins, near net, far side)
    for k in project.links:
        for near, pins, far in ((k.net_a, k.pins_a, k.net_b), (k.net_b, k.pins_b, k.net_a)):
            if near in nets and far not in nets:
                crossing.append((k, pins, near, frozenset(tree.reach(far, stop=nets))))
    for side in dict.fromkeys(c[3] for c in crossing):
        if any(n in tree.sources for n in side):
            continue                       # both sides driven: the current is not known here
        links = [c for c in crossing if c[3] == side]
        amps = tree.drawn(set(side), frozenset())
        shorts = [c for c in links if c[0].resistance_ohm <= 0]
        weights = ([1.0 if c in shorts else 0.0 for c in links] if shorts
                   else [1.0 / c[0].resistance_ohm for c in links])
        for (k, pins, near, _), w in zip(links, weights):
            far = k.net_b if near == k.net_a else k.net_a
            out.append(Draw(f"{k.name} (link, estimated)", k.ref, pins, near,
                            amps * w / sum(weights), f"{k.name} (link to {far})"))
    return out


def estimate_note(draws: Sequence[Draw]) -> List[str]:
    """The assumption line for the estimated draws of a rail solved on its own."""
    if not draws:
        return []
    items = "; ".join(f"{d.what}: {d.current_a:.4g} A" for d in draws)
    return ["Currents this rail delivers to regulators and links are estimated at nominal "
            f"voltages, without copper losses; the power-tree solve computes it exactly: {items}."]


# ---------------------------------------------------------------------- limits

LIMIT_FIELDS = ("max_drop_pct", "max_current_density", "max_via_current", "max_ground_rise_mv")
UNJUDGED = (DISCONNECTED, NOT_CONVERGED, OUT_OF_REGULATION)

LIMITS_RULE = (
    "Limits are looked up per net: a load by its supply net (the net its pins are on; a limit "
    "not set there comes first from the rail it is listed in, when a net tie puts its pins on "
    "another net), a regulator's input by its input "
    "net (judged as a load on the rail feeding it: nominal − (lowest input pin − highest "
    "ground pin)), a return pin by its ground net. A limit not set on a net comes, field by "
    "field, from its group's driving net (a rail's: the net its source drives; a ground's: the "
    "first ground net the project names). The copper of a group (largest ground rise, current "
    "crowding, vias) is judged against the strictest limit set on any of its nets, as the "
    "solve does not keep which net each piece of copper belongs to. A link is not judged as a "
    "point of its own: it joins its nets into one group, whose loads and regulator inputs are "
    "judged.")


def net_limits(tree, net: str, group: Optional[str] = None,
               rail: Optional[str] = None) -> Tuple[Limits, Dict[str, str]]:
    """The limits that judge what is on ``net``: field by field its own (``tree.limits``),
    else those of ``rail``, the rail a load is listed in (another net when a net tie put its
    pins beyond it), else those of its group's driving net (the group's key). Also returns,
    per field set, the net the value came from."""
    key = group or tree.group_of(net) or net
    vals, origin = {}, {}
    for f in LIMIT_FIELDS:
        for src in dict.fromkeys(n for n in (net, rail, key) if n):
            v = getattr(tree.limits.get(src), f, None)
            if v is not None:
                vals[f], origin[f] = v, src
                break
    return Limits(**vals), origin


def group_limits(tree, key: str) -> Tuple[Limits, Dict[str, str]]:
    """The limits that judge the copper of group ``key``: per field the strictest (lowest)
    of its nets' (:func:`net_limits`), with the net it came from."""
    vals, origin = {}, {}
    for net in dict.fromkeys([key] + list(tree.groups[key].nets)):
        lim, org = net_limits(tree, net, key)
        for f in LIMIT_FIELDS:
            v = getattr(lim, f)
            if v is not None and (f not in vals or v < vals[f]):
                vals[f], origin[f] = v, org[f]
    return Limits(**vals), origin


def apply_limits(tree, project: Project) -> None:
    """Read the project's current limits into ``tree.limits`` (every solved net the project
    has a rail for, and every net with a limit set that the tree did not solve: that limit
    is not judged) and judge the loads and regulator inputs again; no solve needed. The
    board temperature's limits too (:func:`coppernicus.temperature.apply_thermal_limits`)."""
    from coppernicus.temperature import apply_thermal_limits
    tree.limits = {}
    for rail in project.rails:
        if tree.group_of(rail.net) is not None or any(v is not None for v in vars(rail.limits).values()):
            tree.limits[rail.net] = copy.copy(rail.limits)
    judge_loads(tree)
    apply_thermal_limits(tree, getattr(project, "thermal", None))   # the board temperature's


def unjudged(item: str, net: str, limit: str, reason: str) -> Dict[str, str]:
    """A not-judged entry: what (a load, a regulator input ``"U2 (input)"``, a return pin, or
    the net itself), on which net, which limit (keyed with its unit) and why."""
    return {"item": item, "net": net, "limit": limit, "reason": reason}


def not_judged_text(items: Sequence[Dict[str, str]]) -> str:
    """The entries as one line: "U3 on +1V8 (max_drop_pct): VRM OUT OF REGULATION; …"."""
    return "; ".join((u["item"] if u["item"] == u.get("net", u["item"]) or not u.get("net")
                      else f"{u['item']} on {u['net']}") + f" ({u['limit']}): {u['reason']}" for u in items)


def unsolved_limits(tree) -> List[Dict[str, str]]:
    """Each limit set on a net the power tree did not solve (excluded, or not set up), as a
    not-judged entry."""
    from coppernicus.problems import LIMIT_KEYS
    return [unjudged(net, net, key, "not solved in the power tree")
            for net, lim in tree.limits.items() if tree.group_of(net) is None
            for key, attr in LIMIT_KEYS.items() if getattr(lim, attr) is not None]


def judge_loads(tree) -> None:
    """Set each load's and each regulator input's verdict against the drop limit of its net
    (:func:`net_limits`), only the user's: ``None`` without a limit, or for one that has no
    physical voltage to judge (not solved, not converged, or behind a regulator that cannot
    hold its voltage: the engine still holds that output at its set point)."""
    for ld in tree.loads:
        limit = net_limits(tree, ld.net or ld.group, ld.group, ld.rail)[0].max_drop_pct
        ok = ld.status not in UNJUDGED and math.isfinite(ld.drop_pct)
        ld.passed = None if limit is None or not ok else bool(ld.drop_pct <= limit)
    for v in tree.vrms:
        if not v.input_group:
            continue
        limit = net_limits(tree, v.input_net, v.input_group)[0].max_drop_pct
        ok = (v.input_status not in UNJUDGED and v.input_drop_pct is not None
              and math.isfinite(v.input_drop_pct))
        v.input_passed = None if limit is None or not ok else bool(v.input_drop_pct <= limit)


def _why_not(status: str, nominal: Optional[float]) -> str:
    if status:
        return status
    return "0 V nominal: a % drop limit does not apply" if not nominal else "not solved"


@dataclass
class GroupVerdict:
    """The verdict of one group: ``fail`` when a limit is exceeded; otherwise ``not_judged``
    when a limit set applies to something that could not be judged (or there is nothing for
    it to judge), or when no limit applies; ``pass`` only when everything was judged."""
    verdict: str
    failed: int
    limits: Dict[str, float]               # the strictest of each kind, keyed with units
    origin: Dict[str, str]                 # limit key -> the net it came from
    not_judged: List[Dict[str, str]] = field(default_factory=list)   # see unjudged()
    problems: list = field(default_factory=list)


def group_verdict(tree, key: str, top: int = 5) -> GroupVerdict:
    from coppernicus.problems import FAIL, LIMIT_KEYS, find_tree_problems, inapplicable_limits, limits_dict
    res = tree.groups[key]
    problems = find_tree_problems(tree, key, top=top)
    failed = sum(p.severity == FAIL for p in problems)
    glim, gorigin = group_limits(tree, key)
    checked = limits_dict(glim, res.kind, res.v_ref)
    origin = {k: gorigin[a] for k, a in LIMIT_KEYS.items() if k in checked}
    missing, judged = [], 0
    for net in dict.fromkeys([key] + list(res.nets)):        # a limit of the wrong kind
        missing += [unjudged(u["item"], net, u["limit"], u["reason"])
                    for u in inapplicable_limits(net, tree.limits.get(net), res.v_ref, res.kind)]
    if checked and not res.converged:
        missing += [unjudged(key, key, k, "its iteration did not converge") for k in checked]
    elif checked:
        if "max_drop_pct" in checked:
            items = [(ld.name, ld.net or key, ld.rail, ld.passed, ld.status, ld.nominal_v)
                     for ld in tree.loads if ld.group == key]
            items += [(f"{v.name} (input)", v.input_net, None, v.input_passed, v.input_status,
                       v.input_nominal_v) for v in tree.vrms if v.input_group == key]
            applies = [it for it in items
                       if net_limits(tree, it[1], key, it[2])[0].max_drop_pct is not None]
            for name, net, _rail, passed, status, nominal in applies:
                if passed is None:
                    missing.append(unjudged(name, net, "max_drop_pct", _why_not(status, nominal)))
            judged += sum(it[3] is not None for it in applies)
            if not applies:
                missing.append(unjudged(key, key, "max_drop_pct", "no load or regulator input to judge"))
        if res.kind == "ground" and "max_ground_rise_mv" in checked:
            for t in res.loads:
                if not (math.isfinite(t.v_max) and math.isfinite(t.v_min)):
                    missing.append(unjudged(t.name, t.net or key, "max_ground_rise_mv", "not solved"))
        judged += any(k != "max_drop_pct" for k in checked)       # the copper is judged
    verdict = "fail" if failed else "not_judged" if missing or not judged else "pass"
    return GroupVerdict(verdict, failed, checked, origin, missing, problems)


def has_limits(gv: GroupVerdict) -> bool:
    """Whether the user set a limit on this group: one that applies, or one that does not
    (and is listed as not judged)."""
    return bool(gv.limits or gv.not_judged)


def tree_verdicts(tree, top: int = 5) -> Tuple[str, Dict[str, GroupVerdict]]:
    """Every group's verdict and the overall one: ``fail`` if a group fails; otherwise
    ``not_judged`` if a group with limits could not be judged in full; ``pass`` if something
    was judged; ``not_judged`` when no limit is set. The board temperature
    (:func:`coppernicus.temperature.thermal_verdict`) counts as one more group."""
    from coppernicus.temperature import thermal_verdict
    groups = {key: group_verdict(tree, key, top) for key in tree.groups}
    tv = thermal_verdict(tree)
    if any(v.verdict == "fail" for v in groups.values()) or tv.verdict == "fail":
        overall = "fail"
    elif (any(has_limits(v) and v.verdict == "not_judged" for v in groups.values()) or unsolved_limits(tree)
          or (tv.limits and tv.verdict == "not_judged")):
        overall = "not_judged"
    else:
        overall = ("pass" if any(v.verdict == "pass" for v in groups.values()) or tv.verdict == "pass"
                   else "not_judged")
    return overall, groups


def own_limits(tree) -> Dict[str, Dict[str, float]]:
    """The limits set on each net, keyed with units (what the user typed; on a solved net,
    those that apply to its kind)."""
    from coppernicus.problems import LIMIT_KEYS, limits_dict
    out = {}
    for net, lim in tree.limits.items():
        key = tree.group_of(net)
        d = (limits_dict(lim, tree.groups[key].kind, tree.groups[key].v_ref) if key else
             {k: getattr(lim, a) for k, a in LIMIT_KEYS.items() if getattr(lim, a) is not None})
        if d:
            out[net] = d
    return out


def verdict_note(tree, groups: Dict[str, GroupVerdict]) -> str:
    """One line on what was judged against what, and what was not (the board temperature's
    limits too)."""
    from coppernicus.temperature import ITEM, thermal_verdict
    own = own_limits(tree)
    tv = thermal_verdict(tree)
    missing = [x for v in groups.values() for x in v.not_judged] + unsolved_limits(tree) + tv.not_judged
    if not own and not tv.limits and not missing:
        return "No limits set: nothing is judged, problems are ranked only."
    judged = ([f"{net}: {k} = {v:g}" for net, d in own.items() for k, v in d.items()]
              + [f"{ITEM}: {k} = {v:g}" for k, v in tv.limits.items()])
    note = ("Judged only against the limits you set: " + ", ".join(judged)
            if judged else "No limit you set applies to what it is set on")
    if tv.failed_rows:                  # the board temperature's fails, named (the groups' are problems)
        note += f". Board temperature over a limit: {'; '.join(tv.failed_rows)}"
    if missing:
        note += ". Not judged: " + not_judged_text(missing)
    return note


# ---------------------------------------------------------------------- summary

def _num(x: Optional[float]) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def _clean(obj):
    """``obj`` with every NaN or infinite number (nothing solved there) as None, for JSON."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return _num(obj)
    return obj


def tree_summary(tree, top: int = 5) -> Dict[str, Any]:
    """A power-tree result (``coppernicus.results.TreeResult``) as a JSON-ready dict: units
    in every key, the assumptions and warnings, the verdict (only against the user's limits,
    per group) and the VRM, load, link and group tables."""
    from coppernicus.problems import limits_dict
    from coppernicus.temperature import thermal_summary, thermal_verdict

    overall, verdicts = tree_verdicts(tree, top)
    tv = thermal_verdict(tree)
    groups, failed = [], 0
    for key, res in tree.groups.items():
        gv = verdicts[key]
        failed += gv.failed
        v_ref = res.v_ref
        by_net = {net: limits_dict(net_limits(tree, net, key)[0], res.kind, res.v_ref) for net in res.nets}
        groups.append({
            "group": key, "kind": res.kind, "nets": list(res.nets), "v_ref_v": v_ref,
            ("max_rise_mv" if res.kind == "ground" else "max_drop_mv"): res.max_drop * 1e3,
            "max_drop_pct": (100.0 * res.max_drop / abs(v_ref)) if v_ref and res.kind == "rail" else None,
            "power_loss_w": res.power_loss, "loss_breakdown_w": res.loss_breakdown(),
            "j_point_max_a_mm2": res.j_max,
            "via_current_max_a": max((v.current for v in res.vias), default=0.0),
            "verdict": gv.verdict, "limits": gv.limits, "failed": gv.failed,
            "limits_by_net": {n: d for n, d in by_net.items() if d},
            "not_judged": list(gv.not_judged),
            "problems": [{"severity": p.severity, "kind": p.kind, "title": p.title, **p.data}
                         for p in gv.problems],
        })
    doc = {
        "kind": "power_tree",
        "verdict": overall, "verdict_note": verdict_note(tree, verdicts), "failed": failed + tv.failed,
        "not_judged": [x for v in verdicts.values() for x in v.not_judged] + unsolved_limits(tree)
        + tv.not_judged,
        "vrms": [{"name": v.name, "kind": v.kind, "group": v.group, "v_set_v": v.v_set_v,
                  "v_out_v": v.v_out_v, "i_out_a": v.i_out_a, "v_in_v": _num(v.v_in_v),
                  "i_in_a": _num(v.i_in_a), "p_out_w": v.p_out_w, "p_in_w": _num(v.p_in_w),
                  "p_loss_w": _num(v.p_loss_w), "v_ground_v": _num(v.v_ground_v),
                  "v_sense_v": _num(v.v_sense_v), "v_ground_sense_v": _num(v.v_ground_sense_v),
                  "regulation": v.regulation, "input_net": v.input_net or None,
                  "input_drop_mv": None if _num(v.input_drop_v) is None else v.input_drop_v * 1e3,
                  "input_drop_pct": _num(v.input_drop_pct), "input_status": v.input_status,
                  "input_exceeds_drop_limit": None if v.input_passed is None else not v.input_passed}
                 for v in tree.vrms],
        "loads": [{"name": ld.name, "group": ld.group, "current_a": ld.current_a,
                   "nominal_v": ld.nominal_v, "v_supply_v": _num(ld.v_supply_v),
                   "v_supply_min_v": _num(ld.v_supply_min_v), "v_ground_v": _num(ld.v_ground_v),
                   "v_across_v": _num(ld.v_across_v), "v_worst_v": _num(ld.v_worst_v),
                   "drop_mv": None if _num(ld.drop_v) is None else ld.drop_v * 1e3,
                   "drop_pct": _num(ld.drop_pct), "status": ld.status,
                   "exceeds_drop_limit": None if ld.passed is None else not ld.passed}
                  for ld in tree.loads],
        "links": [dict(k) for k in tree.links],
        "groups": groups,
    }
    if getattr(tree, "thermal", None) is not None:      # the board temperature (Task 8)
        doc["thermal"] = thermal_summary(tree)
    doc.update({"assumptions": list(tree.assumptions), "warnings": list(tree.warnings)})
    return _clean(doc)


def _fmt(x: Optional[float], width: int, spec: str, unit: str, scale: float = 1.0) -> str:
    """``x`` with its unit, right-aligned in ``width`` characters; "—" when there is none."""
    return f"{'—' if x is None else f'{x * scale:{spec}}{unit}':>{width}}"


def _input_text(v: Dict[str, Any]) -> str:
    """A regulator's input judged on the rail feeding it, for its VRM row."""
    if not v.get("input_net"):
        return ""
    exceeds = v.get("input_exceeds_drop_limit")
    verdict = v.get("input_status") or ("FAIL" if exceeds else "pass" if exceeds is False else "not judged")
    drop = ("—" if v.get("input_drop_mv") is None else
            f"{v['input_drop_mv']:.2f} mV ({_fmt(v.get('input_drop_pct'), 0, '.2f', ' %').strip()})")
    return f"  input drop on {v['input_net']} {drop}: {verdict}"


def _group_worst(gr: Dict[str, Any]) -> str:
    """A group's worst copper for its :func:`tree_text` line, worded as
    :func:`coppernicus.problems.max_drop_text`: "max drop 12.345 mV (0.374 %) below 3.3 V",
    "max drop 12.345 mV (0.374 %) above -3.3 V (toward 0 V)", "max drop 1.234 mV below 0 V"
    (a rail at 0 V: no %), "max ground rise 5.678 mV above 0 V" or "max ground shift 5.678 mV
    below 0 V". The side comes from the summary alone, as the tree solves it: a ground is a
    rise, a rail below 0 V draws toward 0 V, any other rail (one at 0 V too) below its
    nominal."""
    from coppernicus.problems import drop_words

    rise = gr["kind"] == "ground"
    mv = gr["max_rise_mv"] if rise else gr["max_drop_mv"]
    if mv is None:
        return f"max {'ground rise' if rise else 'drop'} —"
    v_ref = gr["v_ref_v"] or 0.0
    what, where = drop_words(mv, v_ref, -1.0 if rise or v_ref < 0 else 1.0, rise)
    pct = "" if gr["max_drop_pct"] is None else f" ({gr['max_drop_pct']:.3f} %)"
    return f"max {what} {abs(mv) if rise else mv:.3f} mV{pct} {where}"


def tree_text(doc: Dict[str, Any]) -> List[str]:
    """The lines ``coppernicus solve --tree`` prints, from a :func:`tree_summary` dict."""
    lines = ["VRMs (sources and regulators)",
             f"  {'name':<14} {'kind':<6} {'group':<10} {'V set':>8} {'V out':>9} {'I out':>9} "
             f"{'V in':>9} {'I in':>9} {'P loss':>9}"]
    for v in doc["vrms"]:
        lines.append(f"  {v['name']:<14} {v['kind']:<6} {v['group']:<10} {_fmt(v['v_set_v'], 8, '.4g', 'V')} "
                     f"{_fmt(v['v_out_v'], 9, '.5f', 'V')} {_fmt(v['i_out_a'], 9, '.4f', 'A')} "
                     f"{_fmt(v['v_in_v'], 9, '.5f', 'V')} {_fmt(v['i_in_a'], 9, '.4f', 'A')} "
                     f"{_fmt(v['p_loss_w'], 9, '.1f', 'mW', 1e3)}"
                     + (f"  {v['regulation']}" if v["regulation"] else "") + _input_text(v))
    lines += ["Loads (the voltage across supply and ground pins)",
              f"  {'name':<14} {'group':<10} {'I':>8} {'nominal':>8} {'supply':>9} {'ground':>9} "
              f"{'across':>9} {'drop':>14}  verdict"]
    for ld in doc["loads"]:
        verdict = ("FAIL" if ld["exceeds_drop_limit"] else "pass" if ld["exceeds_drop_limit"] is False
                   else "not judged")
        drop = (f"{ld['drop_mv']:.2f} mV ({_fmt(ld['drop_pct'], 0, '.2f', ' %').strip()})"
                if ld["drop_mv"] is not None else "—")
        lines.append(f"  {ld['name']:<14} {ld['group']:<10} {_fmt(ld['current_a'], 8, '.4g', 'A')} "
                     f"{_fmt(ld['nominal_v'], 8, '.4g', 'V')} {_fmt(ld['v_supply_v'], 9, '.5f', 'V')} "
                     f"{_fmt(ld['v_ground_v'], 9, '.2f', 'mV', 1e3)} {_fmt(ld['v_across_v'], 9, '.5f', 'V')} "
                     f"{drop:>14}  {ld['status'] or verdict}")
    lines.append("Groups")
    worsts = [_group_worst(gr) + "," for gr in doc["groups"]]
    width = max(map(len, worsts), default=0)
    for gr, worst in zip(doc["groups"], worsts):
        lines.append(f"  {gr['group']:<10} {gr['kind']:<6} {' + '.join(gr['nets']):<22} {worst:<{width}} "
                     f"copper loss {_fmt(gr['power_loss_w'], 0, '.3g', ' mW', 1e3).strip()}: "
                     f"{gr['verdict']}")
    for k in doc["links"]:
        lines.append(f"  link {k['name']}: {_fmt(k['current_a'], 0, '.4f', ' A').strip()}, "
                     f"{_fmt(k['power_w'], 0, '.3g', ' mW', 1e3).strip()} ({' ↔ '.join(k['nets'])})")
    if doc.get("thermal"):                              # the board temperature (Task 8)
        from coppernicus.temperature import temperature_text
        lines += temperature_text(doc["thermal"], skip=doc["assumptions"])
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    lines += [f"  assumption: {a}" for a in doc["assumptions"]]
    lines += [f"  warning: {w}" for w in doc["warnings"]]
    return lines
