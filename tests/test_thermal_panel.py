"""The Temperature tab and the board-temperature map (Task 8 GUI): the text the tab and the
map's readouts show, and the options form against ``project.thermal``. Pure logic here; the
window checks run in a child process (see tests/test_app_window.py)."""

import math

import numpy as np
import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app.hover import temperature_hover_text, temperature_reading  # noqa: E402
from coppernicus.app.thermal_panel import (  # noqa: E402
    field_value,
    form_values,
    heat_text,
    history_text,
    is_limit,
    layer_rows,
    load_rows,
    peak_text,
)
from coppernicus.project import ThermalOptions  # noqa: E402
from coppernicus.results import BoardTemperature, LayerTemperature, TemperatureMap, TreeResult  # noqa: E402
from coppernicus.temperature import thermal_summary  # noqa: E402

HISTORY = [{"iteration": 1, "heat_w": 0.559, "t_max_c": 41.334, "dt_max_c": 16.334},
           {"iteration": 2, "heat_w": 0.589, "t_max_c": 42.257, "dt_max_c": 0.923},
           {"iteration": 3, "heat_w": 0.591, "t_max_c": 42.309, "dt_max_c": 0.0514}]


def board_temperature(settled=True, history=HISTORY):
    t = np.array([[30.0, 41.8], [np.nan, 35.0]])
    maps = [TemperatureMap(0, "F.Cu", np.array([0.5, 1.5]), np.array([0.5, 1.5]), t),
            TemperatureMap(31, "B.Cu", np.array([0.5, 1.5]), np.array([0.5, 1.5]), t - 1.0)]
    rows = [LayerTemperature("F.Cu", 41.5, 16.5, 1.4, 0.6, "+3V3", None, "U1", "2", 0.25),
            LayerTemperature("B.Cu", 38.0, 13.0, 1.0, 1.0, "GND", None, "", "", None)]
    return BoardTemperature(
        maps=maps, copper=rows, ambient_c=25.0, airflow_m_s=0.0, h_w_m2k=10.0, grid_mm=1.0, grid_nx=2,
        grid_ny=2, grid_cells=3, iterations=len(history), settled=settled, history=list(history),
        heat_w=0.591, heat_by_kind_w={"vias": 0.177, "copper": 0.414}, peak_c=41.8, peak_x_mm=1.5,
        peak_y_mm=0.5, peak_layer="F.Cu", peak_pad_ref="U1", peak_pad_number="2", peak_pad_distance_mm=0.0,
        loads=[{"name": "U1", "group": "+3V3", "drop_ambient_v": 0.039156, "drop_v": 0.041354},
               {"name": "U9", "group": "+3V3", "drop_ambient_v": math.nan, "drop_v": 0.01}],
        assumptions=["Board temperature: steady state."], warnings=[])


def summary(settled=True, limits=None, history=HISTORY):
    return thermal_summary(TreeResult(thermal=board_temperature(settled, history), thermal_limits=limits))


# ---------------------------------------------------------------------- the tab's text

def test_history_reads_each_iterations_peak_and_how_it_settled():
    assert history_text(summary()) == "41.33 → 42.26 → 42.31 °C, last change 0.05 °C: settled"
    assert history_text(summary(settled=False)).endswith(
        "last change 0.05 °C: not settled, run more iterations")
    one = summary(settled=None, history=HISTORY[:1])
    assert history_text(one) == "41.33 °C, one iteration: settling not checked"
    tiny = dict(HISTORY[2], dt_max_c=0.0012)
    assert "last change 0.0012 °C" in history_text(summary(history=HISTORY[:2] + [tiny]))


def test_heat_and_the_grid_peak():
    assert heat_text(summary()) == "Heat 0.591 W: copper 0.414 W, vias 0.177 W"
    assert peak_text(summary()) == ("Grid peak 41.80 °C (rise 16.80 °C) on F.Cu at (1.50, 0.50) mm, "
                                    "nearest pad U1.2 (0.00 mm): shown, not judged")


def test_layer_rows_fail_over_a_limit_and_pass_only_when_judged():
    rows = layer_rows(summary(limits={"max_rise_c": 15.0}))
    assert rows == [("F.Cu", "41.50", "16.50", "1.40", "0.60", "+3V3", "U1.2", "0.25", "FAIL"),
                    ("B.Cu", "38.00", "13.00", "1.00", "1.00", "GND", "—", "—", "pass")]
    # Not settled: a row over the limit still fails, one within it is not judged.
    assert [r[-1] for r in layer_rows(summary(settled=False, limits={"max_rise_c": 15.0}))] == [
        "FAIL", "not judged"]
    assert [r[-1] for r in layer_rows(summary(settled=None, history=HISTORY[:1],
                                              limits={"max_rise_c": 15.0}))] == ["FAIL", "not judged"]
    assert [r[-1] for r in layer_rows(summary())] == ["not judged", "not judged"]          # no limits
    stuck = layer_rows(summary(limits={"max_rise_c": 15.0}), stuck={"GND"})
    assert [r[-1] for r in stuck] == ["FAIL", "not judged"]                     # its group did not converge


def test_load_rows_compare_the_drop_at_the_ambient_with_the_electro_thermal_one():
    assert load_rows(summary()) == [("U1", "+3V3", "39.156", "41.354", "+5.6"),
                                    ("U9", "+3V3", "—", "10.000", "—")]


# ---------------------------------------------------------------------- the options form

def test_form_shows_the_options_and_empty_limits():
    values = form_values(ThermalOptions())
    assert values == {"enabled": False, "ambient_c": 25.0, "airflow_m_s": 0.0, "iterations": 3,
                      "grid_mm": 0.0, "load_heat": False, "regulator_heat": "output_pads",
                      "max_temp_c": "", "max_rise_c": ""}
    values = form_values(ThermalOptions(enabled=True, max_temp_c=105.0, max_rise_c=15.5,
                                        regulator_heat="part_pads"))
    assert (values["enabled"], values["max_temp_c"], values["max_rise_c"], values["regulator_heat"]) == (
        True, "105", "15.5", "part_pads")


def test_each_field_is_read_back_as_the_project_stores_it():
    assert field_value("max_rise_c", "") is None and field_value("max_temp_c", "  ") is None
    assert field_value("max_rise_c", "1,5") == 1.5 and field_value("max_temp_c", "100") == 100.0
    assert field_value("iterations", 4) == 4 and isinstance(field_value("iterations", 4.0), int)
    assert field_value("ambient_c", 30) == 30.0 and field_value("enabled", 1) is True
    assert field_value("regulator_heat", "part_pads") == "part_pads"
    with pytest.raises(KeyError):
        field_value("edges", [])
    assert is_limit("max_temp_c") and is_limit("max_rise_c") and not is_limit("ambient_c")
    options = ThermalOptions()
    for key, raw in form_values(options).items():           # the form round-trips every option
        assert field_value(key, raw) == getattr(options, key)


# ---------------------------------------------------------------------- the map's readout

def test_the_temperature_readout_is_one_text_for_the_double_click_and_the_hover():
    assert temperature_reading(41.83, 0.24) == "T = 41.8 °C (0.24 mm cell)"
    assert temperature_reading(math.nan, 0.24) == ""
    assert temperature_hover_text(41.83, 0.24, "F.Cu", ("v", "drop", "layer")) == \
        "T = 41.8 °C (0.24 mm cell)\nF.Cu"
    assert temperature_hover_text(41.83, 0.24, "F.Cu", ("j",)) == "T = 41.8 °C (0.24 mm cell)"
    assert temperature_hover_text(41.83, 0.24, "F.Cu", ()) == ""            # the readout is off
    assert temperature_hover_text(math.nan, 0.24, "F.Cu", ("layer",)) == ""     # outside the board


# ---------------------------------------------------------------------- in the window

def test_the_temperature_tab_follows_loss_by_layer_and_the_map_is_offered():
    run_in_window('''
        tabs = [window.results_tabs.tabText(i) for i in range(window.results_tabs.count())]
        assert tabs[tabs.index("Loss by layer") + 1] == "Temperature", tabs
        assert window.results_tabs.widget(tabs.index("Temperature")) is window.thermal_panel
        fields = [window.field_box.itemText(i) for i in range(window.field_box.count())]
        assert "Board temperature (°C)" in fields, fields
    ''')


def test_the_form_writes_the_options_and_emits_only_on_a_change():
    run_in_window('''
        from coppernicus.project import ThermalOptions
        panel = window.thermal_panel
        heard = []
        panel.options_changed.connect(lambda: heard.append("options"))
        panel.limits_changed.connect(lambda: heard.append("limits"))
        options = ThermalOptions(grid_mm=0.2435)
        panel.set_options(options)
        assert heard == [], heard                                  # loading writes nothing
        panel.ambient.setValue(30.0)
        assert options.ambient_c == 30.0 and heard == ["options"], (options, heard)
        assert options.grid_mm == 0.2435                           # an unrelated edit keeps it exact
        panel.enabled.setChecked(True)
        panel.regulator_heat.setCurrentIndex(panel.regulator_heat.findData("part_pads"))
        assert options.enabled and options.regulator_heat == "part_pads", options
        heard.clear()
        panel.max_rise.setText("15")
        panel.max_rise.editingFinished.emit()
        panel.max_rise.editingFinished.emit()                      # focus leaves again: no change
        assert options.max_rise_c == 15.0 and heard == ["limits"], (options, heard)
        panel.max_rise.setText("")
        panel.max_rise.editingFinished.emit()
        assert options.max_rise_c is None and heard == ["limits", "limits"], (options, heard)
        # Typed and cleared from the keyboard: an empty field must be accepted, or Return and
        # focus-out never report it and the old limit would stay.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        window.show()
        panel.max_temp.setFocus()
        QTest.keyClicks(panel.max_temp, "105")
        QTest.keyClick(panel.max_temp, Qt.Key_Return)
        assert options.max_temp_c == 105.0, options
        panel.max_temp.selectAll()
        QTest.keyClick(panel.max_temp, Qt.Key_Delete)
        QTest.keyClick(panel.max_temp, Qt.Key_Return)
        assert options.max_temp_c is None, options
    ''')


def test_the_wheel_over_the_form_changes_no_field_that_is_not_focused():
    """The form scrolls: a wheel turn over a field on the way must not change it (any change
    of an option drops the solved power tree)."""
    run_in_window('''
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from coppernicus.project import ThermalOptions
        panel = window.thermal_panel
        options = ThermalOptions()
        panel.set_options(options)
        heard = []
        panel.options_changed.connect(lambda: heard.append("options"))
        window.show()
        app.processEvents()
        for field in (panel.ambient, panel.iterations, panel.grid, panel.regulator_heat):
            assert not field.hasFocus() and field.focusPolicy() == Qt.StrongFocus, field
            middle = QPointF(field.width() / 2, field.height() / 2)
            wheel = QWheelEvent(middle, QPointF(field.mapToGlobal(middle.toPoint())), QPoint(),
                                QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            QtWidgets.QApplication.sendEvent(field, wheel)
            app.processEvents()
        assert options == ThermalOptions() and heard == [], (options, heard)
    ''')


def test_the_tree_verdict_counts_the_board_temperature():
    run_in_window('''
        from coppernicus.results import TreeResult
        from coppernicus.temperature import NOT_SOLVED
        window.tree = TreeResult(thermal_limits={"max_rise_c": 15.0})
        text = window._tree_verdict()
        assert "not judged" in text and "board temperature (max_rise_c)" in text, text
        assert NOT_SOLVED in text, text
    ''')


def test_a_cancelled_tree_solve_pops_no_box_and_clears_the_temperature_tab():
    run_in_window('''
        boxes = []
        QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a))
        worker = window.tree_worker = RunningWorker()
        worker.cancelled = True
        worker.failed.connect(window._tree_failed)
        window.thermal_panel.set_solving(True)
        worker.failed.emit("Solve cancelled")
        app.processEvents()
        assert boxes == [], boxes
        status = window.statusBar().currentMessage()
        assert "cancelled" in status, status
        assert "cancelled" in window.thermal_panel.summary.text(), window.thermal_panel.summary.text()
        assert not window.thermal_panel.cancel.isEnabled()
    ''')
