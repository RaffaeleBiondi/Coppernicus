"""Power-tree helpers that need no engine: plain project and result logic.

* :func:`estimated_draws`: what a rail solved on its own (not as part of a tree) draws for
  the regulators and links it feeds, estimated at nominal voltages without copper losses,
  as KiPI's ``_estimated_input`` and ``link_bias`` estimate it. The power-tree solve computes
  the same currents exactly.
* :func:`apply_limits`, :func:`net_limits`, :func:`group_limits`, :func:`tree_verdicts`:
  which of the user's limits judge what in a power tree, and the verdicts (see
  :data:`LIMITS_RULE`).
* :func:`judge_ratings`, :func:`rating_origins`: each regulator's output current against the
  rated output current the user wrote for it (``Source.current_limit_a``: KiCad field PI_IMAX
  or the project file), and where it was written.
* :func:`tree_summary`: a power-tree result as a plain, JSON-ready dict (units in every
  key), for the CLI and the agent interface.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coppernicus.project import Limits, PinSpec, Project, Source
from coppernicus.results import DISCONNECTED, NOT_CONVERGED, OUT_OF_REGULATION, degenerate_triangles

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
    # A rating where no VRM row judges it: the rail excluded (KiPI makes its regulator a load
    # on its input) or solved as a ground (its sources are left out of the setup).
    tree.unsolved_ratings = [
        unjudged(s.label, rail.net, RATING_KEY, "not solved in the power tree")
        for rail in project.rails for s in rail.sources
        if s.current_limit_a is not None and not _solved_as_rail(tree, rail.net)]
    judge_loads(tree)
    judge_ratings(tree)                  # the ratings came with the solve (convert_tree)
    rated_groups = {v.group for v in tree.vrms if v.current_limit_a is not None}
    # The rule, once: on the tree, and on each group a rated regulator drives (the app shows a
    # group's assumptions beside its problems).
    for key, notes in [("", tree.assumptions)] + [(k, r.assumptions) for k, r in tree.groups.items()]:
        rated = key in rated_groups if key else bool(rated_groups or tree.unsolved_ratings)
        if rated and RATING_RULE not in notes:
            notes.append(RATING_RULE)
        elif not rated and RATING_RULE in notes:
            notes.remove(RATING_RULE)
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
    not-judged entry; then each rated source on a rail it did not solve as a rail
    (``tree.unsolved_ratings``, see :func:`apply_limits`)."""
    from coppernicus.problems import LIMIT_KEYS
    limits = [unjudged(net, net, key, "not solved in the power tree")
              for net, lim in tree.limits.items() if tree.group_of(net) is None
              for key, attr in LIMIT_KEYS.items() if getattr(lim, attr) is not None]
    return limits + list(tree.unsolved_ratings)


def _solved_as_rail(tree, net: str) -> bool:
    key = tree.group_of(net)
    return key is not None and tree.groups[key].kind != "ground"


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


# ---------------------------------------------------------------------- regulator ratings

RATING_KEY = "current_limit_a"           # the rating's key in not-judged entries and the note
#: |I out| within this fraction of its rating counts as at it, not over: the solve's round-off
#: (2.0000000000001 A out of a 2 A load) must not fail a part rated for what it delivers.
RATING_ROUND_OFF = 1e-9
RATING_FROM_EITHER = "KiCad field PI_IMAX or the project file"     # where, when it is not known
#: The assumption line of a tree with a rating (:func:`apply_limits`): the rule as judged.
RATING_RULE = ("Regulator ratings (a source's rated output current, current_limit_a: KiCad field "
               "PI_IMAX or the project file): a regulator fails when |I out| is above its rating "
               "(within 1e-9, relative, of it counts as at the rating: the solve's round-off); not "
               "judged while its group, a group it feeds or the power-tree iteration has not "
               "converged, nor on a rail the tree does not solve; a regulator out of regulation is "
               "still judged on its current, as KiPI does.")


def judge_ratings(tree) -> None:
    """Judge each regulator's output current against the rated output current the user wrote
    for it (``VrmReport.current_limit_a``), as KiPI's check does (|I out| <= the rating, within
    :data:`RATING_ROUND_OFF`): ``current_passed`` True or False; None without a rating (no
    limit was set: not judged, and not listed as not judged) or when it cannot be judged (I out
    not settled, or not solved: see :func:`rating_why_not`)."""
    for v in tree.vrms:
        if (v.current_limit_a is None or v.group not in tree.groups or _unsettled(tree, v)
                or not math.isfinite(v.i_out_a)):
            v.current_passed = None
            continue
        amps = abs(v.i_out_a)
        v.current_passed = bool(amps <= v.current_limit_a
                                or math.isclose(amps, v.current_limit_a, rel_tol=RATING_ROUND_OFF))


def _unsettled(tree, v) -> str:
    """Why regulator ``v``'s output current is not settled ("" when it is). It includes what
    every rail it feeds draws, through their regulators' inputs, so each of those groups must
    have converged, and the tree's outer iteration (``tree.converged``; KiPI also marks a load
    NOT CONVERGED then, or when its group's iteration did not settle)."""
    fed, todo = [v.group], [v.group]
    while todo:
        key = todo.pop()
        for u in tree.vrms:
            if u.input_group == key and u.group not in fed:
                fed.append(u.group)
                todo.append(u.group)
    for key in fed:
        res = tree.groups.get(key)
        if res is not None and not res.converged:
            return ("its iteration did not converge" if key == v.group
                    else f"{key}, which it feeds, did not converge")
    if not tree.converged or any(ld.status == NOT_CONVERGED for ld in tree.loads if ld.group in fed):
        return "the power-tree iteration did not converge"
    return ""


def rating_why_not(tree, v) -> str:
    """Why rated regulator ``v``'s output current was not judged."""
    return _unsettled(tree, v) or "its output current was not solved"


def rated_from(v) -> str:
    """Where regulator ``v``'s rating was written (:func:`rating_origins`), or
    :data:`RATING_FROM_EITHER` when that is not known."""
    return v.current_limit_from or RATING_FROM_EITHER


def rating_origins(tree, merged) -> None:
    """Name where each rated regulator's rating was written (``current_limit_from``), from
    ``merged`` (:class:`coppernicus.board_fields.Merged`), the project the tree was solved
    with: when its rail comes from the KiCad fields (origin ``"fields"``), "KiCad field PI_IMAX
    on U1", the part carrying the field (a source held at another part's pads, through
    PI_SOURCE_REF, keeps that part as its label); any other rail (the project file's, one that
    overrides the fields, one the fields leave incomplete) or no KiCad fields at all: "the
    project file"."""
    for v in tree.vrms:
        if v.current_limit_a is None:
            continue
        origin = merged.origins.get(v.group)
        if origin is None or origin.source != "fields":
            v.current_limit_from = "the project file"
            continue
        source = _rated_source(merged.project.rail(v.group), v.name)
        v.current_limit_from = "KiCad field PI_IMAX" + (f" on {source.label}" if source else "")


def _rated_source(rail, name: str) -> Optional[Source]:
    """The source of ``rail`` the engine named ``name`` (its label, or "label (net)" when the
    label is used on several rails), else its only rated source; None when neither is one."""
    if rail is None:
        return None
    named = [s for s in rail.sources if name in (s.label, f"{s.label} ({rail.net})")]
    rated = [s for s in rail.sources if s.current_limit_a is not None]
    return named[0] if len(named) == 1 else rated[0] if len(rated) == 1 else None


def _amps(i_out: float, limit: float) -> str:
    """|I out| as text, with as many digits as it takes to read differently from the rating
    (at least 4 significant): a fail never reads "2 A out, over its rated 2 A"."""
    amps = abs(i_out)
    for digits in range(4, 13):
        text = f"{amps:.{digits}g}"
        if text != f"{limit:.{digits}g}":
            break
    return text


def rating_title(v) -> str:
    """Rated regulator ``v`` against its rating, for problems and the verdict note: "U1: 2.502 A
    out, over its rated 2 A (from KiCad field PI_IMAX on U1)"; "within its rated" when it
    passed, "rated" when it was not judged."""
    rated = f"{v.current_limit_a:g} A (from {rated_from(v)})"
    if not math.isfinite(v.i_out_a):
        return f"{v.name}: output current not solved, rated {rated}"
    how = ("over its rated" if v.current_passed is False else
           "within its rated" if v.current_passed else "rated")
    return f"{v.name}: {_amps(v.i_out_a, v.current_limit_a)} A out, {how} {rated}"


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
    # The regulators driving it that have a rated output current: name -> A (judged as one
    # of its limits; kept out of ``limits``, which holds the net limits).
    ratings: Dict[str, float] = field(default_factory=dict)


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
    rated = [v for v in tree.vrms if v.group == key and v.current_limit_a is not None]
    missing += [unjudged(v.name, key, RATING_KEY, rating_why_not(tree, v))
                for v in rated if v.current_passed is None]
    judged += sum(v.current_passed is not None for v in rated)
    verdict = "fail" if failed else "not_judged" if missing or not judged else "pass"
    return GroupVerdict(verdict, failed, checked, origin, missing, problems,
                        ratings={v.name: v.current_limit_a for v in rated})


def has_limits(gv: GroupVerdict) -> bool:
    """Whether the user set a limit on this group: one that applies, one that does not (and
    is listed as not judged), or the rating of a regulator driving it."""
    return bool(gv.limits or gv.not_judged or gv.ratings)


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
    rated = [v for v in tree.vrms if v.current_limit_a is not None]
    if not own and not tv.limits and not missing and not rated:
        return "No limits set: nothing is judged, problems are ranked only."
    judged = ([f"{net}: {k} = {v:g}" for net, d in own.items() for k, v in d.items()]
              + [f"{ITEM}: {k} = {v:g}" for k, v in tv.limits.items()]
              + [f"{v.name}: {RATING_KEY} = {v.current_limit_a:g} (from {rated_from(v)})" for v in rated])
    note = ("Judged only against the limits you set: " + ", ".join(judged)
            if judged else "No limit you set applies to what it is set on")
    if tv.failed_rows:                  # the board temperature's fails, named (the groups' are problems)
        note += f". Board temperature over a limit: {'; '.join(tv.failed_rows)}"
    over = [rating_title(v) for v in rated if v.current_passed is False]
    if over:                            # named here too: the CLI prints the note, not the problems
        note += f". Over a rated output current: {'; '.join(over)}"
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


def _at(where) -> Dict[str, Any]:
    x, y, layer = where
    return {"x_mm": _num(x), "y_mm": _num(y), "layer": layer or None}


def net_split_rows(res) -> List[Dict[str, Any]]:
    """A power-tree group's per-net split (``RailResult.net_split``) as JSON-ready rows: each
    net's lowest and highest voltage and where they are, its entry (``kind`` "source", "link"
    or "reference", ``name``, ``pads`` as "REF.PAD", ``v_v``), its drop from the entry
    (``drop_mv``; a ground's rise, ``rise_mv``) and its copper and via losses."""
    key = "rise_mv" if res.kind == "ground" else "drop_mv"
    return [{"net": n.net, "v_min_v": _num(n.v_min_v), "v_max_v": _num(n.v_max_v),
             "at_min": _at(n.at_min), "at_max": _at(n.at_max),
             "entry": ({"kind": n.entry_kind, "name": n.entry_name, "pads": list(n.entry_pads),
                        "v_v": _num(n.entry_v)} if n.entry_kind else None),
             key: None if _num(n.drop_v) is None else n.drop_v * 1e3,
             "copper_loss_w": float(n.copper_loss_w), "via_loss_w": float(n.via_loss_w)}
            for n in getattr(res, "net_split", None) or ()]


def segment_keys(segments) -> Dict[str, Any]:
    """A drop's segments (``LoadReport.segments``, ``VrmReport.segments``) as JSON keys:
    ``segments`` ({what, name, from, to, mv}) and ``segments_sum_mv`` (None without any)."""
    segs = list(segments or ())
    return {"segments": [{"what": s.what, "name": s.name, "from": s.frm, "to": s.to, "mv": s.v * 1e3}
                         for s in segs],
            "segments_sum_mv": sum(s.v for s in segs) * 1e3 if segs else None}


def tree_summary(tree, top: int = 5) -> Dict[str, Any]:
    """A power-tree result (``coppernicus.results.TreeResult``) as a JSON-ready dict: units
    in every key, the assumptions and warnings, the verdict (only against the user's limits,
    per group) and the VRM, load, link and group tables."""
    from coppernicus.problems import ATTENTION, limits_dict
    from coppernicus.temperature import thermal_summary, thermal_verdict

    overall, verdicts = tree_verdicts(tree, top)
    tv = thermal_verdict(tree)
    groups, failed = [], 0
    # I max: each VRM's rated output current, the value judged (None where not given); only
    # when some VRM has one, so a document without ratings stays as it was.
    rated = any(v.current_limit_a is not None for v in tree.vrms)
    for key, res in tree.groups.items():
        gv = verdicts[key]
        failed += gv.failed
        v_ref = res.v_ref
        by_net = {net: limits_dict(net_limits(tree, net, key)[0], res.kind, res.v_ref) for net in res.nets}
        degenerate = degenerate_triangles(res)
        groups.append({
            "group": key, "kind": res.kind, "nets": list(res.nets), "v_ref_v": v_ref,
            ("max_rise_mv" if res.kind == "ground" else "max_drop_mv"): res.max_drop * 1e3,
            "max_drop_pct": (100.0 * res.max_drop / abs(v_ref)) if v_ref and res.kind == "rail" else None,
            "power_loss_w": res.power_loss, "loss_breakdown_w": res.loss_breakdown(),
            "j_point_max_a_mm2": res.j_max,
            # Degenerate triangles (an edge under 1 nm) left out of every maximum |J|, if any
            **({"degenerate_triangles": degenerate} if degenerate else {}),
            "via_current_max_a": max((v.current for v in res.vias), default=0.0),
            "verdict": gv.verdict, "limits": gv.limits, "failed": gv.failed,
            # spots above the current-density default: attention, never part of the verdict
            "attention": sum(p.severity == ATTENTION for p in gv.problems),
            "limits_by_net": {n: d for n, d in by_net.items() if d},
            "not_judged": list(gv.not_judged),
            "problems": [{"severity": p.severity, "kind": p.kind, "title": p.title,
                          "detail": p.detail, "layer": p.layer, "x_mm": _num(p.x),
                          "y_mm": _num(p.y), **p.data} for p in gv.problems],
            # Each net's voltages, entry, drop or rise and losses (shown, never judged), and how
            # links in parallel or in a loop were attributed (only when there are some)
            "net_split": net_split_rows(res),
            **({"split_note": res.split_note} if getattr(res, "split_note", "") else {}),
        })
    doc = {
        "kind": "power_tree",
        "verdict": overall, "verdict_note": verdict_note(tree, verdicts), "failed": failed + tv.failed,
        "not_judged": [x for v in verdicts.values() for x in v.not_judged] + unsolved_limits(tree)
        + tv.not_judged,
        "attention": sum(g["attention"] for g in groups),
        "vrms": [{"name": v.name, "kind": v.kind, "group": v.group, "v_set_v": v.v_set_v,
                  "v_out_v": v.v_out_v, "i_out_a": v.i_out_a,
                  **({"i_max_a": v.current_limit_a} if rated else {}), "v_in_v": _num(v.v_in_v),
                  "i_in_a": _num(v.i_in_a), "p_out_w": v.p_out_w, "p_in_w": _num(v.p_in_w),
                  "p_loss_w": _num(v.p_loss_w), "v_ground_v": _num(v.v_ground_v),
                  "v_sense_v": _num(v.v_sense_v), "v_ground_sense_v": _num(v.v_ground_sense_v),
                  "regulation": v.regulation, "input_net": v.input_net or None,
                  "input_drop_mv": None if _num(v.input_drop_v) is None else v.input_drop_v * 1e3,
                  "input_drop_pct": _num(v.input_drop_pct), "input_status": v.input_status,
                  "input_exceeds_drop_limit": None if v.input_passed is None else not v.input_passed,
                  # |I out| over the rated output current the user wrote (None: no rating, or
                  # not judged), and where that rating was written (None: no rating)
                  "exceeds_current_limit": None if v.current_passed is None else not v.current_passed,
                  "current_limit_from": rated_from(v) if v.current_limit_a is not None else None,
                  # its input drop, segment by segment (shown, never judged; [] without one)
                  **segment_keys(v.segments)}
                 for v in tree.vrms],
        "loads": [{"name": ld.name, "group": ld.group, "current_a": ld.current_a,
                   "nominal_v": ld.nominal_v, "v_supply_v": _num(ld.v_supply_v),
                   "v_supply_min_v": _num(ld.v_supply_min_v), "v_ground_v": _num(ld.v_ground_v),
                   "v_across_v": _num(ld.v_across_v), "v_worst_v": _num(ld.v_worst_v),
                   "drop_mv": None if _num(ld.drop_v) is None else ld.drop_v * 1e3,
                   "drop_pct": _num(ld.drop_pct), "status": ld.status,
                   "exceeds_drop_limit": None if ld.passed is None else not ld.passed,
                   # the net its pins are on, and the rail it is listed in (they differ past
                   # a net tie)
                   "net": ld.net or None, "rail": ld.rail or ld.net or None,
                   # its drop, segment by segment (shown, never judged; [] without one)
                   **segment_keys(ld.segments)}
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


def _rating_text(v: Dict[str, Any]) -> str:
    """A regulator's output current against its rating, for its VRM row ("" without one)."""
    if v.get("current_limit_from") is None:
        return ""
    exceeds = v.get("exceeds_current_limit")
    return f"  rated current: {'FAIL' if exceeds else 'pass' if exceeds is False else 'not judged'}"


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


ATTENTION_HEAD = "Attention (Coppernicus's current-density default, not a verdict)"


def attention_text(groups: Sequence[Dict[str, Any]]) -> List[str]:
    """The groups' spots above the current-density default, as lines for the tree's text
    (none when there are none): "Attention (…): +1V8: 1, GND: 3; the strongest 78.6 A/mm² on
    F.Cu (+1V8), default 60 A/mm²"."""
    counts = [(g["group"], g.get("attention", 0)) for g in groups if g.get("attention")]
    if not counts:
        return []
    spots = [(p, g["group"]) for g in groups for p in g.get("problems", [])
             if p.get("severity") == "attention"]
    strongest = ""
    if spots:
        p, key = max(spots, key=lambda s: s[0].get("mean_j_a_mm2") or 0.0)
        strongest = (f"; the strongest {p['mean_j_a_mm2']:.1f} A/mm² on {p.get('layer')} ({key}), "
                     f"default {p['default_j_a_mm2']:.3g} A/mm²")
    return [f"{ATTENTION_HEAD}: " + ", ".join(f"{k}: {n}" for k, n in counts) + strongest
            + "; each is listed in its group's problems."]


def _mv_text(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.3f} mV"


def split_text(doc: Dict[str, Any]) -> List[str]:
    """The lines ``coppernicus solve --tree --split`` adds: each load's and regulator input's
    drop segment by segment, then each net from where it is fed (shown, never judged)."""
    items = [(f"{ld['name']} on {ld.get('net') or ld['group']}", ld.get("segments_sum_mv"),
              ld.get("segments")) for ld in doc["loads"]]
    items += [(f"{v['name']} (input) on {v['input_net']}", v.get("segments_sum_mv"), v.get("segments"))
              for v in doc["vrms"] if v.get("input_net")]
    lines = ["Split (each drop part by part, from its rail's source to its worst pins, toward 0 V; "
             "shown, never judged)"]
    for title, total, segs in items:
        lines.append(f"  {title}: {_mv_text(total)}" + ("" if segs else " (no split: not solved)"))
        segs = segs or []
        width = max((len(f"{s['from']} → {s['to']}") for s in segs), default=0)
        lines += [f"    {s['what']:<14} {s['name']:<14} {s['from'] + ' → ' + s['to']:<{width}} "
                  f"{s['mv']:>10.3f} mV" for s in segs]
    lines += ["Nets (each from where it is fed; shown, never judged)",
              f"  {'net':<12} {'group':<10} {'V min':>10} {'V max':>10}  {'fed from':<20} "
              f"{'drop/rise':>12} {'copper':>10} {'vias':>9}"]
    notes = []
    for gr in doc["groups"]:
        for n in gr.get("net_split") or ():
            entry = n.get("entry")
            fed = f"{entry['name']} ({_text(entry['pads'])})" if entry else "—"
            change = n.get("rise_mv", n.get("drop_mv"))
            word = "rise" if "rise_mv" in n else "drop"
            lines.append(f"  {n['net']:<12} {gr['group']:<10} {_fmt(n['v_min_v'], 10, '.6f', '')} "
                         f"{_fmt(n['v_max_v'], 10, '.6f', '')}  {fed:<20} "
                         f"{(word + ' ' + _mv_text(change)) if change is not None else '—':>12} "
                         f"{_fmt(n['copper_loss_w'], 10, '.3g', ' mW', 1e3)} "
                         f"{_fmt(n['via_loss_w'], 9, '.3g', ' mW', 1e3)}")
        if gr.get("split_note"):
            notes.append(f"  {gr['group']}: {gr['split_note']}")
    for k in doc["links"]:
        if "across_mv" in k:
            lines.append(f"  link {k['name']} ({' → '.join(k['nets'])}): {_mv_text(k['across_mv'])} across")
    return lines + notes


def _text(labels: Sequence[str]) -> str:
    return ", ".join(labels[:3]) + (f" (+{len(labels) - 3})" if len(labels) > 3 else "")


def tree_text(doc: Dict[str, Any], split: bool = False) -> List[str]:
    """The lines ``coppernicus solve --tree`` prints, from a :func:`tree_summary` dict; with
    ``split`` (``--split``) the drops segment by segment and the nets too (:func:`split_text`)."""
    # I max: a source's rated output current (PI_IMAX; judged: _rating_text); only when some VRM
    # has one, so the table is unchanged otherwise.
    rated = any(v.get("i_max_a") is not None for v in doc["vrms"])
    lines = ["VRMs (sources and regulators)",
             f"  {'name':<14} {'kind':<6} {'group':<10} {'V set':>8} {'V out':>9} {'I out':>9} "
             + (f"{'I max':>9} " if rated else "") + f"{'V in':>9} {'I in':>9} {'P loss':>9}"]
    for v in doc["vrms"]:
        lines.append(f"  {v['name']:<14} {v['kind']:<6} {v['group']:<10} {_fmt(v['v_set_v'], 8, '.4g', 'V')} "
                     f"{_fmt(v['v_out_v'], 9, '.5f', 'V')} {_fmt(v['i_out_a'], 9, '.4f', 'A')} "
                     + (f"{_fmt(v.get('i_max_a'), 9, '.4f', 'A')} " if rated else "")
                     + f"{_fmt(v['v_in_v'], 9, '.5f', 'V')} {_fmt(v['i_in_a'], 9, '.4f', 'A')} "
                     f"{_fmt(v['p_loss_w'], 9, '.1f', 'mW', 1e3)}"
                     + (f"  {v['regulation']}" if v["regulation"] else "") + _rating_text(v)
                     + _input_text(v))
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
    if split:
        lines += split_text(doc)
    if doc.get("thermal"):                              # the board temperature (Task 8)
        from coppernicus.temperature import temperature_text
        lines += temperature_text(doc["thermal"], skip=doc["assumptions"])
    lines += attention_text(doc["groups"])
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    lines += [f"  assumption: {a}" for a in doc["assumptions"]]
    lines += [f"  warning: {w}" for w in doc["warnings"]]
    return lines
