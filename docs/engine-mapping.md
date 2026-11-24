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
