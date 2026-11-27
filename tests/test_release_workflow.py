"""The release workflow builds the packaged apps as artifacts and never publishes anything."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_builds_both_apps_as_artifacts_only():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc[True] if True in doc else doc["on"]          # YAML 1.1 reads "on" as True
    assert triggers["push"]["tags"] == ["v*"] and "workflow_dispatch" in triggers
    assert doc["permissions"] == {"contents": "read"}           # cannot create a release
    jobs = doc["jobs"]
    assert {jobs["macos"]["runs-on"], jobs["windows"]["runs-on"]} == {"macos-latest", "windows-latest"}
    for job in jobs.values():
        steps = job["steps"]
        runs = " ".join(str(s.get("run", "")) for s in steps)
        assert "build_macos.sh" in runs or "build_windows.ps1" in runs
        assert "coppernicus-site-packages" in str(steps)          # a first start is exercised
        uses = [s.get("uses", "") for s in steps]
        assert any(u.startswith("actions/upload-artifact@") for u in uses)
        assert not any("release" in u for u in uses) and "gh release" not in runs


def test_the_frozen_app_runs_every_solve_with_its_html_page():
    """Besides solve --tree: ac, noise, tolerance and solve --thermal run frozen on both
    systems, each with --html on the runner's copy of an example, its exit code checked
    (the thermal demo fails on purpose)."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        runs = [str(s.get("run", "")) for s in job["steps"]]
        for word, example, code in ((" ac ", "pdn-relaxed", 0), (" noise ", "noise", 0),
                                    (" tolerance ", "tolerance", 0), (" --thermal ", "demo-thermal", 1)):
            [run] = [r for r in runs if word in r]
            assert f"{example}.coppernicus.yml" in run and "RUNNER_TEMP" in run and "--html" in run
            assert f"(expected {code})" in run and "smoke" in run


def test_the_build_scripts_keep_triangle_out():
    for script in ("build_macos.sh", "build_windows.ps1"):
        text = (ROOT / "packaging" / script).read_text(encoding="utf-8")
        assert "--no-install-package triangle" in text and "check_bundle.py" in text
    spec = (ROOT / "packaging" / "coppernicus.spec").read_text(encoding="utf-8")
    assert '"triangle",' in spec                                # in PyInstaller's excludes


def test_the_build_scripts_bundle_coppermesh_and_check_it():
    """The rust mesher is in the packaged app (not the default mesher): the build environment
    syncs uv's "rust" group, the spec stops without its extension, check_bundle.py and the FMA
    check run on the bundle."""
    for script in ("build_macos.sh", "build_windows.ps1"):
        text = (ROOT / "packaging" / script).read_text(encoding="utf-8")
        assert "--no-dev --group rust" in text and "check_fma.py" in text
    spec = (ROOT / "packaging" / "coppernicus.spec").read_text(encoding="utf-8")
    assert '"coppermesh"' in spec and "coppermesh's extension is not in the bundle" in spec
    check = (ROOT / "packaging" / "check_bundle.py").read_text(encoding="utf-8")
    assert 'RUST = "coppermesh"' in check and "python3.dll" in check
