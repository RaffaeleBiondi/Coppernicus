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
    from coppernicus.app.noise_panel import CUSTOM_START
    from coppernicus.engine.noise import VICTIM_PRESETS  # KiPI's, as the engine holds them
    cmos = VICTIM_PRESETS["cmos"]
    assert CUSTOM_START == {"driver_ohm": cmos.driver_ohm, "receiver_ohm": cmos.receiver_ohm,
                            "receiver_pf": cmos.receiver_pf}


def test_the_stand_ins_named_in_the_phases_table_are_kipis():
    from coppernicus.app.noise_panel import KIPI_COSS_PF, KIPI_QRR_NC, STAND_INS
    from coppernicus.engine.noise import DEFAULT_COSS_F, DEFAULT_QRR_C  # as the engine holds them
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


def test_the_estimate_line_is_short_and_keeps_the_numbers():
    """The label shows the numbers on one short line (the full text is its tooltip and the
    memory question's), so the editor stays in sight in a short dock."""
    from coppernicus.app.noise_panel import estimate_line
    from coppernicus.engine.noise import noise_cost_estimate, noise_ram_estimate
    size = _size()
    last = {"seconds": 2.0, "field_seconds": 0.8, "triangles": 12000.0, "ports": 7.0, "field_solves": 34.0}
    line = estimate_line(size, noise_cost_estimate(size, last), noise_ram_estimate(size.unknowns, 24e9))
    assert line == ("Before solving: 12,000 triangles × 7 ports, 34 field solves, about 2 s; 1600 harmonics "
                    "to 1.6 GHz; memory up to 0.61 GB of 24 GB (hover for how)")
    risky = estimate_line(size, noise_cost_estimate(size), noise_ram_estimate(size.unknowns, 1e8))
    assert "34 field solves;" in risky and "of 0.1 GB: it could exceed this machine's memory" in risky
    off = _size(False, "Board model left out (no ground pads)")
    line = estimate_line(off, noise_cost_estimate(off), noise_ram_estimate(0, None))
    assert line == ("Before solving: 1600 harmonics to 1.6 GHz; no field solve (Board model left out (no "
                    "ground pads)); memory up to 0.2 GB (hover for how)")


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


def test_a_stale_result_never_reads_a_clean_verdict():
    from coppernicus.app.noise_panel import VICTIM_HEADERS, victim_rows
    r = _result([_victim("ok", 0.4), _victim("bad", 0.9), _victim("free", 0.2, margin=None)])
    verdict = VICTIM_HEADERS.index("Verdict")
    assert [row[verdict] for row in victim_rows(r)] == ["PASS", "FAIL", "no margin (ranked)"]
    assert [row[verdict] for row in victim_rows(r, stale=True)] == [
        "PASS (setup changed)", "FAIL (setup changed)", "no margin (ranked) (setup changed)"]


def test_the_rows_a_solve_leaves_out_are_named():
    from coppernicus.app.noise_panel import unsolved_text
    rows = [Victim("SIG", preset="cmos"), Victim("SIG2", preset=""), Victim("SIG", preset="lvds"),
            Victim("OTHER", preset="hiz", driver_pins=["1"])]
    a = Aggressor("U1", phases=[Phase(high_side_net="VIN")])
    text = unsolved_text(a, rows)
    assert text.startswith("2 victim rows not saved are not solved: row 2 (SIG2): draft: ")
    assert "row 3 (SIG): victim names must be unique" in text
    assert "not saved, so solved without it: victim row 4 (OTHER): not saved yet: Driver pins" in text
    assert "phase 1's High-side side is saved with both its part and its net" in text
    assert unsolved_text(Aggressor("U1"), rows[:1]) == ""
    assert unsolved_text(None, rows[1:2]).startswith("1 victim row not saved is not solved: row 1 (SIG2)")


def test_a_logic_v_off_kipis_table_is_not_judged_with_its_note_never_ranked():
    from coppernicus.app.noise_panel import VICTIM_HEADERS, victim_rows
    off = _victim("off", 0.2, margin=None)
    off.margin_note = "logic_v 0.9 V is not in KiPI's JESD8 table: enter noise_margin_v"
    r = _result([off, _victim("free", 0.2, margin=None)])
    rows = {row[0]: dict(zip(VICTIM_HEADERS, row)) for row in victim_rows(r)}
    assert rows["off"]["Verdict"] == "NOT JUDGED" and rows["off"]["Why"] == off.margin_note
    assert rows["off"]["Margin"] == "-" and r.verdict == "not_judged"
    assert rows["free"]["Verdict"] == "no margin (ranked)"


def test_the_notes_carry_every_stand_in_and_warning_verbatim():
    from coppernicus.app.noise_panel import notes_text
    v = _victim("V", 0.5, stand_ins=["C2 on phase 1: KiPI draws no current loop for it (it has no pad on "
                                     "GND), so its loop's coupling is left out"])
    v.warnings = ["No capacitor loops: x"]
    r = _result([v])
    r.warnings = ["C9 is on the rails but in no phase's list: the hot loop does not include it"]
    text = notes_text(r)
    assert v.stand_ins[0] in text and r.warnings[0] in text and "No capacitor loops: x" in text
    ranked = _victim("W", 0.5, margin=None,
                     stand_ins=["capacitive coupling of phase 1 left out: no switch net"])
    noted = _victim("X", 0.5, margin=None, stand_ins=["εr assumed: x"])
    noted.margin_note = "logic_v 0.9 V is not in KiPI's JESD8 table: enter noise_margin_v"
    r = _result([ranked, noted])
    r.stand_ins = ["Qrr not entered on phase 1: y"]
    text = notes_text(r)                          # no reason carries them here: listed on their own
    assert "capacitive coupling of phase 1 left out: no switch net" in text and "εr assumed: x" in text
    assert "Qrr not entered on phase 1: y" in text
    r.emi.notes = ["The radiated estimate counts the loops' differential-mode currents only"]
    assert r.emi.notes[0] in notes_text(r)          # KiPI's notes on the radiated estimate, verbatim


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


def test_a_label_shows_its_ampersand_and_a_lookup_reads_what_qt_shows():
    """Qt takes a lone "&" in a tab, menu, action or button label for a mnemonic marker:
    label_text doubles it, shown_label is what Qt then draws (lookups by the words on screen)."""
    from coppernicus.app import label_text, shown_label
    assert label_text("Ripple & noise") == "Ripple && noise" and label_text("Tolerance") == "Tolerance"
    assert shown_label("Ripple && noise") == "Ripple & noise"
    assert shown_label("Ripple & noise") == "Ripple  noise"           # the "&" eaten, as on screen
    assert shown_label("&File") == "File" and shown_label("a&&&b") == "a&b"
    for text in ("Ripple & noise", "Solve noise of A&B", "&&", "Tolerance"):
        assert shown_label(label_text(text)) == text, text


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


def _precise_copy(tmp_path):
    """The example with values a 6-digit field cannot show exactly, and counts above the
    fields' old maxima: committing a field unchanged must not round them, nor clamp them."""
    p = Project.load(NOISE)
    p.aggressors[0].rise_s, p.aggressors[0].vin_v = 2.123456789e-9, 12.3456789
    p.noise.segment_mm, p.noise.max_harmonics, p.noise.field_per_decade = 0.123456789, 200000, 500
    return p.save(tmp_path / "precise.coppernicus.yml")


def test_the_noise_tab_follows_the_impedance_tab_and_opening_changes_nothing(tmp_path):
    precise = _precise_copy(tmp_path)
    run_in_window(f'''
        from PySide6.QtGui import QKeySequence
        from coppernicus.app import shown_label
        tabs = [shown_label(window.results_tabs.tabText(i)) for i in range(window.results_tabs.count())]
        assert tabs[tabs.index("Temperature") + 1] == "Impedance", tabs
        assert tabs[tabs.index("Impedance") + 1] == "Ripple & noise", tabs     # its "&" on screen
        label = window.results_tabs.tabText(tabs.index("Ripple & noise"))
        assert QKeySequence.mnemonic(label).isEmpty(), label                   # no Alt+Space mnemonic
        for box in [window.results_tabs] + window.findChildren(QtWidgets.QTabWidget):
            for i in range(box.count()):                  # no tab, button or toolbar action eats a "&"
                assert QKeySequence.mnemonic(box.tabText(i)).isEmpty(), box.tabText(i)
        for button in window.findChildren(QtWidgets.QAbstractButton):
            assert QKeySequence.mnemonic(button.text()).isEmpty(), button.text()
        for group in window.findChildren(QtWidgets.QGroupBox):
            assert QKeySequence.mnemonic(group.title()).isEmpty(), group.title()
        for action in window.findChild(QtWidgets.QToolBar, "Main").actions():
            assert QKeySequence.mnemonic(action.text()).isEmpty(), action.text()
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
        window.open_project({str(precise)!r})
        app.processEvents()
        before = window.project.to_dict()
        assert panel.field["rise_s"].text() == "2.12346" and panel.opt["segment_mm"].text() == "0.123457"
        assert panel.opt["max_harmonics"].value() == 200000 and panel.opt["field_per_decade"].value() == 500
        for w in list(panel.field.values()) + list(panel.opt.values()):
            if hasattr(w, "editingFinished"):
                w.editingFinished.emit()                      # shown rounded, committed unchanged
        assert window.dirty is False and window.project.to_dict() == before
        assert window.project.aggressors[0].rise_s == 2.123456789e-9
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
        window.project.rails[1].sources[0].fsw_hz = 1.2e6                # the source's own fsw
        window._remerge()
        panel.set_field_text("fsw_hz", "")
        assert window.project.aggressors[0].fsw_hz is None
        hint = panel.fsw_hint.text()
        assert hint == "Empty: 1.2 MHz from the source U1 (the project file)", hint
        panel.add_victim()
        assert len(window.project.victims) == 3                           # a draft is not written
        panel.victims.item(3, 1).setText("SIG_NEW")
        panel.set_preset(3, "analog")
        assert window.project.victims[-1].net == "SIG_NEW" and window.project.victims[-1].preset == "analog"
    ''')


def test_the_ripple_probes_follow_a_probe_added_in_the_impedance_tab():
    run_in_window(f'''
        window.show()
        window.open_project({NOISE!r})
        app.processEvents()
        ac, panel = window.ac_panel, window.noise_panel
        window.results_tabs.setCurrentWidget(ac)
        app.processEvents()
        ac.add_probe()
        for col, text in ((0, "VINP"), (1, "U1"), (2, "VIN")):
            ac.probes.item(1, col).setText(text)
        assert [p.name for p in window.project.probes] == ["LOAD", "VINP"]
        window.results_tabs.setCurrentWidget(panel)
        app.processEvents()
        items = [panel.probe_list.item(i) for i in range(panel.probe_list.count())]
        assert [(i.data(QtCore.Qt.UserRole), i.checkState() == QtCore.Qt.Checked) for i in items] == \\
            [("LOAD", True), ("VINP", False)], [i.text() for i in items]
        items[1].setCheckState(QtCore.Qt.Checked)
        assert window.project.aggressors[0].ripple_probes == ["LOAD", "VINP"]
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
        asked, defaults = [], []
        QMessageBox.warning = staticmethod(
            lambda *a, **k: asked.append(a[1]) or defaults.append(a[4]) or QMessageBox.No)
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
        assert defaults == [QMessageBox.No]                     # the question's default is No
        assert heard and "could exceed" in heard[0] and "1,000 triangles" in heard[0], heard
        assert panel.estimate.toolTip() == heard[0] and "1,000 triangles × 4 ports" in panel.estimate.text()
        assert len(panel.estimate.text()) < len(heard[0])
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


RESULT = '''
import numpy as np
import coppernicus.app.noise_panel as noise_panel
from coppernicus.coupled_noise import judge
from coppernicus.results import NoiseResult, RippleResult, VictimResult


def make_result(name="U1", peak=0.4, margin=0.8):
    """A judged result shaped as the example's (its three victims, the LOAD probe)."""
    n = 16
    t = np.arange(n) / (n * 1e6)
    z = np.zeros(n)
    victims = [VictimResult(net, net, "U5[1]", "U6[1]", True, "cmos", margin, "noise_margin_v",
                            "noise_margin_v (V)", peak, {"capacitive": peak / 4, "inductive": peak},
                            "inductive", peak / margin, [], 10.0, 5.0, {}, {}, [], z + peak)
               for net in ("SIG_SW", "SIG_LOOP", "SIG_BURIED")]
    ripple = [RippleResult("LOAD", "probe", "VOUT", "GND", "U2", ["1"], 0.004, 0.006, 0.002, 0.003,
                           0.010, 0.005, 0.003, t, z, z)]
    r = NoiseResult(name, "U1", "VOUT", "VIN", "GND", "buck", 12.0, 1.8, 5.0, 0.83, 0.15, 0.9, 1, [],
                    1e6, 2e-9, 2e-9, 1.6e9, 1600, False, [1.5], [], t, [z + 12.0], [z + 5.0], [""],
                    ripple=ripple, victims=victims, stats={"seconds": 2.0})
    return judge(r)


class FakeSolve(QtCore.QObject):
    """The solve phase's signals, sent by hand from the test."""
    succeeded = QtCore.Signal(object)
    phase = "solve"

    def isRunning(self):
        return True

    def cancel(self):
        pass


def solving(panel, name):
    """The panel as it is while ``name`` solves (what Solve sets), with a fake worker."""
    fake = FakeSolve()
    fake.generation = panel._generation
    fake.succeeded.connect(panel._solved)
    panel.worker, panel._busy = fake, True
    panel._solving = (name, noise_panel.project_key(window.project), window.merged)
    return fake


def verdicts(panel):
    col = noise_panel.VICTIM_HEADERS.index("Verdict")
    return [panel.victim_table.item(i, col).text() for i in range(panel.victim_table.rowCount())]
'''


def test_a_solve_that_ends_shows_its_result_and_an_edit_flags_it_everywhere():
    run_in_window(RESULT + textwrap.dedent(f'''
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        solving(panel, "U1").succeeded.emit(make_result("U1", 0.4))
        assert "<b>U1</b>" in panel.header.text() and "PASS" in panel.header.text()
        assert "Setup changed" not in panel.header.text()
        assert verdicts(panel) == ["PASS"] * 3, verdicts(panel)
        assert panel.ripple_table.item(0, 0).text() == "LOAD" and panel.export_button.isEnabled()
        title = panel.wave_figure.axes[0].get_title()
        assert "PASS" in title and "setup changed" not in title, title
        panel.victims.item(0, 8).setText("300")                # an edit after the solve
        assert "Setup changed since this solve" in panel.header.text()
        assert verdicts(panel) == ["PASS (setup changed)"] * 3, verdicts(panel)
        assert "setup changed" in panel.wave_figure.axes[0].get_title()
    '''))


def test_a_solve_that_ends_while_another_aggressor_is_edited_changes_nothing_there():
    """No worker signal changes the selection or refills an editor the user is in."""
    run_in_window(RESULT + textwrap.dedent(f'''
        window.show()
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        window.results_tabs.setCurrentWidget(panel)
        fake = solving(panel, "U1")
        panel.add_aggressor()
        panel.add_phase()
        panel.tabs.setCurrentIndex(1)
        app.processEvents()
        assert panel.aggressor_box.currentText() == "Aggressor 2"
        item = panel.phases.item(0, 12)                         # Aggressor 2's empty Coss
        panel.phases.setCurrentItem(item)
        panel.phases.editItem(item)
        editing = QtWidgets.QAbstractItemView.State.EditingState
        assert panel.phases.state() == editing
        fake.succeeded.emit(make_result("U1", 0.4))             # U1's solve ends now
        app.processEvents()
        assert panel.aggressor_box.currentText() == "Aggressor 2" and panel.phases.state() == editing
        assert "U1 solved: choose it to see the result" in window.statusBar().currentMessage()
        assert "U1" in panel.results and panel.header.text() == ""
        panel.phases.item(0, 12).setText("450")                 # the commit lands on Aggressor 2
        assert window.project.aggressors[1].phases[0].coss_f == 4.5e-10
        assert window.project.aggressors[0].phases[0].coss_f == 3e-10
        panel.aggressor_box.setCurrentIndex(0)                  # choosing U1 shows its result
        assert "<b>U1</b>" in panel.header.text() and verdicts(panel)[0].startswith("PASS")
        fake = solving(panel, "U1")
        panel.set_field_text("name", "U1 buck")                 # renamed while it solves
        fake.succeeded.emit(make_result("U1", 0.4))
        assert "U1" not in panel.results and "U1 buck" not in panel.results, list(panel.results)
        assert "renamed or removed" in window.statusBar().currentMessage()
    '''))


def test_export_asks_before_writing_a_stale_result_and_the_summary_says_so(tmp_path):
    run_in_window(RESULT + textwrap.dedent(f'''
        import json
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox
        out = Path({str(tmp_path)!r})
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        solving(panel, "U1").succeeded.emit(make_result("U1", 0.4))
        panel.results["GONE"] = (make_result("GONE"), noise_panel.project_key(window.project))
        asked = []
        QMessageBox.question = staticmethod(lambda *a, **k: asked.append(a[4]) or QMessageBox.No)
        names = sorted(p.name for p in panel.export_files(out / "fresh"))
        assert asked == [] and not any("GONE" in n for n in names), names     # current: no question
        doc = json.loads((out / "fresh" / "noise.coppernicus_noise_summary.json").read_text())
        assert doc["warnings"] == [] and [a["aggressor"] for a in doc["aggressors"]] == ["U1"]
        panel.victims.item(0, 8).setText("300")                # now U1's result is stale
        assert panel.export_files(out / "no") == [] and not (out / "no").exists()
        assert asked == [QMessageBox.No] and "Not exported" in panel.status.text()
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        panel.export_files(out / "yes")
        doc = json.loads((out / "yes" / "noise.coppernicus_noise_summary.json").read_text())
        assert doc["warnings"] == ["U1: the setup changed since this solve"], doc["warnings"]
    '''))


def test_turning_the_kicad_fields_off_flags_the_noise_and_impedance_results():
    run_in_window(RESULT + textwrap.dedent(f'''
        from coppernicus.app.ac_panel import _project_key as ac_key
        from coppernicus.results import ImpedanceResult
        window.open_project({NOISE!r})
        app.processEvents()
        panel, ac = window.noise_panel, window.ac_panel
        solving(panel, "U1").succeeded.emit(make_result("U1", 0.4))
        rail = ac.rail_box.currentText()
        f = np.array([1e3, 1e6])
        ac.results[rail] = (ImpedanceResult(rail, [rail], "GND", "J1", None, f, f),
                            ac_key(window.project))
        ac._check_stale()
        assert "Setup changed" not in panel.header.text() and "Setup changed" not in ac.header.text()
        window.use_fields_action.trigger()                      # Project → Use KiCad footprint fields
        app.processEvents()
        assert "Setup changed" in panel.header.text(), panel.header.text()
        assert "Setup changed" in ac.header.text(), ac.header.text()
    '''))


def test_solve_names_the_rows_it_leaves_out_and_the_result_keeps_saying_so():
    run_in_window(RESULT + STUB_WORKER + textwrap.dedent(f'''
        StubWorker.start = lambda self: None
        window.open_project({NOISE!r})
        app.processEvents()
        panel = window.noise_panel
        panel.add_victim()                                      # a draft: not saved, not solved
        panel.solve()
        said = window.statusBar().currentMessage()
        assert "1 victim row not saved is not solved: row 4" in said, said
        fake = solving(panel, "U1")
        fake.succeeded.emit(make_result("U1", 0.4))
        assert "1 victim row not saved is not solved: row 4" in panel.notes.toPlainText()
    '''))
