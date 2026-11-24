"""Power-tree helpers that need no engine: plain project and result logic.

* :func:`estimated_draws`: what a rail solved on its own (not as part of a tree) draws for
  the regulators and links it feeds, estimated at nominal voltages without copper losses,
  as KiPI's ``_estimated_input`` and ``link_bias`` estimate it. The power-tree solve computes
  the same currents exactly.
* :func:`tree_summary`: a power-tree result as a plain, JSON-ready dict (units in every
  key), for the CLI and the agent interface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coppernicus.project import PinSpec, Project, Source

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


def judge_loads(tree) -> None:
    """Set each load's verdict against the drop limit of its group (``tree.limits``), only
    the user's: ``None`` without a limit, or for a load that was not solved."""
    for ld in tree.loads:
        limits = tree.limits.get(ld.group)
        limit = getattr(limits, "max_drop_pct", None)
        # A load behind a regulator that cannot hold its voltage has no physical voltage to
        # judge (the engine still holds that output at its set point).
        ok = (ld.status not in ("DISCONNECTED", "NOT CONVERGED", "VRM OUT OF REGULATION")
              and math.isfinite(ld.drop_pct))
        ld.passed = None if limit is None or not ok else bool(ld.drop_pct <= limit)


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
    from coppernicus.problems import FAIL, find_tree_problems, limits_dict

    groups, failed, judged = [], 0, {}
    for key, res in tree.groups.items():
        limits = tree.limits.get(key)
        problems = find_tree_problems(tree, key, limits, top=top)
        checked = limits_dict(limits, res.kind)
        fails = [p for p in problems if p.severity == FAIL]
        failed += len(fails)
        judged.update({f"{key}: {k}": v for k, v in checked.items()})
        v_ref = res.v_ref
        groups.append({
            "group": key, "kind": res.kind, "nets": list(res.nets), "v_ref_v": v_ref,
            ("max_rise_mv" if res.kind == "ground" else "max_drop_mv"): res.max_drop * 1e3,
            "max_drop_pct": (100.0 * res.max_drop / abs(v_ref)) if v_ref and res.kind == "rail" else None,
            "power_loss_w": res.power_loss, "loss_breakdown_w": res.loss_breakdown(),
            "j_point_max_a_mm2": res.j_max,
            "via_current_max_a": max((v.current for v in res.vias), default=0.0),
            "verdict": "fail" if fails else ("pass" if checked else "not_judged"),
            "limits": checked, "failed": len(fails),
            "problems": [{"severity": p.severity, "kind": p.kind, "title": p.title, **p.data}
                         for p in problems],
        })
    verdict = "fail" if failed else ("pass" if judged else "not_judged")
    note = ("Judged only against the limits you set: " + ", ".join(f"{k} = {v:g}" for k, v in judged.items())
            if judged else "No limits set: nothing is judged, problems are ranked only.")
    return _clean({
        "kind": "power_tree",
        "verdict": verdict, "verdict_note": note, "failed": failed,
        "vrms": [{"name": v.name, "kind": v.kind, "group": v.group, "v_set_v": v.v_set_v,
                  "v_out_v": v.v_out_v, "i_out_a": v.i_out_a, "v_in_v": _num(v.v_in_v),
                  "i_in_a": _num(v.i_in_a), "p_out_w": v.p_out_w, "p_in_w": _num(v.p_in_w),
                  "p_loss_w": _num(v.p_loss_w), "v_ground_v": _num(v.v_ground_v),
                  "v_sense_v": _num(v.v_sense_v), "v_ground_sense_v": _num(v.v_ground_sense_v),
                  "regulation": v.regulation} for v in tree.vrms],
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
        "assumptions": list(tree.assumptions), "warnings": list(tree.warnings),
    })


def _fmt(x: Optional[float], width: int, spec: str, unit: str, scale: float = 1.0) -> str:
    """``x`` with its unit, right-aligned in ``width`` characters; "—" when there is none."""
    return f"{'—' if x is None else f'{x * scale:{spec}}{unit}':>{width}}"


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
                     + (f"  {v['regulation']}" if v["regulation"] else ""))
    lines += ["Loads (the voltage across supply and ground pins)",
              f"  {'name':<14} {'group':<10} {'I':>8} {'nominal':>8} {'supply':>9} {'ground':>9} "
              f"{'across':>9} {'drop':>14}  verdict"]
    for ld in doc["loads"]:
        verdict = ("FAIL" if ld["exceeds_drop_limit"] else "pass" if ld["exceeds_drop_limit"] is False
                   else "not judged")
        drop = (f"{ld['drop_mv']:.2f} mV ({ld['drop_pct']:.2f} %)" if ld["drop_mv"] is not None
                else "—")
        lines.append(f"  {ld['name']:<14} {ld['group']:<10} {_fmt(ld['current_a'], 8, '.4g', 'A')} "
                     f"{_fmt(ld['nominal_v'], 8, '.4g', 'V')} {_fmt(ld['v_supply_v'], 9, '.5f', 'V')} "
                     f"{_fmt(ld['v_ground_v'], 9, '.2f', 'mV', 1e3)} {_fmt(ld['v_across_v'], 9, '.5f', 'V')} "
                     f"{drop:>14}  {ld['status'] or verdict}")
    lines.append("Groups")
    for gr in doc["groups"]:
        if gr["kind"] == "ground":
            worst = f"max rise {gr['max_rise_mv']:.3f} mV above 0 V"
        else:
            worst = f"max drop {gr['max_drop_mv']:.3f} mV ({gr['max_drop_pct']:.3f} %)"
        lines.append(f"  {gr['group']:<10} {gr['kind']:<6} {' + '.join(gr['nets']):<22} {worst}, "
                     f"copper loss {gr['power_loss_w'] * 1e3:.3g} mW: {gr['verdict']}")
    for k in doc["links"]:
        lines.append(f"  link {k['name']}: {k['current_a']:.4f} A, {k['power_w'] * 1e3:.3g} mW "
                     f"({' ↔ '.join(k['nets'])})")
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    lines += [f"  assumption: {a}" for a in doc["assumptions"]]
    lines += [f"  warning: {w}" for w in doc["warnings"]]
    return lines
