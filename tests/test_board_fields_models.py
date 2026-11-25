"""KiCad footprint fields, third schema step: component models in library fields. A capacitor's
PI_C, PI_ESR, PI_ESL, PI_VRATED and PI_DIELECTRIC build its model, used by the AC impedance and
the noise; a load's PI_TOL, PI_DC_TOL and PI_AC_TOL and a source's PI_ACC give the tolerance
stack-up its tolerances and accuracies. Never a default behind a verdict: a capacitor whose
fields are partial has no model, and the reason names what it lacks. Synthetic data only."""

import json
from pathlib import Path

import numpy as np
import pytest
from kicad_fixture import write_board

from coppernicus.board_fields import FieldError, field_caps, listing, merge, propose, tolerance
from coppernicus.cli import EXIT_LIMIT, EXIT_OK, main
from coppernicus.engine import Board
from coppernicus.engine.kicad_fields import read_board_fields
from coppernicus.engine.synthetic import (
    AcDecap,
    ac_decap_board,
    ac_decap_project,
    coupled_noise_board,
    coupled_noise_project,
    write_strip_kicad_pcb,
)
from coppernicus.project import (
    AcOptions,
    CapModel,
    Load,
    NoiseOptions,
    Project,
    ProjectError,
    Rail,
    Source,
)

QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4,
             max_edge_mm=2.0)
X7R = {"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "x7r", "PI_VRATED": "16V"}
TEN_U = dict(values={"C1": "10u"}, farads={"C1": 10e-6})


def caps_by_ref(merged):
    return {ref: m for m in merged.project.capacitors for ref in m.refs}


# ------------------------------------------------------------------ capacitors: the model

def test_a_fully_annotated_capacitor_gets_a_model_with_each_value_from_its_field():
    merged = merge(Project(), {"C1": X7R}, **TEN_U)
    [m] = merged.project.capacitors
    assert m.refs == ["C1"] and m.dielectric == "X7R" and m.kept_at_rated_pct is None
    assert (m.capacitance_f, m.esr_ohm, m.rated_v) == (10e-6, 0.005, 16.0)
    assert m.esl_h == pytest.approx(0.5e-9, rel=1e-12)
    assert merged.project.capacitor_gaps == {} and merged.warnings == []
    notes = " ".join(merged.notes())
    for words in ('PI_ESR "5m" on C1', 'PI_ESL "0.5nH" on C1', 'PI_DIELECTRIC "x7r" on C1',
                  'PI_VRATED "16V" on C1', 'footprint Value "10u" of C1'):
        assert words in notes, words
    # PI_C overrides the footprint Value; "5mΩ" and "0.5n" are the same values.
    other = merge(Project(), {"C1": {**X7R, "PI_C": "4.7uF", "PI_ESR": "5mΩ", "PI_ESL": "0.5n"}},
                  **TEN_U)
    [m2] = other.project.capacitors
    assert m2.capacitance_f == pytest.approx(4.7e-6, rel=1e-12) and m2.esr_ohm == 0.005
    assert 'PI_C "4.7uF" on C1' in " ".join(other.notes())


def test_c0g_needs_no_voltage_rating_and_np0_is_c0g():
    for name in ("C0G", "NP0", "np0"):
        merged = merge(Project(), {"C1": {"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": name}},
                       **TEN_U)
        [m] = merged.project.capacitors
        assert (m.dielectric, m.rated_v) == ("C0G", None) and merged.project.capacitor_gaps == {}


def test_a_non_ceramic_capacitor_is_never_derated_for_dc_bias():
    """KiPI derates every rated model but C0G; a tantalum, polymer or aluminium part keeps its
    capacitance under DC bias, so its rating is not passed on (and is not needed)."""
    for name in ("TANT", "POLY", "ALU"):
        merged = merge(Project(), {"C1": {"PI_ESR": "50m", "PI_ESL": "2nH", "PI_DIELECTRIC": name,
                                          "PI_VRATED": "10V"}}, **TEN_U)
        [m] = merged.project.capacitors
        assert (m.dielectric, m.rated_v) == (name, None)
        assert "not derated for DC bias" in " ".join(merged.notes())
        bare = merge(Project(), {"C1": {"PI_ESR": "50m", "PI_ESL": "2nH", "PI_DIELECTRIC": name}}, **TEN_U)
        assert len(bare.project.capacitors) == 1


@pytest.mark.parametrize("fields, values, reason", [
    ({"PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}, {"C1": "10u"}, "C1: PI_ESR missing"),
    ({"PI_ESR": "5m"}, {"C1": "10u"}, "C1: PI_ESL, PI_DIELECTRIC missing"),
    ({**X7R, "PI_VRATED": ""}, {"C1": "10u"},
     "C1: X7R without PI_VRATED: capacitance not derated for DC bias"),
    ({"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}, {"C1": "C_0402"},
     'C1: no capacitance: no PI_C, and the footprint Value "C_0402" is not one (set PI_C, e.g. 100nF)'),
    ({"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}, {"C1": "0.1"},
     'C1: no capacitance: no PI_C, and the footprint Value "0.1" has no unit (set PI_C, e.g. 100nF, '
     "or write the unit in the Value)"),
    ({"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}, None,
     "C1: no capacitance: no PI_C, and no footprint Value was read (set PI_C, e.g. 100nF)"),
    ({"PI_ESR": "5m", "PI_DIELECTRIC": "X5R"}, {"C1": "10u"},
     "C1: PI_ESL missing; X5R without PI_VRATED: capacitance not derated for DC bias"),
])
def test_partial_fields_are_no_model_and_the_reason_names_what_is_missing(fields, values, reason):
    farads = {"C1": 10e-6} if values == {"C1": "10u"} else {}
    merged = merge(Project(), {"C1": fields}, values=values, farads=farads)
    assert merged.project.capacitors == []
    assert merged.project.capacitor_gaps == {"C1": reason}
    assert field_caps({"C1": fields}, values, farads)["C1"].missing == reason


@pytest.mark.parametrize("fields, words", [
    ({"PI_ESR": "5"}, "give the unit"),
    ({"PI_ESR": "0mOhm"}, "above 0"),
    ({"PI_ESL": "-1nH"}, "above 0"),
    ({"PI_ESL": "1nF"}, "PI_ESL takes"),
    ({"PI_C": "100"}, "give the unit"),
    ({"PI_VRATED": "16"}, "give the unit"),
    ({"PI_DIELECTRIC": "X9Z"}, "PI_DIELECTRIC is one of"),
])
def test_a_capacitor_field_that_cannot_be_read_is_an_error(fields, words):
    with pytest.raises(FieldError, match=words):
        merge(Project(), {"C1": fields}, **TEN_U)


def test_capacitor_fields_on_a_part_not_named_c_number_are_not_used_with_a_warning():
    merged = merge(Project(), {"U7": {"PI_ESR": "5m", "PI_ESL": "1nH", "PI_DIELECTRIC": "C0G",
                                      "PI_C": "1uF"}})
    assert merged.project.capacitors == []
    assert any("U7" in w and "C<number>" in w for w in merged.warnings)


# ------------------------------------------------------------------ capacitors: precedence

def test_a_project_model_on_a_ref_with_complete_fields_is_ignored_for_it_with_a_warning():
    c1 = {"PI_ESR": "5mOhm", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}
    own = Project(capacitors=[CapModel("bulk", 10e-6, 0.5e-9, 0.01, ["C1", "C2"])])
    merged = merge(own, {"C1": c1}, **TEN_U)
    by_ref = caps_by_ref(merged)
    assert by_ref["C1"].esr_ohm == 0.005 and by_ref["C1"].name != "bulk"
    assert by_ref["C2"].name == "bulk" and by_ref["C2"].refs == ["C2"]
    [w] = merged.warnings
    assert "bulk" in w and "C1" in w and "override_board_fields" in w and "0.01" in w
    assert own.capacitors[0].refs == ["C1", "C2"]          # the loaded project is not changed
    same = merge(Project(capacitors=[CapModel("bulk", 10e-6, 0.5e-9, 0.005, ["C1"], dielectric="C0G")]),
                 {"C1": c1}, **TEN_U)
    assert same.warnings == [] and [m.esr_ohm for m in same.project.capacitors if m.refs] == [0.005]
    kept = merge(Project(capacitors=[CapModel("bulk", 10e-6, 0.5e-9, 0.01, ["C1"],
                                              override_board_fields=True)]), {"C1": c1}, **TEN_U)
    assert [(m.name, m.refs, m.esr_ohm) for m in kept.project.capacitors] == [("bulk", ["C1"], 0.01)]
    assert kept.warnings == [] and "override_board_fields" in " ".join(kept.notes())


def test_partial_fields_keep_the_project_model_with_a_warning():
    merged = merge(Project(capacitors=[CapModel("bulk", 10e-6, 0.5e-9, 0.01, ["C1"])]),
                   {"C1": {"PI_ESR": "5mOhm"}}, **TEN_U)
    assert [(m.name, m.refs) for m in merged.project.capacitors] == [("bulk", ["C1"])]
    assert merged.project.capacitor_gaps == {}
    [w] = merged.warnings
    assert "C1: PI_ESL, PI_DIELECTRIC missing" in w and "bulk" in w


def test_override_board_fields_on_a_capacitor_model_round_trips_in_the_project_file():
    p = Project(capacitors=[CapModel("bulk", 1e-6, 1e-9, 0.01, ["C1"], override_board_fields=True)])
    assert p.to_dict()["capacitors"][0]["override_board_fields"] is True
    assert Project.from_dict(p.to_dict()).capacitors[0].override_board_fields is True
    plain = Project(capacitors=[CapModel("x", 1e-6, 1e-9, 0.01, ["C1"])]).to_dict()
    assert "override_board_fields" not in plain["capacitors"][0]
    with pytest.raises(ProjectError, match="override_board_fields"):
        Project.from_dict({"capacitors": [{"name": "x", "override_board_fields": "yes"}]})
    assert "capacitor_gaps" not in p.to_dict()


# ------------------------------------------------------------------ capacitors: AC and noise

@pytest.fixture()
def decap():
    """The AC decoupling project without its model, merged with C1's KiCad fields."""
    def make(fields):
        p = ac_decap_project()
        p.capacitors = []
        p.ac = AcOptions(**QUICK, bandwidth_hz={"U1": 1e5})
        p.probes[0].target_ohm = 1.0
        return merge(p, {"C1": fields}, values={"C1": "10u"}, farads={"C1": AcDecap.C})
    return make


def test_a_fully_annotated_capacitor_is_the_model_the_ac_solve_uses(decap):
    from coppernicus.engine.ac import solve_ac_rail
    board = Board(ac_decap_board())
    merged = decap({"PI_ESR": "20mOhm", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"})
    res = solve_ac_rail(board, merged.project, "+3V3")
    reference = ac_decap_project()                       # the same values as a project model
    reference.ac, reference.probes[0].target_ohm = merged.project.ac, 1.0
    expected = solve_ac_rail(board, reference, "+3V3")
    assert res.caps == ["C1"] and res.missing_caps == [] and res.probes[0].passed is True
    np.testing.assert_allclose(np.abs(res.probes[0].z_ohm), np.abs(expected.probes[0].z_ohm), rtol=1e-9)


@pytest.mark.parametrize("fields, words", [
    ({"PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}, "C1: PI_ESR missing"),
    ({"PI_ESR": "20mOhm", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "X7R"},
     "C1: X7R without PI_VRATED: capacitance not derated for DC bias"),
])
def test_partial_fields_leave_the_capacitor_out_and_its_probe_not_judged(decap, fields, words):
    from coppernicus.engine.ac import solve_ac_rail
    res = solve_ac_rail(Board(ac_decap_board()), decap(fields).project, "+3V3")
    assert res.caps == [] and res.missing_caps == ["C1"]
    [p] = res.probes
    assert p.passed is None
    assert p.not_judged.startswith("1 capacitor(s) between +3V3 and GND have no model, so the "
                                   "resonances may move: C1")
    assert words in p.not_judged
    assert any(words in a for a in res.assumptions)


def test_noise_uses_a_fields_model_and_a_partial_one_is_a_stand_in_naming_what_it_lacks():
    from coppernicus.engine.noise import solve_aggressor
    full = {"PI_ESR": "2mOhm", "PI_ESL": "0.6nH", "PI_DIELECTRIC": "X5R", "PI_VRATED": "6.3V"}

    def solve(fields):
        p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
        p.capacitors = [m for m in p.capacitors if m.refs != ["C2"]]
        p.noise = NoiseOptions(board_model=False)
        merged = merge(p, {"C2": fields}, values={"C2": "47uF"}, farads={"C2": 47e-6})
        return solve_aggressor(Board(coupled_noise_board()), merged.project, "U1")
    assert not [s for s in solve(full).stand_ins if "without a model" in s]
    partial = solve({k: v for k, v in full.items() if k != "PI_ESL"})
    [s] = [s for s in partial.stand_ins if "without a model" in s]
    assert s.startswith("capacitors on the converter's rails without a model: C2")
    assert "C2: PI_ESL missing" in s and partial.verdict == "not_judged"


# ------------------------------------------------------------------ tolerances and accuracy

STRIP = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V", "PI_ACC": "1%"},
         "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_TOL": "-3%,+5%"}}


@pytest.mark.parametrize("text, key, pair", [
    ("5%", "tol_pct", (-5.0, 5.0)), ("±5%", "tol_pct", (-5.0, 5.0)), ("50mV", "tol_mv", (-50.0, 50.0)),
    ("-30mV,+50mV", "tol_mv", (-30.0, 50.0)), ("−3%,+5%", "tol_pct", (-3.0, 5.0)),
    ("0%,+5%", "tol_pct", (0.0, 5.0)), ("-2.5%, 1%", "tol_pct", (-2.5, 1.0)),
])
def test_the_tolerance_syntax(text, key, pair):
    assert tolerance("U1", "PI_TOL", text) == (key, pair)
    assert tolerance("U1", "PI_DC_TOL", text) == ("dc_" + key, pair)
    assert tolerance("J1", "PI_ACC", text) == (key.replace("tol", "accuracy"), pair)


@pytest.mark.parametrize("text, words", [
    ("5", "give the unit"), ("-5%", "cannot be negative"), ("-3%,+50mV", "same unit"),
    ("+3%,+5%", "below"), ("-3%,-1%", "above"), ("5V", "takes %"), ("1%,2%,3%", "two values"),
    ("2,5%", "dot"),
])
def test_a_tolerance_that_cannot_be_read_is_an_error(text, words):
    with pytest.raises(FieldError, match=words):
        tolerance("U1", "PI_TOL", text)


def test_pi_tol_and_pi_acc_set_the_fields_rails_tolerance_and_accuracy():
    merged = merge(Project(), STRIP)
    rail = merged.project.rail("VCC")
    assert rail.loads[0].tol_pct == (-3.0, 5.0) and rail.sources[0].accuracy_pct == (-1.0, 1.0)
    notes = " ".join(merged.notes())
    assert 'from KiCad field PI_TOL "-3%,+5%" on U1' in notes and 'PI_ACC "1%" on J1' in notes
    dual = merge(Project(), {**STRIP, "U1": {"PI_RAIL": "VCC;VIO", "PI_LOAD": "1A;50mA",
                                             "PI_TOL": "5%;", "PI_AC_TOL": "20mV"},
                             "J2": {"PI_RAIL": "VIO", "PI_SOURCE": "1.8V"}})
    vcc, vio = dual.project.rail("VCC").loads[0], dual.project.rail("VIO").loads[0]
    assert (vcc.tol_pct, vio.tol_pct) == ((-5.0, 5.0), None)            # one value per rail
    assert vcc.ac_tol_mv == vio.ac_tol_mv == (-20.0, 20.0)             # one value for every rail
    with pytest.raises(FieldError, match="PI_TOL is for a load"):
        merge(Project(), {**STRIP, "J1": {**STRIP["J1"], "PI_TOL": "5%"}})
    with pytest.raises(FieldError, match="PI_ACC is for a source"):
        merge(Project(), {**STRIP, "U1": {**STRIP["U1"], "PI_ACC": "1%"}})


def test_fields_tolerances_win_over_the_carried_project_ones_with_a_warning_when_they_differ():
    own = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3, accuracy_pct=1)],
                              loads=[Load("U1", 1.0, tol_mv=40, ripple_mv=2)])])
    merged = merge(own, STRIP)
    rail = merged.project.rail("VCC")
    u1 = rail.loads[0]
    assert (u1.tol_pct, u1.tol_mv, u1.ripple_mv) == ((-3.0, 5.0), None, (-2.0, 2.0))
    assert rail.sources[0].accuracy_pct == (-1.0, 1.0)
    [w] = merged.warnings
    assert "U1" in w and "PI_TOL" in w and "tol_mv" in w and "J1" not in w
    notes = " ".join(merged.notes())
    assert "ripple_mv of U1 from the project file" in notes and "tol_mv of U1" not in notes


def test_override_board_fields_keeps_the_project_tolerances_and_accuracy():
    own = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3, accuracy_pct=2)],
                              loads=[Load("U1", 1.0, tol_mv=40)], override_board_fields=True)])
    rail = merge(own, STRIP).project.rail("VCC")
    assert (rail.loads[0].tol_mv, rail.loads[0].tol_pct) == ((-40.0, 40.0), None)
    assert rail.sources[0].accuracy_pct == (-2.0, 2.0)


def test_library_tolerances_reach_a_project_rail_by_part():
    """No PI_RAIL: a chip's PI_TOL and a buck's PI_ACC on their library symbols reach the
    project file's rail by part (the buck's fields are on the IC, its source at the inductor)."""
    own = Project(rails=[Rail("+1V0", sources=[Source("L1", 1.0, name="U1", kind="smps", efficiency=0.9,
                                                      input_ref="U1", input_net="VIN")],
                              loads=[Load("U5", 2.0, tol_pct=4)])])
    library = {"U5": {"PI_TOL": "3%"}, "U1": {"PI_ACC": "1.5%", "PI_KIND": "smps", "PI_EFF": "90%"}}
    merged = merge(own, library)
    rail = merged.project.rail("+1V0")
    assert rail.loads[0].tol_pct == (-3.0, 3.0) and rail.sources[0].accuracy_pct == (-1.5, 1.5)
    [w] = merged.warnings
    assert "U5" in w and "PI_TOL" in w and "tol_pct" in w
    assert 'PI_ACC "1.5%" on U1' in " ".join(merged.notes())
    with pytest.raises(FieldError, match="no PI_RAIL"):
        merge(own, {"U5": {"PI_TOL": "3%;5%"}})
    overridden = Project(rails=[Rail("+1V0", sources=own.rails[0].sources, loads=own.rails[0].loads,
                                     override_board_fields=True)])
    assert merge(overridden, library).project.rail("+1V0").loads[0].tol_pct == (-4.0, 4.0)


def test_pi_tol_and_pi_acc_give_the_tolerance_stack_up_its_limits(tmp_path, capsys):
    """J1's PI_ACC and U1's PI_TOL are judged; the project's ripple and transient are carried."""
    def run(tol):
        write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {
            "J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V", "PI_ACC": "1%"},
            "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_TOL": tol}})
        path = Project(board="s.kicad_pcb", rails=[Rail("VCC", sources=[Source("J1", 3.3)], loads=[
            Load("U1", 1.0, tol_pct=4, ripple_mv=2, transient_mv=3)])]).save(tmp_path / "s.coppernicus.yml")
        code = main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")])
        return code, json.loads(capsys.readouterr().out)
    code, doc = run("5%")
    assert code == EXIT_OK and doc["verdict"] == "pass"
    [ld] = doc["loads"]
    assert ld["parts"]["accuracy"]["source"] == "accuracy_pct of J1"
    assert any('PI_TOL "5%" on U1' in a for a in doc["assumptions"])
    assert any("U1" in w and "PI_TOL" in w and "tol_pct" in w for w in doc["warnings"])
    code, doc = run("0.1%")                  # 3.3 mV allowed: the accuracy alone is 33 mV
    assert code == EXIT_LIMIT and doc["verdict"] == "fail"


def test_ci_runs_the_tolerance_stack_up_for_a_tolerance_held_only_in_kicad_fields(tmp_path, monkeypatch):
    """The project file sets no tolerance; U1's PI_TOL on the board does: CI's opt-in run
    takes it, and skips it when the project turns the fields off."""
    from coppernicus import ci
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {
        "J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V", "PI_ACC": "1%"},
        "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_TOL": "5%"}})
    project = Project(board="s.kicad_pcb", rails=[Rail("VCC", sources=[Source("J1", 3.3)], loads=[
        Load("U1", 1.0, ripple_mv=2, transient_mv=3)])])
    project.save(tmp_path / "s.coppernicus.yml")
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all(["s.coppernicus.yml"], Path("ci"), html=False, tolerance=True)
    [entry] = index["projects"]
    assert entry["tolerance"]["verdict"] == "pass" and index["exit_code"] == EXIT_OK
    project.use_board_fields = False
    project.save(tmp_path / "s.coppernicus.yml")
    index = ci.solve_all(["s.coppernicus.yml"], Path("ci2"), html=False, tolerance=True)
    assert "tolerance" not in index["projects"][0]
    # A library PI_TOL on a part that is a load on no rail sets no load's tolerance: no run.
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {
        "J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"}, "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"},
        "U9": {"PI_TOL": "5%"}}, extra_parts=[("U9", 20.0, 3.0, "VCC")])
    project.use_board_fields = True
    project.save(tmp_path / "s.coppernicus.yml")
    index = ci.solve_all(["s.coppernicus.yml"], Path("ci3"), html=False, tolerance=True)
    assert "tolerance" not in index["projects"][0] and index["exit_code"] == EXIT_OK


# ------------------------------------------------------------------ listing and reading

def test_the_listing_gives_each_capacitors_model_or_what_it_lacks():
    fields = {"C1": X7R, "C2": {"PI_ESR": "5m"}, "U5": {"PI_TOL": "3%"}, **STRIP}
    out = listing(fields, values={"C1": "10u", "C2": "1u"}, farads={"C1": 10e-6, "C2": 1e-6})
    caps = {c["ref"]: c for c in out["capacitors"]}
    c1, c2 = caps["C1"], caps["C2"]
    assert c1["complete"] and c1["missing"] is None and c1["fields"] == X7R
    assert c1["model"]["capacitance_f"] == 10e-6 and c1["model"]["rated_v"] == 16.0
    assert c1["model"]["capacitance_from"] == 'footprint Value "10u"'
    assert not c2["complete"] and c2["model"] is None
    assert c2["missing"] == "C2: PI_ESL, PI_DIELECTRIC missing"
    [u5] = out["part_tolerances"]
    assert u5["ref"] == "U5" and u5["fields"] == {"PI_TOL": "3%"} and u5["tol_pct"] == [-3.0, 3.0]
    [vcc] = out["rails"]
    assert vcc["loads"][0]["fields"]["PI_TOL"] == "-3%,+5%"
    assert "capacitors" not in listing(STRIP) and "part_tolerances" not in listing(STRIP)


@pytest.mark.parametrize("text, farads", [
    ("10u", 10e-6), ("4n7", 4.7e-9), ("100nF 16V", 100e-9), ("10 µF", 10e-6), ("1F", 1.0),
    ("10uF/16V", 10e-6), ("0.1", None), ("100", None), ("C_0402", None), ("100 pcs", None),
    ("10 film", None), ("0.1 misc", None), ("", None),
])
def test_a_footprint_value_gives_a_capacitance_only_with_its_unit(text, farads):
    """KiPI reads a bare number as µF; here that unit is never guessed, nor a word after it
    taken for one."""
    from coppernicus.engine.kicad_fields import value_farads
    got = value_farads(text)
    assert got == (None if farads is None else pytest.approx(farads, rel=1e-12))


def test_the_reader_keeps_each_annotated_parts_value_and_the_cli_lists_the_models(tmp_path, capsys):
    path = write_board(tmp_path / "c.kicad_pcb", [
        ("C1", [("1", 0, 0, "VCC"), ("2", 2, 0, "GND")], {"Value": "10u", **X7R}),
        ("C2", [("1", 4, 0, "VCC"), ("2", 6, 0, "GND")], {"Value": "0.1", **X7R}),
        ("R1", [("1", 8, 0, "VCC"), ("2", 10, 0, "GND")], {"Value": "10k"}),
    ], tracks=[])
    found = read_board_fields(path)
    assert found.values == {"C1": "10u", "C2": "0.1"}
    assert found.farads == {"C1": pytest.approx(10e-6, rel=1e-12)}
    assert main(["fields", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Capacitor C1: 10 µF" in out and "Capacitor C2: no model: C2: no capacitance" in out
    assert main(["fields", str(path), "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert [c["complete"] for c in doc["capacitors"]] == [True, False]


# ------------------------------------------------------------------ proposing

def test_propose_round_trips_capacitor_models_tolerances_and_accuracy():
    project = Project(
        rails=[Rail("VCC", sources=[Source("J1", 3.3, accuracy_pct=[-1, 2])],
                    loads=[Load("U1", 1.0, tol_pct=5, dc_tol_mv=[-30, 20], ac_tol_pct=2, ripple_mv=3)])],
        capacitors=[CapModel("bulk", 10e-6, 0.5e-9, 0.02, ["C1", "C2"], rated_v=16, dielectric="X7R"),
                    CapModel("hf", 100e-9, 0.3e-9, 0.05, ["C3"], dielectric="NP0"),
                    CapModel("bare", 1e-6, 0.0, 0.0, ["C4"]),
                    CapModel("kept", 1e-6, 1e-9, 0.01, ["C5"], rated_v=10, kept_at_rated_pct=50,
                             dielectric="X5R"),
                    CapModel("unrated", 1e-6, 1e-9, 0.01, ["C6"], dielectric="X5R")])
    out = propose(project)
    parts = {p["ref"]: p for p in out["parts"]}
    assert parts["C1"]["library_symbol"] == {"PI_C": "10uF", "PI_ESR": "20mOhm", "PI_ESL": "0.5nH",
                                             "PI_VRATED": "16V", "PI_DIELECTRIC": "X7R"}
    assert parts["C1"]["schematic_instance"] == {} and parts["C3"]["library_symbol"]["PI_DIELECTRIC"] == "C0G"
    assert "C4" not in parts and "C6" not in parts
    assert parts["U1"]["library_symbol"] == {"PI_TOL": "5%", "PI_DC_TOL": "-30mV,+20mV", "PI_AC_TOL": "2%"}
    assert parts["J1"]["schematic_instance"]["PI_ACC"] == "-1%,+2%"
    unsaid = {(n["part"], n["what"].split(" ")[0]) for n in out["not_expressed"]}
    assert {("C4", "capacitor"), ("C5", "kept_at_rated_pct"), ("C6", "capacitor"),
            ("U1", "ripple_mv")} <= unsaid
    fields = {p["ref"]: {**p["library_symbol"], **p["schematic_instance"]} for p in out["parts"]}
    back = merge(Project(), fields)
    caps = caps_by_ref(back)
    for ref, m in (("C1", project.capacitors[0]), ("C2", project.capacitors[0]),
                   ("C3", project.capacitors[1]), ("C5", project.capacitors[3])):
        got = caps[ref]
        assert got.capacitance_f == pytest.approx(m.capacitance_f, rel=1e-12)
        assert got.esl_h == pytest.approx(m.esl_h, rel=1e-12) and got.esr_ohm == pytest.approx(m.esr_ohm)
        assert got.rated_v == m.rated_v
        assert got.dielectric == ("C0G" if m.dielectric == "NP0" else m.dielectric)
    rail = back.project.rail("VCC")
    u1 = rail.loads[0]
    assert (u1.tol_pct, u1.dc_tol_mv, u1.ac_tol_pct) == ((-5.0, 5.0), (-30.0, 20.0), (-2.0, 2.0))
    assert rail.sources[0].accuracy_pct == (-1.0, 2.0)
