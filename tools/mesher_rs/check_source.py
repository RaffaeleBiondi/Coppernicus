"""Say where the installed coppermesh comes from, and fail unless it is the one asked for.

    python tools/mesher_rs/check_source.py           # built from this folder (tools/mesher_rs)
    python tools/mesher_rs/check_source.py --pypi    # PyPI's wheel (an install from an index)

The source build and PyPI's wheel are both coppermesh 0.1.0: only the install record tells them
apart. ``uv pip install ./tools/mesher_rs`` (or pip) writes the folder into the distribution's
``direct_url.json`` (PEP 610); an install from an index writes none. uv.lock names PyPI's
wheel, so a plain ``uv sync`` or ``uv run`` puts it back over a source build without a word:
CI and the app builds run this check where they need the source build (and the wheel job where
it needs the wheel).

Exit code 0: the coppermesh asked for; 1: another one, or none installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

CRATE = Path(__file__).resolve().parent


def origin(dist) -> Path | None:
    """The local folder ``dist`` was installed from (``direct_url.json``), or None for an
    install from an index. ValueError for any other direct URL (a git URL, a wheel file)."""
    text = dist.read_text("direct_url.json")
    if not text:
        return None
    info = json.loads(text)
    url = urlparse(info["url"])
    if url.scheme != "file" or "dir_info" not in info:
        raise ValueError(f"installed from {info['url']}, not from a local folder")
    return Path(url2pathname(url.path))


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def problem(dist, pypi: bool = False, crate: Path = CRATE) -> str | None:
    """Why ``dist`` is not the coppermesh asked for (None: it is): built from ``crate``, or with
    ``pypi`` installed from an index."""
    try:
        where = origin(dist)
    except ValueError as e:
        return str(e)
    if pypi:
        return None if where is None else f"built from {where}, not PyPI's wheel"
    if where is None:
        return (f"installed from an index (PyPI's wheel), not built from {crate}: run "
                "`uv pip install ./tools/mesher_rs`, then `uv run --no-sync`")
    if not _same(where, crate):
        return f"built from {where}, not from {crate}"
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pypi", action="store_true",
                        help="expect PyPI's wheel (an install from an index) instead")
    args = parser.parse_args(argv)
    try:
        dist = metadata.distribution("coppermesh")
    except metadata.PackageNotFoundError:
        print("error: coppermesh is not installed", file=sys.stderr)
        return 1
    why = problem(dist, args.pypi)
    if why:
        print(f"error: coppermesh {dist.version} is {why}", file=sys.stderr)
        return 1
    print(f"coppermesh {dist.version}: " + ("PyPI's wheel" if args.pypi else f"built from {CRATE}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
