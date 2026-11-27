"""Where a regulator's conversion loss heats the board with ``regulator_heat: part_pads``: the
pads of its own part (the one with its input pins: an external-inductor buck's IC, not the
inductor its output is held at), and ``output_part_heat_pct``'s share, when entered, on the
output part's pads. Synthetic boards only."""

import dataclasses
from types import SimpleNamespace

import pytest
from kipi.dc.pi.model import PinRef, Vrm, VrmPhase

from coppernicus.engine import Board
from coppernicus.engine.board import EngineError
from coppernicus.engine.pisetup import to_pisetup
from coppernicus.engine.synthetic import coupled_noise_board, coupled_noise_project
from coppernicus.engine.thermal import _part_pads_heat, check_heat_shares, regulator_parts
from coppernicus.engine.tree import solve_project_tree
from coppernicus.project import Project, ProjectError, ThermalOptions

# ---------------------------------------------------------------------- placement (no solve)

class _Recorder:
    """Stands in for KiPI's _Heat: which pads got how many watts."""

    def __init__(self):
        self.calls = []

    def add_pads(self, kind, pad_ids, watts):
        self.calls.append((kind, tuple(pad_ids), watts))


def _snap(*parts):
    pads = [SimpleNamespace(id=f"{ref}.{n}", footprint=ref, shapes=[object()])
            for ref, count in parts for n in range(1, count + 1)]
    return SimpleNamespace(pads=pads)


def _buck(name="U1", phases=1):
    """An external-inductor buck: input on U1, output held at L1 (L2... for more phases)."""
    first = PinRef("L1", "VOUT", ["2"])
    more = [VrmPhase(PinRef(f"L{k}", "VOUT", ["2"]), PinRef("U1", "VIN", ["1"]), None)
            for k in range(2, phases + 1)]
    return Vrm(name, first, 1.8, kind="smps", input=PinRef("U1", "VIN", ["1"]), phases=more)


def _placed(vrm, loss, shares, phase_losses=()):
    rec = _Recorder()
    tree = SimpleNamespace(vrms=[SimpleNamespace(name=vrm.name, p_loss=loss,
                                                 phases=[SimpleNamespace(p_loss=w) for w in phase_losses])])
    snap = _snap(("U1", 4), ("L1", 2), ("L2", 2), ("U9", 3))
    _part_pads_heat(rec, tree, snap, SimpleNamespace(vrms=[vrm]), shares)
    return rec


def test_an_external_inductor_bucks_loss_goes_to_its_ic_not_the_inductor():
    rec = _placed(_buck(), 0.6, {})
    assert rec.calls == [("VRM conversion", ("U1.1", "U1.2", "U1.3", "U1.4"), 0.6)]


def test_an_entered_share_goes_to_the_output_parts_pads_and_the_rest_stays_on_the_ic():
    rec = _placed(_buck(), 0.6, {"U1": 30.0})
    assert rec.calls == [("VRM conversion", ("U1.1", "U1.2", "U1.3", "U1.4"), pytest.approx(0.42)),
                         ("VRM conversion", ("L1.1", "L1.2"), pytest.approx(0.18))]
    assert sum(w for *_, w in rec.calls) == pytest.approx(0.6, rel=1e-15)


def test_each_phase_shares_its_own_loss_with_its_own_inductor():
    rec = _placed(_buck(phases=2), 1.0, {"U1": 50.0}, phase_losses=(0.6, 0.4))
    got = [(pads[0].split(".")[0], w) for _k, pads, w in rec.calls]
    assert got == [("U1", pytest.approx(0.3)), ("L1", pytest.approx(0.3)),
                   ("U1", pytest.approx(0.2)), ("L2", pytest.approx(0.2))]


def test_a_regulator_on_one_part_keeps_all_its_loss_there_as_before():
    """An LDO (input and output on U9): the same single call as before the change."""
    ldo = Vrm("U9", PinRef("U9", "V3", ["2"]), 3.3, kind="ldo", input=PinRef("U9", "V5", ["1"]))
    assert _placed(ldo, 1.2, {}).calls == [("VRM conversion", ("U9.1", "U9.2", "U9.3"), 1.2)]


def test_an_ideal_source_with_a_loss_would_use_its_outputs_part():
    src = Vrm("J1", PinRef("U9", "V3", []), 3.3)          # kind "source": no input pins
    assert regulator_parts(SimpleNamespace(vrms=[src])) == {"J1": [("U9", "U9")]}


# ---------------------------------------------------------------------- the key

def test_the_share_is_saved_and_loaded(tmp_path):
    project = coupled_noise_project()
    project.thermal = ThermalOptions(enabled=True, regulator_heat="part_pads",
                                     output_part_heat_pct={"U1": 25})
    back = Project.load(project.save(tmp_path / "b.coppernicus.yml"))
    assert back.thermal.output_part_heat_pct == {"U1": 25.0}
    assert back.thermal == project.thermal


@pytest.mark.parametrize("value, words", [
    ({"U1": 120}, "0 to 100"), ({"U1": -1}, "0 to 100"), ({"U1": True}, "number"),
    ({"U1": "lots"}, "U1"), ([30], "maps a regulator"), (30, "maps a regulator"),
])
def test_the_share_is_checked(value, words):
    with pytest.raises(ProjectError, match=words):
        ThermalOptions(output_part_heat_pct=value)


# ---------------------------------------------------------------------- solved

@pytest.fixture(scope="module")
def buck_board():
    return Board(coupled_noise_board())


def _thermal_project(**thermal):
    project = coupled_noise_project()
    project.thermal = ThermalOptions(enabled=True, iterations=1, **thermal)
    return project


def test_solved_the_heat_total_is_the_same_wherever_it_goes(buck_board):
    runs = {key: solve_project_tree(buck_board, _thermal_project(**opts)).thermal
            for key, opts in {"out": {}, "part": {"regulator_heat": "part_pads"},
                              "share": {"regulator_heat": "part_pads",
                                        "output_part_heat_pct": {"U1": 40}}}.items()}
    heat = [t.heat_w for t in runs.values()]
    assert heat[0] > 0 and heat[1] == pytest.approx(heat[0], rel=1e-9) == heat[2]
    line = {k: next(a for a in t.assumptions if a.startswith("Regulator heat (U1 ")) for k, t in runs.items()}
    assert "U1's pads, none on L1 (its output part)" in line["part"]
    assert "60 % on U1's pads, 40 % on L1's (output_part_heat_pct, entered)" in line["share"]
    assert "not the inductor" in line["part"] and "none is assumed" in line["part"]
    assert "output pins" in line["out"]


def test_a_share_for_no_regulator_or_one_on_its_own_part_is_refused(buck_board):
    with pytest.raises(EngineError, match="'U7', which is not a regulator.*U1"):
        solve_project_tree(buck_board, _thermal_project(regulator_heat="part_pads",
                                                        output_part_heat_pct={"U7": 10}))
    with pytest.raises(EngineError, match="J1: its output is on its own part"):
        solve_project_tree(buck_board, _thermal_project(regulator_heat="part_pads",
                                                        output_part_heat_pct={"J1": 10}))


def test_a_share_with_output_pads_is_named_as_not_used():
    project = _thermal_project(output_part_heat_pct={"U1": 10})
    setup = to_pisetup(Board(coupled_noise_board()), project)
    assert check_heat_shares(setup, project.thermal) == [
        "output_part_heat_pct is not used: it applies with regulator_heat: part_pads only "
        "(output_pads puts each regulator's loss at its output pins, KiPI's placement)."]
    assert check_heat_shares(setup, dataclasses.replace(project.thermal, output_part_heat_pct={})) == []
