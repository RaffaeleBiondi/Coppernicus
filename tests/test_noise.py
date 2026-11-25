"""DC-DC noise and ripple (Task 10): the project's aggressors, victims and noise options, the
engine wrapping KiPI's ``solve_noise``, KiPI's coupled-noise validation on its synthetic
buck, verdicts and outputs. Field by field: ``docs/engine-mapping.md`` (Task 10)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
