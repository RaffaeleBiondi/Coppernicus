"""SuperLU in its symmetric mode for KiPI's factorizations: an opt-in speed option, off by
default.

KiPI factorizes with SciPy's SuperLU in the COLAMD column order (``kipi/core/solver/direct.py``,
``_superlu`` and ``_superlu_complex``), which ignores that its matrices are symmetric.
SuperLU's symmetric mode (minimum degree on the pattern of A + Aᵀ, ``SymmetricMode``, the
diagonal as pivot) fills about half as much on them: on the heat model of a 50 mm synthetic
board (250,000 unknowns, 4 layers) 121 M → 61 M non-zeros in the factors, 3.2 → 1.4 GB and
3.6 → 2.0 s for the factorization, each later solve 2.4× faster (macOS arm64, a loaded
machine).

Parts, each off unless enabled:

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

OPEN RISK (``dc`` and ``ac``): KiPI's own comment at ``direct._superlu`` says this order "takes
minutes" on its meshes, whose merged pad, ring and port groups give dense rows; on synthetic
meshes, dense pad rows included, it took 0.5 s. Neither is to be made a default before a run on
a real board. The heat model's grid (at most 7 non-zeros a row) does not have such rows.

Every factorization made here is checked as KiPI checks the ones it does not trust: a real one
by its backward error for a right-hand side of ones against KiPI's ``direct.CHECK_TOL``, a
complex one by its relative residual against ``direct.CHECK_TOL_COMPLEX`` (one solve and one
product with A). One that fails, or that SuperLU refuses, is warned of (once) and redone by
KiPI's own function. Where PARDISO loads (the optional ``pypardiso``, x86-64 Windows and Linux),
KiPI reaches these functions only after PARDISO failed: KiPI's own then always run.

Enabled by the environment variable ``COPPERNICUS_SUPERLU_ORDER`` (comma-separated parts, read
when :mod:`coppernicus.engine` is imported) or by :func:`enable`.

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
from kipi.core.solver import direct
from scipy.sparse.linalg import splu

ENV = "COPPERNICUS_SUPERLU_ORDER"
PARTS = ("thermal", "ac", "dc")
KIPI_VERSION = "1.0.1"
# SuperLU's symmetric mode: the column order and, for symmetric positive definite matrices,
# the diagonal as pivot (threshold 0).
ORDER = "MMD_AT_PLUS_A"
SPD_PIVOT_THRESHOLD = 0.0
# Complex symmetric (indefinite) systems: partial pivoting kept, the diagonal preferred.
COMPLEX_PIVOT_THRESHOLD = 0.1
# The proof criteria of "round-off only" per part (see the module's docstring): relative, with
# an absolute floor per unit of this times the unit's largest magnitude.
TOLERANCE_REL = {"thermal": 1e-9, "ac": 1e-6, "dc": 1e-9}
# KiPI's functions replaced, with the parameters they take.
_SEAMS = {"_superlu": ["A"], "_superlu_complex": ["A"]}
# The thread marker that sends a thread's factorizations to KiPI's functions (kipi_path).
_KIPI = "kipi"

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
    """Put KiPI's functions back and turn every part off."""
    global _enabled, _kipi, _warned
    with _lock:
        if _kipi is not None:
            for name, fn in _kipi.items():
                setattr(direct, name, fn)
        _kipi = None
        _enabled = frozenset()
        _warned = False
        _fell_back.clear()


def configure_from_env(environ: Mapping[str, str] = os.environ) -> frozenset:
    """Turn on the parts :data:`ENV` names (none when unset); the parts on afterwards."""
    parts = parse(environ.get(ENV, ""), _stacklevel=3)
    if parts:
        enable(*sorted(parts), _stacklevel=3)
    return enabled()


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
