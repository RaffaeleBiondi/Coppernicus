"""Write the committed examples: synthetic boards and their project files.

    uv run python examples/make_examples.py

then try them:

    uv run coppernicus solve examples/demo/demo.coppernicus.yml --out results/demo
    uv run coppernicus gui examples/demo/demo.coppernicus.yml
    uv run coppernicus gui examples/power_chain/power_chain.coppernicus.yml
    uv run coppernicus gui examples/fields/fields.coppernicus.yml
    uv run coppernicus ac examples/pdn/pdn.coppernicus.yml --out results/pdn
    uv run coppernicus noise examples/noise/noise.coppernicus.yml --out results/noise
    uv run coppernicus tolerance examples/tolerance/tolerance.coppernicus.yml --out results/tolerance

``demo``: one rail, three loads, a tight drop budget. ``power_chain``: a whole power tree
(12 V input, switcher, ferrite, remote-sensed LDO, ground pins), copied from KiPI's
power-chain validation board. ``fields``: a .kicad_pcb whose parts carry the rail in KiCad
footprint fields (J1 ``PI_SOURCE``, U1 ``PI_LOAD`` and ``PI_LIMIT_DROP``), with a project
file whose own rail of the same net differs, so the fields win with a warning.

``demo.coppernicus.yml`` has a deliberately tight 0.25 % drop budget, so it fails;
``demo-relaxed.coppernicus.yml`` is the same rail with a 1 % budget, so it passes. The
GitHub Action's self-test runs both.

``demo-thermal.coppernicus.yml``: the demo rail at four times its currents with the board
temperature on (``thermal: {enabled: true, max_rise_c: 15}``); its copper heats about 17 °C
above the 25 °C ambient, so it fails:

    uv run coppernicus solve examples/demo/demo-thermal.coppernicus.yml --tree --out results/demo

``pdn``: AC impedance (Task 9) of KiPI's decoupling validation board, a 50 mm plane pair with
one 10 µF capacitor (ESL 0.5 nH, ESR 20 mΩ) on C1, the source's bandwidth 100 kHz and KiPI's
default sweep. From one solve: |Z| is 160 mΩ at the bandwidth, 22 mΩ at 1.49 MHz (the series
resonance), so ``pdn.coppernicus.yml``'s 50 mΩ target up to 10 MHz fails and
``pdn-relaxed.coppernicus.yml``'s 200 mΩ holds.

``noise``: DC-DC ripple and coupled noise (Task 10) of KiPI's coupled-noise validation board, a
12 V to 1.8 V buck at 1 MHz (2 ns edges, 1 µH, Coss 300 pF and Qrr 5 nC entered, 10 µF and
47 µF modelled) with three 0.2 mm victims, each a CMOS line on 3.3 V logic with both ends
entered. From one solve: SIG_LOOP, beside the hot loop's VIN track, picks up 655 mV (82 % of
the 0.8 V JESD8 margin), SIG_SW 33 mV, the shielded SIG_BURIED 1.4 mV, so
``noise.coppernicus.yml`` passes and ``noise-tight.coppernicus.yml`` (a 0.5 V noise margin on
every victim) fails; the ripple at the load's probe LOAD (never judged) is 18 mV p-p, 12 mV
through 20 MHz.

``tolerance``: the tolerance stack-up (Task 11) of U2 on the noise example's board (its victims
left out): U1's DC accuracy ±1 %, U2's transient ±30 mV entered (a datasheet figure), the probe
LOAD with a 1 A step at 1 A/µs and U1's bandwidth 100 kHz for KiPI's transient estimate (26 mV,
shown, never judged). From one run: DC drop 27.05 mV below, accuracy 18 mV, ripple at LOAD
7.88 / 10.57 mV, so the total is 82.94 / 58.57 mV; ``tolerance.coppernicus.yml``'s ±6 % (108 mV)
holds with every part known, and ``tolerance-tight.coppernicus.yml``'s ±3 % (54 mV) fails.
"""

from pathlib import Path

from coppernicus.engine.synthetic import (
    ac_decap_board,
    ac_decap_project,
    coupled_noise_board,
    coupled_noise_project,
    demo_board,
    power_chain_board,
    power_chain_project,
    save_snapshot,
    write_strip_kicad_pcb,
)
from coppernicus.project import AcOptions, Limits, Load, Project, Rail, Source, ThermalOptions

ROOT = Path(__file__).resolve().parent
HERE = ROOT / "demo"
CHAIN = ROOT / "power_chain"
FIELDS = ROOT / "fields"
# The rail of the strip in KiCad footprint fields (synthetic numbers).
STRIP_FIELDS = {"J1": {"PI_RAIL": "VCC", "PI_SOURCE": "3.3V"},
                "U1": {"PI_RAIL": "VCC", "PI_LOAD": "1A", "PI_LIMIT_DROP": "2%"}}


def demo_project(max_drop_pct: float) -> Project:
    return Project(board="demo.kipi.json.gz", rails=[Rail(
        "+3V3",
        sources=[Source("J1", 3.3, name="Supply")],
        loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)],
        limits=Limits(max_drop_pct=max_drop_pct),
    )])


def write_fields_example(folder: Path) -> None:
    """The strip as a .kicad_pcb with its rail in footprint fields, and a project file whose
    own VCC rail draws 1.5 A instead of the fields' 1 A: the fields win, with a warning."""
    folder.mkdir(parents=True, exist_ok=True)
    write_strip_kicad_pcb(folder / "fields.kicad_pcb", STRIP_FIELDS)
    Project(board="fields.kicad_pcb", rails=[Rail(
        "VCC", sources=[Source("J1", 3.3)], loads=[Load("U1", 1.5)])]).save(folder / "fields.coppernicus.yml")


def demo_thermal_project() -> Project:
    """The demo rail at four times its currents, with the board temperature solved and a
    15 °C limit on the copper's rise over the ambient (KiPI's defaults otherwise)."""
    return Project(board="demo.kipi.json.gz", rails=[Rail(
        "+3V3",
        sources=[Source("J1", 3.3, name="Supply")],
        loads=[Load("U1", 8.0), Load("U2", 6.0), Load("U3", 1.2)],
    )], thermal=ThermalOptions(enabled=True, max_rise_c=15.0))


def tolerance_project(tol_pct: float) -> Project:
    """The noise example's buck without its victims, for the tolerance stack-up of U2: U1's
    accuracy ±1 %, U2's tolerances (±``tol_pct``, DC ±3 %, AC ±50 mV) and its transient ±30 mV
    entered, a 1 A step at 1 A/µs on the probe LOAD and U1's bandwidth 100 kHz."""
    import dataclasses
    p = coupled_noise_project("tolerance.kipi.json.gz", qrr_nc=5.0)
    p.victims = []
    rail = p.rails[1]
    rail.sources[0] = dataclasses.replace(rail.sources[0], accuracy_pct=1.0)
    rail.loads[0] = dataclasses.replace(rail.loads[0], tol_pct=tol_pct, dc_tol_pct=3.0, ac_tol_mv=50.0,
                                        transient_mv=30.0)
    p.probes[0].step_a, p.probes[0].slew_a_per_us = 1.0, 1.0
    p.ac = AcOptions(bandwidth_hz={"U1": 100e3})
    return p


def write_tolerance_example(folder: Path, board: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if board:
        save_snapshot(coupled_noise_board(), folder / "tolerance.kipi.json.gz")
    for name, tol_pct in (("tolerance", 6.0), ("tolerance-tight", 3.0)):
        tolerance_project(tol_pct).save(folder / f"{name}.coppernicus.yml")


def main(board: bool = True):
    HERE.mkdir(exist_ok=True)
    if board:
        # With LED_A, a net the power heuristic misses: right-click it to mark it as a rail.
        save_snapshot(demo_board(led_net=True), HERE / "demo.kipi.json.gz")
    demo_project(0.25).save(HERE / "demo.coppernicus.yml")
    demo_project(1.0).save(HERE / "demo-relaxed.coppernicus.yml")
    demo_thermal_project().save(HERE / "demo-thermal.coppernicus.yml")
    print(f"Wrote {HERE}")

    CHAIN.mkdir(exist_ok=True)
    save_snapshot(power_chain_board(), CHAIN / "power_chain.kipi.json.gz")
    power_chain_project("power_chain.kipi.json.gz").save(CHAIN / "power_chain.coppernicus.yml")
    print(f"Wrote {CHAIN}")

    write_fields_example(FIELDS)
    print(f"Wrote {FIELDS}")

    # AC impedance: the source U1 (pin 1, ground pin 2) and the probe U1 on the same pins.
    pdn = ROOT / "pdn"
    pdn.mkdir(exist_ok=True)
    save_snapshot(ac_decap_board(), pdn / "pdn.kipi.json.gz")
    for name, target_ohm in (("pdn", 0.05), ("pdn-relaxed", 0.2)):
        p = ac_decap_project("pdn.kipi.json.gz")
        p.probes[0].target_ohm, p.probes[0].target_hz = target_ohm, 10e6
        p.ac = AcOptions(bandwidth_hz={"U1": 100e3})
        p.save(pdn / f"{name}.coppernicus.yml")
    print(f"Wrote {pdn}")

    # DC-DC noise: every device value and both ends of each victim entered, 3.3 V logic.
    noise = ROOT / "noise"
    noise.mkdir(exist_ok=True)
    save_snapshot(coupled_noise_board(), noise / "noise.kipi.json.gz")
    for name, margin in (("noise", None), ("noise-tight", 0.5)):
        p = coupled_noise_project("noise.kipi.json.gz", qrr_nc=5.0, logic_v=3.3)
        for v in p.victims:
            v.noise_margin_v = margin
        p.save(noise / f"{name}.coppernicus.yml")
    print(f"Wrote {noise}")

    # Tolerance stack-up: the noise board's load U2 with every part of its stack-up known.
    write_tolerance_example(ROOT / "tolerance")
    print(f"Wrote {ROOT / 'tolerance'}")


if __name__ == "__main__":
    main()
