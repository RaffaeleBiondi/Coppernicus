# Workstreams

Status and split of the work as of 2026-11-24, agreed with the owner. Update it as items land.

## Decisions already taken

- Coppernicus is GPL-3.0-or-later. It reuses KiPI's engine as a dependency pinned to v1.0.1,
  and only `src/coppernicus/engine/` imports KiPI.
- It is distributed free on GitHub. The repository stays private until the owner decides.
- Real boards live only in the git-ignored `boards_local/`. Nothing board-derived is committed.
- The agent interface is an MCP server (stdio) on an engine-independent service layer, plus
  CLI `--json`. No HTTP API for now. See `docs/agent-interface-brief.md`.
- Triangle (KiPI's mesher) licence: option A for now (source distribution, `uv` installs it).
  Packaging later needs either B (permission from the author) or C (gmsh, GPLv2+, needs a
  patched or vendored mesher plus re-validation). Not decided yet.

## Roles

- **Coordinator:** the original planning session. It reviews, merges to `main`, pushes, and
  dispatches the WS1 phases.
- **WS1:** a background subagent of the coordinator, branch `feat/mcp`, worktree under
  `.claude/worktrees/`.
- **WS2, then WS3:** a second interactive Claude session on this folder (`coppernicus-17`), in
  its own worktree `.worktrees/gui` (branch `feat/gui`, then one branch per WS3 step). It
  reports milestones to the coordinator and does not push or merge.

## WS1: agent interface and automation

Delegated to a background agent on branch `feat/mcp`. The coordinator reviews and merges.

1. **Phase 1:** service layer, CLI `--json`, MCP server (`coppernicus mcp`), tests, and how to
   register the server in Claude Code / Claude Desktop.
   **Status (2026-11-24): merged in `main` (`d2ce457`). Guide for agents: `docs/agents.md`.**
   - `src/coppernicus/service/`: a `Session` with boards and results by id, and an LRU bound on
     result arrays that keeps summaries.
   - Ten MCP tools on `mcp` 2.3.0: `open_board`, `list_nets`, `net_parts`, `solve_rail`,
     `get_problems`, `render_map`, `probe`, `cut_current`, `compare_results`, `solve_project`.
   - `--json` on `nets`, `parts` and `solve`.
   - Additive changes to existing modules:
     - `Board.counts()` and `Board.with_copper_thickness()`, a what-if copy;
     - `Problem.data`;
     - `maps.render_layer_map()`.
   - Tests use synthetic boards only:
     - closed-form cuts (strip; demo neck, 7 mm and 3.5 A);
     - the brief's acceptance test through the SDK client;
     - a real stdio subprocess round trip.
   - Checked by hand: the engine's mesh process pool runs under `coppernicus mcp` over stdio
     (a synthetic 1.2 M-triangle board).
   - The app and the service share `coppernicus/fields.py` (the vectorised `TriangleLocator`,
     `current_vectors`). `service/fields.py` holds only the cross-section cut.
   - Checked by the coordinator: `solve --json` on a local real board gives the same numbers
     as the human report.
   - Not yet tried: registration in a real Claude Code or Claude Desktop host; Windows.
   - Open questions for the owner: `copper_thickness_mm` in the project YAML; unit-keyed
     `*_summary.json`; the default memory bound (8 results, 2 GB).
2. **Phase 2:** interactive offline HTML report per rail (zoom/pan, drop and current maps per
   layer, loads, problems, assumptions), `solve --html` / `--open`, and per-rail worst values
   summarised.
3. **Phase 3:** automation, i.e. a GitHub Action (or workflow template) usable from a KiCad
   repository, which solves with limits, uploads PNG/HTML artifacts and fails on an exceeded
   limit. Also a way to define sources and loads:
   - a text file (today's YAML project);
   - an option defined in KiCad itself (footprint fields or text directives, as padne does),
     to be proposed and agreed with the owner first.

## WS2: GUI (peer session, branch `feat/gui`)

Detailed tasks are in `docs/superpowers/plans/2026-11-24-coppernicus-roadmap.md`, Part A.

Status: T1 (pick pads on the board) and T2 (`coppernicus/fields.py`: current vectors, arrow
sampling, isolines, `TriangleLocator`) are merged in `main` (`d07199e`). T3, T4 and the
cosmetics are done on `feat/gui`; the Part A hand-over is pending.
`coppernicus/fields.py` is the single home of the field helpers. WS1 keeps only its cut tools
in `service/fields.py` and imports the rest.

- Pick sources and loads by clicking pads on the board.
- Drop isolines, current-flow arrows, ΔV between two picked points, hover readout.
- Mark nets as rails by hand, saved in the project, to override the heuristic.
- Before/after comparison of two runs.
- Drop breakdown per layer and segment.
- Report export, reusing the WS1 HTML report.
- Default load names, cosmetics (the macOS menu shows "Python").

## WS3: expose KiPI's other analyses (peer session, after WS2 merges)

Detailed tasks are in the same plan, Part B (Tasks 6–11).


The project model must grow first: VRMs and loads with ground pins, series links, AC probes,
capacitor models and noise aggressors, mapped to KiPI's `PISetup` inside `engine/`.

Order (each step needs the previous one):
1. power tree and ground return;
2. electro-thermal (board temperature);
3. AC PDN impedance (probes, capacitor models, target impedance, load-step response);
4. coupled noise and DC-DC ripple at probes (fields, victims);
5. tolerance stack-up per load.

Each step gets synthetic tests (KiPI's validation boards as a model) and is exposed in the
CLI, MCP and GUI.

## WS4: distribution and quality (later, by the owner's decision)

First make Coppernicus functional; efficiency and packaging come afterwards.

- Efficiency: PARDISO and solver work, mesh and factorization caching, coarsening far from
  current paths.

- Decide on Triangle; then packaging (`.app` / `.exe`, signing / notarization).
- First test run on Windows.
- GitHub Actions CI for the repository's own tests (synthetic boards only).

## WS5: documentation and the local launcher (coordinator)

The owner's goal (2026-11-24): every feature can be tried from the local launcher
(`tools/make_mac_launcher.sh`, which runs `main`), and the README is a short, visual guide.

- The coordinator owns `README.md`. The other streams send status-table lines in their
  hand-over messages instead of editing it.
- Screenshots come only from synthetic boards, made by `tools/make_docs_screenshots.py`. They
  are regenerated at merges that change the UI.
- `docs/agents.md` is the guide for AI agents that use Coppernicus (MCP and CLI `--json`). WS1
  writes it with Phase 1.
- Every Part B task ships a synthetic example in `examples/` that the app opens and that
  exercises the feature from the GUI.
- After every merge: pytest on `main`, then one start through the launcher, then check
  `~/Library/Logs/Coppernicus/launcher.log`.
- The owner gave a standing go-ahead: once a delivery is verified and merged, the coordinator
  dispatches the next phase or task without asking. Decisions that stay with the owner:
  - permission for agents to open windows;
  - for WS1 Phase 3, defining sources and loads inside KiCad;
  - anything about packaging, efficiency or Triangle.

## Coordination rules

- WS1 owns: `src/coppernicus/service*`, `src/coppernicus/mcp*`, `src/coppernicus/report*`,
  CLI additions, `.github/`, its docs.
- WS2 owns: `src/coppernicus/app/`.
- Changes to `engine/`, `project.py` and `results.py` must be additive and backward compatible
  while the streams run in parallel.
- Every stream: `uv run pytest` and `uv run ruff check src tests examples` clean before handing
  over. No real board data. Commits end with the `Co-Authored-By` line.
