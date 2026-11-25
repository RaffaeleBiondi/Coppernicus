"""AC PDN impedance (Task 9): the project's probes, capacitor models and sweep options, the
engine wrapping KiPI's ``solve_ac``, KiPI's AC validation on synthetic boards, verdicts and
outputs. Field by field: ``docs/engine-mapping.md`` (Task 9)."""

import math
import re
from pathlib import Path

import pytest
import yaml

from coppernicus.project import AcOptions, CapModel, Probe, Project, ProjectError, Rail, Source

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
CHAIN = ROOT / "examples" / "power_chain" / "power_chain.coppernicus.yml"


# ---------------------------------------------------------------------- project file

@pytest.mark.parametrize("path", [DEMO, CHAIN], ids=lambda p: p.name)
def test_files_before_t9_gain_no_key(path):
    p = Project.load(path)
    assert p.probes == [] and p.capacitors == [] and p.ac == AcOptions()
    out = p.to_dict()
    assert not {"probes", "capacitors", "ac"} & set(out)


def test_probes_capacitors_and_ac_round_trip_with_defaults_omitted(tmp_path):
    p = Project(board="b.kipi.json.gz")
    p.probes = [Probe("U1", "U1", "+3V3", ["1"], ground_pins=["2"], ground_net="GND",
                      target_ohm=0.05, target_hz=1e7),
                Probe("U2", "U2", "+3V3", target_points=[(1e5, 0.1), (1e7, 0.02)], step_a=1.5,
                      slew_a_per_us=2.0)]
    p.capacitors = [CapModel("10u", 1e-5, 5e-10, 0.02, ["C1", "C2"]),
                    CapModel("100n", 1e-7, rated_v=6.3, kept_at_rated_pct=30.0, dielectric="X7R")]
    p.ac = AcOptions(f_stop_hz=1e9, bandwidth_hz={"U1": 1e5}, link_esl_h={"FB1": 1e-6})
    out = p.save(tmp_path / "p.coppernicus.yml")
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["probes"][0] == {"name": "U1", "ref": "U1", "net": "+3V3", "pins": ["1"],
                                 "ground": {"net": "GND", "pins": ["2"]},
                                 "target_ohm": 0.05, "target_hz": 1e7}
    assert data["probes"][1] == {"name": "U2", "ref": "U2", "net": "+3V3", "pins": "all",
                                 "target_points": [[1e5, 0.1], [1e7, 0.02]],
                                 "step_a": 1.5, "slew_a_per_us": 2.0}
    assert data["capacitors"] == [
        {"name": "10u", "capacitance_f": 1e-5, "esl_h": 5e-10, "esr_ohm": 0.02, "refs": ["C1", "C2"]},
        {"name": "100n", "capacitance_f": 1e-7, "rated_v": 6.3, "kept_at_rated_pct": 30.0,
         "dielectric": "X7R"}]
    assert data["ac"] == {"f_stop_hz": 1e9, "bandwidth_hz": {"U1": 1e5}, "link_esl_h": {"FB1": 1e-6}}
    back = Project.load(out)
    assert back.probes == p.probes and back.capacitors == p.capacitors and back.ac == p.ac


def test_ac_defaults_are_kipis():
    """The sweep defaults are KiPI's ``ACSettings`` (kipi/ac/model.py:237-257)."""
    from coppernicus.engine.ac import kipi_ac_defaults
    a = AcOptions()
    assert {k: getattr(a, k) for k in kipi_ac_defaults()} == kipi_ac_defaults()


def _with(**blocks):
    return {"board": "b.kipi.json.gz", "rails": [], **blocks}


PROBE = {"name": "U1", "ref": "U1", "net": "+3V3", "ground": {"net": "GND"}}


@pytest.mark.parametrize("blocks, words", [
    ({"probes": [{**PROBE, "target_ohm": 0}]}, "target_ohm"),
    ({"probes": [{**PROBE, "target_ohm": -1}]}, "target_ohm"),
    ({"probes": [{**PROBE, "target_ohm": float("nan")}]}, "target_ohm"),
    ({"probes": [{**PROBE, "target_hz": 0}]}, "target_hz"),
    ({"probes": [{**PROBE, "target_ohm": 0.1, "target_points": [[1e6, 0.1]]}]}, "target_points"),
    ({"probes": [{**PROBE, "target_points": [[1e6, 0.1], [1e6, 0.2]]}]}, "frequency"),
    ({"probes": [{**PROBE, "target_points": [[1e6, -0.1]]}]}, "target_points"),
    ({"probes": [{**PROBE, "step_a": 0}]}, "step_a"),
    ({"probes": [{**PROBE, "slew_a_per_us": "x"}]}, "slew_a_per_us"),
    ({"probes": [{**PROBE, "ac_tolerance_v": -0.1}]}, "ac_tolerance_v"),
    ({"probes": [PROBE, PROBE]}, "unique"),
    ({"probes": [{"name": "U1", "net": "+3V3"}]}, "ref"),
    ({"probes": [{"ref": "U1", "net": "+3V3"}]}, "name"),
    ({"capacitors": [{"name": "a", "capacitance_f": -1e-6}]}, "capacitance_f"),
    ({"capacitors": [{"name": "a", "esl_h": float("inf")}]}, "esl_h"),
    ({"capacitors": [{"name": "a", "kept_at_rated_pct": 0}]}, "kept_at_rated_pct"),
    ({"capacitors": [{"name": "a", "kept_at_rated_pct": 120}]}, "kept_at_rated_pct"),
    ({"capacitors": [{"name": "a", "rated_v": 0}]}, "rated_v"),
    ({"capacitors": [{"name": "a"}, {"name": "a"}]}, "unique"),
    ({"capacitors": [{"name": "a", "refs": ["C1"]}, {"name": "b", "refs": ["C1"]}]}, "C1"),
    ({"ac": {"f_start_hz": 1e6, "f_stop_hz": 1e6}}, "f_stop_hz"),
    ({"ac": {"f_start_hz": 0}}, "f_start_hz"),
    ({"ac": {"points_per_decade": 0}}, "points_per_decade"),
    ({"ac": {"points_per_decade": 2.5}}, "points_per_decade"),
    ({"ac": {"points_per_decade": float("inf")}}, "points_per_decade"),
    ({"ac": {"field_points_per_decade": float("nan")}}, "field_points_per_decade"),
    ({"probes": [{**PROBE, "target": 0.1}]}, "unknown probe key target"),
    ({"capacitors": [{"name": "a", "esl": 1e-9}]}, "unknown capacitor model key esl"),
    ({"probes": [{**PROBE, "target_hz": 1e6}]}, "target_hz without target_ohm"),
    ({"probes": [{**PROBE, "target_points": [[1e6, 0.1], [1e7, 0.1]], "target_hz": 1e7}]},
     "target_points"),
    ({"ac": {"field_points_per_decade": -1}}, "field_points_per_decade"),
    ({"ac": {"max_edge_mm": 0}}, "max_edge_mm"),
    ({"ac": {"loss_tangent": -0.01}}, "loss_tangent"),
    ({"ac": {"default_er": 0.5}}, "default_er"),
    ({"ac": {"bandwidth_hz": {"U1": 0}}}, "bandwidth_hz"),
    ({"ac": {"bandwidth_hz": [1e5]}}, "bandwidth_hz"),
    ({"ac": {"link_esl_h": {"FB1": -1e-9}}}, "link_esl_h"),
    ({"ac": {"f_stop": 1e9}}, "f_stop"),
])
def test_bad_ac_values_are_refused(blocks, words):
    with pytest.raises(ProjectError, match=words):
        Project.from_dict(_with(**blocks))


def test_ac_fields_do_not_change_dc_paths():
    """Files from before T9 save byte-identical (tests/test_project.py checks every example);
    a project with AC blocks still maps to the same DC setup fields."""
    p = Project.from_dict(_with(probes=[PROBE], ac={"f_stop_hz": 1e9}))
    assert p.probes[0].ground_net == "GND" and p.probes[0].pins is None
    assert math.isclose(p.ac.f_stop_hz, 1e9)


# ---------------------------------------------------------------------- KiPI's AC validation
# KiPI tests/accuracy/test_ac_accuracy.py, same boards, sweeps and tolerances: the rail and
# ground planes are a parallel-plate capacitor C = ε0·εr·A/h and a cavity whose first mode
# resonates at c/(2a·√εr); a capacitor on the planes is a series RLC, its ESR at the series
# resonance; a ferrite (R + L link) in front of a small plane resonates with that plane.

C0_MM_S = 299_792_458e3          # speed of light, mm/s (KiPI's test, :19)


def _solve(board_fn, project_fn, rail="+3V3"):
    from coppernicus.engine import Board
    from coppernicus.engine.ac import solve_ac_rail
    project = project_fn()
    return project, solve_ac_rail(Board(board_fn()), project, rail)


def _plate(area_mm2, project):
    """Parallel-plate capacitance of ``area_mm2`` of rail over ground, complex with the
    dielectric's loss tangent."""
    from coppernicus.engine.ac import EPS0_F_PER_MM
    from coppernicus.engine.synthetic import AC_ER, AcPlane
    c = EPS0_F_PER_MM * AC_ER * area_mm2 / AcPlane.H
    return c * (1 - 1j * project.ac.loss_tangent)


def _hole_area():
    from coppernicus.engine.synthetic import AcPlane
    return AcPlane.HOLE ** 2           # each part's GND pad cut-out in the rail plane


def _at(result, f):
    import numpy as np
    i = int(np.argmin(np.abs(result.freq_hz - f)))
    return result.freq_hz[i], result.probes[0].z_ohm[i]


def _peaks(result, lo, hi):
    """Local maxima of |Z| between ``lo`` and ``hi``, largest first."""
    import numpy as np
    m, f = np.abs(result.probes[0].z_ohm), result.freq_hz
    k = [i for i in range(1, len(m) - 1) if m[i] > m[i - 1] and m[i] > m[i + 1] and lo <= f[i] <= hi]
    return sorted(((f[i], m[i]) for i in k), key=lambda p: -p[1])


@pytest.fixture(scope="module")
def plane():
    from coppernicus.engine.synthetic import ac_plane_board, ac_plane_project
    return _solve(ac_plane_board, ac_plane_project)


def test_plane_pair_is_a_parallel_plate_capacitor(plane):
    from coppernicus.engine.synthetic import AcPlane
    project, res = plane
    c = _plate(AcPlane.SIZE ** 2 - _hole_area(), project)
    assert res.plane_capacitance_f == pytest.approx(c.real, rel=1e-3)
    for f in (1e3, 1e5, 1e6):
        f, z = _at(res, f)
        assert z == pytest.approx(1 / (2j * math.pi * f * c), rel=5e-3)


def test_plane_pair_first_cavity_resonance(plane):
    from coppernicus.engine.synthetic import AC_ER, AcPlane
    _, res = plane
    f10 = C0_MM_S / (2 * AcPlane.SIZE * math.sqrt(AC_ER))
    (f_peak, _), *_ = _peaks(res, 0.5 * f10, 1.2 * f10)
    assert f_peak == pytest.approx(f10, rel=1.5e-2)


@pytest.fixture(scope="module")
def decap():
    from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project
    return _solve(ac_decap_board, ac_decap_project)


def test_decap_low_frequency_is_both_capacitances(decap):
    from coppernicus.engine.synthetic import AcDecap as g
    project, res = decap
    cp = _plate(g.SIZE ** 2 - 2 * _hole_area(), project)
    assert res.plane_capacitance_f == pytest.approx(cp.real, rel=1e-3)
    for f in (1e3, 1e4):
        f, z = _at(res, f)
        w = 2 * math.pi * f
        expected = 1 / (1 / (g.ESR + 1 / (1j * w * g.C)) + 1j * w * cp)
        assert z == pytest.approx(expected, rel=5e-3)


def test_decap_is_a_series_rlc_with_its_esr_at_resonance(decap):
    import numpy as np

    from coppernicus.engine.synthetic import AcDecap as g
    project, res = decap
    cp = _plate(g.SIZE ** 2 - 2 * _hole_area(), project).real
    ls = []
    for f in (5e6, 1e7, 2e7):      # X = ωL − 1/(ωC) between the series resonance and the planes'
        f, z = _at(res, f)
        w = 2 * math.pi * f
        ls.append((z.imag + 1 / (w * (g.C + cp))) / w)
    assert max(ls) / min(ls) < 1.03
    mounted = float(np.mean(ls))
    assert g.ESL < mounted < g.ESL + 2e-9            # the ESL plus pads, vias and planes
    m = np.abs(res.probes[0].z_ohm)
    i = int(np.argmin(m))
    srf = 1 / (2 * math.pi * math.sqrt(mounted * g.C))
    assert res.freq_hz[i] == pytest.approx(srf, rel=3e-2)
    assert g.ESR <= m[i] <= 1.15 * g.ESR


@pytest.fixture(scope="module")
def ferrite():
    from coppernicus.engine.synthetic import ac_ferrite_board, ac_ferrite_project
    return _solve(ac_ferrite_board, ac_ferrite_project)


def _ferrite_lumped(project, f):
    from coppernicus.engine.synthetic import AcFerrite as g
    w = 2j * math.pi * f
    c_a = _plate(g.SIZE_A ** 2 - _hole_area(), project)
    c_b = _plate(g.SIZE_B ** 2 - _hole_area(), project)
    z_cap = g.ESR + w * g.ESL + 1 / (w * g.C)
    z_a = 1 / (1 / z_cap + w * c_a)                  # bulk capacitor || +3V3 plane
    z_link = g.R_FB + w * g.L_FB
    return 1 / (1 / (z_link + z_a) + w * c_b), c_b   # || +3V3_F plane


def test_ferrite_board_matches_its_lumped_parts(ferrite):
    import numpy as np

    from coppernicus.engine.synthetic import AcFerrite as g
    project, res = ferrite
    expected, c_b = _ferrite_lumped(project, res.freq_hz)
    f0 = 1 / (2 * math.pi * math.sqrt(g.L_FB * c_b.real))
    ratio = np.abs(res.probes[0].z_ohm) / np.abs(expected)
    away = (np.abs(np.log(res.freq_hz / f0)) > 0.2) & (res.freq_hz <= 100e6)
    assert np.all(np.abs(ratio[away] - 1) < 0.01), (ratio[away].min(), ratio[away].max())
    high = res.freq_hz > 100e6
    assert np.all((ratio[high] < 1) & (ratio[high] > 0.9))


def test_ferrite_resonates_with_the_filtered_plane(ferrite):
    import numpy as np

    from coppernicus.engine.synthetic import AcFerrite as g
    project, res = ferrite
    expected, c_b = _ferrite_lumped(project, res.freq_hz)
    f0 = 1 / (2 * math.pi * math.sqrt(g.L_FB * c_b.real))
    (f_peak, z_peak), = _peaks(res, f0 / 3, 3 * f0)
    assert f_peak == pytest.approx(f0, rel=1.5e-2)
    assert z_peak == pytest.approx(np.abs(expected).max(), rel=0.05)
    assert res.rail == "+3V3" and set(res.rail_nets) == {"+3V3", "+3V3_F"} and res.links == ["FB1"]


# ---------------------------------------------------------------------- mapping and errors

QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4,
             max_edge_mm=2.0)


def _decap(**ac):
    """The ac_decap board and project with a quick sweep (a coarse mesh, few frequencies)."""
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project
    project = ac_decap_project()
    project.ac = AcOptions(**{**QUICK, "bandwidth_hz": {"U1": 1e5}, **ac})
    return Board(ac_decap_board()), project


def test_probe_pins_resolve_and_an_unknown_pin_names_the_probe():
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import ac_setup
    board, project = _decap()
    setup, vrm, gnd, notes = ac_setup(board, project, "+3V3")
    (probe,) = setup.probes
    assert (probe.rail.ref, probe.rail.net, probe.rail.pins) == ("U1", "+3V3", ["1"])
    assert (probe.ground.net, probe.ground.pins) == ("GND", ["2"]) and gnd == "GND"
    assert vrm == "U1" and setup.ac.f_stop_hz == 1e8 and notes == []
    assert [v.bandwidth_khz for v in setup.vrms] == [100.0]
    project.probes[0].pins = ["9"]
    with pytest.raises(EngineError, match="Probe U1.*9"):
        ac_setup(board, project, "+3V3")


def test_model_refs_off_the_board_raise_and_non_capacitor_refs_warn():
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import ac_setup
    board, project = _decap()
    project.capacitors[0].refs = ["C1", "C99"]
    with pytest.raises(EngineError, match="C99 is not on the board"):
        ac_setup(board, project, "+3V3")
    project.capacitors[0].refs = ["C1"]
    project.capacitors.append(CapModel("odd", 1e-6, 1e-9, 0.01, ["U1"]))
    _setup, _vrm, _gnd, notes = ac_setup(board, project, "+3V3")
    assert any("U1" in n and "C<number>" in n for n in notes)


def test_a_rail_without_source_and_kipis_messages_become_engine_errors():
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import solve_ac_rail
    board, project = _decap()
    no_source = Project.from_dict({**project.to_dict(),
                                   "rails": [{"net": "+3V3", "sources": [], "loads": []}]})
    with pytest.raises(EngineError, match="no source"):
        solve_ac_rail(board, no_source, "+3V3")
    project.capacitors[0].esl_h = 0.0              # KiPI's check: a model with refs needs its ESL
    with pytest.raises(EngineError, match="Capacitor model 10u: enter its ESL"):
        solve_ac_rail(board, project, "+3V3")
    board, project = _decap()
    project.probes[0].ground_ref, project.probes[0].ground_pins = "C1", ["1"]
    project.probes[0].ground_net = "+3V3"          # its ground pins off the solved ground
    with pytest.raises(EngineError, match="not on the ground being simulated"):
        solve_ac_rail(board, project, "+3V3")


def test_cancel_raises_cancelled():
    from coppernicus.engine import Cancelled
    from coppernicus.engine.ac import solve_ac_rail
    board, project = _decap()
    with pytest.raises(Cancelled):
        solve_ac_rail(board, project, "+3V3", cancelled=lambda: True)


def test_a_net_behind_a_link_is_solved_with_the_rail_feeding_it():
    from coppernicus.engine import Board
    from coppernicus.engine.ac import ac_rails, ac_setup
    from coppernicus.engine.synthetic import ac_ferrite_board, ac_ferrite_project
    board, project = Board(ac_ferrite_board()), ac_ferrite_project()
    assert ac_rails(board, project) == ["+3V3"]
    setup, vrm, gnd, notes = ac_setup(board, project, "+3V3_F")
    assert vrm == "U1" and gnd == "GND" and "solved with +3V3" in notes[0]
    assert next(k for k in setup.links if k.name == "FB1").esl_h == 1e-6


# ---------------------------------------------------------------------- verdicts

FREQS = None


def _arrays(z_low=10.0, z_high=0.01, corner=1e4, f0=1e3, f1=1e9, n=121):
    """|Z| falling from ``z_low`` to ``z_high`` past ``corner`` (an open-loop rise below it):
    about 11 mΩ at 100 kHz, 10 mΩ above."""
    import numpy as np
    f = np.logspace(math.log10(f0), math.log10(f1), n)
    mag = z_high + (z_low - z_high) / (1 + (f / corner) ** 4)
    return f, mag.astype(complex)


def _judged(probe: Probe, bandwidth=1e5, mag=None, missing=(), dielectric="", f=None):
    """An ImpedanceResult built by hand around ``probe``, its target curve and verdict as the
    engine makes them (no solve)."""
    from coppernicus.engine.ac import ASSUMED_BANDWIDTH_HZ, _target, _target_curve
    from coppernicus.impedance import judge
    from coppernicus.results import ImpedanceResult, ProbeImpedance
    if f is None:
        f, mag = _arrays()
    ohm, hz, pts, note = _target(probe)
    shown = bandwidth if bandwidth is not None else ASSUMED_BANDWIDTH_HZ
    t, fallback, judged = _target_curve(f, ohm, hz, pts, bandwidth, shown)
    p = ProbeImpedance(probe.name, probe.ref, probe.pins, probe.net, "GND", mag, ohm, hz, pts, t,
                       note, judged, fallback, step_a=probe.step_a, slew_a_per_us=probe.slew_a_per_us)
    r = ImpedanceResult("+3V3", ["+3V3"], "GND", "U1", bandwidth, f, f[::10], [p],
                        missing_caps=list(missing), dielectric_assumed=dielectric,
                        assumed_bandwidth_hz=None if bandwidth is not None else ASSUMED_BANDWIDTH_HZ)
    return judge(r)


def _probe(**kw):
    return Probe("U1", "U1", "+3V3", ["1"], ground_pins=["2"], ground_net="GND", **kw)


def test_no_target_is_ranked_not_judged():
    from coppernicus.impedance import ac_summary, rail_verdict
    r = _judged(_probe())
    p = r.probes[0]
    assert p.passed is None and p.not_judged == "" and p.worst_ratio is None
    assert p.worst_hz >= 1e5 and p.worst_z_ohm == pytest.approx(max(p.mag_ohm[r.freq_hz >= 1e5]))
    assert rail_verdict(r) == ("not_judged", [], [])
    doc = ac_summary([r])
    assert doc["verdict"] == "not_judged" and doc["not_judged"] == [] and "No target" in doc["verdict_note"]


def test_a_target_is_judged_only_above_the_bandwidth():
    from coppernicus.impedance import ac_summary
    r = _judged(_probe(target_ohm=0.05))       # |Z| 10 Ω at 1 kHz (open loop), 11 mΩ at 100 kHz
    p = r.probes[0]
    assert p.passed is True and p.judged_from_hz == 1e5 and p.worst_ratio < 1
    assert ac_summary([r])["verdict"] == "pass"
    r = _judged(_probe(target_ohm=0.0105))     # above the bandwidth |Z| reaches 11 mΩ
    assert r.probes[0].passed is False and r.probes[0].worst_hz == pytest.approx(1e5, rel=0.05)
    doc = ac_summary([r])
    assert doc["verdict"] == "fail"
    assert doc["failed"][0]["item"] == "U1" and doc["failed"][0]["rail"] == "+3V3"


def test_without_a_bandwidth_nothing_is_judged_and_no_worst_point(tmp_path):
    import csv

    from coppernicus.engine.ac import ASSUMED_BANDWIDTH_HZ
    from coppernicus.impedance import ac_summary, write_csv
    r = _judged(_probe(target_ohm=0.001), bandwidth=None)     # it would fail: not judged anyway
    p = r.probes[0]
    assert p.passed is None and "no control loop bandwidth" in p.not_judged
    assert p.worst_z_ohm is None and p.judged_from_hz is None
    assert r.bandwidth_assumed and r.open_loop_hz == ASSUMED_BANDWIDTH_HZ == 50e3
    doc = ac_summary([r])
    assert doc["verdict"] == "not_judged" and doc["rails"][0]["bandwidth_assumed"] is True
    assert doc["rails"][0]["bandwidth_hz"] is None and doc["rails"][0]["assumed_bandwidth_hz"] == 50e3
    with open(write_csv(r, tmp_path / "z.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][-3:] == ["bandwidth_hz", "assumed_bandwidth_hz", "bandwidth_assumed"]
    assert rows[1][-3:] == ["", "50000", "true"]


def test_a_bandwidth_at_or_above_the_target_frequency_is_not_judged():
    """KiPI would fall back to judging the whole range, open-loop rise included (Q5)."""
    r = _judged(_probe(target_ohm=0.05, target_hz=5e4))
    p = r.probes[0]
    assert p.passed is None and "at or above the target frequency" in p.not_judged
    assert "fallback" in p.not_judged and p.judged_from_hz is None


def test_a_target_beyond_the_sweep_fails_or_is_not_judged():
    r = _judged(_probe(target_ohm=0.05, target_hz=1e10))      # holds where solved
    assert r.probes[0].passed is None and "not inside the sweep" in r.probes[0].not_judged
    assert "raise ac.f_stop_hz to 10 GHz" in r.probes[0].not_judged
    r = _judged(_probe(target_ohm=0.0105, target_hz=1e10))    # exceeded where solved: a fail
    assert r.probes[0].passed is False
    f, flat = _arrays(z_low=0.01)                             # 10 mΩ everywhere
    r = _judged(_probe(target_ohm=0.05), bandwidth=500.0, f=f, mag=flat)   # below the sweep start
    assert r.probes[0].passed is None and "not inside the sweep" in r.probes[0].not_judged
    assert "lower ac.f_start_hz to the bandwidth, 500 Hz" in r.probes[0].not_judged
    r = _judged(_probe(target_ohm=0.05, target_hz=500.0), bandwidth=100.0)   # ends below it
    assert r.probes[0].passed is None and "applies nowhere" in r.probes[0].not_judged


def test_missing_models_and_an_assumed_dielectric_are_not_judged():
    r = _judged(_probe(target_ohm=0.05), missing=["C2", "C3"])
    assert r.probes[0].passed is None and "C2, C3" in r.probes[0].not_judged
    r = _judged(_probe(target_ohm=0.05), dielectric="where the stackup gives no εr, 4.3 is used")
    assert r.probes[0].passed is None and "εr is assumed" in r.probes[0].not_judged


def test_a_mask_is_judged_between_its_points():
    r = _judged(_probe(target_points=[(1e5, 0.05), (1e8, 0.05)]))
    assert r.probes[0].passed is True and r.probes[0].limit == "target_points"
    assert "only between its first and last points" in r.probes[0].target_note
    r = _judged(_probe(target_points=[(1e5, 0.05), (1e10, 0.05)]))
    assert r.probes[0].passed is None and "not inside the sweep" in r.probes[0].not_judged


def test_a_mask_applies_only_between_its_first_and_last_points():
    """No flat hold beyond the corners (KiPI's mask_at would invent target values there)."""
    import numpy as np
    mask = _probe(target_points=[(1e6, 0.05), (1e7, 0.05)])         # bandwidth 100 kHz
    f, flat = _arrays(z_low=0.01)                                   # 10 mΩ everywhere
    for excess in ((f > 1.5e5) & (f < 3e5), f > 2e7):               # around 200 kHz; above 20 MHz
        mag = np.where(excess, 1.0, 0.01).astype(complex)
        r = _judged(mask, f=f, mag=mag)
        t = r.probes[0].target_curve_ohm
        assert np.isnan(t[(f < 1e6 * 0.999) | (f > 1e7 * 1.001)]).all()
        assert r.probes[0].passed is True
    mag = np.where((f > 4e6) & (f < 6e6), 1.0, 0.01).astype(complex)   # inside the mask
    assert _judged(mask, f=f, mag=mag).probes[0].passed is False
    below = _judged(_probe(target_points=[(5e3, 0.05), (2e4, 0.05)]), f=f, mag=flat)
    reason = below.probes[0].not_judged
    assert below.probes[0].passed is None and "entirely below the bandwidth, 100 kHz" in reason
    assert "from 100 kHz to 20 kHz" not in reason


def test_a_computed_target_comes_from_the_transient():
    """Z = V_ac/I_step up to 1/(π·t_rise), t_rise = I_step/slew (kipi/ac/target.py:53-60)."""
    r = _judged(_probe(ac_tolerance_v=0.05, step_a=1.0, slew_a_per_us=1.0))
    p = r.probes[0]
    assert p.target_ohm == pytest.approx(0.05) and p.target_hz == pytest.approx(1 / (math.pi * 1e-6))
    assert "computed by KiPI" in p.target_note and p.passed is True
    r = _judged(_probe(ac_tolerance_v=0.05, step_a=1.0))      # no slew: no target
    assert r.probes[0].target_ohm is None and r.probes[0].passed is None


# ---------------------------------------------------------------------- assumptions and outputs

@pytest.fixture(scope="module")
def quick():
    from coppernicus.engine.ac import solve_ac_rail
    board, project = _decap()
    project.probes[0].target_ohm = 0.05
    return project, solve_ac_rail(board, project, "+3V3")


def test_assumptions_name_the_model_the_dielectric_and_the_open_loop(quick):
    _, r = quick
    a = r.assumptions
    assert a[0] == ("AC model (KiPI): rail and ground copper as 2.5D sheets with two faces; facing "
                    "copper gives the plane capacitance ε₀·εr/h·(1 − j·tanδ) and the loop inductance "
                    "μ₀·h per square; plated barrels are resistors; capacitors, links and probes are "
                    "lumped ports.")
    for line in (
            "Open-loop PDN impedance: the source U1 is left out of the AC model, so |Z| rises below "
            "its control loop bandwidth, 100 kHz; targets are judged only above it.",
            "Dielectric εr, top to bottom: FR4 200 µm εr 4.5, FR4 1000 µm εr 4.5, FR4 200 µm εr 4.5.",
            "Dielectric loss tangent 0.02 (ac.loss_tangent; KiPI's default 0.02).",
            "Sweep 100 kHz to 100 MHz, 20 points per decade; the copper is solved at 13 frequencies "
            "(4 per decade; KiPI's default 20) and interpolated between them (PCHIP of jω·Z on log f).",
            "AC mesh: largest edge 2 mm, refined toward plated holes near the rail from 0.03 mm "
            "(grading 0.3), up to 5 mm far from it (KiPI's AC defaults).",
            "Capacitors: ESR + jω·ESL + 1/(jω·C) as entered, 1 modelled.",
            "Probe U1: target 50 mΩ over the whole sweep (entered)"):
        assert line in a, line
    assert any(x.startswith("Via plating:") for x in a)
    assert r.dielectric_assumed == "" and r.bandwidth_hz == 1e5 and r.assumed_bandwidth_hz is None
    assert r.stats["triangles"] > 0 and r.stats["method"]
    peak = r.stats["memory_peak_mb"]                   # None where it can't be read
    assert peak is None or peak > 0


def test_a_stack_without_er_is_assumed_and_not_judged():
    from coppernicus.engine import Board
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import ac_decap_board
    _, project = _decap()
    project.probes[0].target_ohm = 0.05
    r = solve_ac_rail(Board(ac_decap_board(er=0.0)), project, "+3V3")
    assert "4.3 is used" in r.dielectric_assumed
    line = next(x for x in r.assumptions if x.startswith("Dielectric εr"))
    assert "FR4 200 µm εr 4.3 (Default εr)" in line
    assert line.endswith(" Where the stackup gives no εr, 4.3 is used (ac.default_er, KiPI's default).")
    assert r.probes[0].passed is None and "εr is assumed" in r.probes[0].not_judged
    assert ("FR4 200 µm (F.Cu–In1.Cu), FR4 1000 µm (In1.Cu–In2.Cu), FR4 200 µm (In2.Cu–B.Cu)"
            in r.probes[0].not_judged)


def test_without_a_bandwidth_the_assumption_names_the_placeholder_and_the_source():
    from coppernicus.engine.ac import solve_ac_rail
    board, project = _decap(bandwidth_hz={})
    r = solve_ac_rail(board, project, "+3V3")
    line = next(x for x in r.assumptions if x.startswith("Open-loop"))
    assert "source U1" in line and "no control loop bandwidth is set for it (ac.bandwidth_hz)" in line
    assert "50 kHz" in line and r.bandwidth_hz is None and r.assumed_bandwidth_hz == 50e3


def test_csv_png_and_summary_keys(quick, tmp_path):
    import csv
    import json

    from coppernicus.impedance import ac_summary, write_csv, write_png
    _, r = quick
    with open(write_csv(r, tmp_path / "z.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["freq_hz", "U1_re_ohm", "U1_im_ohm", "U1_mag_ohm", "U1_target_ohm",
                       "bandwidth_hz", "assumed_bandwidth_hz", "bandwidth_assumed"]
    assert len(rows) == 1 + len(r.freq_hz) and rows[1][-3:] == ["100000", "", "false"]
    assert float(rows[-1][4]) == 0.05 and float(rows[-1][3]) == pytest.approx(abs(r.probes[0].z_ohm[-1]))
    assert write_png(r, tmp_path / "z.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    doc = ac_summary([r], {"+3V3": ("z.csv", "z.png")})
    json.dumps(doc, allow_nan=False)
    assert set(doc) == {"kind", "verdict", "verdict_note", "failed", "not_judged", "rails",
                        "warnings"}                    # beyond gate §5: project-level warnings
    assert doc["kind"] == "ac_impedance"
    rail = doc["rails"][0]
    assert set(rail) == {"rail", "rail_nets", "ground", "source", "bandwidth_hz", "f_start_hz",
                         "f_stop_hz", "sweep_points", "field_points", "plane_capacitance_f",
                         "caps_modelled", "caps_missing", "links", "verdict", "not_judged", "csv_file",
                         "png_file", "assumptions", "warnings", "stats",
                         # beyond gate §5: the bandwidth flag and placeholder (brief Q2), the probes
                         "bandwidth_assumed", "assumed_bandwidth_hz", "probes"}
    assert rail["bandwidth_hz"] == 1e5 and rail["assumed_bandwidth_hz"] is None
    assert set(rail["stats"]) == {"seconds", "triangles", "unknowns", "ports", "method",
                                  "memory_peak_mb", "memory_peak_raised"}      # + memory (brief Q7)
    assert set(rail["probes"][0]) == {"name", "net", "ground_net", "z_min_ohm", "z_max_ohm",
                                      "worst_z_ohm", "worst_hz", "worst_ratio", "target_ohm",
                                      "target_hz", "target_points", "target_note", "judged_from_hz",
                                      "passed", "step_a", "slew_a_per_us"}
    assert rail["csv_file"] == "z.csv" and rail["caps_modelled"] == ["C1"] and rail["sweep_points"] == 61


def test_the_plot_shades_the_open_loop_band_and_labels_an_assumed_one():
    from matplotlib.figure import Figure

    from coppernicus.impedance import OPEN_LOOP_LABEL, assumed_label, draw_impedance
    assumed = "below the regulator's control-loop bandwidth (not given; 50 kHz assumed): not judged"
    assert assumed_label(50e3) == assumed
    for bandwidth, label in ((None, assumed), (1e5, OPEN_LOOP_LABEL)):
        ax = Figure().add_subplot(1, 1, 1)
        draw_impedance(ax, _judged(_probe(target_ohm=0.05), bandwidth=bandwidth))
        texts = [t.get_text() for t in ax.get_legend().get_texts()]
        assert label in texts and "target U1" in texts
        assert ("bandwidth not given (50 kHz assumed)" in ax.get_title()) == (bandwidth is None)
    f, mag = _arrays(f0=1e5)                           # an assumed 50 kHz below the sweep: no band
    ax = Figure().add_subplot(1, 1, 1)
    draw_impedance(ax, _judged(_probe(), bandwidth=None, f=f, mag=mag))
    assert "bandwidth not given" in ax.get_title()


# ---------------------------------------------------------------------- the committed example

def test_the_example_fails_and_its_relaxed_copy_holds():
    """examples/pdn (examples/make_examples.py): solved once; the relaxed copy differs only by
    its target, which does not enter the solve, so it is judged on the same curve."""
    from coppernicus.engine import Board
    from coppernicus.engine.ac import _target, _target_curve, solve_ac_rail
    from coppernicus.impedance import ac_verdict, judge
    pdn = ROOT / "examples" / "pdn"
    tight = Project.load(pdn / "pdn.coppernicus.yml")
    relaxed = Project.load(pdn / "pdn-relaxed.coppernicus.yml")
    same = tight.to_dict()
    same["probes"][0]["target_ohm"] = relaxed.probes[0].target_ohm
    assert same == relaxed.to_dict()
    r = solve_ac_rail(Board.open(tight.board_path()), tight, "+3V3")
    p = r.probes[0]
    assert p.passed is False and p.worst_z_ohm == pytest.approx(0.16, rel=0.02)
    assert p.worst_hz == pytest.approx(1e5, rel=0.01) and ac_verdict([r])[0] == "fail"
    ohm, hz, pts, _note = _target(relaxed.probes[0])
    p.target_ohm = ohm
    p.target_curve_ohm, _fallback, _judged = _target_curve(r.freq_hz, ohm, hz, pts, r.bandwidth_hz,
                                                           r.open_loop_hz)
    assert judge(r).probes[0].passed is True and ac_verdict([r])[0] == "pass"


# ---------------------------------------------------------------------- cost and memory (brief Q7)

def test_ac_size_meshes_as_the_solve_does(quick):
    """The pre-solve size: KiPI's own AC mesh of the rail and its ground, and the ports."""
    from coppernicus.engine.ac import ac_size
    board, project = _decap()
    size = ac_size(board, project, "+3V3")
    _, r = quick
    assert size.triangles == int(r.stats["triangles"]) and size.ports == int(r.stats["ports"]) == 2
    assert size.field_frequencies == len(r.field_freq_hz) and size.sweep_points == len(r.freq_hz)
    assert size.rail == "+3V3"


def test_cost_estimate_is_triangles_times_ports_scaled_from_the_last_solve():
    from coppernicus.engine.ac import cost_estimate
    first = cost_estimate(20_000, 2, 111)
    assert first.work == 40_000 and first.seconds is None
    assert "20,000 triangles × 2 ports" in first.text and "111" in first.text
    last = {"seconds": 10.0, "field_seconds": 8.0, "triangles": 20_000.0, "ports": 2.0,
            "field_frequencies": 111.0}
    assert cost_estimate(20_000, 2, 111, last).seconds == pytest.approx(10.0)
    # Only the field solves scale with the frequencies; the mesh and model (2 s) do not.
    assert cost_estimate(20_000, 2, 222, last).seconds == pytest.approx(2.0 + 16.0)
    bigger = cost_estimate(80_000, 2, 111, last)
    assert bigger.seconds > 4 * 10.0 and "last solve" in bigger.text and "10 s" in bigger.text
    assert "n − n_free + ports" in bigger.text


def test_ram_estimate_follows_the_measured_fit_and_warns_past_the_machines_memory():
    from coppernicus.engine.ac import ram_estimate
    # The fit's own measurements (ac_plane meshed finer and finer, one factorization).
    for unknowns, measured_mb in ((19_196, 254.4), (147_110, 2745.0)):
        assert ram_estimate(unknowns, 64e9).bytes / 1e6 == pytest.approx(measured_mb, rel=0.02)
    small = ram_estimate(147_110, 24e9)
    assert not small.risk and "measured on synthetic plane pairs" in small.text
    assert "other boards may need more or less" in small.text
    assert "several times less" not in small.text and "upper" not in small.text
    large = ram_estimate(3_000_000, 24e9)
    assert large.risk and "24 GB" in large.text and large.bytes > 24e9
    unknown = ram_estimate(3_000_000, None)
    assert not unknown.risk and "cannot be read" in unknown.text


# ---------------------------------------------------------------------- hooks and capacitor files

def test_progress_and_fraction_hooks_drop_kipis_own_target_lines():
    from coppernicus.engine.ac import solve_ac_rail
    board, project = _decap()
    messages, fractions = [], []
    solve_ac_rail(board, project, "+3V3", progress=messages.append, fraction=fractions.append)
    assert any(m.startswith("Field solves") for m in messages)
    assert not any(m.startswith("Targets apply above") or ": |Z| " in m for m in messages)
    steps = [f for f in fractions if f is not None]
    assert steps and max(steps) == pytest.approx(1.0) and min(steps) > 0


def test_capacitor_files_round_trip_and_warn_about_dropped_fields(tmp_path):
    from coppernicus.engine.ac import read_cap_file, write_cap_file
    m = CapModel("10u X5R", 1e-5, 5e-10, 0.02, ["C1"], rated_v=6.3, dielectric="X5R")
    path = write_cap_file(tmp_path / "10u.yml", m)
    back, warnings = read_cap_file(path)
    assert warnings == [] and back.refs == []          # parts are the board's, not the file's
    assert (back.name, back.capacitance_f, back.esl_h, back.esr_ohm, back.rated_v, back.dielectric) == (
        "10u X5R", 1e-5, 5e-10, 0.02, 6.3, "X5R")
    assert back.kept_at_rated_pct == 40.0                # KiPI's file holds its typical value
    text = path.read_text(encoding="utf-8")
    assert "kipi-capacitor-model" in text
    path.write_text(text.replace("aging_pct_per_decade: 0.0", "aging_pct_per_decade: 2.0"),
                    encoding="utf-8")
    _back, warnings = read_cap_file(path)
    assert "aging_pct_per_decade" in warnings[0]
    from coppernicus.engine import EngineError
    (tmp_path / "bad.yml").write_text("kind: something-else\n", encoding="utf-8")
    with pytest.raises(EngineError, match="bad.yml"):
        read_cap_file(tmp_path / "bad.yml")


def test_capacitor_candidates_read_their_values():
    from coppernicus.engine.ac import capacitor_candidates
    board, _ = _decap()
    (c,) = capacitor_candidates(board, ["+3V3", "GND"])
    assert (c.ref, c.value, c.nets) == ("C1", "10u", ["+3V3", "GND"])
    assert c.capacitance_f == pytest.approx(1e-5)
    assert capacitor_candidates(board, ["+3V3"]) == []


# ---------------------------------------------------------------------- review follow-ups

def test_a_ground_net_is_refused_by_name():
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import ac_setup
    board, project = _decap()
    with pytest.raises(EngineError, match="GND is a ground in this project"):
        ac_setup(board, project, "GND")
    project.rails.append(Rail("GND", sources=[Source("U1", 0.0, ["2"])]))   # a rail on a ground
    with pytest.raises(EngineError, match="GND is a ground in this project"):
        ac_setup(board, project, "GND")


def test_mesh_and_pad_errors_become_engine_errors(monkeypatch):
    import coppernicus.engine.ac as ac
    from coppernicus.engine import EngineError
    board, project = _decap()
    for error in (ac.MeshError("Net +3V3 has no copper to mesh"), ac.SolveError("U1 has no pads")):
        def boom(*_a, error=error, **_k):
            raise error
        monkeypatch.setattr(ac, "solve_ac", boom)
        with pytest.raises(EngineError, match=re.escape(str(error))):
            ac.solve_ac_rail(board, project, "+3V3")


def test_the_ram_estimate_is_one_factorization_below_the_runs_peak(quick):
    from coppernicus.engine.ac import ram_estimate
    _, r = quick
    est = ram_estimate(r.stats["unknowns"], 64e9, free_bytes=8e9)
    assert "side by side" in est.text and "up to 4 GB more here" in est.text
    assert est.threads_bytes == 4e9 and not est.risk
    if r.stats["memory_peak_mb"] is not None:          # the whole run held more than one
        assert est.bytes < r.stats["memory_peak_mb"] * 1e6


def test_dc_bias_derating_follows_kipis_formula():
    """C/(1 + k·(V/V_rated)^1.5), k = 1/kept − 1, at the source's 3.3 V (kipi/ac/model.py:96-102)."""
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import AcDecap
    board, project = _decap()
    project.capacitors[0].rated_v = 6.3
    r = solve_ac_rail(board, project, "+3V3")
    k = 1 / 0.40 - 1
    assert r.cap_derated_f["C1"] == pytest.approx(AcDecap.C / (1 + k * (3.3 / 6.3) ** 1.5))
    assert ("DC bias: 1 capacitor(s) with a voltage rating derated at the U1 set voltage, 3.3 V: "
            "C/(1 + k·(V/V_rated)^1.5), k from 40 % kept at the rated voltage (KiPI's typical class "
            "II value); a datasheet curve is better.") in r.assumptions
    project.capacitors[0].kept_at_rated_pct = 25.0
    r = solve_ac_rail(board, project, "+3V3")
    assert r.cap_derated_f["C1"] == pytest.approx(AcDecap.C / (1 + 3.0 * (3.3 / 6.3) ** 1.5))
    assert any("k from 25 % kept at the rated voltage (entered)" in a for a in r.assumptions)


def test_copper_temperature_maps_to_kipis_fixed_temperature():
    from coppernicus.engine.ac import ac_setup, solve_ac_rail
    board, project = _decap()
    project.settings.copper_temp_c = 60.0
    setup, _vrm, _gnd, _notes = ac_setup(board, project, "+3V3")
    assert (setup.ac.temperature, setup.ac.temperature_c) == ("fixed", 60.0)
    r = solve_ac_rail(board, project, "+3V3")
    assert ("Copper at 60 °C (settings.copper_temp_c); capacitor models with a dielectric follow "
            "KiPI's typical temperature curve.") in r.assumptions
    project.settings.copper_temp_c = 20.0
    assert ac_setup(board, project, "+3V3")[0].ac.temperature == "none"


def test_link_lines_with_and_without_series_inductance(ferrite):
    from coppernicus.engine import Board
    from coppernicus.engine.ac import solve_ac_rail
    from coppernicus.engine.synthetic import ac_ferrite_board, ac_ferrite_project
    _, res = ferrite
    assert "Link FB1: 1 Ω + jω·1e-06 H between +3V3 and +3V3_F." in res.assumptions
    project = ac_ferrite_project()
    project.ac = AcOptions(**QUICK, bandwidth_hz={"U1": 1e5})
    r = solve_ac_rail(Board(ac_ferrite_board()), project, "+3V3")
    assert ("Link FB1: 1 Ω between +3V3 and +3V3_F (no series inductance set: ac.link_esl_h)."
            in r.assumptions)
