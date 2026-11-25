"""AC impedance results: verdicts, the summary document, text, CSV and PNG (no engine, no Qt).

A probe is judged only against a target the user gave (or one computed from the numbers
they gave), only above its source's control loop bandwidth (the solve is open loop), and
only where the result does not hang on something assumed: no bandwidth given, a capacitor
on the rail without a model, an assumed dielectric εr, or KiPI's fallback that would judge
the open-loop rise. Rules: ``docs/engine-mapping.md`` (Task 9, Verdicts).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from coppernicus.project import Probe
from coppernicus.results import ImpedanceResult, ProbeImpedance

OPEN_LOOP_LABEL = "open loop: the source's control loop holds the rail here"
MISSING_LISTED = 20                 # capacitors named in a reason (KiPI's log lists as many)
REL = 1e-9                          # frequency comparisons (logspace end points)


def hz_text(f: Optional[float]) -> str:
    """A frequency with 4 significant digits in Hz, kHz, MHz or GHz."""
    if f is None:
        return "-"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def ohm_text(z: Optional[float]) -> str:
    """An impedance with 4 significant digits in mΩ, Ω or kΩ."""
    if z is None or not math.isfinite(z):
        return "-"
    if z < 1:
        return f"{z * 1e3:.4g} mΩ"
    if z < 1e3:
        return f"{z:.4g} Ω"
    return f"{z / 1e3:.4g} kΩ"


# ---------------------------------------------------------------------- verdicts

def assumed_label(bandwidth_hz: float) -> str:
    """The plot's label for the band below an assumed bandwidth. KiPI's AC model has no
    regulator impedance, so |Z| does not depend on the value: only the judging does."""
    return (f"below the regulator's control-loop bandwidth (not given; {hz_text(bandwidth_hz)} "
            "assumed): not judged")


def worst_point(p: ProbeImpedance, r: ImpedanceResult) -> Tuple[Optional[float], Optional[float],
                                                                 Optional[float]]:
    """(|Z|, f, |Z|/target) where the impedance comes closest to, or furthest over, its
    target where the target applies; without one, the highest |Z| above the bandwidth, with
    no ratio (KiPI's rule, kipi/ac/solver/sweep.py:67-83). Without a bandwidth given, no
    worst point: the open-loop maximum would sit at the start of the sweep, and a target
    would be measured against an assumed bandwidth."""
    if r.bandwidth_hz is None:
        return None, None, None
    mag, f = p.mag_ohm, np.asarray(r.freq_hz)
    t = p.target_curve_ohm
    if t is not None and np.isfinite(t).any():
        sel = np.flatnonzero(np.isfinite(t))
        i = sel[int(np.argmax(mag[sel] / t[sel]))]
        return float(mag[i]), float(f[i]), float(mag[i] / t[i])
    sel = np.flatnonzero(f >= r.bandwidth_hz * (1 - REL))
    if not len(sel):                          # bandwidth above the sweep: KiPI takes it all
        sel = np.arange(len(f))
    i = sel[int(np.argmax(mag[sel]))]
    return float(mag[i]), float(f[i]), None


def _span(p: ProbeImpedance, bandwidth: float, f1: float) -> Tuple[float, float]:
    """Where the target must be judged for a pass: from the bandwidth (or the mask's first
    point, if later) to ``target_hz``, the mask's last point, or the end of the sweep. A mask
    applies only between its first and last points."""
    if p.target_points:
        pts = sorted(p.target_points)
        return max(bandwidth, pts[0][0]), pts[-1][0]
    return bandwidth, p.target_hz if p.target_hz else f1


def probe_verdict(p: ProbeImpedance, r: ImpedanceResult) -> Tuple[Optional[bool], str]:
    """(passed, why not judged): ``(None, "")`` without a target; ``(None, reason)`` when it
    cannot be judged; else ``True`` or ``False``."""
    if not p.has_target:
        return None, ""
    reasons = []
    if r.bandwidth_hz is None:
        reasons.append(f"no control loop bandwidth is set for {r.source} (ac.bandwidth_hz): "
                       "below it the open-loop |Z| is not what the rail sees, so no target is "
                       "judged")
    elif p.fallback_note:
        reasons.append(f"{p.fallback_note}, which leaves no frequency to judge; KiPI's fallback "
                       "(judging the target over its whole range, the open-loop rise "
                       "included) is not used")
    if r.missing_caps:
        listed = ", ".join(r.missing_caps[:MISSING_LISTED])
        more = "…" if len(r.missing_caps) > MISSING_LISTED else ""
        reasons.append(f"{len(r.missing_caps)} capacitor(s) between {' / '.join(r.rail_nets)} and "
                       f"{r.ground} have no model, so the resonances may move: {listed}{more}")
    if r.dielectric_assumed:
        reasons.append(f"the dielectric εr is assumed: {r.dielectric_assumed}")
    if reasons:
        return None, "; ".join(reasons)
    t = p.target_curve_ohm
    sel = np.isfinite(t) if t is not None else np.zeros(len(r.freq_hz), dtype=bool)
    if sel.any() and bool(np.any(p.mag_ohm[sel] > t[sel])):
        return False, ""
    f0, f1 = float(r.freq_hz[0]), float(r.freq_hz[-1])
    bw = r.bandwidth_hz
    lo, hi = _span(p, bw, f1)
    if lo > hi * (1 + REL):
        what = "the target mask" if p.target_points else "the target"
        return None, (f"{what} ends at {hz_text(hi)}, entirely below the bandwidth, {hz_text(bw)}: "
                      "not judged")
    if not sel.any():
        return None, (f"the target applies nowhere in the sweep {hz_text(f0)} to {hz_text(f1)} "
                      f"above the bandwidth {hz_text(bw)}")
    if not (lo >= f0 * (1 - REL) and hi <= f1 * (1 + REL)):
        fixes = []
        if lo < f0 * (1 - REL):
            fixes.append(f"lower ac.f_start_hz to {'the bandwidth, ' if lo == bw else ''}{hz_text(lo)}")
        if hi > f1 * (1 + REL):
            fixes.append(f"raise ac.f_stop_hz to {hz_text(hi)}")
        return None, (f"the target applies from {hz_text(lo)} to {hz_text(hi)}, not inside the "
                      f"sweep {hz_text(f0)} to {hz_text(f1)}: it holds where solved, but not all "
                      f"of it was solved; {' and '.join(fixes)} to judge it")
    return True, ""


def judge(r: ImpedanceResult) -> ImpedanceResult:
    """Fill in each probe's worst point and verdict, in place."""
    for p in r.probes:
        p.worst_z_ohm, p.worst_hz, p.worst_ratio = worst_point(p, r)
        p.passed, p.not_judged = probe_verdict(p, r)
    return r


def _fail_reason(p: ProbeImpedance) -> str:
    target = (p.worst_z_ohm / p.worst_ratio) if p.worst_ratio else None
    return (f"|Z| {ohm_text(p.worst_z_ohm)} at {hz_text(p.worst_hz)}, above the target "
            f"{ohm_text(target)} there ({p.worst_ratio:.3g}× it)")


def rail_verdict(r: ImpedanceResult) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(verdict, failed entries, not-judged entries) of one rail. A rail fails if a probe
    fails; else it is not judged if a probe with a target was not judged; else it passes if
    a probe was judged; with no target at all, not judged (and no entry: only ranked)."""
    failed = [{"item": p.name, "net": p.net, "limit": p.limit, "reason": _fail_reason(p)}
              for p in r.probes if p.passed is False]
    not_judged = [{"item": p.name, "net": p.net, "limit": p.limit, "reason": p.not_judged}
                  for p in r.probes if p.has_target and p.passed is None]
    return _combine([("fail" if failed else "pass" if any(p.passed for p in r.probes) else "",
                      bool(not_judged))]), failed, not_judged


def _combine(items: Sequence[Tuple[str, bool]]) -> str:
    """``fail`` if one fails; else ``not_judged`` if one has a target not judged; else
    ``pass`` if one passed; else ``not_judged`` (nothing to judge)."""
    if any(v == "fail" for v, _ in items):
        return "fail"
    if any(nj for _, nj in items):
        return "not_judged"
    return "pass" if any(v == "pass" for v, _ in items) else "not_judged"


def unsolved_entries(probes: Sequence[Probe], rails_of: Dict[str, Optional[str]]
                     ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The probes no solved rail reaches (``rails_of``: probe name -> its rail or None): a
    not-judged entry for each one with a target (entered, or the three numbers that compute
    it), a warning for each one without."""
    entries, warnings = [], []
    for p in probes:
        if rails_of.get(p.name) is not None:
            continue
        why = (f"no rail with a source reaches {p.net} (directly or through links), so it was "
               "not solved")
        if p.target_points or p.target_ohm or (p.ac_tolerance_v and p.step_a and p.slew_a_per_us):
            entries.append({"item": p.name, "net": p.net,
                            "limit": "target_points" if p.target_points else "target_ohm",
                            "reason": why, "rail": ""})
        else:
            warnings.append(f"Probe {p.name}: {why}")
    return entries, warnings


def ac_verdict(results: Sequence[ImpedanceResult],
               not_solved: Sequence[Dict[str, Any]] = ()) -> Tuple[str, str]:
    """(verdict, note) of a project's rails, combined as one rail's probes are; a probe with a
    target that was not solved (``not_solved``, see :func:`unsolved_entries`) is not judged."""
    per = [rail_verdict(r) for r in results]
    verdict = _combine([(v, bool(nj)) for v, _f, nj in per] + [("", bool(not_solved))])
    fails = [f"{e['item']} on {e['net']} ({e['limit']}): {e['reason']}" for _v, f, _nj in per for e in f]
    unjudged = [f"{e['item']} on {e['net']} ({e['limit']}): {e['reason']}"
                for _v, _f, nj in per for e in nj]
    unjudged += [f"{e['item']} on {e['net']} ({e['limit']}): {e['reason']}" for e in not_solved]
    if verdict == "fail":
        note = "Fail: " + "; ".join(fails)
    elif unjudged:
        note = "Not judged: " + "; ".join(unjudged)
    elif verdict == "pass":
        note = ("Pass: every target set holds where it applies, above each source's control "
                "loop bandwidth")
    else:
        note = "No target set: the probes are ranked, not judged"
    return verdict, note


# ---------------------------------------------------------------------- summary and text

def _num(x) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _probe_summary(p: ProbeImpedance) -> Dict[str, Any]:
    mag = p.mag_ohm
    return {"name": p.name, "net": p.net, "ground_net": p.ground_net,
            "z_min_ohm": _num(np.min(mag)), "z_max_ohm": _num(np.max(mag)),
            "worst_z_ohm": _num(p.worst_z_ohm), "worst_hz": _num(p.worst_hz),
            "worst_ratio": _num(p.worst_ratio), "target_ohm": _num(p.target_ohm),
            "target_hz": _num(p.target_hz),
            "target_points": [[float(f), float(z)] for f, z in p.target_points],
            "target_note": p.target_note, "judged_from_hz": _num(p.judged_from_hz),
            "passed": p.passed, "step_a": _num(p.step_a), "slew_a_per_us": _num(p.slew_a_per_us)}


def rail_summary(r: ImpedanceResult, csv_file: str = "", png_file: str = "") -> Dict[str, Any]:
    """One rail of the ``ac_impedance`` document (units in every key)."""
    verdict, _failed, not_judged = rail_verdict(r)
    s = r.stats
    return {
        "rail": r.rail, "rail_nets": list(r.rail_nets), "ground": r.ground, "source": r.source,
        "bandwidth_hz": _num(r.bandwidth_hz), "bandwidth_assumed": r.bandwidth_assumed,
        "assumed_bandwidth_hz": _num(r.assumed_bandwidth_hz),
        "f_start_hz": float(r.freq_hz[0]), "f_stop_hz": float(r.freq_hz[-1]),
        "sweep_points": len(r.freq_hz), "field_points": len(r.field_freq_hz),
        "plane_capacitance_f": float(r.plane_capacitance_f),
        "caps_modelled": list(r.caps), "caps_missing": list(r.missing_caps), "links": list(r.links),
        "verdict": verdict, "not_judged": not_judged,
        "csv_file": csv_file, "png_file": png_file,
        "assumptions": list(r.assumptions), "warnings": list(r.warnings),
        "stats": {"seconds": _num(s.get("seconds")), "triangles": int(s.get("triangles", 0)),
                  "unknowns": int(s.get("unknowns", 0)), "ports": int(s.get("ports", 0)),
                  "method": str(s.get("method", "")),
                  "memory_peak_mb": _num(s.get("memory_peak_mb")),
                  "memory_peak_raised": s.get("memory_peak_raised")},
        "probes": [_probe_summary(p) for p in r.probes],
    }


def ac_summary(results: Sequence[ImpedanceResult],
               files: Optional[Dict[str, Tuple[str, str]]] = None,
               not_solved: Sequence[Dict[str, Any]] = (), warnings: Sequence[str] = ()) -> Dict[str, Any]:
    """The ``<project>_ac_summary.json`` document: ``kind``, ``verdict``, ``verdict_note``,
    ``failed`` and ``not_judged`` (each entry with its ``rail``; "" for a probe no rail
    reaches, ``not_solved``), ``warnings`` (project level), then ``rails``. ``files``: rail ->
    (CSV path, PNG path)."""
    files = files or {}
    verdict, note = ac_verdict(results, not_solved)
    failed, not_judged = [], []
    for r in results:
        _v, f, nj = rail_verdict(r)
        failed += [{**e, "rail": r.rail} for e in f]
        not_judged += [{**e, "rail": r.rail} for e in nj]
    return {"kind": "ac_impedance", "verdict": verdict, "verdict_note": note, "failed": failed,
            "not_judged": not_judged + [dict(e) for e in not_solved], "warnings": list(warnings),
            "rails": [rail_summary(r, *files.get(r.rail, ("", ""))) for r in results]}


def ac_text(doc: Dict[str, Any]) -> List[str]:
    """The human-readable report of an ``ac_impedance`` document."""
    lines: List[str] = []
    for rail in doc["rails"]:
        others = [n for n in rail["rail_nets"] if n != rail["rail"]]
        joined = f" (with {', '.join(others)})" if others else ""
        bw = (f"bandwidth not set ({hz_text(rail['assumed_bandwidth_hz'])} assumed for the plot only)"
              if rail["bandwidth_assumed"] else f"bandwidth {hz_text(rail['bandwidth_hz'])}")
        st = rail["stats"]
        lines.append(f"\nRail {rail['rail']}{joined} against {rail['ground']}: source "
                     f"{rail['source']}, {bw}")
        lines.append(f"  solved in {st['seconds']:.1f} s: {st['triangles']:,} triangles, "
                     f"{st['unknowns']:,} unknowns, {st['ports']} ports ({st['method']}); sweep "
                     f"{hz_text(rail['f_start_hz'])} to {hz_text(rail['f_stop_hz'])}, "
                     f"{rail['sweep_points']} points, copper solved at {rail['field_points']}")
        lines.append(f"  plane capacitance {rail['plane_capacitance_f'] * 1e9:.4g} nF; capacitors "
                     f"modelled {len(rail['caps_modelled'])}, without a model "
                     f"{len(rail['caps_missing'])}; links {', '.join(rail['links']) or 'none'}")
        for p in rail["probes"]:
            worst = (f"; worst {ohm_text(p['worst_z_ohm'])} at {hz_text(p['worst_hz'])}"
                     + (f" ({p['worst_ratio']:.3g}× target)" if p["worst_ratio"] is not None else "")
                     if p["worst_z_ohm"] is not None else "")
            tag = {True: "  PASS", False: "  FAIL"}.get(p["passed"], "")
            lines.append(f"  probe {p['name']}: |Z| {ohm_text(p['z_min_ohm'])} to "
                         f"{ohm_text(p['z_max_ohm'])}{worst}{tag}")
        for u in rail["not_judged"]:
            lines.append(f"  NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
        for a in rail["assumptions"]:
            lines.append(f"  assumption: {a}")
        for w in rail["warnings"]:
            lines.append(f"  warning: {w}")
        for key in ("csv_file", "png_file"):
            if rail[key]:
                lines.append(f"    {key[:3]}: {rail[key]}")
    for u in doc["not_judged"]:
        if not u["rail"]:
            lines.append(f"NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
    lines += [f"warning: {w}" for w in doc.get("warnings", [])]
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    return lines


# ---------------------------------------------------------------------- CSV and PNG

def _cell(x) -> str:
    return "" if x is None or not math.isfinite(x) else f"{x:.9g}"


def write_csv(r: ImpedanceResult, path) -> Path:
    """``freq_hz``, then per probe ``<p>_re_ohm``, ``<p>_im_ohm``, ``<p>_mag_ohm``,
    ``<p>_target_ohm`` (empty where no target applies), then on every row ``bandwidth_hz`` (as
    given; empty when not), ``assumed_bandwidth_hz`` (the placeholder; empty when given) and
    ``bandwidth_assumed``."""
    path = Path(path)
    header = ["freq_hz"]
    for p in r.probes:
        header += [f"{p.name}_re_ohm", f"{p.name}_im_ohm", f"{p.name}_mag_ohm", f"{p.name}_target_ohm"]
    header += ["bandwidth_hz", "assumed_bandwidth_hz", "bandwidth_assumed"]
    tail = [_cell(r.bandwidth_hz), _cell(r.assumed_bandwidth_hz),
            "true" if r.bandwidth_assumed else "false"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for i, f in enumerate(r.freq_hz):
            row = [_cell(float(f))]
            for p in r.probes:
                z = complex(p.z_ohm[i])
                t = p.target_curve_ohm[i] if p.target_curve_ohm is not None else None
                row += [_cell(z.real), _cell(z.imag), _cell(abs(z)), _cell(t)]
            w.writerow(row + tail)
    return path


COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#7f7f7f")


def draw_impedance(ax, r: ImpedanceResult) -> None:
    """|Z|(f) per probe on log-log axes, each target where it applies, the band below the
    bandwidth (open loop; hatched when the bandwidth is assumed), each worst point, and the
    rail's verdict as the title."""
    f = np.asarray(r.freq_hz)
    bw = r.open_loop_hz
    if bw is not None and bw > f[0]:
        if r.bandwidth_assumed:
            ax.axvspan(f[0], min(bw, f[-1]), facecolor="#f2c94c", alpha=0.25, hatch="//",
                       edgecolor="#b8860b", lw=0, label=assumed_label(bw))
        else:
            ax.axvspan(f[0], min(bw, f[-1]), color="0.85", alpha=0.6, lw=0, label=OPEN_LOOP_LABEL)
    for k, p in enumerate(r.probes):
        c = COLORS[k % len(COLORS)]
        ax.loglog(f, p.mag_ohm, color=c, lw=1.4, label=f"|Z| {p.name}")
        if p.target_curve_ohm is not None and np.isfinite(p.target_curve_ohm).any():
            ax.loglog(f, p.target_curve_ohm, color=c, lw=1.2, ls="--", label=f"target {p.name}")
        if p.worst_z_ohm is not None:
            ax.plot([p.worst_hz], [p.worst_z_ohm], marker="o", ms=6, mfc="none", mec=c, mew=1.5)
    verdict, _f, _nj = rail_verdict(r)
    words = {"fail": "FAIL", "pass": "PASS", "not_judged": "not judged"}[verdict]
    flag = (f"; bandwidth not given ({hz_text(bw)} assumed)" if r.bandwidth_assumed else "")
    ax.set_title(f"{r.rail} against {r.ground}: PDN impedance (open loop), {words}{flag}", fontsize=10)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("|Z| (Ω)")
    ax.set_xlim(f[0], f[-1])
    ax.grid(True, which="both", lw=0.3, color="0.8")
    ax.legend(fontsize=7, loc="best")


def write_png(r: ImpedanceResult, path, dpi: int = 120) -> Path:
    """The |Z|(f) plot of :func:`draw_impedance` as a PNG (headless)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    path = Path(path)
    fig = Figure(figsize=(8.0, 5.0), dpi=dpi)
    FigureCanvasAgg(fig)
    draw_impedance(fig.add_subplot(1, 1, 1), r)
    fig.tight_layout()
    fig.savefig(path)
    return path
