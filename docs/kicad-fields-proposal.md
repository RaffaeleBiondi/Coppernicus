# Proposal: sources and loads defined in KiCad footprint fields

> **Status: implemented**, with the owner's decisions at the end of this page (they replace
> the proposal where they differ: the fields are read by default, and KiCad wins over the
> project file unless a rail says otherwise). `coppernicus.engine.kicad_fields` reads the
> board file (option 1 below); `coppernicus.board_fields` builds the project's own v2
> objects from the fields and merges them. How to use them is in
> [docs/agents.md](agents.md#kicad-footprint-fields). The rest of the page is the proposal
> as it was approved.

## Why

A rail's power data lives naturally on the schematic: the regulator's output voltage on the
regulator, each chip's current on the chip. If those numbers live in the design, they cannot
drift away from it. A new board could then be checked with an almost empty project file. padne
(see the README's acknowledgements) already reads its directives from the KiCad design.

## The fields

In KiCad, these are fields on the **symbol** in the schematic. Since KiCad 8, footprints have
fields kept in sync with their symbol's fields, so the values reach the `.kicad_pcb` file that
Coppernicus reads, as `(property "NAME" "TEXT")` nodes. They are refreshed when the PCB is
updated from the schematic. They can also be set on the footprint in the PCB editor.

The source is the KiCad development news ("Each footprint's fields are synchronized with its
symbol's fields", [forum post](https://forum.kicad.info/t/post-v7-new-features-and-development-news/40144)).
KiPI's reader matches it: footprint fields are `property` nodes since KiCad 8. Confirm on a
small test project before relying on it.

Every field is optional.

| Field | Example | On | Meaning |
|---|---|---|---|
| `PI_RAIL` | `+3V3` | source, load | The net the part belongs to as a source or load. Needed when the part has pads on several power nets, as a regulator does with `VIN` and `VOUT`. Several rails: `+3V3;+1V8`. |
| `PI_SOURCE` | `3.3V` | source | This part holds the rail at that voltage: a regulator output or a connector. |
| `PI_LOAD` | `2A` | load | This part draws that current in total, split equally across its pins on the rail. |
| `PI_PINS` | `1,2` or `A*` | source, load | Which pads take part, by number or glob. Default: every pad of the part on the rail. |
| `PI_LIMIT_DROP` | `2%` | load or source | A drop limit for the rail, written by the designer. It is a user limit, so FAIL is still judged only against values someone wrote. |

**Several rails on one part.** A part can carry one value per rail, matched by position:
`PI_RAIL=+3V3;+1V8` with `PI_LOAD=1.2A;0.4A`, and `PI_PINS=3,4;7` when needed.

**Units.** Each value carries its unit: `V`, `A` or `mA`, `%`. A bare number is refused, so
"2" can never be read as 2 A when 2 mA was meant. Values use a dot for decimals; KiCad's
locale does not affect field text.

## Precedence: the project file wins

1. Coppernicus reads the fields into **default** rails: every `PI_RAIL` net with at least one
   source and one load.
2. A rail in the project file **replaces** the default rail of the same net entirely. The
   project file is explicit and reviewed, so its rail is never merged field by field with
   the board's.
3. A project file can list only `board:` and `settings:` and take all rails from the fields.
   An option such as `use_board_fields: false` turns the fields off.
4. Every result says where each rail came from, in `assumptions`: "rail +3V3 from footprint
   fields: source U3 (PI_SOURCE 3.3V), loads U1 (2A), U2 (1.5A)". The CLI's `--json` and the
   MCP tools show the same in a `from_board_fields` flag per rail.

This keeps today's behaviour unchanged when no fields are present.

## Does KiPI's importer expose footprint fields? Not today.

I checked KiPI v1.0.1, the pinned version:

- `kipi/core/model/board.py`, `class Footprint`: the snapshot keeps `id`, `reference`,
  `value`, `position`, `side`, `pad_ids`, `footprint` (the library id) and `angle_deg`. It has
  no field for other properties.
- `kipi/sources/kicad_pcb/reader.py`, `read_footprints()`: it builds each `Footprint` with
  `value=_field(fp, "Value")`. `_field(fp, name)` in the same file can read any footprint field
  by name (`(property "NAME" "TEXT")` since KiCad 8, `(fp_text ...)` before), but
  `read_footprints` only asks it for `Value`.
- `kipi/sources/kicad_ipc/extract.py` builds `Footprint` the same way, from the IPC API
  objects, with no custom fields either.

So the custom fields are parsed with the rest of the file and then dropped.

## How to read them (three options)

1. **Inside `coppernicus.engine`, from the board file.** Use KiPI's own s-expression parser
   (`kipi.sources.kicad_pcb.sexpr.parse`), and its helpers `children()` and `_field()`
   pattern, to collect `{reference: {field: text}}` from the `.kicad_pcb`. Add an additive
   `Board.part_fields(ref)`.
   - Pros: no change to KiPI. The rule that only `engine/` imports KiPI still holds.
   - Cons: a second pass over the file, with no extra cost in practice. It works for
     `.kicad_pcb` only, not for ODB++, HyperLynx or snapshots.
2. **Upstream change to KiPI.** Add `fields: Dict[str, str]` to `Footprint` and fill it in both
   KiCad readers.
   - Pros: the cleanest result, and snapshots would keep the fields.
   - Cons: it needs a KiPI release and a pin update, or a vendored patch (open decision 4 in
     the agent brief).
3. **From the schematic** (`.kicad_sch`).
   - Pros: it reads the symbol fields at the source.
   - Cons: it needs the schematic next to the board and a map from symbols to footprints.
     It is more moving parts for no gain over option 1.

**Suggested:** start with option 1, and offer option 2 upstream later.

## What else would change

- `project.py`: a function that builds default rails from `Board.part_fields`. The rail model
  itself does not change.
- The GUI: show the rails taken from fields, and offer "save to project" to make them explicit.
- The GitHub Action: no change. A project file with just `board:` would be enough.
- Tests: a small synthetic `.kicad_pcb` under `tests/boards/` with fields on two parts.
  Synthetic snapshots cannot carry fields until option 2 lands.

## Decisions (the owner's, as implemented)

1. **Names:** the `PI_` prefix. An unknown `PI_*` name (a typo such as `PI_LOADS`, or
   `pi_load` in another case) is an error, so a misspelt current is never silently missing.
2. **On by default.** The fields are read whenever a `.kicad_pcb` has `PI_*` fields; a board
   without them, or of another format, behaves as before. `use_board_fields: false` in the
   project file (saved only when off), `--no-board-fields` on `coppernicus solve`, or
   `use_board_fields: false` on the MCP `solve_project` and `solve_tree` tools turns them
   off. The file is scanned for a `PI_` property before it is parsed a second time.
3. **KiCad wins, with an explicit override.**
   - For a net the fields define, they give the design data: sources, loads, pins and
     converter data. A project-file rail of the same net is ignored, with a warning listing
     the differences ("project file rail +3V3 ignored: KiCad fields define it (KiCad U1 2 A,
     project U1 3 A); set override_board_fields: true on the rail to use the project's
     values"). An identical project rail (part by part; names, limits and remote sense are not
     compared) gives no warning.
   - `override_board_fields: true` on a project rail keeps it entirely; the result says
     `overrides_board_fields: true`, with the KiCad values it overrode.
   - A `PI_LIMIT_DROP` always applies to its net, whether the fields' rail is complete,
     incomplete or overridden (the coordinator's ruling after the review: limits are not
     what-if design data, so a project rail cannot loosen a KiCad limit). It wins over the
     project's `max_drop_pct` (a warning when they differ; the strictest field value when
     several parts give one). The other limits come
     only from the project file and apply to the fields' rail; a project rail with only
     `limits:` (no sources, no loads) adds them and is not compared. Settings stay project-only.
   - The merge is a pure function for the app: `board_fields.merge(project, fields,
     pad_nets)` returns the merged project (a copy), each net's origin (`fields`, `project` or
     `overridden`, with its notes, warnings and the fields' rail as plain data) and all the
     warnings.
4. **Converters, grounds and links in fields:** `PI_KIND` (`source`, `ldo`, `smps`),
   `PI_EFF` (`90%`, required for `smps`), `PI_IQ` (`2mA`, `50uA`), `PI_INPUT` (required for
   `ldo` and `smps`) with `PI_INPUT_PINS`, `PI_GND` (on sources and loads) with
   `PI_GND_PINS`, and `PI_LINK` (`50mOhm`) on a part whose pads are on exactly two nets. They
   map onto `Source.kind`, `efficiency`, `quiescent_a`, `input_*`, `ground_*`,
   `Load.ground_*` and `Link`, so the engine is unchanged; the power-chain example described in
   fields gives the same objects as its project file, remote sense aside. **Remote sense
   stays in the project file** for now: when the fields replace a project rail, its sources'
   sense pins are carried over by part. A `PI_LINK` part replaces a project link on the same
   part (a warning when they differ; links have no override yet). A regulator that also has a
   `PI_LOAD` on its `PI_INPUT` net is refused (counted twice).
5. **Completeness is per group of nets joined by links and net ties.** A rail with a source
   and no load is complete when it feeds a regulator's input or a link (from the fields or the
   project): it draws their estimated current. Loads on a net a net tie joins to a driven net
   go into that rail, even when the project file has a rail on the tied net that holds only
   limits (it stays, limits only); loads behind a link on a net with no source make a
   loads-only rail (per rail it is skipped, its loads drawn through the link by the driven
   rail; the tree solves it). A net with loads and no source in its group, or a source
   feeding nothing, is an error naming the missing field, unless the project file defines
   the rail; then that rail is used,
   with a warning that the fields are incomplete. Pins are compared as the pads they select.
   A footprint with `PI_*` fields and no reference is an error that gives its position.
6. **`coppernicus solve --tree`** reads the fields too, as the MCP `solve_tree` tool does.
7. Confirmed from the first version: `PI_RAIL` is always required next to a per-rail field
   (nothing is guessed from net names); values per rail by position, `;`-separated, an empty
   one skips a rail, a count mismatch is an error; units required (`V`; `A`, `mA`; `%`;
   `uA`/`µA` for `PI_IQ`; `Ohm`, `mOhm`, `Ω` for `PI_LINK`); two footprints with the same
   reference carrying fields are an error; a malformed field fails the run, and its hint names
   the off switch. A footprint marked "do not populate" is read like any other.
8. **Not yet:** the desktop app does not read the fields (the peer will call the merge).
   The test board is a synthetic `.kicad_pcb` written by `tests/kicad_fixture.py`.

## Second step (library-level fields, external inductors, multi-net links)

Decided by the owner for annotating a real KiCad library, additive:

- `PI_KIND`, `PI_EFF`, `PI_IQ`, and the new `PI_VOUT` (fixed output voltage), `PI_IMAX`
  (rated output current) and `PI_FSW` (fixed switching frequency) are library-level: they need
  no `PI_RAIL`, and a part carrying only them is part data. With `PI_RAIL`, one value applies
  to every rail where the part is a source, or one per rail with `;`. `PI_VOUT` is the source
  voltage when the instance has no `PI_SOURCE`; when both differ, `PI_SOURCE` wins with a
  warning. `PI_IMAX` becomes `Source.current_limit_a`, shown next to I_out and not judged yet;
  `PI_FSW` becomes `Source.fsw_hz`, stored for later use.
- `PI_SOURCE_REF=<ref>` on an external-inductor buck's IC names the part whose pads hold the
  rail (the inductor); `PI_PINS` selects that part's pads. It maps to
  `Source(ref=L1, name=U1, input ref=U1, ground ref=U1)`; the board reader also reads the pads
  of the named part, and a named part with no pad on the rail is an error.
- `PI_LINK_PINS="1,2;5"` chooses a link's two sides on a part with more than two nets (load
  switch, MOSFET, 3-pin fuse); each side's pads are on one net. Two-net parts keep working
  without it.

Everything maps onto existing or additive project objects; the engine path is unchanged.
