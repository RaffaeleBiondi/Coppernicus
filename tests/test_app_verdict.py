"""The verdict line and the cards at the top of the DC pages, and the Problems rows: one line
each, the selected row's detail in a strip under the list and ringed on the map."""

import os

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app import theme  # noqa: E402
from coppernicus.app.verdict import dc_cards  # noqa: E402
from coppernicus.engine import Board, solve_rail  # noqa: E402
from coppernicus.engine.synthetic import demo_board, power_chain_board, power_chain_project  # noqa: E402
from coppernicus.engine.tree import solve_project_tree  # noqa: E402
from coppernicus.power_tree import group_limits, group_verdict  # noqa: E402
from coppernicus.problems import FAIL, find_problems  # noqa: E402
from coppernicus.project import Project  # noqa: E402

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo", "demo.coppernicus.yml")


@pytest.fixture(scope="module")
def demo():
    project = Project.load(DEMO)
    rail = project.rail("+3V3")
    res = solve_rail(Board(demo_board()), rail, project.settings)
    return res, rail.limits, find_problems(res, rail.limits)


def test_the_cards_are_the_results_own_numbers(demo):
    res, limits, problems = demo
    cards = {c.key: c for c in dc_cards(res, problems, limits)}
    assert list(cards) == ["Worst load", "Largest drop on the copper", "Copper loss",
                           "Strongest current crowding"]
    worst = max(res.loads, key=res.drop)
    assert cards["Worst load"].value == f"{worst.name}: {res.drop(worst) * 1e3:.2f} mV"
    assert cards["Worst load"].detail.endswith(f"limit {limits.max_drop_pct:g} %")
    assert cards["Worst load"].state == "fail"              # the demo's +3V3 fails at its worst load
    assert cards["Largest drop on the copper"].value == f"{res.max_drop * 1e3:.2f} mV"
    assert cards["Copper loss"].value == f"{res.power_loss * 1e3:.3g} mW"
    top = max(res.loss_breakdown().items(), key=lambda kv: kv[1])[0]
    assert cards["Copper loss"].detail.startswith(f"most on {top} (")
    spot = max((p for p in problems if p.kind == "hotspot"), key=lambda p: p.value)
    assert cards["Strongest current crowding"].value == f"{spot.value:.3g} A/mm² mean"
    assert cards["Strongest current crowding"].state != "fail"      # attention at most: no limit set


def test_a_ground_group_shows_its_rise_not_a_drop():
    tree = solve_project_tree(Board(power_chain_board()), power_chain_project())
    gv = group_verdict(tree, "GND")
    cards = dc_cards(tree.groups["GND"], gv.problems, group_limits(tree, "GND")[0])
    keys = [c.key for c in cards]
    assert "Largest ground rise on the copper" in keys, keys
    rise = next(c for c in cards if c.key == "Largest ground rise on the copper")
    assert rise.detail == "above 0 V"
    assert rise.state == ("fail" if any(p.kind == "rise" and p.severity == FAIL for p in gv.problems) else "")


@pytest.mark.parametrize("name", theme.THEMES)
def test_the_badges_and_the_setup_foot_read_in_both_themes(name):
    from test_app_theme import contrast
    t = theme.tokens(name)
    for badge in ("badge-fail", "badge-pass", "badge-attention", "selected"):
        assert contrast(theme.WHITE, t[badge]) >= 4.5, badge
    assert "QWidget#setupFoot QPushButton:hover" in theme.style_sheet(name)
    hover = theme.style_sheet(name).split("QWidget#setupFoot QPushButton:hover")[1].split("}")[0]
    assert "background" not in hover, hover                  # stays on --selected: white reads on it


def test_the_header_line_cards_rows_strip_and_ring_follow_the_solve():
    run_in_window(f'''
        import html
        import tempfile
        import coppernicus.app.main_window as mw
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)       # never the user's
        QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, tempfile.mkdtemp())
        from coppernicus.app import theme

        class HeldWorker(mw.SolveWorker):
            def start(self):
                pass

        mw.SolveWorker = HeldWorker
        window.resize(1400, 900)
        window.show()
        window.open_project({DEMO!r})
        window.nets.select("+3V3")
        app.processEvents()
        header, panel = window.problems.header, window.problems
        assert header.isVisible() and header.badge.isHidden() and header.shown_cards() == []
        window.solve()
        window.worker.run()
        app.processEvents()
        assert header.badge.text() == "FAIL" and header.badge.property("badge") == "fail", (
            header.badge.text(), panel.summary.text(), window.statusBar().currentMessage())
        cards = header.shown_cards()
        assert [c.key for c in cards][:2] == ["Worst load", "Largest drop on the copper"], cards
        assert cards[0].state == "fail"
        assert "#" not in header.badge.text()                 # colours come from the style sheet
        assert panel.tree.columnCount() == 4 and panel.tree.headerItem().text(3) == "Where (mm)"
        assert window.view.marked() is None                    # nothing selected: no ring
        first = panel.tree.topLevelItem(0)
        panel.tree.setCurrentItem(first)
        app.processEvents()
        problem = panel.current_problem()
        assert html.escape(problem.detail) in panel.detail.text(), panel.detail.text()
        assert window.view.marked() is problem
        before = theme.current()
        window.toggle_theme()                                  # the badge keeps its colour role
        assert header.badge.property("badge") == "fail" and theme.current() != before
        window.results_tabs.setCurrentWidget(window.ac_panel)
        assert not header.isVisible()                          # DC pages only
        window.results_tabs.setCurrentWidget(window.losses)
        assert header.isVisible()
        panel.clear()
        assert header.badge.isHidden() and header.shown_cards() == [] and panel.tree.topLevelItemCount() == 0
        assert window.view.marked() is None
    ''')
