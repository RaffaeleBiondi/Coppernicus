# KiPI analysis — stage 1

> Evaluation of [KiPI](https://github.com/drewsum/KiPI) as the starting point for Coppernicus.
> Status: **draft**. The UX section waits for hands-on notes from a real-board session.

| | |
|---|---|
| KiPI version | `v1.0.1` (commit `3ffce16`), GPL-3.0-or-later |
| Where | read-only clone outside this repo, Python 3.12 venv (`uv`), deps from `requirements-standalone.txt` |
| Machine | Apple M5, 24 GB, macOS; KiCad 10.0.5 installed but **not** used by KiPI standalone |
| Method | ran the accuracy tests, imported two real boards, benchmarked solves, read the solver and mesher source; three code-mapping passes (import, physics, UI) |

Real boards used for testing are not described beyond what the results need: **board P** is
a small 2-layer power board, **board C** a dense 6-layer board with many vias, arc tracks and
filled zones.

## TL;DR for the gate

- **The core is separable from the GUI.** `kipi.dc.solver.run(snapshot, NetProblem)` meshes,
  solves and post-processes one net with no Qt involved; `kipi.dc.pi.tree.solve_tree(snapshot,
  setup)` does the same for a whole power tree. The accuracy tests drive it headless.
- **The physics is sound and validated.** 2.5D P1 FEM per copper sheet, barrels as resistors
  between equipotential drill rings, sources eliminated as Dirichlet unknowns (SPD system).
  All 10 DC accuracy tests pass on macOS against closed-form answers (straight strips within
  0.2 %, arcs, right-angle corners, via rows, method of images).
- **It is fast enough.** Worst case tried: a 6-layer ground net, 3.1 M triangles / 1.5 M unknowns,
  solved in 14 s with 3.4 GB peak memory. Typical power nets solve in well under a second.
- **The standalone `.kicad_pcb` reader is accurate on our boards.** Per-net, per-layer copper
  area matches KiCad's own `pcbnew` within 0.005 % on board P and within 0.7 % on board C, the
  residual being circle discretisation of via rings (it changes sign when pcbnew's arc
  tolerance is refined). Reads take 0.14 s and 0.51 s warm. KiPI's importer tests pass
  (184 passed, 14 skipped for needing a live KiCad or GNU gzip).
- **AC and coupled-noise accuracy tests pass too** (run here, not only read).
- **Licensing:** KiPI is GPL-3; its mesher `triangle` bundles Shewchuk's Triangle, whose
  notice allows "private, research, and institutional use" freely and commercial
  *distribution* only by arrangement with the author; the obvious alternative mesher, gmsh,
  is GPLv2+. See section 7 for the exact text and our reading.

## 1. Codebase overview

~55 k lines of Python in `kipi/`:

| Package | Size | Role |
|---|---|---|
| `kipi/dc` | 576 KB | DC problem setup (VRMs, loads, series parts, power tree), solver, DC GUI panels, reports |
| `kipi/gui` | 483 KB | Qt application, 3D view, dialogs |
| `kipi/noise` | 411 KB | coupled switcher noise on signal nets |
| `kipi/ac` | 348 KB | PDN impedance vs frequency, capacitor models, target impedance |
| `kipi/core` | 328 KB | board model, geometry, mesher, sparse solvers, viz helpers, thermal |
| `kipi/sources` | 232 KB | importers: `.kicad_pcb`, KiCad IPC (live), ODB++, HyperLynx, snapshots |
| `kipi/launcher` | 145 KB | self-installing launcher, plugin environment setup |
| `kipi/tolerance` | 57 KB | tolerance analysis |

Standalone dependencies: PySide6-Essentials, qtpy, pyvista + pyvistaqt (VTK), numpy, scipy,
shapely ≥ 2.1, triangle, pyamg, plotly, matplotlib, PyYAML.

## 2. Board import

From the code-mapping pass (`kipi/sources/kicad_pcb`, `kipi/core/geometry`, `kipi/core/model`).

**Parser.** Own regex-tokenised S-expression reader (`sexpr.py`); no KiCad needed. Refuses
files older than format 20221018 (KiCad 7); no upper bound. Tested on a KiCad 10 fixture and
a KiCad 9 copy. Handles numbered nets (≤ 9) and nets by name (10).

**Items.**
- Tracks and arc tracks (start/mid/end).
- Vias: through, blind, buried, micro (type is a label; layers spanned from first/last),
  KiCad 9+ padstacks (normal, front-inner-back, custom), "remove unused layers".
- Pads: circle, rect (incl. chamfer), roundrect, chamfered rect, oval, trapezoid, custom
  (anchor + primitives), per-layer padstacks, pad/drill offsets; NPTH copper only where the
  pad exceeds the hole.
- Zones: the **stored `filled_polygon` is used as is** (fills are not recomputed; thermal
  spokes and islands are whatever KiCad saved). Unfilled zone → no copper + warning.
  Keepouts/rule areas skipped. Live-KiCad (IPC) import can refill first.
- Copper graphics with a net become zones; graphics without a net are dropped with a warning.
- Not handled: copper text (never copper), net ties (no code), unknown pad shapes (silently
  no copper), arcs without a mid point (dropped). Teardrops are saved by KiCad as zones, so
  they should pass through as zones (inferred, not tested).
- Footprints: rotation via a frame; no explicit mirror step (KiCad stores flipped pads
  already mirrored); verified by tests against the KiCad API.

**Stackup.** Copper thickness from the stackup (defaults 35 µm outer, 17.5 µm inner);
dielectric thickness = sum of sublayers, εr = series combination. If any copper layer is
missing from the stackup the whole stackup is replaced by a default (1.6 mm FR4) with a
warning. A missing dielectric thickness or εr silently becomes 0. Loss tangent not read.

**Per-net copper.** The importer keeps primitives; the per-layer union is built on demand
(`core/geometry/copper.py`): buffered tracks (round caps), discretised arcs (64 segments per
circle), pad polygons (KiCad's `max_error`, default 5 µm), via discs, zone fills →
`union_all`, then same-net drills subtracted (NPTH not cut), invalid polygons repaired.

**Data model** (`core/model/board.py`): `BoardSnapshot` with `Stackup`, `Net`, `Track`,
`Via`, `Pad` (shapes keyed by layer), `Footprint`, `Zone`; mm, KiCad axes, nets by name.
Snapshot file `.kipi.json.gz` (gzipped JSON, `format_version = 1`). ODB++ and HyperLynx
readers produce the same snapshot.

**Assessment.** Broad and careful coverage, tested against the KiCad API. The weak points
for us are silent ones (missing dielectric data → 0, unknown pad shape → no copper) and the
dependence on zones being filled before saving — both are fixable by surfacing warnings in a
new UI rather than by rewriting the importer.

**Measured on real boards** (read-only; warm read, after one load to import modules):

| Board | Read time | Stackup read | Items |
|---|---|---|---|
| P | 0.14 s | 2 copper layers, thickness and εr from the board | small: hundreds of tracks and pads, few vias, filled zones |
| C | 0.51 s | 6 copper layers, per-layer thickness, 5 dielectrics with their own εr | dense: thousands of tracks and pads, arc tracks, many vias and filled zones |

**Geometry check against KiCad.** For 4 nets on board P and 5 on board C (power rails and
ground), the per-layer copper union computed by KiPI (`net_layer_copper`, drills not cut) was
compared with the same union built by KiCad 10's `pcbnew` from tracks, vias, pads, copper
graphics and stored zone fills (drills not cut):

| Board | Net × layer pairs | Worst difference | Notes |
|---|---|---|---|
| P | 8 | 0.005 % | |
| C | 30 | 0.70 % | Largest on inner layers that hold only via rings. With pcbnew's arc tolerance refined from 5 µm to 0.5 µm the difference flips sign (−0.5 %): it is circle discretisation, and the true area lies between the two. |

Drill subtraction and pad-shape details are covered by KiPI's own reader tests, which compare
against the KiCad API (`tests/sources`: 184 passed, 14 skipped for needing a live KiCad or GNU
gzip).

**Side effects to know.** Opening a board in the KiPI GUI writes next to it (a `.kicad_prl`, a
lock file, and the `.kipi-setup.yml` when a setup is saved); `kipi/core/eta.py` writes
`timings.json` to KiPI's application-data folder.

## 3. Physics and numerics (read from source)

**Governing equation** (`kipi/dc/solver/fem.py`): on each copper sheet
div(G_s grad V) = 0 with sheet conductance G_s = t/ρ. Linear triangles contribute
G_s/(4A)·(b bᵀ + c cᵀ). Copper resistivity 1.724·10⁻⁵ Ω·mm (annealed copper, IACS, 20 °C),
temperature coefficient 0.00393 /°C, both overridable per layer (`kipi/dc/solver/setup.py`).

**Vias** (`kipi/dc/solver/via_barrel.py`): a barrel is a copper tube whose *outside* diameter
is the drill and whose wall is the plating, area π(d·t_p − t_p²); between two consecutive
layers it is a resistor ρ·Δz/A with Δz between the layers' mid-planes. Plating comes from a
rule table by hole type and drill size; defaults are IPC-6012 Class 2 minimum average plating
(20 µm through/blind/PTH, 13 µm buried, 12 µm micro). Filled holes: non-conductive epoxy
(wall only), copper (whole hole) or conductive epoxy (core at a user resistivity). Surface
finish is not modelled.

**Terminals:** each drill ring on each layer, and each terminal pad (all its layers), is
merged into **one equipotential unknown** — exact for perfect-conductor contacts and better
conditioned than penalty resistors. A source holds all its pads at its voltage; a load
draws its current **split equally across its pads**. Optional "large pads as copper" mode
meshes big terminal pads and connects the part only at the hole wall or a central share of
an SMD pad.

**Series parts:** resistors, ferrites, fuses as conductances between pad groups; shorts
merged; diodes solved with Newton on their I-V curve; multi-pin parts as conductance matrices.

**Linear algebra** (`kipi/core/solver/spd.py`): sources eliminated as Dirichlet unknowns, so
the reduced system stays SPD. It solves for u = V − V_ref so a relative residual measures the
drop rather than the rail voltage. Direct factorization (SuperLU) up to 200 k unknowns,
PARDISO up to ~1 M where available (x86 only), AMG-preconditioned CG above.

**Robustness checks** (`fem.py`): no source → error; loads with no copper path to a source →
clear error (or dropped, if optional); floating copper islands → left out with a warning;
sources sharing copper at different voltages → error. Energy balance error is reported.

**Electro-thermal:** an optional temperature field scales ρ per triangle and per barrel,
ρ(T) = ρ₂₀·(1 + α(T − 20 °C)).

**Mesher** (`kipi/core/mesh/mesher.py`): constrained Delaunay quality meshing with
`triangle`, min angle 28°, max edge 0.5 mm on planes, 0.02 mm at plated holes, grading
0.2 per mm away from holes, polygon simplification 3 µm, 16-segment circles. All DC calls go
through one wrapper, `_triangulate()` (`mesher.py:207`), so the mesher is swappable in
principle; the switches are Triangle-specific. The solver only sees the `NetMesh` dataclass.
Large jobs mesh layers in a process pool.

**Mesh refinement and peak current density.** The size field grades only toward plated
holes (`graded_size`, `mesher.py:386`); there is no explicit refinement at necks or
corners, though Triangle's quality bound still shrinks elements in narrow copper (the
skinny-trace tests pass within 0.2 %). At a re-entrant (inner) corner the 2D current
density is singular (∝ r^(−1/3) for a 90° corner), so a point maximum of |J| depends on the
mesh; KiPI clips its colour scale at the 99.5th percentile. *For Coppernicus:* report
current density averaged across a neck, not a point maximum.

**Power tree and grounds** (`kipi/dc/pi/tree.py`). VRM kinds: source, LDO, SMPS, isolated.
LDO input current I_in = I_out + I_q; switcher I_in = V_out·I_out/(η·V_in) + I_q. Downstream
rails are solved first, with a fixed-point loop on each VRM's input voltage. Remote sense
steps the output until the sense pins reach the set-point; load line lowers the target by
R_LL·I_out; multi-phase VRMs inject I/N per extra phase. **Ground nets are separate FEM
problems**, coupled to their rails by an outer fixed-point loop.

**Details worth knowing.** Via barrels always use the global ρ (per-layer overrides do not
apply to them). The reported via current is the largest |I| over its barrel segments. The
electro-thermal loop runs a fixed 3 iterations with no convergence check. `kipi/core/eta.py`
records step timings to a `timings.json` in KiPI's data folder, so library use writes to disk.

## 4. Headless use

```python
from kipi.sources.kicad_pcb.source import KiCadFileSource
from kipi.dc.solver import run, NetProblem, VoltageSource, CurrentLoad, SolveSettings

snap = KiCadFileSource("board.kicad_pcb").load()
problem = NetProblem(
    net="VCC",
    sources=[VoltageSource("VRM", [pad.id for pad in vrm_pads], 5.0)],
    loads=[CurrentLoad("LOAD", [pad.id for pad in load_pads], 2.0)],
    settings=SolveSettings(),
)
result = run(snap, problem)      # NetResult(mesh, solution, fields)
result.fields.summary            # max_drop, j_max, power_loss, energy_balance_error, ...
```

`kipi.dc.solver.pads_for(snap, net, "U1", ["1", "2"])` resolves `REF.PAD` to pad objects.
The macOS spawn start method needs the usual `if __name__ == "__main__":` guard because
meshing runs in worker processes.

**Performance** (board C, arbitrary terminals, timing only):

| Net | Layers | Triangles | Unknowns | Total | Peak RSS |
|---|---|---|---|---|---|
| Power rail A | 6 | 87 k | 38 k | 0.2 s | 0.3 GB |
| Power rail B | 2 | 31 k | 16 k | 0.1 s | 0.3 GB |
| Ground | 6 | 3.1 M | 1.5 M | 14 s | 3.4 GB |

Peak RSS is the main process only; meshing worker processes are not included. Direct backend
on this machine: SuperLU (PARDISO is x86-only), so the ground case ran on AMG-CG.

## 5. User interface and visualisation

From the code-mapping pass (`kipi/gui`, `kipi/dc/gui`, `kipi/dc/viz`, `kipi/dc/report`);
the hands-on findings from a real-board session will be added below.

**Layout and workflow.** One main window (`kipi/gui/main_window.py`, 2.6 k lines): toolbar
with DC/AC/Noise mode switch; central 3D view tab; left dock with nets, copper layers and an
info box; right dock with the setup panel (VRMs / Loads / Series links / Solve / Mesh /
Thermal tabs, `kipi/dc/gui/pi_panel.py`, 2.8 k lines); bottom log dock. Flow: open board
(loads `<board>.kipi-setup.yml` if present) → tick nets → define VRMs and loads → *Solve
all* → pick group and field in the results box.

**Defining sources and loads.** Form-based only: a pin picker of *net → part → pin numbers
typed as text*. Parts **cannot be picked in the 3D view**. Autofill guesses ground, input
and other-side pins; bulk input from load-model files, `.pdn.json` and HyperLynx `.bud`.

**Visualisation.** pyvistaqt (VTK) 3D scene, copper as real-Z slabs with Z exaggeration.
Fields: voltage, current density, power density, temperature, via current. Colour maps:
`turbo` for voltage, `inferno` for current/power density (clipped at the 99.5th percentile;
power density on a 4-decade log scale), `viridis` for via current. Flow arrows on a fixed
grid, length ∝ √|J|; no DC streamlines. Probing is **click-only** (no hover): the info box
shows V, deviation, |J|, power, pad/via data. No 2D result view (2D exists only for the
stackup cross-section and the power-tree diagram).

**Results.** Tables for Loads (PASS/FAIL on the worst pin, margin in red), VRMs (current
limit), Series links, Vias (user limit), Losses, Thermal, Notes (warnings). HTML report with
Plotly 3D and matplotlib per-layer maps; PDF via Qt; CSV and VTK exports.

**Coupling with the core.** The GUI calls `solve_tree(snap, setup, progress)`
(`kipi/dc/pi/tree.py`) or `solve_electrothermal`, then `build_scenes(tree, snap)`
(`kipi/dc/viz/results.py`), which returns renderer-independent arrays per layer (points,
triangles, node voltages, per-triangle |J|) with a `probe()` helper. A new front-end could
consume these directly. Logic it would have to re-create: parts-on-net lookup, new VRM/link
defaults, colour clipping rules, pad/via/track hit-testing, group roles.

**Gaps that matter for Coppernicus' goal ("show where and why it fails"):**

- A failing load in the table cannot be located on the board (double-click only switches
  group); setup markers are hidden while results are shown.
- Per-pin voltages exist but only reach the report/CSV, not the UI.
- Drop (mV below the source) is computed per node but is not a selectable field.
- One net group is shown at a time; nothing ranks problems across the whole board.
- Scales are inconsistent between the GUI (full range) and the HTML report (1st–99th
  percentile).
- The 3D view is powerful but heavy for reading a PCB; there is no plain per-layer 2D map.
- Two GUI files above 2.6 k lines with heavy signal/timer wiring; parts of the help are stale
  (it says *Solve* saves the setup, the code no longer does).

**Hands-on notes:** _pending_.

## 6. AC and coupled noise (quick pass)

Read from module docstrings; not used interactively. KiPI's AC and noise accuracy tests
(`tests/accuracy/test_ac_accuracy.py`, `test_noise_accuracy.py`) were run here and pass.

**AC — PDN impedance** (`kipi/ac`, ~350 KB). One rail against its ground per solve.
- 2.5D AC FEM on the **same triangle meshes** as DC (`ac/solver/fem.py`): each copper sheet
  has two faces coupled by a 2×2 sheet impedance (DC resistance at low frequency, skin
  effect at high frequency, plus μ₀h/2 cavity inductance to the facing copper), capacitance
  between facing rail and ground copper, plated barrels.
- Sweep (`ac/solver/sweep.py`): factorise the copper at a few points per decade to get the
  board port matrix, interpolate jω·Z_b, then terminate ports with the parts at every
  frequency; impedance at each probe by Schur complement.
- Parts: capacitor models (C, ESR, ESL, DC-bias derating, aging, tolerance, SPICE
  subcircuit), ferrites, package N-port Touchstone models; VRMs carry no impedance in the
  sweep.
- Target impedance from a transient spec (Z = V_ac / I_step up to f = 1/(π·t_rise)) or a
  custom mask; estimated transient response with the VRM modelled as a PI-compensated
  transconductance amplifier.
- Validation boards: plane capacitance and first cavity resonance, decap series and
  anti-resonance, ferrite filter.

**Coupled noise** (`kipi/noise`, ~410 KB). A buck/boost as aggressor: switch-node,
inductor and capacitor ripple waveforms from the layout, coupled onto named victim nets by
capacitance, mutual inductance and shared ground; Biot–Savart H-field and E-field maps;
victims checked against logic-family noise margins.

**Relevance.** This already covers the "later" items of the Coppernicus roadmap (decoupling,
target impedance, DC-DC ripple). Reusing KiPI's core would bring them in without a new
solver; reimplementing would mean a second, substantially harder FEM (complex, frequency-
dependent, two-sided sheets).

## 7. Licensing (verified from primary sources)

| Component | License | Consequence for Coppernicus |
|---|---|---|
| KiPI | GPL-3.0-or-later | Any code reused or ported → Coppernicus is GPL-3 when distributed. Internal use: no obligation. |
| `triangle` (Python wrapper) | LGPL-3.0 | — |
| Triangle C code (bundled, J. R. Shewchuk) | custom notice, quoted below | Our reading: an internal engineering tool falls under "institutional use"; a distributed commercial build would need the author's consent or another mesher. |
| gmsh | GPLv2+ (PyPI metadata) | Not a license-free alternative. |

Triangle's notice (`README` of the bundled C sources, drufat/triangle-c):

> These programs may be freely redistributed under the condition that the copyright notices
> […] are not removed, and no compensation is received. Private, research, and institutional
> use is free. […] Distribution of this code as part of a commercial system is permissible
> ONLY BY DIRECT ARRANGEMENT WITH THE AUTHOR. (If you are not directly supplying this code to
> a customer, and you are instead telling them how they can obtain it for free, then you are
> not required to make any arrangement with me.)

Reading KiPI's source closely is not a clean-room process; reimplementing the physics is
different from porting the code, but this is not legal advice. Every idea taken from KiPI
should be noted with the file it came from.

## 8. Implications for the gate

| | Branch A — new front-end on KiPI's core | Branch B — reimplementation |
|---|---|---|
| Physics | validated today (accuracy suite passes) | to be written and re-validated |
| Import | broad `.kicad_pcb` coverage + ODB++/HyperLynx | to be written (or same ideas re-coded) |
| AC / ripple roadmap | already in the core | a second, much harder solver |
| Where our effort goes | the UI and visualisation, i.e. the stated goal | mostly re-creating what exists |
| License | GPL-3 if distributed; Triangle needs consent for commercial distribution | free choice, but mesher licences still apply (Triangle / gmsh) |
| Upstream dependency | yes: pin a version, track releases | none |
| Indicative effort to a usable DC app | 2–4 weeks | 4–6 weeks (DC only) |

**Provisional recommendation: branch A** (final after the hands-on notes and a decision on
license intent), with two safeguards:

1. **A thin engine adapter** (`coppernicus/engine/`) is the only code that imports `kipi`.
   It exposes what the UI needs (open board → snapshot; build setup; solve; per-layer result
   arrays; probes). The UI never imports `kipi` directly, so the engine can be swapped later
   (for example for a reimplementation if licensing ever requires it).
2. **Pin KiPI** to a release (a git tag) and track upstream changes deliberately; KiPI is
   very active (1.0.1 released days before this analysis).

**What Coppernicus adds** (from the gaps in section 5): a per-layer 2D view as the main
view; terminals picked by clicking pads on the board; a board-wide ranked problems panel
that zooms to each failure; drop (mV below the source) and per-pin voltages as first-class
fields; consistent colour scales; hover probe; drop breakdown by layer/segment/via;
before/after comparison; surfacing the importer's silent assumptions as visible warnings.

**Open items before deciding:**

- Hands-on notes from a real-board session (section 5).
- Whether `kipi` installs cleanly as a library dependency (its `pyproject.toml` pulls PySide6
  and PyVista/VTK even when the UI is not used) or is better vendored as a pinned fork.
- License intent for Coppernicus (internal tool only, or ever distributed).
