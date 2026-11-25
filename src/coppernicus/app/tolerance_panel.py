"""The Tolerance tab: each load's tolerance stack-up (Task 11, :mod:`coppernicus.tolerance`)
from the results the app already holds.

An editor beside the table. The editor holds each load's tolerances (``tol``, ``dc_tol``,
``ac_tol``, each in % of nominal or mV from it) and entered parts (``ripple_mv``,
``transient_mv``), each source's DC accuracy, and the ``tolerance:`` options; a cell takes a
number (± that) or a signed ``below, above`` as datasheets write it, empty is "not set" and 0 is
a value; an edit builds a new terminal through its own checks (``dataclasses.replace``), so
nothing the project file would refuse is written. A rail from the fields is editable too (a
part's own PI_TOL / PI_ACC fields still win over an edit, as in every merge): the edit is held on the project's own rail of that net,
on the part of the same reference, which the merge carries by ref
(:func:`coppernicus.board_fields.carried_by_ref`); with no rail of its own there, the
project's rail becomes a copy of the fields' parts, so the merge sees nothing to warn about.

The table is built from what the window holds (:func:`compose_held`): the power tree, the
Impedance tab's results and the Ripple & noise tab's. A result held for an older setup is
stale, so its part is missing, never a pass; the tab never starts a solve by itself. Its
Solve button runs the selected row's next missing solve explicitly, through the tab that owns
it (the power tree, the noise, the impedance with KiPI's transient estimate), so the usual
estimate line and "Solve anyway?" apply. Export writes the CLI's two files. The pure
functions at the top need no window.
"""

from __future__ import annotations

import copy
import dataclasses
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from coppernicus import board_fields as BF
from coppernicus.app.panels import FieldComboBox
from coppernicus.project import (
    LOAD_PART_KEYS,
    LOAD_TOLERANCE_KEYS,
    RIPPLE_SUMS,
    SOURCE_ACCURACY_KEYS,
    Load,
    Project,
    ProjectError,
    Rail,
    Source,
    tolerance_pair,
)
from coppernicus.results import TOLERANCE_CHECKS, TOLERANCE_PARTS
from coppernicus.tolerance import KNOWN_KEYS, meant_key

Terminal = Union[Load, Source]
Pair = Tuple[float, float]

# ---------------------------------------------------------------------- a pair in a cell

LOAD_COLUMNS = (("tol_pct", "Tol %"), ("tol_mv", "Tol mV"), ("dc_tol_pct", "DC tol %"),
                ("dc_tol_mv", "DC tol mV"), ("ac_tol_pct", "AC tol %"), ("ac_tol_mv", "AC tol mV"),
                ("ripple_mv", "Ripple mV"), ("transient_mv", "Transient mV"))
SOURCE_COLUMNS = (("accuracy_pct", "Accuracy %"), ("accuracy_mv", "Accuracy mV"))
KEY_TIPS = {
    "tol_pct": "tol_pct: the whole band, % of nominal (the set voltage of the source driving the rail)",
    "tol_mv": "tol_mv: the whole band, mV from nominal",
    "dc_tol_pct": "dc_tol_pct: the DC share (DC drop + the source's accuracy), % of nominal",
    "dc_tol_mv": "dc_tol_mv: the DC share (DC drop + the source's accuracy), mV from nominal",
    "ac_tol_pct": "ac_tol_pct: the AC share (ripple + transient), % of nominal",
    "ac_tol_mv": "ac_tol_mv: the AC share (ripple + transient), mV from nominal",
    "ripple_mv": "ripple_mv: the ripple entered (a datasheet or a measurement), mV from nominal; with an "
                 "aggressor listing the load's probe, the larger of the two per side",
    "transient_mv": "transient_mv: the transient entered, mV from nominal; the only transient judged "
                    "(KiPI's estimate is shown, never judged)",
    "accuracy_pct": "accuracy_pct: the source's DC accuracy, % of its set voltage",
    "accuracy_mv": "accuracy_mv: the source's DC accuracy, mV from its set voltage",
}
PAIR_TIP = ("A number n is ± n; a signed pair as datasheets write it: -3, 2 (or [-3, 2], -3 / +2). "
            "Empty: not set; 0 is a value. A % and its mV: one of the two.")

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_DECIMAL_COMMA = re.compile(r"^\+?\d+,\d+$")       # 3,3 is 3.3; -3,2 can only be a pair


def parse_pair(text: Optional[str], what: str) -> Optional[Pair]:
    """A cell's text as the project file's ``(below, above)`` (:func:`coppernicus.project.
    tolerance_pair`, which checks it): empty → None, ``5`` or ``±5`` → (−5, 5), ``-3, 2``,
    ``[-3, 2]``, ``-3 / +2`` or ``-3; 2`` → (−3, 2); ``3,3`` reads as 3.3. Raises
    :class:`ProjectError` naming ``what``."""
    t = (text or "").strip()
    if not t:
        return None
    t = t.replace("±", "").replace("−", "-").strip()
    if _DECIMAL_COMMA.match(t):
        t = t.replace(",", ".")
    if t.startswith("[") and t.endswith("]"):
        t = t[1:-1]
    parts = [x.strip() for x in re.split(r"[,;/]", t)]
    for x in parts:
        if not _NUMBER.fullmatch(x):
            raise ProjectError(f"{what}: {x!r} is not a number (a number n is ± n, or below, above: -3, 2)")
    values = [float(x) for x in parts]
    if len(values) > 2:
        raise ProjectError(f"{what} must be a number (± that) or [below, above], not {text.strip()!r}")
    return tolerance_pair(values[0] if len(values) == 1 else values, what)


def _num(x: float) -> str:
    return f"{x:.15g}"


def pair_text(pair: Optional[Pair]) -> str:
    """A pair as a cell shows it, read back exactly by :func:`parse_pair`: "5" for ± 5,
    "-3, 2" otherwise, "" for None."""
    if pair is None:
        return ""
    below, above = pair
    return _num(above) if below == -above else f"{_num(below)}, {_num(above)}"


def terminal_keys(term: Terminal) -> Tuple[str, ...]:
    return SOURCE_ACCURACY_KEYS if isinstance(term, Source) else LOAD_TOLERANCE_KEYS + LOAD_PART_KEYS


def with_tolerance(term: Terminal, key: str, text: str) -> Terminal:
    """A copy of ``term`` with ``key`` set from a cell's ``text``, built by
    ``dataclasses.replace`` so its own checks run (a ``_pct`` beside its ``_mv`` is refused)."""
    what = "source" if isinstance(term, Source) else "load"
    if key not in terminal_keys(term):
        raise ValueError(f"{key} is no tolerance key of a {what}")
    return dataclasses.replace(term, **{key: parse_pair(text, f"{what} {term.ref}: {key}")})


# ---------------------------------------------------------------------- where an edit is held

@dataclass
class EditRow:
    """A source or load of the merged project (what is solved): its rail, its index there, the
    terminal as merged (its values shown), and why it cannot be edited ("" when it can)."""
    kind: str                      # "source" or "load"
    net: str
    index: int
    term: Terminal
    read_only: str = ""


def _terms(rail: Optional[Rail], kind: str) -> List[Terminal]:
    return [] if rail is None else (rail.sources if kind == "source" else rail.loads)


def _holder(project: Project, merged: BF.Merged, kind: str, net: str, index: int
            ) -> Tuple[Optional[Rail], Optional[int], str]:
    """Where the edit of the merged terminal is held: ``(the project's rail, its index)``;
    ``(None, None, "")`` when a copy of the fields' parts must hold it; ``(None, None, why)``
    when it cannot be held."""
    origin = merged.origins.get(net)
    own = project.rail(net)
    if origin is None or origin.source != "fields":
        return own, index, ""                 # the project's own rail (merged as a copy of it)
    terms = _terms(merged.project.rail(net), kind)
    target = terms[index]
    own_terms = _terms(own, kind)
    keys = SOURCE_ACCURACY_KEYS if kind == "source" else BF.TOLERANCE_KEYS
    pairs, _lost = BF.carried_by_ref(terms, own_terms, keys)
    paired = {id(t): o for t, o in pairs}
    used = {id(o) for _t, o in pairs}
    held = paired.get(id(target)) or next((o for o in own_terms if o.ref == target.ref and id(o) not in used),
                                          None)
    if held is not None:
        return own, next(i for i, o in enumerate(own_terms) if o is held), ""
    if own is None or not (own.sources or own.loads):
        return None, None, ""
    return None, None, (f"{net} comes from KiCad fields, and the project file's own rail of {net} (which "
                        f"differs from them) has no {target.ref} to hold its tolerances: Override in "
                        "project to edit it")


def edit_rows(project: Project, merged: BF.Merged) -> List[EditRow]:
    """Every source and load of the merged project, rail by rail (sources first), each with why
    it is read-only ("" when editable)."""
    rows = []
    for rail in merged.project.rails:
        for kind in ("source", "load"):
            for i, term in enumerate(_terms(rail, kind)):
                why = _holder(project, merged, kind, rail.net, i)[2]
                rows.append(EditRow(kind, rail.net, i, term, why))
    return rows


def set_value(project: Project, merged: BF.Merged, kind: str, net: str, index: int, key: str,
              text: str) -> str:
    """Write ``key`` of the merged terminal (``kind``, ``net``, ``index``) from a cell's
    ``text`` into ``project``. Returns a note ("" when there is nothing to say); raises
    :class:`ProjectError` (or ValueError, for a read-only one), writing nothing."""
    own, i, why = _holder(project, merged, kind, net, index)
    if why:
        raise ValueError(why)
    if own is not None:
        terms = _terms(own, kind)
        terms[i] = with_tolerance(terms[i], key, text)
        return ""
    fields = merged.project.rail(net)         # a copy of the fields' parts holds it
    sources, loads = copy.deepcopy(fields.sources), copy.deepcopy(fields.loads)
    held = sources if kind == "source" else loads
    held[index] = with_tolerance(held[index], key, text)     # refused: nothing written yet
    own = project.rail(net)
    if own is None:
        own = Rail(net)
        project.rails.append(own)
    own.sources, own.loads = sources, loads
    return (f"{net} comes from the KiCad footprint fields, where no field holds a tolerance yet: its sources "
            f"and loads are now also in the project file's own rail of {net}, which holds it (KiCad still "
            "gives their values)")


# ---------------------------------------------------------------------- misspelt keys of the loaded file

def read_misspelt(path) -> List[Tuple[str, Dict[str, Any]]]:
    """The misspelt tolerance keys of the project file at ``path`` as loaded
    (:func:`coppernicus.tolerance.misspelt_keys`): ``(warning, not-judged entry)`` each. Loading
    ignores them; a save from the app does not keep them. [] when it cannot be read."""
    import yaml

    from coppernicus.tolerance import misspelt_keys
    try:
        with open(path, encoding="utf-8") as fh:
            warnings, entries = misspelt_keys(yaml.safe_load(fh))
    except (OSError, yaml.YAMLError):
        return []
    return list(zip(warnings, entries))


def _siblings(key: str) -> Tuple[str, ...]:
    """A key and its other unit (tol_pct, tol_mv), where it has one."""
    for a, b in (("_pct", "_mv"), ("_mv", "_pct")):
        if key.endswith(a) and key[: -len(a)] + b in KNOWN_KEYS:
            return key, key[: -len(a)] + b
    return (key,)


def live_misspelt(found: Sequence[Tuple[str, Dict[str, Any]]], project: Project
                  ) -> List[Tuple[str, Dict[str, Any]]]:
    """The misspelt keys still standing: one is settled once its part (by label, on its net)
    has the key it meant set, in either unit; never by a save (the file then loses the key
    while the tolerance stays unset)."""
    out = []
    for warning, entry in found:
        rail = project.rail(str(entry.get("net", "")))
        terms = (rail.sources + rail.loads) if rail is not None else []
        keys = _siblings(meant_key(str(entry.get("limit", ""))))
        if not any(t.label == entry.get("item") and getattr(t, k, None) is not None
                   for t in terms for k in keys):
            out.append((warning, entry))
    return out


# ---------------------------------------------------------------------- the results the app holds

SOLVE_BUTTON = "the Tolerance tab's Solve button"
TREE_NOTE = ("the power tree is not solved, or the setup changed since its solve, so its DC drop is not "
             "known: solve the power tree (Ctrl+T)")


def compose_held(pairs: Dict[str, Optional[str]], own: Project, merged: BF.Merged, tree,
                 ac: Dict[str, Tuple[Any, Any]], noise: Dict[str, Tuple[Any, Any]], ac_now: Any,
                 noise_now: Any, misspelt: Sequence[Tuple[str, Dict[str, Any]]] = ()):
    """The stack-up of the merged project from what the app holds: ``tree`` (None: not solved
    or dropped), ``ac`` (rail -> (ImpedanceResult, the key it was solved at)), ``noise``
    (aggressor -> (NoiseResult, key)); a result whose key is not ``ac_now`` / ``noise_now`` is
    stale, so its part is missing, with what to do. ``own``: the user's project (what the KiCad
    fields' rails could not take from it); ``misspelt``: the loaded file's (still standing)."""
    from coppernicus.tolerance import compose, fields_losses
    project = merged.project
    fresh = [r for r, key in ac.values() if key == ac_now]
    stale = [r for r, key in ac.values() if key != ac_now]
    ac_notes = {}
    for p in project.probes:
        held = next((r for r in fresh if r.probe(p.name) is not None), None)
        old = next((r for r in stale if r.probe(p.name) is not None), None)
        if held is not None:
            if held.probe(p.name).transient is None:
                ac_notes[p.name] = (f"estimate not available: solve the impedance of {held.rail} with the "
                                    f"transient ({SOLVE_BUTTON})")
        elif old is not None:
            ac_notes[p.name] = (f"the setup changed since the impedance of {old.rail} was solved: solve it "
                                f"again with the transient ({SOLVE_BUTTON})")
        else:
            ac_notes[p.name] = f"the impedance at {p.name} is not solved ({SOLVE_BUTTON}, with the transient)"
    results, noise_notes = [], {}
    for a in project.aggressors:
        entry = noise.get(a.name)
        if entry is None:
            noise_notes[a.name] = "not solved yet: Ripple & noise tab (Ctrl+Shift+N)"
        elif entry[1] != noise_now:
            noise_notes[a.name] = ("the setup changed since its solve: solve it again in the Ripple & "
                                   "noise tab")
        else:
            results.append(entry[0])
    lost_warnings, dropped, lost_accuracy = fields_losses(own, project, merged.from_fields)
    live = live_misspelt(misspelt, project)
    result = compose(project, tree, fresh, results, pairs, ac_notes, noise_notes,
                     list(merged.warnings) + [w for w, _e in live] + lost_warnings,
                     [dict(e) for _w, e in live] + dropped, lost_accuracy,
                     tree_note=TREE_NOTE if tree is None else "")
    merged.annotate(result)                  # where the rails came from, as the CLI says
    return result


def _source_of(project: Project, label: str) -> Optional[Source]:
    return next((s for r in project.rails for s in r.sources if s.label == label), None)


def needed_solve(row, project: Project, has_tree: bool, fresh_noise: Sequence[str] = ()
                 ) -> Optional[Tuple[str, str, str]]:
    """The next solve the stack-up ``row`` misses, as ``(kind, target, button text)``: the
    power tree; a noise solve of an aggressor listing its probe that is not solved (or stale);
    the impedance of its probe's net with KiPI's transient estimate (shown, never judged), when
    the probe has a load step and its source a bandwidth. None: nothing a solve would add."""
    if not has_tree:
        return "tree", "", "Solve power tree"
    if row.parts.get("ripple") is None and row.probe:
        for a in project.aggressors:
            if row.probe in a.ripple_probes and a.name not in row.ripple_from and a.name not in fresh_noise:
                return "noise", a.name, f"Solve noise of {a.name}"
    if row.transient_estimate_v is None and row.probe:
        probe = next((p for p in project.probes if p.name == row.probe), None)
        source = _source_of(project, row.source)
        bw = project.ac.bandwidth_hz
        if (probe is not None and probe.step_a and probe.slew_a_per_us and source is not None
                and bw.get(source.label, bw.get(source.ref)) is not None):
            return "ac", probe.net, f"Solve impedance of {probe.net} + transient"
    return None


# ---------------------------------------------------------------------- cells

VERDICT_WORDS = {"pass": "PASS", "fail": "FAIL", "not_judged": "NOT JUDGED", "": "no tolerance"}
RESULT_HEADERS = ("Load", "Net", "Nominal", "DC drop", "DC accuracy", "Ripple", "Transient", "DC check",
                  "AC check", "Total check", "Verdict", "Why")
RESULT_TIPS = ("The load", "Its supply net", "The set voltage of the source driving its rail",
               "mV below / above nominal, and where each part came from (a missing part: what to do)", "", "",
               "Only an entered transient_mv is judged; KiPI's estimate is shown, never judged",
               "DC drop + accuracy against dc_tol", "Ripple + transient against ac_tol",
               "Every part against tol", "Fail > not judged > pass over its checks",
               "Why it fails or is not judged")


def _mv(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 1e3:.4g}"


def origin_text(source: str) -> str:
    """Where a part came from, short: tree, entered, aggressor U1 (+ ripple_mv), or as given."""
    if source.startswith("power tree"):
        return "tree" + source[len("power tree"):]
    if source.startswith("entered"):
        return "entered"
    if source.startswith("noise of "):
        names = source[len("noise of "):].split(" at ")[0]
        word = "aggressors" if "," in names else "aggressor"
        return f"{word} {names}" + (" + ripple_mv" if source.endswith(" and ripple_mv") else "")
    return source


def part_cell(row, kind: str) -> Tuple[str, str]:
    """(text, tooltip) of a part: mV below / above and its origin, or missing (with its least
    value where known) and what to do."""
    p = row.parts.get(kind)
    if p is None:
        f = row.floors.get(kind)
        least = f" · at least {_mv(f.below_v)} / {_mv(f.above_v)} mV" if f is not None else ""
        return f"missing{least}", row.hints.get(kind, "")
    tip = p.source + (f": {p.detail}" if p.detail else "")
    return f"{_mv(p.below_v)} / {_mv(p.above_v)} mV · {origin_text(p.source)}", tip


def check_cell(c) -> Tuple[str, str]:
    """(text, tooltip) of a check: its verdict, the sums against what is allowed, the margin;
    "" without a tolerance."""
    if not c.limit:
        return "", "No tolerance set for this check"
    tag = {True: "PASS", False: "FAIL"}.get(c.passed, "NOT JUDGED")
    tip = c.limit + (f": {c.reason}" if c.reason else "")
    if not c.has_limit:
        return f"{tag} · {c.reason}", tip
    text = (f"{tag} · {_mv(c.below_v)} / {_mv(c.above_v)} of {_mv(c.allowed_below_v)} / "
            f"{_mv(c.allowed_above_v)} mV · margin {_mv(c.margin_below_v)} / {_mv(c.margin_above_v)}")
    return text + ("" if c.complete else " (parts missing)"), tip


def row_reasons(result, row) -> str:
    """Why the row fails or is not judged: its entries of the result."""
    entries = [e for e in result.failed + result.not_judged
               if e.get("item") == row.load and e.get("net") == row.net]
    return "; ".join(f"{e['limit']}: {e['reason']}" for e in entries)


def result_rows(result) -> List[Tuple[str, ...]]:
    """The results table's texts, one row per load (RESULT_HEADERS)."""
    out = []
    for row in result.rows:
        nominal = "-" if row.nominal_v is None else f"{row.nominal_v:g} V"
        parts = [part_cell(row, k)[0] for k in TOLERANCE_PARTS]
        checks = [check_cell(row.checks[k])[0] if k in row.checks else "" for k in TOLERANCE_CHECKS]
        out.append((row.load, row.net, nominal, *parts, *checks, VERDICT_WORDS.get(row.verdict, row.verdict),
                    row_reasons(result, row)))
    return out


FAIL_COLOR, AMBER = "#c0392b", "#b9770e"


def header_html(result, why: str = "") -> str:
    """The verdict line: fail > not judged > pass, and its note; or why nothing is judged."""
    if result is None:
        return f"<b>Tolerance stack-up</b>: {html.escape(why)}" if why else ""
    words = {"fail": f"<span style='color:{FAIL_COLOR}'><b>FAIL</b></span>", "pass": "<b>PASS</b>",
             "not_judged": f"<span style='color:{AMBER}'><b>NOT JUDGED</b></span>"}
    return (f"<b>Tolerance stack-up</b>: {words.get(result.verdict, result.verdict)}"
            f"<br><small>{html.escape(result.verdict_note)}</small>")


def notes_text(result) -> str:
    lines = [f"FAIL {e['item']} ({e['limit']}): {e['reason']}" for e in result.failed]
    lines += [f"NOT JUDGED {e['item']} ({e['limit']}): {e['reason']}" for e in result.not_judged]
    lines += [f"warning: {w}" for w in result.warnings]
    lines += [f"assumption: {a}" for a in result.assumptions]
    return "\n".join(lines)


# ---------------------------------------------------------------------- the tab

READ_ONLY_BG, MISSING_BG = QColor("#f0f0f0"), QColor("#fff3cd")


def _table(headers, tips=()) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.setMinimumHeight(60)
    for c, tip in enumerate(tips):
        if tip:
            t.horizontalHeaderItem(c).setToolTip(tip)
    return t


class TolerancePanel(QWidget):
    """The Tolerance results tab (see the module docstring)."""

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self.result = None                     # the last stack-up shown (None: nothing judged)
        self.why = ""                          # why nothing is judged
        self._rows: List[EditRow] = []
        self._signature = None
        self._pending = False
        self._dirty = True
        self._pairs: Tuple[Any, Dict[str, Optional[str]]] = (None, {})
        self._next: Optional[Tuple[str, str, str]] = None
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self):
        self.loads = _table(("Load", "Rail") + tuple(h for _k, h in LOAD_COLUMNS),
                            ("", "") + tuple(f"{KEY_TIPS[k]}. {PAIR_TIP}" for k, _h in LOAD_COLUMNS))
        self.sources = _table(("Source", "Rail") + tuple(h for _k, h in SOURCE_COLUMNS),
                              ("", "") + tuple(f"{KEY_TIPS[k]}. {PAIR_TIP}" for k, _h in SOURCE_COLUMNS))
        self.ripple_sum = FieldComboBox()
        self.ripple_sum.addItems(list(RIPPLE_SUMS))
        self.ripple_sum.setToolTip("How several aggressors' ripple at one probe adds: linear, the sum of "
                                   "their peaks (unsynchronised switchers line up at times), or rss, KiPI's "
                                   "root sum of squares (tolerance.ripple_sum)")
        self.bw_limit = QCheckBox("Judge the ripple through 20 MHz")
        self.bw_limit.setToolTip("tolerance.bw_limit_20mhz: through a 20 MHz bandwidth limit, as many "
                                 "datasheets measure, instead of the full band (both are shown)")
        options = QWidget()
        form = QFormLayout(options)
        form.addRow("Ripple sum", self.ripple_sum)
        form.addRow(self.bw_limit)
        self.editor_tabs = QTabWidget()
        self.editor_tabs.setUsesScrollButtons(True)
        self.editor_tabs.addTab(self.loads, "Loads")
        self.editor_tabs.addTab(self.sources, "Sources")
        self.editor_tabs.addTab(options, "Options")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        editor = QWidget()
        el = QVBoxLayout(editor)
        el.setContentsMargins(4, 4, 4, 4)
        el.addWidget(QLabel("<b>Tolerances</b> (a number n is ± n; or below, above)"))
        el.addWidget(self.editor_tabs, 1)
        el.addWidget(self.status)

        self.header = QLabel()
        self.header.setWordWrap(True)
        self.header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.solve_button = QPushButton("Solve")
        self.solve_button.setToolTip("Run the selected load's next missing solve (power tree, noise, or the "
                                     "impedance with KiPI's transient estimate), in its own tab")
        self.solve_button.setEnabled(False)
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip("The table as `coppernicus tolerance` writes it: "
                                      "<project>_tolerance.csv and <project>_tolerance_summary.json")
        self.export_button.setEnabled(False)
        self.table = _table(RESULT_HEADERS, RESULT_TIPS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMinimumHeight(40)
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        head.addWidget(self.header, 1)
        head.addWidget(self.solve_button, 0, Qt.AlignTop)
        head.addWidget(self.export_button, 0, Qt.AlignTop)
        rl.addLayout(head)
        rl.addWidget(self.table, 2)
        rl.addWidget(self.notes, 1)

        split = QSplitter(Qt.Horizontal)
        for w in (editor, results):
            scroll = QScrollArea()                # the dock is short on a laptop: scroll, never grow it
            scroll.setWidget(w)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            split.addWidget(scroll)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([480, 720])
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

        self.loads.itemChanged.connect(lambda item: self._item_changed("load", item))
        self.sources.itemChanged.connect(lambda item: self._item_changed("source", item))
        self.ripple_sum.currentIndexChanged.connect(lambda *_: self._option_edited())
        self.bw_limit.toggled.connect(lambda *_: self._option_edited())
        self.table.currentCellChanged.connect(lambda row, *_: self._row_selected(row))
        self.table.cellClicked.connect(lambda row, _c: self.go_to_row(row))
        self.solve_button.clicked.connect(self.solve_next)
        self.export_button.clicked.connect(self._export_dialog)

    # ------------------------------------------------------------------ refresh
    def schedule_refresh(self):
        """Something the stack-up rests on changed (the project, the power tree, a result held):
        recomposed once, after the events now queued, while the tab is shown; else when shown."""
        self._dirty = True
        if self.isVisible() and not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._refresh_pending)

    def _refresh_pending(self):
        self._pending = False
        if self._dirty and self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def _say(self, text: str, bar: bool = False):
        self.status.setText(text)
        if bar:
            self._win.statusBar().showMessage(text)

    def _load_probes(self) -> Dict[str, Optional[str]]:
        """Each load's probe by KiPI's rule (engine.tolerance.load_probes), kept while the rails
        and probes are the same."""
        from coppernicus.app.ac_panel import _project_key
        from coppernicus.engine.tolerance import load_probes
        win = self._win
        key = (id(win.board), json.dumps(_project_key(win.merged.project), sort_keys=True, default=str))
        if self._pairs[0] != key:
            self._pairs = (key, load_probes(win.board, win.merged.project))
        return self._pairs[1]

    def compose(self):
        """(the stack-up of what the window holds, why nothing is judged)."""
        from coppernicus.app.ac_panel import _project_key as ac_key
        from coppernicus.app.noise_panel import project_key as noise_key
        from coppernicus.engine import EngineError
        win = self._win
        if win.board is None:
            return None, "open a board or a project"
        if win.fields_error:
            return None, ("nothing is judged while a KiCad footprint field cannot be used: "
                          f"{win._fields_refusal()}")
        merged = win.merged
        try:
            pairs = self._load_probes()
        except EngineError as e:
            return None, f"nothing is judged until the setup can be read: {e}"
        result = compose_held(pairs, win.project, merged, win.tree, win.ac_panel.results,
                              win.noise_panel.results, ac_key(merged.project), noise_key(merged.project),
                              getattr(win, "misspelt", []))
        return result, ""

    def refresh(self):
        """Fill the editor (when its rows changed) and the table from what the window holds now."""
        self._dirty = False
        self._fill_editor()
        self.result, self.why = self.compose()
        self._fill_table()

    # ------------------------------------------------------------------ editor
    def _fill_editor(self, force: bool = False):
        win = self._win
        rows = edit_rows(win.project, win.merged) if win.board is not None else []
        signature = [(r.kind, r.net, r.index, r.term.label, r.read_only,
                      tuple(pair_text(getattr(r.term, k)) for k in terminal_keys(r.term))) for r in rows]
        opts = win.project.tolerance
        self.ripple_sum.blockSignals(True)
        self.ripple_sum.setCurrentText(opts.ripple_sum)
        self.ripple_sum.blockSignals(False)
        self.bw_limit.blockSignals(True)
        self.bw_limit.setChecked(opts.bw_limit_20mhz)
        self.bw_limit.blockSignals(False)
        editing = QAbstractItemView.State.EditingState
        in_edit = editing in (self.loads.state(), self.sources.state())
        if not force and (signature == self._signature or in_edit):
            return
        self._signature, self._rows = signature, rows
        for table, kind, columns in ((self.loads, "load", LOAD_COLUMNS),
                                     (self.sources, "source", SOURCE_COLUMNS)):
            mine = [r for r in rows if r.kind == kind]
            table.blockSignals(True)
            table.setRowCount(len(mine))
            for i, r in enumerate(mine):
                for c, text in enumerate((r.term.label, r.net)):
                    item = QTableWidgetItem(text)
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setData(Qt.UserRole, self._rows.index(r))
                    table.setItem(i, c, item)
                for c, (key, _h) in enumerate(columns, start=2):
                    item = QTableWidgetItem(pair_text(getattr(r.term, key)))
                    item.setData(Qt.UserRole, self._rows.index(r))
                    if r.read_only:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setBackground(QBrush(READ_ONLY_BG))
                        item.setToolTip(r.read_only)
                    else:
                        item.setToolTip(f"{KEY_TIPS[key]}. {PAIR_TIP}")
                    table.setItem(i, c, item)
            table.blockSignals(False)

    def set_cell(self, kind: str, label: str, key: str, text: str):
        """Type ``text`` into ``key`` of the ``kind`` row labelled ``label`` (as a user would)."""
        table, columns = (self.loads, LOAD_COLUMNS) if kind == "load" else (self.sources, SOURCE_COLUMNS)
        col = 2 + [k for k, _h in columns].index(key)
        for i in range(table.rowCount()):
            if table.item(i, 0).text() == label:
                table.item(i, col).setText(text)
                return
        raise KeyError(f"no {kind} {label} in the editor")

    def _item_changed(self, kind: str, item: QTableWidgetItem):
        table, columns = (self.loads, LOAD_COLUMNS) if kind == "load" else (self.sources, SOURCE_COLUMNS)
        c = item.column()
        if c < 2:
            return
        row = self._rows[item.data(Qt.UserRole)]
        key = columns[c - 2][0]
        win = self._win
        old = pair_text(getattr(row.term, key))
        try:
            note = set_value(win.project, win.merged, kind, row.net, row.index, key, item.text())
        except (IndexError, AttributeError):  # the rail changed under the edit: show it anew
            self._fill_editor(force=True)
            self._say("Not applied: the rail changed meanwhile; type it again", bar=True)
            return
        except ValueError as e:               # ProjectError too: nothing written
            table.blockSignals(True)
            item.setText(old)
            table.blockSignals(False)
            self._say(f"Not applied: {e}", bar=True)
            return
        win._merge_again()                    # what the next stack-up takes; never drops a solve
        win.dirty = True
        win._update_title()
        self._fill_editor(force=True)
        value = pair_text(getattr(self._term(kind, row.net, row.index), key))
        self._say(f"{row.term.label} {key} {value or 'cleared'}" + (f" · {note}" if note else ""),
                  bar=bool(note))
        self.refresh()

    def _term(self, kind: str, net: str, index: int) -> Terminal:
        return _terms(self._win.merged.project.rail(net), kind)[index]

    def _option_edited(self):
        win = self._win
        try:
            win.project.tolerance = dataclasses.replace(win.project.tolerance,
                                                        ripple_sum=self.ripple_sum.currentText(),
                                                        bw_limit_20mhz=self.bw_limit.isChecked())
        except ProjectError as e:
            self._say(f"Not applied: {e}", bar=True)
            return
        win._remerge()
        win.dirty = True
        win._update_title()
        self.refresh()

    # ------------------------------------------------------------------ table
    def _fill_table(self):
        result = self.result
        self.header.setText(header_html(result, self.why))
        self.export_button.setEnabled(result is not None and bool(result.rows))
        keep = self.table.currentRow()
        self.table.blockSignals(True)
        if result is None:
            self.table.setRowCount(0)
            self.notes.setPlainText("")
            self.table.blockSignals(False)
            self._row_selected(-1)
            return
        rows = result_rows(result)
        self.table.setRowCount(len(rows))
        for i, (texts, row) in enumerate(zip(rows, result.rows)):
            tips = (["", "", ""] + [part_cell(row, k)[1] for k in TOLERANCE_PARTS]
                    + [check_cell(row.checks[k])[1] if k in row.checks else "" for k in TOLERANCE_CHECKS]
                    + ["", texts[-1]])
            for c, (text, tip) in enumerate(zip(texts, tips)):
                item = QTableWidgetItem(text)
                if tip:
                    item.setToolTip(tip)
                if text.startswith("missing"):
                    item.setBackground(QBrush(MISSING_BG))
                if text.startswith("FAIL"):
                    item.setForeground(QBrush(QColor(FAIL_COLOR)))
                elif text.startswith("NOT JUDGED"):
                    item.setForeground(QBrush(QColor(AMBER)))
                self.table.setItem(i, c, item)
        self.notes.setPlainText(notes_text(result))
        if 0 <= keep < len(rows):
            self.table.setCurrentCell(keep, 0)
        elif rows:
            self.table.setCurrentCell(0, 0)
        self.table.blockSignals(False)
        self._row_selected(self.table.currentRow())

    def _row_selected(self, r: int):
        """The Solve button offers the selected row's next missing solve."""
        win = self._win
        self._next = None
        if self.result is not None and 0 <= r < len(self.result.rows):
            fresh = [n for n, (_res, key) in win.noise_panel.results.items()
                     if key == self._noise_now()]
            self._next = needed_solve(self.result.rows[r], win.merged.project, win.tree is not None, fresh)
        if self._next is None:
            self.solve_button.setText("Solve")
            self.solve_button.setEnabled(False)
            self.solve_button.setToolTip("Nothing a solve would add to the selected load" if self.result
                                         else "Open a board or a project")
        else:
            self.solve_button.setText(self._next[2])
            self.solve_button.setEnabled(True)
            self.solve_button.setToolTip(f"{self._next[2]}, in its own tab (its estimate first)")

    def _noise_now(self):
        from coppernicus.app.noise_panel import project_key
        return project_key(self._win.merged.project)

    def solve_next(self) -> str:
        """Run the selected row's next missing solve through the tab that owns it."""
        win = self._win
        if self._next is None:
            return ""
        kind, target, text = self._next
        if kind == "tree":
            win.solve_tree()
        elif kind == "noise":
            win.noise_panel.solve_aggressor(target)
        else:
            win.ac_panel.solve_rail(target, transient=True)
        return text

    def go_to_row(self, r: int) -> str:
        """Show the row's load on the board: its rail, zoomed to its pads. Returns the status line."""
        win = self._win
        if self.result is None or not 0 <= r < len(self.result.rows) or win.board is None:
            return ""
        row = self.result.rows[r]
        from coppernicus.engine import EngineError
        from coppernicus.engine.tolerance import loads_by_name
        found = loads_by_name(win.merged.project).get(row.name)
        if found is None:
            return ""
        net, load = found
        if win.net != net:
            win.nets.select(net)
        rail = win.merged.project.rail(net)
        try:
            pads = win.board.pads(win._rail_nets(rail), load.ref, load.pins) if rail is not None else []
        except EngineError:
            pads = []
        text = f"{row.load} on {row.net}: {VERDICT_WORDS.get(row.verdict, row.verdict)}"
        if pads:
            x = sum(p.position[0] for p in pads) / len(pads)
            y = sum(p.position[1] for p in pads) / len(pads)
            QTimer.singleShot(0, lambda: win.view.zoom_to(x, y))
        else:
            text += " (its pads are not on the board)"
        win.statusBar().showMessage(text)
        return text

    # ------------------------------------------------------------------ export
    def export_files(self, folder) -> List[Path]:
        """The table as ``coppernicus tolerance`` writes it into ``folder``:
        ``<stem>_tolerance.csv`` and ``<stem>_tolerance_summary.json`` (a stale result's part
        is missing there too)."""
        from coppernicus.app.noise_panel import export_stem
        from coppernicus.tolerance import tolerance_summary, write_csv
        if self.result is None or not self.result.rows:
            self._say("Nothing to export: " + (self.why or "no load"), bar=True)
            return []
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        board = self._win.board
        stem = export_stem(self._win.project.path, board.name if board else "")
        csv_path = write_csv(self.result, folder / f"{stem}_tolerance.csv")
        doc = tolerance_summary(self.result, csv_path.as_posix())
        summary = folder / f"{stem}_tolerance_summary.json"
        doc["summary_file"] = summary.as_posix()
        summary.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        self._say(f"Wrote {csv_path.name} and {summary.name} in {folder}", bar=True)
        return [csv_path, summary]

    def _export_dialog(self):
        path = self._win.project.path
        start = Path(path).parent / "results" if path else Path.home()
        folder = QFileDialog.getExistingDirectory(self, "Export the tolerance stack-up to", str(start))
        if folder:
            self.export_files(folder)

    # ------------------------------------------------------------------ board and window
    def board_changed(self):
        """A board (or project) was opened: nothing of the previous one survives."""
        self.result, self.why = None, ""
        self._signature = None
        self._pairs = (None, {})
        self._say("")
        self.schedule_refresh()
