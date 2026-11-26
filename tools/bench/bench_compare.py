"""Summaries and comparisons of benchmark runs, as aligned Markdown tables (readable in a
terminal as they are).

A run (``bench.py run``) is one JSON document: ``cases`` → ``steps`` → ``repeats``, each
repeat one fresh subprocess (see README.md). ``compare`` matches steps by (case kind, case,
step, target) and gives, per step, the time ratio (B over A, of the minimum and of the median
wall time), the memory ratios (of the median peak RSS, and of the step's RSS above the
opened board), the load the machine had, the adaptive choices that changed (pool workers,
field threads, solver method) and the fingerprint verdict.

Scalars are compared relative to the larger of the two values, floored per unit: a value
near zero (a drop of 1e-13 V on a rail with no current) is judged against UNIT_EPS times the
largest magnitude of that unit in the two fingerprints, never against its own round-off.
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any, Dict, List, Optional, Sequence

from bench_probes import PHASES

MB = 1e6
#: The relative floor of a scalar's comparison: this times the largest magnitude among the
#: scalars of the same unit (the name's suffix: _v, _a, _w, _ohm...) in both fingerprints.
UNIT_EPS = 1e-9
#: Unit suffixes of scalar names (the longest first); a name without one is a count.
UNITS = ("a_mm2", "dbuv_m", "m_s", "ohm", "bytes", "hz", "mm", "pct", "v", "a", "w", "f", "h", "c", "s")
#: Counts set by KiPI from the machine (spare cores, free memory): a change explains a time.
ADAPTIVE = ("pool_workers", "field_threads", "solver_method", "ac_factor_method")


def rel_diff(a: float, b: float) -> float:
    """|a - b| over the larger magnitude; 0 for equal values (NaN equal to NaN, inf to inf),
    inf when only one is NaN."""
    a, b = float(a), float(b)
    if math.isnan(a) or math.isnan(b):
        return 0.0 if math.isnan(a) and math.isnan(b) else math.inf
    if a == b:
        return 0.0
    if math.isinf(a) or math.isinf(b):
        return math.inf
    return abs(a - b) / max(abs(a), abs(b))


def stats(values: Sequence[Optional[float]]) -> Dict[str, Any]:
    """min, median, max and count of the values given (None left out)."""
    vs = [float(v) for v in values if v is not None]
    if not vs:
        return {"min": None, "median": None, "max": None, "n": 0}
    return {"min": min(vs), "median": statistics.median(vs), "max": max(vs), "n": len(vs)}


def _ratio(b: Optional[float], a: Optional[float]) -> Optional[float]:
    return b / a if a and b is not None else None


def _ok(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in step.get("repeats", []) if not r.get("error")]


def _errors(step: Dict[str, Any]) -> List[str]:
    return [r["error"] for r in step.get("repeats", []) if r.get("error")]


def _values(reps, key) -> List[Optional[float]]:
    return [r.get(key) for r in reps]


def _triangles(reps) -> set:
    return {(r.get("counts") or {}).get("triangles") for r in reps}


def _hashes(reps) -> set:
    return {(r.get("fingerprint") or {}).get("hash") for r in reps}


def unit_of(key: str) -> str:
    """The unit suffix of a scalar's name ("load[U1].v_min_v" -> "v"); "" for a count."""
    field = re.sub(r"\[[^\]]*\]", "", key).rsplit(".", 1)[-1]
    for unit in UNITS:
        if field.endswith("_" + unit):
            return unit
    return ""


def scalar_diffs(sa: Dict[str, float], sb: Dict[str, float]) -> List[Dict[str, Any]]:
    """The keys both fingerprints have that differ, the largest relative difference first:
    key, a, b, abs and rel = |a - b| / max(|a|, |b|, UNIT_EPS x the unit's largest magnitude)."""
    scale: Dict[str, float] = {}
    for k, v in list(sa.items()) + list(sb.items()):
        if v is not None and math.isfinite(v):
            u = unit_of(k)
            scale[u] = max(scale.get(u, 0.0), abs(v))
    out = []
    for k in sorted(set(sa) & set(sb)):
        a, b = float(sa[k]), float(sb[k])
        d = rel_diff(a, b)
        if d == 0.0:
            continue
        if math.isfinite(d):
            d = abs(a - b) / max(abs(a), abs(b), UNIT_EPS * scale.get(unit_of(k), 0.0))
        out.append({"key": k, "a": a, "b": b, "abs": abs(a - b) if math.isfinite(d) else math.inf, "rel": d})
    out.sort(key=lambda x: -x["rel"])
    return out


def _pair_diffs(ra, rb) -> List[Dict[str, Any]]:
    """Every repeat of A against every repeat of B: per key, the largest difference."""
    worst: Dict[str, Dict[str, Any]] = {}
    for x in ra:
        for y in rb:
            sa = (x.get("fingerprint") or {}).get("scalars") or {}
            sb = (y.get("fingerprint") or {}).get("scalars") or {}
            for d in scalar_diffs(sa, sb):
                if d["key"] not in worst or d["rel"] > worst[d["key"]]["rel"]:
                    worst[d["key"]] = d
    return sorted(worst.values(), key=lambda d: -d["rel"])


def _keys(reps) -> set:
    return {k for r in reps for k in ((r.get("fingerprint") or {}).get("scalars") or {})}


def fingerprint_verdict(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]],
                        diffs: Optional[List[Dict[str, Any]]] = None) -> str:
    """"identical" only when both steps ran without error, with the same triangle count, and
    every repeat of both has the same hash; otherwise what differs (``diffs``: every repeat
    of A against every repeat of B, as :func:`_pair_diffs`)."""
    if a is None:
        return "missing in A"
    if b is None:
        return "missing in B"
    if a.get("skipped") and b.get("skipped"):
        return "skipped (both)"
    for side, step in (("A", a), ("B", b)):
        if step.get("skipped"):
            return f"skipped in {side}: {step['skipped']}"
    for side, step in (("A", a), ("B", b)):
        errs = _errors(step)
        if errs or not _ok(step):
            return f"error in {side}: {errs[0] if errs else 'no repeat'}"
    ra, rb = _ok(a), _ok(b)
    notes = [f"repeats differ in {side}" for side, reps in (("A", ra), ("B", rb)) if len(_hashes(reps)) > 1]
    ta, tb = _triangles(ra), _triangles(rb)
    hashes = _hashes(ra) | _hashes(rb)
    if not notes and ta == tb and len(hashes) == 1 and None not in hashes:
        return "identical"
    diffs = _pair_diffs(ra, rb) if diffs is None else diffs
    only_a, only_b = _keys(ra) - _keys(rb), _keys(rb) - _keys(ra)
    if ta != tb:
        notes.insert(0, "mesh differs (triangles " + "/".join(_fmt_count(t) for t in sorted(ta, key=str))
                     + " -> " + "/".join(_fmt_count(t) for t in sorted(tb, key=str)) + ")")
    if not diffs:
        notes.append("hash differs, scalars equal")
    else:
        notes.append(f"max rel diff {diffs[0]['rel']:.2e} ({diffs[0]['key']})")
    if only_a or only_b:
        notes.append(f"scalars only in A: {len(only_a)}, only in B: {len(only_b)}")
    return "; ".join(notes)


def _adaptive(reps) -> Dict[str, str]:
    out = {}
    for key in ADAPTIVE:
        vals = set()
        for r in reps:
            v = (r.get("counts") or {}).get(key)
            if v is not None:
                vals.update(map(str, v) if isinstance(v, list) else [str(v)])
        if vals:
            out[key] = "/".join(sorted(vals))
    return out


def _step_rss(reps) -> List[Optional[float]]:
    return [r["rss_peak_bytes"] - r["rss_after_open_bytes"]
            if r.get("rss_peak_bytes") is not None and r.get("rss_after_open_bytes") is not None else None
            for r in reps]


def compare_steps(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One row of ``compare``: the step's times, memory and fingerprint in A and B."""
    any_step = a or b or {}
    ra, rb = (_ok(a) if a else []), (_ok(b) if b else [])
    wa, wb = stats(_values(ra, "wall_s")), stats(_values(rb, "wall_s"))
    ma, mb = stats(_values(ra, "rss_peak_bytes")), stats(_values(rb, "rss_peak_bytes"))
    sa, sb = stats(_step_rss(ra)), stats(_step_rss(rb))
    xa, xb = stats(_values(ra, "rss_tree_peak_bytes")), stats(_values(rb, "rss_tree_peak_bytes"))
    la, lb = stats(_values(ra, "load_1m")), stats(_values(rb, "load_1m"))
    diffs = _pair_diffs(ra, rb) if ra and rb else []
    aa, ab = _adaptive(ra), _adaptive(rb)
    notes = [f"{k} {aa.get(k, '-')} -> {ab.get(k, '-')}" for k in ADAPTIVE if aa.get(k) != ab.get(k)]
    return {"case": any_step.get("case"), "step": any_step.get("step"), "target": any_step.get("target"),
            "wall_a": wa["min"], "wall_b": wb["min"], "wall_ratio": _ratio(wb["min"], wa["min"]),
            "wall_median_ratio": _ratio(wb["median"], wa["median"]),
            "rss_a": ma["median"], "rss_b": mb["median"], "rss_ratio": _ratio(mb["median"], ma["median"]),
            "step_rss_a": sa["median"], "step_rss_b": sb["median"],
            "step_rss_ratio": _ratio(sb["median"], sa["median"]),
            "tree_rss_ratio": _ratio(xb["max"], xa["max"]), "load_a": la["median"], "load_b": lb["median"],
            "fingerprint": fingerprint_verdict(a, b, diffs if ra and rb else None),
            "top_diffs": diffs[:3], "notes": "; ".join(notes)}


def step_key(step: Dict[str, Any]):
    return (step.get("case_kind"), step.get("case"), step.get("step"), step.get("target") or "")


def all_steps(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every step of a run, each knowing its case's kind."""
    return [{"case_kind": case.get("kind"), **s}
            for case in run.get("cases", []) for s in case.get("steps", [])]


def compare_runs(run_a: Dict[str, Any], run_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A row per step of either run, in A's order, then B's new steps; a step listed twice in a
    run is flagged, never compared."""
    rows = []
    sides = []
    for run in (run_a, run_b):
        seen: Dict[tuple, Dict[str, Any]] = {}
        dup = set()
        for s in all_steps(run):
            k = step_key(s)
            if k in seen:
                dup.add(k)
            seen[k] = s
        sides.append((seen, dup))
    (by_a, dup_a), (by_b, dup_b) = sides
    for k in list(by_a) + [k for k in by_b if k not in by_a]:
        if k in dup_a or k in dup_b:
            side = " and ".join(n for n, d in (("A", dup_a), ("B", dup_b)) if k in d)
            row = compare_steps(None, None)
            row.update(case=k[1], step=k[2], target=k[3] or None,
                       fingerprint=f"duplicate in {side}; not compared")
            rows.append(row)
        else:
            rows.append(compare_steps(by_a.get(k), by_b.get(k)))
    return rows


# ---------------------------------------------------------------------------------- formatting

def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """An aligned Markdown table: every column padded to its widest cell."""
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def line(r):
        return "| " + " | ".join(c.ljust(w) for c, w in zip(r, widths)) + " |"
    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(cells[0]), sep] + [line(r) for r in cells[1:]])


def _fmt_s(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{x:.3f}" if x < 10 else f"{x:.2f}" if x < 100 else f"{x:.1f}"


def _fmt_mb(x: Optional[float]) -> str:
    return "-" if x is None else f"{x / MB:,.0f}"


def _fmt_count(x: Optional[float]) -> str:
    return "-" if x is None else f"{int(x):,}"


def _fmt_ratio(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.2f}"


def _name(step: Dict[str, Any]) -> str:
    return step["step"] + (f" {step['target']}" if step.get("target") else "")


def run_rows(run: Dict[str, Any]) -> List[List[str]]:
    rows = []
    for step in all_steps(run):
        reps = _ok(step)
        errs = _errors(step)
        status = (step.get("skipped") or (f"error: {errs[0]}"[:60] if errs else "ok"))
        if reps and len(_hashes(reps)) > 1:
            status = "ok; repeats differ"
        missing = sorted({m for r in reps for m in (r.get("missing_stages") or [])})
        if missing:
            status += "; no call: " + ", ".join(missing)
        w = stats(_values(reps, "wall_s"))
        cpu = stats(_values(reps, "cpu_s"))
        rss = stats(_values(reps, "rss_peak_bytes"))
        tree_rss = stats(_values(reps, "rss_tree_peak_bytes"))
        counts = (reps[0].get("counts") or {}) if reps else {}
        rows.append([step["case"], _name(step), str(len(reps)), _fmt_s(w["min"]), _fmt_s(w["median"]),
                     _fmt_s(cpu["median"]), _fmt_mb(rss["median"]), _fmt_mb(tree_rss["max"]),
                     _fmt_count(counts.get("triangles")), _fmt_count(counts.get("unknowns")), status])
    return rows


RUN_HEADERS = ["case", "step", "n", "wall min s", "wall med s", "CPU med s", "RSS MB", "tree MB",
               "triangles", "unknowns", "status"]


def run_table(run: Dict[str, Any]) -> str:
    """The run's table: per step its repeats, wall (min, median), CPU (median), peak RSS of the
    measuring process (median) and of its process tree (sampled, max), and the mesh counts."""
    return table(RUN_HEADERS, run_rows(run))


def stage_table(run: Dict[str, Any]) -> str:
    """Per step, the median exclusive seconds of each timed stage (runtime wrapping, see
    bench_probes), what no stage covers ("other": wall - stages timed in the main thread), and
    the entry points' inclusive seconds. A stage seen in several threads ("[10 thr]") sums
    their seconds: it can exceed the wall time."""
    rows = []
    for step in all_steps(run):
        reps = _ok(step)
        if not reps:
            continue
        names = sorted({k for r in reps for k in (r.get("stages") or {})})
        parts = []
        for name in names:
            got = [(r.get("stages") or {}).get(name) or {} for r in reps]
            s = stats([g.get("seconds") for g in got])
            threads = max((g.get("threads") or 1) for g in got)
            if s["median"] is not None and s["median"] >= 0.0005:
                parts.append(f"{name} {_fmt_s(s['median'])}" + (f" [{threads} thr]" if threads > 1 else ""))
        phases = []
        for name in PHASES:
            s = stats([((r.get("stages") or {}).get(name) or {}).get("inclusive_s") for r in reps])
            if s["median"] is not None:
                phases.append(f"{name} {_fmt_s(s['median'])}")
        other = stats(_values(reps, "unattributed_s"))
        rows.append([step["case"], _name(step), _fmt_s(stats(_values(reps, "wall_s"))["median"]),
                     ", ".join(parts) or "-", _fmt_s(other["median"]), ", ".join(phases) or "-"])
    return table(["case", "step", "wall med s", "stages, exclusive (median s)", "other s",
                  "entry points, inclusive (median s)"], rows)


COMPARE_HEADERS = ["case", "step", "wall A s", "wall B s", "x wall (min)", "x wall (med)", "RSS A MB",
                   "RSS B MB", "x RSS", "step RSS A MB", "step RSS B MB", "x step RSS", "x tree RSS",
                   "load A/B", "fingerprint", "adaptive changes"]


def _fmt_load(a: Optional[float], b: Optional[float]) -> str:
    return "-" if a is None and b is None else f"{'-' if a is None else f'{a:.1f}'}/" \
        f"{'-' if b is None else f'{b:.1f}'}"


def compare_table(rows: List[Dict[str, Any]]) -> str:
    """One row per step; "step RSS" is the peak RSS above the RSS once the board was opened,
    "load" the machine's 1-minute load average when the repeats started (median)."""
    out = []
    for r in rows:
        name = (r["step"] or "") + (f" {r['target']}" if r.get("target") else "")
        out.append([r["case"], name, _fmt_s(r["wall_a"]), _fmt_s(r["wall_b"]), _fmt_ratio(r["wall_ratio"]),
                    _fmt_ratio(r["wall_median_ratio"]), _fmt_mb(r["rss_a"]), _fmt_mb(r["rss_b"]),
                    _fmt_ratio(r["rss_ratio"]), _fmt_mb(r.get("step_rss_a")), _fmt_mb(r.get("step_rss_b")),
                    _fmt_ratio(r.get("step_rss_ratio")), _fmt_ratio(r["tree_rss_ratio"]),
                    _fmt_load(r.get("load_a"), r.get("load_b")), r["fingerprint"], r.get("notes") or ""])
    return table(COMPARE_HEADERS, out)


def diff_details(rows: List[Dict[str, Any]]) -> str:
    """The largest scalar differences of each step whose fingerprint is not identical."""
    lines = []
    for r in rows:
        if not r.get("top_diffs"):
            continue
        name = f"{r['case']} {r['step']}" + (f" {r['target']}" if r.get("target") else "")
        lines.append(f"- {name}:")
        for d in r["top_diffs"]:
            lines.append(f"    {d['key']}: A {d['a']!r}, B {d['b']!r}, "
                         f"abs {d['abs']:.3g}, rel {d['rel']:.3g}")
    return "\n".join(lines)
