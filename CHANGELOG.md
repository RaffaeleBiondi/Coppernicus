# Changelog

What changed in each release of Coppernicus, for people who use the desktop app, the command
line, the MCP server or the GitHub Action. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Coppernicus is pre-alpha: check its
results against your own judgement.

## 0.5.0 (2026-11-27)

### Added

- `output_part_heat_pct` in the `thermal:` block (and the MCP `thermal_options`): with
  `part_pads`, a regulator's name (as in the power tree) to the share of its loss, in %, that
  goes to the pads of the part its output is held at when that is another part, the inductor;
  the rest stays on the IC. Only the share you enter, from the parts' data: none is assumed. A
  name that is not a regulator, or one whose output is on its own part, is refused. The
  board-temperature assumptions say where each regulator's loss went.
- Every result names the mesher that meshed its copper: `mesher` (`name`, `version`,
  `experimental`) in each JSON summary (rail, project, tree, AC, noise, tolerance), and
  `settings.mesher` in a rail's summary and HTML report. The mesher is chosen with `--mesher`,
  the project's `settings.mesher` or the MCP solve tools' `mesher`: `rust`, Coppernicus's own
  (`tools/mesher_rs`, built with `uv sync --group rust`; the default, see Changed), or
  `triangle`, KiPI's own meshing; `compare_results` and `compare_revisions` warn when two results
  were meshed differently.
- The app's **Mesher** box, at the top of the setup panel, chooses the mesher of every solve
  (rail, tree and board temperature, impedance, noise, tolerance, and the estimates that mesh)
  and is kept for the next start; a project's `settings.mesher` wins and locks it. While the
  packaged app has not downloaded Triangle, the box offers **Triangle (download…)**, which shows
  Triangle's licence and downloads it. A mesher that cannot run, or Triangle missing, is shown
  with its message and what to do, not as an unexpected error. The status bar and the HTML
  report name the mesher each result was meshed with.
- The app's first run shows a guided start: four steps to a first solve (open a board, pick
  a net, add a source and the loads, solve), each ticked as it gets done; it goes after the
  first solve, or with Don't show again, and never comes back.
- Help → Keyboard shortcuts (F1) lists every shortcut; every toolbar button's tooltip names
  its own. The toolbar and the navigator have icons (Phosphor Icons, MIT, listed in the
  packaged app's third-party notices).

### Changed

- The app's look follows the HTML report: dark by default with the report's colours, **☀ Light**
  at the right end of the toolbar (also View menu, Ctrl+Shift+L) switches to light and back,
  kept for the next start; the same look on macOS, Windows and Linux (Qt's Fusion style). The
  plots follow the theme, in the app and in the HTML pages (shown inverted in dark with their
  hues turned back, so a red limit stays red; the maps keep their colour scales); the files
  written to disk (PNG and CSV exports, the CLI's plots) stay light.
- The app's layout follows the HTML report's pages: the results pages are chosen in a
  navigator under the nets (Rail, Power tree, AC, Tolerance), the map sits above the page that
  has one, and the right panel shows the setup of the page shown with its solve button at the
  foot (hide it from its title bar; ⇤ Setup in the status bar or View → Setup panel shows it
  again). Tolerance: double-click a row to see its load on the board. The probes table shows
  Name, Part, Pins and Target; the selected probe's other fields are in a card under it.
- The Rail pages open with the verdict (a FAIL / PASS / not judged badge beside the summary
  line) and up to four cards: the worst load, the largest drop (or ground rise) on the copper,
  the copper loss and where most of it goes, the strongest current crowding. Problems are one
  line each; the selected one's detail shows in a strip under the list and a ring marks it on
  the map.
- The toolbar has the four solves by their verbs (Solve rail, Solve tree, Solve impedance,
  Solve noise), with Open ▾, Save ▾ and Export ▾ menus for the rest. Every page and rail net
  carries a badge: its verdict, or a dashed "not solved"; a group shows the worst of its solved
  pages; Problems counts its FAILs.
- The Impedance and Ripple & noise pages open with the verdict and cards, as the Rail pages
  do: the worst probe against its target and the |Z| range; the worst victim against its noise
  margin, the ripple, the hot loop and the radiated estimate (these never judged). A result from
  before a setup change keeps its verdict with a dashed outline, on the page and in the
  navigator, and its tooltip says to solve again; Tolerance still treats it as missing.
- The Tolerance page has one row per load with its verdict; under it, the selected load's
  stack-up (DC drop + accuracy and ripple + transient against the allowed band), its parts (a
  missing part named, with what gives it) and its cards.
- Every numeric field of the app shows its unit ("50 mΩ", "500 kHz", "3 %", "±5 mV") and takes a
  number typed with it or without it: alone, the number is in the unit the field's label names;
  with an SI prefix or the unit it means what it says ("0.5 MHz" in a kHz field is 500 kHz, "50m"
  in an Ω cell is 50 mΩ).
- The app shows the mesher's warnings with every result: the fallback to Triangle where the rust
  mesher is not installed, and a coarse AC mesh edge. The Tolerance page states the AC and noise
  solves' mesh edge in its assumptions, as `coppernicus tolerance` does.
- **The rust mesher is the default.** `solve`, `ac`, `noise`, `tolerance`, the MCP solve tools
  and the app mesh with Coppernicus's own mesher (`rust`, `tools/mesher_rs`) unless a mesher is
  named; `--mesher triangle` (the project's `settings.mesher`, the MCP `mesher`) meshes with
  KiPI's Triangle. Results record `mesher.default`, and one meshed with Triangle by choice says so
  in its assumptions; a saved summary that records no mesher still reads as Triangle. Where the
  rust mesher is not installed (a plain `uv sync` without `--group rust`, a pip install), a solve
  that names no mesher meshes with Triangle and says so in `warnings` (`mesher.fallback_from:
  "rust"`); one that names rust stops with `mesher_unavailable`. Numbers move slightly (the mesh,
  not the design); verdicts on the examples are unchanged.
- **Triangle is optional.** In a Python install it comes with KiPI; the packaged app no longer
  downloads it on the first start, only when the triangle mesher is chosen (`--mesher triangle`
  or `--fetch-triangle`, with its licence note), and Help → Remove downloaded mesher says when it
  comes back.
- **The AC mesh edge follows the mesher.** `ac: max_edge_mm` is no longer 1.0 mm by default: when
  the project does not set it, the AC, noise and tolerance solves (and their estimates) use the
  mesher's own default, 0.5 mm with rust (finer than KiPI's 1.0 mm so that copper loops are
  resolved as Triangle resolves them at 1.0 mm), 1.0 mm with Triangle. A value set in the project
  wins for any mesher and is kept on save, even 1.0. The documents give `ac.max_edge_mm_used`
  and `ac.max_edge_mm_from` ("rust default", "triangle default", "project") with an assumption
  line; with rust, a set value coarser than 0.5 mm adds a warning (never a verdict): |Z| and the
  noise can come out a few percent low; use 0.5 mm or leave it unset (0.25 mm for the highest
  noise accuracy).
- A result judged against a user's `max_current_density` (rail, project, power tree, board
  temperature) says in its assumptions that a crowding spot's mean |J| depends on the mesh, with
  either mesher: typically 1-2 % on detailed boards, up to about 6 %, and more on a spot of only
  a few elements at a sharp copper corner, where neither mesher converges (15 % seen on a real
  board). Read a spot within a few percent of the limit, or one of only a few elements, with
  care. Not for the documented default (attention items only).

### Fixed

- `regulator_heat: part_pads` puts a regulator's conversion loss on the pads of its own part,
  the one with its input pins. For an external-inductor buck, whose output is held at the
  inductor (KiCad field `PI_SOURCE_REF`), that is the IC: the loss no longer lands on the
  inductor's two pads alone, which put the board's hot spot on the inductor. A regulator whose
  input and output are on one part (an LDO) gives the same results as before; `output_pads`
  (KiPI's placement, the default) is unchanged.

## 0.4.0 (2026-11-27)

### Added

- Where each power-tree drop comes from: every load's and regulator input's drop split into the
  source's regulation, each net's copper, each link and the ground, parts that add up to it
  exactly (`loads[].segments`, `vrms[].segments`); each net of a group with its range, where it
  is fed from, its drop or rise and its losses (`groups[].net_split`); each link's voltage across
  it (`links[].across_mv`). Shown, never judged: `coppernicus solve --tree --split`, the JSON and
  `solve_tree`, and the HTML report's Split column and Nets table.
- The power tree shown from another 0 V, with no re-solve: **0 V at** in the app's Power tree tab
  (a part's ground pins, or a pad typed or picked on the map), `tree: {reference: {ground_of:
  U1}}` (or `{pads: [R12.2, NT1.*]}`) in the project file, `--reference U1` on the command line
  and `reference` on the MCP `solve_tree` (not saved). The tables, the voltage map and the
  readouts move by the reference's potential; every drop, rise, limit and verdict stays from
  the solve's 0 V, and the JSON keeps its keys and values with a `reference` block
  (`offset_v`). Each regulator also shows its output across its own pins (`V across`,
  `v_out_across_v`), and a probe on a tree group adds `voltage_from_reference_v`.
- A source's ground terminal on several parts, as its output already could be: KiCad field
  `PI_GND_REF=BT4,BT8` on a source (`PI_GND_PINS` with `REF.PAD` entries or a pad for every
  listed part), project key `extra_ground_pins: [BT8.*]` beside `ground` (also in
  `write_project`). In the power tree every pad of every listed part is held at 0 V together
  and the copper shares the return between them: a battery whose negative pole is two cell
  holders no longer forces the whole return through one holder, which overstated the ground
  rise and the ground copper's loss. Ideal supplies (`kind: source`) only; a single ground part
  gives the same results as before.
- Agents can probe, map and cut the power tree: the MCP `solve_tree` returns `tree_id` (`t1`)
  and a `result_id` per group (`t1:GND`), which `probe`, `render_map` and `cut_current` take;
  `report(result_ids=["t1"])` writes the tree's page. A probe on a group says its group, kind
  and whether the number is a drop, a ground rise or a shift; `relative_to` (a point or a pad,
  `U3.2`) gives the voltage between two points of the same solve, across groups too (a chip's
  supply pad against its ground pad). The tools for one rail result refuse tree ids with a hint.
  The tree counts in the session's memory bounds and is dropped whole. On the command line,
  `coppernicus solve PROJECT --tree --probe LAYER@X,Y` (repeatable) prints the readouts
  (`probes` in the JSON).
- **+** and **−** in the app step the Layer selector to the next and previous copper layer, as
  in KiCad (also **=** and the keypad's + and −), round from the last layer to the first. They
  are left to the field while you type in a text field, a spin box or a table cell. The Layer
  selector's tooltip says so.
- **Export report** in the app writes the power tree's page too when the tree is solved: the page
  `coppernicus solve --tree --html` writes, beside the rails' report with the command line's
  `_tree_report` suffix (`demo_report.html` → `demo_tree_report.html`). With only the tree
  solved, the Save dialog is for the tree's page alone. Same folder as before (`results/` next
  to the project, or `~/Documents/Coppernicus/results`); as during a rail solve, export is
  refused while the tree solves.
- The HTML report's map has a label next to the pointer, as the app's: the value of the map
  shown in large type, worded and rounded as in the app (the drop with its % of the source
  voltage, the voltage, or |J|), then the drop's side and the layer, small and muted. It flips
  at the map's edges, takes the page's dark or light colours, hides off the copper and when the
  pointer leaves the map, and follows the layer, the map and the rail or tree group shown. The
  readout line under the map keeps its words and digits, and now also reads the point again
  after a zoom under a still pointer.

### Changed

- A DC solve above KiPI's 200,000-unknown direct limit is now factorized directly, up to 400,000
  unknowns by default, instead of solved with AMG-CG: about half the time on large rails, about a
  quarter more memory, results equal within 1e-9 (CG's tolerance). It stays within KiPI's SuperLU
  limit and half the free memory. `COPPERNICUS_DC_DIRECT_MAX=0` or empty restores KiPI's rule;
  any other value sets the ceiling (`COPPERNICUS_SUPERLU_ORDER=off` turns it off too).
- The hover label in the app leads with the value of the map shown, as one large number: the
  drop (with its % of the source voltage), V, |J|, the change of drop on the Δ map, or the
  board temperature. It switches at once when Show changes under a still pointer.
  **View → Hover shows** chooses the values under it: the layer by default; a choice of your
  own saved by 0.3.1 is kept under the new number, and a readout turned off stays off. The
  label follows the app's light or dark look.
- Every current-density map (the HTML report, the PNG maps, the app) writes above its colour
  bar the layer's peak |J| (degenerate triangles left out, marked mesh-dependent) and, when a
  limit applies, the layer's verdict as the problem list judges it from the spot means:
  "limit 60 A/mm²: FAIL · worst spot 89.1 A/mm² over 3.02 mm²", "limit 60 A/mm²: pass", or
  with no `max_current_density` "default 60 A/mm² (attention): 3 spots above" or "below". It
  follows the layer, the rail or group and the map shown, so copper above the colour scale's
  top no longer goes unseen. The scale itself is unchanged (the layer's 99.5th percentile), as
  are the maps' colours and every verdict.
- Shorter names in the app: **Solve rail** (F5, was Solve), **Solve tree** (Ctrl+T, was Solve
  power tree), **Pin baseline** (Ctrl+B, was Pin as baseline), and the net-tie option is
  **Follow net ties**, its long text in the tooltip. The shortcuts are unchanged.

### Fixed

- The HTML report samples each layer in blocks: the same pages, several times faster and with
  far less memory on boards with large planes.
- A power tree that fails because its loads' ground return is broken now says so and names the
  ground net, instead of saying that the supply rail's loads are not connected.
- A layer that carries only round-off current gets no ranked current-crowding spots.

## 0.3.1 (2026-11-26)

### Changed

- The board temperature and DC solves factor with SuperLU's symmetric ordering by default
  (about half the time and memory of the electro-thermal solve on a large board);
  `COPPERNICUS_SUPERLU_ORDER=off` restores KiPI's method.
- Boards open faster: a bit-identical stand-in for the KiCad reader's remove-unused-layers
  trims (`COPPERNICUS_FAST_TRIM=0` restores KiPI's own).

### Fixed

- Boards opened at the same time (parallel `open_board` calls to the MCP server, or a board
  opened while another one solves) no longer swap import warnings: each board lists only
  the warnings of its own file. Before, every board opened together got the warnings of all
  of them, such as unfilled zones or a default stackup that its file does not have.
- A mesh triangle with an edge shorter than 1 nm no longer sets a peak current density. 1 nm
  is the resolution of KiPI's geometry snap, so such a triangle comes from points the mesher
  added, and its |J| is set by the last bits of the node voltages. It is left out of every
  maximum or peak |J|: `j_point_max_a_mm2` of rails and groups, each current-crowding spot
  (its peak and its members), the report cards, a cut's maximum, and the |J| colour scale of
  the HTML report, the PNG maps and the app. It stays in the solution, the copper loss and the
  areas. The warnings say how many were left out on each layer and the largest |J| among
  them; the JSON has `degenerate_triangles`. Verdicts still judge a spot's area-weighted mean,
  but a spot made only of such triangles is no longer listed. Boards without them give the
  same results as before.

## 0.3.0 (2026-11-26)

Three more of KiPI's analyses, each in the app, the command line, the MCP server and the
Action: the PDN impedance, the ripple and coupled noise of switching regulators, and the
tolerance stack-up of each load. The board temperature comes to the app, to agents and to CI.
Regulator ratings are judged, capacitor models and tolerances can live in KiCad fields, and a
documented current-density default flags crowded copper without ever giving a verdict.

### Added

- **PDN impedance |Z|(f)** at probes on a chip's supply and ground pins, with decoupling
  capacitor models (ESR, ESL, capacitance, DC-bias derating) and the source's control-loop
  bandwidth (project keys `probes:`, `capacitors:`, `ac:`). Judged only against your target: a
  flat `target_ohm` up to `target_hz`, a mask (`target_points`), or one computed from the chip's
  `ac_tolerance_v`, `step_a` and `slew_a_per_us`. Nothing below the bandwidth is judged, and a
  probe is not judged when no bandwidth is given or a capacitor on its rail has no model.
  - Command line: `coppernicus ac PROJECT` (`--rail`, `--out`, `--no-board-fields`, `--json`,
    `--html`) writes a CSV and a PNG of |Z|(f) per rail and `<project>_ac_summary.json`.
  - App: the **Impedance** tab (probes, capacitor models and sweep, picked on the board;
    **Solve impedance**, Ctrl+I; CSV and PNG export). The capacitor models from KiCad fields
    are listed there too, read-only, each cell naming its field and part.
  - Agents: the MCP tool `solve_ac`. CI: the Action's opt-in `ac` input.
  - Example: `examples/pdn`. Guide: [docs/ac-impedance.md](docs/ac-impedance.md).
- **Ripple and coupled noise.** For each switching regulator you mark as an aggressor
  (`aggressors:`): its hot loops, the waveforms of one period, the ripple at its pins and at the
  probes you name, H and E field maps and a radiated estimate. For each victim net
  (`victims:`): the noise at its receiver. A victim is judged only against your
  `noise_margin_v`, its preset's margin or the JESD8 levels of an entered `logic_v`, and is not
  judged while a KiPI default stands in (Coss, Qrr, a capacitor without a model, guessed ends).
  The ripple and the radiated estimate are never judged.
  - Command line: `coppernicus noise PROJECT` (`--aggressor`, `--out`, `--no-board-fields`,
    `--json`, `--html`) writes, per aggressor, a CSV of one period, the waveforms PNG and the
    field-map PNGs, and `<project>_noise_summary.json`.
  - App: the **Ripple & noise** tab (aggressors, phases with **Fill from board**, victims with
    **Guess ends**; **Solve noise**, Ctrl+Shift+N; export).
  - Agents: the MCP tool `solve_noise`. CI: the Action's opt-in `noise` input.
  - Example: `examples/noise`. Guide: [docs/noise.md](docs/noise.md).
- **Tolerance stack-up per load**, each side of nominal apart: the DC drop on its worst pins
  (from the power tree) plus the source's DC accuracy, and the ripple (from the noise solve, or
  your `ripple_mv`) plus your `transient_mv`, against your `tol_*`, `dc_tol_*` and `ac_tol_*`
  (on loads; `accuracy_*` on sources; an optional `tolerance:` block). A check passes only when
  every part is known: a missing part is never counted as 0, and a misspelt tolerance key is a
  warning and leaves the load not judged. KiPI's transient estimate is shown, never judged.
  - Command line: `coppernicus tolerance PROJECT` (`--skip-ac`, `--skip-noise`, `--out`,
    `--no-board-fields`, `--json`, `--html`) writes `<project>_tolerance.csv` and
    `<project>_tolerance_summary.json`.
  - App: the **Tolerance** tab, built from the results the window holds; a value a KiCad field
    gives (`PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL`, `PI_ACC`) is read-only there, naming the field.
  - Agents: the MCP tool `solve_tolerance`. CI: the Action's opt-in `tolerance` input.
  - Example: `examples/tolerance`. Guide: [docs/tolerance.md](docs/tolerance.md).
- **Board temperature in the app:** the **Temperature** tab (options and limits, the hottest
  copper of each layer with the nearest part, the history of the iterations) and a **Board
  temperature** map with a hover readout.
- **Board temperature for agents and CI:** the MCP tool `solve_tree` takes `thermal` and
  `thermal_options` (which imply `thermal: true`). The power-tree HTML report gains a Board
  temperature section with each layer's map. With the Action's `tree` input, a project that
  enables the temperature is judged by it (exit 1 on a fail, 3 when not judged).
- **The cost first, for heavy solves.** The board temperature, the impedance and the noise say
  how large the solve is and how much memory it may need before they start. The app asks
  before a solve that could exceed this machine's memory and can cancel a running one, the
  command line warns, and an MCP call is refused (`solve_too_large`) until it is repeated with
  `confirm_large: true`.
- **Regulator ratings:** a source's rated output current (KiCad `PI_IMAX`, or `current_limit_a`
  in the project file) fails in the power tree when its output current is above it, naming
  where the rating was written. It is not judged while the solve has not settled there, nor on
  a rail the tree does not solve. An **I max** column sits next to I out in the app's Power tree
  tab and in the command line's table.
- **KiCad fields** ([docs/kicad-fields.md](docs/kicad-fields.md)):
  - part data on the library symbol, with no `PI_RAIL`: `PI_KIND`, `PI_EFF`, `PI_IQ`, and the
    new `PI_VOUT` (a fixed output voltage), `PI_IMAX` and `PI_FSW` (a switching frequency, used
    by the noise solve when the aggressor gives none);
  - `PI_SOURCE_REF`: the part whose pads hold the rail, for an external-inductor buck, or a list
    of parts that make one source terminal, such as cell holders of a battery in parallel
    (project key `extra_pins`);
  - `PI_GND_REF` for ground pins on another part, and `PI_LINK_PINS` for a link on a part with
    more than two nets, such as a load switch;
  - capacitor models: `PI_ESR`, `PI_ESL`, `PI_DIELECTRIC`, `PI_VRATED`, and `PI_C` (else the
    footprint Value, when it gives its unit), used by the impedance and the noise. A model is
    never completed with a default: with a field missing the capacitor has no model, and what
    depends on it is not judged, naming the field;
  - tolerances and accuracy: a load's `PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL` and a source's
    `PI_ACC`, for the tolerance stack-up;
  - `coppernicus fields` and the MCP tool `board_fields` also list each capacitor's model (or
    what it lacks) and the parts whose tolerance fields have no `PI_RAIL`;
    `coppernicus fields --propose` and `propose_fields` propose the model and tolerance fields.
- **Current-density default:** without a `max_current_density`, a crowding spot whose mean |J|
  is above 60 A/mm² at 1 oz, scaled ×√(35 µm / t) for its layer's copper, is listed as
  ATTENTION: amber in the app and the HTML report, counted in the command line's text, the JSON
  documents and the CI summary. It never changes a verdict, a `not_judged` list or an exit
  code. A `max_current_density` you set replaces it. The reasons and sources are in
  [docs/technical.md](docs/technical.md#current-density-default).
- **HTML pages:** `coppernicus ac`, `noise` and `tolerance` take `--html` for one offline page
  each, and a link ending in `#group=NET` opens that rail or group of a report.
- **Power-tree documents** (`solve --tree --json`, MCP `solve_tree`): each group problem carries
  its detail, its place on the board and the limit that judged it (`limit_pct` / `limit_mv`,
  `limit_from`), and load rows name the net their pins are on and the rail they are listed in
  (`net`, `rail`).
- **Packaged app:** **Help → Remove downloaded mesher (Triangle)…**.
- **Examples:** `examples/pdn`, `examples/noise` and `examples/tolerance`, synthetic boards
  (the first two after KiPI's validation boards), each with a passing and a failing project.

### Changed

- HTML reports open in a dark theme, with a **☀ Light / ☾ Dark** button in each page's header
  (remembered where the browser allows it). The ac, noise and tolerance pages use the same
  colours as the rail report; plots embedded in a page sit in a light frame.

- A project that sets `thermal: max_temp_c` or `max_rise_c` and is solved rail by rail
  (`coppernicus solve` without `--tree`, the MCP tool `solve_project`, the Action without
  `tree`) now lists each of these limits as NOT JUDGED ("solved with the power tree only") and
  exits 3. It used to pass silently.
- Power-tree HTML report: a group's loads table and worst-load card show what the tree judged
  (the voltage across supply and ground pins, the drop from nominal) instead of the rail's
  copper drop, so they agree with the verdict. Tree groups no longer carry the rail-alone note
  that the ground return is not included, and nets joined by a link are named as such.
- `coppernicus fields` and the MCP tool `board_fields` call a rail complete exactly when a
  solve takes it (loads behind a link, a source that feeds a link or a regulator input), and
  say what completes it (`complete_through`).
- MCP `write_project`: an agent's copper-weight step is always counted from the board file's
  thickness, and `settings.copper_thickness_mm` is merged layer by layer (a layer set to `null`
  goes back to the board file's copper).

### Fixed

- App: a limit cleared from the keyboard means "no limit" and reaches the project. Clearing the
  via plating, a regulator's efficiency or its quiescent current is written back too.
- App: the Rail setup's number fields and drop-down lists change with the mouse wheel only when
  they have the focus, so a wheel turn over the panel no longer changes a value.
- App: **Export report** starts in `~/Documents/Coppernicus/results` when the `results/` folder
  next to the project cannot be written (a project opened from the disk image, for example),
  and says why.
- Packaged app: removing the downloaded Triangle while its files are in use (on Windows, or
  with another Coppernicus window open) no longer fails: the folder is removed at the next
  start (`--remove-triangle` and the new Help menu item).
- `coppernicus solve --tree` and `--thermal` name their files after the project file as the
  other commands do, its `-` kept: `demo-thermal.coppernicus_tree_summary.json`, no longer
  `demoNthermal.coppernicus_tree_summary.json` (the same for the temperature PNGs and the tree
  report). `+` and `-` still become `P` and `N` in net names only (`+3V3` → `P3V3`).

## 0.2.0 (2026-11-25)

The whole power tree with its ground return, the power data read from KiCad footprint fields,
the board temperature on the command line, and a packaged app.

### Added

- **Power tree with ground return:** every rail and ground net of a project solved together.
  Each regulator (LDO or switcher, with its efficiency and quiescent current) is a load on the
  rail that feeds it, remote sense holds a regulator's set point at the sensed pins, the return
  current raises the ground, and a load's drop is measured across its supply and ground pins.
  Limits apply per net, regulator inputs are judged as loads, and what cannot be judged is
  named as not judged, never passed.
  - App: **Solve power tree** (Ctrl+T), the **Group** selector, the **Power tree** tab, and the
    regulator box in the Rail setup.
  - Command line: `coppernicus solve PROJECT --tree` (with `--json`, and `--html` for one page
    with every group).
  - Agents: the MCP tool `solve_tree`. CI: the Action's opt-in `tree` input adds the tree's
    tables to the job summary.
- **Ground-rise limit** `max_ground_rise_mv` for a ground net. Each limit judges only its own
  kind of net; one set on the other kind is listed as not judged.
- A rail solved on its own also draws the regulator inputs and links it feeds, estimated at
  nominal voltages and listed as "U1 (input, estimated)".
- **KiCad footprint fields:** sources, loads, regulators, links and a drop limit written on the
  parts (`PI_RAIL`, `PI_SOURCE`, `PI_LOAD`, `PI_PINS`, `PI_LIMIT_DROP`, `PI_KIND`, `PI_EFF`,
  `PI_IQ`, `PI_INPUT`, `PI_GND`, `PI_LINK`…), read by default from `.kicad_pcb` files by the
  app, the command line, the MCP server and the Action. They win over a project rail of the same
  net unless that rail sets `override_board_fields: true`; `use_board_fields: false` or
  `--no-board-fields` turns them off.
  - `coppernicus fields BOARD` lists them (`--json`), and `coppernicus fields --propose PROJECT`
    prints the fields that describe a project, to paste into KiCad.
  - In the app, rails from KiCad are marked **[KiCad]** and read-only, with **Override in
    project**, **Project → Use KiCad footprint fields** and **File → Reload board**
    (Ctrl+Shift+R).
  - Example: `examples/fields`.
- **Board temperature** on the command line: `coppernicus solve PROJECT --thermal` (or
  `thermal: enabled: true` in the project) heats the copper with its own losses and solves the
  tree again hot. It gives a temperature map per copper layer, the hottest copper of each layer
  and each load's drop hot, judged only against `max_temp_c` / `max_rise_c`. Example:
  `examples/demo/demo-thermal.coppernicus.yml`.
- **Copper-thickness what-if in the project file** (`settings: copper_thickness_mm`, at most
  1 mm per layer), used by every solve of the project and edited in the app under **Copper
  thickness → Edit…**.
- MCP tools `board_fields`, `propose_fields`, `read_project` and `write_project` (set up and
  edit a project file), `compare_revisions` (two revisions of a board, before and after a change
  in KiCad) and `copper_step` (one copper layer one standard weight up or down).
- **Hover readout** next to the pointer in the app (**View → Hover** chooses the values).
- **Exit code 3** on the command line and in CI: a limit you set could not judge something.
  Results list each such item and the reason (`not_judged`) in the JSON, the text, the HTML
  report and the CI summary, and the Action's `fail-on-not-judged` input (on by default) fails
  the check on it.
- **Packaged app:** a macOS disk image (Apple silicon) and a Windows zip, built by the release
  workflow, unsigned. Triangle is not bundled: the app downloads it from PyPI on its first
  start, checks it against the hash `uv.lock` pins, and then works offline; `--remove-triangle`
  deletes it. See [docs/install.md](docs/install.md).

### Changed

- A rail solved on its own now also carries the current of the regulator inputs and links it
  feeds (estimated at nominal voltages), so its drops grow: a check that passed with 0.1.0 can
  fail.
- A limit you set that cannot judge something (a % drop limit on a 0 V net, a limit of the
  wrong kind, a rail that was not solved) is no longer passed silently: the command line exits
  3, and the Action fails the check unless `fail-on-not-judged: false`.
- An agent's copper what-if in the MCP tool `solve_rail` takes only a standard weight at most
  one step from the design's thickness on that layer; anything else is refused and is a change
  to make in KiCad.
- The window opens no larger than the screen's free area and fits a 1440 px screen (the map
  bar on two rows).

### Fixed

- Negative rails and nets at 0 V solved on their own show their real voltages: a negative
  rail's loads sit toward 0 V, a ground rises above 0 V, and a ground below 0 V reads as a
  ground shift. Loads on a net at 0 V are ranked by their drop in mV, with no "nan %".
- The double-click readout reads exactly the point under the pointer.
- A rail solve that finishes after its setup changed is dropped, with a message.
- An unexpected error on the command line exits with 2 and a one-line message, never with 1,
  which means a limit was exceeded.
- The project loader refuses NaN numbers.

## 0.1.0 (2026-11-24)

First public release: DC voltage drop and current density for KiCad boards.

### Added

- **Desktop app.** Open a `.kicad_pcb` file (only read; KiCad need not be running), choose a
  rail (power and ground nets are recognised by name and copper pours, or marked by hand and
  saved in the project), pick the sources and loads on the board, type voltages, currents and
  optional limits (maximum drop %, current density, via current), and **Solve** (F5).
  - Voltage-drop, voltage and current-density maps per copper layer, drop isolines,
    current-flow arrows, a double-click readout and **Measure ΔV**.
  - The **Problems** tab: every load by drop, the most crowded copper, the busiest vias and the
    assumptions; click a row to zoom to it.
  - Copper loss per layer and in the vias (**Loss by layer**), and **Pin as baseline** with the
    **Compare** tab and a Δ drop map.
- Pass or fail only against the limits you set; without limits, results are ranked.
- Net ties found automatically and solved as shorts.
- **Project file** (`*.coppernicus.yml`): the board, the rails with their sources, loads and
  limits, and the solve settings. Regulator kinds, input, ground and sense pins, and links can
  be written in it.
- **Command line:** `nets`, `parts`, `solve` (a report per rail, a PNG map per layer and a JSON
  summary; `--json`, `--top`, `--rail`, `--no-maps`), `gui` (`--solve`, `--field`, `--layer`)
  and `mcp`. Exit codes: 0 no limit exceeded, 1 a limit exceeded, 2 an error.
- **Offline interactive HTML report:** every rail and copper layer with zoom, pan and a readout
  under the mouse, plus the problems, the worst values and the assumptions. From
  `solve --html` (`--open` opens it), the app's **Export report** (Ctrl+R) and the MCP tool
  `report`.
- **MCP server** (`coppernicus mcp`, over stdio) with 11 tools: `open_board`, `list_nets`,
  `net_parts`, `solve_rail`, `get_problems`, `render_map`, `probe`, `cut_current`,
  `compare_results`, `solve_project` and `report`. `solve_rail` takes what-if settings: via
  plating, copper temperature and copper thickness.
- A **Claude Code skill**, `skills/analyzing-pdn-with-coppernicus`.
- A **GitHub Action** for KiCad repositories: it solves the project files, writes a job
  summary, attaches the maps and the HTML report, and fails the check on an exceeded limit.
- Synthetic examples `examples/demo` and `examples/power_chain`.
- `tools/make_mac_launcher.sh`: a double-clickable macOS app that runs your clone.
