# Engine mapping: Coppernicus project → KiPI

Coppernicus keeps its own engine-independent project model (`src/coppernicus/project.py`).
Only `src/coppernicus/engine/` imports KiPI. This file records, field by field, how the
project maps onto KiPI v1.0.1's setup, so the mapping can be reviewed without reading the
code and redone if the engine changes.

Each Part B task appends its own section. Sources of truth: the KiPI module named in the
section heading, and the Coppernicus code named in it.

---

## Task 6: project model v2 → `PISetup` (`kipi/dc/pi/model.py`)

Code: `coppernicus.engine.pisetup.to_pisetup(board, project) -> PISetup`.

`to_pisetup` only maps the project. Checking the result (an LDO without input pins, a ground
with no 0 V reference, a loop in the power tree…) is KiPI's `validate()`. The power-tree
solve (Task 7) runs it and turns its messages into `EngineError`. Problems that can only be
seen while mapping are raised by `to_pisetup` itself, as an `EngineError` that names the
part:

- a part with no pads on the net;
- a pin pattern that matches nothing;
- a missing link pad;
- a rail with several sources;
- a switcher without an efficiency;
- a load on a regulator's input pins, whose current KiPI would count twice: the message
  names the load and the VRM and says to remove the load or the VRM input;
- a load on a link's pads, for example one standing in for what flows through a ferrite,
  as a single-rail setup would have it: the message says to remove the load or the link;
- an unknown engine option.

`validate_setup(setup)` returns KiPI's `validate()` messages. `terminal_names(project)`
gives the name each source, load and link gets in the setup, so Task 7 can map KiPI's
reports back to the project.

A rail solved on its own (`solve_rail`) does not use the v2 fields. Its assumptions say
which ones only the power-tree solve uses: the regulator kind, the input, ground and sense
pins, and the loads' ground pins.

### Pin references

In the project file, a part's pins are given in a nested mapping: `{ref, net, pins}`. It is
used for a source's `input`, `ground`, `sense` and `ground_sense`, for a load's `ground`, and
for a link's sides `a` and `b`. On a link, the part (`ref`) is given once, at link level.

| Coppernicus | KiPI `PinRef` | Default | Notes |
|---|---|---|---|
| `ref` | `ref` | the part's own `ref` | Empty means the same part: a regulator's input and ground pins are on the regulator. |
| `net` | `net` | required | A pin reference is set only when its `net` is given; without a net the reference is absent (`None`). |
| `pins` | `pins` | `all` | See below. |

The `pins` values are resolved on the board when the project is mapped:
- `all` becomes `[]`, which KiPI reads as every pad of the part on that net;
- a glob pattern (`"A*"`) becomes the pad numbers it matches;
- exact pad numbers are checked against the board.

A part with no pads on the net, or a pattern that matches nothing, raises `EngineError`
with `Board.pads`' message: it lists the pins that *are* on the net.

A source's output pins and a load's supply pins carry no net of their own: they use the
rail's net. When the rail follows net ties, the net is the one the pads are actually on,
which is the rail's net or a net tied to it. A KiPI `PinRef` holds a single net, so pads
spread over several tied nets raise `EngineError`: pick the pins on one net.

### Source → `Vrm`

| Coppernicus (`rails[].sources[]`) | KiPI `Vrm` | Unit | Default | Notes |
|---|---|---|---|---|
| `name`, else `ref` | `name` | — | `ref` | Made unique (see Names). |
| `ref`, `pins` on the rail's net | `output` | — | `all` pins | See Pin references. |
| `voltage` | `voltage` | V | required | Field name kept from v1, which has no unit suffix. |
| `kind` | `kind` | — | `"source"` | One of `"source"` (ideal supply), `"ldo"` or `"smps"`. KiPI's `"isolated"` is not exposed yet: it also needs `input_ground`. |
| `efficiency` | `efficiency` | fraction 0–1 | `None` | Required for `"smps"`; without it, `EngineError` (no default efficiency). For `"source"` and `"ldo"`, `1.0` is passed: KiPI's DC solve (`tree._input_current`, `validate`) does not read it for these kinds. KiPI's Coupled Noise checks do read it (`noise/checks.py`: `vrm.efficiency or 0.9`), so Task 10 must take it from the user. The project loader rejects values outside (0, 1], so "90" given as a percentage is caught. |
| `quiescent_a` | `quiescent_a` | A | `0.0` | Extra input current of an LDO or switcher. |
| `input` (`input_ref`, `input_pins`, `input_net`) | `input` | — | none | KiPI requires it for `"ldo"` and `"smps"`: this is what connects the regulator into the power tree. It is dropped for `"source"`, which has no input. The project file still keeps it. |
| `ground` (`ground_ref`, `ground_pins`, `ground_net`) | `ground` | — | none | Turns on the ground-net solve. A `"source"`'s ground pins are that ground's 0 V reference. |
| `sense` (`sense_ref`, `sense_pins`, `sense_net`) | `sense` | — | none | **Proposed (beyond the plan's field list).** Remote sense: the regulator holds its voltage at these pins. KiPI's power-chain validation board, which Task 7 copies, uses it. |
| `ground_sense` (`ground_sense_ref`, `ground_sense_pins`, `ground_sense_net`) | `ground_sense` | — | none | **Proposed.** Negative remote sense: the regulator holds the voltage above these pins. |
| — | `input_ground`, `phases`, `current_limit_a`, `dropout_v`, `vin_min`, `vin_max`, `vin_pct`, `vin_dv`, `acc_*`, `load_line_mohm`, `mesh_sense`, `sense_points` | — | KiPI defaults (off) | Not exposed in Task 6. |
| — | `bandwidth_khz` | kHz | KiPI's 50 | AC only; the DC solve does not read it. Task 9 must take it from the user, not from KiPI's default. |

### Load → `Load`

| Coppernicus (`rails[].loads[]`) | KiPI `Load` | Unit | Default | Notes |
|---|---|---|---|---|
| `name`, else `ref` | `name` | — | `ref` | Made unique (see Names). |
| `ref`, `pins` on the rail's net | `supply` | — | `all` pins | See Pin references. |
| `current` | `current` | A | required | Split equally across the supply pins, as in the per-rail solve (`groups` stays empty). |
| `ground` (`ground_ref`, `ground_pins`, `ground_net`) | `ground` | — | none | The pins the load's current returns through. |
| — | `tol_min`, `tol_max`, `tol_pct`, `tol_dv`, `dc_tol_*`, `ac_tol_*` | — | `None` | Without a tolerance, KiPI's `LoadReport.passed` stays `None` (`tree._load_report`). It becomes `False` only when the load's regulator is out of regulation: an LDO whose input sits below its output (dropout 0), or an input range, which is not exposed. That is a physical condition, not a threshold. Coppernicus judges against the rail limits the user set (see below). |
| — | `groups` | — | `[]` | Equal split. |
| — | `sources` | — | `False` | Loads that feed current back into the rail are not exposed. |

### Link → `DcLink` (new: `links:` in the project file)

| Coppernicus (`links[]`) | KiPI `DcLink` | Unit | Default | Notes |
|---|---|---|---|---|
| `name` | `name` | — | required | Made unique (see Names). |
| `ref` with `a` (`pins_a`, `net_a`) | `side_a` = `PinRef(ref, net_a, pins_a)` | — | `all` pins | A missing pad raises `EngineError`. |
| `ref` with `b` (`pins_b`, `net_b`) | `side_b` = `PinRef(ref, net_b, pins_b)` | — | `all` pins | |
| `resistance_ohm` | `resistance` | Ω | required | Must be ≥ 0. A value of 0 is a short (KiPI's `SHORT_OHMS` = 1e-9 Ω). |
| — | `esl_h` | H | `None` | AC only (Task 9). |
| — | `spice`, `more_sides` | — | none | Not exposed. |
| a net-tie footprint on the board | `DcLink("net tie <ref>", PinRef(ref, net_a, [number of pad_a]), PinRef(ref, net_b, [number of pad_b]), 0.0)` | Ω | 0 | Added automatically, as the per-rail solve does, for every net in the setup. A tie that touches a net of a rail with `follow_net_ties: false` is left out, even when it is reached from another net, so that rail stays apart. `NetTie` holds pad ids, which become pad numbers through `Board.pad(id).number`. KiPI's `tree._links_in` turns the link into a `SeriesLink` with `dc_resistance()` = 0, the same short the per-rail solve builds. |

### Project and settings → `PISetup`

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `settings.max_edge_mm` | `settings.max_edge_mm` (`SolveSettings`) | mm | 0.5 | Through `engine_settings()`, as in the per-rail solve, so the numbers match. |
| `settings.via_plating_um` | `settings.via_rules` | µm → mm | `None` = KiPI's table | Same as the per-rail solve. |
| `settings.copper_temp_c` | not in `PISetup`: `solve_tree(temperature=…)` | °C | 20 | A callable from xy (mm) to °C, which `tree` hands to `NetProblem(temperature=…)`. Task 7 passes the same uniform field that `solve_rail` sets on `NetProblem.temperature`. |
| `rails[].limits.max_drop_pct` | — | % | `None` | Judged by Coppernicus on each group's result (Task 7), never inside KiPI. |
| `rails[].limits.max_current_density` | — | A/mm² | `None` | Same. |
| `rails[].limits.max_via_current` | — | A | `None` | `PISetup.via_current_limit_a` is set to `None`. KiPI's default is a 1 A placeholder, and Coppernicus does not invent limits. In KiPI only its own HTML report reads this field (`dc/report/html.py`); `solve_tree` does not. |
| `rails[].follow_net_ties` | net-tie `DcLink`s | — | `true` | See the Link table. |
| `engine_options.excluded_nets` | `excluded_nets` | — | `[]` | KiPI's `active_setup`: solve part of a tree before the rest is modelled. |
| `engine_options.<any other key>` | — | — | — | `EngineError` listing the keys accepted. This is an engine-specific passthrough, validated here. |
| `marked_rails`, `board`, `path` | — | — | — | GUI and file only. |
| — | `thermal` | — | off | Task 8. |
| — | `caps`, `probes`, `packages`, `ac` | — | empty | Task 9. |
| — | `aggressors`, `victims`, `noise` | — | empty | Task 10. |
| — | `stackup` | — | `None` (the board's own) | Only KiPI's GUI reads it; `solve_tree` takes the stackup from the snapshot. |
| — | `nets`, `html_report`, `report_format` | — | KiPI defaults | GUI and report only; `solve_tree` does not read them. |

### Rails, grounds, names

- **Rails.** Every rail with at least one source or load is mapped. A rail with neither (a
  net picked but not set up yet) is skipped.
- **Several sources.** KiPI drives each net group from one VRM, so a rail with two or more
  sources raises `EngineError`. The per-rail solve still accepts several sources.
- **Ground nets** need no rail entry: they come from the `ground` and `ground_sense` fields.
  A rail on a net the project uses as a ground (set up with a 0 V source and its return
  pins as loads, to solve that ground on its own) is not mapped as a rail: the tree solves
  the net as a ground and says so in a warning. Its limits still judge the ground group.
- **Names.** KiPI wants every VRM, load and link name to be unique.
  - A label used once is kept as it is.
  - A source or load label used more than once becomes `"<label> (<net>)"`. A typical case
    is the same IC loading two rails.
  - Any duplicate still left gets `" (2)"`, `" (3)"`, … in order (VRMs, then loads, then
    links), never onto a name already in use. Net-tie links take `"net tie <ref>"` the same
    way.

### Project file (v2 additions; every one is omitted while at its default)

```yaml
rails:
- net: +1V8
  sources:
  - name: U2
    ref: U2
    pins: ['2']
    voltage: 1.8
    kind: ldo
    quiescent_a: 0.002
    input: {net: +3V3_F, pins: ['1']}          # ref omitted = U2
    ground: {net: GND, pins: ['3']}
    sense: {ref: U3, net: +1V8, pins: ['1']}
    ground_sense: {ref: U3, net: GND, pins: ['2']}
  loads:
  - ref: U3
    pins: ['1']
    current: 2.0
    ground: {net: GND, pins: ['2']}
links:
- name: FB1
  ref: FB1
  a: {net: +3V3, pins: ['1']}
  b: {net: +3V3_F, pins: ['2']}
  resistance_ohm: 0.05
engine_options:
  excluded_nets: []
```

A project file written before v2 loads unchanged, and its `to_dict()` is the same dict: no
key is added.

---

## Task 7: power-tree solve → Coppernicus results (`kipi/dc/pi/tree.py`, `kipi/dc/pi/results.py`)

Code: `coppernicus.engine.tree.solve_project_tree(board, project, progress=None,
cancelled=…) -> TreeResult`. It wraps `kipi.dc.pi.tree.solve_tree(snapshot, to_pisetup(board,
project), progress, temperature)`.

Before solving, it raises `EngineError` for:
- KiPI's `validate()` messages (a VRM whose input is its own output, an LDO without input
  pins, a ground with no 0 V reference, a loop…);
- a load without ground pins while a ground net is solved;
- an LDO or switcher without ground pins while a ground net is solved. Plain sources may go
  without. The message names every such part and says "add its ground pins (ground) or
  exclude the ground net (engine_options: excluded_nets)". A ground net listed in
  `excluded_nets` is not solved and asks for nothing.

KiPI's own `SolveError`s become `EngineError` too.

### Groups → `TreeResult.groups: Dict[str, RailResult]`

| KiPI `GroupResult` | Coppernicus | Notes |
|---|---|---|
| key `label_of(g)` ("busiest net (+N nets)") | the key | A rail group is keyed by the project rail its VRM drives (`+3V3`, even when FB1 joins `+3V3_F` to it). A ground group is keyed by the first ground net the project names in it (`GND`). |
| `nets` | `RailResult.net` = the key, `RailResult.nets` = every net of the group, the key first | |
| `kind` (`"rail"` / `"ground"`) | `RailResult.kind` (new field, default `"rail"`) | Additive. Per-rail results keep `"rail"`. |
| `result.mesh / solution / fields` | layers, vias, power loss, `j_max`: through the per-rail `_convert` | The same arrays as a per-rail result, so maps, isolines, arrows, Measure and Compare work unchanged. |
| `fields.v_ref` (rail: the VRM's set point above the ground it regulates against; ground: 0 V) | `RailResult.v_ref` | Drops on a rail are measured from the set point, as in KiPI. A remote-sensed VRM lifts its output pins above it: a boost, shown as a negative drop. |
| `fields.summary["max_drop"]` | `RailResult.max_drop` | On a ground group, `max_drop` is the largest **rise** above 0 V (`summary["max_boost"]`), and the map field becomes "Ground rise (mV)". |
| `fields.terminals` | `RailResult.sources` / `loads` | Every terminal of the group: VRM outputs, setup loads, the inputs of the VRMs it feeds (`"U1 (input)"`), sense pins (0 A), ground references (`"J1 (gnd)"`), returns (`"U3 (return)"`), VRM grounds (`"U1 (gnd)"`). `ref` = the footprint of the first pad. |
| `iterations`, `converged` | `RailResult.stats["iterations"]`, a warning when not converged | |

### VRMs → `TreeResult.vrms: List[VrmReport]` (new, `results.py`)

| KiPI `VrmReport` | Coppernicus `VrmReport` | Unit | Notes |
|---|---|---|---|
| `name` | `name` | — | The setup name (`terminal_names`). |
| `kind`, `group` | `kind`, `group` (our key) | — |
| `v_set` | `v_set_v` | V |
| `v_out` | `v_out_v` (at its output pins) | V |
| `v_sense` | `v_sense_v` (`None` without sense) | V |
| `i_out` | `i_out_a` | A |
| `v_in`, `i_in` | `v_in_v`, `i_in_a` (`None` for a source) | V, A |
| `p_out`, `p_in`, `p_loss` | `p_out_w`, `p_in_w`, `p_loss_w` | W | Power across its own pins, from its ground pins (KiPI). |
| `v_ground` | `v_ground_v` | V |
| `v_ground_sense` | `v_ground_sense_v` | V |
| `regulation` | `regulation` | — | KiPI's reason it cannot hold its voltage (an LDO with less than its dropout across it; the dropout defaults to 0 V, so this means its input fell below its output). |
| `current_limit_a`, `passed`, `phases`, `load_line_mohm`, `v_droop` | — | | Not exposed (no current limit, single phase, no load line). |

### Loads → `TreeResult.loads: List[LoadReport]` (new, `results.py`)

| KiPI `LoadReport` | Coppernicus `LoadReport` | Unit | Notes |
|---|---|---|---|
| `name`, `group` | `name`, `group` (our key) | — | |
| `current` | `current_a` | A | 0 when disconnected. |
| `nominal` | `nominal_v` | V | The target voltage of the VRM that drives the group. |
| `v_supply`, `v_supply_min` | `v_supply_v` (mean over the supply pins), `v_supply_min_v` | V | |
| `v_ground` | `v_ground_v` (mean, `None` without ground pins) | V | |
| `v_effective` | `v_across_v` = `v_supply_v − (v_ground_v or 0)` | V | |
| `v_low` (else `v_worst`) | `v_worst_v` | V | Lowest supply pin minus highest ground pin: the worst case for a drop limit. |
| — | `drop_v` = `nominal_v − v_worst_v`, `drop_pct` | V, % | The ground's rise counts in the drop: this is the voltage the part sees. |
| `status` | `status` | — | `DISCONNECTED`, `NOT CONVERGED` or `VRM OUT OF REGULATION`, as KiPI reports them. |
| `passed` | `passed` | — | **Coppernicus's own verdict** (`power_tree.judge_loads`): `drop_pct ≤ max_drop_pct` of the group's rail; `None` without a limit, or for a load that is disconnected or not converged. KiPI's `passed` is ignored, since no tolerance is passed to it. When a limit changes after the solve, the GUI judges again without solving. |

### Verdicts (only against the user's limits, per group)

- A rail group uses the limits of the project rail it is keyed by (`max_drop_pct` on its loads'
  `drop_pct`; `max_current_density` and `max_via_current` on its copper and vias, as in the
  per-rail solve).
- A ground group uses the limits of a project rail with the ground's net, if the user set
  limits on it (select `GND` in the net list and type a limit).
  - `max_drop_pct` does not apply to a ground, which has no nominal voltage: it is reported
    as not judged.
  - `max_ground_rise_mv` judges each return pin's rise and the copper's
    largest rise above 0 V.
  - The other two limits apply as on a rail.
  - `max_ground_rise_mv` set on a supply rail does not apply in the tree.
- A net solved on its own with `max_ground_rise_mv` set gets one "rise" problem: the copper
  furthest from its source voltage, in mV, against the limit. It is meant for a ground net
  solved alone.
- The field is omitted from the project file while it is unset, so older files round-trip
  unchanged.
- The Problems list of a tree group ranks its loads by `drop_pct` (supply minus ground), not by
  supply drop alone. On a ground group it ranks return pins by rise. Hotspots and vias are
  ranked the same way on both.

### Per-rail solve of a rail that feeds regulators or links (coordinator-approved)

`solve_rail(board, rail, settings, …, project=None)`. With the project, a rail that feeds a
regulator input or a link draws an estimate of that current. The estimate uses nominal
voltages and no copper losses, mirroring KiPI's `_estimated_input`:
- LDO: `I_out + I_q`;
- switcher: `|V_out|·I_out / (η·|V_in nominal|) + I_q`;
- a link to another net: what that net's group draws.

Here `I_out` is what the regulator's output group draws, recursively. Each estimate is a load
named `"U1 (input, estimated)"` / `"FB1 (link, estimated)"` on those pins. One assumption
line names the regulators, their η and I_q, and says "estimated at nominal voltages; the
power-tree solve computes it exactly". A switcher without η raises. A load already on those
pins raises the same double-counting error as the tree. Without `project`, `solve_rail` is
unchanged.

`tree_summary(tree)` (`coppernicus.power_tree`) is the JSON form of a `TreeResult`. It is
printed by `coppernicus solve --tree --json` and written to `<project>_tree_summary.json`,
and the agent interface will reuse it. It has:
- `verdict` and `verdict_note`, judged per group against the user's limits only;
- `vrms`, `loads`, `links` and `groups` tables, with units in every key (a ground group has
  `max_rise_mv`; its `max_drop_pct` is `null`);
- `assumptions` and `warnings`.
