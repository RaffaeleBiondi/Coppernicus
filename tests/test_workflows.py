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
