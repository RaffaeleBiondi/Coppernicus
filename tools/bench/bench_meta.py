"""What a benchmark run ran on: code, libraries, BLAS and thread settings, machine, load.

Times are only comparable between runs with the same of these (or with the differences
known): ``compare`` prints the keys that differ.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import subprocess
import sys
from importlib import metadata as _md
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

#: Environment variables that set how many threads BLAS, OpenMP and the solvers use.
THREAD_VARS = ("OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
               "NUMEXPR_NUM_THREADS", "KIPI_SERIAL")
#: Keys whose difference between two runs is worth saying before their times are compared.
COMPARED_KEYS = ("git_commit", "git_dirty", "kipi_version", "kipi_commit", "python", "numpy", "scipy",
                 "blas", "kipi_direct_backend", "thread_env", "child_env", "cpu_count", "ram_bytes",
                 "machine", "mesher")


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _version(dist: str) -> Optional[str]:
    try:
        return _md.version(dist)
    except _md.PackageNotFoundError:
        return None


def _kipi_commit() -> Optional[str]:
    """The KiPI commit pip/uv installed (its dist-info ``direct_url.json``), if from git."""
    try:
        info = json.loads(_md.distribution("kipi").read_text("direct_url.json") or "{}")
    except (_md.PackageNotFoundError, ValueError, OSError):
        return None
    return (info.get("vcs_info") or {}).get("commit_id")


def _code_state(package_dir: Path, exclude: Sequence[Path] = ()) -> Dict[str, Any]:
    """The git state of the checkout holding ``package_dir`` (the coppernicus the children
    import, which may not be the checkout holding tools/bench): its root, commit, branch,
    whether tracked files changed, and the untracked files outside ``exclude``."""
    root = _git(package_dir, "rev-parse", "--show-toplevel")
    if root is None:
        return {"git_repo": None, "git_commit": None, "git_branch": None, "git_dirty": None,
                "git_untracked": None}
    repo = Path(root)
    tracked = _git(repo, "status", "--porcelain", "--untracked-files=no")
    listed = _git(repo, "status", "--porcelain", "--untracked-files=all") or ""
    skip = [Path(e).resolve() for e in exclude]
    untracked = 0
    for line in listed.splitlines():
        if not line.startswith("?? "):
            continue
        path = (repo / line[3:].strip().strip('"')).resolve()
        if not any(path == d or d in path.parents for d in skip):
            untracked += 1
    return {"git_repo": str(repo), "git_commit": _git(repo, "rev-parse", "HEAD"),
            "git_branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": None if tracked is None else bool(tracked), "git_untracked": untracked}


def _blas(module) -> Optional[Dict[str, Any]]:
    """BLAS and LAPACK as the library was built with (``show_config(mode="dicts")``)."""
    try:
        cfg = module.show_config(mode="dicts")
    except Exception:                               # noqa: BLE001 - an old numpy/scipy: say nothing
        return None
    deps = cfg.get("Build Dependencies", {}) if isinstance(cfg, dict) else {}
    return {k: {f: deps[k].get(f) for f in ("name", "version", "openblas configuration")}
            for k in ("blas", "lapack") if isinstance(deps.get(k), dict)}


def _cpu_name() -> Optional[str]:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                 text=True, timeout=5)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return platform.processor() or None


def run_metadata(repo: Path, exclude: Sequence[Path] = ()) -> Dict[str, Any]:
    """The run's code: git state of the coppernicus package imported (``git_dirty``: tracked
    files changed; untracked files outside ``exclude``, e.g. the run's --out, counted apart),
    KiPI's version and commit, both packages' files, the harness's own commit (``repo``);
    libraries (Python, numpy, scipy, BLAS), KiPI's direct solver backend, thread variables,
    CPU count, RAM, load and date."""
    import kipi
    import numpy
    import scipy

    import coppernicus
    from coppernicus import __version__
    from coppernicus.engine.ac import free_memory_bytes, total_memory_bytes

    try:
        from kipi.core.solver import direct
        backend = direct.backend()
    except Exception:                               # noqa: BLE001 - recorded as unknown
        backend = None
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        load = list(os.getloadavg())
    except (AttributeError, OSError):               # Windows
        load = None
    return {
        **_code_state(Path(coppernicus.__file__).resolve().parent, exclude),
        "bench_commit": _git(repo, "rev-parse", "HEAD"),
        "coppernicus_version": __version__,
        "coppernicus_file": coppernicus.__file__,
        "kipi_version": _version("kipi"),
        "kipi_commit": _kipi_commit(),
        "kipi_file": kipi.__file__,
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy": numpy.__version__,
        "scipy": scipy.__version__,
        "pyamg": _version("pyamg"),
        "triangle": _version("triangle"),
        "shapely": _version("shapely"),
        "blas": {"numpy": _blas(numpy), "scipy": _blas(scipy)},
        "kipi_direct_backend": backend,
        "thread_env": {k: os.environ.get(k) for k in THREAD_VARS},
        "cpu_count": os.cpu_count(),
        "cpu": _cpu_name(),
        "ram_bytes": total_memory_bytes(),
        "free_memory_bytes_kipi": free_memory_bytes(),
        "load_average": load,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "date": now.isoformat(timespec="seconds"),
        "date_local": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def differences(meta_a: Dict[str, Any], meta_b: Dict[str, Any]):
    """The compared keys whose values differ, as (key, A, B)."""
    return [(k, meta_a.get(k), meta_b.get(k)) for k in COMPARED_KEYS if meta_a.get(k) != meta_b.get(k)]
