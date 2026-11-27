"""The main window's layout C: the results pages chosen in a navigator under the nets, the map
above the page where the page has one, and the setup panel on the right that follows the page,
its solve button at the foot, hidden and shown again."""

import os

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo", "demo.coppernicus.yml")


def test_the_navigator_groups_the_pages_and_a_group_shows_its_first_page():
    run_in_window('''
        from coppernicus.app import shown_label
        nav, pages = window.navigator, window.results_tabs
        assert nav.groups() == ["Rail", "Power tree", "AC", "Tolerance"], nav.groups()
        labels = [shown_label(pages.tabText(i)) for i in range(pages.count())]
        assert labels == ["Problems", "Loss by layer", "Compare", "Tree", "Temperature", "Impedance",
                          "Ripple & noise", "Tolerance"], labels
        assert pages.currentWidget() is window.problems                     # where a first solve lands
        assert nav.currentItem() is nav.item_of(window.problems)
        nav.setCurrentItem(nav.topLevelItem(2))                             # "AC": its first page
        assert pages.currentWidget() is window.ac_panel
        assert nav.currentItem() is nav.item_of(window.ac_panel)
        pages.setCurrentWidget(window.tree_panel)                           # code moves the navigator too
        assert nav.currentItem() is nav.item_of(window.tree_panel)
        assert window.results_title.text() == "Results"
    ''')


def test_the_setup_panel_follows_the_page_with_its_solve_at_the_foot():
    run_in_window(f'''
        dock, pages = window.setup_dock, window.results_tabs
        window.show()
        window.open_project({DEMO!r})
        app.processEvents()
        expected = {{window.problems: ("rail", "Rail setup", window.rail_panel.solve),
                    window.compare: ("rail", "Rail setup", window.rail_panel.solve),
                    window.tree_panel: ("rail", "Rail setup", dock.foot_button("rail", "tree")),
                    window.thermal_panel: ("thermal", "Thermal setup", dock.foot_button("thermal", "tree")),
                    window.ac_panel: ("impedance", "Impedance setup", window.ac_panel.solve_button),
                    window.noise_panel: ("noise", "Noise setup", window.noise_panel.solve_button),
                    window.tolerance_panel: ("tolerance", "Tolerance setup",
                                             window.tolerance_panel.solve_button)}}
        for page, (key, title, button) in expected.items():
            pages.setCurrentWidget(page)                                    # at once, no event loop
            assert dock.current() == key and dock.title.text() == title, (key, dock.title.text())
            assert button.isVisible(), (title, button.text())
            assert dock.stack.currentWidget().isAncestorOf(button), title
        pages.setCurrentWidget(window.ac_panel)
        assert dock.isAncestorOf(window.ac_panel.rail_box) and not window.ac_panel.isAncestorOf(
            window.ac_panel.rail_box)                                       # the editor moved, not copied
        assert window.ac_panel.isAncestorOf(window.ac_panel.canvas)       # the plot stays on the page
        assert dock.foot_button("rail", "tree").text().startswith("Solve tree")
        assert window.rail_panel.solve.text() == "Solve rail  (F5)"
    ''')


def test_the_map_is_full_small_or_absent_as_the_page_needs():
    run_in_window('''
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        pages = window.results_tabs
        for page, size in ((window.problems, "full"), (window.ac_panel, "small"),
                           (window.noise_panel, "none"), (window.tolerance_panel, "none"),
                           (window.tree_panel, "full"), (window.thermal_panel, "full")):
            pages.setCurrentWidget(page)
            app.processEvents()
            assert window.map_size() == size and window.map_pane.isVisible() == (size != "none"), size
        pages.setCurrentWidget(window.problems)
        app.processEvents()
        full = window.map_pane.height()
        pages.setCurrentWidget(window.ac_panel)
        app.processEvents()
        assert window.map_pane.height() < full * 0.7, (window.map_pane.height(), full)
    ''')


def test_the_setup_panel_hides_and_comes_back_from_the_status_bar_or_the_view_menu():
    run_in_window('''
        window.show()
        app.processEvents()
        dock, again = window.setup_dock, window.setup_button
        assert dock.isVisible() and not again.isVisible()
        dock.hide_button.click()
        app.processEvents()
        assert not dock.isVisible() and again.isVisible()
        again.click()
        app.processEvents()
        assert dock.isVisible() and not again.isVisible()
        toggle = dock.toggleViewAction()
        assert toggle in window.view_menu.actions() and toggle.text() == "Setup panel"
        toggle.trigger()
        app.processEvents()
        assert not dock.isVisible()
    ''')


def test_solve_rail_at_the_foot_is_off_without_a_board_and_while_a_rail_solves():
    run_in_window(f'''
        solve = window.rail_panel.solve
        assert not solve.isEnabled()                                        # no board: nothing to solve
        window.open_project({DEMO!r})
        window.nets.select("+3V3")
        app.processEvents()
        assert solve.isEnabled()
        window.rail_panel.set_solve_free(False)                            # a rail solve running
        assert not solve.isEnabled()
        window.rail_panel.setEnabled(False)
        window.rail_panel.setEnabled(True)
        assert not solve.isEnabled()                                        # still running
        window.rail_panel.set_solve_free(True)
        assert solve.isEnabled()
    ''')


PDN = os.path.join(os.path.dirname(__file__), "..", "examples", "pdn", "pdn.coppernicus.yml")


def test_the_probes_table_shows_four_columns_and_the_card_edits_the_rest():
    """D4: Name, Part, Pins, Target in the narrow setup panel; the selected probe's other
    fields in the card under it, closed to one line, written through the table's own checks."""
    run_in_window(f'''
        from coppernicus.app.ac_panel import PROBE_COLUMNS
        window.show()
        window.open_project({PDN!r})
        panel = window.ac_panel
        window.results_tabs.setCurrentWidget(panel)
        app.processEvents()
        t = panel.probes
        shown = [h for c, (h, _k) in enumerate(PROBE_COLUMNS) if not t.isColumnHidden(c)]
        assert shown == ["Name", "Part", "Pins", "Target (Ω)"], shown
        t.setCurrentCell(0, 0)
        app.processEvents()
        assert panel.probe_card_toggle.text() == "Details of U1"
        assert "Ground net GND" in panel.probe_summary.text(), panel.probe_summary.text()
        assert panel.probe_summary.isVisible() and not panel.probe_card_body.isVisible()
        panel.probe_card_toggle.setChecked(True)
        app.processEvents()
        assert panel.probe_card_body.isVisible() and not panel.probe_summary.isVisible()
        up_to = panel.probe_fields["target_hz"]
        up_to.setText("20M")
        up_to.editingFinished.emit()
        col = [k for _h, k in PROBE_COLUMNS].index("target_hz")
        assert t.item(0, col).text() == "20M" and window.project.probes[0].target_hz == 2e7
        before = window.project.to_dict()
        up_to.setText("lots")
        up_to.editingFinished.emit()
        assert up_to.property("state") == "error" and "Not applied" in up_to.toolTip(), up_to.toolTip()
        assert window.project.to_dict() == before                       # refused: nothing written
    ''')
