"""A regulator's rated output current (``Source.current_limit_a``: the KiCad field PI_IMAX, or
``current_limit_a`` in the project file) next to the current it delivers in the power tree.

On the synthetic power chain (U1 delivers about 2.5 A, U2 2 A) and on a synthetic KiCad buck
whose PI_IMAX is on the IC, not on the inductor that holds the rail. Window checks run in a
child process, offscreen (the pattern of tests/test_app_window.py)."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402
from test_board_fields_library import buck_board  # noqa: E402

from coppernicus.project import Project  # noqa: E402

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"

SOLVE = '''
        def solve_tree(project):
            window.dirty = False
            assert window.open_project(project), project
            window.solve_tree()
            window.tree_worker.wait(120000)
            app.processEvents()
            app.processEvents()
            assert window.tree is not None, window.tree_panel.summary.text()
            return window.tree

        def vrm_table():
            table = window.tree_panel.vrms
            headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
            return headers, {row[0]: dict(zip(headers, row)) for row in window.tree_panel.vrm_rows()}
'''


def chain_project(folder: Path, ratings=None) -> Path:
    """The power-chain example in ``folder``, each source of ``ratings`` (net -> A) rated."""
    shutil.copytree(CHAIN, folder)
    project = Project.load(folder / "power_chain.coppernicus.yml")
    for net, amps in (ratings or {}).items():
        project.rail(net).sources[0].current_limit_a = amps
    return project.save(folder / "power_chain.coppernicus.yml")


def rated_buck(folder: Path, imax: str = "2A") -> Path:
    """The synthetic buck of tests/test_board_fields_library.py with PI_IMAX on its IC (U1;
    the rail is held at the inductor L1's pad), and an empty project file next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    board = buck_board(folder / "buck.kicad_pcb")
    text = board.read_text(encoding="utf-8")
    assert text.count('(property "PI_KIND" "smps"') == 1
    board.write_text(text.replace('(property "PI_KIND" "smps"',
                                  f'(property "PI_IMAX" "{imax}" (at 0 0 0) (layer "F.Fab")) '
                                  '(property "PI_KIND" "smps"'), encoding="utf-8")
    return Project(board="buck.kicad_pcb").save(folder / "buck.coppernicus.yml")


# ---------------------------------------------------------------------- the Power tree tab

def test_the_power_tree_tab_shows_i_max_next_to_i_out_only_when_a_project_has_ratings(tmp_path):
    plain = chain_project(tmp_path / "plain")
    rated = chain_project(tmp_path / "rated", {"+3V3": 3.0})
    buck = rated_buck(tmp_path / "buck")
    run_in_window(SOLVE + f'''
        solve_tree({str(plain)!r})
        headers, rows = vrm_table()
        assert "I max (A)" not in headers, headers                 # no rating: no column

        solve_tree({str(rated)!r})                                  # from the project file
        headers, rows = vrm_table()
        assert headers[headers.index("I out (A)") + 1] == "I max (A)", headers
        assert rows["U1"]["I max (A)"] == "3" and rows["J1"]["I max (A)"] == "—", rows
        assert rows["U2"]["I max (A)"] == "—", rows

        solve_tree({str(buck)!r})                                   # from KiCad field PI_IMAX on U1
        headers, rows = vrm_table()
        assert rows["U1"]["I max (A)"] == "2" and rows["J1"]["I max (A)"] == "—", rows
    ''')


def dual_label_chain(folder: Path) -> Path:
    """The power chain with both regulators labelled REG (one part, two outputs): the engine
    names them "REG (+3V3)" and "REG (+1V8)". REG on +3V3 rated 2 A (it delivers 2.502 A)."""
    project = Project.load(chain_project(folder, {"+3V3": 2.0}))
    for net in ("+3V3", "+1V8"):
        project.rail(net).sources[0].name = "REG"
    return project.save(folder / "power_chain.coppernicus.yml")


def test_the_i_max_column_shows_the_judged_rating_of_a_label_on_several_rails_and_then_goes(tmp_path):
    dual = dual_label_chain(tmp_path / "dual")
    plain = chain_project(tmp_path / "plain")
    run_in_window(SOLVE + f'''
        solve_tree({str(dual)!r})
        headers, rows = vrm_table()
        assert "I max (A)" in headers, headers
        assert rows["REG (+3V3)"]["I max (A)"] == "2" and rows["REG (+1V8)"]["I max (A)"] == "—", rows
        table = window.tree_panel.vrms
        red = [table.item(r, 0).text() for r in range(table.rowCount())
               if table.item(r, 0).foreground().color().name() == "#c0392b"]
        assert red == ["REG (+3V3)"], red

        window.dirty = False
        assert window.open_project({str(plain)!r})                  # the tree is dropped
        headers, rows = vrm_table()
        assert headers == window.tree_panel.VRM_HEADERS and rows == {{}}, (headers, rows)
        solve_tree({str(plain)!r})
        headers, rows = vrm_table()
        assert "I max (A)" not in headers, headers
    ''')


# ---------------------------------------------------------------------- the judgement

def solved_chain(ratings=None):
    """The synthetic power chain solved as a tree, each source of ``ratings`` (net -> A) rated:
    U1 (+3V3) delivers about 2.502 A, U2 (+1V8) 2 A, J1 (VIN) about 0.77 A."""
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    from coppernicus.engine.tree import solve_project_tree
    project = power_chain_project()
    for net, amps in (ratings or {}).items():
        project.rail(net).sources[0].current_limit_a = amps
    return solve_project_tree(Board(power_chain_board()), project), project


def regulator_problems(doc, group):
    return [p for p in next(g for g in doc["groups"] if g["group"] == group)["problems"]
            if p["kind"] == "regulator"]


def test_a_regulator_over_its_rating_fails_its_group_and_names_where_the_rating_was_written():
    from coppernicus.board_fields import Merged
    from coppernicus.power_tree import rating_origins, tree_summary, tree_text
    tree, project = solved_chain({"+3V3": 2.0, "+1V8": 3.0})
    rating_origins(tree, Merged(project))                  # no KiCad fields: the project file
    doc = tree_summary(tree)
    assert doc["verdict"] == "fail" and doc["failed"] == 1, (doc["verdict"], doc["failed"])
    rows = {v["name"]: v for v in doc["vrms"]}
    assert rows["U1"]["exceeds_current_limit"] is True and rows["U2"]["exceeds_current_limit"] is False
    assert rows["J1"]["exceeds_current_limit"] is None and rows["J1"]["current_limit_from"] is None
    assert rows["U1"]["current_limit_from"] == "the project file"
    groups = {g["group"]: g for g in doc["groups"]}
    assert groups["+3V3"]["verdict"] == "fail" and groups["+3V3"]["failed"] == 1
    assert groups["+1V8"]["verdict"] == "pass" and groups["VIN"]["verdict"] == "not_judged"
    assert "current_limit_a" not in groups["+3V3"]["limits"]       # limits keep LIMIT_KEYS only
    [fail] = regulator_problems(doc, "+3V3")
    title = "U1: 2.502 A out, over its rated 2 A (from the project file)"
    assert fail["severity"] == "fail" and fail["title"] == title, fail
    assert fail["current_limit_a"] == 2.0 and abs(fail["i_out_a"] - 2.502) < 1e-6, fail
    assert fail["current_limit_from"] == "the project file"
    assert groups["+3V3"]["problems"][0]["title"] == title          # fails first
    [ok] = regulator_problems(doc, "+1V8")
    assert ok["severity"] == "info", ok
    assert ok["title"] == "U2: 2 A out, within its rated 3 A (from the project file)", ok
    assert regulator_problems(doc, "VIN") == []                     # J1 has no rating: silent
    assert "U1: current_limit_a = 2 (from the project file)" in doc["verdict_note"], doc["verdict_note"]
    assert title in doc["verdict_note"], doc["verdict_note"]
    assert not any(u["limit"] == "current_limit_a" for u in doc["not_judged"]), doc["not_judged"]
    lines = tree_text(doc)
    u1 = next(line for line in lines if line.strip().startswith("U1 "))
    u2 = next(line for line in lines if line.strip().startswith("U2 "))
    j1 = next(line for line in lines if line.strip().startswith("J1 "))
    assert "rated current: FAIL" in u1 and "rated current: pass" in u2 and "rated" not in j1, lines
    assert title in next(line for line in lines if line.startswith("Verdict: fail")), lines


def test_a_rating_alone_judges_the_tree_and_without_one_nothing_is_judged_or_listed():
    """A rating is a limit the user wrote: alone, within it, the tree passes (the note does
    not say no limit was set). No rating: not judged, and not listed as not judged either."""
    from coppernicus.power_tree import tree_summary
    tree, _ = solved_chain({"+3V3": 3.0})
    doc = tree_summary(tree)
    assert doc["verdict"] == "pass" and doc["failed"] == 0, doc["verdict"]
    assert "No limits set" not in doc["verdict_note"] and "U1: current_limit_a = 3" in doc["verdict_note"]
    plain = tree_summary(solved_chain()[0])
    assert plain["verdict"] == "not_judged" and plain["not_judged"] == []
    assert plain["verdict_note"].startswith("No limits set")
    assert all(v["exceeds_current_limit"] is None and v["current_limit_from"] is None for v in plain["vrms"])
    assert not any(p["kind"] == "regulator" for g in plain["groups"] for p in g["problems"])


def test_a_rating_brings_one_assumption_line_stating_the_rule_as_judged():
    from coppernicus.power_tree import apply_limits, tree_summary
    tree, project = solved_chain({"+3V3": 2.0})
    apply_limits(tree, project)                                     # judged again: still once
    lines = [a for a in tree_summary(tree)["assumptions"] if "current_limit_a" in a]
    assert lines == [
        "Regulator ratings (a source's rated output current, current_limit_a: KiCad field PI_IMAX "
        "or the project file): a regulator fails when |I out| is above its rating (within 1e-9, "
        "relative, of it counts as at the rating: the solve's round-off); not judged while its "
        "group, a group it feeds or the power-tree iteration has not converged, nor on a rail the "
        "tree does not solve; a regulator out of regulation is still judged on its current, as "
        "KiPI does."], lines
    plain = tree_summary(solved_chain()[0])
    assert not any("current_limit_a" in a for a in plain["assumptions"])


def test_a_regulator_delivering_exactly_its_rating_passes_despite_round_off():
    """U2 feeds a 2 A load; the solve's round-off can put it a hair above (KiPI v1.0.1 gave
    2.0000000000001097 A). Rated exactly 2 A it passes (|I out| <= rating, as KiPI's check;
    within 1e-9 of it counts as at it)."""
    from coppernicus.power_tree import apply_limits, tree_summary
    tree, project = solved_chain({"+1V8": 2.0})
    u2 = next(v for v in tree.vrms if v.name == "U2")
    u2.i_out_a = 2.0 * (1 + 5e-11)                                  # round-off above the rating
    apply_limits(tree, project)
    doc = tree_summary(tree)
    assert doc["verdict"] == "pass", doc["verdict_note"]
    assert {v["name"]: v for v in doc["vrms"]}["U2"]["exceeds_current_limit"] is False


def test_a_fail_never_prints_its_current_equal_to_the_rating():
    from coppernicus.power_tree import rating_title
    from coppernicus.results import VrmReport
    v = VrmReport("U9", "ldo", "+1V8", 1.8, 1.8, 2.0003, current_limit_a=2.0, current_passed=False)
    assert rating_title(v) == ("U9: 2.0003 A out, over its rated 2 A (from KiCad field PI_IMAX or "
                               "the project file)")                 # where it came from not known
    v = VrmReport("U9", "ldo", "-5V", -5.0, -5.0, -1.5, current_limit_a=1.0, current_passed=False,
                  current_limit_from="the project file")
    assert rating_title(v) == "U9: 1.5 A out, over its rated 1 A (from the project file)"


def test_a_rating_of_a_group_that_did_not_converge_is_not_judged():
    from coppernicus.power_tree import apply_limits, tree_summary
    tree, project = solved_chain({"+3V3": 2.0})
    tree.groups["+3V3"].converged = False
    apply_limits(tree, project)                                     # judged again, no solve
    doc = tree_summary(tree)
    assert doc["verdict"] == "not_judged" and doc["failed"] == 0, doc["verdict"]
    assert {"item": "U1", "net": "+3V3", "limit": "current_limit_a",
            "reason": "its iteration did not converge"} in doc["not_judged"], doc["not_judged"]
    assert {v["name"]: v for v in doc["vrms"]}["U1"]["exceeds_current_limit"] is None


def test_a_rating_is_not_judged_while_a_rail_it_feeds_has_not_converged():
    """U1's output current includes U2's input: +1V8 unsettled, U1 is not judged either. A
    load KiPI marks NOT CONVERGED (its outer iteration did not settle) stops it too."""
    from coppernicus.power_tree import apply_limits, tree_summary
    from coppernicus.results import NOT_CONVERGED
    tree, project = solved_chain({"+3V3": 2.0, "+1V8": 3.0})
    tree.groups["+1V8"].converged = False
    apply_limits(tree, project)
    doc = tree_summary(tree)
    assert doc["verdict"] == "not_judged" and doc["failed"] == 0, doc["verdict"]
    assert {"item": "U1", "net": "+3V3", "limit": "current_limit_a",
            "reason": "+1V8, which it feeds, did not converge"} in doc["not_judged"], doc["not_judged"]
    assert {"item": "U2", "net": "+1V8", "limit": "current_limit_a",
            "reason": "its iteration did not converge"} in doc["not_judged"], doc["not_judged"]
    tree, project = solved_chain({"+3V3": 2.0})
    next(ld for ld in tree.loads if ld.name == "U3").status = NOT_CONVERGED
    apply_limits(tree, project)
    doc = tree_summary(tree)
    assert {"item": "U1", "net": "+3V3", "limit": "current_limit_a",
            "reason": "the power-tree iteration did not converge"} in doc["not_judged"], doc["not_judged"]
    assert doc["failed"] == 0, doc["failed"]


def test_a_rating_is_not_judged_when_the_trees_outer_iteration_did_not_settle():
    """KiPI's ground-sense (outer) iteration can fail to settle with every group converged
    (KiPI tree.py:707-711: tree_converged False and its warning). The tree carries it, so a
    rating is not judged even where no load would show it."""
    from types import SimpleNamespace

    from coppernicus.engine.tree import outer_converged
    from coppernicus.power_tree import apply_limits, tree_summary
    assert outer_converged(SimpleNamespace(warnings=["+3V3: something else"]))
    assert not outer_converged(SimpleNamespace(
        warnings=["Ground-sense iteration did not converge (last change 1.20e-04 V)"]))
    tree, project = solved_chain({"+3V3": 2.0})
    assert tree.converged
    tree.converged = False
    apply_limits(tree, project)
    doc = tree_summary(tree)
    assert {"item": "U1", "net": "+3V3", "limit": "current_limit_a",
            "reason": "the power-tree iteration did not converge"} in doc["not_judged"], doc["not_judged"]
    assert doc["failed"] == 0 and doc["verdict"] == "not_judged", doc["verdict"]


def test_a_rating_on_a_rail_the_tree_does_not_solve_is_not_judged_not_dropped(tmp_path, capsys):
    """U2 (rated 1 A, delivers 2 A) on +1V8 left out with excluded_nets: KiPI turns it into a
    load on its input, so no VRM row judges it. Not judged, as a drop limit there is (exit 3).
    Likewise a rated source on a rail solved as a ground."""
    import json

    from coppernicus.cli import EXIT_NOT_JUDGED, main
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import power_chain_board, power_chain_project
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.power_tree import tree_summary
    from coppernicus.project import Rail, Source
    entry = {"item": "U2", "net": "+1V8", "limit": "current_limit_a",
             "reason": "not solved in the power tree"}
    project = chain_project(tmp_path / "chain", {"+1V8": 1.0})
    p = Project.load(project)
    p.engine_options = {"excluded_nets": ["+1V8"]}
    p.save(project)
    assert main(["solve", str(project), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_NOT_JUDGED
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "not_judged" and entry in doc["not_judged"], doc["not_judged"]
    note = doc["verdict_note"]
    assert "U2 on +1V8 (current_limit_a): not solved in the power tree" in note, note

    chain = power_chain_project()                       # a rated source on a rail used as a ground
    chain.rails.append(Rail("GND", sources=[Source("J1", 0.0, ["2"], current_limit_a=1.0)]))
    doc = tree_summary(solve_project_tree(Board(power_chain_board()), chain))
    assert {"item": "J1", "net": "GND", "limit": "current_limit_a",
            "reason": "not solved in the power tree"} in doc["not_judged"], doc["not_judged"]
    assert doc["verdict"] == "not_judged", doc["verdict"]


def test_the_rating_origin_follows_where_the_merged_rail_came_from():
    """KiCad fields define the rail: the part carrying PI_IMAX; the project file's rail (its
    own, overriding the fields, or the fields incomplete there): the project file."""
    from coppernicus.board_fields import Merged, Origin
    from coppernicus.power_tree import rating_origins
    tree, project = solved_chain({"+3V3": 2.0})
    for source, said in (("fields", "KiCad field PI_IMAX on U1"), ("overridden", "the project file"),
                         ("project", "the project file")):
        rating_origins(tree, Merged(project, origins={"+3V3": Origin("+3V3", source)}))
        assert next(v for v in tree.vrms if v.name == "U1").current_limit_from == said, source


def test_the_cli_tree_judges_a_kicad_rating_and_names_the_field(tmp_path, capsys):
    """The buck's PI_IMAX is on its IC U1 (the rail is held at the inductor's pad): U1
    delivers 0.5 A, rated 400 mA. Text and JSON say so; the exit code is 1."""
    import json

    from coppernicus.cli import EXIT_LIMIT, main
    project = rated_buck(tmp_path / "buck", "400mA")
    assert main(["solve", str(project), "--tree", "--out", str(tmp_path / "o")]) == EXIT_LIMIT
    out = capsys.readouterr().out
    said = "U1: 0.5 A out, over its rated 0.4 A (from KiCad field PI_IMAX on U1)"
    assert said in next(line for line in out.splitlines() if line.startswith("Verdict: fail")), out
    assert "rated current: FAIL" in next(line for line in out.splitlines() if line.strip().startswith("U1 "))
    assert main(["solve", str(project), "--tree", "--json", "--out", str(tmp_path / "o")]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    u1 = next(v for v in doc["vrms"] if v["name"] == "U1")
    assert u1["exceeds_current_limit"] is True and u1["i_max_a"] == 0.4
    assert u1["current_limit_from"] == "KiCad field PI_IMAX on U1"
    assert said in [p["title"] for g in doc["groups"] for p in g["problems"]]


def test_the_power_tree_tab_and_problems_show_a_regulator_over_its_rating(tmp_path):
    rated = chain_project(tmp_path / "rated", {"+3V3": 2.0, "+1V8": 3.0})
    buck = rated_buck(tmp_path / "buck", "400mA")
    run_in_window(SOLVE + f'''
        solve_tree({str(rated)!r})
        summary = window.tree_panel.summary.text()
        assert "1 limit(s) exceeded" in summary, summary
        table = window.tree_panel.vrms
        color = {{table.item(r, 0).text(): table.item(r, 0).foreground().color().name()
                 for r in range(table.rowCount())}}
        assert color["U1"] == "#c0392b" and color["U2"] != "#c0392b", color
        window.show_group("+3V3")
        app.processEvents()
        top = window.problems.tree.topLevelItem(0)
        assert top.text(0) == "FAIL", top.text(0)
        assert top.text(1) == "U1: 2.502 A out, over its rated 2 A (from the project file)", top.text(1)
        assert "nan" not in top.text(3), top.text(3)                # at U1's output pins

        solve_tree({str(buck)!r})
        window.show_group("+3V3")
        app.processEvents()
        titles = [window.problems.tree.topLevelItem(i).text(1)
                  for i in range(window.problems.tree.topLevelItemCount())]
        assert "U1: 0.5 A out, over its rated 0.4 A (from KiCad field PI_IMAX on U1)" in titles, titles
    ''')
