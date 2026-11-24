"""Footprint fields from a KiCad board file, which KiPI's importer parses and then drops.

KiCad 8 and later copy a symbol's fields onto its footprint, and the board file keeps them as
``(property "NAME" "TEXT")`` in each ``(footprint ...)``. This reads them with KiPI's own
s-expression reader, in a second pass over the file. The board file is only read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from kipi.sources.kicad_pcb.reader import KiCadFileError, load_tree
from kipi.sources.kicad_pcb.sexpr import children

from coppernicus.engine.board import EngineError

PREFIX = "PI_"


def read_part_fields(path, prefix: str = PREFIX) -> Dict[str, Dict[str, str]]:
    """``{reference: {field name: text}}`` for the footprints of the ``.kicad_pcb`` at
    ``path`` that have fields whose names start with ``prefix`` (other fields are left out).
    Raises :class:`EngineError` for another board format (only KiCad board files carry
    footprint fields), an unreadable file, or two footprints with the same reference both
    carrying such fields."""
    path = Path(path)
    if path.suffix.lower() != ".kicad_pcb":
        raise EngineError(f"{path.name}: footprint fields are read from KiCad board files "
                          "(.kicad_pcb) only; this board is another format")
    try:
        root = load_tree(path)
    except (KiCadFileError, OSError) as e:
        raise EngineError(str(e)) from None
    out: Dict[str, Dict[str, str]] = {}
    for fp in children(root, "footprint"):
        props = {p[1]: p[2] for p in children(fp, "property")
                 if len(p) > 2 and isinstance(p[1], str) and isinstance(p[2], str)}
        ref = props.get("Reference") or next(
            (t[2] for t in children(fp, "fp_text") if len(t) > 2 and t[1] == "reference"), "")
        mine = {k: v for k, v in props.items() if k.startswith(prefix)}
        if not mine:
            continue
        if ref in out:
            raise EngineError(f"{path.name}: two footprints are {ref!r} and both carry "
                              f"{prefix}* fields; give each part its own reference")
        out[ref] = mine
    return out
