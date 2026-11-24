<p align="center">
  <img src="Logo/Copper%20Circuit%20Orbit%20Logo%20NEW.png" alt="Coppernicus logo" width="180">
</p>

<h1 align="center">Coppernicus</h1>

<p align="center">
  <em>See where your copper fails: a standalone DC power-integrity analyzer for KiCad PCBs.</em>
</p>

<p align="center">
  <img alt="Status: pre-alpha" src="https://img.shields.io/badge/status-pre--alpha-orange">
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776ab">
  <img alt="Tested on macOS" src="https://img.shields.io/badge/tested%20on-macOS-blue">
  <img alt="License: GPL-3.0-or-later" src="https://img.shields.io/badge/license-GPL--3.0--or--later-green">
</p>

---

> [!IMPORTANT]
> **Pre-alpha.** It works end to end, and the screens below are real, but the program is young.
> Check its results against your own judgement before you rely on them.

<p align="center">
  <img src="docs/images/guide-3-drop.png" alt="Coppernicus showing the voltage-drop map of a synthetic demo board, with the problems ranked underneath" width="900">
</p>

## What it does

You have a KiCad board and a power net, say +3V3. Coppernicus tells you:

- **how much voltage each chip really gets**, after the copper between the regulator and the
  chip has eaten its share;
- **where the copper is crowded**: thin necks, narrow tracks, overloaded vias;
- **why**: which layer loses the most, which spot, which via.

It solves the real copper geometry on every layer (planes, pours, tracks, pads and vias) and
paints the results on the board. It is a **standalone program**: it reads the `.kicad_pcb` file
directly, never changes it, and does not need KiCad to be running.

All the pictures on this page come from the synthetic demo board in `examples/demo`.

## Install

You need [uv](https://docs.astral.sh/uv/). It installs Python and everything else, solver
included.

```sh
git clone git@github.com:RaffaeleBiondi/Coppernicus.git
cd Coppernicus
uv sync
uv run coppernicus            # opens the app
```

**On macOS you can get a double-clickable app.** Run `tools/make_mac_launcher.sh`. It builds
`~/Applications/Coppernicus.app` with the project icon, ready for the Dock or Launchpad. The app
starts Coppernicus from your copy of the repository, so it always runs the latest version you
have pulled. It works only on the machine where you built it. Rebuild it if you move the folder.

## Use it in six steps

Try it on the demo first: **Open project** → `examples/demo/demo.coppernicus.yml`.

### 1. Open a board and choose a net

Click **Open board** and choose a `.kicad_pcb` file. The list on the left shows the rails: the
power and ground nets, recognised by their names and copper pours. Untick *Rails only* to see
every net. If a rail is missing, right-click its net and choose **Mark as rail**; the choice is
saved in the project. Click the net you want to analyse.

### 2. Tell it where the current enters and leaves

The **source** is where the current enters, usually a regulator output. The **loads** are the
chips that draw the current. In the *Rail setup* panel on the right, press **Pick on board**
next to *Sources* and click the regulator's output pad on the board. Then press **Pick on
board** next to *Loads* and click the pads of each chip. Press **Esc** when you are done.

<p align="center">
  <img src="docs/images/guide-1-pick.png" alt="Picking pads on the board: the source pad is outlined in green, a load pad in red, the other pads of the net in brown" width="800">
</p>

Pads of the net are outlined while you pick. The source is green, the loads are red. A click on
a pad of another net is refused, and the status bar says why. You can also add rows with
**Add** and choose the part from a list.

### 3. Type the values

Give the source its voltage and each load its current. A load's current is split equally across
the pins you picked. To use every pin of a part, write `all`.

<p align="center">
  <img src="docs/images/guide-2-setup.png" alt="The rail setup panel: source J1 at 3.3 V, loads U1, U2, U3 with their currents, a 0.25 % drop limit" width="420">
</p>

**Limits are optional, and there are no default limits.** Without limits, Coppernicus ranks what
it finds but never says pass or fail. If you set a limit, anything beyond it is marked
**FAIL**. The limits are maximum drop (%), maximum current density (A/mm²) and maximum current
per via (A).

### 4. Solve and read the drop map

Press **Solve** (or **F5**). The board turns into a map of **how many millivolts each point is
below the source**. The view opens on the layer that loses the most power, and the *Layer* menu
shows the others. Drag to pan, use the wheel to zoom, and press **Fit** to see the whole board.

Tick **Isolines** to draw lines of equal drop, at the same levels on every layer, and
**Arrows** to show which way the current flows. A longer arrow means more current. In the
picture at the top, the slot in the plane forces the current through the neck below it, so the
loads on the far side get less voltage.

### 5. Find where the copper is crowded

Set *Show* to **Current density (A/mm²)**. Bright areas carry a lot of current through little
copper. **Double-click** anywhere to read the voltage, the drop and the current density at that
point in the status bar.

<p align="center">
  <img src="docs/images/guide-4-current.png" alt="Current-density map: the copper at the end of the slot is bright; the status bar shows the values at a double-clicked point" width="800">
</p>

To measure the voltage difference between two points, press **Measure ΔV** and click point A,
then point B. The status bar shows ΔV A→B and both voltages. Press **Esc** to stop.

<p align="center">
  <img src="docs/images/guide-6-measure.png" alt="Measuring ΔV between the source pad and U1's pad: the status bar shows 9.6 mV" width="800">
</p>

### 6. Read the results

The *Results* panel at the bottom has three tabs. **Problems** lists, in order:

- every load, with the voltage it gets;
- the most crowded spots;
- the vias that carry the most current.

Click a row to zoom to it. The first line sums the rail up: worst load, total copper loss and
where it is lost (per layer and in the vias). Underneath, the **assumptions** of the solve are
listed: via plating, copper temperature and mesh size.

<p align="center">
  <img src="docs/images/guide-5-problems.png" alt="The Problems panel: loads with their drop, crowding spots, vias, and the assumptions of the solve" width="900">
</p>

> The two FAIL rows come from the demo project's own limit, a 0.25 % maximum drop, which is
> deliberately tight. Coppernicus has no built-in thresholds.

**Loss by layer** draws the copper loss of each layer as a bar chart.

**Compare** shows what a change does. Press **Pin as baseline** (Ctrl+B), change something (a
current, the via plating, or the board itself in KiCad), and solve again. The tab lists the
change in drop of every load and in loss per layer. *Show → Δ drop vs baseline* paints where
the drop grew (red) or shrank.

**Export report** (Ctrl+R) writes the offline HTML report described under
[Command line](#command-line), to send to someone who does not have Coppernicus.

Finally, **Save project**. It writes a small `*.coppernicus.yml` file with the board path, the
rails, their sources, loads and limits. Open it again later, or use it from the command line.

## What the numbers mean

- **Drop**: source voltage minus the voltage at a point, in mV. For a load, the worst of its pins.
- **Current density |J|**: current per copper cross-section, in A/mm². A crowding spot is ranked
  by its average |J|. The peak value at a sharp inner corner depends on the mesh, so treat it as
  a hint, not a number.
- **Via current**: the current through one plated hole. The barrel is modelled with the plating
  thickness listed in the assumptions.
- **Copper loss**: I²R power lost in the copper, per layer and in the vias.

How hot a track gets for a given current depends on the copper, the layer, the board and the
airflow. Coppernicus does not guess it for you. Set the limits your design rules call for.

## Command line

Everything the app solves can also be run without a window, in scripts or in CI:

```sh
uv run coppernicus nets board.kicad_pcb                  # power and ground nets
uv run coppernicus parts board.kicad_pcb +3V3            # parts and pins on a net
uv run coppernicus solve project.coppernicus.yml         # solve every rail of a project
uv run coppernicus gui project.coppernicus.yml --solve +3V3 --field current
```

Add `--json` to `nets`, `parts` or `solve` to get one JSON document on stdout instead of text.
The `*_summary.json` file that `solve` writes holds the same document.

`solve` prints a report per rail and writes a PNG map per layer plus a JSON summary into
`results/`, next to the project file. It exits with **0** when everything is within your
limits, **1** when a limit is exceeded and **2** on an error.

**HTML report.** Add `--html` to `solve` (and `--open` to open it straight away). Coppernicus
writes one offline HTML file next to the summary. It holds every rail and copper layer as voltage
drop, voltage or current density, with zoom, pan and a readout under the mouse, plus the problems
table (click a row to zoom), the worst values per rail and the assumptions. It needs no network,
so you can send it to a colleague. On a 10-layer board with four rails it weighs about 6 MB.

```sh
uv run coppernicus solve project.coppernicus.yml --html --open
```

<p align="center">
  <img src="docs/images/report-demo.png" alt="The offline HTML report of the demo board: summary cards, the drop map with sources, loads and hotspots, and the hover readout" width="800">
</p>

## Use it from an AI agent

`coppernicus mcp` is an [MCP](https://modelcontextprotocol.io) server over stdio. With it,
Claude Code, Claude Desktop or another MCP client can open a board, solve rails, read the drop
and current density, render maps, measure a cross-section and compare what-ifs. An agent that
only has a shell can use `--json` on `nets`, `parts` and `solve` instead. Registration, the
tools, conventions and a worked example are in [docs/agents.md](docs/agents.md).

For Claude Code there is also a skill that teaches the agent how to use these tools well. It
shows the order of the calls and the answer to give: numbers, then a verdict only against your
limits, then where and why. It also tells the agent never to invent thresholds. Install it
once:

```sh
mkdir -p ~/.claude/skills
cp -r skills/analyzing-pdn-with-coppernicus ~/.claude/skills/
```

It starts by itself when you ask Claude whether a rail holds, about voltage drop, current
density, via current or copper heating.

## Project file

The app writes the project file for you, and it is also easy to edit by hand:

```yaml
board: demo.kipi.json.gz          # relative to this file; a .kicad_pcb works the same way
settings:
  max_edge_mm: 0.5                # largest mesh edge on planes
  via_plating_um: null            # null = engine table (IPC-6012 Class 2 minimum average)
  copper_temp_c: 20.0             # resistivity is referenced to 20 °C
rails:
- net: +3V3
  sources:
  - name: Supply
    ref: J1
    pins: all                     # or a list of pad numbers; globs like "A*" work
    voltage: 3.3
  loads:
  - ref: U1
    pins: all
    current: 2.0                  # split equally across the pins
  limits:
    max_drop_pct: 0.25            # optional; also max_current_density, max_via_current
```

## Status

| Feature | Status |
|---|---|
| Voltage-drop, voltage and current-density maps, one layer at a time | ✅ |
| Problems panel: loads ranked by drop, crowding spots, loaded vias; click to zoom | ✅ |
| Pass/fail only against limits you set | ✅ |
| Copper loss per layer and in the vias | ✅ |
| Net ties found automatically and solved as shorts | ✅ |
| Sources and loads picked by clicking pads ("Pick on board"); pads of another net are refused with a message | ✅ |
| Double-click readout of V, drop and \|J\| | ✅ |
| Drop isolines (the same levels on every layer) and current-flow arrows (length ∝ √\|J\|) | ✅ |
| ΔV between two clicked points ("Measure ΔV") | ✅ |
| Nets marked as rails by hand, saved in the project (right-click a net) | ✅ |
| Command line with exit codes for scripts and CI | ✅ |
| AI-agent interface: MCP server and `--json` on the CLI ([guide](docs/agents.md)) | ✅ first version (registration in a real Claude host not yet tried) |
| Offline interactive HTML report (`solve --html`, MCP `report` tool) | ✅ (tested in Chromium and Chrome; Safari and Firefox not yet) |
| GitHub Action for KiCad repositories | ⏳ next |
| Before/after comparison: "Pin as baseline", Compare tab (per-load, total and per-layer deltas, settings that changed), "Δ drop vs baseline" map | ✅ |
| Copper loss by layer as a chart (baseline beside current) | ✅ |
| Offline HTML report exported from the app (Export report…, Ctrl+R) | ✅ |
| Readout while hovering (today: double-click) | ⏳ |
| Power tree with ground return, board temperature | ⏳ (KiPI's engine has both) |
| AC impedance with decoupling capacitors, DC-DC ripple, tolerance budget | ⏳ (KiPI's engine has them) |
| Standalone `.app` / `.exe` that needs no uv | ⏳ later |

Tested on macOS. Windows is a target but has not been tried yet. The engine also reads ODB++
and HyperLynx files, which have not been tried through Coppernicus yet.

## More documentation

- [How it works](docs/technical.md): architecture, physics, validation, repository layout.
- [Stage-1 analysis of KiPI](docs/kipi-analysis.md), the engine underneath.
- [Workstreams](docs/workstreams.md) and the [roadmap plan](docs/superpowers/plans/2026-11-24-coppernicus-roadmap.md).

## Acknowledgements

- **[KiPI](https://github.com/drewsum/KiPI)** by Drew Maatman (GPL-3.0-or-later). Its engine
  does the import, meshing and solving.
- **[padne](https://github.com/atx/padne)** by Josef Gajdusek (GPL-3.0): KiCad-native 2.5D FEM
  DC voltage-drop solver. Prior art.
- KiPI meshes with [Triangle](https://www.cs.cmu.edu/~quake/triangle.html) by J. R. Shewchuk,
  whose notice reads in part: *"Private, research, and institutional use is free. […]
  Distribution of this code as part of a commercial system is permissible ONLY BY DIRECT
  ARRANGEMENT WITH THE AUTHOR."* The full text is quoted in the [analysis](docs/kipi-analysis.md).

## License

Coppernicus is free software under the **GNU General Public License v3.0 or later**. See
[LICENSE](LICENSE).
