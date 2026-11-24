# Coppernicus Functional Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Coppernicus fully functional: a GUI that explains where a PDN fails, and every
KiPI analysis (power tree and ground return, thermal, AC impedance, DC-DC noise and ripple,
tolerance stack-up) usable from the project file, the CLI and the GUI.

**Architecture:**
- Coppernicus keeps its own engine-independent project model (`project.py`) and plain-array
  results (`results.py`).
- `src/coppernicus/engine/` is the only code that imports KiPI. It maps the project onto
  KiPI's `PISetup` and the KiPI results back to ours.
- The GUI (`app/`) only talks to `engine`, `project`, `results` and pure helper modules.
- Efficiency (solver, caching) and packaging are explicitly **out of scope**: functional first.

**Tech Stack:** Python 3.12, uv, KiPI v1.0.1 (pinned git dependency), PySide6, pyvista/VTK,
NumPy/SciPy/Shapely, matplotlib, PyYAML, pytest, ruff.

**Spec:**
- `docs/workstreams.md` (scope and roles);
- `docs/agent-interface-brief.md` (current APIs, hard rules, engine capabilities and entry
  points);
- `docs/kipi-analysis.md` (physics, limits).

Read all three before Task 1.

## Global Constraints

- Only `src/coppernicus/engine/` imports `kipi`. Everything else uses `engine`, `project`,
  `results` and pure modules.
- Board files are read-only. Never write next to a board; outputs go to an explicit folder.
- No real board data in the repository. Tests use synthetic boards
  (`coppernicus.engine.synthetic`, built in Python). Real boards live only in the git-ignored
  `boards_local/`, read-only.
- No invented pass/fail thresholds: a verdict only against a limit the user set. Otherwise
  rank.
- Every result carries its assumptions (a list of strings), shown in the CLI and GUI.
- Physics constants and third-party APIs are taken from KiPI's code or verified docs, never
  from memory.
- Multiprocessing: keep the `__main__` guard and `multiprocessing.freeze_support()`.
- Units in field names: `_v`, `_mv`, `_a`, `_a_mm2`, `_mm`, `_w`, `_hz`, `_ohm`, `_f`, `_h`,
  `_c`. Coordinates are mm in KiCad's frame (y down). Efficiency is a fraction 0–1, never %.
- Backward compatibility: every project file written before this plan must still load and
  solve to the same numbers (`examples/demo/demo.coppernicus.yml`, `tests/`).
- `uv run ruff check src tests examples` and `uv run pytest` clean before each hand-over.
- Commits are small, and each message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **No push and no merge to `main`.** The coordinator session reviews and merges.
- WS1 (a separate agent, branch `feat/mcp`) owns `src/coppernicus/service*`, `mcp*`,
  `report*` and `.github/`. Do not edit them. CLI additions go at the end of
  `build_parser()`.
- Talk to the owner in Italian. Ask before irreversible actions. Warn before opening windows on
  their screen.

## Review Focus

- **Pad picking where pads overlap or a PTH pad spans layers:** pick the pad on the shown layer
  first, nearest centre. A click on a pad of another net says so instead of adding it
  (test in Task 1).
- **Old project files without the new fields:** they must load, and their solve results must be
  identical (test in Task 6).
- **Copper islands not connected to a source (NaN voltages):** isolines, arrows and probes skip
  NaN instead of crashing (tests in Tasks 2 and 3).
- **Big meshes (~400 k triangles per layer):** isolines and arrows stay bounded (≤ 20 levels,
  ≤ 2 000 arrows) and redraw in under 1 s (check in Task 3, timed in the GUI script).
- **Power-tree mistakes** (a load without ground pins when a ground net is set, a VRM whose
  input is its own output, a missing link pad): a clear `EngineError` naming the part, never a
  hang or traceback (test in Task 7).

---

## Part A: GUI (WS2), branch `feat/gui` in worktree `.worktrees/gui`

### Task 1: Pick sources and loads by clicking pads

**Files:**
- Modify: `src/coppernicus/engine/board.py` (add `PadHit`, `Board.pad_at`)
- Modify: `src/coppernicus/app/board_view.py` (single-click pick mode, highlight)
- Modify: `src/coppernicus/app/main_window.py`, `src/coppernicus/app/panels.py` ("Add source
  from board" / "Add load from board" toggle buttons)
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: `PadHit(ref: str, pin: str, pad_id: str, net: str, position: tuple[float, float],
  layer_id: int)`;
  `Board.pad_at(x: float, y: float, layer_id: int | None = None, tolerance_mm: float = 0.3) ->
  PadHit | None`. It looks for pads whose copper shape on `layer_id` (any layer if `None`)
  contains the point or lies within `tolerance_mm`, and returns the nearest centre.
- Produces: the `BoardView.pad_picked` signal `(x_mm: float, y_mm: float)` in pick mode.

- [ ] **Step 1: Failing tests** in `tests/test_engine.py`:
  - `test_pad_at_finds_pad_on_layer_and_nearest`: on `pins_board()`,
    `Board.pad_at(12.0, 0.0, F)` returns `ref == "U1"` and `pin == "2"`.
    `Board.pad_at(100, 100)` is `None`.
  - `test_pad_at_reports_other_net`: `pad_at(16.0, 0.0, F).net == "OTHER"`.
- [ ] **Step 2:** `uv run pytest tests/test_engine.py -k pad_at -v`. Expected: FAIL (no
  `pad_at`).
- [ ] **Step 3:** Implement `pad_at` with shapely `Point.distance` on the pad polygons
  (`Pad.shapes[layer]`). Keep a lazily built STRtree for speed.
- [ ] **Step 4:** Run the same command again. Expected: PASS.
- [ ] **Step 5:** GUI wiring:
  - The toggle buttons in `RailPanel` put `BoardView` in pick mode, which emits `pad_picked`
    on a single left click without drag (a move of less than 3 px).
  - `MainWindow` calls `pad_at`. If the pad's net is the rail net (or a tied net), it appends a
    row with that ref and pin to sources or loads, or appends the pin to an existing row of the
    same ref. Otherwise the status bar shows `"<ref>.<pin> is on <net>, not on <rail>"`.
  - Picked pads are highlighted (a small outline glyph).
- [ ] **Step 6:** Extend the scripted GUI check (pattern: drive `MainWindow` from code, as in
  the coordinator's from-scratch loop). On `examples/demo`, pick J1 as source and U1 and U3 as
  loads by calling the pick handler with their coordinates, then solve.
  Expected: rows J1 / U1 / U3, and the solve succeeds.
- [ ] **Step 7:** Commit `feat(gui): pick sources and loads by clicking pads`.

### Task 2: Flow-field helpers (pure, testable)

**Files:**
- Create: `src/coppernicus/fields.py`
- Test: `tests/test_fields.py`

> First check `main`. If WS1 has merged a non-GUI field helper (a triangle locator, J vectors,
> cut current), reuse it instead of duplicating it, and tell the coordinator.

**Interfaces:**
- Produces:
  - `current_vectors(layer: LayerField) -> np.ndarray`, shape (M, 2) in A/mm²: the direction of
    −∇V per triangle, scaled to `layer.current_density`. NaN voltages give `(0, 0)`.
  - `sample_flow(layer: LayerField, spacing_mm: float, max_arrows: int = 2000) ->
    tuple[np.ndarray, np.ndarray]`: points (K, 2) and vectors (K, 2), on a grid of `spacing_mm`
    kept only inside copper and thinned to `max_arrows`.
  - `isolines(layer: LayerField, values: np.ndarray, levels: Sequence[float]) ->
    list[np.ndarray]`: polylines (N_i, 2), computed with matplotlib's `tricontour` on an Agg
    figure, masked where values are NaN.
  - `TriangleLocator`, moved here from `app/board_view.py`; `board_view` imports it from here.

- [ ] **Step 1: Failing tests** on `strip_board()` solved with 1 A:
  - `test_current_vectors_point_from_source_to_load`: the mean x-component is greater than 0
    (J1 at x=0 to U1 at x=40), and |J| is within 5 % of `1/(0.5*0.035)` away from the pads.
  - `test_sample_flow_bounded_and_inside`: `len(points) <= 2000`, and every point lies inside
    the strip's copper.
  - `test_isolines_of_drop_are_vertical_on_a_strip`: for `levels=[5.0]` mV of drop, every
    polyline has an x-spread of less than 0.6 mm.
  - `test_nan_voltages_are_skipped`: a layer with half its voltages NaN raises nothing and
    returns no vectors from the NaN triangles.
- [ ] **Step 2:** `uv run pytest tests/test_fields.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the four interfaces above.
- [ ] **Step 4:** Run again. Expected: PASS. `tests/test_app_logic.py` still passes, with
  `TriangleLocator` imported from `fields`.
- [ ] **Step 5:** Commit `feat: flow-field helpers (vectors, sampling, isolines)`.

### Task 3: Isolines, current-flow arrows and ΔV between two points in the view

**Files:**
- Modify: `src/coppernicus/app/board_view.py`, `src/coppernicus/app/main_window.py`

**Interfaces:**
- Consumes: Task 2 functions.
- Produces:
  - `BoardView.show_field(..., isolines: bool, arrows: bool)`;
  - a "Measure ΔV" mode in which two clicks emit
    `probed("ΔV A→B = … mV (A: … V, B: … V)")`.

- [ ] **Step 1:** Add the "Isolines" and "Arrows" check boxes to the view bar.
  - Isolines use 10 evenly spaced drop levels across the rail's range (≤ 20), drawn as thin
    dark lines.
  - Arrows use `sample_flow` with a spacing chosen so that at most 2 000 arrows appear, drawn as
    2D glyphs scaled by √|J| (KiPI's convention).
- [ ] **Step 2:** Add the "Measure ΔV" toggle: first click A, second click B, using
  `TriangleLocator` plus barycentric interpolation (as the existing probe does). NaN → message
  "no copper of this net here".
- [ ] **Step 3:** GUI check on `examples/demo` (hook `--screenshot`). Expected: isolines bend
  around the slot, arrows run from the supply to the loads, and the ΔV between J1 and U1 is
  within 0.5 mV of `drop(U1)`.
- [ ] **Step 4:** Time the redraw on the largest local rail (the +3V3 rail of the local 10-layer
  project). Expected: under 1 s with isolines and arrows on. Report the number.
- [ ] **Step 5:** Commit `feat(gui): isolines, flow arrows, ΔV between two points`.

### Task 4: Mark nets as rails by hand

**Files:**
- Modify: `src/coppernicus/project.py` (field `marked_rails: List[str]`, saved only when not
  empty)
- Modify: `src/coppernicus/app/panels.py` (NetPanel context menu "Mark as rail" / "Unmark",
  and a "Rails" filter: marked rails plus nets with sources/loads, plus heuristic power nets)
- Test: `tests/test_project.py`

- [ ] **Step 1: Failing test** `test_marked_rails_round_trip`: save `marked_rails=["VBUS_X"]`,
  reload, and get it back. A file without the key loads with `[]`.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Implement it. Saving writes the key only when the list is non-empty.
- [ ] **Step 4:** Run it. Expected: PASS. Wire the context menu, then run a GUI check (mark,
  save, reopen).
- [ ] **Step 5:** Commit `feat: mark nets as rails by hand`.

### Task 5: Before/after comparison and per-layer loss breakdown panel

> Do this after WS1 phase 1 is merged (the coordinator will say so), and reuse the service
> layer's `compare_results` if it exists.

**Files:**
- Modify: `src/coppernicus/app/main_window.py`, `src/coppernicus/app/panels.py`
- Create, only if WS1 did not provide one: `src/coppernicus/compare.py`
- Test: `tests/test_compare.py` (if `compare.py` is created)

**Interfaces:**
- Produces, if created:
  - `compare(a: RailResult, b: RailResult) -> Comparison`, with `load_deltas: dict[str,
    float]` (drop_b − drop_a, V) and `layer_loss_deltas: dict[str, float]` (W);
  - `difference_layer(a: LayerField, b: LayerField) -> LayerField`: b's voltage interpolated
    onto a's nodes with `TriangleLocator`, and `voltage = b − a`.

- [ ] **Step 1: Failing test** `test_compare_strip_two_currents`: strip at 1 A versus 2 A gives a
  `load_deltas["U1"]` equal to `drop_1A` (linear), and a difference layer whose max equals
  `drop_1A` within 1 %.
- [ ] **Step 2:** Implement it, then run it. Expected: PASS.
- [ ] **Step 3:** GUI:
  - "Pin as baseline" stores the current result.
  - After a new solve, "Compare" shows the per-load deltas table and a "Difference" field in
    the view.
  - A "Loss by layer" bar chart (matplotlib in Qt) goes in the Problems dock.
- [ ] **Step 4:** Commit `feat(gui): before/after comparison and loss-by-layer chart`.

**Hand-over A:** message the coordinator. Include:
- the branch and commits;
- test counts;
- screenshots of Tasks 1–5;
- the redraw time from Task 3;
- anything unverified.

Wait for the merge before Part B.

---

## Part B: KiPI analyses (WS3), one branch per task from the updated `main`

Every task in Part B starts with a **design gate**:
1. read the named KiPI module and its tests;
2. write a mapping table (Coppernicus field → KiPI field, unit, default) at the top of
   `docs/engine-mapping.md` (create it in Task 6, append in later tasks);
3. message the coordinator, then continue.

### Task 6: Project model v2 and the KiPI `PISetup` mapping (power tree, ground)

**Files:**
- Modify: `src/coppernicus/project.py`
- Create: `src/coppernicus/engine/pisetup.py`
- Create: `docs/engine-mapping.md`
- Test: `tests/test_project.py`, `tests/test_pisetup.py`

**Interfaces (decided):**
- `Source` gains:
  - `kind: str = "source"` (`"source"`, `"ldo"` or `"smps"`);
  - `efficiency: float | None = None` (SMPS, 0–1);
  - `quiescent_a: float = 0.0`;
  - `input_ref: str = ""`, `input_pins: PinSpec = None`, `input_net: str = ""`;
  - `ground_ref: str = ""`, `ground_pins: PinSpec = None`, `ground_net: str = ""`.
- `Load` gains `ground_ref: str = ""`, `ground_pins: PinSpec = None`, `ground_net: str = ""`.
- New `Link(name: str, ref: str, pins_a: PinSpec, net_a: str, pins_b: PinSpec, net_b: str,
  resistance_ohm: float)` for ferrites, sense resistors and fuses.
- `Project` gains:
  - `links: List[Link]`;
  - `engine_options: Dict[str, Any] = {}`: an engine-specific passthrough, documented as such
    and validated by the engine.
- `engine/pisetup.py`: `to_pisetup(board: Board, project: Project) -> PISetup`, using KiPI's
  `Vrm`, `Load`, `PinRef`, `DcLink` (field list in `kipi/dc/pi/model.py`).

- [ ] **Step 1: Design gate** (read `kipi/dc/pi/model.py`, `kipi/dc/pi/tree.py`;
  `docs/engine-mapping.md` table).
- [ ] **Step 2: Failing tests:**
  - `test_v1_project_loads_unchanged`: `examples/demo/demo.coppernicus.yml` loads, and
    `to_dict()` equals the v1 dict plus nothing.
  - `test_round_trip_v2_fields`: links and ground pins survive a save and reload.
  - `test_to_pisetup_maps_sources_loads_links`: on a synthetic power chain (Task 7 board),
    vrms / loads / links have the expected `PinRef`s and kinds.
- [ ] **Step 3:** Implement the model fields (defaults keep v1 output byte-identical) and
  `to_pisetup`.
- [ ] **Step 4:** Run `uv run pytest -v`. Expected: all PASS, including the old suite (the v1
  numbers are unchanged).
- [ ] **Step 5:** Commit `feat: project model v2 and PISetup mapping`.

### Task 7: Power-tree solve with ground return

**Files:**
- Create: `src/coppernicus/engine/tree.py`
- Modify: `src/coppernicus/results.py`, `src/coppernicus/engine/synthetic.py`,
  `src/coppernicus/cli.py` (append `solve --tree`), `src/coppernicus/app/*` (VRM kind,
  efficiency and ground editor; one result per group, ground nets included)
- Test: `tests/test_tree.py`

**Interfaces:**
- Produces:
  - `solve_project_tree(board: Board, project: Project, progress=None) -> TreeResult`
    (ours, in `results.py`), with:
    - `groups: Dict[str, RailResult]` (rails and ground nets, keyed by net name or joined
      names);
    - `vrms: List[VrmReport(name, v_out_v, i_out_a, i_in_a, p_loss_w, v_in_v)]`;
    - `loads: List[LoadReport(name, v_supply_v, v_ground_v, v_across_v, current_a)]`;
    - `assumptions`, `warnings`.
  - `power_chain_board()` in `synthetic.py`: a copy of the dimensions of KiPI's `power_chain`
    validation board (12 V source, switcher, ferrite link, remote-sensed LDO, 2 mm strips over
    a ground strip).
- Wraps `kipi.dc.pi.tree.solve_tree(snap, setup, progress)`.

- [ ] **Step 1: Design gate** (`tree.py`, KiPI `tests/accuracy/test_dc_accuracy.py` power-chain
  tests).
- [ ] **Step 2: Failing tests** modelled on KiPI's power-chain accuracy tests:
  - LDO: I_in = I_out + I_q.
  - Switcher: I_in = V_out·I_out/(η·V_in) + I_q.
  - Ground strip drop: R·I.
  - Every load's `v_across_v = v_supply_v − v_ground_v`.
  - Review-focus errors: a load without ground pins when `ground_net` is set, and a VRM whose
    input is its own output, each raise `EngineError` with the part named.
- [ ] **Step 3:** Implement `solve_project_tree` and the result conversion (reuse `_convert` for
  each group).
- [ ] **Step 4:** Run `uv run pytest tests/test_tree.py -v`. Expected: PASS.
- [ ] **Step 5:**
  - CLI `solve --tree` prints the VRM and load tables.
  - GUI: VRM kind, efficiency and ground fields; a group selector including ground nets; the
    load table shows supply, ground and across voltages.
  - GUI check on the synthetic power chain.
- [ ] **Step 6:** Commit `feat: power tree and ground return`.

### Task 8: Electro-thermal: board temperature

**Files:**
- Modify: `src/coppernicus/project.py` (`ThermalOptions(enabled=False, ambient_c=25.0,
  iterations=3, airflow_m_s=0.0, grid_mm=1.0)`; take the defaults from KiPI's `ThermalSettings`
  at the gate and write them in the mapping doc)
- Create: `src/coppernicus/engine/thermal.py`
- Modify: `results.py` (`TemperatureMap(layer_name, x_mm, y_mm, t_c)` per layer, `peak_c`),
  CLI (`solve --thermal`), GUI (a "Temperature" field)
- Test: `tests/test_thermal.py`

- [ ] **Step 1: Design gate** (`kipi/dc/thermal/model.py`, `cosim.py`, KiPI
  `tests/dc/test_thermal.py`).
- [ ] **Step 2: Failing tests** modelled on KiPI's: uniform heating matches the convection
  balance; hot copper raises the drop (the electro-thermal drop is greater than at ambient);
  results carry the assumptions (ambient, airflow, iterations, materials).
- [ ] **Step 3:** Implement it, wrapping `solve_electrothermal(snap, setup, ThermalSettings(...),
  progress)`.
- [ ] **Step 4:** Tests PASS. CLI and GUI show the temperature map and peak, with a user-limit
  verdict only if `max_temp_c` / `max_rise_c` is set.
- [ ] **Step 5:** Commit `feat: electro-thermal board temperature`.

### Task 9: AC PDN impedance

**Files:**
- Modify: `project.py`:
  - `Probe(name, rail, ground, ref, pins: PinSpec, target_ohm=None, target_hz=None,
    step_a=None, slew_a_per_us=None)`;
  - `CapModel(name, capacitance_f, esl_h, esr_ohm, refs: List[str], rated_v=None,
    dielectric="")`;
  - `AcOptions` (sweep range and points; defaults from KiPI at the gate).
- Create: `src/coppernicus/engine/ac.py`
- Modify: `results.py` (`ImpedanceResult(freq_hz, z_ohm per probe, target_ohm, worst)`), CLI
  (`ac PROJECT`), GUI (a |Z|(f) plot tab with the target, and a probe and capacitor editor)
- Test: `tests/test_ac.py`

- [ ] **Step 1: Design gate** (`kipi/ac/model.py`, `kipi/ac/solver/`, KiPI
  `tests/accuracy/test_ac_accuracy.py`).
- [ ] **Step 2: Failing tests** modelled on KiPI's AC validation (synthetic boards copied from
  KiPI's `ac_plane`, `ac_decap`, `ac_ferrite` dimensions):
  - plane pair: low-frequency C = ε₀·εr·A/h;
  - first cavity resonance at c/(2a·√εr);
  - decap: |Z| at the series resonance equals the ESR;
  - ferrite filter behaviour, as asserted in KiPI's tests.
- [ ] **Step 3:** Implement it, wrapping `solve_ac(snap, setup, rail, ground)`.
- [ ] **Step 4:** Tests PASS. CLI writes a CSV and a PNG of |Z|(f). GUI tab. Verdict only
  against the user's target.
- [ ] **Step 5:** Commit `feat: AC PDN impedance`.

### Task 10: DC-DC noise and ripple

**Files:**
- Modify: `project.py`:
  - `Aggressor(name, vrm, topology, vin_v, fsw_hz, rise_s, fall_s, duty=None, load_a=None,
    ripple_probes: List[str])`;
  - `Victim(net, name, preset, driver_ohm, receiver_ohm, receiver_pf, logic_v,
    noise_margin_v)`;
  - fields and defaults from `kipi/noise/model.py` at the gate.
- Create: `src/coppernicus/engine/noise.py`
- Modify: `results.py` (`RippleResult` per probe: peak-to-peak V and the 20 MHz-limited value;
  `VictimResult`: noise V vs margin, mechanisms), CLI (`noise PROJECT --aggressor NAME`), GUI
  (a "Ripple & noise" tab: ripple at probes, victims table, field map image)
- Test: `tests/test_noise.py`

- [ ] **Step 1: Design gate** (`kipi/noise/{model,solver,result}.py`, KiPI
  `tests/accuracy/test_noise_accuracy.py`).
- [ ] **Step 2: Failing tests** modelled on KiPI's coupled-noise validation (a synthetic
  12 V → 1.8 V buck):
  - victim ranking: capacitive beside the switch node > inductive beside the hot loop >
    shielded on an inner layer;
  - the ripple at a probe is non-zero and carries its assumptions.
- [ ] **Step 3:** Implement it, wrapping `solve_noise(snap, setup, aggressor, progress,
  dc_result)`.
- [ ] **Step 4:** Tests PASS. CLI and GUI output; verdicts only against the noise margins the
  user gave (or a preset they chose).
- [ ] **Step 5:** Commit `feat: DC-DC ripple and coupled noise`.

### Task 11: Tolerance stack-up per load

**Files:**
- Modify: `project.py` (`Load` gains `tol_pct: float | None`, `dc_tol_pct`, `ac_tol_pct`; VRM
  accuracy `acc_pct: float | None`)
- Create: `src/coppernicus/engine/tolerance.py`
- Modify: `results.py` (`StackupRow(load, dc_drop_v, vrm_acc_v, ripple_v, transient_v,
  total_below_v, total_above_v, verdict)`), CLI (`tolerance PROJECT`), GUI (a table)
- Test: `tests/test_tolerance.py`

- [ ] **Step 1: Design gate** (`kipi/tolerance/{evaluate,gather,model}.py`).
- [ ] **Step 2: Failing tests:**
  - DC + accuracy + ripple + transient add up as KiPI's evaluate does;
  - missing parts give "cannot pass yet" plus a hint (never PASS);
  - several aggressors' ripple combines as RSS (KiPI's rule).
- [ ] **Step 3:** Implement it.
- [ ] **Step 4:** Tests PASS. CLI and GUI table.
- [ ] **Step 5:** Commit `feat: tolerance stack-up per load`.

**Hand-over after each of Tasks 6–11:** message the coordinator with:
- the branch;
- the commits;
- the test counts;
- the mapping-doc section;
- screenshots;
- anything unverified.

Wait for the merge before starting the next task. The coordinator syncs the WS1 service layer
and MCP tools with each merged analysis.

---

## Not in this plan (owner's decision: later)

- Efficiency: PARDISO / solver work, mesh and factorization caching for what-if runs, DC
  coarsening far from current paths.
- Packaging (`.app` / `.exe`) and the Triangle decision (A now; later B or C).
- WS1 phases 2–3 (HTML report, GitHub Action, sources and loads defined in KiCad): owned by the
  WS1 agent.
- GUI items that come after Part A and the coordinator will assign:
  - a report export button reusing the WS1 HTML report, once phase 2 merges;
  - a hover readout;
  - default load names;
  - the macOS menu showing "Python".
