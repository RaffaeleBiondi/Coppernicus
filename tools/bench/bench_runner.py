"""One measurement = one fresh subprocess (``bench_child.py``), watched from outside.

The child reads its own peak RSS (a per-process high-water mark) and its workers' (the
largest reaped child's). KiPI's mesher pool spawns worker processes, so the runner also
samples the whole process tree's resident memory every ``sample_s`` with ``ps`` (macOS and
Linux; none on Windows, where it is left out) and keeps the largest sum: an approximation
that misses peaks shorter than the interval. The child runs in its own session, so a
timeout or the RSS cap kills its workers with it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
CHILD = HERE / "bench_child.py"
REPO = HERE.parents[1]

# An absolute path: POSIX (a "/" not after a word character, ".", ">" or "~", so "1/3",
# "A/mm2" and "<repo>/src" stay) or Windows ("C:\\..."), up to a space, quote or bracket.
_ABS_PATH = re.compile(r"(?<![\w.>~])/[^\s'\"`,;()\[\]{}<>]+|\b[A-Za-z]:\\[^\s'\"`,;()\[\]{}<>]+")


def redact(text: Optional[str], private: Sequence[str] = ()) -> Optional[str]:
    """``text`` with this repository's folder written ``<repo>``, every other absolute path
    ``<path>``, and each of ``private`` (an external project's file and board names)
    ``<private>``: error messages and tracebacks go into run files that may be shared."""
    if not text:
        return text
    text = text.replace(str(REPO), "<repo>")
    text = _ABS_PATH.sub("<path>", text)
    for name in sorted({p for p in private if p}, key=len, reverse=True):
        text = text.replace(name, "<private>")
    return text


def maxrss_bytes(value: float, platform: str = sys.platform) -> int:
    """``ru_maxrss`` in bytes: macOS gives bytes, Linux kilobytes."""
    return int(value) if platform == "darwin" else int(value) * 1024


def tree_rss_kib(ps_text: str, root: int) -> Optional[int]:
    """Resident memory (KiB) of process ``root`` and all its descendants, from the lines of
    ``ps -A -o pid=,ppid=,rss=``; None when ``root`` is not listed."""
    rss: Dict[int, int] = {}
    kids: Dict[int, List[int]] = {}
    for line in ps_text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, kib = (int(p) for p in parts)
        except ValueError:
            continue
        rss[pid] = kib
        kids.setdefault(ppid, []).append(pid)
    if root not in rss:
        return None
    total, todo, seen = 0, [root], set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        todo += kids.get(pid, [])
    return total


def _can_sample() -> bool:
    """Process-tree memory is read with ``ps``: macOS and Linux only."""
    return os.name == "posix"


def _ps() -> Optional[str]:
    try:
        return subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True,
                              timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None


class _Sampler(threading.Thread):
    def __init__(self, pid: int, interval: float, cap_bytes: Optional[float]):
        super().__init__(daemon=True)
        self.pid, self.interval, self.cap = pid, interval, cap_bytes
        self.peak_bytes: Optional[int] = None
        self.samples = 0
        self.failures = 0                  # ps calls that gave nothing (retried at the next tick)
        self.over_cap = False
        self._halt = threading.Event()

    def run(self):
        if not _can_sample():
            return
        while not self._halt.is_set():
            text = _ps()
            if text is None:
                self.failures += 1
                self._halt.wait(self.interval)
                continue
            kib = tree_rss_kib(text, self.pid)
            if kib is not None:
                self.samples += 1
                b = kib * 1024
                self.peak_bytes = b if self.peak_bytes is None else max(self.peak_bytes, b)
                if self.cap is not None and b > self.cap:
                    self.over_cap = True
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def run_child(spec: Dict[str, Any], work: Path, log_path: Path, timeout_s: Optional[float] = None,
              max_rss_bytes: Optional[float] = None, sample_s: float = 0.25,
              env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run one measurement; the child's record, with the runner's own readings (tree RSS,
    exit code, why it was killed) added."""
    work.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path = work / f"{spec['id']}.spec.json"
    out_path = work / f"{spec['id']}.out.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    out_path.unlink(missing_ok=True)
    cmd = [sys.executable, str(CHILD), str(spec_path), str(out_path)]
    try:
        load = os.getloadavg()[0]          # the machine's load as this measurement starts
    except (AttributeError, OSError):      # Windows
        load = None
    t0 = time.perf_counter()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(work),
                                start_new_session=(os.name == "posix"))
        sampler = _Sampler(proc.pid, sample_s, max_rss_bytes)
        sampler.start()
        killed = None
        while proc.poll() is None:
            if timeout_s is not None and time.perf_counter() - t0 > timeout_s:
                killed = f"timeout after {timeout_s:g} s"
            elif sampler.over_cap:
                killed = f"process tree over the RSS cap ({max_rss_bytes / 1e9:g} GB)"
            if killed:
                _kill_tree(proc)
                proc.wait()
                break
            time.sleep(0.05)
        sampler.stop()
        sampler.join(timeout=15)
    elapsed = time.perf_counter() - t0
    try:
        record = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
        record = {"error": killed or f"the measuring process exited with {proc.returncode} and no record: "
                  + " / ".join(tail)}
    if killed:
        record["error"] = killed
    # The tree holds the child: its own exact peak is a floor for the sampled sum, which can
    # miss a short peak between two samples.
    floor = [b for b in (sampler.peak_bytes, record.get("rss_peak_bytes")) if b]
    record.update({"returncode": proc.returncode, "killed": killed, "process_s": elapsed, "load_1m": load,
                   "rss_tree_peak_bytes": max(floor) if floor else None,
                   "rss_tree_sampled_bytes": sampler.peak_bytes, "rss_tree_samples": sampler.samples,
                   "rss_tree_sample_failures": sampler.failures})
    spec_path.unlink(missing_ok=True)
    out_path.unlink(missing_ok=True)
    return record
