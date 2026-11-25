"""The tolerance stack-up's engine side (Task 11): KiPI's limits and judge, a thin copy of its
``stack()`` taking Coppernicus's parts (so completeness is Coppernicus's: a part not known is
missing, never 0), the pairing of loads and probes by KiPI's rule, the check of a load's AC
tolerance against its probe's ``ac_tolerance_v``, and KiPI's own ``stack()`` as the drift
reference. The parts themselves are put together by :mod:`coppernicus.tolerance`.

KiPI (``kipi/tolerance/evaluate.py:1-10``): per load, each side apart (below and above
nominal), every value a deviation in volts, never negative: DC = the DC drop (or boost) + the
source's DC accuracy, against the DC tolerance; AC = ripple + transient, against the AC
tolerance; total = DC + AC, against the tolerance. Field by field: ``docs/engine-mapping.md``
(Task 11).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from kipi.dc.pi.model import Load as KipiLoad
from kipi.dc.pi.model import PinRef, Vrm
from kipi.tolerance import evaluate as kipi_evaluate
from kipi.tolerance.evaluate import CHECK_PARTS, EPS_V, deviation_limits, judge  # noqa: F401 (EPS_V re-exported)
from kipi.tolerance.gather import probe_for_load
from kipi.tolerance.model import Contribution, LoadEntry

from coppernicus.engine.board import Board, EngineError
from coppernicus.engine.pisetup import _numbers, _pads, terminal_names, to_pisetup
from coppernicus.project import Load, Probe, Project, Source
from coppernicus.results import TOLERANCE_CHECKS as CHECKS
from coppernicus.results import TOLERANCE_PARTS as PARTS  # noqa: F401 (re-exported)
from coppernicus.results import ToleranceCheck

Pair = Tuple[float, float]
Limit = Tuple[Optional[float], Optional[float]]
#: The load's tolerance judged by each check, without its unit (``_pct`` or ``_mv``).
CHECK_TOLERANCE = {"dc": "dc_tol", "ac": "ac_tol", "total": "tol"}
REL = 1e-9                         # the Q9 comparison's relative rounding margin


# ---------------------------------------------------------------------- limits

def tolerance_of(load: Load, check: str) -> Tuple[Optional[Pair], bool, str]:
    """``(pair, pct, key)`` of the load's tolerance judged by ``check``: the pair as entered,
    whether it is in % of nominal (else mV from it), and the key set ("" and None when none)."""
    base = CHECK_TOLERANCE[check]
    for key, pct in ((f"{base}_pct", True), (f"{base}_mv", False)):
        if getattr(load, key) is not None:
            return getattr(load, key), pct, key
    return None, True, ""


def allowed(nominal: Optional[float], pair: Optional[Pair], pct: bool) -> Limit:
    """``(below, above)`` volts a tolerance lets the load go from ``nominal``, KiPI's
    ``deviation_limits`` (kipi/tolerance/evaluate.py:28-36; a % of a negative rail is of its
    magnitude, ``pct_to_volts`` dc/pi/model.py:254-259; mV from nominal add as they are).
    ``(None, None)`` without a nominal or a tolerance, and for a % of 0 V, which places
    nothing (KiPI would allow 0 V either side)."""
    if pair is None or nominal is None or (pct and nominal == 0):
        return None, None
    lo, hi = pair if pct else (pair[0] / 1e3, pair[1] / 1e3)
    return deviation_limits(nominal, lo, hi, pct, not pct)


def load_limits(load: Load, nominal: Optional[float]) -> Dict[str, Limit]:
    """``{"dc" | "ac" | "total": (below, above) allowed}`` for the load's three tolerances
    (KiPI's ``load_limits``, evaluate.py:39-48)."""
    out = {}
    for check in CHECKS:
        pair, pct, _key = tolerance_of(load, check)
        out[check] = allowed(nominal, pair, pct)
    return out


def accuracy_sides(nominal: float, source: Source) -> Optional[Pair]:
    """How far below and above its set voltage the source's entered DC accuracy lets its
    output sit (V), KiPI's own ``Vrm.accuracy_sides`` (dc/pi/model.py:171-177); None when not
    entered (KiPI would count 0: a false pass), or in % of 0 V."""
    pct = source.accuracy_pct is not None
    pair = source.accuracy_pct if pct else source.accuracy_mv
    if pair is None or (pct and nominal == 0):
        return None
    lo, hi = pair if pct else (pair[0] / 1e3, pair[1] / 1e3)
    return Vrm(source.label, PinRef(source.ref, ""), float(nominal), acc_min=lo, acc_max=hi,
               acc_pct=pct, acc_dv=not pct).accuracy_sides()


# ---------------------------------------------------------------------- stack and judge

def stack(parts: Dict[str, Optional[Pair]], limits: Dict[str, Limit]) -> Dict[str, ToleranceCheck]:
    """The three checks from the parts (``None``: missing), a thin copy of KiPI's ``stack()``
    loop (evaluate.py:123-129) with KiPI's ``judge`` (:96-102, +1e-12 V): a check adds the
    parts it knows, each side apart, and is complete only when it knows them all, so it can
    fail before it is complete but pass only once it is."""
    out = {}
    for kind, names in CHECK_PARTS.items():
        known = [parts.get(n) for n in names if parts.get(n) is not None]
        below = sum(p[0] for p in known)
        above = sum(p[1] for p in known)
        complete = len(known) == len(names)
        lo, hi = limits.get(kind, (None, None))
        out[kind] = ToleranceCheck(kind, float(below), float(above), lo, hi, complete,
                                   judge(below, above, (lo, hi), complete),
                                   missing=[n for n in names if parts.get(n) is None])
    return out


def kipi_reference_stack(nominal: float, load: Load, source: Source,
                         parts: Dict[str, Optional[Pair]], bw: bool = False,
                         not_applicable=()) -> Dict[str, object]:
    """KiPI's own ``stack()`` on the same input, built from KiPI objects (its ``Load`` with the
    tolerances, its ``Vrm`` with the accuracy, a ``LoadEntry`` of ``Contribution``\\ s for the
    dc, ripple and transient ``parts``, None where not known): the drift test's reference, and
    the "before" of KiPI's false passes. ``{"parts": {kind: pair | None}, "checks": {kind:
    (below, above, allowed, complete, passed)}, "verdict": True | False | None}``."""
    tol = {}
    for check, prefix in (("total", "tol"), ("dc", "dc_tol"), ("ac", "ac_tol")):
        pair, pct, _key = tolerance_of(load, check)
        if pair is not None:
            lo, hi = pair if pct else (pair[0] / 1e3, pair[1] / 1e3)
            tol.update({f"{prefix}_min": lo, f"{prefix}_max": hi, f"{prefix}_pct": pct,
                        f"{prefix}_dv": not pct})
    kload = KipiLoad(load.label, PinRef(load.ref, "rail"), float(load.current), **tol)
    pct = source.accuracy_pct is not None
    acc = source.accuracy_pct if pct else source.accuracy_mv
    lo, hi = (None, None) if acc is None else (acc if pct else (acc[0] / 1e3, acc[1] / 1e3))
    vrm = Vrm(source.label, PinRef(source.ref, "rail"), float(nominal), acc_min=lo, acc_max=hi,
              acc_pct=pct, acc_dv=not pct)
    entry = LoadEntry(**{k: None if parts.get(k) is None else Contribution(*parts[k])
                         for k in ("dc", "ripple", "transient")})
    st = kipi_evaluate.stack(kload, entry, vrm, bw, not_applicable)
    return {"parts": dict(st.parts), "verdict": st.verdict,
            "checks": {k: (c.below_v, c.above_v, c.allowed, c.complete, c.passed)
                       for k, c in st.checks.items()}}


# ---------------------------------------------------------------------- loads and probes

def _pairing_probe(board: Board, p: Probe):
    """KiPI's probe for the pairing: its supply pins and, when given, its ground pins."""
    from kipi.ac.model import Probe as KipiProbe
    pads = _pads(board, [p.net], p.ref, p.pins, f"Probe {p.name} (supply pins)")
    ground = None
    if p.ground_net:
        gref = p.ground_ref or p.ref
        gpads = _pads(board, [p.ground_net], gref, p.ground_pins, f"Probe {p.name} (ground pins)")
        ground = PinRef(gref, p.ground_net, _numbers(gpads, p.ground_pins))
    return KipiProbe(p.name, PinRef(p.ref, p.net, _numbers(pads, p.pins)), ground)


def load_probes(board: Board, project: Project) -> Dict[str, Optional[str]]:
    """Each load's probe, by its engine name (``terminal_names``): KiPI's ``probe_for_load``
    (kipi/tolerance/gather.py:62-71), the probe on the same supply pins (and ground pins,
    where both have them), else the same part on the same supply net; None without one. A
    probe whose pins are not on the board is left out here (its solve names it)."""
    setup = to_pisetup(board, project)
    setup.probes = []
    for p in project.probes:
        try:
            setup.probes.append(_pairing_probe(board, p))
        except EngineError:
            continue
    return {ld.name: getattr(probe_for_load(setup, ld), "name", None) for ld in setup.loads}


def loads_by_name(project: Project) -> Dict[str, Tuple[str, Load]]:
    """Each load of the project by its engine name: ``(its rail's net, the load)``."""
    names = terminal_names(project)
    return {name: (net, project.rail(net).loads[i])
            for (what, net, i), name in names.items() if what == "load"}


def ac_tolerance_mismatch(load: Load, probe: Probe, nominal: Optional[float]) -> str:
    """The warning when the load's AC tolerance and its probe's ``ac_tolerance_v`` disagree
    (brief Q9: neither is derived from the other); "" when they agree or either is not set.
    T9 takes ``ac_tolerance_v`` as one deviation in volts (its target Z = V / I_step), so the
    load's is compared on its tighter side, in volts from nominal (:func:`allowed`), with a
    relative margin of 1e-9."""
    pair, pct, key = tolerance_of(load, "ac")
    if probe.ac_tolerance_v is None or pair is None:
        return ""
    sides = [x for x in allowed(nominal, pair, pct) if x is not None]
    if not sides:
        return ""
    tight, given = min(sides), float(probe.ac_tolerance_v)
    if abs(tight - given) <= REL * max(abs(tight), abs(given)):
        return ""
    entered = _pair_text(pair, "%" if pct else "mV")
    return (f"Load {load.label}'s AC tolerance ({key} {entered}: {tight * 1e3:.4g} mV on its tighter "
            f"side) and probe {probe.name}'s ac_tolerance_v ({given * 1e3:.4g} mV, which sets its "
            "impedance target) disagree: neither is derived from the other; enter the same value "
            "in both, or check which one is right")


def _pair_text(pair: Pair, unit: str) -> str:
    below, above = pair
    return f"±{above:g} {unit}" if below == -above else f"{below:g} / +{above:g} {unit}"


def mismatch_warnings(project: Project, pairs: Dict[str, Optional[str]],
                      nominal_of: Dict[str, Optional[float]], probes: Optional[List[str]] = None
                      ) -> List[str]:
    """:func:`ac_tolerance_mismatch` for every load paired with a probe (``pairs``: engine
    load name -> probe name; ``nominal_of``: engine load name -> its nominal), only the probes
    in ``probes`` when given."""
    by_name = {p.name: p for p in project.probes}
    out = []
    for name, (_net, load) in loads_by_name(project).items():
        probe = by_name.get(pairs.get(name) or "")
        if probe is None or (probes is not None and probe.name not in probes):
            continue
        text = ac_tolerance_mismatch(load, probe, nominal_of.get(name))
        if text:
            out.append(text)
    return out
