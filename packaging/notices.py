"""THIRD_PARTY_NOTICES.txt for the packaged app, from what the bundle actually contains.

coppernicus.spec calls :func:`write` with the names PyInstaller collected. Every Python
distribution with a module, extension or data file in the bundle is listed with its version and
licence (from its metadata), and the licence files its wheel ships (``*.dist-info/licenses/``,
``LICENSE``, ``COPYING``, ``NOTICE``) follow in full, so vendored libraries (OpenBLAS in
numpy and scipy, GEOS in shapely, the image libraries in Pillow...) are covered by their own
wheels' texts. Python's licence and the LGPL-3.0 text for Qt are added by hand. coppermesh, the
rust mesher, is one extension compiled from Rust crates: when it is in the bundle, the licence
texts of every crate it is built from (licenses/coppermesh-crates.txt, generated from its
Cargo.lock by tools/mesher_rs/crate_licences.py) follow, in full.

Not importable as ``packaging.notices``: this folder must stay without ``__init__.py`` so that
it never shadows the ``packaging`` distribution.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
#: The licences of the Rust crates compiled into coppermesh (tools/mesher_rs/crate_licences.py).
RUST_CRATES = Path(__file__).resolve().parent / "licenses" / "coppermesh-crates.txt"

HEADER = """\
Coppernicus {version}: third-party notices
{rule}

Coppernicus is free software under the GNU General Public License, version 3 or (at your
option) any later version: see LICENSE. Source code: https://github.com/RaffaeleBiondi/Coppernicus

This packaged app also contains the Python interpreter and the Python distributions listed
below, each under its own licence. Their licence texts, as each distribution ships them,
follow the list.

Triangle (the mesher) is NOT included
--------------------------------------
Triangle, by Jonathan Richard Shewchuk, may not be distributed as part of Coppernicus. On
its first start the app downloads the 'triangle' Python package (LGPL-3.0) from PyPI into a
folder of the user's own; see licenses/TRIANGLE_NOTICE.txt for Triangle's terms, verbatim.

Qt and PySide6
--------------
The user interface uses Qt through PySide6 (PySide6_Essentials, shiboken6), licensed here
under the GNU Lesser General Public License, version 3 (licenses/LGPL-3.0.txt; the GPL-3.0
text is in LICENSE). The Qt libraries are separate shared libraries that Coppernicus loads
at run time (dynamic linking): they can be replaced with a compatible build of the same Qt
version. Qt's source code: https://download.qt.io/official_releases/qt/
PySide6's source code: https://code.qt.io/cgit/pyside/pyside-setup.git/

Distributions in this bundle
----------------------------
"""


def top_level_names(names: Iterable[str]) -> Set[str]:
    """The top-level import names of the bundle's entries (``numpy._core.x`` → ``numpy``,
    ``PySide6/Qt/lib/...`` → ``PySide6``; ``numpy.libs`` folders count as ``numpy``)."""
    tops = set()
    for name in names:
        first = str(name).replace("\\", "/").split("/")[0]
        top = first.split(".")[0] if "/" not in str(name).replace("\\", "/") else first
        if top.endswith(".libs"):
            top = top[: -len(".libs")]
        tops.add(top)
    return tops


def bundled_distributions(tops: Set[str]) -> List[md.Distribution]:
    """The installed distributions that provide one of ``tops``."""
    providers = md.packages_distributions()
    names = {dist for top in tops for dist in providers.get(top, [])}
    found: Dict[str, md.Distribution] = {}
    for name in names:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        found[dist.metadata["Name"].lower()] = dist
    return sorted(found.values(), key=lambda d: d.metadata["Name"].lower())


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::") and "OSI Approved" != c.split("::")[-1].strip()]
    text = (meta.get("License") or "").strip()
    if text and len(text.splitlines()) == 1 and len(text) <= 80:
        return text
    return ", ".join(classifiers) or "see the licence text below"


def licence_files(dist: md.Distribution) -> List[tuple]:
    files = []
    for f in dist.files or []:
        parts = Path(str(f)).parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        if (len(parts) > 2 and parts[1] == "licenses") or (len(parts) == 2 and LICENSE_FILE.match(parts[1])):
            try:
                files.append((str(f), Path(f.locate()).read_bytes().decode("utf-8", "replace")))
            except OSError:
                continue
    return files


def _url(dist: md.Distribution) -> str:
    meta = dist.metadata
    if meta.get("Home-page"):
        return meta["Home-page"]
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "source code"):
            return url.strip()
    return f"https://pypi.org/project/{meta['Name']}/"


def write(out: Path, names: Iterable[str], version: str, python_licence: Optional[Path] = None) -> List[str]:
    """Write the notices for the bundle whose entries are ``names``; returns the listed
    distribution names. Fails if ``triangle`` is among them."""
    dists = bundled_distributions(top_level_names(names))
    listed = [d.metadata["Name"] for d in dists]
    if any(n.lower() == "triangle" for n in listed):
        raise SystemExit("triangle must not be in the bundle")
    title = f"Coppernicus {version}: third-party notices"
    lines = [HEADER.format(version=version, rule="=" * len(title)).rstrip("\n"), ""]
    py = ".".join(map(str, sys.version_info[:3]))
    lines.append(f"  {'Python':28s} {py:12s} PSF-2.0  https://www.python.org/")
    for d in dists:
        lines.append(f"  {d.metadata['Name']:28s} {d.version:12s} {licence_of(d)}  {_url(d)}")
    lines += ["", ""]
    texts = []
    if python_licence and Path(python_licence).is_file():
        text = Path(python_licence).read_text(encoding="utf-8", errors="replace")
        texts.append(("Python", "LICENSE.txt", text))
    for d in dists:
        files = licence_files(d)
        if not files:
            texts.append((d.metadata["Name"], "(no licence file in the wheel)",
                          f"{licence_of(d)}: see {_url(d)}\n"))
        for path, text in files:
            texts.append((d.metadata["Name"], path, text))
    for name, path, text in texts:
        head = f"{name}: {path}"
        lines += ["=" * 78, head, "=" * 78, "", text.rstrip(), "", ""]
    if any(n.lower() == "coppermesh" for n in listed):
        if not RUST_CRATES.is_file():
            raise SystemExit(f"coppermesh is in the bundle but {RUST_CRATES} is missing: "
                             "run tools/mesher_rs/crate_licences.py")
        lines += ["#" * 78, "", RUST_CRATES.read_text(encoding="utf-8").rstrip(), "", ""]
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listed
