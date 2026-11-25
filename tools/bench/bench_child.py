"""One measurement in a fresh process (started by bench_runner):

    python bench_child.py SPEC.json OUT.json

The spec names a case (a project file, or a synthetic board built in memory), a step and its
target (a rail, an aggressor). The child installs the stage probes (bench_probes), opens the
case as the CLI does (project file, board, the KiCad footprint fields merged), then runs the
step alone, timed: wall, CPU of this process (all its threads) and of its reaped worker
processes, peak RSS of this process and of its largest worker, the stages, the counts and
the result's fingerprint. Errors are recorded, never raised: the record is always written.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

#: The steps, in the order a run takes them.
STEPS = ("open", "mesh", "dc", "tree", "thermal", "ac_estimate", "ac", "noise_estimate", "noise", "tolerance")


@dataclass
class Case:
    path: Optional[Path]          # the project file; None for a synthetic board
    loaded: Any                   # the project as loaded, before the KiCad fields
    project: Any                  # merged with the fields (what every solve uses)
    board: Any
    merged: Any


def _children_usage():
    """(CPU seconds, peak RSS bytes of the largest) of this process's reaped children; POSIX."""
    try:
        import resource
    except ImportError:
        return None, None
    from bench_runner import maxrss_bytes
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime, (maxrss_bytes(ru.ru_maxrss) or None)


def _reap_workers(limit_s: float = 30.0) -> int:
    """Wait for KiPI's pool workers to exit (the pool shuts down without waiting), so their
    CPU and memory count in this process's children; how many were still running."""
    deadline = time.perf_counter() + limit_s
    left = multiprocessing.active_children()
    while left and time.perf_counter() < deadline:
        time.sleep(0.05)
        left = multiprocessing.active_children()
    return len(left)


def open_case(spec: Dict[str, Any], rec=None) -> Case:
    from coppernicus import board_fields
    from coppernicus.board_fields import BoardFields
    from coppernicus.engine import Board
    from coppernicus.engine.kicad_fields import read_board_fields
    from coppernicus.project import Project

    case = spec["case"]
    if case["kind"] == "synthetic":
        import contextlib

        import bench_synth
        with rec.span("synthetic_build") if rec is not None else contextlib.nullcontext():
            board, loaded = bench_synth.build(case["synthetic"])
        path = None
    else:
        path = Path(case["project"])
        loaded = Project.load(path)
        board = Board.open(loaded.board_path())
    merged = board_fields.apply(
        loaded, lambda: read_board_fields(board.path) if board.path is not None else BoardFields(),
        None, ties=[(t.net_a, t.net_b) for t in board.net_ties()])
    return Case(path, loaded, merged.project, board, merged)


# ----------------------------------------------------------------------------------- steps

def _rail(case: Case, net: str):
    rail = case.project.rail(net)
    if rail is None:
        raise ValueError(f"no rail {net} in the project (after the KiCad fields)")
    return rail


def step_mesh(case: Case, spec, rec):
    """The rail's mesh alone: the DC solve's own call (solve_rail), stopped once meshed."""
    import bench_fingerprint as F
    import bench_probes as P

    from coppernicus.engine import solve_rail
    rec.stop_after_mesh = True
    try:
        solve_rail(case.board, _rail(case, spec["target"]), case.project.settings, project=case.project)
    except P.StopAfterMesh as stop:
        return F.mesh(stop.mesh), {}
    finally:
        rec.stop_after_mesh = False
    raise RuntimeError("the rail solved without meshing through kipi's mesh_nets")


def step_dc(case: Case, spec, rec):
    """One rail as ``coppernicus solve --rail`` solves it (Session._solve): solve_rail with the
    project, then its problems; no PNG maps, no HTML."""
    import bench_fingerprint as F

    from coppernicus.engine import solve_rail
    from coppernicus.problems import find_problems
    rail = _rail(case, spec["target"])
    res = solve_rail(case.board, rail, case.project.settings, project=case.project)
    find_problems(res, rail.limits, top=5)
    return F.rail(res), {}


def _tree(case: Case, spec, rec, thermal: bool):
    """The power tree as ``solve --tree`` (``--thermal``) solves it, with its summary; no maps."""
    import bench_fingerprint as F

    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.power_tree import rating_origins, tree_summary
    tree = solve_project_tree(case.board, case.project, thermal=thermal)
    with rec.span("summary"):
        case.merged.annotate(tree)
        rating_origins(tree, case.merged)
        tree_summary(tree, top=5)
    extra = {"tree_seconds": tree.stats.get("seconds")}
    return F.tree(tree), extra


def step_tree(case: Case, spec, rec):
    return _tree(case, spec, rec, thermal=False)


def step_thermal(case: Case, spec, rec):
    return _tree(case, spec, rec, thermal=True)


def step_ac_estimate(case: Case, spec, rec):
    import bench_fingerprint as F

    from coppernicus.service.session import ac_estimate
    est = ac_estimate(case.board, case.project, spec["target"])
    return F.estimate(est), {"estimate": est}


def step_ac(case: Case, spec, rec):
    """``coppernicus ac --rail``'s solve (no CSV, PNG or HTML written)."""
    import bench_fingerprint as F

    from coppernicus.engine.ac import solve_ac_rail
    res = solve_ac_rail(case.board, case.project, spec["target"])
    case.merged.annotate(res)
    return F.impedance(res), {"method_note": res.stats.get("method")}


def step_noise_estimate(case: Case, spec, rec):
    import bench_fingerprint as F

    from coppernicus.service.session import noise_estimate
    est = noise_estimate(case.board, case.project, spec["target"], fields_rails=case.merged.from_fields)
    return F.estimate(est), {"estimate": est}


def step_noise(case: Case, spec, rec):
    """``coppernicus noise --aggressor``'s solve (no CSV or PNG written)."""
    import bench_fingerprint as F

    from coppernicus.engine.noise import solve_aggressor
    res = solve_aggressor(case.board, case.project, spec["target"], fields_rails=case.merged.from_fields)
    case.merged.annotate(res)
    return F.noise(res), {}


def step_tolerance(case: Case, spec, rec):
    """``coppernicus tolerance PROJECT --json --out TMP`` itself (it opens the project again,
    as a user's run does): the power tree, then the AC and noise solves it needs, each
    estimate first. Its result is caught from ``coppernicus.tolerance.compose``."""
    import contextlib
    import io

    import bench_fingerprint as F

    import coppernicus.tolerance as T
    from coppernicus import cli
    if case.path is None:
        raise ValueError("the tolerance step runs the CLI on a project file")
    caught = {}
    original = T.compose

    def compose(*args, **kwargs):
        caught["result"] = out = original(*args, **kwargs)
        return out
    T.compose = compose
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["tolerance", str(case.path), "--json", "--out", spec["tmp_dir"]])
    finally:
        T.compose = original
    if code == 2 or "result" not in caught:
        raise RuntimeError(f"coppernicus tolerance exited with {code}: {stdout.getvalue()[-300:]}")
    return F.tolerance(caught["result"]), {"exit_code": code,
                                           "estimates": caught["result"].estimates}


def step_plan(case: Case, spec, rec):
    """What can be measured on this case: rails to solve one by one, the power tree, the board
    temperature (with its estimate), AC rails, aggressors, the tolerance stack-up."""
    import bench_fingerprint as F

    from coppernicus.engine import tied_nets
    from coppernicus.engine.ac import probe_rails
    from coppernicus.power_tree import EstimateError, estimated_draws
    from coppernicus.service.session import thermal_estimate
    p, board = case.project, case.board
    ties = [(t.net_a, t.net_b) for t in board.net_ties()]

    def solvable(rail) -> bool:
        if not rail.sources or rail.net not in board.nets():
            return False
        if rail.loads:
            return True
        nets = tied_nets(board, rail.net)[0] if rail.follow_net_ties else [rail.net]
        try:
            return bool(estimated_draws(p, nets, ties))
        except EstimateError:
            return True                       # the solve says why
    ac_rails, ac_note = [], ""
    if p.probes:
        try:
            reached = set(probe_rails(board, p).values())
            ac_rails = [r.net for r in p.rails if r.net in reached]
        except Exception as e:                # noqa: BLE001 - reported in the plan
            ac_note = f"{type(e).__name__}: {e}"
    plan = {"rails": [r.net for r in p.rails if solvable(r)], "all_rails": [r.net for r in p.rails],
            "tree": any(r.sources for r in p.rails), "thermal_enabled": bool(p.thermal.enabled),
            "thermal_estimate": thermal_estimate(board, p.thermal) if p.rails else None,
            "ac_rails": ac_rails, "ac_note": ac_note, "aggressors": [a.name for a in p.aggressors],
            "tolerance": case.path is not None and any(ld.has_tolerance() for r in p.rails for ld in r.loads),
            "board_warnings": list(board.warnings), "fields_warnings": list(case.merged.warnings)}
    return F.board(board), {"plan": plan}


RUNNERS: Dict[str, Callable] = {"mesh": step_mesh, "dc": step_dc, "tree": step_tree, "thermal": step_thermal,
                                "ac_estimate": step_ac_estimate, "ac": step_ac,
                                "noise_estimate": step_noise_estimate, "noise": step_noise,
                                "tolerance": step_tolerance, "plan": step_plan}


def _measure(fn: Callable, rec, *args):
    """Run ``fn`` timed: (its value, wall s, CPU s of this process, CPU s of reaped children)."""
    child0, _ = _children_usage()
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    out = fn(*args)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    rec.counts["workers_left"] = _reap_workers()
    child1, _ = _children_usage()
    return out, wall, cpu, (child1 - child0 if child0 is not None and child1 is not None else None)


def _mesher(pool: Dict[str, Any]) -> Dict[str, Any]:
    """KiPI's meshing mode in this process: KIPI_SERIAL (the runner's --mesher serial, as the
    frozen app, whose can_spawn() is False), how many workers a pool of 8 layer tasks would get
    (0: none, every layer meshed here), and the pool's size thresholds, lowered when the run
    asked (--pool-min-triangles / --pool-min-spread, a test knob: not KiPI's defaults)."""
    from kipi.core import workers
    from kipi.core.mesh import parallel
    for key, attr in (("min_triangles", "MIN_TRIANGLES"), ("min_spread", "MIN_SPREAD")):
        if pool.get(key) is not None:
            setattr(parallel, attr, int(pool[key]))
    return {"kipi_serial": os.environ.get(workers.SERIAL_ENV), "can_spawn": workers.can_spawn(),
            "workers_for_8_tasks": workers.workers(8), "min_triangles": parallel.MIN_TRIANGLES,
            "min_spread": parallel.MIN_SPREAD}


def main(spec_path: str, out_path: str) -> int:
    t_start = time.perf_counter()
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    record: Dict[str, Any] = {"id": spec["id"], "step": spec["step"], "target": spec.get("target"),
                              "pid": os.getpid(), "error": None}
    try:
        import bench_fingerprint as F
        import bench_probes as P

        from coppernicus.engine.ac import peak_rss_bytes
        rec = P.Recorder()
        record["patched"] = P.install(rec)
        record["mesher"] = _mesher(spec.get("pool") or {})
        record["import_s"] = time.perf_counter() - t_start
        record["rss_after_import_bytes"] = peak_rss_bytes()
        step = spec["step"]
        if step == "open":
            case, wall, cpu, child_cpu = _measure(open_case, rec, spec, rec)
            fingerprint, extra = F.board(case.board), {}
        else:
            case, record["open_s"], _, _ = _measure(open_case, rec, spec, rec)
            record["rss_after_open_bytes"] = peak_rss_bytes()
            rec.reset()
            (fingerprint, extra), wall, cpu, child_cpu = _measure(RUNNERS[step], rec, case, spec, rec)
        record.update(wall_s=wall, cpu_s=cpu, children_cpu_s=child_cpu,
                      cpu_total_s=cpu + (child_cpu or 0.0), fingerprint=fingerprint, **extra)
        record.update(rec.snapshot(wall))
    except BaseException as e:               # noqa: BLE001 - recorded: the runner reads it
        record["error"] = f"{type(e).__name__}: {e}".strip()
        record["traceback"] = traceback.format_exc()[-4000:]
        if "rec" in locals():
            record.update(rec.snapshot())
    try:
        from coppernicus.engine.ac import peak_rss_bytes
        record["rss_peak_bytes"] = peak_rss_bytes()
    except Exception:                        # noqa: BLE001
        record["rss_peak_bytes"] = None
    record["rss_workers_peak_bytes"] = _children_usage()[1]
    Path(out_path).write_text(json.dumps(record, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
