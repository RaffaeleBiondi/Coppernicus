"""The coppermesh wheels workflow builds and checks on every run, and publishes only where it is
told to: TestPyPI from a coppermesh-v* tag or a manual run with publish=testpypi, PyPI only from a
manual run with publish=pypi (on main or such a tag) through the environment pypi; Trusted
Publishing alone (id-token only on the publish jobs, no token secret); every action pinned."""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "wheels.yml"
PUBLISH = {"publish-testpypi": "testpypi", "publish-pypi": "pypi"}


def _doc():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc):
    return doc[True] if True in doc else doc["on"]          # YAML 1.1 reads "on" as True


def _runs(job):
    return [str(s.get("run", "")) for s in job["steps"]]


def _uses(job):
    return [s["uses"] for s in job["steps"] if "uses" in s]


def _starts_with(text, prefix):
    return text.startswith(prefix)


def _evaluate(condition, event_name, ref, publish):
    """A job's ``if:`` for one run, with the few expression forms the workflow uses (==, &&,
    ||, parentheses, startsWith); anything else fails the translation."""
    expr = condition.replace("&&", " and ").replace("||", " or ").replace("startsWith(", "_starts_with(")
    names = {"github.event_name": repr(event_name), "github.ref": repr(ref), "inputs.publish": repr(publish)}
    for name, value in names.items():
        expr = expr.replace(name, value)
    leftover = re.sub(r"'[^']*'|\band\b|\bor\b|_starts_with|==|[(),\s]", "", expr)
    assert leftover == "", f"unexpected expression in {condition!r}: {leftover!r}"
    return eval(expr, {"_starts_with": _starts_with})       # the workflow's own text, translated


def _published(event_name, ref, publish=""):
    jobs = _doc()["jobs"]
    return {index for job, index in PUBLISH.items() if _evaluate(jobs[job]["if"], event_name, ref, publish)}


def test_only_a_tag_push_or_a_manual_run_starts_it():
    triggers = _triggers(_doc())
    assert set(triggers) == {"push", "workflow_dispatch"}             # no pull_request, no schedule
    assert triggers["push"] == {"tags": ["coppermesh-v*"]}            # no branch push starts it
    publish = triggers["workflow_dispatch"]["inputs"]["publish"]
    assert publish["type"] == "choice" and publish["options"] == ["none", "testpypi", "pypi"]
    assert publish["default"] == "none"


def test_pypi_only_by_a_manual_run_with_publish_pypi():
    tag = "refs/tags/coppermesh-v0.1.0"
    assert _published("push", tag) == {"testpypi"}                    # a tag: TestPyPI, never PyPI
    assert _published("workflow_dispatch", "refs/heads/main", "none") == set()
    assert _published("workflow_dispatch", "refs/heads/main", "testpypi") == {"testpypi"}
    assert _published("workflow_dispatch", "refs/heads/main", "pypi") == {"pypi"}
    assert _published("workflow_dispatch", tag, "pypi") == {"pypi"}
    assert _published("workflow_dispatch", "refs/heads/feature", "pypi") == set()   # main or a tag
    assert _published("workflow_dispatch", "refs/heads/feature", "testpypi") == {"testpypi"}
    assert _published("push", "refs/tags/v0.4.0") == set()            # the app's tags (release.yml)


def test_trusted_publishing_only_on_the_publish_jobs_behind_their_environments():
    doc = _doc()
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False          # an upload is never cut short
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")     # no token, OIDC only
    for name, job in doc["jobs"].items():
        publishes = [u for u in _uses(job) if u.startswith("pypa/gh-action-pypi-publish@")]
        if name in PUBLISH:
            assert job["permissions"] == {"id-token": "write"}
            assert job["environment"]["name"] == PUBLISH[name]       # pypi: its required reviewer
            assert set(job["needs"]) == {"wheel", "sdist"} and job["runs-on"] == "ubuntu-latest"
            [step] = [s for s in job["steps"] if s.get("uses", "").startswith("pypa/gh-action-pypi-publish@")]
            assert "password" not in step.get("with", {})
            test_pypi = step["with"].get("repository-url") == "https://test.pypi.org/legacy/"
            assert test_pypi == (name == "publish-testpypi")
            assert step["with"].get("skip-existing", False) is (name == "publish-testpypi")
            assert not any("checkout" in u for u in _uses(job))     # uploads only the built files
        else:
            assert "permissions" not in job and "environment" not in job and not publishes


def test_three_platforms_and_the_sdist():
    jobs = _doc()["jobs"]
    matrix = jobs["wheel"]["strategy"]["matrix"]["include"]
    by_target = {m["target"]: m for m in matrix}
    assert set(by_target) == {"x86_64-unknown-linux-gnu", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"}
    assert by_target["x86_64-unknown-linux-gnu"]["manylinux"] == "2_28"
    assert by_target["x86_64-unknown-linux-gnu"]["os"].startswith("ubuntu-")
    assert re.fullmatch(r"macos-(1[4-9]|[2-9]\d|latest)", by_target["aarch64-apple-darwin"]["os"])
    assert by_target["x86_64-pc-windows-msvc"]["os"].startswith("windows-")
    assert "x86_64-apple-darwin" not in WORKFLOW.read_text(encoding="utf-8")    # no Intel macOS
    for job, command in (("wheel", "build"), ("sdist", "sdist")):
        [build] = [s for s in jobs[job]["steps"] if s.get("uses", "").startswith("PyO3/maturin-action@")]
        assert build["with"]["command"] == command and build["with"]["working-directory"] == "tools/mesher_rs"
        if job == "wheel":
            assert build["with"]["args"].startswith("--release --locked")


def test_each_wheel_and_the_sdist_s_own_wheel_are_installed_fresh_and_checked():
    jobs = _doc()["jobs"]
    for job, built in (("wheel", "coppermesh-*-cp312-abi3-*.whl"), ("sdist", "-m pip wheel --no-deps")):
        [run] = [r for r in _runs(jobs[job]) if "check_fma.py" in r]
        assert "-m venv" in run and built in run
        order = [run.index(word) for word in ("-m pip install", "import coppermesh", "check_fma.py",
                                               "check_digest.py")]
        assert order == sorted(order)
        assert any("rustup component add llvm-tools" in r for r in _runs(jobs[job]))    # its objdump
        assert any(s.get("uses", "").startswith("actions/upload-artifact@") for s in jobs[job]["steps"])


def test_every_action_is_pinned_to_a_commit():
    for job in _doc()["jobs"].values():
        for uses in _uses(job):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses


def test_the_package_and_the_crate_have_one_version():
    crate = ROOT / "tools" / "mesher_rs"
    project = tomllib.loads((crate / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    cargo = tomllib.loads((crate / "Cargo.toml").read_text(encoding="utf-8"))
    assert project["name"] == cargo["package"]["name"] == "coppermesh"
    assert project["version"] == cargo["package"]["version"]
    assert project["requires-python"] == ">=3.12" and project["license"] == "GPL-3.0-or-later"
    assert "abi3-py312" in cargo["dependencies"]["pyo3"]["features"]
