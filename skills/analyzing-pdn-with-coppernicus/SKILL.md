---
name: analyzing-pdn-with-coppernicus
description: Use when asked whether a PCB power rail "is OK" or "holds", about IR drop / voltage drop at a chip, current density, current crowding, via current, copper heating, or a what-if on copper thickness, via plating or temperature, for a KiCad (.kicad_pcb), ODB++ or HyperLynx board, and the Coppernicus MCP tools (open_board, solve_rail, render_map, cut_current…) or the `coppernicus` CLI are available.
---

# Analyzing a PDN with Coppernicus

## Overview

Coppernicus solves DC current flow on the real copper (planes, tracks, pads, vias) of one net
and returns drops, current density, via currents and losses. **It gives facts, not verdicts.**
A verdict exists only against limits the user gave. Your job is to measure, explain where and
why, and ask for what is missing.

## Workflow (MCP tools)

1. `open_board(path)`: absolute path. The board file is opened read-only. Never edit it.
2. `list_nets(board_id)`, then `net_parts(board_id, net)`: the parts and pins on the rail.
   Through vias touch every layer, so read `layers_with_copper` (tracks or zones) to see where
   a rail really runs, not `layers`.
3. Sources and loads come from the user.
   - **Source** = where the rail is regulated, at its voltage. For a buck regulator, that is the
     output inductor's output pad or the output-capacitor node, not the SW pin.
   - **Loads** = the parts drawing current, with their worst-case current.
   - If the source is a connector, the drops off the board (cable, contacts, the upstream
     supply) are not included. Say so.
   - If the currents are unknown, ask for them. Meanwhile, measure **transfer resistances**
     instead of guessing currents:
     - run one solve per load, with that load at 1 A and **the other loads kept at 0 A**, so their
       drops are reported too;
     - each solve gives one column of mV per A at every load;
     - at fixed temperature the drops add up: drop_k = Σ_j R_kj · I_j.
4. `solve_rail(board_id, net, sources=[{ref, voltage_v, pins?}], loads=[{ref, current_a, pins?}], limits?)`.
   Pass `limits` only if the user gave them.
5. `get_problems(result_id, limits?)` re-judges without re-solving. Limits:
   `max_drop_pct`, `max_current_density_a_mm2`, `max_via_current_a`, and
   `max_ground_rise_mv` for a ground net solved on its own (0 V source, return pins as loads).
   Problem kinds: `load`, `hotspot`, `via`; `rise` (the copper furthest from the source,
   only with `max_ground_rise_mv`); `return` (a ground return pin in the power tree).
6. Explain with `render_map` (field `drop`, `current` or `voltage`; `region_mm` zooms; look at the
   image), `probe(layer, x_mm, y_mm)` and `cut_current(layer, x1, y1, x2, y2)`. Use the cut to
   **measure** a neck or track width and the current through it. Do not back-compute widths
   from J or read them off a PNG.
7. What-if: re-solve with `settings={"copper_thickness_mm": {"B.Cu": 0.07}}`,
   `via_plating_um` or `copper_temp_c`, then `compare_results(a, b)`.
   - `copper_thickness_mm` is an optional what-if, in mm (1 oz = 0.035, not 35). It never
     changes the board file; real changes of copper weight are made in KiCad.
   - The stack is rebuilt around the new thickness, so via barrels get longer when it thickens.
     A net with no copper on that layer can still move slightly. Do not read that shift as a
     copper effect.
   - A project file can hold the same setting. If a result's assumptions start with
     "What-if copper thickness", say so: the numbers are not the board file's copper.
8. Hand-off: `report(result_ids?)` writes one offline HTML page for the user. It has every
   layer's maps with zoom, pan and a hover readout, plus the problems, worst values and
   assumptions. Give the user its `path`; you cannot read the page yourself.

A summary's `problems` lists every load, then the top `top` current-crowding spots and the top
`top` vias (default 5 each). Pass a larger `top` to see more vias. Vias carrying under a
millionth of the rail current are left out as round-off.

Coordinates are in mm in KiCad's frame, with **y down**. Errors return `{"error": {code, message, hint}}`:
follow the hint. `result_evicted` means you must solve again.

**Without MCP**, use the CLI:
- `uv run --project <Coppernicus repo> coppernicus nets|parts BOARD [NET] --json`;
- `coppernicus solve PROJECT.coppernicus.yml --json --out DIR [--top N] [--html]`. The project
  YAML takes `settings: {max_edge_mm, via_plating_um, copper_temp_c, copper_thickness_mm?}`;
  `copper_thickness_mm: {B.Cu: 0.07}` is the optional what-if of step 7.
- Write the YAML and the outputs outside the board's folder.
- stdout and the file named by `summary_file` hold the same document. Its `problems` lists every
  load plus the top N spots and vias (`--top N`, default 5).
- A project rail also draws the regulator inputs and links it feeds, estimated at nominal
  voltages: loads named "U1 (input, estimated)" / "FB1 (link, estimated)", with
  `"estimated": true`. Say that these currents are estimates, not the user's numbers. A rail
  with no loads that feeds nothing comes back as `skipped`: say it was not solved; do not
  invent a current for it. MCP `solve_rail` has no project and draws only the loads given, so
  for a what-if on a project rail, change the project's settings and solve the project again.
- `--html` also writes an offline report for the user; its path is in `report_file`. It lets the
  user zoom and read values by hovering, but you cannot read it.
- The CLI has no cut, probe, zoomed map or compare for you. Say that widths and point values
  were not measured. Do not estimate them from J or from a PNG, and do not parse the board file
  for geometry. For those, point the user to the MCP tools, to the `--html` report (hover
  readout), or to the app: `coppernicus gui PROJECT --solve NET`, then double-click or
  **Measure ΔV**.

## Limits: never invent them

The tool has no default thresholds, and neither do you. If the user gave no limit:
- solve **without** `limits`, so the verdict is `not_judged`;
- report the numbers, with what each must be compared against (the regulator's tolerance
  budget, the fab's via-current rule, the design's temperature-rise budget);
- ask the user for the limit.

| Rationalization | Reality |
|---|---|
| "The user wants a quick yes/no" | A yes/no built on your own threshold is your opinion dressed as a result. Give the numbers and ask for the limit. |
| "2 % / 3 % / 5 % drop is typical" | The budget depends on the regulator's accuracy and the chip's supply tolerance. Ask. |
| "1 A per 0.3 mm via is a common rule" | It is folklore, not a standard. Report the via current; the limit is the user's or the fab's. |
| "I'll pass my limit to the tool and label it as mine" | It writes FAIL/PASS into the result and its files. Do not pass limits the user did not give. |
| "50 A/mm² is a safe screening value" | No such universal value exists. Heating depends on width, layer, board and airflow. |

**Red flags:** a headline "sì, regge" or "OK" with no user limit; a table column "Soglia
(mia)"; "if you don't give me one, I'll use X %".

**Heating questions:** Coppernicus computes no temperature. If asked, you may give an IPC-2221
estimate, **labelled as an estimate**: I = k·ΔT^0.44·A^0.725, with A in mil² and k = 0.048 for
external or 0.024 for internal layers. It is conservative and superseded by IPC-2152. Apply it
to a measured cross-section (`cut_current`).
- Never apply it to a via barrel as if it were an isolated track.
- For a short neck inside a plane, or for currents far below the IPC charts, give it as an order
  of magnitude only. The plane spreads heat, so the formula overstates the rise.

## Answer recipe

1. **Inputs:** net, sources and loads with their currents, and who gave each number.
2. **Numbers:** the drop per load in mV and %, the copper loss per layer and in the vias, the top
   crowding spots by **mean** |J| with their area, and the top vias by current. On a ground rail
   (`v_ref_v` 0) the % is n/a (`null`): give mV only.
3. **Verdict:** only against user limits. Otherwise write "not judged", followed by what you need.
   A `not_judged` verdict with items listed means a limit was set but could not judge them
   (a % limit on a 0 V net, a load with no voltage, no vias for a via limit): name each item
   and its reason; never report it as a pass.
4. **Where and why:** the layer, the location in mm, and the cause (a neck, a slot, a single via,
   a thin track), with the cut that proves it.
5. **Assumptions** from the `assumptions` field: plating, temperature, mesh, the equal split
   across pins, and **no ground return** (solve GND as its own rail).
6. **The report path**, if you wrote one (`report` tool or `solve --html`), so the user can look
   at the maps themselves.

## Common mistakes

- Quoting the **peak** |J| at a sharp corner or via edge as a fact. It depends on the mesh.
  - Use the spot's mean |J| and its area instead.
  - A tiny spot (a few triangles at a via edge or a corner) that is flagged against a J limit
    may be a mesh effect. Re-solve with a smaller `max_edge_mm` and check whether its mean |J|
    holds before you call it real.
- Calling a "current crowding" spot a defect. The spots are the copper with the highest |J|, the
  top 1 % per layer. A spot can be a whole uniform narrow track, which is a sizing question, not a
  local defect.
- Forgetting that a rail solve excludes the ground return. If the board has no ground net at
  all (`ground_nets: 0`), say that the return path cannot be modelled from this file.
- Taking the voltage from the net name without saying so.
- Writing results next to the board file, or committing board-derived data to a public repo.
