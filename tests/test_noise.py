"""DC-DC noise and ripple (Task 10): the project's aggressors, victims and noise options, the
engine wrapping KiPI's ``solve_noise``, KiPI's coupled-noise validation on its synthetic
buck, verdicts and outputs. Field by field: ``docs/engine-mapping.md`` (Task 10)."""

import math
from pathlib import Path

import pytest
import yaml

from coppernicus.project import Aggressor, NoiseOptions, Phase, Project, ProjectError, Victim

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = [ROOT / "examples" / d / f"{d}.coppernicus.yml" for d in ("demo", "power_chain", "pdn")]


# ---------------------------------------------------------------------- synthetic board

def test_the_coupled_noise_board_is_kipis_buck_with_three_victims():
    from coppernicus.engine.synthetic import CoupledNoise, coupled_noise_board
    snap = coupled_noise_board()
    refs = {f.reference for f in snap.footprints}
    assert refs == {"J1", "C1", "U1", "L1", "C2", "U2", "U3", "U4", "U5", "U6", "U7", "U8"}
    nets = {n.name for n in snap.nets} - {""}
    assert nets == {"VIN", "GND", "SW", "VOUT", "SIG_SW", "SIG_LOOP", "SIG_BURIED"}
    layers = {t.net: t.layer for t in snap.tracks if t.net.startswith("SIG")}
    assert layers == {"SIG_SW": 3, "SIG_LOOP": 3, "SIG_BURIED": 5}       # F.Cu, F.Cu, In2.Cu
    assert {t.width_mm for t in snap.tracks if t.net.startswith("SIG")} == {0.2}
    assert (CoupledNoise.FSW, CoupledNoise.VIN, CoupledNoise.VOUT) == (1e6, 12.0, 1.8)


def test_the_board_matches_kipis_validation_snapshot():
    """Counts and coordinates read from KiPI's examples/validation/coupled_noise/
    coupled_noise.kipi.json.gz (v1.0.1)."""
    from coppernicus.engine.synthetic import coupled_noise_board
    snap = coupled_noise_board()
    assert (len(snap.pads), len(snap.vias), len(snap.tracks), len(snap.zones), len(snap.footprints)) == \
        (25, 11, 8, 2, 12)
    tracks = [(t.net, t.layer, t.width_mm, tuple(t.start), tuple(t.end)) for t in snap.tracks]
    assert tracks == [("VIN", 3, 1.0, (2, 10), (10, 10)), ("GND", 3, 1.0, (2, 14), (10, 14)),
                      ("SW", 3, 1.0, (12, 12), (16, 12)), ("VOUT", 3, 1.0, (20, 12), (28, 12)),
                      ("GND", 3, 1.0, (24, 15), (28, 15)),
                      ("SIG_SW", 3, 0.2, (11.5, pytest.approx(14.3)), (24.0, pytest.approx(14.3))),
                      ("SIG_LOOP", 3, 0.2, (3.0, pytest.approx(9.2)), (9.5, pytest.approx(9.2))),
                      ("SIG_BURIED", 5, 0.2, (3.0, pytest.approx(9.2)), (9.5, pytest.approx(9.2)))]
    zones = {z.net: sorted(map(tuple, z.filled[next(iter(z.filled))][0].outline)) for z in snap.zones}
    assert zones == {"SW": [(11.0, 10.0), (11.0, 14.0), (15.0, 10.0), (15.0, 14.0)],
                     "GND": [(0.0, 0.0), (0.0, 20.0), (32.0, 0.0), (32.0, 20.0)]}
    pads = {p.id: (p.net, tuple(p.position)) for p in snap.pads}
    assert [pads[k] for k in ("u1_1", "u1_2", "u1_3", "l1_1", "l1_2", "c2_1", "c2_2")] == [
        ("VIN", (10.0, 10.0)), ("GND", (10.0, 14.0)), ("SW", (12.0, 12.0)), ("SW", (16.0, 12.0)),
        ("VOUT", (20.0, 12.0)), ("VOUT", (24.0, 12.0)), ("GND", (24.0, 15.0))]
    assert [(v.net, tuple(v.position)) for v in snap.vias[:4]] == \
        [("GND", (2, 14.5)), ("GND", (6, 14.5)), ("GND", (10, 14.5)), ("GND", (24, 15.5))]
    assert [(c.name, c.thickness_mm) for c in snap.stackup.copper] == \
        [("F.Cu", 0.035), ("In1.Cu", 0.0175), ("In2.Cu", 0.0175), ("B.Cu", 0.035)]
    assert [(d.thickness_mm, d.epsilon_r) for d in snap.stackup.dielectrics] == \
        [(0.2, 4.5), (1.0, 4.5), (0.2, 4.5)]


# ---------------------------------------------------------------------- project file

@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_files_before_t10_gain_no_key(path):
    p = Project.load(path)
    assert p.aggressors == [] and p.victims == [] and p.noise == NoiseOptions()
    assert not {"aggressors", "victims", "noise"} & set(p.to_dict())


def _aggressor(**kw):
    phase = Phase(inductor="L1", inductance_h=1e-6, switch_net="SW", high_side_ref="U1",
                  high_side_pins=["1"], high_side_net="VIN", low_side_ref="U1", low_side_pins=["2"],
                  low_side_net="GND", input_caps=["C1"], output_caps=["C2"], coss_f=3e-10, qrr_nc=5.0)
    return Aggressor(**{"name": "U1 buck", "vrm": "U1", "fsw_hz": 1e6, "rise_s": 2e-9,
                        "fall_s": 2e-9, "ripple_probes": ["LOAD"], "phases": [phase], **kw})


def test_aggressors_victims_and_noise_round_trip_with_defaults_omitted(tmp_path):
    p = Project(board="b.kipi.json.gz")
    p.aggressors = [_aggressor()]
    p.victims = [Victim("SIG_LOOP", preset="cmos", driver_ref="U5", driver_pins=["1"],
                        receiver_ref="U6", receiver_pins=["1"], logic_v=3.3),
                 Victim("SIG_SW", name="near SW", preset="custom", driver_ohm=50.0,
                        receiver_ohm=1e4, receiver_pf=2.0, termination_ohm=100.0,
                        noise_margin_v=0.2)]
    p.noise = NoiseOptions(field_per_decade=5, board_model=False)
    out = p.save(tmp_path / "p.coppernicus.yml")
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["aggressors"] == [{
        "name": "U1 buck", "vrm": "U1", "fsw_hz": 1e6, "rise_s": 2e-9, "fall_s": 2e-9,
        "ripple_probes": ["LOAD"],
        "phases": [{"inductor": "L1", "inductance_h": 1e-6, "switch_net": "SW",
                    "high_side": {"ref": "U1", "net": "VIN", "pins": ["1"]},
                    "low_side": {"ref": "U1", "net": "GND", "pins": ["2"]},
                    "input_caps": ["C1"], "output_caps": ["C2"], "coss_f": 3e-10, "qrr_nc": 5.0}]}]
    assert data["victims"] == [
        {"net": "SIG_LOOP", "preset": "cmos", "driver": {"ref": "U5", "pins": ["1"]},
         "receiver": {"ref": "U6", "pins": ["1"]}, "logic_v": 3.3},
        {"net": "SIG_SW", "name": "near SW", "preset": "custom", "driver_ohm": 50.0,
         "receiver_ohm": 1e4, "receiver_pf": 2.0, "termination_ohm": 100.0, "noise_margin_v": 0.2}]
    assert data["noise"] == {"board_model": False, "field_per_decade": 5}
    back = Project.load(out)
    assert back.aggressors == p.aggressors and back.victims == p.victims and back.noise == p.noise
    assert back.victims[0].label == "SIG_LOOP" and back.victims[1].label == "near SW"


def test_an_entered_qrr_of_zero_and_a_draft_aggressor_survive_save_and_load(tmp_path):
    """Qrr 0 (a Schottky or GaN low side) is entered, not missing; an aggressor still
    without its switching frequency, edges or phases loads (it is refused at solve)."""
    p = Project(board="b.kipi.json.gz")
    a = _aggressor()
    a.phases[0].qrr_nc = 0.0
    p.aggressors = [a, Aggressor("draft", vrm="U9")]
    back = Project.load(p.save(tmp_path / "p.coppernicus.yml"))
    assert back.aggressors[0].phases[0].qrr_nc == 0.0
    d = back.aggressors[1]
    assert (d.fsw_hz, d.rise_s, d.fall_s, d.phases) == (None, None, None, [])
    assert yaml.safe_load((tmp_path / "p.coppernicus.yml").read_text())["aggressors"][1] == \
        {"name": "draft", "vrm": "U9"}


def _with(**blocks):
    return {"board": "b.kipi.json.gz", "rails": [], **blocks}


AGG = {"name": "A", "vrm": "U1", "fsw_hz": 1e6, "rise_s": 2e-9, "fall_s": 2e-9}
PH = {"inductor": "L1", "inductance_h": 1e-6, "high_side": {"ref": "U1", "net": "VIN"},
      "low_side": {"ref": "U1", "net": "GND"}}
VIC = {"net": "SIG", "preset": "cmos"}


@pytest.mark.parametrize("blocks, words", [
    ({"aggressors": [{**AGG, "speed": 1}]}, "unknown aggressor key speed"),
    ({"aggressors": [{**AGG, "topology": "flyback"}]}, "topology"),
    ({"aggressors": [{**AGG, "fsw_hz": 0}]}, "fsw_hz"),
    ({"aggressors": [{**AGG, "fsw_hz": float("nan")}]}, "fsw_hz"),
    ({"aggressors": [{**AGG, "rise_s": -1e-9}]}, "rise_s"),
    ({"aggressors": [{**AGG, "fall_s": "fast"}]}, "fall_s"),
    ({"aggressors": [{**AGG, "duty": 0}]}, "duty"),
    ({"aggressors": [{**AGG, "duty": 1.0}]}, "duty"),
    ({"aggressors": [{**AGG, "vin_v": 0}]}, "vin_v"),
    ({"aggressors": [{**AGG, "load_a": -1}]}, "load_a"),
    ({"aggressors": [{**AGG, "min_ring_zeta": 0}]}, "min_ring_zeta"),
    ({"aggressors": [{**AGG, "ripple_probes": "LOAD"}]}, "ripple_probes"),
    ({"aggressors": [AGG, AGG]}, "unique"),
    ({"aggressors": [{**AGG, "name": ""}]}, "name"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "turns": 3}]}]}, "unknown phase key turns"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "inductor_axis": "diagonal"}]}]}, "inductor_axis"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "inductance_h": -1e-6}]}]}, "inductance_h"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "coss_f": 0}]}]}, "coss_f"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "qrr_nc": -1}]}]}, "qrr_nc"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "qrr_c": 5e-9}]}]}, "qrr_c"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "inductor_shielded": "yes"}]}]}, "inductor_shielded"),
    ({"aggressors": [{**AGG, "phases": [{**PH, "high_side": {"net": "VIN"}}]}]}, "high_side needs its part"),
    ({"victims": [{"net": "SIG"}]}, "preset"),
    ({"victims": [{**VIC, "preset": "ttl"}]}, "preset"),
    ({"victims": [{**VIC, "driver_ohm": 50.0}]}, "custom"),
    ({"victims": [{**VIC, "preset": "custom", "driver_ohm": 50.0}]}, "receiver_ohm"),
    ({"victims": [{**VIC, "logic_v": -3.3}]}, "logic_v"),
    ({"victims": [{**VIC, "noise_margin_v": 0}]}, "noise_margin_v"),
    ({"victims": [{**VIC, "driver": {"pins": ["1"]}}]}, "driver needs its part"),
    ({"victims": [{**VIC, "color": "red"}]}, "unknown victim key color"),
    ({"victims": [VIC, VIC]}, "unique"),
    ({"victims": [{"preset": "cmos"}]}, "net"),
    ({"noise": {"harmonics": 10}}, "unknown noise option harmonics"),
    ({"noise": {"max_harmonics": 0}}, "max_harmonics"),
    ({"noise": {"max_harmonics": 1.5}}, "max_harmonics"),
    ({"noise": {"field_per_decade": 0}}, "field_per_decade"),
    ({"noise": {"board_model": "yes"}}, "board_model"),
    ({"noise": {"f_max_hz": -1}}, "f_max_hz"),
    ({"noise": {"segment_mm": 0}}, "segment_mm"),
    ({"noise": {"dead_time_ns": -1}}, "dead_time_ns"),
    ({"aggressors": {"A": 1}}, "list"),
])
def test_bad_noise_values_are_refused(blocks, words):
    with pytest.raises(ProjectError, match=words):
        Project.from_dict(_with(**blocks))


def test_noise_blocks_load_with_their_defaults():
    p = Project.from_dict(_with(aggressors=[{**AGG, "phases": [PH]}], victims=[VIC]))
    (a,) = p.aggressors
    assert (a.topology, a.min_ring_zeta, a.soft_edge_zeta, a.vin_v, a.duty, a.load_a) == \
        ("auto", 0.05, 0.5, None, None, None)
    (ph,) = a.phases
    assert (ph.high_side_pins, ph.coss_f, ph.qrr_nc, ph.inductor_axis, ph.inductor_shielded) == \
        (None, None, None, "vertical", True)
    (v,) = p.victims
    assert (v.name, v.label, v.driver_ref, v.logic_v, v.driver_ohm) == ("", "SIG", "", None, None)
    assert math.isclose(p.noise.window_mm, 3.0) and p.noise.board_model is True


def test_project_choices_and_defaults_are_kipis():
    """project.py copies KiPI's presets, topologies, axes and defaults (it cannot import
    KiPI): they must match."""
    from coppernicus.engine.noise import kipi_noise_choices
    from coppernicus.project import CUSTOM_PRESET, INDUCTOR_AXES, NOISE_TOPOLOGIES, VICTIM_PRESETS
    k = kipi_noise_choices()
    assert (k["presets"], k["custom"], k["topologies"], k["axes"]) == \
        (VICTIM_PRESETS, CUSTOM_PRESET, NOISE_TOPOLOGIES, INDUCTOR_AXES)
    assert k["options"] == {key: getattr(NoiseOptions(), key) for key in k["options"]}
    assert set(k["options"]) == set(vars(NoiseOptions()))
    a, ph = Aggressor("x"), Phase()
    assert k["zetas"] == (a.min_ring_zeta, a.soft_edge_zeta)
    assert k["inductor"] == (ph.inductor_axis, ph.inductor_shielded)


# ---------------------------------------------------------------------- KiPI's validation
# KiPI tests/accuracy/test_noise_accuracy.py and tests/noise/test_noise_solve.py, the same
# board, setup and tolerances: the victim beside the hot loop's VIN track is the noisiest
# (inductive), the one beside the switch node's pour has a capacitive and an inductive part of
# one size, the one behind the plane is shielded.

def _board():
    from coppernicus.engine import Board
    from coppernicus.engine.synthetic import coupled_noise_board
    return Board(coupled_noise_board())


def _solve_keeping_kipi(project, name="U1", **kw):
    """Our result and KiPI's own NoiseResult of the same solve."""
    import coppernicus.engine.noise as noise
    kept = {}
    real = noise.solve_noise

    def keep(*a, **k):
        kept["kipi"] = real(*a, **k)
        return kept["kipi"]
    mp = pytest.MonkeyPatch()
    mp.setattr(noise, "solve_noise", keep)
    try:
        return noise.solve_aggressor(_board(), project, name, **kw), kept["kipi"]
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def solved():
    """KiPI's setup (Qrr not entered) with the LOAD probe and logic_v 3.3."""
    from coppernicus.engine.synthetic import coupled_noise_project
    return _solve_keeping_kipi(coupled_noise_project(logic_v=3.3))


def test_each_victim_is_coupled_the_way_its_route_says(solved):
    r, _k = solved
    sw, loop = r.victim("SIG_SW").peak_by_v, r.victim("SIG_LOOP")
    assert sw["capacitive"] > 10 * loop.peak_by_v["capacitive"] + 1e-3
    assert 0.5 < sw["capacitive"] / sw["inductive"] < 2.0
    assert loop.dominant == "inductive"
    assert set(loop.peak_by_v) == {"capacitive", "inductive", "common"}


def test_the_plane_shields_the_buried_victim(solved):
    r, _k = solved
    assert r.victim("SIG_BURIED").peak_v < 0.05 * r.victim("SIG_LOOP").peak_v


def test_the_ranking_is_hot_loop_then_switch_node_then_shielded(solved):
    r, _k = solved
    peaks = [r.victim(n).peak_v for n in ("SIG_LOOP", "SIG_SW", "SIG_BURIED")]
    assert peaks == sorted(peaks, reverse=True)
    assert [v.name for v in r.victims] == ["SIG_SW", "SIG_LOOP", "SIG_BURIED"]   # project order


def test_hot_loop_and_waveforms(solved):
    """KiPI tests/noise/test_noise_solve.py:42-80: the loop traced through the copper over
    In1.Cu, the switch node Coss + copper (more than the 4 mm pour's plate capacitance), the
    ringing 1/(2π√(LC)), the duty, the inductor's mean current and the harmonics' reach."""
    r, _k = solved
    (h,) = r.hot_loops
    assert 0.1e-9 < h.loop_l_h < 3e-9 and h.traced and h.image_plane == "In1.Cu"
    plate = 8.854e-15 * 4.5 * 16 / 0.2
    assert plate < h.c_copper_f < 1.6 * plate
    assert h.c_sw_f == pytest.approx(300e-12 + h.c_copper_f) and h.coss_entered
    assert h.f_ring_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(h.loop_l_h * h.c_sw_f)), rel=1e-6)
    assert 0 < h.zeta < 1 and not h.qrr_entered and h.qrr_nc == pytest.approx(5.0)
    assert r.duty == pytest.approx(1.8 / 12) and r.topology == "buck"
    assert float(r.inductor_a[0].mean()) == pytest.approx(5.0, rel=1e-3)
    assert r.f_max_hz >= 10 / (math.pi * 2e-9) and max(r.switch_node_v[0]) > 12.0
    assert len(r.t_s) == len(r.switch_node_v[0]) == len(r.victims[0].receiver_v)
    assert r.board["used"] and r.board["caps_modelled"] == ["C1", "C2"] and r.board["caps_missing"] == []


def test_the_ripple_holds_kipis_four_tolerance_values(solved):
    """Task 11 stacks KiPI's ripple_peaks values (kipi/tolerance/gather.py:96-108): they are
    the RippleResult's four, exactly; the load's ripple is under twice the device pins', the
    input's above the load's (test_probe_ripple.py:25-59)."""
    from coppernicus.engine.noise import ripple_peaks
    r, k = solved
    load = r.ripple_at("LOAD")
    assert (load.below_v, load.above_v, load.below_bw_v, load.above_bw_v) == ripple_peaks(k)["LOAD"]
    assert (load.where, load.net, load.ground_net, load.ref, load.pins) == \
        ("probe", "VOUT", "GND", "U2", ["1"])
    out, inp = r.ripple_at("output"), r.ripple_at("input")
    assert (out.where, out.ref, out.pins, inp.ref, inp.pins) == \
        ("device pins", "L1", ["2"], "U1", ["1"])
    assert 0 < load.pp_v < 2 * out.pp_v and inp.pp_v > load.pp_v
    assert load.pp_bw_v < load.pp_v and load.rms_v > 0
    assert load.pp_v == pytest.approx(load.v.max() - load.v.min())


def test_a_stand_in_leaves_every_victim_not_judged_whatever_its_peak(solved):
    r, _k = solved
    assert r.stand_ins == ["Qrr not entered on phase 1: KiPI's 5 nC sets the hard edge (enter 0 for "
                           "a Schottky or GaN low side)"]
    assert r.verdict == "not_judged" and r.failed == []
    assert [e["item"] for e in r.not_judged] == ["SIG_SW", "SIG_LOOP", "SIG_BURIED"]
    loop = r.victim("SIG_LOOP")
    assert loop.margin_v == pytest.approx(0.8) and loop.limit == "logic_v JESD8 margin (V)"
    assert loop.passed is None and loop.not_judged.startswith("Qrr not entered on phase 1")
    assert loop.not_judged.endswith(f"peak {loop.peak_v * 1e3:.4g} mV = "
                                    f"{100 * loop.peak_v / 0.8:.3g} % of the margin")
    assert loop.margin_from == "the JESD8 levels of a 3.3 V family, VIL 0.8 V, VIH 2 V"


@pytest.fixture(scope="module")
def entered():
    """Every value entered (Qrr 5 nC), logic_v 3.3, plus a hiz and an lvds victim."""
    from coppernicus.engine.synthetic import coupled_noise_project
    p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
    p.victims += [Victim("SIG_BURIED", name="buried hiz", preset="hiz", driver_ref="U7",
                         driver_pins=["1"], receiver_ref="U8", receiver_pins=["1"], logic_v=3.3),
                  Victim("SIG_SW", name="sw lvds", preset="lvds", driver_ref="U3", driver_pins=["1"],
                         receiver_ref="U4", receiver_pins=["1"])]
    from coppernicus.engine.noise import solve_aggressor
    return solve_aggressor(_board(), p, "U1")


def test_with_everything_entered_the_victims_are_judged(entered):
    r = entered
    assert r.stand_ins == []
    loop = r.victim("SIG_LOOP")
    assert loop.passed is True and loop.ratio == pytest.approx(loop.peak_v / 0.8)
    lvds = r.victim("sw lvds")
    assert (lvds.margin_v, lvds.margin_from, lvds.limit) == \
        (pytest.approx(0.1), "preset lvds", "preset lvds margin (V)")
    assert lvds.passed == (lvds.peak_v <= 0.1)


def test_a_line_already_past_its_threshold_is_never_judged(entered):
    """KiPI's hiz at 3.3 V sits at 1.65 V held high, under VIH 2.0 V, yet keeps a 0.35 V
    margin (model.py:131-133) and would pass: Coppernicus never judges such a line."""
    hiz = entered.victim("buried hiz")
    assert hiz.margin_v == pytest.approx(0.35)
    assert hiz.passed is None
    assert hiz.not_judged.startswith("held high it sits at 1.65 V, beyond its 2 V threshold: no "
                                     "margin left")
    assert entered.verdict == "not_judged"
    assert {e["item"] for e in entered.not_judged} == {"buried hiz"} | (
        set() if entered.victim("sw lvds").passed is not None else {"sw lvds"})
    high = next(s for s in hiz.states if s["name"] == "high")
    assert high["level_v"] == pytest.approx(1.65) and high["threshold_v"] == pytest.approx(2.0)


# ---------------------------------------------------------------------- mapping

def _project(**agg):
    from coppernicus.engine.synthetic import coupled_noise_project
    p = coupled_noise_project(qrr_nc=5.0)
    for k, v in agg.items():
        setattr(p.aggressors[0], k, v)
    return p


def test_the_setup_carries_the_aggressor_victims_probes_and_models():
    from coppernicus.engine.noise import noise_setup
    setup, notes = noise_setup(_board(), _project(), "U1")
    (a,) = setup.aggressors
    (ph,) = a.phases
    assert (a.vrm, a.fsw_hz, a.rise_s, ph.inductor, ph.switch_net) == ("U1", 1e6, 2e-9, "L1", "SW")
    assert (ph.high_side.ref, ph.high_side.net, ph.high_side.pins) == ("U1", "VIN", ["1"])
    assert ph.qrr_c == pytest.approx(5e-9) and ph.coss_f == pytest.approx(300e-12)
    assert [p.name for p in setup.probes] == ["LOAD"]
    assert sorted(m.name for m in setup.caps) == ["10u", "47u"]
    assert [v.name for v in setup.victims] == ["SIG_SW", "SIG_LOOP", "SIG_BURIED"]
    assert setup.victims[0].driver.label() == "U3[1]" and setup.noise.board_model is True
    assert notes == []


def test_presets_take_their_terminations_and_custom_its_own():
    """KiPI's Victim(preset=...) alone keeps cmos terminations: apply_preset must run."""
    from coppernicus.engine.noise import noise_setup
    p = _project()
    p.victims = [Victim("SIG_SW", preset="open_drain"),
                 Victim("SIG_LOOP", preset="custom", driver_ohm=12.0, receiver_ohm=1e4,
                        receiver_pf=7.0, driver_low_ohm=6.0)]
    setup, _ = noise_setup(_board(), p, "U1")
    od, cu = setup.victims
    assert (od.driver_ohm, od.driver_low_ohm, od.receiver_pf, od.logic_v) == (4700.0, 20.0, 5.0, 3.3)
    assert (cu.preset, cu.driver_ohm, cu.receiver_ohm, cu.receiver_pf, cu.driver_low_ohm) == \
        ("custom", 12.0, 1e4, 7.0, 6.0)


@pytest.mark.parametrize("change, words", [
    (dict(fsw_hz=None), "enter its fsw_hz"),
    (dict(rise_s=None, fall_s=None), "enter its rise_s, fall_s"),
    (dict(phases=[]), "add its phases"),
    (dict(vrm="U9"), "U9 is not a source"),
    (dict(ripple_probes=["NOPE"]), "no probe named NOPE"),
])
def test_what_stops_a_solve_is_named(change, words):
    from coppernicus.engine import EngineError
    from coppernicus.engine.noise import noise_setup
    with pytest.raises(EngineError, match=words):
        noise_setup(_board(), _project(**change), "U1")


def test_pins_resolve_and_a_missing_pin_or_net_names_its_owner():
    from coppernicus.engine import EngineError
    from coppernicus.engine.noise import noise_setup
    p = _project()
    p.aggressors[0].phases[0].high_side_pins = ["7"]
    with pytest.raises(EngineError, match="phase 1 \\(high-side pins\\)"):
        noise_setup(_board(), p, "U1")
    p = _project()
    p.victims[0].receiver_pins = ["9"]
    with pytest.raises(EngineError, match="Victim SIG_SW \\(receiver pins\\)"):
        noise_setup(_board(), p, "U1")
    p = _project()
    p.victims = [Victim("NOT_A_NET", preset="cmos")]
    with pytest.raises(EngineError, match="NOT_A_NET is not on the board"):
        noise_setup(_board(), p, "U1")
    with pytest.raises(EngineError, match="No aggressor named X"):
        noise_setup(_board(), p, "X")


def test_an_input_rail_without_a_source_of_its_own_still_maps():
    """Brief Q7: T9's ac_setup maps one rail and refuses one without a source; an aggressor
    with vin_v entered needs no source on its input, so the setup is built per object."""
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import ac_setup
    from coppernicus.engine.noise import noise_setup
    p = _project(vin_v=12.0)
    p.rails = [r for r in p.rails if r.net != "VIN"]
    setup, _ = noise_setup(_board(), p, "U1")
    assert sorted(m.name for m in setup.caps) == ["10u", "47u"]       # C1 on VIN included
    with pytest.raises(EngineError, match="no source"):
        ac_setup(_board(), p, "VIN")


def test_the_regulators_switching_frequency_is_the_default_and_an_explicit_one_wins():
    """Coordinator's change to Q1: the aggressor's own fsw_hz wins; without one, its
    regulator's (Source.fsw_hz, KiCad field PI_FSW or the project file) with an assumption
    line naming where it came from; with neither, refused."""
    from coppernicus.engine import EngineError
    from coppernicus.engine.noise import _context, noise_setup
    p = _project()
    p.rails[1].sources[0].fsw_hz = 2.2e6
    assert noise_setup(_board(), p, "U1")[0].aggressors[0].fsw_hz == 1e6        # explicit wins
    assert _context(_board(), p, "U1").fsw_note == ""
    p.aggressors[0].fsw_hz = None
    assert noise_setup(_board(), p, "U1")[0].aggressors[0].fsw_hz == 2.2e6
    assert _context(_board(), p, "U1").fsw_note == (
        "Switching frequency 2.2 MHz from the source U1 (the project file): the aggressor gives "
        "no fsw_hz of its own, which would win.")
    assert "(KiCad field PI_FSW)" in _context(_board(), p, "U1", fields_rails=["VOUT"]).fsw_note
    p.rails[1].sources[0].fsw_hz = None
    with pytest.raises(EngineError, match="enter its fsw_hz"):
        noise_setup(_board(), p, "U1")


def _quick(p, **kw):
    """A solve without the board model (under a second)."""
    from coppernicus.engine.noise import solve_aggressor
    p.noise = NoiseOptions(board_model=False)
    return solve_aggressor(_board(), p, "U1", **kw)


def test_the_source_default_frequency_is_an_assumption_line():
    p = _project(fsw_hz=None)
    p.rails[1].sources[0].fsw_hz = 1e6
    r = _quick(p)
    assert r.fsw_hz == 1e6
    assert any(a.startswith("Switching frequency 1 MHz from the source U1 (the project file)")
               for a in r.assumptions)


def test_without_the_board_model_shared_ground_is_left_out_and_nothing_is_judged():
    p = _project()
    for v in p.victims:
        v.noise_margin_v = 1.0
    r = _quick(p)
    assert not r.board["used"] and "shared-ground coupling left out: board model off " \
        "(noise.board_model)" in r.stand_ins
    assert all(v.passed is None and "common" not in v.peak_by_v for v in r.victims)
    assert [x.where for x in r.ripple] == ["capacitor bank", "capacitor bank"]
    assert any(a.startswith("Board model off (noise.board_model)") for a in r.assumptions)
    assert any("The ripple at AC probes needs the board model" in w for w in r.warnings)


def test_kipis_refusals_become_engine_errors_and_a_cancel_cancels():
    from coppernicus.engine import EngineError
    from coppernicus.engine.solve import Cancelled
    with pytest.raises(EngineError, match="Noise solve of U1: .*boost cannot take 12 V to 1.8 V"):
        _quick(_project(topology="boost"))
    with pytest.raises(Cancelled):
        _quick(_project(), cancelled=lambda: True)


def test_the_progress_hooks_and_a_partial_result():
    seen, parts, fractions = [], [], []
    r = _quick(_project(), progress=seen.append, partial=parts.append, fraction=fractions.append)
    assert any(m.startswith("== Waveforms") for m in seen)
    assert parts and parts[0].partial and parts[0].victims == [] and parts[0].verdict == ""
    assert parts[0].stand_ins == [] and parts[0].assumptions == []
    assert len(parts[0].switch_node_v[0]) == len(r.switch_node_v[0]) and fractions
    assert not r.partial and r.verdict


def test_stand_ins_for_coss_models_ends_switch_node_and_harmonics():
    p = _project()
    ph = p.aggressors[0].phases[0]
    ph.coss_f, ph.qrr_nc, ph.switch_net, ph.input_caps = None, None, "", []
    p.capacitors = [p.capacitors[0]]                     # C2 loses its model
    p.victims[0].driver_ref, p.victims[0].receiver_ref = "", ""
    p.noise = NoiseOptions(board_model=False, max_harmonics=100)
    for v in p.victims:
        v.noise_margin_v = 1.0
    from coppernicus.engine.noise import solve_aggressor
    r = solve_aggressor(_board(), p, "U1")
    text = "; ".join(r.stand_ins)
    for words in ("Coss not entered on phase 1: KiPI's 200 pF", "Qrr not entered on phase 1",
                  "without a model: C2", "capacitive coupling of phase 1 left out: no switch net",
                  "harmonics stop at 100 MHz (noise.max_harmonics 100)",
                  "phase 1: no input capacitor loop is drawn, so the hot loop is the package only"):
        assert words in text, words
    assert any(w.startswith("C1 is on the rails but in no phase's list") for w in r.warnings)
    sw = r.victim("SIG_SW")
    assert "driver and receiver not both entered: KiPI guessed U3[1] and U4[1]" in sw.stand_ins
    assert r.harmonics_capped and r.harmonics == 100


def test_seed_phases_reads_the_board_and_a_solve_never_calls_it(monkeypatch):
    import coppernicus.engine.noise as noise
    p = _project()
    (ph,) = noise.seed_phases(_board(), p, "U1")
    assert (ph.inductor, ph.inductance_h, ph.switch_net, ph.input_caps, ph.output_caps) == \
        ("L1", pytest.approx(1e-6), "SW", ["C1"], ["C2"])
    assert (ph.high_side_ref, ph.high_side_net, ph.high_side_pins) == ("U1", "VIN", ["1"])
    assert (ph.low_side_ref, ph.low_side_net, ph.low_side_pins) == ("U1", "GND", ["2"])

    def boom(*a, **k):
        raise AssertionError("seed_phases called at solve")
    monkeypatch.setattr(noise, "_kipi_seed_phases", boom)
    assert _quick(p).hot_loops
    assert noise.guess_ends(_board(), "SIG_LOOP") == (("U5", ["1"]), ("U6", ["1"]))


# ---------------------------------------------------------------------- verdicts (hand-built)

def _victim(name="V", peak=0.5, margin=0.8, stand_ins=(), limit="noise_margin_v (V)"):
    import numpy as np

    from coppernicus.results import VictimResult
    return VictimResult(name, "SIG", "U1[1]", "U2[1]", True, "cmos", margin,
                        "noise_margin_v" if margin is not None else "", limit if margin else "",
                        peak, {"capacitive": peak / 2, "inductive": peak}, "inductive",
                        peak / margin if margin else None, [], 10.0, 5.0, {}, {}, [], np.zeros(8),
                        stand_ins=list(stand_ins))


def _result(victims, name="A"):
    import numpy as np

    from coppernicus.results import NoiseResult
    return NoiseResult(name, "U1", "VOUT", "VIN", "GND", "buck", 12.0, 1.8, 5.0, 0.83, 0.15, 0.9, 1,
                       [], 1e6, 2e-9, 2e-9, 1.6e9, 1600, False, [1.5], [], np.zeros(8),
                       [np.zeros(8)], [np.zeros(8)], [""], victims=list(victims))


def test_no_margin_is_ranked_an_entered_margin_passes_or_fails():
    from coppernicus.coupled_noise import judge
    r = judge(_result([_victim(margin=None)]))
    assert (r.verdict, r.not_judged, r.victims[0].passed) == ("not_judged", [], None)
    assert r.verdict_note.startswith("No noise margins set: nothing is judged")
    assert judge(_result([_victim(peak=0.5)])).verdict == "pass"
    r = judge(_result([_victim(peak=0.9), _victim("W", peak=0.1)]))
    assert r.verdict == "fail" and [e["item"] for e in r.failed] == ["V"]
    assert r.failed[0]["limit"] == "noise_margin_v (V)" and "above its margin 800 mV" in r.failed[0]["reason"]


def test_a_stand_in_is_not_judged_even_over_the_margin_and_aggressors_combine():
    from coppernicus.coupled_noise import judge, noise_verdict
    over = judge(_result([_victim(peak=1.6, stand_ins=["Coss not entered on phase 1: x"])], "A"))
    assert over.verdict == "not_judged" and over.victims[0].passed is None
    assert over.not_judged[0]["reason"] == ("Coss not entered on phase 1: x; peak 1600 mV = 200 % of "
                                            "the margin")
    ok = judge(_result([_victim(peak=0.1)], "B"))
    bad = judge(_result([_victim(peak=0.9)], "C"))
    none = judge(_result([_victim(margin=None)], "D"))
    assert noise_verdict([ok, none])[0] == "pass"
    assert noise_verdict([ok, over])[0] == "not_judged"
    verdict, note, failed, not_judged = noise_verdict([ok, over, bad])
    assert verdict == "fail" and failed[0]["aggressor"] == "C" and not_judged[0]["aggressor"] == "A"
    assert noise_verdict([none])[:2] == ("not_judged", "No noise margins set: nothing is judged; "
                                                       "the victims are ranked")


# ---------------------------------------------------------------------- outputs

def test_summary_keys_and_no_nan(solved, tmp_path):
    import json

    from coppernicus.coupled_noise import noise_summary
    r, _k = solved
    doc = noise_summary([r], {"U1": ("a.csv", ["a.png"])}, warnings=["w"])
    json.dumps(doc, allow_nan=False)
    assert set(doc) == {"kind", "verdict", "verdict_note", "failed", "not_judged", "warnings",
                        "aggressors"}
    assert doc["kind"] == "noise" and doc["warnings"] == ["w"]
    (a,) = doc["aggressors"]
    assert {"aggressor", "vrm", "rail", "input_net", "ground", "topology", "vin_v", "vout_v", "iout_a",
            "iin_a", "duty", "efficiency", "phases", "operating_notes", "fsw_hz", "rise_s", "fall_s",
            "f_max_hz", "harmonics", "harmonics_capped", "inductor_ripple_a", "hot_loops", "ripple",
            "victims", "board", "emi", "stand_ins", "verdict", "verdict_note", "failed", "not_judged",
            "csv_file", "png_files", "assumptions", "warnings", "stats"} == set(a)
    assert (a["csv_file"], a["png_files"]) == ("a.csv", ["a.png"])
    load = next(x for x in a["ripple"] if x["name"] == "LOAD")
    assert {"name", "where", "net", "ground_net", "ref", "pins", "pp_mv", "pp_20mhz_mv", "below_mv",
            "above_mv", "below_20mhz_mv", "above_20mhz_mv", "rms_mv"} == set(load)
    assert load["below_mv"] == pytest.approx(r.ripple_at("LOAD").below_v * 1e3)
    v = next(x for x in a["victims"] if x["name"] == "SIG_LOOP")
    assert {"name", "net", "driver", "receiver", "ends_entered", "preset", "margin_mv", "margin_from",
            "limit", "peak_mv", "peak_by_mv", "dominant", "ratio", "states", "length_mm",
            "coupled_mm", "cm_f", "m_h", "splits", "stand_ins", "passed", "not_judged",
            "warnings"} == set(v)
    assert v["margin_mv"] == pytest.approx(800.0) and v["passed"] is None
    assert set(v["states"][0]) == {"name", "level_v", "threshold_v", "driver_ohm", "excursion_mv",
                                   "margin_mv"}
    assert {"phase", "loop_l_h", "c_sw_f", "coss_entered", "qrr_nc", "qrr_entered", "f_ring_hz",
            "zeta"} <= set(a["hot_loops"][0])
    assert {"used", "note", "unknowns", "field_solves", "ports", "caps_modelled", "caps_missing"} == \
        set(a["board"])
    assert {"height_mm", "distance_m", "hotspots", "radiated_dbuv_m", "radiated_hz",
            "cispr32_b_dbuv_m", "radiated_source", "notes"} == set(a["emi"])
    assert {"seconds", "triangles", "unknowns", "ports", "field_solves", "memory_peak_mb",
            "memory_peak_raised"} <= set(a["stats"])
    assert doc["not_judged"][0]["aggressor"] == "U1"


def test_text_csv_and_pngs(solved, tmp_path):
    import csv

    from coppernicus.coupled_noise import (
        map_xy,
        noise_summary,
        noise_text,
        write_csv,
        write_field_png,
        write_png,
    )
    r, _k = solved
    lines = noise_text(noise_summary([r]))
    text = "\n".join(lines)
    for words in ("Aggressor U1", "ripple LOAD", "victim SIG_LOOP", "NOT JUDGED SIG_LOOP",
                  "assumption: Coupled noise (KiPI)", "for scale, not judged", "Verdict: not_judged"):
        assert words in text, words
    path = write_csv(r, tmp_path / "n.csv")
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0][:3] == ["t_s", "phase1_switch_node_v", "phase1_inductor_a"]
    assert {"ripple_LOAD_v", "ripple_LOAD_20mhz_v", "victim_SIG_LOOP_v"} <= set(rows[0])
    assert len(rows) == 1 + len(r.t_s)
    assert write_png(r, tmp_path / "w.png").stat().st_size > 1000
    outline = _board().outline()
    for kind in ("H", "E"):
        assert write_field_png(r, kind, tmp_path / f"{kind}.png", outline).stat().st_size > 1000
    # The map's strongest point is where KiPI's strongest hotspot is (rows from y0, y down).
    import numpy as np
    m = r.emi.h_map
    i, j = np.unravel_index(np.argmax(m.values), m.values.shape)
    x, y = map_xy(m, i, j)
    spot = next(s for s in r.emi.hotspots if s["kind"] == "H")
    cell = (m.bounds[2] - m.bounds[0]) / (m.values.shape[1] - 1)
    assert math.hypot(x - spot["x_mm"], y - spot["y_mm"]) < 3 * cell + 1.0


# ---------------------------------------------------------------------- cost and memory

def test_mesh_estimate_meshes_as_the_solve_does(solved):
    from coppernicus.engine.noise import mesh_estimate, noise_cost_estimate
    from coppernicus.engine.synthetic import coupled_noise_project
    r, _k = solved
    size = mesh_estimate(_board(), coupled_noise_project(logic_v=3.3), "U1")
    assert size.board_model and size.triangles == int(r.stats["triangles"])
    assert size.ports == int(r.stats["ports"]) and size.field_solves == int(r.stats["field_solves"])
    assert size.harmonics == r.harmonics and size.unknowns >= int(r.stats["unknowns"])
    est = noise_cost_estimate(size, r.stats)
    assert est.seconds == pytest.approx(r.stats["seconds"], rel=1e-6)
    assert noise_cost_estimate(size).seconds is None


def test_the_ram_estimate_is_the_measured_upper_bound_and_warns_past_the_machine():
    from coppernicus.engine.noise import noise_ram_estimate
    e = noise_ram_estimate(175_084, 24e9)
    assert e.bytes == pytest.approx(0.2e9 + 36e3 * 175_084) and not e.risk
    assert e.bytes > 5.05e9                       # above KiPI's four-phase example's measured peak
    big = noise_ram_estimate(1_000_000, 16e9)
    assert big.risk and "could swap" in big.text and "extrapolated" in big.text
    assert "cannot be read" in noise_ram_estimate(1000, None).text


def test_the_example_passes_and_its_tight_copy_fails(tmp_path, capsys):
    """examples/noise: every value entered, 3.3 V logic; SIG_LOOP's 655 mV is 82 % of the
    0.8 V JESD8 margin (pass) and above noise-tight's 0.5 V (fail)."""
    import json

    from coppernicus.cli import EXIT_LIMIT, EXIT_OK, main
    folder = ROOT / "examples" / "noise"
    assert main(["noise", str(folder / "noise.coppernicus.yml"), "--json", "--out", str(tmp_path)]) == EXIT_OK
    (a,) = json.loads(capsys.readouterr().out)["aggressors"]
    assert a["stand_ins"] == [] and [v["passed"] for v in a["victims"]] == [True, True, True]
    peaks = {v["name"]: f"{v['peak_mv']:.4g}" for v in a["victims"]}
    assert peaks == {"SIG_SW": "32.67", "SIG_LOOP": "655.4", "SIG_BURIED": "1.425"}
    loop = next(v for v in a["victims"] if v["name"] == "SIG_LOOP")
    assert loop["ratio"] == pytest.approx(0.82, abs=0.01)
    rp = {x["name"]: x for x in a["ripple"]}

    def four(x, *keys):
        return [f"{x[k]:.4g}" for k in keys]
    assert four(rp["LOAD"], "pp_mv", "below_20mhz_mv", "above_20mhz_mv") == ["18.45", "4.727", "7.006"]
    assert four(rp["input"], "pp_mv", "pp_20mhz_mv") == ["1.534e+04", "1873"]
    assert four(rp["output"], "pp_mv", "pp_20mhz_mv") == ["1878", "268.9"]
    assert main(["noise", str(folder / "noise-tight.coppernicus.yml"), "--json",
                 "--out", str(tmp_path)]) == EXIT_LIMIT
    doc = json.loads(capsys.readouterr().out)
    assert [e["item"] for e in doc["failed"]] == ["SIG_LOOP"]


def test_a_phase_without_its_hot_loop_capacitors_is_not_judged():
    """Without the phase's input capacitors KiPI takes a buck's hot loop as the package alone:
    SIG_LOOP's 655 mV falls to under 1 mV. That must never pass."""
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.synthetic import coupled_noise_project
    p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
    p.aggressors[0].phases[0].input_caps = []
    r = solve_aggressor(_board(), p, "U1")
    loop = r.victim("SIG_LOOP")
    assert loop.peak_v < 0.01 and loop.passed is None
    assert loop.not_judged.startswith("phase 1: no input capacitor loop is drawn, so the hot loop is "
                                      "the package only")
    assert r.verdict == "not_judged"


def test_listed_capacitors_without_a_drawn_loop_are_not_judged():
    """Coordinator's C1: KiPI draws a capacitor's loop only from its pads on the exact input
    or output net and the low-side ground. C1 and C2 swapped: the hot loop shrinks to the
    ESL and package, SIG_LOOP falls to about 1.5 mV and passed before; now not judged."""
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.synthetic import coupled_noise_project
    p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
    ph = p.aggressors[0].phases[0]
    ph.input_caps, ph.output_caps = ["C2"], ["C1"]
    r = solve_aggressor(_board(), p, "U1")
    assert r.hot_loops[0].loop_l_h < 1e-9 and r.victim("SIG_LOOP").peak_v < 0.01
    for words in ("C2 on phase 1: KiPI draws no current loop for it (it has no pad on VIN), so its "
                  "loop's coupling is left out",
                  "C1 on phase 1: KiPI draws no current loop for it (it has no pad on VOUT)",
                  "phase 1: no input capacitor loop is drawn, so the hot loop is their ESL plus the "
                  "package only"):
        assert any(x.startswith(words) for x in r.stand_ins), words
    assert r.victim("SIG_LOOP").passed is None and r.verdict == "not_judged"


def test_a_capacitor_on_the_rails_in_no_phase_list_is_a_warning_not_a_stand_in():
    """Coordinator's ruling: the user chose the loop's capacitors; one left out is shown."""
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.synthetic import coupled_noise_project
    p = coupled_noise_project(qrr_nc=5.0, logic_v=3.3)
    p.aggressors[0].phases[0].output_caps = []
    r = solve_aggressor(_board(), p, "U1")
    assert r.stand_ins == []
    assert "C2 is on the rails but in no phase's list: the hot loop does not include it" in r.warnings
    assert r.victim("SIG_LOOP").passed is True


def test_an_off_table_logic_v_is_not_judged_and_a_rated_model_is_derated():
    """Coordinator's I1: a logic_v off KiPI's JESD8 table gives no margin, and that is a
    not-judged entry (exit 3), never a silent pass."""
    from coppernicus.coupled_noise import noise_summary, noise_text
    p = _project()
    for v in p.victims:
        v.logic_v = 0.9
    p.capacitors[0].rated_v = 25.0
    r = _quick(p)
    assert all(v.margin_v is None and v.passed is None for v in r.victims)
    assert r.verdict == "not_judged"
    assert r.not_judged[0] == {"item": "SIG_SW", "net": "SIG_SW", "limit": "logic_v margin (V)",
                               "reason": "logic_v 0.9 V is not in KiPI's JESD8 table: enter noise_margin_v"}
    assert len(r.not_judged) == 3
    assert ("Victim SIG_SW: logic_v 0.9 V is not in KiPI's JESD8 table, so it gives no margin: not "
            "judged (enter noise_margin_v).") in r.assumptions
    assert not any("no noise margin set" in a for a in r.assumptions)
    assert not any("no margin set" in line for line in noise_text(noise_summary([r])))
    assert "DC bias: rated capacitors derated at the rails' nominal voltages, 12 V and 1.8 V; no DC " \
        "solve is passed." in r.assumptions


def test_no_current_loops_at_all_leave_inductive_coupling_out():
    """No inductor and no capacitors on the phase: KiPI's own "No capacitor loops" flag."""
    p = _project()
    ph = p.aggressors[0].phases[0]
    ph.inductor, ph.input_caps, ph.output_caps = "", [], []
    for v in p.victims:
        v.noise_margin_v = 1.0
    r = _quick(p)
    assert all("inductive coupling left out: no capacitor loops (set the switching device's pins and "
               "the capacitors)" in v.stand_ins for v in r.victims)


def test_an_assumed_dielectric_is_a_stand_in():
    from coppernicus.engine import Board
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.synthetic import coupled_noise_board
    snap = coupled_noise_board()
    snap.stackup.dielectrics[1].epsilon_r = 0.0
    p = _project()
    p.noise = NoiseOptions(board_model=False)
    r = solve_aggressor(Board(snap), p, "U1")
    assert any(x.startswith("εr assumed: the stackup gives no εr for FR4 1000 µm (In1.Cu–In2.Cu), so "
                            "4.3 is used (ac.default_er)") for x in r.stand_ins)
    assert any("Where the stackup gives no εr, 4.3 is used (ac.default_er)." in a for a in r.assumptions)


def test_the_assumption_lines(entered):
    lines = entered.assumptions
    # The hot loop's L (2.07 nH here) and so its ringing (200.5 MHz) follow the mesh of the traced
    # loop, which moves them by up to 6e-3 when its input moves by 1 to 3 ulp: they are written
    # from the result, as the line writes them (test_hot_loop_and_waveforms checks their physics).
    h = entered.hot_loops[0]
    for lead in ("Coupled noise (KiPI): the converter's waveforms are exact Fourier series of "
                 "piecewise-linear periods, 1592 harmonics of 1 MHz to 1592 MHz (ten times",
                 "Operating point: Topology: buck (from the voltages); Vin 12 V",
                 f"Phase 1: hot loop {h.loop_l_h * 1e9:.3g} nH (traced through the copper), switch node "
                 f"303.9 pF (Coss 300 pF entered + copper 3.87 pF); rings at {h.f_ring_hz / 1e6:.4g} MHz, "
                 "ζ 0.05 (at least min_ring_zeta",
                 "Phase 1: hard edge with Qrr 5 nC (entered); soft edge damped at least soft_edge_zeta "
                 "0.5; dead time 20 ns (noise.dead_time_ns, KiPI's 20 unless set); on-resistance 10 mΩ "
                 "for damping (KiPI's 10 mΩ); package loop 0 nH.",
                 "Capacitors on VIN and VOUT: ESR + jω·ESL + 1/(jω·C), 2 modelled.",
                 "Board model: the copper of VIN / VOUT and GND as AC mode's 2.5D field model,",
                 "Dielectric εr, top to bottom: FR4 200 µm εr 4.5, FR4 1000 µm εr 4.5, FR4 200 µm εr "
                 "4.5; solder mask εr 3.5 (KiPI's typical LPI)",
                 "Victims: traces cut every 0.5 mm with copper within 3 mm",
                 "Victim SIG_LOOP: CMOS push-pull, driver 33 Ω, receiver 1e+06 Ω ∥ 3 pF; ends U5[1] → "
                 "U6[1] (entered).",
                 "Victim SIG_LOOP: margin 800 mV from the JESD8 levels of a 3.3 V family",
                 "Victim sw lvds: Differential leg (LVDS), driver 50 Ω, receiver 1e+06 Ω ∥ 3 pF, "
                 "termination 50 Ω;",
                 "Victim sw lvds: margin 100 mV from preset lvds;",
                 "Inductor L1: shielded, winding axis vertical (KiPI's defaults: vertical, shielded).",
                 "Near field 5 mm over the top copper and a radiated estimate at 3 m",
                 "Via plating:",
                 "Ripple at LOAD: the board model's voltage between its pins (VOUT − GND)",
                 "Ripple at input: the board model's voltage between its pins (VIN − GND)"):
        assert any(x.startswith(lead) for x in lines), lead


def test_the_estimate_leaves_the_board_model_out_where_the_solve_would():
    from coppernicus.engine.noise import mesh_estimate
    p = _project()
    ph = p.aggressors[0].phases[0]
    ph.low_side_ref, ph.low_side_pins, ph.low_side_net = "", None, ""
    size = mesh_estimate(_board(), p, "U1")
    assert not size.board_model and size.note.startswith("Board model left out (phase 1 needs")
    assert mesh_estimate(_board(), _project(), "U1").board_model


def test_a_user_f_max_below_the_edges_or_kipis_ceiling_is_a_stand_in():
    """Coordinator's I2 and M2: harmonics cut short by noise.f_max_hz, or by KiPI's 3 GHz
    ceiling (waveforms.py:33, :96-97) on faster edges."""
    p = _project()
    for v in p.victims:
        v.noise_margin_v = 1.0
    p.noise = NoiseOptions(board_model=False, f_max_hz=100e6)
    from coppernicus.engine.noise import solve_aggressor
    r = solve_aggressor(_board(), p, "U1")
    assert "harmonics stop at 100 MHz (noise.f_max_hz), below the edges' 1592 MHz" in r.stand_ins
    p = _project(fsw_hz=2e6, rise_s=0.5e-9, fall_s=0.5e-9)
    r = _quick(p)
    assert r.f_max_hz == pytest.approx(3e9)
    assert "harmonics stop at KiPI's 3 GHz ceiling, below the edges' 6366 MHz" in r.stand_ins


def test_a_switch_net_missing_on_one_phase_is_a_stand_in_for_that_phase():
    """Coordinator's I3, on a two-phase aggressor where only phase 2 lacks it."""
    import dataclasses
    p = _project()
    a = p.aggressors[0]
    a.phases.append(dataclasses.replace(a.phases[0], switch_net=""))
    r = _quick(p)
    assert "capacitive coupling of phase 2 left out: no switch net" in r.stand_ins
    assert not any("phase 1 left out" in x for x in r.stand_ins)
    # Its output capacitor's loop runs through the switch node: KiPI draws none for phase 2.
    assert ("C2 on phase 2: KiPI draws no current loop for it (its loop runs through the switch node, "
            "and the phase has no switch net), so its loop's coupling is left out") in r.stand_ins
    assert not any(x.startswith("C2 on phase 1") for x in r.stand_ins)
    assert r.phases == 2


def test_a_cancel_during_the_field_solves_stops_the_solve():
    """Coordinator's M5: KiPI calls progress from its field-solve worker threads; a cancel
    asked there stops the solve before the victims."""
    import threading

    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.solve import Cancelled
    seen, asked = [], {}

    def progress(m):
        seen.append(m)
        if "] solving " in m and "[1/" not in m and not asked:
            asked["thread"] = threading.current_thread().name
    with pytest.raises(Cancelled):
        solve_aggressor(_board(), _project(), "U1", progress=progress, cancelled=lambda: bool(asked))
    solving = [m for m in seen if "] solving " in m]
    assert asked and 2 <= len(solving) < 34
    assert not any(m.startswith("== Victims") for m in seen)
    if any("at a time" in m for m in seen):            # KiPI solved side by side here
        assert asked["thread"].startswith("kipi-field")
