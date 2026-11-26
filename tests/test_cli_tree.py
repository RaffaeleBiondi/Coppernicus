"""``coppernicus solve --tree`` on the synthetic power chain."""

import json

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_NOT_JUDGED, EXIT_OK, main
from coppernicus.engine.synthetic import power_chain_board, power_chain_project, save_snapshot
from coppernicus.project import Limits


@pytest.fixture()
def chain(tmp_path):
    save_snapshot(power_chain_board(), tmp_path / "power_chain.kipi.json.gz")
    return power_chain_project().save(tmp_path / "chain.coppernicus.yml")


def test_tree_prints_vrm_load_and_group_tables(chain, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert main(["solve", str(chain), "--tree", "--out", str(out_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    for word in ("VRMs", "Loads", "Groups", "U1", "smps", "U3", "across", "GND", "rise",
                 "not_judged", "assumption"):
        assert word in out, word
    doc = json.loads((out_dir / "chain.coppernicus_tree_summary.json").read_text(encoding="utf-8"))
    assert doc["kind"] == "power_tree" and doc["summary_file"].endswith("_tree_summary.json")


def test_tree_json_only_json_and_exit_codes(chain, tmp_path, capsys):
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "not_judged" and {g["group"] for g in doc["groups"]} == {
        "VIN", "+3V3", "+1V8", "GND"}
    from coppernicus.project import Project
    p = Project.load(chain)
    p.rail("+3V3").limits = Limits(max_drop_pct=0.01)
    p.save(chain)
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_LIMIT
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"


def test_tree_errors(chain, tmp_path, capsys):
    assert main(["solve", str(chain), "--tree", "--rail", "+3V3"]) == EXIT_ERROR
    assert "--tree" in capsys.readouterr().err
    from coppernicus.project import Project
    p = Project.load(chain)
    p.rail("+3V3").loads[0].ground_net = ""
    p.save(chain)
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "solve_failed" and "U4" in err["message"]
    assert main(["solve", str(tmp_path / "missing.coppernicus.yml"), "--tree"]) == EXIT_ERROR


def test_tree_text_names_what_was_not_judged_and_regulator_input_drops(chain, tmp_path, capsys):
    """The text output says which items a limit could not judge (exit code 3, as the
    per-rail solve: the owner's rule), and each regulator's input drop on the rail feeding it."""
    from coppernicus.project import Project
    p = Project.load(chain)
    p.rail("+1V8").sources[0].voltage = 3.4                  # an LDO cannot boost
    p.rail("+1V8").limits = Limits(max_drop_pct=1.0)
    p.save(chain)
    assert main(["solve", str(chain), "--tree", "--out", str(tmp_path / "o")]) == EXIT_NOT_JUDGED
    out = capsys.readouterr().out
    verdict = next(line for line in out.splitlines() if line.startswith("Verdict:"))
    assert "not_judged" in verdict and "U3" in verdict and "OUT OF REGULATION" in verdict
    u1 = next(line for line in out.splitlines() if line.strip().startswith("U1 "))
    assert "input drop" in u1


def test_tree_summary_file_is_written_with_forward_slashes(chain, tmp_path, capsys):
    assert main(["solve", str(chain), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert "\\" not in doc["summary_file"] and doc["summary_file"].endswith("_tree_summary.json")
    assert "probes" not in doc                                # only asked for with --probe


def test_tree_probe_reads_the_group_under_each_point(chain, tmp_path, capsys):
    """``--probe LAYER@X,Y`` (repeatable): the group with copper there, its voltage and its
    drop or rise, as the app's readout words them; the power chain's prototype, U3's ground
    pad rising 38.470 mV."""
    args = ["solve", str(chain), "--tree", "--out", str(tmp_path / "o"),
            "--probe", "B.Cu@97.5,1.0", "--probe", "F.Cu@90,1", "--probe", "F.Cu@50,20"]
    assert main(args) == EXIT_OK
    out = capsys.readouterr().out
    gnd = next(line for line in out.splitlines() if line.startswith("Probe B.Cu"))
    assert "GND" in gnd and "rise = 38.470 mV above 0 V (ground rise)" in gnd, gnd
    rail = next(line for line in out.splitlines() if line.startswith("Probe F.Cu (90"))
    assert "+1V8" in rail and "drop = " in rail, rail
    assert any(line.startswith("Probe F.Cu (50") and "no solved copper" in line
               for line in out.splitlines())
    assert main([*args, "--json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    first, second, off = doc["probes"]
    assert first["group"] == "GND" and first["kind"] == "ground" and first["layer"] == "B.Cu"
    assert first["drop_mv"] == pytest.approx(38.470, abs=5e-4) and first["drop_word"] == "rise"
    assert second["group"] == "+1V8" and off["on_copper"] is False
    saved = json.loads((tmp_path / "o" / "chain.coppernicus_tree_summary.json").read_text(encoding="utf-8"))
    assert saved["probes"] == doc["probes"]


@pytest.mark.parametrize("extra, word", [
    (["--tree", "--probe", "B.Cu97.5,1"], "LAYER@X,Y"),
    (["--tree", "--probe", "B.Cu@97.5"], "LAYER@X,Y"),
    (["--tree", "--probe", "In9.Cu@1,1"], "B.Cu"),
    (["--probe", "B.Cu@97.5,1"], "--tree"),
])
def test_tree_probe_errors_before_solving(chain, tmp_path, capsys, extra, word):
    assert main(["solve", str(chain), "--json", "--out", str(tmp_path / "o"), *extra]) == EXIT_ERROR
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "invalid_input" and word in err["message"] + err["hint"], err
    assert not (tmp_path / "o" / "chain.coppernicus_tree_summary.json").exists()


def test_solve_prints_a_negative_rail_load_at_its_worst_pin(tmp_path, capsys):
    """On a negative rail the worst pin of a three-pin load is its highest (toward 0 V)."""
    from coppernicus.engine import Board, solve_rail
    from coppernicus.engine.synthetic import pins_board
    from coppernicus.project import Load, Project, Rail, Source
    save_snapshot(pins_board(), tmp_path / "pins.kipi.json.gz")
    project = Project(board="pins.kipi.json.gz", rails=[Rail("VCC", sources=[Source("J1", -3.3)],
                                                             loads=[Load("U1", 1.0)])])
    path = project.save(tmp_path / "neg.coppernicus.yml")
    res = solve_rail(Board(pins_board()), project.rails[0])
    [u1] = res.loads
    assert u1.v_max - u1.v_min > 1e-4
    assert main(["solve", str(path), "--no-maps", "--out", str(tmp_path / "o")]) == EXIT_OK
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("  load   U1")]
    assert f"{u1.v_max:8.4f} V" in line
