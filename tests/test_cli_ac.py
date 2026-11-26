"""``coppernicus ac`` on the synthetic decoupling board (a quick sweep)."""

import json

import pytest

from coppernicus.cli import EXIT_CODES, EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project, save_snapshot
from coppernicus.project import AcOptions, Project

QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4,
             max_edge_mm=2.0)
STEM = "d.coppernicus"


@pytest.fixture()
def decap(tmp_path):
    """Save the board and return a function writing the project with a target (or none)."""
    save_snapshot(ac_decap_board(), tmp_path / "ac_decap.kipi.json.gz")
    path = tmp_path / "d.coppernicus.yml"

    def write(target_ohm=None, bandwidth_hz=1e5):
        p = ac_decap_project()
        p.ac = AcOptions(**QUICK, bandwidth_hz={"U1": bandwidth_hz} if bandwidth_hz else {})
        p.probes[0].target_ohm = target_ohm
        return p.save(path)
    return write


def test_ac_writes_csv_png_and_summary(decap, tmp_path, capsys):
    path = decap(target_ohm=1.0)                       # min |Z| about 22 mΩ: a loose target
    out = tmp_path / "out"
    assert main(["ac", str(path), "--out", str(out)]) == EXIT_OK
    text = capsys.readouterr().out
    for word in ("Rail +3V3", "probe U1", "PASS", "assumption: Open-loop", "Verdict: pass", "Summary:"):
        assert word in text, word
    assert (out / f"{STEM}_P3V3_impedance.csv").is_file()
    assert (out / f"{STEM}_P3V3_impedance.png").is_file()
    doc = json.loads((out / f"{STEM}_ac_summary.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "ac_impedance" and doc["verdict"] == "pass"
    assert doc["summary_file"].endswith(f"{STEM}_ac_summary.json")
    assert doc["rails"][0]["csv_file"].endswith("_P3V3_impedance.csv")
    assert doc["rails"][0]["png_file"].endswith("_P3V3_impedance.png")


def test_ac_exit_codes_and_json(decap, tmp_path, capsys):
    out = ["--out", str(tmp_path / "o")]
    assert main(["ac", str(decap(target_ohm=0.001)), "--json", *out]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)          # --json: only the document on stdout
    assert doc["verdict"] == "fail" and doc["failed"][0]["item"] == "U1"
    assert main(["ac", str(decap()), "--json", *out]) == EXIT_OK          # no target: ranked
    assert json.loads(capsys.readouterr().out)["verdict"] == "not_judged"
    # A target with no bandwidth given: not judged (exit 3, as solve).
    assert main(["ac", str(decap(target_ohm=1.0, bandwidth_hz=None)), "--json", *out]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert doc["not_judged"][0]["rail"] == "+3V3" and doc["rails"][0]["bandwidth_assumed"] is True


def test_ac_takes_the_kicad_fields_unless_no_board_fields(decap, tmp_path, monkeypatch, capsys):
    """The KiCad footprint fields merge as in noise and tolerance; --no-board-fields reaches
    the merge as use=False, and the solve runs on the project file alone."""
    from coppernicus import board_fields
    path = decap(target_ohm=1.0)                       # the project file's loose target: passes
    seen = []

    def apply(project, read, use=None, ties=()):
        seen.append(use)
        if use is not False:                           # as fields that change the project would
            project.probes[0].target_ohm = 0.001
        return board_fields.Merged(project)
    monkeypatch.setattr(board_fields, "apply", apply)
    out = ["--json", "--out", str(tmp_path / "o")]
    assert main(["ac", str(path), *out]) == EXIT_LIMIT
    assert main(["ac", str(path), "--no-board-fields", *out]) == EXIT_OK
    assert seen == [None, False]


@pytest.mark.parametrize("command", ["ac", "noise", "tolerance", "solve"])
def test_help_lists_the_exit_codes(command, capsys):
    """``ac --help`` ends with the exit codes, as noise, tolerance and solve do."""
    with pytest.raises(SystemExit) as stop:
        main([command, "--help"])
    assert stop.value.code == 0
    text = " ".join(capsys.readouterr().out.split())   # argparse rewraps the epilog
    assert EXIT_CODES in text and "--no-board-fields" in text


def test_ac_errors(decap, tmp_path, capsys):
    path = decap()
    assert main(["ac", str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
    p = Project.load(path)
    p.probes[0].pins = ["9"]
    p.save(path)
    assert main(["ac", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "solve_failed" and "Probe U1" in err["message"]
    p.probes = []
    p.save(path)
    assert main(["ac", str(path)]) == EXIT_ERROR
    assert "no probe" in capsys.readouterr().err.lower()


def test_ac_default_out_is_results_next_to_the_project_and_rail_option(decap, tmp_path, capsys):
    path = decap()
    assert main(["ac", str(path), "--rail", "+3V3"]) == EXIT_OK
    assert (tmp_path / "results" / f"{STEM}_ac_summary.json").is_file()
    assert main(["ac", str(path), "--rail", "NOPE"]) == EXIT_ERROR
    assert "NOPE" in capsys.readouterr().err


def test_a_probe_no_rail_reaches_is_not_judged_or_named(decap, tmp_path, capsys):
    """A probe whose net no rail with a source reaches is not solved: with a target it is a
    not-judged entry (exit 3), without one a warning; with no probe reached, an error."""
    from coppernicus.project import Probe
    path = decap()
    p = Project.load(path)
    p.probes.append(Probe("far", "U1", "GND", ["2"], target_ohm=0.1))
    p.save(path)
    out = ["--out", str(tmp_path / "o")]
    assert main(["ac", str(path), "--json", *out]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    (entry,) = doc["not_judged"]
    assert (entry["item"], entry["net"], entry["rail"]) == ("far", "GND", "")
    assert "not solved" in entry["reason"]
    p.probes[1].target_ohm = None
    p.save(path)
    assert main(["ac", str(path), "--json", *out]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["not_judged"] == [] and "Probe far" in doc["warnings"][0]
    assert main(["ac", str(path), *out]) == EXIT_OK
    assert "warning: Probe far" in capsys.readouterr().out
    p.probes = p.probes[1:]
    p.save(path)
    assert main(["ac", str(path), *out]) == EXIT_ERROR
    assert "far on GND" in capsys.readouterr().err


def test_two_rail_nets_of_one_rail_are_solved_once(tmp_path, monkeypatch, capsys):
    """+3V3_F is behind FB1: --rail +3V3 --rail +3V3_F resolves both to +3V3 before solving."""
    import coppernicus.engine.ac as ac
    from coppernicus.engine.synthetic import ac_ferrite_board, ac_ferrite_project
    save_snapshot(ac_ferrite_board(), tmp_path / "ac_ferrite.kipi.json.gz")
    p = ac_ferrite_project()
    p.ac = AcOptions(**QUICK, bandwidth_hz={"U1": 1e5}, link_esl_h={"FB1": 1e-6})
    path = p.save(tmp_path / "f.coppernicus.yml")
    solved = []
    real = ac.solve_ac_rail

    def counted(board, project, rail, **kw):
        solved.append(rail)
        return real(board, project, rail, **kw)
    monkeypatch.setattr(ac, "solve_ac_rail", counted)
    assert main(["ac", str(path), "--rail", "+3V3", "--rail", "+3V3_F", "--json",
                 "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert solved == ["+3V3"] and [r["rail"] for r in doc["rails"]] == ["+3V3"]
