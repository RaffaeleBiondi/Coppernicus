"""The packaged app's first start: triangle fetched once, verified, installed atomically.

No network: wheels are small zips built here and served from ``file://`` URLs, or fake openers
that fail as an offline machine does."""

import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import zipfile
from pathlib import Path

import pytest

from coppernicus import bootstrap_triangle as bt

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "uv.lock"


def _here_platform_tag() -> str:
    """A wheel platform tag that choose_wheel accepts on the machine running the tests."""
    import platform
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macosx_11_0_" + ("arm64" if machine in ("arm64", "aarch64") else "x86_64")
    if sys.platform == "win32":
        return {"arm64": "win_arm64"}.get(machine, "win_amd64" if sys.maxsize > 2**32 else "win32")
    return f"manylinux_2_17_{machine}"


def make_wheel(folder: Path, module: str, version: str = "1.0", members=None) -> Path:
    """A small wheel of ``module`` for this Python and platform: ``VALUE = 42`` and a RECORD."""
    py = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    path = folder / f"{module}-{version}-{py}-{py}-{_here_platform_tag()}.whl"
    info = f"{module}-{version}.dist-info"
    files = members if members is not None else {
        f"{module}/__init__.py": "VALUE = 42\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {module}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\n",
    }
    if members is None:
        files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in files) + f"{info}/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


def pin_for(wheel_file: Path, module: str, version: str = "1.0", sha256=None) -> bt.Pin:
    digest = sha256 or hashlib.sha256(wheel_file.read_bytes()).hexdigest()
    return bt.Pin(module, version, (bt.Wheel(wheel_file.name, wheel_file.as_uri(), digest,
                                             wheel_file.stat().st_size),))


def leftovers(folder: Path):
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --------------------------------------------------------------------------- pin and wheel
def test_the_pin_is_the_triangle_uv_lock_installs():
    pin = bt.pin_from_lock(LOCK.read_text(encoding="utf-8"))
    assert pin.name == "triangle"
    assert pin.version == importlib.metadata.version("triangle")      # uv sync'd from the same lock
    assert pin.wheels
    for w in pin.wheels:
        assert w.filename.startswith(f"triangle-{pin.version}-") and w.filename.endswith(".whl")
        assert w.url.startswith("https://files.pythonhosted.org/") and w.url.endswith(w.filename)
        assert len(w.sha256) == 64 and int(w.sha256, 16) >= 0


def test_the_pin_survives_the_trip_through_the_bundle(tmp_path):
    pin = bt.pin_from_lock(LOCK.read_text(encoding="utf-8"))
    bt.write_pin(pin, tmp_path / bt.PIN_FILE)
    assert bt.read_pin(tmp_path / bt.PIN_FILE) == pin


@pytest.mark.parametrize("lock, problem", [
    ('[[package]]\nname = "other"\nversion = "1"\nsource = { registry = "x" }\n', "not in uv.lock"),
    ('[[package]]\nname = "triangle"\nversion = "1"\nsource = { git = "x" }\n', "registry"),
    ('[[package]]\nname = "triangle"\nversion = "1"\nsource = { registry = "x" }\n'
     'wheels = [{ url = "https://h/triangle-1-cp312-cp312-win_amd64.whl", hash = "md5:00" }]\n', "SHA-256"),
    ('[[package]]\nname = "triangle"\nversion = "1"\nsource = { registry = "x" }\n', "no wheel"),
])
def test_a_lock_without_a_verifiable_pin_is_refused(lock, problem):
    with pytest.raises(ValueError, match=problem):
        bt.pin_from_lock(lock)


@pytest.mark.parametrize("system, machine, bits64, py, tag", [
    ("darwin", "arm64", True, (3, 12), "cp312-cp312-macosx_11_0_arm64"),
    ("darwin", "arm64", True, (3, 13), "cp313-cp313-macosx_11_0_arm64"),
    ("win32", "AMD64", True, (3, 12), "cp312-cp312-win_amd64"),
    ("win32", "x86", False, (3, 13), "cp313-cp313-win32"),
    ("linux", "x86_64", True, (3, 12), "cp312-cp312-manylinux_2_17_x86_64"),
])
def test_the_wheel_for_the_running_python_and_platform(system, machine, bits64, py, tag):
    pin = bt.pin_from_lock(LOCK.read_text(encoding="utf-8"))
    wheel = bt.choose_wheel(pin, py, system, machine, bits64)
    assert tag in wheel.filename


@pytest.mark.parametrize("system, machine, py", [
    ("darwin", "x86_64", (3, 12)),          # triangle has no Intel macOS wheel
    ("darwin", "arm64", (3, 14)),           # nor one for a Python the lock does not cover
    ("win32", "ARM64", (3, 12)),
])
def test_no_wheel_is_a_clear_error(system, machine, py):
    pin = bt.pin_from_lock(LOCK.read_text(encoding="utf-8"))
    with pytest.raises(bt.BootstrapError, match="has no wheel for Python"):
        bt.choose_wheel(pin, py, system, machine, True)


def test_the_per_user_folder():
    home = Path("/home/u")
    assert bt.site_dir({}, "darwin", home) == home / "Library/Application Support/Coppernicus/site-packages"
    assert bt.site_dir({"LOCALAPPDATA": "C:/Users/u/AppData/Local"}, "win32", home) == \
        Path("C:/Users/u/AppData/Local") / "Coppernicus" / "site-packages"
    assert bt.site_dir({}, "win32", home) == home / "AppData/Local/Coppernicus/site-packages"
    assert bt.site_dir({bt.SITE_ENV: "/tmp/x"}, "darwin", home) == Path("/tmp/x")


# --------------------------------------------------------------------------- install
def test_a_verified_wheel_is_installed_and_imported(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_ok")
    pin = pin_for(wheel, "cpn_fake_ok")
    site = tmp_path / "user" / "site-packages"
    seen = []
    bt.install(pin, site, progress=lambda done, total: seen.append((done, total)))
    assert bt.is_installed(site, pin, pin.wheels[0])
    assert seen[-1] == (wheel.stat().st_size, wheel.stat().st_size)
    assert leftovers(site.parent) == ["site-packages"]          # no temporary folder left
    stamp = json.loads((site / bt.STAMP_FILE).read_text(encoding="utf-8"))
    assert stamp["sha256"] == pin.wheels[0].sha256 and stamp["version"] == "1.0"
    before = list(sys.path)
    try:
        bt.activate(site)
        bt.activate(site)                                       # once on sys.path, at the end
        assert sys.path.count(str(site)) == 1 and sys.path[-1] == str(site)
        assert importlib.import_module("cpn_fake_ok").VALUE == 42
    finally:
        sys.path[:] = before
        sys.modules.pop("cpn_fake_ok", None)


def test_a_wrong_hash_installs_nothing(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_bad")
    pin = pin_for(wheel, "cpn_fake_bad", sha256="0" * 64)
    site = tmp_path / "user" / "site-packages"
    with pytest.raises(bt.VerificationError, match="not the one this build expects"):
        bt.install(pin, site)
    assert not site.exists() and leftovers(site.parent) == []


def test_a_failed_update_keeps_the_previous_install(tmp_path):
    old = make_wheel(tmp_path, "cpn_fake_up", "1.0")
    old_pin = pin_for(old, "cpn_fake_up", "1.0")
    site = tmp_path / "user" / "site-packages"
    bt.install(old_pin, site)
    new = make_wheel(tmp_path, "cpn_fake_up", "2.0")
    with pytest.raises(bt.VerificationError):
        bt.install(pin_for(new, "cpn_fake_up", "2.0", sha256="f" * 64), site)
    assert bt.is_installed(site, old_pin, old_pin.wheels[0])
    assert leftovers(site.parent) == ["site-packages"]
    new_pin = pin_for(new, "cpn_fake_up", "2.0")                # a good update replaces it
    assert not bt.is_installed(site, new_pin, new_pin.wheels[0])
    bt.install(new_pin, site)
    assert bt.is_installed(site, new_pin, new_pin.wheels[0])
    assert leftovers(site.parent) == ["site-packages"]


def test_a_file_that_is_not_a_wheel_installs_nothing(tmp_path):
    bogus = tmp_path / f"cpn_fake_zip-1.0-cp312-cp312-{_here_platform_tag()}.whl"
    bogus.write_bytes(b"not a zip file")
    pin = pin_for(bogus, "cpn_fake_zip")                        # the hash matches: still refused
    site = tmp_path / "user" / "site-packages"
    with pytest.raises(bt.VerificationError, match="cannot unpack"):
        bt.install(pin, site, wheel=pin.wheels[0])
    assert not site.exists() and leftovers(site.parent) == []


def test_a_wheel_member_outside_the_folder_is_refused(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_slip", members={"../escaped.txt": "x"})
    pin = pin_for(wheel, "cpn_fake_slip")
    site = tmp_path / "user" / "site-packages"
    with pytest.raises(bt.VerificationError, match="outside its folder"):
        bt.install(pin, site)
    assert not (tmp_path / "user" / "escaped.txt").exists()
    assert not site.exists() and leftovers(site.parent) == []


def test_an_install_with_a_missing_file_is_not_installed(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_gone")
    pin = pin_for(wheel, "cpn_fake_gone")
    site = tmp_path / "site-packages"
    bt.install(pin, site)
    (site / "cpn_fake_gone" / "__init__.py").unlink()
    assert not bt.is_installed(site, pin, pin.wheels[0])


def test_another_start_that_installed_first_wins(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_race")
    pin = pin_for(wheel, "cpn_fake_race")
    site = tmp_path / "site-packages"
    bt.install(pin, site)
    marker = site / "kept"
    marker.write_text("first")
    bt.install(pin, site)                                       # finds it installed: keeps it
    assert marker.read_text() == "first" and bt.is_installed(site, pin, pin.wheels[0])


@pytest.mark.parametrize("error, text", [
    (urllib.error.URLError(OSError(8, "nodename nor servname provided")), "nodename"),
    (urllib.error.HTTPError("https://x/w.whl", 503, "Service Unavailable", {}, None), "503"),
    (TimeoutError("timed out"), "timed out"),
])
def test_offline_is_a_clear_error_and_installs_nothing(tmp_path, error, text):
    wheel = make_wheel(tmp_path, "cpn_fake_off")
    pin = pin_for(wheel, "cpn_fake_off")
    site = tmp_path / "user" / "site-packages"

    def opener(_url):
        raise error
    with pytest.raises(bt.DownloadError, match=text) as caught:
        bt.install(pin, site, opener=opener)
    assert "start Coppernicus again" in caught.value.hint
    assert caught.value.to_dict()["code"] == "triangle_unavailable"
    assert not site.exists() and leftovers(site.parent) == []


def test_a_connection_lost_halfway_installs_nothing(tmp_path):
    wheel = make_wheel(tmp_path, "cpn_fake_cut")
    pin = pin_for(wheel, "cpn_fake_cut")
    site = tmp_path / "user" / "site-packages"

    class Cut(io.BytesIO):
        headers = {}

        def read(self, n=-1):
            if self.tell() > 10:
                raise ConnectionResetError("connection reset by peer")
            return super().read(10)
    with pytest.raises(bt.DownloadError, match="reset"):
        bt.install(pin, site, opener=lambda _url: Cut(wheel.read_bytes()))
    assert not site.exists() and leftovers(site.parent) == []


# --------------------------------------------------------------------------- the frozen app
def test_nothing_happens_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(bt.SITE_ENV, str(tmp_path / "site"))
    before = list(sys.path)
    assert bt.needed() is None
    assert sys.path == before and not (tmp_path / "site").exists()


def test_the_frozen_app_fetches_once_then_works_offline(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    wheel = make_wheel(tmp_path, "cpn_fake_frozen")
    pin = pin_for(wheel, "cpn_fake_frozen")
    bt.write_pin(bt.Pin(pin.name, pin.version, (bt.Wheel(wheel.name, "https://unused.invalid/x.whl",
                                                         pin.wheels[0].sha256),)),
                 bundle / bt.PIN_FILE)
    site = tmp_path / "site"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv(bt.SITE_ENV, str(site))
    monkeypatch.setenv(bt.URL_ENV, wheel.as_uri())             # the hash is still checked
    before = list(sys.path)
    try:
        plan = bt.needed()
        assert plan is not None and plan.site == site and plan.url == wheel.as_uri()
        out = io.StringIO()
        bt.fetch_on_console(plan, stream=out)
        assert "Downloading the Triangle mesher from PyPI (once)" in out.getvalue()
        assert bt.TRIANGLE_URL in out.getvalue() and str(site) in out.getvalue()
        assert importlib.import_module("cpn_fake_frozen").VALUE == 42
        monkeypatch.setenv(bt.URL_ENV, "http://127.0.0.1:9/unreachable.whl")   # offline now
        assert bt.needed() is None and str(site) in sys.path
    finally:
        sys.path[:] = before
        sys.modules.pop("cpn_fake_frozen", None)


def test_a_bundle_without_its_pin_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(bt.BootstrapError, match="incomplete"):
        bt.needed()


def _frozen_main():
    spec = importlib.util.spec_from_file_location("frozen_main", ROOT / "packaging" / "frozen_main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_command_line_start_that_cannot_fetch_exits_2_with_the_error_document(monkeypatch, capsys):
    frozen_main = _frozen_main()

    def offline(env=None):
        raise bt.DownloadError("cannot download triangle.whl: no network")
    monkeypatch.setattr(bt, "needed", offline)
    assert frozen_main.ensure_triangle(["nets", "board.kicad_pcb", "--json"]) == 2
    out, err = capsys.readouterr()
    assert json.loads(out)["error"]["code"] == "triangle_unavailable"
    assert "no network" in err
    assert frozen_main.ensure_triangle(["mcp"]) == 2                 # stdout is the protocol's
    out, _ = capsys.readouterr()
    assert out == ""


def test_the_dialog_fetches_and_reports_a_failure(tmp_path):
    pytest.importorskip("PySide6")
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox
        from coppernicus import bootstrap_triangle as bt

        site = Path({str(tmp_path / 'site')!r})
        pin = bt.Pin("triangle", "1", (bt.Wheel("triangle-1-cp312-cp312-x.whl", "https://x", "0" * 64, 100),))
        plan = bt.Plan(pin, pin.wheels[0], site, "https://x")

        def works(pin, site, wheel=None, url=None, progress=None):
            progress(50, 100)
            progress(100, 100)
        assert bt.fetch_with_dialog(plan, install_fn=works) is True
        assert str(site) in sys.path

        boxes = []
        QMessageBox.exec = lambda self: boxes.append(self.informativeText()) or 0
        def offline(pin, site, wheel=None, url=None, progress=None):
            raise bt.DownloadError("cannot download triangle-1.whl: no network")
        assert bt.fetch_with_dialog(plan, install_fn=offline) is False      # no Retry clicked
        assert "no network" in boxes[0] and "start Coppernicus again" in boxes[0], boxes
        print("ok")
    """)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0 and "ok" in out.stdout, out.stdout + out.stderr
