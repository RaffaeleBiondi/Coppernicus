"""Before/after of two solves of a rail in the app: plain logic, no window.

The comparison itself is the service layer's (:func:`compare_summaries`), fed with the same
summaries the service builds (:func:`rail_summary`), so the app, the CLI and the MCP tools
report the same deltas.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict

from coppernicus.problems import drop_words, find_problems
from coppernicus.project import Rail, Settings
from coppernicus.results import RailResult
from coppernicus.service.convert import rail_summary
from coppernicus.service.session import compare_summaries


@dataclass
class Run:
    """One solve as the app keeps it: the result and what produced it."""
    result: RailResult
    rail: Rail
    settings: Settings
    board: str                      # which board file (and version) it was solved on

    def __post_init__(self):        # the user may edit the rail and settings afterwards
        self.rail, self.settings = copy.deepcopy(self.rail), copy.deepcopy(self.settings)


def run_summary(run: Run, label: str) -> Dict[str, Any]:
    problems = find_problems(run.result, run.rail.limits)
    return rail_summary(label, run.board, run.result, run.rail, problems, run.settings, {}, {})


def largest_drop_label(baseline: RailResult, current: RailResult) -> str:
    """The Compare row label of the rail's largest drop, naming the side of its reference
    as the summaries do (:func:`coppernicus.problems.drop_words`): "Largest drop on the
    rail, mV below 3.3 V", "..., mV above -3.3 V (toward 0 V)", "Largest ground rise on the
    rail, mV above 0 V". It names what a positive value means (the cells are signed), from
    the current run; a baseline with another reference is named after it:
    "... mV below 5 V (baseline: drop below 3.3 V)"."""
    def words(res: RailResult):
        return drop_words(1.0, res.v_ref, res.sign, res.is_rise())
    what, where = words(current)
    label = f"Largest {what} on the rail, mV {where}"
    if words(baseline) != (what, where):
        label += " (baseline: {} {})".format(*words(baseline))
    return label


def compare_runs(baseline: Run, current: Run) -> Dict[str, Any]:
    """Deltas current − baseline (per load, per layer, settings that changed), as
    :func:`compare_summaries` returns them, with ids "baseline" and "current", plus
    ``max_drop_label`` (:func:`largest_drop_label`) for the Compare tab."""
    out = compare_summaries(run_summary(baseline, "baseline"), run_summary(current, "current"))
    out["max_drop_label"] = largest_drop_label(baseline.result, current.result)
    return out
