"""The benchmark harness in tools/bench (dev tooling, SPEED Phase A): its fingerprints, its
comparison arithmetic, its run metadata, its memory readings and, end to end, one quick run on
the demo example in fresh subprocesses. Solver figures are never written here as digits:
the end-to-end test compares a run with itself."""

import importlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "tools" / "bench"


def _bench(name):
    """A module of tools/bench (its scripts import each other by name, from their folder)."""
    if str(BENCH) not in sys.path:
        sys.path.insert(0, str(BENCH))
    return importlib.import_module(name)


# ---------------------------------------------------------------------------------- fingerprint

def _print(arrays, scalars):
    fp = _bench("bench_fingerprint").Print()
    for name, a in arrays:
        fp.array(name, a)
    for name, v in scalars:
        fp.scalar(name, v)
    return fp.done()


def test_a_fingerprint_is_the_same_for_the_same_numbers_built_apart():
    rng = np.random.default_rng(7)
    v = rng.random(1000)
    tris = rng.integers(0, 1000, (300, 3))
    a = _print([("v", v), ("tris", tris)], [("max_drop_v", 0.1 + 0.2), ("loss_w", 3.5)])
    # Copies, a non-contiguous view of the same values, the scalars in another order.
    wide = np.empty(2000)
    wide[::2] = v
    b = _print([("v", wide[::2]), ("tris", tris.copy())], [("loss_w", 3.5), ("max_drop_v", 0.1 + 0.2)])
    assert a["hash"] == b["hash"]
    assert a["scalars"] == b["scalars"]


def test_one_bit_in_one_array_changes_the_hash_and_no_scalar():
    v = np.linspace(0.0, 1.0, 101)
    w = v.copy()
    w[50] = np.nextafter(w[50], 2.0)
    a = _print([("v", v)], [("max_drop_v", 0.25)])
    b = _print([("v", w)], [("max_drop_v", 0.25)])
    assert a["hash"] != b["hash"]
    assert a["scalars"] == b["scalars"]


def test_the_hash_covers_names_dtypes_shapes_and_the_scalars_bits():
    v = np.arange(12, dtype=float)
    base = _print([("v", v)], [("x", 1.0)])["hash"]
    assert _print([("u", v)], [("x", 1.0)])["hash"] != base
    assert _print([("v", v.view(np.int64))], [("x", 1.0)])["hash"] != base      # same bytes
    assert _print([("v", v.reshape(3, 4))], [("x", 1.0)])["hash"] != base       # same bytes
    assert _print([("v", v)], [("x", np.nextafter(1.0, 2.0))])["hash"] != base


def test_nan_and_complex_arrays_hash_stably():
    z = np.array([1 + 2j, np.nan, 3.5 - 1e-9j])
    v = np.array([np.nan, 1.0, np.inf])
    assert _print([("z", z), ("v", v)], [("y", math.nan)]) == _print([("z", z.copy()), ("v", v.copy())],
                                                                    [("y", math.nan)])


def test_scalars_keep_full_precision_through_json():
    fp = _print([], [("a_v", 0.1 + 0.2), ("b_a", 1 / 3), ("n", 7), ("skip", None)])
    back = json.loads(json.dumps(fp))
    assert back["scalars"]["a_v"] == 0.1 + 0.2
    assert back["scalars"]["b_a"] == 1 / 3
    assert back["scalars"]["n"] == 7.0
    assert "skip" not in back["scalars"]


# -------------------------------------------------------------------------------------- compare

def test_relative_difference_is_over_the_larger_magnitude_and_nan_safe():
    rel = _bench("bench_compare").rel_diff
    assert rel(1.0, 1.0) == 0.0
    assert rel(0.0, 0.0) == 0.0
    assert rel(1.0, 1.1) == pytest.approx(0.1 / 1.1)
    assert rel(1.1, 1.0) == rel(1.0, 1.1)
    assert rel(-1.0, 1.0) == pytest.approx(2.0)
    assert rel(math.nan, math.nan) == 0.0
    assert rel(math.nan, 1.0) == math.inf
    assert rel(math.inf, math.inf) == 0.0


def test_summary_statistics_skip_missing_values():
    stats = _bench("bench_compare").stats
    assert stats([3.0, 1.0, 2.0]) == {"min": 1.0, "median": 2.0, "max": 3.0, "n": 3}
    assert stats([4.0, None, 1.0, 2.0, 3.0])["median"] == 2.5
    assert stats([None]) == {"min": None, "median": None, "max": None, "n": 0}


def _step(walls, rss, hashes, scalars, triangles=1000.0, error=None):
    reps = [{"wall_s": w, "cpu_s": w, "rss_peak_bytes": r, "fingerprint": {"hash": h, "scalars": scalars},
             "counts": {"triangles": triangles}, "error": error} for w, r, h in zip(walls, rss, hashes)]
    return {"case": "c", "step": "dc", "target": "VCC", "repeats": reps}


def test_compare_gives_time_and_memory_ratios_and_identical_fingerprints():
    cmp = _bench("bench_compare")
    a = _step([2.0, 1.0, 1.5], [100e6, 110e6, 105e6], ["h"] * 3, {"max_drop_v": 0.1})
    b = _step([1.0, 0.5, 0.6], [55e6, 50e6, 52e6], ["h"] * 3, {"max_drop_v": 0.1})
    row = cmp.compare_steps(a, b)
    assert row["wall_ratio"] == pytest.approx(0.5)          # min over min
    assert row["wall_median_ratio"] == pytest.approx(0.6 / 1.5)
    assert row["rss_ratio"] == pytest.approx(52e6 / 105e6)    # median over median
    assert row["fingerprint"] == "identical"


def test_compare_names_the_largest_scalar_difference():
    cmp = _bench("bench_compare")
    a = _step([1.0], [1e6], ["h1"], {"max_drop_v": 0.100, "loss_w": 2.0, "n": 5.0})
    b = _step([1.0], [1e6], ["h2"], {"max_drop_v": 0.101, "loss_w": 2.0, "n": 5.0})
    verdict = cmp.compare_steps(a, b)["fingerprint"]
    assert verdict.startswith("max rel diff 9.9")
    assert "max_drop_v" in verdict
    same = cmp.compare_steps(a, _step([1.0], [1e6], ["h2"], {"max_drop_v": 0.100, "loss_w": 2.0, "n": 5.0}))
    assert same["fingerprint"] == "hash differs, scalars equal"


def test_compare_never_says_identical_for_a_missing_errored_or_remeshed_step():
    cmp = _bench("bench_compare")
    a = _step([1.0], [1e6], ["h"], {"x": 1.0})
    assert cmp.compare_steps(a, None)["fingerprint"] == "missing in B"
    assert cmp.compare_steps(None, a)["fingerprint"] == "missing in A"
    err = _step([1.0], [1e6], [None], {}, error="EngineError: boom")
    assert cmp.compare_steps(a, err)["fingerprint"].startswith("error in B")
    remeshed = _step([1.0], [1e6], ["h"], {"x": 1.0}, triangles=1001.0)
    assert cmp.compare_steps(a, remeshed)["fingerprint"].startswith("mesh differs")
    # A run whose repeats disagree with each other is not identical either.
    flaky = _step([1.0, 1.0], [1e6, 1e6], ["h", "h3"], {"x": 1.0})
    assert cmp.compare_steps(a, flaky)["fingerprint"] != "identical"


def test_the_table_is_aligned_markdown():
    table = _bench("bench_compare").table(["step", "wall s"], [["dc VCC", "1.25"], ["tree", "12.5"]])
    lines = table.splitlines()
    assert lines[0].startswith("| step")
    assert set(lines[1]) <= set("|-: ")
    assert len({len(line) for line in lines}) == 1             # every column padded


# ------------------------------------------------------------------------------------- metadata

def test_run_metadata_has_the_keys_a_comparison_needs():
    meta = _bench("bench_meta").run_metadata(ROOT)
    for key in ("git_commit", "git_dirty", "git_branch", "coppernicus_version", "kipi_version",
                "python", "numpy", "scipy", "blas", "kipi_direct_backend", "thread_env", "cpu_count",
                "ram_bytes", "platform", "date"):
        assert key in meta, key
    assert meta["git_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", meta["git_commit"])
    assert meta["git_dirty"] in (True, False, None)
    for var in ("OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        assert var in meta["thread_env"]
    assert meta["cpu_count"] >= 1
    json.dumps(meta)                                           # plain data


# ------------------------------------------------------------------------------- memory readings

def test_ru_maxrss_is_bytes_on_macos_and_kilobytes_on_linux():
    to_bytes = _bench("bench_runner").maxrss_bytes
    assert to_bytes(2048, "darwin") == 2048
    assert to_bytes(2048, "linux") == 2048 * 1024


def test_the_process_tree_memory_sums_a_process_and_its_descendants():
    tree_kib = _bench("bench_runner").tree_rss_kib
    ps = """\
      1     0   1000
    100     1   5000
    101   100   2000
    102   101    700
    103     1   9999
    104   100    300
"""
    assert tree_kib(ps, 100) == 5000 + 2000 + 700 + 300
    assert tree_kib(ps, 103) == 9999
    assert tree_kib(ps, 555) is None


# -------------------------------------------------------------------------------- the runner

def test_a_measurement_over_its_time_is_killed_and_recorded(tmp_path):
    runner = _bench("bench_runner")
    spec = {"id": "slow", "case": {"name": "demo", "kind": "example",
                                   "project": str(ROOT / "examples" / "demo" / "demo.coppernicus.yml")},
            "step": "dc", "target": "+3V3", "tmp_dir": str(tmp_path)}
    rec = runner.run_child(spec, tmp_path / "work", tmp_path / "slow.log", timeout_s=0.2)
    assert rec["killed"].startswith("timeout")
    assert rec["error"] == rec["killed"]
    assert not list((tmp_path / "work").glob("slow.*"))          # spec and output removed


def test_synthetic_names_are_checked_and_boards_built_in_memory():
    synth = _bench("bench_synth")
    assert synth.parse("grid-100") == (100.0, 2)
    assert synth.parse("grid-60x3") == (60.0, 3)
    for bad in ("grid-10", "plane-50", "grid-50x9", "grid-abc"):
        with pytest.raises(ValueError):
            synth.parse(bad)
    board, project = synth.build("grid-30")
    assert board.path is None                                    # nothing written
    assert [r.net for r in project.rails] == ["RAIL1", "RAIL2"]
    def plane_layers(net):
        return {lid for z in board.snapshot.zones if z.net == net for lid in z.filled}
    for rail in project.rails:
        assert rail.sources and len(rail.loads) >= 2
        assert len(plane_layers(rail.net)) == 2                  # F.Cu and B.Cu: a pool can split it
    assert len(plane_layers("GND")) == 2                          # In1.Cu and In2.Cu
    assert [p.net for p in project.probes] == ["RAIL1"]


def _run_bench(*args):
    return subprocess.run([sys.executable, str(BENCH / "bench.py"), *args], capture_output=True, text=True,
                          timeout=240, cwd=ROOT)


def test_a_quick_run_on_the_demo_example_compared_with_itself(tmp_path):
    """The smoke test: two fresh processes per step on the demo rail, then compare."""
    r = _run_bench("run", "--examples", "demo", "--steps", "open,dc", "--repeats", "2", "--out",
                   str(tmp_path), "--label", "smoke")
    assert r.returncode == 0, r.stdout + r.stderr
    run = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    assert run["metadata"]["mesher"] == "pool"
    steps = {(s["step"], s["target"]): s for s in run["cases"][0]["steps"]}
    assert set(steps) == {("open", None), ("dc", "+3V3")}
    dc = steps["dc", "+3V3"]["repeats"]
    assert len(dc) == 2
    for rep in dc:
        assert rep["error"] is None
        assert rep["wall_s"] > 0 and rep["cpu_s"] > 0 and rep["rss_peak_bytes"] > 0
        assert rep["counts"]["triangles"] > 0 and rep["counts"]["unknowns"] > 0
        for stage in ("mesh", "assemble", "factor", "solve", "post", "convert", "problems"):
            assert rep["stages"][stage]["calls"] >= 1, stage
        assert rep["unattributed_s"] < rep["wall_s"]
        assert rep["fingerprint"]["scalars"]["max_drop_v"] > 0
    assert len({rep["fingerprint"]["hash"] for rep in dc}) == 1  # same code, same machine
    assert not list(tmp_path.glob("*.work"))                     # cleaned up
    assert not list(tmp_path.rglob("*.coppernicus.yml"))         # never a project file in --out
    c = _run_bench("compare", str(tmp_path / "smoke.json"), str(tmp_path / "smoke.json"))
    assert c.returncode == 0, c.stderr
    line = next(x for x in c.stdout.splitlines() if "| dc +3V3" in x)
    assert "identical" in line
    assert line.count("| 1.00 ") >= 3                            # wall (min, median) and RSS ratios


def test_the_serial_mesher_reaches_the_measuring_process(tmp_path):
    r = _run_bench("run", "--examples", "demo", "--steps", "open", "--repeats", "1", "--mesher", "serial",
                   "--out", str(tmp_path), "--label", "serial")
    assert r.returncode == 0, r.stdout + r.stderr
    run = json.loads((tmp_path / "serial.json").read_text(encoding="utf-8"))
    rep = run["cases"][0]["steps"][0]["repeats"][0]
    assert rep["mesher"]["kipi_serial"] == "1"
    assert rep["mesher"]["workers_for_8_tasks"] == 0             # KiPI meshes every layer in-process


# ---------------------------------------------------------------------------------- the probes

def test_nested_stages_are_exclusive_and_a_stage_name_can_depend_on_its_parent():
    probes = _bench("bench_probes")
    rec = probes.Recorder()
    clock = iter(range(100))
    real = probes.time.perf_counter
    probes.time.perf_counter = lambda: float(next(clock))
    try:
        inner = rec.wrap(lambda: None, lambda parent: "thermal_factor" if parent == "thermal_model" else None)
        with rec.span("thermal_model"):          # t 0 .. 3
            inner()                              # t 1 .. 2: inside thermal_model
        inner()                                  # outside: not timed
        with rec.span("factor"):                 # t 4 .. 5
            pass
    finally:
        probes.time.perf_counter = real
    snap = rec.snapshot(wall_s=10.0)
    st = snap["stages"]
    assert st["thermal_model"]["seconds"] == 2.0 and st["thermal_model"]["inclusive_s"] == 3.0
    assert st["thermal_factor"]["seconds"] == 1.0 and st["thermal_factor"]["calls"] == 1
    assert st["factor"]["seconds"] == 1.0
    assert snap["unattributed_s"] == 10.0 - 4.0
