# Brief: make Coppernicus drivable by AI agents

> **For a planning agent.** Produce an implementation plan (not code) for the agent interface
> described here. Read this brief, then the files it points to. Ask the owner about the items
> under *Open decisions* before fixing them in the plan.
>
> Written 2026-11-24 against `main` @ `b091c49`.

## 1. Goal

Coppernicus (GPL-3.0-or-later, Python 3.12) is a standalone DC power-integrity analyzer for
KiCad boards. Its solver engine is [KiPI](https://github.com/drewsum/KiPI) v1.0.1. Today it has
a CLI and a draft desktop app.

The goal is to let an AI agent (Claude Code, Claude Desktop, any MCP client) run the same
analyses a human runs in the app, then reason about the results. The agent should be able to:

- open a board and find its power nets;
- define sources and loads, solve, and read the voltage drop and current density;
- locate and measure bottlenecks;
- render maps;
- compare what-if variants.

The GUI redesign is a **separate track** and not part of this plan.

## 2. Decided

- **Agent surface: an MCP server over stdio**, built on a new engine-independent **service
  layer**. Reasons:
  - Agents call tools natively and the tool schemas describe themselves.
  - A board is loaded once (up to ~2 s for a 10-layer board) and its results stay addressable
    by id across calls.
  - Maps can be returned as images to multimodal agents.
  - There is no network listener.
- **CLI `--json`** on `nets`, `parts` and `solve`, through the same service layer. This is the
  cheap first step: agents with a shell can already drive the CLI.
- **No HTTP API for now.** If a remote or multi-user use appears, it reuses the service layer.
- **MCP SDK:** the official `mcp` package, v2.3.0 at the time of writing (MIT, Python ≥ 3.10).
  The v2 server API is `from mcp.server import MCPServer` with `@mcp.tool()` (from the PyPI
  README). **Check the current docs at <https://py.sdk.modelcontextprotocol.io/>** for:
  - image or structured content in tool results;
  - progress notifications;
  - lifespan/state handling.

  Do not rely on v1 names: `FastMCP` is gone from the README.

## 3. Current state (verified)

```sh
uv sync
uv run pytest                                          # 23 passed (1 more with boards_local/ projects)
uv run coppernicus nets examples/demo/demo.kipi.json.gz
uv run coppernicus parts examples/demo/demo.kipi.json.gz +3V3
uv run coppernicus solve examples/demo/demo.coppernicus.yml --out results/demo
uv run coppernicus gui examples/demo/demo.coppernicus.yml --solve +3V3 --field current
```

| Module | What it offers |
|---|---|
| `src/coppernicus/engine/board.py` | `Board.open(path)`; `nets()`, `power_nets()`, `net_kind(net)` (heuristic), `layers()`, `parts_on_net(net)`, `pads(nets, ref, pins)` (exact, glob, or `None` for all; errors list the available pins), `net_ties()`, `outline()`, `copper(net, layer_id)` (shapely), `net_layers(net)`, `warnings` |
| `src/coppernicus/engine/solve.py` | `solve_rail(board, rail, settings, progress=cb, cancelled=fn) -> RailResult`. It follows net ties as shorts and returns `assumptions` (plating, ρ(T), mesh, load model) |
| `src/coppernicus/engine/synthetic.py` | Synthetic boards with closed-form answers (strip, via, pins, net tie, demo); `save_snapshot()` |
| `src/coppernicus/project.py` | `Project` / `Rail` / `Source` / `Load` / `Limits` / `Settings`, YAML I/O. Engine-independent |
| `src/coppernicus/results.py` | `RailResult`: per-layer `LayerField` (points, tris, voltage per node, \|J\|, power and area per triangle), `TerminalResult`, `ViaCurrent`, `loss_breakdown()`, `drop()`, `drop_pct()` |
| `src/coppernicus/problems.py` | `find_problems(result, limits, top=5)`: loads ranked by drop, current-crowding spots (area-weighted mean \|J\|, top 1 % of layer area, 80 % hysteresis), top vias. FAIL only against user limits |
| `src/coppernicus/maps.py` | `save_layer_maps(result, outline, out_dir, problems)`: drop and current PNG per layer |
| `src/coppernicus/cli.py` | `nets`, `parts`, `solve` (exit 0 / 1 limit exceeded / 2 error), `gui` |
| `src/coppernicus/app/` | PySide6 + pyvista draft GUI; `board_view.TriangleLocator` (point → triangle) |

**Performance on real boards:**
- import: 0.1–1.9 s;
- typical rail: 0.1–3 s;
- worst case (a 6-layer ground net, 1.5 M unknowns): 14 s and 3.4 GB on an Apple M5.

**Missing tool, proven by hand.** In a real analysis the most useful extra query was a
**cross-section cut**: copper width along a line, the current through it (∫ J·n·t ds) and the
mean and max |J|. It was done with a scratch script:
1. compute the per-triangle J = −∇V/ρ from `LayerField`;
2. sample the line;
3. locate triangles with `TriangleLocator`;
4. take the copper width from `Board.copper(...) ∩ line`.

It belongs in the service layer.

## 4. Hard rules (the plan must keep them)

1. **Only `coppernicus/engine/` imports KiPI.** The service layer, MCP server, CLI and GUI talk
   to `engine`, `project`, `results` and `problems`.
2. **Board files are read-only.** Never write next to a board. Outputs go to an explicit folder.
   The engine's GUI writes `.kicad_prl` files and locks; Coppernicus must not.
3. **No real board data in the repository.** Tests and examples use synthetic boards. Real
   boards live in the git-ignored `boards_local/`. Any `boards_local/**/*.coppernicus.yml` is
   solved by `tests/test_local_boards.py`, which is skipped when absent.
4. **No invented thresholds.** A verdict exists only against limits the user set. Without
   limits, rank. Coppernicus's documented current-density default
   ([technical.md](technical.md#current-density-default)) raises attention items, never a
   verdict. Every result carries its assumptions; tools must return them.
5. **Multiprocessing:** large nets mesh in a process pool, so entry points need the
   `__main__` guard and `multiprocessing.freeze_support()`.
6. **stdio hygiene:** under the MCP stdio transport nothing may print to stdout. Route the
   engine's logging (logger `kipi`) and progress to stderr or a log file.
7. Units in every field name (`_v`, `_mv`, `_a`, `_a_mm2`, `_mm`, `_w`). Coordinates are mm in
   KiCad's frame (y down), with layer names as in KiCad.
8. Errors are structured and actionable, for example: `{code: "pin_not_on_net", message,
   hint: "pins on this net: 1, 2, 3"}`.

## 5. Proposed shape (to refine in the plan)

- `coppernicus/service.py` (or a package): a `Session` holding
  - boards by id (path + mtime);
  - projects;
  - results by id;
  - an LRU bound on memory, since a big result is ~1 GB.

  Every function takes and returns JSON-serialisable data.
- `coppernicus/mcp_server.py`: thin tool wrappers, started by a `coppernicus mcp` subcommand
  over stdio. Document how to register it in Claude Code / Claude Desktop.
- `cli.py`: `--json` output through the service layer.

**Draft tool set (aim for about ten).** The owner's client already loads many MCP servers.

| Tool | In | Out |
|---|---|---|
| `open_board` | path | board_id, layers (name, thickness), counts, import warnings |
| `list_nets` | board_id, kind (power / ground / all), filter | nets with kind and layers |
| `net_parts` | board_id, net | parts (ref, value, footprint, pins), net ties |
| `solve_rail` | board_id, net, sources, loads, limits?, settings? | result_id, compact summary: load voltages and drops, max drop, loss per layer, top problems, assumptions, warnings |
| `get_problems` | result_id, limits? | ranked problems with layer and x/y (re-rank without re-solving) |
| `render_map` | result_id, layer, field (drop / voltage / current), region? | PNG image and file path |
| `probe` | result_id, layer, x, y | V, drop, \|J\| |
| `cut_current` | result_id, layer, p1, p2 | width, current, mean and max \|J\| |
| `compare_results` | result_a, result_b | per-load and per-layer deltas (for what-if) |
| `solve_project` | project path, rails? | batch of the above summaries |

What-if knobs worth exposing as solve settings: copper thickness per layer, via plating,
copper temperature. Adding virtual vias to a copy of the board is a later step.

**Acceptance test.** An agent with only these tools, on `examples/demo`, should be able to:
- find the power net and its parts;
- solve with the three loads;
- list the problems and render the drop and current maps of the busiest layer;
- measure a cut through the neck beside the slot;
- conclude whether the 0.25 % limit holds.

The same run on a real board is done locally by the owner.

## 6. Engine capabilities not exposed yet

Evidence is in three tiers, so the plan does not overstate what works.

| Tier | Capability | Entry point (from KiPI's tests) |
|---|---|---|
| **Used through Coppernicus** | DC drop, current density, via currents and losses per rail, net ties | `kipi.dc.solver.run(snap, NetProblem)` |
| **KiPI accuracy tests pass here; not run on a real board** | AC PDN impedance Z(f): two-sided copper sheets, plane capacitance, skin effect, capacitor models (C, ESR, ESL, DC bias, aging, tolerance, SPICE), ferrites, package Touchstone, target impedance from a transient spec, estimated load-step response | `kipi.ac.solver.solve_ac(snap, setup, setup.ac.rail, setup.ac.ground)` |
| | Coupled switcher noise: buck/boost waveforms as harmonic spectra; capacitive, inductive and shared-ground coupling onto signal victims; noise-margin checks; scan for victims | `kipi.noise.solver.solve_noise(snap, setup, aggressor_ref)` |
| **Docstrings or tests only** | Electro-thermal co-simulation: board temperature per layer (copper and barrel losses, VRM losses, convection, edge boundaries), ρ(T) feedback | `kipi.dc.thermal.cosim.solve_electrothermal(snap, setup, ThermalSettings(...))` |
| | Power tree: VRM chains (LDO / SMPS efficiency), remote sense, load line, multi-phase, series links (ferrites, sense resistors, diodes), ground nets as their own problems | `kipi.dc.pi.tree.solve_tree(snap, setup)` |
| | **DC-DC ripple** at AC probes (harmonics on the meshed rail and ground), input/output ripple, hot-loop H- and E-field maps | part of the noise solve, with the board model on |
| | Tolerance stack-up per load: DC drop + VRM accuracy + ripple + transient against the load's tolerances, with a report | `kipi.tolerance` |
| | Load-model files, HyperLynx `.bud` / PI-model and `.pdn.json` power-tree importers | `kipi.dc.pi.load_model`, `kipi.dc.pi.importers` |

**Important for the plan.** Everything beyond tier 1 takes KiPI's `PISetup`: VRMs with ground
pins, loads with ground pins, series links, AC probes and capacitor models, noise aggressors. It
does not take the per-net `NetProblem` that Coppernicus uses today. So the `Project` model has
to grow, mapped to `PISetup` inside `engine/`, before these can be exposed.

The docstrings suggest this order, each step needing the previous:
1. power tree and ground return;
2. thermal;
3. AC probes and capacitor models;
4. noise and ripple;
5. tolerance stack-up.

## 7. Open decisions (owner's call)

1. Which analysis follows DC in the agent interface: power tree / ground return, thermal, or AC.
2. Tool granularity and count (the ~10 above, or fewer and coarser).
3. Whether the GUI moves onto the service layer now or later.
4. Whether KiPI stays a pinned dependency or is vendored, if engine patches become necessary.
5. Packaging and Triangle's licence. Separate from this plan, but it affects how an MCP server
   is shipped to others.

## 8. Caveats to carry into tool outputs

- Loads draw their current split equally across their pins; source pins are equipotential.
- A rail solve does not include the ground return. Ground can be solved as its own rail today.
- Net power/ground classification is a name-and-zone heuristic.
- Point maxima of |J| at sharp inner corners depend on the mesh. Report spot means and cuts
  instead.
- If a tool ever reports a temperature-rise *estimate* from current density, the IPC-2221 trace
  formula I = k·ΔT^0.44·A^0.725 (A in mil², k = 0.048 external, 0.024 internal) was taken from
  secondary sources ([Qorvo](https://www.qorvo.com/design-hub/design-tools/interactive/pcb-trace-power-handling-calculator),
  [DigiKey](https://www.digikey.com/zh/resources/conversion-calculators/conversion-calculator-pcb-trace-width),
  [Protoexpress](https://protoexpress.com/?p=12833)), not the standard. It is described as
  conservative and superseded by IPC-2152. Label it as an estimate and prefer the engine's
  thermal model.

## 9. References

- `docs/kipi-analysis.md`: stage-1 analysis of KiPI (physics, import accuracy, performance,
  UI gaps, licensing).
- `README.md`: user-facing overview, project file format, roadmap.
- KiPI source, read-only reference clone outside the repo: `kipi/dc`, `kipi/ac`, `kipi/noise`,
  `kipi/tolerance`, `kipi/dc/thermal`. Headless examples are in `tests/accuracy/` and
  `tests/dc/test_thermal.py`.
