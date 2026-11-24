"""Before/after of two solves of a rail in the app: plain logic, no window.

The comparison itself is the service layer's (:func:`compare_summaries`), fed with the same
summaries the service builds (:func:`rail_summary`), so the app, the CLI and the MCP tools
report the same deltas.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict

from coppernicus.problems import find_problems
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


def compare_runs(baseline: Run, current: Run) -> Dict[str, Any]:
    """Deltas current − baseline (per load, per layer, settings that changed), as
    :func:`compare_summaries` returns them, with ids "baseline" and "current"."""
    return compare_summaries(run_summary(baseline, "baseline"), run_summary(current, "current"))
