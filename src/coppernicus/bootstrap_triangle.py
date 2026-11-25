"""The Triangle mesher, fetched once by the packaged app.

KiPI meshes copper with Triangle, Jonathan Richard Shewchuk's mesher, through the ``triangle``
Python package; ``kipi/core/mesh/mesher.py`` imports it when it loads. Triangle's licence
forbids distributing it as part of a commercial system without the author's permission, so the
packaged app (``packaging/``) does not contain it. The licence allows telling users how to get
it free of charge, and that is what happens here, once, on the first start:

1. the build embeds the pin: the ``triangle`` version and every wheel's file name, URL and
   SHA-256 exactly as ``uv.lock`` lists them (:func:`pin_from_lock`, :func:`write_pin`);
2. the app picks the wheel for its Python and platform (:func:`choose_wheel`), downloads it
   from PyPI to a temporary file, checks its SHA-256, unpacks it beside the target folder and
   moves it into place in one rename (:func:`install`): a failure leaves nothing behind;
3. the folder goes on ``sys.path`` before anything imports KiPI (:func:`activate`); later starts
   find it there and need no network.

The folder is per user: ``~/Library/Application Support/Coppernicus/site-packages`` on macOS,
``%LOCALAPPDATA%\\Coppernicus\\site-packages`` on Windows (:func:`site_dir`). Two environment
variables exist for tests, mirrors and offline machines: :data:`SITE_ENV` moves the folder and
:data:`URL_ENV` replaces the download URL (a mirror, or ``file:///`` path to a wheel copied by
hand). The SHA-256 is checked whatever the URL.

In a normal install (uv, pip: not frozen) nothing here runs: :func:`needed` returns ``None``
and ``triangle`` comes from the environment, as KiPI declares it.

At module load only the standard library is imported: the frozen entry point imports this
module before anything else. The Qt dialog (:func:`fetch_with_dialog`) imports PySide6 when
it is called.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import importlib
import json
import os
import platform
import shutil
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO, Tuple

PACKAGE = "triangle"
#: Environment variable: the folder triangle is installed into (default: :func:`site_dir`).
SITE_ENV = "COPPERNICUS_SITE_PACKAGES"
#: Environment variable: the URL to fetch the wheel from instead of the one uv.lock lists.
URL_ENV = "COPPERNICUS_TRIANGLE_URL"
#: The pin the build writes into the bundle (beside the frozen app's modules).
PIN_FILE = "triangle_pin.json"
#: Written last into an installed folder: what was installed, from which verified file.
STAMP_FILE = ".coppernicus-triangle.json"
#: Shewchuk's notice, copied into the bundle by the build.
NOTICE_FILE = Path("licenses") / "TRIANGLE_NOTICE.txt"

TRIANGLE_URL = "https://www.cs.cmu.edu/~quake/triangle.html"
PYPI_URL = "https://pypi.org/project/triangle/"
DOCS_URL = "https://github.com/RaffaeleBiondi/Coppernicus/blob/main/docs/install.md"
#: Seconds without data before a download is given up (a socket timeout, not an analysis limit).
TIMEOUT_S = 60
CHUNK = 64 * 1024

RETRY_HINT = ("check the internet connection (or the proxy), then start Coppernicus again; "
              "nothing was installed")


class BootstrapError(Exception):
    """Triangle could not be made available. ``hint`` says what the user can do."""

    code = "triangle_unavailable"

    def __init__(self, message: str, hint: str = RETRY_HINT):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


class DownloadError(BootstrapError):
    """The wheel could not be downloaded (offline, proxy, server)."""


class VerificationError(BootstrapError):
    """The downloaded file is not the one uv.lock pins: it was discarded."""


class Cancelled(BootstrapError):
    """The user cancelled the download."""


@dataclass(frozen=True)
class Wheel:
    filename: str
    url: str
    sha256: str
    size: Optional[int] = None


@dataclass(frozen=True)
class Pin:
    name: str
    version: str
    wheels: Tuple[Wheel, ...]


@dataclass(frozen=True)
class Plan:
    """What the frozen app has to fetch: :attr:`wheel` of :attr:`pin`, from :attr:`url`, into
    :attr:`site`."""
    pin: Pin
    wheel: Wheel
    site: Path
    url: str


# --------------------------------------------------------------------------- the pin
def pin_from_lock(text: str, name: str = PACKAGE) -> Pin:
    """The pin of ``name`` in the text of a ``uv.lock``: its version and its wheels, each with
    the SHA-256 the lock holds. Only a package from a registry (PyPI) has wheels to pin."""
    lock = tomllib.loads(text)
    for package in lock.get("package", []):
        if package.get("name") != name:
            continue
        if "registry" not in package.get("source", {}):
            raise ValueError(f"{name} in uv.lock does not come from a registry")
        wheels = []
        for w in package.get("wheels", []):
            algorithm, _, digest = w["hash"].partition(":")
            if algorithm != "sha256" or len(digest) != 64:
                raise ValueError(f"{name} wheel {w['url']} has no SHA-256 in uv.lock")
            wheels.append(Wheel(w["url"].rsplit("/", 1)[-1], w["url"], digest.lower(), w.get("size")))
        if not wheels:
            raise ValueError(f"{name} in uv.lock lists no wheel")
        return Pin(name, str(package["version"]), tuple(wheels))
    raise ValueError(f"{name} is not in uv.lock")


def write_pin(pin: Pin, path: Path) -> None:
    doc = {"name": pin.name, "version": pin.version,
           "wheels": [{"filename": w.filename, "url": w.url, "sha256": w.sha256, "size": w.size}
                      for w in pin.wheels]}
    Path(path).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def read_pin(path: Path) -> Pin:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return Pin(doc["name"], doc["version"],
               tuple(Wheel(w["filename"], w["url"], w["sha256"], w.get("size")) for w in doc["wheels"]))


# --------------------------------------------------------------------------- the wheel
def _tags(filename: str) -> Tuple[str, str, Sequence[str]]:
    """``name-version[-build]-python-abi-platform.whl`` → (python, abi, platforms)."""
    parts = filename[:-4].split("-")
    if not filename.endswith(".whl") or len(parts) < 5:
        raise ValueError(f"not a wheel file name: {filename}")
    return parts[-3], parts[-2], parts[-1].split(".")


def _platform_ok(tag: str, system: str, machine: str, bits64: bool) -> bool:
    machine = machine.lower()
    if system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
        return tag.startswith("macosx_") and tag.endswith((f"_{arch}", "_universal2"))
    if system == "win32":
        if machine in ("arm64", "aarch64"):
            return tag == "win_arm64"
        return tag == ("win_amd64" if bits64 else "win32")
    if system.startswith("linux"):
        return tag.startswith("manylinux") and tag.endswith(f"_{machine}")
    return False


def choose_wheel(pin: Pin, version_info=None, system: Optional[str] = None,
                 machine: Optional[str] = None, bits64: Optional[bool] = None) -> Wheel:
    """The wheel of ``pin`` built for this Python (CPython ``X.Y``) and platform. Raises
    :class:`BootstrapError` when the lock has none (an Intel Mac, for one: triangle publishes
    macOS wheels for Apple silicon only)."""
    major, minor = (version_info or sys.version_info)[:2]
    system = system or sys.platform
    machine = machine or platform.machine()
    bits64 = sys.maxsize > 2**32 if bits64 is None else bits64
    py = f"cp{major}{minor}"
    for wheel in pin.wheels:
        python, abi, platforms = _tags(wheel.filename)
        if python == py and abi == py and any(_platform_ok(p, system, machine, bits64) for p in platforms):
            return wheel
    raise BootstrapError(
        f"{pin.name} {pin.version} has no wheel for Python {major}.{minor} on {system} {machine}",
        f"this build of Coppernicus cannot run on this computer; see {DOCS_URL}")


# --------------------------------------------------------------------------- the folder
def site_dir(env=None, system: Optional[str] = None, home: Optional[Path] = None) -> Path:
    """The per-user folder triangle goes into (``$COPPERNICUS_SITE_PACKAGES`` if set)."""
    env = os.environ if env is None else env
    system = system or sys.platform
    home = Path.home() if home is None else Path(home)
    if env.get(SITE_ENV):
        return Path(env[SITE_ENV])
    if system == "darwin":
        base = home / "Library" / "Application Support"
    elif system == "win32":
        base = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
    else:
        base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / "Coppernicus" / "site-packages"


def _stamp(pin: Pin, wheel: Wheel) -> dict:
    return {"name": pin.name, "version": pin.version, "wheel": wheel.filename, "sha256": wheel.sha256}


def is_installed(site: Path, pin: Pin, wheel: Wheel) -> bool:
    """Whether ``site`` holds exactly ``wheel`` of ``pin``, installed by :func:`install`, with
    every file its wheel's ``RECORD`` lists still there."""
    site = Path(site)
    try:
        stamp = json.loads((site / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return stamp == _stamp(pin, wheel) and _record_complete(site)


def _record_complete(site: Path) -> bool:
    records = list(site.glob("*.dist-info/RECORD"))
    if len(records) != 1:
        return False
    try:
        with open(records[0], newline="", encoding="utf-8") as f:
            rows = [row for row in csv.reader(f) if row]
    except (OSError, csv.Error, UnicodeDecodeError):
        return False
    return bool(rows) and all((site / row[0]).is_file() for row in rows)


def activate(site: Path) -> None:
    """Put ``site`` on ``sys.path`` (at the end: nothing in it may shadow the app's modules)."""
    entry = str(site)
    sys.path_importer_cache.pop(entry, None)       # a folder looked up before it existed
    if entry not in sys.path:
        sys.path.append(entry)
    importlib.invalidate_caches()


# --------------------------------------------------------------------------- download, install
Progress = Callable[[int, Optional[int]], None]


def _ssl_context():
    """A TLS context that verifies PyPI's certificate: the system's store, plus certifi's
    bundle when the app holds it (a frozen Python may not find the system's CA file)."""
    import ssl
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        pass
    return context


def _open(url: str):
    if url.startswith("https:"):
        return urllib.request.urlopen(url, timeout=TIMEOUT_S, context=_ssl_context())
    return urllib.request.urlopen(url, timeout=TIMEOUT_S)


def download(url: str, dest: Path, sha256: str, size: Optional[int] = None,
             progress: Optional[Progress] = None, opener: Callable = _open) -> None:
    """Download ``url`` to ``dest`` and check its SHA-256. On any failure ``dest`` is removed
    and :class:`DownloadError` or :class:`VerificationError` raised."""
    digest = hashlib.sha256()
    done = 0
    try:
        with opener(url) as response, open(dest, "wb") as out:
            length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            total = int(length) if length and length.isdigit() else size
            if progress:
                progress(0, total)
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BootstrapError:
        _unlink(dest)
        raise
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        _unlink(dest)
        plain = isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError)
        reason = e.reason if plain else e
        raise DownloadError(f"cannot download {url.rsplit('/', 1)[-1]}: {reason}") from e
    if digest.hexdigest() != sha256.lower():
        _unlink(dest)
        raise VerificationError(
            f"the downloaded file is not the one this build expects (SHA-256 {digest.hexdigest()}, "
            f"expected {sha256}): it was deleted",
            "try again later; if it happens again, report it: the file on the server changed")


def _unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _extract(wheel_file: Path, target: Path) -> None:
    """Unpack a wheel (a zip) into ``target``, refusing members that would land outside it."""
    target = target.resolve()
    with zipfile.ZipFile(wheel_file) as z:
        for member in z.infolist():
            path = (target / member.filename).resolve()
            if path != target and target not in path.parents:
                raise VerificationError(f"the wheel holds a file outside its folder: {member.filename}",
                                        "report it: the file is not a normal wheel")
        z.extractall(target)


def install(pin: Pin, site: Path, wheel: Optional[Wheel] = None, url: Optional[str] = None,
            progress: Optional[Progress] = None, opener: Callable = _open) -> Path:
    """Download ``wheel`` (default: the one for this platform) of ``pin`` from ``url`` (default:
    the wheel's), verify it and install it as the folder ``site``, replacing whatever was there.

    Everything happens in a temporary folder beside ``site``; the result is moved into place
    by one rename, after the stamp is written. A failure at any step leaves ``site`` as it was
    and removes the temporary folder."""
    wheel = wheel or choose_wheel(pin)
    site = Path(site)
    site.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".triangle-download-", dir=site.parent))
    try:
        wheel_file = staging / wheel.filename
        download(url or wheel.url, wheel_file, wheel.sha256, wheel.size, progress, opener)
        new = staging / "site-packages"
        try:
            _extract(wheel_file, new)
            (new / STAMP_FILE).write_text(json.dumps(_stamp(pin, wheel), indent=1) + "\n",
                                          encoding="utf-8")
        except zipfile.BadZipFile as e:
            raise VerificationError(f"cannot unpack {wheel.filename}: {e}",
                                    "report it: the file is not a normal wheel") from e
        except OSError as e:
            raise BootstrapError(f"cannot unpack {wheel.filename}: {e}",
                                 f"check that {site.parent} is writable and has free space, "
                                 "then start Coppernicus again") from e
        _move_into_place(new, site, lambda: is_installed(site, pin, wheel))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return site


def _move_into_place(new: Path, site: Path, already_done: Callable[[], bool]) -> None:
    if site.exists():
        if already_done():                      # another start of the app got there first
            return
        old = site.with_name(f".{site.name}-old-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(site, old)
        except OSError as e:
            raise BootstrapError(f"cannot replace {site}: {e}",
                                 "close every Coppernicus window, then start it again") from e
        shutil.rmtree(old, ignore_errors=True)
    try:
        os.replace(new, site)
    except OSError as e:
        if already_done():
            return
        raise BootstrapError(f"cannot write {site}: {e}",
                             f"check that {site.parent} is writable, then start Coppernicus again") from e


# --------------------------------------------------------------------------- the frozen app
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where the frozen app's data files are (PyInstaller's ``sys._MEIPASS``)."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))


def needed(env=None) -> Optional[Plan]:
    """What the frozen app must fetch before it imports KiPI, or ``None``: not frozen
    (triangle comes from the environment), or already installed (its folder is then put on
    ``sys.path``). Raises :class:`BootstrapError` when this platform has no wheel."""
    if not frozen():
        return None
    env = os.environ if env is None else env
    try:
        pin = read_pin(bundle_dir() / PIN_FILE)
    except (OSError, ValueError, KeyError) as e:
        raise BootstrapError(f"this copy of Coppernicus is incomplete ({PIN_FILE}: {e})",
                             f"download Coppernicus again; see {DOCS_URL}") from e
    wheel = choose_wheel(pin)
    site = site_dir(env)
    if is_installed(site, pin, wheel):
        activate(site)
        return None
    return Plan(pin, wheel, site, env.get(URL_ENV) or wheel.url)


def notice_text() -> str:
    """Shewchuk's notice for Triangle, as the bundle holds it."""
    try:
        return (bundle_dir() / NOTICE_FILE).read_text(encoding="utf-8")
    except OSError:
        return f"Triangle's licence: {TRIANGLE_URL}"


def licence_note(plan: Plan) -> str:
    """Why the download happens, in plain text."""
    return (f"Coppernicus meshes copper with Triangle, by Jonathan Richard Shewchuk, through the "
            f"'{plan.pin.name}' Python package (version {plan.pin.version}, LGPL-3.0). Triangle's "
            f"licence does not allow it to be included in Coppernicus, but anyone may get it free "
            f"of charge: Coppernicus downloads it from PyPI once, checks its SHA-256 and keeps it "
            f"in {plan.site}. Triangle's licence: {TRIANGLE_URL}")


def fetch_on_console(plan: Plan, stream: Optional[TextIO] = None, install_fn: Callable = install) -> None:
    """Fetch triangle for a command-line start and put it on ``sys.path``, reporting progress
    on ``stream`` (stderr: under ``mcp`` and ``--json`` stdout belongs to the protocol or the
    document). Raises :class:`BootstrapError`, for the caller to report."""
    stream = stream or sys.stderr
    print(f"Downloading the Triangle mesher from PyPI (once)… {plan.wheel.filename}", file=stream, flush=True)
    print(licence_note(plan), file=stream, flush=True)
    shown = [-1]

    def progress(done, total):
        if total:
            tenth = min(10, done * 10 // total)
            if tenth != shown[0]:
                shown[0] = tenth
                print(f"  {tenth * 10:3d} %  {done / 1e6:.1f} of {total / 1e6:.1f} MB",
                      file=stream, flush=True)
    install_fn(plan.pin, plan.site, wheel=plan.wheel, url=plan.url, progress=progress)
    activate(plan.site)
    print(f"Triangle {plan.pin.version} installed in {plan.site}", file=stream, flush=True)


def fetch_with_dialog(plan: Plan, install_fn: Callable = install) -> bool:
    """Fetch triangle for a start of the app, with a small dialog that shows the progress and
    the licence note; on failure a box says why and offers to retry. Returns whether triangle
    is installed and on ``sys.path``."""
    from html import escape

    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
    )

    class Worker(QThread):
        progressed = Signal(int, int)
        failed = Signal(object)

        def __init__(self, parent):
            super().__init__(parent)
            self.cancelled = False

        def run(self):
            def progress(done, total):
                if self.cancelled:
                    raise Cancelled("cancelled", "start Coppernicus again to download it")
                self.progressed.emit(done, total or 0)
            try:
                install_fn(plan.pin, plan.site, wheel=plan.wheel, url=plan.url, progress=progress)
            except BootstrapError as e:
                self.failed.emit(e)
            except Exception as e:                       # noqa: BLE001 - shown, never a crash
                self.failed.emit(BootstrapError(f"{type(e).__name__}: {e}"))

    class Dialog(QDialog):
        def __init__(self):
            super().__init__()
            self.error: Optional[BootstrapError] = None
            self.setWindowTitle("Coppernicus: first start")
            self.setMinimumWidth(520)
            layout = QVBoxLayout(self)
            title = QLabel("<b>Downloading the Triangle mesher from PyPI (once)…</b>")
            layout.addWidget(title)
            self.bar = QProgressBar()
            self.bar.setRange(0, 0)
            layout.addWidget(self.bar)
            self.status = QLabel(plan.wheel.filename)
            layout.addWidget(self.status)
            note = QLabel(
                f"Coppernicus meshes copper with <b>Triangle</b>, by Jonathan Richard Shewchuk, "
                f"through the <i>{plan.pin.name}</i> Python package (version {plan.pin.version}, "
                f"LGPL-3.0). Triangle's licence does not allow it to be included in Coppernicus, "
                f"but anyone may get it free of charge: Coppernicus downloads it from PyPI once, "
                f"checks its SHA-256 and keeps it in <code>{escape(str(plan.site))}</code>. Later "
                f"starts work offline.<br><a href='{TRIANGLE_URL}'>Triangle</a> · "
                f"<a href='{PYPI_URL}'>{plan.pin.name} on PyPI</a>")
            note.setWordWrap(True)
            note.setOpenExternalLinks(True)
            layout.addWidget(note)
            buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
            licence = QPushButton("Triangle's licence…")
            buttons.addButton(licence, QDialogButtonBox.HelpRole)
            licence.clicked.connect(self.show_licence)
            buttons.rejected.connect(self.cancel)
            layout.addWidget(buttons)
            self.worker = Worker(self)
            self.worker.progressed.connect(self.progressed)
            self.worker.failed.connect(self.failed)
            self.worker.finished.connect(self.finished_download)

        def start(self):
            self.worker.start()

        def progressed(self, done, total):
            if total:
                self.bar.setRange(0, total)
                self.bar.setValue(done)
                self.status.setText(f"{plan.wheel.filename}: {done / 1e6:.1f} of {total / 1e6:.1f} MB")

        def failed(self, error):
            self.error = error

        def finished_download(self):
            # done(), not reject(): the thread may still count as running when this arrives.
            self.done(QDialog.Accepted if self.error is None else QDialog.Rejected)

        def cancel(self):
            self.worker.cancelled = True
            self.status.setText("Cancelling…")

        def reject(self):
            if self.worker.isRunning():          # Esc or the close button: cancel, then close
                self.cancel()
                return
            super().reject()

        def show_licence(self):
            box = QMessageBox(QMessageBox.Information, "Triangle's licence", notice_text(), parent=self)
            box.exec()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Coppernicus")
    while True:
        dialog = Dialog()
        dialog.show()
        dialog.start()
        dialog.exec()
        dialog.worker.wait()
        if dialog.error is None:
            activate(plan.site)
            return True
        if isinstance(dialog.error, Cancelled):
            return False
        box = QMessageBox(QMessageBox.Critical, "Coppernicus",
                          "Coppernicus could not get the Triangle mesher, which it needs to solve.")
        box.setInformativeText(f"{dialog.error.message}.\n\nWhat to do: {dialog.error.hint}. "
                               "Retry tries again now.")
        retry = box.addButton("Retry", QMessageBox.AcceptRole)
        box.addButton("Quit", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not retry:
            return False
