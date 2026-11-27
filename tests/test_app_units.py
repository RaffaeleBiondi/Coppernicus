"""D5: every numeric field shows its unit and takes a number with it or without it. A number
typed alone is in the unit the field's label names ("500" in "fsw (kHz)" is 500 kHz); with an
SI prefix or the unit it means what it says ("0.5 MHz", "500k", "500 kHz" are the same)."""

import os

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app.units import Unit  # noqa: E402

PDN = os.path.join(os.path.dirname(__file__), "..", "examples", "pdn", "pdn.coppernicus.yml")


def test_a_number_alone_is_in_the_labels_unit_and_a_prefix_means_what_it_says():
    khz = Unit("Hz", 3)
    for text in ("500", "500k", "500 kHz", "0.5 MHz", "0,5M", "500000 Hz"):
        assert khz.parse(text) == 5e5, text
    assert khz.text(5e5) == "500 kHz"
    mv = Unit("V", -3, stored=-3)                  # a limit the project holds in mV
    assert mv.parse("50") == 50.0 and mv.parse("0.05 V") == 50.0 and mv.parse("50 mV") == 50.0
    assert mv.text(50.0) == "50 mV"
    nc = Unit("C", -9, stored=-9)                  # Qrr, held in nC
    assert nc.parse("5") == 5.0 and nc.parse("0.005 µC") == 5.0 and nc.text(5.0) == "5 nC"
    with pytest.raises(ValueError):
        khz.parse("5 Ω")                            # another unit is refused, never converted


def test_an_si_prefix_is_picked_for_each_value_where_the_label_names_the_unit_alone():
    amp, ohm = Unit("A", None), Unit("Ω", None)
    assert amp.text(0.005) == "5 mA" and amp.parse("5m") == 0.005 and amp.parse("0.005") == 0.005
    assert ohm.text(1e6) == "1 MΩ" and ohm.text(33.0) == "33 Ω" and ohm.parse("1 Mohm") == 1e6


def test_units_without_prefixes_take_the_number_with_or_without_the_unit():
    for unit, text, value, shown in ((Unit("%"), "3%", 3.0, "3 %"),
                                     (Unit("A/mm²"), "60 A/mm2", 60.0, "60 A/mm²"),
                                     (Unit("°C"), "85 C", 85.0, "85 °C"),
                                     (Unit("mm"), "0,035 mm", 0.035, "0.035 mm"),
                                     (Unit("µm"), "25 um", 25.0, "25 µm")):
        assert unit.parse(text) == value and unit.parse(str(value)) == value, text
        assert unit.text(value) == shown
    with pytest.raises(ValueError):
        Unit("mm").parse("35 µm")


def test_a_field_shows_every_digit_it_reads_back():
    for unit, x in ((Unit("mm"), 0.0350001234), (Unit("Hz", 3), 123456.789), (Unit("A", None), 1 / 3)):
        assert unit.parse(unit.exact(x)) == x, (unit, x)


def test_the_fields_show_their_units_and_take_a_typed_one():
    run_in_window(f'''
        from coppernicus.app.units import UnitEdit
        rp = window.rail_panel
        window.open_project({PDN!r})
        window.nets.select("+3V3")
        app.processEvents()
        assert all(isinstance(e, UnitEdit) for e in (rp.max_drop, rp.max_j, rp.max_via, rp.max_rise,
                                                     rp.plating, rp.min_edge))
        rp.max_drop.setText("2.5%")
        rp.max_drop.editingFinished.emit()
        assert rp.max_drop.text() == "2.5 %" and window.project.rail("+3V3").limits.max_drop_pct == 2.5
        rp.max_via.setText("500m")
        rp.max_via.editingFinished.emit()
        assert rp.max_via.text() == "500 mA" and window.project.rail("+3V3").limits.max_via_current == 0.5
        rp.plating.setText("25")
        rp.plating.editingFinished.emit()
        assert rp.plating.text() == "25 µm" and window.project.settings.via_plating_um == 25.0
        th = window.thermal_panel
        th.max_temp.setText("85")
        th.max_temp.editingFinished.emit()
        assert th.max_temp.text() == "85 °C" and window.project.thermal.max_temp_c == 85.0
    ''')
