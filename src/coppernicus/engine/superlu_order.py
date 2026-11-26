"""SuperLU in its symmetric mode for KiPI's factorizations: a speed option, on by default for the
board temperature and the DC solves (``thermal`` and ``dc``); ``COPPERNICUS_SUPERLU_ORDER=off``
restores KiPI's method exactly.

KiPI factorizes with SciPy's SuperLU in the COLAMD column order (``kipi/core/solver/direct.py``,
``_superlu`` and ``_superlu_complex``), which ignores that its matrices are symmetric.
SuperLU's symmetric mode (minimum degree on the pattern of A + Aᵀ, ``SymmetricMode``, the
diagonal as pivot) fills about half as much on them: on the heat model of a 50 mm synthetic
board (250,000 unknowns, 4 layers) 121 M → 61 M non-zeros in the factors, 3.2 → 1.4 GB and
3.6 → 2.0 s for the factorization, each later solve 2.4× faster (macOS arm64, a loaded
machine).

Parts (:data:`DEFAULT_PARTS` on by default, ``ac`` only when named):

* ``thermal``: the board temperature's heat model (KiPI ``BoardThermalModel``, symmetric
  positive definite), factorized without pivoting, which is stable for such a matrix. Only the
  factorization made inside :func:`thermal_model` (on the calling thread, so solves in other
  threads keep KiPI's) uses it.
* ``ac``: the complex symmetric systems of the AC field solves (one per field frequency; the
  noise and tolerance solves run the same), with partial pivoting kept at threshold 0.1 (the
  diagonal preferred, a row swapped when its pivot is under a tenth of the column's largest;
  KiPI's SciPy default is 1.0), since these systems are indefinite. Process-wide: KiPI solves
  the frequencies in its own threads. The factor keeps KiPI's name ``superlu`` and its true
  memory, which KiPI's thread count reads (``kipi/ac/solver/field.py`` ``threads``). On the
  pdn example (``examples/pdn``; 19,783 unknowns, 111 field frequencies): factors 5.2 M →
  3.3 M non-zeros, each 0.25 → 0.155 s and 208 → 135 MB.
* ``dc``: every other real factorization, outside :func:`thermal_model`: the DC copper's
  direct solves (KiPI ``SpdSolver``: up to 200,000 unknowns with SuperLU, and at any size its
  fallback after a CG that did not converge, spd.py:163), symmetric positive definite, without
  pivoting. On the rails of a 100 mm synthetic board (103,000 and 169,000 unknowns): factors
  halved, factorization 0.28 → 0.20 s and 0.58 → 0.42 s, solves 2× faster.

KiPI's own comment at ``direct._superlu`` says this order "takes minutes" on its meshes, whose
merged pad, ring and port groups give dense rows. On synthetic meshes, dense pad rows included,
it took 0.5 s, and on a large private test board the DC factorizations took 0.73–0.91× KiPI's time
(the base of the ``dc`` default; the electro-thermal solve took about half the time and memory).
``ac`` stays opt-in until a real board's AC solve is checked. The heat model's grid (at most 7
non-zeros a row) does not have such rows.

Every factorization made here is checked as KiPI checks the ones it does not trust: a real one
by its backward error for a right-hand side of ones against KiPI's ``direct.CHECK_TOL``, a
complex one by its relative residual against ``direct.CHECK_TOL_COMPLEX`` (one solve and one
product with A). One that fails, or that SuperLU refuses, is warned of (once) and redone by
KiPI's own function. Where PARDISO loads (the optional ``pypardiso``, x86-64 Windows and Linux),
KiPI reaches these functions only after PARDISO failed: KiPI's own then always run.

Set by the environment variable ``COPPERNICUS_SUPERLU_ORDER``, read when :mod:`coppernicus.engine`
is imported: unset, the default parts (``thermal``, ``dc``); ``off`` or an empty value, none;
otherwise the comma-separated parts it names (e.g. ``thermal,dc,ac``). Or by :func:`enable`.

Off, nothing is replaced and KiPI's own path runs: bit-identical. On, a different order rounds
differently, so results move by round-off only. The proof criteria for that (checks of
"round-off only", not limits of the product), :data:`TOLERANCE_REL`:

* thermal, dc: the benchmark fingerprint's scalars agree within 1e-9 relative, with a floor per
  unit for values near zero (the unit's largest magnitude in the fingerprint × 1e-9; together:
  |Δ| ≤ 1e-9 × the largest value of that unit). Exception: a point maximum (``j_max``) held by
  a degenerate triangle whose node voltages span fewer than about 1e7 ulp follows those
  voltages' last bits, and is judged against KiPI's own spread instead. On a real board's core
  rail one sub-nanometre sliver (edges 1.6e-7 to 4.8e-7 mm, node voltages 1.9e6 ulp apart) held
  ``j_max``: it moved 7.5e-7 with the thermal part on, and up to 2.7e-7 under KiPI's own call on
  the heat model with its unknowns permuted at random (3 seeds), while the rail's largest |J|
  off such triangles agreed to 3.9e-12;
* ac: |Z| within 1e-6 relative, since KiPI's own |Z| carries round-off of order 1e-8 to 1e-7
  (max measured 6.0e-8): on the noise example (``examples/noise``) KiPI's own call (COLAMD,
  threshold 1.0) on the same systems with their unknowns permuted at random (6 seeds) moves
  |Z| by up to 6.0e-8, and this order by 5.2e-8, of the same order as KiPI's own spread, up to
  about 10 MHz (that probe has no target: never judged); on the pdn example (``examples/pdn``)
  everything agrees to about 2e-11 in its judged band, 3.3e-11 over the whole sweep;
* and the backward error ‖b − Ax‖ / (‖A‖ ‖x‖ + ‖b‖), on and off, of the same order (a few
  1e-16).

The seam: KiPI's private ``direct._superlu`` and ``direct._superlu_complex`` are replaced
process-wide, once, by functions that hand every factorization outside an enabled part to
KiPI's own. They are installed only on the KiPI they were written for (:data:`KIPI_VERSION`)
with those functions of the expected signature; otherwise a warning (once) and KiPI's path.
:func:`kipi_path` marks a thread's factorizations as KiPI's whatever is on (the electro-thermal
drift reference). :func:`uninstall` puts KiPI's back (the tests).

The DC direct ceiling (opt-in: ``COPPERNICUS_DC_DIRECT_MAX=<unknowns>`` or
:func:`set_dc_direct_max`). KiPI solves a DC system of more than 200,000 unknowns with AMG-CG, to
a 1e-10 relative residual, where SuperLU is its backend (``spd.DIRECT_SOLVE_MAX_UNKNOWNS``). With
the ceiling set and the ``dc`` part active, a system up to the ceiling is factorized instead
(and checked as above), never past KiPI's SuperLU limit (``DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO``,
1,000,000) nor past the largest system whose estimated memory (:func:`dc_direct_memory_bytes`)
fits half the memory free, the share KiPI allows PARDISO. On synthetic boards (tools/bench, a
loaded machine) a rail of 262,000 to 352,000 unknowns solved in about half the time, a power tree
in 0.6–0.9×, an electro-thermal tree in 0.8–1.0× (each iteration factorizes again, where AMG-CG
reuses its hierarchy), all with 1.3–1.8× the peak memory; results moved by at most 5.9e-11
relative. Criterion ``dc_direct``, 1e-9 relative with the floor per unit: CG's 1e-10 relative
residual bounds its own error at about that order; the exact solve removes it.

The memory estimates (the board temperature's
:func:`coppernicus.engine.thermal.thermal_ram_estimate`, the AC and noise solves'
:func:`coppernicus.engine.ac.ram_estimate` and :func:`coppernicus.engine.noise.noise_ram_estimate`)
were measured with KiPI's order: with their part :func:`active` they overstate, and say so.
"""

from __future__ import annotations

import inspect
import os
import threading
import warnings
from contextlib import contextmanager
from typing import Mapping, Optional, Set

import kipi
import numpy as np
import scipy.sparse as sp
from kipi.core.memory import free_memory
from kipi.core.solver import direct, spd
from scipy.sparse.linalg import splu

ENV = "COPPERNICUS_SUPERLU_ORDER"
PARTS = ("thermal", "ac", "dc")
# On with the variable unset (the owner's decision); ac waits for a real-board check.
DEFAULT_PARTS = ("thermal", "dc")
# The variable's value that turns every part off (as does an empty value): KiPI's method.
OFF = "off"
KIPI_VERSION = "1.0.1"
# SuperLU's symmetric mode: the column order and, for symmetric positive definite matrices,
# the diagonal as pivot (threshold 0).
ORDER = "MMD_AT_PLUS_A"
SPD_PIVOT_THRESHOLD = 0.0
# Complex symmetric (indefinite) systems: partial pivoting kept, the diagonal preferred.
COMPLEX_PIVOT_THRESHOLD = 0.1
# The proof criteria of "round-off only" per part (see the module's docstring): relative, with
# an absolute floor per unit of this times the unit's largest magnitude.
TOLERANCE_REL = {"thermal": 1e-9, "ac": 1e-6, "dc": 1e-9, "dc_direct": 1e-9}
# KiPI's functions replaced, with the parameters they take.
_SEAMS = {"_superlu": ["A"], "_superlu_complex": ["A"]}
# The thread marker that sends a thread's factorizations to KiPI's functions (kipi_path).
_KIPI = "kipi"

# The raised direct-solve ceiling for the DC copper (opt-in): see set_dc_direct_max.
DC_DIRECT_ENV = "COPPERNICUS_DC_DIRECT_MAX"
# The memory a DC direct solve in SuperLU's symmetric order adds, bytes = A · unknowns ** B: the
# least-squares fit (A 72.15, B 1.2343, within -7 % / +8 % of each point) of the peak resident
# memory one factorization and one solve added, one fresh process each, on the DC systems of
# synthetic boards (tools/bench grid-100 to grid-200; macOS arm64), scaled up 8.1 % so that it
# lies at or above every point: (unknowns, MB added).
DC_DIRECT_FIT_POINTS = ((102_956, 107), (162_832, 178), (169_188, 205), (262_412, 322),
                        (274_252, 383), (352_301, 449), (456_000, 633), (620_464, 1036))
DC_DIRECT_FIT_A, DC_DIRECT_FIT_B = 78.0, 1.2343
# KiPI's function replaced, with the parameters it takes.
_SPD_SEAM = {"direct_solve_max_unknowns": []}
#: KiPI's own, as kipi.core.solver.spd had it when this module was imported.
KIPI_SPD = {name: getattr(spd, name) for name in _SPD_SEAM}
_dc_direct_max: Optional[int] = None
_dc_warned = False

_lock = threading.Lock()
_enabled: frozenset = frozenset()
_kipi: Optional[dict] = None          # KiPI's own functions while ours are installed
_local = threading.local()
_warned = False
_fell_back: Set[str] = set()          # "real" / "complex": a check failed once (warned)


def parse(text: str, _stacklevel: int = 2) -> Set[str]:
    """The parts named in ``text`` (comma-separated, any case); an unknown name is warned of
    and left out."""
    parts = {w.strip().lower() for w in (text or "").split(",") if w.strip()}
    unknown = sorted(parts - set(PARTS))
    if unknown:
        warnings.warn(f"{ENV}: unknown part(s) {', '.join(unknown)} left out "
                      f"(known: {', '.join(PARTS)})", UserWarning, stacklevel=_stacklevel)
    return parts & set(PARTS)


def enabled() -> frozenset:
    """The parts on now."""
    return _enabled


def active(part: str) -> bool:
    """Whether ``part``'s factorizations run in SuperLU's symmetric mode here: it is on and
    KiPI's backend is SuperLU (with PARDISO, KiPI's own functions only meet its failures)."""
    return part in _enabled and direct.backend() == "superlu"


def seam_problem() -> Optional[str]:
    """Why ours can't replace KiPI's functions here, or None."""
    version = getattr(kipi, "__version__", None)
    if version != KIPI_VERSION:
        return f"written for KiPI {KIPI_VERSION}, this is KiPI {version}"
    for name, params in _SEAMS.items():
        fn = getattr(direct, name, None)
        if _kipi is not None and fn is _ours().get(name):
            continue
        if not callable(fn):
            return f"KiPI has no kipi.core.solver.direct.{name}"
        try:
            got = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            got = None
        if got != params:
            return (f"kipi.core.solver.direct.{name} takes ({', '.join(got or ['?'])}), "
                    f"not ({', '.join(params)})")
    return None


def _ours() -> dict:
    return {"_superlu": _superlu, "_superlu_complex": _superlu_complex}


def _installed() -> bool:
    """Whether every one of KiPI's replaced functions is ours now, by identity."""
    return all(getattr(direct, name, None) is fn for name, fn in _ours().items())


def _warn_once(problem: str, stacklevel: int) -> None:
    global _warned
    if not _warned:
        _warned = True
        warnings.warn(f"{ENV}: SuperLU's symmetric mode not used ({problem}); KiPI's own "
                      "factorizations run", UserWarning, stacklevel=stacklevel + 1)


def enable(*parts: str, _stacklevel: int = 2) -> bool:
    """Turn ``parts`` on (with the others already on), installing ours if needed. False, with a
    warning the first time, when KiPI's functions are not the ones ours were written for, or
    when ours, once installed, have been replaced since: then nothing more is turned on."""
    global _enabled, _kipi
    wanted = set(parts)
    unknown = sorted(wanted - set(PARTS))
    if unknown:
        raise ValueError(f"unknown part(s) {', '.join(unknown)} (known: {', '.join(PARTS)})")
    with _lock:
        if not wanted:
            return True
        problem = seam_problem()
        if problem is not None:
            _warn_once(problem, _stacklevel)
            return False
        if _kipi is None:
            _kipi = {name: getattr(direct, name) for name in _SEAMS}
            for name, fn in _ours().items():
                setattr(direct, name, fn)
        elif not _installed():
            _warn_once("KiPI's factorization functions were replaced by something else after "
                       "ours were installed", _stacklevel)
            return False
        _enabled = frozenset(_enabled | wanted)
        return True


def disable(*parts: str) -> None:
    """Turn ``parts`` off (all with none given). Ours stay installed, handing everything to
    KiPI's functions; :func:`uninstall` removes them."""
    global _enabled
    with _lock:
        _enabled = frozenset(_enabled - set(parts)) if parts else frozenset()


def uninstall() -> None:
    """Put KiPI's functions back, turn every part off and drop a raised DC direct ceiling."""
    global _enabled, _kipi, _warned, _dc_direct_max, _dc_warned
    with _lock:
        if _kipi is not None:
            for name, fn in _kipi.items():
                setattr(direct, name, fn)
        for name, fn in KIPI_SPD.items():
            setattr(spd, name, fn)
        _kipi = None
        _enabled = frozenset()
        _dc_direct_max = None
        _warned = _dc_warned = False
        _fell_back.clear()


def configure_from_env(environ: Mapping[str, str] = os.environ) -> frozenset:
    """Turn on the parts :data:`ENV` names: unset, :data:`DEFAULT_PARTS`; ``off`` or empty,
    none (nothing installed: KiPI's method); else the parts listed. The parts on afterwards."""
    value = environ.get(ENV)
    if value is None:
        parts = set(DEFAULT_PARTS)
    elif value.strip().lower() in ("", OFF):
        parts = set()
    else:
        parts = parse(value, _stacklevel=3)
    if parts:
        enable(*sorted(parts), _stacklevel=3)
    ceiling = parse_dc_direct_max(environ.get(DC_DIRECT_ENV), _stacklevel=3)
    if ceiling is not None:
        set_dc_direct_max(ceiling, _stacklevel=3)
    return enabled()


# ---------------------------------------------------------------------- the DC direct ceiling

def dc_direct_memory_bytes(unknowns: float) -> float:
    """The memory a DC direct solve of ``unknowns`` adds in SuperLU's symmetric order (the fit
    of :data:`DC_DIRECT_FIT_POINTS`)."""
    return DC_DIRECT_FIT_A * float(unknowns) ** DC_DIRECT_FIT_B


def dc_direct_memory_cap(free_bytes: float) -> int:
    """The largest system whose :func:`dc_direct_memory_bytes` fits in KiPI's share of the
    memory free for PARDISO's factorizations (``spd.PARDISO_MEMORY_SHARE``)."""
    budget = spd.PARDISO_MEMORY_SHARE * float(free_bytes)
    if budget <= 0:
        return 0
    n = int((budget / DC_DIRECT_FIT_A) ** (1.0 / DC_DIRECT_FIT_B))
    while n > 0 and dc_direct_memory_bytes(n) > budget:        # the float root, rounded down
        n -= 1
    while dc_direct_memory_bytes(n + 1) <= budget:
        n += 1
    return n


def dc_direct_max() -> Optional[int]:
    """The raised ceiling set now (None: KiPI's)."""
    return _dc_direct_max


def parse_dc_direct_max(text: Optional[str], _stacklevel: int = 2) -> Optional[int]:
    """A ceiling from :data:`DC_DIRECT_ENV`'s value (a whole number of unknowns, ``4e5``
    accepted); None when unset, empty or 0. Anything else is warned of and left out."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not (value >= 0 and value == int(value)):
        warnings.warn(f"{DC_DIRECT_ENV}: {text!r} is not a number of unknowns; KiPI's ceiling kept",
                      UserWarning, stacklevel=_stacklevel)
        return None
    return int(value) or None


def _spd_seam_problem() -> Optional[str]:
    version = getattr(kipi, "__version__", None)
    if version != KIPI_VERSION:
        return f"written for KiPI {KIPI_VERSION}, this is KiPI {version}"
    for name, params in _SPD_SEAM.items():
        fn = getattr(spd, name, None)
        if fn is _direct_solve_max_unknowns:
            continue
        if not callable(fn):
            return f"KiPI has no kipi.core.solver.spd.{name}"
        try:
            got = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            got = None
        if got != params:
            return f"kipi.core.solver.spd.{name} takes ({', '.join(got or ['?'])}), not ()"
    return None


def set_dc_direct_max(unknowns: Optional[int], _stacklevel: int = 2) -> bool:
    """Raise KiPI's direct-solve ceiling for the DC copper to ``unknowns`` (None: KiPI's own
    again). It acts only while the ``dc`` part is :func:`active` (SuperLU's symmetric order, with
    SuperLU as KiPI's backend), never past KiPI's SuperLU limit
    (``spd.DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO``) nor past the largest system whose estimated
    memory (:func:`dc_direct_memory_bytes`) fits KiPI's own rule, half the memory free. False,
    with a warning the first time, on a KiPI this was not written for."""
    global _dc_direct_max, _dc_warned
    with _lock:
        if unknowns is None:
            for name, fn in KIPI_SPD.items():
                setattr(spd, name, fn)
            _dc_direct_max = None
            return True
        problem = _spd_seam_problem()
        if problem is not None:
            if not _dc_warned:
                _dc_warned = True
                warnings.warn(f"{DC_DIRECT_ENV}: KiPI's direct-solve ceiling kept ({problem})",
                              UserWarning, stacklevel=_stacklevel)
            return False
        spd.direct_solve_max_unknowns = _direct_solve_max_unknowns
        _dc_direct_max = int(unknowns)
        return True


def _direct_solve_max_unknowns() -> int:
    """KiPI's ``spd.direct_solve_max_unknowns`` (spd.py:53), raised to :func:`dc_direct_max`
    where it applies (see :func:`set_dc_direct_max`)."""
    kipis = KIPI_SPD["direct_solve_max_unknowns"]()
    limit = _dc_direct_max
    if limit is None or not active("dc"):
        return kipis
    free = free_memory()
    cap = dc_direct_memory_cap(free) if free else 0
    return max(kipis, min(limit, cap, spd.DIRECT_SOLVE_MAX_UNKNOWNS_PARDISO))


@contextmanager
def _marked(part: str):
    before = getattr(_local, "part", None)
    _local.part = part
    try:
        yield
    finally:
        _local.part = before


def thermal_model():
    """Marks the factorizations made on this thread meanwhile as the heat model's."""
    return _marked("thermal")


def kipi_path():
    """Marks the factorizations made on this thread meanwhile as KiPI's own, whatever part is
    on (KiPI's reference electro-thermal solve, for the drift test)."""
    return _marked(_KIPI)


def current_part() -> Optional[str]:
    """The part this thread's factorizations belong to now (None: no marked part; ``"kipi"``:
    KiPI's own path)."""
    return getattr(_local, "part", None)


def _kipi_fn(name: str):
    """KiPI's own function ``name``: kept while ours are installed, back in its module after
    :func:`uninstall` (for a factorization already under way then)."""
    kept = _kipi
    return kept[name] if kept is not None else getattr(direct, name)


def _fall_back(kind: str, name: str, why: str, A):
    """KiPI's own ``name`` for A, after ours failed its check (warned of once per ``kind``)."""
    if kind not in _fell_back:
        _fell_back.add(kind)
        warnings.warn(f"{ENV}: SuperLU's symmetric mode failed its check on a {kind} system "
                      f"({why}); KiPI's own factorization used for it", RuntimeWarning, stacklevel=3)
    return _kipi_fn(name)(A)


def _superlu(A: sp.spmatrix) -> direct.DirectFactor:
    """KiPI's ``direct._superlu`` (a real symmetric positive definite A), in SuperLU's
    symmetric mode when the part it belongs to is on (``thermal`` inside
    :func:`thermal_model`, ``dc`` outside) and SuperLU is KiPI's backend; checked by its
    backward error against KiPI's ``CHECK_TOL``, KiPI's own on failure."""
    part = current_part() or "dc"
    if part not in _enabled or direct.backend() != "superlu" or A.shape[0] == 0:
        return _kipi_fn("_superlu")(A)
    try:
        lu = splu(sp.csc_matrix(A), permc_spec=ORDER, diag_pivot_thresh=SPD_PIVOT_THRESHOLD,
                  options={"SymmetricMode": True})
    except (RuntimeError, ValueError) as e:                 # e.g. an exactly singular pivot
        return _fall_back("real", "_superlu", f"{type(e).__name__}: {e}", A)
    factor = direct.DirectFactor(lu.solve, "superlu")
    b = np.ones(A.shape[0])
    err = direct.backward_error(A, factor(b), b)
    if not (np.isfinite(err) and err <= direct.CHECK_TOL):
        return _fall_back("real", "_superlu", f"backward error {err:.1e}", A)
    return factor


def _superlu_complex(A: sp.spmatrix) -> direct.ComplexFactor:
    """KiPI's ``direct._superlu_complex`` (a complex symmetric A), in SuperLU's symmetric mode
    with partial pivoting when the ``ac`` part is on and SuperLU is KiPI's backend; checked by
    its relative residual against KiPI's ``CHECK_TOL_COMPLEX``, KiPI's own on failure."""
    if ("ac" not in _enabled or current_part() == _KIPI or direct.backend() != "superlu"
            or A.shape[0] == 0):
        return _kipi_fn("_superlu_complex")(A)
    A = sp.csc_matrix(A, dtype=complex)
    try:
        lu = splu(A, permc_spec=ORDER, diag_pivot_thresh=COMPLEX_PIVOT_THRESHOLD,
                  options={"SymmetricMode": True})
    except (RuntimeError, ValueError) as e:
        return _fall_back("complex", "_superlu_complex", f"{type(e).__name__}: {e}", A)
    b = np.ones(A.shape[0], dtype=complex)
    rel = float(np.linalg.norm(b - A @ lu.solve(b)) / np.linalg.norm(b))
    if not (np.isfinite(rel) and rel <= direct.CHECK_TOL_COMPLEX):
        return _fall_back("complex", "_superlu_complex", f"relative residual {rel:.1e}", A)
    # L and U: a complex value and a row index each, as KiPI counts them.
    return direct.ComplexFactor(lu.solve, "superlu", nbytes=24 * int(lu.nnz))
