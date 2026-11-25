"""Summaries and comparisons of benchmark runs, as aligned Markdown tables (readable in a
terminal as they are).

A run (``bench.py run``) is one JSON document: ``cases`` → ``steps`` → ``repeats``, each
repeat one fresh subprocess (see README.md). ``compare`` matches steps by (case, step,
target) and gives, per step, the time ratio (B over A, of the minimum and of the median
wall time), the memory ratio (of the median peak RSS) and the fingerprint verdict.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Sequence

MB = 1e6


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


def scalar_diff(sa: Dict[str, float], sb: Dict[str, float]):
    """(largest relative difference over the keys both have, its key, keys only in A, only in B)."""
    worst, key = 0.0, None
    for k in sorted(set(sa) & set(sb)):
        d = rel_diff(sa[k], sb[k])
        if d > worst:
            worst, key = d, k
    return worst, key, sorted(set(sa) - set(sb)), sorted(set(sb) - set(sa))


def fingerprint_verdict(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> str:
    """"identical" only when both steps ran without error, with the same triangle count, and
    every repeat of both has the same hash; otherwise what differs."""
    if a is None:
        return "missing in A"
    if b is None:
        return "missing in B"
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
    sa = (ra[0].get("fingerprint") or {}).get("scalars") or {}
    sb = (rb[0].get("fingerprint") or {}).get("scalars") or {}
    worst, key, only_a, only_b = scalar_diff(sa, sb)
    if ta != tb:
        notes.insert(0, "mesh differs (triangles " + "/".join(_fmt_count(t) for t in sorted(ta, key=str))
                     + " -> " + "/".join(_fmt_count(t) for t in sorted(tb, key=str)) + ")")
    if worst == 0.0:
        notes.append("hash differs, scalars equal")
    else:
        notes.append(f"max rel diff {worst:.2e} ({key})")
    if only_a or only_b:
        notes.append(f"scalars only in A: {len(only_a)}, only in B: {len(only_b)}")
    return "; ".join(notes)


def compare_steps(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One row of ``compare``: the step's times, memory and fingerprint in A and B."""
    any_step = a or b or {}
    ra, rb = (_ok(a) if a else []), (_ok(b) if b else [])
    wa, wb = stats(_values(ra, "wall_s")), stats(_values(rb, "wall_s"))
    ma, mb = stats(_values(ra, "rss_peak_bytes")), stats(_values(rb, "rss_peak_bytes"))
    xa, xb = stats(_values(ra, "rss_tree_peak_bytes")), stats(_values(rb, "rss_tree_peak_bytes"))
    return {"case": any_step.get("case"), "step": any_step.get("step"), "target": any_step.get("target"),
            "wall_a": wa["min"], "wall_b": wb["min"], "wall_ratio": _ratio(wb["min"], wa["min"]),
            "wall_median_ratio": _ratio(wb["median"], wa["median"]),
            "rss_a": ma["median"], "rss_b": mb["median"], "rss_ratio": _ratio(mb["median"], ma["median"]),
            "tree_rss_ratio": _ratio(xb["max"], xa["max"]),
            "fingerprint": fingerprint_verdict(a, b)}


def step_key(step: Dict[str, Any]):
    return (step.get("case"), step.get("step"), step.get("target") or "")


def all_steps(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for case in run.get("cases", []) for s in case.get("steps", [])]


def compare_runs(run_a: Dict[str, Any], run_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A row per step of either run, in A's order, then B's new steps."""
    by_a = {step_key(s): s for s in all_steps(run_a)}
    by_b = {step_key(s): s for s in all_steps(run_b)}
    keys = list(by_a) + [k for k in by_b if k not in by_a]
    return [compare_steps(by_a.get(k), by_b.get(k)) for k in keys]


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
    """Per step, the median seconds of each timed stage (runtime wrapping, see bench_probes),
    and what no stage covers ("other": wall - stages timed in the main thread). A stage seen
    in several threads ("[10 thr]") sums their seconds: it can exceed the wall time."""
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
        other = stats(_values(reps, "unattributed_s"))
        rows.append([step["case"], _name(step), _fmt_s(stats(_values(reps, "wall_s"))["median"]),
                     ", ".join(parts) or "-", _fmt_s(other["median"])])
    return table(["case", "step", "wall med s", "stages (median s)", "other s"], rows)


COMPARE_HEADERS = ["case", "step", "wall A s", "wall B s", "x wall (min)", "x wall (med)", "RSS A MB",
                   "RSS B MB", "x RSS", "x tree RSS", "fingerprint"]


def compare_table(rows: List[Dict[str, Any]]) -> str:
    out = []
    for r in rows:
        name = (r["step"] or "") + (f" {r['target']}" if r.get("target") else "")
        out.append([r["case"], name, _fmt_s(r["wall_a"]), _fmt_s(r["wall_b"]), _fmt_ratio(r["wall_ratio"]),
                    _fmt_ratio(r["wall_median_ratio"]), _fmt_mb(r["rss_a"]), _fmt_mb(r["rss_b"]),
                    _fmt_ratio(r["rss_ratio"]), _fmt_ratio(r["tree_rss_ratio"]), r["fingerprint"]])
    return table(COMPARE_HEADERS, out)
