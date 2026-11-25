"""The Impedance tab: the AC PDN impedance |Z|(f) of a rail at its probes (KiPI ``kipi/ac``
through :mod:`coppernicus.engine.ac`).

An editor beside a plot. The editor holds the project's probes, capacitor models and ``ac:``
options (the sweep, each source's control loop bandwidth, each link's series inductance);
an edit changes only the field edited, so an untouched value is never rounded through its
cell, and nothing the project file would refuse is ever written. Solve first meshes the
rail as the solve will and shows the cost estimate and the memory estimate (asking before
it goes on when one factorization could exceed this machine's memory), then solves in a
worker with progress and Cancel. The plot is :func:`coppernicus.impedance.draw_impedance`
(the GUI never words the band or the verdict itself); the results table gives each probe's
worst point, ratio and verdict; CSV and PNG are the core's writers.

The project solved is the window's merged project (``window.merged.project``: the project
merged with the board's KiCad footprint fields, as ``coppernicus ac`` merges them); an edit
writes ``window.project`` and merges again (``window._remerge()``). The pure functions at the
top (form <-> model, estimate and stats text, results rows) need no window.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import textwrap
import traceback
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app.panels import without_tolerances
from coppernicus.impedance import ac_summary, ac_text, draw_impedance, hz_text, ohm_text, rail_verdict
from coppernicus.project import (
    AcOptions,
    CapModel,
    Link,
    Probe,
    Project,
    ProjectError,
    Source,
    check_ac,
    parse_pins,
    pins_text,
)

# ---------------------------------------------------------------------- numbers

# The quick preset (Coppernicus's choice; KiPI has none): the copper solved at this many
# frequencies per decade instead of KiPI's 20 (kipi/ac/model.py:242), as KiPI's own decoupling
# and ferrite validation boards are (examples/validation/make_boards.py:406, :447), which its
# tests/accuracy/test_ac_accuracy.py holds to its tolerances. Applied only when pressed.
QUICK_FIELD_POINTS = 10
KIPI_FIELD_POINTS = AcOptions().field_points_per_decade       # KiPI's default (AcOptions copies it)

_SI = {"p": -12, "n": -9, "u": -6, "µ": -6, "μ": -6, "m": -3, "k": 3, "K": 3, "M": 6, "G": 9}
_SI_RE = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([pnuµμmkKMG]?)$")
_PREFIXES = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"),
             (1e-12, "p"))
SI_TIP = "SI prefixes work: p n u (µ) m k M G, so 50m = 0.05 and 10M = 1e7 (m is milli, M is mega)"


def parse_si(text: Optional[str]) -> Optional[float]:
    """A number typed in a cell: ``"50m"`` → 0.05, ``"10M"`` → 1e7, ``"3,3"`` → 3.3; ``None``
    when empty. Raises ValueError for anything else."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None
    m = _SI_RE.match(t)
    if not m:
        raise ValueError(f"{text.strip()!r} is not a number ({SI_TIP})")
    number, prefix = m.groups()
    if not prefix:
        return float(number)
    if "e" in number.lower():
        return float(number) * 10.0 ** _SI[prefix]
    return float(f"{number}e{_SI[prefix]}")          # "33m" is 0.033 exactly as typed


def si_text(x: Optional[float]) -> str:
    """A number as a cell shows it, 4 significant digits with an SI prefix ("" for None)."""
    if x is None:
        return ""
    if x == 0:
        return "0"
    scale, prefix = _PREFIXES[-1]
    for s, p in _PREFIXES:
        if abs(x) >= s * 0.9995:
            scale, prefix = s, p
            break
    return f"{x / scale:.4g}{prefix}"


def parse_mask(text: str) -> List[Tuple[float, float]]:
    """A target mask typed as ``"Hz Ω; Hz Ω; …"`` (``"100k 50m; 10M 50m"``)."""
    out = []
    for part in (p.strip() for p in (text or "").split(";")):
        if not part:
            continue
        words = part.split()
        if len(words) != 2:
            raise ValueError(f"a mask is Hz Ω pairs separated by ';' (100k 50m; 10M 50m), not {part!r}")
        out.append((parse_si(words[0]), parse_si(words[1])))
    return out


def mask_text(points: Sequence[Tuple[float, float]]) -> str:
    return "; ".join(f"{si_text(f)} {si_text(z)}" for f, z in points)


# ---------------------------------------------------------------------- probe and model rows

PROBE_COLUMNS = [("Name", "name"), ("Part", "ref"), ("Net", "net"), ("Pins", "pins"),
                 ("Ground part", "ground_ref"), ("Ground net", "ground_net"), ("Ground pins", "ground_pins"),
                 ("Target (Ω)", "target_ohm"), ("Up to (Hz)", "target_hz"),
                 ("Mask (Hz Ω; …)", "target_points"), ("AC tolerance (V)", "ac_tolerance_v"),
                 ("Step (A)", "step_a"), ("Slew (A/µs)", "slew_a_per_us")]
PROBE_TIPS = {
    "name": "Unique in the project", "ref": "The part whose supply pins the probe looks into",
    "net": "The net of the supply pins", "pins": "Pad numbers, comma separated; 'all' = every pin on the net",
    "ground_ref": "Empty: the probe's own part", "ground_net": "The net of the ground pins",
    "ground_pins": "Pad numbers, comma separated; 'all' = every pin on the ground net",
    "target_ohm": "Flat target up to 'Up to' (the whole sweep when empty). " + SI_TIP,
    "target_hz": "Where the flat target ends. " + SI_TIP,
    "target_points": "A mask instead of a flat target: Hz Ω corners joined on the log-log plot",
    "ac_tolerance_v": "With Step and Slew and no Target: KiPI's computed target Z = V_ac/I_step "
                      "up to 1/(π·t_rise), t_rise = I_step/slew",
    "step_a": "The load step, A", "slew_a_per_us": "The load step's slew rate, A/µs"}
_PROBE_TEXT = ("name", "ref", "net", "ground_ref", "ground_net")
_PROBE_NUMBERS = ("target_ohm", "target_hz", "ac_tolerance_v", "step_a", "slew_a_per_us")

CAP_COLUMNS = [("Name", "name"), ("C (F)", "capacitance_f"), ("ESL (H)", "esl_h"), ("ESR (Ω)", "esr_ohm"),
               ("Parts", "refs"), ("Rated (V)", "rated_v"), ("Kept at rated (%)", "kept_at_rated_pct"),
               ("Dielectric", "dielectric")]
CAP_TIPS = {
    "name": "Unique in the project", "capacitance_f": "Capacitance; empty = not entered. " + SI_TIP,
    "esl_h": "Mounted ESL; empty = not entered (there is no default: the solve asks). " + SI_TIP,
    "esr_ohm": "ESR; empty = not entered (there is no default: the solve asks). " + SI_TIP,
    "refs": "The parts using this model (refs C<number>), comma separated; or Pick parts",
    "rated_v": "With a voltage rating, the capacitance is derated for DC bias (not for C0G)",
    "kept_at_rated_pct": "% of the capacitance kept at the rated voltage; empty: KiPI's typical "
                         "class II value",
    "dielectric": "C0G (no DC bias), X5R, X7R, ...: the typical temperature curve when the copper "
                  "temperature is not 20 °C"}
_CAP_VALUES = ("capacitance_f", "esl_h", "esr_ohm")
DRAFT_PROBE = ("draft: a probe needs a name, a part and the net of its supply pins before it is "
               "saved and solved")
DRAFT_CAP = "draft: a capacitor model needs a name before it is saved and solved"


def probe_cell(p: Probe, key: str) -> str:
    """What the probe table shows in column ``key``."""
    v = getattr(p, key)
    if key == "pins":
        return pins_text(v)
    if key == "ground_pins":
        return pins_text(v) if p.ground_net else ""
    if key == "target_points":
        return mask_text(v)
    if key in _PROBE_NUMBERS:
        return si_text(v)
    return str(v)


def is_draft(p: Probe) -> bool:
    """A probe row not yet complete: no name, part or net (kept in the table, not written)."""
    return not (p.name and p.ref and p.net)


def _header(columns, key: str) -> str:
    return next(h for h, k in columns if k == key)


def with_probe_cell(p: Probe, key: str, text: str) -> Probe:
    """A copy of ``p`` with column ``key`` read from ``text`` (every other field as it was).
    Raises ValueError, with the project file's reason, for a value the file would refuse."""
    try:
        if key in _PROBE_TEXT:
            value: Any = text.strip()
        elif key in ("pins", "ground_pins"):
            value = parse_pins(text)
        elif key == "target_points":
            value = parse_mask(text)
        else:
            value = parse_si(text)
    except (ValueError, ProjectError) as e:
        raise ValueError(f"{_header(PROBE_COLUMNS, key)}: {e}") from None
    q = copy.deepcopy(p)
    setattr(q, key, value)
    if key == "target_ohm" and value is None:
        q.target_hz = None                 # the file takes no target_hz without target_ohm
    if key == "ground_net" and not value:
        q.ground_ref, q.ground_pins = "", None     # the file keeps ground pins only with their net
    if key in ("ground_ref", "ground_pins") and value and not q.ground_net:
        raise ValueError(f"{_header(PROBE_COLUMNS, key)}: set the ground net first (Ground net): "
                         "without it the ground pins are not saved")
    # A draft is checked for its values: its missing name, part or net stand in.
    check_ac([dataclasses.replace(q, name=q.name or "draft", ref=q.ref or "?", net=q.net or "?")], [])
    return q


def edit_note(old: Probe, new: Probe, key: str) -> str:
    """What else an edit of column ``key`` changed (clearing the target clears its "Up to",
    clearing the ground net its part and pins), for the status line; "" when nothing."""
    other = [h for h, k in PROBE_COLUMNS if k != key and getattr(old, k) != getattr(new, k)]
    return (f"{' and '.join(other)} cleared with {_header(PROBE_COLUMNS, key)}: the project file "
            "keeps them only with it") if other else ""


def _named_probe(p: Probe) -> Probe:
    return dataclasses.replace(p, ref=p.ref or "?", net=p.net or "?")


def probe_row_conflict(rows: Sequence[Probe], r: int, q: Probe) -> Optional[str]:
    """The project file's reason when row ``r``, edited to ``q``, conflicts with another row
    (a name in use, drafts included); None. Such an edit is refused: written, it would drop a
    saved probe at the next save."""
    if not q.name:
        return None
    for i, x in enumerate(rows):
        if i != r and x.name:
            try:
                check_ac([_named_probe(x), _named_probe(q)], [])
            except ProjectError as e:
                return str(e)
    return None


def cap_row_conflict(rows: Sequence[CapModel], r: int, q: CapModel) -> Optional[str]:
    """As :func:`probe_row_conflict` for capacitor models: a name in use, or a part another
    model has."""
    if not q.name:
        return None
    for i, x in enumerate(rows):
        if i != r and x.name:
            try:
                check_ac([], [x, q])
            except ProjectError as e:
                return str(e)
    return None


def usable_probes(rows: Sequence[Probe]) -> Tuple[List[Probe], Dict[int, str]]:
    """(the probes to write, {row: why it is not written}): drafts and a second probe of the
    same name stay in the table only."""
    out, problems, seen = [], {}, set()
    for i, p in enumerate(rows):
        if is_draft(p):
            problems[i] = DRAFT_PROBE
        elif p.name in seen:
            problems[i] = f"probe names must be unique: {p.name} is used above, so this row is not saved"
        else:
            seen.add(p.name)
            out.append(p)
    check_ac(out, [])
    return out, problems


def cap_cell(m: CapModel, key: str) -> str:
    """What the capacitor model table shows in column ``key`` (0 = not entered: empty)."""
    v = getattr(m, key)
    if key in _CAP_VALUES:
        return si_text(v) if v else ""
    if key == "refs":
        return ", ".join(v)
    if key == "rated_v":
        return si_text(v)
    if key == "kept_at_rated_pct":
        return "" if v is None else f"{v:g}"
    return str(v)


def with_cap_cell(m: CapModel, key: str, text: str) -> CapModel:
    """A copy of ``m`` with column ``key`` read from ``text``; ValueError as
    :func:`with_probe_cell`."""
    try:
        if key in ("name", "dielectric"):
            value: Any = text.strip()
        elif key == "refs":
            value = [r for r in re.split(r"[,\s]+", text.strip()) if r]
        elif key in _CAP_VALUES:
            value = parse_si(text) or 0.0
        else:
            value = parse_si(text)
    except ValueError as e:
        raise ValueError(f"{_header(CAP_COLUMNS, key)}: {e}") from None
    q = copy.deepcopy(m)
    setattr(q, key, value)
    check_ac([], [dataclasses.replace(q, name=q.name or "draft")])
    return q


def usable_caps(rows: Sequence[CapModel]) -> Tuple[List[CapModel], Dict[int, str]]:
    """(the models to write, {row: why it is not written}): drafts, a second model of the same
    name, and a model naming a part another model above already has."""
    out, problems, names, owner = [], {}, set(), {}
    for i, m in enumerate(rows):
        twice = [r for r in m.refs if r in owner]
        if not m.name:
            problems[i] = DRAFT_CAP
        elif m.name in names:
            problems[i] = f"capacitor model names must be unique: {m.name} is used above"
        elif twice:
            problems[i] = (f"{', '.join(twice)} already in model {owner[twice[0]]}: a part has one "
                           "model, so this row is not saved")
        else:
            names.add(m.name)
            owner.update({r: m.name for r in m.refs})
            out.append(m)
    check_ac([], out)
    return out, problems


# ---------------------------------------------------------------------- rails, bandwidths, links

def solvable_rails(project: Project) -> List[str]:
    """The rails an AC solve can start from without the engine: those with a source, then the
    nets of probes not among them (the engine solves a net behind a link with the rail
    feeding it)."""
    out = [r.net for r in project.rails if r.sources]
    for p in project.probes:
        if p.net and p.net not in out:
            out.append(p.net)
    return out


def rail_source(project: Project, net: str) -> Optional[Source]:
    """The source of ``net``'s rail, or of the nearest rail its links join it to; None."""
    seen, queue = {net}, [net]
    while queue:
        n = queue.pop(0)
        rail = project.rail(n)
        if rail is not None and rail.sources:
            return rail.sources[0]
        for k in project.links:
            for a, b in ((k.net_a, k.net_b), (k.net_b, k.net_a)):
                if a == n and b not in seen:
                    seen.add(b)
                    queue.append(b)
    return None


def _key(values: Dict[str, float], names: Sequence[str]) -> str:
    return next((k for k in names if k in values), names[0])


def bandwidth_of(ac: AcOptions, source: Source) -> Optional[float]:
    """The source's control loop bandwidth as the engine reads it (its name, else its ref)."""
    return ac.bandwidth_hz.get(source.label, ac.bandwidth_hz.get(source.ref))


def set_bandwidth(ac: AcOptions, source: Source, hz: Optional[float]) -> None:
    """Set (None: remove) the bandwidth under the key already used, else the source's name."""
    key = _key(ac.bandwidth_hz, [source.label, source.ref])
    if hz is None:
        ac.bandwidth_hz.pop(key, None)
    else:
        ac.bandwidth_hz[key] = float(hz)


def link_esl_of(ac: AcOptions, link: Link) -> Optional[float]:
    return ac.link_esl_h.get(link.ref, ac.link_esl_h.get(link.name))


def set_link_esl(ac: AcOptions, link: Link, henry: Optional[float]) -> None:
    """Set (None: remove) a link's series inductance under the key already used, else its ref."""
    key = _key(ac.link_esl_h, [link.ref, link.name])
    if henry is None:
        ac.link_esl_h.pop(key, None)
    else:
        ac.link_esl_h[key] = float(henry)


def quick_preset(ac: AcOptions) -> AcOptions:
    """``ac`` with the copper solved at QUICK_FIELD_POINTS frequencies per decade. Refused
    when the sweep has no more points per decade than that: KiPI would then solve every point
    (kipi/ac/model.py:287-288), which is slower, not quicker."""
    if QUICK_FIELD_POINTS >= ac.points_per_decade:
        raise ValueError(f"the quick preset solves the copper at {QUICK_FIELD_POINTS} frequencies per "
                         f"decade, which must be below the sweep's points per decade "
                         f"({ac.points_per_decade}); KiPI would solve every point")
    return dataclasses.replace(ac, field_points_per_decade=QUICK_FIELD_POINTS,
                               bandwidth_hz=dict(ac.bandwidth_hz), link_esl_h=dict(ac.link_esl_h))


# ---------------------------------------------------------------------- estimate, stats, rows

def estimate_text(cost, ram) -> str:
    """The pre-solve estimate: the cost (triangles × ports, frequencies, the time scaled from
    the last solve) and the memory (:func:`coppernicus.engine.ac.ram_estimate`)."""
    risk = "It could exceed this machine's memory. " if ram.risk else ""
    return f"Before solving: {cost.text}. {risk}{ram.text}"


def may_start(ram, answered_yes: bool) -> bool:
    """A solve starts unless one factorization could exceed the memory and the user did not
    answer Yes."""
    return (not ram.risk) or answered_yes


def stats_text(st: Dict[str, Any]) -> str:
    """One line of a solve's stats, with its memory peak and whether this solve raised it."""
    parts = [f"solved in {float(st.get('seconds', 0.0)):.1f} s",
             f"{int(st.get('triangles', 0)):,} triangles", f"{int(st.get('unknowns', 0)):,} unknowns",
             f"{int(st.get('ports', 0))} ports ({st.get('method', '')})",
             f"copper solved at {int(st.get('field_frequencies', 0))} frequencies"]
    mb = st.get("memory_peak_mb")
    if mb is None:
        parts.append("memory peak cannot be read on this system")
    elif st.get("memory_peak_raised"):
        parts.append(f"memory peak {mb:.0f} MB (this solve raised the process's peak)")
    else:
        parts.append(f"memory peak {mb:.0f} MB: the process's peak so far; this solve stayed at or "
                     "below it")
    return " · ".join(parts)


RESULT_HEADERS = ("Probe", "Net", "|Z| min", "|Z| max", "Worst |Z|", "at", "÷ target", "Target",
                  "Verdict", "Not judged because")


def _target_text(p) -> str:
    if p.target_points:
        return f"mask, {len(p.target_points)} points"
    if p.target_ohm:
        return (f"{ohm_text(p.target_ohm)} up to {hz_text(p.target_hz)}" if p.target_hz
                else f"{ohm_text(p.target_ohm)} over the sweep")
    return "-"


def result_rows(r) -> List[Tuple[str, ...]]:
    """One row per probe of an ImpedanceResult (RESULT_HEADERS)."""
    rows = []
    for p in r.probes:
        mag = p.mag_ohm
        verdict = ("FAIL" if p.passed is False else "pass" if p.passed
                   else "not judged" if p.has_target else "no target (ranked)")
        rows.append((p.name, p.net, ohm_text(float(mag.min())), ohm_text(float(mag.max())),
                     ohm_text(p.worst_z_ohm), hz_text(p.worst_hz),
                     f"{p.worst_ratio:.3g}" if p.worst_ratio is not None else "-",
                     _target_text(p), verdict, p.not_judged))
    return rows


def _project_key(project: Project) -> Dict[str, Any]:
    """What a solve depends on, to tell when a result no longer matches the project: not the
    board's path (a save rewrites it; another board clears the results), the nets marked as
    rails, the rails' DC limits, the aggressors, victims and noise options, or the tolerances,
    entered parts and accuracies (no AC solve reads them). Taken of the merged project, what is
    solved: a project rail ignored for the KiCad fields' changes nothing."""
    d = without_tolerances(project.to_dict())
    for key in ("marked_rails", "board", "aggressors", "victims", "noise"):
        d.pop(key, None)
    for rail in d.get("rails", []):
        rail.pop("limits", None)              # DC limits: no AC solve reads them
    return d


LAST_SOLVE_SETTING = "ac/last_solve"     # the last AC solve's size and time (QSettings), for the estimate


def _settings() -> QSettings:
    from coppernicus.app import APP_NAME
    return QSettings(QSettings.defaultFormat(), QSettings.UserScope, APP_NAME, APP_NAME)


def export_name(project_path, board_name: str, rail: str) -> str:
    """The CSV's file name as ``coppernicus ac`` writes it: the project file's stem (its
    ``.coppernicus`` too), else the board's, then the rail (the PNG: same name, .png)."""
    from coppernicus.maps import safe_file_name, safe_name
    stem = Path(project_path).stem if project_path else ((board_name or "").split(".")[0] or "coppernicus")
    return f"{safe_file_name(stem)}_{safe_name(rail)}_impedance.csv"


LEGEND_FONT = 6.5


def draw_result(fig, r) -> None:
    """The result drawn by :func:`coppernicus.impedance.draw_impedance` (its labels and title,
    never reworded), the title and the legend's labels wrapped so a narrow column cuts off
    neither."""
    fig.clear()
    ax = fig.add_subplot()
    draw_impedance(ax, r)
    ax.title.set_wrap(True)
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        width = max(16, int(fig.get_figwidth() * 72 * 0.45 / (0.55 * LEGEND_FONT)))   # ≤ ~half the width
        ax.legend(handles, [textwrap.fill(t, width) for t in labels], fontsize=LEGEND_FONT, loc="best")


# ---------------------------------------------------------------------- worker

class AcWorker(QThread):
    """Phase "size": the rail meshed as the solve will mesh it
    (:func:`coppernicus.engine.ac.ac_size`); phase "solve": the AC solve. Both on a copy of the
    window's merged project (``merged``, a :class:`coppernicus.board_fields.Merged`), whose
    notes and warnings the result gets. KiPI calls the progress hooks from its own threads:
    they reach the panel as queued signals."""
    message = Signal(str)
    fraction = Signal(object)        # 0..1 per field frequency, None while meshing
    sized = Signal(object)           # AcSize
    succeeded = Signal(object)       # ImpedanceResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, board, merged, rail: str, phase: str, parent=None):
        super().__init__(parent)
        self._board, self.merged, self.rail, self.phase = board, merged, rail, phase
        self._cancel = False
        self.generation = 0

    def cancel(self):
        self._cancel = True

    def run(self):
        from coppernicus.engine import Cancelled, EngineError
        from coppernicus.engine.ac import ac_size, solve_ac_rail
        try:
            if self.phase == "size":
                size = ac_size(self._board, self.merged.project, self.rail, progress=self.message.emit,
                               cancelled=lambda: self._cancel)
                if self._cancel:
                    raise Cancelled("cancelled")
                self.sized.emit(size)
            else:
                res = solve_ac_rail(self._board, self.merged.project, self.rail, progress=self.message.emit,
                                    cancelled=lambda: self._cancel, fraction=self.fraction.emit)
                self.merged.annotate(res)
                self.succeeded.emit(res)
        except Cancelled:
            self.cancelled.emit()
        except EngineError as e:
            self.failed.emit(str(e))
        except Exception:                                   # noqa: BLE001 - shown to the user
            self.failed.emit("Unexpected error:\n" + traceback.format_exc())


# ---------------------------------------------------------------------- widgets

def _table(headers, tips=None) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.setMinimumHeight(60)
    for c, tip in enumerate(tips or []):
        t.horizontalHeaderItem(c).setToolTip(tip)
    return t


PROBLEM_BG, DRAFT_BG, ERROR_BG = QColor("#fdecea"), QColor("#f0f0f0"), QColor("#f8c9c4")
CHART_SURFACE = "#fcfcfb"


class AcPanel(QWidget):
    """The Impedance results tab (see the module docstring)."""
    estimated = Signal(str)          # the estimate, shown before the solve starts

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self.results: Dict[str, Tuple[Any, Dict[str, Any]]] = {}   # rail -> (result, project it solved)
        self.worker: Optional[AcWorker] = None
        self._busy = False
        self._cancel_requested = False             # Cancel pressed: no phase starts after it
        self._generation = 0
        self._loading = False
        self._probe_rows: List[Probe] = []
        self._cap_rows: List[CapModel] = []
        self.last_estimate = ""
        self._shown: Dict[str, str] = {}      # what each line edit shows: a commit of it unchanged is no edit
        self._build()
        window.view.pad_picked.connect(self._pad_picked)
        window.rail_panel.pick_changed.connect(lambda kind: kind and self._leave_pick())
        window.measure.toggled.connect(lambda on: on and self._leave_pick())
        window.rail_panel.rail_changed.connect(self._setup_changed)
        window.rail_panel.settings_changed.connect(self._check_stale)
        self.load_project()

    # ------------------------------------------------------------------ build
    def _build(self):
        # Editor.
        self.rail_box = QComboBox()
        self.rail_box.setToolTip("The rail to solve: rails with a source, then the nets of probes "
                                 "(a net behind a link is solved with the rail feeding it)")
        self.rail_box.setMinimumContentsLength(8)
        self.bandwidth = QLineEdit()
        self.bandwidth.setPlaceholderText("not given")
        self.bandwidth.setMaximumWidth(90)
        self.source_label = QLabel()

        self.probes = _table([h for h, _ in PROBE_COLUMNS], [PROBE_TIPS[k] for _, k in PROBE_COLUMNS])
        self.pick_supply = QPushButton("Pick supply")
        self.pick_ground = QPushButton("Pick ground")
        add_p, rem_p = QPushButton("Add"), QPushButton("Remove")
        pb = QHBoxLayout()
        for w in (add_p, rem_p, self.pick_supply, self.pick_ground):
            pb.addWidget(w)
        pb.addStretch(1)
        probe_tab = QWidget()
        pl = QVBoxLayout(probe_tab)
        pl.setContentsMargins(2, 2, 2, 2)
        pl.addLayout(pb)
        pl.addWidget(self.probes, 1)

        self.caps = _table([h for h, _ in CAP_COLUMNS], [CAP_TIPS[k] for _, k in CAP_COLUMNS])
        self.pick_parts = QPushButton("Pick parts")
        add_c, rem_c = QPushButton("Add"), QPushButton("Remove")
        from_values = QPushButton("Models from values")
        from_values.setToolTip("One model per capacitance value of the rail's capacitors that have no "
                               "model yet, C read from the value; ESL and ESR left empty (the solve "
                               "asks for them)")
        imp, exp = QPushButton("Import…"), QPushButton("Export…")
        imp.setToolTip("A KiPI capacitor model file (kind: kipi-capacitor-model)")
        exp.setToolTip("The selected model as a KiPI capacitor model file (without its parts)")
        cap_tab = QWidget()
        cl = QVBoxLayout(cap_tab)
        cl.setContentsMargins(2, 2, 2, 2)
        for buttons in ((add_c, rem_c, self.pick_parts), (from_values, imp, exp)):   # two rows: narrow
            cb = QHBoxLayout()
            for w in buttons:
                cb.addWidget(w)
            cb.addStretch(1)
            cl.addLayout(cb)
        cl.addWidget(self.caps, 1)

        self._pick_buttons = {"supply": self.pick_supply, "ground": self.pick_ground,
                              "parts": self.pick_parts}
        for b in self._pick_buttons.values():
            b.setCheckable(True)
            b.setToolTip("Click pads in the board view (any net; the shown layer first). Esc or a "
                         "second press stops picking.")

        self.opt: Dict[str, QWidget] = {}
        form = QFormLayout()
        for key, label in (("f_start_hz", "Start (Hz)"), ("f_stop_hz", "Stop (Hz)")):
            self.opt[key] = QLineEdit()
            self.opt[key].setToolTip(SI_TIP)
            form.addRow(label, self.opt[key])
        for key, low in (("points_per_decade", 1), ("field_points_per_decade", 0)):
            spin = QSpinBox()
            spin.setRange(low, 2000)
            spin.setKeyboardTracking(False)
            self.opt[key] = spin
        self.opt["field_points_per_decade"].setSpecialValueText("every point")
        self.opt["field_points_per_decade"].setToolTip(
            "The copper is solved at this many frequencies per decade and interpolated between "
            f"them (KiPI's default {KIPI_FIELD_POINTS}); the run time grows with it")
        form.addRow("Points / decade", self.opt["points_per_decade"])
        self.quick = QPushButton(f"Quick ({QUICK_FIELD_POINTS})")
        self.quick.setToolTip(f"Solve the copper at {QUICK_FIELD_POINTS} frequencies per decade instead "
                              f"of KiPI's {KIPI_FIELD_POINTS}, as KiPI's own decoupling and ferrite "
                              "validation boards. Set here, visible and saved: never applied by itself.")
        kipi_default = QPushButton(f"KiPI's {KIPI_FIELD_POINTS}")
        kipi_default.setToolTip(f"Back to KiPI's default, {KIPI_FIELD_POINTS} copper solves per decade")
        form.addRow("Copper solves / decade", self.opt["field_points_per_decade"])
        row = QHBoxLayout()
        row.addWidget(self.quick)
        row.addWidget(kipi_default)
        row.addStretch(1)
        form.addRow("", row)
        for key, label, tip in (("max_edge_mm", "AC mesh edge (mm)", "The AC mesh's largest edge (its "
                                 "own; settings.max_edge_mm is DC's): a coarser one needs less memory"),
                                ("loss_tangent", "Loss tangent", "Of the dielectric (KiPI's default 0.02)"),
                                ("default_er", "Default εr", "Used where the stackup gives no εr")):
            self.opt[key] = QLineEdit()
            self.opt[key].setToolTip(tip)
            form.addRow(label, self.opt[key])
        self.links = _table(["Link", "Nets", "Series L (H)"])
        self.links.setToolTip("A link's series inductance (ac.link_esl_h); empty: a plain resistance "
                              "in AC. " + SI_TIP)
        sweep_tab = QWidget()
        sl = QVBoxLayout(sweep_tab)
        sl.setContentsMargins(2, 2, 2, 2)
        sl.addLayout(form)
        sl.addWidget(QLabel("Links (series inductance)"))
        sl.addWidget(self.links, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(probe_tab, "Probes")
        self.tabs.addTab(cap_tab, "Capacitor models")
        self.tabs.addTab(sweep_tab, "Sweep")
        self.solve_button = QPushButton("Solve impedance")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(14)
        top = QHBoxLayout()                       # two short rows: the editor fits a narrow column
        top.addWidget(QLabel("Rail"))
        top.addWidget(self.rail_box, 1)
        top.addWidget(self.solve_button)
        top.addWidget(self.cancel_button)
        run = QHBoxLayout()
        run.addWidget(self.source_label)
        run.addWidget(self.bandwidth)
        run.addWidget(self.progress, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.estimate = QLabel()                  # the pre-solve estimate, kept through the solve
        self.estimate.setWordWrap(True)
        self.estimate.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.estimate.setStyleSheet("color: #52514e")
        # Editor: rail, Solve and progress on top (always in sight in a short dock), then the tabs.
        editor = QWidget()
        el = QVBoxLayout(editor)
        el.setContentsMargins(4, 4, 4, 4)
        el.addLayout(top)
        el.addLayout(run)
        el.addWidget(self.status)
        el.addWidget(self.estimate)
        el.addWidget(self.tabs, 1)
        scroll = QScrollArea()
        scroll.setWidget(editor)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        # Results: the plot with the dock's full height beside the verdict, rows, stats and notes.
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(6, 3), facecolor=CHART_SURFACE, layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumSize(160, 100)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.table = _table(RESULT_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMinimumHeight(40)
        self.export_button = QPushButton("Export CSV and PNG…")
        self.export_button.setEnabled(False)
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        head.addWidget(self.header, 1)
        head.addWidget(self.export_button, 0, Qt.AlignTop)
        rl.addLayout(head)
        rl.addWidget(self.table, 1)
        rl.addWidget(self.stats)
        rl.addWidget(self.notes, 1)
        results_scroll = QScrollArea()           # the dock is short on a laptop: scroll, never grow it
        results_scroll.setWidget(results)
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QScrollArea.NoFrame)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(scroll)
        split.addWidget(self.canvas)
        split.addWidget(results_scroll)
        for i, stretch in enumerate((2, 3, 2)):
            split.setStretchFactor(i, stretch)
        split.setSizes([430, 560, 300])           # the editor at its natural width, the plot widest
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

        # Wiring.
        self.rail_box.currentIndexChanged.connect(lambda *_: self._rail_shown())
        self.bandwidth.editingFinished.connect(self._bandwidth_edited)
        add_p.clicked.connect(self.add_probe)
        rem_p.clicked.connect(self._remove_probes)
        add_c.clicked.connect(self.add_model)
        rem_c.clicked.connect(self._remove_models)
        from_values.clicked.connect(self.models_from_values)
        imp.clicked.connect(self._import_dialog)
        exp.clicked.connect(self._export_model_dialog)
        self.probes.itemChanged.connect(self._probe_item_changed)
        self.caps.itemChanged.connect(self._cap_item_changed)
        self.links.itemChanged.connect(self._link_item_changed)
        for kind, b in self._pick_buttons.items():
            b.toggled.connect(lambda on, k=kind: self._pick_toggled(k, on))
        for key, w in self.opt.items():
            if isinstance(w, QSpinBox):
                w.valueChanged.connect(lambda _v, k=key: self._option_edited(k))
            else:
                w.editingFinished.connect(lambda k=key: self._option_edited(k))
        self.quick.clicked.connect(self.use_quick_preset)
        kipi_default.clicked.connect(lambda: self.opt["field_points_per_decade"].setValue(KIPI_FIELD_POINTS))
        self.solve_button.clicked.connect(self.solve)
        self.cancel_button.clicked.connect(self.cancel)
        self.export_button.clicked.connect(self._export_dialog)
        self._show_empty()

    # ------------------------------------------------------------------ project in and out
    @property
    def project(self) -> Project:
        """The user's project, the one edited and saved (never kept: opening a board replaces it)."""
        return self._win.project

    @property
    def merged_project(self) -> Project:
        """The project merged with the KiCad footprint fields: what is solved."""
        return self._win.merged.project

    def load_project(self):
        """Fill the editor from the window's project (after a board or project is opened)."""
        self._loading = True
        try:
            self._probe_rows = copy.deepcopy(self.project.probes)
            self._cap_rows = copy.deepcopy(self.project.capacitors)
            self._fill(self.probes, PROBE_COLUMNS, self._probe_rows, probe_cell)
            self._fill(self.caps, CAP_COLUMNS, self._cap_rows, cap_cell)
            self._fill_options()
            self._fill_links()
            self.refresh_rails()
        finally:
            self._loading = False

    def _fill(self, table, columns, rows, cell):
        table.blockSignals(True)
        table.setRowCount(0)
        for r, obj in enumerate(rows):
            table.insertRow(r)
            for c, (_h, key) in enumerate(columns):
                table.setItem(r, c, QTableWidgetItem(cell(obj, key)))
        table.blockSignals(False)

    def _fill_options(self):
        a = self.project.ac
        for key, w in self.opt.items():
            w.blockSignals(True)
            if isinstance(w, QSpinBox):
                w.setValue(int(getattr(a, key)))
            else:
                w.setText(si_text(getattr(a, key)) if key in ("f_start_hz", "f_stop_hz")
                          else f"{getattr(a, key):g}")
                self._shown[key] = w.text()
            w.blockSignals(False)

    def _fill_links(self):
        self.links.blockSignals(True)
        self.links.setRowCount(0)
        for r, k in enumerate(self.merged_project.links):         # the KiCad fields' links too
            self.links.insertRow(r)
            for c, text in enumerate((f"{k.name} ({k.ref})" if k.name != k.ref else k.ref,
                                      f"{k.net_a} ↔ {k.net_b}", si_text(link_esl_of(self.project.ac, k)))):
                item = QTableWidgetItem(text)
                if c < 2:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.links.setItem(r, c, item)
        self.links.blockSignals(False)

    def _edited(self):
        """After any edit written to the project: merged again with the KiCad fields (what the
        next solve takes), dirty, and every result checked again."""
        self._win._remerge()
        self._win.dirty = True
        self._win._update_title()
        self._check_stale()

    def _say(self, text: str, bar: bool = False):
        self.status.setText(text)
        if bar:
            self._win.statusBar().showMessage(text)

    # ------------------------------------------------------------------ probes
    def add_probe(self):
        self._probe_rows.append(Probe("", "", ""))
        self._fill(self.probes, PROBE_COLUMNS, self._probe_rows, probe_cell)
        self.probes.setCurrentCell(len(self._probe_rows) - 1, 0)
        self._write_probes()

    def _remove_probes(self):
        rows = sorted({i.row() for i in self.probes.selectedIndexes()}, reverse=True)
        if not rows and self._probe_rows:
            rows = [len(self._probe_rows) - 1]
        for r in rows:
            self._probe_rows.pop(r)
        self._fill(self.probes, PROBE_COLUMNS, self._probe_rows, probe_cell)
        self._write_probes()

    def _probe_item_changed(self, item: QTableWidgetItem):
        r, c = item.row(), item.column()
        if self._loading or r >= len(self._probe_rows):
            return
        key, old = PROBE_COLUMNS[c][1], self._probe_rows[r]
        try:
            q = with_probe_cell(old, key, item.text())
            why = probe_row_conflict(self._probe_rows, r, q)
            if why:
                raise ValueError(why)
        except ValueError as e:
            self._mark_cell(self.probes, item, str(e))
            self._say(f"Not applied: {e}")
            return
        self._probe_rows[r] = q
        self._refill_row(self.probes, PROBE_COLUMNS, r, q, probe_cell)
        self._write_probes()
        note = edit_note(old, q, key)
        if note:
            self._say(note)

    def _write_probes(self):
        probes, problems = usable_probes(self._probe_rows)
        self._flag_rows(self.probes, problems)
        if probes != self.project.probes:
            self.project.probes = copy.deepcopy(probes)
            self._edited()
            self.refresh_rails()
        if problems:
            self._say(" · ".join(f"Probe row {i + 1}: {why}" for i, why in sorted(problems.items())))

    # ------------------------------------------------------------------ capacitor models
    def add_model(self, model: Optional[CapModel] = None):
        self._cap_rows.append(model or CapModel(""))
        self._fill(self.caps, CAP_COLUMNS, self._cap_rows, cap_cell)
        self.caps.setCurrentCell(len(self._cap_rows) - 1, 0)
        self._write_caps()

    def _remove_models(self):
        rows = sorted({i.row() for i in self.caps.selectedIndexes()}, reverse=True)
        if not rows and self._cap_rows:
            rows = [len(self._cap_rows) - 1]
        for r in rows:
            self._cap_rows.pop(r)
        self._fill(self.caps, CAP_COLUMNS, self._cap_rows, cap_cell)
        self._write_caps()

    def _cap_item_changed(self, item: QTableWidgetItem):
        r, c = item.row(), item.column()
        if self._loading or r >= len(self._cap_rows):
            return
        key = CAP_COLUMNS[c][1]
        try:
            q = with_cap_cell(self._cap_rows[r], key, item.text())
            why = cap_row_conflict(self._cap_rows, r, q)
            if why:
                raise ValueError(why)
        except ValueError as e:
            self._mark_cell(self.caps, item, str(e))
            self._say(f"Not applied: {e}")
            return
        self._cap_rows[r] = q
        self._set_cell(self.caps, r, c, cap_cell(q, key))
        self._write_caps()

    def _write_caps(self):
        models, problems = usable_caps(self._cap_rows)
        self._flag_rows(self.caps, problems)
        if models != self.project.capacitors:
            self.project.capacitors = copy.deepcopy(models)
            self._edited()
        if problems:
            self._say(" · ".join(f"Model row {i + 1}: {why}" for i, why in sorted(problems.items())))

    def _unique_name(self, name: str) -> str:
        names = {m.name for m in self._cap_rows}
        out, k = name, 2
        while out in names:
            out, k = f"{name}_{k}", k + 1
        return out

    def models_from_values(self) -> str:
        """One model per distinct capacitance of the shown rail's capacitors (between its nets
        and its ground) that are in no model, C read from the value field; ESL and ESR empty."""
        board, rail = self._win.board, self.rail_box.currentText()
        if board is None or not rail:
            return self._report("Open a board and choose a rail first")
        nets, grounds = self._rail_and_ground_nets(rail)
        if not grounds:
            return self._report(f"{rail}: give its source or a probe ground pins first, so the "
                                "capacitors between the rail and its ground can be found")
        from coppernicus.engine.ac import capacitor_candidates
        used = {r for m in self._cap_rows for r in m.refs}
        by_value: Dict[float, List] = {}
        unread = []
        for c in capacitor_candidates(board, sorted(nets | grounds)):
            if c.ref in used or not (set(c.nets) & nets and set(c.nets) & grounds):
                continue
            if c.capacitance_f is None:
                unread.append(f"{c.ref} ({c.value or 'no value'})")
                continue
            by_value.setdefault(c.capacitance_f, []).append(c)
        for farads, parts in sorted(by_value.items()):
            name = self._unique_name(parts[0].value.strip() or si_text(farads) + "F")
            self._cap_rows.append(CapModel(name, farads, refs=sorted(c.ref for c in parts)))
        self._fill(self.caps, CAP_COLUMNS, self._cap_rows, cap_cell)
        self._write_caps()
        n = sum(len(v) for v in by_value.values())
        msg = (f"Added {len(by_value)} model(s) from the values of {n} capacitor(s) on {rail}: enter "
               "each one's ESL and ESR (the solve needs them)" if by_value
               else f"No capacitor on {rail} without a model")
        if unread:
            msg += f"; value not read as a capacitance: {', '.join(unread)}"
        return self._report(msg)

    def _rail_and_ground_nets(self, rail: str):
        """The rail's nets (joined by the project's links) and its ground nets (the source's,
        else its probes')."""
        p = self.merged_project
        nets, queue = {rail}, [rail]
        while queue:
            n = queue.pop(0)
            for k in p.links:
                for a, b in ((k.net_a, k.net_b), (k.net_b, k.net_a)):
                    if a == n and b not in nets:
                        nets.add(b)
                        queue.append(b)
        src = rail_source(p, rail)
        grounds = {src.ground_net} if src is not None and src.ground_net else set()
        grounds |= {q.ground_net for q in p.probes if q.net in nets and q.ground_net}
        return nets, grounds

    def import_cap_file(self, path) -> str:
        from coppernicus.engine import EngineError
        from coppernicus.engine.ac import read_cap_file
        try:
            model, warnings = read_cap_file(path)
        except EngineError as e:
            return self._report(f"Cannot import: {e}")
        model.name = self._unique_name(model.name)
        try:
            check_ac([], [model])
        except ProjectError as e:
            return self._report(f"Cannot import {Path(path).name}: {e}")
        self.add_model(model)
        return self._report(f"Imported capacitor model {model.name} from {Path(path).name}"
                            + "".join(f"; {w}" for w in warnings) + ": pick or type its parts")

    def export_cap_file(self, row: int, path) -> str:
        from coppernicus.engine.ac import write_cap_file
        if not 0 <= row < len(self._cap_rows):
            return self._report("Select the model to export")
        out = write_cap_file(path, self._cap_rows[row])
        return self._report(f"Wrote capacitor model {self._cap_rows[row].name} to {out}")

    def _import_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import a capacitor model", "",
                                              "KiPI capacitor model (*.yaml *.yml);;All files (*)")
        if path:
            self.import_cap_file(path)

    def _export_model_dialog(self):
        row = self.caps.currentRow()
        if not 0 <= row < len(self._cap_rows):
            self._report("Select the model to export")
            return
        name = self._cap_rows[row].name
        path, _ = QFileDialog.getSaveFileName(self, "Export a capacitor model", f"{name}.yaml",
                                              "KiPI capacitor model (*.yaml)")
        if path:
            self.export_cap_file(row, path)

    # ------------------------------------------------------------------ options, bandwidth, links
    def _option_edited(self, key: str):
        if self._loading:
            return
        w = self.opt[key]
        if not isinstance(w, QSpinBox) and w.text().strip() == self._shown.get(key):
            return                                # committed unchanged: the exact value stays
        try:
            value: Any = w.value() if isinstance(w, QSpinBox) else parse_si(w.text())
            if value is None:
                raise ValueError("give a number")
            new = dataclasses.replace(self.project.ac, **{key: value})
            check_ac([], [], new)
        except (ValueError, ProjectError) as e:
            self._say(f"Not applied: {key}: {e}")
            self._fill_options()
            return
        if getattr(self.project.ac, key) != value:
            setattr(self.project.ac, key, value)
            self._edited()
        self._fill_options()

    def use_quick_preset(self) -> str:
        """The quick preset, only on the user's press: the copper solved at QUICK_FIELD_POINTS
        frequencies per decade, set in the visible field (and saved with the project)."""
        try:
            quick_preset(self.project.ac)
        except ValueError as e:
            return self._report(f"Quick preset not applied: {e}")
        self.opt["field_points_per_decade"].setValue(QUICK_FIELD_POINTS)
        return self._report(f"Quick preset: the copper is solved at {QUICK_FIELD_POINTS} frequencies per "
                            f"decade instead of KiPI's {KIPI_FIELD_POINTS} (the result's assumptions say so)")

    def _source(self, rail: str) -> Optional[Source]:
        """The source whose bandwidth the rail's solve uses (a rail from the KiCad fields too)."""
        return rail_source(self.merged_project, rail)

    def _bandwidth_edited(self):
        src = self._source(self.rail_box.currentText())
        if src is None or self._loading or self.bandwidth.text().strip() == self._shown.get("bandwidth"):
            return
        try:
            hz = parse_si(self.bandwidth.text())
            new = copy.deepcopy(self.project.ac)
            set_bandwidth(new, src, hz)
            check_ac([], [], new)
        except (ValueError, ProjectError) as e:
            self._say(f"Not applied: bandwidth: {e}")
            self._rail_shown()
            return
        if new.bandwidth_hz != self.project.ac.bandwidth_hz:
            self.project.ac.bandwidth_hz = new.bandwidth_hz
            self._edited()
        self._rail_shown()

    def set_bandwidth_text(self, text: str):
        """Type ``text`` into the bandwidth field and commit it (for scripts)."""
        self.bandwidth.setText(text)
        self._bandwidth_edited()

    def _link_item_changed(self, item: QTableWidgetItem):
        r = item.row()
        links = self.merged_project.links
        if self._loading or item.column() != 2 or r >= len(links):
            return
        k = links[r]
        try:
            henry = parse_si(item.text())
            new = copy.deepcopy(self.project.ac)
            set_link_esl(new, k, henry)
            check_ac([], [], new)
        except (ValueError, ProjectError) as e:
            self._say(f"Not applied: series L of {k.name}: {e}")
            self._fill_links()
            return
        if new.link_esl_h != self.project.ac.link_esl_h:
            self.project.ac.link_esl_h = new.link_esl_h
            self._edited()
        self._fill_links()

    # ------------------------------------------------------------------ rails and results
    def refresh_rails(self):
        """The rail list (no engine call): the merged project's rails with a source (the
        KiCad fields' too), then the probes' nets."""
        keep = self.rail_box.currentText()
        rails = solvable_rails(self.merged_project)
        self.rail_box.blockSignals(True)
        self.rail_box.clear()
        self.rail_box.addItems(rails)
        i = self.rail_box.findText(keep)
        self.rail_box.setCurrentIndex(i if i >= 0 else (0 if rails else -1))
        self.rail_box.blockSignals(False)
        self._rail_shown()

    def _setup_changed(self):
        """The rail setup changed elsewhere (a source, a pad picked): the rail list follows,
        refilled only when it changed, and the shown result is checked again."""
        rails = solvable_rails(self.merged_project)
        if rails != [self.rail_box.itemText(i) for i in range(self.rail_box.count())]:
            self.refresh_rails()
        self._check_stale()

    def _rail_shown(self):
        rail = self.rail_box.currentText()
        src = self._source(rail) if rail else None
        self.bandwidth.blockSignals(True)
        self.bandwidth.setEnabled(src is not None)
        hz = bandwidth_of(self.project.ac, src) if src is not None else None
        self.bandwidth.setText(si_text(hz))
        self._shown["bandwidth"] = self.bandwidth.text()
        self.bandwidth.blockSignals(False)
        if src is None:
            self.source_label.setText("Bandwidth (Hz)")
            self.bandwidth.setToolTip("No source reaches this net in the project")
        else:
            self.source_label.setText(f"Bandwidth of {src.label} (Hz)")
            self.bandwidth.setToolTip(
                f"The control loop bandwidth of {src.label} (ac.bandwidth_hz). Empty: not given; "
                "the curve is shown, nothing is judged. " + SI_TIP)
        self.show_result()

    def _show_empty(self, text: str = "Solve a rail's impedance to see |Z|(f) at its probes"):
        self.figure.clear()
        ax = self.figure.add_subplot()
        ax.axis("off")
        ax.text(0.5, 0.5, text, ha="center", va="center", color="#898781", transform=ax.transAxes)
        self.canvas.draw_idle()
        self.header.setText("")
        self.table.setRowCount(0)
        self.stats.setText("")
        self.notes.clear()
        self.export_button.setEnabled(False)

    def show_result(self):
        """The shown rail's last result: plot, verdict line, rows, stats and notes."""
        entry = self.results.get(self.rail_box.currentText())
        if entry is None:
            self._show_empty()
            return
        r = entry[0]
        draw_result(self.figure, r)
        self.canvas.draw_idle()
        rows = result_rows(r)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if row[8] == "FAIL":
                    item.setForeground(QBrush(QColor("#c0392b")))
                self.table.setItem(i, c, item)
        self.stats.setText(stats_text(r.stats))
        self.notes.setPlainText("\n".join(line.strip("\n") for line in ac_text(ac_summary([r]))))
        self.export_button.setEnabled(True)
        self._check_stale()

    def _check_stale(self):
        """The verdict line of the shown result, flagged when the setup changed since it."""
        entry = self.results.get(self.rail_box.currentText())
        if entry is None:
            self.header.setText("")
            return
        r, solved = entry
        verdict = {"fail": "<span style='color:#c0392b'><b>FAIL</b></span>", "pass": "<b>pass</b>",
                   "not_judged": "<span style='color:#b9770e'><b>not judged</b></span>"}[rail_verdict(r)[0]]
        stale = ("<br><span style='color:#b9770e'><b>Setup changed since this solve</b>: solve "
                 "again</span>" if _project_key(self.project) != solved else "")
        self.header.setText(f"<b>{r.rail}</b> against {r.ground}: {verdict}{stale}")

    @property
    def stale(self) -> bool:
        entry = self.results.get(self.rail_box.currentText())
        return entry is not None and _project_key(self.project) != entry[1]

    def showEvent(self, event):
        super().showEvent(event)
        if not self._busy:
            self.refresh_rails()

    # ------------------------------------------------------------------ export
    def _export_start(self, rail: str) -> Path:
        """Where Export suggests the CSV: results/ by the project file (the home folder for an
        unsaved project), named as ``coppernicus ac`` names it."""
        path, board = self.project.path, self._win.board
        folder = Path(path).parent / "results" if path else Path.home()
        return folder / export_name(path, board.name if board else "", rail)

    def export_files(self, csv_path) -> Tuple[Path, Path]:
        """The shown result as the CLI writes it: the CSV and, beside it, the PNG."""
        from coppernicus.impedance import write_csv, write_png
        r = self.results[self.rail_box.currentText()][0]
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        out = write_csv(r, csv_path), write_png(r, csv_path.with_suffix(".png"))
        self._report(f"Wrote {out[0].name} and {out[1].name} in {out[0].parent}")
        return out

    def _export_dialog(self):
        rail = self.rail_box.currentText()
        if rail not in self.results:
            return
        start = self._export_start(rail)
        path, _ = QFileDialog.getSaveFileName(self, "Export the impedance (CSV, and the PNG beside it)",
                                              str(start), "CSV (*.csv)")
        if path:
            self.export_files(path if path.lower().endswith(".csv") else path + ".csv")

    def _report(self, text: str) -> str:
        self._say(text, bar=True)
        return text

    # ------------------------------------------------------------------ picking on the board
    @property
    def pick_kind(self) -> str:
        """"supply", "ground", "parts" or "" — where the pads clicked in the view go."""
        return next((k for k, b in self._pick_buttons.items() if b.isChecked()), "")

    def start_pick(self, kind: str):
        self._pick_buttons[kind].setChecked(True)

    def _pick_toggled(self, kind: str, on: bool):
        if on:
            for k, b in self._pick_buttons.items():
                if k != kind and b.isChecked():
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
            win = self._win
            win.rail_panel.set_pick("")                # one click mode at a time
            win.measure.setChecked(False)
            win.view.set_click_mode("pick")
            what = {"supply": "the probe's supply pins", "ground": "the probe's ground pins",
                    "parts": "the model's parts (a second click takes a part out)"}[kind]
            self._report(f"Click pads to add them to {what} · Esc to stop")
        elif not self.pick_kind:
            self.stop_pick()

    def _leave_pick(self):
        """Another click mode took the view: uncheck ours without touching the view."""
        for b in self._pick_buttons.values():
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)

    def stop_pick(self):
        """Esc: leave the AC pick modes (and the view's pick mode, if no rail pick has it)."""
        self._leave_pick()
        win = self._win
        if win.view.click_mode == "pick" and not win.rail_panel.pick_kind:
            win.view.set_click_mode(None)

    def _pad_picked(self, x: float, y: float) -> str:
        QTimer.singleShot(0, self._setup_changed)          # a rail pad picked changes the setup too
        kind, board = self.pick_kind, self._win.board
        if not kind or board is None:
            return ""
        lid = self._win.layer_box.currentData()
        hit = (board.pad_at(x, y, lid) if lid is not None else None) or board.pad_at(x, y)
        if hit is None:
            return self._report(f"No pad at ({x:.2f}, {y:.2f}) mm")
        if not hit.ref:
            return self._report(f"The pad at ({x:.2f}, {y:.2f}) mm belongs to no part")
        if kind == "parts":
            return self._pick_part(hit.ref)
        from coppernicus.app.picking import add_probe_pin
        r = self.probes.currentRow()
        if not self._probe_rows:
            self.add_probe()
            r = 0
        if not 0 <= r < len(self._probe_rows):
            return self._report("Select the probe row to pick into")
        p = copy.deepcopy(self._probe_rows[r])
        if not p.name:                       # a picked probe is named after its part, never twice
            names, name, k = {x.name for x in self._probe_rows}, hit.ref, 2
            while name in names:
                name, k = f"{hit.ref}_{k}", k + 1
            p.name = name
        msg = add_probe_pin(p, kind, hit.ref, hit.pin, hit.net)
        if p != self._probe_rows[r]:
            self._probe_rows[r] = p
            self._fill(self.probes, PROBE_COLUMNS, self._probe_rows, probe_cell)
            self.probes.setCurrentCell(r, 0)
            self._write_probes()
        why = usable_probes(self._probe_rows)[1].get(r)
        return self._report(f"{msg} · row {r + 1}: {why}" if why else msg)

    def _pick_part(self, ref: str) -> str:
        from coppernicus.app.picking import toggle_cap_ref
        r = self.caps.currentRow()
        if not self._cap_rows:
            return self._report("Add a capacitor model first (Add, or Models from values)")
        if not 0 <= r < len(self._cap_rows):
            return self._report("Select the capacitor model to pick parts into")
        models = copy.deepcopy(self._cap_rows)
        msg = toggle_cap_ref(models, r, ref)
        self._cap_rows = models
        self._fill(self.caps, CAP_COLUMNS, self._cap_rows, cap_cell)
        self.caps.setCurrentCell(r, 0)
        self._write_caps()
        return self._report(msg)

    # ------------------------------------------------------------------ cells
    def _refill_row(self, table, columns, r, obj, cell):
        for c, (_h, key) in enumerate(columns):
            self._set_cell(table, r, c, cell(obj, key))

    def _set_cell(self, table, r, c, text):
        table.blockSignals(True)
        item = table.item(r, c)
        item.setText(text)
        item.setBackground(QBrush())
        item.setToolTip("")
        table.blockSignals(False)

    def _mark_cell(self, table, item, why):
        table.blockSignals(True)
        item.setBackground(QBrush(ERROR_BG))
        item.setToolTip(f"Not applied: {why}")
        table.blockSignals(False)

    def _flag_rows(self, table, problems: Dict[int, str]):
        table.blockSignals(True)
        for r in range(table.rowCount()):
            why = problems.get(r, "")
            bg = QBrush(DRAFT_BG if why.startswith("draft") else PROBLEM_BG) if why else QBrush()
            for c in range(table.columnCount()):
                item = table.item(r, c)
                if item is not None and item.background().color() != ERROR_BG:
                    item.setBackground(bg)
                    item.setToolTip(why)
        table.blockSignals(False)

    # ------------------------------------------------------------------ solve
    @property
    def busy(self) -> bool:
        """True from Solve until the solve has ended (sizing, the question, the solve)."""
        return self._busy

    def _last_solve(self) -> Optional[Dict[str, float]]:
        try:
            text = _settings().value(LAST_SOLVE_SETTING, "")
            return json.loads(text) if text else None
        except (TypeError, ValueError):
            return None

    def solve(self):
        """Size the shown rail (KiPI's AC mesh), show the estimate, ask when the memory could
        be exceeded, then solve; all off the UI thread, with progress and Cancel."""
        win = self._win
        rail = self.rail_box.currentText()
        if win.board is None:
            self._report("Open a board first")
            return
        if self._busy:
            self._report("An impedance solve is already running")
            return
        if not rail:
            self._report("Add a probe on a rail with a source first")
            return
        if win._refuse_fields("Cannot solve the impedance"):    # a KiCad field cannot be used
            return
        self.stop_pick()
        self._busy, self._cancel_requested = True, False
        self.solve_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.last_estimate = ""
        self.estimate.setText("Estimating: meshing the rail as the AC solve will…")
        # What is solved: a copy of the merged project as it is now (the user may edit on).
        self._solving = (rail, _project_key(self.project), copy.deepcopy(win.merged))
        self._start(AcWorker(win.board, self._solving[2], rail, "size", parent=self))
        self._report(f"{rail}: meshing it as the AC solve will, for the estimate…")

    def add_solve_action(self, toolbar) -> QAction:
        """A "Solve impedance" action (Ctrl+I) on the window's toolbar: shows this tab, then
        solves its rail."""
        act = QAction("Solve impedance", self._win)
        act.setShortcut(QKeySequence("Ctrl+I"))
        act.setToolTip("Solve the AC impedance of the Impedance tab's rail (Ctrl+I)")
        act.triggered.connect(self.solve_shown)
        toolbar.addAction(act)
        return act

    def solve_shown(self):
        self._win.results_tabs.setCurrentWidget(self)
        self.solve()

    def _start(self, worker: AcWorker):
        worker.generation = self._generation
        worker.message.connect(self._message)
        worker.fraction.connect(self._fraction)
        worker.sized.connect(self._sized)
        worker.succeeded.connect(self._solved)
        worker.failed.connect(self._failed)
        worker.cancelled.connect(self._cancelled)
        worker.finished.connect(lambda w=worker: self._worker_finished(w))
        self.worker = worker
        worker.start()

    def _current(self) -> bool:
        w = self.sender()
        return w is self.worker and getattr(w, "generation", -1) == self._generation

    def _message(self, text: str):
        if self._current():
            self.status.setText(text)
            self._win.statusBar().showMessage(text)

    def _fraction(self, value):
        if not self._current():
            return
        if value is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(round(1000 * min(max(float(value), 0.0), 1.0))))

    def _sized(self, payload):
        if not self._current():
            return
        # The sizing thread is done: its end must not end the solve (a box's own event loop
        # can deliver it before the solve starts).
        self.worker, generation = None, self._generation
        if self._cancel_requested:
            self._cancel_done()
            return
        from coppernicus.engine.ac import cost_estimate, free_memory_bytes, ram_estimate, total_memory_bytes
        size = payload
        cost = cost_estimate(size.triangles, size.ports, size.field_frequencies, self._last_solve())
        ram = ram_estimate(size.unknowns, total_memory_bytes(), free_memory_bytes())
        self.last_estimate = estimate_text(cost, ram)
        self.estimate.setText(self.last_estimate)
        self.estimated.emit(self.last_estimate)
        yes = False
        if ram.risk:
            answer = QMessageBox.warning(
                self, "This solve could exceed the memory",
                f"{self.last_estimate}\n\nSolve anyway? It could make this computer swap; a coarser "
                "AC mesh (Sweep → AC mesh largest edge) lowers it.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            yes = answer == QMessageBox.Yes
        if generation != self._generation:          # another board was opened while asking
            return
        if self._cancel_requested:
            self._cancel_done()
            return
        if not may_start(ram, yes):
            self._report(f"Not solved: {size.rail} could exceed this machine's memory (see the estimate)")
            self._busy_off()
            return
        self._busy = True
        self.solve_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self._start(AcWorker(self._win.board, self._solving[2], size.rail, "solve", parent=self))

    def _solved(self, res):
        if not self._current():
            return
        _rail, solved, _merged = self._solving
        self.results[res.rail] = (res, solved)
        st = res.stats
        try:
            _settings().setValue(LAST_SOLVE_SETTING, json.dumps(
                {k: float(st[k]) for k in ("seconds", "field_seconds", "triangles", "ports",
                                           "field_frequencies")}))
        except (KeyError, TypeError, ValueError):
            pass
        if self.rail_box.findText(res.rail) < 0:
            self.refresh_rails()
        self.rail_box.setCurrentIndex(self.rail_box.findText(res.rail))
        self.show_result()
        verdict = {"fail": "FAIL", "pass": "pass", "not_judged": "not judged"}[rail_verdict(res)[0]]
        self._report(f"{res.rail} impedance solved in {st.get('seconds', 0.0):.1f} s: {verdict}")

    def _failed(self, message: str):
        if not self._current():
            return
        self._clear_estimating()
        self._report("The impedance could not be solved")
        QMessageBox.warning(self, "Cannot solve the impedance", message)

    def _cancelled(self):
        if self._current():
            self._clear_estimating()
            self._report("Impedance solve cancelled")

    def _cancel_done(self):
        """A Cancel pressed before the solve phase started: nothing more starts."""
        self._clear_estimating()
        self._report("Impedance solve cancelled")
        self._busy_off()

    def _clear_estimating(self):
        if not self.last_estimate:                 # still "Estimating…": no estimate came
            self.estimate.setText("")

    def _worker_finished(self, worker: AcWorker):
        if worker is self.worker:
            self.worker = None
            self._busy_off()
        worker.deleteLater()

    def _busy_off(self):
        self._busy = False
        self.solve_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)

    def cancel(self):
        """Ask the running phase to stop (KiPI checks between frequencies). Solve stays off
        until its thread has ended."""
        if not self._busy:
            return
        self._cancel_requested = True              # kept for a phase not started yet
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self._report("Cancelling the impedance solve…")

    # ------------------------------------------------------------------ board and window
    def board_changed(self):
        """A board (or project) was opened: nothing of the previous one survives."""
        self.stop_pick()
        self._generation += 1
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()            # its signals are now stale; Solve waits for its end
        else:
            self._busy = False
            self.solve_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
        self.results.clear()
        self.last_estimate = ""
        self.estimate.setText("")
        self._say("")
        self.load_project()

    def close_workers(self):
        """The window is closing: a running solve is cancelled, nothing of it is shown, and the
        window waits for its thread to end (KiPI checks for a cancel between frequencies)."""
        w = self.worker
        if w is not None and w.isRunning():
            with warnings.catch_warnings():               # a signal with no slot left to drop
                warnings.simplefilter("ignore", RuntimeWarning)
                for signal in (w.message, w.fraction, w.sized, w.succeeded, w.failed, w.cancelled,
                               w.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            w.cancel()
            self._win.statusBar().showMessage("Finishing the impedance solve before closing…")
            self._win.statusBar().repaint()
            w.wait()
