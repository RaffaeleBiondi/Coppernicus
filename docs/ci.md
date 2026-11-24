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
`max_current_density`, `max_via_current` and, for a ground net solved on its own,
`max_ground_rise_mv`; the summary names the ones that were set ("ground rise ≤ 10 mV").

A rail with no loads is shown as "skipped: no loads". In a power tree this is a rail that only
feeds a regulator's input. It is not an error and does not fail the check.

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
| `python-version` | `3.12` | Python used to run Coppernicus (3.12 or 3.13). |
| `uv-version` | latest | uv version to install. |
| `upload-artifact` | `true` | Upload the results folder as an artifact. |
| `artifact-name` | `coppernicus-results` | Its name. |

## Outputs

| Output | Meaning |
|---|---|
| `verdict` | `pass`, `fail`, `not_judged`, `pass_partially_judged` or `error`, over every project |
| `exit-code` | `0` no limit exceeded, `1` a limit exceeded, `2` an error |
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
- **Privacy.** The board is processed on the runner of your repository's workflow. The results
  stay in that run's artifacts. Nothing is sent anywhere else.
- **Self-test.** This repository runs the action on the synthetic demo at every push
  (`.github/workflows/action-selftest.yml`). It checks that the demo's tight 0.25 % budget fails
  the step and that `examples/demo/demo-relaxed.coppernicus.yml` (1 %) passes.
