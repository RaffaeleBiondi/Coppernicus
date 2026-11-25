"""``coppernicus tolerance`` on KiPI's synthetic coupled-noise board (Task 11)."""

import dataclasses
import json
import runpy
from pathlib import Path

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import coupled_noise_board, coupled_noise_project, save_snapshot
from coppernicus.project import AcOptions, Project

STEM = "t.coppernicus"


@pytest.fixture()
def write(tmp_path):
    """Save the board and return a function writing the project: U2 with a tolerance, U1's
    accuracy, the probe LOAD with a 1 A step at 1 A/µs, U1's bandwidth 100 kHz."""
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    path = tmp_path / "t.coppernicus.yml"

    def write(tol_pct=6.0, transient_mv=30.0, accuracy_pct=1.0, bandwidth=True, ripple_mv=None, listed=True):
        p = coupled_noise_project(qrr_nc=5.0, ripple_probe=True)
        p.victims = []
        if not listed:                    # U1 does not list LOAD: the entered ripple_mv stands
            p.aggressors[0].ripple_probes = []
        rail = p.rails[1]
        rail.loads[0] = dataclasses.replace(rail.loads[0], tol_pct=tol_pct, transient_mv=transient_mv,
                                            ripple_mv=ripple_mv)
        rail.sources[0] = dataclasses.replace(rail.sources[0], accuracy_pct=accuracy_pct)
        p.probes[0].step_a, p.probes[0].slew_a_per_us = 1.0, 1.0
        p.ac = AcOptions(bandwidth_hz={"U1": 100e3} if bandwidth else {})
        return p.save(path)
    return write


def _doc(out):
    return json.loads((out / f"{STEM}_tolerance_summary.json").read_text(encoding="utf-8"))


def test_skipping_ac_and_noise_leaves_the_ripple_missing_and_says_which_flag(write, tmp_path, capsys):
    out = tmp_path / "out"
    code = main(["tolerance", str(write()), "--skip-ac", "--skip-noise", "--out", str(out)])
    assert code == EXIT_NOT_JUDGED
    text = capsys.readouterr().out
    assert "Impedance of" not in text and "Noise of" not in text          # nothing heavy solved
    for words in ("Load U2 on VOUT (nominal 1.8 V from U1, probe LOAD): NOT JUDGED",
                  "ripple       missing: U1 not solved (--skip-noise)",
                  "NOT JUDGED U2 (tol_pct): ripple: U1 not solved (--skip-noise)", "Verdict: not_judged.",
                  "Summary:"):
        assert words in text, words
    doc = _doc(out)
    assert doc["kind"] == "tolerance" and doc["verdict"] == "not_judged"
    assert doc["summary_file"].endswith(f"{STEM}_tolerance_summary.json")
    assert doc["csv_file"].endswith(f"{STEM}_tolerance.csv") and (out / f"{STEM}_tolerance.csv").is_file()
    (ld,) = doc["loads"]
    assert ld["parts"]["transient"]["source"] == "entered (transient_mv)"
    assert ld["parts"]["transient"]["detail"] == ""                       # --skip-ac: no estimate
    dc = ld["parts"]["dc"]                 # one supply pin: the drop is below nominal only
    assert 0 < dc["below_mv"] < 100 and dc["above_mv"] == 0


def test_without_a_transient_the_hint_carries_kipis_estimate(write, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["tolerance", str(write(transient_mv=None, ripple_mv=10.0)), "--skip-noise",
                 "--out", str(out)]) == EXIT_NOT_JUDGED
    text = capsys.readouterr().out
    assert text.index("Impedance of VOUT:") < text.index("Load U2")       # the estimate first
    ld = _doc(out)["loads"][0]
    est = ld["transient_estimate"]
    assert est["below_mv"] > 0 and est["above_mv"] > 0
    assert ld["hints"]["transient"].startswith(
        f"enter transient_mv for U2; KiPI's estimate with a typical loop at 100 kHz: "
        f"{est['below_mv']:.4g} / {est['above_mv']:.4g} mV")
    # Without a bandwidth given, no AC solve at all and no estimate.
    assert main(["tolerance", str(write(transient_mv=None, bandwidth=False)), "--skip-noise",
                 "--out", str(out)]) == EXIT_NOT_JUDGED
    text = capsys.readouterr().out
    assert "Impedance of" not in text
    assert _doc(out)["loads"][0]["hints"]["transient"] == (
        "enter transient_mv for U2; no KiPI estimate: no control loop bandwidth is given for U1 "
        "(ac.bandwidth_hz)")
    assert main(["tolerance", str(write(transient_mv=None)), "--skip-noise", "--skip-ac",
                 "--out", str(out)]) == EXIT_NOT_JUDGED
    assert _doc(out)["loads"][0]["hints"]["transient"] == (
        "enter transient_mv for U2; no KiPI estimate: --skip-ac left it out")


def test_exit_codes_and_json(write, tmp_path, capsys):
    o = ["--out", str(tmp_path / "o"), "--skip-ac", "--skip-noise"]
    # An entered ripple where no aggressor lists the probe: complete, 6 % holds.
    assert main(["tolerance", str(write(ripple_mv=12.0, listed=False)), "--json", *o]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)              # --json: only the document on stdout
    assert doc["verdict"] == "pass" and doc["loads"][0]["checks"]["total"]["passed"] is True
    # DC and accuracy alone are over 2 %: a fail while the ripple is missing.
    assert main(["tolerance", str(write(tol_pct=2.0)), "--json", *o]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    assert doc["failed"][0]["limit"] == "tol_pct" and "known parts alone" in doc["failed"][0]["reason"]
    # No tolerance on any load: nothing to judge, exit 0.
    assert main(["tolerance", str(write(tol_pct=None)), "--json", *o]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "not_judged" and doc["not_judged"] == []
    hints = doc["loads"][0]["hints"]                       # shown: why nothing was solved for it
    assert hints["ripple"] == "U1 not solved (no load with a tolerance needs it)"


def test_errors(write, tmp_path, capsys):
    assert main(["tolerance", str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
    path = write()
    path.write_text(path.read_text(encoding="utf-8").replace("tol_pct: 6.0", "tol_pct: [2, 5]"),
                    encoding="utf-8")
    assert main(["tolerance", str(path), "--json"]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "invalid_project" and "below" in err["message"]


def _strip(tmp_path, sources, loads, net="VCC"):
    """The strip as a .kicad_pcb whose J1 and U1 define ``net`` in KiCad fields, and a project
    file with its own rail of that net."""
    from coppernicus.engine.synthetic import write_strip_kicad_pcb
    from coppernicus.project import Rail
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {"J1": {"PI_RAIL": net, "PI_SOURCE": "3.3V"},
                                                     "U1": {"PI_RAIL": net, "PI_LOAD": "1A"}}, net=net)
    return Project(board="s.kicad_pcb", rails=[Rail(net, sources=sources, loads=loads)]
                   ).save(tmp_path / "s.coppernicus.yml")


def test_a_kicad_fields_rail_carries_the_project_tolerances_by_ref(tmp_path, capsys):
    from coppernicus.project import Load, Source
    path = _strip(tmp_path, [Source("J1", 3.3, accuracy_pct=1)], [Load("U1", 1.0, tol_pct=5, ripple_mv=2,
                                                                       transient_mv=3)])
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    (ld,) = doc["loads"]
    assert ld["verdict"] == "pass" and ld["parts"]["accuracy"]["source"] == "accuracy_pct of J1"
    notes = doc["assumptions"]
    assert any("tol_pct, ripple_mv, transient_mv of U1 from the project file" in a for a in notes)
    assert any("accuracy_pct of J1 from the project file" in a for a in doc["assumptions"])
    assert not [w for w in doc["warnings"] if "not used" in w]
    assert main(["tolerance", str(path), "--json", "--no-board-fields", "--out", str(tmp_path / "o")]
                ) == EXIT_OK                  # the project's own rail
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "pass" and not [a for a in doc["assumptions"] if "from the project file" in a]


def test_a_tolerance_on_a_part_the_kicad_fields_rail_lacks_is_lost_and_not_judged(tmp_path, capsys):
    from coppernicus.project import Load, Source
    # J5 and U9 are not in the fields' VCC: J5's accuracy and U9's tolerance are lost.
    path = _strip(tmp_path, [Source("J5", 3.3, accuracy_pct=1)],
                  [Load("U1", 1.0, tol_pct=5, ripple_mv=2, transient_mv=3), Load("U9", 1.0, tol_pct=4)])
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert any("KiCad footprint fields" in w and "U9" in w and "J5" in w and "U1" not in w
               for w in doc["warnings"])
    lost = [e for e in doc["not_judged"] if e["item"] == "U9"]
    assert len(lost) == 1 and lost[0]["limit"] == "tol_pct" and "override_board_fields" in lost[0]["reason"]
    hint = doc["loads"][0]["hints"]["accuracy"]
    assert hint.startswith("J5's accuracy_pct in the project file was lost: rail VCC comes from the "
                           "board's KiCad footprint fields, whose source is J1")
    assert "enter" not in hint.split(":")[0]


# ---------------------------------------------------------------------- the example

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tolerance"


def test_make_examples_writes_the_committed_tolerance_example(tmp_path):
    make = runpy.run_path(str(EXAMPLE.parent / "make_examples.py"))
    make["write_tolerance_example"](tmp_path, board=False)
    for name in ("tolerance.coppernicus.yml", "tolerance-tight.coppernicus.yml"):
        assert (tmp_path / name).read_text(encoding="utf-8") == (EXAMPLE / name).read_text(encoding="utf-8")
    assert (EXAMPLE / "tolerance.kipi.json.gz").is_file()


def test_the_example_passes_and_its_tight_copy_fails(tmp_path, capsys):
    """Solver figures are compared with a relative tolerance (CI runs on Linux and Windows)."""
    out = tmp_path / "out"
    assert main(["tolerance", str(EXAMPLE / "tolerance.coppernicus.yml"), "--out", str(out)]) == EXIT_OK
    text = capsys.readouterr().out
    # Each solve's cost first, then the report.
    assert text.index("Impedance of VOUT:") < text.index("Noise of U1:") < text.index("Load U2 on VOUT")
    doc = json.loads((out / "tolerance.coppernicus_tolerance_summary.json").read_text(encoding="utf-8"))
    (ld,) = doc["loads"]
    parts, total = ld["parts"], ld["checks"]["total"]
    assert {k: p["source"] for k, p in parts.items()} == {
        "dc": "power tree", "accuracy": "accuracy_pct of U1", "ripple": "noise of U1 at LOAD",
        "transient": "entered (transient_mv)"}
    assert (parts["accuracy"]["below_mv"], parts["transient"]["above_mv"]) == pytest.approx((18.0, 30.0))
    assert 0 < parts["dc"]["below_mv"] < 54 and 0 < parts["ripple"]["above_mv"] < 30
    for side in ("below", "above"):                         # the total adds the four parts
        assert total[f"{side}_mv"] == pytest.approx(sum(p[f"{side}_mv"] for p in parts.values()), rel=1e-9)
    assert total["allowed_below_mv"] == pytest.approx(108.0)
    assert (total["passed"], total["complete"]) == (True, True)
    assert ld["verdict"] == doc["verdict"] == "pass"
    assert "Load U2 on VOUT (nominal 1.8 V from U1, probe LOAD): PASS" in text
    assert "KiPI's estimate with a typical loop at 100 kHz" in parts["transient"]["detail"]
    assert f"check total {total['below_mv']:.4g} / {total['above_mv']:.4g} mV of 108 / 108 allowed" in text
    assert [e["solve"] for e in doc["estimates"]] == ["AC VOUT", "noise U1"]
    assert {"harmonics", "field_solves", "board_model"} <= set(doc["estimates"][1])     # noise_estimate's
    assert doc["stats"]["ac_rails"] == 1 and doc["stats"]["aggressors"] == 1
    assert main(["tolerance", str(EXAMPLE / "tolerance-tight.coppernicus.yml"), "--json",
                 "--out", str(out)]) == EXIT_LIMIT
    tight = json.loads(capsys.readouterr().out)
    (e,) = tight["failed"]
    assert (e["item"], e["limit"]) == ("U2", "tol_pct")
    t = tight["loads"][0]["checks"]["total"]
    assert t["below_mv"] == pytest.approx(total["below_mv"], rel=1e-4)       # the same board, solved again
    assert e["reason"] == (f"total {t['below_mv']:.4g} mV below nominal, over the 54 mV allowed and "
                           f"{t['above_mv']:.4g} mV above nominal, over the 54 mV allowed")


def test_a_rail_only_in_the_kicad_fields_solves(tmp_path, capsys):
    """A project file that names only the board: every rail from the fields, none of its own."""
    from coppernicus.engine.synthetic import write_strip_kicad_pcb
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"},
                                                     "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    path = Project(board="s.kicad_pcb").save(tmp_path / "s.coppernicus.yml")
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "not_judged" and doc["not_judged"] == [] and len(doc["loads"]) == 1
    # The KiCad-field notes, as the other commands carry them (merged.annotate).
    assert any(a.startswith("Rail VCC from KiCad footprint fields") for a in doc["assumptions"])


def test_a_project_with_the_board_temperature_prints_its_estimate_first(tmp_path, capsys):
    """examples/demo/demo-thermal: the tree is solved electro-thermally, its cost printed first."""
    demo = EXAMPLE.parent / "demo" / "demo-thermal.coppernicus.yml"
    assert main(["tolerance", str(demo), "--out", str(tmp_path / "o")]) == EXIT_OK
    text = capsys.readouterr().out
    assert text.startswith("Board temperature:") and "nominal 3.3 V from Supply" in text
    doc = json.loads((tmp_path / "o" / "demo-thermal.coppernicus_tolerance_summary.json").read_text(
        encoding="utf-8"))
    assert doc["estimates"][0]["solve"] == "board temperature"
    ld = doc["loads"][0]                   # settled, or a floor if not (temperature.py's rule)
    assert (ld["parts"]["dc"] or ld["floors"]["dc"])["source"] == "power tree (electro-thermal)"


def test_a_misspelt_tolerance_key_is_not_judged_with_a_suggestion(write, tmp_path, capsys):
    """The file loads (unknown keys are ignored, as ever), the load's ripple and transient are
    complete, but its tolerance was misspelt: not judged (exit 3), never a silent pass."""
    path = write(tol_pct=None, ripple_mv=12.0, listed=False)
    text = path.read_text(encoding="utf-8").replace("ripple_mv: 12.0", "ripple_mv: 12.0\n    tol_pc: 6")
    path.write_text(text, encoding="utf-8")
    code = main(["tolerance", str(path), "--json", "--skip-ac", "--out", str(tmp_path / "o")])
    assert code == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert "Load U2 on VOUT: unknown key 'tol_pc' in the project file, ignored: did you mean 'tol_pct'?" \
        in doc["warnings"]
    (e,) = doc["not_judged"]
    assert (e["item"], e["net"], e["limit"]) == ("U2", "VOUT", "tol_pc") and "did you mean" in e["reason"]


def test_a_second_project_load_on_the_same_ref_is_lost_not_silent(tmp_path, capsys):
    """The fields' rail has one U1: the project's first U1 row carries its tolerances, the second
    loses its own, which is not judged (never silently dropped)."""
    from coppernicus.project import Load, Source
    path = _strip(tmp_path, [Source("J1", 3.3, accuracy_pct=1)],
                  [Load("U1", 1.0, tol_pct=5, ripple_mv=2, transient_mv=3),
                   Load("U1", 0.5, name="U1b", tol_pct=3)])
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert doc["loads"][0]["verdict"] == "pass"            # the first U1, carried
    (e,) = doc["not_judged"]
    assert (e["item"], e["limit"]) == ("U1b", "tol_pct")


def test_a_net_name_with_braces_does_not_break_the_lost_reason(tmp_path, capsys):
    """KiCad writes "/" in a label as {slash}: the reason is built per part, never formatted."""
    from coppernicus.project import Load, Source
    path = _strip(tmp_path, [Source("J5", 3.3, accuracy_pct=1)],
                  [Load("U1", 1.0, tol_pct=5), Load("U9", 1.0, tol_pct=4)], net="V{slash}A")
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    (u9,) = [e for e in doc["not_judged"] if e["item"] == "U9"]
    assert u9["reason"].startswith("rail V{slash}A comes from the board's KiCad footprint fields, where no "
                                   "part takes U9's tolerances")
    assert doc["loads"][0]["hints"]["accuracy"].startswith("J5's accuracy_pct in the project file was lost: "
                                                           "rail V{slash}A")
