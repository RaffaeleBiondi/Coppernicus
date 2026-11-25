"""The copper-thickness what-if of the solve settings (``Settings.copper_thickness_mm``,
layer name → mm): its summary line and the dialog that edits it, one field per copper layer
of the open board. An empty field means the board file's thickness, so its key is removed;
a typed value keeps its key even when it equals the file's. The board file is never
changed."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping

from coppernicus.project import check_copper_thickness


def thickness_summary(values: Mapping[str, float]) -> str:
    """The settings row's text: "board file" when nothing is set, else each layer with its
    thickness ("B.Cu 0.07 mm")."""
    if not values:
        return "board file"
    return ", ".join(f"{layer} {mm:g} mm" for layer, mm in values.items())


def off_board(values: Mapping[str, float], layers: Iterable[str]) -> List[str]:
    """The keys of ``values`` naming no copper layer of the board (each would stop every
    solve), in their order."""
    on_board = set(layers)
    return [layer for layer in values if layer not in on_board]


def thickness_from_texts(texts: Mapping[str, str], kept: Mapping[str, float]) -> Dict[str, float]:
    """The settings from the dialog: ``texts`` is each board layer's field (empty: the board
    file's value, no key), ``kept`` the keys for layers not on the board still listed. A
    comma reads as the decimal sign. Checked by :func:`check_copper_thickness`, which raises
    :class:`ProjectError` naming the layer."""
    values: Dict[str, object] = {layer: text.strip().replace(",", ".")
                                 for layer, text in texts.items() if text.strip()}
    values.update(kept)
    return check_copper_thickness(values)
