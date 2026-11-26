# Changelog

What changed in each release of Coppernicus, for people who use the desktop app, the command
line, the MCP server or the GitHub Action. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Coppernicus is pre-alpha: check its
results against your own judgement.

## Unreleased

### Added

- A source's ground terminal on several parts, as its output already could be: KiCad field
  `PI_GND_REF=BT4,BT8` on a source (`PI_GND_PINS` with `REF.PAD` entries or a pad for every
  listed part), project key `extra_ground_pins: [BT8.*]` beside `ground` (also in
  `write_project`). In the power tree every pad of every listed part is held at 0 V together
  and the copper shares the return between them: a battery whose negative pole is two cell
  holders no longer forces the whole return through one holder, which overstated the ground
  rise and the ground copper's loss. Ideal supplies (`kind: source`) only; a single ground part
  gives the same results as before.
- `COPPERNICUS_DC_DIRECT_MAX=<unknowns>` (opt-in) solves DC systems above KiPI's 200,000-unknown
  direct limit with a direct factorization instead of AMG-CG, within KiPI's SuperLU limit and
  half the free memory; results equal within 1e-9 (CG's tolerance).
- Agents can probe, map and cut the power tree: the MCP `solve_tree` returns `tree_id` (`t1`)
  and a `result_id` per group (`t1:GND`), which `probe`, `render_map` and `cut_current` take;
  `report(result_ids=["t1"])` writes the tree's page. A probe on a group says its group, kind
  and whether the number is a drop, a ground rise or a shift; `relative_to` (a point or a pad,
  `U3.2`) gives the voltage between two points of the same solve, across groups too (a chip's
  supply pad against its ground pad). The tools for one rail result refuse tree ids with a hint.
  The tree counts in the session's memory bounds and is dropped whole. On the command line,
  `coppernicus solve PROJECT --tree --probe LAYER@X,Y` (repeatable) prints the readouts
  (`probes` in the JSON).

### Changed

- The hover label in the app leads with the value of the map shown, as one large number: the
  drop (with its % of the source voltage), V, |J|, the change of drop on the Δ map, or the
  board temperature. It switches at once when Show changes under a still pointer.
  **View → Hover shows** chooses the values under it: the layer by default; a choice of your
  own saved by 0.3.1 is kept under the new number, and a readout turned off stays off. The
  label follows the app's light or dark look.

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
