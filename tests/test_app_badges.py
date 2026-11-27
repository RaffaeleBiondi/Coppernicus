"""The toolbar with the four solves by their verbs (Open ▾, Save ▾, Export ▾ around them) and
the badges of the navigator and the net list: a page's verdict or a dashed "not solved", a
group's worst, a rail net's DC verdict."""

import os

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app.verdict import badge, worst  # noqa: E402

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo", "demo.coppernicus.yml")


def test_the_worst_verdict_and_the_badge_of_each():
    assert worst(["pass", "not_judged", None, ""]) == "not_judged"
    assert worst(["pass", "fail", "not_judged"]) == "fail"
    assert worst([None, ""]) is None
    assert badge(None) == ("ns", "not solved")
    assert badge("fail") == ("fail", "FAIL") and badge("pass") == ("pass", "PASS")
    assert badge("not_judged") == ("nj", "not judged")


def test_the_toolbar_has_the_four_solves_by_their_verbs_and_menus_for_the_rest():
    run_in_window('''
        bar = window.findChild(QtWidgets.QToolBar, "Main")
        shown = []
        for act in bar.actions():
            w = bar.widgetForAction(act)
            if act.isSeparator():
                shown.append("|")
            elif isinstance(w, QtWidgets.QToolButton) and w.text():
                shown.append(w.text())
        assert shown == ["Open", "Save", "|", "Solve rail", "Solve tree", "Solve impedance", "Solve noise",
                         "|", "Pin baseline", "Export", window.theme_action.text()], shown
        assert bar.widgetForAction(window.toolbar_actions["solve_rail"]).objectName() == "primary"
        menus = {name: [a.text() for a in b.menu().actions()] for name, b in window.toolbar_menus.items()}
        assert menus == {"Open": ["Open board…", "Open project…", "Reload board"],
                         "Save": ["Save project", "Save project as…"],
                         "Export": ["Export maps…", "Export report…"]}, menus
        own = {a.text(): a.shortcut().toString() for a in window.actions()}     # shortcuts in any menu
        assert own["Export maps…"] == "Ctrl+E" and own["Export report…"] == "Ctrl+R", own
        assert own["Reload board"] == "Ctrl+Shift+R" and own["Solve rail"] == "F5", own
        assert [a.text() for a in window.file_menu.actions() if a.text()] == [
            "Open board…", "Open project…", "Save project", "Save project as…", "Reload board"]
    ''')


def test_the_badges_follow_the_solves():
    run_in_window(f'''
        import coppernicus.app.main_window as mw

        class HeldWorker(mw.SolveWorker):
            def start(self):
                pass

        mw.SolveWorker = HeldWorker
        window.show()
        window.open_project({DEMO!r})
        window.nets.select("+3V3")
        app.processEvents()
        window.refresh_badges()
        nav = window.navigator
        assert nav.badge_of(window.problems) == ("ns", "not solved")
        assert nav.group_badge("Rail") == ("ns", "not solved")
        assert nav.badge_of(window.compare) == ("note", "no baseline")
        assert nav.badge_of(window.thermal_panel) == ("note", "off")      # electro-thermal not ticked
        for page in (window.tree_panel, window.ac_panel, window.noise_panel, window.tolerance_panel):
            assert nav.badge_of(page) == ("ns", "not solved"), page
        assert nav.group_badge("AC") == ("ns", "not solved")
        assert window.nets.badge_of("+3V3") == ("ns", "not solved")
        window.solve()
        window.worker.run()
        app.processEvents()
        app.processEvents()
        fails = sum(p.severity == "fail" for p in window.problems._problems)
        assert fails >= 1
        assert nav.badge_of(window.problems) == ("fail", f"{{fails}} FAIL")
        assert nav.group_badge("Rail") == ("fail", "FAIL")
        assert window.nets.badge_of("+3V3") == ("fail", "FAIL")
        assert nav.group_badge("Power tree") == ("ns", "not solved")    # nothing of the tree solved
        window.pin_baseline()
        app.processEvents()
        app.processEvents()
        assert nav.badge_of(window.compare) is None
    ''')
