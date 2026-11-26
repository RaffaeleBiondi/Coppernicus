"""The power tree's 0 V reference view (S2): each VRM's voltage across its own pins, and the
voltages shown from another 0 V (a part's ground pins or pads) with no re-solve. A display
shift only: the tree is never changed, every drop, rise, limit and verdict stays from the
solve's 0 V (the sources' ground pins). examples/power_chain and synthetic boards only."""

import copy
import json
import re
import shutil
from pathlib import Path

import anyio
import numpy as np
import pytest
import yaml
from mcp import Client

from coppernicus.cli import EXIT_ERROR, EXIT_OK, main
from coppernicus.engine import Board, EngineError
from coppernicus.engine.synthetic import F, SyntheticBoard, two_layer_stackup
from coppernicus.engine.tree import reference_parts, solve_project_tree, tree_reference
from coppernicus.mcp_server import build_server
from coppernicus.power_tree import tree_summary, tree_text, tree_verdicts
from coppernicus.project import Load, Project, ProjectError, Rail, Source, reference_from_text, reference_spec
from coppernicus.service import ServiceError, Session
from coppernicus.service.session import probe_tree

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"
U1_GROUND_V = 5.6906892570577e-3        # U1.3 in the solve's frame (the prototype)


@pytest.fixture(scope="module")
def board():
    return Board.open(Project.load(CHAIN / "power_chain.coppernicus.yml").board_path())


def limited_project():
    """The power chain with limits, so that something is judged (and fails): +3V3's loads
    within 0.4 %, GND's rise within 10 mV."""
    project = Project.load(CHAIN / "power_chain.coppernicus.yml")
    project.rail("+3V3").limits.max_drop_pct = 0.4
    project.ensure_rail("GND").limits.max_ground_rise_mv = 10.0
    return project


@pytest.fixture(scope="module")
def chain(board):
    return solve_project_tree(board, limited_project())


@pytest.fixture()
def chain_copy(tmp_path):
    shutil.copytree(CHAIN, tmp_path / "chain")
    return tmp_path / "chain" / "power_chain.coppernicus.yml"


def _vrm(tree, name):
    return next(v for v in tree.vrms if v.name == name)


def shown_from(tree, board, spec):
    """A copy of ``tree`` shown from ``spec``: the tree itself is left alone."""
    out = copy.deepcopy(tree)
    out.reference = tree_reference(board, out, spec)
    return out


# ---------------------------------------------------------------------- the engine

def test_each_vrm_has_its_output_across_its_own_pins(chain):
    """The prototype: U1 3.30000 V, U2 1.81453 V (V out minus its own ground, not its ground
    sense: 1.80000 V would be across the sense pins)."""
    assert _vrm(chain, "U1").v_out_across_v == pytest.approx(3.30000, abs=5e-6)
    assert _vrm(chain, "U2").v_out_across_v == pytest.approx(1.81453, abs=5e-6)
    assert _vrm(chain, "J1").v_out_across_v == pytest.approx(12.0, abs=1e-12)
    for v in chain.vrms:
        assert v.v_out_across_v == pytest.approx(v.v_out_v - (v.v_ground_v or 0.0), abs=1e-15)


def test_the_default_reference_is_the_sources_ground_pins(chain):
    ref = chain.reference
    assert ref.given is None and ref.offset_v == 0.0 and ref.spread_v == 0.0
    assert ref.pads == ["J1.2"] and "J1" in ref.label and "solve's 0 V" in ref.label


def test_with_the_reference_at_u1_s_ground_pins_u1_reads_3v3(chain, board):
    ref = tree_reference(board, chain, {"ground_of": "U1"})
    assert ref.label == "U1's ground pins" and ref.pads == ["U1.3"] and ref.given == {"ground_of": "U1"}
    assert ref.offset_v == pytest.approx(U1_GROUND_V, abs=1e-12)
    assert ref.offset_v == _vrm(chain, "U1").v_ground_v and ref.spread_v == 0.0   # KiPI's v_ground
    u1 = _vrm(chain, "U1")
    assert ref.shown(u1.v_out_v) == pytest.approx(u1.v_out_across_v, abs=1e-12)
    assert ref.shown(u1.v_out_v) == pytest.approx(3.3, abs=1e-9)
    assert ref.shown(u1.v_ground_v) == 0.0                     # exactly: no "-0.00"


def test_pads_take_exact_terminal_voltages_or_the_pad_centre(chain, board):
    u3 = tree_reference(board, chain, {"ground_of": "U3"})
    pad = tree_reference(board, chain, {"pads": ["U3.2"]})
    assert pad.offset_v == u3.offset_v and pad.pads == ["U3.2"]              # a terminal pad: exact
    fb1 = tree_reference(board, chain, {"pads": ["FB1.1"]})                  # not a terminal pad
    plus = next(n for n in chain.groups["+3V3"].net_split if n.net == "+3V3")
    assert fb1.offset_v == pytest.approx(plus.v_min_v, abs=1e-6)             # +3V3's far end
    both = tree_reference(board, chain, {"pads": ["U3.*", "U1.3"]})          # one global offset
    assert both.pads == ["U3.1", "U3.2", "U1.3"]
    volts = [_load(chain, "U3").v_supply_v, _load(chain, "U3").v_ground_v, U1_GROUND_V]
    assert both.offset_v == pytest.approx(np.mean(volts), abs=1e-9)
    assert both.spread_v == pytest.approx(max(volts) - min(volts), abs=1e-9)


def _load(tree, name):
    return next(ld for ld in tree.loads if ld.name == name)


def test_reference_parts_lists_the_parts_whose_ground_pins_are_solved(chain):
    assert reference_parts(chain) == ["J1", "U4", "U3", "U1", "U2"]


def _arrays(tree):
    return [a for res in tree.groups.values() for lf in res.layers
            for a in (lf.voltage, lf.current_density, lf.tri_power)]


def test_the_tree_is_never_shifted(chain, board):
    """Resolving references changes nothing in the tree: its voltages, v_ref, tables."""
    before = copy.deepcopy(chain)
    doc = tree_summary(chain)
    for spec in ({"ground_of": "U1"}, {"pads": ["FB1.1", "U3.*"]}, {"ground_of": "U2"}):
        tree_reference(board, chain, spec)
    assert all(np.array_equal(a, b, equal_nan=True) for a, b in zip(_arrays(before), _arrays(chain)))
    assert {k: r.v_ref for k, r in before.groups.items()} == {k: r.v_ref for k, r in chain.groups.items()}
    assert tree_summary(chain) == doc and chain.reference == before.reference


def without_reference(doc):
    """The document without the reference's own additions: its block and its assumption line."""
    doc = json.loads(json.dumps(doc))
    doc.pop("reference")
    doc["assumptions"] = [a for a in doc["assumptions"] if not a.startswith("Voltages are shown from 0 V")]
    return doc


def test_judged_keys_and_verdicts_are_byte_identical_with_and_without_a_reference(chain, board):
    plain = tree_summary(chain)
    assert plain["verdict"] == "fail" and plain["failed"] >= 2           # something is judged
    for spec in ({"ground_of": "U1"}, {"ground_of": "U3"}, {"pads": ["FB1.1"]}):
        shown = shown_from(chain, board, spec)
        doc = tree_summary(shown)
        assert json.dumps(without_reference(doc), sort_keys=True) == json.dumps(
            without_reference(plain), sort_keys=True)
        assert tree_verdicts(shown) == tree_verdicts(chain)
        assert doc["reference"]["given"] == spec and doc["reference"]["offset_v"] == shown.reference.offset_v
        [line] = [a for a in doc["assumptions"] if a.startswith("Voltages are shown from 0 V")]
        assert "the ground rise's too, is taken from the solve's 0 V" in line
    assert plain["reference"]["given"] is None
    assert not any(a.startswith("Voltages are shown from 0 V") for a in plain["assumptions"])


def test_the_cli_table_shows_absolute_voltages_from_the_reference(chain, board):
    doc = tree_summary(shown_from(chain, board, {"ground_of": "U1"}))
    lines = tree_text(doc)
    assert lines[0].startswith("Shown from 0 V at U1's ground pins (U1.3), +5.691 mV")
    u1 = next(line for line in lines if line.startswith("  U1 "))
    assert "3.30000V  3.30000V" in u1                                    # V out, V across
    u4 = next(line for line in lines if line.startswith("  U4 "))
    assert "3.29199V" in u4 and "8.63mV" in u4 and "3.28336V" in u4      # supply, ground; across kept
    assert "16.64 mV (0.50 %)" in u4 and u4.endswith("FAIL")             # the drop and verdict kept
    groups = {line.split()[0]: line for line in lines if line.startswith("  ") and " max " in line}
    assert "below 3.3 V," in groups["+3V3"]
    assert "max ground rise 38.470 mV above -0.00569069 V" in groups["GND"]       # the solve's 0 V, shown
    plain = tree_text(tree_summary(chain))
    assert plain[0] == "VRMs (sources and regulators)"                    # no header at the solve's 0 V
    assert "3.30569V  3.30000V" in next(line for line in plain if line.startswith("  U1 "))
    assert "below 3.30569 V" in next(line for line in plain if line.startswith("  +3V3 "))


# ---------------------------------------------------------------------- a negative rail

def _negative():
    b = SyntheticBoard("strip_gnd", two_layer_stackup())
    for ref, x in (("J1", 0.0), ("U1", 40.0)):
        b.square_pad(ref, "1", "VCC", x, 0.0, 1.5)
        b.square_pad(ref, "2", "GND", x, 5.0, 1.5)
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    b.track("GND", F, 0.5, (0.0, 5.0), (40.0, 5.0))
    return Board(b.snapshot()), Project(rails=[
        Rail("VCC", sources=[Source("J1", -3.3, ["1"], ground_pins=["2"], ground_net="GND")],
             loads=[Load("U1", 1.0, ["1"], ground_pins=["2"], ground_net="GND")])])


def test_a_negative_rail_strip_still_reads_as_a_ground_shift():
    board, project = _negative()
    tree = solve_project_tree(board, project)
    plain = tree_summary(tree)
    shown = shown_from(tree, board, {"ground_of": "U1"})
    assert shown.reference.offset_v < 0                                  # U1's ground below 0 V
    doc = tree_summary(shown)
    assert without_reference(doc) == without_reference(plain)
    gnd = next(line for line in tree_text(doc) if line.startswith("  GND "))
    assert "max ground shift" in gnd and f"below {-shown.reference.offset_v:g} V" in gnd
    p = probe_tree(shown, "F.Cu", 20.0, 5.0)
    assert p["group"] == "GND" and p["drop_word"] == "shift"
    assert p["drop_mv"] == probe_tree(tree, "F.Cu", 20.0, 5.0)["drop_mv"]
    assert p["voltage_from_reference_v"] == pytest.approx(           # both rounded to 7 digits
        p["voltage_v"] - shown.reference.offset_v, rel=1e-6)


# ---------------------------------------------------------------------- invalid references

def _excluded_ground():
    project = Project.load(CHAIN / "power_chain.coppernicus.yml")
    project.engine_options["excluded_nets"] = ["GND"]
    return project


def _isolated_pad_board():
    b = SyntheticBoard("isolated", two_layer_stackup())
    b.square_pad("J1", "1", "VCC", 0.0, 0.0, 1.5)
    b.square_pad("U1", "1", "VCC", 40.0, 0.0, 1.5)
    b.square_pad("R9", "1", "VCC", 0.0, 10.0, 1.5)                      # its copper reaches nothing
    b.track("VCC", F, 0.5, (0.0, 0.0), (40.0, 0.0))
    return Board(b.snapshot()), Project(rails=[Rail("VCC", sources=[Source("J1", 3.3, ["1"])],
                                                    loads=[Load("U1", 1.0, ["1"])])])


@pytest.mark.parametrize("spec, words", [
    ({"ground_of": "U1", "pads": ["U1.3"]}, "not both"),
    ({}, "give ground_of or pads"),
    ({"pads": ["GND"]}, "a net is not one potential"),
    ({"ground_of": "GND"}, "GND is a net, not a part"),
    ({"ground_of": "U9"}, "no part U9 on the board"),
    ({"ground_of": "FB1"}, "FB1 has no ground pins in this power tree"),
    ({"pads": ["U1.9"]}, "U1 has no pad '9' (pads of U1: 1, 2, 3)"),
    ({"pads": ["Q7.1"]}, "no part Q7"),
    ({"net": "GND"}, "unknown key net"),
])
def test_an_invalid_reference_says_what_to_give(chain, board, spec, words):
    with pytest.raises(EngineError) as e:
        tree_reference(board, chain, spec)
    assert words in str(e.value)


def test_a_reference_on_an_unsolved_net_or_disconnected_copper_is_refused(board):
    tree = solve_project_tree(board, _excluded_ground())
    with pytest.raises(EngineError, match="no ground net is solved"):
        tree_reference(board, tree, {"ground_of": "U3"})
    with pytest.raises(EngineError, match="U3.2 is on GND, which this power tree does not solve"):
        tree_reference(board, tree, {"pads": ["U3.2"]})
    assert tree_reference(board, tree, {"pads": ["U3.*"]}).pads == ["U3.1"]   # a glob skips it
    iso_board, project = _isolated_pad_board()
    iso = solve_project_tree(iso_board, project)
    with pytest.raises(EngineError, match="R9.1 is on no connected, solved copper"):
        tree_reference(iso_board, iso, {"pads": ["R9.1"]})


# ---------------------------------------------------------------------- the project file

def test_the_project_file_keeps_the_reference(tmp_path):
    project = Project.load(CHAIN / "power_chain.coppernicus.yml")
    assert project.tree_reference is None and "tree" not in project.to_dict()
    for given in ({"ground_of": "U1"}, {"pads": ["R12.2", "NT1.*"]}):
        project.tree_reference = reference_spec(given)
        path = project.save(tmp_path / "p.coppernicus.yml")
        assert yaml.safe_load(path.read_text())["tree"] == {"reference": given}
        assert Project.load(path).tree_reference == given


@pytest.mark.parametrize("block, words", [
    ({"reference": {"ground_of": "U1"}, "zero": 1}, "tree: unknown option zero"),
    ({"reference": {"part": "U1"}}, "unknown key part"),
    ({"reference": {"pads": ["GND"]}}, "a net is not one potential"),
    ({"reference": {"ground_of": "U1.3"}}, "ground_of is a part reference"),
    ({"reference": "U1"}, "must be a mapping"),
])
def test_an_invalid_tree_block_is_refused(block, words):
    data = yaml.safe_load((CHAIN / "power_chain.coppernicus.yml").read_text())
    with pytest.raises(ProjectError, match=re.escape(words)):
        Project.from_dict({**data, "tree": block})


def test_reference_from_text():
    assert reference_from_text("U1") == {"ground_of": "U1"}
    assert reference_from_text("R12.2,NT1.*") == {"pads": ["R12.2", "NT1.*"]}
    assert reference_from_text("U3.2") == {"pads": ["U3.2"]}
    assert reference_from_text("ground_of=U2") == {"ground_of": "U2"}
    assert reference_from_text("pads=U3.2") == {"pads": ["U3.2"]}
    with pytest.raises(ValueError, match="not REF.PAD"):
        reference_from_text("U3.2,GND")


# ---------------------------------------------------------------------- CLI, session, MCP

def test_cli_reference(chain_copy, tmp_path, capsys):
    out = str(tmp_path / "out")
    assert main(["solve", str(chain_copy), "--tree", "--reference", "U1", "--out", out,
                 "--probe", "B.Cu@97.5,1.0"]) == EXIT_OK
    text = capsys.readouterr().out
    assert "Shown from 0 V at U1's ground pins (U1.3)" in text
    assert re.search(r"\n  U1 .* 3\.30000V  3\.30000V", text)
    assert "V = 0.03847 V (0.03278 V from 0 V at U1's ground pins) · rise = 38.470 mV" in text
    assert main(["solve", str(chain_copy), "--tree", "--json", "--reference", "U1", "--out", out]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["reference"]["given"] == {"ground_of": "U1"}
    assert doc["reference"]["offset_v"] == pytest.approx(U1_GROUND_V, abs=1e-12)
    assert main(["solve", str(chain_copy), "--tree", "--json", "--out", out]) == EXIT_OK
    plain = json.loads(capsys.readouterr().out)
    doc.pop("summary_file"), plain.pop("summary_file")
    assert without_reference(doc) == without_reference(plain) and plain["reference"]["given"] is None


def test_cli_takes_the_project_s_reference_and_refuses_an_invalid_one(chain_copy, tmp_path, capsys):
    out = str(tmp_path / "out")
    project = Project.load(chain_copy)
    project.tree_reference = {"pads": ["U3.2"]}
    project.save(chain_copy)
    assert main(["solve", str(chain_copy), "--tree", "--json", "--out", out]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["reference"]["pads"] == ["U3.2"]
    assert main(["solve", str(chain_copy), "--tree", "--json", "--reference", "U1", "--out", out]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["reference"]["pads"] == ["U1.3"]   # --reference wins
    for argv, words in ((["--tree", "--reference", "GND"], "GND is a net, not a part"),
                        (["--tree", "--reference", "U3.2,GND"], "not REF.PAD"),
                        (["--reference", "U1"], "add --tree")):
        assert main(["solve", str(chain_copy), "--json", "--out", out, *argv]) == EXIT_ERROR
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["code"] == "invalid_input" and words in err["message"], err


def test_session_reference_is_not_saved_and_probe_reads_from_it(chain_copy, tmp_path):
    before = chain_copy.read_text()
    session = Session(out_dir=tmp_path / "out")
    doc = session.solve_tree(chain_copy, reference={"ground_of": "U1"})
    assert chain_copy.read_text() == before                              # not saved
    assert doc["reference"]["label"] == "U1's ground pins"
    u1 = next(v for v in doc["vrms"] if v["name"] == "U1")
    assert u1["v_out_v"] - doc["reference"]["offset_v"] == pytest.approx(u1["v_out_across_v"], abs=1e-12)
    p = session.probe("t1:GND", "B.Cu", 97.5, 1.0)
    assert p["drop_mv"] == pytest.approx(38.470, abs=5e-4) and p["drop_word"] == "rise"
    assert p["voltage_from_reference_v"] == pytest.approx(p["voltage_v"] - U1_GROUND_V, rel=1e-6)
    plain = session.solve_tree(chain_copy)
    assert plain["reference"]["given"] is None
    assert session.probe("t2:GND", "B.Cu", 97.5, 1.0)["voltage_from_reference_v"] == p["voltage_v"]
    with pytest.raises(ServiceError) as e:
        session.solve_tree(chain_copy, reference={"pads": ["GND"]})
    assert e.value.code == "invalid_input" and "not one potential" in e.value.message
    with pytest.raises(ServiceError) as e:
        session.solve_tree(chain_copy, reference={"ground_of": "FB1"})
    assert e.value.code == "invalid_input" and "FB1 has no ground pins" in e.value.message


def test_mcp_solve_tree_reference(chain_copy, tmp_path):
    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            ok = await c.call_tool("solve_tree", {"project_path": str(chain_copy),
                                                  "reference": {"ground_of": "U1"}})
            probe = await c.call_tool("probe", {"result_id": "t1:GND", "layer": "B.Cu", "x_mm": 97.5,
                                                "y_mm": 1.0})
            bad = await c.call_tool("solve_tree", {"project_path": str(chain_copy),
                                                   "reference": {"ground_of": "GND"}})
            return tools, ok, probe, bad
    tools, ok, probe, bad = anyio.run(scenario)
    assert "reference" in tools["solve_tree"].input_schema["properties"]
    doc = ok.structured_content
    assert doc["reference"]["given"] == {"ground_of": "U1"}
    assert probe.structured_content["voltage_from_reference_v"] == pytest.approx(
        probe.structured_content["voltage_v"] - doc["reference"]["offset_v"], rel=1e-6)
    assert bad.structured_content["error"]["code"] == "invalid_input"
    assert "is a net, not a part" in bad.structured_content["error"]["message"]


# ---------------------------------------------------------------------- the HTML report

def _meta(path):
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(re.search(r'<script type="application/json" id="meta">(.*?)</script>', text, re.S)
                      .group(1))


def test_the_report_moves_only_the_voltage_map(chain, board, tmp_path):
    from coppernicus.report import write_tree_report
    shown = shown_from(chain, board, {"ground_of": "U1"})
    plain = _meta(write_tree_report(chain, tree_summary(chain), tmp_path / "plain.html"))
    moved = _meta(write_tree_report(shown, tree_summary(shown), tmp_path / "moved.html"))
    off = shown.reference.offset_v
    for a, b in zip(plain["rails"], moved["rails"]):
        assert "v_offset_v" not in a and b["v_offset_v"] == off and b["reference"] == "U1's ground pins"
        for key in ("v_lo_v", "v_hi_v", "v_ref_v"):
            assert b[key] == pytest.approx(a[key] - off, abs=1e-15)
        for key in ("drop_max_mv", "drop_unit", "drop_word", "drop_sign", "worst", "loads", "verdict"):
            assert b[key] == a[key], key                                   # the drop map does not move
        assert any("shown from 0 V at U1's ground pins" in n for n in b["notes"])
    assert moved["tree"]["vrms"] == plain["tree"]["vrms"]                  # the tables: solve frame


def test_report_js_decides_words_and_percent_in_the_solve_frame():
    js = (Path(__file__).resolve().parents[1] / "src" / "coppernicus" / "report_assets"
          / "report.js").read_text(encoding="utf-8")
    assert "function vref(rail) { return rail.v_ref_v + (rail.v_offset_v || 0); }" in js
    assert not re.search(r"rail\.v_ref_v \?", js)              # no truthiness on the moved v_ref


# ---------------------------------------------------------------------- the app (offscreen)

def test_the_app_shows_the_tree_from_0_v_at_a_part_or_a_pad_with_no_re_solve(chain_copy):
    pytest.importorskip("PySide6")
    from test_app_window import run_in_window
    run_in_window(f'''
        assert window.open_project({str(chain_copy)!r})
        window.solve_tree()
        window.tree_worker.wait(120000)
        app.processEvents(); app.processEvents()
        tree = window.tree
        assert tree is not None and tree.reference.given is None
        panel = window.tree_panel
        def row(name):
            headers = [panel.vrms.horizontalHeaderItem(c).text() for c in range(panel.vrms.columnCount())]
            return dict(zip(headers, next(r for r in panel.vrm_rows() if r[0] == name)))
        assert row("U1")["V out"] == "3.30569" and row("U1")["Across (V)"] == "3.30000", row("U1")
        voltages = [lf.voltage.copy() for r in tree.groups.values() for lf in r.layers]

        i = panel.reference_box.findData("U1")                       # 0 V at U1's ground pins
        assert i > 0, [panel.reference_box.itemText(k) for k in range(panel.reference_box.count())]
        panel.reference_box.setCurrentIndex(i)
        panel.reference_box.activated.emit(i)
        app.processEvents()
        assert window.tree is tree and tree.reference.label == "U1's ground pins"   # no re-solve
        assert row("U1")["V out"] == "3.30000" and row("U1")["Across (V)"] == "3.30000", row("U1")
        u4 = next(r for r in panel.load_rows() if r[0] == "U4")
        assert u4[4] == "3.29199" and u4[5] == "8.63" and u4[6] == "3.28336" and u4[7] == "16.64", u4
        assert "U1's ground pins" in panel.reference_note.text()

        window.group_box.setCurrentIndex(window.group_box.findData("GND"))
        app.processEvents()
        window.field_box.setCurrentIndex(window.field_box.findData("voltage"))
        app.processEvents()
        msg = window.view.probe_at(97.5, 1.0)
        assert "V = 0.03278 V from 0 V at U1's ground pins" in msg and "rise = 38.470 mV" in msg, msg
        assert "from 0 V at U1's ground pins" in window.view.colorbar._title, window.view.colorbar._title
        lead = window.view.hover_at(97.5, 1.0)
        assert lead.startswith("V = 0.03278 V"), lead

        panel.reference_pad.setText("GND")                           # refused, nothing changes
        panel.reference_pad.returnPressed.emit()
        app.processEvents()
        assert tree.reference.label == "U1's ground pins"
        assert "not one potential" in panel.reference_note.text(), panel.reference_note.text()

        panel.reference_pick.setChecked(True)                        # Pick: U3's ground pad
        assert window.view.click_mode == "pick"
        window.pick_pad(97.5, 1.0)
        app.processEvents()
        assert tree.reference.pads == ["U3.2"] and not panel.picking() and window.tree is tree
        assert "V = 0.00000 V" in window.view.probe_at(97.5, 1.0)

        panel.reference_box.setCurrentIndex(0)                       # back to the solve's 0 V
        panel.reference_box.activated.emit(0)
        app.processEvents()
        assert tree.reference.given is None and row("U1")["V out"] == "3.30569"
        msg = window.view.probe_at(97.5, 1.0)
        assert "V = 0.03847 V, rise = 38.470 mV" in msg, msg
        after = [lf.voltage for r in tree.groups.values() for lf in r.layers]
        import numpy as np
        assert all(np.array_equal(a, b, equal_nan=True) for a, b in zip(voltages, after))
    ''')
