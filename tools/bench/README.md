# Benchmark harness (dev tooling)

Times and memory of every Coppernicus step, each repeat measured in a fresh subprocess, with a
fingerprint of its result so a speed-up can be proven not to change the answer. Nothing under
`src/` is changed: per-stage times come from wrapping KiPI and engine functions at runtime in
the measuring process (`bench_probes.py`).

```
uv run python tools/bench/bench.py run --out DIR [--label NAME]              # every example
uv run python tools/bench/bench.py run --out DIR --synthetic grid-25 grid-50 grid-100
uv run python tools/bench/bench.py run --out DIR --project PATH [--rail NET ...] [--steps LIST]
uv run python tools/bench/bench.py compare DIR/A.json DIR/B.json [--out table.md]
uv run python tools/bench/bench.py report DIR/A.json
```

Options: `--repeats N` (3), `--mesher pool|serial` (serial: `KIPI_SERIAL=1`, as the frozen app,
whose `can_spawn()` is False), `--steps open,mesh,dc,tree,thermal,ac_estimate,ac,noise_estimate,noise,tolerance`,
`--aggressor NAME`, `--timeout S`, `--max-rss-gb G` (kills a measurement whose process tree
grows past it), `--allow-memory-risk`, `--pool-min-triangles/--pool-min-spread` (a test knob
lowering KiPI's pool thresholds; recorded in the run).

- **Cases**: the examples; `grid-<side mm>[x<rails>]` synthetic four-layer boards built in memory
  (`bench_synth.py`, power tree ~10^5 triangles at 25 mm to ~2.7·10^6 at 200 mm); an external
  project, named `project1`... in the output, never by its path. An external project is only
  opened unless `--rail` (mesh, dc) or `--steps` (heavy steps) say more. Results for a real
  project belong in `boards_local/_bench_speed/` (git-ignored). No project file is ever written.
- **Steps** call what the CLI calls, without writing maps or reports: `dc` = `solve_rail` +
  problems; `mesh` = the same call stopped once meshed; `tree`/`thermal` = `solve_project_tree`;
  `ac_estimate`/`noise_estimate` = the CLI's estimate functions, always run (and printed) before
  `ac`/`noise`; `tolerance` = `coppernicus tolerance --json` itself.
- **Per repeat**: wall, CPU (this process's threads, plus reaped mesher workers), peak RSS of the
  process (high-water mark), of its largest worker, and of the process tree (sampled with `ps`,
  floored at the process's own peak; none on Windows), triangles, unknowns, nnz, solver method,
  pool workers, field threads, and the stages: exclusive seconds (nested stages taken out of the
  outer one; a stage in several threads sums their seconds), entry points' inclusive seconds, and
  `unattributed_s` (wall minus the main thread's stages).
- **Fingerprint**: SHA-256 of the key result arrays and the scalars' bits, and the scalars at
  full precision (drops, load voltages, losses, via currents, temperatures, |Z| at probes,
  ripple, counts). `compare` says `identical` only when every repeat of both runs has the same
  hash and triangle count; otherwise the largest relative difference and its scalar.

Not split without touching `src/` (or the libraries): the work inside KiPI's mesher worker
processes (only the pooled mesh's wall time, workers' CPU and peak RSS); SuperLU's ordering vs
numeric factorization (one `factor`); per-frequency factor vs solve inside the AC field threads
(thread-seconds only; `ac_field_solves` is the wall time); the AC estimate's mesh cannot be
reused by the AC solve (both are timed); peak RSS per stage (per process only).
