"""Coppernicus benchmark harness (dev tooling): time and memory of every step, each measured
in a fresh subprocess, with a fingerprint of its result. See README.md.

    uv run python tools/bench/bench.py run --out DIR [--label NAME] [--examples [NAME ...]]
        [--synthetic grid-50 grid-100 ...] [--project PATH] [--rail NET ...] [--aggressor NAME ...]
        [--steps open,mesh,dc,...] [--repeats 3] [--mesher pool|serial] [--timeout S]
        [--max-rss-gb G] [--allow-memory-risk]
    uv run python tools/bench/bench.py compare A.json B.json [--out FILE]
    uv run python tools/bench/bench.py report RUN.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import bench_compare as C  # noqa: E402
import bench_runner as R  # noqa: E402

SCHEMA = "coppernicus-bench/1"
STEPS = ("open", "mesh", "dc", "tree", "thermal", "ac_estimate", "ac", "noise_estimate", "noise", "tolerance")
#: Steps that may be heavy on a large board: on an external project only when listed (--steps).
HEAVY = ("tree", "thermal", "ac_estimate", "ac", "noise_estimate", "noise", "tolerance")
#: A heavy solve and the estimate printed before it (the CLI's estimate functions).
ESTIMATE_OF = {"ac": "ac_estimate", "noise": "noise_estimate"}
EXAMPLES = {
    "demo": "examples/demo/demo.coppernicus.yml",
    "demo-thermal": "examples/demo/demo-thermal.coppernicus.yml",
    "power_chain": "examples/power_chain/power_chain.coppernicus.yml",
    "pdn": "examples/pdn/pdn.coppernicus.yml",
    "noise": "examples/noise/noise.coppernicus.yml",
    "tolerance": "examples/tolerance/tolerance.coppernicus.yml",
    "fields": "examples/fields/fields.coppernicus.yml",
}
#: Steps of a synthetic case unless --steps says otherwise (AC and thermal grow fast).
SYNTHETIC_STEPS = ("open", "mesh", "dc", "tree")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "x"


def _say(text: str = "") -> None:
    print(text, flush=True)


def _mb(b: Optional[float]) -> str:
    return "-" if b is None else f"{b / 1e6:,.0f} MB"


class Runner:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out).resolve()
        self.label = args.label
        self.work = self.out / f"{self.label}.work"
        self.logs = self.out / f"{self.label}.logs"
        self.env = dict(os.environ)
        self.env.pop("KIPI_SERIAL", None)
        if args.mesher == "serial":            # KiPI's switch: as the frozen app (can_spawn False)
            self.env["KIPI_SERIAL"] = "1"
        self.env.setdefault("MPLBACKEND", "Agg")
        self.max_rss = args.max_rss_gb * 1e9 if args.max_rss_gb else None

    def child(self, case: Dict[str, Any], step: str, target: Optional[str], rep: int) -> Dict[str, Any]:
        sid = "-".join(_slug(p) for p in (case["name"], step, target or "", f"r{rep}") if p)
        tmp = self.work / f"{sid}.tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        spec = {"id": sid, "case": case, "step": step, "target": target, "tmp_dir": str(tmp),
                "pool": {"min_triangles": self.args.pool_min_triangles,
                         "min_spread": self.args.pool_min_spread}}
        rec = R.run_child(spec, self.work, self.logs / f"{sid}.log", timeout_s=self.args.timeout,
                          max_rss_bytes=self.max_rss, sample_s=self.args.sample_s, env=self.env)
        shutil.rmtree(tmp, ignore_errors=True)
        return rec

    def plan(self, case: Dict[str, Any]) -> Dict[str, Any]:
        rec = self.child(case, "plan", None, 0)
        if rec.get("error"):
            raise SystemExit(f"{case['name']}: cannot open the case: {rec['error']}")
        return rec["plan"]

    def steps_for(self, case: Dict[str, Any], plan: Dict[str, Any]) -> List[tuple]:
        """(step, target) in run order, from the plan, --steps, --rail and --aggressor."""
        a = self.args
        if a.steps:
            wanted = list(a.steps)
        elif case["kind"] == "project":         # external: light by default
            wanted = ["open", "mesh", "dc"] if a.rail else ["open"]
        elif case["kind"] == "synthetic":
            wanted = list(SYNTHETIC_STEPS)
        else:
            wanted = list(STEPS)
        self.auto = set()
        for heavy, est in ESTIMATE_OF.items():  # a heavy solve is never run without its estimate
            if heavy in wanted and est not in wanted:
                wanted.append(est)
                self.auto.add(est)              # measured once: there as the solve's gate
        rails = [r for r in plan["rails"] if not a.rail or r in a.rail]
        ac_rails = [r for r in plan["ac_rails"] if not a.rail or r in a.rail]
        aggressors = [g for g in plan["aggressors"] if not a.aggressor or g in a.aggressor]
        if a.rail:
            unknown = [r for r in a.rail if r not in plan["all_rails"]]
            if unknown:
                _say(f"  note: --rail {', '.join(unknown)} is not a rail of {case['name']} "
                     f"(rails: {', '.join(plan['all_rails']) or 'none'})")
        out = []
        for step in STEPS:
            if step not in wanted:
                continue
            if step in ("mesh", "dc"):
                out += [(step, r) for r in rails]
            elif step == "tree" and plan["tree"]:
                out.append((step, None))
            elif step == "thermal" and plan["tree"] and (plan["thermal_enabled"] or a.steps):
                out.append((step, None))      # listed: forced on, as solve --thermal does
            elif step in ("ac_estimate", "ac"):
                out += [(step, r) for r in ac_rails]
            elif step in ("noise_estimate", "noise"):
                out += [(step, g) for g in aggressors]
            elif step == "tolerance" and plan["tolerance"]:
                out.append((step, None))
            elif step == "open":
                out.append((step, None))
        return out

    def gate(self, case, step: str, target: Optional[str], plan, estimates) -> Optional[str]:
        """Print a heavy step's estimate before it runs; the reason to skip it, if any."""
        est = None
        if step == "thermal":
            est = plan.get("thermal_estimate")
        elif step in ESTIMATE_OF:
            est = estimates.get((ESTIMATE_OF[step], target))
            if est is None:
                return f"skipped: its estimate ({ESTIMATE_OF[step]}) did not run"
        elif step == "tree" and case["kind"] == "project":
            _say("    estimate: none exists for the power tree (every rail and ground at once)")
        elif step == "tolerance":
            _say("    estimate: the tolerance command prints its own (in the child's log)")
        if est is None:
            return None
        _say(f"    estimate: {est.get('text')}")
        if est.get("exceeds_memory") and not self.args.allow_memory_risk:
            return "skipped: its estimate could exceed this machine's memory (--allow-memory-risk runs it)"
        return None

    def run_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        _say(f"\n== {case['name']}")
        plan = self.plan(case)
        todo = self.steps_for(case, plan)
        _say(f"  rails {', '.join(plan['rails']) or '-'}; AC rails {', '.join(plan['ac_rails']) or '-'}; "
             f"aggressors {', '.join(plan['aggressors']) or '-'}; steps "
             + ", ".join(s + (f" {t}" if t else "") for s, t in todo))
        steps, estimates = [], {}
        for step, target in todo:
            entry = {"case": case["name"], "step": step, "target": target, "repeats": []}
            steps.append(entry)
            _say(f"  {step}{' ' + target if target else ''}")
            skip = self.gate(case, step, target, plan, estimates)
            if skip:
                entry["skipped"] = skip
                _say(f"    {skip}")
                continue
            n = 1 if step in self.auto else self.args.repeats
            for k in range(1, n + 1):
                rec = self.child(case, step, target, k)
                entry["repeats"].append(rec)
                _say("    " + _line(rec, k, n))
                if rec.get("estimate") and (step, target) not in estimates:
                    estimates[(step, target)] = rec["estimate"]
                if rec.get("error"):
                    break                        # the same error again tells nothing new
        return {"name": case["name"], "kind": case["kind"], "project": case.get("project"),
                "plan": plan, "steps": steps}


def _line(rec: Dict[str, Any], k: int, n: int) -> str:
    if rec.get("error"):
        return f"[{k}/{n}] ERROR {rec['error'][:300]}"
    c = rec.get("counts") or {}
    tris = f"tris {int(c['triangles']):,}" if c.get("triangles") else ""
    unk = f"unknowns {int(c['unknowns']):,}" if c.get("unknowns") else ""
    pool = f"pool {c['pool_workers']}" if c.get("pool_workers") else ""
    h = (rec.get("fingerprint") or {}).get("hash", "")[:10]
    parts = [f"[{k}/{n}]", f"wall {rec['wall_s']:.3f} s", f"cpu {rec['cpu_total_s']:.2f} s",
             f"RSS {_mb(rec.get('rss_peak_bytes'))}", f"tree {_mb(rec.get('rss_tree_peak_bytes'))}",
             tris, unk, pool, f"#{h}"]
    return "  ".join(p for p in parts if p)


def _cases(args) -> List[Dict[str, Any]]:
    cases = []
    if args.examples is not None:
        names = args.examples or list(EXAMPLES)
        for name in names:
            if name not in EXAMPLES:
                raise SystemExit(f"unknown example {name!r}; examples: {', '.join(EXAMPLES)}")
            cases.append({"name": name, "kind": "example", "project": str(ROOT / EXAMPLES[name])})
    for name in args.synthetic or ():
        import bench_synth
        bench_synth.parse(name)                  # a wrong name stops here, before any solve
        cases.append({"name": f"synthetic/{name}", "kind": "synthetic", "synthetic": name})
    for i, path in enumerate(args.project or (), start=1):
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"no such project file: {path}")
        # Named by position, never by its file name: run JSONs may be shared.
        cases.append({"name": f"project{i}", "kind": "project", "project": str(p)})
    return cases


def cmd_run(args) -> int:
    import bench_meta

    if args.examples is None and not args.synthetic and not args.project:
        args.examples = []                       # nothing named: every example
    if args.steps:
        args.steps = [s.strip() for part in args.steps for s in part.split(",") if s.strip()]
        bad = [s for s in args.steps if s not in STEPS]
        if bad:
            raise SystemExit(f"unknown step {', '.join(bad)}; steps: {', '.join(STEPS)}")
    cases = _cases(args)
    meta = bench_meta.run_metadata(ROOT)
    meta["mesher"] = args.mesher + (" (pool thresholds lowered)" if args.pool_min_triangles is not None
                                    or args.pool_min_spread is not None else "")
    args.label = args.label or (_dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                                + (f"-{meta['git_commit'][:7]}" if meta.get("git_commit") else ""))
    runner = Runner(args)
    runner.out.mkdir(parents=True, exist_ok=True)
    out_json = runner.out / f"{args.label}.json"
    _say(f"Run {args.label}: commit {(meta.get('git_commit') or '?')[:10]}"
         f"{' (dirty)' if meta.get('git_dirty') else ''}, KiPI {meta.get('kipi_version')}, "
         f"{meta.get('cpu_count')} CPUs, {_mb(meta.get('ram_bytes'))} RAM, load {meta.get('load_average')}, "
         f"mesher {args.mesher}, {args.repeats} repeat(s)")
    run = {"schema": SCHEMA, "label": args.label, "metadata": meta,
           "options": {"repeats": args.repeats, "mesher": args.mesher, "steps": args.steps,
                       "pool_min_triangles": args.pool_min_triangles, "pool_min_spread": args.pool_min_spread,
                       "rails": args.rail, "aggressors": args.aggressor, "timeout_s": args.timeout,
                       "max_rss_gb": args.max_rss_gb, "sample_s": args.sample_s},
           "cases": []}
    try:
        for case in cases:
            run["cases"].append(runner.run_case(case))
            out_json.write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")   # as it goes
    finally:
        if not args.keep_work:
            shutil.rmtree(runner.work, ignore_errors=True)
    run["finished"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    out_json.write_text(json.dumps(run, indent=1, default=str), encoding="utf-8")
    _say("\n" + C.run_table(run) + "\n\n" + C.stage_table(run))
    _say(f"\nRun: {out_json}")
    return 0


def _load(path) -> Dict[str, Any]:
    run = json.loads(Path(path).read_text(encoding="utf-8"))
    if run.get("schema") != SCHEMA:
        raise SystemExit(f"{path}: not a benchmark run ({SCHEMA})")
    return run


def cmd_compare(args) -> int:
    import bench_meta

    a, b = _load(args.a), _load(args.b)
    lines = [f"A: {a['label']} ({(a['metadata'].get('git_commit') or '?')[:10]}), "
             f"B: {b['label']} ({(b['metadata'].get('git_commit') or '?')[:10]}); ratios are B / A", ""]
    diffs = bench_meta.differences(a["metadata"], b["metadata"])
    if diffs:
        lines += ["Runs differ in: " + "; ".join(f"{k}: {va} -> {vb}" for k, va, vb in diffs), ""]
    lines.append(C.compare_table(C.compare_runs(a, b)))
    text = "\n".join(lines)
    _say(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


def cmd_report(args) -> int:
    run = _load(args.run)
    m = run["metadata"]
    _say(f"Run {run['label']}: commit {(m.get('git_commit') or '?')[:10]}, mesher {m.get('mesher')}, "
         f"{m.get('cpu_count')} CPUs, load {m.get('load_average')}\n")
    _say(C.run_table(run) + "\n\n" + C.stage_table(run))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bench.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="measure cases step by step, each repeat in a fresh process")
    p.add_argument("--out", required=True, help="folder for the run's JSON (<label>.json) and logs")
    p.add_argument("--label", help="the run's name (default: date-time and commit)")
    p.add_argument("--examples", nargs="*", metavar="NAME",
                   help=f"examples to run (default when no case is named: all): {', '.join(EXAMPLES)}")
    p.add_argument("--synthetic", nargs="+", metavar="CASE",
                   help="synthetic boards built in memory: grid-<side mm>[x<rails>], e.g. grid-25 grid-100")
    p.add_argument("--project", action="append", metavar="PATH",
                   help="an external project file (repeatable); by default only opened: name rails "
                        "with --rail (mesh and DC), heavy steps with --steps")
    p.add_argument("--rail", action="append", metavar="NET", help="only these rails (mesh, dc, AC)")
    p.add_argument("--aggressor", action="append", metavar="NAME", help="only these aggressors (noise)")
    p.add_argument("--steps", action="append", metavar="LIST",
                   help=f"steps to run, comma separated (default: every one that applies): {','.join(STEPS)}")
    p.add_argument("--repeats", type=int, default=3, help="fresh processes per step (default 3)")
    p.add_argument("--mesher", choices=("pool", "serial"), default="pool",
                   help="pool: KiPI meshes large boards' layers in worker processes, as a Python "
                        "install runs; serial: one process, as the frozen app runs (KIPI_SERIAL=1)")
    p.add_argument("--pool-min-triangles", type=int, metavar="N",
                   help="test knob: KiPI's pool starts from this many estimated triangles (KiPI: "
                        "1,500,000); recorded in the run, so its times are not KiPI's defaults")
    p.add_argument("--pool-min-spread", type=int, metavar="N",
                   help="test knob: ... and this many outside the largest layer (KiPI: 500,000)")
    p.add_argument("--timeout", type=float, default=None, help="seconds before a measurement is killed")
    p.add_argument("--max-rss-gb", type=float, default=None,
                   help="kill a measurement whose process tree holds more than this (sampled)")
    p.add_argument("--sample-s", type=float, default=0.25, help="process-tree memory sampling interval")
    p.add_argument("--allow-memory-risk", action="store_true",
                   help="run a heavy step even when its estimate says it could exceed this machine's memory")
    p.add_argument("--keep-work", action="store_true", help="keep the specs and step outputs (debugging)")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("compare", help="compare two runs step by step (time, memory, fingerprint)")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--out", help="also write the table to this file")
    p.set_defaults(func=cmd_compare)
    p = sub.add_parser("report", help="print a run's tables again")
    p.add_argument("run")
    p.set_defaults(func=cmd_report)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
