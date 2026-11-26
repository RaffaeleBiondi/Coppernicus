"""SuperLU's symmetric mode for KiPI's factorizations (coppernicus.engine.superlu_order): off by
default (KiPI's own path, bit-identical), opt-in per part, installed only on the KiPI it was
written for; on, results move by round-off only (the proof criteria in the module's docstring)."""

import threading
import warnings

import numpy as np
import pytest
import scipy.sparse as sp
from scipy.sparse.linalg import splu

import coppernicus.engine.superlu_order as so
from coppernicus.engine import Board
from coppernicus.engine.synthetic import demo_board
from coppernicus.engine.tree import solve_project_tree
from coppernicus.project import Load, Project, Rail, Source, ThermalOptions

direct = so.direct                     # KiPI's kipi.core.solver.direct, as the engine imports it
KIPI_SUPERLU = direct._superlu


@pytest.fixture(autouse=True)
def kipi_path():
    """Every test starts and ends with nothing installed."""
    so.uninstall()
    yield
    so.uninstall()


def grid_spd(nx=24, ny=20, nl=3, seed=0) -> sp.csc_matrix:
    """A small heat-model-like SPD matrix: an nx x ny grid of nl layers (unknown c * nl + l),
    random in-plane conductances, vertical ties, convection on the outer layers."""
    rng = np.random.default_rng(seed)
    idx = np.arange(nx * ny).reshape(ny, nx)
    n = nx * ny * nl
    rows, cols, vals, diag = [], [], [], np.zeros(n)

    def couple(a, b, g):
        rows.extend([a, b])
        cols.extend([b, a])
        vals.extend([-g, -g])
        np.add.at(diag, a, g)
        np.add.at(diag, b, g)

    for li in range(nl):
        g = np.exp(rng.normal(-3, 1.5, (ny, nx)))
        for di, dj in ((0, 1), (1, 0)):
            a, b = idx[: ny - di, : nx - dj], idx[di:, dj:]
            ga, gb = g[: ny - di, : nx - dj], g[di:, dj:]
            couple((a * nl + li).ravel(), (b * nl + li).ravel(), (2 * ga * gb / (ga + gb)).ravel())
    cells = idx.ravel()
    for li in range(nl - 1):
        couple(cells * nl + li, cells * nl + li + 1, np.full(cells.size, 1e-2))
    diag[cells * nl] += 1e-5
    diag[cells * nl + nl - 1] += 1e-5
    return sp.coo_matrix((np.concatenate(vals + [diag]), (np.concatenate(rows + [np.arange(n)]),
                          np.concatenate(cols + [np.arange(n)]))), shape=(n, n)).tocsc()


def backward_error(A, x, b) -> float:
    """‖b − Ax‖∞ / (‖A‖∞ ‖x‖∞ + ‖b‖∞)."""
    r = np.abs(b - A @ x).max()
    return float(r / (abs(A).sum(axis=1).max() * np.abs(x).max() + np.abs(b).max()))


def close(a, b, rel) -> bool:
    """The proof criterion: |a − b| within ``rel`` of the larger magnitude, or within the floor
    ``rel`` × the largest magnitude of the values compared (one unit), for those near zero."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    floor = rel * float(np.nanmax(np.abs(np.concatenate([a.ravel(), b.ravel()]))))
    ok = np.abs(a - b) <= np.maximum(rel * np.maximum(np.abs(a), np.abs(b)), floor)
    return bool(np.all(ok | (np.isnan(a) & np.isnan(b))))


def demo_thermal_project():
    """examples/demo/demo-thermal.coppernicus.yml's rail and thermal block, as tests/test_thermal.py."""
    return Project(rails=[Rail("+3V3", sources=[Source("J1", 3.3, name="Supply")],
                               loads=[Load("U1", 8.0), Load("U2", 6.0), Load("U3", 1.2)])],
                   thermal=ThermalOptions(enabled=True, max_rise_c=15.0))


# ------------------------------------------------------------------- the switch and the seam

def test_off_nothing_is_replaced():
    assert so.enabled() == frozenset()
    assert direct._superlu is KIPI_SUPERLU


@pytest.mark.parametrize("text, parts", [("", set()), ("thermal", {"thermal"}),
                                         (" Thermal , ", {"thermal"})])
def test_parts_from_the_environment(text, parts):
    assert so.parse(text) == parts


def test_an_unknown_part_is_named_and_left_out():
    with pytest.warns(UserWarning, match="fast"):
        assert so.parse("thermal,fast") == {"thermal"}


def test_configure_from_the_environment_enables_and_installs():
    assert so.configure_from_env({so.ENV: "thermal"}) == frozenset({"thermal"})
    assert direct._superlu is not KIPI_SUPERLU
    so.uninstall()
    assert so.configure_from_env({}) == frozenset()
    assert direct._superlu is KIPI_SUPERLU


def test_install_is_idempotent_and_uninstall_restores_kipi():
    assert so.enable("thermal")
    ours = direct._superlu
    assert so.enable("thermal")
    assert direct._superlu is ours
    so.uninstall()
    assert direct._superlu is KIPI_SUPERLU and so.enabled() == frozenset()


def test_only_the_kipi_it_was_written_for(monkeypatch):
    monkeypatch.setattr(so.kipi, "__version__", "1.0.2")
    with pytest.warns(UserWarning, match="1.0.2"):
        assert not so.enable("thermal")
    assert direct._superlu is KIPI_SUPERLU and so.enabled() == frozenset()


def test_not_over_a_function_of_another_shape(monkeypatch):
    monkeypatch.setattr(direct, "_superlu", lambda A, order: None)
    with pytest.warns(UserWarning, match="_superlu"):
        assert not so.enable("thermal")
    assert so.enabled() == frozenset()


def test_the_seam_problem_is_warned_once(monkeypatch):
    monkeypatch.setattr(so.kipi, "__version__", "9")
    with pytest.warns(UserWarning):
        so.enable("thermal")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert not so.enable("thermal")


# ------------------------------------------------------------------- the thermal part

def test_thermal_part_only_for_the_heat_model():
    A = grid_spd()
    b = np.random.default_rng(1).standard_normal(A.shape[0])
    kipi_x = KIPI_SUPERLU(A)(b)
    so.enable("thermal")
    assert np.array_equal(direct.factorize(A)(b), kipi_x)            # not marked: KiPI's
    with so.thermal_model():
        x = direct.factorize(A)(b)
    sym = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
    assert np.array_equal(x, sym.solve(b))
    assert not np.array_equal(x, kipi_x)


def test_thermal_part_off_the_marked_model_is_kipis():
    A = grid_spd()
    b = np.ones(A.shape[0])
    with so.thermal_model():                                          # installed: nothing
        assert np.array_equal(direct.factorize(A)(b), KIPI_SUPERLU(A)(b))


def test_the_marker_is_the_calling_threads():
    so.enable("thermal")
    seen = {}

    def other():
        seen["part"] = so.current_part()
    with so.thermal_model():
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert so.current_part() == "thermal"
    assert seen["part"] is None and so.current_part() is None


def test_the_factor_keeps_kipis_name():
    so.enable("thermal")
    with so.thermal_model():
        f = direct.factorize(grid_spd())
    assert f.name == "superlu"


def test_backward_error_on_and_off_alike():
    """On a heat-model-like grid, the symmetric order's backward error is of the order of
    KiPI's (both a few 1e-16): an exact direct solve either way."""
    A = grid_spd(40, 36, 4, seed=3)
    b = np.random.default_rng(2).standard_normal(A.shape[0])
    off = backward_error(A, KIPI_SUPERLU(A)(b), b)
    so.enable("thermal")
    with so.thermal_model():
        on = backward_error(A, direct.factorize(A)(b), b)
    assert off < 1e-14 and on < 1e-14
    assert on < 10 * off + 1e-16 and off < 10 * on + 1e-16


def test_symmetric_order_fills_less():
    A = grid_spd(40, 36, 4)
    kipi_lu = splu(A, permc_spec="COLAMD")
    sym = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
    assert sym.nnz < kipi_lu.nnz


@pytest.fixture(scope="module")
def demo():
    return Board(demo_board(led_net=True))


def test_thermal_solve_moves_by_round_off_only(demo, monkeypatch):
    """The demo's electro-thermal tree with the heat model in symmetric order: the same mesh,
    temperatures and load drops within the proof criterion (1e-9 relative, the floor per unit),
    and only the heat model factorized ours (once)."""
    off = solve_project_tree(demo, demo_thermal_project())
    calls = []
    real = so.splu

    def spy(A, **kw):
        calls.append(kw["permc_spec"])
        return real(A, **kw)
    monkeypatch.setattr(so, "splu", spy)
    so.enable("thermal")
    on = solve_project_tree(demo, demo_thermal_project())
    assert calls == ["MMD_AT_PLUS_A"]
    assert ({k: g.stats.get("triangles") for k, g in on.groups.items()}
            == {k: g.stats.get("triangles") for k, g in off.groups.items()})
    for a, b in zip(off.thermal.maps, on.thermal.maps, strict=True):
        assert np.array_equal(np.isnan(a.t_c), np.isnan(b.t_c))
        assert close(a.t_c, b.t_c, so.TOLERANCE_REL["thermal"])
    assert close([ld.drop_v for ld in off.loads], [ld.drop_v for ld in on.loads], so.TOLERANCE_REL["thermal"])
    assert close([r.t_max_c for r in off.thermal.copper], [r.t_max_c for r in on.thermal.copper],
                 so.TOLERANCE_REL["thermal"])


def test_the_ram_estimate_says_it_overstates_with_thermal_on():
    """The heat model's memory fit was measured in KiPI's order: with the thermal part on, its
    text says that it overstates; off, the text is KiPI-order's alone."""
    from coppernicus.engine.thermal import thermal_ram_estimate
    off = thermal_ram_estimate(250_000, 4, 24e9)
    assert "symmetric" not in off.text
    so.enable("thermal")
    on = thermal_ram_estimate(250_000, 4, 24e9)
    assert on.bytes == off.bytes and on.risk == off.risk
    assert on.text.startswith(off.text) and "overstates" in on.text and so.ENV in on.text


# ------------------------------------------------------------------- the AC part

KIPI_SUPERLU_COMPLEX = direct._superlu_complex


def ac_like(nx=24, ny=20, nl=2, omega=2e6, weak=1.0) -> sp.csc_matrix:
    """A small AC-like complex symmetric matrix: conductance G, capacitance on the diagonal and
    an inductive grid, G + j(ωC − B/ω): indefinite in its imaginary part. ``weak`` scales the
    diagonal: under 1, pivots fall under the column's largest entry and pivoting acts."""
    G = grid_spd(nx, ny, nl, seed=4)
    B = grid_spd(nx, ny, nl, seed=5) * 1e3
    C = sp.diags_array(np.full(G.shape[0], 1e-9))
    A = sp.csc_matrix(G + 1j * (omega * C - B / omega))
    return sp.csc_matrix(A + (weak - 1.0) * sp.diags_array(A.diagonal()))


def test_ac_part_factorizes_complex_systems_in_symmetric_mode_with_pivoting():
    A = ac_like(weak=0.4)
    b = np.random.default_rng(6).standard_normal(A.shape[0]) + 0j
    so.enable("ac")
    f = direct._superlu_complex(A)
    sym = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
    assert np.array_equal(f(b), sym.solve(b))
    full = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=1.0, options={"SymmetricMode": True})
    assert not np.array_equal(f(b), full.solve(b))           # this matrix tells 0.1 from 1.0
    # KiPI's thread count (kipi/ac/solver/field.py threads()) reads the name and the memory.
    assert f.name == "superlu" and f.nbytes == 24 * int(sym.nnz)


def test_ac_part_reaches_kipis_threads():
    """The AC field solves run in KiPI's own threads: the part is process-wide."""
    A = ac_like()
    b = np.ones(A.shape[0], dtype=complex)
    so.enable("ac")
    got = {}
    t = threading.Thread(target=lambda: got.update(x=direct._superlu_complex(A)(b)))
    t.start()
    t.join()
    sym = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.1, options={"SymmetricMode": True})
    assert np.array_equal(got["x"], sym.solve(b))


def test_parts_are_apart():
    """Each part only its own: thermal on leaves the complex systems KiPI's, AC on leaves the
    heat model KiPI's."""
    A, S = ac_like(), grid_spd()
    b, s = np.ones(A.shape[0], dtype=complex), np.ones(S.shape[0])
    so.enable("thermal")
    assert np.array_equal(direct._superlu_complex(A)(b), KIPI_SUPERLU_COMPLEX(A)(b))
    so.disable()
    so.enable("ac")
    with so.thermal_model():
        assert np.array_equal(direct.factorize(S)(s), KIPI_SUPERLU(S)(s))
    assert so.parse("ac, thermal") == {"ac", "thermal"}


@pytest.mark.parametrize("weak", [1.0, 0.4])
def test_ac_backward_error_on_and_off_alike(weak):
    """Also where pivots fall under the column's largest entry (``weak``): threshold 0.1 stays
    as accurate as KiPI's full partial pivoting."""
    A = ac_like(36, 30, 2, weak=weak)
    b = np.random.default_rng(7).standard_normal(A.shape[0]) + 0j
    off = backward_error(A, KIPI_SUPERLU_COMPLEX(A)(b), b)
    so.enable("ac")
    on = backward_error(A, direct._superlu_complex(A)(b), b)
    assert off < 1e-14 and on < 1e-14
    assert on < 10 * off + 1e-16 and off < 10 * on + 1e-16


def test_the_ac_seam_is_checked_too(monkeypatch):
    monkeypatch.setattr(direct, "_superlu_complex", lambda A, warn: None)
    with pytest.warns(UserWarning, match="_superlu_complex"):
        assert not so.enable("ac")
    assert direct._superlu is KIPI_SUPERLU


def test_ac_solve_moves_by_round_off_only(monkeypatch):
    """KiPI's decoupling-capacitor validation board: the same mesh and ports, |Z| at every
    frequency within the AC proof criterion (1e-8 relative), every field factorization ours."""
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project
    board = Board(ac_decap_board())
    off = solve_ac_rail(board, ac_decap_project(), "+3V3")
    calls = []
    real = so.splu

    def spy(A, **kw):
        calls.append(kw["permc_spec"])
        return real(A, **kw)
    monkeypatch.setattr(so, "splu", spy)
    so.enable("ac")
    on = solve_ac_rail(board, ac_decap_project(), "+3V3")
    assert calls and set(calls) == {"MMD_AT_PLUS_A"}
    assert len(calls) == on.stats.get("field_frequencies", len(calls))
    assert on.stats.get("triangles") == off.stats.get("triangles")
    assert np.array_equal(on.freq_hz, off.freq_hz)
    for p, q in zip(off.probes, on.probes, strict=True):
        assert np.all(np.abs(np.abs(q.z_ohm) - np.abs(p.z_ohm)) <= so.TOLERANCE_REL["ac"] * np.abs(p.z_ohm))


def test_the_ac_and_noise_ram_estimates_say_they_overstate_with_ac_on():
    from coppernicus.engine.ac import ram_estimate
    from coppernicus.engine.noise import noise_ram_estimate
    offs = [ram_estimate(50_000, 24e9, 12e9), noise_ram_estimate(50_000, 24e9)]
    so.enable("ac")
    ons = [ram_estimate(50_000, 24e9, 12e9), noise_ram_estimate(50_000, 24e9)]
    for off, on in zip(offs, ons):
        assert "symmetric" not in off.text
        assert on.bytes == off.bytes and on.risk == off.risk
        assert on.text.startswith(off.text) and "overstates" in on.text and so.ENV in on.text


# ------------------------------------------------------------------- the DC part

def test_dc_part_takes_every_real_factorization_but_the_heat_models():
    A = grid_spd()
    b = np.random.default_rng(8).standard_normal(A.shape[0])
    so.enable("dc")
    sym = splu(A, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
    assert np.array_equal(direct.factorize(A)(b), sym.solve(b))
    with so.thermal_model():                                          # thermal off: KiPI's
        assert np.array_equal(direct.factorize(A)(b), KIPI_SUPERLU(A)(b))
    assert np.array_equal(direct._superlu_complex(ac_like())(b[:960] + 0j),
                          KIPI_SUPERLU_COMPLEX(ac_like())(b[:960] + 0j))


def test_thermal_on_leaves_the_dc_factorizations_kipis():
    A = grid_spd()
    b = np.ones(A.shape[0])
    so.enable("thermal")
    assert np.array_equal(direct.factorize(A)(b), KIPI_SUPERLU(A)(b))


def test_dc_backward_error_on_and_off_alike_with_a_pad_node():
    """A DC-like system: a grid with a pad node tied stiffly to 300 nodes (a dense row, as a
    pad's terminal): the symmetric order's backward error is of the order of KiPI's."""
    G = grid_spd(40, 36, 2, seed=9).tolil()
    n = G.shape[0]
    A = sp.lil_matrix((n + 1, n + 1))
    A[:n, :n] = G
    tied = np.arange(500, 800)
    for i in tied:
        A[n, i] = A[i, n] = -1e3
        A[i, i] += 1e3
    A[n, n] = 1e3 * len(tied) + 1e-6
    A = A.tocsc()
    b = np.random.default_rng(10).standard_normal(n + 1)
    off = backward_error(A, KIPI_SUPERLU(A)(b), b)
    so.enable("dc")
    on = backward_error(A, direct.factorize(A)(b), b)
    assert off < 1e-14 and on < 1e-14
    assert on < 10 * off + 1e-16 and off < 10 * on + 1e-16


def test_dc_solve_moves_by_round_off_only(demo, monkeypatch):
    """The demo rail solved on its own: the same mesh, drops, load voltages, via currents and
    losses within the proof criterion (1e-9 relative, the floor per unit); its factorization
    ours."""
    from coppernicus.engine import solve_rail
    rail = Rail("+3V3", sources=[Source("J1", 3.3)],
                loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])
    off = solve_rail(demo, rail)
    calls = []
    real = so.splu

    def spy(A, **kw):
        calls.append(kw["permc_spec"])
        return real(A, **kw)
    monkeypatch.setattr(so, "splu", spy)
    so.enable("dc")
    on = solve_rail(demo, rail)
    assert calls and set(calls) == {"MMD_AT_PLUS_A"}
    assert on.stats.get("triangles") == off.stats.get("triangles")
    tol = so.TOLERANCE_REL["dc"]
    assert close([t.v_min for t in off.loads], [t.v_min for t in on.loads], tol)
    assert close([off.max_drop], [on.max_drop], tol) and close([off.power_loss], [on.power_loss], tol)
    assert close([v.current for v in off.vias], [v.current for v in on.vias], tol)
    for la, lb in zip(off.layers, on.layers, strict=True):
        assert np.array_equal(la.points, lb.points)
        assert close(la.voltage, lb.voltage, tol)
