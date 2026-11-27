"""The licences of the Rust crates compiled into coppermesh reach the packaged app's notices:
packaging/licenses/coppermesh-crates.txt is what tools/mesher_rs/Cargo.lock gives, notices.py
appends it when coppermesh is bundled, and check_bundle.py fails notices that lack a crate."""

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CRATES = ROOT / "packaging" / "licenses" / "coppermesh-crates.txt"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crate_licences = _load("crate_licences", ROOT / "tools" / "mesher_rs" / "crate_licences.py")


def test_the_committed_file_was_generated_from_the_current_cargo_files():
    """Cargo.lock or Cargo.toml changed without running tools/mesher_rs/crate_licences.py."""
    recorded = crate_licences.recorded_hashes(CRATES.read_text(encoding="utf-8"))
    for name in ("Cargo.lock", "Cargo.toml"):
        data = (ROOT / "tools" / "mesher_rs" / name).read_bytes().replace(b"\r\n", b"\n")
        assert recorded[name] == hashlib.sha256(data).hexdigest(), \
            f"{name} changed: run tools/mesher_rs/crate_licences.py"


def test_every_listed_crate_is_locked_at_its_version_and_has_a_text():
    text = CRATES.read_text(encoding="utf-8")
    lock = (ROOT / "tools" / "mesher_rs" / "Cargo.lock").read_text(encoding="utf-8")
    listed = crate_licences.listed_crates(text)
    assert ("pyo3", "0.30.1") in listed and ("robust", "1.2.0") in listed and ("numpy", "0.30.0") in listed
    assert all(n != "coppermesh" for n, _ in listed)
    for name, version in listed:
        assert f'name = "{name}"\nversion = "{version}"' in lock
        assert f"  {name} {version} (" in text                 # among a licence text's users


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on PATH")
def test_regenerating_gives_the_committed_file():
    assert crate_licences.main(["--check"]) == 0


def _packaging():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        return _load("notices", ROOT / "packaging" / "notices.py"), \
            _load("check_bundle", ROOT / "packaging" / "check_bundle.py")
    finally:
        sys.path.remove(str(ROOT / "packaging"))


def test_bundled_notices_hold_every_crate_or_the_bundle_check_fails(tmp_path):
    notices, check_bundle = _packaging()
    crates = CRATES.read_text(encoding="utf-8")
    if importlib.util.find_spec("coppermesh") is None:
        pytest.skip("coppermesh not installed (uv sync --group rust)")
    out = tmp_path / "THIRD_PARTY_NOTICES.txt"
    listed = notices.write(out, ["coppermesh/coppermesh.abi3.so", "coppermesh/__init__.py"], "0.0")
    assert "coppermesh" in listed
    full = out.read_text(encoding="utf-8")
    assert check_bundle.crate_notice_problems(full, crates) == []
    without = full.replace("Rust crate: numpy 0.30.0", "Rust crate: (removed)")
    assert check_bundle.crate_notice_problems(without, crates) == [
        "THIRD_PARTY_NOTICES.txt lacks the licences of Rust crate: numpy 0.30.0"]
    edited = full.replace("Permission is hereby granted", "Permission is granted", 1)
    assert "a licence text differs" in check_bundle.crate_notice_problems(edited, crates)[0]
    assert check_bundle.crate_notice_problems(None, crates) == ["no THIRD_PARTY_NOTICES.txt in the bundle"]


def test_notices_without_coppermesh_leave_the_crates_out(tmp_path):
    notices, _ = _packaging()
    out = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write(out, ["numpy/__init__.py"], "0.0")
    assert "Rust crate:" not in out.read_text(encoding="utf-8")
