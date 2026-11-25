"""Footprint fields from a KiCad board file, which KiPI's importer parses and then drops.

KiCad 8 and later copy a symbol's fields onto its footprint, and the board file keeps them as
``(property "NAME" "TEXT")`` in each ``(footprint ...)``. This reads the ``PI_*`` ones (see
:mod:`coppernicus.board_fields`), and the nets of those parts' pads, with KiPI's own
s-expression reader, in a second pass over the file. The board file is only read.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from kipi.core.model.parts import parse_capacitance
from kipi.sources.kicad_pcb.reader import KiCadFileError, Nets, load_tree
from kipi.sources.kicad_pcb.sexpr import child, children, xy

from coppernicus.board_fields import PREFIX, BoardFields
from coppernicus.engine.board import EngineError

# A capacitance written with its unit: a prefix or F right after the number ("10u", "4n7",
# "100 nF"), ending where KiPI's own pattern ends a value (kipi/core/model/parts.py _CAP_RE), so
# a word after a bare number ("100 pcs") is no unit. KiPI reads a bare number as µF; that unit
# is a guess, never used behind a verdict.
_WITH_UNIT = re.compile(r"^\s*\d*\.?\d*\s*(?:[pnuµμm]\d*\s*[Ff]?|[Ff])(?=$|[\s,;/_(])")


def value_farads(text: str) -> Optional[float]:
    """The capacitance of a footprint Value (KiPI's ``parse_capacitance``), only when the text
    gives its unit; None otherwise ("0.1", "C_0402")."""
    farads = parse_capacitance(text)
    return farads if farads is not None and _WITH_UNIT.match(text) else None


def read_board_fields(path, prefix: str = PREFIX) -> BoardFields:
    """The ``PI_*`` footprint fields of the board at ``path`` (names matched in any case, so
    that a lower-case ``pi_load`` is refused rather than missed) and the nets of those
    parts' pads, and of the parts a ``PI_SOURCE_REF`` names (an external-inductor buck's
    inductor, which has no fields). Another board format, or a KiCad board with no such field, gives none: the
    text is scanned first and parsed only when it has one. Raises :class:`EngineError` for
    an unreadable file, or two footprints with the same reference both carrying fields."""
    path = Path(path)
    if path.suffix.lower() != ".kicad_pcb":
        return BoardFields()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise EngineError(f"{path.name}: {e}") from None
    if not re.search(r'\(property\s+"?' + re.escape(prefix), text, re.IGNORECASE):
        return BoardFields()
    try:
        root = load_tree(path)
    except (KiCadFileError, OSError) as e:
        raise EngineError(str(e)) from None
    nets = Nets(root)
    out = BoardFields()
    others: Dict[str, object] = {}           # reference -> footprint, for those without fields

    def pad_nets(fp):
        return {pad[1]: nets.of(pad) for pad in children(fp, "pad")
                if len(pad) > 1 and isinstance(pad[1], str)}
    for fp in children(root, "footprint"):
        props = {p[1]: p[2] for p in children(fp, "property")
                 if len(p) > 2 and isinstance(p[1], str) and isinstance(p[2], str)}
        mine = {k: v for k, v in props.items() if k.upper().startswith(prefix.upper())}
        ref = props.get("Reference") or next(
            (t[2] for t in children(fp, "fp_text") if len(t) > 2 and t[1] == "reference"), "")
        if not mine:
            if ref.strip():
                others.setdefault(ref, fp)
            continue
        if not ref.strip():
            x, y = xy(child(fp, "at"))
            raise EngineError(f"{path.name}: a footprint at ({x:g}, {y:g}) mm has {prefix}* fields "
                              "but no reference: set its reference in KiCad")
        if ref in out.fields:
            raise EngineError(f"{path.name}: two footprints are {ref!r} and both carry "
                              f"{prefix}* fields; give each part its own reference")
        out.fields[ref] = mine
        out.pad_nets[ref] = pad_nets(fp)
        value = props.get("Value")
        if value is None:                     # KiCad 7 and older: (fp_text value "10u" ...)
            value = next((t[2] for t in children(fp, "fp_text")
                          if len(t) > 2 and t[1] == "value" and isinstance(t[2], str)), None)
        if value is not None:
            out.values[ref] = value
            farads = value_farads(value)
            if farads is not None:
                out.farads[ref] = farads
    named = {r.strip() for f in out.fields.values() for k, v in f.items()
             if k.upper() in (f"{prefix.upper()}SOURCE_REF", f"{prefix.upper()}GND_REF")
             for r in v.replace(";", ",").split(",") if r.strip()}
    for ref in sorted(named - set(out.pad_nets)):
        if ref in others:
            out.pad_nets[ref] = pad_nets(others[ref])
    return out


def read_part_fields(path, prefix: str = PREFIX) -> Dict[str, Dict[str, str]]:
    """``{reference: {field name: text}}``: the fields alone (see :func:`read_board_fields`)."""
    return read_board_fields(path, prefix).fields
