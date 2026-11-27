"""The mesher selector: the order a mesher is chosen in (call > project > session > Triangle), the
default leaving KiPI's own meshing untouched, one clear error for a mesher that cannot run, the
mesher recorded in every summary and compared, tools on a stored result never meshing again, and
parallel calls with different meshers taking turns (the installed mesher is global)."""

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import anyio
import numpy as np
import pytest
from mcp import Client

from coppernicus.engine.meshing import registry
from coppernicus.engine.meshing import session as meshing
from coppernicus.mcp_server import build_server
from coppernicus.project import Project, ProjectError
from coppernicus.service import ServiceError, Session
from coppernicus.service.session import compare_summaries, mesher_warning

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo"
DEMO_BOARD = DEMO / "demo.kipi.json.gz"
SOURCES = [{"ref": "J1", "voltage_v": 3.3}]
LOADS = [{"ref": "U1", "current_a": 2.0}, {"ref": "U2", "current_a": 1.5}, {"ref": "U3", "current_a": 0.3}]

try:
    import coppermesh  # noqa: F401
    HAVE_RUST = True
except ImportError:
    HAVE_RUST = False
needs_rust = pytest.mark.skipif(not HAVE_RUST, reason="coppermesh is not built (tools/mesher_rs)")


@pytest.fixture(autouse=True)
def nothing_left_installed():
    assert registry.installed() is None
    yield
    assert registry.installed() is None


@pytest.fixture()
def spy(monkeypatch):
    """Every registry.install call, by name."""
    calls = []
    real = registry.install

    def install(name, **options):
        calls.append(name)
        return real(name, **options)
    monkeypatch.setattr(registry, "install", install)
    return calls


def digest(res) -> str:
    h = hashlib.sha256()
    for lf in res.layers:
        for a in (lf.points, lf.tris, lf.voltage, lf.current_density):
            h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


def demo_project(folder, mesher=None, name="demo") -> Path:
    """The demo project in ``folder``, with ``settings.mesher`` (None: not set)."""
    p = Project.load(DEMO / "demo.coppernicus.yml")
    p.board = str(DEMO_BOARD)
    p.settings.mesher = mesher
    return p.save(folder / f"{name}.coppernicus.yml")


# ------------------------------------------------------------------ the order and the context

def test_the_order_call_project_app_default():
    assert meshing.resolve() == "triangle"
    assert meshing.resolve(app="rust") == "rust"
    assert meshing.resolve(project="rust", app="triangle") == "rust"
    assert meshing.resolve("Triangle ", project="rust", app="rust") == "triangle"
    assert meshing.resolve("", project=None, app="") == "triangle"


def test_the_default_leaves_the_registry_uninstalled(spy):
    with meshing.using() as info:
        assert registry.installed() is None and meshing.current() == "triangle"
        assert info["name"] == "triangle" and info["experimental"] is False
    with meshing.using("triangle"):
        assert registry.installed() is None
    assert spy == [] and meshing.current() is None


@needs_rust
def test_using_installs_and_puts_back_what_it_found(spy):
    with meshing.using("rust") as info:
        assert registry.installed() == "rust" and info["experimental"] is True
        with meshing.using("triangle"):              # nested, one thread: the lock is re-entrant
            assert registry.installed() is None
        assert registry.installed() == "rust"
        with meshing.using("rust"):
            assert registry.installed() == "rust"
    assert registry.installed() is None


def test_an_unavailable_mesher_is_one_error_with_a_hint(monkeypatch, spy):
    from coppernicus.engine.meshing.rust_backend import RustBackend
    monkeypatch.setattr(RustBackend, "available", lambda self: False)
    with pytest.raises(meshing.MesherUnavailable) as e:
        with meshing.using("rust"):
            pytest.fail("the block must not run")
    assert "not available" in e.value.message and "tools/mesher_rs" in e.value.hint
    with pytest.raises(meshing.MesherUnavailable) as e:
        meshing.check("nope")
    assert "triangle" in e.value.hint
    assert spy == []


# ------------------------------------------------------------------ the project key

def test_the_project_key_round_trips_and_is_written_only_when_set(tmp_path):
    path = demo_project(tmp_path, "rust")
    assert Project.load(path).settings.mesher == "rust"
    assert "mesher: rust" in path.read_text(encoding="utf-8")
    assert Project.from_dict({"board": "x.kicad_pcb", "settings": {"mesher": " Rust"}},
                             tmp_path / "p.yml").settings.mesher == "rust"
    plain = demo_project(tmp_path, name="plain")
    assert "mesher:" not in plain.read_text(encoding="utf-8")
    with pytest.raises(ProjectError, match="mesher"):
        Project.from_dict({"board": "x.kicad_pcb", "settings": {"mesher": 3}}, tmp_path / "p.yml")


def test_write_project_takes_the_mesher(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    path = demo_project(tmp_path)
    session.write_project(str(path), settings={"mesher": "rust"}, overwrite=True)
    assert Project.load(path).settings.mesher == "rust"
    session.write_project(str(path), settings={"mesher": None}, overwrite=True)
    assert Project.load(path).settings.mesher is None


# ------------------------------------------------------------------ the session

def test_a_default_solve_records_triangle_and_installs_nothing(tmp_path, spy):
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_project(demo_project(tmp_path))
    [rail] = doc["rails"]
    assert spy == []
    assert doc["mesher"]["name"] == rail["mesher"]["name"] == rail["settings"]["mesher"] == "triangle"
    assert not any("experimental" in a for a in rail["assumptions"])


def test_an_unavailable_mesher_fails_the_project_once(tmp_path, monkeypatch):
    from coppernicus.engine.meshing.rust_backend import RustBackend
    monkeypatch.setattr(RustBackend, "available", lambda self: False)
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_project(demo_project(tmp_path, "rust"))
    assert e.value.code == "mesher_unavailable" and "tools/mesher_rs" in e.value.hint
    bid = session.open_board(str(DEMO_BOARD))["board_id"]
    with pytest.raises(ServiceError) as e:
        session.solve_rail(bid, "+3V3", SOURCES, LOADS, mesher="cgal")
    assert e.value.code == "mesher_unavailable"


@needs_rust
def test_call_wins_over_project_wins_over_session(tmp_path, spy):
    path = demo_project(tmp_path, "rust")
    assert Session(out_dir=tmp_path / "a").solve_project(path)["mesher"]["name"] == "rust"
    explicit = Session(out_dir=tmp_path / "b").solve_project(path, mesher="triangle")
    assert explicit["mesher"]["name"] == "triangle"
    plain = demo_project(tmp_path, name="plain")
    assert Session(out_dir=tmp_path / "c", mesher="rust").solve_project(plain)["mesher"]["name"] == "rust"
    assert Session(out_dir=tmp_path / "d", mesher="rust").solve_project(path, mesher="triangle")[
        "mesher"]["name"] == "triangle"
    assert spy == ["rust", "rust"]


@needs_rust
def test_compare_warns_on_different_meshers_and_copper_step_keeps_the_baselines(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(str(DEMO_BOARD))["board_id"]
    a = session.solve_rail(bid, "+3V3", SOURCES, LOADS)
    b = session.solve_rail(bid, "+3V3", SOURCES, LOADS, mesher="rust")
    assert a["mesher"]["name"] == "triangle" and b["mesher"]["name"] == "rust"
    assert any("experimental rust mesher" in x for x in b["assumptions"])
    cmp_ = session.compare_results(a["result_id"], b["result_id"])
    assert cmp_["settings_changed"]["mesher"] == {"a": "triangle", "b": "rust"}
    assert any(w.startswith("Different meshers") for w in cmp_["warnings"])
    rev = session.compare_revisions(a["result_id"], b["result_id"], out_dir=tmp_path / "rev")
    assert any(w.startswith("Different meshers") for w in rev["warnings"])
    again = session.solve_rail(bid, "+3V3", SOURCES, LOADS)["result_id"]
    same = session.compare_results(a["result_id"], again)
    assert not any(w.startswith("Different meshers") for w in same["warnings"])
    step = session.copper_step(b["result_id"], "B.Cu", "up")
    assert session._record(step["result_id"]).summary["mesher"]["name"] == "rust"
    assert not any(w.startswith("Different meshers") for w in step["compare"]["warnings"])


def test_a_saved_summary_keeps_its_mesher_and_an_older_one_reads_as_triangle(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(str(DEMO_BOARD))["board_id"]
    a = session.solve_rail(bid, "+3V3", SOURCES, LOADS)
    saved = json.loads(json.dumps(a))                  # as written to *_summary.json and read back
    assert saved["mesher"]["name"] == "triangle"
    older = json.loads(json.dumps(a))
    del older["mesher"], older["settings"]["mesher"]
    assert mesher_warning(saved, older) is None
    assert "mesher" not in compare_summaries(older, saved)["settings_changed"]
    other = {**saved, "mesher": {"name": "rust", "version": "coppermesh 0.1.0"}}
    assert "coppermesh 0.1.0" in mesher_warning(saved, other)
    newer = {**saved, "mesher": {**saved["mesher"], "version": "triangle 0"}}
    assert "triangle 0" in mesher_warning(saved, newer)           # another version of the same one


@needs_rust
def test_tools_on_a_stored_result_never_mesh_again(tmp_path, monkeypatch):
    """render_map, probe, cut_current, get_problems, report, compare_results and
    compare_revisions read the stored mesh: with KiPI's mesher made to fail, they all work."""
    import kipi.core.mesh.mesher as M
    session = Session(out_dir=tmp_path / "out")
    info = session.open_board(str(DEMO_BOARD))
    bid = info["board_id"]
    a = session.solve_rail(bid, "+3V3", SOURCES, LOADS)["result_id"]
    b = session.solve_rail(bid, "+3V3", SOURCES, LOADS, mesher="rust")["result_id"]

    def no_meshing(*args, **kwargs):
        raise AssertionError("meshed again")
    monkeypatch.setattr(M, "mesh_polygon", no_meshing)
    monkeypatch.setattr(registry, "install", no_meshing)
    res = session.rail_result(b)
    x, y = res.layer("B.Cu").points[0]
    session.render_map(b, "B.Cu", "current")
    session.probe(b, "B.Cu", float(x), float(y), relative_to={"pad": "U1.1"})
    _, y0, _, y1 = info["outline_bbox_mm"]
    session.cut_current(b, "B.Cu", [29.5, y0], [29.5, y1])
    session.get_problems(b)
    session.report([a, b], path=tmp_path / "report.html")
    session.compare_results(a, b)
    session.compare_revisions(a, b, out_dir=tmp_path / "rev")


# ------------------------------------------------------------------ parallel calls

def _solve(session, mesher, barrier=None):
    bid = session.open_board(str(DEMO_BOARD))["board_id"]
    if barrier is not None:
        barrier.wait()
    rid = session.solve_rail(bid, "+3V3", SOURCES, LOADS, mesher=mesher)["result_id"]
    return digest(session.rail_result(rid))


@needs_rust
def test_parallel_calls_with_different_meshers_take_turns(tmp_path):
    """Two sessions (so their own solve locks do not serialise them), one thread each, started
    together: each result is the one it gives alone, and nothing stays installed."""
    alone = {m: _solve(Session(out_dir=tmp_path / f"alone-{m}"), m) for m in ("triangle", "rust")}
    assert alone["triangle"] != alone["rust"]
    def run(m, folder, barrier, out, errors):
        try:
            out[(m, folder.name)] = _solve(Session(out_dir=folder), m, barrier)
        except BaseException as e:                # reported below, not lost in the thread
            errors.append(e)
    for round_ in range(3):
        barrier, out, errors = threading.Barrier(4), {}, []
        threads = [threading.Thread(target=run, args=(m, tmp_path / f"{round_}-{m}-{k}", barrier, out,
                                                      errors))
                   for m in ("triangle", "rust") for k in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=300)
        assert not errors, errors
        assert {k: v == alone[k[0]] for k, v in out.items()} == {k: True for k in out} and len(out) == 4
    assert registry.installed() is None


@needs_rust
def test_triangle_after_rust_is_triangle_again(tmp_path):
    """No mesh of one mesher is reused by the next solve on the same board."""
    session = Session(out_dir=tmp_path / "out")
    first, rust, again = (_solve(session, m) for m in ("triangle", "rust", "triangle"))
    assert first == again != rust


@needs_rust
def test_parallel_mcp_calls_with_different_meshers(tmp_path):
    """The MCP tools run in worker threads: two solve_rail calls at once, different meshers."""
    async def main():
        server = build_server(Session(out_dir=tmp_path / "maps"))
        async with Client(server, raise_exceptions=True) as client:
            board = (await client.call_tool("open_board", {"path": str(DEMO_BOARD)})).structured_content
            args = {"board_id": board["board_id"], "net": "+3V3", "sources": SOURCES, "loads": LOADS}
            out = {}

            async def solve(m):
                r = await client.call_tool("solve_rail", {**args, "mesher": m})
                assert not r.is_error, r.content[0].text
                out[m] = r.structured_content
            async with anyio.create_task_group() as tg:
                for m in ("triangle", "rust", "triangle"):
                    tg.start_soon(solve, m)
            bad = await client.call_tool("solve_rail", {**args, "mesher": "nope"})
            tools = {t.name: t for t in (await client.list_tools()).tools}
            return out, bad, tools
    out, bad, tools = anyio.run(main)
    assert out["triangle"]["mesher"]["name"] == "triangle" and out["rust"]["mesher"]["name"] == "rust"
    assert out["triangle"]["max_drop_mv"] != out["rust"]["max_drop_mv"]
    assert bad.is_error and json.loads(bad.content[0].text)["error"]["code"] == "mesher_unavailable"
    for name in ("solve_rail", "solve_project", "solve_tree", "solve_ac", "solve_noise", "solve_tolerance"):
        assert "mesher" in tools[name].input_schema["properties"], name


# ------------------------------------------------------------------ the CLI

def _cli(*args):
    run = subprocess.run([sys.executable, "-m", "coppernicus", *args], capture_output=True, text=True,
                         encoding="utf-8", timeout=600,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return run.returncode, (json.loads(run.stdout) if run.stdout.strip() else None), run.stderr


@needs_rust
def test_cli_flag_wins_over_the_project_key(tmp_path):
    path = demo_project(tmp_path, "rust")
    code, doc, _ = _cli("solve", str(path), "--json", "--no-maps", "--out", str(tmp_path / "a"))
    assert code in (0, 1) and doc["mesher"]["name"] == doc["rails"][0]["settings"]["mesher"] == "rust"
    saved = json.loads(Path(doc["summary_file"]).read_text(encoding="utf-8"))
    assert saved["mesher"]["name"] == "rust"
    code, doc, _ = _cli("solve", str(path), "--json", "--no-maps", "--out", str(tmp_path / "b"),
                        "--mesher", "triangle")
    assert code in (0, 1) and doc["mesher"]["name"] == "triangle"


def test_cli_unknown_mesher_is_one_error(tmp_path):
    code, doc, err = _cli("solve", str(demo_project(tmp_path)), "--json", "--mesher", "cgal")
    assert code == 2 and doc["error"]["code"] == "mesher_unavailable"
    assert "Traceback" not in err and doc["error"]["hint"]


@needs_rust
def test_cli_tree_records_its_mesher(tmp_path):
    project = ROOT / "examples" / "power_chain" / "power_chain.coppernicus.yml"
    code, doc, _ = _cli("solve", str(project), "--tree", "--json", "--out", str(tmp_path), "--mesher", "rust")
    assert code in (0, 1, 3) and doc["mesher"]["name"] == "rust"
    assert any("experimental rust mesher" in a for a in doc["assumptions"])
