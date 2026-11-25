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
    assert len(parts[0].switch_node_v[0]) == len(r.switch_node_v[0]) and fractions
    assert not r.partial and r.verdict


def test_stand_ins_for_coss_models_ends_switch_node_and_harmonics():
    p = _project()
    ph = p.aggressors[0].phases[0]
    ph.coss_f, ph.qrr_nc, ph.switch_net = None, None, ""
    p.capacitors = [p.capacitors[0]]                     # C2 loses its model
    p.victims[0].driver_ref, p.victims[0].receiver_ref = "", ""
    p.noise = NoiseOptions(board_model=False, max_harmonics=100)
    for v in p.victims:
        v.noise_margin_v = 1.0
    from coppernicus.engine.noise import solve_aggressor
    r = solve_aggressor(_board(), p, "U1")
    text = "; ".join(r.stand_ins)
    for words in ("Coss not entered on phase 1: KiPI's 200 pF", "Qrr not entered on phase 1",
                  "without a model: C2", "capacitive coupling left out: no switch net on any phase",
                  "harmonics stop at 100 MHz (noise.max_harmonics 100)"):
        assert words in text, words
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
