"""The CI workflows say what the owner decided: Windows is a required platform; and which coppermesh
each job tests (this repository's tools/mesher_rs, or PyPI's wheel)."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_windows_tests_are_required():
    jobs = yaml.safe_load((WORKFLOWS / "tests.yml").read_text(encoding="utf-8"))["jobs"]
    assert not any(job.get("continue-on-error") for job in jobs.values())
    assert not any("informative" in str(job.get("name", "")) for job in jobs.values())
    oses = [os for job in jobs.values() for os in job.get("strategy", {}).get("matrix", {}).get("os", [])]
    assert {"ubuntu-latest", "macos-latest", "windows-latest"} <= set(oses)
    # A hung job must not hold a required check for GitHub's 6 h default; the Windows runner
    # needs about 20 minutes for the whole suite, so 40 leaves room for it to grow.
    assert all(job.get("timeout-minutes") == 40 for job in jobs.values())


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


def _runs(job):
    return [str(s.get("run", "")) for s in job["steps"]]


def test_the_pytest_jobs_test_this_repositorys_mesher_and_one_job_pypis_wheel():
    """uv.lock names PyPI's coppermesh. The pytest jobs (and the Triangle-free one) sync without
    it, build tools/mesher_rs into the environment, prove it with check_source.py, and never run
    a sync after that (the wheel would come back silently: both are coppermesh 0.1.0). The job
    pypi-wheel installs as users do, uv forbidden to build coppermesh, and proves the wheel; the
    job no-rust leaves coppermesh out."""
    doc = yaml.safe_load((WORKFLOWS / "tests.yml").read_text(encoding="utf-8"))
    assert doc["env"]["COPPERNICUS_REQUIRE_RUST"] == "1"
    jobs = doc["jobs"]
    free = yaml.safe_load((WORKFLOWS / "triangle-free.yml").read_text(encoding="utf-8"))["jobs"]["test"]
    for job in (jobs["test"], free):
        runs = _runs(job)
        sync = next(i for i, r in enumerate(runs) if "uv sync" in r)
        build = next(i for i, r in enumerate(runs) if "uv pip install" in r and "./tools/mesher_rs" in r)
        assert "--no-install-package coppermesh" in runs[sync] and sync < build
        assert "tools/mesher_rs/check_source.py" in runs[build].split("./tools/mesher_rs", 1)[1]
        after = [line for r in runs[build:] for line in r.splitlines()]
        assert not any("uv sync" in line for line in after)
        assert all("--no-sync" in line for line in after if "uv run" in line)
    assert any("pytest" in r for r in _runs(jobs["test"]))
    wheel = jobs["pypi-wheel"]
    assert "COPPERNICUS_REQUIRE_RUST" not in wheel.get("env", {})          # the workflow's "1"
    runs = _runs(wheel)
    assert any("uv sync --frozen --no-build-package coppermesh" in r for r in runs)
    assert any("check_source.py --pypi" in r for r in runs)
    assert not any("./tools/mesher_rs" in r for r in runs)                  # no source build here
    no_rust = jobs["no-rust"]
    assert no_rust["env"]["COPPERNICUS_REQUIRE_RUST"] == ""
    assert any("uv sync --frozen --no-install-package coppermesh" in r for r in _runs(no_rust))
