"""A raised direct-solve ceiling for the DC copper (coppernicus.engine.superlu_order, on by
default at 400,000 unknowns, COPPERNICUS_DC_DIRECT_MAX=0 or empty restores KiPI's rule, any other
value sets the ceiling): KiPI solves a system of more than 200,000 unknowns with AMG-CG (to a
1e-10 relative residual) where SuperLU is its backend; with the ceiling raised, and the dc part's
symmetric order on, it is factorized instead, as long as its estimated memory fits KiPI's own
rule (half the memory free, the share KiPI allows PARDISO). Results move from CG's tolerance to
an exact solve (the criterion in the module's docstring)."""

import os
import subprocess
import sys
import warnings

import numpy as np
import pytest

import coppernicus.engine.superlu_order as so
from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import demo_board
from coppernicus.project import Load, Rail, Source

spd = so.spd                           # KiPI's kipi.core.solver.spd, as the engine imports it
KIPI_LIMIT = spd.DIRECT_SOLVE_MAX_UNKNOWNS


@pytest.fixture(autouse=True)
def as_configured():
    """Each test starts with dc on and no raised ceiling; what was configured comes back after."""
    before, ceiling = so.enabled(), so.dc_direct_max()
    so.uninstall()
    assert so.enable("dc")
    yield
    so.uninstall()
    if before:
        so.enable(*sorted(before))
    if ceiling is not None:
        so.set_dc_direct_max(ceiling)


def test_kipis_ceiling_unless_raised():
    assert so.dc_direct_max() is None
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT


def test_raised_while_its_memory_fits(monkeypatch):
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    assert so.set_dc_direct_max(400_000)
    assert spd.direct_solve_max_unknowns() == 400_000


def test_never_past_what_the_memory_estimate_allows(monkeypatch):
    """KiPI's rule for PARDISO: the factorization within half the memory free."""
    free = 2**30
    monkeypatch.setattr(so, "free_memory", lambda: free)
    assert so.set_dc_direct_max(10**6)
    cap = so.dc_direct_memory_cap(free)
    assert KIPI_LIMIT < cap < 10**6
    assert spd.direct_solve_max_unknowns() == cap
    budget = spd.PARDISO_MEMORY_SHARE * free
    assert so.dc_direct_memory_bytes(cap) <= budget < so.dc_direct_memory_bytes(cap + 1)


def test_never_below_kipis_own_ceiling(monkeypatch):
    monkeypatch.setattr(so, "free_memory", lambda: 2**20)          # 1 MB free: nothing more fits
    assert so.set_dc_direct_max(400_000)
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT
    monkeypatch.setattr(so, "free_memory", lambda: None)            # unknown: nothing more
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT


def test_not_past_kipis_superlu_limit(monkeypatch):
    """KiPI hands SuperLU nothing above DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO (1M)."""
    monkeypatch.setattr(so, "free_memory", lambda: 2**50)
    assert so.set_dc_direct_max(5 * 10**6)
    assert spd.direct_solve_max_unknowns() == spd.DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO


def test_only_with_the_dc_part_and_superlu(monkeypatch):
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    assert so.set_dc_direct_max(400_000)
    so.disable("dc")                                                # KiPI's order: no raise
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT
    so.enable("dc")
    kipis = so.KIPI_SPD["direct_solve_max_unknowns"]
    monkeypatch.setattr(so.direct, "backend", lambda: "pardiso")    # KiPI's own rule there
    assert spd.direct_solve_max_unknowns() == kipis()


def test_unset_puts_kipis_function_back():
    assert so.set_dc_direct_max(400_000)
    assert spd.direct_solve_max_unknowns is not so.KIPI_SPD["direct_solve_max_unknowns"]
    assert so.set_dc_direct_max(None)
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]
    assert so.set_dc_direct_max(400_000)
    so.uninstall()
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]
    assert so.dc_direct_max() is None


def test_only_on_the_kipi_it_was_written_for(monkeypatch):
    monkeypatch.setattr(spd, "direct_solve_max_unknowns", lambda n: 0)
    with pytest.warns(UserWarning, match="direct_solve_max_unknowns"):
        assert not so.set_dc_direct_max(400_000)
    assert so.dc_direct_max() is None


@pytest.mark.parametrize("text, value", [("400000", 400_000), ("4e5", 400_000), (" 250000 ", 250_000),
                                         ("", None), ("0", None)])
def test_the_ceiling_from_the_environment(text, value):
    assert so.parse_dc_direct_max(text) == value


def test_a_wrong_ceiling_is_named_and_left_out():
    with pytest.warns(UserWarning, match="lots"):
        assert so.parse_dc_direct_max("lots") is None


def test_the_memory_estimate_grows_with_the_unknowns():
    sizes = [100_000, 200_000, 400_000, 800_000]
    mem = [so.dc_direct_memory_bytes(n) for n in sizes]
    assert all(b > a for a, b in zip(mem, mem[1:]))
    for n in (250_000, 500_000):
        cap = so.dc_direct_memory_cap(so.dc_direct_memory_bytes(n) / spd.PARDISO_MEMORY_SHARE)
        assert abs(cap - n) <= 1


@pytest.mark.parametrize("order, value, ceiling, installed", [
    (None, None, "400000", True),              # the default
    (None, "0", "-", False), (None, "", "-", False), (None, "  ", "-", False),   # KiPI's rule
    (None, "250000", "250000", True), (None, "4e5", "400000", True),
    ("off", None, "-", False),                 # KiPI's method: nothing installed
    ("thermal", None, "-", False),             # no dc part: the ceiling does not act
    (None, "lots", "-", False)])               # not a number: warned, KiPI's rule
def test_importing_the_engine_sets_the_ceiling_the_environment_names(order, value, ceiling, installed):
    env = {k: v for k, v in os.environ.items() if k not in (so.DC_DIRECT_ENV, so.ENV)}
    if value is not None:
        env[so.DC_DIRECT_ENV] = value
    if order is not None:
        env[so.ENV] = order
    code = ("import coppernicus.engine.superlu_order as so; "
            "print(so.dc_direct_max() or '-', "
            "so.spd.direct_solve_max_unknowns is not so.KIPI_SPD['direct_solve_max_unknowns'])")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [ceiling, str(installed)]


# ------------------------------------------------------------------- the default (the owner's decision)

def test_unset_the_default_ceiling_is_400000(monkeypatch):
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    so.uninstall()
    assert so.configure_from_env({}) == frozenset({"thermal", "dc"})
    assert so.DC_DIRECT_DEFAULT == 400_000 == so.dc_direct_max()
    assert spd.direct_solve_max_unknowns() == 400_000


@pytest.mark.parametrize("value", ["0", "", " "])
def test_0_or_empty_restores_kipis_rule(value):
    so.uninstall()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        so.configure_from_env({so.DC_DIRECT_ENV: value})
    assert so.dc_direct_max() is None
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT


def test_an_explicit_value_sets_the_ceiling(monkeypatch):
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    so.uninstall()
    so.configure_from_env({so.DC_DIRECT_ENV: "250000"})
    assert so.dc_direct_max() == 250_000 and spd.direct_solve_max_unknowns() == 250_000


def test_an_invalid_value_is_warned_and_keeps_kipis_rule():
    so.uninstall()
    with pytest.warns(UserWarning, match="not a number of unknowns"):
        so.configure_from_env({so.DC_DIRECT_ENV: "lots"})
    assert so.dc_direct_max() is None
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]


@pytest.mark.parametrize("order", ["off", "", "thermal", "ac"])
def test_without_the_dc_part_nothing_is_raised(order):
    """COPPERNICUS_SUPERLU_ORDER=off (or any list without dc) leaves KiPI's direct-solve rule
    untouched: nothing installed for it."""
    so.uninstall()
    so.configure_from_env({so.ENV: order})
    assert so.dc_direct_max() is None
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]
    so.uninstall()
    so.configure_from_env({so.ENV: order, so.DC_DIRECT_ENV: "300000"})     # named, still no dc
    assert spd.direct_solve_max_unknowns is so.KIPI_SPD["direct_solve_max_unknowns"]
    assert spd.direct_solve_max_unknowns() == KIPI_LIMIT


def test_with_pardiso_the_default_keeps_kipis_rule(monkeypatch):
    """With PARDISO KiPI's own rule reads the free memory at each call: it is held fixed here,
    or two calls (KiPI's, then ours through it) see a few pages more or less on a busy machine."""
    so.uninstall()
    so.configure_from_env({})
    kipis = so.KIPI_SPD["direct_solve_max_unknowns"]
    monkeypatch.setattr(so.direct, "backend", lambda: "pardiso")
    monkeypatch.setattr(spd, "free_memory", lambda: 64 * 2**30)
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    assert spd.direct_solve_max_unknowns() == kipis()


def test_a_system_between_400k_and_1m_stays_on_amg_cg_by_default(monkeypatch):
    """The default ceiling: 400,000 unknowns. A 500,000-unknown system is above it (AMG-CG, KiPI's
    path), and so is any up to KiPI's SuperLU limit; scaled down, the demo rail above a lowered
    default stays on AMG-CG (nothing factorized) where a ceiling above it factorizes it."""
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    so.uninstall()
    so.configure_from_env({})
    limit = spd.direct_solve_max_unknowns()
    assert limit == 400_000 and not 500_000 <= limit and not 999_999 <= limit
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 1000)
    monkeypatch.setattr(so, "DC_DIRECT_DEFAULT", 10_000)     # the demo rail has some 22,000 unknowns
    calls = []
    real = so.splu

    def spy(A, **kw):
        calls.append(A.shape[0])
        return real(A, **kw)
    monkeypatch.setattr(so, "splu", spy)
    so.uninstall()
    so.configure_from_env({})
    assert so.dc_direct_max() == 10_000
    rail = Rail("+3V3", sources=[Source("J1", 3.3)],
                loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])
    solve_rail(Board(demo_board()), rail)
    assert calls == []                                       # above the default: AMG-CG
    so.set_dc_direct_max(10**6)
    solve_rail(Board(demo_board()), rail)
    assert len(calls) == 1 and calls[0] > 10_000             # under a ceiling above it: factorized


def test_a_rail_over_kipis_ceiling_is_factorized_and_moves_within_cg_s_tolerance(monkeypatch):
    """KiPI's ceiling lowered so the demo rail goes to AMG-CG; raised again, it is factorized:
    the same mesh; drops, load voltages and losses within the dc_direct criterion (CG's own
    accuracy at its 1e-10 relative residual)."""
    monkeypatch.setattr(spd, "DIRECT_SOLVE_MAX_UNKNOWNS", 1000)
    monkeypatch.setattr(so, "free_memory", lambda: 64 * 2**30)
    calls = []
    real = so.splu

    def spy(A, **kw):
        calls.append(A.shape[0])
        return real(A, **kw)
    monkeypatch.setattr(so, "splu", spy)
    board = Board(demo_board())
    rail = Rail("+3V3", sources=[Source("J1", 3.3)],
                loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)])
    cg = solve_rail(board, rail)
    assert calls == []                                              # AMG-CG: nothing factorized
    assert so.set_dc_direct_max(10**6)
    exact = solve_rail(board, rail)
    assert len(calls) == 1 and calls[0] > 1000                      # factorized, past KiPI's ceiling
    assert exact.stats.get("triangles") == cg.stats.get("triangles")
    tol = so.TOLERANCE_REL["dc_direct"]
    vmax = max(abs(t.v_min) for t in cg.loads)
    for a, b in zip(cg.loads, exact.loads, strict=True):
        assert abs(a.v_min - b.v_min) <= tol * vmax
    assert abs(cg.max_drop - exact.max_drop) <= tol * vmax
    assert abs(cg.power_loss - exact.power_loss) <= tol * cg.power_loss
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert np.isfinite(exact.max_drop)
