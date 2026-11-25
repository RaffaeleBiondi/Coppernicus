"""The Tolerance tab (Task 11b): the editors' text <-> project mapping, where an edit is held
(a project rail, a rail from the KiCad fields), the stack-up built from the results the app
holds (a stale one is missing), the misspelt keys of the loaded file (pure logic, no window);
and, in a child process, what only a window shows. No solve runs here: the results are made by
hand; the solves are in tools/gui_check.py (run_tolerance)."""

import dataclasses

import pytest

pytest.importorskip("PySide6")

from test_tolerance import _comp_project, _comp_tree, _impedance, _noise_result  # noqa: E402

from coppernicus import board_fields as BF  # noqa: E402
from coppernicus.project import (  # noqa: E402
    Load,
    Probe,
    Project,
    ProjectError,
    Rail,
    Source,
    ToleranceOptions,
)

# ---------------------------------------------------------------------- a pair in a cell


def test_a_cell_reads_a_pair_as_the_file_writes_it():
    from coppernicus.app.tolerance_panel import pair_text, parse_pair
    assert parse_pair("", "tol_pct") is None and parse_pair("  ", "tol_pct") is None
    assert parse_pair("5", "tol_pct") == (-5.0, 5.0) and parse_pair("±5", "tol_pct") == (-5.0, 5.0)
    assert parse_pair("0", "ripple_mv") == (0.0, 0.0)                    # 0 is a value
    for text in ("-3, 2", "[-3, 2]", "-3 / +2", "-3; 2", "[-3,2]"):
        assert parse_pair(text, "tol_pct") == (-3.0, 2.0), text
    assert parse_pair("3,3", "tol_pct") == (-3.3, 3.3)                    # a decimal comma
    assert parse_pair("1e1", "tol_mv") == (-10.0, 10.0)
    for pair in ((-5.0, 5.0), (-3.0, 2.0), (0.0, 0.0), (-0.123456789, 2.5), (0.0, 3.0)):
        assert parse_pair(pair_text(pair), "tol_pct") == pair, pair
    assert pair_text(None) == "" and pair_text((-5.0, 5.0)) == "5" and pair_text((-3.0, 2.0)) == "-3, 2"


@pytest.mark.parametrize("text, words", [("abc", "not a number"), ("-5", "cannot be negative"),
                                         ("3, 2", "below"), ("-3, -2", "above"),
                                         ("1, 2, 3", r"\[below, above\]"), ("nan", "not a number"),
                                         ("inf", "not a number")])
def test_a_cell_the_file_would_refuse_is_refused_with_its_reason(text, words):
    from coppernicus.app.tolerance_panel import parse_pair
    with pytest.raises(ProjectError, match=words) as e:
        parse_pair(text, "load U2: tol_pct")
    assert "tol_pct" in str(e.value)


def test_an_edit_builds_a_new_terminal_through_its_own_checks():
    """dataclasses.replace runs __post_init__: a _pct beside its _mv is refused, the terminal
    given is never changed, and every other field stays."""
    from coppernicus.app.tolerance_panel import with_tolerance
    ld = Load("U2", 5.0, ["1"], ground_net="GND", tol_pct=(-6.0, 6.0), ripple_mv=(-2.0, 2.0))
    q = with_tolerance(ld, "dc_tol_mv", "-20, 10")
    assert q.dc_tol_mv == (-20.0, 10.0) and q.tol_pct == (-6.0, 6.0) and q.ground_net == "GND"
    assert ld.dc_tol_mv is None                                           # a copy
    assert with_tolerance(ld, "ripple_mv", "").ripple_mv is None          # empty: not entered
    with pytest.raises(ProjectError, match="both set"):
        with_tolerance(ld, "tol_mv", "50")
    src = with_tolerance(Source("L1", 1.8, name="U1"), "accuracy_pct", "-1, 2")
    assert src.accuracy_pct == (-1.0, 2.0) and src.name == "U1"
    with pytest.raises(ValueError, match="no tolerance key"):
        with_tolerance(ld, "current", "1")


# ---------------------------------------------------------------------- staleness


def test_a_tolerance_edit_never_flags_an_impedance_or_a_noise_result():
    """No AC or noise solve reads a tolerance, an entered part, an accuracy or the tolerance
    options: typing one must not make the held results stale (the tab would then count the
    ripple it needs as missing). Any other edit still does."""
    from coppernicus.app.ac_panel import _project_key as ac_key
    from coppernicus.app.noise_panel import project_key as noise_key
    p = _comp_project(aggressors=[("A1", True)])
    keys = ac_key(p), noise_key(p)
    rail = p.rails[0]
    rail.loads[0] = dataclasses.replace(rail.loads[0], tol_pct=5, dc_tol_mv=[-20, 10], ripple_mv=2,
                                        transient_mv=3)
    rail.sources[0] = dataclasses.replace(rail.sources[0], accuracy_pct=1)
    p.tolerance = ToleranceOptions(ripple_sum="rss", bw_limit_20mhz=True)
    assert (ac_key(p), noise_key(p)) == keys
    rail.loads[0] = dataclasses.replace(rail.loads[0], current=4.0)
    assert ac_key(p) != keys[0] and noise_key(p) != keys[1]


# ---------------------------------------------------------------------- the stack-up without a tree


def test_without_a_power_tree_the_dc_part_is_missing_with_why_never_a_pass():
    from coppernicus.tolerance import compose
    p = _comp_project(load={"tol_pct": 50, "transient_mv": 1, "ripple_mv": 1}, source={"accuracy_pct": 1})
    note = "the power tree is not solved: solve it (Ctrl+T)"
    r = compose(p, None, (), (), {"U2": "P1"}, tree_note=note)
    (row,) = r.rows
    assert row.parts["dc"] is None and row.hints["dc"] == note
    assert row.nominal_v == 1.8 and row.source == "U1"                    # the rail's own source
    assert row.parts["accuracy"] is not None and row.verdict == "not_judged" and r.verdict == "not_judged"
    assert row.checks["total"].missing == ["dc"] and note in row.checks["total"].reason
    p.rails[0].loads[0] = dataclasses.replace(p.rails[0].loads[0], tol_pct=(-0.5, 0.5))
    r = compose(p, None, (), (), {"U2": "P1"}, tree_note=note)
    assert r.verdict == "fail"                                            # its known parts alone are over
    with_tree = compose(p, _comp_tree(), (), (), {"U2": "P1"})
    assert with_tree.rows[0].parts["dc"] is not None                      # a tree held: as before


# ---------------------------------------------------------------------- a rail from the KiCad fields

FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"}, "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}
PADS = {"J1": {"1": "VCC"}, "U1": {"1": "VCC"}}


def _merge(project):
    return BF.merge(project, FIELDS, PADS)


def test_the_parts_lost_to_a_kicad_fields_rail_are_said_as_the_cli_says_them():
    """tolerance.fields_losses, shared with coppernicus tolerance (test_cli_tolerance has the
    CLI's side): J5 and U9 are not in the fields' VCC."""
    from coppernicus.tolerance import fields_losses
    own = Project(rails=[Rail("VCC", sources=[Source("J5", 3.3, accuracy_pct=1)],
                              loads=[Load("U1", 1.0, tol_pct=5), Load("U9", 1.0, tol_pct=4, ripple_mv=1)])])
    merged = _merge(own)
    warnings, dropped, lost = fields_losses(own, merged.project, merged.from_fields)
    assert len(warnings) == 1 and "J5, U9" in warnings[0] and "override_board_fields" in warnings[0]
    assert [(e["item"], e["limit"]) for e in dropped] == [("U9", "tol_pct")]
    assert lost["VCC"].startswith("J5's accuracy_pct in the project file was lost: rail VCC comes from")
    assert fields_losses(own, own, []) == ([], [], {})


def test_a_project_rail_is_edited_in_place():
    from coppernicus.app.tolerance_panel import edit_rows, set_value
    own = _comp_project()
    merged = BF.Merged(own)
    rows = edit_rows(own, merged)
    assert [(r.kind, r.net, r.index, r.term.label, r.read_only) for r in rows] == \
        [("source", "VOUT", 0, "U1", ""), ("load", "VOUT", 0, "U2", "")]
    before = own.rails[0].loads[0]
    assert set_value(own, merged, "load", "VOUT", 0, "tol_pct", "5") == ""
    after = own.rails[0].loads[0]
    assert after is not before and after.tol_pct == (-5.0, 5.0) and after.pins == ["1"]
    set_value(own, merged, "source", "VOUT", 0, "accuracy_mv", "-10, 20")
    assert own.rails[0].sources[0].accuracy_mv == (-10.0, 20.0) and own.rails[0].sources[0].kind == "smps"


def test_a_kicad_fields_rail_is_held_by_a_mirror_that_raises_no_warning():
    """No KiCad field holds a tolerance yet, so it is editable on a rail from the fields: held
    on the project's own rail of the net, a copy of the fields' parts (the merge carries it by
    ref), so the merge sees no difference to warn about."""
    from coppernicus.app.tolerance_panel import edit_rows, set_value
    own = Project(rails=[Rail("VCC")])                          # limits only, as the app writes them
    own.rails[0].limits.max_via_current = 2.0
    merged = _merge(own)
    rows = edit_rows(own, merged)
    assert [(r.kind, r.term.ref, r.read_only) for r in rows] == [("source", "J1", ""), ("load", "U1", "")]
    note = set_value(own, merged, "load", "VCC", 0, "tol_pct", "5")
    assert "KiCad footprint fields" in note and "project file" in note
    rail = own.rail("VCC")
    assert [s.ref for s in rail.sources] == ["J1"] and [ld.ref for ld in rail.loads] == ["U1"]
    assert rail.loads[0].tol_pct == (-5.0, 5.0) and rail.limits.max_via_current == 2.0
    again = _merge(own)
    assert again.origins["VCC"].source == "fields" and again.warnings == []
    assert not again.origins["VCC"].warnings
    assert again.project.rail("VCC").loads[0].tol_pct == (-5.0, 5.0)
    assert any("tol_pct of U1 from the project file" in n for n in again.notes())
    set_value(own, again, "source", "VCC", 0, "accuracy_pct", "1")     # now held: no second copy
    assert len(own.rail("VCC").sources) == 1 and own.rail("VCC").sources[0].accuracy_pct == (-1.0, 1.0)
    fresh = Project()                                           # no rail of the net at all
    note = set_value(fresh, _merge(fresh), "source", "VCC", 0, "accuracy_mv", "20")
    assert note and fresh.rail("VCC").sources[0].accuracy_mv == (-20.0, 20.0)
    assert not _merge(fresh).warnings


def test_a_fields_rail_edit_goes_to_the_project_part_of_its_ref_or_is_read_only_without_one():
    from coppernicus.app.tolerance_panel import edit_rows, set_value
    # The project's own VCC differs from the fields (U1 at 2 A, an extra U9): ignored, warned.
    own = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3)], loads=[Load("U9", 1.0), Load("U1", 2.0)])])
    merged = _merge(own)
    set_value(own, merged, "load", "VCC", 0, "ripple_mv", "3")            # the fields' U1
    assert own.rail("VCC").loads[1].ripple_mv == (-3.0, 3.0) and own.rail("VCC").loads[1].current == 2.0
    assert _merge(own).project.rail("VCC").loads[0].ripple_mv == (-3.0, 3.0)
    own = Project(rails=[Rail("VCC", sources=[Source("J5", 3.3)], loads=[Load("U1", 1.0)])])
    rows = edit_rows(own, _merge(own))
    (src,) = [r for r in rows if r.kind == "source"]
    assert "J1" in src.read_only and "Override in project" in src.read_only, src.read_only
    with pytest.raises(ValueError, match="Override in project"):
        set_value(own, _merge(own), "source", "VCC", 0, "accuracy_pct", "1")
    assert own.rail("VCC").sources == [Source("J5", 3.3)]


def test_a_refused_edit_writes_nothing_not_even_the_mirror():
    from coppernicus.app.tolerance_panel import set_value
    own = Project()
    with pytest.raises(ProjectError, match="below"):
        set_value(own, _merge(own), "load", "VCC", 0, "tol_pct", "3, 2")
    assert own.rails == []
    p = _comp_project(load={"tol_pct": 5})
    with pytest.raises(ProjectError, match="both set"):
        set_value(p, BF.Merged(p), "load", "VOUT", 0, "tol_mv", "50")
    assert p.rails[0].loads[0].tol_mv is None and p.rails[0].loads[0].tol_pct == (-5.0, 5.0)


# ---------------------------------------------------------------------- misspelt keys of the loaded file


def test_a_misspelt_key_is_not_judged_until_the_key_it_meant_is_set(tmp_path):
    from coppernicus.app.tolerance_panel import live_misspelt, read_misspelt
    path = tmp_path / "t.coppernicus.yml"
    path.write_text("rails:\n- net: VOUT\n  sources:\n  - {ref: L1, name: U1, voltage: 1.8, acuracy_pct: 1}\n"
                    "  loads:\n  - {ref: U2, current: 5, tol_pc: 5, ripple: 2}\n", encoding="utf-8")
    found = read_misspelt(path)
    assert [e["limit"] for _w, e in found] == ["acuracy_pct", "tol_pc", "ripple"]
    assert all(w.startswith(("Source U1 on VOUT", "Load U2 on VOUT")) for w, _e in found)
    p = Project.load(path)
    assert [e["limit"] for _w, e in live_misspelt(found, p)] == ["acuracy_pct", "tol_pc", "ripple"]
    rail = p.rail("VOUT")
    rail.loads[0] = dataclasses.replace(rail.loads[0], tol_mv=50)          # tol_pc meant tol_pct: either unit
    rail.sources[0] = dataclasses.replace(rail.sources[0], accuracy_pct=1)
    assert [e["limit"] for _w, e in live_misspelt(found, p)] == ["ripple"]
    assert read_misspelt(tmp_path / "missing.yml") == []


# ---------------------------------------------------------------------- the results the app holds


def _held(project, tree=True, ac=None, noise=None, misspelt=(), ac_now="now", noise_now="now"):
    from coppernicus.app.tolerance_panel import compose_held
    return compose_held({"U2": "P1"}, project, BF.Merged(project), _comp_tree() if tree else None,
                        ac or {}, noise or {}, ac_now, noise_now, misspelt)


COMPLETE = dict(load={"tol_pct": 5, "transient_mv": 20}, source={"accuracy_pct": 1},
                aggressors=[("A1", True)])


def test_held_results_give_a_pass_only_while_none_is_stale():
    p = _comp_project(**COMPLETE)
    fresh = {"A1": (_noise_result("A1", (0.002, 0.003, 0.001, 0.002)), "now")}
    r = _held(p, noise=fresh)
    assert r.verdict == "pass" and r.rows[0].parts["ripple"].source == "noise of A1 at P1"
    stale = {"A1": (fresh["A1"][0], "before")}
    r = _held(p, noise=stale)
    hint = r.rows[0].hints["ripple"]
    assert r.verdict == "not_judged" and r.rows[0].parts["ripple"] is None
    assert "A1 not solved" in hint and "the setup changed since its solve" in hint, hint
    r = _held(p)
    assert "A1 not solved (not solved yet: Ripple & noise tab" in r.rows[0].hints["ripple"]
    r = _held(p, tree=False, noise=fresh)
    assert r.verdict == "not_judged" and "Ctrl+T" in r.rows[0].hints["dc"]


def test_the_transient_estimate_needs_an_impedance_solved_with_it():
    """The Impedance tab solves without KiPI's transient estimate: say so, never invent one
    (only an entered transient_mv is judged)."""
    p = _comp_project(load={"tol_pct": 5}, source={"accuracy_pct": 1})
    p.probes = [Probe("P1", "U2", "VOUT", ["1"], step_a=1.0, slew_a_per_us=1.0)]
    p.ac.bandwidth_hz = {"U1": 1e5}
    without = _impedance(estimate=None)
    r = _held(p, ac={"VOUT": (without, "now")})
    hint = r.rows[0].hints["transient"]
    assert "estimate not available: solve the impedance of VOUT with the transient" in hint, hint
    assert r.rows[0].transient_estimate_v is None
    r = _held(p, ac={"VOUT": (_impedance(), "before")})                   # stale: not shown
    assert r.rows[0].transient_estimate_v is None and "the setup changed" in r.rows[0].hints["transient"]
    r = _held(p, ac={"VOUT": (_impedance(), "now")})
    assert r.rows[0].transient_estimate_v is not None and "KiPI's estimate" in r.rows[0].hints["transient"]
    r = _held(p)
    assert "the impedance at P1 is not solved" in r.rows[0].hints["transient"]


def test_held_results_carry_the_misspelt_and_lost_entries():
    p = _comp_project(**COMPLETE)
    fresh = {"A1": (_noise_result("A1", (0.002, 0.003, 0.001, 0.002)), "now")}
    typo = ("Load U2 on VOUT: unknown key 'dc_tol_pc' ...",
            {"item": "U2", "net": "VOUT", "limit": "dc_tol_pc", "reason": "unknown key 'dc_tol_pc'"})
    r = _held(p, noise=fresh, misspelt=[typo])
    assert r.verdict == "not_judged" and typo[0] in r.warnings and r.not_judged[-1]["limit"] == "dc_tol_pc"
    p.rails[0].loads[0] = dataclasses.replace(p.rails[0].loads[0], dc_tol_mv=90)
    r = _held(p, noise=fresh, misspelt=[typo])                            # dc_tol_mv set: what it meant
    assert r.verdict == "pass" and typo[0] not in r.warnings


def test_the_next_solve_a_row_needs_is_offered_in_order():
    from coppernicus.app.tolerance_panel import needed_solve
    p = _comp_project(**COMPLETE)
    p.probes = [Probe("P1", "U2", "VOUT", ["1"], step_a=1.0, slew_a_per_us=1.0)]
    p.ac.bandwidth_hz = {"U1": 1e5}
    r = _held(p, tree=False)
    assert needed_solve(r.rows[0], p, has_tree=False) == ("tree", "", "Solve power tree")
    r = _held(p)
    assert needed_solve(r.rows[0], p, has_tree=True) == ("noise", "A1", "Solve noise of A1")
    fresh = {"A1": (_noise_result("A1", (0.002, 0.003, 0.001, 0.002)), "now")}
    r = _held(p, noise=fresh)
    assert needed_solve(r.rows[0], p, has_tree=True) == ("ac", "VOUT", "Solve impedance of VOUT + transient")
    r = _held(p, noise=fresh, ac={"VOUT": (_impedance(), "now")})
    assert needed_solve(r.rows[0], p, has_tree=True) is None
    p.ac.bandwidth_hz = {}                                                # no estimate possible: nothing
    r = _held(p, noise=fresh)
    assert needed_solve(r.rows[0], p, has_tree=True) is None


def test_part_and_check_cells_name_the_origin_and_never_a_clean_verdict_on_a_missing_part():
    from coppernicus.app.tolerance_panel import check_cell, part_cell, result_rows
    p = _comp_project(load={"tol_pct": 5, "transient_mv": 20, "ripple_mv": [-1, 2]},
                      source={"accuracy_pct": 1}, aggressors=[("A1", True)])
    fresh = {"A1": (_noise_result("A1", (0.002, 0.003, 0.001, 0.002)), "now")}
    r = _held(p, noise=fresh)
    row = r.rows[0]
    assert part_cell(row, "dc")[0].endswith("mV · tree")
    assert part_cell(row, "transient")[0].endswith("· entered")
    assert part_cell(row, "accuracy")[0].endswith("· accuracy_pct of U1")
    assert part_cell(row, "ripple")[0].endswith("· aggressor A1 + ripple_mv")
    text, tip = check_cell(row.checks["total"])
    assert text.startswith("PASS") and "margin" in text and "tol_pct" in tip
    r = _held(p)                                                          # A1 not solved: ripple missing
    row = r.rows[0]
    text, tip = part_cell(row, "ripple")
    assert text.startswith("missing") and "at least 1 / 2 mV" in text and "A1 not solved" in tip
    text, _tip = check_cell(row.checks["total"])
    assert text.startswith("NOT JUDGED") and "parts missing" in text
    (cells,) = result_rows(r)
    assert cells[-2] == "NOT JUDGED" and "ripple" in cells[-1]
