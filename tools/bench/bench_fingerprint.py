"""Fingerprints of a step's result: an exact hash and the key scalars at full precision.

The hash (SHA-256) covers the key result arrays (name, dtype, shape and bytes) and the
scalars' float64 bits: equal hashes mean bit-identical results. Across machines Triangle's
mesh moves with the input's last bits, so the hash alone gives false alarms there: the
scalars (max drop, worst load voltages, losses, via currents, temperatures, |Z| at the
probes, ripple peaks, counts) say how far apart two results really are.

The extractors read Coppernicus' result objects (``coppernicus.results``) and estimate
dicts; names carry units as the product's own fields do.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

import numpy as np


class Print:
    """Arrays and scalars gathered for one fingerprint."""

    def __init__(self):
        self._arrays = []
        self._scalars: Dict[str, float] = {}

    def array(self, name: str, a) -> None:
        if a is None:
            return
        self._arrays.append((name, np.ascontiguousarray(np.asarray(a))))

    def scalar(self, name: str, value) -> None:
        """A number at full precision (bools and ints as floats); None is left out."""
        if value is None:
            return
        self._scalars[name] = float(value)

    def done(self) -> Dict[str, Any]:
        h = hashlib.sha256()
        for name, a in self._arrays:
            h.update(f"{name}|{a.dtype.str}|{a.shape}|".encode())
            h.update(memoryview(a))       # C-contiguous: hashed in place, no copy
        keys = sorted(self._scalars)
        h.update("|".join(keys).encode())
        h.update(np.array([self._scalars[k] for k in keys], dtype=np.float64).tobytes())
        return {"hash": h.hexdigest(), "scalars": {k: self._scalars[k] for k in keys},
                "arrays": len(self._arrays)}


# ----------------------------------------------------------------------------------- results

def add_rail(fp: Print, res, prefix: str = "") -> None:
    """A DC rail (``RailResult``): its fields per layer, terminals, vias, losses, counts."""
    for lf in sorted(res.layers, key=lambda f: f.layer_id):
        tag = f"{prefix}layer{lf.layer_id}"
        fp.array(f"{tag}.voltage", lf.voltage)
        fp.array(f"{tag}.current_density", lf.current_density)
        fp.array(f"{tag}.tri_power", lf.tri_power)
    fp.array(f"{prefix}via_current_a", np.array([v.current for v in res.vias], dtype=float))
    fp.scalar(f"{prefix}max_drop_v", res.max_drop)
    fp.scalar(f"{prefix}j_max_a_mm2", res.j_max)
    fp.scalar(f"{prefix}power_loss_w", res.power_loss)
    fp.scalar(f"{prefix}loss_vias_w", res.loss_vias)
    fp.scalar(f"{prefix}loss_series_w", res.loss_series)
    fp.scalar(f"{prefix}v_ref_v", res.v_ref)
    for t in [*res.sources, *res.loads]:
        tag = f"{prefix}{t.kind}[{t.name}]"
        fp.scalar(f"{tag}.current_a", t.current)
        fp.scalar(f"{tag}.v_min_v", t.v_min)
        fp.scalar(f"{tag}.v_max_v", t.v_max)
    currents = [abs(v.current) for v in res.vias]
    fp.scalar(f"{prefix}via_count", len(res.vias))
    fp.scalar(f"{prefix}via_current_max_a", max(currents) if currents else 0.0)
    fp.scalar(f"{prefix}via_current_sum_a", float(sum(currents)))
    fp.scalar(f"{prefix}triangles", res.stats.get("triangles"))
    fp.scalar(f"{prefix}unknowns", res.stats.get("unknowns"))


def rail(res) -> Dict[str, Any]:
    fp = Print()
    add_rail(fp, res)
    return fp.done()


def tree(t) -> Dict[str, Any]:
    """A power tree (``TreeResult``), with its board temperature when it has one."""
    fp = Print()
    for key in sorted(t.groups):
        add_rail(fp, t.groups[key], prefix=f"group[{key}].")
    for v in t.vrms:
        tag = f"vrm[{v.name}]"
        for field in ("v_out_v", "i_out_a", "v_in_v", "i_in_a", "p_loss_w", "v_in_worst_v"):
            fp.scalar(f"{tag}.{field}", getattr(v, field))
    for ld in t.loads:
        tag = f"load[{ld.name}]"
        for field in ("current_a", "v_worst_v", "drop_v", "v_supply_min_v"):
            fp.scalar(f"{tag}.{field}", getattr(ld, field))
    for link in t.links:
        current = link.get("current_a", link.get("current"))
        if isinstance(current, (int, float)):
            fp.scalar(f"link[{link.get('name')}].current_a", current)
    fp.scalar("converged", t.converged)
    th = t.thermal
    if th is not None:
        for m in sorted(th.maps, key=lambda m: m.layer_id):
            fp.array(f"temperature.layer{m.layer_id}", m.t_c)
        fp.scalar("thermal.peak_c", th.peak_c)
        fp.scalar("thermal.heat_w", th.heat_w)
        fp.scalar("thermal.iterations", th.iterations)
        fp.scalar("thermal.grid_cells", th.grid_cells)
        for c in th.copper:
            fp.scalar(f"thermal.t_max_c[{c.layer_name}]", c.t_max_c)
    return fp.done()


def impedance(res) -> Dict[str, Any]:
    """An AC solve (``ImpedanceResult``): |Z|(f) at every probe."""
    fp = Print()
    fp.array("freq_hz", res.freq_hz)
    fp.array("field_freq_hz", res.field_freq_hz)
    fp.scalar("plane_capacitance_f", res.plane_capacitance_f)
    for p in res.probes:
        tag = f"probe[{p.name}]"
        z = np.asarray(p.z_ohm)
        fp.array(f"{tag}.z_ohm", z)
        mag = np.abs(z)
        if len(mag):
            fp.scalar(f"{tag}.z_first_ohm", mag[0])
            fp.scalar(f"{tag}.z_last_ohm", mag[-1])
            fp.scalar(f"{tag}.z_min_ohm", np.nanmin(mag))
            fp.scalar(f"{tag}.z_max_ohm", np.nanmax(mag))
        fp.scalar(f"{tag}.worst_z_ohm", p.worst_z_ohm)
        fp.scalar(f"{tag}.worst_hz", p.worst_hz)
        tr = p.transient
        if tr is not None:
            fp.scalar(f"{tag}.undershoot_v", tr.undershoot_v)
            fp.scalar(f"{tag}.overshoot_v", tr.overshoot_v)
    for key in ("triangles", "unknowns", "ports", "field_frequencies", "sweep_points"):
        fp.scalar(key, res.stats.get(key))
    return fp.done()


def noise(res) -> Dict[str, Any]:
    """A noise solve (``NoiseResult``): waveforms, ripple and victims' peaks."""
    fp = Print()
    fp.array("t_s", res.t_s)
    for i, v in enumerate(res.switch_node_v):
        fp.array(f"switch_node_v[{i}]", v)
    for r in res.ripple:
        tag = f"ripple[{r.name}]"
        fp.array(f"{tag}.v", r.v)
        for field in ("pp_v", "pp_bw_v", "rms_v", "below_v", "above_v"):
            fp.scalar(f"{tag}.{field}", getattr(r, field))
    for v in res.victims:
        fp.array(f"victim[{v.name}].receiver_v", v.receiver_v)
        fp.scalar(f"victim[{v.name}].peak_v", v.peak_v)
    for i, h in enumerate(res.hot_loops):
        fp.scalar(f"hot_loop[{i}].loop_l_h", h.loop_l_h)
        fp.scalar(f"hot_loop[{i}].f_ring_hz", h.f_ring_hz)
    if res.emi is not None:
        fp.scalar("emi.radiated_dbuv_m", res.emi.radiated_dbuv_m)
    fp.scalar("harmonics", res.harmonics)
    fp.scalar("iin_a", res.iin_a)
    return fp.done()


def tolerance(result) -> Dict[str, Any]:
    """A tolerance stack-up (``ToleranceResult``): each load's parts and checks."""
    fp = Print()
    for row in result.rows:
        tag = f"row[{row.name}]"
        for kind in sorted(row.checks):
            c = row.checks[kind]
            fp.scalar(f"{tag}.{kind}.below_v", c.below_v)
            fp.scalar(f"{tag}.{kind}.above_v", c.above_v)
            fp.scalar(f"{tag}.{kind}.complete", c.complete)
        for part in sorted(row.parts):
            p = row.parts[part]
            if p is not None:
                fp.scalar(f"{tag}.part[{part}].below_v", p.below_v)
                fp.scalar(f"{tag}.part[{part}].above_v", p.above_v)
    fp.scalar("rows", len(result.rows))
    return fp.done()


ESTIMATE_KEYS = ("triangles", "nodes", "ports", "field_frequencies", "sweep_points", "unknowns",
                 "memory_bytes", "harmonics", "field_solves", "f_max_hz", "grid_nx", "grid_ny",
                 "cells", "layers", "cell_mm")


def estimate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """A cost estimate (``ac_estimate``, ``noise_estimate``, ``thermal_estimate``): its counts
    (not the machine's memory or the text)."""
    fp = Print()
    for key in ESTIMATE_KEYS:
        if isinstance(doc.get(key), (int, float)):
            fp.scalar(key, doc[key])
    return fp.done()


def board(b) -> Dict[str, Any]:
    """A board as read (``coppernicus.engine.Board``): its items counted, pad positions hashed."""
    snap = b.snapshot
    fp = Print()
    fp.array("pad_xy_mm", np.array([p.position for p in snap.pads], dtype=float).reshape(-1, 2))
    fp.array("via_xy_mm", np.array([v.position for v in snap.vias], dtype=float).reshape(-1, 2))
    vertices = sum(len(poly.outline) + sum(len(h) for h in poly.holes)
                   for z in snap.zones for polys in z.filled.values() for poly in polys)
    for key, n in (("nets", len(snap.nets)), ("pads", len(snap.pads)), ("vias", len(snap.vias)),
                   ("tracks", len(snap.tracks)), ("zones", len(snap.zones)),
                   ("zone_vertices", vertices), ("copper_layers", len(snap.stackup.copper))):
        fp.scalar(key, n)
    return fp.done()


def mesh(m) -> Dict[str, Any]:
    """A KiPI mesh (``NetMesh``): its nodes and triangles, exactly."""
    fp = Print()
    fp.array("points", m.points)
    fp.array("tris", m.tris)
    fp.array("tri_layer", m.tri_layer)
    fp.scalar("nodes", m.n_nodes)
    fp.scalar("triangles", m.n_tris)
    fp.scalar("barrels", len(m.barrels))
    for lid in m.layers:
        fp.scalar(f"layer{lid}.triangles", int((m.tri_layer == lid).sum()))
    return fp.done()
