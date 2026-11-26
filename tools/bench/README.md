# Benchmark harness (dev tooling)

Times and memory of every Coppernicus step, each repeat measured in a fresh subprocess, with a
fingerprint of its result so a speed-up can be proven not to change the answer. Nothing under
`src/` is changed: per-stage times come from wrapping KiPI and engine functions at runtime in
the measuring process (`bench_probes.py`).

```
uv run python tools/bench/bench.py run --out DIR [--label NAME]                 # every example
uv run python tools/bench/bench.py run --out DIR --examples demo power_chain     # some of them
uv run python tools/bench/bench.py run --out DIR --synthetic grid-25 grid-50 grid-100
uv run python tools/bench/bench.py run --out DIR --project PATH [--rail NET ...] [--steps LIST]
uv run python tools/bench/bench.py compare DIR/A.json DIR/B.json [--out table.md]
uv run python tools/bench/bench.py report DIR/A.json
```

Run options: `--examples [NAME ...]`, `--synthetic CASE ...`, `--project PATH` (repeatable),
`--rail NET`, `--aggressor NAME`, `--steps open,mesh,dc,tree,thermal,ac_estimate,ac,noise_estimate,noise,tolerance`,
`--repeats N` (3), `--mesher pool|serial`, `--timeout S`, `--max-rss-gb G` (kills a measurement
whose process tree grows past it), `--sample-s S` (process-tree memory sampling, 0.25 s),
`--allow-memory-risk`, `--keep-work` (keep each measurement's spec and outputs, for debugging),
`--pool-min-triangles N` / `--pool-min-spread N` (a test knob lowering KiPI's pool thresholds;
recorded in the run, so its times are not KiPI's defaults).

## Cases and steps

- **Cases**: the examples; `grid-<side mm>[x<rails>]` synthetic four-layer boards built in memory
  (`bench_synth.py`: power tree 96k triangles at 25 mm to 2.7M at 200 mm); an external project,
  named `project1`... in the run file. No project or board file is ever written.
- **Steps** call what the CLI calls, without writing maps or reports: `dc` = `solve_rail` +
  problems; `mesh` = the same call stopped once meshed; `tree`/`thermal` = `solve_project_tree`;
  `ac_estimate`/`noise_estimate` = the CLI's estimate functions; `tolerance` = `coppernicus
  tolerance --json` itself.
- **Memory gate**: `ac` and `noise` always run their estimate first (printed) and are skipped
  when it could exceed this machine's memory, unless `--allow-memory-risk`; `thermal` prints
  its estimate the same way. `tolerance` gets the estimates of every AC rail and aggressor it may
  solve (a superset of those it needs) and runs with `--skip-ac` / `--skip-noise` for a solve
  whose estimate could exceed the memory or did not run (`tolerance_flags` in the run). An
  external project is only opened unless `--rail` (mesh, dc) or `--steps` (heavy steps) ask more;
  no estimate exists for the power tree.
- **Mesher**: `pool` as a Python install runs (KiPI meshes a large net's layers in worker
  processes), `serial` as the frozen app (`KIPI_SERIAL=1`: KiPI's `can_spawn()` is False there).
  Below KiPI's thresholds (1.5M estimated triangles, 0.5M outside the largest layer, one net at a
  time) both mesh in one process and give the same times, unless the test knob lowers them.

## What a repeat records

Wall; CPU of the measuring process (all threads) and of its reaped mesher workers; peak RSS of
the process (a high-water mark), of its largest worker, and of the process tree (sampled with
`ps`, floored at the process's own peak); the step's RSS above the opened board; triangles,
unknowns, nnz, solver method, pool workers, field threads; the machine's load when it started;
and the stages: exclusive seconds (a stage inside another is taken out of it; a stage in several
threads sums their seconds), entry points' inclusive seconds, `unattributed_s` (wall minus the
main thread's stages) and `missing_stages` (a stage the step always runs that had no call).

- `factor` is SpdSolver's setup: the direct factorization up to 200k unknowns (KiPI's
  `DIRECT_SOLVE_MAX_UNKNOWNS` without PARDISO), the AMG hierarchy's setup above, where `solve`
  holds the CG iterations.
- The wrappers name KiPI's functions, some private (`_copper`, `_board_model`...): after a KiPI
  update a renamed target stops every measurement with an error naming it, and a target no
  longer reached shows in `missing_stages`.

**Fingerprint**: SHA-256 of the key result arrays and the scalars' bits, and the scalars at full
precision (drops, load voltages, losses, via currents, temperatures, |Z| at probes, ripple,
counts). `compare` says `identical` only when every repeat of both runs has the same hash and
triangle count. Otherwise it gives the largest relative difference over every pair of repeats,
and the top three with their absolute and relative values. Relative means over the larger of
the two values, floored at `UNIT_EPS` = 1e-9 times the largest magnitude of the same unit (the
name's suffix: `_v`, `_a`, `_w`...) in both fingerprints, so a round-off "zero" (a drop of
1e-13 V) is judged against the volts on that board, not against itself. `compare` also flags
steps skipped or listed twice, and changes of pool workers, field threads or solver method,
which KiPI picks from the free memory and spare cores.

## Privacy

The run file never holds an external project's path, and errors and tracebacks are stored with
absolute paths and the project's and board's file names replaced. But step targets and scalar
names hold net names and part references, so the run file of a private project is private, and
so are the logs (`<label>.logs/`). Results for a real project belong in
`boards_local/_bench_speed/` (git-ignored).

## Limits

- **Windows**: the process tree's memory is not sampled (`--max-rss-gb` does nothing), a timeout
  kills the measuring process but not KiPI's mesher workers, and the workers' CPU and peak memory
  are not read; the run prints this as a warning.
- Not split without touching `src/` (or the libraries): the work inside KiPI's mesher worker
  processes (only the pooled mesh's wall time, workers' CPU and peak RSS); SuperLU's ordering vs
  numeric factorization (one `factor`); per-frequency factor vs solve inside the AC field threads
  (thread-seconds only; `ac_field_solves` is the wall time); the AC estimate's mesh cannot be
  reused by the AC solve (both are timed); peak RSS per stage (per process only).
