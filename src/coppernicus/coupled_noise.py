"""DC-DC noise results: verdicts, the summary document, text, CSV and PNG (no engine, no Qt).

A victim is judged only against a margin the user gave (``noise_margin_v``, a named preset's
own margin, or the JESD8 levels of an entered ``logic_v``), and only while no KiPI stand-in
is in play (Coss, Qrr, a capacitor without a model, assumed εr, guessed ends, a coupling
mechanism left out, harmonics cut short, a line already past its threshold). Ripple and the
radiated estimate are never judged here. Rules: ``docs/engine-mapping.md`` (Task 10,
Verdicts).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from coppernicus.results import NoiseResult, VictimResult

# ---------------------------------------------------------------------- verdicts

def victim_verdict(v: VictimResult) -> Tuple[Optional[bool], str]:
    """(passed, why not judged): ``(None, "")`` without a margin (ranked); ``(None, reason)``
    while a stand-in is in play, whatever the peak; else ``peak_v <= margin_v`` (KiPI's rule,
    kipi/noise/result.py:76-82)."""
    if v.margin_v is None:
        return None, ""
    if v.stand_ins:
        return None, (f"{'; '.join(v.stand_ins)}; peak {v.peak_v * 1e3:.4g} mV = "
                      f"{100.0 * v.peak_v / v.margin_v:.3g} % of the margin")
    return v.peak_v <= v.margin_v, ""


def _fail_reason(v: VictimResult) -> str:
    return (f"peak {v.peak_v * 1e3:.4g} mV at {v.receiver or 'the receiver'}, above its margin "
            f"{v.margin_v * 1e3:.4g} mV ({100.0 * v.peak_v / v.margin_v:.3g} %; mostly {v.dominant})")


def entries(r: NoiseResult) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(failed, not judged) entries of one aggressor: ``{item, net, limit, reason}``."""
    failed = [{"item": v.name, "net": v.net, "limit": v.limit, "reason": _fail_reason(v)}
              for v in r.victims if v.passed is False]
    not_judged = [{"item": v.name, "net": v.net, "limit": v.limit, "reason": v.not_judged}
                  for v in r.victims if v.margin_v is not None and v.passed is None]
    return failed, not_judged


def _combine(items: Sequence[Tuple[str, bool]]) -> str:
    """``fail`` if one fails; else ``not_judged`` if one with a margin was not judged; else
    ``pass`` if one passed; else ``not_judged`` (nothing to judge)."""
    if any(v == "fail" for v, _ in items):
        return "fail"
    if any(nj for _, nj in items):
        return "not_judged"
    return "pass" if any(v == "pass" for v, _ in items) else "not_judged"


def _note(verdict: str, failed, not_judged) -> str:
    def text(e):
        return f"{e['item']} on {e['net']} ({e['limit']}): {e['reason']}"
    if verdict == "fail":
        return "Fail: " + "; ".join(text(e) for e in failed)
    if not_judged:
        return "Not judged: " + "; ".join(text(e) for e in not_judged)
    if verdict == "pass":
        return "Pass: every victim with a noise margin stays within it"
    return "No noise margins set: nothing is judged; the victims are ranked"


def judge(r: NoiseResult) -> NoiseResult:
    """Fill in each victim's verdict and the aggressor's, in place. A partial result (handed
    over during the solve) gets none."""
    if r.partial:
        return r
    for v in r.victims:
        v.passed, v.not_judged = victim_verdict(v)
    r.failed, r.not_judged = entries(r)
    r.verdict = _combine([("fail" if r.failed else "pass" if any(v.passed for v in r.victims) else "",
                           bool(r.not_judged))])
    r.verdict_note = _note(r.verdict, r.failed, r.not_judged)
    return r


def noise_verdict(results: Sequence[NoiseResult]) -> Tuple[str, str, List[Dict[str, Any]],
                                                           List[Dict[str, Any]]]:
    """(verdict, note, failed, not judged) over several aggressors, combined as one
    aggressor's victims are; each entry gains its ``aggressor``."""
    failed = [{**e, "aggressor": r.aggressor} for r in results for e in r.failed]
    not_judged = [{**e, "aggressor": r.aggressor} for r in results for e in r.not_judged]
    verdict = _combine([(r.verdict if r.verdict != "not_judged" else "", bool(r.not_judged))
                        for r in results])
    return verdict, _note(verdict, failed, not_judged), failed, not_judged


def _num(x) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None
