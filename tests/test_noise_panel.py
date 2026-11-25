"""The Ripple & noise tab (Task 10b): the editor's form <-> project mapping, the pre-solve
estimate, the stats and results rows (pure logic, no window); and, in a child process, what
only a window shows (the tab, edits, Fill from board, Guess ends, the solve's question, Cancel
and closing). No noise solve runs here: a stub worker stands in; the solves are in
tests/test_noise.py and tools/gui_check.py (run_noise)."""

import dataclasses
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

pytest.importorskip("PySide6")

from coppernicus.project import (  # noqa: E402
    Aggressor,
    NoiseOptions,
    Phase,
    Project,
    ProjectError,
    Rail,
    Source,
    Victim,
)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
NOISE = os.path.join(ROOT, "examples", "noise", "noise.coppernicus.yml")


# ---------------------------------------------------------------------- numbers in the cells

def test_scaled_numbers_read_back_exactly_what_was_typed():
    from coppernicus.app.noise_panel import parse_scaled, scaled_text
    assert parse_scaled("", -6) is None and parse_scaled("  ", 3) is None
    assert parse_scaled("1.5", -9) == 1.5e-9 and parse_scaled("1000", 3) == 1e6
    assert parse_scaled("3,3", 0) == 3.3 and parse_scaled("2e3", -3) == pytest.approx(2.0)
    assert parse_scaled("300", -12) == 3e-10
    for value, exp in ((1e-6, -6), (3e-10, -12), (2e-9, -9), (1e6, 3), (0.05, 0), (5.0, 0)):
        assert parse_scaled(scaled_text(value, exp), exp) == pytest.approx(value), (value, exp)
    assert scaled_text(None, 3) == "" and scaled_text(1e6, 3) == "1000" and scaled_text(3e-10, -12) == "300"
    for bad in ("abc", "1x", "nan", "inf", "1 2"):
        with pytest.raises(ValueError):
            parse_scaled(bad, 0)


# ---------------------------------------------------------------------- the file's own checks

def test_check_noise_runs_the_project_files_checks_with_its_reasons():
    from coppernicus.app.noise_panel import check_noise
    check_noise([Aggressor("A", vrm="U1", fsw_hz=1e6)], [Victim("SIG", preset="cmos")])
    with pytest.raises(ProjectError, match="duty") as e:
        check_noise([Aggressor("A", duty=1.5)])
    assert not str(e.value).startswith("invalid project file")
    with pytest.raises(ProjectError, match="unique"):
        check_noise([Aggressor("A"), Aggressor("A")])
    with pytest.raises(ProjectError, match="unique"):
        check_noise([], [Victim("SIG", preset="cmos"), Victim("SIG", preset="lvds")])
    with pytest.raises(ProjectError, match="field_per_decade"):
        check_noise([], [], NoiseOptions(field_per_decade=0))


# ---------------------------------------------------------------------- aggressor form

def test_aggressor_fields_map_to_the_file_in_the_forms_units():
    from coppernicus.app.noise_panel import aggressor_text, with_aggressor_field
    a = Aggressor("U1 buck", vrm="U1", fsw_hz=1e6, rise_s=2e-9, fall_s=3e-9, vin_v=12.0)
    assert [aggressor_text(a, k) for k in ("name", "fsw_hz", "rise_s", "fall_s", "vin_v", "duty",
                                           "min_ring_zeta", "soft_edge_zeta")] == \
        ["U1 buck", "1000", "2", "3", "12", "", "0.05", "0.5"]
    q = with_aggressor_field(a, "fsw_hz", "500")
    assert q.fsw_hz == 5e5 and q.rise_s == 2e-9 and a.fsw_hz == 1e6        # a copy, one field
    assert with_aggressor_field(a, "rise_s", "1.5").rise_s == 1.5e-9
    assert with_aggressor_field(a, "fsw_hz", "").fsw_hz is None             # empty: not entered
    assert with_aggressor_field(a, "duty", "0.15").duty == 0.15
    assert with_aggressor_field(a, "topology", "buck").topology == "buck"
    for key, text, words in (("duty", "1.2", "duty"), ("fsw_hz", "-1", "fsw_hz"),
                             ("min_ring_zeta", "", "give a number"), ("name", " ", "name"),
                             ("topology", "flyback", "topology"), ("load_a", "x", "not a number")):
        with pytest.raises(ValueError, match=words):
            with_aggressor_field(a, key, text)


def test_noise_options_are_checked_by_the_file_and_quick_is_five_per_decade():
    from coppernicus.app.noise_panel import QUICK_FIELD_PER_DECADE, option_text, with_option
    o = NoiseOptions()
    assert option_text(o, "f_max_hz") == "" and option_text(o, "segment_mm") == "0.5"
    assert with_option(o, "f_max_hz", "500").f_max_hz == 5e8 and o.f_max_hz is None    # MHz, a copy
    assert with_option(o, "f_max_hz", "").f_max_hz is None
    assert with_option(o, "dead_time_ns", "0").dead_time_ns == 0.0
    assert with_option(o, "max_harmonics", 2000).max_harmonics == 2000
    assert with_option(o, "board_model", False).board_model is False
    for key, text, words in (("segment_mm", "0", "segment_mm"), ("window_mm", "", "give a number"),
                             ("field_per_decade", 0, "field_per_decade")):
        with pytest.raises(ValueError, match=words):
            with_option(o, key, text)
    assert QUICK_FIELD_PER_DECADE == 5 < NoiseOptions().field_per_decade


def test_renaming_an_aggressor_to_a_name_in_use_is_a_conflict():
    from coppernicus.app.noise_panel import aggressor_conflict, with_aggressor_field
    rows = [Aggressor("A"), Aggressor("B")]
    assert aggressor_conflict(rows, 1, with_aggressor_field(rows[1], "name", "C")) is None
    why = aggressor_conflict(rows, 1, with_aggressor_field(rows[1], "name", "A"))
    assert why and "unique" in why and "A" in why


def test_the_ripple_probes_keep_the_projects_order_and_any_unknown_name():
    from coppernicus.app.noise_panel import with_ripple_probe
    a = Aggressor("A", ripple_probes=["GONE"])
    a = with_ripple_probe(a, "LOAD", True, ["VIN", "LOAD"])
    a = with_ripple_probe(a, "VIN", True, ["VIN", "LOAD"])
    assert a.ripple_probes == ["VIN", "LOAD", "GONE"]
    assert with_ripple_probe(a, "GONE", False, ["VIN", "LOAD"]).ripple_probes == ["VIN", "LOAD"]


# ---------------------------------------------------------------------- the switcher and its fsw

def _switcher_project(fsw=None):
    return Project(rails=[
        Rail("VIN", sources=[Source("J1", 12.0, ["1"], ground_net="GND")]),
        Rail("VOUT", sources=[Source("L1", 1.8, ["2"], name="U1", kind="smps", efficiency=0.9,
                                     input_net="VIN", input_ref="U1", ground_net="GND",
                                     fsw_hz=fsw)]),
        Rail("V3", sources=[Source("U3", 3.3, ["2"], kind="ldo", input_net="VIN")])])


def test_the_source_combo_lists_the_switchers_by_their_engine_name():
    from coppernicus.app.noise_panel import switcher_names
    assert switcher_names(_switcher_project()) == ["U1"]
    p = _switcher_project()
    p.rails.append(Rail("V5", sources=[Source("L2", 5.0, ["2"], name="U1", kind="smps", efficiency=0.9)]))
    assert switcher_names(p) == ["U1 (VOUT)", "U1 (V5)"]          # terminal_names' unique names


def test_the_fsw_default_is_the_engines_with_where_it_comes_from():
    """The hint beside an empty fsw: the source's fsw_hz, from the project file or from the
    KiCad field PI_FSW (the merged project's rails from the fields) — as the engine reads it."""
    from coppernicus.app.noise_panel import fsw_default
    from coppernicus.engine.noise import _source_fsw
    p = _switcher_project(fsw=1.2e6)
    a = Aggressor("A", vrm="U1")
    assert fsw_default(p, "U1", []) == (1.2e6, "U1", "the project file")
    assert fsw_default(p, "U1", ["VOUT"]) == (1.2e6, "U1", "KiCad field PI_FSW")
    for rails, where in (([], "the project file"), (["VOUT"], "KiCad field PI_FSW")):
        hz, note = _source_fsw(p, a, rails)
        assert hz == fsw_default(p, "U1", rails)[0] and f"from the source U1 ({where})" in note
    assert fsw_default(_switcher_project(), "U1", []) == (None, "", "")
    assert fsw_default(p, "J1", [])[0] is None and fsw_default(p, "", [])[0] is None


# ---------------------------------------------------------------------- phases

def _agg():
    return Aggressor("U1", vrm="U1", fsw_hz=1e6, rise_s=2e-9, fall_s=2e-9, phases=[
        Phase(inductor="L1", inductance_h=1e-6, switch_net="SW", high_side_ref="U1",
              high_side_pins=["1"], high_side_net="VIN", low_side_ref="U1", low_side_pins=["2"],
              low_side_net="GND", input_caps=["C1"], output_caps=["C2"], coss_f=3e-10, qrr_nc=5.0)])


def test_phase_cells_show_and_read_the_files_fields_in_the_tables_units():
    from coppernicus.app.noise_panel import PHASE_COLUMNS, edit_phase, phase_cell
    ph = _agg().phases[0]
    cells = {key: phase_cell(ph, key) for _h, key in PHASE_COLUMNS}
    assert cells["inductance_h"] == "1" and cells["coss_f"] == "300" and cells["qrr_nc"] == "5"
    assert cells["high_side_pins"] == "1" and cells["input_caps"] == "C1"
    assert cells["rds_on_hs_ohm"] == "" and cells["inductor_shielded"] == "yes"
    assert cells["dcr_ohm"] == "0" and cells["inductor_axis"] == "vertical"
    a = _agg()
    q = edit_phase(a, 0, "coss_f", "450")
    assert q.phases[0].coss_f == 4.5e-10 and a.phases[0].coss_f == 3e-10      # a copy
    assert q.phases[0].inductance_h == 1e-6 and q.phases[0].qrr_nc == 5.0     # the rest as it was
    assert edit_phase(a, 0, "qrr_nc", "0").phases[0].qrr_nc == 0.0            # 0 is entered
    assert edit_phase(a, 0, "qrr_nc", "").phases[0].qrr_nc is None            # empty: stand-in
    assert edit_phase(a, 0, "output_caps", "C2, C3 C4").phases[0].output_caps == ["C2", "C3", "C4"]
    assert edit_phase(a, 0, "inductance_h", "2.2").phases[0].inductance_h == 2.2e-6
    assert edit_phase(a, 0, "high_side_pins", "all").phases[0].high_side_pins is None
    assert edit_phase(a, 0, "inductor_shielded", "no").phases[0].inductor_shielded is False
    for key, text, words in (("coss_f", "0", "coss_f"), ("inductor_axis", "diagonal", "inductor_axis"),
                             ("inductor_shielded", "maybe", "yes or no"), ("dcr_ohm", "-1", "dcr_ohm")):
        with pytest.raises(ValueError, match=words):
            edit_phase(a, 0, key, text)


def test_a_sides_part_and_net_can_be_typed_in_either_order():
    """The file saves a side's pins with its net and needs its part with the net: a half-typed
    side is kept in the row, not written, never refused (in either order)."""
    from coppernicus.app.noise_panel import edit_phase, savable_aggressor
    a = Aggressor("U1", phases=[Phase()])
    net_first = edit_phase(a, 0, "high_side_net", "VIN")
    saved, notes = savable_aggressor(net_first)
    assert net_first.phases[0].high_side_net == "VIN"
    assert saved.phases[0].high_side_net == "" and "phase 1" in notes[0] and "High-side" in notes[0]
    both = edit_phase(net_first, 0, "high_side_ref", "U1")
    saved, notes = savable_aggressor(both)
    assert (saved.phases[0].high_side_ref, saved.phases[0].high_side_net) == ("U1", "VIN") and notes == []
    part_first = edit_phase(edit_phase(a, 0, "low_side_ref", "U1"), 0, "low_side_pins", "2")
    saved, notes = savable_aggressor(part_first)
    assert saved.phases[0].low_side_ref == "" and saved.phases[0].low_side_pins is None and notes
    done = edit_phase(part_first, 0, "low_side_net", "GND")
    saved, notes = savable_aggressor(done)
    assert (saved.phases[0].low_side_ref, saved.phases[0].low_side_pins, saved.phases[0].low_side_net) \
        == ("U1", ["2"], "GND") and notes == []
    Project.from_dict(Project(aggressors=[savable_aggressor(net_first)[0]]).to_dict())   # loads back


# ---------------------------------------------------------------------- victims

def test_victim_cells_map_to_the_file_and_a_preset_owns_its_terminations():
    from coppernicus.app.noise_panel import VICTIM_COLUMNS, victim_cell, with_victim_cell
    v = Victim("SIG", preset="cmos", driver_ref="U5", driver_pins=["1"], logic_v=3.3,
               noise_margin_v=0.4)
    cells = {key: victim_cell(v, key) for _h, key in VICTIM_COLUMNS}
    assert (cells["name"], cells["net"], cells["preset"], cells["driver_ref"], cells["driver_pins"]) == \
        ("", "SIG", "cmos", "U5", "1")
    assert cells["receiver_pins"] == "" and cells["logic_v"] == "3.3" and cells["noise_margin_v"] == "400"
    q, note = with_victim_cell(v, "noise_margin_v", "250")
    assert q.noise_margin_v == 0.25 and v.noise_margin_v == 0.4 and note == ""
    with pytest.raises(ValueError, match="own terminations"):
        with_victim_cell(v, "driver_ohm", "50")
    custom, note = with_victim_cell(v, "preset", "custom")
    assert (custom.driver_ohm, custom.receiver_ohm, custom.receiver_pf) == (33.0, 1e6, 3.0)
    assert "33 Ω" in note
    custom, _ = with_victim_cell(custom, "termination_ohm", "50")
    named, note = with_victim_cell(custom, "preset", "lvds")
    assert named.driver_ohm is None and named.termination_ohm is None and "cleared" in note
    for key, text, words in (("logic_v", "0", "logic_v"), ("noise_margin_v", "-5", "noise_margin_v"),
                             ("preset", "ttl", "preset")):
        with pytest.raises(ValueError, match=words):
            with_victim_cell(v, key, text)


def test_the_custom_starting_terminations_are_kipis_cmos():
    from kipi.noise.model import VICTIM_PRESETS

    from coppernicus.app.noise_panel import CUSTOM_START
    cmos = VICTIM_PRESETS["cmos"]
    assert CUSTOM_START == {"driver_ohm": cmos.driver_ohm, "receiver_ohm": cmos.receiver_ohm,
                            "receiver_pf": cmos.receiver_pf}


def test_the_stand_ins_named_in_the_phases_table_are_kipis():
    from kipi.noise.model import DEFAULT_COSS_F, DEFAULT_QRR_C

    from coppernicus.app.noise_panel import KIPI_COSS_PF, KIPI_QRR_NC, STAND_INS
    assert KIPI_COSS_PF == pytest.approx(DEFAULT_COSS_F * 1e12)
    assert KIPI_QRR_NC == pytest.approx(DEFAULT_QRR_C * 1e9)
    assert "200 pF" in STAND_INS["coss_f"] and "5 nC" in STAND_INS["qrr_nc"]


def test_drafts_and_duplicate_victims_stay_in_the_table_only():
    from coppernicus.app.noise_panel import usable_victims, victim_conflict, with_victim_cell
    rows = [Victim("SIG", preset="cmos"), Victim("", preset=""), Victim("SIG2", preset=""),
            Victim("SIG", name="", preset="lvds"), Victim("OTHER", preset="hiz", driver_pins=["1"])]
    out, problems = usable_victims(rows)
    assert [v.net for v in out] == ["SIG", "OTHER"] and out[1].driver_pins is None
    assert problems[1].startswith("draft") and problems[2].startswith("draft") and "preset" in problems[2]
    assert "unique" in problems[3]
    assert problems[4].startswith("not saved yet") and "Driver pins" in problems[4]
    why = victim_conflict(rows, 2, with_victim_cell(rows[2], "net", "SIG")[0])
    assert why and "unique" in why
    assert victim_conflict(rows, 2, with_victim_cell(rows[2], "name", "second")[0]) is None


# ---------------------------------------------------------------------- estimate, stats, rows

def _size(board_model=True, note=""):
    from coppernicus.engine.noise import NoiseSize
    if not board_model:
        return NoiseSize("U1", False, 0, 0, 0, 0, 1600, 1.6e9, 0.0, note)
    return NoiseSize("U1", True, 12000, 6500, 7, 34, 1600, 1.6e9, 0.4)


def test_the_estimate_text_joins_the_cost_harmonics_and_memory():
    from coppernicus.app.noise_panel import estimate_text, may_start
    from coppernicus.engine.noise import noise_cost_estimate, noise_ram_estimate
    size = _size()
    ram = noise_ram_estimate(size.unknowns, 24e9)
    text = estimate_text(size, noise_cost_estimate(size), ram)
    assert text.startswith("Before solving: 12,000 triangles × 7 ports")
    assert "1600 harmonics to 1.6 GHz" in text
    assert ram.text in text and "could exceed" not in text and may_start(ram, False)
    risky = noise_ram_estimate(size.unknowns, 1e8)
    text = estimate_text(size, noise_cost_estimate(size), risky)
    assert "It could exceed this machine's memory" in text
    assert not may_start(risky, False) and may_start(risky, True)
    off = _size(False, "Board model left out (no ground pads)")
    text = estimate_text(off, noise_cost_estimate(off), noise_ram_estimate(0, 24e9))
    assert "no field solve" in text and "Board model left out (no ground pads)" in text
    assert "0 triangles" not in text
    text = estimate_text(_size(False), noise_cost_estimate(_size(False)), noise_ram_estimate(0, 24e9))
    assert "board model off (noise.board_model)" in text


def test_stats_text_names_the_memory_peak_and_leaves_out_what_was_not_measured():
    from coppernicus.app.noise_panel import stats_text
    st = {"seconds": 2.04, "harmonics": 1600.0, "triangles": 12000.0, "unknowns": 11000.0, "ports": 7.0,
          "field_solves": 34.0, "field_seconds": 0.8, "memory_peak_mb": 612.0, "memory_peak_raised": True}
    text = stats_text(st)
    assert text.startswith("solved in 2.0 s") and "11,000 unknowns" in text
    assert "34 field solves in 0.8 s" in text
    assert "memory peak 612 MB (this solve raised the process's peak)" in text
    st.update(memory_peak_raised=False, triangles=None, unknowns=None, ports=None, field_solves=None,
              field_seconds=None)
    text = stats_text(st)
    assert "None" not in text and "triangles" not in text and "the process's peak so far" in text
    assert "cannot be read" in stats_text({"seconds": 1.0, "memory_peak_mb": None})


def _ripple(name, where, pp_bw):
    from coppernicus.results import RippleResult
    z = np.zeros(4)
    return RippleResult(name, where, "VOUT", "GND", "U2", ["1"], 0.004, 0.006, pp_bw / 2, pp_bw / 2,
                        0.010, pp_bw, 0.003, z, z, z)


def _victim(name="V", peak=0.5, margin=0.8, stand_ins=(), ends=True):
    from coppernicus.results import VictimResult
    return VictimResult(name, "SIG", "U5[1]", "U6[1]", ends, "cmos", margin,
                        "noise_margin_v" if margin is not None else "",
                        "noise_margin_v (V)" if margin else "",
                        peak, {"capacitive": peak / 4, "inductive": peak, "common": 0.001}, "inductive",
                        peak / margin if margin else None, [], 10.0, 5.0, {}, {}, [], np.zeros(4),
                        stand_ins=list(stand_ins))


def _result(victims=(), ripple=()):
    from coppernicus.coupled_noise import judge
    from coppernicus.results import NearField, NoiseResult
    r = NoiseResult("U1", "U1", "VOUT", "VIN", "GND", "buck", 12.0, 1.8, 5.0, 0.83, 0.15, 0.9, 1,
                    [], 1e6, 2e-9, 2e-9, 1.6e9, 1600, False, [1.5], [], np.zeros(4),
                    [np.zeros(4)], [np.zeros(4)], [""], ripple=list(ripple), victims=list(victims))
    r.emi = NearField(5.0, 3.0, radiated_dbuv_m=31.24, radiated_hz=210e6, cispr32_b_dbuv_m=30.0)
    return judge(r)


def test_ripple_rows_put_the_20_mhz_column_first_and_are_never_judged():
    from coppernicus.app.noise_panel import RIPPLE_HEADERS, ripple_rows
    rows = ripple_rows(_result(ripple=[_ripple("LOAD", "probe", 0.0047),
                                       _ripple("output", "device pins", 0.269)]))
    assert RIPPLE_HEADERS[3] == "p-p, 20 MHz" and RIPPLE_HEADERS.index("p-p, 20 MHz") < RIPPLE_HEADERS.index(
        "p-p, full band")
    assert rows[0][:4] == ("LOAD", "probe", "VOUT − GND", "4.7 mV") and rows[1][3] == "269 mV"
    assert {row[-1] for row in rows} == {"not judged"}


def test_victim_rows_give_peak_margin_verdict_and_why():
    from coppernicus.app.noise_panel import VICTIM_HEADERS, victim_rows
    guess = _victim("guess", 1.6, stand_ins=["driver and receiver not both entered: x"], ends=False)
    r = _result([_victim("ok", 0.4), _victim("bad", 0.9), guess, _victim("free", 0.2, margin=None)])
    rows = {row[0]: dict(zip(VICTIM_HEADERS, row)) for row in victim_rows(r)}
    assert (rows["ok"]["Peak"], rows["ok"]["Margin"], rows["ok"]["% of margin"], rows["ok"]["Verdict"]) == \
        ("400 mV", "800 mV", "50", "PASS")
    assert rows["bad"]["Verdict"] == "FAIL" and "above its margin" in rows["bad"]["Why"]
    assert rows["guess"]["Verdict"] == "NOT JUDGED" and rows["guess"]["Why"].startswith("driver and receiver")
    assert rows["guess"]["Ends"].endswith("(guessed by KiPI)") and rows["ok"]["Ends"].endswith("(entered)")
    assert rows["free"]["Verdict"] == "no margin (ranked)" and rows["free"]["Margin"] == "-"
    assert rows["ok"]["Mostly"] == "inductive" and rows["ok"]["Capacitive"] == "100 mV"


def test_the_radiated_estimate_is_for_scale_and_the_header_never_judges_ripple():
    from coppernicus.app.noise_panel import header_html, radiated_text
    r = _result([_victim("bad", 0.9)])
    text = radiated_text(r)
    assert "31.2 dBµV/m at 210 MHz" in text and "CISPR 32 class B 30 dBµV/m" in text
    assert text.endswith("for scale, not judged")
    head = header_html(r, stale=False)
    assert "FAIL" in head and "ripple and the radiated estimate are never judged" in head
    assert "Setup changed" in header_html(r, stale=True)
    r.emi = None
    assert radiated_text(r) == ""


def test_a_result_goes_stale_on_a_setup_edit_not_on_a_save_a_marked_net_or_a_dc_limit():
    from coppernicus.app.noise_panel import project_key
    p = Project.load(NOISE)
    key = project_key(p)
    p.board, p.marked_rails = "/elsewhere/noise.kipi.json.gz", ["VOUT"]
    p.rails[1].limits.max_drop_pct = 1.0
    p.thermal = dataclasses.replace(p.thermal, enabled=True)
    assert project_key(p) == key
    p.victims[0].noise_margin_v = 0.3
    assert project_key(p) != key


def test_the_export_names_are_the_clis():
    """cli.py _cmd_noise: <project file stem, .coppernicus included>_<aggressor>_noise.csv/.png,
    _h_field.png / _e_field.png beside it, <stem>_noise_summary.json."""
    from coppernicus.app.noise_panel import export_stem
    assert export_stem("/x/noise.coppernicus.yml", "noise.kipi.json.gz") == "noise.coppernicus"
    assert export_stem(None, "board.kicad_pcb") == "board"


# ---------------------------------------------------------------------- in a window (child process)

PRELUDE = '''
from PySide6 import QtCore, QtWidgets
from coppernicus.app.main_window import MainWindow

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MainWindow()
window.dirty = False
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_noise_tab_follows_the_impedance_tab_and_opening_changes_nothing():
    run_in_window(f'''
        tabs = [window.results_tabs.tabText(i) for i in range(window.results_tabs.count())]
        assert tabs[tabs.index("Power tree") + 1] == "Impedance", tabs
        assert tabs[tabs.index("Impedance") + 1] == "Ripple & noise", tabs
        bar = window.findChild(QtWidgets.QToolBar, "Main")
        act = next(a for a in bar.actions() if a.text() == "Solve noise")
        assert act.shortcut().toString() == "Ctrl+Shift+N"
        act.trigger()                                         # no board yet: says so, shows the tab
        assert window.results_tabs.currentWidget() is window.noise_panel
        assert "Open a board" in window.statusBar().currentMessage()
        window.show()
        app.processEvents()
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        before = window.project.to_dict()
        window.results_tabs.setCurrentWidget(panel)
        app.processEvents()
        assert panel.aggressor_box.currentText() == "U1" and panel.vrm_box.currentData() == "U1"
        assert panel.field["fsw_hz"].text() == "1000" and panel.field["rise_s"].text() == "2"
        assert panel.phases.rowCount() == 1 and panel.phases.item(0, 0).text() == "L1"
        assert panel.victims.rowCount() == 3 and panel.victims.item(1, 1).text() == "SIG_LOOP"
        probes = [panel.probe_list.item(i) for i in range(panel.probe_list.count())]
        assert [(i.data(QtCore.Qt.UserRole), i.checkState() == QtCore.Qt.Checked) for i in probes] == \\
            [("LOAD", True)]
        for w in list(panel.field.values()) + list(panel.opt.values()):
            if hasattr(w, "editingFinished"):
                w.editingFinished.emit()                      # committed unchanged: no edit
        assert window.dirty is False and window.project.to_dict() == before
        with_tab = window.minimumSizeHint()                  # the laptop fit: the tab grows nothing
        window.results_tabs.removeTab(window.results_tabs.indexOf(panel))
        app.processEvents()
        without = window.minimumSizeHint()
        assert with_tab.height() <= without.height() and with_tab.width() <= without.width(), (
            with_tab, without)
    ''')


def test_edits_write_the_project_merge_again_and_conflicts_are_refused():
    run_in_window(f'''
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.set_field_text("fsw_hz", "500")
        assert window.project.aggressors[0].fsw_hz == 5e5 and window.dirty
        assert window.merged.project.aggressors[0].fsw_hz == 5e5           # merged again
        panel.set_field_text("duty", "1.5")                                # refused: kept
        assert window.project.aggressors[0].duty is None and "Not applied" in panel.status.text()
        assert panel.field["duty"].text() == ""
        panel.victims.item(0, 8).setText("300")                           # margin (mV)
        assert window.project.victims[0].noise_margin_v == 0.3
        panel.victims.item(1, 1).setText("SIG_SW")                        # SIG_LOOP -> a net in use
        assert [v.label for v in window.project.victims] == ["SIG_SW", "SIG_LOOP", "SIG_BURIED"]
        assert "Not applied" in panel.status.text() and "unique" in panel.status.text()
        panel.phases.item(0, 12).setText("")                              # Coss: not entered
        assert window.project.aggressors[0].phases[0].coss_f is None
        assert panel.phases.item(0, 12).toolTip().startswith("Not entered: KiPI's")
        panel.add_aggressor()
        assert [a.name for a in window.project.aggressors] == ["U1", "Aggressor 2"]
        panel.set_field_text("name", "U1")                                 # a name in use
        assert [a.name for a in window.project.aggressors] == ["U1", "Aggressor 2"]
        assert "unique" in panel.status.text()
        panel.remove_aggressor()
        assert [a.name for a in window.project.aggressors] == ["U1"]
        panel.use_quick_preset()
        assert window.project.noise.field_per_decade == 5 and panel.opt["field_per_decade"].value() == 5
        panel.add_victim()
        assert len(window.project.victims) == 3                           # a draft is not written
        panel.victims.item(3, 1).setText("SIG_NEW")
        panel.set_preset(3, "analog")
        assert window.project.victims[-1].net == "SIG_NEW" and window.project.victims[-1].preset == "analog"
    ''')


def test_fill_from_board_asks_first_and_its_stand_ins_are_marked():
    run_in_window(f'''
        from PySide6.QtWidgets import QMessageBox
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        asked = []
        QMessageBox.question = staticmethod(lambda *a, **k: asked.append((a[1], k, a[3:])) or QMessageBox.No)
        before = window.project.to_dict()
        panel.fill_from_board()
        assert asked and window.project.to_dict() == before and not window.dirty
        assert asked[0][2][-1] == QMessageBox.No or asked[0][1].get("defaultButton") == QMessageBox.No
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        panel.fill_from_board()
        ph = window.project.aggressors[0].phases[0]
        assert (ph.inductor, ph.switch_net, ph.input_caps, ph.output_caps) == ("L1", "SW", ["C1"], ["C2"])
        assert ph.coss_f is None and ph.qrr_nc is None and window.dirty
        assert "not judged" in panel.status.text()
        assert panel.phases.item(0, 12).toolTip().startswith("Not entered: KiPI's")
        assert panel.phases.item(0, 13).toolTip().startswith("Not entered: KiPI's")
    ''')


def test_guessed_ends_are_shown_not_written_until_used():
    run_in_window(f'''
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.victims.item(1, 3).setText("")                              # driver part cleared
        panel.victims.item(1, 5).setText("")                              # receiver part cleared
        v = window.project.victims[1]
        assert (v.driver_ref, v.receiver_ref) == ("", "")
        panel.victims.setCurrentCell(1, 0)
        panel.guess_ends()
        assert (panel.victims.item(1, 3).text(), panel.victims.item(1, 5).text()) == ("U5", "U6")
        assert "guessed" in panel.victims.item(1, 3).toolTip()
        v = window.project.victims[1]
        assert (v.driver_ref, v.receiver_ref) == ("", ""), v                # still not entered
        assert "not judged" in panel.status.text()
        panel.use_guessed_ends()
        v = window.project.victims[1]
        assert (v.driver_ref, v.driver_pins, v.receiver_ref, v.receiver_pins) == ("U5", ["1"], "U6", ["1"])
        assert "guessed" not in panel.victims.item(1, 3).toolTip()
    ''')


STUB_WORKER = '''
import time
from PySide6.QtWidgets import QMessageBox
import shiboken6
import coppernicus.app.noise_panel as noise_panel
import coppernicus.engine.noise as engine_noise
from coppernicus.engine.noise import NoiseSize, RamEstimate


class StubWorker(QtCore.QThread):
    """NoiseWorker's signals and phases without KiPI: sizing reports a small mesh at once (or
    fails); solving hands over a partial result, then runs until it is cancelled."""
    message = QtCore.Signal(str)
    fraction = QtCore.Signal(object)
    partial = QtCore.Signal(object)
    sized = QtCore.Signal(object)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()
    fail_sizing = False
    partial_result = None

    def __init__(self, board, merged, name, phase, parent=None):
        super().__init__(parent)
        self.merged, self.name, self.phase, self.generation, self._cancel = merged, name, phase, 0, False

    def cancel(self):
        self._cancel = True

    def run(self):
        if self.phase == "size":
            if StubWorker.fail_sizing:
                self.failed.emit("the mesher failed")
            else:
                self.sized.emit(NoiseSize(self.name, True, 1000, 600, 4, 34, 1600, 1.6e9, 0.1))
            return
        if StubWorker.partial_result is not None:
            self.partial.emit(StubWorker.partial_result)
        while not self._cancel:
            time.sleep(0.01)
        self.cancelled.emit()


noise_panel.NoiseWorker = StubWorker


def pump(done, seconds=10.0):
    t0 = time.monotonic()
    while not done() and time.monotonic() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)
'''


def test_solve_takes_a_copy_of_the_merged_project_and_a_field_error_refuses():
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        started, boxes = [], []
        StubWorker.start = lambda self: started.append(self)
        QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[1]) or QMessageBox.Ok)
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.set_field_text("rise_s", "3")
        panel.solve()
        solved = started[-1].merged.project
        assert started[-1].phase == "size" and started[-1].name == "U1"
        assert solved is not window.project and solved is not window.merged.project     # a copy
        assert solved.aggressors[0].rise_s == 3e-9
        assert panel.busy and panel.cancel_button.isEnabled() and not panel.solve_button.isEnabled()
        assert panel.estimate.text().startswith("Estimating")
        panel._busy = False
        n = len(started)
        window.fields_error = "a field cannot be used"
        panel.solve()
        assert len(started) == n and boxes == ["Cannot solve the noise"], boxes
    '''))


def test_a_solve_that_could_exceed_the_memory_starts_only_on_yes():
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        asked = []
        QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.No)
        forced = RamEstimate(9e12, 8e9, True, "up to 9000 GB (forced)")
        engine_noise.noise_ram_estimate = lambda *a, **k: forced
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        heard = []
        panel.estimated.connect(heard.append)
        panel.solve()
        pump(lambda: not panel.busy)
        assert asked == ["This noise solve could exceed the memory"] and panel.worker is None, asked
        assert heard and "could exceed" in heard[0] and "1,000 triangles" in heard[0], heard
        assert "Not solved" in panel.status.text() and panel.results == {{}}
    '''))


def test_solve_anyway_answered_yes_solves_busy_with_cancel_and_one_solve_at_a_time():
    """The RAM question's own event loop delivers the sizing thread's end before Yes comes
    back: the solve that follows must still be busy, cancellable, and the only one. A partial
    result drawn during the solve is replaced by the empty state once it is cancelled."""
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        import numpy as np
        from coppernicus.results import NoiseResult

        def nested_yes(*_a, **_k):
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(300, loop.quit)
            loop.exec()
            return QMessageBox.Yes

        QMessageBox.warning = staticmethod(nested_yes)
        forced = RamEstimate(9e12, 8e9, True, "up to 9000 GB (forced)")
        engine_noise.noise_ram_estimate = lambda *a, **k: forced
        t = np.linspace(0, 1e-6, 8, endpoint=False)
        StubWorker.partial_result = NoiseResult(
            "U1", "U1", "VOUT", "VIN", "GND", "buck", 12.0, 1.8, 5.0, 0.83, 0.15, 0.9, 1, [], 1e6, 2e-9,
            2e-9, 1.6e9, 1600, False, [1.5], [], t, [np.ones(8)], [np.ones(8)], [""], partial=True)
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.solve()
        pump(lambda: panel.worker is not None and panel.worker.phase == "solve")
        solving = panel.worker
        assert solving is not None and solving.phase == "solve", solving
        pump(lambda: bool(panel.wave_figure.axes) and "partial" in panel.wave_figure.axes[0].get_title(), 2)
        assert panel.wave_figure.axes and "partial" in panel.wave_figure.axes[0].get_title()
        pump(lambda: False, 0.3)                              # the sizing thread's end, delivered
        assert panel.busy and panel.cancel_button.isEnabled() and not panel.solve_button.isEnabled()
        panel.solve()
        assert panel.worker is solving and "already running" in panel.status.text()
        panel.cancel()
        pump(lambda: not panel.busy)
        assert not panel.busy and panel.solve_button.isEnabled() and panel.worker is None
        assert "cancelled" in panel.status.text(), panel.status.text()
        assert not any("partial" in ax.get_title() for ax in panel.wave_figure.axes)   # not left drawn
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        assert not shiboken6.isValid(solving)                 # finished workers are deleted
    '''))


def test_a_cancel_between_sizing_and_its_slot_is_kept_and_a_failed_sizing_clears_the_estimate():
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        boxes = []
        QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[1]) or QMessageBox.Ok)
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.solve()
        panel.worker.wait()                                   # sized is queued, not yet delivered
        panel.cancel()
        pump(lambda: not panel.busy)
        assert panel.worker is None and not panel.busy, panel.worker
        assert "cancelled" in panel.status.text() and panel.estimate.text() == "", panel.estimate.text()
        assert boxes == []
        StubWorker.fail_sizing = True
        panel.solve()
        pump(lambda: not panel.busy)
        assert boxes == ["Cannot solve the noise"] and panel.estimate.text() == "", panel.estimate.text()
    '''))


def test_closing_waits_for_a_noise_solve_and_leaves_none_of_its_signals_connected():
    """No 'QThread: Destroyed while thread is still running': the window waits for the end
    (no timeout), saying so in the status bar; nothing the solve sends afterwards is shown."""
    run_in_window('''
        import time

        class Slow(QtCore.QThread):
            message = QtCore.Signal(str)
            fraction = QtCore.Signal(object)
            partial = QtCore.Signal(object)
            sized = QtCore.Signal(object)
            succeeded = QtCore.Signal(object)
            failed = QtCore.Signal(str)
            cancelled = QtCore.Signal()
            stopped = False

            def cancel(self):
                self.stopped = True                            # ignored for a while, as KiPI may

            def run(self):
                time.sleep(5.5)
                self.failed.emit("late")

        heard, said = [], []
        window.statusBar().messageChanged.connect(said.append)
        worker = window.noise_panel.worker = Slow()
        for signal in (worker.message, worker.failed, worker.succeeded, worker.partial):
            signal.connect(heard.append)
        worker.start()
        window.close()
        app.processEvents()
        assert not worker.isRunning(), "the window closed while the solve still ran"
        assert worker.stopped and heard == [], heard
        assert any("Finishing the noise solve" in m for m in said), said
    ''')
