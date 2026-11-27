"""The Ripple & noise tab: a switching regulator's ripple and the noise it couples onto signal
nets (KiPI ``kipi/noise`` through :mod:`coppernicus.engine.noise`).

An editor beside the plots and the results. The editor holds the project's aggressors (a
switcher source, its switching frequency and edges, its phases, the probes to show its ripple
at), its victims (net, type, ends, logic level, margin) and the ``noise:`` options; an edit
changes only the field edited, so an untouched value is never rounded through its cell, and
nothing the project file would refuse is written (a value it would refuse is not applied; a
row it could not save yet, a victim without its net or type, a phase side without its part or
its net, stays in the table only). "Fill from board" (asking before it replaces phases) and
"Guess ends" are explicit buttons, never run at solve: phases filled from the board leave
Coss and Qrr empty, so KiPI's stand-ins apply and the victims are not judged; guessed ends are
shown, not entered, until the user enters them (Use guessed ends, or typing them), so a victim
with guessed ends is not judged.

Solve first meshes the aggressor's rails and ground as the solve will and shows the cost and
memory estimate (asking before it goes on when the solve could exceed this machine's
memory), then solves in a worker with progress and Cancel; the waveforms show as soon as KiPI
hands them over. The plots are :func:`coppernicus.coupled_noise.draw_waveforms` and
:func:`~coppernicus.coupled_noise.draw_field_map`; ripple and the radiated estimate are never
judged; export uses the core's writers with the CLI's file names.

The project solved is the window's merged project (``window.merged.project``: the project
merged with the board's KiCad footprint fields, as ``coppernicus noise`` merges them); an edit
writes ``window.project`` and merges again (``window._remerge()``). The pure functions at the
top (form <-> model, estimate and stats text, results rows) need no window.
"""

from __future__ import annotations

import copy
import dataclasses
import html
import json
import re
import traceback
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QAction, QBrush, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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

from coppernicus.app import settings as app_settings
from coppernicus.app import theme
from coppernicus.app.mesher import failure
from coppernicus.app.mesher import shown as mesher_shown
from coppernicus.app.panels import (
    FieldComboBox,
    OptionalDecimalValidator,
    WheelWhenFocused,
    without_tolerances,
)
from coppernicus.engine.meshing import session as meshing
from coppernicus.engine.pisetup import terminal_names  # engine names; fsw_default mirrors the engine
from coppernicus.impedance import hz_text
from coppernicus.project import (
    CUSTOM_PRESET,
    NOISE_TOPOLOGIES,
    TERMINATION_KEYS,
    VICTIM_PRESETS,
    Aggressor,
    NoiseOptions,
    Phase,
    Project,
    ProjectError,
    Victim,
    parse_pins,
    pins_text,
)
from coppernicus.triangle_standin import TriangleNotInstalled

# ---------------------------------------------------------------------- numbers

_NUMBER = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")


def parse_scaled(text: Optional[str], exp: int) -> Optional[float]:
    """A number typed in a field shown in units of 10^exp (kHz: 3, ns: -9), in SI units:
    ``("1.5", -9)`` → 1.5e-9 exactly as typed; ``"3,3"`` reads as 3.3; None when empty.
    Raises ValueError for anything else."""
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None
    if not _NUMBER.match(t):
        raise ValueError(f"{text.strip()!r} is not a number")
    if exp == 0:
        return float(t)
    if "e" in t.lower():
        return float(t) * 10.0 ** exp
    return float(f"{t}e{exp}")


def scaled_text(x: Optional[float], exp: int) -> str:
    """A value as a field in units of 10^exp shows it, 6 significant digits ("" for None)."""
    return "" if x is None else f"{x / 10.0 ** exp:.6g}"


def _volts(x: Optional[float]) -> str:
    """A voltage in mV below 1 V and in V above (4 significant digits)."""
    if x is None:
        return "-"
    return f"{x:.4g} V" if abs(x) >= 0.99995 else f"{x * 1e3:.4g} mV"


# ---------------------------------------------------------------------- the file's own checks

def check_noise(aggressors: Sequence[Aggressor] = (), victims: Sequence[Victim] = (),
                options: Optional[NoiseOptions] = None) -> None:
    """The project file's checks of aggressors, victims and noise options (what a file with
    them would be refused for), raising ProjectError with the file's reason."""
    p = Project(aggressors=list(aggressors), victims=list(victims),
                noise=options if options is not None else NoiseOptions())
    try:
        Project.from_dict(p.to_dict())
    except ProjectError as e:
        raise ProjectError(str(e).removeprefix("invalid project file: ")) from None


def _checked(what: str, *args) -> None:
    try:
        check_noise(*args)
    except ProjectError as e:
        raise ValueError(f"{what}: {e}" if what else str(e)) from None


# ---------------------------------------------------------------------- aggressor form

# key: (label, power of ten of the unit shown, may be empty)
AGGRESSOR_FIELDS: Dict[str, Tuple[str, int, bool]] = {
    "vin_v": ("Vin (V)", 0, True), "fsw_hz": ("fsw (kHz)", 3, True), "rise_s": ("Rise (ns)", -9, True),
    "fall_s": ("Fall (ns)", -9, True), "duty": ("Duty", 0, True), "load_a": ("Load (A)", 0, True),
    "min_ring_zeta": ("Ringing ζ at least", 0, False), "soft_edge_zeta": ("Soft edge ζ at least", 0, False)}
AGGRESSOR_TIPS = {
    "name": "Unique in the project",
    "vin_v": "Empty: the voltage of the source feeding the switcher's input",
    "fsw_hz": "The switching frequency; empty: the source's own (its fsw_hz, or the KiCad field "
              "PI_FSW), else the solve asks for it",
    "rise_s": "The switch node's rise time (required to solve: it sets the spectrum)",
    "fall_s": "The switch node's fall time (required to solve: it sets the spectrum)",
    "duty": "Empty: from the voltages and the losses entered (between 0 and 1)",
    "load_a": "Empty: the loads on the switcher's output rail",
    "min_ring_zeta": "The least damping of the switch node's ringing (KiPI's floor 0.05)",
    "soft_edge_zeta": "The least damping of a soft edge (KiPI's floor 0.5)"}


def aggressor_text(a: Aggressor, key: str) -> str:
    """What the aggressor form shows for ``key``."""
    if key in AGGRESSOR_FIELDS:
        return scaled_text(getattr(a, key), AGGRESSOR_FIELDS[key][1])
    return str(getattr(a, key))


def with_aggressor_field(a: Aggressor, key: str, text: str) -> Aggressor:
    """A copy of ``a`` with ``key`` read from ``text`` (every other field as it was). Raises
    ValueError, with the project file's reason, for a value the file would refuse."""
    q = copy.deepcopy(a)
    if key in ("name", "vrm", "topology"):
        value: Any = (text or "").strip()
        if key == "name" and not value:
            raise ValueError("an aggressor needs a name")
    else:
        label, exp, optional = AGGRESSOR_FIELDS[key]
        try:
            value = parse_scaled(text, exp)
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from None
        if value is None and not optional:
            raise ValueError(f"{label}: give a number (it cannot be empty)")
    setattr(q, key, value)
    _checked("", [savable_aggressor(q)[0]])
    return q


def aggressor_conflict(rows: Sequence[Aggressor], i: int, q: Aggressor) -> Optional[str]:
    """The project file's reason when row ``i``, edited to ``q``, takes another row's name;
    None. Such an edit is refused: written, it would drop a saved aggressor."""
    for j, x in enumerate(rows):
        if j != i and x.name == q.name:
            try:
                check_noise([Aggressor(x.name), Aggressor(q.name)])
            except ProjectError as e:
                return str(e)
    return None


def with_ripple_probe(a: Aggressor, name: str, on: bool, order: Sequence[str]) -> Aggressor:
    """A copy of ``a`` with probe ``name`` named for its ripple (or not): the project's probes
    in their order (``order``), then names the project has no probe for (kept until removed)."""
    names = [n for n in a.ripple_probes if n != name] + ([name] if on else [])
    q = copy.deepcopy(a)
    q.ripple_probes = [n for n in order if n in names] + [n for n in names if n not in order]
    return q


# ---------------------------------------------------------------------- the switcher and its fsw

def switcher_names(project: Project) -> List[str]:
    """The switcher sources an aggressor can name, by their engine name
    (:func:`coppernicus.engine.pisetup.terminal_names`: the label, or "label (net)" when two
    share it), as ``Aggressor.vrm`` holds them."""
    names = terminal_names(project)
    return [names[("source", r.net, i)] for r in project.rails for i, s in enumerate(r.sources)
            if s.kind == "smps" and ("source", r.net, i) in names]


def fsw_default(project: Project, vrm: str, from_fields: Sequence[str]) -> Tuple[Optional[float], str, str]:
    """(fsw, source label, where it comes from) of the source ``vrm`` as the noise solve takes
    it when the aggressor gives none: its ``fsw_hz``, from the KiCad field PI_FSW when its rail
    came from the fields (``from_fields``), else from the project file; (None, "", "") when it
    has none. The same rule as ``engine.noise._source_fsw`` (a test holds them together),
    copied only so that this module does not import ``engine.noise`` when the window opens."""
    if not vrm:
        return None, "", ""
    names = terminal_names(project)
    for r in project.rails:
        for i, s in enumerate(r.sources):
            if names.get(("source", r.net, i)) == vrm and s.fsw_hz is not None:
                where = "KiCad field PI_FSW" if r.net in from_fields else "the project file"
                return float(s.fsw_hz), s.label, where
    return None, "", ""


def fsw_hint(a: Aggressor, project: Project, from_fields: Sequence[str]) -> str:
    """The line under the fsw field: where the switching frequency comes from."""
    hz, label, where = fsw_default(project, a.vrm, from_fields)
    if a.fsw_hz is not None:
        return (f"Entered: it wins over the source {label}'s {hz_text(hz)} ({where})" if hz
                else "Entered")
    if hz:
        return f"Empty: {hz_text(hz)} from the source {label} ({where})"
    return ("Required to solve: enter it, or give the source its switching frequency (fsw_hz, or "
            "the KiCad field PI_FSW)")


# ---------------------------------------------------------------------- phases

PHASE_COLUMNS = [("Inductor", "inductor"), ("L (µH)", "inductance_h"), ("DCR (mΩ)", "dcr_ohm"),
                 ("Switch net", "switch_net"), ("High-side part", "high_side_ref"),
                 ("High-side net", "high_side_net"), ("High-side pins", "high_side_pins"),
                 ("Low-side part", "low_side_ref"), ("Low-side net", "low_side_net"),
                 ("Low-side pins", "low_side_pins"), ("Input caps", "input_caps"),
                 ("Output caps", "output_caps"), ("Coss (pF)", "coss_f"), ("Qrr (nC)", "qrr_nc"),
                 ("Rds on, high (mΩ)", "rds_on_hs_ohm"), ("Rds on, low (mΩ)", "rds_on_ls_ohm"),
                 ("Package L (nH)", "package_l_h"), ("Axis", "inductor_axis"),
                 ("Shielded", "inductor_shielded")]
_PHASE_SCALE = {"inductance_h": -6, "dcr_ohm": -3, "package_l_h": -9, "coss_f": -12, "qrr_nc": 0,
                "rds_on_hs_ohm": -3, "rds_on_ls_ohm": -3}
_PHASE_ZERO = ("inductance_h", "dcr_ohm", "package_l_h")        # empty is 0: not entered
_PHASE_TEXT = ("inductor", "switch_net", "high_side_ref", "high_side_net", "low_side_ref",
               "low_side_net", "inductor_axis")
# KiPI's stand-ins for a phase's device data (kipi/noise/model.py:29, :34; a test checks them).
KIPI_COSS_PF = 200.0
KIPI_QRR_NC = 5.0
STAND_INS = {
    "coss_f": f"Not entered: KiPI's {KIPI_COSS_PF:g} pF stands in (it sets the ringing), so the victims "
              "are not judged",
    "qrr_nc": f"Not entered: KiPI's {KIPI_QRR_NC:g} nC stands in (it sets the hard edge; enter 0 for a "
              "Schottky or GaN low side), so the victims are not judged"}
PHASE_TIPS = {
    "inductor": "The power inductor's part", "inductance_h": "Required to solve (above 0)",
    "dcr_ohm": "The inductor's DC resistance", "switch_net": "The switch node's net; empty: no "
    "capacitive coupling, and the victims are not judged",
    "high_side_ref": "The switching device's part on the rail its hot loop is fed from (VIN for a buck)",
    "high_side_net": "That rail's net", "high_side_pins": "Pad numbers, comma separated; 'all' = every pin "
    "of the part on the net", "low_side_ref": "The switching device's part on its power ground",
    "low_side_net": "The power ground's net", "low_side_pins": "Pad numbers, comma separated; 'all' = every "
    "pin of the part on the net", "input_caps": "The hot loop's input capacitors (parts), comma separated",
    "output_caps": "The output capacitors (parts), comma separated", "coss_f": STAND_INS["coss_f"],
    "qrr_nc": STAND_INS["qrr_nc"], "rds_on_hs_ohm": "Empty: KiPI's 10 mΩ, for damping only (an assumption)",
    "rds_on_ls_ohm": "Empty: KiPI's 10 mΩ, for damping only (an assumption)",
    "package_l_h": "The device package's own loop inductance", "inductor_axis": "vertical, along or across "
    "(KiPI's default vertical)", "inductor_shielded": "yes or no (KiPI's default yes)"}
_SIDES = (("high_side", "High-side"), ("low_side", "Low-side"))


def phase_cell(ph: Phase, key: str) -> str:
    """What the phases table shows in column ``key``."""
    v = getattr(ph, key)
    if key in _PHASE_SCALE:
        return scaled_text(v, _PHASE_SCALE[key])
    if key.endswith("_pins"):
        side = key[: -len("_pins")]
        return pins_text(v) if (getattr(ph, f"{side}_net") or getattr(ph, f"{side}_ref")) else ""
    if key.endswith("_caps"):
        return ", ".join(v)
    if key == "inductor_shielded":
        return "yes" if v else "no"
    return str(v)


def _header(columns, key: str) -> str:
    return next(h for h, k in columns if k == key)


def with_phase_cell(ph: Phase, key: str, text: str) -> Phase:
    """A copy of ``ph`` with column ``key`` read from ``text`` (not checked: see
    :func:`edit_phase`)."""
    t = (text or "").strip()
    try:
        if key in _PHASE_TEXT:
            value: Any = t
        elif key.endswith("_pins"):
            value = parse_pins(t)
        elif key.endswith("_caps"):
            value = [r for r in re.split(r"[,\s]+", t) if r]
        elif key == "inductor_shielded":
            if t.lower() not in ("yes", "no", "true", "false", "1", "0"):
                raise ValueError(f"yes or no, not {t!r}")
            value = t.lower() in ("yes", "true", "1")
        else:
            value = parse_scaled(t, _PHASE_SCALE[key])
            if value is None and key in _PHASE_ZERO:
                value = 0.0
    except (ValueError, ProjectError) as e:
        raise ValueError(f"{_header(PHASE_COLUMNS, key)}: {e}") from None
    q = copy.deepcopy(ph)
    setattr(q, key, value)
    return q


def savable_aggressor(a: Aggressor) -> Tuple[Aggressor, List[str]]:
    """(the aggressor as the project file can hold it, why some of it is not saved yet): a
    phase side is saved with both its part and its net (the file needs the part with the net
    and keeps the pins only with it), so a half-typed side is left out, never refused, in
    whichever order its cells are typed."""
    q = copy.deepcopy(a)
    notes = []
    for i, ph in enumerate(q.phases, 1):
        for side, label in _SIDES:
            ref, net, pins = (getattr(ph, f"{side}_{k}") for k in ("ref", "net", "pins"))
            if bool(ref) != bool(net) or (pins is not None and not net):
                missing = " and ".join(w for w, have in (("part", ref), ("net", net)) if not have)
                notes.append(f"not saved yet: phase {i}'s {label} side is saved with both its part and "
                             f"its net: give its {missing}")
                setattr(ph, f"{side}_ref", "")
                setattr(ph, f"{side}_net", "")
                setattr(ph, f"{side}_pins", None)
    return q, notes


def edit_phase(a: Aggressor, i: int, key: str, text: str) -> Aggressor:
    """A copy of ``a`` with column ``key`` of phase ``i`` read from ``text``; ValueError, with
    the file's reason, for a value it would refuse."""
    q = copy.deepcopy(a)
    q.phases[i] = with_phase_cell(q.phases[i], key, text)
    _checked(_header(PHASE_COLUMNS, key), [savable_aggressor(q)[0]])
    return q


# ---------------------------------------------------------------------- victims

VICTIM_COLUMNS = [("Name", "name"), ("Net", "net"), ("Type (preset)", "preset"),
                  ("Driver part", "driver_ref"), ("Driver pins", "driver_pins"),
                  ("Receiver part", "receiver_ref"), ("Receiver pins", "receiver_pins"),
                  ("Logic (V)", "logic_v"), ("Margin (mV)", "noise_margin_v"), ("Driver (Ω)", "driver_ohm"),
                  ("Receiver (Ω)", "receiver_ohm"), ("Receiver (pF)", "receiver_pf"),
                  ("Termination (Ω)", "termination_ohm"), ("Driver low (Ω)", "driver_low_ohm")]
_VICTIM_SCALE = {"logic_v": 0, "noise_margin_v": -3, "driver_ohm": 0, "receiver_ohm": 0, "receiver_pf": 0,
                 "termination_ohm": 0, "driver_low_ohm": 0}
# A custom victim's terminations start at KiPI's cmos preset (kipi/noise/model.py:153-173; a
# test checks them): the user enters the line's own.
CUSTOM_START = {"driver_ohm": 33.0, "receiver_ohm": 1e6, "receiver_pf": 3.0}
PRESET_CHOICES = (("", "choose…"), *((p, p) for p in VICTIM_PRESETS), (CUSTOM_PRESET, "custom"))
VICTIM_TIPS = {
    "name": "Empty: the net's name; unique in the project", "net": "The signal net the noise couples onto",
    "preset": "The line's type: it sets the driver and receiver (custom: enter them)",
    "driver_ref": "The driver's part; empty: KiPI picks one and the victim is not judged",
    "driver_pins": "Pad numbers, comma separated; 'all' = every pin of the part on the net",
    "receiver_ref": "The receiver's part; empty: KiPI picks one and the victim is not judged",
    "receiver_pins": "Pad numbers, comma separated; 'all' = every pin of the part on the net",
    "logic_v": "The logic family's supply: its JESD8 levels give the margin when no margin is entered",
    "noise_margin_v": "The noise the receiver tolerates; empty: the preset's own (lvds, analog), else the "
                      "logic level's; none: ranked, never judged",
    **{k: "A custom line's own (choose custom to enter it)" for k in TERMINATION_KEYS}}
DRAFT_VICTIM = "draft: a victim needs its net and its type (preset) before it is saved and solved"
GUESS_TIP = ("KiPI's guess (the net's two pads farthest apart), not entered: guessed ends leave the victim "
             "not judged. Use guessed ends, or type the part, to enter them")


def victim_cell(v: Victim, key: str) -> str:
    """What the victims table shows in column ``key``."""
    x = getattr(v, key)
    if key in _VICTIM_SCALE:
        return scaled_text(x, _VICTIM_SCALE[key])
    if key.endswith("_pins"):
        return pins_text(x) if (x is not None or getattr(v, key.replace("_pins", "_ref"))) else ""
    return str(x)


def is_draft_victim(v: Victim) -> bool:
    return not (v.net and v.preset)


def savable_victim(v: Victim) -> Tuple[Victim, List[str]]:
    """(the victim as the file can hold it, why some of it is not saved yet): an end's pins
    are saved with its part."""
    q = copy.deepcopy(v)
    notes = []
    for what, label in (("driver", "Driver"), ("receiver", "Receiver")):
        if getattr(q, f"{what}_pins") is not None and not getattr(q, f"{what}_ref"):
            setattr(q, f"{what}_pins", None)
            notes.append(f"not saved yet: {label} pins are saved with the {what}'s part: give it")
    return q, notes


def with_victim_cell(v: Victim, key: str, text: str) -> Tuple[Victim, str]:
    """(a copy of ``v`` with column ``key`` read from ``text``, a note of what else changed):
    choosing a named type clears the terminations (it has its own); choosing custom starts
    them at CUSTOM_START. ValueError, with the file's reason, for a value it would refuse."""
    t = (text or "").strip()
    header = _header(VICTIM_COLUMNS, key)
    try:
        if key in ("name", "net", "driver_ref", "receiver_ref"):
            value: Any = t
        elif key.endswith("_pins"):
            value = parse_pins(t)
        elif key == "preset":
            if t not in (*VICTIM_PRESETS, CUSTOM_PRESET, ""):
                raise ValueError(f"one of {', '.join(VICTIM_PRESETS)} or {CUSTOM_PRESET}, not {t!r}")
            value = t
        else:
            if key in TERMINATION_KEYS and v.preset != CUSTOM_PRESET:
                raise ValueError(f"the preset {v.preset or '(none)'} has its own terminations: choose "
                                 f"{CUSTOM_PRESET} to enter them")
            value = parse_scaled(t, _VICTIM_SCALE[key])
    except (ValueError, ProjectError) as e:
        raise ValueError(f"{header}: {e}") from None
    q = copy.deepcopy(v)
    setattr(q, key, value)
    note = ""
    if key == "preset" and value == CUSTOM_PRESET:
        filled = [k for k in CUSTOM_START if getattr(q, k) is None]
        for k in filled:
            setattr(q, k, CUSTOM_START[k])
        if filled:
            note = ("Custom terminations start at KiPI's cmos values, driver 33 Ω, receiver 1 MΩ ∥ 3 pF: "
                    "enter the line's own")
    elif key == "preset":
        cleared = [k for k in TERMINATION_KEYS if getattr(q, k) is not None]
        for k in cleared:
            setattr(q, k, None)
        if cleared:
            note = f"Terminations cleared: the preset {value or '(none)'} has its own"
    # A draft is checked for its values: its missing net or type stands in.
    trial = dataclasses.replace(savable_victim(q)[0], net=q.net or "draft", preset=q.preset or "cmos")
    _checked(header, [], [trial])
    return q, note


def victim_conflict(rows: Sequence[Victim], r: int, q: Victim) -> Optional[str]:
    """The project file's reason when row ``r``, edited to ``q``, takes another row's name (a
    victim's name is its net unless one is given), drafts included; None."""
    if not q.net:
        return None
    for i, x in enumerate(rows):
        if i != r and x.net and x.label == q.label:
            try:
                check_noise([], [Victim(x.net, name=x.name, preset="cmos"),
                                 Victim(q.net, name=q.name, preset="cmos")])
            except ProjectError as e:
                return str(e)
    return None


def usable_victims(rows: Sequence[Victim]) -> Tuple[List[Victim], Dict[int, str]]:
    """(the victims to write, as the file holds them; {row: why it is not, or not all, saved}):
    drafts and a second victim of the same name stay in the table only."""
    out, problems, seen = [], {}, set()
    for i, v in enumerate(rows):
        if is_draft_victim(v):
            problems[i] = DRAFT_VICTIM
            continue
        if v.label in seen:
            problems[i] = (f"victim names must be unique: {v.label} is used above, so this row is not saved "
                           "(a victim's name is its net unless one is given)")
            continue
        seen.add(v.label)
        q, notes = savable_victim(v)
        if notes:
            problems[i] = "; ".join(notes)
        out.append(q)
    check_noise([], out)
    return out, problems


# ---------------------------------------------------------------------- noise options

# key: (label, power of ten of the unit shown, may be empty)
OPTION_FIELDS: Dict[str, Tuple[str, int, bool]] = {
    "f_max_hz": ("Highest harmonic (MHz)", 6, True), "dead_time_ns": ("Dead time (ns)", 0, False),
    "segment_mm": ("Victim cut every (mm)", 0, False), "window_mm": ("Copper within (mm)", 0, False)}
# The quick preset (Coppernicus's choice, as the Impedance tab's): the board model solved at
# this many frequencies per decade instead of KiPI's 10 (kipi/noise/model.py:264). Applied only
# when pressed; the result's assumptions say how many were used.
QUICK_FIELD_PER_DECADE = 5
KIPI_FIELD_PER_DECADE = NoiseOptions().field_per_decade


def option_text(o: NoiseOptions, key: str) -> str:
    return scaled_text(getattr(o, key), OPTION_FIELDS[key][1])


def with_option(o: NoiseOptions, key: str, value: Any) -> NoiseOptions:
    """A copy of ``o`` with ``key`` set: text for the number fields, an int or a bool for the
    others; ValueError, with the file's reason, for a value it would refuse."""
    if key in OPTION_FIELDS:
        label, exp, optional = OPTION_FIELDS[key]
        try:
            value = parse_scaled(value, exp)
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from None
        if value is None and not optional:
            raise ValueError(f"{label}: give a number (it cannot be empty)")
    q = dataclasses.replace(o, **{key: value})
    _checked("", [], [], q)
    return q


# ---------------------------------------------------------------------- estimate, stats, rows

def estimate_text(size, cost, ram) -> str:
    """The pre-solve estimate: the board model's size and field solves (the time scaled from
    the last noise solve), the harmonics, and the memory (:func:`coppernicus.engine.noise.
    noise_ram_estimate`). Without the board model there is no field solve to size."""
    reach = f"{size.harmonics} harmonics to {hz_text(size.f_max_hz)}"
    if not size.board_model:
        why = size.note or "board model off (noise.board_model)"
        return f"Before solving: {reach}; no field solve: {why}. {ram.text}"
    risk = "It could exceed this machine's memory. " if ram.risk else ""
    return f"Before solving: {cost.text}; {reach}. {risk}{ram.text}"


def _gb(x: float) -> str:
    return f"{x / 1e9:.3g} GB"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min" if seconds < 5400 else f"{seconds / 3600:.1f} h"


def estimate_line(size, cost, ram) -> str:
    """The estimate's numbers on one short line, for the label (its tooltip and the memory
    question give :func:`estimate_text`): the editor stays in sight in a short dock."""
    reach = f"{size.harmonics} harmonics to {hz_text(size.f_max_hz)}"
    memory = f"memory up to {_gb(ram.bytes)}" + (f" of {_gb(ram.total_bytes)}" if ram.total_bytes else "")
    memory += ": it could exceed this machine's memory" if ram.risk else " (hover for how)"
    if not size.board_model:
        why = size.note or "board model off (noise.board_model)"
        return f"Before solving: {reach}; no field solve ({why}); {memory}"
    took = f", about {_duration(cost.seconds)}" if cost.seconds is not None else ""
    return (f"Before solving: {cost.triangles:,} triangles × {cost.ports} ports, {cost.field_frequencies} "
            f"field solves{took}; {reach}; {memory}")


def may_start(ram, answered_yes: bool) -> bool:
    """A solve starts unless it could exceed the memory and the user did not answer Yes."""
    return (not ram.risk) or answered_yes


def stats_text(st: Dict[str, Any]) -> str:
    """One line of a noise solve's stats (only what was measured), with its memory peak and
    whether this solve raised it."""
    parts = [f"solved in {float(st.get('seconds') or 0.0):.1f} s"]
    if st.get("harmonics") is not None:
        parts.append(f"{int(st['harmonics'])} harmonics")
    for key in ("triangles", "unknowns", "ports"):
        if st.get(key) is not None:
            parts.append(f"{int(st[key]):,} {key}")
    if st.get("field_solves") is not None:
        fs = f"{int(st['field_solves'])} field solves"
        if st.get("field_seconds") is not None:
            fs += f" in {float(st['field_seconds']):.1f} s"
        parts.append(fs)
    mb = st.get("memory_peak_mb")
    if mb is None:
        parts.append("memory peak cannot be read on this system")
    elif st.get("memory_peak_raised"):
        parts.append(f"memory peak {mb:.0f} MB (this solve raised the process's peak)")
    else:
        parts.append(f"memory peak {mb:.0f} MB: the process's peak so far; this solve stayed at or below it")
    return " · ".join(parts)


RIPPLE_HEADERS = ("Ripple at", "Where", "Between", "p-p, 20 MHz", "Below, 20 MHz", "Above, 20 MHz",
                  "p-p, full band", "rms", "Verdict")


def ripple_rows(r) -> List[Tuple[str, ...]]:
    """One row per ripple of a NoiseResult (RIPPLE_HEADERS): through 20 MHz first (a scope's
    bandwidth limit), then the full band; never judged here (Task 11 stacks it per load)."""
    return [(rp.name, rp.where, f"{rp.net} − {rp.ground_net}", _volts(rp.pp_bw_v), _volts(rp.below_bw_v),
             _volts(rp.above_bw_v), _volts(rp.pp_v), _volts(rp.rms_v), "not judged") for rp in r.ripple]


VICTIM_HEADERS = ("Victim", "Net", "Peak", "Margin", "% of margin", "Verdict", "Mostly", "Capacitive",
                  "Inductive", "Common", "Ends", "Why")


def _meant_a_margin(v) -> bool:
    """A margin was set, or meant (a logic_v off KiPI's JESD8 table: ``margin_note``)."""
    return v.margin_v is not None or bool(v.margin_note)


def _verdict_word(v) -> str:
    if v.passed is True:
        return "PASS"
    if v.passed is False:
        return "FAIL"
    return "NOT JUDGED" if _meant_a_margin(v) else "no margin (ranked)"


STALE_MARK = " (setup changed)"


def victim_rows(r, stale: bool = False) -> List[Tuple[str, ...]]:
    """One row per victim of a judged NoiseResult (VICTIM_HEADERS): its peak at the receiver,
    its margin and where the verdict comes from (the failure, the stand-ins, or no margin).
    ``stale``: the setup changed since the solve, and every verdict says so."""
    failed = {e["item"]: e["reason"] for e in r.failed}
    rows = []
    for v in r.victims:
        if v.passed is False:
            why = failed.get(v.name, "")
        elif _meant_a_margin(v):
            why = v.not_judged                      # the core's reason, verbatim
        else:
            why = "no noise margin set: ranked, not judged"
        ends = f"{v.driver} → {v.receiver} ({'entered' if v.ends_entered else 'guessed by KiPI'})"
        rows.append((v.name, v.net, _volts(v.peak_v), _volts(v.margin_v),
                     f"{100 * v.ratio:.3g}" if v.ratio is not None else "-",
                     _verdict_word(v) + (STALE_MARK if stale else ""),
                     v.dominant or "-", *(_volts(v.peak_by_v[k]) if k in v.peak_by_v else "-"
                                          for k in ("capacitive", "inductive", "common")), ends, why))
    return rows


def notes_text(r) -> str:
    """The result's report as the CLI prints it (verdict, not-judged reasons, assumptions,
    warnings: verbatim), then every stand-in, the aggressor's and each victim's own (a victim
    without a margin has no reason to carry them), and each victim's warnings from KiPI."""
    from coppernicus.coupled_noise import noise_summary, noise_text
    lines = [line.strip("\n") for line in noise_text(noise_summary([r]))]
    lines += [f"  stand-in: {x}" for x in r.stand_ins]
    lines += [f"  stand-in ({v.name}): {x}" for v in r.victims for x in v.stand_ins if x not in r.stand_ins]
    lines += [f"  warning ({v.name}): {w}" for v in r.victims for w in v.warnings]
    lines += [f"  radiated estimate: {n}" for n in (r.emi.notes if r.emi is not None else [])]
    return "\n".join(lines)


def unsolved_text(a: Optional[Aggressor], rows: Sequence[Victim]) -> str:
    """What a solve leaves out, said at Solve and kept with its result ("" when nothing): the
    victim rows not saved (drafts, a name used twice), and what is solved without a part not
    saved yet (an end's pins without its part, a phase side without its part or its net)."""
    try:
        problems = usable_victims(rows)[1]
    except ProjectError:
        problems = {}

    def row(r: int, why: str, prefix: str = "") -> str:
        return f"{prefix}row {r + 1} ({rows[r].label or 'no net'}): {why}"
    skipped = [row(r, why) for r, why in sorted(problems.items()) if not why.startswith("not saved yet")]
    partly = [row(r, why, "victim ") for r, why in sorted(problems.items())
              if why.startswith("not saved yet")]
    partly += savable_aggressor(a)[1] if a is not None else []
    parts = []
    if skipped:
        n = len(skipped)
        parts.append(f"{n} victim row{'s' if n > 1 else ''} not saved {'are' if n > 1 else 'is'} not solved: "
                     + "; ".join(skipped))
    if partly:
        parts.append("not saved, so solved without it: " + "; ".join(partly))
    return " · ".join(parts)


def radiated_text(r) -> str:
    """The radiated estimate, for scale only ("" when there is none)."""
    e = r.emi
    if e is None or e.radiated_dbuv_m is None:
        return ""
    limit = (f" (CISPR 32 class B {e.cispr32_b_dbuv_m:.0f} dBµV/m there)" if e.cispr32_b_dbuv_m is not None
             else " (below CISPR 32's 30 MHz band)")
    return (f"Radiated estimate at {e.distance_m:g} m: {e.radiated_dbuv_m:.1f} dBµV/m at "
            f"{hz_text(e.radiated_hz)}{limit}: for scale, not judged")


def header_html(r, stale: bool) -> str:
    """The verdict line of a result: the victims' verdict (ripple and the radiated estimate
    are never judged), its note, and a flag when the setup changed since the solve."""
    words = {"fail": theme.span("fail", "<b>FAIL</b>"), "pass": "<b>PASS</b>",
             "not_judged": theme.span("warn", "<b>not judged</b>")}
    head = (f"<b>{html.escape(r.aggressor)}</b> ({html.escape(r.vrm)}, {r.topology}: {r.vin_v:g} V → "
            f"{r.vout_v:g} V, {r.iout_a:.3g} A at {hz_text(r.fsw_hz)}): victims "
            f"{words.get(r.verdict, 'partial')}; ripple and the radiated estimate are never judged")
    note = f"<br><small>{html.escape(r.verdict_note)}</small>" if r.verdict_note else ""
    flag = "<br>" + theme.span("warn", "<b>Setup changed since this solve</b>: solve again") if stale else ""
    return head + note + flag


def project_key(project: Project) -> Dict[str, Any]:
    """What a noise solve depends on, to tell when a result no longer matches the project:
    not the board's path (a save rewrites it; another board clears the results), the nets
    marked as rails, the rails' DC limits, the board temperature, or the tolerances, entered
    parts and accuracies (no noise solve reads them). Taken of the merged project, what is
    solved: a project rail ignored for the KiCad fields' changes nothing."""
    d = without_tolerances(project.to_dict())
    for key in ("marked_rails", "board", "thermal"):
        d.pop(key, None)
    for rail in d.get("rails", []):
        rail.pop("limits", None)
    return d


def export_stem(project_path, board_name: str) -> str:
    """The file names' stem as ``coppernicus noise`` writes them: the project file's stem (its
    ``.coppernicus`` too), else the board's name."""
    from coppernicus.maps import safe_file_name
    if project_path:
        return safe_file_name(Path(project_path).stem)
    return safe_file_name((board_name or "").split(".")[0] or "coppernicus")


LAST_SOLVE_SETTING = "noise/last_solve"     # the last noise solve's size and time (QSettings)
_LAST_KEYS = ("seconds", "field_seconds", "triangles", "ports", "field_solves")


def _settings() -> QSettings:
    return app_settings()


# ---------------------------------------------------------------------- worker

class NoiseWorker(QThread):
    """Phase "size": the aggressor's rails and ground meshed as the solve will mesh them
    (:func:`coppernicus.engine.noise.mesh_estimate`); phase "solve": the noise solve. Both on
    a copy of the window's merged project (``merged``, a :class:`coppernicus.board_fields.
    Merged`), whose notes and warnings the result gets. KiPI calls the progress hooks from its
    own threads: they reach the panel as queued signals."""
    message = Signal(str)
    fraction = Signal(object)        # 0..1 over the victims, None while it cannot tell
    partial = Signal(object)         # the waveforms, as KiPI hands them over (NoiseResult, partial)
    sized = Signal(object)           # NoiseSize
    succeeded = Signal(object)       # NoiseResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, board, merged, name: str, phase: str, parent=None):
        super().__init__(parent)
        self._board, self.merged, self.name, self.phase = board, merged, name, phase
        self._cancel = False
        self.generation = 0
        self.mesher: Optional[str] = None       # the solve's mesher (None: the default); set by the panel

    def cancel(self):
        self._cancel = True

    def run(self):
        from coppernicus.engine import Cancelled, EngineError
        from coppernicus.engine import noise as engine_noise
        try:
            with meshing.using(self.mesher):     # the estimate meshes too
                if self.phase == "size":
                    size = engine_noise.mesh_estimate(self._board, self.merged.project, self.name,
                                                      progress=self.message.emit,
                                                      cancelled=lambda: self._cancel,
                                                      fields_rails=self.merged.from_fields)
                    if self._cancel:
                        raise Cancelled("cancelled")
                    self.sized.emit(size)
                else:
                    res = engine_noise.solve_aggressor(
                        self._board, self.merged.project, self.name, progress=self.message.emit,
                        cancelled=lambda: self._cancel, fraction=self.fraction.emit,
                        partial=self.partial.emit, fields_rails=self.merged.from_fields)
                    self.merged.annotate(res)
                    self.succeeded.emit(res)
        except Cancelled:
            self.cancelled.emit()
        except (TriangleNotInstalled, meshing.MesherUnavailable) as e:
            self.failed.emit(failure(e))
        except EngineError as e:
            self.failed.emit(str(e))
        except Exception:                                   # noqa: BLE001 - shown to the user
            self.failed.emit("Unexpected error:\n" + traceback.format_exc())


# ---------------------------------------------------------------------- widgets

class _IntSpin(WheelWhenFocused, QSpinBox):
    """A whole-number field that takes the wheel only once focused."""


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


def _set_item(table: QTableWidget, r: int, c: int, text: str) -> QTableWidgetItem:
    item = table.item(r, c)
    if item is None:
        item = QTableWidgetItem(text)
        table.setItem(r, c, item)
    else:
        item.setText(text)
    return item


CHART_SURFACE = "#fcfcfb"
PLOT_FONT = 7


def _row_bg(why: str) -> QBrush:
    if not why:
        return QBrush()
    return theme.brush("cell-readonly" if why.startswith(("draft", "not saved yet")) else "cell-problem")


class NoisePanel(QWidget):
    """The Ripple & noise results tab (see the module docstring)."""
    estimated = Signal(str)          # the estimate, shown before the solve starts
    results_changed = Signal()       # a result kept, or the results dropped (the Tolerance tab reads them)

    def __init__(self, window):
        super().__init__(window)
        self._win = window
        self.results: Dict[str, Tuple[Any, Dict[str, Any]]] = {}   # aggressor -> (result, project it solved)
        self.worker: Optional[NoiseWorker] = None
        self._busy = False
        self._cancel_requested = False             # Cancel pressed: no phase starts after it
        self._generation = 0
        self._loading = False
        self._agg_rows: List[Aggressor] = []
        self._victim_rows: List[Victim] = []
        # Guessed ends shown in the victims table, never written: row -> {"driver"|"receiver": (ref, pins)}
        self._guessed: Dict[int, Dict[str, Tuple[str, Optional[List[str]]]]] = {}
        self._shown: Dict[str, str] = {}           # what each line edit shows: committed unchanged is no edit
        self._solving: Optional[Tuple[str, Dict[str, Any], Any]] = None
        self._solving_note = ""                    # what the running solve leaves out (unsolved_text)
        self._result_notes: Dict[str, str] = {}    # aggressor -> what its result left out
        self._wave_title: Optional[str] = None     # the shown result's plot title (None: none or partial)
        self._outline = None
        self.last_estimate = ""
        self._build()
        window.rail_panel.rail_changed.connect(self._setup_changed)
        window.rail_panel.settings_changed.connect(self._check_stale)
        self.load_project()

    def take_setup(self) -> QWidget:
        """The editor, handed to the window's setup panel (layout C): the page keeps
        the waveforms and the results."""
        self.setup_area.setParent(None)
        self._split.setSizes([560, 400])
        return self.setup_area

    # ------------------------------------------------------------------ build
    def _build(self):
        # Editor: the aggressor, Solve and progress on top (in sight in a short dock), then tabs.
        self.aggressor_box = FieldComboBox()
        self.aggressor_box.setToolTip("The aggressor (a switching regulator) to edit and solve")
        self.aggressor_box.setMinimumContentsLength(8)
        add_a, rem_a = QPushButton("Add"), QPushButton("Remove")
        add_a.setToolTip("A new aggressor on a switcher source of the project")
        self.solve_button = QPushButton("Solve noise")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(14)
        top = QHBoxLayout()
        top.addWidget(QLabel("Aggressor"))
        top.addWidget(self.aggressor_box, 1)
        top.addWidget(add_a)
        top.addWidget(rem_a)
        run = QHBoxLayout()
        run.addWidget(self.solve_button)
        run.addWidget(self.cancel_button)
        run.addWidget(self.progress, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.estimate = QLabel()                  # the pre-solve estimate, kept through the solve
        self.estimate.setWordWrap(True)
        self.estimate.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.estimate.setProperty("role", "muted")

        # Aggressor tab.
        self.field: Dict[str, QLineEdit] = {}
        form = QFormLayout()
        self.field["name"] = QLineEdit()
        form.addRow("Name", self.field["name"])
        self.vrm_box = FieldComboBox()
        self.vrm_box.setToolTip("The switcher source (kind smps) whose noise this is, by its name in the "
                                "solve")
        form.addRow("Source (switcher)", self.vrm_box)
        self.topology_box = FieldComboBox()
        self.topology_box.addItems(list(NOISE_TOPOLOGIES))
        self.topology_box.setToolTip("auto: from the input and output voltages")
        form.addRow("Topology", self.topology_box)
        placeholders = {"vin_v": "from the input's source", "fsw_hz": "required (or the source's)",
                        "rise_s": "required", "fall_s": "required", "duty": "from voltages and losses",
                        "load_a": "the output rail's loads"}
        for key, (label, _exp, _optional) in AGGRESSOR_FIELDS.items():
            w = QLineEdit()
            w.setValidator(OptionalDecimalValidator(w))
            w.setPlaceholderText(placeholders.get(key, ""))
            self.field[key] = w
            form.addRow(label, w)
            if key == "fsw_hz":
                self.fsw_hint = QLabel()
                self.fsw_hint.setWordWrap(True)
                self.fsw_hint.setProperty("role", "muted")
                form.addRow("", self.fsw_hint)
        for key, w in self.field.items():
            w.setToolTip(AGGRESSOR_TIPS[key])
        self.probe_list = QListWidget()
        self.probe_list.setMaximumHeight(80)
        self.probe_list.setToolTip("The ripple is shown at the ticked probes (the project's probes: "
                                   "probes: in the file, the Impedance tab's), besides the switcher's pins")
        form.addRow("Ripple at probes", self.probe_list)
        agg_tab = QWidget()
        al = QVBoxLayout(agg_tab)
        al.setContentsMargins(2, 2, 2, 2)
        al.addLayout(form)
        al.addStretch(1)

        # Phases tab.
        self.phases = _table([h for h, _ in PHASE_COLUMNS], [PHASE_TIPS[k] for _, k in PHASE_COLUMNS])
        add_p, rem_p = QPushButton("Add"), QPushButton("Remove")
        fill = QPushButton("Fill from board")
        fill.setToolTip("Read the phases from the board for the aggressor's source (KiPI: its pins, the "
                        "inductor and switch node, the capacitors to ground); asks before replacing phases. "
                        "Coss and Qrr stay empty: KiPI's stand-ins apply until you enter them.")
        pb = QHBoxLayout()
        for w in (add_p, rem_p, fill):
            pb.addWidget(w)
        pb.addStretch(1)
        legend = QLabel("<small>Amber: not entered, KiPI's stand-in is used and the victims are not judged. "
                        "Grey: not saved yet (a side needs both its part and its net).</small>")
        legend.setWordWrap(True)
        phase_tab = QWidget()
        pl = QVBoxLayout(phase_tab)
        pl.setContentsMargins(2, 2, 2, 2)
        pl.addLayout(pb)
        pl.addWidget(self.phases, 1)
        pl.addWidget(legend)

        # Victims tab.
        self.victims = _table([h for h, _ in VICTIM_COLUMNS], [VICTIM_TIPS[k] for _, k in VICTIM_COLUMNS])
        add_v, rem_v = QPushButton("Add"), QPushButton("Remove")
        guess, use = QPushButton("Guess ends"), QPushButton("Use guessed ends")
        guess.setToolTip("Show KiPI's guess at the selected victims' empty driver and receiver (the net's "
                         "two pads farthest apart). A guess is not entered: the victim is not judged.")
        use.setToolTip("Enter the guessed ends shown in the selected rows: then they count as yours")
        victim_tab = QWidget()
        vl = QVBoxLayout(victim_tab)
        vl.setContentsMargins(2, 2, 2, 2)
        for buttons in ((add_v, rem_v), (guess, use)):          # two rows: the column is narrow
            vb = QHBoxLayout()
            for w in buttons:
                vb.addWidget(w)
            vb.addStretch(1)
            vl.addLayout(vb)
        vl.addWidget(self.victims, 1)

        # Options tab.
        self.opt: Dict[str, QWidget] = {}
        oform = QFormLayout()
        for key, (label, _exp, optional) in OPTION_FIELDS.items():
            w = QLineEdit()
            w.setValidator(OptionalDecimalValidator(w))
            if optional:
                w.setPlaceholderText("auto")
            self.opt[key] = w
            oform.addRow(label, w)
        self.opt["f_max_hz"].setToolTip("Empty: ten times the faster edge's corner 1/(π·t), or three times "
                                        "the ringing, at most 3 GHz (KiPI). Below that reach the harmonics "
                                        "stop short, and the victims are then not judged")
        self.opt["dead_time_ns"].setToolTip("Between the two switches (KiPI's 20 ns)")
        self.opt["segment_mm"].setToolTip("Victims are cut into 2D cross-sections this often (KiPI's 0.5 mm)")
        self.opt["window_mm"].setToolTip("Copper this close to a victim is in its cross-section "
                                         "(KiPI's 3 mm)")
        for key in ("max_harmonics", "field_per_decade"):     # the file: a whole number, at least 1
            spin = _IntSpin()
            spin.setRange(1, 2 ** 31 - 1)
            spin.setKeyboardTracking(False)
            self.opt[key] = spin
        self.opt["max_harmonics"].setToolTip("At most this many harmonics (KiPI's 4000); fewer cut the "
                                             "spectrum short, and the victims are then not judged")
        oform.addRow("Harmonics at most", self.opt["max_harmonics"])
        self.opt["field_per_decade"].setToolTip(
            f"The board model's field solves per decade (KiPI's {KIPI_FIELD_PER_DECADE}); the run time and "
            "memory grow with it")
        oform.addRow("Field solves / decade", self.opt["field_per_decade"])
        self.quick = QPushButton(f"Quick ({QUICK_FIELD_PER_DECADE})")
        self.quick.setToolTip(f"{QUICK_FIELD_PER_DECADE} field solves per decade instead of KiPI's "
                              f"{KIPI_FIELD_PER_DECADE}: set here, visible and saved, never applied by "
                              "itself")
        kipi_default = QPushButton(f"KiPI's {KIPI_FIELD_PER_DECADE}")
        quick_row = QHBoxLayout()                # under the form: the column is narrow
        quick_row.addWidget(self.quick)
        quick_row.addWidget(kipi_default)
        quick_row.addStretch(1)
        board_model = QCheckBox("Board model")
        board_model.setToolTip("The copper's field model of the rails and ground: the ripple at probes and "
                               "the shared-ground coupling need it; off, the victims are not judged")
        self.opt["board_model"] = board_model
        oform.addRow("", board_model)
        self.mesh_note = QLabel()
        self.mesh_note.setWordWrap(True)
        self.mesh_note.setProperty("role", "muted")
        oform.addRow("", self.mesh_note)
        opt_tab = QWidget()
        ol = QVBoxLayout(opt_tab)
        ol.setContentsMargins(2, 2, 2, 2)
        ol.addLayout(oform)
        ol.addLayout(quick_row)
        ol.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        self.tabs.addTab(agg_tab, "Aggressor")
        self.tabs.addTab(phase_tab, "Phases")
        self.tabs.addTab(victim_tab, "Victims")
        self.tabs.addTab(opt_tab, "Options")
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

        # Plots: the waveforms, the H and E field maps.
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.plot_tabs = QTabWidget()
        self.plot_tabs.setUsesScrollButtons(True)
        self.canvases = {}
        for key, title in (("wave", "Waveforms"), ("H", "H field"), ("E", "E field")):
            fig = Figure(figsize=(6, 4), facecolor=CHART_SURFACE, layout="constrained")
            canvas = FigureCanvasQTAgg(fig)
            canvas.setMinimumSize(160, 100)
            self.canvases[key] = (fig, canvas)
            self.plot_tabs.addTab(canvas, title)
        self.wave_figure = self.canvases["wave"][0]

        # Results: the verdict, the ripple and victims tables, the radiated estimate, stats, notes.
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.export_button = QPushButton("Export…")
        self.export_button.setToolTip("Each solved aggressor's CSV, waveforms and field-map PNGs, and the "
                                      "summary JSON, named as `coppernicus noise` names them")
        self.export_button.setEnabled(False)
        self.ripple_table = _table(RIPPLE_HEADERS)
        self.ripple_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.victim_table = _table(VICTIM_HEADERS)
        self.victim_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.radiated = QLabel()
        self.radiated.setWordWrap(True)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        self.notes = QPlainTextEdit(readOnly=True)
        self.notes.setMinimumHeight(40)
        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(4, 4, 4, 4)
        head = QHBoxLayout()
        head.addWidget(self.header, 1)
        head.addWidget(self.export_button, 0, Qt.AlignTop)
        rl.addLayout(head)
        rl.addWidget(QLabel("<b>Ripple</b> (never judged here)"))
        rl.addWidget(self.ripple_table, 1)
        rl.addWidget(QLabel("<b>Victims</b>"))
        rl.addWidget(self.victim_table, 1)
        rl.addWidget(self.radiated)
        rl.addWidget(self.stats)
        rl.addWidget(self.notes, 1)
        results_scroll = QScrollArea()           # the dock is short on a laptop: scroll, never grow it
        results_scroll.setWidget(results)
        results_scroll.setWidgetResizable(True)
        results_scroll.setFrameShape(QScrollArea.NoFrame)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(scroll)
        split.addWidget(self.plot_tabs)
        split.addWidget(results_scroll)
        for i, stretch in enumerate((2, 3, 2)):
            split.setStretchFactor(i, stretch)
        split.setSizes([430, 520, 360])
        self.setup_area, self._split = scroll, split
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(split)

        # Wiring.
        self.aggressor_box.currentIndexChanged.connect(lambda *_: self._aggressor_shown())
        add_a.clicked.connect(self.add_aggressor)
        rem_a.clicked.connect(self.remove_aggressor)
        for key, w in self.field.items():
            w.editingFinished.connect(lambda k=key: self._field_edited(k))
        self.vrm_box.currentIndexChanged.connect(
            lambda *_: self._combo_edited("vrm", self.vrm_box.currentData()))
        self.topology_box.currentIndexChanged.connect(
            lambda *_: self._combo_edited("topology", self.topology_box.currentText()))
        self.probe_list.itemChanged.connect(self._probe_toggled)
        add_p.clicked.connect(self.add_phase)
        rem_p.clicked.connect(self.remove_phases)
        fill.clicked.connect(self.fill_from_board)
        self.phases.itemChanged.connect(self._phase_item_changed)
        add_v.clicked.connect(self.add_victim)
        rem_v.clicked.connect(self.remove_victims)
        guess.clicked.connect(self.guess_ends)
        use.clicked.connect(self.use_guessed_ends)
        self.victims.itemChanged.connect(self._victim_item_changed)
        for key, w in self.opt.items():
            if isinstance(w, QSpinBox):
                w.valueChanged.connect(lambda _v, k=key: self._option_edited(k))
            elif isinstance(w, QCheckBox):
                w.toggled.connect(lambda _on, k=key: self._option_edited(k))
            else:
                w.editingFinished.connect(lambda k=key: self._option_edited(k))
        self.quick.clicked.connect(self.use_quick_preset)
        kipi_default.clicked.connect(lambda: self.opt["field_per_decade"].setValue(KIPI_FIELD_PER_DECADE))
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
            keep = self.aggressor_box.currentText()
            self._agg_rows = copy.deepcopy(self.project.aggressors)
            self._victim_rows = copy.deepcopy(self.project.victims)
            self._guessed = {}
            self._fill_aggressor_box(keep)
            self._fill_victims()
            self._fill_options()
        finally:
            self._loading = False
        self._aggressor_shown()

    def _edited(self):
        """After any edit written to the project: merged again with the KiCad fields (what the
        next solve takes), dirty, and the shown result checked again."""
        self._win._remerge()
        self._win.dirty = True
        self._win._update_title()
        self._check_stale()

    def _say(self, text: str, bar: bool = False):
        self.status.setText(text)
        if bar:
            self._win.statusBar().showMessage(text)

    def _report(self, text: str) -> str:
        self._say(text, bar=True)
        return text

    # ------------------------------------------------------------------ aggressors
    def _index(self) -> int:
        i = self.aggressor_box.currentIndex()
        return i if 0 <= i < len(self._agg_rows) else -1

    def _fill_aggressor_box(self, keep: str = ""):
        self.aggressor_box.blockSignals(True)
        self.aggressor_box.clear()
        self.aggressor_box.addItems([a.name for a in self._agg_rows])
        i = self.aggressor_box.findText(keep)
        self.aggressor_box.setCurrentIndex(i if i >= 0 else (0 if self._agg_rows else -1))
        self.aggressor_box.blockSignals(False)

    def _aggressor_shown(self):
        """The current aggressor in the form, its phases and probes; then its last result."""
        i = self._index()
        a = self._agg_rows[i] if i >= 0 else None
        self._loading = True
        try:
            for key, w in self.field.items():
                w.setEnabled(a is not None)
                w.setText(aggressor_text(a, key) if a is not None else "")
                self._shown[key] = w.text()
            self.topology_box.setEnabled(a is not None)
            self.topology_box.setCurrentText(a.topology if a is not None else "auto")
            self._fill_vrm_box()
            self._fill_probes()
            self._fill_phases()
        finally:
            self._loading = False
        self._show_fsw_hint()
        self.show_result()

    def _fill_vrm_box(self):
        i = self._index()
        a = self._agg_rows[i] if i >= 0 else None
        names = switcher_names(self.merged_project)
        self.vrm_box.blockSignals(True)
        self.vrm_box.clear()
        if a is not None and not a.vrm:
            self.vrm_box.addItem("choose a switcher…", "")
        for n in names:
            self.vrm_box.addItem(n, n)
        if a is not None and a.vrm and a.vrm not in names:
            self.vrm_box.addItem(f"{a.vrm} (not a switcher source of the project)", a.vrm)
        self.vrm_box.setCurrentIndex(self.vrm_box.findData(a.vrm) if a is not None else -1)
        self.vrm_box.setEnabled(a is not None)
        self.vrm_box.blockSignals(False)

    def _show_fsw_hint(self):
        i = self._index()
        self.fsw_hint.setText(fsw_hint(self._agg_rows[i], self.merged_project, self._win.merged.from_fields)
                              if i >= 0 else "")
        from coppernicus.engine.ac import ac_max_edge
        edge = ac_max_edge(self.project.ac)
        given = "ac.max_edge_mm" if edge.origin == "project" else f"the {edge.origin}; ac.max_edge_mm"
        self.mesh_note.setText(f"AC mesh: largest edge {edge.mm:g} mm ({given}, the Impedance tab's "
                               "Sweep): a coarser one needs less memory")

    def _fill_probes(self):
        i = self._index()
        self.probe_list.blockSignals(True)
        self.probe_list.clear()
        if i >= 0:
            a = self._agg_rows[i]
            known = [p.name for p in self.merged_project.probes]
            for p in self.merged_project.probes:
                item = QListWidgetItem(f"{p.name} ({p.net})")
                item.setData(Qt.UserRole, p.name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if p.name in a.ripple_probes else Qt.Unchecked)
                self.probe_list.addItem(item)
            for n in a.ripple_probes:
                if n not in known:
                    item = QListWidgetItem(f"{n} (not a probe of the project: the solve refuses it)")
                    item.setData(Qt.UserRole, n)
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked)
                    item.setForeground(theme.brush("fail"))
                    self.probe_list.addItem(item)
        self.probe_list.blockSignals(False)

    def add_aggressor(self):
        names = {a.name for a in self._agg_rows}
        k = len(self._agg_rows) + 1
        while f"Aggressor {k}" in names:
            k += 1
        used = {a.vrm for a in self._agg_rows}
        vrm = next((n for n in switcher_names(self.merged_project) if n not in used), "")
        self._agg_rows.append(Aggressor(f"Aggressor {k}", vrm=vrm))
        self._fill_aggressor_box(f"Aggressor {k}")
        self._write_aggressors()
        self._aggressor_shown()
        self._report(f"Added Aggressor {k}: choose its source, enter its fsw and edges, then Fill from board "
                     "(Phases)")

    def remove_aggressor(self):
        i = self._index()
        if i < 0:
            return
        gone = self._agg_rows.pop(i)
        self.results.pop(gone.name, None)
        self._result_notes.pop(gone.name, None)
        self._fill_aggressor_box()
        self._write_aggressors()
        self._aggressor_shown()

    def _write_aggressors(self) -> List[str]:
        """Write the rows as the file can hold them; the notes of the current one."""
        saved = [savable_aggressor(a) for a in self._agg_rows]
        out = [s for s, _ in saved]
        if out != self.project.aggressors:
            self.project.aggressors = copy.deepcopy(out)
            self._edited()
        i = self._index()
        return saved[i][1] if i >= 0 else []

    def set_field_text(self, key: str, text: str):
        """Type ``text`` into the aggressor field ``key`` and commit it (for scripts)."""
        self.field[key].setText(text)
        self._field_edited(key)

    def _field_edited(self, key: str):
        i = self._index()
        w = self.field[key]
        if self._loading or i < 0 or w.text().strip() == self._shown.get(key):
            return                                # committed unchanged: the exact value stays
        old = self._agg_rows[i]
        try:
            q = with_aggressor_field(old, key, w.text())
            why = aggressor_conflict(self._agg_rows, i, q) if key == "name" else None
            if why:
                raise ValueError(why)
        except ValueError as e:
            self._say(f"Not applied: {e}")
            w.setText(aggressor_text(old, key))
            self._shown[key] = w.text()
            return
        self._agg_rows[i] = q
        if key == "name":
            self.results.pop(old.name, None)
            self._result_notes.pop(old.name, None)
            self.aggressor_box.blockSignals(True)
            self.aggressor_box.setItemText(i, q.name)
            self.aggressor_box.blockSignals(False)
        w.setText(aggressor_text(q, key))
        self._shown[key] = w.text()
        self._write_aggressors()
        self._show_fsw_hint()

    def _combo_edited(self, key: str, value):
        i = self._index()
        if self._loading or i < 0 or value is None or getattr(self._agg_rows[i], key) == value:
            return
        try:
            q = with_aggressor_field(self._agg_rows[i], key, value)
        except ValueError as e:
            self._say(f"Not applied: {e}")
            return
        self._agg_rows[i] = q
        self._write_aggressors()
        if key == "vrm":
            self._fill_vrm_box()
        self._show_fsw_hint()

    def _probe_toggled(self, item: QListWidgetItem):
        i = self._index()
        if self._loading or i < 0:
            return
        order = [p.name for p in self.merged_project.probes]
        self._agg_rows[i] = with_ripple_probe(self._agg_rows[i], item.data(Qt.UserRole),
                                              item.checkState() == Qt.Checked, order)
        self._write_aggressors()
        if item.data(Qt.UserRole) not in order:
            self._fill_probes()

    # ------------------------------------------------------------------ phases
    def _fill_phases(self):
        i = self._index()
        phases = self._agg_rows[i].phases if i >= 0 else []
        notes = savable_aggressor(self._agg_rows[i])[1] if i >= 0 else []
        self.phases.blockSignals(True)
        self.phases.setRowCount(len(phases))
        for r in range(len(phases)):
            self._decorate_phase_row(r, phases[r], next((n for n in notes if f"phase {r + 1}'s" in n), ""))
        self.phases.blockSignals(False)

    def _decorate_phase_row(self, r: int, ph: Phase, why: str):
        self.phases.blockSignals(True)
        for c, (_h, key) in enumerate(PHASE_COLUMNS):
            item = _set_item(self.phases, r, c, phase_cell(ph, key))
            if key in STAND_INS and getattr(ph, key) is None:
                item.setBackground(theme.brush("cell-missing"))
                item.setToolTip(STAND_INS[key])
            else:
                item.setBackground(_row_bg(why))
                item.setToolTip(why or PHASE_TIPS[key])
        self.phases.blockSignals(False)

    def add_phase(self):
        i = self._index()
        if i < 0:
            self._report("Add an aggressor first")
            return
        q = copy.deepcopy(self._agg_rows[i])
        q.phases.append(Phase())
        self._agg_rows[i] = q
        self._write_aggressors()
        self._fill_phases()

    def remove_phases(self):
        i = self._index()
        if i < 0 or not self._agg_rows[i].phases:
            return
        q = copy.deepcopy(self._agg_rows[i])
        rows = sorted({x.row() for x in self.phases.selectedIndexes()}, reverse=True) or [len(q.phases) - 1]
        for r in rows:
            q.phases.pop(r)
        self._agg_rows[i] = q
        self._write_aggressors()
        self._fill_phases()

    def _phase_item_changed(self, item: QTableWidgetItem):
        i, r, c = self._index(), item.row(), item.column()
        if self._loading or i < 0 or r >= len(self._agg_rows[i].phases):
            return
        key = PHASE_COLUMNS[c][1]
        try:
            q = edit_phase(self._agg_rows[i], r, key, item.text())
        except ValueError as e:
            self.phases.blockSignals(True)
            item.setBackground(theme.brush("cell-error"))
            item.setToolTip(f"Not applied: {e}")
            self.phases.blockSignals(False)
            self._say(f"Not applied: {e}")
            return
        self._agg_rows[i] = q
        notes = self._write_aggressors()
        why = next((n for n in notes if f"phase {r + 1}'s" in n), "")
        self._decorate_phase_row(r, q.phases[r], why)
        self._say(why)

    def fill_from_board(self) -> str:
        """The phases KiPI reads from the board for the aggressor's source (asking first when
        it has phases): its pins, the inductor and switch node, the capacitors to ground. Coss
        and Qrr are left empty: KiPI's stand-ins, and the victims are not judged until entered."""
        win, i = self._win, self._index()
        if win.board is None:
            return self._report("Open a board first")
        if i < 0:
            return self._report("Add an aggressor first")
        a = self._agg_rows[i]
        if not a.vrm:
            return self._report(f"{a.name}: choose its source (a switcher) first")
        if a.phases:
            answer = QMessageBox.question(
                self, "Fill from board", f"Replace the {len(a.phases)} phase(s) of {a.name} with those read "
                "from the board? Values entered in them (Coss, Qrr, …) are lost.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return self._report(f"{a.name}: phases kept as they were")
        from coppernicus.engine import EngineError
        from coppernicus.engine.noise import seed_phases
        try:
            phases = seed_phases(win.board, copy.deepcopy(self.merged_project), a.vrm, a.topology, a.vin_v)
        except EngineError as e:
            return self._report(f"Cannot fill from the board: {e}")
        if not phases:
            return self._report(f"{a.name}: KiPI found no phase on the board for {a.vrm}")
        q = copy.deepcopy(a)
        q.phases = phases
        self._agg_rows[i] = q
        self._write_aggressors()
        self._fill_phases()
        what = "; ".join(f"{ph.inductor or '?'}, switch node {ph.switch_net or '?'}, capacitors "
                         f"{', '.join(ph.input_caps + ph.output_caps) or 'none'}" for ph in phases)
        return self._report(f"Filled {len(phases)} phase(s) of {a.name} from the board ({what}). Coss and "
                            f"Qrr are not entered: KiPI's stand-ins ({KIPI_COSS_PF:g} pF, {KIPI_QRR_NC:g} "
                            "nC) apply and the victims are not judged until you enter them (amber cells)")

    # ------------------------------------------------------------------ victims
    def _fill_victims(self):
        problems = self._victim_problems()
        self.victims.blockSignals(True)
        self.victims.setRowCount(len(self._victim_rows))
        for r in range(len(self._victim_rows)):
            self._decorate_victim_row(r, problems.get(r, ""))
        self.victims.blockSignals(False)

    def _victim_problems(self) -> Dict[int, str]:
        try:
            return usable_victims(self._victim_rows)[1]
        except ProjectError as e:
            return {r: str(e) for r in range(len(self._victim_rows))}

    def _decorate_victim_row(self, r: int, why: str):
        v = self._victim_rows[r]
        guessed = self._guessed.get(r, {})
        self.victims.blockSignals(True)
        italic = QFont()
        italic.setItalic(True)
        for c, (_h, key) in enumerate(VICTIM_COLUMNS):
            end = key.split("_")[0] if key.startswith(("driver_ref", "driver_pins", "receiver_ref",
                                                        "receiver_pins")) else ""
            if end in guessed:
                ref, pins = guessed[end]
                text = ref if key.endswith("_ref") else pins_text(pins)
            else:
                text = victim_cell(v, key)
            item = _set_item(self.victims, r, c, text)
            flags = item.flags() | Qt.ItemIsEditable
            if key in TERMINATION_KEYS and v.preset != CUSTOM_PRESET or key == "preset":
                flags &= ~Qt.ItemIsEditable
            item.setFlags(flags)
            item.setFont(italic if end in guessed else QFont())
            if end in guessed:
                item.setBackground(theme.brush("cell-missing"))
                item.setToolTip(GUESS_TIP)
            else:
                item.setBackground(_row_bg(why))
                item.setToolTip(why or VICTIM_TIPS[key])
        combo = self.victims.cellWidget(r, 2)
        if not isinstance(combo, FieldComboBox):
            combo = FieldComboBox()
            for key, label in PRESET_CHOICES:
                combo.addItem(label, key)
            combo.setToolTip(VICTIM_TIPS["preset"])
            combo.currentIndexChanged.connect(lambda _i, w=combo: self._preset_changed(w))
            self.victims.setCellWidget(r, 2, combo)
        combo.blockSignals(True)
        combo.setCurrentIndex(max(combo.findData(v.preset), 0))
        combo.blockSignals(False)
        self.victims.blockSignals(False)

    def _victim_row_of(self, widget) -> int:
        return next((r for r in range(self.victims.rowCount())
                     if self.victims.cellWidget(r, 2) is widget), -1)

    def set_preset(self, r: int, preset: str):
        """Choose ``preset`` in row ``r``'s type (for scripts)."""
        combo = self.victims.cellWidget(r, 2)
        combo.setCurrentIndex(combo.findData(preset))

    def _preset_changed(self, combo):
        r = self._victim_row_of(combo)
        if self._loading or r < 0:
            return
        try:
            q, note = with_victim_cell(self._victim_rows[r], "preset", combo.currentData())
        except ValueError as e:
            self._say(f"Not applied: {e}")
            return
        self._victim_rows[r] = q
        problems = self._write_victims()
        self._decorate_victim_row(r, problems.get(r, ""))
        self._say(note or problems.get(r, ""))

    def add_victim(self):
        self._victim_rows.append(Victim(""))
        self._fill_victims()
        self.victims.setCurrentCell(len(self._victim_rows) - 1, 1)
        self._write_victims()

    def remove_victims(self):
        rows = sorted({x.row() for x in self.victims.selectedIndexes()}, reverse=True)
        if not rows and self._victim_rows:
            rows = [len(self._victim_rows) - 1]
        for r in rows:
            self._victim_rows.pop(r)
        # Guesses follow their rows.
        self._guessed = {r - sum(1 for g in rows if g < r): g for r, g in self._guessed.items()
                         if r not in rows}
        self.victims.setRowCount(0)                # the type combos are made again
        self._fill_victims()
        self._write_victims()

    def _victim_item_changed(self, item: QTableWidgetItem):
        r, c = item.row(), item.column()
        if self._loading or r >= len(self._victim_rows):
            return
        key = VICTIM_COLUMNS[c][1]
        if key == "preset":
            return
        try:
            q, note = with_victim_cell(self._victim_rows[r], key, item.text())
            why = victim_conflict(self._victim_rows, r, q)
            if why:
                raise ValueError(why)
        except ValueError as e:
            self.victims.blockSignals(True)
            item.setBackground(theme.brush("cell-error"))
            item.setToolTip(f"Not applied: {e}")
            self.victims.blockSignals(False)
            self._say(f"Not applied: {e}")
            return
        guessed = self._guessed.get(r, {})
        if key == "net":
            guessed.clear()                         # a guess was for the other net
        elif key.startswith(("driver_", "receiver_")):
            guessed.pop(key.split("_")[0], None)    # typed: entered by the user
        if not guessed:
            self._guessed.pop(r, None)
        self._victim_rows[r] = q
        problems = self._write_victims()
        self._decorate_victim_row(r, problems.get(r, ""))
        self._say(note or problems.get(r, ""))

    def _write_victims(self) -> Dict[int, str]:
        try:
            out, problems = usable_victims(self._victim_rows)
        except ProjectError as e:
            self._say(f"Not saved: {e}")
            return {}
        if out != self.project.victims:
            self.project.victims = copy.deepcopy(out)
            self._edited()
        return problems

    def _selected_victims(self) -> List[int]:
        rows = sorted({x.row() for x in self.victims.selectedIndexes()})
        if not rows and 0 <= self.victims.currentRow() < len(self._victim_rows):
            rows = [self.victims.currentRow()]
        return rows

    def guess_ends(self) -> str:
        """KiPI's guess at the selected victims' empty driver and receiver, shown in their
        cells and never written: a guessed end is a stand-in, so the victim is not judged."""
        board = self._win.board
        if board is None:
            return self._report("Open a board first")
        rows = self._selected_victims()
        if not rows:
            return self._report("Select the victims to guess the ends of")
        from coppernicus.engine.noise import guess_ends
        said = []
        for r in rows:
            v = self._victim_rows[r]
            need = [w for w in ("driver", "receiver") if not getattr(v, f"{w}_ref")]
            if not v.net or not need:
                continue
            if v.net not in board.nets():
                said.append(f"{v.label}: {v.net} is not on the board")
                continue
            got = dict(zip(("driver", "receiver"), guess_ends(board, v.net)))
            found = {w: got[w] for w in need if got[w] is not None}
            if found:
                self._guessed[r] = found
                shown = " → ".join(f"{found[w][0]}[{pins_text(found[w][1])}]" if w in found else
                                   f"{getattr(v, w + '_ref')} (entered)" for w in ("driver", "receiver"))
                said.append(f"{v.label}: {shown}")
            else:
                said.append(f"{v.label}: KiPI found no ends")
            self._decorate_victim_row(r, self._victim_problems().get(r, ""))
        if not said:
            return self._report("The selected victims have both ends entered: nothing to guess")
        return self._report("Guessed ends (KiPI: the net's two pads farthest apart) " + "; ".join(said)
                            + ". A guess is not entered: the victim is not judged until you enter its ends "
                            "(Use guessed ends, or type them)")

    def use_guessed_ends(self) -> str:
        """Enter the guessed ends shown in the selected rows (then they count as the user's)."""
        done = []
        for r in self._selected_victims():
            guessed = self._guessed.pop(r, None)
            if not guessed:
                continue
            q = copy.deepcopy(self._victim_rows[r])
            for w, (ref, pins) in guessed.items():
                setattr(q, f"{w}_ref", ref)
                setattr(q, f"{w}_pins", list(pins) if pins else None)
            self._victim_rows[r] = q
            done.append(q.label)
        if not done:
            return self._report("No guessed ends in the selected rows (Guess ends first)")
        problems = self._write_victims()
        for r in range(len(self._victim_rows)):
            self._decorate_victim_row(r, problems.get(r, ""))
        return self._report(f"Entered the guessed ends of {', '.join(done)}: they count as entered now")

    # ------------------------------------------------------------------ options
    def _fill_options(self):
        o = self.project.noise
        for key, w in self.opt.items():
            w.blockSignals(True)
            if isinstance(w, QSpinBox):
                w.setValue(int(getattr(o, key)))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(getattr(o, key)))
            else:
                w.setText(option_text(o, key))
                self._shown[f"opt:{key}"] = w.text()
            w.blockSignals(False)

    def _option_edited(self, key: str):
        if self._loading:
            return
        w = self.opt[key]
        if isinstance(w, QLineEdit) and w.text().strip() == self._shown.get(f"opt:{key}"):
            return
        value = (w.value() if isinstance(w, QSpinBox) else w.isChecked() if isinstance(w, QCheckBox)
                 else w.text())
        try:
            new = with_option(self.project.noise, key, value)
        except ValueError as e:
            self._say(f"Not applied: {e}")
            self._fill_options()
            return
        if new != self.project.noise:
            self.project.noise = new
            self._edited()
        self._fill_options()

    def use_quick_preset(self) -> str:
        """The quick preset, only on the user's press: QUICK_FIELD_PER_DECADE field solves per
        decade, set in the visible field (and saved with the project)."""
        self.opt["field_per_decade"].setValue(QUICK_FIELD_PER_DECADE)
        return self._report(f"Quick preset: {QUICK_FIELD_PER_DECADE} field solves per decade instead of "
                            f"KiPI's {KIPI_FIELD_PER_DECADE} (the result's assumptions say so)")

    # ------------------------------------------------------------------ results
    def _board_outline(self):
        if self._outline is None and self._win.board is not None:
            self._outline = self._win.board.outline()
        return self._outline or ()

    def _empty(self, key: str, text: str):
        fig, canvas = self.canvases[key]
        fig.clear()
        ax = fig.add_subplot()
        ax.axis("off")
        ax.text(0.5, 0.5, text, ha="center", va="center", color="#898781", transform=ax.transAxes, wrap=True)
        canvas.draw_idle()

    def _show_empty(self):
        self._empty("wave", "Solve an aggressor's noise to see its waveforms, ripple and victims")
        for kind in "HE":
            self._empty(kind, f"The near {kind} field shows here after a solve")
        self.header.setText("")
        self.ripple_table.setRowCount(0)
        self.victim_table.setRowCount(0)
        self.radiated.setText("")
        self.stats.setText("")
        self.notes.clear()
        self.export_button.setEnabled(False)

    def _draw_waveforms(self, r):
        from coppernicus.coupled_noise import draw_waveforms
        fig, canvas = self.canvases["wave"]
        fig.clear()
        draw_waveforms(fig, r)
        for ax in fig.axes:              # the core's plot, its text smaller: the dock is short
            ax.title.set_wrap(True)
            ax.title.set_fontsize(PLOT_FONT + 1)
            ax.yaxis.label.set_fontsize(PLOT_FONT)
            ax.xaxis.label.set_fontsize(PLOT_FONT)
            ax.tick_params(labelsize=PLOT_FONT)
            legend = ax.get_legend()
            if legend is not None:
                for text in legend.get_texts():
                    text.set_fontsize(PLOT_FONT - 1)
        canvas.draw_idle()

    def _draw_map(self, r, kind: str):
        from coppernicus.coupled_noise import draw_field_map
        fig, canvas = self.canvases[kind]
        fig.clear()
        ax = fig.add_subplot()
        img = draw_field_map(ax, r, kind, self._board_outline())
        if img is not None:
            unit = (r.emi.h_map if kind == "H" else r.emi.e_map).unit
            fig.colorbar(img, ax=ax, label=f"{kind} ({unit}, strongest harmonic)")
        ax.title.set_wrap(True)
        canvas.draw_idle()

    def _fill_rows(self, table, rows, verdict_col: int):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if row[verdict_col] == "FAIL":
                    item.setForeground(theme.brush("fail"))
                elif row[verdict_col] == "NOT JUDGED":
                    item.setForeground(theme.brush("warn"))
                table.setItem(i, c, item)

    def show_result(self):
        """The current aggressor's last result: plots, verdict line, rows, stats and notes."""
        i = self._index()
        entry = self.results.get(self._agg_rows[i].name) if i >= 0 else None
        if entry is None:
            self._show_empty()
            return
        r = entry[0]
        self._draw_waveforms(r)
        self._wave_title = self.wave_figure.axes[0].get_title() if self.wave_figure.axes else None
        for kind in "HE":
            self._draw_map(r, kind)
        self._fill_rows(self.ripple_table, ripple_rows(r), RIPPLE_HEADERS.index("Verdict"))
        self._fill_rows(self.victim_table, victim_rows(r), VICTIM_HEADERS.index("Verdict"))
        self.radiated.setText(radiated_text(r))
        self.stats.setText(stats_text(r.stats))
        left_out = self._result_notes.get(r.aggressor, "")
        head = f"Left out of this solve: {left_out}\n\n" if left_out else ""
        self.notes.setPlainText(head + notes_text(r))
        self.export_button.setEnabled(True)
        self._check_stale()

    def _check_stale(self):
        """The shown result flagged wherever it is read when the setup changed since its solve:
        the verdict line, each victim's verdict and the waveforms' title."""
        i = self._index()
        entry = self.results.get(self._agg_rows[i].name) if i >= 0 else None
        if entry is None:
            self.header.setText("")
            return
        r, solved = entry
        stale = project_key(self.merged_project) != solved
        self.header.setText(header_html(r, stale))
        col = VICTIM_HEADERS.index("Verdict")
        for k, row in enumerate(victim_rows(r, stale)):
            item = self.victim_table.item(k, col)
            if item is not None:
                item.setText(row[col])
        if self._wave_title is not None and self.wave_figure.axes:
            flag = "\n(the setup changed since this solve: solve again)" if stale else ""
            self.wave_figure.axes[0].set_title(self._wave_title + flag, fontsize=PLOT_FONT + 1, wrap=True)
            self.canvases["wave"][1].draw_idle()

    @property
    def stale(self) -> bool:
        i = self._index()
        entry = self.results.get(self._agg_rows[i].name) if i >= 0 else None
        return entry is not None and project_key(self.merged_project) != entry[1]

    def _setup_changed(self):
        """The setup changed elsewhere (the rail setup; the Impedance tab's probes, seen when
        this tab is shown): the switcher list, the probes and the fsw hint follow, and the shown
        result is checked again."""
        if not self._loading:
            self._fill_vrm_box()
            self._fill_probes()                     # a probe added in the Impedance tab
            self._show_fsw_hint()
        self._check_stale()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._busy:
            self._setup_changed()

    # ------------------------------------------------------------------ export
    def export_files(self, folder) -> List[Path]:
        """Every kept result as ``coppernicus noise`` writes it into ``folder``: per aggressor
        ``<stem>_<aggressor>_noise.csv`` and ``.png`` and the ``_h_field`` / ``_e_field`` PNGs,
        and ``<stem>_noise_summary.json`` over them."""
        from coppernicus.coupled_noise import noise_summary, write_csv, write_field_png, write_png
        from coppernicus.maps import safe_name
        names = [a.name for a in self._agg_rows]
        kept = {n: e for n, e in self.results.items() if n in names}     # removed or renamed: never
        if not kept:
            self._report("Nothing to export: solve an aggressor first")
            return []
        key = project_key(self.merged_project)
        stale = [n for n, (_r, solved) in kept.items() if solved != key]
        if stale:
            answer = QMessageBox.question(
                self, "Export results the setup changed since?",
                f"The setup changed since {', '.join(stale)} was solved: the result no longer matches the "
                "project. Export it anyway? The summary will say so.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self._report(f"Not exported: the setup changed since {', '.join(stale)} was solved (solve "
                             "again, or export anyway)")
                return []
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        board = self._win.board
        stem = export_stem(self.project.path, board.name if board else "")
        outline = self._board_outline()
        files, written, results = {}, [], []
        for name, (r, _key) in kept.items():
            base = folder / f"{stem}_{safe_name(name)}_noise"
            pngs = [write_png(r, base.with_name(base.name + ".png"))]
            maps = (r.emi.h_map, r.emi.e_map) if r.emi is not None else (None, None)
            pngs += [write_field_png(r, kind, base.with_name(f"{base.name}_{kind.lower()}_field.png"),
                                     outline) for kind, m in zip("HE", maps) if m is not None]
            csv_path = write_csv(r, base.with_name(base.name + ".csv"))
            files[name] = (csv_path.as_posix(), [p.as_posix() for p in pngs])
            written += [csv_path, *pngs]
            results.append(r)
        doc = noise_summary(results, files,
                            warnings=[f"{n}: the setup changed since this solve" for n in stale])
        summary = folder / f"{stem}_noise_summary.json"
        doc["summary_file"] = summary.as_posix()
        summary.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        written.append(summary)
        self._report(f"Wrote {len(written)} files in {folder} ({summary.name} and each aggressor's CSV and "
                     "PNGs)"
                     + (f"; the setup changed since {', '.join(stale)} was solved" if stale else ""))
        return written

    def _export_dialog(self):
        if not self.results:
            return
        path = self.project.path
        start = Path(path).parent / "results" if path else Path.home()
        folder = QFileDialog.getExistingDirectory(self, "Export the noise results to", str(start))
        if folder:
            self.export_files(folder)

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

    def add_solve_action(self, toolbar) -> QAction:
        """A "Solve noise" action (Ctrl+Shift+N) on the window's toolbar: shows this tab, then
        solves its aggressor."""
        act = QAction("Solve noise", self._win)
        act.setShortcut(QKeySequence("Ctrl+Shift+N"))
        act.setToolTip("Solve the ripple and coupled noise of the Ripple & noise tab's aggressor "
                       "(Ctrl+Shift+N)")
        act.triggered.connect(self.solve_shown)
        toolbar.addAction(act)
        return act

    def solve_shown(self):
        self._win.results_tabs.setCurrentWidget(self)
        self.solve()

    def solve_aggressor(self, name: str):
        """Show this tab with aggressor ``name`` chosen and solve it as :meth:`solve` does (the
        Tolerance tab's Solve button)."""
        i = next((k for k, a in enumerate(self._agg_rows) if a.name == name), -1)
        if i < 0:
            self._report(f"No aggressor {name} in the Ripple & noise tab")
            return
        self.aggressor_box.setCurrentIndex(i)
        self.solve_shown()

    def solve(self):
        """Mesh the aggressor's rails and ground as the solve will, show the estimate, ask when
        the memory could be exceeded, then solve; all off the UI thread, with progress and Cancel."""
        win = self._win
        if win.board is None:
            self._report("Open a board first")
            return
        if self._busy:
            self._report("A noise solve is already running")
            return
        i = self._index()
        if i < 0:
            self._report("Add an aggressor first (Add)")
            return
        name = self._agg_rows[i].name
        if win._refuse_fields("Cannot solve the noise"):         # a KiCad field cannot be used
            return
        self._busy, self._cancel_requested = True, False
        self.solve_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.last_estimate = ""
        self.estimate.setToolTip("")
        self.estimate.setText("Estimating: meshing the aggressor's rails and ground as the noise solve "
                              "will…")
        # What is solved: a copy of the merged project as it is now (the user may edit on).
        self._solving = (name, project_key(self.merged_project), copy.deepcopy(win.merged))
        self._solving_note = unsolved_text(self._agg_rows[i], self._victim_rows)
        self._start(NoiseWorker(win.board, self._solving[2], name, "size", parent=self))
        self._report(f"{name}: meshing its rails and ground as the noise solve will, for the estimate…"
                     + (f" · {self._solving_note}" if self._solving_note else ""))

    def _start(self, worker: NoiseWorker):
        worker.generation = self._generation
        worker.mesher = self._mesher = self._win.mesher_name()    # the estimate's and the solve's
        worker.message.connect(self._message)
        worker.fraction.connect(self._fraction)
        worker.partial.connect(self._partial)
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

    def _partial(self, res):
        """KiPI's waveforms, handed over before the board model and the victims: drawn at once
        (the tables keep the last result until the solve ends)."""
        if self._current() and self._solving and res.aggressor == self._solving[0]:
            i = self._index()
            if i >= 0 and self._agg_rows[i].name == res.aggressor:
                self._draw_waveforms(res)
                self._wave_title = None             # not the shown result's: never flagged as it

    def _sized(self, size):
        if not self._current():
            return
        # The sizing thread is done: its end must not end the solve (the question's own event
        # loop can deliver it before the solve starts).
        self.worker, generation = None, self._generation
        if self._cancel_requested:
            self._cancel_done()
            return
        from coppernicus.engine import noise as engine_noise
        from coppernicus.engine.ac import total_memory_bytes
        cost = engine_noise.noise_cost_estimate(size, self._last_solve())
        ram = engine_noise.noise_ram_estimate(size.unknowns, total_memory_bytes())
        self.last_estimate = estimate_text(size, cost, ram)
        self.estimate.setText(estimate_line(size, cost, ram))
        self.estimate.setToolTip(self.last_estimate)
        self.estimated.emit(self.last_estimate)
        yes = False
        if ram.risk:
            answer = QMessageBox.warning(
                self, "This noise solve could exceed the memory",
                f"{self.last_estimate}\n\nSolve anyway? It could make this computer swap; a coarser AC mesh "
                "(ac.max_edge_mm) or fewer field solves per decade (Options) lowers it.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            yes = answer == QMessageBox.Yes
        if generation != self._generation:          # another board was opened while asking
            return
        if self._cancel_requested:
            self._cancel_done()
            return
        if not may_start(ram, yes):
            self._report(f"Not solved: {size.aggressor} could exceed this machine's memory (see the "
                         "estimate)")
            self._busy_off()
            return
        self._busy = True
        self.solve_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self._start(NoiseWorker(self._win.board, self._solving[2], size.aggressor, "solve", parent=self))

    def _solved(self, res):
        if not self._current():
            return
        _name, solved, _merged = self._solving
        st = res.stats
        if all(st.get(k) is not None for k in _LAST_KEYS):     # a solve without field solves times nothing
            try:
                _settings().setValue(LAST_SOLVE_SETTING, json.dumps({k: float(st[k]) for k in _LAST_KEYS}))
            except (TypeError, ValueError):
                pass
        if res.aggressor not in [a.name for a in self._agg_rows]:
            self._report(f"{res.aggressor} solved, but it was renamed or removed meanwhile: its result is "
                         "dropped (solve it again)")
            return
        self.results[res.aggressor] = (res, solved)
        self._result_notes[res.aggressor] = self._solving_note
        self.results_changed.emit()
        # Never a change of selection, nor a refill of the editor the user may be in: the result
        # shows only where its aggressor is the one shown.
        i = self._index()
        if i < 0 or self._agg_rows[i].name != res.aggressor:
            self._report(f"{res.aggressor} solved: choose it to see the result")
            return
        self.show_result()
        verdict = {"fail": "FAIL", "pass": "PASS", "not_judged": "not judged"}.get(res.verdict, res.verdict)
        seconds = float(st.get("seconds") or 0.0)
        self._report(f"{res.aggressor} noise solved in {seconds:.1f} s: victims {verdict}"
                     f" · meshed with {mesher_shown(self._mesher)}")

    def _failed(self, message: str):
        if not self._current():
            return
        self._clear_estimating()
        self.show_result()                          # a partial drawn: back to the last result
        self._report("The noise could not be solved")
        QMessageBox.warning(self, "Cannot solve the noise", message)

    def _cancelled(self):
        if self._current():
            self._clear_estimating()
            self.show_result()                      # a partial drawn: back to the last result
            self._report("Noise solve cancelled")

    def _cancel_done(self):
        """A Cancel pressed before the solve phase started: nothing more starts."""
        self._clear_estimating()
        self._report("Noise solve cancelled")
        self._busy_off()

    def _clear_estimating(self):
        if not self.last_estimate:                 # still "Estimating…": no estimate came
            self.estimate.setText("")
            self.estimate.setToolTip("")

    def _worker_finished(self, worker: NoiseWorker):
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
        """Ask the running phase to stop (KiPI checks with each message and between victims).
        Solve stays off until its thread has ended."""
        if not self._busy:
            return
        self._cancel_requested = True              # kept for a phase not started yet
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self._report("Cancelling the noise solve…")

    # ------------------------------------------------------------------ board and window
    def board_changed(self):
        """A board (or project) was opened: nothing of the previous one survives."""
        self._generation += 1
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()            # its signals are now stale; Solve waits for its end
        else:
            self._busy = False
            self.solve_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
        self.results.clear()
        self._result_notes.clear()
        self.results_changed.emit()
        self._outline = None
        self.last_estimate = ""
        self.estimate.setText("")
        self.estimate.setToolTip("")
        self._say("")
        self.load_project()

    def close_workers(self):
        """The window is closing: a running solve is cancelled, nothing of it is shown, and the
        window waits for its thread to end, however long (KiPI checks for a cancel with each
        message, never inside a field solve)."""
        w = self.worker
        if w is not None and w.isRunning():
            with warnings.catch_warnings():               # a signal with no slot left to drop
                warnings.simplefilter("ignore", RuntimeWarning)
                for signal in (w.message, w.fraction, w.partial, w.sized, w.succeeded, w.failed,
                               w.cancelled, w.finished):
                    try:
                        signal.disconnect()
                    except (RuntimeError, TypeError):
                        pass
            w.cancel()
            self._win.statusBar().showMessage("Finishing the noise solve before closing…")
            self._win.statusBar().repaint()
            w.wait()
