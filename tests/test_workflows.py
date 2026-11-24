"""The CI workflows say what the owner decided: Windows is a required platform."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_windows_tests_are_required():
    jobs = yaml.safe_load((WORKFLOWS / "tests.yml").read_text(encoding="utf-8"))["jobs"]
    assert not any(job.get("continue-on-error") for job in jobs.values())
    assert not any("informative" in str(job.get("name", "")) for job in jobs.values())
    oses = [os for job in jobs.values() for os in job.get("strategy", {}).get("matrix", {}).get("os", [])]
    assert {"ubuntu-latest", "macos-latest", "windows-latest"} <= set(oses)
    # A hung job must not hold a required check for GitHub's 6 h default.
    assert all(job.get("timeout-minutes") == 20 for job in jobs.values())


ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def test_the_action_fails_on_a_limit_that_could_not_judge_unless_told_not_to():
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert action["inputs"]["fail-on-not-judged"]["default"] == "true"
    verdict = next(s for s in action["runs"]["steps"] if s.get("name") == "Verdict")
    assert verdict["env"]["FAIL_ON_NOT_JUDGED"] == "${{ inputs.fail-on-not-judged }}"
    assert "3)" in verdict["run"] and "FAIL_ON_NOT_JUDGED" in verdict["run"]
    assert "3" in action["outputs"]["exit-code"]["description"]


def test_the_action_can_add_the_power_tree_to_the_summary():
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert action["inputs"]["tree"]["default"] == "false"
    solve = next(s for s in action["runs"]["steps"] if s.get("id") == "solve")
    assert solve["env"]["COPPERNICUS_TREE"] == "${{ inputs.tree }}" and "--tree" in solve["run"]
