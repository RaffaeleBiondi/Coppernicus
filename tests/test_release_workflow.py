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


def test_the_build_scripts_keep_triangle_out():
    for script in ("build_macos.sh", "build_windows.ps1"):
        text = (ROOT / "packaging" / script).read_text(encoding="utf-8")
        assert "--no-install-package triangle" in text and "check_bundle.py" in text
    spec = (ROOT / "packaging" / "coppernicus.spec").read_text(encoding="utf-8")
    assert '"triangle",' in spec                                # in PyInstaller's excludes
