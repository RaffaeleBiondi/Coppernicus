"""The Tolerance tab (Task 11b): the editors' text <-> project mapping, where an edit is held
(a project rail, a rail from the KiCad fields), the stack-up built from the results the app
holds (a stale one is missing), the misspelt keys of the loaded file (pure logic, no window);
and, in a child process, what only a window shows. No solve runs here: the results are made by
hand; the solves are in tools/gui_check.py (run_tolerance)."""

import dataclasses
import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


def test_a_copy_of_the_fields_parts_flags_no_result_whatever_the_order_of_the_rails():
    """The merge lists the project's rails first, then the fields-only ones by net: a copy
    holding a tolerance on VCC moves it ahead of AAA. Nothing solved changed, so neither the
    Impedance nor the Ripple & noise key may change."""
    from coppernicus.app.ac_panel import _project_key as ac_key
    from coppernicus.app.noise_panel import project_key as noise_key
    from coppernicus.app.tolerance_panel import set_value
    fields = {**FIELDS, "J2": {"PI_RAIL": "AAA", "PI_SOURCE": "5V"},
              "U3": {"PI_RAIL": "AAA", "PI_LOAD": "2A"}}
    pads = {**PADS, "J2": {"1": "AAA"}, "U3": {"1": "AAA"}}
    own = Project()
    merged = BF.merge(own, fields, pads)
    assert [r.net for r in merged.project.rails] == ["AAA", "VCC"]
    keys = ac_key(merged.project), noise_key(merged.project)
    set_value(own, merged, "load", "VCC", 0, "tol_pct", "5")
    again = BF.merge(own, fields, pads)
    assert [r.net for r in again.project.rails] == ["VCC", "AAA"] and not again.warnings
    assert (ac_key(again.project), noise_key(again.project)) == keys


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


HELD_FIELDS = {"J1": {**FIELDS["J1"], "PI_ACC": "1%"}, "U1": {**FIELDS["U1"], "PI_AC_TOL": "20mV"}}
FREE_LOAD_KEYS = ("tol_pct", "tol_mv", "dc_tol_pct", "dc_tol_mv", "ripple_mv", "transient_mv")


def test_a_value_a_kicad_field_holds_is_read_only_cell_by_cell():
    """KiCad fields always win: PI_ACC on J1 and PI_AC_TOL on U1 hold both units of their
    family, read-only, naming the field and the part and how to change it; U1's other cells,
    which no field holds, stay editable (a row partly held). set_value refuses a held key,
    writing nothing (not even the copy of the fields' parts)."""
    from coppernicus.app.tolerance_panel import edit_rows, set_value
    own = Project()
    merged = BF.merge(own, HELD_FIELDS, PADS)
    src, load = edit_rows(own, merged)
    assert src.read_only == load.read_only == ""
    assert set(src.held) == {"accuracy_pct", "accuracy_mv"} and set(load.held) == {"ac_tol_pct", "ac_tol_mv"}
    assert src.why("accuracy_mv") == ('from KiCad field PI_ACC "1%" on J1; change it in KiCad, or set '
                                      "override_board_fields: true on rail VCC to use the project's")
    assert load.why("ac_tol_pct").startswith('from KiCad field PI_AC_TOL "20mV" on U1; change it in KiCad')
    assert [load.why(k) for k in FREE_LOAD_KEYS] == [""] * len(FREE_LOAD_KEYS)
    for kind, key, text in (("source", "accuracy_pct", "2"), ("source", "accuracy_mv", ""),
                            ("load", "ac_tol_mv", "30"), ("load", "ac_tol_pct", "1")):
        with pytest.raises(ValueError, match=r"^from KiCad field PI_A\w+ \"\w+%?\" on (J1|U1); change it"):
            set_value(own, merged, kind, "VCC", 0, key, text)
    assert own.rails == []


def test_a_copy_of_the_fields_parts_keeps_what_a_field_holds_as_merged():
    """An edit of a free cell on a rail from the fields writes a copy of the fields' parts as
    merged, the values their fields give included (as Use KiCad fields and Override → start from
    the KiCad values copy them), so Override in project later keeps them; the same fields merge
    with no warning, and a field changed in KiCad still wins, with its warning."""
    from coppernicus.app.tolerance_panel import set_value
    own = Project()
    note = set_value(own, BF.merge(own, HELD_FIELDS, PADS), "load", "VCC", 0, "tol_pct", "5")
    assert "no KiCad field gives U1's tol_pct" in note
    rail = own.rail("VCC")
    assert (rail.loads[0].tol_pct, rail.loads[0].ac_tol_mv) == ((-5.0, 5.0), (-20.0, 20.0))
    assert rail.sources[0].accuracy_pct == (-1.0, 1.0)
    again = BF.merge(own, HELD_FIELDS, PADS)
    u1, j1 = again.project.rail("VCC").loads[0], again.project.rail("VCC").sources[0]
    assert (u1.tol_pct, u1.ac_tol_mv, j1.accuracy_pct) == ((-5.0, 5.0), (-20.0, 20.0), (-1.0, 1.0))
    assert not again.warnings
    changed = BF.merge(own, {**HELD_FIELDS, "J1": {**HELD_FIELDS["J1"], "PI_ACC": "2%"}}, PADS)
    [w] = changed.warnings
    assert 'KiCad field PI_ACC "2%" on J1' in w
    assert changed.project.rail("VCC").sources[0].accuracy_pct == (-2.0, 2.0)


def test_library_fields_hold_a_project_rails_cells_unless_the_rail_overrides_them():
    """A part's PI_ACC without PI_RAIL reaches the project file's rail by part: held there too.
    With override_board_fields the project's value is used, and edits; a per-rail PI_TOL holds
    only the rail it is given for."""
    from coppernicus.app.tolerance_panel import edit_rows, set_value
    own = Project(rails=[Rail("VCC", sources=[Source("J1", 3.3, accuracy_pct=2)], loads=[Load("U1", 1.0)])])
    library = {"J1": {"PI_ACC": "1%"}}
    merged = BF.merge(own, library)
    assert merged.origins["VCC"].source == "project"
    src, load = edit_rows(own, merged)
    assert src.term.accuracy_pct == (-1.0, 1.0) and 'PI_ACC "1%" on J1' in src.why("accuracy_pct")
    assert not load.held
    with pytest.raises(ValueError, match="PI_ACC"):
        set_value(own, merged, "source", "VCC", 0, "accuracy_pct", "3")
    assert own.rails[0].sources[0].accuracy_pct == (-2.0, 2.0)
    own.rails[0].override_board_fields = True
    merged = BF.merge(own, library)
    src, _load = edit_rows(own, merged)
    assert not src.held and src.why("accuracy_pct") == "" and src.term.accuracy_pct == (-2.0, 2.0)
    set_value(own, merged, "source", "VCC", 0, "accuracy_pct", "3")
    assert own.rails[0].sources[0].accuracy_pct == (-3.0, 3.0)
    dual = {"J1": FIELDS["J1"], "U1": {"PI_RAIL": "VCC;VIO", "PI_LOAD": "1A;50mA", "PI_TOL": "5%;"},
            "J2": {"PI_RAIL": "VIO", "PI_SOURCE": "1.8V"}}
    rows = {(r.net, r.term.ref): r for r in edit_rows(Project(), BF.merge(Project(), dual))}
    assert set(rows["VCC", "U1"].held) == {"tol_pct", "tol_mv"} and not rows["VIO", "U1"].held


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


# ---------------------------------------------------------------------- in a window (child process)

TESTS = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(TESTS, "..", "examples", "tolerance", "tolerance.coppernicus.yml")

PRELUDE = f'''
import sys
sys.path.insert(0, {TESTS!r})
import dataclasses
from PySide6 import QtCore, QtWidgets
from coppernicus.app.main_window import MainWindow
from coppernicus.app.ac_panel import _project_key as ac_key
from coppernicus.app.noise_panel import project_key as noise_key
from test_tolerance import _comp_tree, _impedance, _noise_result

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MainWindow()
window.dirty = False
panel = window.tolerance_panel


def held(tree=True, noise=True):
    """A power tree holding U2 (10 mV below at worst) and U1's noise at LOAD, made by hand,
    as the window holds them after the solves."""
    window.tree = _comp_tree() if tree else None
    if noise:
        window.noise_panel.results["U1"] = (_noise_result("U1", (0.008, 0.011, 0.004, 0.006), probe="LOAD"),
                                            noise_key(window.merged.project))
    panel.refresh()


def cells(r=0):
    return [panel.table.item(r, c).text() for c in range(panel.table.columnCount())]
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_tolerance_tab_follows_ripple_and_noise_and_opening_changes_nothing():
    run_in_window(f'''
        from coppernicus.app import shown_label
        tabs = [shown_label(window.results_tabs.tabText(i)) for i in range(window.results_tabs.count())]
        assert tabs[tabs.index("Ripple & noise") + 1] == "Tolerance", tabs
        assert window.results_tabs.widget(tabs.index("Tolerance")) is panel
        window.show()
        app.processEvents()
        window.open_project({EXAMPLE!r})
        window.results_tabs.setCurrentWidget(panel)
        app.processEvents()
        before = window.project.to_dict()
        row = [panel.loads.item(0, c).text() for c in range(panel.loads.columnCount())]
        assert row == ["U2", "VOUT", "6", "", "3", "", "", "50", "", "30"], row
        assert [panel.sources.item(i, 0).text() for i in range(panel.sources.rowCount())] == ["J1", "U1"]
        assert panel.sources.item(1, 2).text() == "1" and panel.sources.item(0, 2).text() == ""
        assert panel.ripple_sum.currentText() == "linear" and not panel.bw_limit.isChecked()
        for c in range(2, panel.loads.columnCount()):
            item = panel.loads.item(0, c)
            item.setText(item.text())                         # committed unchanged: no edit
        assert window.dirty is False and window.project.to_dict() == before
        assert "NOT JUDGED" in panel.header.text(), panel.header.text()
        assert cells()[3].startswith("missing") and "Ctrl+T" in panel.table.item(0, 3).toolTip()
        with_tab = window.minimumSizeHint()                  # the laptop fit: the tab grows nothing
        window.results_tabs.removeTab(window.results_tabs.indexOf(panel))
        app.processEvents()
        without = window.minimumSizeHint()
        assert with_tab.height() <= without.height() and with_tab.width() <= without.width(), (
            with_tab, without)
    ''')


def test_an_edit_writes_the_project_merges_again_and_drops_no_solve():
    run_in_window(f'''
        window.open_project({EXAMPLE!r})
        app.processEvents()
        held()
        assert panel.result.verdict == "pass", panel.header.text()
        assert cells()[-2] == "PASS" and cells()[3].endswith("mV · tree") and "aggressor U1" in cells()[5]
        tree, load = window.tree, window.project.rail("VOUT").loads[0]
        panel.set_cell("load", "U2", "tol_pct", "3")
        new = window.project.rail("VOUT").loads[0]
        assert new is not load and new.tol_pct == (-3.0, 3.0) and new.dc_tol_pct == (-3.0, 3.0)
        assert window.merged.project.rail("VOUT").loads[0].tol_pct == (-3.0, 3.0) and window.dirty
        assert window.tree is tree and not window.noise_panel.stale, "a tolerance edit drops no solve"
        assert panel.result.verdict == "fail" and cells()[-2] == "FAIL", cells()
        panel.set_cell("load", "U2", "tol_mv", "50")          # beside tol_pct: refused, kept
        assert window.project.rail("VOUT").loads[0].tol_mv is None
        assert "Not applied" in panel.status.text() and "both set" in panel.status.text()
        assert panel.loads.item(0, 3).text() == ""
        panel.set_cell("load", "U2", "ripple_mv", "3, 2")     # below > 0: refused
        assert window.project.rail("VOUT").loads[0].ripple_mv is None and panel.loads.item(0, 8).text() == ""
        panel.set_cell("source", "U1", "accuracy_pct", "")    # cleared: missing, never 0
        assert window.project.rail("VOUT").sources[0].accuracy_pct is None
        assert cells()[4].startswith("missing") and cells()[-2] == "NOT JUDGED", cells()
        assert "enter U1's DC accuracy" in panel.table.item(0, 4).toolTip()
        panel.bw_limit.setChecked(True)
        assert window.project.tolerance.bw_limit_20mhz and panel.result.options["bw_limit_20mhz"]
    ''')


def test_a_rail_edit_turns_a_pass_not_judged_without_a_tab_switch():
    """The Results and Rail setup docks are on screen together: an edit there drops the power
    tree and makes the noise stale, so the shown PASS must turn NOT JUDGED at once."""
    run_in_window(f'''
        window.show()
        window.open_project({EXAMPLE!r})
        window.results_tabs.setCurrentWidget(panel)
        window.nets.select("VOUT")
        app.processEvents()
        held()
        assert cells()[-2] == "PASS"
        window.rail_panel.loads.table.cellWidget(0, 3).setValue(4.0)   # U2's current, in the Rail setup
        app.processEvents()
        assert window.tree is None and window.noise_panel.stale
        assert cells()[-2] == "NOT JUDGED", cells()
        assert cells()[3].startswith("missing") and cells()[5].startswith("missing")
        assert "the setup changed since its solve" in panel.table.item(0, 5).toolTip()
    ''')


def test_a_row_click_shows_its_load_on_the_board():
    run_in_window(f'''
        window.show()
        window.open_project({EXAMPLE!r})
        window.results_tabs.setCurrentWidget(panel)
        window.nets.select("VIN")
        app.processEvents()
        zoomed = []
        window.view.zoom_to = lambda x, y, *a: zoomed.append((x, y))
        text = panel.go_to_row(0)
        app.processEvents()
        assert window.net == "VOUT" and text.startswith("U2 on VOUT"), (window.net, text)
        assert window.statusBar().currentMessage() == text and len(zoomed) == 1, zoomed
    ''')


def test_export_writes_the_clis_two_files(tmp_path):
    run_in_window(f'''
        import csv, json
        from pathlib import Path
        out = Path({str(tmp_path)!r})
        window.open_project({EXAMPLE!r})
        app.processEvents()
        held()
        names = sorted(p.name for p in panel.export_files(out))
        stem = "tolerance.coppernicus"
        assert names == [f"{{stem}}_tolerance.csv", f"{{stem}}_tolerance_summary.json"], names
        doc = json.loads((out / names[1]).read_text(encoding="utf-8"))
        assert doc["kind"] == "tolerance" and doc["verdict"] == "pass" and doc["csv_file"].endswith(names[0])
        rows = list(csv.DictReader(open(out / names[0], encoding="utf-8")))
        assert rows[0]["load"] == "U2" and rows[0]["verdict"] == "pass"
        assert rows[0]["dc_source"] == "power tree"
    ''')


def test_a_misspelt_key_in_the_loaded_file_is_not_judged_until_the_key_it_meant_is_set(tmp_path):
    text = Path(EXAMPLE).read_text(encoding="utf-8").replace("    accuracy_pct: 1.0", "    acuracy_pct: 1.0")
    text = text.replace("board: tolerance.kipi.json.gz",
                        f"board: {Path(EXAMPLE).with_name('tolerance.kipi.json.gz').resolve().as_posix()}")
    typo = tmp_path / "typo.coppernicus.yml"
    typo.write_text(text, encoding="utf-8")
    run_in_window(f'''
        window.open_project({str(typo)!r})
        app.processEvents()
        held()
        notes = panel.notes.toPlainText()
        assert panel.result.verdict == "not_judged", panel.header.text()
        assert "acuracy_pct" in notes and "did you mean 'accuracy_pct'" in notes, notes
        window._save(window.project.path)                     # the file loses the key; the tab never does
        assert "acuracy_pct" not in open({str(typo)!r}, encoding="utf-8").read()
        window.reload_board()
        held()
        assert any(e["limit"] == "acuracy_pct" for e in panel.result.not_judged)
        panel.set_cell("source", "U1", "accuracy_pct", "1")   # what it meant
        assert not any(e["limit"] == "acuracy_pct" for e in panel.result.not_judged)
        assert panel.result.verdict == "pass", panel.header.text()
        window.dirty = False
        window.open_project({EXAMPLE!r})
        app.processEvents()
        assert window.misspelt == []
    ''')


def test_nothing_is_judged_while_the_setup_cannot_be_read():
    run_in_window(f'''
        from coppernicus.project import Load
        window.open_project({EXAMPLE!r})
        app.processEvents()
        held()
        window.fields_error = "strip.coppernicus.yml: bad field"
        panel.refresh()
        assert panel.result is None and panel.table.rowCount() == 0
        assert "KiCad footprint field" in panel.header.text() and not panel.export_button.isEnabled()
        window.fields_error = None
        window.project.rail("VOUT").loads.append(Load("U99", 1.0))
        window._remerge()
        panel.refresh()
        assert panel.result is None and "U99" in panel.header.text(), panel.header.text()
    ''')


def test_the_solve_button_runs_the_rows_next_solve_in_the_tab_that_owns_it():
    run_in_window(f'''
        import coppernicus.app.ac_panel as ac_panel
        import coppernicus.app.noise_panel as noise_panel
        started, asked = [], []
        ac_panel.AcWorker.start = lambda self: started.append(self)
        noise_panel.NoiseWorker.start = lambda self: started.append(self)
        window.solve_tree = lambda: asked.append("tree")
        window.open_project({EXAMPLE!r})
        app.processEvents()
        held(tree=False, noise=False)
        assert panel.solve_button.text() == "Solve power tree" and panel.solve_button.isEnabled()
        panel.solve_button.click()
        assert asked == ["tree"]
        held(noise=False)
        assert panel.solve_button.text() == "Solve noise of U1"
        panel.solve_button.click()
        assert window.results_tabs.currentWidget() is window.noise_panel
        assert started[-1].phase == "size" and started[-1].name == "U1"
        window.noise_panel._busy = False
        held()
        assert panel.solve_button.text() == "Solve impedance of VOUT + transient", panel.solve_button.text()
        panel.solve_button.click()
        ac = window.ac_panel
        assert window.results_tabs.currentWidget() is ac and ac.rail_box.currentText() == "VOUT"
        assert started[-1].phase == "size" and ac._transient
        from coppernicus.engine.ac import AcSize
        ac._current = lambda: True                             # the sizing ends: the solve starts
        ac._sized(AcSize("VOUT", 1000, 500, 2, 111, 278, 0.1))
        assert started[-1].phase == "solve" and started[-1].transient is True
        ac._busy = False
        ac.solve()                                             # the Impedance tab's own: no transient
        assert not ac._transient
        ac._busy = False
        window.ac_panel.results["VOUT"] = (_impedance(probe="LOAD"), ac_key(window.merged.project))
        panel.refresh()
        assert not panel.solve_button.isEnabled(), panel.solve_button.text()
    ''')


FIELDS_PRELUDE = '''
import numpy as np
from pathlib import Path
from kicad_fixture import write_strip_board
from coppernicus.app import fields_view
from coppernicus.project import Load, Project, Rail, Source
from coppernicus.results import ImpedanceResult
FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"}, "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A"}}


def strip_project(tmp, name, rails=()):
    write_strip_board(tmp / "strip.kicad_pcb", FIELDS)
    path = Project(board="strip.kicad_pcb", rails=list(rails)).save(tmp / name)
    window.dirty = False
    assert window.open_project(str(path))
    app.processEvents()
    return path
'''


def test_a_kicad_fields_rail_takes_a_tolerance_in_the_tab_with_no_warning_and_no_stale_result(tmp_path):
    """No KiCad field holds a tolerance: typed on a rail from the fields, it is held by a copy
    of the fields' parts in the project's own rail, which the merge sees as no difference, and
    which changes nothing solved (the held impedance stays current)."""
    run_in_window(FIELDS_PRELUDE + textwrap.dedent(f'''
        tmp = Path({str(tmp_path)!r})
        strip_project(tmp, "strip.coppernicus.yml")
        assert window.merged.origins["VCC"].source == "fields" and window.project.rails == []
        f = np.array([1e3, 1e6])
        window.ac_panel.results["VCC"] = (ImpedanceResult("VCC", ["VCC"], "", "J1", None, f, f),
                                          ac_key(window.merged.project))
        assert window.ac_panel.rail_box.currentText() == "VCC" and not window.ac_panel.stale
        panel.refresh()
        assert [panel.loads.item(0, c).text() for c in (0, 1)] == ["U1", "VCC"]
        assert panel.loads.item(0, 2).flags() & QtCore.Qt.ItemIsEditable
        panel.set_cell("load", "U1", "tol_pct", "5")
        own = window.project.rail("VCC")
        assert [s.ref for s in own.sources] == ["J1"] and own.loads[0].tol_pct == (-5.0, 5.0)
        assert window.merged.origins["VCC"].source == "fields" and window.merged.warnings == []
        assert window.merged.project.rail("VCC").loads[0].tol_pct == (-5.0, 5.0)
        assert "KiCad footprint fields" in window.statusBar().currentMessage()
        assert window.ac_panel.rail_box.currentText() == "VCC" and not window.ac_panel.stale
        assert any("tol_pct of U1 from the project file" in a for a in panel.result.assumptions)
    '''))


def test_a_cell_a_kicad_field_holds_is_read_only_in_the_tab_and_its_neighbours_still_edit(tmp_path):
    """PI_ACC on J1 and PI_AC_TOL on U1: those cells show the field's value with the look of the
    tab's other read-only cells and a tooltip naming the field and the part; typed anyway, the
    value is refused and nothing is written. U1's tol_pct, held by no field, still edits."""
    run_in_window(FIELDS_PRELUDE + textwrap.dedent(f'''
        from coppernicus.app.tolerance_panel import LOAD_COLUMNS, READ_ONLY_BG, SOURCE_COLUMNS
        tmp = Path({str(tmp_path)!r})
        held = {{"J1": {{**FIELDS["J1"], "PI_ACC": "1%"}}, "U1": {{**FIELDS["U1"], "PI_AC_TOL": "20mV"}}}}
        write_strip_board(tmp / "strip.kicad_pcb", held)
        path = Project(board="strip.kicad_pcb").save(tmp / "held.coppernicus.yml")
        window.dirty = False
        assert window.open_project(str(path))
        app.processEvents()
        panel.refresh()

        def cell(table, columns, key):
            return table.item(0, 2 + [k for k, _h in columns].index(key))
        editable = QtCore.Qt.ItemIsEditable
        for table, columns, key, text, field, part in (
                (panel.sources, SOURCE_COLUMNS, "accuracy_pct", "1", "PI_ACC", "J1"),
                (panel.sources, SOURCE_COLUMNS, "accuracy_mv", "", "PI_ACC", "J1"),
                (panel.loads, LOAD_COLUMNS, "ac_tol_mv", "20", "PI_AC_TOL", "U1"),
                (panel.loads, LOAD_COLUMNS, "ac_tol_pct", "", "PI_AC_TOL", "U1")):
            item = cell(table, columns, key)
            assert item.text() == text and not item.flags() & editable, (key, item.text())
            assert item.background().color() == READ_ONLY_BG, key
            tip = item.toolTip()
            assert f"from KiCad field {{field}}" in tip and f"on {{part}}; change it in KiCad" in tip, tip
        for key in ("tol_pct", "dc_tol_mv", "ripple_mv", "transient_mv"):
            item = cell(panel.loads, LOAD_COLUMNS, key)
            assert item.flags() & editable and item.background().style() == QtCore.Qt.NoBrush, key
        panel.set_cell("source", "J1", "accuracy_pct", "2")     # typed anyway: refused, nothing written
        said = panel.status.text()
        assert said.startswith('Not applied: from KiCad field PI_ACC "1%" on J1'), said
        assert cell(panel.sources, SOURCE_COLUMNS, "accuracy_pct").text() == "1"
        assert window.project.rails == [] and not window.dirty
        panel.set_cell("load", "U1", "tol_pct", "5")
        assert window.project.rail("VCC").loads[0].tol_pct == (-5.0, 5.0) and window.dirty
        assert window.merged.warnings == [] and cell(panel.loads, LOAD_COLUMNS, "tol_pct").text() == "5"
        assert not cell(panel.loads, LOAD_COLUMNS, "ac_tol_mv").flags() & editable
        assert window.merged.project.rail("VCC").sources[0].accuracy_pct == (-1.0, 1.0)

        def no_dialog(*_a):
            raise AssertionError("Override in project asked, though nothing differs")
        fields_view.ask_override = no_dialog
        assert window.override_fields("VCC")                 # the tooltip's way: the project's values
        panel.refresh()
        for table, columns, key, text in ((panel.sources, SOURCE_COLUMNS, "accuracy_pct", "1"),
                                          (panel.loads, LOAD_COLUMNS, "ac_tol_mv", "20")):
            item = cell(table, columns, key)
            assert item.text() == text and item.flags() & editable, (key, item.text())
            assert item.background().style() == QtCore.Qt.NoBrush, key
        panel.set_cell("source", "J1", "accuracy_pct", "2")
        assert window.project.rail("VCC").sources[0].accuracy_pct == (-2.0, 2.0)
        assert window.merged.project.rail("VCC").sources[0].accuracy_pct == (-2.0, 2.0)
    '''))


def test_the_way_back_to_the_kicad_fields_keeps_the_tolerances_by_ref(tmp_path):
    """Use KiCad fields empties the project rail's sources and loads: their tolerances stay, by
    ref, in a copy of the fields' parts; a part the fields lack is named before it goes. And
    Override → start from the KiCad values keeps them too (the merged rail carried them)."""
    run_in_window(FIELDS_PRELUDE + textwrap.dedent(f'''
        tmp = Path({str(tmp_path)!r})
        own = Rail("VCC", [Source("J1", 3.3, accuracy_pct=1)],
                   [Load("U1", 2.0, tol_pct=4), Load("U9", 1.0, tol_pct=3)], override_board_fields=True)
        strip_project(tmp, "over.coppernicus.yml", [own])
        asked = []
        fields_view.confirm_use_fields = lambda _w, net, *rest: asked.append((net, rest)) or True
        assert window.use_fields("VCC")
        assert asked and asked[0][0] == "VCC" and "U9" in str(asked[0][1]), asked
        own = window.project.rail("VCC")
        assert not own.override_board_fields
        assert [(t.ref, t.accuracy_pct) for t in own.sources] == [("J1", (-1.0, 1.0))], own.sources
        assert [(t.ref, t.current, t.tol_pct) for t in own.loads] == [("U1", 1.0, (-4.0, 4.0))], own.loads
        assert window.merged.origins["VCC"].source == "fields" and window.merged.warnings == []
        assert window.merged.project.rail("VCC").loads[0].tol_pct == (-4.0, 4.0)

        plain = Rail("VCC", [Source("J1", 3.3)], [Load("U1", 2.0)], override_board_fields=True)
        strip_project(tmp, "plain.coppernicus.yml", [plain])
        assert window.use_fields("VCC")                      # nothing to keep: emptied, as before
        assert window.project.rail("VCC").sources == [] and window.project.rail("VCC").loads == []

        ignored = Rail("VCC", [Source("J1", 3.3)], [Load("U1", 2.0, tol_pct=4)])
        strip_project(tmp, "ignored.coppernicus.yml", [ignored])
        fields_view.ask_override = lambda *a: fields_view.START_FROM_KICAD
        assert window.override_fields("VCC")
        loads = window.project.rail("VCC").loads
        assert [(t.ref, t.current, t.tol_pct) for t in loads] == [("U1", 1.0, (-4.0, 4.0))], loads
    '''))
