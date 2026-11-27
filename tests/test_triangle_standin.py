"""Coppernicus without Triangle (coppernicus.triangle_standin): KiPI loads with the stand-in in
Triangle's place; KiPI's own mesher then stops with one clear error; the "triangle" backend of the
seam is not available; the rust backend solves; a real Triangle is never replaced.

Each case runs in a fresh process. Where Triangle is installed, a meta-path finder blocks it there,
so the cases run everywhere (and in the Triangle-free CI job, .github/workflows/triangle-free.yml)."""

import importlib.machinery
import importlib.util
import json
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coppernicus import triangle_standin as ts

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
HAS_TRIANGLE = importlib.machinery.PathFinder.find_spec("triangle") is not None   # not sys.modules
HAS_RUST = importlib.util.find_spec("coppermesh") is not None

BLOCK = '''
import importlib.abc, sys
class _NoTriangle(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "triangle" or name.startswith("triangle."):
            raise ModuleNotFoundError("No module named 'triangle' (blocked by the test)", name=name)
_BLOCKER = _NoTriangle()
sys.meta_path.insert(0, _BLOCKER)
'''


def _run(code: str, block: bool = True, timeout: int = 300) -> str:
    env = {**os.environ, "KIPI_SERIAL": "1", "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
    script = "import sys\n" + (BLOCK if block else "") + textwrap.dedent(code)
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", cwd=ROOT, env=env)
    assert run.returncode == 0, run.stderr[-3000:]
    return run.stdout


# --------------------------------------------------------------------------- in this process
def test_the_standin_raises_one_clear_error_that_survives_a_pool_worker():
    assert ts.is_standin(ts.STANDIN) and ts.STANDIN.__name__ == "triangle"
    with pytest.raises(ts.TriangleNotInstalled) as caught:
        ts.STANDIN.triangulate({"vertices": [], "segments": []}, "pQ")
    e = caught.value
    assert isinstance(e, RuntimeError) and e.code == "triangle_unavailable"
    assert "Triangle is not installed" in str(e) and "rust mesher" in str(e)
    back = pickle.loads(pickle.dumps(e))
    assert type(back) is ts.TriangleNotInstalled and back.to_dict() == e.to_dict()


def test_whatever_holds_triangle_is_kept(monkeypatch):
    other = object()
    monkeypatch.setitem(sys.modules, "triangle", other)
    assert ts.ensure_triangle_importable() is False and sys.modules["triangle"] is other
    monkeypatch.setitem(sys.modules, "triangle", ts.STANDIN)
    assert ts.ensure_triangle_importable() is True and sys.modules["triangle"] is ts.STANDIN


def test_the_mcp_server_returns_it_as_a_structured_error():
    from coppernicus import mcp_server

    def solve():
        raise ts.TriangleNotInstalled()
    result = mcp_server._call("solve_rail", solve)
    assert result.is_error and result.structured_content["error"] == ts.TriangleNotInstalled().to_dict()


# --------------------------------------------------------------------------- without Triangle
def test_coppernicus_and_kipi_load_without_triangle():
    out = _run('''
        import coppernicus.engine
        from coppernicus import triangle_standin as ts
        import kipi.core.mesh.mesher as M
        import kipi.core.viz.meshes as V
        from shapely.geometry import box
        assert sys.modules["triangle"] is ts.STANDIN and M.triangle is ts.STANDIN
        assert ts.ensure_triangle_importable() and ts.ensure_triangle_importable()   # idempotent
        assert sys.modules["triangle"] is ts.STANDIN and ts.standin_in_use()
        assert V._triangle_fast(box(0, 0, 2, 1)) is None      # the 3D view falls back to GEOS
        print("ok")
    ''')
    assert out.strip().endswith("ok")


def test_kipis_own_mesher_stops_with_the_clear_error():
    out = _run(f'''
        import json
        from coppernicus import triangle_standin as ts
        from coppernicus.engine import meshing
        from coppernicus.engine.board import Board
        from coppernicus.engine.solve import solve_rail
        from coppernicus.project import Project
        assert meshing.installed() is None                    # KiPI's own path
        p = Project.load({str(DEMO)!r})
        b = Board.open(p.board_path())
        try:
            solve_rail(b, p.rails[0], p.settings, project=p)
        except ts.TriangleNotInstalled as e:
            print(json.dumps(e.to_dict()))
        else:
            raise AssertionError("solved without Triangle")
    ''')
    err = json.loads(out.strip().splitlines()[-1])
    assert err["code"] == "triangle_unavailable" and err["message"] == "Triangle is not installed"
    assert "rust mesher" in err["hint"]


def test_the_cli_says_triangle_is_not_installed(tmp_path):
    out = _run(f'''
        from coppernicus.cli import main
        code = main(["solve", {str(DEMO)!r}, "--json", "--out", {str(tmp_path)!r}])
        print("exit", code)
    ''')
    lines = out.strip().splitlines()
    assert lines[-1] == "exit 2"
    doc = json.loads(_last_json(lines[:-1]))
    # The default mesher is checked before anything is solved (the selector), with the stand-in's hint.
    assert doc["error"]["code"] == "mesher_unavailable"
    assert "'triangle'" in doc["error"]["message"] and "rust mesher" in doc["error"]["hint"]


def _last_json(lines):
    start = max(i for i, line in enumerate(lines) if line.startswith("{"))
    return "\n".join(lines[start:])


def test_the_triangle_backend_is_not_available_with_the_standin():
    _run('''
        import pytest
        from coppernicus import triangle_standin as ts
        from coppernicus.engine import meshing
        assert "triangle" not in meshing.available()
        with pytest.raises(RuntimeError, match="'triangle' is not installed here: choose the rust mesher"):
            meshing.get("triangle")
        if "rust" in meshing.available():                     # its own stand-in in sys.modules
            meshing.install("rust")
            assert sys.modules["triangle"] is not ts.STANDIN and "triangle" not in meshing.available()
            meshing.uninstall()
            assert sys.modules["triangle"] is ts.STANDIN
    ''')


@pytest.mark.skipif(not HAS_RUST, reason="coppermesh not built")
def test_the_rust_mesher_solves_a_repo_example_without_triangle(tmp_path):
    out = _run(f'''
        import math
        from coppernicus.cli import main
        from coppernicus.engine import meshing
        from coppernicus.engine.board import Board
        from coppernicus.engine.meshing import pipeline
        from coppernicus.engine.solve import solve_rail
        from coppernicus.project import Project
        assert meshing.available() == ["rust"]
        meshing.install("rust")
        p = Project.load({str(DEMO)!r})
        b = Board.open(p.board_path())
        for rail in p.rails:
            r = solve_rail(b, rail, p.settings, project=p)
            assert r.layers and math.isfinite(r.max_drop) and r.max_drop > 0, rail.net
        assert pipeline.STATS and all(s["mesher"] == "rust" for s in pipeline.STATS)
        print("exit", main(["solve", {str(DEMO)!r}, "--mesher", "rust", "--json",
                            "--out", {str(tmp_path)!r}]))
    ''')
    assert out.strip().splitlines()[-1] in ("exit 0", "exit 1")      # solved (1: a limit exceeded)


# --------------------------------------------------------------------------- with Triangle
@pytest.mark.skipif(not HAS_TRIANGLE, reason="Triangle not installed")
def test_a_real_triangle_is_never_replaced():
    _run('''
        import types
        import coppernicus.engine
        from coppernicus import triangle_standin as ts
        from coppernicus.engine import meshing
        import kipi.core.mesh.mesher as M
        assert ts.ensure_triangle_importable() is False and not ts.standin_in_use()
        real = sys.modules["triangle"]
        assert isinstance(real, types.ModuleType) and not ts.is_standin(real) and M.triangle is real
        assert "triangle" in meshing.available()
    ''', block=False)


@pytest.mark.skipif(not HAS_TRIANGLE, reason="Triangle not installed")
def test_a_triangle_that_appears_later_replaces_the_standin():
    """What the packaged app does when Triangle is fetched on demand, after KiPI loaded."""
    _run('''
        from coppernicus import triangle_standin as ts
        from coppernicus.engine import meshing
        import kipi.core.mesh.mesher as M
        import kipi.core.viz.meshes as V
        assert M.triangle is ts.STANDIN and V._triangle is ts.STANDIN
        assert "triangle" not in meshing.available()
        sys.meta_path.remove(_BLOCKER)                        # now it is there
        assert ts.use_real_triangle() and not ts.standin_in_use()
        assert M.triangle is sys.modules["triangle"] is V._triangle and not ts.is_standin(M.triangle)
        assert "triangle" in meshing.available()
    ''')
