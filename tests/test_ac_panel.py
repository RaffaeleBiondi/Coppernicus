"""The Impedance tab (Task 9b): the editor's form <-> project mapping, the pre-solve estimate
text, the stats and results rows (pure logic, no window); and, in a child process, what
only a window shows (the tab, the close and pick routes). No AC solve runs here: the solves
are in tests/test_ac.py and tools/gui_check.py (run_ac)."""

import dataclasses
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
    with pytest.raises(ProjectError, match="ground net is its supply net"):
        check_ac([Probe("U1", "U1", "+3V3", ground_net="+3V3")], [])


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
    cleared = with_probe_cell(p, "target_ohm", "")
    assert cleared.target_ohm is None and cleared.target_hz is None     # its "up to" goes with it
    with pytest.raises(ValueError, match="target_hz without target_ohm"):
        with_probe_cell(cleared, "target_hz", "50M")
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
        assert tabs[tabs.index("Temperature") + 1] == "Impedance", tabs       # after the power tree's pages
        bar = window.findChild(QtWidgets.QToolBar, "Main")
        act = next(a for a in bar.actions() if a.text() == "Solve impedance")
        assert act.shortcut().toString() == "Ctrl+I"
        act.trigger()                                         # no board yet: says so, shows the tab
        assert window.results_tabs.currentWidget() is window.ac_panel
        assert "Open a board" in window.statusBar().currentMessage()
        window.show()                                         # sizes are laid out only when shown
        app.processEvents()
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
        with_tab = window.minimumSizeHint()                  # the laptop fit: the tab grows nothing
        window.results_tabs.removeTab(window.results_tabs.indexOf(panel))
        app.processEvents()
        without = window.minimumSizeHint()
        assert with_tab.height() <= without.height() and with_tab.width() <= without.width(), (
            with_tab, without)
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
        assert p.name == "U1_2" and [q.name for q in window.project.probes] == ["U1", "U1_2"]
        panel.probes.item(1, 0).setText("U1b")                # a picked probe never takes a name in use
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
        panel._solving = ("+3V3", {{}}, window.merged)
        fake.sized.emit(AcSize("+3V3", 1000, 500, 2, 111, 278, 0.1))
        assert asked and panel.worker is None and not panel.busy, (asked, panel.worker)   # no solve
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


def test_a_result_goes_stale_on_a_setup_edit_not_on_a_save_or_a_marked_net():
    """Saving rewrites the board's path relative to the file, and marking a net as a rail
    changes no solve: neither flags the shown result; any other edit does."""
    from coppernicus.app.ac_panel import _project_key
    p = _project()
    p.board = "/abs/b.kipi.json.gz"
    key = _project_key(p)
    p.board, p.marked_rails = "b.kipi.json.gz", ["+1V8"]
    p.rails[0].limits.max_drop_pct = 1.0                   # a DC limit: no AC solve reads it
    assert _project_key(p) == key
    p.probes[0].target_ohm = 0.05
    assert _project_key(p) != key


def test_a_noise_edit_does_not_flag_an_impedance_result():
    """No AC solve reads the aggressors, victims or noise options."""
    from coppernicus.app.ac_panel import _project_key
    from coppernicus.project import Aggressor, Victim
    p = _project()
    key = _project_key(p)
    p.aggressors = [Aggressor("U1", vrm="U1", fsw_hz=1e6)]
    p.victims = [Victim("SIG", preset="cmos", noise_margin_v=0.3)]
    p.noise.field_per_decade = 5
    assert _project_key(p) == key


FIELDS_BOARD = os.path.join(os.path.dirname(__file__), "..", "examples", "fields", "fields.kicad_pcb")


def test_an_ac_edit_reaches_the_next_solve_through_the_merged_project(tmp_path):
    """Edits write the user's project and merge again; Solve takes a copy of the window's
    merged project: on examples/pdn (no KiCad fields) and on a board whose rail is only in its
    KiCad footprint fields (examples/fields), where the rail and its source come from the
    fields and the probe, the model and the bandwidth from the editor. A field that cannot be
    used refuses the solve, as F5 does. No solve runs: the worker is never started."""
    path = Project(board=os.path.abspath(FIELDS_BOARD)).save(tmp_path / "fields-ac.coppernicus.yml")
    run_in_window(f'''
        from PySide6.QtWidgets import QMessageBox
        import coppernicus.app.ac_panel as ac_panel
        started, boxes = [], []
        ac_panel.AcWorker.start = lambda self: started.append(self)
        QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[1]) or QMessageBox.Ok)
        panel = window.ac_panel

        window.open_project({PDN!r})
        app.processEvents()
        panel.probes.item(0, 7).setText("40m")                 # target
        panel.caps.item(0, 2).setText("1n")                    # ESL
        panel.set_bandwidth_text("200k")
        panel.solve()
        solved = started[-1].merged.project
        assert solved is not window.project and solved is not window.merged.project   # a copy
        assert solved.probes[0].target_ohm == 0.04 and solved.capacitors[0].esl_h == 1e-9
        assert solved.ac.bandwidth_hz == {{"U1": 2e5}}, solved.ac.bandwidth_hz

        window.dirty = False
        window.open_project({str(path)!r})
        app.processEvents()
        assert window.project.rails == [] and window.merged.project.rail("VCC").sources
        assert [panel.rail_box.itemText(i) for i in range(panel.rail_box.count())] == ["VCC"]
        assert panel.bandwidth.isEnabled() and "J1" in panel.source_label.text()
        panel.add_probe()
        for col, text in ((0, "U1"), (1, "U1"), (2, "VCC"), (5, "GND")):
            panel.probes.item(0, col).setText(text)
        panel.set_bandwidth_text("100k")
        assert [p.name for p in window.merged.project.probes] == ["U1"]    # merged again
        panel.solve()
        solved = started[-1].merged.project
        assert solved.rail("VCC").sources[0].ref == "J1" and [p.name for p in solved.probes] == ["U1"]
        assert solved.ac.bandwidth_hz == {{"J1": 1e5}} and window.project.rails == []

        panel._busy = False
        n = len(started)
        window.fields_error = "a field cannot be used"
        panel.solve()
        assert len(started) == n and boxes == ["Cannot solve the impedance"], boxes
    ''')



# ---------------------------------------------------------------------- review fixes (pure)

def test_a_cell_edit_that_conflicts_with_another_row_is_refused_with_the_files_reason():
    """Renaming a probe to a name in use, or giving a model a part another model has, would
    drop a saved probe or model at the next save: such an edit is refused instead."""
    from coppernicus.app.ac_panel import cap_row_conflict, probe_row_conflict, with_cap_cell, with_probe_cell
    probes = [Probe("U1", "U1", "+3V3"), Probe("U2", "U2", "+3V3"), Probe("", "", "")]
    assert probe_row_conflict(probes, 1, with_probe_cell(probes[1], "name", "U3")) is None
    why = probe_row_conflict(probes, 1, with_probe_cell(probes[1], "name", "U1"))
    assert why and "unique" in why and "U1" in why
    assert "unique" in probe_row_conflict(probes, 2, with_probe_cell(probes[2], "name", "U2"))  # a draft too
    models = [CapModel("a", refs=["C1"]), CapModel("b", refs=["C2"]), CapModel("")]
    why = cap_row_conflict(models, 1, with_cap_cell(models[1], "refs", "C2, C1"))
    assert why and "C1" in why and "two capacitor models" in why
    assert "unique" in cap_row_conflict(models, 1, with_cap_cell(models[1], "name", "a"))
    assert cap_row_conflict(models, 1, with_cap_cell(models[1], "refs", "C2, C3")) is None


def test_ground_pins_need_a_ground_net_and_clearing_it_or_the_target_says_what_went_with_it():
    from coppernicus.app.ac_panel import edit_note, with_probe_cell
    bare = Probe("U1", "U1", "+3V3", ["1"])
    for key, text in (("ground_ref", "J1"), ("ground_pins", "2")):
        with pytest.raises(ValueError, match="ground net first"):
            with_probe_cell(bare, key, text)
    assert with_probe_cell(bare, "ground_pins", "all").ground_pins is None     # nothing typed
    grounded = Probe("U1", "U1", "+3V3", ["1"], "J1", ["2"], "GND", target_ohm=0.05, target_hz=1e7)
    q = with_probe_cell(grounded, "ground_net", "")
    assert (q.ground_ref, q.ground_pins, q.ground_net) == ("", None, "")
    assert "Ground part" in edit_note(grounded, q, "ground_net") and "Ground pins" in edit_note(
        grounded, q, "ground_net")
    q = with_probe_cell(grounded, "target_ohm", "")
    assert "Up to" in edit_note(grounded, q, "target_ohm")
    assert edit_note(grounded, with_probe_cell(grounded, "step_a", "1"), "step_a") == ""
    with pytest.raises(ValueError, match="supply net"):
        with_probe_cell(grounded, "ground_net", "+3V3")


def test_the_export_name_is_the_clis():
    """cli.py: <project file stem, .coppernicus included>_<rail>_impedance.csv."""
    from coppernicus.app.ac_panel import export_name
    name = export_name("/x/pdn.coppernicus.yml", "pdn.kipi.json.gz", "+3V3")
    assert name == "pdn.coppernicus_P3V3_impedance.csv"
    assert export_name(None, "board.kicad_pcb", "-5V") == "board_N5V_impedance.csv"


def test_the_legend_stays_inside_a_narrow_plot():
    """The core's long band label, drawn in a narrow column, is wrapped, not cut off."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    from coppernicus.app.ac_panel import draw_result
    r = _result(bandwidth=None)
    r.assumed_bandwidth_hz = 5e4
    fig = Figure(figsize=(3.2, 2.6), dpi=100, layout="constrained")
    canvas = FigureCanvasAgg(fig)
    draw_result(fig, r)
    canvas.draw()
    box, page = fig.axes[0].get_legend().get_window_extent(), fig.bbox
    assert box.x0 >= page.x0 - 1 and box.x1 <= page.x1 + 1 and box.y0 >= page.y0 - 1, (box, page)


# ---------------------------------------------------------------------- review fixes (window)

STUB_WORKER = '''
import time
from PySide6.QtWidgets import QMessageBox
import shiboken6
import coppernicus.app.ac_panel as ac_panel
import coppernicus.engine.ac as engine_ac
from coppernicus.engine.ac import AcSize, RamEstimate


class StubWorker(QtCore.QThread):
    """AcWorker's signals and phases without KiPI: sizing reports a small mesh at once (or
    fails); solving runs until it is cancelled."""
    message = QtCore.Signal(str)
    fraction = QtCore.Signal(object)
    sized = QtCore.Signal(object)
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()
    fail_sizing = False

    def __init__(self, board, merged, rail, phase, parent=None):
        super().__init__(parent)
        self.merged, self.rail, self.phase, self.generation, self._cancel = merged, rail, phase, 0, False

    def cancel(self):
        self._cancel = True

    def run(self):
        if self.phase == "size":
            if StubWorker.fail_sizing:
                self.failed.emit("the mesher failed")
            else:
                self.sized.emit(AcSize(self.rail, 1000, 500, 2, 111, 278, 0.1))
            return
        while not self._cancel:
            time.sleep(0.01)
        self.cancelled.emit()


ac_panel.AcWorker = StubWorker


def pump(done, seconds=10.0):
    t0 = time.monotonic()
    while not done() and time.monotonic() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)
'''


def test_solve_anyway_answered_yes_solves_busy_with_cancel_and_one_solve_at_a_time():
    """The RAM question's own event loop delivers the sizing thread's end before Yes comes
    back: the solve that follows must still be busy, cancellable, and the only one."""
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        def nested_yes(*_a, **_k):
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(300, loop.quit)
            loop.exec()
            return QMessageBox.Yes

        QMessageBox.warning = staticmethod(nested_yes)
        engine_ac.ram_estimate = lambda *a, **k: RamEstimate(9e12, 8e9, True, "up to 9000 GB (forced)")
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        panel.solve()
        pump(lambda: panel.worker is not None and panel.worker.phase == "solve")
        solving = panel.worker
        assert solving is not None and solving.phase == "solve", solving
        pump(lambda: False, 0.3)                              # the sizing thread's end, delivered
        assert panel.busy and panel.cancel_button.isEnabled() and not panel.solve_button.isEnabled()
        panel.solve()
        assert panel.worker is solving and "already running" in panel.status.text()
        panel.cancel()
        pump(lambda: not panel.busy)
        assert not panel.busy and panel.solve_button.isEnabled() and panel.worker is None
        assert "cancelled" in panel.status.text(), panel.status.text()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        assert not shiboken6.isValid(solving)                 # finished workers are deleted
    '''))


def test_a_cancel_between_sizing_and_its_slot_is_kept_and_a_failed_sizing_clears_the_estimate():
    run_in_window(STUB_WORKER + textwrap.dedent(f'''
        boxes = []
        QMessageBox.warning = staticmethod(lambda *a, **k: boxes.append(a[1]) or QMessageBox.Ok)
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        panel.solve()
        panel.worker.wait()                                   # sized is queued, not yet delivered
        panel.cancel()
        pump(lambda: not panel.busy)
        assert panel.worker is None and not panel.busy, panel.worker
        assert "cancelled" in panel.status.text() and panel.estimate.text() == "", panel.estimate.text()
        StubWorker.fail_sizing = True
        panel.solve()
        pump(lambda: not panel.busy)
        assert boxes == ["Cannot solve the impedance"] and panel.estimate.text() == "", panel.estimate.text()
    '''))


def test_closing_waits_for_an_impedance_solve_that_ignores_cancel_for_a_while():
    """No 'QThread: Destroyed while thread is still running': the window waits for the end,
    saying so in the status bar."""
    run_in_window('''
        import time

        class Slow(QtCore.QThread):
            message = QtCore.Signal(str)
            fraction = QtCore.Signal(object)
            sized = QtCore.Signal(object)
            succeeded = QtCore.Signal(object)
            failed = QtCore.Signal(str)
            cancelled = QtCore.Signal()

            def cancel(self):
                pass                                           # ignored: KiPI between frequencies

            def run(self):
                time.sleep(5.5)

        heard = []
        window.statusBar().messageChanged.connect(heard.append)
        worker = window.ac_panel.worker = Slow()
        worker.start()
        window.close()
        assert not worker.isRunning(), "the window closed while the solve still ran"
        assert any("Finishing the impedance solve" in m for m in heard), heard
    ''')


def test_an_edit_that_conflicts_with_another_row_is_refused_and_both_rows_stay_saved():
    run_in_window(f'''
        window.open_project({PDN!r})
        app.processEvents()
        panel = window.ac_panel
        panel.add_probe()
        for col, text in ((0, "U2"), (1, "U1"), (2, "+3V3")):
            panel.probes.item(1, col).setText(text)
        assert [p.name for p in window.project.probes] == ["U1", "U2"]
        panel.probes.item(1, 0).setText("U1")                 # the name of the first probe
        assert [p.name for p in window.project.probes] == ["U1", "U2"], window.project.probes
        assert "Not applied" in panel.status.text() and "unique" in panel.status.text()
        panel.add_model()
        panel.caps.item(1, 0).setText("b")
        panel.caps.item(1, 4).setText("C1")                   # C1 is in model 10u
        caps = window.project.capacitors
        assert [(m.name, m.refs) for m in caps] == [("10u", ["C1"]), ("b", [])], caps
        assert "Not applied" in panel.status.text() and "C1" in panel.status.text()
    ''')


def test_links_from_kicad_fields_are_listed_and_take_a_series_inductance(tmp_path):
    tests = os.path.dirname(os.path.abspath(__file__))
    run_in_window(f'''
        import sys
        from pathlib import Path
        sys.path.insert(0, {tests!r})
        from kicad_fixture import write_board
        from coppernicus.project import Project
        folder = Path({str(tmp_path)!r})
        parts = [("J1", [("1", 0.0, 0.0, "VCC")], {{"PI_RAIL": "VCC", "PI_SOURCE": "1V"}}),
                 ("U1", [("1", 18.0, 0.0, "VCC")], {{"PI_RAIL": "VCC", "PI_LOAD": "1A"}}),
                 ("FB1", [("1", 19.5, 0.0, "VCC"), ("2", 20.5, 0.0, "VCC_F")], {{"PI_LINK": "50mOhm"}})]
        write_board(folder / "link.kicad_pcb", parts,
                    [("VCC", (0.0, 0.0), (19.5, 0.0), 0.5), ("VCC_F", (20.5, 0.0), (40.0, 0.0), 0.5)])
        path = Project(board="link.kicad_pcb").save(folder / "link.coppernicus.yml")
        window.open_project(path)
        app.processEvents()
        panel = window.ac_panel
        assert window.project.links == [] and [k.ref for k in window.merged.project.links] == ["FB1"]
        assert panel.links.rowCount() == 1 and "FB1" in panel.links.item(0, 0).text()
        panel.links.item(0, 2).setText("1u")
        assert window.project.ac.link_esl_h == {{"FB1": 1e-6}}, window.project.ac.link_esl_h
        assert window.merged.project.ac.link_esl_h == {{"FB1": 1e-6}} and window.dirty
    ''')


# ---------------------------------------------------------------------- Q9 follows the values now

TOLERANCE = os.path.join(os.path.dirname(__file__), "..", "examples", "tolerance",
                         "tolerance.coppernicus.yml")


def test_the_q9_warning_shown_follows_a_tolerance_edit_and_the_result_stays():
    """Task 11 (Q9): the AC result warns when a load's AC tolerance and its probe's
    ac_tolerance_v disagree. A tolerance changes no |Z|, so a Tolerance-tab edit keeps the
    result (never stale); the warning shown is the one for the values now, without solving
    again, and the held result (which the Tolerance tab reads too) keeps no solve-time copy.
    The result is the engine's shape, built here: no AC solve runs."""
    run_in_window(f'''
        import copy
        import numpy as np
        from coppernicus.app.ac_panel import _project_key
        from coppernicus.engine.tolerance import load_probes, mismatch_warnings
        from coppernicus.results import ImpedanceResult, ProbeImpedance
        window.show()
        window.open_project({TOLERANCE!r})
        app.processEvents()
        panel = window.ac_panel
        panel.probes.item(0, 10).setText("40m")        # LOAD's ac_tolerance_v; U2's ac_tol_mv is 50
        merged = window.merged
        pairs = load_probes(window.board, merged.project)
        q9 = mismatch_warnings(merged.project, pairs, {{name: 1.8 for name in pairs}}, ["LOAD"])
        assert len(q9) == 1 and "ac_tol_mv ±50 mV" in q9[0], q9
        f = np.logspace(3, 8, 6)
        z = np.array([1.0, 0.2, 0.1, 0.02, 0.05, 0.3], complex)
        res = ImpedanceResult("VOUT", ["VOUT"], "GND", "U1", 1e5, f, f,
                              [ProbeImpedance("LOAD", "U2", ["1"], "VOUT", "GND", z)],
                              stats={{"seconds": 1.0, "triangles": 100}}, warnings=["another warning"] + q9,
                              ac_tolerance_warnings=list(q9))     # the engine keeps them apart too

        class Done(QtCore.QObject):
            succeeded = QtCore.Signal(object)
            generation = panel._generation

        done = Done()
        done.succeeded.connect(panel._solved)
        panel.worker = done
        panel._solving = ("VOUT", _project_key(merged.project), copy.deepcopy(merged))
        done.succeeded.emit(res)
        app.processEvents()
        notes = panel.notes.toPlainText()
        assert notes.count("disagree") == 1 and "ac_tol_mv ±50 mV" in notes and "another warning" in notes
        assert not [w for w in panel.results["VOUT"][0].warnings if "disagree" in w]   # held: none

        tol = window.tolerance_panel
        window.results_tabs.setCurrentWidget(tol)
        app.processEvents()
        tol.set_cell("load", "U2", "ac_tol_mv", "40")   # agrees with the probe's 40 mV now
        app.processEvents()
        notes = panel.notes.toPlainText()
        assert "disagree" not in notes and "another warning" in notes, notes
        assert panel.results["VOUT"][0] is res and not panel.stale
        tol.set_cell("load", "U2", "ac_tol_mv", "45")
        app.processEvents()
        notes = panel.notes.toPlainText()
        assert notes.count("disagree") == 1 and "ac_tol_mv ±45 mV" in notes, notes
        assert "ac_tol_mv ±50 mV" not in notes and not panel.stale
        window.results_tabs.setCurrentWidget(panel)    # shown again: the same
        app.processEvents()
        assert panel.notes.toPlainText().count("disagree") == 1 and "Setup changed" not in panel.header.text()
        panel.probes.item(0, 10).setText("45m")        # the probe's: the target changes, so stale
        app.processEvents()
        assert "disagree" not in panel.notes.toPlainText() and panel.stale
    ''')


# ---------------------------------------------------------------------- models from KiCad fields

CAP_FIELDS = {"C1": {"PI_ESR": "5m", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "X7R", "PI_VRATED": "16V"},
              "C2": {"PI_C": "100nF", "PI_ESR": "20mOhm", "PI_ESL": "0.3nH", "PI_DIELECTRIC": "C0G"},
              "C3": {"PI_ESR": "5m"},                                     # partial: no model
              "C5": {"PI_C": "22u", "PI_ESR": "50m", "PI_ESL": "1nH", "PI_DIELECTRIC": "TANT",
                     "PI_VRATED": "10V"}}


def _field_merge(caps):
    from coppernicus.board_fields import merge
    return merge(Project(capacitors=caps), CAP_FIELDS, values={"C1": "10u"}, farads={"C1": 10e-6})


def test_the_kicad_fields_models_are_found_with_the_fields_that_give_them():
    """What is solved: the merged project's capacitor models that came from KiCad fields (by
    the merge's own record, not by their name: it renames one on a clash), and the parts a
    project model names that went to them, warned about or not."""
    from coppernicus.app.ac_panel import field_models, lost_parts
    from coppernicus.board_fields import Merged
    own = [CapModel("bulk", 10e-6, 0.5e-9, 0.01, ["C1", "C4"])]
    merged = _field_merge(own)
    assert [(m.name, m.refs, c.ref) for m, c in field_models(merged)] == [
        ("C1 (KiCad)", ["C1"], "C1"), ("C2 (KiCad)", ["C2"], "C2"), ("C5 (KiCad)", ["C5"], "C5")]
    assert lost_parts(own, merged) == {0: [("C1", "C1 (KiCad)")]}
    same = [CapModel("bulk", 10e-6, 0.5e-9, 0.005, ["C1"], rated_v=16.0, dielectric="X7R")]
    quiet = _field_merge(same)
    assert not [w for w in quiet.warnings if "bulk" in w]                  # the same values: no warning
    assert lost_parts(same, quiet) == {0: [("C1", "C1 (KiCad)")]}          # ... and still lost
    kept = [CapModel("bulk", 1e-6, 1e-9, 0.01, ["C1"], override_board_fields=True)]
    merged = _field_merge(kept)
    assert lost_parts(kept, merged) == {} and [m.name for m, _c in field_models(merged)] == [
        "C2 (KiCad)", "C5 (KiCad)"]
    clash = [CapModel("C2 (KiCad)", 1e-6, 1e-9, 0.01, ["C9"])]
    assert [m.name for m, _c in field_models(_field_merge(clash))] == [
        "C1 (KiCad)", "C2 (KiCad) 2", "C5 (KiCad)"]
    assert field_models(Merged(Project(capacitors=own))) == []           # no fields: none


def test_a_kicad_fields_rows_tips_say_where_each_value_lives_and_how_to_use_the_projects():
    from coppernicus.app.fields_view import cap_field_tip, cap_lost_tip
    held = _field_merge([]).cap_fields
    c1, c2, c5 = held["C1 (KiCad)"], held["C2 (KiCad)"], held["C5 (KiCad)"]
    override = "set override_board_fields: true on a project capacitor model naming C1 to use the project's"
    tip = cap_field_tip(c1, "esr_ohm")
    assert tip.startswith('set in KiCad field PI_ESR "5m" on C1; ') and override in tip, tip
    assert 'KiCad field PI_ESL "0.5nH" on C1' in cap_field_tip(c1, "esl_h")
    assert 'footprint Value "10u" of C1' in cap_field_tip(c1, "capacitance_f")
    assert 'KiCad field PI_C "100nF" on C2' in cap_field_tip(c2, "capacitance_f")
    assert 'KiCad field PI_VRATED "16V" on C1' in cap_field_tip(c1, "rated_v")
    assert "no PI_VRATED on C2" in cap_field_tip(c2, "rated_v")
    rated = cap_field_tip(c5, "rated_v")
    assert 'PI_VRATED "10V" on C5' in rated and "not derated" in rated, rated
    assert 'KiCad field PI_DIELECTRIC "X7R" on C1' in cap_field_tip(c1, "dielectric")
    assert "KiPI's typical class II value" in cap_field_tip(c1, "kept_at_rated_pct")
    for cap, diel in ((c2, "C0G"), (c5, "TANT")):          # KiPI derates class II models only
        assert f"a {diel} capacitor is not derated for DC bias" in cap_field_tip(cap, "kept_at_rated_pct")
    name = cap_field_tip(c1, "name")
    assert all(f in name for f in ('PI_ESR "5m"', 'PI_ESL "0.5nH"', 'PI_DIELECTRIC "X7R"', 'PI_VRATED "16V"'))
    assert "read-only" in name
    lost = cap_lost_tip([("C1", "C1 (KiCad)")])
    assert "C1" in lost and "C1 (KiCad)" in lost and "override_board_fields: true" in lost


def test_the_kicad_fields_models_are_shown_read_only_and_never_saved(tmp_path):
    tests = os.path.dirname(os.path.abspath(__file__))
    run_in_window(f'''
        import sys
        from pathlib import Path
        sys.path.insert(0, {tests!r})
        from kicad_fixture import write_board
        from coppernicus.app import theme
        from coppernicus.project import CapModel, Project, Rail, Source
        folder = Path({str(tmp_path)!r})
        c1 = {{"PI_C": "10u", "PI_ESR": "20mOhm", "PI_ESL": "0.5nH", "PI_DIELECTRIC": "C0G"}}
        c2 = {{"Value": "100n", "PI_C": "100nF", "PI_ESR": "30mOhm", "PI_ESL": "0.3nH",
              "PI_DIELECTRIC": "X7R", "PI_VRATED": "16V"}}
        parts = [("J1", [("1", 0.0, 0.0, "VCC"), ("2", 0.0, 3.0, "GND")], {{}}),
                 ("C1", [("1", 10.0, 0.0, "VCC"), ("2", 10.0, 3.0, "GND")], c1),
                 ("C2", [("1", 20.0, 0.0, "VCC"), ("2", 20.0, 3.0, "GND")], c2),
                 ("C3", [("1", 30.0, 0.0, "VCC"), ("2", 30.0, 3.0, "GND")], {{"Value": "1u"}})]
        write_board(folder / "caps.kicad_pcb", parts,
                    [("VCC", (0.0, 0.0), (30.0, 0.0), 0.5), ("GND", (0.0, 3.0), (30.0, 3.0), 0.5)])
        bulk = CapModel("bulk", 10e-6, 0.5e-9, 0.01, ["C1", "C3"])
        feed = Source("J1", 3.3, ["1"], ground_net="GND", ground_pins=["2"])
        path = Project(board="caps.kicad_pcb", capacitors=[bulk], rails=[Rail("VCC", sources=[feed])]).save(
            folder / "caps.coppernicus.yml")
        window.open_project(path)
        app.processEvents()
        panel = window.ac_panel
        caps = panel.caps
        names = [caps.item(r, 0).text() for r in range(caps.rowCount())]
        assert names == ["bulk", "C1 (KiCad)", "C2 (KiCad)"], names
        assert [m.name for m in window.project.capacitors] == ["bulk"]
        for r in (1, 2):
            for c in range(caps.columnCount()):
                item = caps.item(r, c)
                assert not item.flags() & QtCore.Qt.ItemIsEditable, (r, c)
                assert item.background().color().name() == theme.color("cell-readonly"), (r, c)
        assert caps.item(2, 1).text() == "100n" and caps.item(2, 5).text() == "16"
        esr = caps.item(1, 3).toolTip()
        assert 'set in KiCad field PI_ESR "20mOhm" on C1' in esr and "override_board_fields" in esr, esr
        parts_tip = caps.item(0, 4).toolTip()
        assert "C1 (KiCad)" in parts_tip and "override_board_fields" in parts_tip, parts_tip
        assert caps.item(0, 4).text() == "C1, C3" and caps.item(0, 4).flags() & QtCore.Qt.ItemIsEditable
        note = panel.cap_note.text()
        assert not panel.cap_note.isHidden() and "C1 (KiCad)" in note and "bulk" in note, note

        caps.item(1, 3).setText("1")                       # an edit of a field row: never applied
        assert caps.item(1, 3).text() == "20m" and "Not applied" in panel.status.text()
        assert "PI_ESR" in panel.status.text(), panel.status.text()
        assert [(m.name, m.refs, m.esr_ohm) for m in window.project.capacitors] == [
            ("bulk", ["C1", "C3"], 0.01)]
        assert window.merged.project.capacitors[1].esr_ohm == 0.02
        caps.clearSelection()
        caps.selectRow(2)
        panel._remove_models()                             # a field row selected: nothing removed
        assert [m.name for m in window.project.capacitors] == ["bulk"] and caps.rowCount() == 3
        assert "KiCad fields" in panel.status.text(), panel.status.text()
        caps.setCurrentCell(2, 0)
        assert "KiCad fields" in panel._pick_part("C3") and window.project.capacitors[0].refs == ["C1", "C3"]
        msg = panel.models_from_values()                   # C2 has its KiCad fields' model
        assert [m.name for m in window.project.capacitors] == ["bulk"] and "No capacitor" in msg, msg

        caps.item(0, 4).setText("C3")                      # bulk lets go of C1: no lost part
        assert window.project.capacitors[0].refs == ["C3"] and "C1 (KiCad)" not in caps.item(0, 4).toolTip()
        assert caps.rowCount() == 3 and "bulk" not in panel.cap_note.text()
        panel.add_model()                                  # the field rows stay after the project's
        shown = [caps.item(r, 0).text() for r in range(caps.rowCount())]
        assert shown == ["bulk", "", "C1 (KiCad)", "C2 (KiCad)"], shown
        saved = folder / "saved.coppernicus.yml"
        window._save(saved)
        text = saved.read_text(encoding="utf-8")
        assert "KiCad" not in text and "esr_ohm: 0.02" not in text, text
        assert [m.name for m in Project.load(saved).capacitors] == ["bulk"]
        window._use_fields_toggled(False)                  # no fields: no field rows
        app.processEvents()
        assert [caps.item(r, 0).text() for r in range(caps.rowCount())] == ["bulk", ""]
        assert panel.cap_note.isHidden()
    ''')
