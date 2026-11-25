"""The Impedance tab (Task 9b): the editor's form <-> project mapping, the pre-solve estimate
text, the stats and results rows (pure logic, no window); and, in a child process, what
only a window shows (the tab, the close and pick routes). No AC solve runs here: the solves
are in tests/test_ac.py and tools/gui_check.py (run_ac)."""

import dataclasses
import math
import os
import subprocess
import sys
import textwrap

import numpy as np
import pytest

pytest.importorskip("PySide6")

from coppernicus.project import (  # noqa: E402
    AcOptions,
    CapModel,
    Link,
    Probe,
    Project,
    ProjectError,
    Rail,
    Source,
    check_ac,
)

# ---------------------------------------------------------------------- the file's own checks


def test_check_ac_runs_the_project_files_checks():
    check_ac([Probe("U1", "U1", "+3V3", ["1"], ground_net="GND")], [CapModel("10u", refs=["C1"])])
    with pytest.raises(ProjectError, match="target_points"):
        check_ac([Probe("U1", "U1", "+3V3", target_ohm=0.1, target_points=[(1e6, 0.1)])], [])
    with pytest.raises(ProjectError, match="unique"):
        check_ac([Probe("U1", "U1", "+3V3"), Probe("U1", "U2", "+3V3")], [])
    with pytest.raises(ProjectError, match="C1"):
        check_ac([], [CapModel("a", refs=["C1"]), CapModel("b", refs=["C1"])])
    with pytest.raises(ProjectError, match="f_stop_hz"):
        check_ac([], [], AcOptions(f_start_hz=1e6, f_stop_hz=1e5))


# ---------------------------------------------------------------------- numbers in the cells

def test_si_text_and_parse_si_read_back_what_they_show():
    from coppernicus.app.ac_panel import parse_si, si_text
    assert parse_si("") is None and parse_si("  ") is None
    for text, value in (("50m", 0.05), ("10M", 1e7), ("10u", 1e-5), ("10µ", 1e-5), ("0.5n", 5e-10),
                        ("100k", 1e5), ("1e-6", 1e-6), ("3,3", 3.3), ("2", 2.0), ("4.7p", 4.7e-12),
                        ("1G", 1e9)):
        assert parse_si(text) == pytest.approx(value), text
    for value in (0.05, 1e7, 1e-5, 5e-10, 1e5, 2.0, 0.2, 300e6, 1.5):
        assert parse_si(si_text(value)) == pytest.approx(value), si_text(value)
    assert si_text(None) == "" and si_text(0.05) == "50m" and si_text(1e7) == "10M"
    for bad in ("abc", "1x", "m", "nan", "inf"):
        with pytest.raises(ValueError):
            parse_si(bad)


def test_a_mask_is_written_and_read_as_hz_ohm_pairs():
    from coppernicus.app.ac_panel import mask_text, parse_mask
    pts = [(1e5, 0.05), (1e7, 0.05), (1e8, 0.5)]
    assert mask_text(pts) == "100k 50m; 10M 50m; 100M 500m"
    assert parse_mask(mask_text(pts)) == pts
    assert parse_mask("") == [] and mask_text([]) == ""
    with pytest.raises(ValueError, match="pairs"):
        parse_mask("100k; 10M 50m")


# ---------------------------------------------------------------------- probe rows

def test_an_untouched_probe_keeps_every_field_exactly():
    """Filling a row and editing one cell changes that field only: no other value goes
    through the text (a 4-digit cell would round 1.23456e-2)."""
    from coppernicus.app.ac_panel import PROBE_COLUMNS, probe_cell, with_probe_cell
    p = Probe("U1", "U1", "+3V3", ["1"], "", ["2"], "GND", target_ohm=0.0123456, target_hz=1.23456e7)
    cells = [probe_cell(p, key) for _h, key in PROBE_COLUMNS]
    assert cells[:7] == ["U1", "U1", "+3V3", "1", "", "GND", "2"]
    q = with_probe_cell(p, "step_a", "1.5")
    assert q.step_a == 1.5 and q.target_ohm == 0.0123456 and q.target_hz == 1.23456e7
    assert dataclasses.replace(q, step_a=None) == p and p.step_a is None      # p itself unchanged


def test_probe_cells_map_to_the_file_fields():
    from coppernicus.app.ac_panel import probe_cell, with_probe_cell
    p = Probe("", "", "")
    p = with_probe_cell(p, "name", " U7 ")
    p = with_probe_cell(p, "ref", "U7")
    p = with_probe_cell(p, "net", "+1V8")
    p = with_probe_cell(p, "pins", "A1, B2")
    p = with_probe_cell(p, "ground_net", "GND")
    p = with_probe_cell(p, "ground_pins", "all")
    p = with_probe_cell(p, "target_ohm", "20m")
    p = with_probe_cell(p, "target_hz", "50M")
    assert p == Probe("U7", "U7", "+1V8", ["A1", "B2"], "", None, "GND", target_ohm=0.02, target_hz=5e7)
    assert probe_cell(p, "pins") == "A1, B2" and probe_cell(p, "ground_pins") == "all"
    assert with_probe_cell(p, "target_ohm", "").target_ohm is None
    masked = with_probe_cell(with_probe_cell(p, "target_ohm", ""), "target_points", "1M 10m; 100M 1")
    assert masked.target_points == [(1e6, 0.01), (1e8, 1.0)]
    computed = with_probe_cell(with_probe_cell(with_probe_cell(
        with_probe_cell(p, "target_ohm", ""), "ac_tolerance_v", "33m"), "step_a", "2"), "slew_a_per_us", "1")
    assert (computed.ac_tolerance_v, computed.step_a, computed.slew_a_per_us) == (0.033, 2.0, 1.0)


@pytest.mark.parametrize("key, text, words", [
    ("target_ohm", "-1", "target_ohm"),
    ("target_ohm", "abc", "Target"),
    ("target_hz", "0", "target_hz"),
    ("step_a", "x1", "Step"),
    ("target_points", "1M 10m", "target_ohm or target_points"),      # a mask beside a target
    ("slew_a_per_us", "0", "slew_a_per_us"),
])
def test_a_bad_probe_cell_is_refused_with_the_files_reason(key, text, words):
    from coppernicus.app.ac_panel import with_probe_cell
    p = Probe("U1", "U1", "+3V3", ["1"], ground_net="GND", target_ohm=0.05)
    with pytest.raises(ValueError, match=words):
        with_probe_cell(p, key, text)


def test_clearing_a_name_part_or_net_makes_the_row_a_draft():
    from coppernicus.app.ac_panel import is_draft, with_probe_cell
    p = Probe("U1", "U1", "+3V3", ["1"], ground_net="GND", target_ohm=0.05)
    for key in ("name", "ref", "net"):
        assert is_draft(with_probe_cell(p, key, "")) and not is_draft(p)


def test_a_draft_probe_is_checked_for_its_values_but_not_written():
    from coppernicus.app.ac_panel import is_draft, usable_probes, with_probe_cell
    draft = with_probe_cell(Probe("", "", ""), "target_ohm", "50m")       # no part or net yet
    assert draft.target_ohm == 0.05 and is_draft(draft)
    with pytest.raises(ValueError, match="target_ohm"):
        with_probe_cell(Probe("", "", ""), "target_ohm", "-1")
    done = Probe("U1", "U1", "+3V3")
    twin = Probe("U1", "U2", "+3V3")
    probes, problems = usable_probes([draft, done, twin])
    assert probes == [done] and set(problems) == {0, 2}
    assert "draft" in problems[0] and "unique" in problems[2]


# ---------------------------------------------------------------------- capacitor rows

def test_capacitor_cells_map_to_the_file_fields_and_keep_untouched_ones():
    from coppernicus.app.ac_panel import CAP_COLUMNS, cap_cell, usable_caps, with_cap_cell
    m = CapModel("10u", 1.23456e-5, 5e-10, 0.02, ["C1", "C2"])
    cells = [cap_cell(m, key) for _h, key in CAP_COLUMNS]
    assert cells == ["10u", "12.35µ", "500p", "20m", "C1, C2", "", "", ""]
    q = with_cap_cell(m, "rated_v", "6.3")
    assert q.rated_v == 6.3 and q.capacitance_f == 1.23456e-5
    q = with_cap_cell(q, "dielectric", "X7R")
    q = with_cap_cell(q, "kept_at_rated_pct", "35")
    q = with_cap_cell(q, "refs", "C1 C3,C4")
    assert (q.dielectric, q.kept_at_rated_pct, q.refs) == ("X7R", 35.0, ["C1", "C3", "C4"])
    empty = with_cap_cell(CapModel("new"), "esl_h", "")
    assert empty.esl_h == 0.0 and cap_cell(empty, "esl_h") == ""         # 0: not entered
    with pytest.raises(ValueError, match="kept_at_rated_pct"):
        with_cap_cell(m, "kept_at_rated_pct", "120")
    with pytest.raises(ValueError, match="ESL"):
        with_cap_cell(m, "esl_h", "1q")
    models, problems = usable_caps([m, CapModel("", 1e-6), CapModel("x", refs=["C2"])])
    assert models == [m] and "draft" in problems[1] and "C2" in problems[2]


# ---------------------------------------------------------------------- rails, bandwidths, links

def _project():
    return Project(rails=[Rail("+3V3", sources=[Source("U1", 3.3, ["1"], ground_net="GND")]),
                          Rail("+5V", sources=[Source("J1", 5.0, name="Supply")]),
                          Rail("+1V8")],
                   links=[Link("Ferrite", "FB1", ["1"], "+3V3", ["2"], "+3V3_F", 0.1)],
                   probes=[Probe("U2", "U2", "+3V3_F"), Probe("U9", "U9", "+9V")])


def test_the_rails_offered_are_those_with_a_source_then_the_probe_nets():
    from coppernicus.app.ac_panel import rail_source, solvable_rails
    p = _project()
    assert solvable_rails(p) == ["+3V3", "+5V", "+3V3_F", "+9V"]
    assert rail_source(p, "+3V3").ref == "U1" and rail_source(p, "+3V3_F").ref == "U1"
    assert rail_source(p, "+5V").label == "Supply" and rail_source(p, "+9V") is None


def test_a_bandwidth_is_written_under_its_existing_key_else_the_sources_name():
    from coppernicus.app.ac_panel import bandwidth_of, set_bandwidth
    p = _project()
    supply = p.rails[1].sources[0]
    set_bandwidth(p.ac, supply, 2e5)
    assert p.ac.bandwidth_hz == {"Supply": 2e5} and bandwidth_of(p.ac, supply) == 2e5
    p.ac.bandwidth_hz = {"J1": 1e5}
    set_bandwidth(p.ac, supply, 3e5)
    assert p.ac.bandwidth_hz == {"J1": 3e5}
    set_bandwidth(p.ac, supply, None)
    assert p.ac.bandwidth_hz == {} and bandwidth_of(p.ac, supply) is None


def test_a_link_inductance_is_written_under_its_existing_key_else_its_ref():
    from coppernicus.app.ac_panel import link_esl_of, set_link_esl
    p = _project()
    k = p.links[0]
    set_link_esl(p.ac, k, 1e-6)
    assert p.ac.link_esl_h == {"FB1": 1e-6} and link_esl_of(p.ac, k) == 1e-6
    p.ac.link_esl_h = {"Ferrite": 2e-6}
    set_link_esl(p.ac, k, 3e-6)
    assert p.ac.link_esl_h == {"Ferrite": 3e-6}
    set_link_esl(p.ac, k, None)
    assert p.ac.link_esl_h == {}


def test_the_quick_preset_is_fewer_copper_solves_and_only_below_the_sweep_points():
    from coppernicus.app.ac_panel import QUICK_FIELD_POINTS, quick_preset
    a = AcOptions()
    assert a.field_points_per_decade == 20 and QUICK_FIELD_POINTS == 10
    b = quick_preset(a)
    assert b.field_points_per_decade == 10 and a.field_points_per_decade == 20
    assert dataclasses.replace(b, field_points_per_decade=20) == a
    with pytest.raises(ValueError, match="points per decade"):
        quick_preset(AcOptions(points_per_decade=10))        # KiPI would solve every point


# ---------------------------------------------------------------------- estimate, stats, rows

def test_the_estimate_text_joins_the_cost_and_the_memory():
    from coppernicus.app.ac_panel import estimate_text, may_start
    from coppernicus.engine.ac import cost_estimate, ram_estimate
    cost = cost_estimate(20_000, 2, 111)
    ram = ram_estimate(20_000, 64e9)
    text = estimate_text(cost, ram)
    assert "20,000 triangles × 2 ports" in text and "111 frequencies" in text and "64 GB" in text
    risky = ram_estimate(3_000_000, 8e9)
    assert risky.risk and "could exceed" in estimate_text(cost, risky)
    assert may_start(ram, False) and may_start(risky, True) and not may_start(risky, False)


def test_stats_text_names_the_memory_peak_and_whether_this_solve_raised_it():
    from coppernicus.app.ac_panel import stats_text
    st = {"seconds": 4.21, "triangles": 9876.0, "unknowns": 9800.0, "ports": 2.0, "method": "superlu",
          "field_frequencies": 111.0, "memory_peak_mb": 812.4, "memory_peak_raised": True}
    text = stats_text(st)
    assert "4.2 s" in text and "9,876 triangles" in text and "2 ports" in text and "111" in text
    assert "812 MB" in text and "this solve raised" in text
    st["memory_peak_raised"] = False
    assert "stayed at or below" in stats_text(st)
    st["memory_peak_mb"] = None
    assert "cannot be read" in stats_text(st)


def _result(bandwidth=1e5, target=0.05, passed=False):
    from coppernicus.results import ImpedanceResult, ProbeImpedance
    f = np.logspace(3, 8, 6)
    z = np.array([1.0, 0.2, 0.1, 0.02, 0.05, 0.3], complex)
    p = ProbeImpedance("U1", "U1", ["1"], "+3V3", "GND", z, target_ohm=target, target_hz=1e7,
                       worst_z_ohm=0.1, worst_hz=1e5, worst_ratio=2.0, passed=passed)
    q = ProbeImpedance("U2", "U2", None, "+3V3", "GND", z)
    return ImpedanceResult("+3V3", ["+3V3"], "GND", "U1", bandwidth, f, f, [p, q])


def test_result_rows_give_each_probes_worst_point_ratio_and_verdict():
    from coppernicus.app.ac_panel import RESULT_HEADERS, result_rows
    rows = result_rows(_result())
    assert len(RESULT_HEADERS) == len(rows[0]) == 10
    assert rows[0][:2] == ("U1", "+3V3") and rows[0][2] == "20 mΩ" and rows[0][3] == "1 Ω"
    assert rows[0][4:7] == ("100 mΩ", "100 kHz", "2") and rows[0][7] == "50 mΩ up to 10 MHz"
    assert rows[0][8] == "FAIL" and rows[1][8] == "no target (ranked)" and rows[1][7] == "-"
    r = _result(passed=None)
    r.probes[0].not_judged = "no control loop bandwidth"
    assert result_rows(r)[0][8:] == ("not judged", "no control loop bandwidth")
    r.probes[0].passed = True
    assert result_rows(r)[0][8] == "pass"
    r.probes[0].worst_ratio = None
    assert result_rows(r)[0][6] == "-"
    assert math.isnan(float("nan"))


# ---------------------------------------------------------------------- the window (child process)

PRELUDE = '''
from PySide6 import QtCore, QtWidgets
from coppernicus.app.main_window import MainWindow

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
window = MainWindow()
window.dirty = False
'''


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", PRELUDE + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


PDN = os.path.join(os.path.dirname(__file__), "..", "examples", "pdn", "pdn.coppernicus.yml")


def test_the_impedance_tab_follows_the_power_tree_and_opening_changes_nothing():
    run_in_window(f'''
        tabs = [window.results_tabs.tabText(i) for i in range(window.results_tabs.count())]
        assert tabs[tabs.index("Power tree") + 1] == "Impedance", tabs
        before = window.minimumSizeHint()
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        before_dict = window.project.to_dict()
        window.results_tabs.setCurrentWidget(panel)
        app.processEvents()
        assert panel.probes.rowCount() == 1 and panel.caps.rowCount() == 1
        assert panel.probes.item(0, 7).text() == "50m" and panel.caps.item(0, 4).text() == "C1"
        assert panel.rail_box.currentText() == "+3V3" and panel.bandwidth.text() == "100k"
        panel.bandwidth.editingFinished.emit()          # committed unchanged: no edit
        for w in panel.opt.values():
            if hasattr(w, "editingFinished"):
                w.editingFinished.emit()
        assert window.dirty is False and window.project.to_dict() == before_dict
        after = window.minimumSizeHint()
        assert after.height() <= max(before.height(), 600) and after.width() <= 1440, (before, after)
    ''')


def test_a_cell_edit_changes_that_field_only_and_marks_the_project_dirty():
    run_in_window(f'''
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        panel.probes.item(0, 11).setText("1.5")              # Step (A)
        p = window.project.probes[0]
        assert p.step_a == 1.5 and p.target_ohm == 0.05 and p.target_hz == 1e7 and window.dirty
        panel.probes.item(0, 7).setText("-1")                # refused, the project keeps 50 mΩ
        assert window.project.probes[0].target_ohm == 0.05 and "Not applied" in panel.status.text()
        panel.caps.item(0, 2).setText("1n")                  # ESL
        assert window.project.capacitors[0].esl_h == 1e-9
        assert window.project.capacitors[0].capacitance_f == 1e-5
        panel.set_bandwidth_text("")                          # no bandwidth: removed from ac:
        assert window.project.ac.bandwidth_hz == {{}}
        panel.use_quick_preset()
        assert window.project.ac.field_points_per_decade == 10
    ''')


def test_picking_fills_a_probe_and_toggles_a_models_part():
    run_in_window(f'''
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        window.dirty = False
        panel.add_probe()
        assert len(window.project.probes) == 1                # the new row is a draft
        panel.start_pick("supply")
        assert window.view.click_mode == "pick" and window.rail_panel.pick_kind == ""
        window.view.pad_picked.emit(25.0, 25.0)               # U1 pad 1 on +3V3
        panel.start_pick("ground")
        window.view.pad_picked.emit(26.2, 25.0)               # U1 pad 2 on GND
        p = panel._probe_rows[1]
        assert (p.ref, p.net, p.pins, p.ground_net, p.ground_pins) == ("U1", "+3V3", ["1"], "GND", ["2"]), p
        assert p.name == "U1" and "unique" in panel.status.text()      # a second U1: not written
        panel.probes.item(1, 0).setText("U1b")
        assert [q.name for q in window.project.probes] == ["U1", "U1b"]
        panel.start_pick("parts")
        panel.caps.setCurrentCell(0, 0)
        window.view.pad_picked.emit(25.0, 30.0)               # C1: leaves model 10u
        assert window.project.capacitors[0].refs == []
        window.view.pad_picked.emit(25.0, 30.0)
        assert window.project.capacitors[0].refs == ["C1"]
        window.stop_clicking()                                 # Esc
        assert panel.pick_kind == "" and window.view.click_mode is None
        panel.start_pick("supply")
        window.measure.setChecked(True)                        # another click mode takes the view
        assert panel.pick_kind == "" and window.view.click_mode == "measure"
        panel.start_pick("supply")
        window.rail_panel.set_pick("load")
        assert panel.pick_kind == "" and window.view.click_mode == "pick"
    ''')


def test_a_solve_that_could_exceed_the_memory_starts_only_on_yes():
    """The RAM question answered No (or closed): no solve starts and the panel is idle."""
    run_in_window(f'''
        import types
        from PySide6.QtWidgets import QMessageBox
        import coppernicus.engine.ac as engine_ac
        from coppernicus.engine.ac import AcSize, RamEstimate
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        asked = []
        QMessageBox.warning = staticmethod(lambda *a, **k: asked.append(a[1]) or QMessageBox.No)
        engine_ac.ram_estimate = lambda *a, **k: RamEstimate(9e12, 8e9, True, "up to 9000 GB")

        class Sizing(QtCore.QObject):
            sized = QtCore.Signal(object)
            generation = panel._generation

        fake = Sizing()
        fake.sized.connect(panel._sized)
        panel.worker, panel._busy = fake, True
        heard = []
        panel.estimated.connect(heard.append)
        merged = types.SimpleNamespace(project=window.project)
        fake.sized.emit((AcSize("+3V3", 1000, 500, 2, 111, 278, 0.1), merged))
        assert asked and panel.worker is fake and not panel.busy, (asked, panel.worker)
        assert heard and "could exceed" in heard[0] and "1,000 triangles" in heard[0]
        assert "Not solved" in panel.status.text() + window.statusBar().currentMessage()
    ''')


def test_closing_during_an_impedance_solve_leaves_none_of_its_signals_connected():
    run_in_window('''
        class RunningAc(QtCore.QObject):
            message = QtCore.Signal(str)
            fraction = QtCore.Signal(object)
            sized = QtCore.Signal(object)
            succeeded = QtCore.Signal(object)
            failed = QtCore.Signal(str)
            cancelled = QtCore.Signal()
            finished = QtCore.Signal()
            stopped = False

            def isRunning(self):
                return True

            def cancel(self):
                self.stopped = True

            def wait(self, _ms=None):
                return True

        panel = window.ac_panel
        worker = panel.worker = RunningAc()
        heard = []
        for signal in (worker.message, worker.fraction, worker.sized, worker.succeeded, worker.failed):
            signal.connect(heard.append)
        worker.finished.connect(lambda: heard.append("finished"))
        window.close()
        app.processEvents()
        worker.message.emit("Field solves")
        worker.failed.emit("boom")
        worker.cancelled.emit()
        worker.finished.emit()
        assert heard == [] and worker.stopped, heard
    ''')
