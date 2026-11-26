# Coppernicus for AI agents

Coppernicus can be driven by an AI agent (Claude Code, Claude Desktop or any MCP client) that
runs the same DC analyses as the app and reasons about the results. There are two ways in:

- **The MCP server** (`coppernicus mcp`). This is the main way. Boards and results stay in
  memory between calls and maps come back as images.
- **The command line with `--json`.** This is the fallback for an agent that only has a shell.

Both go through the same service layer (`src/coppernicus/service/`) and return the same data.

The Claude Code skill [`skills/analyzing-pdn-with-coppernicus`](../skills/analyzing-pdn-with-coppernicus/SKILL.md)
teaches an agent the workflow and the rules (verdicts only against the user's limits, the
current-density default as attention only, mean |J| rather than peaks).
Install it with `cp -r skills/analyzing-pdn-with-coppernicus ~/.claude/skills/`.

## Register the server

The server speaks MCP over stdio. It opens no network port. Use absolute paths everywhere,
because a host starts the server from its own working directory.

**Claude Code.** `-s user` makes the server available in every project:

```sh
claude mcp add -s user coppernicus -- \
  uv run --project /absolute/path/to/Coppernicus coppernicus mcp --out /absolute/path/to/coppernicus-maps
```

Run `/mcp` in a Claude Code session to check that `coppernicus` is connected and lists its
tools.

**Claude Desktop.** Add an entry to `claude_desktop_config.json`, then fully quit Claude Desktop
and reopen it. The file is in `~/Library/Application Support/Claude/` on macOS and in
`%APPDATA%\Claude\` on Windows. Claude Desktop starts servers with a minimal `PATH`, so give the
absolute path to `uv` (from `which uv`):

```json
{
  "mcpServers": {
    "coppernicus": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--project", "/absolute/path/to/Coppernicus", "coppernicus", "mcp",
               "--out", "/absolute/path/to/coppernicus-maps"]
    }
  }
}
```

**Options of `coppernicus mcp`:**

| Option | Default | Meaning |
|---|---|---|
| `--out DIR` | a new folder under the system temp directory | Where maps are written, in one `session-YYYYMMDD-HHMMSS-PID` subfolder per server run. Maps never go next to a board. |
| `--log-file FILE` | stderr | The server log. Nothing is ever written to stdout, which carries the protocol. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `--max-results N` | 8 | How many solved results keep their fields (meshes, voltages, current density) in memory. |
| `--max-result-mb MB` | 2048 | Memory bound for those fields. |

To check the launch command by hand, run it in a terminal. It should print nothing and wait
for a client on stdin; stop it with Ctrl-C.

## Tools

These are the tools that `src/coppernicus/mcp_server.py` registers. `?` marks an optional
input.

| Tool | Inputs | What it returns |
|---|---|---|
| `open_board` | `path` | Opens a board read-only. Returns `board_id`, copper layers with thickness, counts, the outline bounding box and import warnings. Reopening an unchanged file returns the same `board_id`. |
| `list_nets` | `board_id`, `kind`?, `filter`?, `limit`? | Nets with their kind (`power_or_ground` by default, or `power`, `ground`, `signal`, `all`) and their copper layers: `layers` (all touched), `layers_with_copper` (tracks or zones) and `layers_with_only_vias_or_pads`. Through vias touch every layer, so `layers_with_copper` is the guide to where a rail runs. `filter` is a case-insensitive substring, or a glob over whole names such as `+3V*`. |
| `board_fields` | `board_id` | The board's KiCad footprint fields (`PI_*`; `.kicad_pcb` only) and the rails and links they describe, each value with the field it came from. Each rail's `complete` is judged as a solve judges it, from the fields alone (their `PI_LINK` links and the board's net ties, no project file); a rail completed by a link, a net tie or a regulator input it feeds names it in `complete_through` and its `note`; `missing` says why an incomplete one is. With any, `capacitors` gives each capacitor's model from its library fields (`PI_ESR`, `PI_ESL`, ...) or what it lacks, and `part_tolerances` the parts whose `PI_TOL` / `PI_ACC` has no `PI_RAIL`. Nothing is solved. See [KiCad footprint fields](#kicad-footprint-fields). |
| `net_parts` | `board_id`, `net` | Parts on the net with value, footprint and pins, used to choose sources and loads. Also the net ties that join the net to others, which are solved as shorts. |
| `solve_rail` | `board_id`, `net`, `sources`, `loads`, `limits`?, `settings`?, `follow_net_ties`?, `top`? | Solves one rail. A source is `{ref, voltage_v, pins?, name?}`; a load is `{ref, current_a, pins?, name?}`. Returns `result_id` and a summary: load voltages and drops, max drop, loss per layer, busiest layer, top problems, verdict. |
| `get_problems` | `result_id`, `limits`?, `top`? | Ranked problems: loads by drop, current-crowding spots by mean \|J\|, vias by current. Each has a layer and x/y in mm. Pass `limits` to re-judge without solving again. Rail results only (a tree group's problems are in `solve_tree`'s `groups[].problems`). |
| `render_map` | `result_id`, `layer`?, `field`?, `region_mm`?, `dpi`? | A map of `drop` (mV), `voltage` (V) or `current` (A/mm²) on one layer. It comes back as an image and as a PNG file whose path is returned. The default layer is the one with the most copper loss. `region_mm = [x_min, y_min, x_max, y_max]` zooms. Takes a tree group's id too (`t1:GND`, see [Probing, mapping and cutting tree groups](#probing-mapping-and-cutting-tree-groups)). |
| `probe` | `result_id`, `layer`, `x_mm`, `y_mm`, `relative_to`? | Voltage, drop and current density (magnitude and vector) at a point. Off copper, it says which layers have copper there and where the nearest copper is. On a tree group (`t1:GND`) it adds `group`, `kind`, `drop_word`, `drop_unit` and the exact `net`. `relative_to` (`{layer, x_mm, y_mm}` or `{pad}`, of the same solve) adds `delta_v`, the voltage between the two points, across groups too. |
| `cut_current` | `result_id`, `layer`, `x1_mm`, `y1_mm`, `x2_mm`, `y2_mm` | A cross-section along a segment: the copper width crossed, the current through it (∫ J·n t ds, signed along `normal`), and the mean and max \|J\|. Use it to measure a neck. Takes a tree group's id too. |
| `compare_results` | `result_a`, `result_b` | What-if deltas b − a: settings changed, max drop, loss, drop per load, loss per layer, and both verdicts. |
| `compare_revisions` | `result_a`, `result_b` | Two revisions of a board (different board files, or the same file edited and saved in KiCad, then opened again), whose meshes differ: per-load drop deltas matched by part, per-layer loss deltas, via currents matched by position and drill, where the drop grew or shrank most (B's drop mapped onto A's mesh, a PNG Δ map per layer), and what could not be matched. See [Comparing revisions](#comparing-revisions). |
| `copper_step` | `result_id`, `layer`, `step` (`up`/`down`) | What-if: the result re-solved with one copper layer **one** standard weight up or down (0.5 oz 0.0175 mm, 1 oz 0.035, 2 oz 0.070, 3 oz 0.105), from the design's own thickness (the project's `copper_thickness_mm`, else the board file's), with the comparison against that result and the new `result_id`. A project rail keeps its estimated draws. Bigger jumps, a non-standard start (more than 3 % off a standard weight) and a result that is already a what-if (any layer off the design's copper) are refused: real changes are made in KiCad. The assumptions carry the via-barrel note. |
| `solve_project` | `project_path`, `rails`?, `top`?, `use_board_fields`? | Solves the rails of a `*.coppernicus.yml` project with its own sources, loads, limits and settings, and the rails of the board's KiCad footprint fields, which win over a project rail of the same net (see [KiCad footprint fields](#kicad-footprint-fields)). Returns one `solve_rail` summary per rail and an overall verdict. A rail that fails to solve is reported in place. Each rail also draws the regulator inputs and links it feeds, estimated at nominal voltages without copper losses, exactly as the desktop app solves it (see [Estimated draws](#estimated-draws)). A rail with no loads that feeds nothing is reported as `skipped`; that is not an error, and it is left out of the verdict. The board temperature is a power-tree solve: when the project sets `thermal.max_temp_c` or `max_rise_c`, each is a top-level `not_judged` entry (`rail: null`, `item: "board temperature"`, "solved with the power tree only…"), so the verdict is `not_judged` (CLI and CI exit 3) unless something fails; use `solve_tree` to judge it. |
| `solve_tree` | `project_path`, `top`?, `use_board_fields`?, `thermal`?, `thermal_options`?, `confirm_large`? | Solves the project's whole power tree at once: every rail and ground group together, with the regulators' input currents and the ground return computed exactly (when a ground net is solved, every load and regulator needs its ground pins). Returns the same document as `coppernicus solve --tree --json` (without `summary_file`), plus `tree_id` (`t1`) and each group's `result_id` (`t1:GND`) for `probe`, `render_map`, `cut_current` and `report`: see [Power tree](#power-tree). Limits come from the project file. With `thermal` (or the project's `thermal.enabled`) it also solves the board temperature: see [Board temperature](#board-temperature). |
| `solve_ac` | `project_path`, `rails`?, `confirm_large`?, `use_board_fields`? | The open-loop PDN impedance \|Z\|(f) at the project's probes, rail by rail: the document of `coppernicus ac --json`, with each rail's CSV and PNG of \|Z\|(f), sized first. See [AC impedance](#ac-impedance). |
| `solve_noise` | `project_path`, `aggressors`?, `confirm_large`?, `use_board_fields`? | The DC-DC ripple and the noise each switching regulator (aggressor) couples into the victim nets: the document of `coppernicus noise --json`, with each aggressor's CSV, waveforms PNG and field-map PNGs, sized first. Judged only against the victims' noise margins; the ripple and the radiated estimate are never judged. See [Ripple and coupled noise](#ripple-and-coupled-noise). |
| `solve_tolerance` | `project_path`, `skip_ac`?, `skip_noise`?, `confirm_large`?, `use_board_fields`? | Each load's tolerance stack-up (DC drop + accuracy, ripple + transient, their total): the document of `coppernicus tolerance --json`, with its CSV, every AC and noise solve it needs sized first. Judged only against the tolerances the user set, and only when every part is known; KiPI's transient estimate is shown, never judged. See [Tolerance stack-up](#tolerance-stack-up). |
| `propose_fields` | `project_path` or `result_id` | The KiCad `PI_*` field text that describes a project's rails and links (or one solved rail), part by part, to paste into KiCad. See [Proposing fields](#proposing-fields). |
| `read_project` | `project_path` | A project file as written (rails, sources, loads, limits, links, settings; unknown keys included), checked by the project loader, with the board it names. |
| `write_project` | `project_path`, `board_path`?, `rails`?, `remove_rails`?, `links`?, `remove_links`?, `settings`?, `use_board_fields`?, `from_project`?, `overwrite`? | Creates or edits a project file for the user. See [Editing projects](#editing-projects). |
| `report` | `result_ids`?, `path`? | Writes the offline HTML report (see [below](#html-report)) of the given results, by default every rail result still in memory, and returns its path, size and cell sizes. A tree's id alone (`["t1"]`) writes the power tree's page instead (every group, the VRM, load and link tables). It is meant for a person to open in a browser. |

A summary's `problems` list is complete: every load, then the `top` current-crowding spots and
the `top` vias by current (default 5 each), plus any that exceed a limit you set. Vias carrying
under a millionth of the rail current are left out, and so is a layer whose current density
stays under a millionth of the rail's peak (copper of the net that no current crosses, such as
a through-hole pad's ring on a layer with no load). That is solver round-off, and leaving it
out is not a judgement.

Each load has `v_min_v`, `v_max_v` and `v_worst_v`: the pin furthest from the source in the
direction of the drop, the lowest on a positive rail and the highest on a negative rail or a
0 V net, so read `v_worst_v`, not `v_min_v`. The summary's `drop_sign` (+1 or −1) gives that
direction: drop = `drop_sign` · (`v_ref_v` − V). The HTML report carries the same, per rail
(`drop_sign`, `drop_unit`, `drop_word`) and per load (`v_worst_v`), with the drop as the app
shows it: a ground below 0 V (under a negative rail) reads as a **ground shift**, its depth
below 0 V, with `drop_sign` +1 and `drop_unit` "mV below 0 V (ground shift)".

On a **ground rail**, which is solved with the source at 0 V, `v_ref_v` is 0. A percentage has
no meaning there: `max_drop_pct` and `drop_pct` are `null`, and a `max_drop_pct` limit cannot
judge its loads (they are listed in `not_judged`). Give the drops in mV, and judge a ground
with `max_ground_rise_mv`. On a **negative rail** percentages are of |`v_ref_v`|, so they are
positive like the drops.

`solve_rail` settings, all optional:
- `max_edge_mm`: the largest mesh edge on planes (default 0.5). It does not refine the mesh
  at plated holes; the next two do.
- `min_edge_mm` (advanced): the smallest mesh element, at a plated hole's wall, in mm
  (default: KiPI's 0.02).
- `grading` (advanced): how fast elements grow away from a plated hole, mm of edge per mm of
  distance, 0 (off) to 2 (default: KiPI's 0.2). Set both smaller (e.g. 0.01 and 0.1) to check
  whether a small current-density spot at a via is resolved: a resolved one keeps its area and
  mean |J|. Left out, KiPI's defaults apply and results are unchanged.
- `via_plating_um`: plating for every plated hole. The default is the engine's table by hole
  type.
- `copper_temp_c`: copper temperature for ρ(T) (default 20).
- `copper_thickness_mm`: an optional what-if thickness per copper layer name, in mm, for
  example `{"B.Cu": 0.07}` (1 oz copper is 0.035, not 35). The board file is not changed: a
  real change of copper weight is made in KiCad, which then becomes the board that is solved.
  An unknown layer name is an error (`layer_not_found`) whose message lists the board's copper
  layers.

  **One standard step at most.** In `solve_rail` (the agent's what-if), each layer takes only
  a standard weight (0.5, 1, 2, 3 oz: 0.0175, 0.035, 0.070, 0.105 mm, within 3 %) at most
  one step from the design's thickness on that layer, or the design's own value. The design's
  thickness is the board file's (an in-process caller's `base_settings` give a project's).
  From a design that is not a standard weight, no step is offered. Anything else is refused
  (`invalid_input`); the hint points to `copper_step` and to KiCad. `write_project` applies
  the same rule, from the board file's thickness, to the layers you change in
  `settings.copper_thickness_mm` (see
  [Editing projects](#editing-projects)). A project the user edits (the app, by hand, the
  CLI) keeps the free value (more than 0, at most 1 mm): that is the user's own what-if.

  A project file can hold the same setting under `settings:` (see
  [Project files](#copper-thickness-in-project-files)). The `solve_rail` tool has no project,
  so it uses only the thicknesses you pass. An in-process caller that passes a project's
  settings as `base_settings` gets them merged layer by layer: a layer you name overrides the
  project's, and the other layers keep the project's value.

  The stack is rebuilt around the new thickness, so via barrels get longer when the stack
  thickens (shorter when it thins). Even a net with no copper on the changed layer then moves
  slightly through its vias. The result's assumptions say which layers were overridden, with
  the board file's value next to each.

### Comparing revisions

To see what a change in KiCad did: solve the rail on the board before the change (`result_a`),
let the user change and save the board, open it again (a changed file gets a new `board_id`;
the earlier result keeps its own board), solve the same rail (`result_b`), then
`compare_revisions(result_a, result_b)`. It returns:
- `loads`: per load, matched by name (the part, unless named), the drops and `delta_drop_mv`;
- `losses`, `max_drop_mv`, `power_loss_w`: as `compare_results`;
- `vias`: `matched` by position (within 0.01 mm) and drill, with both currents and the change,
  `largest_changes`, and `only_in_a` / `only_in_b`;
- `drop_map`: B's drop interpolated onto A's mesh, minus A's: `most_increased` and
  `most_decreased` (layer, x/y in mm, mV; null when nothing grew, or shrank, beyond
  round-off), and the `copper` only one of them has, as areas both ways. When less than half
  of A's copper is also copper in B, no point is named;
- `maps`: a PNG per common layer, red where the drop grew, blue where it shrank;
- `unmatched`: `loads_only_in_a`, `loads_only_in_b`, `moved` (a part's first pad moved more
  than 0.01 mm), `nets` (when the rail's net was renamed).

It warns when the load currents, the sources or the settings differ, since the deltas then mix
copper and those. Both results must still have their fields (`result_evicted` otherwise).

### Editing projects

`write_project` sets up a project for the user, so they can solve it again with the CLI, the
app or CI. It writes one file, at the path you give (a `*.coppernicus.yml`):
- it starts from `from_project` (to save an edited copy elsewhere), else the file at
  `project_path` when it exists, else an empty project for `board_path`;
- `rails` are added, or replaced **whole** by net (sources with their v2 fields: `kind`,
  `efficiency`, `quiescent_a`, `input`, `ground`, `sense`, `ground_sense`, `extra_pins`,
  `extra_ground_pins`; loads with
  `ground`; `limits`; `follow_net_ties`; `override_board_fields`); `remove_rails` removes by
  net; `links` and `remove_links` work by name; `settings` keys are set, and null removes one
  (its default again); `use_board_fields: false` turns the KiCad fields off;
- `settings.copper_thickness_mm` is merged layer by layer: the layers you name are set, a
  layer set to null goes back to the board file's copper, the others stay as they are. Each
  layer you change takes a standard weight at most one step from the **board file's**
  thickness, never from a value already in the project. Giving back the value the project
  already holds is not a change, so a value the user typed stays as it is. Anything else is
  refused (`invalid_input`) and nothing is written. `copper_thickness_mm: null` removes every
  layer's value;
- an existing file is replaced only with `overwrite: true`, else `file_exists`;
- the file is rewritten from its data: **comments are lost**, keys Coppernicus does not know
  are kept (a replaced rail's included); the board path is written relative to the project's
  folder;
- the result goes through the project loader before the file is replaced (a temporary file
  beside it, then a rename): a refused edit (`invalid_project`) leaves the old file as it was;
- the board file is only read, never written;
- when the board's KiCad fields define a rail you wrote and it differs from theirs, the warning
  a solve would give says they win (the rail is ignored, with the differences) and that
  `override_board_fields: true` on it uses your values; a rail equal to the fields' gives no
  warning. A malformed field is a warning too, since this is how the user turns the fields off.

Ask the user before you overwrite their project file, and tell them where you saved it.

### KiCad footprint fields

The power data can live on the parts' symbols in KiCad, as fields. KiCad 8 and later copy a
symbol's fields onto its footprint, so they reach the board file; Coppernicus reads them from
the `.kicad_pcb` (only; other formats have none) and never writes it. They are **read by
default** whenever the board has `PI_*` fields; a board without them behaves as before. To
turn them off: `use_board_fields: false` in the project file, `--no-board-fields` on
`coppernicus solve`, `ac`, `noise` or `tolerance`, or `use_board_fields: false` on the
`solve_project` / `solve_tree` tools.

| Field | Example | On | Meaning |
|---|---|---|---|
| `PI_RAIL` | `+3V3` | source, load | The net the part is a source or load on. Required with every per-rail field below. Several rails: `VIN;+3V3`. |
| `PI_SOURCE` | `3.3V` | source | The part holds the rail at this voltage (a regulator output, a connector). |
| `PI_LOAD` | `2A`, `250mA` | load | The part draws this current, split equally across its pins on the rail. |
| `PI_PINS` | `1,2`, `A*` | source, load | Which pads take part, by number or glob. Default: every pad of the part on the rail. |
| `PI_LIMIT_DROP` | `2%` | source, load | A drop limit for the rail; the strictest one on a rail is used. |
| `PI_KIND` | `ldo` | source | `source` (an ideal supply, the default), `ldo` or `smps`. |
| `PI_EFF` | `90%` | switcher | Its efficiency; required with `PI_KIND` `smps`. |
| `PI_IQ` | `2mA`, `50uA` | regulator | Its quiescent current (`A`, `mA`, `uA` or `µA`). |
| `PI_INPUT` | `VIN` | regulator | The net it draws from (its pads there); required with `ldo` and `smps`. `PI_INPUT_PINS` chooses the pads. |
| `PI_GND` | `GND` | source, load | The ground net of its ground pins (the return path the power tree solves). `PI_GND_PINS` chooses the pads. |
| `PI_LINK` | `50mOhm` | a two-net part | A link (ferrite, sense resistor, fuse) of this resistance between the nets of its pads (`Ohm`, `mOhm`, `Ω`). Not with `PI_RAIL`. |
| `PI_LINK_PINS` | `1,2;5` | a link part with more nets | The link's two sides on a part with more than two nets (load switch, MOSFET, 3-pin fuse): side A's pads `;` side B's, each side's pads on one net. A two-net part needs none. |
| `PI_VOUT` | `3.3V` | source | A fixed-output part's voltage: the source voltage when the instance has `PI_RAIL` and no `PI_SOURCE` (the assumption says "from KiCad field PI_VOUT"). When both are given and differ, `PI_SOURCE` wins, with a warning. |
| `PI_IMAX` | `2A`, `500mA` | source | Its rated output current: kept as the source's `current_limit_a` and shown next to I_out (rail summary `current_limit_a`, tree `vrms[].i_max_a`, the report and CI tables). **Judged in the power tree:** an I_out above it fails (see [Regulator ratings](#regulator-ratings-in-the-power-tree)); not judged while the solve is unsettled or its rail is not solved. |
| `PI_FSW` | `500kHz`, `2.2MHz` | switcher | Its fixed switching frequency (`Hz`, `kHz`, `MHz`): kept as the source's `fsw_hz`, which the noise solve uses for an aggressor that gives no `fsw_hz` of its own (an assumption line says where it came from). |
| `PI_SOURCE_REF` | `L1`, `BT1,BT5` | source | The part whose pads hold the rail, when it is not this part: an external-inductor buck's inductor. `PI_PINS` then selects that part's pads; input, ground and kind stay on this part. A list of parts (`BT1,BT5`) makes **one terminal**: every listed part's pads on the rail, all held at the source voltage (cell holders in parallel), not parallel regulators; `PI_PINS` then takes `REF.PAD` entries (`BT1.1,BT5.1`), and a plain pad applies to every listed part. An error when a part has no pad on the rail. |
| `PI_GND_REF` | `BT4`, `BT4,BT8` | source, load | The part whose pads are the ground pins on `PI_GND`, when not this part (`PI_GND_PINS` chooses them). A source's takes a list of parts (`BT4,BT8`): **one ground terminal**, every listed part's pads held at the ground's 0 V in the power tree (a battery's negative pole on several holders; project key `extra_ground_pins`); `PI_GND_PINS` then takes `REF.PAD` entries, or a pad for every listed part. A load's is one part; a regulator cannot take a list. An error when a part has no pad on that net. |
| `PI_TOL` | `5%`, `-3%,+5%`, `50mV` | load | Its tolerance band, the load's `tol_pct` / `tol_mv` for the tolerance stack-up: one value is ± that, two are `below,above` (below ≤ 0 ≤ above, one unit: `%` of nominal or `mV` from it). |
| `PI_DC_TOL`, `PI_AC_TOL` | `2%`, `-20mV,+30mV` | load | Its DC and AC shares (`dc_tol_*`, `ac_tol_*`), the same syntax. |
| `PI_ACC` | `1%`, `-1.5%,+2%` | source | Its DC accuracy (`accuracy_pct` / `accuracy_mv`), the same syntax. |
| `PI_C` | `10uF`, `100n` | capacitor | Its capacitance; without it, the footprint Value's, only when the Value gives its unit (`10u`, `4n7`; not `0.1`). |
| `PI_ESR` | `5mOhm`, `5m` | capacitor | Its ESR (`Ohm`, `mOhm`, `Ω`, `mΩ`, or `m` alone). Required for a model. |
| `PI_ESL` | `0.5nH`, `0.5n` | capacitor | Its ESL (`H`, `uH`, `nH`, `pH`, or the prefix alone). Required for a model. |
| `PI_VRATED` | `16V` | capacitor | Its rated voltage: DC-bias derating at the rail's voltage (KiPI's typical curve, 40 % kept at the rating). Required with a class II dielectric. |
| `PI_DIELECTRIC` | `X7R` | capacitor | Required for a model: `C0G` (`NP0`), class II `X5R`, `X6S`, `X7R`, `X7S`, `X7T`, `X8R`, `Y5V`, `Z5U`, or `TANT`, `POLY`, `ALU` (not ceramic: never derated). |

- **Library and instance.** `PI_KIND`, `PI_EFF`, `PI_IQ`, `PI_VOUT`, `PI_IMAX` and `PI_FSW` say
  what the part **is**: put them on the library symbol. They need no `PI_RAIL`: a part that
  carries only them is part data, used once its instance has `PI_RAIL`. With `PI_RAIL`, one
  such value applies to every rail where the part is a source (on a rail where it is a load
  it is not used), or one per rail with `;`. A part that is a source on no rail but carries
  them is refused (a regulator placed as a load). A chip's `PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL`, a
  fixed-output regulator's `PI_ACC` and a capacitor's `PI_C`, `PI_ESR`, `PI_ESL`, `PI_VRATED`,
  `PI_DIELECTRIC` are library data too (below). The other fields say where the part **sits**:
  put them on the schematic instance; they need `PI_RAIL` (`PI_LINK` and `PI_LINK_PINS` stand
  alone). See [How to annotate](#how-to-annotate-a-kicad-library) below.

- **Several rails on one part** give one value per rail, by position, separated by `;`, in
  every per-rail field. An empty value skips that rail: a dual-output regulator is
  `PI_RAIL=+3V3;+1V8`, `PI_SOURCE=3.3V;1.8V`, `PI_KIND=smps;ldo`, `PI_EFF=90%;`,
  `PI_INPUT=VIN;+3V3`. A regulator draws its input through `PI_INPUT`; a `PI_LOAD` on that same
  net is refused as counted twice.
- **Units are required:** a bare number (`2`), a comma (`2,5A`) or a KiCad-style value
  (`3V3`) is refused, with the part, the field and the text in the message. An unknown `PI_*`
  name (a typo such as `PI_LOADS`, or `pi_load` in lower case) is refused too. A blank field
  is not set. Nothing is assumed: a regulator without `PI_INPUT`, a switcher without
  `PI_EFF`, a net with loads but no source is an `invalid_board_field` error naming the
  missing field. A malformed field fails the whole run; its hint names the off switch. An
  error found while solving a field rail (a `PI_RAIL` net not on the board, a part with no pad
  on it, a `PI_PINS` that matches nothing) says the rail came from the fields.
- **Rails.** A net with a `PI_SOURCE` and a `PI_LOAD` is a rail. So is a net with a source and
  no load that feeds a regulator's input or a link (through net ties too): it draws their
  estimated current. Completeness is per group of nets joined by links and net ties:
  - loads on a net that a **net tie** joins to a driven net go into that rail (its solve joins
    the tied copper and finds their pads there). A project-file rail of the tied net that
    holds only limits does not stop this: it stays, limits only. A `PI_LIMIT_DROP` on the
    tied net still wins over its `max_drop_pct`; with no project rail there, it stays on that
    net in a rail that holds only the limit. Per rail, that rail is skipped and its limits
    are listed as not judged. In the power tree a load past a tie is judged by its own net's
    limit, then the rail it is listed in's, then the group's driving net's (field by field);
    the tied net's copper limits count in the group's strictest. The tree's load rows say
    both: `net` (where its pins are) and `rail` (where it is listed);
  - loads behind a **link** (`PI_LINK`, a ferrite), on a net with no source of its own, make
    a loads-only rail, as in a project file. Per rail it is skipped ("its loads are drawn
    through link FB1 by rail +3V3", estimated there; its limits are listed as not judged),
    and the power tree solves it with its driven net.
- **KiCad wins.** For a net the fields define, the fields give the design data: sources,
  loads, pins and converter data. A project-file rail of the same net is **ignored**, with a
  warning that lists the differences ("project file rail +3V3 ignored: KiCad fields define it
  (KiCad U1 2 A, project U1 3 A); set override_board_fields: true on the rail to use the
  project's values"). An identical project rail gives no warning: that is the normal case once
  the project has been saved with the fields' rails. Pins are compared as the pads they select,
  so `pins: [1]` on a one-pad part equals "every pin".
- **`override_board_fields: true`** on a project rail keeps that rail entirely instead; its
  summary says `"overrides_board_fields": true` with the KiCad values it overrode in
  `board_fields_overridden`, and its assumptions say so.
- **Limits:** a `PI_LIMIT_DROP` always applies to its net, whether the fields' rail is
  complete, incomplete (the project rail is used) or overridden: a project rail cannot loosen
  a KiCad limit. It wins over the project's `max_drop_pct` (a warning when they differ; the
  assumptions say so). `max_current_density`, `max_via_current` and `max_ground_rise_mv` come only
  from the project file, and they apply to the fields' rail: give them on a project rail with
  only `limits:` (no sources, no loads), which is not compared with the fields and gives no
  warning. Settings stay project-only.
- **Remote sense** stays in the project file: when the fields replace a project rail, the
  sense pins of its sources are carried over by part, and the assumptions say so.
- **Tolerances and accuracy.** `PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL` (a load's) and `PI_ACC` (a
  source's) are part data like `PI_KIND`: one value for every rail where the part is a load (a
  source), or one per rail. Without `PI_RAIL` they reach the part's terminal on a project-file
  rail by part (a source's on the part whose fields describe it: an external-inductor buck's
  IC); several values without `PI_RAIL` are refused. A field wins over the project's value of
  the same family for that part (`tol_pct` or `tol_mv`, ...), with a warning when they differ
  ("KiCad field PI_TOL "5%" on U1 gives tol_pct ±5 % for U1 on VCC; the project file's tol_pct
  ±4 % is not used ..."), unless the rail sets `override_board_fields`. What the fields do not
  give (`ripple_mv`, `transient_mv`, or a family without its field) is carried over by part
  ("Rail VCC: ripple_mv of U1 from the project file (the part's KiCad fields do not give
  them)."). A `PI_TOL` on a part that is a load nowhere, or a `PI_ACC` on one that is a source
  nowhere, is refused. No `PI_TOL`: no tolerance, nothing to judge; no `PI_ACC`: the accuracy
  part is missing, not judged.
- **Capacitor models.** A part named `C<number>` with `PI_ESR`, `PI_ESL`, `PI_DIELECTRIC` and a
  capacitance (`PI_C`, else its footprint Value with its unit) and, for a class II dielectric,
  `PI_VRATED`, has a capacitor model in the merged project (`capacitors`, named "C21 (KiCad)"),
  used by `solve_ac`, `solve_noise` and the tolerance stack-up's AC and noise solves; one
  assumption line lists each model's values with their fields. **Never a default:** with any of
  them missing the part has no model. The AC impedance then leaves it out and does not judge
  the probes on its rail, the noise counts it as a stand-in and does not judge the victims, and
  each reason names what it lacks ("1 capacitor(s) between +3V3 and GND have no model, so the
  resonances may move: C45 (their KiCad fields give no model: C45: PI_ESR missing)"; "C12: X7R
  without PI_VRATED: capacitance not derated for DC bias"). A value that cannot be read is an
  `invalid_board_field` error; fields on a part not named `C<number>` are a warning and not
  used. A project `capacitors:` model naming a part with a complete fields model loses that
  part, with a warning listing the differences, unless the model sets
  `override_board_fields: true`; with partial fields the project's model stays in use, with a
  warning. A `TANT`, `POLY` or `ALU` part is never derated for DC bias (KiPI derates every rated
  model but `C0G`, so its rating is not passed on). The DC-bias derating of a class II part
  takes KiPI's typical 40 % kept at the rating: no field gives `kept_at_rated_pct`; keep the
  model in the project file, with `override_board_fields`, to set it.
- `board_fields` lists, with any, `capacitors` (each: `ref`, `fields`, `complete`, `model` with
  its `capacitance_f`, `capacitance_from`, `esr_ohm`, `esl_h`, `rated_v`, `dielectric`,
  `derated_for_dc_bias`, or `missing`, what it lacks) and `part_tolerances` (parts whose
  tolerance fields have no `PI_RAIL`, each key parsed: `tol_pct: [-3, 3]`). A load's or source's
  tolerances on a fields rail are in its entry, with the field in `fields`.
- **Links:** a `PI_LINK` part replaces a project link on the same part (a warning when they
  differ); there is no override for links yet.
- **Where each number came from.** Every rail summary has `from_board_fields`,
  `overrides_board_fields` and `board_fields_warnings` (the warnings about that rail, also in
  `warnings`); `solve_project` adds a top-level `board_fields_warnings` with all of them. A rail
  from the fields has one assumption line giving every value with its field: `load U1 2 A
  (from KiCad field PI_LOAD "2A" on U1)`. Say so when you report these numbers, and if you copy
  values from `board_fields` into `solve_rail`.
- `solve_project`, `solve_tree`, `coppernicus solve` (with or without `--tree`), the GitHub
  Action and the desktop app read the fields, through the same merge
  (`coppernicus.board_fields.merge(project, fields, pad_nets, ties)`, returning the merged
  project, each net's origin and the warnings). In the app a rail from the fields is listed
  `[KiCad]` and shown read-only; **Override in project** keeps a project rail instead, **Use
  KiCad fields** goes back, **Project → Use KiCad footprint fields** turns the fields off and
  **File → Reload board** reads them again after a change in KiCad.

### Proposing fields

`propose_fields(project_path)` (or a `result_id`) is the inverse of the merge: the field text
that describes the project's rails and links, part by part, for the user to paste into the
symbols in KiCad. Coppernicus never writes KiCad files. Each part has:
- `library_symbol`: `PI_KIND`, `PI_EFF`, `PI_IQ`, `PI_IMAX`, `PI_FSW`, a load's `PI_TOL`,
  `PI_DC_TOL`, `PI_AC_TOL`, a capacitor model's `PI_C`, `PI_ESR`, `PI_ESL`, `PI_VRATED`,
  `PI_DIELECTRIC`, what the part is;
- `schematic_instance`: `PI_RAIL`, `PI_SOURCE`, `PI_SOURCE_REF`, `PI_LOAD`, `PI_PINS`,
  `PI_LIMIT_DROP`, `PI_INPUT`, `PI_INPUT_PINS`, `PI_GND`, `PI_GND_PINS`, `PI_GND_REF`,
  `PI_LINK`, `PI_LINK_PINS`, and a source's `PI_ACC` beside its `PI_SOURCE` (move both, as
  `PI_VOUT` and `PI_ACC`, to a fixed-output part's library symbol: a project file does not
  say whether an output is fixed), where it sits.

Each part of a `capacitors:` model gets its fields, `PI_C` always (the footprint Value is not
known here). A model the fields would leave without one (an ESR, ESL or capacitance not
entered, no dielectric or an unknown one, a class II one without `rated_v`, a part not named
`C<number>`) is not proposed; `not_expressed` says why, and lists a model's
`kept_at_rated_pct`, a non-ceramic model's `rated_v` (kept, never derated as fields) and a
load's `ripple_mv` / `transient_mv`.

A part on several rails gets one value per rail, by position (`PI_RAIL=+3V3;+1V8`,
`PI_LOAD=1.2A;400mA`). Values are written with their unit and read back exactly. A rail's
`max_drop_pct` becomes `PI_LIMIT_DROP` on its first source; if it is a chip's own supply
tolerance, it belongs in that chip's library symbol instead. The proposal is checked by
parsing it back, which gives the project's rails. `not_expressed` lists what fields cannot
carry: remote sense, terminal names, the other limits, `follow_net_ties: false`. A source held
at another part's pads, with its input and ground on one part, is proposed on that part with
`PI_SOURCE_REF`; a link on a part with more nets, or using only some of its pads, gets
`PI_LINK_PINS` (when the board's pads are known). A rail whose input and ground pins are on
two other parts, or a link whose pins are not its part's pads on its two nets, is not proposed
at all, since a wrong field would fail every run. The proposal gives the source's voltage as
`PI_SOURCE`; move it to `PI_VOUT` on the library symbol when it is the part's fixed output.
The CLI prints the same: `coppernicus fields --propose PROJECT [--json]`.

### How to annotate a KiCad library

Put what a part **is** on its library symbol, once, and what it **does on this board** on the
placed symbol. KiCad copies a symbol's fields onto its footprint when the PCB is updated from
the schematic (**Tools → Update PCB from Schematic…**), which is where Coppernicus reads them.
Fields added to a library symbol reach symbols already placed through **Tools → Update
Symbols from Library…**. (These KiCad steps follow the KiCad 9 manual; check them once on a
small project with your KiCad version.) Part names below are generic.

**A fixed 3.3 V LDO** (one library symbol, used on many boards):

| Where | Fields |
|---|---|
| Library symbol `LDO_3V3` | `PI_KIND=ldo`, `PI_VOUT=3.3V`, `PI_IQ=50uA`, `PI_IMAX=500mA` |
| Placed U3 | `PI_RAIL=+3V3`, `PI_INPUT=+5V`, `PI_GND=GND` |

U3 holds +3V3 at 3.3 V (from `PI_VOUT`), draws its input from its pads on +5V, and returns
through its pads on GND. Every pad of U3 on +3V3 is its output (add `PI_PINS` to choose).

**An external-inductor buck.** The IC has the input, the ground, the switch node and the
feedback; the rail itself is held at the inductor's output pad. The IC carries the fields;
`PI_SOURCE_REF` points at the inductor:

| Where | Fields |
|---|---|
| Library symbol `BUCK_IC` | `PI_KIND=smps`, `PI_EFF=90%`, `PI_IQ=2mA`, `PI_IMAX=3A`, `PI_FSW=1.2MHz` |
| Placed U1 | `PI_RAIL=+3V3`, `PI_SOURCE=3.3V`, `PI_SOURCE_REF=L1`, `PI_PINS=2`, `PI_INPUT=VIN`, `PI_INPUT_PINS=1`, `PI_GND=GND`, `PI_GND_PINS=3` |
| Placed L1 | nothing |

This makes the source `L1` pin 2 at 3.3 V, named U1, a switcher with η 0.9 drawing from U1
pin 1 on VIN and returning through U1 pin 3 on GND: in a project file, `ref: L1`,
`pins: ['2']`, `name: U1`, `input: {ref: U1, net: VIN, pins: ['1']}`,
`ground: {ref: U1, net: GND, pins: ['3']}`. An adjustable buck gives its voltage on the
instance (`PI_SOURCE`), since it depends on the board's divider; a fixed one can carry
`PI_VOUT` on the library symbol instead. `PI_IMAX` is shown next to I_out and judged in the
power tree (an I_out above it fails); `PI_FSW` is the switching frequency the noise solve uses
when the aggressor gives none.

**A load switch** (input pins 1 and 2 on VIN, enable 3, ground 4, output 5 on VOUT), with
an on-resistance of 20 mΩ: a link between its input and output pads.

| Where | Fields |
|---|---|
| Placed Q1 | `PI_LINK=20mOhm`, `PI_LINK_PINS=1,2;5` |

The link joins VIN (Q1 pins 1, 2) and VOUT (Q1 pin 5). Without `PI_LINK_PINS` the part is
refused, since its pads are on four nets. The loads on VOUT are drawn through it by the rail
that drives VIN; the power tree solves VIN and VOUT as one group. A two-pad ferrite needs only
`PI_LINK`.

**A battery of cell holders** (2S2P from single-contact holders): BT1 and BT5 sit in parallel on
+V_BAT, the negative pole is BT4 on -V_BAT, and no single part has pads on both. One part,
the anchor, carries the fields:

| Where | Fields |
|---|---|
| Placed BT1 | `PI_RAIL=+V_BAT`, `PI_KIND=source`, `PI_SOURCE=6.4V`, `PI_SOURCE_REF=BT1,BT5`, `PI_GND=-V_BAT`, `PI_GND_REF=BT4` |
| Placed BT5, BT4 | nothing |

The source is one terminal, BT1's and BT5's pads on +V_BAT all held at 6.4 V, with its ground
pins on BT4 (the reference of the ground net in the power tree). In a project file it is a
source with `ref: BT1` and `extra_pins: [BT5.*]` ("REF.PAD", the pad may be a glob; every pad
of BT5 on the rail), and `ground: {ref: BT4, net: -V_BAT}`. Rail by rail every pad of the
terminal is held at the voltage; the power tree holds them with KiPI's mesh sense (one point
per part), so it cannot be combined with a remote sense of the source's own. To use only some
pads: `PI_PINS=BT1.1,BT5.1`, or `PI_PINS=1` for pad 1 of every listed part.

When the negative pole is two holders as well (BT4 and BT8, each with its pads on -V_BAT),
BT1 carries `PI_GND_REF=BT4,BT8`: **one ground terminal**, every pad of both holders held at
the ground's 0 V in the power tree, the copper sharing the return between them (with BT4 alone
all of it would go through BT4, overstating the ground rise and the ground copper's loss). In
a project file: `ground: {ref: BT4, net: -V_BAT}` and `extra_ground_pins: [BT8.*]` ("REF.PAD",
as `extra_pins`). `PI_GND_PINS` then takes `REF.PAD` entries (`BT4.1,BT8.1`) or a pad for
every listed part. Only an ideal supply (`PI_KIND=source`) takes a list; the tree's
assumption line names the parts that hold 0 V. The rail-by-rail solve solves no ground, so it
does not use them.

**Loads** carry instance fields: `PI_RAIL=+3V3`, `PI_LOAD=1.2A`, and `PI_GND=GND` for the
power tree. A chip on two rails gives one value per rail: `PI_RAIL=+3V3;+1V2`,
`PI_LOAD=200mA;1.5A`. Its supply tolerance, from its datasheet, goes on its library symbol:
`PI_TOL=5%` for both rails, or `PI_TOL=5%;-3%,+5%` one per rail (`PI_DC_TOL`, `PI_AC_TOL` the
same way). A fixed 3.3 V LDO's accuracy goes beside its `PI_VOUT` on the library symbol
(`PI_ACC=2%`); an adjustable buck's beside its `PI_SOURCE` on the placed symbol (`PI_ACC=1%`,
the reference's accuracy; the divider's resistors widen it).

**A capacitor** carries its datasheet model on its library symbol, so every board inherits it:

| Where | Fields |
|---|---|
| Library symbol `C_10u_0805_X7R_16V` (Value `10u`) | `PI_ESR=5mOhm`, `PI_ESL=0.5nH`, `PI_DIELECTRIC=X7R`, `PI_VRATED=16V` |
| Placed C21, C22 | nothing |

Each placed part has the model 10 µF (from the Value), 5 mΩ, 0.5 nH, X7R, derated for DC bias
from its 16 V rating. A Value without its unit (`10`, `0.1`) gives no capacitance: add
`PI_C=10uF`. A C0G part needs no `PI_VRATED`; a tantalum, polymer or aluminium part (`TANT`,
`POLY`, `ALU`) needs none and is never derated.

Check the result with `coppernicus fields BOARD.kicad_pcb`, or the MCP `board_fields` tool:
both list every part's fields and the rails and links they make, with each value's field.
A rail is listed complete exactly when a solve of the fields alone (no project file) would take it (loads behind a ferrite, a
battery that only feeds links towards a regulator's input), and its note says what completes
it ("Complete without a source of its own: link FB1 joins it to +3V3, driven by U1").

### Estimated draws

A rail solved from a project (`solve_project`, the CLI, the GitHub Action, the app) also
delivers the current of the regulator inputs and links (`links:`) it feeds, estimated at
nominal voltages without copper losses: an LDO's input is its output current plus I_q, a
switcher's is |V_out|·I_out / (η·|V_in|) + I_q, a link carries what its far side draws. Each is
a load named `"U1 (input, estimated)"` or `"FB1 (link, estimated)"` on those pins, listed in
`loads` with `"estimated": true` (and `pins` as pad numbers). Its load problem carries
`"estimated": true` too. One assumption line names them; the power-tree solve computes the
same currents exactly. A rail with no loads now solves when it feeds something, which is how a
regulator's input rail is checked.

The MCP `solve_rail` tool has no project, so it draws only the loads you give it, and every
load it lists has `"estimated": false`. `compare_results` warns when two results' estimated
draws differ (for example a `solve_project` result against a `solve_rail` what-if of the same
rail): the deltas then mix copper and current. For a what-if on a project rail, change the
project's settings and solve the project again.

### Power tree

`solve_tree(project_path)` solves every rail and ground of a project together, as
`coppernicus solve --tree` does. A regulator's input current is computed from what its output
draws, and the ground return is solved on the ground net, so a load sees its supply minus its
ground. It needs the project's v2 fields: each regulator's `kind` and `input` pins and, when a
ground net is solved, each regulator's and load's `ground` pins (see
`docs/engine-mapping.md`). The document has:
- `verdict`, `verdict_note`, `failed` and a top-level `not_judged`: judged per group against
  the project's limits only. `verdict_note` lists the limits per net, then ". Not judged: …".
  A tree's not-judged entry is `{"item", "net", "limit", "reason"}`: the item is a load,
  `"U2 (input)"`, a return pin, or the net itself for a net-level entry (a limit of the wrong
  kind, a net the tree does not solve, a group that did not converge, a `max_drop_pct` with
  nothing to judge), or a rated regulator (`"limit": "current_limit_a"`, see below);
- `vrms`: each source and regulator, with `v_set_v`, `v_out_v`, `i_out_a`, `v_in_v`, `i_in_a`,
  `p_loss_w` and `regulation` (why it cannot hold its voltage, if so). A regulator's input, as
  a load of the rail that feeds it: `input_net`, `input_drop_mv`, `input_drop_pct`,
  `input_exceeds_drop_limit`, `input_status`. Its rating: `i_max_a` (the rated output current
  judged, `null` where none is given; the key is there only when some VRM has a rating),
  `exceeds_current_limit` (`true`/`false`; `null` without a rating or when not judged) and
  `current_limit_from` (where the rating was written; `null` without one);
- `loads`: each load's `v_supply_v`, `v_ground_v`, `v_across_v`, `drop_mv` and `drop_pct`
  (from the nominal voltage, ground rise included), `status` and `exceeds_drop_limit`, `net`
  (the net its supply pins are on) and `rail` (the rail it is listed in; another net past a
  net tie);
- `links` and `groups`. A rail group has `max_drop_mv` and `max_drop_pct`. A ground group has
  `max_rise_mv`, **signed**: negative when the ground sinks below 0 V (under a negative rail),
  judged by its magnitude; give it with its sign. Its `max_drop_pct` is `null`. Each group has
  `limits` (the strictest of each kind over its nets), `limits_by_net` (the limits each net
  sets), `verdict`, `not_judged` and `problems`: `load` on a rail, the regulator inputs it
  feeds among them as `"U1 (input)"` (`current_a` is the input current; `drop_mv` can be
  `null`), and `regulator` for a rated regulator driving it (below); `rise` and `return` on a
  ground; `hotspot` and `via` on both. Each problem has
  `severity`, `kind`, `title`, `detail` (the line that says, among others, which limit
  judged it: "limit 2 % (from +5V)"), and its place as in a rail summary: `layer`, `x_mm`,
  `y_mm` (`null` when it has none). A `load` (a regulator input included) also has
  `limit_pct` (the `max_drop_pct` that judged it) and `limit_from` (the net that limit came
  from: its own, the rail it is listed in, or the group's driving net); a `return` has
  `limit_mv` (`max_ground_rise_mv`) and `limit_from`; both `null` when no limit is set;
- `assumptions` and `warnings`.

#### Regulator ratings in the power tree

A source's rated output current (KiCad field `PI_IMAX`, or `current_limit_a` in the project
file) is a limit the user wrote: the regulator **fails** when |I_out| is above it (|I_out|
within 1e-9, relative, of the rating counts as at it: the solve's round-off). It belongs to
the group the regulator drives:
- a `regulator` problem there, `"U1: 2.502 A out, over its rated 2 A (from KiCad field
  PI_IMAX on U1)"` (or `"(from the project file)"`; `"within its rated"` when it passes, an
  info row), with `name`, `i_out_a`, `current_limit_a`, `current_limit_from` and
  `rating_used_pct`; a fail is also named in `verdict_note` ("Over a rated output current: …");
- not judged, listed as `{"item": "U1", "net": "+3V3", "limit": "current_limit_a", "reason":
  …}`, while I_out is not settled ("its iteration did not converge", "+1V8, which it feeds,
  did not converge", "the power-tree iteration did not converge"), and for a rated source on a
  rail the tree does not solve as a rail, excluded or used as a ground (`"reason": "not solved
  in the power tree"`; the item is the source);
- a regulator out of regulation is still judged on its current, as KiPI does;
- a source without a rating is not judged and not listed.

The rating stays out of a group's `limits`; a tree whose only limit is a rating passes or fails
by it. An assumption line states the rule whenever a rating exists.

Verdicts in the tree follow the rule above: a copper limit (via, current density, rise) with
nothing to judge passes; a `max_drop_pct` with no load to judge is not judged; a limit set on
a net the tree does not solve is a not-judged item ("NET: not solved in the power tree").

#### Probing, mapping and cutting tree groups

The session keeps the tree as one result. `solve_tree` returns `tree_id` (`"t1"`) and each
group's `result_id` (`"t1:GND"`, `"t1:+3V3"`: the tree id, a colon, the group's key; split on
the first colon, so a net name may hold one). A net of a group names that group too
(`"t1:+3V3_F"` reads group `+3V3` when FB1 joins them; the answer gives the group's id).
- `probe`, `render_map` and `cut_current` take a group's `result_id`. `report` takes the tree's
  id alone (`report(result_ids=["t1"])`) and writes the tree's page, as
  `coppernicus solve PROJECT --tree --html` does: every group's maps and problems, the tree's
  verdict and its VRM, load and link tables.
- A probe on a group answers with `group`, `kind` (`rail` or `ground`), and `net`, the net at
  the point, only where it is known exactly (a group of one net). Its `drop_mv` is worded as
  the app's readout words it: `drop_word` is `drop`, `rise` (a ground above 0 V) or `shift` (a
  ground under a negative rail), and `drop_unit` says what the number measures (`"mV above
  0 V (ground rise)"`; on a shift, `"mV below 0 V (ground shift)"`, the depth below 0 V). On
  the power-chain example, `probe(result_id="t1:GND", layer="B.Cu", x_mm=97.5, y_mm=1.0)`
  reads U3's ground pad: a 38.470 mV rise.
- `relative_to` on `probe` gives `delta_v`, this point's voltage minus another point's:
  `{layer, x_mm, y_mm}` (`x`/`y` are accepted too) or `{pad: "U3.2"}` (a pad as REF.PAD, read at
  its centre, or at the nearest meshed copper of the pad when its centre is a drilled hole),
  with `result_id` for another group of the **same** tree (with a pad, the tree's id finds the
  pad's group from its net). The answer has `relative_to` with that point's own reading. It
  works across groups: U3's supply pad against `{pad: "U3.2"}` is the voltage across U3 (its
  `v_across_v` in the loads table). Both readings come from one solve, in its own frame
  (0 V at the source's ground pins); nothing is shifted or solved again. Points of separate
  solves share no 0 V and are refused, as is a reference off copper (the hint gives the nearest
  copper) or on copper not connected to a source.
- The tools for one rail result refuse a tree's or a group's id with `invalid_input` and a hint:
  `get_problems` (a group's problems are in `groups[].problems`), `copper_step` (one step of
  `copper_thickness_mm` in the project file, then `solve_tree` again), `compare_results` and
  `compare_revisions` (compare two tree documents), `propose_fields` (give the `project_path`).
- The tree counts as one result in the session's memory bounds (every group's arrays, and the
  locators built by later probes) and is dropped whole: then every group answers
  `result_evicted`; call `solve_tree` again.

On the command line, `coppernicus solve PROJECT --tree --probe LAYER@X,Y` (repeatable) prints
one readout line per point after the tables: the group with copper there, its voltage, its drop
(or rise) and |J|. With `--json` the readouts are the document's `probes` list (each with
`group`, `kind`, `layer`, `x_mm`, `y_mm`, `voltage_v`, `drop_mv`, `drop_word`, `drop_unit`,
`j_a_mm2`; off every group's copper, `on_copper: false` and `groups_with_copper_here`).

### Board temperature

The power tree can also solve the board's temperature (electro-thermal: the copper is heated
by its own losses, then solved again at that temperature, KiPI's co-simulation). It is a
power-tree solve only: `solve_tree` with `thermal: true`, or the project's `thermal:` block with
`enabled: true` (`coppernicus solve PROJECT --thermal` on the command line).

- `thermal`: true solves it, false not, omitted follows the project's `thermal.enabled`.
- `thermal_options` imply `thermal: true` when `thermal` is omitted (with `thermal: false` they
  are refused, `invalid_input`). For this solve only (the file is not changed), any of the project's
  `thermal:` keys: `ambient_c` (default 25), `iterations` (3), `airflow_m_s` (0), `grid_mm`
  (0 = automatic, about 250 cells across), `load_heat`, `regulator_heat` (`output_pads` or
  `part_pads`), and the limits `max_temp_c` and `max_rise_c`. Unknown keys are refused.
- **Cost first.** Before the solve, `thermal_estimate` gives KiPI's grid (`grid_nx`,
  `grid_ny`, `cell_mm`, `cells`, `layers`, `unknowns`), the memory of its thermal model
  (`memory_bytes`) against this machine's (`machine_memory_bytes`, `exceeds_memory`) and a
  `text`, from the same estimate the app shows. When it exceeds this machine's memory, the
  call is refused with `solve_too_large`, the estimate in the message: ask the user, then
  call again with `confirm_large: true`, or with a coarser `thermal_options.grid_mm`. The CLI
  prints the same line before it solves ("Board temperature: …").
- The document then has `thermal`: its verdict (only against `max_temp_c` / `max_rise_c`),
  the hottest solved copper per layer with its nearest pad, the grid peak (shown, never
  judged), the history of the iterations and whether it settled, the heat by kind, and each
  load's drop at the ambient and electro-thermal. Its keys are listed in
  [engine-mapping.md](engine-mapping.md#summary-tree_summarytreethermal-present-only-when-the-temperature-was-solved).
  The board temperature counts as one more group in the overall verdict and the exit code.
- Rail by rail (`solve_project`, `coppernicus solve`), the temperature is not solved: its
  limits are listed as not judged (exit 3), never passed.
- `coppernicus solve PROJECT --thermal --html` adds a "Board temperature" section to the tree
  report: the verdict, the grid peak, the per-layer rows, the load drops at the ambient and
  electro-thermal, the assumptions, and each copper layer's temperature map inside the page.

### AC impedance

`solve_ac(project_path)` computes the open-loop PDN impedance |Z|(f) seen at each probe of the
project (`probes:`, with the capacitor models of `capacitors:` and the sweep of `ac:`), rail by
rail, as `coppernicus ac PROJECT --json` does. Its project keys and the physics are in
[engine-mapping.md](engine-mapping.md#task-9-ac-pdn-impedance-zf--kipiac-kipiacsolversweeppy-kipiacmodelpy).

- The document: `kind: "ac_impedance"`, `verdict` and `verdict_note` (only against each
  probe's own `target_ohm`, or the target KiPI computes from the probe's step and tolerance;
  below the source's control-loop bandwidth nothing is judged), `failed`, `not_judged`,
  `warnings`, `summary_file`, and `rails`: each with its probes (`worst_z_ohm`, `worst_hz`,
  `target_ohm`, `passed`…), `bandwidth_hz`, `bandwidth_assumed` and `assumed_bandwidth_hz`
  (50 kHz when no bandwidth is given), `csv_file` and `png_file` of |Z|(f), `assumptions`,
  `warnings`, and `stats` with `memory_peak_mb` and `memory_peak_raised`. The files go into
  the server's output folder.
- **Cost first.** It is heavy: minutes and gigabytes on a large board. Each rail is first
  meshed and sized, in `estimate`: `triangles`, `ports`, `field_frequencies`, `unknowns`,
  `memory_bytes` of one factorization against `machine_memory_bytes`, `exceeds_memory`, and a
  `text` (the app's figures). When a rail would exceed this machine's memory, nothing is
  solved and the call is refused with `solve_too_large`: tell the user the estimate, and call
  again with `confirm_large: true` only if they agree (or after a coarser `ac: max_edge_mm`).
- `rails` picks the rails (every rail a probe is on by default). Exit codes as the others.
- On the command line, `coppernicus ac PROJECT` prints "Impedance of <rail>: <estimate>"
  before each rail, and `--html [PATH]` writes one offline page with each rail's |Z|(f) and
  its probes. In CI, the Action's `ac: true` input runs it (see [CI](ci.md)).

### Ripple and coupled noise

`solve_noise(project_path)` computes, for each switching regulator the project marks as an
aggressor (`aggressors:`), its hot loops, its waveforms, the ripple at its pins and at the
probes it names, the near field and a radiated estimate, and the noise it couples into each
victim net (`victims:`), as `coppernicus noise PROJECT --json` does. What it computes and what
the user enters: [Ripple and coupled noise](noise.md); every key:
[engine-mapping.md](engine-mapping.md#task-10-dc-dc-ripple-and-coupled-noise--kipinoise-kipinoisesolverpy-kipinoisemodelpy).

- The document: `kind: "noise"`, `verdict` and `verdict_note`, `failed` and `not_judged`
  (each entry `{item, net, limit, reason, aggressor}`), `warnings`, `summary_file`, and
  `aggressors`: each with its operating point (`vin_v`, `vout_v`, `iout_a`, `duty`, `fsw_hz`,
  edges), `hot_loops`, `ripple` (`pp_20mhz_mv`, `below_20mhz_mv`, `above_20mhz_mv`, `pp_mv`,
  `rms_mv`…), `victims` (`peak_mv`, `peak_by_mv` per mechanism, `dominant`, `margin_mv`,
  `margin_from`, `ratio`, `passed`, `not_judged`, `stand_ins`…), `emi` (hotspots and the
  radiated estimate), `stand_ins`, `assumptions`, `warnings`, `csv_file`, `png_files` (the
  waveforms, then the H and E field maps), `stats` with `memory_peak_mb`, and `estimate`. The
  files go into the server's output folder.
- **The verdict comes only from the noise margins the user set**: a victim's
  `noise_margin_v`, else its preset's own (lvds, analog), else the JESD8 levels of an entered
  `logic_v`. A victim without one is ranked (`passed: null`, no entry). A KiPI default
  standing in for a value the user did not give (Coss, Qrr, a capacitor without a model,
  guessed ends, a coupling left out: listed in `stand_ins`) makes the victim **not judged,
  never a pass**, whatever its peak; so does a `logic_v` off KiPI's table. Report each
  `not_judged` reason and what the user can enter to remove it. **The ripple and the radiated
  estimate are never judged**: give their numbers, never a pass or fail. Never invent a margin.
- **Cost first.** It is heavy: minutes and gigabytes on a large board. Each aggressor is
  first checked and sized, in `estimate`: `harmonics`, `f_max_hz`, `board_model` (with `note`
  when the board model is off or left out), `triangles`, `ports`, `field_solves`, `unknowns`,
  `memory_bytes` (an upper bound of the whole solve) against `machine_memory_bytes`,
  `exceeds_memory`, and a `text`. When an aggressor would exceed this machine's memory,
  nothing is solved and the call is refused with `solve_too_large`: tell the user the
  estimate, and call again with `confirm_large: true` only if they agree (or after a coarser
  `ac: max_edge_mm` or fewer `noise: field_per_decade`).
- `aggressors` picks the aggressors by name (every one by default). Exit codes as the others:
  1 a margin exceeded, 3 a margin that could not judge.
- On the command line, `coppernicus noise PROJECT` prints "Noise of <aggressor>: <estimate>"
  before each aggressor (on stderr with `--json`), and `--html [PATH]` writes one offline page
  with each aggressor's waveforms, field maps, ripple and victims. In CI, the Action's
  `noise: true` input runs it (see [CI](ci.md)).

Limits, all optional:
- `max_drop_pct`: the largest allowed load drop, in % of the source voltage;
- `max_current_density_a_mm2`;
- `max_via_current_a`;
- `max_ground_rise_mv`: for a net at 0 V (a ground solved on its own, with a 0 V source at
  the reference point and its return pins as loads, or a ground group of the power tree), the
  largest allowed rise of its copper above 0 V, in mV.

Each limit judges its own kind of net: `max_ground_rise_mv` nets at 0 V only, `max_drop_pct`
nets with a nonzero nominal voltage. A limit of the wrong kind is not judged: it is listed in
`not_judged` (`"+3V3 (max_ground_rise_mv): applies to nets at 0 V (grounds) only"`), never
passed or failed. **Behaviour change:** a rise limit on a supply rail used to fail; it is now
`not_judged`.

Problem kinds, in `problems` of every summary and of `get_problems`:
- `load`: a load's drop at its worst pin (`drop_mv`, `drop_pct`);
- `hotspot`: a current-crowding spot (`mean_j_a_mm2`, `peak_j_a_mm2`, `area_mm2`, and
  `elements`, its mesh element count); one above the current-density default has
  `severity: "attention"`, `default_j_a_mm2` and `thickness_mm` (see Conventions);
- `via`: a via or plated hole by barrel current (`current_a`);
- `rise`: only with `max_ground_rise_mv` set on a net at 0 V, the copper furthest from 0 V
  (`rise_mv`, signed: negative below 0 V; judged by its magnitude), with its layer and x/y;
- `return`: in a power-tree ground group, one ground return pin and its rise above 0 V
  (`rise_mv`, `current_a`), judged against `max_ground_rise_mv`.

### Tolerance stack-up

`solve_tolerance(project_path)` adds up, per load and per side (below and above nominal), the
DC drop on its worst pins (the power tree) and its source's DC accuracy against the DC
tolerance, the ripple at its probe and its transient against the AC tolerance, and all four
against the tolerance, as `coppernicus tolerance PROJECT --json` does, through the same code.
What it adds up and what the user enters: [Tolerance stack-up](tolerance.md); every key:
[engine-mapping.md](engine-mapping.md#task-11-tolerance-stack-up--kipitolerance-kipitoleranceevaluatepy-kipitolerancegatherpy).

- The document: `kind: "tolerance"`, `verdict` and `verdict_note`, `failed` and `not_judged`
  (each entry `{item, net, limit, reason}`; some have no load row: a misspelt key, a tolerance
  lost to a KiCad-fields rail), `options`, `warnings`, `assumptions`, `estimates` (each solve's
  cost, `solve` "board temperature", "AC <rail>" or "noise <aggressor>"), `stats`, `csv_file`,
  `summary_file`, and `loads`: each with `nominal_v`, `source`, `probe`, `verdict`, `parts`
  (`dc`, `accuracy`, `ripple`, `transient`: `{below_mv, above_mv, source, detail}`, or `null`
  when missing), `hints` (what to enter for a missing part), `floors`, `transient_estimate`,
  `checks` (`dc`, `ac`, `total`: `limit`, `below_mv`, `above_mv`, `allowed_below_mv`,
  `allowed_above_mv`, `margin_below_mv`, `margin_above_mv`, `complete`, `passed`, `missing`,
  `reason`) and `stand_ins`. The files go into the server's output folder.
- **The verdict comes only from the tolerances the user set** (`tol_*`, `dc_tol_*`,
  `ac_tol_*`), and **a check passes only when every part is known**. A part not known is
  missing, never 0: an accuracy not entered, a ripple no aggressor gives (and no `ripple_mv`),
  an aggressor not solved or resting on KiPI stand-ins, a `transient_mv` not entered. A check
  can fail on its known parts alone (`failed` says so), but is otherwise **not judged, never a
  pass**; report each `not_judged` reason and the hint of what to enter. **KiPI's transient
  estimate is shown, never judged**: give its numbers, never use them as the transient. Never
  invent a tolerance, an accuracy or a ripple.
- The KiCad fields are merged as in the other solves; a rail from the fields carries the
  project's tolerances by part reference, and a tolerance on a part the fields' rail lacks is
  not judged (with a warning). A key that looks like a misspelt tolerance (`tol_pc`) is a
  warning and a `not_judged` entry.
- **Cost first.** The power tree is cheap; an AC solve (KiPI's estimate, only when a probe has
  a step and its source a bandwidth) and a noise solve (the ripple) run only where a load with
  a tolerance needs them. Each is sized before anything is solved, in `estimates` (with the
  board temperature's when the project enables it). When one would exceed this machine's
  memory, nothing is solved and the call is refused with `solve_too_large`: tell the user the
  estimate, and call again with `confirm_large: true` only if they agree, or with `skip_ac` /
  `skip_noise` (those parts are then left out: missing, so not judged, for the ripple).
- Exit codes as the others: 1 a check exceeded, 3 a tolerance that could not judge.
- On the command line, `coppernicus tolerance PROJECT` prints each solve's estimate first (on
  stderr with `--json`), and `--html [PATH]` writes one offline page with each load's parts and
  checks. In CI, the Action's `tolerance: true` input runs it (see [CI](ci.md)).

## Conventions

- **Units are in every key name:** `_v`, `_mv`, `_a`, `_a_mm2`, `_mm`, `_mm2`, `_w`, `_pct`,
  `_um`, `_c`. Inputs follow the same rule (`voltage_v`, `current_a`).
- **Ids.** Boards are `b1`, `b2`, … and results are `r1`, `r2`, …. Ids are valid for the life
  of one server process and restart with every run.
- **Coordinates** are mm in KiCad's frame: x to the right, **y down**. Layer names are as in
  KiCad (`F.Cu`, `In1.Cu`, `B.Cu`).
- **Errors** are tool errors whose text is `{"error": {"code", "message", "hint"}}`. The hint
  says what to do. For example, `part_not_on_net` lists the parts on the net and
  `pin_not_on_net` lists the pins of the part. Codes: `file_not_found`, `board_unreadable`,
  `board_not_open`, `invalid_project`, `invalid_input`, `net_not_found`, `part_not_on_net`,
  `pin_not_on_net`, `missing_source`, `missing_load`, `layer_not_found`, `result_not_found`,
  `result_evicted`, `cut_misses_copper`, `invalid_board_field`, `solve_failed`,
  `solve_too_large` (a solve whose estimate exceeds this machine's memory, refused until
  confirmed), `cancelled`, `internal_error`. An
  argument of the wrong type is rejected by the tool's input schema before it runs.
- **Verdicts.** A verdict exists only against limits the user gives:
  - `fail`: an item exceeds a limit you set;
  - `not_judged`: no limits are set, so problems are ranked and nothing is judged; or a limit
    you set could not judge something. `not_judged` then lists each item as `{item, limit,
    reason}`: a load of a 0 V net (a % has no meaning there), a load with no voltage at its
    pins, no load at all, a limit of the wrong kind for the net (above), or a rail that was not
    solved (skipped: no source or loads, or nothing to draw), each of whose limits is listed.
    A failure elsewhere still makes the verdict `fail`;
  - `pass`: limits are set and none is exceeded. A copper limit (`max_via_current_a`,
    `max_current_density_a_mm2`, `max_ground_rise_mv`) with nothing to judge, such as a via
    limit on a rail with no vias, passes, as in the power tree.

  `solve_project` adds a top-level `not_judged` with the rail of each item (skipped rails
  included, so a limit never drops out of the verdict), and its verdict is
  `not_judged` when any rail has such items (`pass_partially_judged` stays for "the rails with
  limits pass, others have none"). The CLI text and the CI job summary print each item as
  "NOT JUDGED …". `coppernicus solve` (and `--tree`) exits 3 then, so CI fails on it (the
  Action's `fail-on-not-judged`); with no limit at all it exits 0.

  Verdicts only against the user's limits: Coppernicus invents no thresholds for them, and
  an agent should not either. Coppernicus's documented current-density default raises
  attention items, never a verdict (next item).
- **Attention: the current-density default.** Without a `max_current_density_a_mm2`, every
  current-crowding spot whose mean |J| is above 60 A/mm² at 1 oz, scaled ×√(35 µm / t) with
  its layer's copper thickness as solved (85 at 0.5 oz, 42.4 at 2 oz, 34.6 at 3 oz), is a
  `hotspot` problem with `severity: "attention"` (between `"fail"` and `"info"`). It has
  `default_j_a_mm2` (the default used) and `thickness_mm` besides `mean_j_a_mm2`,
  `peak_j_a_mm2` and `area_mm2`, and is measured as a limit is, on the spot's mean |J|. Every
  one is listed, on top of the ranked spots, except a spot of fewer than 3 mesh elements
  (`elements`): a numerical-resolution rule of the default, not a physical limit. Refine the
  mesh near holes (`min_edge_mm`, `grading`) to see such a spot; a `max_current_density_a_mm2`
  the user sets judges every spot. A spot is listed once: a ranked `info` spot that shares
  copper (one mesh triangle) with an attention or `fail` spot on its layer is not listed
  again, and the `top` ranked spots are counted after that. It changes no verdict, no
  `not_judged` list and no exit code; a rail summary, each
  tree group and the tree and project documents count them in `attention`. A
  `max_current_density_a_mm2` the user sets replaces the default on that rail or group (a
  `fail` when exceeded, no attention items). Every rail and power-tree result has an
  assumption line with each layer's value. Why it is attention only, and the sources:
  [technical.md](technical.md#current-density-default).
- **`assumptions` and `warnings` come with every answer.** Read them. They say:
  - the via plating, ρ(T) and mesh that were used;
  - that loads split their current equally across their pins;
  - that a rail solve does not include the ground return;
  - that point maxima of |J| at sharp corners depend on the mesh;
  - which degenerate mesh triangles (an edge shorter than 1 nm, the resolution of KiPI's
    geometry snap) were left out of every maximum or peak |J|. A rail summary and a tree group
    then also have `degenerate_triangles`, one `{layer, triangles, j_max_a_mm2}` per layer:
    the largest |J| left out, a numerical artefact, never a real peak. The key is absent when
    there are none ([technical.md](technical.md#current-density-default));
  - whether the board file changed on disk since it was opened.
- **Progress.** `solve_rail` and `solve_project` send MCP progress notifications with the
  engine's messages. Cancelling the call stops the solve.

## A typical session, on the demo board

`examples/demo/demo.kipi.json.gz` is a synthetic 60 × 40 mm two-layer board. A B.Cu plane is
fed from J1 on the left, and a slot from the top edge (x 28–31 mm, down to y = 31 mm) squeezes
the plane into a neck. U1 and U2 sit beyond the slot; U3 hangs off a thin F.Cu track. Suppose
the user asks: *"U1 draws 2 A, U2 1.5 A, U3 0.3 A from the 3.3 V supply J1. Does it meet a
0.25 % drop budget?"*

1. `open_board(path="/abs/…/examples/demo/demo.kipi.json.gz")` returns `board_id: "b1"`,
   layers F.Cu and B.Cu (0.035 mm each), and `outline_bbox_mm: [0, 0, 60, 40]`.
2. `list_nets(board_id="b1")` returns `+3V3`, a power net on F.Cu and B.Cu.
3. `net_parts(board_id="b1", net="+3V3")` returns J1 (value `SUPPLY`) and U1, U2, U3 (`LOAD`),
   each with pin `1`.
4. Solve the rail:

   ```
   solve_rail(board_id="b1", net="+3V3",
              sources=[{"ref": "J1", "voltage_v": 3.3}],
              loads=[{"ref": "U1", "current_a": 2.0}, {"ref": "U2", "current_a": 1.5},
                     {"ref": "U3", "current_a": 0.3}],
              limits={"max_drop_pct": 0.25})
   ```

   It returns `result_id: "r1"`, `verdict: "fail"` and `busiest_layer: "B.Cu"`. The loads:

   | Load | Drop | Drop % | Exceeds 0.25 %? |
   |---|---|---|---|
   | U1 | 9.60 mV | 0.291 % | yes |
   | U2 | 8.67 mV | 0.263 % | yes |
   | U3 | 6.88 mV | 0.209 % | no |
5. `get_problems(result_id="r1", top=10)` lists the two failing loads first, then the
   current-crowding spots and the most loaded vias. On B.Cu the broadest spot (about 49 mm²) is
   at x ≈ 29.5 mm, y ≈ 33.6 mm: the neck below the slot.
6. `render_map(result_id="r1", field="drop")` and
   `render_map(result_id="r1", field="current")` show the busiest layer by default.
   `region_mm=[20, 25, 40, 40]` zooms on the neck.
7. Probe a point and cut across the neck:
   - `probe(result_id="r1", layer="B.Cu", x_mm=29.5, y_mm=35)` gives V, drop and |J| in the
     neck.
   - `cut_current(result_id="r1", layer="B.Cu", x1_mm=29.5, y1_mm=0, x2_mm=29.5, y2_mm=40)`
     cuts top to bottom through the neck. It returns `width_mm: 7.0` (y 31–38) and
     `current_a ≈ −3.51`, that is U1 + U2 = 3.5 A to within the mesh error. The sign is
     negative because the current flows toward +x, against this cut's `normal` of [−1, 0].
     The mean |J| is about 14.3 A/mm².
8. What-if: solve again with `settings={"copper_thickness_mm": {"B.Cu": 0.07}}`, which gives
   `r2`. Then `compare_results(result_a="r1", result_b="r2")` shows:
   - max drop 9.60 → 6.30 mV;
   - B.Cu loss halved;
   - `verdict_b: "pass"`.

Conclusion for the user: with 1 oz copper the 0.25 % budget fails at U1 and U2. The neck
carries all 3.5 A of the far loads. 2 oz on B.Cu would meet the budget, or the slot could be
shortened; that second option needs a board edit, which is not modelled here.

`tests/test_mcp.py` runs steps 1 to 7 (all but the probe) as an automated test through the
SDK's own client (`test_acceptance_an_agent_reproduces_the_demo_analysis`). It finds the neck
from the broadest hotspot, as above, and checks the 7 mm width and the 3.5 A current.

To hand the result to the person, call `report()` (or `report(result_ids=["r1", "r2"])` to put
the what-if next to the original) and give them the path.

## HTML report

<p align="center">
  <img src="images/report-demo.png" alt="Coppernicus HTML report of the demo board: verdict against the 0.25 % limit, worst values, the B.Cu drop map and a hover readout" width="820">
</p>

`coppernicus solve PROJECT --html` (or the MCP `report` tool) writes one self-contained HTML
file. It needs no network and no installed software beyond a current browser (tested in
Chromium and Chrome; Safari and Firefox not yet). It holds:

- every rail and every copper layer, as voltage drop, voltage or current density, with zoom
  (wheel), pan (drag), a label beside the pointer that leads with the value of the map shown
  (as in the app) and a **hover readout** of V, drop and |J| under the map;
- toggles for sources, loads and current-crowding spots;
- the verdict, which names the limits you set ("not judged" when there are none), and a
  worst-values summary per rail: worst load, largest drop, copper loss, strongest current
  crowding, most loaded via;
- the problems table (click a row to zoom to it), the loads and sources, the loss per layer,
  the settings, the assumptions and the warnings.

**How the maps are stored.** A large board's mesh is too big to embed, so each layer is sampled
on square cells:
- the voltage at each cell centre is interpolated in the full-resolution triangle that holds
  it, and |J| is that triangle's value;
- both are kept to 16 bits and compressed.

The readout gives the values **at the centre of the cell under the pointer**, says so, and
states the cell size and the storage step. It never interpolates between cells. Summary
numbers and problems come from the full mesh. A browser check on the demo matched the Python
probe at three points, to 1 µV and to the |J| step.

The cell size is chosen per report to stay under a file-size budget (about 27 MB, so at most
about 30 MB), and never finer than a sixth of the median mesh edge. Measured on a synthetic
4-layer board with 3.07 M triangles (80 × 60 mm planes):
- cells of 0.032 mm, 2498 × 1874 per layer;
- a 19.7 MB file, written in 5–7 s;
- in the Chromium that Playwright drives on an Apple-silicon Mac, with the page served from
  localhost:
  - the first map was up at most 0.16 s after navigation started (an upper bound);
  - each further layer decodes in about 45 ms;
  - the page holds about 190 MB of JavaScript memory after every layer has been shown.

These numbers come from smooth synthetic planes. Real boards, with more edges and holes,
compress less well, and their sizes have not been measured yet. If a page would pass 30 MB,
`write_report` and the `report` tool coarsen the cells and rebuild it. The rail-by-rail
`solve --html` path warns instead.

The demo board's report is 2.2 MB, with 0.027 mm cells.

Paths: `--html` alone writes `<out>/<project>_report.html`, where `<out>` defaults to
`results/` next to the project file. `--html PATH` writes it elsewhere, and `--open` also opens
it in the default browser. A link ending in `#group=NET` (for example `report.html#group=+3V3`)
opens that rail, or that group of a power-tree report, instead of the first.

In the power-tree report (`solve --tree --html`), each group's loads table and worst-load card
show what the tree judged: the voltage across each load's supply (or a regulator's input) and
ground pins, its drop from its nominal voltage and the tree's verdict, each by its own net's
limit. The map is still the copper drop below the group's source output, as its scale says.

## Without MCP: the CLI with `--json`

`nets`, `parts`, `fields`, `solve`, `ac`, `noise` and `tolerance` take `--json`. Each prints one JSON document on
stdout (the same data as the tools) and nothing else; progress from `-v` goes to stderr.

```sh
uv run coppernicus nets board.kicad_pcb --json [--all]
uv run coppernicus parts board.kicad_pcb +3V3 --json
uv run coppernicus solve project.coppernicus.yml --out results --json [--rail NET] [--no-maps] \
  [--top N] [--html [PATH]] [--open] [--no-board-fields] [--tree]
# --tree --html writes <project>_tree_report.html: every group, the VRM, load and link tables
uv run coppernicus fields board.kicad_pcb --json
uv run coppernicus ac project.coppernicus.yml --out results --json [--rail NET] [--html [PATH]] \
  [--no-board-fields]
uv run coppernicus noise project.coppernicus.yml --out results --json [--aggressor NAME] [--html [PATH]] \
  [--no-board-fields]
uv run coppernicus tolerance project.coppernicus.yml --out results --json [--skip-ac] [--skip-noise] \
  [--html [PATH]] [--no-board-fields]
```

`fields` lists a board's KiCad footprint fields and the rails they make, as the `board_fields`
tool does. `solve` reads them by default; `--no-board-fields` ignores them for one run, as
`use_board_fields: false` in the project file does (see
[KiCad footprint fields](#kicad-footprint-fields)).

`--top N` sets how many current-crowding spots and vias are listed per rail (default 5 each,
more when they exceed a limit; every load is always listed). The spots above the
current-density default come on top of the N, and a spot listed as a fail or an attention item
is not ranked again.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done, and no limit you set was exceeded (or none was set). |
| 1 | `solve`, `ac`, `noise`, `tolerance`: a limit you set was exceeded (a drop, a target, a noise margin, a tolerance). |
| 3 | `solve`, `ac`, `noise`, `tolerance`: a limit you set could not judge something (the `not_judged` items). Precedence: 2, 1, 3, 0. |
| 2 | Error, including a crash (a bug: code `internal_error`). stdout holds `{"error": {"code", "message", "hint"}}` with `--json`, and stderr a one-line message. A crash never exits 1. |

`solve` writes the PNG maps and `<project>_summary.json` to `--out`, with or without `--json`.
The summary file **is** the document that `--json` prints, unit-keyed like the MCP tools. Its
path is in the document's `summary_file`, and the HTML report's path, when one is written, is
in `report_file`.

In CI, the GitHub Action (see [docs/ci.md](ci.md)) runs the same `solve --json --html` on every
project file of a KiCad repository. It writes a job summary and fails on an exceeded limit.

### Copper thickness in project files

A project file can hold an optional what-if copper thickness, in mm, per copper layer name:

```yaml
settings:
  max_edge_mm: 0.5
  via_plating_um: null
  copper_temp_c: 20.0
  copper_thickness_mm:
    B.Cu: 0.07
```

Every solve of that project uses it: `coppernicus solve` (with or without `--json` and
`--html`), the MCP `solve_project` tool, the GitHub Action and the desktop app. The board file
is not changed. Real changes of copper weight are made in KiCad. Each rail's assumptions name
the overridden layers, with the board file's value next to each; the HTML report lists the
setting with the others.

- Values are in mm, finite, above 0 and at most 1: 1 oz copper is 0.035, not 35. A value such as
  35 makes the project file invalid.
- A layer name the board does not have fails the whole project, with one `layer_not_found`
  error listing the board's copper layers.
- Without the key, the file is read and saved exactly as before.
- The desktop app edits it under **Copper thickness → Edit…** in the solve settings, and keeps
  the key when it saves the project.

## Limits of this version

- **Memory.** A large rail's result can take about 1 GB. Only the `--max-results` most recently
  used results, within `--max-result-mb`, keep their fields. An older result keeps its summary,
  so `compare_results` still works on it. Maps, probes and cuts on it return `result_evicted`,
  whose hint says which rail to solve again.
- **Ground return.** A single-rail solve does not include the ground return; solve the ground
  net as its own rail, or the whole project with `solve_tree`. The board temperature
  (`solve_tree` with `thermal`), the AC impedance (`solve_ac`), the DC-DC ripple and coupled
  noise (`solve_noise`) and the tolerance stack-up (`solve_tolerance`) are solved from a
  project file only, with no result ids.
- **No maps of tree groups.** `solve_tree` returns tables and problems with their x/y, not
  result ids.
- **What-ifs are settings only:** copper thickness, via plating and temperature, in `solve_rail`
  or in a project file. Adding vias or editing copper is a later step; real changes are made in
  KiCad.
- **The HTML report's maps are sampled on cells** (see [HTML report](#html-report)). Features
  smaller than a cell, such as a hole or a very thin track, can look blocky or vanish in the
  map, while the numbers, the problems and the probes stay exact.
- **KiCad footprint fields are read from `.kicad_pcb` files only**, not from ODB++, HyperLynx
  or snapshots. Remote sense cannot be a field yet.
- **Tested** on macOS, with synthetic boards and the SDK's own client (in memory and over stdio).
  Registration in Claude Code and Claude Desktop follows their documented commands but has not
  been run in those hosts yet. The tests run in CI on Ubuntu, macOS and Windows, all required.
