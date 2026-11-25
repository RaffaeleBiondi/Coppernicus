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
