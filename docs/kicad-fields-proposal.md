# Proposal: sources and loads defined in KiCad footprint fields

> **Status: proposal, not implemented.** Today sources, loads and limits come from the
> project file (`*.coppernicus.yml`), and that stays the first and complete way. This page
> describes an optional second way, to be decided by the maintainer. Nothing here is built
> yet.

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

## Open questions for the maintainer

1. The field names: the `PI_` prefix, or something more specific such as `COPPERNICUS_`, to
   avoid clashing with other tools' fields?
2. Should `PI_LIMIT_DROP` exist at all, or should limits stay only in the project file, which
   keeps every verdict in reviewed text?
3. Should the fields be on by default, or only with `use_board_fields: true`?
4. Option 1, option 2, or both?
