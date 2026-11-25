"""``coppernicus noise`` on KiPI's synthetic coupled-noise board."""

import json

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import coupled_noise_board, coupled_noise_project, save_snapshot
from coppernicus.project import NoiseOptions, Project

STEM = "n.coppernicus"


@pytest.fixture()
def write(tmp_path):
    """Save the board and return a function writing the project (Qrr entered, logic_v 3.3)."""
    save_snapshot(coupled_noise_board(), tmp_path / "coupled_noise.kipi.json.gz")
    path = tmp_path / "n.coppernicus.yml"

    def write(board_model=True, qrr_nc=5.0, logic_v=3.3, margin=None):
        p = coupled_noise_project(qrr_nc=qrr_nc, logic_v=logic_v)
        p.noise = NoiseOptions(board_model=board_model)
        if margin is not None:
            p.victims[1].noise_margin_v = margin            # SIG_LOOP, 655 mV
        return p.save(path)
    return write


def test_noise_writes_csv_pngs_and_summary(write, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["noise", str(write()), "--out", str(out)]) == EXIT_OK
    text = capsys.readouterr().out
    for words in ("Aggressor U1", "ripple LOAD", "victim SIG_LOOP", "PASS", "assumption: Coupled noise",
                  "Verdict: pass", "Summary:"):
        assert words in text, words
    for suffix in ("noise.csv", "noise.png", "noise_h_field.png", "noise_e_field.png"):
        assert (out / f"{STEM}_U1_{suffix}").is_file(), suffix
    doc = json.loads((out / f"{STEM}_noise_summary.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "noise" and doc["verdict"] == "pass"
    assert doc["summary_file"].endswith(f"{STEM}_noise_summary.json")
    (a,) = doc["aggressors"]
    assert a["csv_file"].endswith("_U1_noise.csv") and len(a["png_files"]) == 3


def test_noise_exit_codes_and_json(write, tmp_path, capsys):
    out = ["--out", str(tmp_path / "o")]
    assert main(["noise", str(write(margin=0.5)), "--json", *out]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)          # --json: only the document on stdout
    assert doc["verdict"] == "fail" and doc["failed"][0]["item"] == "SIG_LOOP"
    assert doc["failed"][0]["aggressor"] == "U1" and doc["failed"][0]["limit"] == "noise_margin_v (V)"
    # KiPI's Qrr stand-in: a victim with a margin is not judged (exit 3, as solve).
    assert main(["noise", str(write(board_model=False, qrr_nc=None)), "--json", *out]) == EXIT_NOT_JUDGED
    assert json.loads(capsys.readouterr().out)["not_judged"][0]["aggressor"] == "U1"
    # No margin at all: ranked, nothing to judge (exit 0).
    assert main(["noise", str(write(board_model=False, logic_v=None)), "--json", *out]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["verdict"] == "not_judged"


def test_noise_errors(write, tmp_path, capsys):
    path = write(board_model=False)
    assert main(["noise", str(tmp_path / "missing.coppernicus.yml")]) == EXIT_ERROR
    assert main(["noise", str(path), "--aggressor", "NOPE"]) == EXIT_ERROR
    assert "NOPE" in capsys.readouterr().err
    p = Project.load(path)
    p.aggressors[0].fsw_hz = None
    p.save(path)
    assert main(["noise", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "solve_failed" and "enter its fsw_hz" in err["message"]
    p.aggressors = []
    p.save(path)
    assert main(["noise", str(path)]) == EXIT_ERROR
    assert "no aggressor" in capsys.readouterr().err.lower()


def test_noise_default_out_and_aggressor_option(write, tmp_path):
    path = write(board_model=False, logic_v=None)
    assert main(["noise", str(path), "--aggressor", "U1", "--aggressor", "U1"]) == EXIT_OK
    doc = json.loads((tmp_path / "results" / f"{STEM}_noise_summary.json").read_text(encoding="utf-8"))
    assert [a["aggressor"] for a in doc["aggressors"]] == ["U1"]
    assert doc["aggressors"][0]["png_files"][0].endswith("_U1_noise.png")


def test_noise_takes_the_kicad_fields_and_names_their_switching_frequency(write, tmp_path, monkeypatch,
                                                                         capsys):
    """The KiCad footprint fields merge as in the other solves (brief Q9); a regulator's
    PI_FSW is the aggressor's default switching frequency, named in the assumptions."""
    from coppernicus import board_fields
    path = write(board_model=False, logic_v=None)
    p = Project.load(path)
    p.aggressors[0].fsw_hz = None
    p.save(path)
    seen = {}

    def apply(project, read, use=None, ties=()):
        seen["use"] = use
        project.rails[1].sources[0].fsw_hz = 1e6                # as PI_FSW would set it
        return board_fields.Merged(project, {"VOUT": board_fields.Origin("VOUT", "fields")})
    monkeypatch.setattr(board_fields, "apply", apply)
    assert main(["noise", str(path), "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    a = json.loads(capsys.readouterr().out)["aggressors"][0]
    assert a["fsw_hz"] == 1e6 and seen["use"] is None
    assert any("from the source U1 (KiCad field PI_FSW)" in x for x in a["assumptions"])
    assert main(["noise", str(path), "--no-board-fields", "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    assert seen["use"] is False
