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
| `current_limit_a` | — (not passed) | A | `None` | The rated output current (KiCad field `PI_IMAX`, or the project file). Not passed to KiPI's `Vrm.current_limit_a`: Coppernicus judges it itself (Verdicts: regulator ratings). Passing it would change nothing else (the power-chain tree summary is identical with and without it: KiPI only sets `VrmReport.passed` from it, `tree.py:836-845`), but KiPI's `passed` is set whether or not the group converged, and its bare `abs(i_out) <= limit` fails a part rated exactly for what it delivers on round-off. |
| — | `input_ground`, `phases`, `dropout_v`, `vin_min`, `vin_max`, `vin_pct`, `vin_dv`, `acc_*`, `load_line_mohm`, `mesh_sense`, `sense_points` | — | KiPI defaults (off) | Not exposed in Task 6. |
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
| `settings.min_edge_mm` | `settings.min_edge_mm` (`SolveSettings`) | mm | `None` = KiPI's 0.02 | Advanced: the smallest element at a plated hole's wall. Set only when given; `None` leaves KiPI's default untouched. |
| `settings.grading` | `settings.grading` (`SolveSettings`) | mm/mm | `None` = KiPI's 0.2 | Advanced: element growth away from plated holes, 0 (off) to 2, the range of KiPI's own "Via grading" field. Same path. |
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
| `current_limit_a`, `passed`, `phases`, `load_line_mohm`, `v_droop` | — | | Not read (the rating is not passed to KiPI, single phase, no load line). |
| — (the project's `Source.current_limit_a`) | `current_limit_a`, `current_limit_from`, `current_passed` | A | The rating as the user wrote it (copied in `convert_tree` from the source the engine named), where it was written (`power_tree.rating_origins`, from the merged project: "KiCad field PI_IMAX on U1" or "the project file"; "" when not known), and `power_tree.judge_ratings`'s verdict (Verdicts: regulator ratings). |
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
- **A load's supply net is the net its pins are on**, which a net tie can make another net
  than the rail it is listed in (a load on `VCC_B`, tied to `VCC`, listed in rail `VCC` as
  the KiCad fields put it): it is judged by `VCC_B`'s limits, and a limit `VCC_B` does not
  set comes from the rail it is listed in, then from the driving net. So a limit on the tied
  net (a limits-only rail, or a `PI_LIMIT_DROP` there) judges the loads on it, a looser one
  too. The tree no longer judges them by their rail's net only, which passed a load over
  its own net's limit.
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
- **Regulator ratings** (`power_tree.judge_ratings`). A source's rated output current
  (`Source.current_limit_a`: KiCad field `PI_IMAX`, or `current_limit_a` in the project file)
  is a limit the user wrote: the regulator fails when |I out| is over it, as KiPI's own check
  (`abs(i_out) <= limit`), with |I out| within 1e-9 (relative) of the rating counted as at it
  (the solve's round-off: 2.0000000000001 A out of a 2 A load). It belongs to the group the
  regulator drives: a fail is a `regulator` problem there, "U1: 2.502 A out, over its rated 2 A
  (from the project file)" or "(from KiCad field PI_IMAX on U1)", and is named in the verdict
  note ("Over a rated output current: …"). Where the rating was written comes from the merged
  project's origins (`power_tree.rating_origins`, called by the CLI, the app and
  `Session.solve_tree`: a rail from the KiCad fields, or the project file's rail, its override,
  or a rail the fields leave incomplete); a caller that does not pass the merged project gets
  "(from KiCad field PI_IMAX or the project file)".
  - Not judged while its I out is not settled (I out includes the inputs of the regulators on
    the rails it feeds): its group or a group it feeds did not converge, KiPI's outer
    (ground-sense) iteration did not settle (`TreeResult.converged`, from KiPI's warning,
    `kipi/dc/pi/tree.py:707-711`), or KiPI marks a load there NOT CONVERGED (`{"item": "U1",
    "net": "+3V3", "limit": "current_limit_a", "reason": "+1V8, which it feeds, did not
    converge"}`).
  - A regulator out of regulation is still judged on its current, as KiPI does, unlike the
    voltage rule for loads (a load's drop is not judged behind a regulator that cannot hold its
    voltage, which the engine still holds at its set point).
  - A rated source on a rail the tree does not solve as a rail (excluded: KiPI makes its
    regulator a load on its input, `model.py:975-994`; or on a net used as a ground, left out
    of the setup by `pisetup._mapped`) is not judged: `{"item": "U2", "net": "+1V8", "limit":
    "current_limit_a", "reason": "not solved in the power tree"}`, as a drop limit there
    (`TreeResult.unsolved_ratings`, through `unsolved_limits`; exit 3).
  - A source without a rating is not judged and not listed. The rating stays out of the
    groups' `limits` (keyed by the net limits); a group judged only by a rating passes or fails
    by it. An assumption line (`power_tree.RATING_RULE`) states the rule whenever a rating
    exists.
  - VRM rows of `tree_summary`: `i_max_a` (the rating judged, `VrmReport.current_limit_a`; on
    every row when some VRM has one, absent otherwise), `exceeds_current_limit` (None: no
    rating, or not judged) and `current_limit_from` (None: no rating).

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
  inputs are among a rail group's problems as kind `"load"`, named `"U1 (input)"`. Each
  problem has `severity` (`"fail"`, `"attention"`: a spot above the current-density default,
  never a verdict, see [technical.md](technical.md#current-density-default); or `"info"`),
  `kind`, `title`, `detail`, `layer`, `x_mm`, `y_mm` (`null` when not finite) and its data;
  each group, and the document, count the attention items in `attention`; a `load` has `limit_pct`
  and `limit_from` (the `max_drop_pct` that judged it and the net it came from, by
  `net_limits`), a `return` has `limit_mv` and `limit_from`; `null` when no limit is set;
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
| Report: meta `drop_sign`, `drop_unit`, `drop_word`, `drop_max_mv`, layers' `max_drop_mv`, `worst.max_drop_mv`; `report.js` lut, readout, colour bar, worst card | The drop as the app shows it: `shown_drop_at`, labelled `shown_drop_unit`, named by `drop_word` (drop, rise, shift). On a ground below 0 V (a shift) that is its depth below 0 V, so the meta's `drop_sign` is +1 there. The same formula in JS (`dropSign(rail)`; reports written before the field read +1). |
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
could not judge.

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
ambient to the board's peak), unless `--no-maps`; the document (`--json` and the summary file)
lists them in `maps`, as the per-rail document does. `--rail` is refused, as with `--tree`.

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
silkscreen with their thickness and k, when the stackup has them; the board shape (outline, or the copper's bounding box
plus 1 mm); what is not modelled (heat sinks, enclosures, mounting, edge clamps; packages, no
junction temperature; transients); what is judged (above).

### Known limitations

- **Laminate names.** KiPI recognizes a laminate by a regular expression on its material name
  (materials.py:33-51); the prepreg patterns need a word boundary (materials.py:47-48), so a
  prepreg name with a prefix glued to it falls back to FR4 core (0.8 / 0.3 W/m·K in place of
  prepreg's 0.6 / 0.25). The assumption line names every laminate and what it was taken as.
  No override is exposed.
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

---

## Task 9: AC PDN impedance |Z|(f) → `kipi/ac` (`kipi/ac/solver/sweep.py`, `kipi/ac/model.py`)

Code: `coppernicus.engine.ac` (`ac_setup`, `solve_ac_rail`, `ac_rails`, `ac_size`,
`cost_estimate`, `ram_estimate`, `read_cap_file`, `write_cap_file`, `capacitor_candidates`),
`coppernicus.impedance` (verdicts, summary, text, CSV, PNG; no engine), `results.ImpedanceResult`
and `ProbeImpedance`, `coppernicus ac` in `cli.py`.

KiPI's entry point is `solve_ac(snap, setup, vrm_name, gnd, progress)` (sweep.py:390-511): its
third argument names a VRM. The rail is that VRM's output net plus the nets series links join
to it; the ground is `gnd` plus the nets links join to it (checks.py:53-69). The method is a
2.5D FEM of rail and ground copper with two faces per sheet (fem.py:1-37); the port matrix is
solved at the field frequencies, PCHIP-interpolated as jω·Z on log f and terminated by the
parts. VRMs carry no impedance (checks.py:6-7): the result is the **open-loop** impedance.
The board snapshot carries each dielectric's thickness and εr (core/model/board.py:36-91),
which the solve reads; where a stackup gives none, `ac.default_er` is used (fem.py:575-581).

### Probe → `kipi.ac.model.Probe` (new: `probes:`)

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `name` | `name` | — | required | Unique in the project. |
| `ref`, `pins` on `net` | `rail = PinRef(ref, net, pins)` | — | all pins | Resolved as in Task 6; a missing pin raises `EngineError` naming the probe. |
| `ground` (`{ref, net, pins}`) | `ground = PinRef(…)` | — | none | `ref` defaults to the probe's part. Solving without it raises `EngineError`. |
| `target_ohm`, `target_hz` | not passed | Ω, Hz | none | Flat target up to `target_hz` (the whole sweep if unset), with KiPI's `target_at` (target.py:76-95). `target_hz` without `target_ohm`: refused. |
| `target_points` | not passed | [[Hz, Ω]…] | `[]` | Log-log mask (`mask_at`) that applies **only between its first and last points** (coordinator's ruling): KiPI's `mask_at` holds it flat beyond them, which would judge against values the user never gave, so outside them nothing is judged. Together with `target_ohm` or `target_hz`: refused. |
| `ac_tolerance_v`, `step_a`, `slew_a_per_us` | not passed | V, A, A/µs | none | With `target_ohm` empty and all three set, the target is KiPI's `target_from_transient(v, i, slew·1e6)`: Z = V_ac/I_step up to 1/(π·t_rise), t_rise = I_step/slew (target.py:53-60). `step_a` and `slew_a_per_us` are kept for Task 11. |

KiPI gets no target, step or slew, so it logs no PASS/FAIL of its own; its "Targets apply
above…" and per-probe worst lines are dropped from the progress. Only the solved rail's
probes and models are passed, so a half-made probe or model on another rail does not block it.
Unknown keys in a probe or a capacitor model are refused, as in `ac:`.

### CapModel → `kipi.ac.model.CapModel` (new: `capacitors:`)

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `name` | `name` | — | required | Unique. |
| `capacitance_f`, `esl_h`, `esr_ohm` | same | F, H, Ω | 0 = not entered | KiPI refuses a model that has parts and a value ≤ 0 (checks.py:145-148): no default ESL or ESR. |
| `refs` | `refs` | — | `[]` | A ref in two models is refused at load. A ref off the board raises `EngineError`. KiPI uses a ref only if it matches `C<number>` with all its pads on rail or ground nets (parts.py:72, checks.py:92-114); a non-`C<n>` part between rail and ground is left out with a warning. |
| `rated_v` | same | V | none | Turns on KiPI's DC-bias derating at the source's set voltage (no DC result is passed): C/(1 + k(V/V_rated)^1.5) (model.py:96-102). Not for C0G. |
| `kept_at_rated_pct` | same | % | none → KiPI's 40 | 0 < x ≤ 100. The assumption says when KiPI's typical value is used. |
| `dielectric` | same | — | `""` | "C0G" (no bias), and the typical temperature curve when `copper_temp_c` ≠ 20 °C. |
| `override_board_fields` | — | — | `false` | Keep this model for its refs even where their KiCad fields give a complete model (below). Saved only when on. |
| — | `aging_pct_per_decade`, `mfg_tolerance_pct`, `spice` | — | off | Not kept; `read_cap_file` (a KiPI model file, cap_file.py) warns when it drops one. |

**From KiCad library fields** (`board_fields.field_caps`, merged by `board_fields.merge`). A part
`C<n>` with `PI_ESR`, `PI_ESL`, `PI_DIELECTRIC` and a capacitance, and with a class II dielectric
`PI_VRATED`, becomes one `CapModel` named "C21 (KiCad)" with `refs: [C21]`:

| Field | `CapModel` | Notes |
|---|---|---|
| `PI_C`, else the footprint Value | `capacitance_f` | The Value through KiPI's `parse_capacitance` (parts.py:52-69), read in `engine.kicad_fields` (`BoardFields.values`, `.farads`) only when the text gives its unit: KiPI reads a bare `0.1` as µF, a guessed unit, so it gives no capacitance here. |
| `PI_ESR`, `PI_ESL` | `esr_ohm`, `esl_h` | > 0 (`FieldError` otherwise: KiPI refuses 0). |
| `PI_DIELECTRIC` | `dielectric` | Normalised: `NP0`, `COG`, `NPO` → "C0G" (KiPI spares only that exact name from the derating, model.py:86, and keys its curves by it); class II `X5R`, `X6S`, `X7R`, `X7S`, `X7T`, `X8R`, `Y5V`, `Z5U`; `TANT`, `POLY`, `ALU` (no KiPI curve). |
| `PI_VRATED` | `rated_v` | Not passed for `TANT`, `POLY`, `ALU` (no DC-bias loss; KiPI would derate any rated model but C0G). |
| — | `kept_at_rated_pct` | None: KiPI's 40, stated by the DC-bias assumption line as for a project model. No field. |

Partial fields give no model (never a default): the merged project's `capacitor_gaps` (not
saved) maps the part to what it lacks, `ImpedanceResult.cap_gaps` carries those of the rail's
`missing_caps`, and the probe's not-judged reason, the AC "Capacitors:" assumption and the
noise stand-in "capacitors on the converter's rails without a model" append "(their KiCad fields
give no model: C45: PI_ESR missing)". Precedence: a complete fields model takes the ref from a
project model (a warning listing the differences, none when they agree), unless that model sets
`override_board_fields`; partial fields keep the project's model (a warning). Each ref stays in
one model and model names stay unique, as KiPI requires (checks.py:139-152).

### AcOptions → `ACSettings` and the rail's VRM (new: `ac:`; keys omitted at their default)

| Coppernicus (`ac:`) | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `f_start_hz`, `f_stop_hz`, `points_per_decade` | same | Hz, — | 1e3, 300e6, 250 | KiPI's (model.py:237-239); a test checks they match. The point counts must be finite whole numbers. |
| `field_points_per_decade` | same | — | 20 | 0 = every sweep point. Run time scales with it. |
| `max_edge_mm` | `ac.max_edge_mm` | mm | 1.0 | The AC mesh's own; `settings.max_edge_mm` stays DC's. |
| `loss_tangent`, `default_er` | same | — | 0.02, 4.3 | |
| `bandwidth_hz: {source: Hz}` | `Vrm.bandwidth_khz` (÷1e3) | Hz | none | Keyed by the source's name, else its ref. Always written onto the solved VRM: KiPI's `Vrm` defaults to 50 kHz (dc/pi/model.py:127). Without it: below. |
| `link_esl_h: {link: H}` | `DcLink.esl_h` | H | none | Keyed by the link's ref, else its name. Without it a link is a plain R in AC. |
| — | `grading` 0.3, `min_edge_mm` 0.03, near-rail grading, `coarsen_far`, `far_max_edge_mm` 5 | | KiPI's | Stated in the assumptions. |
| `settings.copper_temp_c` | `ac.temperature` "fixed" at that value when ≠ 20 °C, else "none" | °C | 20 | As in DC. |
| `settings.via_plating_um`, `copper_thickness_mm` | `settings.via_rules`; restacked board | | | Through `engine_settings()` and `with_copper_thickness`, as in DC. |

Unknown keys of `bandwidth_hz` or `link_esl_h` (no such source or link) are warnings, since
the KiCad footprint fields can add sources after the file is read. The ground is the source's
`ground_net`, else the first probe's. A net with no source of its own (behind a ferrite) is
solved with the rail whose links join it, and a note says so (`resolve_rail()` gives that rail
without solving); a net the project uses as a ground is refused by name. `probe_rails()` gives
each probe's rail (None when no rail with a source reaches its net) and `ac_rails()` the rails
with a probe after link joins. KiPI's `ACError`, `SolveError` (pads) and `MeshError` become
`EngineError`. `solve_ac_rail`'s `progress` callback is also called from KiPI's worker threads
(frequencies solved side by side), `fraction` and `cancelled` from the solving thread: a GUI
must only emit signals from them.

### No bandwidth given (coordinator's Q2; a deviation from the gate's placeholder)

KiPI refuses an AC solve without a VRM bandwidth (checks.py:124-128), but the bandwidth never
enters |Z|: `solve_ac` uses it only in `judged_from`, the transient and the log
(sweep.py:468-508). So without `ac.bandwidth_hz` the solve runs with **KiPI's bandwidth for a
new VRM, 50 kHz** (`DEFAULT_BANDWIDTH_KHZ`, dc/pi/model.py:57), not with `f_start_hz` as the
gate proposed (that would mark an empty band). KiPI's AC model has no regulator impedance, so
|Z| does not depend on it: no target is judged and no worst point is given;
`ImpedanceResult.bandwidth_hz` stays `None` (Task 11 must not feed a placeholder into a
transient) and `assumed_bandwidth_hz` holds 50 kHz; the assumption line names the source and
the value, and says |Z| does not depend on it; the plot hatches the band below it, labelled
"below the regulator's control-loop bandwidth (not given; 50 kHz assumed): not judged" (the
value is the placeholder's); the summary and the CSV have `bandwidth_hz` empty (null),
`assumed_bandwidth_hz` 50000 and `bandwidth_assumed` true.

### Results (`results.py`, at the end)

- `ProbeImpedance`: `name`, `ref`, `pins`, `net`, `ground_net`; `z_ohm` (complex Ω at
  `freq_hz`); `target_ohm`, `target_hz`, `target_points`; `target_curve_ohm` (KiPI's
  `target_at` above the bandwidth, given or assumed; NaN where none applies; a flat target
  ending below the sweep applies nowhere and a mask only between its first and last points,
  where KiPI would hold both flat);
  `target_note`, `judged_from_hz`, `fallback_note`; `worst_z_ohm`, `worst_hz`, `worst_ratio`;
  `passed`, `not_judged`; `step_a`, `slew_a_per_us`; `mag_ohm`, `phase_deg`, `has_target`, `limit`.
- `ImpedanceResult`: `rail`, `rail_nets`, `ground`, `source`, `bandwidth_hz`,
  `assumed_bandwidth_hz`, `freq_hz`, `field_freq_hz`, `probes`, `plane_capacitance_f`, `caps`,
  `missing_caps`, `links`, `cap_derated_f`, `load_line_ohm` (0: not exposed), `stats`,
  `warnings`, `assumptions`, `dielectric_assumed`.
- `stats`: `seconds`, `triangles`, `nodes`, `unknowns`, `ports`, `method`,
  `field_frequencies`, `sweep_points`, `field_seconds`, `setup_seconds`, `memory_peak_mb` (this
  process's high-water mark when the solve ended: `ru_maxrss`, or `PeakWorkingSetSize` on
  Windows; KiPI's mesher worker processes are not counted) and `memory_peak_raised` (whether
  this solve raised it; if not, its own peak was at most that).

### Verdicts (only against the user's target)

Per probe, worst point with KiPI's rule (sweep.py:67-83): where |Z|/target is highest where the
target applies; without a target, the highest |Z| above the bandwidth. No worst point without
a given bandwidth.
- **No target:** no verdict and no entry; the probe is ranked.
- **Not judged, whatever |Z|:** no bandwidth given; KiPI's `judged_from` fallback (the
  bandwidth at or above `target_hz`, or above the sweep: KiPI would judge the open-loop rise,
  target.py:110-125); capacitors between rail and ground without a model (`missing_caps`: they
  move the resonances); an assumed dielectric (a stackup not read from the board, or
  dielectrics without εr, named in the reason with the copper layers around them).
- **Otherwise** `fail` if |Z| > target anywhere it applies; else `not_judged` if [max(bandwidth,
  first mask point), `target_hz` or last mask point] is not inside the sweep (the reason says
  which of `ac.f_start_hz` or `ac.f_stop_hz` to move, and to what), if a mask ends below the
  bandwidth ("entirely below the bandwidth"), or if a target ends below the sweep (it applies
  nowhere); else `pass`.

A not-judged entry is `{"item": probe, "net", "limit": "target_ohm" | "target_points",
"reason"}` (plus `"rail"` at the top level); a failed entry has the same keys. A rail fails if a
probe fails; else it is `not_judged` if a probe with a target was not judged; `pass` if one was
judged; `not_judged` with no target at all (and no entry). A project combines its rails the
same way. A probe no solved rail reaches is not solved: with a target it is a project-level
not-judged entry (`"rail": ""`), without one a project-level warning; with no probe reached at
all, `coppernicus ac` stops with an error naming them (`--rail` solves only the rails asked for,
each resolved first and solved once, and leaves the other probes as chosen). `coppernicus ac` exits as `solve` does (`_exit_for`):
0, 1 when a target is exceeded, 3 when a target could not be judged, 2 on an error. (The gate
said 0, 1 or 2: it predates exit code 3.)

### Assumption lines

In order: the AC model; the open loop (with the bandwidth, or naming the source, "no control
loop bandwidth is set for it (ac.bandwidth_hz)", the 50 kHz marked for the plot and that |Z|
does not depend on it); KiPI's
`dielectric_summary` (plus the `ac.default_er` sentence where εr was defaulted; a what-if
restack reads "[rebuilt for the copper_thickness_mm what-if]"); the loss tangent; the sweep and
field frequencies; the AC mesh (KiPI's grading defaults); the capacitors (modelled, and those
without a model by ref); DC bias, when a modelled part has a rating; one line per link in the
model (R, and + jω·L when `link_esl_h` gives it); net ties as shorts; the via plating and the
what-if thickness, as in DC; the copper temperature when ≠ 20 °C; one line per target
("entered", a mask's "entered; applies only between its first and last points", or "computed
by KiPI … from ac_tolerance_v, step_a, slew_a_per_us").

### Outputs

- `impedance.write_csv` → `<project>_<rail>_impedance.csv`: `freq_hz`, then per probe
  `<p>_re_ohm`, `<p>_im_ohm`, `<p>_mag_ohm`, `<p>_target_ohm` (empty where no target applies),
  then on every row `bandwidth_hz` (as given; empty when not), `assumed_bandwidth_hz` (the
  placeholder; empty when given) and `bandwidth_assumed`.
- `impedance.write_png` → `<project>_<rail>_impedance.png` (`draw_impedance`, the GUI's
  drawing): log-log |Z| per probe, each target where it applies, the band below the bandwidth
  (grey "open loop: the source's control loop holds the rail here", or hatched "below the
  regulator's control-loop bandwidth (not given; 50 kHz assumed): not judged"), each worst
  point, the verdict in the title (plus
  "bandwidth not given (50 kHz assumed)", visible even when the sweep starts above it).
- `<project>_ac_summary.json` (`allow_nan=False`): top level `kind: "ac_impedance"`, `verdict`,
  `verdict_note`, `failed`, `not_judged`, `warnings`, `rails[]`, `summary_file`. Per rail: `rail`,
  `rail_nets`, `ground`, `source`, `bandwidth_hz`, `bandwidth_assumed`, `assumed_bandwidth_hz`,
  `f_start_hz`,
  `f_stop_hz`, `sweep_points`, `field_points`, `plane_capacitance_f`, `caps_modelled`,
  `caps_missing` (lists of refs), `links`, `verdict`, `not_judged`, `csv_file`, `png_file`,
  `assumptions`, `warnings`, `stats{seconds, triangles, unknowns, ports, method,
  memory_peak_mb, memory_peak_raised}`, `probes[]`. Per probe: `name`, `net`, `ground_net`,
  `z_min_ohm`, `z_max_ohm`, `worst_z_ohm`, `worst_hz`, `worst_ratio`, `target_ohm`,
  `target_hz`, `target_points`, `target_note`, `judged_from_hz`, `passed`, `step_a`,
  `slew_a_per_us`. Beyond the gate's list: the top-level `warnings`, `bandwidth_assumed`,
  `assumed_bandwidth_hz`, `probes`, the two memory keys. A rail's `bandwidth_hz` is the
  bandwidth as given, null when not; `assumed_bandwidth_hz` is then the placeholder (50000),
  and null when the bandwidth is given.
- `coppernicus ac PROJECT [--rail NET ...] [--out DIR] [--json] [-v]`: CSV and PNG per rail and
  the summary into `--out` (default `results/` next to the project). The KiCad footprint fields
  apply, as in the other solves.

### Cost and memory before a solve (coordinator's Q7)

- `ac_size(board, project, rail)` meshes the rail and its ground as the solve does (KiPI's
  `mesh_rail_and_ground`, seconds on a large board) and counts triangles, nodes, ports and
  frequencies; it checks the setup as the solve does.
- `cost_estimate(triangles, ports, field_frequencies, last=None)`: triangles × ports and the
  field frequencies; with the `stats` of an earlier AC solve, a time: its field solves
  (`field_seconds`) scaled with KiPI's work per frequency, n^1.5 + 5·n·rhs (core/eta.py:37-41;
  KiPI's right-hand sides are n − n_free + ports, field.py:44, taken as the ports since n_free
  is known only once the model is built), the rest (mesh and model) in proportion to the
  triangles. No earlier solve, no time.
- `ram_estimate(unknowns, total_memory_bytes(), free_memory_bytes())`: the memory of the mesh,
  the model and **one factorization**, measured on `ac_plane_board` meshed at 2.0, 1.4, 1.0
  and 0.7 mm, two frequencies one after the other, each size in its own process (19 196 to
  147 110 unknowns: 254 MB to 2.75 GB above an 85 MB process), fitted as 2466.6 · n^1.1706
  bytes (within 1.2 % of each point), about one unknown per triangle (0.98 to 1.0 there).
  Measured on synthetic plane pairs only: other boards may need more or less. `risk` when one
  factorization exceeds the machine's memory. The run's peak is higher: after
  the first frequency KiPI solves others side by side, each with its own factorization, as many
  as fit in half the memory free (field.py:68-82; `threads_bytes`, and the text says how much);
  that part is sized to the memory, so one factorization is the risk. Measured here: 1.5 to
  1.6 GB peak for the 20 k-unknown boards, where one factorization is 0.25 GB.
- A run on this machine (M5, SuperLU): `coppernicus ac examples/pdn/pdn.coppernicus.yml`
  takes 8.5 s (solve 7.5 s: 20 587 triangles, 2 ports, 111 field frequencies), 1.6 GB peak.

### Validation (KiPI's `tests/accuracy/test_ac_accuracy.py`, same tolerances)

`engine.synthetic` copies KiPI's `ac_plane`, `ac_decap` and `ac_ferrite`
(examples/validation/make_boards.py; `four_layer_stackup`, `AcPlane`, `AcDecap`, `AcFerrite`,
`ac_*_board()`, `ac_*_project()`). Here:

| Board | Asserted | Measured |
|---|---|---|
| `ac_plane` (19 596 triangles, 121 field frequencies, 6.9 s) | C_plane = ε₀·4.5·(100² − 1.2²)/0.2, rel 1e-3; Z at 1 kHz, 100 kHz, 1 MHz = 1/(jωC(1 − j·tanδ)), rel 5e-3; largest peak in [0.5, 1.2]·f10, f10 = c/(2·100·√4.5), rel 1.5e-2 | C 1.99191 nF (hand 1.99191 nF); peak 707.95 MHz vs 706.62 MHz (+0.19 %) |
| `ac_decap` (20 587 triangles, 4.4 s) | C_plane with 2 cut-outs, rel 1e-3; Z at 1 and 10 kHz; mounted L flat within 3 %, ESL < L < ESL + 2 nH; min\|Z\| at 1/(2π√(LC)), rel 3e-2; ESR ≤ min\|Z\| ≤ 1.15·ESR | C 0.49747 nF (hand 0.49747 nF); min\|Z\| 22.01 mΩ at 1.479 MHz, 1.10 × ESR |
| `ac_ferrite` (17 706 triangles, 3.6 s) | \|Z\| ÷ lumped model within 1 % away from f0 up to 100 MHz, 0.9–1 above; one peak in [f0/3, 3f0] at f0 rel 1.5e-2, height rel 0.05 | peak 23.862 MHz vs f0 23.848 MHz (+0.06 %) |

---

## Task 10: DC-DC ripple and coupled noise → `kipi/noise` (`kipi/noise/solver.py`, `kipi/noise/model.py`)

Code: `coppernicus.engine.noise` (`noise_setup`, `solve_aggressor`, `seed_phases`, `guess_ends`,
`mesh_estimate`, `noise_cost_estimate`, `noise_ram_estimate`, `kipi_noise_choices`),
`coppernicus.coupled_noise` (verdicts, summary, text, CSV, PNG; no engine), `results.HotLoop`,
`RippleResult`, `VictimResult`, `FieldMap`, `NearField`, `NoiseResult`, `coppernicus noise` in
`cli.py`, `engine.synthetic.coupled_noise_board()` / `coupled_noise_project()`, `examples/noise`.

KiPI's entry point is `solve_noise(snap, setup, aggressor, progress, dc_result)`
(solver.py:223-326); `aggressor` names a `setup.aggressors` entry. Method (solver.py:1-8,
waveforms.py:1-22, extract/common.py:1-25, victims.py:1-7): the converter's two rails and its
ground are meshed and each phase's hot loop traced through the copper (L; the switch node's
C = Coss + copper; f_ring; ζ at least `min_ring_zeta`); the waveforms are exact Fourier series of
piecewise-linear periods, harmonics to max(10/(π·t_min), 3·f_ring) ≤ 3 GHz (waveforms.py:90-101);
AC mode's 2.5D field model of the copper (`field_per_decade` field solves between fsw and the
last harmonic, interpolated), terminated by the capacitors, splits each phase's pulsed and
inductor currents and gives the ripple at phase 1's device pins and at probes and the ground
difference under each victim's ends; the near field 5 mm up and a radiated estimate at 3 m
follow; each victim is cut into 2D cross-sections (`segment_mm`, `window_mm`), coupled
capacitively (switch nodes), inductively (capacitor and inductor loops) and through the shared
ground (the copper's resistance only), each mechanism solved alone and added as phasors, held
high and held low. **`dc_result` is always `None`** (brief Q10): KiPI's own tree result carries
capacitor DC volts and diode-link currents that Coppernicus's `TreeResult` does not; the
capacitors are derated at the rails' nominal voltages.

### Setup: per-object mappers, not `ac_setup` per rail (brief Q7)

T9's `ac_setup(board, project, rail)` maps one rail; an aggressor spans two (its input and its
output) and names its own probes. `noise_setup` therefore builds KiPI's setup from
`to_pisetup(board, project)` plus T9's per-object mappers: `_kipi_probe` for the probes the
aggressor names (`ripple_probes`; an unknown name raises `EngineError`, where KiPI only warns),
and one `_kipi_caps` call over KiPI's own net sets of both rails (`rail_nets` of the VRM, the
`net_groups` of its input net, `ground_nets`; extract/common.py:138-143), so only models with a
part between those rails and the ground are passed. The `ac:` sweep keys (mesh, `default_er`,
`loss_tangent`), each link's series inductance and the copper temperature are copied as T9's
`_context` does. `ac_setup` per rail would refuse an input rail with no source of its own in
the project (an aggressor with `vin_v` entered needs none) and resolve probes by rail; a test
covers that case. T9's files are unchanged.

### Aggressor → `kipi.noise.model.Aggressor` (new: `aggressors:`)

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `name` | `name` | — | required | Unique. |
| `vrm` | `vrm` | — | `""` | A source by its engine name (`terminal_names`: the label, or "label (net)" when shared). Not a source of the project: `EngineError` listing the switchers. Not an `"smps"`: KiPI's message, as `EngineError`. |
| `topology` | same | — | `"auto"` | auto / buck / boost; else `ProjectError` (KiPI silently takes auto, model.py:316). |
| `fsw_hz` | same | Hz | **required**, else the regulator's `Source.fsw_hz` | Coordinator's change to Q1: without its own value the aggressor takes its regulator's (KiCad field `PI_FSW`, or `fsw_hz` in the project file), with the assumption line "Switching frequency {f} from the source {s} (KiCad field PI_FSW \| the project file): the aggressor gives no fsw_hz of its own, which would win."; with neither, `EngineError` naming it. KiPI's 500 kHz (model.py:74) is never used. |
| `rise_s`, `fall_s` | same | s | **required** | 0-100 % edges; missing at solve: `EngineError` naming them (KiPI's 2 ns never used). Edges as long as a period: KiPI's check, as `EngineError`. |
| `vin_v`, `duty`, `load_a` | same | V, —, A | `None` | `None`: the voltage of the source feeding the input rail (checks.py:62-72), from the voltages and the entered losses (:114-125), the output rail's loads (:75-81). `duty` in (0, 1). |
| `ripple_probes` | same | — | `[]` | T9 probe names. A probe off the aggressor's rails: KiPI's warning, no ripple there. |
| `phases` | same | — | `[]` | Required to solve (`EngineError` "add its phases (Fill from board)"). Filled only by an explicit action calling `seed_phases` (KiPI's `seed_phases`, checks.py:199-254); a solve never calls it (a test checks). |
| `min_ring_zeta`, `soft_edge_zeta` | same | — | 0.05, 0.5 | KiPI's floors (model.py:87, :91), stated in the assumptions. |

The file may hold a draft (no `fsw_hz`, edges or phases): it loads and is refused at solve.

### Phase → `AggressorPhase`

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `inductor`, `inductance_h`, `dcr_ohm` | same | —, H, Ω | `""`, 0, 0 | Inductance > 0 to solve (KiPI's check). |
| `switch_net` | same | — | `""` | Empty on a phase: no capacitive coupling from that phase (a stand-in for it, below). |
| `high_side`, `low_side` `{ref, net, pins}` | `PinRef` | — | none | `ref` required (the regulator's part may be the inductor); pins resolved as in Task 6, a missing pin names the phase. |
| `input_caps`, `output_caps` | same | refs | `[]` | Off the board: KiPI's error, as `EngineError`. |
| `coss_f` | same | F | `None` → KiPI's 200 pF | Stand-in when empty (parasitics.py:670-674). |
| `qrr_nc` | `qrr_c` (× 1e-9) | **nC** | `None` → KiPI's 5 nC | `_c` means °C in Coppernicus. 0 is entered (a Schottky or GaN low side) and kept on save (`is not None`). |
| `rds_on_hs_ohm`, `rds_on_ls_ohm`, `package_l_h` | same | Ω, Ω, H | `None` → 10 mΩ (damping only), 0 | Entered Rds also raise a buck's duty (checks.py:114-120). An assumption line, not a stand-in. |
| `inductor_axis`, `inductor_shielded` | same | — | `"vertical"`, `true` | Unknown axis: `ProjectError` (KiPI silently vertical, model.py:303-304). |

### Victim → `kipi.noise.model.Victim` (new: `victims:`)

| Coppernicus | KiPI | Unit | Default | Notes |
|---|---|---|---|---|
| `net`, `name` | same | — | required, `""` = the net | Unique names. A net off the board: `EngineError`. |
| `preset` | same | — | **required in the file** | One of cmos, open_drain, series, parallel, lvds, analog, hiz, or `custom`. A named preset is applied with `apply_preset` (KiPI's `Victim(preset=…)` alone keeps the cmos terminations; a test checks open_drain's 4.7 kΩ / 20 Ω). |
| `driver`, `receiver` `{ref, pins}` | `PinRef` on the victim's net | — | none | Empty: KiPI guesses them (the net's two pads farthest apart, victims.py:29-45): a stand-in. |
| `driver_ohm`, `receiver_ohm`, `receiver_pf`, `termination_ohm`, `driver_low_ohm` | same | Ω, Ω, pF, Ω, Ω | the preset's | Written only for `custom`, which needs the first three; with a named preset: `ProjectError`. |
| `logic_v` | same | V | `None` | `None`: KiPI gets its 3.3 V only to build the held-high and held-low states; neither the noise nor a margin from `noise_margin_v` or a preset depends on it. |
| `noise_margin_v` | same | V | `None` | > 0. |

### NoiseOptions (`noise:`) → `NoiseSettings` (keys omitted at KiPI's default)

`f_max_hz` (`None` = automatic), `max_harmonics` 4000, `dead_time_ns` 20, `segment_mm` 0.5,
`window_mm` 3.0, `board_model` true, `field_per_decade` 10 (model.py:244-264; a test checks
they match). Not mapped: KiPI's GUI and report state, `victims` (always "all"), the net scan
(`scan*`, brief Q5) and the picking and highlight of segments and loops (Q6).

`project.py` cannot import KiPI, so it copies the preset names, topologies, inductor axes and
these defaults; `engine.noise.kipi_noise_choices()` reads KiPI's and a test compares them.

### Results (`results.py`, at the end; no KiPI object kept)

- `HotLoop`: `phase`, `loop_l_h`, `package_l_h`, `c_sw_f`, `coss_f`, `coss_entered`, `qrr_nc`,
  `qrr_entered`, `c_copper_f`, `r_loop_ohm`, `f_ring_hz` (None: no ringing), `zeta`, `zeta_loop`,
  `traced`, `image_plane`.
- `RippleResult` per ripple probe, then `output` and `input` at phase 1's device pins (buck: VIN
  between the high-side and low-side pins, VOUT at the inductor's output pad; boost the other
  way; extract/common.py:298-301), or of the capacitor bank without the board model: `name`,
  `where`, `net`, `ground_net`, `ref`, `pins`; **KiPI tolerance's four values exactly**
  (`kipi/tolerance/gather.py` `ripple_peaks`: below and above the mean, full band and through a
  single pole at 20 MHz), `below_v`, `above_v`, `below_bw_v`, `above_bw_v` (a test compares them
  with `ripple_peaks` of the same solve); `pp_v`, `pp_bw_v`, `rms_v`; `t_s`, `v`, `v_bw` (one
  period). Task 11 pairs a load by `ref`, `pins`, `net`.
- `VictimResult`: `name`, `net`, `driver`, `receiver` (KiPI's labels), `ends_entered`, `preset`,
  `margin_v` (None: no margin set), `margin_from`, `limit`, `peak_v`, `peak_by_v` {capacitive,
  inductive, common}, `dominant`, `ratio`, `states` (name, level, threshold, driver Ω,
  excursion, margin; threshold and margin None without a margin), `length_mm`, `coupled_mm`,
  `cm_f`, `m_h`, `splits`, `receiver_v` (one period, driven high), `stand_ins`, `passed`,
  `not_judged`, `warnings`.
- `NoiseResult`: the operating point (`topology`, `vin_v`, `vout_v`, `iout_a`, `iin_a`, `duty`,
  `efficiency`, `phases`, `operating_notes`), `fsw_hz`, `rise_s`, `fall_s`, `f_max_hz`,
  `harmonics`, `harmonics_capped`, `inductor_ripple_a`, `hot_loops`, `t_s`, `switch_node_v`,
  `inductor_a`, `edges` (per phase), `ripple`, `victims`, `board` {used, note, unknowns,
  field_solves, ports, caps_modelled, caps_missing}, `emi` (`NearField`: the H and E `FieldMap`s,
  each point's strongest harmonic, rows from y0; hotspots; the radiated estimate nearest the
  CISPR 32 B limit), `stand_ins`, `partial`, `verdict`, `verdict_note`, `failed`, `not_judged`,
  `assumptions`, `warnings`, `stats` {seconds, harmonics, triangles, nodes, unknowns, ports,
  field_solves, field_seconds (read from KiPI's progress lines), memory_peak_mb,
  memory_peak_raised}.
- `solve_aggressor(…, partial=)` hands over KiPI's partial results converted
  (`partial=True`, no verdict, no stand-ins, no assumptions): the waveforms first
  (solver.py:283). `NoiseError`, `ACError`, `SolveError`
  and `MeshError` become `EngineError`; KiPI's `Canceled` becomes `Cancelled`. `progress` (and
  `cancelled`, checked with each message) is also called from KiPI's worker threads while the
  board model solves frequencies side by side (kipi/ac/solver/field.py:85-126); `fraction` and
  `partial` from the solving thread: a GUI must only emit signals from them.

### Verdicts (brief Q2-Q4; only the user's margins)

**Margin, in order:** `noise_margin_v`; else the named preset's own (lvds 0.1 V, analog 0.01 V;
model.py:163, :165); else the JESD8 levels of an entered `logic_v` on KiPI's table
(model.py:96-103); else none: ranked, no verdict, no entry. The value is KiPI's own
`Victim.margin()`, the smaller of the held-high and held-low state margins (0.8 V for 3.3 V
logic), never `logic_margin()` (0.4 V there). KiPI's 35/65 % rule off its table is never used:
an off-table `logic_v` alone sets no margin and is **not judged, never a silent pass**
(coordinator's I1): entry `{item, net, limit: "logic_v margin (V)", reason: "logic_v {lv} V is
not in KiPI's JESD8 table: enter noise_margin_v"}`, exit 3; its assumption line says so and
never "no noise margin set".

**Not judged, whatever the peak** (the reason ends "…; peak {p} mV = {r} % of the margin"),
while any KiPI stand-in is in play:
- for every victim of the aggressor (`NoiseResult.stand_ins`): "Coss not entered on phase {i}:
  KiPI's 200 pF sets the ringing"; "Qrr not entered on phase {i}: KiPI's 5 nC sets the hard edge
  (enter 0 for a Schottky or GaN low side)"; "capacitors on the converter's rails without a
  model: {refs}" (KiPI's `rail_caps` with the board model, else the phases' capacitors); "εr
  assumed: {why}" (T9's rule); "capacitive coupling of phase {i} left out: no switch net" (any
  phase without one, gate §3); "harmonics stop at {f} MHz (noise.max_harmonics {k})"; "harmonics
  stop at {f} MHz (noise.f_max_hz), below the edges' {auto} MHz" (a user's `f_max_hz` below
  KiPI's automatic reach, `harmonic_count` with no `f_max`); "harmonics stop at KiPI's 3 GHz
  ceiling, below the edges' {raw} MHz" (`F_MAX_CAP_HZ` clipping max(10/(π·t_min), 3·f_ring),
  waveforms.py:33, :96-97); "shared-ground coupling left out: board model off
  (noise.board_model)" or "… board model left out ({KiPI's reason})";
- **what KiPI drew, not what was listed** (coordinator's C1): KiPI finds a capacitor's loop from
  its pads on the exact input or output net and the low-side ground (parasitics.py:194-196,
  :393-396, :600-639). Each listed capacitor with no drawn loop (no `"{ref} (phase {i})"` in
  KiPI's `loop_paths`) is "{ref} on phase {i}: KiPI draws no current loop for it ({why}), so its
  loop's coupling is left out", the reason read from the board: "it has no pad on {net | gnd}",
  "the switching device's high-side or low-side pins are not set", and for the other side's loop,
  which runs through the inductor and the switch node, "its loop runs through the switch node,
  and the phase has no switch net" or "… through the inductor, whose pads are not on {sw} and
  {rail}" (else "it could be neither traced nor drawn"); a phase whose hot-loop side
  has no drawn loop at all is "phase {i}: no {input | output} capacitor loop is drawn, so the hot
  loop is {their ESL plus the package | the package} only". This covers swapped input and output
  capacitors (measured before the fix: hot loop 0.6 nH, SIG_LOOP 1.5 mV, judged PASS), an empty
  list, a capacitor on a net linked to the rail, and one grounded on a tied ground;
- per victim: "driver and receiver not both entered: KiPI guessed {d} and {r}"; "inductive
  coupling left out: no capacitor loops (set the switching device's pins and the capacitors)";
  "nothing coupled: {net} has no tracks"; "driver or receiver pins not on the board";
  "shared-ground coupling left out: its driver or receiver has no pad on {gnd}"; "held {high |
  low} it sits at {level} V, beyond its {threshold} V threshold: no margin left".

Stand-ins are taken from structured data (`coss_default`, `qrr_default`, `rail_caps`,
`harmonic_count`, `loop_paths`, an empty `board_note`, T9's `_dielectric`), never from warning
text, except KiPI's own "No capacitor loops" flag on a victim. Rds_on, the ζ floors, the dead
time, the package loop and the inductor's axis and shield stay assumption lines.

A capacitor between the rails and the ground (KiPI's `rail_caps`) that no phase lists is a
**warning**, not a stand-in (coordinator's ruling: the user chose which capacitors form the
loop): "{refs} is | are on the rails but in no phase's list: the hot loop does not include it |
them".

**Otherwise** `fail` if `peak_v > margin_v` (KiPI's rule, result.py:76-82: the largest receiver
|v| over a period, held high or low), else `pass`. Entry: `{"item": victim, "net", "limit":
"noise_margin_v (V)" | "preset {p} margin (V)" | "logic_v JESD8 margin (V)" | "logic_v margin
(V)" (off the table), "reason"}` (plus
`"aggressor"` at the top level). An aggressor fails if a victim fails; else it is `not_judged`
if a victim with a margin was not judged; `pass` if one was judged; `not_judged` with no margin
at all (no entry: "No noise margins set: nothing is judged; the victims are ranked"). The CLI
combines its aggressors the same way and exits as `solve` (`_exit_for`): 0, 1 a margin exceeded,
3 a margin not judged, 2 an error. **Never judged** (Q4): the ripple (Task 11 stacks it per
load) and the radiated estimate (CISPR 32 B for scale).

### Assumption lines

In order: the coupled-noise method with the harmonics and their reach; the operating point
(KiPI's notes) and the source's efficiency; where `fsw_hz` came from when it is the
regulator's; per phase the hot loop (L, traced or straight, switch-node C with Coss entered or
KiPI's, copper C, ringing and ζ against `min_ring_zeta` and the loop's own) and the edges (Qrr
entered or KiPI's, `soft_edge_zeta`, dead time, on-resistance entered or KiPI's 10 mΩ, package
loop); the capacitors (modelled, and those without a model at their board value with KiPI's
5 mΩ, 0.5 nH, 10 µF fallback); the board model (rails, ground, unknowns, field solves) or why it
is off; DC bias when a modelled part has a rating; KiPI's dielectric summary with its 3.5
solder-mask εr; the victims' cross-sections and mechanisms; per victim its type, terminations
and ends (entered or guessed), and its margin and source, or that its off-table `logic_v` gives
no margin ("not judged"), or "no noise margin set: ranked, not judged"; per inductor its shield and axis; the near field and radiated estimate (an indicator,
never judged); the via plating and what-if thickness, as in DC; the copper temperature when
≠ 20 °C; per ripple where it is measured, the 20 MHz single pole and "never judged here".

### Outputs

- `coupled_noise.write_csv` → `<project>_<aggressor>_noise.csv`, one period: `t_s`, per phase
  `phase<i>_switch_node_v`, `phase<i>_inductor_a`, per ripple `ripple_<name>_v` and
  `ripple_<name>_20mhz_v` (about the mean), per victim `victim_<name>_v`.
- `write_png` → `<project>_<aggressor>_noise.png` (`draw_waveforms`: two periods of the switch
  node, the inductor current, the ripple at the probes, else at the output pins, 20 MHz solid
  and full band thin, each victim's receiver voltage with its margin); `write_field_png` →
  `…_noise_h_field.png` and `…_noise_e_field.png` (`draw_field_map`: log scale over the board
  outline, KiCad frame with y down, the hotspots numbered; a test checks the map's maximum lies
  at KiPI's strongest hotspot).
- `<project>_noise_summary.json` (`allow_nan=False`; units in every key): `kind: "noise"`,
  `verdict`, `verdict_note`, `failed`, `not_judged`, `warnings`, `aggressors[]`, `summary_file`.
  Per aggressor: the operating point and switching keys above, `hot_loops[]`, `ripple[]` (`name`,
  `where`, `net`, `ground_net`, `ref`, `pins`, `pp_mv`, `pp_20mhz_mv`, `below_mv`, `above_mv`,
  `below_20mhz_mv`, `above_20mhz_mv`, `rms_mv`), `victims[]` (`name`, `net`, `driver`,
  `receiver`, `ends_entered`, `preset`, `margin_mv`, `margin_from`, `limit`, `peak_mv`,
  `peak_by_mv`, `dominant`, `ratio`, `states[]` {name, level_v, threshold_v, driver_ohm,
  excursion_mv, margin_mv}, `length_mm`, `coupled_mm`, `cm_f`, `m_h`, `splits`, `stand_ins`,
  `passed`, `not_judged`, `warnings`), `board{}`, `emi{}` (no maps: `height_mm`, `distance_m`,
  `hotspots`, `radiated_dbuv_m`, `radiated_hz`, `cispr32_b_dbuv_m`, `radiated_source`, `notes`),
  `stand_ins`, `verdict`, `verdict_note`, `failed`, `not_judged`, `csv_file`, `png_files`,
  `assumptions`, `warnings`, `stats`. Infinite or NaN values (an infinite ratio, a radiated
  estimate below the band) are null.
- `coppernicus noise PROJECT [--aggressor NAME ...] [--out DIR] [--no-board-fields] [--json]
  [--html [PATH]] [-v]`: every aggressor (or each `--aggressor`, once), the files above into
  `--out` (default `results/` next to the project). The KiCad footprint fields merge as in
  `solve --tree` and `ac` (brief Q9); the rails taken from them word where a regulator's
  `PI_FSW` came from. Before each aggressor it prints "Noise of <aggressor>: <estimate>" (on
  stderr with `--json`; `service.session.noise_estimate`, below) and each aggressor's entry
  carries it as `estimate`; `--html` writes `report.write_noise_report`'s offline page. The
  MCP `solve_noise` (`Session.solve_noise`) returns the same document, refused over this
  machine's memory unless confirmed; CI runs it with the Action's `noise` input.

### Cost and memory before a solve (brief Q8: warn and confirm, never cancel)

- `mesh_estimate(board, project, name)` checks the setup as the solve does, then builds KiPI's
  `board_ports` and meshes the rails and ground with `mesh_rail_and_ground` (seconds on a large
  board; no field solve) and counts triangles, nodes, ports and the field solves, the latter
  from the edges or `noise.f_max_hz` (the ringing may add a few: KiPI takes 3·f_ring when
  higher). Without the board model it meshes nothing. A test checks its triangles, ports and
  field solves equal the solve's. Where the board model cannot be built (a phase without its
  pins or an inductor pad on the rail) the solve leaves it out with a warning, and so does the
  estimate (`board_model` False, `note` saying why).
- `noise_cost_estimate(size, last)`: T9's `cost_estimate` with the field solves as its
  frequencies, scaled from an earlier noise solve's `stats`.
- `noise_ram_estimate(unknowns, total)`: the whole solve's peak, an **upper bound** measured on
  synthetic boards (gate T10 §9: 0.2 GB + 26 to 36 kB per unknown from 9 691 to 175 084
  unknowns; 36 kB is used), unknowns ≈ 0.95 × triangles (0.80 to 0.85 on those boards; 0.95 is a
  chosen upper figure). T9's fit (one factorization, ~3.4 GB at 175 k unknowns) is not reused:
  that point measured 5.05 GB for the whole noise solve. Fill-in depends on the copper: other
  boards may need less. `risk` above the machine's memory.
- A run on this machine (M-series): `coppernicus noise
  examples/noise/noise.coppernicus.yml` takes 5.2 s wall (3.8 s in the solve: 15 534 triangles,
  12 101 unknowns, 8 ports, 34 field solves), 680 MB peak RSS. Larger meshes start KiPI's
  mesher worker processes: scripts that solve need `if __name__ == "__main__":`.

### Validation (KiPI's `tests/accuracy/test_noise_accuracy.py` and `tests/noise`, same tolerances)

`engine.synthetic.coupled_noise_board()` copies KiPI's coupled-noise board
(examples/validation/make_boards.py:454-525; pads, vias, tracks, zones and stackup equal KiPI's
checked-in snapshot); `coupled_noise_project()` its setup (J1 12 V on VIN; U1 a switcher, output
L1.2, input U1.1, ground U1.2, efficiency 0.9 = KiPI's `Vrm` default its setup keeps; U2 5 A;
models 10u and 47u; aggressor U1 1 MHz, 2 ns, 1 µH, Coss 300 pF; three cmos victims with ends).

| Asserted (KiPI's tolerances) | Measured here | KiPI's own solve |
|---|---|---|
| SIG_SW capacitive > 10 × SIG_LOOP capacitive + 1 mV; 0.5 < SIG_SW capacitive / inductive < 2; SIG_LOOP dominant inductive with all three mechanisms | 17.23 mV > 0 + 1 mV; 17.23 / 19.98 = 0.86; inductive | 17.24 / 19.98 mV |
| SIG_BURIED peak < 0.05 × SIG_LOOP peak | 1.425 < 32.8 mV | 1.425 mV |
| peaks SIG_LOOP ≥ SIG_SW ≥ SIG_BURIED | 655.4 ≥ 32.66 ≥ 1.425 mV | 655.4 ≥ 32.67 ≥ 1.425 mV |
| hot loop 0.1-3 nH traced over In1.Cu; plate < C_copper < 1.6 × plate (ε₀·4.5·16 mm²/0.2 mm); C_sw = 300 pF + copper; f_ring = 1/(2π√(LC)); 0 < ζ < 1 | 2.07 nH, 3.87 pF copper, 303.9 pF, 200.5 MHz, ζ 0.05 (the floor; the loop alone 0.0041) | same |
| duty 1.8/12; inductor mean 5 A; f_max ≥ 10/(π·2 ns); switch node > 12 V | 0.15; 5 A; 1.592 GHz (1592 harmonics) | same |
| the probe's four values = `ripple_peaks`; 0 < LOAD p-p < 2 × output p-p; input > LOAD | LOAD 7.884 / 10.57 mV, 20 MHz 4.727 / 7.006 mV; output 1.878 V p-p (269 mV at 20 MHz), input 15.34 V (1.873 V) | — |

SIG_SW reads 32.66 mV from the board built in memory and 32.67 mV from the saved example
snapshot (KiPI's own solve: 32.67 mV): the last digit moves with the snapshot's float round trip
and with the mesh refined at the probe's pads (SIG_SW's common part 1.692 mV with the probe
LOAD, 1.697 mV without; KiPI's test_probe_ripple.py:48 allows 2 %). Every tolerance holds.

### Known limitations

- **KiPI's false pass of a line already past its threshold** (owner's call: recorded here, not
  reported upstream). KiPI's `LogicState.margin_v` is |level − threshold| (model.py:131-133), so
  a state whose idle level is already beyond its threshold keeps a positive margin, and KiPI
  only warns (victims.py:155-157): "hiz" on 3.3 V logic sits at 1.65 V held high, under VIH
  2.0 V, with a 0.35 V margin, and passes any small noise; "parallel" at 2.5 V and 5 V likewise.
  Coppernicus never judges such a line (a stand-in, above).
- `dc_result` is `None`: capacitors are derated at the rails' nominal voltages, diode links take
  KiPI's estimate from the loads beyond them.
- KiPI's net scan, the picking of ends and phase pins, and the highlight of segments and loops
  are not offered (brief Q5, Q6): ends are typed, guessed by "Guess ends", phases filled by "Fill
  from board".
- The field-solve count before a solve comes from the edges; a ringing above a third of the
  edges' reach adds a few. The RAM bound comes from synthetic boards.
- The board model is quasi-static: approximate above about 1 GHz (KiPI's warning says so).

---

## Task 11: Tolerance stack-up → `kipi/tolerance` (`kipi/tolerance/evaluate.py`, `kipi/tolerance/gather.py`)

Code: `coppernicus.engine.tolerance` (`allowed`, `load_limits`, `accuracy_sides`, `stack`,
`load_probes`, `ac_tolerance_mismatch`, `kipi_reference_stack`), `coppernicus.tolerance`
(`compose`, `misspelt_keys`, `tolerance_summary`, `tolerance_text`, `write_csv`; it imports the
engine only inside the functions that need it, so importing it does not load KiPI),
`results.TolerancePart`, `ToleranceCheck` (with `floors`), `ToleranceRow` (with `floors`,
`ripple_from`, `transient_estimate_v`), `ToleranceResult`, `TransientResult` (in
`ProbeImpedance.transient`), `LoadReport.v_low_v` / `v_high_v`, `board_fields.carried_by_ref`,
`coppernicus.service.tolerance` (`project_inputs`, `plan`, `solve`: the run `coppernicus
tolerance` in `cli.py` and `Session.solve_tolerance` / MCP `solve_tolerance` share),
`report.write_tolerance_report`, `ci` (`--tolerance`), `examples/tolerance`.

KiPI (evaluate.py:1-10): per load, each side apart (below and above nominal), every value a
deviation in volts, never negative: **DC** = the DC drop (or boost) + the VRM's DC accuracy,
against the DC tolerance; **AC** = ripple + transient, against the AC tolerance; **total** =
DC + AC, against the tolerance (`CHECK_PARTS`, :23). `judge()` (:96-102): FAIL if a side
exceeds its allowed deviation (+1e-12 V), PASS only when every part is known, else none.
Nominal is the target voltage of the VRM driving the load's rail (evaluate.py:110, gather.py
`driver` :48). Coppernicus keeps this arithmetic and KiPI's limits, and replaces KiPI's
completeness: a part KiPI counts as a known 0 is **missing** here (§ Known limitations).

### Project fields (each omitted while unset; older files load, save and solve unchanged)

| Coppernicus | KiPI | Unit | Notes |
|---|---|---|---|
| `Load.tol_pct` / `tol_mv` | `Load.tol_min`, `tol_max`, `tol_pct` (or `tol_dv`, ÷1000) | % of nominal / mV from it | The whole band. A number `n` is ± n; a signed pair `[below, above]` as datasheets write it (`[-5, 3]`). Below ≤ 0 and above ≥ 0 (Coppernicus's rule, `ProjectError`; KiPI has none). 0 is a value. A `_pct` with its `_mv`: `ProjectError`. KiCad field `PI_TOL` (`5%`, `-3%,+5%`, `50mV`; `FieldError` for the same faults). |
| `Load.dc_tol_pct` / `dc_tol_mv` | `dc_tol_*` | same | The DC share (dc/pi/model.py:197-211; KiPI's own model says "nothing reads them yet": only the tolerance stack-up does). KiCad field `PI_DC_TOL`. |
| `Load.ac_tol_pct` / `ac_tol_mv` | `ac_tol_*` | same | The AC share. Independent of `Probe.ac_tolerance_v` (T9's impedance target, brief Q9): see below. KiCad field `PI_AC_TOL`. |
| `Load.ripple_mv`, `Load.transient_mv` | — (a `Contribution` accepted in KiPI's table) | mV from nominal, below / above | Entered parts (a datasheet or measured figure), same format and sign rule, in volts' direction (below = more negative). |
| `Source.accuracy_pct` / `accuracy_mv` | `Vrm.acc_min`, `acc_max`, `acc_pct` (or `acc_dv`) | % / mV | Any source kind. Read through KiPI's own `Vrm.accuracy_sides()` (model.py:171-177). KiCad field `PI_ACC`. |
| `tolerance: {ripple_sum, bw_limit_20mhz}` | `stack(…, bw)`; KiPI's ripple is always RSS | — | `ripple_sum: linear` (default) or `rss` (brief Q3); `bw_limit_20mhz: false` (default, brief Q4). Unknown keys: `ProjectError`. Omitted at the defaults. |

Validation runs in `__post_init__`, so a `Load` or `Source` made by the app is checked as one
loaded from a file; `carry_over` (the rail editor) keeps the new keys.

**Misspelt keys** (coordinator's ruling d). Load and source mappings still ignore unknown keys,
so old files keep loading (there is no loader warnings channel). `coppernicus tolerance` reads
the raw file (`tolerance.misspelt_keys`): an unknown load or source key that looks like a
tolerance (it holds "tol", "acc", "ripple" or "transient", or is within difflib's 0.8 of a known
tolerance key: `acuracy_pct`) is a warning, "Load U2 on VOUT: unknown key 'tol_pc' in the project
file, ignored: did you mean 'tol_pct'?", in the result and the summary, and a not-judged entry
`{item, net, limit: the misspelt key, reason}`: a typo never ends in a silent pass.

**KiCad fields** (ruling a; the owner's fields decision). `PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL`
(a load's) and `PI_ACC` (a source's) give these keys (`board_fields.tolerance`: one value ± that,
or `below,above`, in % or mV). On a rail taken from the board's footprint fields they are read
per rail position (one value for every rail where the part is a load or a source, or one per
rail). That rail replaces the project's terminals; `board_fields._from_fields` carries back by
ref, as it does remote sense, what the part's fields do not give: the loads' `tol_*`,
`dc_tol_*`, `ac_tol_*` of a family without its field, `ripple_mv`, `transient_mv` (no field) and
the sources' `accuracy_*`, each with the note "Rail VCC: ripple_mv, transient_mv of U1 from the
project file (the part's KiCad fields do not give them)." Where the field gives a family, it
wins, with a warning when the project's value differs ("KiCad field PI_TOL "5%" on U1 gives
tol_pct ±5 % for U1 on VCC; the project file's tol_mv ±40 mV is not used (set
override_board_fields: true on the rail to use the project's)"). On a project-file rail (one the
fields do not define, or leave incomplete) the same fields reach its terminals by part
(`_library_tolerances`; a source's on `_source_part`, an external-inductor buck's IC), with the
same warning; a rail with `override_board_fields` keeps the project's. A part the fields' rail
lacks loses the project's values: `coppernicus tolerance` warns, each tolerance
the user set on such a load is a not-judged entry (exit 3, never a silent pass), and an accuracy
entered for a source it lacks is said lost ("J5's accuracy_pct in the project file was lost:
rail VCC comes from the board's KiCad footprint fields, whose source is J1 …"), not "enter it".
MCP `solve_tolerance` does the same, through the same code (`service.tolerance.project_inputs`).
The lost-parts block is `tolerance.fields_losses`, which `project_inputs` and the app's
Tolerance tab both call (§ The app's Tolerance tab).

Limits: KiPI's `deviation_limits` (evaluate.py:28-36) through `range_volts` (model.py:276):
a % of a negative rail is of its magnitude (`pct_to_volts`, model.py:254-259, swaps the ends:
−5 % of −3.3 V lies toward 0 V, **above** it), mV from nominal add as they are. **A % of 0 V
places nothing** (KiPI would allow 0 V either side, a spurious fail): the check is not judged,
"{key} is a % of 0 V, which places nothing: enter {…}_mv"; the same for `accuracy_pct` of a 0 V
source.

### Parts (each a `TolerancePart(below_v, above_v, source, detail)`, or missing with a hint)

- **DC** from the power tree (`solve_project_tree`: links, ground return, the board
  temperature when `thermal.enabled`). Both sides from the **worst pins** (brief Q1): below =
  nominal − v_low, above = v_high − nominal, each at least 0, from the additive
  `LoadReport.v_low_v` / `v_high_v` (KiPI's `v_low` = lowest supply pin − highest ground pin,
  `v_high` = highest supply pin − lowest ground pin; dc/pi/results.py:63-64). `v_worst_v` and
  every other tree number are unchanged. KiPI's tolerance table uses the **mean** (gather.py
  `_dc` :75, `v_effective`); it is in the detail, with an assumption line. On a remote-sensed
  rail the near pins sit above nominal and the far ones below: both sides are non-zero (the
  mean would read one). Missing when the load is DISCONNECTED, NOT CONVERGED, VRM OUT OF
  REGULATION, absent from the tree (an excluded net, a rail solved as a ground), or when
  `TreeResult.converged` is False. From an electro-thermal solve that did not settle (or ran one
  iteration), `temperature.py`'s rule: the DC drop is a **floor** (below), so it can fail a check
  but never pass one, and the hint uses its wording ("the board temperature (not settled: last
  change 0.83 °C): its DC drop … can fail a check, never pass one").
- **Accuracy**: the driving source's (`TreeResult.vrms` entry whose `group` is the load's
  group, so a load behind a link or net tie gets its rail's source). Unset: missing, "enter
  U1's DC accuracy (accuracy_pct)" (F1).
- **Transient**: only the load's entered `transient_mv` is judged (brief Q2). KiPI's estimate
  is **shown, never judged**, in the part's detail or in the hint. It is computed in
  `engine/ac.py` **only when asked for** (`solve_ac_rail(…, transient=True)`, which only the
  tolerance command passes: `coppernicus ac`, the app and MCP keep their cost and memory), per
  probe with a step and a slew rate, **only with a bandwidth the user gave**
  (`ac.bandwidth_hz`), by calling `kipi.ac.transient.transient(freqs, z, given_bw, step_a,
  slew·1e6, 0)` and `bandwidth_limited(…, 20 MHz)` itself: `_context` writes the 50 kHz
  placeholder into KiPI's VRM, so `transients(result)` is never used. `ProbeImpedance.transient`
  (`TransientResult`: under- and overshoot, both through 20 MHz, `t_s`, `i_a`, `v`, `v_bw`,
  KiPI's notes, the edge's inductance and dip; no load line: `load_line_ohm` 0); |Z| unchanged
  bit for bit. On a negative rail its under- and overshoot swap (a step pulls the rail up,
  gather.py:163). Not entered: missing, "enter transient_mv for U2; KiPI's estimate with a
  typical loop at 100 kHz: 25.99 / 25.98 mV, … through 20 MHz (shown, never judged)", or "…; no
  KiPI estimate: {no probe on U2's pins | probe P1 has no load step (step_a, slew_a_per_us) | no
  control loop bandwidth is given for U1 (ac.bandwidth_hz) | --skip-ac left it out | the AC
  solve of R failed}".
- **Ripple**: from every aggressor listing the load's probe in `ripple_probes`, its
  `RippleResult` at that probe (`where == "probe"`; the four peaks of KiPI's `ripple_peaks`),
  added per side by `tolerance.ripple_sum`: the **sum of the peaks** (linear, default) or KiPI's
  root sum of squares (gather.py:201); full band, or through 20 MHz with `bw_limit_20mhz`
  (both shown). Missing when any listing aggressor is not solved (F2: "A2 not solved
  (--skip-noise)"; a **partial** result, handed over during a solve, counts as not solved),
  rests on KiPI stand-ins (its `NoiseResult.stand_ins`, T10's rule: not
  judged whatever the peak; the values in the hint, the stand-ins in `ToleranceRow.stand_ins`),
  or shows no ripple at the probe (board model off, or the probe not on its rails). With both a
  usable computed ripple and `ripple_mv`, the larger per side (the detail names both). When no
  aggressor lists the probe: the entered `ripple_mv`, else missing (F3, brief Q5): on a switching
  rail (an `smps` source or an aggressor on it) "add probe P1 to A1's ripple probes, or enter
  ripple_mv for U2" (or "add an aggressor for U1 with probe P1 in its ripple probes, …"), else
  "enter ripple_mv for U2 (no switcher's ripple is solved here)". **A listing aggressor that
  gives nothing usable leaves the ripple missing even with `ripple_mv` entered** (the literal
  reading of "both a usable computed ripple and ripple_mv → the larger"): the hint says
  "ripple_mv … entered, not used while an aggressor listing P1 gives no usable ripple (it is the
  ripple's floor)".
- The load's probe is KiPI's `probe_for_load` (gather.py:62-71) over all the project's probes
  (`engine.tolerance.load_probes`: the probe on the same supply pins, and ground pins where
  both have them, else the same part on the same supply net); a probe whose pins are not on the
  board is left out, not an error.

The stack itself is `engine.tolerance.stack`, a thin copy of KiPI's `stack()` loop
(evaluate.py:123-129) with KiPI's own `judge` and `CHECK_PARTS` imported: a check adds the
parts it knows, is complete only when it knows them all, and so can fail before it is complete
but passes only once it is. Stand-in parts are missing, so a check fails only if its usable
parts alone exceed (T10's rule kept, and T8's "fail while incomplete" for what is known).

- **Floors** (finding I4, brief Q6 "a check fails if its usable parts alone exceed"): a missing
  part whose least value is known adds it for the FAIL test only, the check staying incomplete
  (`ToleranceCheck.floors`, `ToleranceRow.floors`): the entered `ripple_mv` (the final ripple is
  the larger per side, never below it), the sum (or the RSS when chosen) of the **usable**
  solved aggressors while another listing aggressor is unsolved or rests on stand-ins, and the
  DC drop of an unsettled electro-thermal solve. **A stand-in's value is never a floor.** The
  fail text names them: "total 68 mV below nominal, over the 54 mV allowed (from its known parts
  alone, ripple at least 20 / 20 mV (ripple_mv); missing: ripple)".
- **Not finite** (I1): a part that is not a finite number counts as missing ("ripple is not a
  finite number …"), a limit that is not finite places nothing (not judged): NaN would pass any
  comparison.

### Verdicts (only the user's tolerances)

- Per check with a tolerance set (`limit` = the key actually set: `tol_pct`, `dc_tol_mv`, …):
  `fail` if a side's usable parts exceed (+1e-12 V); `pass` only complete and within; else not
  judged, the reason every missing part's hint ("accuracy: enter U1's DC accuracy
  (accuracy_pct)"), or why the tolerance places nothing (no source drives the rail; a % of 0 V).
- A row: fail > not judged > pass over its checks; a load without any tolerance is shown with
  its parts and no verdict ("" ; no entry).
- Entries `{item: load label, net, limit, reason}`; a failed one says which side is over and,
  while parts are missing, "(from its known parts alone; missing: …)".
- Overall: `fail` if any check fails; else `not_judged` if one with a tolerance is not judged;
  else `pass` if one passed; with no tolerance anywhere `not_judged` and no entry ("No tolerance
  set: the loads' parts are shown, not judged"), as `coppernicus ac` without a target (Task 9,
  Verdicts). Exit 0 / 1 / 3 / 2 through `_exit_for`. `solve --tree`'s verdict is unchanged.
- **Q9**: when a load's probe has `ac_tolerance_v` and the load has `ac_tol_*`, they are compared
  in volts (T9 takes `ac_tolerance_v` as one deviation, Z = V / I_step; the load's on its tighter
  side, through `allowed`), relative margin 1e-9; a mismatch puts the same warning, naming both
  values, in the tolerance result and in the AC result (`ImpedanceResult.warnings`). Neither is
  derived from the other.

### Assumption lines and warnings (in `ToleranceResult`)

Assumptions, in order: the stack-up (KiPI's three checks, each side apart); nominal = the
driving source's set voltage; the DC drop on the worst pins (the formulas, and that KiPI's
table uses the mean); a part not known is missing, never 0 (what KiPI would count as nothing);
only an entered transient is judged, KiPI's estimate shown (the typical PI loop at the bandwidth
given, about 60°, not the converter's own); entered parts are mV from nominal; the ripple sum
rule (linear, or "root sum of squares, KiPI's rule for unsynchronised switchers on average;
peaks can coincide"); full band or 20 MHz; when a judged load is on a **negative rail**, "a _pct
pair follows the magnitude of nominal (KiPI's pct_to_volts: −5 % lies toward 0 V, above it),
while a _mv pair and the entered ripple_mv and transient_mv follow volts (below = more
negative)", and each of its parts' details says "below = more negative". Then what the numbers
rest on (finding I3): the power tree's assumptions, and those of each noise result used,
prefixed with the aggressor's name ("U1: …"); the KiCad-field notes (`merged.annotate`, as the
other commands). Warnings: the KiCad-field and misspelt-key warnings, the tree's, those of each
noise result used ("U1: …"), the AC result's beside each estimate shown ("AC VOUT, behind KiPI's
transient estimate at LOAD (shown, never judged): …"; a Q9 mismatch it also carries is shown once),
and the Q9 mismatches. The AC results themselves are not annotated with the KiCad-field notes
(those are in the result already). Where each part
came from is in its `source` and `detail`.

### Outputs

- `<project>_tolerance_summary.json` (`allow_nan=False`, no arrays): `kind: "tolerance"`,
  `verdict`, `verdict_note`, `failed`, `not_judged`, `options` {ripple_sum, bw_limit_20mhz},
  `warnings`, `assumptions`, `estimates[]` (each solve's cost as printed, `solve` "AC VOUT" |
  "noise U1" | "board temperature"), `stats` {seconds, tree_seconds, ac_rails, aggressors,
  memory_peak_mb}, `csv_file`, `loads[]`, `summary_file`. Per load: `load`, `name`, `net`,
  `rail`, `nominal_v`, `source`, `probe`, `verdict`, `parts` {dc, accuracy, ripple, transient:
  {below_mv, above_mv, source, detail} | null}, `hints` {part: text}, `floors` {part:
  {below_mv, above_mv, source}}, `transient_estimate`
  {below_mv, above_mv, below_20mhz_mv, above_20mhz_mv} | null, `checks` {dc, ac, total:
  {limit, below_mv, above_mv, allowed_below_mv, allowed_above_mv, margin_below_mv,
  margin_above_mv, complete, passed, missing, floors, reason}}, `stand_ins`.
- `<project>_tolerance.csv`: one row per load: `load`, `net`, `nominal_v`, per part
  `<part>_below_mv`, `<part>_above_mv`, `<part>_source` (empty when missing), per check
  `<check>_limit`, `_below_mv`, `_above_mv`, `_allowed_below_mv`, `_allowed_above_mv`,
  `_margin_below_mv`, `_margin_above_mv`, `_verdict`, then `verdict`. A check's sums (CSV and
  summary) include its floors, so on an incomplete check they are a least value.
- `coppernicus tolerance PROJECT [--skip-ac] [--skip-noise] [--out DIR] [--no-board-fields]
  [--json] [--html [PATH]] [-v]`: the power tree (KiCad footprint fields merged as `solve
  --tree`; the board temperature's estimate first when `thermal.enabled`), then **only where a
  load with a tolerance needs it** one AC solve per rail (a probe with a step and a slew whose
  source has a bandwidth given) and one noise solve per aggressor listing such a probe, every
  cost printed before anything is solved (`thermal_estimate`; `ac_estimate`; `noise_estimate`,
  as `coppernicus noise`) with the memory warning on stderr; an AC or noise solve that cannot
  be sized is left out as one that fails. `--skip-ac` / `--skip-noise` leave
  those parts out (the hint names the flag); a load without a tolerance gets no solve of its
  own (its hints say so). A failed AC solve only loses KiPI's estimate (a
  warning); a failed noise solve leaves the ripple missing (a warning; not judged). Prints per
  load the four parts (mV below / above, where each came from), the checks with what each
  allows and the margin, the verdict, what to do about a missing part; then the assumptions,
  the warnings and the verdict line. `--html` writes `report.write_tolerance_report`'s offline
  page (default `<out>/<project>_tolerance_report.html`, its path in `report_file`).
- `Session.solve_tolerance` / MCP `solve_tolerance(project_path, skip_ac?, skip_noise?,
  confirm_large?, use_board_fields?)`: the same document through the same run
  (`service.tolerance.plan` then `solve`), its files in the session's folder; when an estimate
  exceeds this machine's memory nothing is solved or written (`solve_too_large`, the hint per
  kind of solve) unless `confirm_large`.
- CI (`python -m coppernicus.ci solve --tolerance`; action.yml `tolerance`, default false): on
  each project that sets a tolerance on a load or holds a misspelt-looking key; fail 1, not
  judged 3 (its entries added to the project's `not_judged`, `rail` null, "tolerance of <load>
  on <net>"), error 2; the job summary's "### Tolerance" table (load, check, allowed, total,
  verdict).

### Cost

The tree is cheap; AC (Task 9's fit, 2466.6·n^1.1706 B for one factorization) and noise
(0.2 GB + 36 kB per unknown) dominate, and run only where a load with a tolerance needs them.
`transient()` runs only in the tolerance command's AC solves (`transient=True`): 17 ms on the
example (96 566 samples per array, four arrays kept); KiPI caps it at 2^21 samples, about 67 MB
per probe for the four arrays. A run on this machine
(M-series): `coppernicus tolerance examples/tolerance/tolerance.coppernicus.yml` takes 2.2 s
wall (tree 0.08 s; AC 5 708 triangles, 111 field frequencies; noise 14 059 unknowns, 34 field
solves), 0.70 GB peak RSS.

### Validation

- **Drift** (`test_our_stack_equals_kipis_where_both_rules_agree`): where both rules agree
  (accuracy set, every part present, one aggressor, the mean = the worst pin), our numbers and
  verdicts equal KiPI's `stack()` built from KiPI objects (`kipi_reference_stack`: its `Load`,
  `Vrm`, `LoadEntry` of `Contribution`s), within KiPI's 6-figure rounding of a part, on % and
  mV tolerances, asymmetric pairs, a negative rail, passes and fails.
- KiPI's false passes, before → after on the same input: an unset accuracy (KiPI's stack
  passes, here not judged), a partly solved ripple, a rail without an aggressor (KiPI's
  `not_applicable`).
- The RSS ripple equals KiPI's own `Gatherer._ripple` on the same peaks; the transient estimate
  equals `transient()` / `bandwidth_limited()` called on the same |Z|, bit for bit.
- Worst pins against the mean on `pins_board` (U1's three pins along a strip), and with J1
  remote-sensing U1's middle pin: near pin above 3.3 V, far pin below, both sides non-zero.
- Tolerance fields change no number of the tree or of an AC solve.
- Tests compare solver figures with a relative tolerance and derive the expected text from the
  summary's own numbers (CI runs on Linux and Windows).
- `examples/tolerance`: DC 27.05 / 0 mV, accuracy 18 / 18 mV, ripple 7.883 / 10.57 mV, entered
  transient ±30 mV (KiPI's estimate 25.99 / 25.98 mV), total 82.94 / 58.57 mV: ±6 % holds,
  ±3 % fails.

### The app's Tolerance tab (T11b, `app/tolerance_panel.py`)

- After "Ripple & noise". It composes (`compose_held` → `tolerance.compose`) **only from what
  the window holds**: `window.tree`, the Impedance tab's results, the Ripple & noise tab's. A
  result whose key no longer matches the merged project is stale, so its part is missing
  ("the setup changed since its solve: …"); with no tree, `compose(tree=None, tree_note=…)`
  leaves every DC part missing ("solve the power tree (Ctrl+T)") and takes each nominal from
  its rail's own source. It never starts a solve: its Solve button runs the selected load's
  next missing one through the tab that owns it (power tree; the noise of a listing aggressor;
  the impedance with `solve_ac_rail(…, transient=True)`, `AcPanel.solve_rail`), so the estimate
  line and "Solve anyway?" apply. The Impedance tab's own solve keeps `transient=False`: its
  result says "estimate not available: solve the impedance of R with the transient".
- The Impedance and Ripple & noise keys leave out the tolerances, entered parts, accuracies
  and the `tolerance:` block (`panels.without_tolerances`) and are taken of the merged
  project: typing a tolerance never makes a held result stale, nor drops the power tree.
- Editors: per load `tol` / `dc_tol` / `ac_tol` in % and in mV, `ripple_mv`, `transient_mv`;
  per source the accuracy; the options. A cell is a number (± n) or `below, above` (also
  `[-3, 2]`, `-3 / +2`; `3,3` reads 3.3); empty is unset, 0 a value; the new terminal is
  built by `dataclasses.replace` (its checks run: a `_pct` beside its `_mv` is refused).
- A cell whose value a KiCad field gives is read-only, cell by cell (`Merged.tolerance_field`,
  recorded by the merge in `_from_fields` and `_library_tolerances`; both units of the family;
  never `ripple_mv` / `transient_mv`), its tooltip naming the field and the part
  (`field_held`); `set_value` refuses it with that text. Under `override_board_fields` no
  field holds anything, so every cell edits.
- Elsewhere a rail from the KiCad fields is editable: the edit goes to the
  project's part of the same ref (`carried_by_ref`'s pairing); with no project rail of its own
  there, that rail becomes a copy of the fields' sources and loads as merged (the values their
  fields give included, as Use KiCad fields copies them), which `_differences` sees
  as no difference (no warning; KiCad still gives the values). A row is read-only where the
  project's own rail differs from the fields and has no part of that ref ("Override in
  project"). Use KiCad fields keeps the tolerances by ref the same way, and names the parts
  whose go; Override → start from the KiCad values keeps them (the merged rail carried them).
- Misspelt keys: read from the file at open (`read_misspelt`), kept through a reload and a
  save (the save drops the key while the tolerance stays unset), settled only once the part
  has the key meant set (either unit; `tolerance.meant_key`).
- Clicking a row selects its rail and zooms to its pads; Export writes
  `<stem>_tolerance.csv` and `<stem>_tolerance_summary.json`, as the CLI.

### Known limitations

KiPI's false passes Coppernicus does not inherit (each a missing part, so not judged; recorded
here, not reported upstream):
- **F1** an unset VRM accuracy counts as 0 (`accuracy_sides`, model.py:171-177; `stack()` always
  uses it, evaluate.py:118, and keeps `accuracy_set` unused).
- **F2** a partly solved ripple counts as complete: aggressors not solved only add a hint
  (gather.py:206-209).
- **F3** a rail without an aggressor counts its ripple as 0 (`NO_SWITCHER`, gather.py:147, :186),
  whatever the source.
- **F4** KiPI's transient rests on a loop the user cannot enter (a typical PI compensation,
  about 60° of phase margin; transient.py:7-18): a real loop with less margin dips more, so it
  is not a bound either way. Only an entered `transient_mv` is judged.
- **F5** (judgement) KiPI adds several switchers' ripple as RSS, "on average" (gather.py:201):
  their peaks can coincide, so the default here is the linear sum.

Also:
- KiCad fields `PI_TOL`, `PI_DC_TOL`, `PI_AC_TOL` and `PI_ACC` give a part's tolerances and
  accuracy (merged in `board_fields.merge`, so every caller gets them); a rail from the board's
  fields carries the project's by ref where the part's fields give none; a part it lacks loses
  them (a warning; each lost tolerance not judged). `coppernicus tolerance`, MCP
  `solve_tolerance` and the app's Tolerance tab report the lost ones.
- Loads and sources still ignore unknown keys when loading; only `coppernicus tolerance` (and
  MCP `solve_tolerance`, CI's `--tolerance`, the app's Tolerance tab) warns about one that looks
  like a tolerance (and does not judge its load).
- KiPI's estimate is shown even when its AC result hangs on something assumed (capacitors
  without a model, an assumed εr); it is never judged.
- One-sided tolerances (a `null` end) are not offered: both ends are entered.

---

## Solver order options: SuperLU's symmetric mode (`kipi/core/solver/direct.py`, opt-in)

KiPI factorizes with SciPy's SuperLU in the COLAMD column order (`direct._superlu`,
`direct._superlu_complex`), which ignores that its matrices are symmetric. SuperLU's symmetric mode (minimum degree on the pattern
of A + Aᵀ, `SymmetricMode`, the diagonal as pivot) fills about half as much on them, so it takes
less time and memory. `src/coppernicus/engine/superlu_order.py` offers it per part, each **off
by default**.

| Part | What runs in SuperLU's symmetric mode | Pivoting |
|------|---------------------------------------|----------|
| `thermal` | the board temperature's heat model (`BoardThermalModel`, model.py:282), marked on the calling thread around its construction in `engine/thermal.py` | none (symmetric positive definite) |
| `dc` | every other real factorization (outside the heat model's marker): the DC copper's direct solves (`SpdSolver`, spd.py:117 and its fallback at :163; SuperLU up to 200,000 unknowns) | none (symmetric positive definite) |
| `ac` | every complex factorization: the AC field solves, one per field frequency (field.py, fem.py:648), and so the noise and tolerance solves; process-wide, since KiPI solves the frequencies in its own threads | partial, threshold 0.1 (KiPI: SciPy's 1.0): the systems are indefinite |

How to enable: `COPPERNICUS_SUPERLU_ORDER=thermal,ac,dc` in the environment (comma-separated
parts, read when `coppernicus.engine` is imported), or `superlu_order.enable("thermal", "ac", "dc")`.

Off, nothing is replaced and KiPI's own path runs: bit-identical (the drift test of our
electro-thermal loop against KiPI's `solve_electrothermal` runs with it off). On, a different
order rounds differently: results move by round-off only. Proof criteria (checks of "round-off
only", not limits of the product):

- `thermal` and `dc`: the benchmark fingerprint's scalars (tools/bench) within 1e-9 relative, with a
  floor per unit for values near zero (the unit's largest magnitude in the fingerprint × 1e-9);
- `ac`: |Z| within 1e-8 relative (and the noise and tolerance scalars that come from it): at low
  frequency the rail is tied to ground only through the plane capacitance, an ill-conditioned
  system in which round-off grows (measured up to 8.6e-10);
- the backward error ‖b − Ax‖ / (‖A‖ ‖x‖ + ‖b‖), on and off, of the same order (a few 1e-16;
  `tests/test_superlu_order.py`).

Measured (macOS arm64, a loaded machine: provisional), the heat model of a 50 mm synthetic board
(250,000 unknowns, 4 layers): factors 121 M → 61 M non-zeros, memory +3.2 → +1.4 GB,
factorization 3.6 → 2.0 s, each later solve 0.37 → 0.15 s; a 6-layer 250 × 250 grid (375,000
unknowns): 12.6 s / +8.3 GB → 5.2 s / +3.0 GB. KiPI's pdn example's AC solve (19,783 unknowns,
111 field frequencies): each factorization 5.2 M → 3.3 M non-zeros, 0.25 → 0.155 s, 208 → 135 MB;
the backward error at every frequency, off and on, at most 6.2e-17 and 4.9e-17. The factor keeps
KiPI's name `superlu` and its true memory, which KiPI's thread count reads (field.py `threads`).

The seam: KiPI's private functions are replaced process-wide, once, only on KiPI 1.0.1 with
those functions of the expected signature (otherwise a warning, once, and KiPI's path); a part
that is off hands its factorizations to KiPI's own function. `uninstall()` puts KiPI's back.

DC: on the rails of a 100 mm synthetic board (103,000 and 169,000 unknowns) the factors halve,
the factorization takes 0.28 → 0.20 s and 0.58 → 0.42 s, each solve half the time. **Open
risk:** KiPI's own comment at `direct._superlu` says this order "takes minutes" on its meshes.
On synthetic meshes it took 0.5 s even with dense pad rows (20 nodes tied to 1,000 nodes each:
COLAMD 15.8 s there), but that does not refute a remark made on real boards: `dc` is not to be
made a default before a run on a real board on a quiet machine.

Recorded for a later ruling, not built: with this order a direct solve beats KiPI's AMG-CG
(above 200,000 unknowns) at 352,000 unknowns (1.13 s against 2.68 s, but 647 MB against
241 MB) and ties at 620,000 (3.6 s against 3.4 s, 1.7 GB against 0.43 GB): raising the direct
ceiling would replace CG's 1e-10 relative residual with an exact solve for mid-size rails.

Not refitted: the memory estimates (`thermal_ram_estimate`; the AC `ram_estimate` and
`noise_ram_estimate`) were measured in KiPI's order; with their part on they overstate, and
their text says so.
