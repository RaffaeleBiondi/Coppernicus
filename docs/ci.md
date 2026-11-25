# Coppernicus in CI

Coppernicus ships a GitHub Action (`action.yml` at the root of this repository). It runs the
DC power-integrity check on a KiCad repository at every push or pull request:

- it solves the rails of your Coppernicus project files;
- it publishes the maps and an interactive HTML report;
- it writes a summary on the run's page;
- it fails the check when a limit you wrote in a project file is exceeded.

Sources, loads and limits come from the project file (`*.coppernicus.yml`). You write it by
hand or with the app; its format is in the [README](../README.md). Keep the project file in
the repository next to your design. Its `board:` path is relative to the project file.

## Use it

```yaml
# .github/workflows/power-integrity.yml in your KiCad repository
name: Power integrity
on: [push, pull_request]
permissions:
  contents: read
jobs:
  pdn:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: RaffaeleBiondi/Coppernicus@main   # better: a release tag or a commit SHA
        with:
          project: hardware/*.coppernicus.yml
```

The check fails when a rail exceeds a limit set in its project file. The run's summary page
then shows:
- one row per rail: worst load, largest drop in mV and %, copper loss and verdict;
- the top problems of each rail;
- the assumptions.

The results (PNG maps, HTML report, JSON) are attached to the run as the
`coppernicus-results` artifact.

Without limits in the project file, nothing is judged: the numbers are reported and the check
passes. Coppernicus has no built-in thresholds. The limits are `max_drop_pct`,
`max_current_density`, `max_via_current` and, for a net at 0 V (a ground),
`max_ground_rise_mv`; the summary names the ones that were set ("ground rise ≤ 10 mV").

A rail is "NOT JUDGED" when a limit you set cannot judge something: a drop limit on a load of
a 0 V net (use `max_ground_rise_mv` there) or on a load with no voltage at its pins, a limit
of the wrong kind for the net (`max_ground_rise_mv` judges nets at 0 V only, `max_drop_pct`
nets with a nonzero nominal; a rise limit on a supply rail used to fail and is now not
judged), or a rail that was not solved (skipped), whose every limit is listed. The summary
lists each item with its reason, and the overall verdict is then `not_judged`. **This fails
the check** (exit code 3), so a limit you wrote never passes silently; set
`fail-on-not-judged: false` to report it in the job summary only. A limit exceeded elsewhere
gives exit code 1 instead. With no limit at all nothing is judged and nothing fails (exit
code 0). A via or current-density limit with nothing to judge (no vias, say) passes.

A rail that feeds a regulator's input or a link (`links:`) also draws that current,
estimated at nominal voltages without copper losses, as the desktop app does. It is listed as
a load named "U1 (input, estimated)" or "FB1 (link, estimated)", and the assumptions say so.
Such a draw is judged against `max_drop_pct` like any load, and its current also flows through
the rail's copper, so the rail's other drops grow: a check that passed before this change can
now fail. A rail with no loads that feeds nothing is shown as "skipped: nothing to draw". It
is not an error and does not fail the check.

Sources, loads, converters and links can also come from fields on the parts in KiCad
(`PI_RAIL`, `PI_SOURCE`, `PI_LOAD`, `PI_KIND`, `PI_INPUT`, `PI_LINK`...; see
[docs/agents.md](agents.md#kicad-footprint-fields)). The action reads them by default, with no
extra input: a project file with only `board:` is enough. For a net the fields define, they
win over the project file's rail, which is ignored with a warning listing the differences,
unless it sets `override_board_fields: true`. Those warnings are shown in the job summary, and
the assumptions give every value with its field ("from KiCad field PI_LOAD …"). A field
Coppernicus cannot use (a value without its unit, a rail with loads but no source) fails the
step with an error naming the part and the field. To ignore the fields, set
`use_board_fields: false` in the project file.

A project file can set an optional what-if copper thickness per layer, in mm, under
`settings: copper_thickness_mm:` (for example `B.Cu: 0.07`; 1 oz is 0.035). The check then
solves with that copper instead of the board file's, and the summary's assumptions name the
overridden layers. The board file is not changed. To change the real copper weight, change
the stack-up in KiCad. A layer name the board does not have fails the step. Leave the
setting out to check the board as designed.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `project` | required | Path or glob of `*.coppernicus.yml`, relative to the repository root. Put several patterns on separate lines. `**` matches folders recursively. A path or pattern that matches nothing is an error, so a typo cannot pass unnoticed. |
| `output-dir` | `coppernicus-results` | Folder for the results, relative to the repository root. Each project gets its own subfolder. Nothing is ever written next to a board, and nothing is committed. |
| `html` | `true` | Also write the interactive offline HTML report of each project. |
| `top` | `5` | Current-crowding spots and vias listed per rail. Every load is always listed. |
| `fail-on-limit` | `true` | Fail when a limit is exceeded. Set it to `false` to report only. A project that cannot be solved fails the step either way. |
| `fail-on-not-judged` | `true` | Fail when a limit that was set could not judge something (NOT JUDGED in the summary). Set it to `false` to report only. |
| `tree` | `false` | Also solve each project's whole power tree (`coppernicus solve --tree`) and add its VRM and load tables to the job summary. The tree's verdict is shown there; it does not set the exit code, except for the board temperature of a project that enables it (see Notes). |
| `ac` | `false` | Also solve the AC PDN impedance \|Z\|(f) of each project that has probes (`coppernicus ac`). Heavy: minutes and gigabytes on a large board; its size and memory estimate is printed in the log first. Its verdict, only against the probes' own targets, sets the step's result (1 fail, 3 not judged), and the summary adds each probe's worst \|Z\| against its target. The CSV and PNG of \|Z\|(f) go into the results. |
| `python-version` | `3.12` | Python used to run Coppernicus (3.12 or 3.13). |
| `uv-version` | latest | uv version to install. |
| `upload-artifact` | `true` | Upload the results folder as an artifact. |
| `artifact-name` | `coppernicus-results` | Its name. |

## Outputs

| Output | Meaning |
|---|---|
| `verdict` | `pass`, `fail`, `not_judged`, `pass_partially_judged` or `error`, over every project |
| `exit-code` | `0` no limit exceeded (or none set), `1` a limit exceeded, `3` a limit that was set could not judge something, `2` an error (a crash included: it never exits 1). Precedence 2, 1, 3, 0. |
| `results-dir` | Absolute path of the results folder |
| `artifact-url` | Download URL of the uploaded results |

For example, to post the verdict elsewhere, give the step an `id` and read
`steps.<id>.outputs.verdict`.

## What it does

1. Installs uv and Python, then Coppernicus from the action's own checkout, with the locked
   dependencies (`uv sync --frozen`).
2. Runs `python -m coppernicus.ci solve` on the matching project files. This runs
   `coppernicus solve PROJECT --json --html` once per project, each in its own process.
3. Uploads the results folder, writes the job summary, and turns the exit code into the step's
   result.

The same helper works in any CI that has Python and uv:

```sh
uv run --project path/to/Coppernicus python -m coppernicus.ci solve --out results "hardware/*.coppernicus.yml"
uv run --project path/to/Coppernicus python -m coppernicus.ci summary --out results > summary.md
```

## Notes

- **Time.** The first run installs Coppernicus's dependencies, which include the desktop app's
  Qt and VTK. setup-uv caches them for later runs. Solving takes from seconds on small boards
  to a few minutes on large multi-rail ones.
- **Pinning.** `@main` follows this repository's latest commit. To keep a check reproducible,
  pin a commit SHA, or a release tag once there are releases.
- **Board temperature.** A project with `thermal.max_temp_c` or `max_rise_c` set needs the
  power tree to judge them: the rail-by-rail check lists each as NOT JUDGED ("solved with the
  power tree only") and exits 3, never a silent pass. With `tree: true` and `thermal: enabled:
  true` in the project, the tree run solves the temperature (heavier; its size and memory
  estimate is printed in the log first): its verdict then judges those limits and sets the
  step's result (fail 1, not judged 3), and the summary adds a "Board temperature" block with
  the hottest copper per layer.
- **Privacy.** The board is processed on the runner of your repository's workflow. The results
  stay in that run's artifacts. Nothing is sent anywhere else.
- **Self-test.** This repository runs the action on the synthetic demo at every push
  (`.github/workflows/action-selftest.yml`). It checks that the demo's tight 0.25 % budget fails
  the step and that `examples/demo/demo-relaxed.coppernicus.yml` (1 %) passes.
