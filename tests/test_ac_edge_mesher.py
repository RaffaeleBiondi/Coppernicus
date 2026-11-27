"""The AC mesh edge by mesher in AC, noise and tolerance results: unset, the default of the mesher
that meshed (rust finer than Triangle's 1.0 mm), set in the project, the user's value for any mesher;
the value and its source in the summaries (``ac``) and the assumptions; a warning (never a verdict)
when rust meshes with an edge the project sets coarser than rust's default. Also the assumption line
of a user max_current_density (the crowding mean |J| depends on the mesh). KiPI's
synthetic boards."""

import dataclasses
from pathlib import Path

import pytest

from coppernicus.engine.ac import RUST_AC_MAX_EDGE_MM
from coppernicus.engine.synthetic import (
    ac_decap_board,
    ac_decap_project,
    coupled_noise_board,
    coupled_noise_project,
    save_snapshot,
)
from coppernicus.problems import DENSITY_MESH_NOTE
from coppernicus.project import AcOptions, NoiseOptions, Project
from coppernicus.service import Session

try:
    import coppermesh  # noqa: F401
    HAVE_RUST = True
except ImportError:
    HAVE_RUST = False
needs_rust = pytest.mark.skipif(not HAVE_RUST, reason="coppermesh is not built (tools/mesher_rs)")

COARSE = "coarser than the rust mesher's default"
QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4)


def _decap(tmp_path, **ac) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    save_snapshot(ac_decap_board(), tmp_path / "ac_decap.kipi.json.gz")
    p = ac_decap_project()
    p.ac = AcOptions(**{**QUICK, "bandwidth_hz": {"U1": 1e5}, **ac})
    return str(p.save(tmp_path / "d.coppernicus.yml"))


def _noise(tmp_path, **ac) -> str:
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
    p.noise = NoiseOptions(board_model=True)
    p.ac = AcOptions(**ac)
    return str(p.save(tmp_path / "n.coppernicus.yml"))


def _tolerance(tmp_path, **ac) -> str:
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    p = coupled_noise_project(qrr_nc=5.0, ripple_probe=True)
    p.victims = []
    rail = p.rails[1]
    rail.loads[0] = dataclasses.replace(rail.loads[0], tol_pct=6.0, transient_mv=30.0)
    p.probes[0].step_a, p.probes[0].slew_a_per_us = 1.0, 1.0
    p.ac = AcOptions(bandwidth_hz={"U1": 100e3}, **ac)
    return str(p.save(tmp_path / "t.coppernicus.yml"))


def _coarse(doc, *parts) -> bool:
    texts = list(doc["warnings"]) + [w for part in parts for w in part["warnings"]]
    return any(COARSE in w for w in texts)


@needs_rust
def test_ac_unset_is_rusts_default_and_a_coarser_set_edge_warns_only_with_rust(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_ac(_decap(tmp_path / "a"))
    [rail] = doc["rails"]
    assert doc["mesher"]["name"] == "rust"
    assert doc["ac"] == rail["ac"] == {"max_edge_mm_used": RUST_AC_MAX_EDGE_MM,
                                       "max_edge_mm_from": "rust default"}
    line = "AC mesh: largest edge 0.5 mm, the rust mesher's default"
    assert any(a.startswith(line) for a in rail["assumptions"])
    assert not _coarse(doc, rail)
    coarse = _decap(tmp_path / "b", max_edge_mm=2.0)
    doc = session.solve_ac(coarse)
    [rail] = doc["rails"]
    assert doc["ac"] == {"max_edge_mm_used": 2.0, "max_edge_mm_from": "project"} and _coarse(doc, rail)
    assert any(a.startswith("AC mesh: largest edge 2 mm, set in the project") for a in rail["assumptions"])
    assert doc["verdict"] in ("pass", "fail", "not_judged")         # a warning, never a verdict
    doc = session.solve_ac(coarse, mesher="triangle")
    [rail] = doc["rails"]
    assert doc["ac"]["max_edge_mm_used"] == 2.0 and not _coarse(doc, rail)
    doc = session.solve_ac(_decap(tmp_path / "c"), mesher="triangle")
    assert doc["ac"] == {"max_edge_mm_used": 1.0, "max_edge_mm_from": "triangle default"}


@needs_rust
def test_noise_records_the_edge_and_warns_on_a_coarser_set_edge_with_rust(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_noise(_noise(tmp_path, max_edge_mm=1.0))
    [agg] = doc["aggressors"]
    assert doc["ac"] == agg["ac"] == {"max_edge_mm_used": 1.0, "max_edge_mm_from": "project"}
    assert _coarse(doc, agg)
    assert any(a.startswith("AC mesh: largest edge 1 mm, set in the project") for a in agg["assumptions"])
    doc = session.solve_noise(_noise(tmp_path, max_edge_mm=1.0), mesher="triangle")
    assert not _coarse(doc, *doc["aggressors"])


@needs_rust
def test_tolerance_records_the_edge_and_warns_on_a_coarser_set_edge_with_rust(tmp_path):
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_tolerance(_tolerance(tmp_path, max_edge_mm=1.0))
    assert doc["mesher"]["name"] == "rust"
    assert doc["ac"] == {"max_edge_mm_used": 1.0, "max_edge_mm_from": "project"}
    assert any(COARSE in w for w in doc["warnings"])
    assert any(a.startswith("AC mesh: largest edge 1 mm, set in the project") for a in doc["assumptions"])
    doc = session.solve_tolerance(_tolerance(tmp_path, max_edge_mm=1.0), mesher="triangle")
    assert doc["ac"]["max_edge_mm_from"] == "project" and not any(COARSE in w for w in doc["warnings"])
    doc = session.solve_tolerance(_tolerance(tmp_path), skip_ac=True, skip_noise=True)
    assert "ac" not in doc and not any(COARSE in w for w in doc["warnings"])


def test_estimates_use_the_resolved_edge(tmp_path):
    """The pre-solve size meshes as the solve will: the finer rust default gives more triangles."""
    from coppernicus.engine import Board
    from coppernicus.engine.ac import ac_size
    from coppernicus.engine.meshing import session as meshing
    path = _decap(tmp_path)
    p = Project.load(path)
    board = Board.open(p.board_path())
    with meshing.using("triangle"):
        coarse = ac_size(board, p, "+3V3").triangles
        p.ac.max_edge_mm = 0.5
        fine = ac_size(board, p, "+3V3").triangles
    assert fine > coarse
    if HAVE_RUST:
        p.ac.max_edge_mm = None
        with meshing.using("rust"):
            rust = ac_size(board, p, "+3V3").triangles
        assert rust > coarse


# ---------------------------------------------------------------------- max_current_density

DEMO_SOURCES = [{"ref": "J1", "voltage_v": 3.3}]
DEMO_LOADS = [{"ref": "U1", "current_a": 2.0}, {"ref": "U2", "current_a": 1.5},
              {"ref": "U3", "current_a": 0.3}]


def test_a_user_max_current_density_adds_the_mesh_assumption_and_the_default_does_not(tmp_path):
    from pathlib import Path
    demo = Path(__file__).resolve().parents[1] / "examples" / "demo"
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(str(demo / "demo.kipi.json.gz"))["board_id"]
    judged = session.solve_rail(bid, "+3V3", DEMO_SOURCES, DEMO_LOADS,
                                limits={"max_current_density_a_mm2": 30.0})
    assert DENSITY_MESH_NOTE in judged["assumptions"]
    plain = session.solve_rail(bid, "+3V3", DEMO_SOURCES, DEMO_LOADS, limits={"max_drop_pct": 5.0})
    assert DENSITY_MESH_NOTE not in plain["assumptions"]
    assert "with either mesher" in DENSITY_MESH_NOTE and "typically 1-2 %" in DENSITY_MESH_NOTE


def test_a_tree_with_a_user_max_current_density_has_the_mesh_assumption(tmp_path):
    from pathlib import Path
    chain = Path(__file__).resolve().parents[1] / "examples" / "power_chain" / "power_chain.coppernicus.yml"
    p = Project.load(chain)
    p.board = str(chain.parent / p.board)
    plain = Session(out_dir=tmp_path / "a").solve_tree(str(p.save(tmp_path / "plain.coppernicus.yml")),
                                                       mesher="triangle")
    assert DENSITY_MESH_NOTE not in plain["assumptions"]
    p.rails[0].limits.max_current_density = 40.0
    judged = Session(out_dir=tmp_path / "b").solve_tree(str(p.save(tmp_path / "j.coppernicus.yml")),
                                                        mesher="triangle")
    assert DENSITY_MESH_NOTE in judged["assumptions"]


# ---------------------------------------------------------------------- the round trip

def test_write_project_keeps_a_set_ac_edge_and_never_writes_an_unset_one(tmp_path):
    """write_project (MCP) and the app save through Project.save: an ``ac: max_edge_mm`` the file
    has is a user value, kept even at the old default 1.0; a file without it never gains it."""
    import yaml
    path = _decap(tmp_path / "set", max_edge_mm=1.0)
    plain = _decap(tmp_path / "plain")
    session = Session(out_dir=tmp_path / "out")
    for p in (path, plain):
        session.write_project(p, settings={"copper_temp_c": 30.0}, overwrite=True)
    assert yaml.safe_load(Path(path).read_text(encoding="utf-8"))["ac"]["max_edge_mm"] == 1.0
    assert Project.load(path).ac.max_edge_mm == 1.0
    assert "max_edge_mm" not in (yaml.safe_load(Path(plain).read_text(encoding="utf-8")).get("ac") or {})
    assert Project.load(plain).ac.max_edge_mm is None
