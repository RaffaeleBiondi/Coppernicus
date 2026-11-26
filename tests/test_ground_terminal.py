"""A source's ground terminal on several parts (``extra_ground_pins``, KiCad ``PI_GND_REF=A,B``),
the ground twin of ``extra_pins``: every listed part's pads on the ground net are held at the
0 V reference together, and the copper sets how the return splits between them. The case: a
cell pack whose negative pole is two holders at the ends of a ground plane, the load returning
into its middle (engine/synthetic.py ``Pack``). Synthetic boards only."""

from pathlib import Path

import pytest
from kipi.dc.pi.model import PinRef
from kipi.dc.pi.tree import solve_tree

from coppernicus.engine import Board, EngineError, ground_terminal
from coppernicus.engine.pisetup import to_pisetup
from coppernicus.engine.synthetic import (
    Pack,
    demo_board,
    pack_board,
    pack_project,
    power_chain_board,
    power_chain_project,
    save_snapshot,
)
from coppernicus.engine.tree import solve_project_tree
from coppernicus.project import Project, ProjectError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def board():
    return Board(pack_board())


def holder_amps(board, project):
    """The amps each part of the source's 0 V terminal takes from the ground copper (KiPI's
    solved per-pad currents of the ground's reference), by part; and the ground's terminal."""
    kt = solve_tree(board.snapshot, to_pisetup(board, project))
    [ground] = [g for g in kt.groups.values() if g.kind == "ground"]
    [term] = [t for t in ground.result.fields.terminals if t.kind == "source"]
    out = {}
    for pid, amps in zip(term.pad_ids, term.i_pads):
        ref = board.pad(pid).footprint
        out[ref] = out.get(ref, 0.0) + amps
    return out, term


def test_with_one_holder_as_ground_all_the_return_enters_it(board):
    amps, term = holder_amps(board, pack_project(("BT4",)))
    assert set(amps) == {"BT4"} and len(term.pad_ids) == 2
    assert amps["BT4"] == pytest.approx(Pack.I_U1, rel=1e-6)


def test_with_both_holders_the_return_splits_and_the_ground_rise_drops(board):
    amps, term = holder_amps(board, pack_project(("BT4", "BT8")))
    assert set(amps) == {"BT4", "BT8"} and len(term.pad_ids) == 4
    assert sum(amps.values()) == pytest.approx(Pack.I_U1, rel=1e-6)          # the totals balance
    assert amps["BT4"] == pytest.approx(Pack.I_U1 / 2, rel=0.02)             # symmetric: half each
    assert amps["BT8"] == pytest.approx(Pack.I_U1 / 2, rel=0.02)
    assert term.v_min == term.v_max == 0.0                                    # every pad at 0 V
    one = solve_project_tree(board, pack_project(("BT4",)))
    both = solve_project_tree(board, pack_project(("BT4", "BT8")))
    g1, g2 = one.groups[Pack.NEG], both.groups[Pack.NEG]
    assert 0 < g2.max_drop < 0.75 * g1.max_drop                               # the ground rise
    assert 0 < g2.power_loss < 0.75 * g1.power_loss                           # the ground copper's loss
    [u1_one], [u1_both] = one.loads, both.loads
    assert 0 < u1_both.v_ground_v < u1_one.v_ground_v
    assert u1_both.drop_v < u1_one.drop_v
    [vrm] = both.vrms
    assert vrm.v_ground_v == 0.0 and vrm.i_out_a == pytest.approx(Pack.I_U1, rel=1e-6)
    assert any("BT4" in a and "BT8" in a and "0 V" in a for a in both.assumptions)


def test_the_order_of_the_parts_does_not_matter(board):
    a, _ = holder_amps(board, pack_project(("BT4", "BT8")))
    b, _ = holder_amps(board, pack_project(("BT8", "BT4")))
    assert a == pytest.approx(b, rel=1e-9)


def test_the_pads_can_be_chosen_part_by_part(board):
    """``REF.PAD`` takes one tab of the other holder; the anchor's ``pins`` choose its own."""
    project = pack_project(("BT4", "BT8"))
    src = project.rails[0].sources[0]
    src.ground_pins, src.extra_ground_pins = ["1"], ["BT8.2"]
    amps, term = holder_amps(board, project)
    assert sorted(board.pad(p).number for p in term.pad_ids) == ["1", "2"] and set(amps) == {"BT4", "BT8"}
    assert sum(amps.values()) == pytest.approx(Pack.I_U1, rel=1e-6)


@pytest.mark.parametrize("extra, named", [
    (["BT9.*"], "BT9 has no pads on net -V_BAT"),                 # a misspelt part
    (["BT8.7"], "BT8 has no pin '7' on net -V_BAT"),              # a pad it does not have
    (["U1.1"], "U1 has no pin '1' on net -V_BAT"),                # a pad on another net
])
def test_unknown_parts_and_pads_are_named(board, extra, named):
    project = pack_project(("BT4",))
    project.rails[0].sources[0].extra_ground_pins = extra
    with pytest.raises(EngineError, match="extra ground pins") as e:
        to_pisetup(board, project)
    assert named in str(e.value) and "BT1" in str(e.value)


def test_only_a_plain_source_with_its_ground_takes_them(board):
    project = pack_project(("BT4", "BT8"))
    src = project.rails[0].sources[0]
    src.ground_net, src.ground_ref = "", ""
    with pytest.raises(EngineError, match="BT1.*extra_ground_pins.*ground"):
        to_pisetup(board, project)
    project = pack_project(("BT4", "BT8"))
    project.rails[0].sources[0].kind = "ldo"
    project.rails[0].sources[0].input_net = Pack.POS
    with pytest.raises(EngineError, match="BT1.*ldo.*kind: source"):
        to_pisetup(board, project)


def test_never_solved_with_one_part_on_a_kipi_it_was_not_written_for(board, monkeypatch):
    ground_terminal.uninstall()
    monkeypatch.setattr(ground_terminal.kipi, "__version__", "0.0.0")
    with pytest.raises(EngineError, match="BT1.*several parts.*KiPI 0.0.0"):
        to_pisetup(board, pack_project(("BT4", "BT8")))
    assert not ground_terminal.installed()


def test_a_single_ground_part_maps_exactly_as_before():
    """Without extra_ground_pins the setup holds KiPI's own PinRef, on the examples, the demo,
    the power chain and the pack, and the seam is not needed."""
    ground_terminal.uninstall()
    demo = Project.load(ROOT / "examples" / "demo" / "demo.coppernicus.yml")
    for b, project in ((Board(demo_board()), demo), (Board(power_chain_board()), power_chain_project()),
                       (Board(pack_board()), pack_project(("BT4",)))):
        setup = to_pisetup(b, project)
        assert all(v.ground is None or type(v.ground) is PinRef for v in setup.vrms)
    assert not ground_terminal.installed()
    setup = to_pisetup(Board(pack_board()), pack_project(("BT4", "BT8")))
    assert ground_terminal.installed() and type(setup.vrms[0].ground) is ground_terminal.PartsPinRef
    assert setup.vrms[0].ground.parts() == ["BT4", "BT8"]


def test_the_project_file_round_trips_them_and_refuses_what_is_not_ref_pad(tmp_path):
    p = pack_project(("BT4", "BT8"))
    p.save(tmp_path / "pack.coppernicus.yml")
    text = (tmp_path / "pack.coppernicus.yml").read_text(encoding="utf-8")
    assert "extra_ground_pins:" in text and "BT8.*" in text
    assert Project.load(tmp_path / "pack.coppernicus.yml").rails == p.rails
    [one] = Project(rails=pack_project(("BT4",)).rails).to_dict()["rails"][0]["sources"]
    assert "extra_ground_pins" not in one
    rail = {"net": Pack.POS, "sources": [{"ref": "BT1", "voltage": 6.4,
                                          "ground": {"ref": "BT4", "net": Pack.NEG}}]}
    for bad, why in ((["BT8"], "REF.PAD"), ("BT8.*", "a list"), (["BT8.*", ".1"], "REF.PAD")):
        rail["sources"][0]["extra_ground_pins"] = bad
        with pytest.raises(ProjectError, match=why):
            Project.from_dict({"board": "b.kicad_pcb", "rails": [rail]})
    rail["sources"][0]["extra_ground_pins"] = ["BT8.*"]
    del rail["sources"][0]["ground"]
    with pytest.raises(ProjectError, match="extra_ground_pins needs its ground"):
        Project.from_dict({"board": "b.kicad_pcb", "rails": [rail]})


def test_write_project_takes_them_and_the_mcp_schema_lists_them(tmp_path):
    from coppernicus.mcp_server import ProjectSourceSpec
    from coppernicus.service import ServiceError, Session
    board = tmp_path / "pack.kipi.json.gz"
    save_snapshot(pack_board(), board)
    path = tmp_path / "p.coppernicus.yml"
    spec = {"ref": "BT1", "voltage_v": 6.4, "ground": {"ref": "BT4", "net": Pack.NEG},
            "extra_ground_pins": ["BT8.*"]}
    ProjectSourceSpec.model_validate(spec)                       # extra="forbid": it is known
    assert "extra_ground_pins" in ProjectSourceSpec.model_json_schema()["properties"]
    session = Session(out_dir=tmp_path / "o")
    session.write_project(path, board_path=board, rails=[{
        "net": Pack.POS, "sources": [spec],
        "loads": [{"ref": "U1", "current_a": 2.0, "pins": ["1"],
                   "ground": {"net": Pack.NEG, "pins": ["2"]}}]}])
    [src] = Project.load(path).rails[0].sources
    assert (src.ground_ref, src.extra_ground_pins) == ("BT4", ["BT8.*"])
    tree = session.solve_tree(path)
    assert tree["vrms"][0]["v_ground_v"] == 0.0
    with pytest.raises(ServiceError, match="REF.PAD"):
        session.write_project(path, rails=[{"net": Pack.POS, "sources": [
            {**spec, "extra_ground_pins": ["BT8"]}]}], overwrite=True)


APP_SCRIPT = '''
from PySide6.QtWidgets import QApplication
from coppernicus.app.panels import RegulatorBox
from coppernicus.engine.synthetic import pack_project
app = QApplication.instance() or QApplication([])
src = pack_project(("BT4", "BT8")).rails[0].sources[0]
box = RegulatorBox()
box.show_source(src, ["+V_BAT", "-V_BAT"])
box.write(src)
assert (src.ground_ref, src.extra_ground_pins) == ("BT4", ["BT8.*"]), src
box.ground.set("", None, "")
box.write(src)
assert src.ground_net == "" and src.extra_ground_pins is None, src
print("ok")
'''


def test_the_app_keeps_them_and_drops_them_with_the_ground():
    """The Regulator box keeps a source's extra ground pins, and drops them when its ground is
    cleared, so the project file it saves loads again (child process, offscreen)."""
    import os
    import subprocess
    import sys
    pytest.importorskip("PySide6")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    run = subprocess.run([sys.executable, "-c", APP_SCRIPT], env=env, capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0 and "ok" in run.stdout, run.stderr
