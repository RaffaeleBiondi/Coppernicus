"""Per-stage timing by runtime wrapping, in the measuring process only (nothing under src/
changes).

:func:`install` wraps KiPI's and the engine's stage functions and methods with a
:class:`Recorder` span. Functions are bound by name in several modules (``from X import f``),
so every ``kipi.*`` / ``coppernicus.*`` module global that *is* the original is rebound;
methods are wrapped on their class. Each target records how many bindings it replaced
(``patched``), each stage its calls: a stage that should have run with no call is a missed
patch, and the step's ``unattributed_s`` (wall minus the stages timed in the main thread)
shows time no stage covers.

Stage times are exclusive: a stage running inside another (``factor`` inside
``system_setup``) is taken out of the outer one, so the stages of one thread add up to at
most the wall time. Stages also run in KiPI's threads (``ac_port_solve`` in the AC field
solves' "kipi-field" threads): ``seconds`` sums every thread, ``main_seconds`` the main
thread's only, ``threads`` counts the threads seen. KiPI's mesher worker processes are not
reached: a pooled mesh is seen as its wall time in ``mesh`` (and ``mesh_pool_start``).

KiPI's own ``ProgressReporter.timed`` blocks are recorded too (``kipi_timed``, inclusive,
labels with net names and counts folded: "Meshing VCC" -> "meshing"); those that are no
function of their own (a noise victim, the AC sweep...) are also stages (``TIMED_STAGES``).
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import re
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Tuple

MAIN = threading.main_thread()


class StopAfterMesh(BaseException):
    """Raised after the rail's mesh is made, to time the mesh alone (the ``mesh`` step)."""

    def __init__(self, mesh):
        super().__init__("stopped after meshing")
        self.mesh = mesh


class Recorder:
    """Stage times (exclusive, per thread), KiPI's timed blocks and counts of one measurement."""

    def __init__(self):
        self._lock = threading.Lock()
        self._local = threading.local()
        self.stop_after_mesh = False
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.stages: Dict[str, Dict[str, Any]] = {}
            self.timed: Dict[str, Dict[str, Any]] = {}
            self.counts: Dict[str, Any] = {}
            self.hook_errors: List[str] = []

    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    @contextmanager
    def span(self, stage: str):
        stack = self._stack()
        frame = [0.0, stage]               # time of the stages inside this one, its name
        stack.append(frame)
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            stack.pop()
            if stack:
                stack[-1][0] += dt
            self._add(stage, dt - frame[0], dt)

    def _add(self, stage: str, exclusive: float, inclusive: float) -> None:
        thread = threading.current_thread()
        with self._lock:
            s = self.stages.setdefault(stage, {"seconds": 0.0, "main_seconds": 0.0, "inclusive_s": 0.0,
                                               "calls": 0, "threads": set()})
            s["seconds"] += exclusive
            s["inclusive_s"] += inclusive
            if thread is MAIN:
                s["main_seconds"] += exclusive
            s["calls"] += 1
            s["threads"].add(thread.ident)

    def add_timed(self, label: str, seconds: float) -> None:
        name = timed_name(label)
        with self._lock:
            t = self.timed.setdefault(name, {"seconds": 0.0, "calls": 0})
            t["seconds"] += seconds
            t["calls"] += 1

    def observe(self, key: str, value, how: str = "sum") -> None:
        """A count seen during the step: summed, the largest kept, or a set of values."""
        if value is None:
            return
        with self._lock:
            if how == "sum":
                self.counts[key] = self.counts.get(key, 0) + value
            elif how == "max":
                self.counts[key] = max(self.counts.get(key, value), value)
            else:
                self.counts.setdefault(key, set()).add(value)

    def parent(self) -> Optional[str]:
        """The stage running now in this thread, if any."""
        stack = self._stack()
        return stack[-1][1] if stack else None

    def wrap(self, fn: Callable, stage, after: Optional[Callable] = None) -> Callable:
        """``fn`` timed as ``stage``: a name, None (not timed, only ``after``), or a function
        of the enclosing stage's name giving the name (None: not timed there)."""
        rec = self

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            name = stage(rec.parent()) if callable(stage) else stage
            if name is None:
                out = fn(*args, **kwargs)
            else:
                with rec.span(name):
                    out = fn(*args, **kwargs)
            if after is not None:
                try:
                    after(rec, args, kwargs, out)
                except StopAfterMesh:
                    raise
                except Exception as e:     # noqa: BLE001 - a count lost, never the solve
                    rec.hook_errors.append(f"{stage}: {type(e).__name__}: {e}")
            return out
        wrapper.__bench_original__ = fn
        return wrapper

    def snapshot(self, wall_s: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            stages = {k: {**v, "threads": len(v["threads"])} for k, v in self.stages.items()}
            counts = {k: (sorted(v, key=str) if isinstance(v, set) else v) for k, v in self.counts.items()}
            out = {"stages": stages, "kipi_timed": {k: dict(v) for k, v in self.timed.items()},
                   "counts": counts, "hook_errors": list(self.hook_errors)}
        if wall_s is not None:
            out["unattributed_s"] = wall_s - sum(v["main_seconds"] for v in stages.values())
        return out


# KiPI's timed() labels folded to names without nets, parts or counts.
_TIMED = [(re.compile(r"^Meshing rail and ground"), "meshing rail and ground"),
          (re.compile(r"^Meshing "), "meshing"),
          (re.compile(r"^Sweep: "), "sweep"),
          (re.compile(r"^phase \d+ layout"), "phase layout"),
          (re.compile(r"^victim "), "victim")]


#: KiPI timed blocks that are no function of their own: also stages (exclusive, as the rest).
TIMED_STAGES = {"victim": "noise_victim", "field views": "noise_field_views",
                "phase layout": "noise_phase_layout", "scan": "noise_scan", "sweep": "ac_sweep",
                "checking load connections": "tree_check_loads"}


def timed_name(label: str) -> str:
    for pattern, name in _TIMED:
        if pattern.search(label):
            return name
    return re.sub(r"\d+", "#", label).strip().lower()


# --------------------------------------------------------------------------- counts (hooks)

def _after_mesh_many(rec: Recorder, args, kwargs, meshes) -> None:
    tris = [int(m.n_tris) for m in meshes]
    rec.observe("triangles", sum(tris))
    rec.observe("nodes", sum(int(m.n_nodes) for m in meshes))
    rec.observe("max_mesh_triangles", max(tris, default=0), "max")
    rec.observe("mesh_calls", 1)


def _after_mesh_nets(rec: Recorder, args, kwargs, mesh) -> None:
    if rec.stop_after_mesh:
        raise StopAfterMesh(mesh)


def _after_pool(rec: Recorder, args, kwargs, pooled) -> None:
    rec.observe("pool_workers", int(pooled.pool.n) if pooled is not None else 0, "max")


def _after_assemble(rec: Recorder, args, kwargs, out) -> None:
    K, nm = out[0], int(out[2])
    rec.observe("unknowns", nm, "max")
    rec.observe("unknowns_total", nm)
    rec.observe("nnz", int(K.nnz), "max")
    rec.observe("dc_systems", 1)


def _after_factor(rec: Recorder, args, kwargs, out) -> None:
    rec.observe("solver_method", str(args[0].stats.get("method")), "set")


def _after_ac_system(rec: Recorder, args, kwargs, out) -> None:
    system = args[0]
    rec.observe("unknowns", int(system.n), "max")
    rec.observe("ac_systems", 1)
    nnz = getattr(system.stats, "nnz", None)
    if nnz is not None:
        rec.observe("nnz", int(nnz), "max")


def _after_field_threads(rec: Recorder, args, kwargs, n) -> None:
    rec.observe("field_threads", int(n), "max")


def _after_complex_factor(rec: Recorder, args, kwargs, factor) -> None:
    name = getattr(factor, "name", None)
    if name:
        rec.observe("ac_factor_method", str(name).split()[0], "set")


def _thermal_factor(parent: Optional[str]) -> Optional[str]:
    """KiPI's heat model factorizes its matrix itself (kipi/dc/thermal/model.py:282); elsewhere
    a direct factorization is inside ``factor`` (SpdSolver) already."""
    return "thermal_factor" if parent == "thermal_model" else None


# (module, function, stage, hook): functions, rebound wherever they are bound by name.
FUNCTIONS: List[Tuple[str, str, Any, Optional[Callable]]] = [
    ("kipi.core.mesh.mesher", "mesh_many", "mesh", _after_mesh_many),
    ("kipi.core.mesh.mesher", "mesh_nets", "mesh_combine", _after_mesh_nets),
    ("kipi.core.mesh.parallel", "mesh_layers", "mesh_pool_start", _after_pool),
    ("kipi.dc.solver.fem", "assemble", "assemble", _after_assemble),
    ("kipi.dc.solver.post", "compute_fields", "post", None),
    ("kipi.core.solver.direct", "factorize", _thermal_factor, None),
    ("kipi.ac.solver.sweep", "build_ports", "ac_ports", None),
    ("kipi.ac.solver.field", "field_solves", "ac_field_solves", None),
    ("kipi.ac.solver.field", "threads", None, _after_field_threads),
    ("kipi.noise.solver", "_copper", "noise_copper", None),
    ("kipi.noise.solver", "_board_model", "noise_board_model", None),
    ("kipi.noise.solver", "_victims", "noise_victims", None),
    ("kipi.noise.solver", "_fields", "noise_fields", None),
    ("kipi.sources.kicad_pcb.reader", "load_tree", "kicad_parse", None),
    ("kipi.sources.kicad_pcb.reader", "read_footprints", "kicad_footprints", None),
    ("kipi.sources.kicad_pcb.reader", "read_zones", "kicad_zones", None),
    ("kipi.sources.kicad_pcb.reader", "trim_unused_layers", "kicad_trim", None),
    ("kipi.sources.kicad_pcb.reader", "trim_unused_via_pads", "kicad_trim", None),
    ("kipi.sources.kicad_pcb.reader", "read_outline", "kicad_outline", None),
    ("coppernicus.board_fields", "apply", "board_fields", None),
    # Entry points (PHASES): their exclusive time is Coppernicus' own work around KiPI's.
    ("coppernicus.engine.solve", "solve_rail", "dc_solve", None),
    ("coppernicus.engine.tree", "solve_project_tree", "tree_solve", None),
    ("coppernicus.engine.ac", "solve_ac_rail", "ac_solve", None),
    ("coppernicus.engine.ac", "ac_size", "ac_size", None),
    ("coppernicus.engine.noise", "solve_aggressor", "noise_solve", None),
    ("coppernicus.engine.noise", "mesh_estimate", "noise_size", None),
    ("coppernicus.engine.solve", "_convert", "convert", None),
    ("coppernicus.engine.tree", "convert_tree", "convert", None),
    ("coppernicus.engine.thermal", "_finish", "convert", None),
    ("coppernicus.engine.ac", "_convert", "convert", None),
    ("coppernicus.problems", "find_problems", "problems", None),
]
#: Stages each step always runs (a missing call means a patch that no longer reaches its
#: target, e.g. after a KiPI update renamed a private function).
_DC = ("mesh", "assemble", "factor", "solve", "post", "convert")
EXPECTED = {"mesh": ("mesh",), "dc": _DC + ("problems", "dc_solve"), "tree": _DC + ("tree_solve",),
            "thermal": _DC + ("tree_solve", "thermal_model", "thermal_factor", "thermal_solve"),
            "ac_estimate": ("mesh", "ac_size"),
            "ac": ("mesh", "ac_model", "ac_factor", "ac_port_solve", "ac_field_solves", "convert",
                   "ac_solve"),
            "noise_estimate": ("noise_size",), "noise": ("noise_solve",), "tolerance": ("tree_solve",)}


def missing_stages(step: str, stages: Dict[str, Any], kind: str = "example") -> List[str]:
    """The stages ``step`` always runs that recorded no call."""
    expected = EXPECTED.get(step, ())
    if step == "open" and kind != "synthetic":
        expected = ("project_load", "board_open")
    return [s for s in expected if not (stages.get(s) or {}).get("calls")]


#: Stages that are entry points or wrap others: reports also give their inclusive time.
PHASES = ("board_open", "dc_solve", "tree_solve", "ac_solve", "ac_size", "noise_solve", "noise_size")
# (module, class, method, stage, hook): methods, wrapped on their class.
METHODS: List[Tuple[str, str, str, Optional[str], Optional[Callable]]] = [
    ("coppernicus.project", "Project", "load", "project_load", None),
    ("coppernicus.engine.board", "Board", "open", "board_open", None),
    ("kipi.dc.solver.fem", "LinearSystem", "__init__", "system_setup", None),
    ("kipi.core.solver.spd", "SpdSolver", "_setup", "factor", _after_factor),
    ("kipi.core.solver.spd", "SpdSolver", "solve", "solve", None),
    ("kipi.ac.solver.fem", "ACSystem", "__init__", "ac_model", _after_ac_system),
    ("kipi.ac.solver.fem", "ACSystem", "port_impedance", "ac_port_solve", None),
    ("kipi.core.solver.direct", "ComplexSeries", "factorize", "ac_factor", _after_complex_factor),
    ("kipi.dc.thermal.model", "BoardThermalModel", "__init__", "thermal_model", None),
    ("kipi.dc.thermal.model", "BoardThermalModel", "solve", "thermal_solve", None),
]
# Imported first, so every name-bound copy of a target exists when it is rebound.
MODULES = ("kipi.core.workers", "kipi.core.mesh.mesher", "kipi.core.mesh.parallel", "kipi.core.solver.spd",
           "kipi.core.solver.direct", "kipi.dc.solver", "kipi.dc.solver.fem", "kipi.dc.solver.post",
           "kipi.dc.pi.tree", "kipi.dc.thermal.model", "kipi.dc.thermal.cosim", "kipi.ac.solver.fem",
           "kipi.ac.solver.field", "kipi.ac.solver.sweep", "kipi.noise.solver",
           "kipi.sources.kicad_pcb.reader", "coppernicus.project",
           "coppernicus.board_fields", "coppernicus.problems", "coppernicus.engine.board",
           "coppernicus.engine.solve", "coppernicus.engine.tree", "coppernicus.engine.thermal",
           "coppernicus.engine.ac", "coppernicus.engine.noise", "coppernicus.engine.tolerance",
           "coppernicus.tolerance", "coppernicus.power_tree", "coppernicus.service.session",
           "coppernicus.cli")


def _rebind(original, wrapper) -> int:
    """Replace ``original`` by ``wrapper`` in every kipi / coppernicus module that binds it."""
    n = 0
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "kipi" or name.startswith(("kipi.", "coppernicus"))):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                setattr(module, attr, wrapper)
                n += 1
    return n


def install(rec: Recorder) -> Dict[str, int]:
    """Wrap every target; returns, per target, how many bindings were replaced."""
    for name in MODULES:
        importlib.import_module(name)
    patched: Dict[str, int] = {}
    for mod_name, fn_name, stage, after in FUNCTIONS:
        original = getattr(sys.modules[mod_name], fn_name)
        patched[f"{mod_name}.{fn_name}"] = _rebind(original, rec.wrap(original, stage, after))
    for mod_name, cls_name, meth, stage, after in METHODS:
        cls = getattr(sys.modules[mod_name], cls_name)
        raw = cls.__dict__[meth]
        if isinstance(raw, classmethod):
            setattr(cls, meth, classmethod(rec.wrap(raw.__func__, stage, after)))
        else:
            setattr(cls, meth, rec.wrap(raw, stage, after))
        patched[f"{mod_name}.{cls_name}.{meth}"] = 1
    from kipi.core.progress import ProgressReporter
    original_timed = ProgressReporter.timed

    @contextmanager
    def timed(self, name):
        stage = TIMED_STAGES.get(timed_name(name))
        t0 = time.perf_counter()
        try:
            with rec.span(stage) if stage else contextlib.nullcontext():
                with original_timed(self, name):
                    yield
        finally:
            rec.add_timed(name, time.perf_counter() - t0)
    ProgressReporter.timed = timed
    patched["kipi.core.progress.ProgressReporter.timed"] = 1
    return patched
