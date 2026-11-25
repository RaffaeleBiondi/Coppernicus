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
    assert ld["parts"]["dc"]["below_mv"] == pytest.approx(27.05, abs=0.01)


def test_without_a_transient_the_hint_carries_kipis_estimate(write, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["tolerance", str(write(transient_mv=None, ripple_mv=10.0)), "--skip-noise",
                 "--out", str(out)]) == EXIT_NOT_JUDGED
    text = capsys.readouterr().out
    assert text.index("Impedance of VOUT:") < text.index("Load U2")       # the estimate first
    hint = _doc(out)["loads"][0]["hints"]["transient"]
    assert hint.startswith("enter transient_mv for U2; KiPI's estimate with a typical loop at "
                           "100 kHz: 25.99 / ")
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


def test_errors(write, tmp_path, capsys):
    assert main(["tolerance", str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
    path = write()
    path.write_text(path.read_text(encoding="utf-8").replace("tol_pct: 6.0", "tol_pct: [2, 5]"),
                    encoding="utf-8")
    assert main(["tolerance", str(path), "--json"]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "invalid_project" and "below" in err["message"]


def test_a_rail_from_kicad_fields_warns_that_the_project_tolerances_are_not_used(tmp_path, capsys):
    from coppernicus.engine.synthetic import write_strip_kicad_pcb
    from coppernicus.project import Load, Rail, Source
    write_strip_kicad_pcb(tmp_path / "s.kicad_pcb", {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"},
                                                     "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}})
    path = Project(board="s.kicad_pcb", rails=[Rail("VCC", sources=[Source("J1", 3.3, accuracy_pct=1)],
                                                    loads=[Load("U1", 1.0, tol_pct=5)])]
                   ).save(tmp_path / "s.coppernicus.yml")
    assert main(["tolerance", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert any("KiCad footprint fields" in w and "U1" in w and "J1" in w for w in doc["warnings"])
    assert doc["loads"][0]["verdict"] == ""                 # the fields' U1 has no tolerance
    assert main(["tolerance", str(path), "--json", "--no-board-fields", "--out", str(tmp_path / "o")]
                ) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert doc["loads"][0]["verdict"] == "not_judged" and not doc["warnings"]


# ---------------------------------------------------------------------- the example

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "tolerance"


def test_make_examples_writes_the_committed_tolerance_example(tmp_path):
    make = runpy.run_path(str(EXAMPLE.parent / "make_examples.py"))
    make["write_tolerance_example"](tmp_path, board=False)
    for name in ("tolerance.coppernicus.yml", "tolerance-tight.coppernicus.yml"):
        assert (tmp_path / name).read_text(encoding="utf-8") == (EXAMPLE / name).read_text(encoding="utf-8")
    assert (EXAMPLE / "tolerance.kipi.json.gz").is_file()


def test_the_example_passes_and_its_tight_copy_fails(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["tolerance", str(EXAMPLE / "tolerance.coppernicus.yml"), "--out", str(out)]) == EXIT_OK
    text = capsys.readouterr().out
    # Each solve's cost first, then the report.
    assert text.index("Impedance of VOUT:") < text.index("Noise of U1:") < text.index("Load U2 on VOUT")
    for words in ("Load U2 on VOUT (nominal 1.8 V from U1, probe LOAD): PASS", "DC drop      27.05 / 0 mV",
                  "DC accuracy  18 / 18 mV  accuracy_pct of U1", "ripple       7.883 / 10.57 mV  noise of U1 "
                  "at LOAD", "transient    30 / 30 mV  entered (transient_mv); KiPI's estimate with a "
                  "typical loop at 100 kHz: 25.99 / 25.98 mV", "check total 82.94 / 58.57 mV of 108 / 108 "
                  "allowed (tol_pct)", "Verdict: pass."):
        assert words in text, words
    doc = json.loads((out / "tolerance.coppernicus_tolerance_summary.json").read_text(encoding="utf-8"))
    assert [e["solve"] for e in doc["estimates"]] == ["AC VOUT", "noise U1"]
    assert doc["stats"]["ac_rails"] == 1 and doc["stats"]["aggressors"] == 1
    assert main(["tolerance", str(EXAMPLE / "tolerance-tight.coppernicus.yml"), "--json",
                 "--out", str(out)]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    (e,) = doc["failed"]
    assert (e["item"], e["limit"]) == ("U2", "tol_pct")
    assert e["reason"] == "total 82.94 mV below nominal, over the 54 mV allowed and 58.57 mV above " \
                          "nominal, over the 54 mV allowed"
