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

One copy only: every start checks the marker (version and wheel SHA-256) and the hash of every
installed file against the wheel's ``RECORD`` (:func:`is_installed`, no network); the dialog and
the download happen only when the pinned version is missing or fails that check. A new pin (an
app update) replaces the folder, and the old version's files go with it. Anything else in or
beside the folder (a stale or partial copy, a download interrupted) is deleted (:func:`tidy`).
:func:`remove` deletes the download (``--remove-triangle`` in the packaged app, and Help →
Remove downloaded mesher in the app). When it cannot now (a file in use: on Windows, the
running app has it loaded), it leaves the folder whole, writes :data:`REMOVE_MARKER` and raises
:class:`RemovalDeferred`; the next start deletes the folder before anything imports it, then
downloads it again as usual.

When to fetch is one switch, :data:`ON_DEMAND` (and :data:`ON_DEMAND_ENV`, :func:`on_demand`).
Off (the default while Triangle is the default mesher): on the first start, as above. On: a
start fetches nothing (:func:`at_start`) and Coppernicus's stand-in takes Triangle's place
(:mod:`coppernicus.triangle_standin`); the dialog and the download happen when the user chooses
the triangle mesher (:func:`fetch_for_mesher`), or with ``--fetch-triangle`` in the packaged app.

The folder is per user:``~/Library/Application Support/Coppernicus/site-packages`` on macOS,
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

import base64
import contextlib
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
from pathlib import Path, PurePosixPath
from typing import Callable, List, Optional, Sequence, TextIO, Tuple

PACKAGE = "triangle"
#: Environment variable: the folder triangle is installed into (default: :func:`site_dir`).
SITE_ENV = "COPPERNICUS_SITE_PACKAGES"
#: Environment variable: the URL to fetch the wheel from instead of the one uv.lock lists.
URL_ENV = "COPPERNICUS_TRIANGLE_URL"
#: The pin the build writes into the bundle (beside the frozen app's modules).
PIN_FILE = "triangle_pin.json"
#: The marker, written last into an installed folder: the version installed and the SHA-256 of
#: the verified wheel it came from.
STAMP_FILE = ".coppernicus-triangle.json"
#: Beside the folder: the lock that lets one start of the app install at a time, and the
#: prefix of the temporary folders an install works in.
LOCK_FILE = ".triangle.lock"
STAGING_PREFIX = ".triangle-download-"
#: Beside the folder: a removal asked for while a file of it was in use; the next start deletes
#: the folder before anything imports it.
REMOVE_MARKER = ".triangle-remove"
#: Whether the packaged app fetches Triangle only when the triangle mesher is chosen, not on
#: the first start (:func:`on_demand`). False while Triangle is the default mesher: the first
#: start fetches it, as before.
ON_DEMAND = False
#: Environment variable: ``1`` fetches on demand, ``0`` on the first start (else :data:`ON_DEMAND`).
ON_DEMAND_ENV = "COPPERNICUS_TRIANGLE_ON_DEMAND"
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
    """Whether ``site`` holds ``wheel`` of ``pin`` as :func:`install` left it: the marker
    (:data:`STAMP_FILE`) names that version and file, and every file the wheel's ``RECORD``
    lists is there with the hash ``RECORD`` gives. Reads the files (about 4 MB), no network."""
    site = Path(site)
    try:
        stamp = json.loads((site / STAMP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    rows = _record(site)
    return stamp == _stamp(pin, wheel) and rows is not None and all(
        _file_ok(site / row[0], row[1] if len(row) > 1 else "") for row in rows)


def _record(site: Path) -> Optional[List[List[str]]]:
    """The rows (path, hash, size) of the one ``RECORD`` in ``site``, or ``None``."""
    records = list(Path(site).glob("*.dist-info/RECORD"))
    if len(records) != 1:
        return None
    try:
        with open(records[0], newline="", encoding="utf-8") as f:
            rows = [row for row in csv.reader(f) if row]
    except (OSError, csv.Error, UnicodeDecodeError):
        return None
    return rows or None


def _file_ok(path: Path, recorded: str) -> bool:
    """Whether ``path`` is a file with the hash ``RECORD`` gives for it (``sha256=`` and the
    digest in URL-safe base64; ``RECORD`` lists itself without a hash)."""
    if not path.is_file():
        return False
    if not recorded:
        return True
    algorithm, _, expected = recorded.partition("=")
    if algorithm not in hashlib.algorithms_guaranteed:
        return False
    try:
        digest = hashlib.new(algorithm, path.read_bytes()).digest()
    except OSError:
        return False
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == expected


# --------------------------------------------------------------------------- one copy only
def leftovers(site: Path) -> List[Path]:
    """What an interrupted install left beside ``site``: its download folders, and a previous
    version moved aside but not yet deleted."""
    site = Path(site)
    try:
        entries = list(site.parent.iterdir())
    except OSError:
        return []
    return sorted(p for p in entries if p.name.startswith((STAGING_PREFIX, f".{site.name}-old-")))


def extraneous(site: Path) -> List[Path]:
    """The files and folders in ``site`` that are not part of the installed wheel (its
    ``RECORD``), the marker or Python's ``__pycache__`` folders."""
    site = Path(site)
    files = {row[0] for row in _record(site) or []} | {STAMP_FILE}
    folders = set()
    for name in files:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            folders.add(str(parent))
            parent = parent.parent
    extra = []
    for root, dirnames, filenames in os.walk(site):
        here = Path(root).relative_to(site)
        for d in list(dirnames):
            if d == "__pycache__" or (here / d).as_posix() not in folders:
                dirnames.remove(d)
                if d != "__pycache__":
                    extra.append(Path(root) / d)
        extra += [Path(root) / f for f in filenames if (here / f).as_posix() not in files]
    return sorted(extra)


@contextlib.contextmanager
def _lock(folder: Path):
    """Hold the per-user folder's lock: one start of the app installs, tidies or removes at a
    time; another one waits."""
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOCK_FILE, "a+b") as f:
        if sys.platform == "win32":
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:                  # LK_LOCK gives up after 10 s: wait on
                    continue
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _clean(paths) -> None:
    """Delete ``paths``, as far as possible: what stays (a file in use on Windows) is tried
    again at the next start."""
    for path in paths:
        try:
            _delete(path)
        except OSError:
            pass


def tidy(site: Path) -> None:
    """Delete what does not belong beside or inside an installed ``site``
    (:func:`leftovers`, :func:`extraneous`). Nothing is downloaded."""
    site = Path(site)
    with _lock(site.parent):
        _clean([*leftovers(site), *extraneous(site)])


def _size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() or p.is_symlink())


class RemovalDeferred(BootstrapError):
    """:func:`remove` could not delete the folder now (a file of it in use): it is whole, the
    marker is written, and the next start deletes it before anything imports it."""

    code = "triangle_removal_deferred"


def _loaded_from(site: Path) -> bool:
    """Whether this process has imported a module from ``site``."""
    root = os.path.normcase(str(Path(site).resolve()))
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if file and os.path.normcase(str(Path(file).resolve())).startswith(root + os.sep):
            return True
    return False


def _defer(site: Path, why: str) -> None:
    (site.parent / REMOVE_MARKER).write_text(why + "\n", encoding="utf-8")
    raise RemovalDeferred("Triangle will be removed the next time Coppernicus starts "
                          f"({why})", "close every Coppernicus window: the next start deletes it "
                          "before using it, then downloads it again")


def remove(site: Path) -> int:
    """Delete the downloaded Triangle: the folder ``site`` and any :func:`leftovers`. Returns
    the bytes deleted (0: there was nothing). The next start downloads it again.

    The folder is first moved aside in one rename, so a file in use leaves it whole: then, and
    on Windows when this process has it loaded (it could not delete it), the marker is written
    and :class:`RemovalDeferred` raised; the next start deletes it (:func:`needed`)."""
    site = Path(site)
    if not site.parent.is_dir():
        return 0
    try:
        with _lock(site.parent):
            targets = ([site] if site.exists() else []) + leftovers(site)
            size = sum(_size(p) for p in targets)
            if site.exists():
                if platform.system() == "Windows" and _loaded_from(site):
                    _defer(site, "the running app has it loaded")
                aside = site.parent / f".{site.name}-old-{uuid.uuid4().hex[:8]}"
                try:
                    os.replace(site, aside)
                except OSError as e:
                    _defer(site, f"a file of it is in use: {e}")
                targets = [aside] + [p for p in targets if p != site]
            _clean(targets)                  # moved away: what stays goes at the next start
            _unlink(site.parent / REMOVE_MARKER)
    except OSError as e:
        raise BootstrapError(f"cannot delete the downloaded Triangle in {site.parent}: {e}",
                             "close every Coppernicus window, then try again") from e
    _unlink(site.parent / LOCK_FILE)
    return size


def _remove_if_asked(site: Path) -> None:
    """At a start, before anything imports Triangle: delete the folder when a removal was
    deferred (:data:`REMOVE_MARKER`); if it is still in use, the next start tries again."""
    if (Path(site).parent / REMOVE_MARKER).exists():
        try:
            remove(site)
        except BootstrapError:
            pass


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
    the wheel's), verify it and install it as the folder ``site``, replacing whatever was there
    (an older version, a damaged copy): one copy only.

    Everything happens in a temporary folder beside ``site``, under the per-user folder's lock;
    the result is moved into place by one rename, after the marker is written, and the previous
    folder is deleted. A failure at any step leaves ``site`` as it was and removes the
    temporary folder. If another start of the app installed the same wheel meanwhile, nothing
    is downloaded."""
    wheel = wheel or choose_wheel(pin)
    site = Path(site)
    try:
        return _install(pin, site, wheel, url, progress, opener)
    except OSError as e:                         # the per-user folder: its lock, its temporary folder
        raise BootstrapError(f"cannot write in {site.parent}: {e}",
                             f"check that {site.parent} is writable and has free space, "
                             "then start Coppernicus again") from e


def _install(pin: Pin, site: Path, wheel: Wheel, url: Optional[str], progress: Optional[Progress],
             opener: Callable) -> Path:
    with _lock(site.parent):
        if is_installed(site, pin, wheel):
            _clean([*leftovers(site), *extraneous(site)])
            return site
        _clean(leftovers(site))                  # what an interrupted install left
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=site.parent))
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
            _replace(new, site)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return site


def _replace(new: Path, site: Path) -> None:
    """Rename ``new`` to ``site``; the folder that was there (another version's files and
    dist-info) is moved aside first, then deleted."""
    old = None
    if site.exists():
        old = site.with_name(f".{site.name}-old-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(site, old)
        except OSError as e:
            raise BootstrapError(f"cannot replace {site}: {e}",
                                 "close every Coppernicus window, then start it again") from e
    try:
        os.replace(new, site)
    except OSError as e:
        if old is not None:
            try:
                os.replace(old, site)            # put the previous folder back
            except OSError:
                pass
        raise BootstrapError(f"cannot write {site}: {e}",
                             f"check that {site.parent} is writable, then start Coppernicus again") from e
    if old is not None:
        _clean([old])                            # if a file is in use, the next start retries


# --------------------------------------------------------------------------- the frozen app
def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where the frozen app's data files are (PyInstaller's ``sys._MEIPASS``)."""
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))


def needed(env=None) -> Optional[Plan]:
    """What the frozen app must fetch before it imports KiPI, or ``None``: not frozen
    (triangle comes from the environment), or already installed and verified (its folder is
    then put on ``sys.path``, after deleting whatever else is in or beside it). A normal start
    downloads nothing and shows nothing. Raises :class:`BootstrapError` when this platform has
    no wheel."""
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
    _remove_if_asked(site)                   # a removal asked for while it was in use
    if is_installed(site, pin, wheel):
        if leftovers(site) or extraneous(site):
            try:
                tidy(site)
            except OSError:                      # best effort: tried again at the next start
                pass
        activate(site)
        return None
    return Plan(pin, wheel, site, env.get(URL_ENV) or wheel.url)


def on_demand(env=None) -> bool:
    """Whether the packaged app fetches Triangle only when the triangle mesher is chosen
    (:func:`fetch_for_mesher`) instead of on the first start: :data:`ON_DEMAND_ENV` set to
    ``1`` or ``0`` decides, else :data:`ON_DEMAND`."""
    env = os.environ if env is None else env
    value = env.get(ON_DEMAND_ENV, "").strip()
    return ON_DEMAND if value not in ("0", "1") else value == "1"


def at_start(env=None) -> Optional[Plan]:
    """What a start of the frozen app must fetch before it imports KiPI, or ``None``.

    :func:`needed` runs in both modes: a deferred removal is done, an installed and verified
    copy is put on ``sys.path``, what lies around it is tidied. Fetching on the first start
    (the default) returns its :class:`Plan` and raises its :class:`BootstrapError`. On demand
    (:func:`on_demand`) nothing is fetched and nothing stops the start (no wheel for this
    platform, an incomplete bundle): Coppernicus's stand-in takes Triangle's place
    (:mod:`coppernicus.triangle_standin`) until the triangle mesher is chosen."""
    if not on_demand(env):
        return needed(env)
    try:
        needed(env)
    except BootstrapError:
        pass
    return None


def fetch_for_mesher(name: str, gui: bool, env=None, stream: Optional[TextIO] = None) -> bool:
    """The user chose the mesher ``name``; for "triangle", make the real Triangle what KiPI uses,
    fetching it first in the frozen app if it is not here (with the dialog when ``gui``, else
    with lines on ``stream``): the download happens here, not at the start, when
    :func:`on_demand`. Returns whether Triangle is in use; any other mesher needs nothing (True).
    Call it with no seam backend installed (see
    :func:`coppernicus.triangle_standin.use_real_triangle`). Raises :class:`BootstrapError` from a
    console fetch; the dialog shows its own errors (False)."""
    if name != PACKAGE:
        return True
    from coppernicus import triangle_standin
    if not triangle_standin.standin_in_use():
        return True
    plan = needed(env)
    if plan is not None:
        if gui:
            if not fetch_with_dialog(plan):
                return False
        else:
            fetch_on_console(plan, stream)
    return triangle_standin.use_real_triangle()


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
