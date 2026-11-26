"""A faster stand-in for the KiCad reader's two "remove unused layers" trims, bit-identical to
KiPI's: on by default, off with ``COPPERNICUS_FAST_TRIM=0``.

KiPI's reader (``kipi/sources/kicad_pcb/reader.py``) drops the pad of a via (``trim_unused_via_pads``,
:334) or the copper of a through-hole pad (``trim_unused_layers``, :614) that is set to remove
unused layers, on each layer where nothing of its net meets it. To decide, each function first
converts every pad shape and every zone fill of every net, on every layer, to shapely, and
repairs each invalid one (``poly_to_shapely``: ``make_valid`` of KiCad's "fractured" fills, whose
holes are joined to the outline by cuts). On a board with many vias set to remove unused layers
and large zone fills, that conversion, nearly all of it ``make_valid`` on the zone fills, is most
of the trim's time; the tests themselves (a disc, track ends, ``intersects``) are cheap.

The stand-in runs the same tests, on the same geometries, in the same order, and changes only
what is converted and when:

* a geometry is converted (with KiPI's own ``poly_to_shapely``) only when a trimmed item of its
  net and layer gets to it, and then once;
* before converting a pad shape or zone fill, its coordinates' bounding box (padded by 1 nm) is
  compared with the bounding box of what it is tested against: a geometry whose box is apart
  cannot intersect it (a converted and repaired polygon lies within its coordinates' box), so
  its test is False without converting it.

``any()`` stops at the first geometry that meets the item, in either version; every test is
the same exact shapely predicate on the same geometry, so every decision, and so the board,
is the same to the last bit (tests/test_fast_trim.py: the snapshot's pickled bytes and a deep
comparison, on KiPI's reader test boards, the repository's examples and boards with vias set
to remove unused layers all over their zone fills).

The seam: the two functions of ``kipi.sources.kicad_pcb.reader`` are replaced, once, only on
the KiPI they were written for (:data:`KIPI_VERSION`) with those functions of the expected
signatures; otherwise a warning (once) and KiPI's own run. :func:`uninstall` puts KiPI's back.
"""

from __future__ import annotations

import inspect
import os
import threading
import warnings
from collections import defaultdict
from typing import Callable, Dict, List, Mapping, Optional, Tuple

import kipi
import kipi.sources.kicad_pcb.reader as reader
from kipi.core.geometry.copper import poly_to_shapely

ENV = "COPPERNICUS_FAST_TRIM"
KIPI_VERSION = "1.0.1"
# The padding of a polygon's coordinate box, mm (1 nm): wider than any rounding of the points a
# repair (make_valid) can add, which lie on the polygon's own edges.
BOX_PAD_MM = 1e-6
# KiPI's functions replaced, with the parameters they take.
_SEAMS = {"trim_unused_via_pads": ["trim", "tracks", "pads", "zones", "outer", "vias"],
          "trim_unused_layers": ["trim", "tracks", "vias", "zones", "outer"]}
#: KiPI's own functions, as the reader module had them when this module was imported.
KIPI = {name: getattr(reader, name) for name in _SEAMS}

_lock = threading.Lock()
_warned = False


# ---------------------------------------------------------------------- copper, converted lazily

class _Lazy:
    """The pad shapes and zone fills of each (net, layer), in KiPI's order (pads first, then
    zones), each converted with KiPI's ``poly_to_shapely`` only when a test reaches it."""

    def __init__(self):
        self._items: Dict[tuple, List[list]] = defaultdict(list)    # key -> [poly, box, geom, done]

    def add(self, key: tuple, polys) -> None:
        items = self._items[key]
        for p in polys:
            items.append([p, None, None, False])

    @staticmethod
    def _box(p) -> Optional[Tuple[float, float, float, float]]:
        pts = list(p.outline) + [q for h in p.holes for q in h]
        if not pts:
            return None
        xs = [q[0] for q in pts]
        ys = [q[1] for q in pts]
        return (min(xs) - BOX_PAD_MM, min(ys) - BOX_PAD_MM, max(xs) + BOX_PAD_MM, max(ys) + BOX_PAD_MM)

    def any(self, key: tuple, bounds: Tuple[float, float, float, float], test: Callable) -> bool:
        """Whether ``test(geometry)`` holds for any converted geometry of ``key`` (KiPI leaves out
        those ``poly_to_shapely`` gives None for). ``bounds``: the box of what is tested against;
        a geometry whose coordinate box is apart from it is not converted, its test False."""
        x0, y0, x1, y1 = bounds
        for item in self._items.get(key, ()):
            if not item[3]:
                if item[1] is None:
                    item[1] = self._box(item[0]) or (1.0, 1.0, -1.0, -1.0)
                bx0, by0, bx1, by1 = item[1]
                if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                    continue
                item[2] = poly_to_shapely(item[0])
                item[3] = True
            if item[2] is not None and test(item[2]):
                return True
        return False


# ---------------------------------------------------------------------- the two trims

def trim_unused_via_pads(trim, tracks, pads, zones, outer, vias=None) -> int:
    """KiPI's ``trim_unused_via_pads`` (reader.py:334), its zone fills and pad shapes converted
    lazily (see the module's docstring): the same decisions."""
    if not trim:
        return 0
    by_via = reader.via_neighbour_layers(trim, vias, outer) if vias else {}
    from shapely.geometry import Point as SPoint
    ends = defaultdict(list)          # (net, layer) -> track end points and widths
    for t in tracks:
        ends[(t.net, t.layer)] += [(t.start, t.width_mm), (t.end, t.width_mm)]
    copper = _Lazy()                  # (net, layer) -> pad copper and zone fills
    for p in pads:
        for lid, polys in p.shapes.items():
            copper.add((p.net, lid), polys)
    for z in zones:
        for lid, polys in z.filled.items():
            copper.add((z.net, lid), polys)
    dropped = 0
    for via, keep_ends in trim:
        for lid, d in list(via.pad_mm.items()):
            if (keep_ends and lid in outer) or not via.net or lid in by_via.get(via.id, ()):
                continue
            key = (via.net, lid)
            disc = SPoint(via.position).buffer(d / 2.0)
            x, y = via.position
            connected = (any((e[0] - x) ** 2 + (e[1] - y) ** 2 <= (d / 2.0 + w / 2.0) ** 2
                             for e, w in ends.get(key, ()))
                         or copper.any(key, disc.bounds, lambda g, disc=disc: g.intersects(disc)))
            if not connected:
                del via.pad_mm[lid]
                dropped += 1
        if not via.pad_mm:
            via.diameter_mm = 0.0
    return dropped


def trim_unused_layers(trim, tracks, vias, zones, outer) -> int:
    """KiPI's ``trim_unused_layers`` (reader.py:614), its zone fills converted lazily (see the
    module's docstring): the same decisions."""
    if not trim:
        return 0
    from shapely.geometry import Point as SPoint
    from shapely.prepared import prep
    ends = defaultdict(list)          # (net, layer) -> track end points
    for t in tracks:
        ends[(t.net, t.layer)] += [t.start, t.end]
    via_at = defaultdict(list)        # (net, layer) -> via centers
    for v in vias:
        for lid in v.layers:
            via_at[(v.net, lid)].append(v.position)
    fills = _Lazy()                   # (net, layer) -> zone fills
    for z in zones:
        for lid, polys in z.filled.items():
            fills.add((z.net, lid), polys)
    dropped = 0
    for item in trim:
        pad = item.pad
        for lid in list(pad.shapes):
            if item.keep_ends and lid in outer:
                continue
            key = (pad.net, lid)
            geoms = [g for g in map(poly_to_shapely, pad.shapes[lid]) if g is not None]
            if not geoms or not pad.net:
                continue
            shape = geoms[0] if len(geoms) == 1 else geoms[0].union(geoms[1:])
            p = prep(shape)
            connected = (any(p.covers(SPoint(e)) for e in ends.get(key, ()))
                         or any(p.covers(SPoint(c)) for c in via_at.get(key, ()))
                         or fills.any(key, shape.bounds, p.intersects))
            if not connected:
                del pad.shapes[lid]
                dropped += 1
    return dropped


# ---------------------------------------------------------------------- the seam

_OURS = {"trim_unused_via_pads": trim_unused_via_pads, "trim_unused_layers": trim_unused_layers}


def installed() -> bool:
    """Whether the reader runs ours now (both functions, by identity)."""
    return all(getattr(reader, name, None) is fn for name, fn in _OURS.items())


def seam_problem() -> Optional[str]:
    """Why ours can't replace KiPI's functions here, or None."""
    version = getattr(kipi, "__version__", None)
    if version != KIPI_VERSION:
        return f"written for KiPI {KIPI_VERSION}, this is KiPI {version}"
    for name, params in _SEAMS.items():
        fn = getattr(reader, name, None)
        if fn is _OURS[name]:
            continue
        if not callable(fn):
            return f"KiPI has no kipi.sources.kicad_pcb.reader.{name}"
        try:
            got = list(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            got = None
        if got != params:
            return (f"kipi.sources.kicad_pcb.reader.{name} takes ({', '.join(got or ['?'])}), "
                    f"not ({', '.join(params)})")
    return None


def install(_stacklevel: int = 2) -> bool:
    """Put ours in the reader; False, with a warning the first time, when KiPI's functions are not
    the ones ours were written for (KiPI's own then run)."""
    global _warned
    with _lock:
        problem = seam_problem()
        if problem is not None:
            if not _warned:
                _warned = True
                warnings.warn(f"{ENV}: the faster reader trims not used ({problem}); KiPI's own run",
                              UserWarning, stacklevel=_stacklevel)
            return False
        for name, fn in _OURS.items():
            setattr(reader, name, fn)
        return True


def uninstall() -> None:
    """Put KiPI's functions back in the reader."""
    global _warned
    with _lock:
        for name, fn in KIPI.items():
            setattr(reader, name, fn)
        _warned = False


def configure_from_env(environ: Mapping[str, str] = os.environ) -> bool:
    """Install ours unless :data:`ENV` is ``0``; whether ours run now."""
    if environ.get(ENV, "").strip() == "0":
        return installed()
    return install(_stacklevel=3)
