"""Tolerance stack-up per load (Task 11): the parts from the power tree, the AC and noise
results and what the user entered, the checks, the verdicts, the summary document, text and
CSV.

Per load, each side apart (below and above nominal), every part a deviation in volts (KiPI
``kipi/tolerance``): DC = the DC drop on the worst pins + the source's DC accuracy, against
the DC tolerance; AC = ripple + transient, against the AC tolerance; total = DC + AC, against
the tolerance. A part that is not known is missing, never 0 (KiPI counts an accuracy not set,
a rail with no switcher and a partly solved ripple as known): a check fails on its known parts
alone and passes only with all of them. A part resting on a KiPI stand-in is missing too (its
value in the hint). Only an entered transient is judged; KiPI's estimate is shown. Rules:
``docs/engine-mapping.md`` (Task 11).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from coppernicus.impedance import hz_text
from coppernicus.project import Load, Project, Source
from coppernicus.results import (
    DISCONNECTED,
    NOT_CONVERGED,
    OUT_OF_REGULATION,
    ImpedanceResult,
    LoadReport,
    NoiseResult,
    ProbeImpedance,
    RippleResult,
    ToleranceCheck,
    TolerancePart,
    ToleranceResult,
    ToleranceRow,
    TreeResult,
)
from coppernicus.results import TOLERANCE_CHECKS as CHECKS
from coppernicus.results import TOLERANCE_PARTS as PARTS

PART_NAMES = {"dc": "DC drop", "accuracy": "DC accuracy", "ripple": "ripple", "transient": "transient"}
CHECK_NAMES = {"dc": "DC", "ac": "AC", "total": "total"}
STATUS_WHY = {DISCONNECTED: "no copper path to its source",
              NOT_CONVERGED: "its group's iteration did not converge",
              OUT_OF_REGULATION: "its source cannot hold its voltage"}

Pair = Tuple[float, float]


def _mv(x: float) -> str:
    return f"{x * 1e3:.4g}"


def _pair_mv(below: float, above: float) -> str:
    return f"{_mv(below)} / {_mv(above)} mV"


def _entered(pair: Pair, unit: str = "mV") -> str:
    """An entered pair as written: "±5 mV", or "-3 / +2 %"."""
    below, above = pair
    return f"±{above:g} {unit}" if below == -above else f"{below:g} / +{above:g} {unit}"


def _deviation(pair: Pair) -> Pair:
    """An entered (below, above) pair in mV, signed, as deviations in V (never negative)."""
    return (0.0 - pair[0]) / 1e3, pair[1] / 1e3


# ---------------------------------------------------------------------- parts

def _dc(tree: TreeResult, rep: Optional[LoadReport]) -> Tuple[Optional[TolerancePart], str]:
    """The DC part from the worst pins on both sides (brief Q1): below = nominal − v_low,
    above = v_high − nominal, each at least 0; the mean (KiPI's tolerance table, gather.py:75)
    in the detail."""
    if rep is None:
        return None, ("not in the power tree (its net is excluded, or solved as a ground), so its "
                      "DC drop is not known")
    if not tree.converged:
        return None, ("the power tree's ground-sense iteration did not converge, so its DC drop "
                      "is not known")
    if rep.status:
        return None, (f"{rep.status}: {STATUS_WHY.get(rep.status, 'not judged in the power tree')}, "
                      "so its DC drop is not known")
    low, high, nominal = rep.v_low_v, rep.v_high_v, rep.nominal_v
    if low is None or high is None or not (math.isfinite(low) and math.isfinite(high)):
        return None, "the power tree gives no worst pins for it, so its DC drop is not known"
    mean = rep.v_across_v
    detail = (f"worst pins across its ground: lowest {low:.5f} V, highest {high:.5f} V; the mean "
              f"of its pins {mean:.5f} V (KiPI's tolerance table uses the mean: "
              f"{_pair_mv(max(nominal - mean, 0.0), max(mean - nominal, 0.0))})")
    source = "power tree" + (" (electro-thermal)" if tree.thermal is not None else "")
    return TolerancePart(max(nominal - low, 0.0), max(high - nominal, 0.0), source, detail), ""


def _accuracy(source: Optional[Source], nominal: Optional[float]) -> Tuple[Optional[TolerancePart], str]:
    from coppernicus.engine.tolerance import accuracy_sides
    if source is None or nominal is None:
        return None, "no source drives its rail, so there is no DC accuracy"
    sides = accuracy_sides(nominal, source)
    if sides is None:
        if source.accuracy_pct is not None:
            return None, (f"{source.label}'s accuracy_pct is a % of 0 V, which places nothing: "
                          "enter accuracy_mv")
        return None, f"enter {source.label}'s DC accuracy (accuracy_pct)"
    pct = source.accuracy_pct is not None
    key = "accuracy_pct" if pct else "accuracy_mv"
    pair = source.accuracy_pct if pct else source.accuracy_mv
    return TolerancePart(float(sides[0]), float(sides[1]), f"{key} of {source.label}",
                         f"{key} {_entered(pair, '%' if pct else 'mV')} of {nominal:g} V"), ""


def _bandwidth(project: Project, source: Optional[Source]) -> Optional[float]:
    """The source's control loop bandwidth as given (ac.bandwidth_hz, by name, else ref)."""
    if source is None:
        return None
    bw = project.ac.bandwidth_hz
    return bw.get(source.label, bw.get(source.ref))


def _no_estimate(project: Project, load: Load, probe: Optional[str], source: Optional[Source],
                 ac_notes: Dict[str, str]) -> str:
    """Why there is no KiPI estimate of the load's transient ("" when not known)."""
    if probe is None:
        return f"no probe on {load.ref}'s pins"
    p = next((x for x in project.probes if x.name == probe), None)
    if p is not None and not (p.step_a and p.slew_a_per_us):
        return f"probe {probe} has no load step (step_a, slew_a_per_us)"
    if source is not None and _bandwidth(project, source) is None:
        return f"no control loop bandwidth is given for {source.label} (ac.bandwidth_hz)"
    return ac_notes.get(probe, "")


def _transient(project: Project, load: Load, nominal: Optional[float], probe: Optional[str],
               shown: Optional[ProbeImpedance], source: Optional[Source], ac_notes: Dict[str, str]
               ) -> Tuple[Optional[TolerancePart], str, Optional[Tuple[float, float, float, float]]]:
    """The entered ``transient_mv`` (judged) and KiPI's estimate at the probe (shown, never
    judged, brief Q2; a step pulls a negative rail up, so its under- and overshoot swap,
    gather.py:163)."""
    est = shown.transient if shown is not None else None
    estimate, text = None, ""
    if est is not None:
        e = (est.undershoot_v, est.overshoot_v, est.undershoot_bw_v, est.overshoot_bw_v)
        if nominal is not None and nominal < 0:
            e = (e[1], e[0], e[3], e[2])
        estimate = tuple(float(x) for x in e)
        text = (f"KiPI's estimate with a typical loop at {hz_text(est.bandwidth_hz)}: "
                f"{_pair_mv(e[0], e[1])}, {_pair_mv(e[2], e[3])} through 20 MHz")
    if load.transient_mv is not None:
        detail = f"{text} (shown, never judged)" if text else ""
        return TolerancePart(*_deviation(load.transient_mv), "entered (transient_mv)", detail), "", estimate
    why = _no_estimate(project, load, probe, source, ac_notes) if not text else ""
    hint = f"enter transient_mv for {load.label}"
    hint += f"; {text} (shown, never judged)" if text else f"; no KiPI estimate: {why}" if why else ""
    return None, hint, estimate


def _probe_ripple(r: NoiseResult, probe: str) -> Optional[RippleResult]:
    return next((x for x in r.ripple if x.where == "probe" and x.name == probe), None)


def _ripple(project: Project, load: Load, probe: Optional[str], source: Optional[Source],
            source_name: str, noise: Dict[str, NoiseResult], notes: Dict[str, str]
            ) -> Tuple[Optional[TolerancePart], str, List[str], Optional[TolerancePart]]:
    """The ripple from every aggressor listing the load's probe (any of them unsolved, with
    stand-ins, or showing no ripple there: missing), added by ``tolerance.ripple_sum``, the
    larger per side with an entered ``ripple_mv``; with none listing it, the entered
    ``ripple_mv``; else missing (never 0). ``(part, hint, stand-ins, floor)``: while it is
    missing, its least value where known (the usable aggressors added by the same rule, and
    ``ripple_mv``, the larger per side; a stand-in's value never)."""
    bw = project.tolerance.bw_limit_20mhz
    entered = load.ripple_mv
    listing = [a for a in project.aggressors if probe is not None and probe in a.ripple_probes]
    if listing:
        values, problems, stand_ins = [], [], []
        for a in listing:
            r = noise.get(a.name)
            rp = _probe_ripple(r, probe) if r is not None else None
            if r is None or r.partial:    # a result handed over during the solve is not solved
                note = ("its result is partial: the solve did not finish" if r is not None
                        else notes.get(a.name, ""))
                problems.append(f"{a.name} not solved" + (f" ({note})" if note else ""))
            elif r.stand_ins:
                stand_ins += [s for s in r.stand_ins if s not in stand_ins]
                seen = f": {_pair_mv(*_sides(rp, bw))} not used" if rp is not None else ""
                problems.append(f"{a.name}'s ripple rests on KiPI's stand-ins "
                                f"({'; '.join(r.stand_ins)}){seen}")
            elif rp is None:
                problems.append(f"{a.name} shows no ripple at {probe} (its board model is off, or "
                                f"{probe} is not on its rails)")
            else:
                values.append((a.name, rp))
        below, above, how = _combine(project, values, bw)
        if problems:
            hint = "; ".join(problems)
            if entered is not None:
                hint += (f"; ripple_mv {_entered(entered)} entered, not used while an aggressor "
                         f"listing {probe} gives no usable ripple (it is the ripple's floor)")
            floor, what = None, []
            if values:
                floor, what = (below, above), [f"noise of {', '.join(n for n, _ in values)} at {probe}"]
            if entered is not None:
                eb, ea = _deviation(entered)
                floor = (eb, ea) if floor is None else (max(floor[0], eb), max(floor[1], ea))
                what.append("ripple_mv")
            least = (None if floor is None else
                     TolerancePart(float(floor[0]), float(floor[1]), " and ".join(what)))
            return None, hint, stand_ins, least
        each = "; ".join(f"{name} {_pair_mv(rp.below_v, rp.above_v)} "
                         f"(20 MHz {_pair_mv(rp.below_bw_v, rp.above_bw_v)})" for name, rp in values)
        band = "through 20 MHz" if bw else "full band"
        detail = f"at probe {probe}, {band}: {how}{each}"
        what = f"noise of {', '.join(n for n, _ in values)} at {probe}"
        if entered is not None:
            eb, ea = _deviation(entered)
            below, above = max(below, eb), max(above, ea)
            detail += f"; ripple_mv {_entered(entered)} entered: the larger per side"
            what += " and ripple_mv"
        return TolerancePart(float(below), float(above), what, detail), "", [], None
    if entered is not None:
        return TolerancePart(*_deviation(entered), "entered (ripple_mv)", ""), "", [], None
    switching = source is not None and (source.kind == "smps"
                                        or any(a.vrm == source_name for a in project.aggressors))
    if not switching:
        return None, f"enter ripple_mv for {load.label} (no switcher's ripple is solved here)", [], None
    where = f"probe {probe}" if probe is not None else f"a probe on {load.ref}'s pins"
    on = [a.name for a in project.aggressors if a.vrm == source_name]
    if on:
        return None, (f"add {where} to {', '.join(on)}'s ripple probes, or enter ripple_mv for "
                      f"{load.label}"), [], None
    return None, (f"add an aggressor for {source.label} with {where} in its ripple probes, or enter "
                  f"ripple_mv for {load.label}"), [], None


def _sides(rp: RippleResult, bw: bool) -> Pair:
    return (rp.below_bw_v, rp.above_bw_v) if bw else (rp.below_v, rp.above_v)


def _combine(project: Project, values: List[Tuple[str, RippleResult]],
             bw: bool) -> Tuple[float, float, str]:
    """(below, above, how) of several aggressors' ripple by ``tolerance.ripple_sum``."""
    sides = [_sides(rp, bw) for _name, rp in values]
    if project.tolerance.ripple_sum == "rss":
        return (math.sqrt(sum(s[0] ** 2 for s in sides)), math.sqrt(sum(s[1] ** 2 for s in sides)),
                "the root sum of squares of " if len(values) > 1 else "")
    return (sum(s[0] for s in sides), sum(s[1] for s in sides),
            "the sum of the peaks of " if len(values) > 1 else "")


# ---------------------------------------------------------------------- rows and verdicts

def _judge_row(row: ToleranceRow, load: Load) -> None:
    """Each check's limit key and why it is not judged, and the row's verdict."""
    from coppernicus.engine.tolerance import tolerance_of
    for kind, c in row.checks.items():
        _pair, _pct, key = tolerance_of(load, kind)
        c.limit = key
        if not key:
            continue
        if not c.has_limit:
            c.passed = None
            if row.nominal_v is None:
                c.reason = "no source drives its rail, so the tolerance has no nominal"
            elif not math.isfinite(row.nominal_v):
                c.reason = f"the nominal ({row.nominal_v:g} V) is not a finite number: {key} places nothing"
            elif key.endswith("_pct") and row.nominal_v == 0:
                c.reason = f"{key} is a % of 0 V, which places nothing: enter {key[:-len('_pct')]}_mv"
            else:
                c.reason = f"{key} places no finite limit"

        elif c.passed is None:
            c.reason = "; ".join(f"{m}: {row.hints.get(m, 'not known')}" for m in c.missing)
    judged = [c for c in row.checks.values() if c.limit]
    if not judged:
        row.verdict = ""
    elif any(c.passed is False for c in judged):
        row.verdict = "fail"
    elif all(c.passed for c in judged):
        row.verdict = "pass"
    else:
        row.verdict = "not_judged"


def _fail_reason(c: ToleranceCheck, floors: Dict[str, TolerancePart]) -> str:
    from coppernicus.engine.tolerance import EPS_V
    sides = []
    if c.allowed_below_v is not None and c.below_v > c.allowed_below_v + EPS_V:
        sides.append(f"{_mv(c.below_v)} mV below nominal, over the {_mv(c.allowed_below_v)} mV allowed")
    if c.allowed_above_v is not None and c.above_v > c.allowed_above_v + EPS_V:
        sides.append(f"{_mv(c.above_v)} mV above nominal, over the {_mv(c.allowed_above_v)} mV allowed")
    least = "".join(f", {PART_NAMES[k]} at least {_pair_mv(floors[k].below_v, floors[k].above_v)} "
                    f"({floors[k].source})" for k in c.floors if k in floors)
    known = ("" if c.complete else
             f" (from its known parts alone{least}; missing: {', '.join(c.missing)})")
    return f"{CHECK_NAMES[c.kind]} {' and '.join(sides)}{known}"


def _row(project: Project, tree: TreeResult, name: str, rail: str, load: Load,
         rep: Optional[LoadReport], sources: Dict[str, Source], probe: Optional[str],
         shown: Dict[str, ProbeImpedance], noise: Dict[str, NoiseResult],
         ac_notes: Dict[str, str], noise_notes: Dict[str, str], rail_source: Dict[str, str]) -> ToleranceRow:
    from coppernicus.engine.tolerance import load_limits, stack
    driver = next((v.name for v in tree.vrms if rep is not None and v.group == rep.group), None)
    driver = driver if driver is not None else rail_source.get(rail)
    source = sources.get(driver) if driver else None
    nominal = rep.nominal_v if rep is not None else (source.voltage if source is not None else None)
    row = ToleranceRow(load.label, name, rep.net if rep is not None and rep.net else rail, rail,
                       None if nominal is None else float(nominal),
                       source.label if source is not None else "", probe or "")
    from coppernicus.temperature import _not_settled
    found = {"dc": _dc(tree, rep), "accuracy": _accuracy(source, nominal)}
    unsettled = _not_settled(tree.thermal) if tree.thermal is not None else ""
    if unsettled and found["dc"][0] is not None:      # temperature.py's rule: fail, never pass
        dc = found["dc"][0]
        row.floors["dc"] = dc
        found["dc"] = (None, f"the board temperature is {unsettled}: its DC drop "
                             f"{_pair_mv(dc.below_v, dc.above_v)} can fail a check, never pass one")
    part, hint, row.transient_estimate_v = _transient(project, load, nominal, probe, shown.get(probe or ""),
                                                    source, ac_notes)
    found["transient"] = (part, hint)
    part, hint, row.stand_ins, least = _ripple(project, load, probe, source, driver or "", noise,
                                               noise_notes)
    found["ripple"] = (part, hint)
    if least is not None:
        row.floors["ripple"] = least
    for kind in PARTS:
        part, text = found[kind]
        if part is not None and not all(math.isfinite(x) for x in (part.below_v, part.above_v)):
            text = (f"{PART_NAMES[kind]} is not a finite number ({part.below_v:g} / {part.above_v:g} V "
                    f"from {part.source})")
            part = None
        row.parts[kind] = part
        if part is None:
            row.hints[kind] = text
    row.checks = stack({k: None if p is None else (p.below_v, p.above_v) for k, p in row.parts.items()},
                       load_limits(load, row.nominal_v),
                       {k: (p.below_v, p.above_v) for k, p in row.floors.items()})
    _judge_row(row, load)
    return row


def _note(verdict: str, failed, not_judged) -> str:
    def text(e):
        return f"{e['item']} on {e['net']} ({e['limit']}): {e['reason']}"
    if verdict == "fail":
        return "Fail: " + "; ".join(text(e) for e in failed)
    if not_judged:
        return "Not judged: " + "; ".join(text(e) for e in not_judged)
    if verdict == "pass":
        return "Pass: every tolerance set holds, with all its parts known"
    return "No tolerance set: the loads' parts are shown, not judged"


def assumptions(project: Project, tree: TreeResult) -> List[str]:
    t = project.tolerance
    out = [
        "Tolerance stack-up (KiPI kipi/tolerance): per load, each side apart (below and above "
        "nominal), every part a deviation in volts: DC = the DC drop + the source's DC accuracy, "
        "against dc_tol; AC = ripple + transient, against ac_tol; total = DC + AC, against tol.",
        "Nominal is the set voltage of the source driving the load's rail.",
        "DC drop from the power tree" + (" (electro-thermal)" if tree.thermal is not None else "")
        + " on the worst pins: below = nominal − (lowest supply pin − highest ground pin), above "
        "= (highest supply pin − lowest ground pin) − nominal, each at least 0. KiPI's tolerance "
        "table uses the mean of the pins instead (shown in the detail), which reads less on a "
        "load with unequal pins and nothing above nominal on a remote-sensed rail.",
        "A part that is not known (not entered, not solved, or resting on a KiPI stand-in) is "
        "missing, never 0: a check fails on its known parts alone and passes only with all its "
        "parts. KiPI would count an unset accuracy, a rail without an aggressor and an "
        "aggressor not solved as nothing.",
        "Transient: only an entered transient_mv is judged. KiPI's estimate (the open-loop |Z| "
        "with the source put back as a typical PI loop crossing over at the bandwidth given, "
        "about 60° of phase margin into a capacitive PDN, not the converter's own) is shown, "
        "never judged, and only with a bandwidth given (ac.bandwidth_hz).",
        "Entered parts (ripple_mv, transient_mv) are mV from nominal, below and above.",
    ]
    if t.ripple_sum == "rss":
        out.append("Several aggressors' ripple at one probe adds as the root sum of squares of their "
                   "peaks, each side apart (tolerance.ripple_sum: rss), KiPI's rule for "
                   "unsynchronised switchers on average; peaks can coincide.")
    else:
        out.append("Several aggressors' ripple at one probe adds as the sum of their peaks, each side "
                   "apart (tolerance.ripple_sum: linear): unsynchronised switchers drift through "
                   "every relative phase, so their peaks line up at times (KiPI adds them as the "
                   "root sum of squares: rss).")
    if t.bw_limit_20mhz:
        out.append("Ripple judged through a 20 MHz bandwidth limit, a single pole as a scope's "
                   "(tolerance.bw_limit_20mhz), as many datasheets measure; the full band is shown "
                   "too.")
    else:
        out.append("Ripple judged over the full band; through 20 MHz shown too "
                   "(tolerance.bw_limit_20mhz: true judges that instead).")
    return out


def compose(project: Project, tree: TreeResult, impedance: Sequence[ImpedanceResult] = (),
            noise: Sequence[NoiseResult] = (), pairs: Optional[Dict[str, Optional[str]]] = None,
            ac_notes: Optional[Dict[str, str]] = None, noise_notes: Optional[Dict[str, str]] = None,
            warnings: Sequence[str] = (), not_judged: Sequence[Dict[str, Any]] = ()) -> ToleranceResult:
    """Every load's stack-up from the power ``tree``, the AC results (``impedance``: KiPI's
    transient estimate at each probe, shown), the ``noise`` results (the ripple at each probe)
    and what the project enters. ``pairs``: engine load name -> its probe
    (:func:`coppernicus.engine.tolerance.load_probes`); ``ac_notes``: probe -> why its rail was
    not solved in AC; ``noise_notes``: aggressor -> why it was not solved; ``not_judged``: more
    not-judged entries ``{item, net, limit, reason}`` (a tolerance set on a load the solve does
    not hold: a rail from KiCad fields)."""
    from coppernicus.engine.pisetup import terminal_names
    from coppernicus.engine.tolerance import loads_by_name, mismatch_warnings
    pairs, ac_notes, noise_notes = pairs or {}, ac_notes or {}, noise_notes or {}
    names = terminal_names(project)
    sources = {name: project.rail(net).sources[i] for (what, net, i), name in names.items()
               if what == "source"}
    rail_source = {net: name for (what, net, i), name in names.items() if what == "source" and i == 0}
    reports = {ld.name: ld for ld in tree.loads}
    shown = {p.name: p for r in impedance for p in r.probes}
    by_noise = {r.aggressor: r for r in noise}
    out = ToleranceResult(options={"ripple_sum": project.tolerance.ripple_sum,
                                   "bw_limit_20mhz": project.tolerance.bw_limit_20mhz})
    for name, (rail, load) in loads_by_name(project).items():
        row = _row(project, tree, name, rail, load, reports.get(name), sources, pairs.get(name),
                   shown, by_noise, ac_notes, noise_notes, rail_source)
        out.rows.append(row)
        for c in row.checks.values():
            if c.passed is False:
                out.failed.append({"item": row.load, "net": row.net, "limit": c.limit,
                                   "reason": _fail_reason(c, row.floors)})
            elif c.limit and c.passed is None:
                out.not_judged.append({"item": row.load, "net": row.net, "limit": c.limit,
                                       "reason": c.reason})
    out.not_judged += [dict(e) for e in not_judged]
    out.verdict = ("fail" if out.failed else "not_judged" if out.not_judged else
                   "pass" if any(r.verdict == "pass" for r in out.rows) else "not_judged")
    out.verdict_note = _note(out.verdict, out.failed, out.not_judged)
    out.assumptions = assumptions(project, tree)
    nominal = {r.name: r.nominal_v for r in out.rows}
    out.warnings = list(dict.fromkeys(list(warnings) + mismatch_warnings(project, pairs, nominal)))
    return out


# ---------------------------------------------------------------------- summary and text

def _num(x) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _mv_num(x) -> Optional[float]:
    x = _num(x)
    return None if x is None else x * 1e3


def _part_summary(p: Optional[TolerancePart]) -> Optional[Dict[str, Any]]:
    if p is None:
        return None
    return {"below_mv": _mv_num(p.below_v), "above_mv": _mv_num(p.above_v), "source": p.source,
            "detail": p.detail}


def _check_summary(c: ToleranceCheck) -> Dict[str, Any]:
    return {"limit": c.limit, "below_mv": _mv_num(c.below_v), "above_mv": _mv_num(c.above_v),
            "allowed_below_mv": _mv_num(c.allowed_below_v), "allowed_above_mv": _mv_num(c.allowed_above_v),
            "margin_below_mv": _mv_num(c.margin_below_v), "margin_above_mv": _mv_num(c.margin_above_v),
            "complete": c.complete, "passed": c.passed, "missing": list(c.missing),
            "floors": list(c.floors), "reason": c.reason}


def _row_summary(r: ToleranceRow) -> Dict[str, Any]:
    e = r.transient_estimate_v
    return {"load": r.load, "name": r.name, "net": r.net, "rail": r.rail, "nominal_v": _num(r.nominal_v),
            "source": r.source, "probe": r.probe, "verdict": r.verdict,
            "parts": {k: _part_summary(r.parts.get(k)) for k in PARTS},
            "hints": {k: r.hints[k] for k in PARTS if k in r.hints},
            "floors": {k: {"below_mv": _mv_num(f.below_v), "above_mv": _mv_num(f.above_v), "source": f.source}
                       for k, f in r.floors.items()},
            "transient_estimate": None if e is None else {
                "below_mv": _mv_num(e[0]), "above_mv": _mv_num(e[1]), "below_20mhz_mv": _mv_num(e[2]),
                "above_20mhz_mv": _mv_num(e[3])},
            "checks": {k: _check_summary(r.checks[k]) for k in CHECKS if k in r.checks},
            "stand_ins": list(r.stand_ins)}


def tolerance_summary(result: ToleranceResult, csv_file: str = "") -> Dict[str, Any]:
    """The ``<project>_tolerance_summary.json`` document (units in every key; no arrays, no
    NaN): ``kind`` "tolerance", ``verdict``, ``verdict_note``, ``failed``, ``not_judged``,
    ``options``, ``warnings``, ``assumptions``, ``estimates``, ``stats``, ``csv_file``, then
    ``loads``."""
    return {"kind": "tolerance", "verdict": result.verdict, "verdict_note": result.verdict_note,
            "failed": [dict(e) for e in result.failed], "not_judged": [dict(e) for e in result.not_judged],
            "options": dict(result.options), "warnings": list(result.warnings),
            "assumptions": list(result.assumptions), "estimates": [dict(e) for e in result.estimates],
            "stats": dict(result.stats), "csv_file": csv_file,
            "loads": [_row_summary(r) for r in result.rows]}


def _side_text(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.4g}"


def tolerance_text(doc: Dict[str, Any]) -> List[str]:
    """The human-readable report of a ``tolerance`` document: per load its four parts (mV
    below / above nominal, where each came from), the three checks against what each allows,
    its verdict and what to do about a missing part; then the warnings, assumptions and the
    verdict."""
    lines: List[str] = []
    tags = {"pass": "PASS", "fail": "FAIL", "not_judged": "NOT JUDGED", "": "no tolerance set"}
    for ld in doc["loads"]:
        nominal = "no nominal" if ld["nominal_v"] is None else f"nominal {ld['nominal_v']:g} V"
        via = f" from {ld['source']}" if ld["source"] else ""
        probe = f", probe {ld['probe']}" if ld["probe"] else ""
        lines.append(f"\nLoad {ld['load']} on {ld['net']} ({nominal}{via}{probe}): {tags[ld['verdict']]}")
        for kind in PARTS:
            p = ld["parts"][kind]
            if p is None:
                f = ld["floors"].get(kind)
                least = (f" (at least {_side_text(f['below_mv'])} / {_side_text(f['above_mv'])} mV from "
                         f"{f['source']})" if f else "")
                lines.append(f"  {PART_NAMES[kind]:12s} missing{least}: {ld['hints'].get(kind, '')}")
            else:
                more = f"; {p['detail']}" if p["detail"] else ""
                lines.append(f"  {PART_NAMES[kind]:12s} {_side_text(p['below_mv'])} / "
                             f"{_side_text(p['above_mv'])} mV  {p['source']}{more}")
        for kind in CHECKS:
            c = ld["checks"][kind]
            if not c["limit"]:
                continue
            tag = {True: "PASS", False: "FAIL"}.get(c["passed"], "NOT JUDGED")
            lines.append(f"  check {CHECK_NAMES[kind]:5s} {_side_text(c['below_mv'])} / "
                         f"{_side_text(c['above_mv'])} mV of {_side_text(c['allowed_below_mv'])} / "
                         f"{_side_text(c['allowed_above_mv'])} allowed ({c['limit']}), margin "
                         f"{_side_text(c['margin_below_mv'])} / {_side_text(c['margin_above_mv'])} mV"
                         f"{'' if c['complete'] else ' (parts missing)'}  {tag}")
    for e in doc["failed"]:
        lines.append(f"FAIL {e['item']} ({e['limit']}): {e['reason']}")
    for e in doc["not_judged"]:
        lines.append(f"NOT JUDGED {e['item']} ({e['limit']}): {e['reason']}")
    lines += [f"assumption: {a}" for a in doc["assumptions"]]
    lines += [f"warning: {w}" for w in doc["warnings"]]
    if doc.get("csv_file"):
        lines.append(f"CSV: {doc['csv_file']}")
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    return lines


# ---------------------------------------------------------------------- CSV

def _cell(x) -> str:
    return "" if x is None or not math.isfinite(x) else f"{x:.9g}"


def write_csv(result: ToleranceResult, path) -> Path:
    """One row per load: ``load``, ``net``, ``nominal_v``; per part ``<part>_below_mv``,
    ``<part>_above_mv`` (empty when missing), ``<part>_source``; per check ``<check>_limit``,
    ``<check>_below_mv``, ``<check>_above_mv``, ``<check>_allowed_below_mv``,
    ``<check>_allowed_above_mv``, ``<check>_margin_below_mv``, ``<check>_margin_above_mv``,
    ``<check>_verdict`` ("pass", "fail", "not_judged"; empty without a tolerance); then
    ``verdict``."""
    path = Path(path)
    header = ["load", "net", "nominal_v"]
    for kind in PARTS:
        header += [f"{kind}_below_mv", f"{kind}_above_mv", f"{kind}_source"]
    for kind in CHECKS:
        header += [f"{kind}_{k}" for k in ("limit", "below_mv", "above_mv", "allowed_below_mv",
                                            "allowed_above_mv", "margin_below_mv", "margin_above_mv",
                                            "verdict")]
    header.append("verdict")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in result.rows:
            row: List[str] = [r.load, r.net, _cell(r.nominal_v)]
            for kind in PARTS:
                p = r.parts.get(kind)
                row += (["", "", ""] if p is None else
                        [_cell(p.below_v * 1e3), _cell(p.above_v * 1e3), p.source])
            for kind in CHECKS:
                c = r.checks[kind]
                mv = [None if x is None else x * 1e3 for x in (
                    c.below_v, c.above_v, c.allowed_below_v, c.allowed_above_v, c.margin_below_v,
                    c.margin_above_v)]
                verdict = "" if not c.limit else {True: "pass", False: "fail"}.get(c.passed, "not_judged")
                row += [c.limit, *[_cell(x) for x in mv], verdict]
            w.writerow(row + [r.verdict])
    return path
