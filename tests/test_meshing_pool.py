"""KiPI's layer pool runs the installed mesher: its workers install the same backend with the same
settings, and a pooled solve is bit-identical to one meshed in one process. The pool is forced on a
repository example by lowering KiPI's thresholds; that the workers really meshed is checked, not
assumed, since KiPI meshes in this process when its pool fails (``POOL_ERRORS``)."""

import json
import os
from pathlib import Path

import numpy as np
import pytest

from coppernicus import mesh_worker
from coppernicus.engine import meshing
from coppernicus.engine.meshing import pipeline, pool, registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore():
    yield
    meshing.uninstall()


@pytest.fixture(params=["triangle", "rust"])
def name(request):
    if request.param not in meshing.available():
        pytest.skip(f"mesher {request.param!r} not available here")
    return request.param


@pytest.fixture
def forced(monkeypatch, tmp_path):
    """KiPI's layer pool on for any mesh, with two workers whatever the cores here; the workers'
    trace file; the pools started (each records whether it broke)."""
    import kipi.core.mesh.parallel as P
    import kipi.core.workers as W
    monkeypatch.delenv("KIPI_SERIAL", raising=False)
    monkeypatch.setattr(P, "MIN_TRIANGLES", 0)
    monkeypatch.setattr(P, "MIN_SPREAD", 0)
    monkeypatch.setattr(W, "spare", lambda: 2)
    trace = tmp_path / "workers.jsonl"
    monkeypatch.setenv(mesh_worker.TRACE_ENV, str(trace))
    started = []

    class Spied(P.Pooled):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)
    monkeypatch.setattr(P, "Pooled", Spied)

    def lines():
        return [json.loads(t) for t in trace.read_text().splitlines()] if trace.exists() else []
    return started, lines


def _demo():
    from coppernicus.engine.board import Board
    from coppernicus.project import Project
    yml = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
    project = Project.load(yml)
    return Board.open(yml.parent / project.board), project


def _solve(board, project, serial, monkeypatch):
    """The demo's rail, in one process (``KIPI_SERIAL=1``, as a user sets it) or in the pool;
    with the seam's per-polygon records made in this process."""
    from coppernicus.engine.solve import solve_rail
    if serial:
        monkeypatch.setenv("KIPI_SERIAL", "1")
    else:
        monkeypatch.delenv("KIPI_SERIAL", raising=False)
    pipeline.STATS.clear()
    res = solve_rail(board, project.rails[0], project.settings, project=project)
    here = list(pipeline.STATS)
    pipeline.STATS.clear()
    return res, here


def _same(a, b) -> bool:
    return (len(a.layers) == len(b.layers)
            and all(np.array_equal(x.points, y.points) and np.array_equal(x.tris, y.tris)
                    and np.array_equal(x.voltage, y.voltage)
                    and np.array_equal(x.current_density, y.current_density)
                    for x, y in zip(a.layers, b.layers))
            and (a.max_drop, a.power_loss, a.loss_vias) == (b.max_drop, b.power_loss, b.loss_vias))


def _meshed_in_workers(started, lines, here, name, settings):
    """Every polygon was meshed outside this process, by live workers that had ``name`` installed
    with ``settings``, and the workers' first step ran before KiPI's mesher or Triangle loaded.

    Each pool is sized for two workers, but how many of them the executor starts is its own
    business: it starts them as tasks come, and on Windows (slow to spawn) the first one may take
    all of the demo's polygons. So at least one worker, never this process."""
    assert started and not any(p.broken for p in started)        # the pool ran and never broke
    assert all(p.pool.n >= 2 for p in started)                   # sized for two workers
    assert here == []                                            # no polygon meshed here
    pids = {t["pid"] for t in lines}
    assert pids and os.getpid() not in pids
    for t in lines:
        assert t["installed"] == name and t["settings"] == settings
        assert t["early"] == {"triangle": False, "kipi_mesher": False}


def test_a_pooled_solve_is_bit_identical_to_one_process(name, forced, monkeypatch):
    started, lines = forced
    board, project = _demo()
    backend = meshing.install(name)
    assert registry.pooled()
    serial, here = _solve(board, project, True, monkeypatch)
    assert here and all(r["mesher"] == name for r in here)       # one process: meshed here, by the seam
    assert not started and not lines()
    pooled, here = _solve(board, project, False, monkeypatch)
    expected = {k: getattr(backend, k) for k in backend.settings}
    _meshed_in_workers(started, lines(), here, name, expected)
    assert _same(serial, pooled)


def test_the_rust_pool_is_not_kipis_mesh(forced, monkeypatch):
    """What makes the rust case discriminating: its mesh is not Triangle's, so workers meshing with
    KiPI's own Triangle would not give the rust result."""
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    if "triangle" not in meshing.available():
        pytest.skip("mesher 'triangle' not available here")
    board, project = _demo()
    meshing.uninstall()
    kipi_own, _ = _solve(board, project, False, monkeypatch)
    meshing.install("rust")
    rust, _ = _solve(board, project, False, monkeypatch)
    assert not _same(kipi_own, rust)


def test_a_setting_changed_after_install_reaches_the_workers(forced, monkeypatch):
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    started, lines = forced
    board, project = _demo()
    backend = meshing.install("rust")
    area, _ = _solve(board, project, True, monkeypatch)
    backend.size_rule = "edge"
    backend.tiny_angle_deg = 0.5
    edge, _ = _solve(board, project, True, monkeypatch)
    assert not _same(area, edge)                                 # the setting changes the mesh
    pooled, here = _solve(board, project, False, monkeypatch)
    _meshed_in_workers(started, lines(), here, "rust",
                       {"size_rule": "edge", "min_edge_mm": None, "tiny_angle_deg": 0.5})
    assert _same(edge, pooled)


def test_a_setting_that_is_not_plain_data_meshes_in_one_process(forced, monkeypatch):
    """A worker could not be given it: KiPI's pool does not start (the RuntimeError is one of its
    POOL_ERRORS) and the solve meshes here, with the setting."""
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    started, lines = forced
    board, project = _demo()
    backend = meshing.install("rust")
    backend.min_edge_mm = np.float32(2e-6)                      # not a plain value
    with pytest.raises(RuntimeError, match="min_edge_mm"):
        registry._worker_spec()
    serial, _ = _solve(board, project, True, monkeypatch)
    fallback, here = _solve(board, project, False, monkeypatch)
    assert not started and not lines() and here                 # no pool: meshed here
    assert _same(serial, fallback)


def test_kipi_serial_set_by_the_user_is_kept(name, monkeypatch):
    import kipi.core.mesh.parallel as P
    import kipi.core.workers as W
    for value in (None, "0", "1"):
        if value is None:
            monkeypatch.delenv("KIPI_SERIAL", raising=False)
        else:
            monkeypatch.setenv("KIPI_SERIAL", value)
        before = (P.mesh_layers, W._start)
        meshing.install(name)
        assert os.environ.get("KIPI_SERIAL") == value and P.mesh_layers is before[0]
        assert isinstance(W._start, pool.WorkerStart) and W._start.kipi_start is before[1]
        meshing.uninstall()
        assert os.environ.get("KIPI_SERIAL") == value and (P.mesh_layers, W._start) == before


@pytest.mark.parametrize("how", ["registered", "shadowing", "stats", "unpicklable"])
def test_what_a_worker_cannot_rebuild_stays_in_one_process(how, monkeypatch):
    import kipi.core.mesh.parallel as P
    import kipi.core.workers as W
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    from coppernicus.engine.meshing.rust_backend import RustBackend
    options = {}
    name = "rust"
    if how == "registered":
        name = "rust_edge_pool_test"
        monkeypatch.setitem(registry._factories, name, lambda: RustBackend("edge"))
    elif how == "shadowing":
        monkeypatch.setitem(registry._factories, "rust", lambda: RustBackend("edge"))
    elif how == "stats":
        options = {"stats": []}
    else:
        options = {"empty": lambda: None}
    for value in (None, "0"):
        if value is None:
            monkeypatch.delenv("KIPI_SERIAL", raising=False)
        else:
            monkeypatch.setenv("KIPI_SERIAL", value)
        before = (P.mesh_layers, W._start)
        meshing.install(name, **options)
        assert registry.serial_reason(name, options) and not registry.pooled()
        assert meshing.seam_problem() is None
        assert os.environ["KIPI_SERIAL"] == "1" and P.mesh_layers(None) is None and W._start is before[1]
        meshing.uninstall()
        assert os.environ.get("KIPI_SERIAL") == value and (P.mesh_layers, W._start) == before


def test_the_3d_view_pool_runs_the_installed_mesher(forced, monkeypatch):
    """KiPI's 3D-view pool (``kipi.core.viz.parallel.build_nets``, the nets' meshes as arrays; no
    plotter, no window) hooks into the same initializer: its workers install the backend and its
    ``triangle`` stand-in before KiPI's ``_start`` loads the view there, and the nets come out as
    built in one process."""
    if "rust" not in meshing.available():
        pytest.skip("rust not available here")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("PYVISTA_OFF_SCREEN", "true")
    import kipi.core.viz.parallel as VP
    from kipi.core.viz.batch import Packed, _arrays
    _, lines = forced
    board, _ = _demo()
    snap = board.snapshot
    nets = snap.net_names()
    meshing.install("rust")

    def build(n):
        monkeypatch.setattr(VP, "workers", lambda n_nets: n)
        return {ns.net: ns for ns in VP.build_nets(snap, list(nets), {}) if not isinstance(ns, VP.Coatings)}

    def arrays(ns):
        return [(lm.layer_id, *(np.asarray(a).tobytes() for a in _arrays(lm.mesh))) for lm in ns.copper]
    here = build(0)
    assert not lines()
    pooled = build(2)
    assert sorted(here) == sorted(pooled) == sorted(nets)
    assert any(ns.copper for ns in pooled.values())
    assert all(isinstance(lm.mesh, Packed) for ns in pooled.values() for lm in ns.copper)  # from the pool
    assert all(arrays(here[k]) == arrays(pooled[k]) for k in nets)
    traced = lines()
    pids = {t["pid"] for t in traced}                  # how many of the two start is the executor's
    assert pids and os.getpid() not in pids
    for t in traced:
        assert t["installed"] == "rust" and t["early"] == {"triangle": False, "kipi_mesher": False}
        assert t["view_triangle"] == "coppernicus.engine.meshing.rust_backend:TriangleCompat"


@pytest.mark.parametrize("exe", ["Coppernicus.app/Contents/MacOS/Coppernicus", "Coppernicus/Coppernicus.exe",
                                 "Coppernicus/coppernicus-cli.exe"])
def test_the_packaged_app_still_meshes_in_one_process(name, exe, forced, monkeypatch, tmp_path):
    """The packaged app's programs (packaging/coppernicus.spec) are not a Python: KiPI's
    ``can_spawn()`` says no, so no pool is built and the hook is never pickled, mesher installed
    or not."""
    import sys

    import kipi.core.workers as W
    started, lines = forced
    board, project = _demo()
    monkeypatch.setattr(sys, "executable", str(tmp_path / exe))
    assert not W.can_spawn() and W.workers(16) == 0
    meshing.install(name)
    assert registry.pooled()
    _, here = _solve(board, project, False, monkeypatch)
    assert here and not started and not lines()                 # meshed here, no worker
