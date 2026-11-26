# Workstreams

Status and split of the work as of 2026-11-24, agreed with the owner. Update it as items land.

## Decisions already taken

- Coppernicus is GPL-3.0-or-later. It reuses KiPI's engine as a dependency pinned to v1.0.1,
  and only `src/coppernicus/engine/` imports KiPI.
- It is distributed free on GitHub. The repository is public since 2026-11-24, recreated with a
  clean history; the earlier private repository is kept as `Coppernicus-private`. Commits use
  the GitHub no-reply address (repo-local git config). Nothing about real boards or the owner's
  employer goes into the repository, commit messages included.
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
   - Owner's decisions (2026-11-24): `copper_thickness_mm` is an optional what-if in the
     project settings and the MCP (real changes are made in KiCad; first MCP-only, made a
     project setting the same evening); `*_summary.json` moves to the unit-keyed format in Phase 2;
     the memory bound stays at 8 results / 2 GB; the HTML report may take up to about 30 MB.
   - The MCP server is registered for the owner in Claude Code (user scope).
2. **Phase 2:** interactive offline HTML report per rail (zoom/pan, drop and current maps per
   layer, loads, problems, assumptions), `solve --html` / `--open`, and per-rail worst values
   summarised.
   **Status (2026-11-24): merged in `main` (`5ff5d1d`).** `coppernicus/report.py`
   (`write_report(results, project, path, *, outline, board_name, ...)`), `solve --html/--open/--top`,
   MCP `report` tool (11 tools), one unit-keyed summary format, full problem lists. Measured:
   19.7 MB for a synthetic 3.07 M-triangle board; about 6 MB for a local 10-layer, 4-rail board,
   checked in Chromium. Safari and Firefox not tried. Assets in `src/coppernicus/report_assets/`
   must be packaged as data files (WS4).
3. **Phase 3 (merged 2026-11-24, `bd156f6`):** root `action.yml`, self-test and test workflows
   (Ubuntu, macOS; Windows informative), `docs/ci.md`, `docs/kicad-fields-proposal.md`. KiCad
   footprint fields implemented in phase 4b (`5965aaa`): on by default, KiCad wins unless
   `override_board_fields`. Original scope:
   automation, i.e. a GitHub Action (or workflow template) usable from a KiCad
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
1. power tree and ground return; ✅ (T6 project model v2, T7 power-tree solve, optional
   `max_ground_rise_mv`);
2. electro-thermal (board temperature); ✅ (T8: CLI in 0.2.0; app, MCP, report and CI after it);
3. AC PDN impedance (probes, capacitor models, target impedance, load-step response); ✅ (T9);
4. coupled noise and DC-DC ripple at probes (fields, victims); ✅ (T10);
5. tolerance stack-up per load. ✅ (T11)

Each step gets synthetic tests (KiPI's validation boards as a model) and is exposed in the
CLI, MCP and GUI.

## WS4: distribution and quality (later, by the owner's decision)

First make Coppernicus functional; efficiency and packaging come afterwards.

- Efficiency: PARDISO and solver work, mesh and factorization caching, coarsening far from
  current paths.

- Packaging, owner's decision (2026-11-24): a `.dmg` (macOS) and an installer or `.zip`
  (Windows) holding the complete app **except Triangle**. On first start the app fetches the
  `triangle` package from PyPI into the user's folder, once, before the engine loads. Triangle's
  licence allows telling users how to obtain it for free; bundling it in a GPL-3 app is not
  compatible. Unsigned builds first (macOS asks once: right-click → Open). It can start before
  T11, since it does not depend on the analyses.
- Also on the table: a permission request to Triangle's author (draft by the coordinator, sent
  by the owner), and an experiment with our own clean-room mesher (Delaunay refinement from the
  literature, never reading Triangle's source), benchmarked like gmsh.
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

**Final task, after T11 (owner's request, 2026-11-24): documentation refresh.** Before the
project is called complete, bring every user-facing document up to date with everything that
was built, in one pass:
- the README guide and its screenshots (all regenerated from synthetic boards), the status table;
- per-feature user documentation in `docs/` (how to use each analysis: power tree, KiCad
  fields, thermal, AC impedance, ripple, tolerance), with screenshots;
- `docs/agents.md` and the MCP tools: every feature reachable by an agent, as the owner scopes it;
- the skill in `skills/` (and its installed copy): what Coppernicus can do with Claude and how;
- `docs/technical.md`, `docs/ci.md`, the examples' descriptions.
Owner's scope (2026-11-24):
- MCP: the T8–T11 analyses; creating and editing projects (rails, sources, loads, limits, saved
  to `.coppernicus.yml`); proposing KiCad `PI_*` fields part by part (never editing KiCad
  files); comparing two board revisions (before and after a change in KiCad); a copper-weight
  what-if limited to one standard step (e.g. 0.5 oz → 1 oz on a layer, no bigger changes) to
  see whether it solves a problem.
- Skill: analysis and verdicts, the new analyses, and helping the user write `PI_*` fields
  (library symbol versus schematic instance). Not layout-fix proposals.
- Human docs: the README as the quick guide plus one page per feature in `docs/`, with
  screenshots. English only.
- WS1 starts the MCP items that do not depend on T8–T11 (project editing, field proposals,
  revision compare, copper-weight step) after phase 4c.

## Coordination rules

- WS1 owns: `src/coppernicus/service*`, `src/coppernicus/mcp*`, `src/coppernicus/report*`,
  CLI additions, `.github/`, its docs.
- WS2 owns: `src/coppernicus/app/`.
- Changes to `engine/`, `project.py` and `results.py` must be additive and backward compatible
  while the streams run in parallel.
- Every stream: `uv run pytest` and `uv run ruff check src tests examples` clean before handing
  over. No real board data. Commits end with the `Co-Authored-By` line.
