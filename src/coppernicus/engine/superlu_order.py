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
  memory, which KiPI's thread count reads (``kipi/ac/solver/field.py`` ``threads``). On KiPI's
  pdn example (19,783 unknowns, 111 field frequencies): factors 5.2 M → 3.3 M non-zeros, each
  0.25 → 0.155 s and 208 → 135 MB.
* ``dc``: every other real factorization, outside :func:`thermal_model`: the DC copper's
  direct solves (KiPI ``SpdSolver``, up to 200,000 unknowns with SuperLU), symmetric positive
  definite, without pivoting. On the rails of a 100 mm synthetic board (103,000 and 169,000
  unknowns): factors halved, factorization 0.28 → 0.20 s and 0.58 → 0.42 s, solves 2× faster.
  OPEN RISK: KiPI's own comment at ``direct._superlu`` says this order "takes minutes" on its
  meshes; on synthetic meshes, dense pad rows included, it took 0.5 s. Not to be made a default
  before a run on a real board.

Enabled by the environment variable ``COPPERNICUS_SUPERLU_ORDER`` (comma-separated parts, read
when :mod:`coppernicus.engine` is imported) or by :func:`enable`.

Off, nothing is replaced and KiPI's own path runs: bit-identical. On, a different order rounds
differently, so results move by round-off only. The proof criteria for that (checks of
"round-off only", not limits of the product), :data:`TOLERANCE_REL`: the benchmark fingerprint's
scalars agree within 1e-9 relative (thermal, dc), with a floor per unit for values near zero (the
unit's largest magnitude in the fingerprint × 1e-9); |Z| within 1e-6 relative (ac), since KiPI's
own |Z| carries round-off of order 1e-7: on KiPI's noise example its own call (COLAMD,
threshold 1.0) on the same systems with their unknowns permuted at random (6 seeds) moves |Z|
by up to 6.0e-8, as far as this order does (5.2e-8), up to about 10 MHz (that probe has no
target: never judged); on the pdn example both agree to 3.3e-11 everywhere, 1.9e-11 in its
judged band; and the backward error ‖b − Ax‖ / (‖A‖ ‖x‖ + ‖b‖) is of the same order on and off
(both a few 1e-16).

The seam: KiPI's private ``direct._superlu`` and ``direct._superlu_complex`` are replaced
process-wide, once, by functions that hand every factorization outside an enabled part to KiPI's
own. They are installed only on the KiPI they were written for (:data:`KIPI_VERSION`) with those
functions of the expected signature;
otherwise a warning (once) and KiPI's path. :func:`uninstall` puts KiPI's back (the tests).

The memory estimates (the board temperature's :func:`coppernicus.engine.thermal.thermal_ram_estimate`,
the AC and noise solves' :func:`coppernicus.engine.ac.ram_estimate` and
:func:`coppernicus.engine.noise.noise_ram_estimate`) were measured with KiPI's order: with their
part on they overstate, and say so.
"""

from __future__ import annotations

import inspect
import os
import threading
import warnings
from contextlib import contextmanager
from typing import Mapping, Optional, Set

import kipi
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

_lock = threading.Lock()
_enabled: frozenset = frozenset()
_kipi: Optional[dict] = None          # KiPI's own functions while ours are installed
_local = threading.local()
_warned = False


def parse(text: str) -> Set[str]:
    """The parts named in ``text`` (comma-separated, any case); an unknown name is warned of
    and left out."""
    parts = {w.strip().lower() for w in (text or "").split(",") if w.strip()}
    unknown = sorted(parts - set(PARTS))
    if unknown:
        warnings.warn(f"{ENV}: unknown part(s) {', '.join(unknown)} left out (known: {', '.join(PARTS)})",
                      UserWarning, stacklevel=2)
    return parts & set(PARTS)


def enabled() -> frozenset:
    """The parts on now."""
    return _enabled


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


def enable(*parts: str) -> bool:
    """Turn ``parts`` on (with the others already on), installing ours if needed. False, with a
    warning the first time, when KiPI's functions are not the ones ours were written for: then
    nothing is on and KiPI's path runs."""
    global _enabled, _kipi, _warned
    wanted = set(parts)
    unknown = sorted(wanted - set(PARTS))
    if unknown:
        raise ValueError(f"unknown part(s) {', '.join(unknown)} (known: {', '.join(PARTS)})")
    with _lock:
        if not wanted:
            return True
        problem = seam_problem()
        if problem is not None:
            if not _warned:
                _warned = True
                warnings.warn(f"{ENV}: SuperLU's symmetric mode not used ({problem}); KiPI's own "
                              "factorizations run", UserWarning, stacklevel=2)
            return False
        if _kipi is None:
            _kipi = {name: getattr(direct, name) for name in _SEAMS}
            for name, fn in _ours().items():
                setattr(direct, name, fn)
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


def configure_from_env(environ: Mapping[str, str] = os.environ) -> frozenset:
    """Turn on the parts :data:`ENV` names (none when unset); the parts on afterwards."""
    parts = parse(environ.get(ENV, ""))
    if parts:
        enable(*sorted(parts))
    return enabled()


@contextmanager
def thermal_model():
    """Marks the factorizations made on this thread meanwhile as the heat model's."""
    before = getattr(_local, "part", None)
    _local.part = "thermal"
    try:
        yield
    finally:
        _local.part = before


def current_part() -> Optional[str]:
    """The part this thread's factorizations belong to now (None: no marked part)."""
    return getattr(_local, "part", None)


def _superlu(A: sp.spmatrix) -> direct.DirectFactor:
    """KiPI's ``direct._superlu`` (a real symmetric positive definite A), in SuperLU's
    symmetric mode when the part it belongs to is on: ``thermal`` inside
    :func:`thermal_model`, ``dc`` outside."""
    if (current_part() or "dc") not in _enabled:
        return _kipi["_superlu"](A)
    lu = splu(sp.csc_matrix(A), permc_spec=ORDER, diag_pivot_thresh=SPD_PIVOT_THRESHOLD,
              options={"SymmetricMode": True})
    return direct.DirectFactor(lu.solve, "superlu")


def _superlu_complex(A: sp.spmatrix) -> direct.ComplexFactor:
    """KiPI's ``direct._superlu_complex`` (a complex symmetric A), in SuperLU's symmetric mode
    with partial pivoting when the ``ac`` part is on."""
    if "ac" not in _enabled:
        return _kipi["_superlu_complex"](A)
    lu = splu(sp.csc_matrix(A, dtype=complex), permc_spec=ORDER, diag_pivot_thresh=COMPLEX_PIVOT_THRESHOLD,
              options={"SymmetricMode": True})
    # L and U: a complex value and a row index each, as KiPI counts them.
    return direct.ComplexFactor(lu.solve, "superlu", nbytes=24 * int(lu.nnz))
