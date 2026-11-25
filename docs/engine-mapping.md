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
| `fields.summary["max_drop"]` | `RailResult.max_drop` | On a negative rail, the voltage lost toward 0 V (`summary["max_boost"]`). On a ground group, the **rise** with the largest magnitude, signed: `max_boost` above 0 V, or `−max_drop` when the ground sinks below 0 V (under a negative rail). The same signed number is in the rise problem, the return pins, the map and the double-click readout; limits judge its magnitude. |
| `fields.terminals` | `RailResult.sources` / `loads` | Every terminal of the group: VRM outputs, setup loads, the inputs of the VRMs it feeds (`"U1 (input)"`), sense pins (0 A), ground references (`"J1 (gnd)"`), returns (`"U3 (return)"`), VRM grounds (`"U1 (gnd)"`). `ref` = the footprint of the first pad. |
| `iterations`, `converged` | `RailResult.stats["iterations"]`, `RailResult.converged` (new field, default `True`), a warning when not converged | Nothing in a group that did not converge is judged, copper included. |
| terminal pads | `TerminalResult.net` (new field, default `""`) | The net of its first pad: return pins are judged by their own net's limits. |

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
| terminal `"U1 (input)"` of the feeding group, `"U1 (gnd)"` of the ground group | `input_net`, `input_group`, `input_nominal_v`, `v_in_worst_v`, `input_drop_v`, `input_drop_pct`, `input_status`, `input_passed` | V, % | New (second review). A regulator's input judged as a load on the rail feeding it: `v_in_worst_v` = lowest input pin − highest ground pin (on a negative rail the highest minus the lowest), `input_drop_v` = toward 0 V from the set voltage of the source driving that rail. `input_status`: `DISCONNECTED`, `NOT CONVERGED`, or `VRM OUT OF REGULATION` when that source cannot hold its voltage. |

### Loads → `TreeResult.loads: List[LoadReport]` (new, `results.py`)

| KiPI `LoadReport` | Coppernicus `LoadReport` | Unit | Notes |
|---|---|---|---|
| `name`, `group` | `name`, `group` (our key) | — | |
| `current` | `current_a` | A | 0 when disconnected. |
| `nominal` | `nominal_v` | V | The target voltage of the VRM that drives the group. |
| `v_supply`, `v_supply_min` | `v_supply_v` (mean over the supply pins), `v_supply_min_v` | V | |
| `v_ground` | `v_ground_v` (mean, `None` without ground pins) | V | |
| `v_effective` | `v_across_v` = `v_supply_v − (v_ground_v or 0)` | V | |
| `v_low` (else `v_worst`) | `v_worst_v` | V | Lowest supply pin minus highest ground pin: the worst case for a drop limit (on a negative rail `v_high`, the pin that lost the most toward 0 V). |
| — | `drop_v` = `nominal_v − v_worst_v` (toward 0 V), `drop_pct` = `drop_v / |nominal_v|` | V, % | The ground's rise counts in the drop: this is the voltage the part sees. No percentage at 0 V nominal (`NaN`, `null` in JSON, "—" in tables): not judged. |
| — | `net` (new field) | — | Its supply net, for the per-net limits. |
| `status` | `status` | — | `DISCONNECTED`, `NOT CONVERGED` or `VRM OUT OF REGULATION`, as KiPI reports them. |
| `passed` | `passed` | — | **Coppernicus's own verdict** (`power_tree.judge_loads`): `drop_pct ≤ max_drop_pct` of its net (per-net rule below); `None` without a limit, or for a load that is disconnected, not converged, behind a regulator out of regulation, or at 0 V nominal. KiPI's `passed` is ignored, since no tolerance is passed to it. When a limit changes after the solve, or changed while it solved, the GUI judges again without solving (`power_tree.apply_limits` reads the project's current limits). |

### Verdicts (only against the user's limits; second review, coordinator-approved)

Which limit judges what (`power_tree.LIMITS_RULE`, also an assumption line of every tree):
- **Per net.** A load is judged by its supply net's limits, a regulator's input by its input
  net's, a return pin by its ground net's. A limit not set on a net comes, field by field,
  from its group's driving net: a rail group's key (the net its source drives), or a ground
  group's key (the first ground net the project names). So a limit on `+3V3` also judges
  `U2 (input)` on `+3V3_F` beyond FB1, unless `+3V3_F` has its own.
- **Copper per group, strictest.** The largest ground rise, current crowding and vias of a
  group are judged against the strictest (lowest) value set on any of its nets, because the
  solve does not keep which net each triangle belongs to. This is conservative: it may fail
  copper of a net against a tighter limit of another net of the group, never pass it falsely.
  A copper fail names the limit and where it came from: "(limit 1 mV from AGND, strictest in
  group)". Per-net copper attribution is a known limitation.
- **Links** are not judged as points of their own: a link joins its nets into one group,
  whose loads and regulator inputs are judged directly.
- **Regulator inputs** are judged as loads on the rail feeding them (`VrmReport.input_*`),
  across their input and ground pins, as the tree's loads are. VIN solved alone judges the
  estimated input on the input pins only (no ground in a single-rail solve); both fail the
  same tight limit on the example.
- **Which limit applies to which net: one rule, by the reference voltage (T7.1), in the tree
  and for a rail solved alone** (`problems.limits_dict`, `problems.inapplicable_limits`).
  `max_ground_rise_mv` judges a net at 0 V: a ground group, or a net driven at 0 V (solved
  alone, or a 0 V rail group of the tree). `max_drop_pct` judges a net with a nonzero
  nominal. Copper limits apply to both. A limit set on the other kind is a not-judged entry,
  never dropped or judged as something else: "+3V3 (max_ground_rise_mv): applies to nets at
  0 V (grounds) only"; on a ground group, "GND (max_drop_pct): 0 V nominal: a % drop limit
  does not apply". On a rail driven at 0 V a drop limit is listed load by load ("0 V
  nominal"), as the per-rail service always did. So `+3V3` solved alone with
  `max_ground_rise_mv` no longer fails on its deviation: it is not judged. The rail panel
  disables the field that does not apply to the shown net (kept editable while it holds a
  value, so a value read from the file can be cleared).
- On a ground, `max_ground_rise_mv` judges each return pin's rise and the copper's largest
  rise, by magnitude (signed values shown).

The verdict of a group (`power_tree.group_verdict`):
- `fail` when a limit is exceeded;
- otherwise `not_judged` when a limit set applies to an item that could not be judged
  (`DISCONNECTED`, `NOT CONVERGED`, `VRM OUT OF REGULATION`, 0 V nominal for a drop
  percentage, a return pin not solved), when `max_drop_pct` has no load or regulator input
  to judge, when the group did not converge, or when no limit applies;
- `pass` only when everything a limit applies to was judged and passed.

Each not-judged entry is an object `{"item", "net", "limit", "reason"}` (T7.1; the per-rail
service's entries are `{"item", "limit", "reason"}`, plus `"rail"` at project level):
`item` is the load, the regulator input `"U2 (input)"`, the return pin, or the net itself
for net-level entries (a wrong-kind limit, a net not solved, a group that did not converge,
a drop limit with nothing to judge); `limit` is the limit's key with its unit; `reason` is
text. The verdict note stays text: "Not judged: U3 on +1V8 (max_drop_pct): VRM OUT OF
REGULATION; …".

Overall (`power_tree.tree_verdicts`): `fail` if a group fails; otherwise `not_judged` if a
group with limits (or with a limit of the wrong kind) is `not_judged`, or a limit is set on a net the tree does not solve
(excluded, or not set up); otherwise `pass` if something was judged; `not_judged` when no
limit is set. Groups without limits do not stop a pass ("judged only against the limits you
set"). `solve --tree` exits 0 for `not_judged`, as the per-rail solve does; its text output
names the not-judged items on the `Verdict:` line.
- A net driven at 0 V solved on its own with `max_ground_rise_mv` set gets one "rise"
  problem: the copper furthest from 0 V, signed, judged by magnitude. On a net with a
  nonzero nominal the limit does not apply (above).
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
- `verdict` and `verdict_note`, judged against the user's limits only (rules above);
  `not_judged`: the items a limit set could not judge, with the reason;
- `vrms`, `loads`, `links` and `groups` tables, with units in every key (a ground group has
  `max_rise_mv`, signed; its `max_drop_pct` is `null`). Each VRM has `input_net`,
  `input_drop_mv`, `input_drop_pct`, `input_status`, `input_exceeds_drop_limit`; each group
  has `limits` (the strictest of each kind), `limits_by_net` and `not_judged`. Regulator
  inputs are among a rail group's problems as kind `"load"`, named `"U1 (input)"`;
- `assumptions` and `warnings`.

### Which way the load current flows (T7.1): negative rails and nets at 0 V

A load takes its current from a positive rail, so the copper falls below the source toward
the loads. On a negative rail, and on a net at 0 V (a ground), it returns its current into
the copper: a negative rail's copper moves toward 0 V, and a ground rises above it. KiPI's
power tree does this (`_Tree.sign`, #220). `solve_rail` does the same: each load's (and
estimated draw's) current is given the sign of its rail, from the sources' set voltages
(`engine.solve.load_sign`: all above 0 V → out of the copper; all at or below 0 V → into
it; sources on both sides of 0 V are refused), and an assumption line says so.

On the 40 mm strip with 1 A: at −3.3 V the load reads −3.262072 V alone as in the tree (it
read −3.337928 V before T7.1); at 0 V it reads +37.928 mV, as the ground group of a tree
reads its return pin. Node by node V(−3.3 V) = −V(+3.3 V) and V(0 V) = 3.3 V − V(+3.3 V);
current densities, losses, via currents and drops are unchanged, and positive rails are
identical to the last digit (power chain and a real project checked).

`RailResult.drop_sign` (new field, default +1) records the direction, because it cannot be
told from `v_ref`: a rail group at 0 V inside KiPI's tree (no ground pins named) still
draws its loads' current (KiPI's sign is −1 only below 0 V). Everything measures the drop
as `drop = drop_sign · (v_ref − V)` (`RailResult.drop_at`), its worst pin by the same sign
(`worst_v`: the highest pin when the current flows in), and its label with `drop_unit`
("mV below 3.3 V", "mV above -3.3 V (toward 0 V)", "mV above 0 V (ground rise)"). A ground
group is always a rise (`RailResult.sign` is −1 there).

| Where | What |
|---|---|
| `RailResult.drop`, `drop_pct`, `max_drop` | Toward 0 V on a negative rail, the rise at 0 V (KiPI's `max_boost` when the current flows in); `drop_pct` of |v_ref|. |
| GUI map, isolines, colour bar, double-click readout, Δ drop vs baseline | `drop_at`; label `drop_unit`; "rise =" at 0 V. |
| `maps.py` PNG maps | `drop_at`; label "drop, …" or "rise, …". |
| Service: rail summary `drop_sign`, layers' `max_drop_mv`, `probe` `drop_mv` / `drop_pct` | `drop_at`. `v_min_v` / `v_max_v` stay the lowest and highest pin: on a negative rail or at 0 V the worst pin is `v_max_v`. |
| Report: meta `drop_sign`, `drop_unit`, `drop_max_mv`, layers' `max_drop_mv`; `report.js` lut, readout, colour bar, worst card | the same formula in JS (`dropSign(rail)`; reports written before the field read +1). |
| Tree: a 0 V rail group (no ground pins named) | `drop_sign` +1, and a warning in `tree.warnings` and the group's notes: "… is solved as a 0 V rail (no ground pins named): KiPI draws its loads' current out of it, so its voltages are shown with the opposite sign to a ground's; name ground pins on it to solve it as a ground." |

---

## Task 8: electro-thermal board temperature (`kipi/dc/thermal/cosim.py`, `kipi/dc/thermal/model.py`)

Code: `coppernicus.engine.tree.solve_project_tree(board, project, progress=None, cancelled=…,
thermal=None)`. `thermal=None` follows `project.thermal.enabled`; `True` forces the
electro-thermal solve, `False` the plain tree. With it, `coppernicus.engine.thermal` solves
the tree and the result carries `TreeResult.thermal` (a `BoardTemperature`). Without it the
tree is solved exactly as in Task 7: the thermal-off `tree_summary` of the demo and
power-chain examples is byte-identical before and after this task. One rail (F5, `solve
--rail`) is never heated; with `thermal.enabled` set its result says "the board temperature is
solved with the power tree only (Ctrl+T / solve --tree)".

### `ThermalOptions` → `ThermalSettings` (model.py:84-98)

| Coppernicus (`thermal:` block) | KiPI | Default (KiPI's) | Checked on load |
|---|---|---|---|
| `enabled` | `enabled` | `false` | true or false |
| `ambient_c` | `ambient_c` | 25 °C | finite |
| `iterations` | `iterations` | 3 | a whole number ≥ 1 (KiPI clamps; we refuse) |
| `airflow_m_s` | `airflow_m_s` | 0 m/s | finite, ≥ 0 |
| `grid_mm` | `grid_mm` | 0 = automatic (max(span/250, 0.2 mm), model.py:182) | finite, ≥ 0; no floor |
| `load_heat` | `load_power` | `false` | true or false |
| `max_temp_c`, `max_rise_c` | not passed (judged by Coppernicus) | none | finite or empty |
| `regulator_heat` | — (see below) | `output_pads` | `output_pads` or `part_pads` |
| — | `edges` | none | not exposed |

The block is written only with the keys not at their default (`thermal:` is omitted while
all are), so older files load and save unchanged. Unknown keys raise `ProjectError`.

### The loop: a thin copy of `solve_electrothermal` (cosim.py:232-280)

`engine.thermal._electrothermal` repeats KiPI's loop with KiPI's own pieces: one
`BoardThermalModel` (grid, conductances, one factorization), every cell at `ambient_c`, then
`iterations` passes of `solve_tree(temperature=model.sampler(temps), mesh_cache, factor_cache)`
→ heat (`heat_sources`, or `_Heat` + `_copper_heat` for `part_pads`) → `model.deposit` →
`model.solve`, with KiPI's history entry per pass. Iteration 1 therefore solves the copper at
the ambient, not at 20 °C; each later one at the temperature the last gave, each layer its own
(`kipi.core.temperature.at` with `on_layers`). It differs from KiPI's function in three ways
only:
- it keeps iteration 1's load drops: the "drop at the ambient" baseline, with no extra solve;
- `regulator_heat: part_pads` places the regulators' conversion loss differently (below);
- its settling warning is Coppernicus's own (in `BoardTemperature.warnings`).

**Drift test** (`tests/test_thermal.py::test_our_loop_is_bit_identical_to_kipis`): with
`output_pads`, the temperatures, the history and every load drop equal KiPI's own
`solve_electrothermal` to the last bit (`==`, NaN masks equal), on the demo board and on the
power chain. `engine.thermal.solve_kipi_reference` runs KiPI's function through the same
conversion for it. A KiPI upgrade that changes the loop fails this test.

`copper_temp_c` is not read by the electro-thermal solve (the copper's temperature comes from
the loop); the assumption "Copper at … °C" is replaced by the electro-thermal one, and a line
says `copper_temp_c` is not used when it is not 20 °C. `copper_thickness_mm` restacks the board
as for every solve, and the heat model reads the restacked thickness. `via_plating_um` (KiPI's
`via_rules`) sizes the barrels that carry heat between layers as well as current.

### Where the heat goes (cosim.py:190-229)

- Copper losses: each triangle's loss at its centroid, on its own layer.
- Via and plated-hole barrels: their loss shared by the layers the barrel spans.
- Links (`DcLink`): their loss spread evenly over the link's pads.
- Regulators: each phase's conversion loss (input − output power), split evenly over pads,
  each pad's share over its copper layers, as a point at the pad centre deposited into the one
  grid cell that holds it (model.py:348-375; a point just outside the board goes to the
  nearest inside cell). `regulator_heat` chooses the pads:
  - `output_pads` (default): KiPI's placement, the phase's output pins (cosim.py:205-214);
  - `part_pads`: every copper pad of the regulator's part, all nets.
- Loads (`load_heat: true`): each load's power, Σ pin current × pin voltage, at its pins.

The package itself (its body, thermal pad, junction-to-board resistance, its own convection)
is not modelled under either option. With `output_pads` the temperatures at the output pads
are likely overestimated (an upper-bound reading); with `part_pads` the split is even, so
individual pads can be over- or under-estimated: on a test board the grid peak rose 11 % and
moved to the regulator's GND pad, copper of no solved net.

### Results (`results.py`)

- `TemperatureMap`: one per copper layer, `x_mm`, `y_mm` (cell centres) and `t_c` (ny, nx)
  °C, NaN outside the board; `peak_c`, `hottest()`, `at(x, y)` (the cell holding the point).
- `LayerTemperature`: KiPI's `layer_temps` row (report.py:94-119): the hottest **solved
  copper** of a layer (the temperature read at its mesh nodes), its rise over the ambient,
  where, its group, `passed`, and the nearest pad (`pad_ref`, `pad_number`,
  `pad_distance_mm`: the distance in the plane from the point to the pad's copper on any
  layer, 0 on the pad; the nearest centre on a tie). Always given, with no threshold.
- `BoardTemperature`: the maps, the rows, the grid peak (any layer; shown, never judged) with
  its nearest pad, `ambient_c`, `airflow_m_s`, `h_w_m2k` (KiPI's `h_w_mm2_k()·1e6`), the cell
  size and count, `iterations`, `settled` (KiPI's rule, cosim.py:276: the last iteration
  changed no cell by more than 5 % of the peak rise; `None` after one iteration), `history`
  (`iteration`, `heat_w`, `t_max_c`, `dt_max_c`), `heat_w` and `heat_by_kind_w`, the load
  drops at the ambient and electro-thermal, `regulator_heat`, `load_heat`, assumptions and
  warnings.
- `TreeResult.thermal` (`None` without the solve) and `TreeResult.thermal_limits`
  (`{max_temp_c, max_rise_c}`, those set; `None` when none is).

### Verdict (coordinator's rulings; `coppernicus.temperature`)

Only the user's `max_temp_c` / `max_rise_c` judge, by KiPI's rule (`temp_passed`,
model.py:114-120), and only the solved-copper rows. `apply_thermal_limits(tree, options)`
re-judges without a solve.

| Board temperature | When |
|---|---|
| `fail` | a row exceeds a limit, settled or not; when not settled the text says "not settled: last change X °C" |
| `not_judged` | no row exceeds, and: the solve did not settle ("last change X °C over a Y °C rise: run more iterations"), ran one iteration ("one iteration: settling not checked"), a row's group did not converge, or there is no solved copper. One entry per limit set: `{item: "board temperature", net: "", limit: "max_temp_c" / "max_rise_c", reason}` |
| `not_judged` | limits set but the tree was solved without the temperature: "board temperature not solved (thermal disabled in the project)" |
| `not_judged` | no limit set (no entry: nothing is judged) |
| `pass` | every row judged and within the limits |

When no row exceeds a limit but the grid peak does (for instance on copper of a net that is
not solved), the verdict note names the peak, its nearest pad, and says it is not judged.

The overall power-tree verdict (`power_tree.tree_verdicts`) takes the board temperature as one
more group: fail wins, then a not-judged item from a limit set, then pass. `tree_summary`
merges its not-judged entries and failed rows into the top-level `not_judged` and `failed`,
and names the thermal limits in `verdict_note`; `solve` exits 1 on fail, 3 when a limit set
could not judge. The app's verdict header (`main_window._tree_verdict`) still counts groups
only: the Temperature tab (task 8b) adds the board temperature there.

### Summary (`tree_summary(tree)["thermal"]`, present only when the temperature was solved)

| Group | Keys |
|---|---|
| Verdict | `verdict`, `verdict_note`, `not_judged`, `limits`, `failed`, `problems` (one per layer row: `severity`, `kind: "temperature"`, `title`, `layer`, `x_mm`, `y_mm`, `t_max_c`, `rise_c`, `group`, `nearest_pad`) |
| Solve | `ambient_c`, `airflow_m_s`, `h_w_m2k`, `grid_mm`, `grid_nx`, `grid_ny`, `grid_cells`, `iterations`, `settled`, `history`, `load_heat`, `regulator_heat` |
| Heat and peak | `heat_w`, `heat_by_kind_w`, `peak_c`, `peak_rise_c`, `peak_x_mm`, `peak_y_mm`, `peak_layer`, `peak_nearest_pad` |
| Rows | `layers: [{layer, t_max_c, rise_c, x_mm, y_mm, group, exceeds_limit, nearest_pad}]`; `loads: [{name, group, drop_ambient_mv, drop_mv, change_pct}]` |
| Notes | `assumptions`, `warnings` |

`nearest_pad` is `{ref, pad, distance_mm}`. No arrays; NaN becomes `null`.

### Command line

`coppernicus solve PROJECT --thermal` (alone, or with `--tree` in either order) solves the
power tree with the board temperature; `--tree` alone does too when the project enables it.
The text output adds a "Board temperature" block (grid peak with its nearest pad, history,
heat by kind, one row per layer with its nearest pad and verdict, load drops at the ambient
→ electro-thermal, the verdict, the thermal assumptions). One PNG per copper layer is written
next to the summary (`<project>_temperature_<layer>.png`, every layer on one scale from the
ambient to the board's peak), unless `--no-maps`. `--rail` is refused, as with `--tree`.

Measured on `examples/demo/demo-thermal.coppernicus.yml` (the demo rail at 8 + 6 + 1.2 A,
automatic 250 × 167 grid, 3 iterations, macOS arm64, SuperLU): grid peak 42.31 °C on J1.1;
hottest copper F.Cu 42.29 °C (rise 17.29 °C, on J1.1), B.Cu 41.77 °C; settled (last change
0.05 °C); U1's drop 39.156 mV at the ambient, 41.354 mV electro-thermal; `max_rise_c` 15 →
fail. About 0.7 s for the solve.

### Assumptions (in `BoardTemperature.assumptions`)

Steady state, ambient, airflow and h per face (KiPI's 10 + 3.8·v W/m²K); the iterations and
ρ(T) with the last change and whether it settled (also in the tree's assumptions, in place of
the copper-temperature line); the heat counted, where the regulators' loss goes for the chosen
`regulator_heat`, and whether the loads' own power is; a line per regulator heat source with
the overestimate (`output_pads`) or the even split (`part_pads`) caveat, when a regulator
dissipates; the grid (cells, size, count inside the board; a cell is all copper or none, read
at its centre; every net's copper spreads heat, k = 390 W/m·K); one line per laminate, with
what it was taken as (an unrecognized name: FR4 core, 0.8 / 0.3 W/m·K); the solder mask and
silkscreen, when the stackup has them; the board shape (outline, or the copper's bounding box
plus 1 mm); what is not modelled (heat sinks, enclosures, mounting, edge clamps; packages, no
junction temperature; transients); what is judged (above).

### Known limitations

- **Laminate names.** KiPI recognizes a laminate by a regular expression on its material name
  (materials.py:33-51); the prepreg patterns need a word boundary (materials.py:47-48), so a
  prepreg name with a prefix glued to it falls back to FR4 core (0.8 / 0.3 W/m·K in place of
  prepreg's 0.6 / 0.25). The assumption line names every laminate and what it was taken as.
  Not reported upstream (coordinator's ruling); no override is exposed.
- **Regulator heat is a point deposit.** Each pad's share of a regulator's loss lands in the
  single grid cell holding the pad centre, with no package model: the temperature there grows
  slowly as the cell shrinks (+5-8 % from the automatic grid down to 0.05-0.1 mm on the test
  boards), while the hottest solved copper per layer moves less than 2 %. Read regulator-pad
  temperatures as rough.
- **The grid peak is not judged.** It can sit on laminate or on copper of a net that is not
  solved (a regulator's GND pad with `part_pads`); the rows judge solved copper only, and the
  verdict note names a peak that exceeds a limit the rows do not.
- **No cancel inside the factorization.** KiPI checks for cancel once per iteration and inside
  the tree solve, not during the thermal model's one factorization (most of the time on a large
  board).
