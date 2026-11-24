"""``coppernicus solve --tree`` on the synthetic power chain."""

import json

import pytest

from coppernicus.cli import EXIT_ERROR, EXIT_LIMIT, EXIT_OK, main
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
    doc = json.loads((out_dir / "chain.coppernicus_tree_summary.json").read_text())
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
    """The text output says which items a limit could not judge (exit code 0, as the
    per-rail solve), and each regulator's input drop on the rail feeding it."""
    from coppernicus.project import Project
    p = Project.load(chain)
    p.rail("+1V8").sources[0].voltage = 3.4                  # an LDO cannot boost
    p.rail("+1V8").limits = Limits(max_drop_pct=1.0)
    p.save(chain)
    assert main(["solve", str(chain), "--tree", "--out", str(tmp_path / "o")]) == EXIT_OK
    out = capsys.readouterr().out
    verdict = next(line for line in out.splitlines() if line.startswith("Verdict:"))
    assert "not_judged" in verdict and "U3" in verdict and "OUT OF REGULATION" in verdict
    u1 = next(line for line in out.splitlines() if line.strip().startswith("U1 "))
    assert "input drop" in u1
