"""DC-DC noise results: verdicts, the summary document, text, CSV and PNG (no engine, no Qt).

A victim is judged only against a margin the user gave (``noise_margin_v``, a named preset's
own margin, or the JESD8 levels of an entered ``logic_v``), and only while no KiPI stand-in
is in play (Coss, Qrr, a capacitor without a model, assumed εr, guessed ends, a coupling
mechanism left out, harmonics cut short, a line already past its threshold). Ripple and the
radiated estimate are never judged here. Rules: ``docs/engine-mapping.md`` (Task 10,
Verdicts).
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

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


# ---------------------------------------------------------------------- summary and text

def _mv(x) -> Optional[float]:
    x = _num(x)
    return None if x is None else x * 1e3


def _hot_loop(h) -> Dict[str, Any]:
    return {"phase": h.phase, "loop_l_h": _num(h.loop_l_h), "package_l_h": _num(h.package_l_h),
            "c_sw_f": _num(h.c_sw_f), "coss_f": _num(h.coss_f), "coss_entered": h.coss_entered,
            "qrr_nc": _num(h.qrr_nc), "qrr_entered": h.qrr_entered, "c_copper_f": _num(h.c_copper_f),
            "r_loop_ohm": _num(h.r_loop_ohm), "f_ring_hz": _num(h.f_ring_hz), "zeta": _num(h.zeta),
            "zeta_loop": _num(h.zeta_loop), "traced": h.traced, "image_plane": h.image_plane}


def _ripple(rp) -> Dict[str, Any]:
    return {"name": rp.name, "where": rp.where, "net": rp.net, "ground_net": rp.ground_net,
            "ref": rp.ref, "pins": None if rp.pins is None else list(rp.pins),
            "pp_mv": _mv(rp.pp_v), "pp_20mhz_mv": _mv(rp.pp_bw_v), "below_mv": _mv(rp.below_v),
            "above_mv": _mv(rp.above_v), "below_20mhz_mv": _mv(rp.below_bw_v),
            "above_20mhz_mv": _mv(rp.above_bw_v), "rms_mv": _mv(rp.rms_v)}


def _victim(v: VictimResult) -> Dict[str, Any]:
    states = [{"name": s["name"], "level_v": _num(s["level_v"]), "threshold_v": _num(s["threshold_v"]),
               "driver_ohm": _num(s["driver_ohm"]), "excursion_mv": _mv(s["excursion_v"]),
               "margin_mv": _mv(s["margin_v"])} for s in v.states]
    return {"name": v.name, "net": v.net, "driver": v.driver, "receiver": v.receiver,
            "ends_entered": v.ends_entered, "preset": v.preset, "margin_mv": _mv(v.margin_v),
            "margin_from": v.margin_from, "limit": v.limit, "peak_mv": _mv(v.peak_v),
            "peak_by_mv": {k: _mv(x) for k, x in v.peak_by_v.items()}, "dominant": v.dominant,
            "ratio": _num(v.ratio), "states": states, "length_mm": _num(v.length_mm),
            "coupled_mm": _num(v.coupled_mm), "cm_f": {k: _num(x) for k, x in v.cm_f.items()},
            "m_h": {k: _num(x) for k, x in v.m_h.items()}, "splits": list(v.splits),
            "stand_ins": list(v.stand_ins), "passed": v.passed, "not_judged": v.not_judged,
            "warnings": list(v.warnings)}


def _emi(e) -> Optional[Dict[str, Any]]:
    """The near field without its maps (they are the PNGs)."""
    if e is None:
        return None
    return {"height_mm": _num(e.height_mm), "distance_m": _num(e.distance_m),
            "hotspots": [{**s, "x_mm": _num(s["x_mm"]), "y_mm": _num(s["y_mm"]),
                          "value": _num(s["value"]), "f_hz": _num(s["f_hz"])} for s in e.hotspots],
            "radiated_dbuv_m": _num(e.radiated_dbuv_m), "radiated_hz": _num(e.radiated_hz),
            "cispr32_b_dbuv_m": _num(e.cispr32_b_dbuv_m), "radiated_source": e.radiated_source,
            "notes": list(e.notes)}


STAT_KEYS = ("seconds", "harmonics", "triangles", "nodes", "unknowns", "ports", "field_solves",
             "field_seconds", "memory_peak_mb")


def aggressor_summary(r: NoiseResult, csv_file: str = "",
                      png_files: Sequence[str] = ()) -> Dict[str, Any]:
    """One aggressor of the ``noise`` document (units in every key; no NaN)."""
    b = r.board
    return {
        "aggressor": r.aggressor, "vrm": r.vrm, "rail": r.rail, "input_net": r.input_net,
        "ground": r.ground, "topology": r.topology, "vin_v": _num(r.vin_v), "vout_v": _num(r.vout_v),
        "iout_a": _num(r.iout_a), "iin_a": _num(r.iin_a), "duty": _num(r.duty),
        "efficiency": _num(r.efficiency), "phases": r.phases, "operating_notes": list(r.operating_notes),
        "fsw_hz": _num(r.fsw_hz), "rise_s": _num(r.rise_s), "fall_s": _num(r.fall_s),
        "f_max_hz": _num(r.f_max_hz), "harmonics": r.harmonics, "harmonics_capped": r.harmonics_capped,
        "inductor_ripple_a": [_num(x) for x in r.inductor_ripple_a],
        "hot_loops": [_hot_loop(h) for h in r.hot_loops], "ripple": [_ripple(x) for x in r.ripple],
        "victims": [_victim(v) for v in r.victims],
        "board": {"used": bool(b.get("used")), "note": b.get("note", ""),
                  "unknowns": _int(b.get("unknowns")), "field_solves": _int(b.get("field_solves")),
                  "ports": _int(b.get("ports")), "caps_modelled": list(b.get("caps_modelled", [])),
                  "caps_missing": list(b.get("caps_missing", []))},
        "emi": _emi(r.emi), "stand_ins": list(r.stand_ins), "verdict": r.verdict,
        "verdict_note": r.verdict_note, "failed": list(r.failed), "not_judged": list(r.not_judged),
        "csv_file": csv_file, "png_files": list(png_files), "assumptions": list(r.assumptions),
        "warnings": list(r.warnings),
        "stats": {**{k: _num(r.stats.get(k)) for k in STAT_KEYS},
                  "memory_peak_raised": r.stats.get("memory_peak_raised")},
    }


def _int(x) -> Optional[int]:
    x = _num(x)
    return None if x is None else int(x)


def noise_summary(results: Sequence[NoiseResult],
                  files: Optional[Dict[str, Tuple[str, Sequence[str]]]] = None,
                  warnings: Sequence[str] = ()) -> Dict[str, Any]:
    """The ``<project>_noise_summary.json`` document: ``kind`` "noise", ``verdict``,
    ``verdict_note``, ``failed`` and ``not_judged`` (each entry with its ``aggressor``),
    ``warnings`` (project level), then ``aggressors``. ``files``: aggressor -> (CSV path, PNG
    paths)."""
    files = files or {}
    verdict, note, failed, not_judged = noise_verdict(results)
    return {"kind": "noise", "verdict": verdict, "verdict_note": note, "failed": failed,
            "not_judged": not_judged, "warnings": list(warnings),
            "aggressors": [aggressor_summary(r, *files.get(r.aggressor, ("", ()))) for r in results]}


def _hz(f) -> str:
    if f is None:
        return "-"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale * 0.9995:
            return f"{f / scale:.4g} {unit}"
    return f"{f:.4g} Hz"


def _mv_text(x) -> str:
    return "-" if x is None else f"{x:.4g} mV"


def noise_text(doc: Dict[str, Any]) -> List[str]:
    """The human-readable report of a ``noise`` document (the 20 MHz ripple first)."""
    lines: List[str] = []
    for a in doc["aggressors"]:
        st = a["stats"]
        lines.append(f"\nAggressor {a['aggressor']} (source {a['vrm']}, {a['topology']}): "
                     f"{a['vin_v']:g} V → {a['vout_v']:g} V, {a['iout_a']:.4g} A, duty {a['duty']:.4g}, "
                     f"{_hz(a['fsw_hz'])}, edges {a['rise_s'] * 1e9:g} / {a['fall_s'] * 1e9:g} ns")
        board = a["board"]
        model = (f"board model {board['unknowns']:,} unknowns, {board['field_solves']} field solves"
                 if board["used"] and board["unknowns"] else "no board model")
        took = f"solved in {st['seconds']:.1f} s: " if st.get("seconds") is not None else ""
        lines.append(f"  {took}{a['harmonics']} harmonics to {_hz(a['f_max_hz'])}; {model}")
        for h in a["hot_loops"]:
            ring = (f"rings at {_hz(h['f_ring_hz'])}, ζ {h['zeta']:.3g}" if h["f_ring_hz"]
                    else "no ringing")
            lines.append(f"  phase {h['phase']}: hot loop {h['loop_l_h'] * 1e9:.3g} nH, switch node "
                         f"{h['c_sw_f'] * 1e12:.4g} pF; {ring}")
        for rp in a["ripple"]:
            lines.append(f"  ripple {rp['name']} ({rp['where']}, {rp['net']} − {rp['ground_net']}): "
                         f"{_mv_text(rp['pp_20mhz_mv'])} p-p at 20 MHz ({_mv_text(rp['below_20mhz_mv'])}"
                         f" below, {_mv_text(rp['above_20mhz_mv'])} above), full band "
                         f"{_mv_text(rp['pp_mv'])} p-p; not judged")
        for v in a["victims"]:
            margin = (f", margin {_mv_text(v['margin_mv'])} ({100 * v['ratio']:.3g} %)"
                      if v["margin_mv"] is not None and v["ratio"] is not None else ", no margin set")
            tag = {True: "  PASS", False: "  FAIL"}.get(v["passed"], "")
            lines.append(f"  victim {v['name']} on {v['net']}: peak {_mv_text(v['peak_mv'])} "
                         f"(mostly {v['dominant'] or '-'}){margin}{tag}")
        for u in a["failed"]:
            lines.append(f"  FAIL {u['item']} ({u['limit']}): {u['reason']}")
        for u in a["not_judged"]:
            lines.append(f"  NOT JUDGED {u['item']} ({u['limit']}): {u['reason']}")
        e = a["emi"]
        if e is not None and e["radiated_dbuv_m"] is not None:
            limit = (f", CISPR 32 class B {e['cispr32_b_dbuv_m']:.0f} dBµV/m"
                     if e["cispr32_b_dbuv_m"] is not None else ", below the 30 MHz band")
            lines.append(f"  radiated estimate at {e['distance_m']:g} m: {e['radiated_dbuv_m']:.1f} dBµV/m "
                         f"at {_hz(e['radiated_hz'])}{limit} (for scale, not judged)")
        lines += [f"  assumption: {x}" for x in a["assumptions"]]
        lines += [f"  warning: {x}" for x in a["warnings"]]
        if a["csv_file"]:
            lines.append(f"    csv: {a['csv_file']}")
        lines += [f"    png: {p}" for p in a["png_files"]]
    lines += [f"warning: {w}" for w in doc.get("warnings", [])]
    lines.append(f"Verdict: {doc['verdict']}. {doc['verdict_note']}")
    return lines


# ---------------------------------------------------------------------- CSV and PNG

def _cell(x) -> str:
    return "" if x is None or not math.isfinite(x) else f"{x:.9g}"


def write_csv(r: NoiseResult, path) -> Path:
    """One switching period: ``t_s``, per phase ``phase<i>_switch_node_v`` and
    ``phase<i>_inductor_a``, per ripple ``ripple_<name>_v`` and ``ripple_<name>_20mhz_v``
    (about the mean), per victim ``victim_<name>_v`` (at its receiver, driven high)."""
    path = Path(path)
    columns: List[Tuple[str, Any]] = [("t_s", r.t_s)]
    for i, (sw, il) in enumerate(zip(r.switch_node_v, r.inductor_a), 1):
        columns += [(f"phase{i}_switch_node_v", sw), (f"phase{i}_inductor_a", il)]
    for rp in r.ripple:
        columns += [(f"ripple_{rp.name}_v", rp.v), (f"ripple_{rp.name}_20mhz_v", rp.v_bw)]
    columns += [(f"victim_{v.name}_v", v.receiver_v) for v in r.victims]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([name for name, _ in columns])
        for k in range(len(r.t_s)):
            w.writerow([_cell(float(col[k])) for _, col in columns])
    return path


COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#7f7f7f")
PERIODS_SHOWN = 2                   # KiPI's periods_shown


def _two(r: NoiseResult, y):
    t = np.concatenate([r.t_s + k / r.fsw_hz for k in range(PERIODS_SHOWN)])
    return t * 1e6, np.tile(y, PERIODS_SHOWN)


def shown_ripple(r: NoiseResult) -> List[Any]:
    """The ripple the waveform plot shows: the probes', else the output at the device pins."""
    probes = [rp for rp in r.ripple if rp.where == "probe"]
    return probes or [rp for rp in r.ripple if rp.name == "output"]


def draw_waveforms(fig, r: NoiseResult) -> None:
    """Two periods of the switch node and inductor current per phase, the ripple (20 MHz
    limit solid, full band thin) and each victim's receiver voltage, with its margin."""
    axes = fig.subplots(4, 1, sharex=True)
    for i, (sw, il) in enumerate(zip(r.switch_node_v, r.inductor_a)):
        c = COLORS[i % len(COLORS)]
        axes[0].plot(*_two(r, sw), color=c, lw=1.0, label=f"phase {i + 1}")
        axes[1].plot(*_two(r, il), color=c, lw=1.0, label=f"phase {i + 1}")
    axes[0].set_ylabel("switch node (V)")
    axes[1].set_ylabel("inductor (A)")
    for k, rp in enumerate(shown_ripple(r)):
        c = COLORS[k % len(COLORS)]
        axes[2].plot(*_two(r, rp.v * 1e3), color=c, lw=0.5, alpha=0.5)
        axes[2].plot(*_two(r, rp.v_bw * 1e3), color=c, lw=1.2, label=f"{rp.name} (20 MHz limit)")
    axes[2].set_ylabel("ripple (mV)")
    for k, v in enumerate(r.victims):
        c = COLORS[k % len(COLORS)]
        axes[3].plot(*_two(r, v.receiver_v * 1e3), color=c, lw=1.0, label=v.name)
        if v.margin_v is not None:
            for sign in (1, -1):
                axes[3].axhline(sign * v.margin_v * 1e3, color=c, lw=0.8, ls="--")
    axes[3].set_ylabel("victims (mV)")
    axes[3].set_xlabel("time (µs)")
    for ax in axes:
        ax.grid(True, lw=0.3, color="0.8")
        if ax.get_legend_handles_labels()[0]:
            ax.legend(fontsize=7, loc="upper right")
    words = {"fail": "FAIL", "pass": "PASS", "not_judged": "not judged", "": "partial"}[r.verdict]
    axes[0].set_title(f"{r.aggressor}: {r.vin_v:g} V → {r.vout_v:g} V at {_hz(r.fsw_hz)}, victims "
                      f"{words}; ripple never judged", fontsize=10)


def write_png(r: NoiseResult, path, dpi: int = 110) -> Path:
    """The waveforms of :func:`draw_waveforms` as a PNG (headless)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    path = Path(path)
    fig = Figure(figsize=(8.0, 9.0), dpi=dpi)
    FigureCanvasAgg(fig)
    draw_waveforms(fig, r)
    fig.tight_layout()
    fig.savefig(path)
    return path


def map_xy(m, i: int, j: int) -> Tuple[float, float]:
    """KiCad (x, y) mm of a field map's cell in row ``i``, column ``j`` (rows from y0)."""
    ny, nx = m.values.shape
    x0, y0, x1, y1 = m.bounds
    return (x0 + (x1 - x0) * j / max(nx - 1, 1), y0 + (y1 - y0) * i / max(ny - 1, 1))


def draw_field_map(ax, r: NoiseResult, kind: str, outline: Sequence[Any] = ()) -> Any:
    """The near field's strongest harmonic per point (``kind`` "H" or "E") on a log scale over
    the board outline (KiCad frame, y down), with its hotspots; returns the image."""
    from matplotlib.colors import LogNorm
    m = getattr(r.emi, "h_map" if kind == "H" else "e_map") if r.emi is not None else None
    if m is None:
        ax.text(0.5, 0.5, f"no {kind} field", ha="center", va="center", transform=ax.transAxes)
        return None
    ny, nx = m.values.shape
    x0, y0, x1, y1 = m.bounds
    dx, dy = (x1 - x0) / max(nx - 1, 1), (y1 - y0) / max(ny - 1, 1)
    vals = np.where(m.values > 0, m.values, np.nan)
    top = float(np.nanmax(vals)) if np.isfinite(vals).any() else 1.0
    img = ax.imshow(vals, origin="upper", extent=(x0 - dx / 2, x1 + dx / 2, y1 + dy / 2, y0 - dy / 2),
                    norm=LogNorm(vmin=top * 1e-3, vmax=top), cmap="inferno", interpolation="nearest")
    for ring in outline:
        pts = np.asarray(ring, float)
        ax.plot(np.append(pts[:, 0], pts[0, 0]), np.append(pts[:, 1], pts[0, 1]), color="0.6", lw=0.8)
    spots = [s for s in r.emi.hotspots if s["kind"] == kind]
    for k, s in enumerate(spots):
        ax.plot([s["x_mm"]], [s["y_mm"]], marker="o", ms=7, mfc="none", mec="#00e5ff", mew=1.4)
        ax.annotate(str(k + 1), (s["x_mm"], s["y_mm"]), color="#00e5ff", fontsize=7,
                    xytext=(4, 4), textcoords="offset points")
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"{r.aggressor}: {kind} field {r.emi.height_mm:g} mm above the top copper, strongest "
                 "harmonic (indicator, never judged)", fontsize=9)
    return img


def write_field_png(r: NoiseResult, kind: str, path, outline: Sequence[Any] = (),
                    dpi: int = 110) -> Path:
    """The field map of :func:`draw_field_map` as a PNG with its colour bar (headless)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    path = Path(path)
    fig = Figure(figsize=(8.0, 6.0), dpi=dpi)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1)
    img = draw_field_map(ax, r, kind, outline)
    if img is not None:
        unit = (r.emi.h_map if kind == "H" else r.emi.e_map).unit
        fig.colorbar(img, ax=ax, label=f"{kind} ({unit}, peak of the strongest harmonic)")
    fig.tight_layout()
    fig.savefig(path)
    return path
