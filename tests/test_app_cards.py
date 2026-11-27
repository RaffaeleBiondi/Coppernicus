"""The verdict line and cards on the Impedance and Ripple & noise pages, and the mark of a result
from before a setup change: its badge and cards outlined dashed in the verdict's colour (never
a plain verdict), "solve again" in the tooltip."""

import os
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app.verdict import ac_cards, badge, noise_cards  # noqa: E402

PDN = os.path.join(os.path.dirname(__file__), "..", "examples", "pdn", "pdn.coppernicus.yml")


def probe(name, passed, worst, ratio, mag=(0.02, 30.0)):
    return SimpleNamespace(name=name, passed=passed, worst_z_ohm=worst, worst_hz=1.001e5, worst_ratio=ratio,
                           mag_ohm=np.array(mag), target_ohm=0.05, target_hz=1e7, target_points=[],
                           has_target=True, ac_tolerance_v=None, step_a=None, slew_a_per_us=None)


def test_the_impedance_cards_show_the_failing_probe_first():
    r = SimpleNamespace(probes=[probe("U2", True, 0.03, 0.6), probe("U1", False, 0.1598, 3.2)])
    cards = ac_cards(r)
    assert [c.key for c in cards] == ["Worst |Z|, U1", "Target, U1", "Lowest |Z|", "Highest |Z|"]
    assert cards[0].state == "fail" and "3.2× the target" in cards[0].detail
    assert all(c.state == "" for c in cards[1:])
    assert ac_cards(SimpleNamespace(probes=[])) == []


def test_the_noise_cards_judge_only_the_victims():
    victim = SimpleNamespace(name="SIG_LOOP", ratio=1.31, peak_v=0.6554, margin_v=0.5, passed=False)
    ripple = SimpleNamespace(name="LOAD", pp_bw_v=0.01173)
    loop = SimpleNamespace(phase=1, loop_l_h=2.07e-9, f_ring_hz=2.005e8)
    emi = SimpleNamespace(radiated_dbuv_m=22.2, radiated_hz=2.02e8, distance_m=3.0)
    cards = noise_cards(SimpleNamespace(victims=[victim], ripple=[ripple], hot_loops=[loop], emi=emi))
    assert [c.key for c in cards] == ["Worst victim", "Ripple at LOAD", "Hot loop, phase 1",
                                      "Radiated estimate"]
    assert cards[0].state == "fail" and "131 % of its 500 mV margin" in cards[0].detail
    assert all(c.state == "" for c in cards[1:])                   # never judged: never a border
    assert "never judged" in cards[1].detail and "never judged" in cards[3].detail


def test_a_stale_badge_keeps_its_verdict_and_carries_the_mark():
    assert badge("fail", stale=True) == ("fail", "FAIL", True)
    assert badge("fail") == ("fail", "FAIL") and badge(None, stale=True) == ("ns", "not solved")


def test_a_result_from_before_a_setup_change_is_marked_on_the_page_and_in_the_navigator():
    run_in_window(f'''
        from coppernicus.app import ac_panel as acm
        from coppernicus.app.verdict import STALE_TIP
        from coppernicus.engine.ac import solve_ac_rail
        window.show()
        window.open_project({PDN!r})
        window.nets.select("+3V3")
        app.processEvents()
        p = window.ac_panel
        res = solve_ac_rail(window.board, window.merged.project, "+3V3")
        p.results["+3V3"] = (res, acm._project_key(p.merged_project))
        window.results_tabs.setCurrentWidget(p)
        p.show_result()
        window.refresh_badges()
        head = p.verdict
        assert head.badge.text() == "FAIL" and head.badge.property("stale") == ""
        assert [c.key for c in head.shown_cards()][0] == "Worst |Z|, U1"
        assert window.navigator.badge_of(p) == ("fail", "FAIL")
        p.bandwidth.setText("200k")                                  # the setup changes
        p.bandwidth.editingFinished.emit()
        app.processEvents()
        window.refresh_badges()
        assert head.badge.property("stale") == "true" and head.badge.toolTip() == STALE_TIP
        assert all(c.property("stale") == "true" for c in head.cards if not c.isHidden())
        assert window.navigator.badge_of(p) == ("fail", "FAIL", True)
        assert window.navigator.item_of(p).toolTip(0) == STALE_TIP
    ''')
