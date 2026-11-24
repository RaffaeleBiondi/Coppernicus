"""Write the committed examples: synthetic boards and their project files.

    uv run python examples/make_examples.py

then try them:

    uv run coppernicus solve examples/demo/demo.coppernicus.yml --out results/demo
    uv run coppernicus gui examples/demo/demo.coppernicus.yml
    uv run coppernicus gui examples/power_chain/power_chain.coppernicus.yml
    uv run coppernicus gui examples/fields/fields.coppernicus.yml

``demo``: one rail, three loads, a tight drop budget. ``power_chain``: a whole power tree
(12 V input, switcher, ferrite, remote-sensed LDO, ground pins), copied from KiPI's
power-chain validation board. ``fields``: a .kicad_pcb whose parts carry the rail in KiCad
footprint fields (J1 ``PI_SOURCE``, U1 ``PI_LOAD`` and ``PI_LIMIT_DROP``), with a project
file whose own rail of the same net differs, so the fields win with a warning.

``demo.coppernicus.yml`` has a deliberately tight 0.25 % drop budget, so it fails;
``demo-relaxed.coppernicus.yml`` is the same rail with a 1 % budget, so it passes. The
GitHub Action's self-test runs both.
"""

from pathlib import Path

from coppernicus.engine.synthetic import (
    demo_board,
    power_chain_board,
    power_chain_project,
    save_snapshot,
    write_strip_kicad_pcb,
)
from coppernicus.project import Limits, Load, Project, Rail, Source

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


def main(board: bool = True):
    HERE.mkdir(exist_ok=True)
    if board:
        # With LED_A, a net the power heuristic misses: right-click it to mark it as a rail.
        save_snapshot(demo_board(led_net=True), HERE / "demo.kipi.json.gz")
    demo_project(0.25).save(HERE / "demo.coppernicus.yml")
    demo_project(1.0).save(HERE / "demo-relaxed.coppernicus.yml")
    print(f"Wrote {HERE}")

    CHAIN.mkdir(exist_ok=True)
    save_snapshot(power_chain_board(), CHAIN / "power_chain.kipi.json.gz")
    power_chain_project("power_chain.kipi.json.gz").save(CHAIN / "power_chain.coppernicus.yml")
    print(f"Wrote {CHAIN}")

    write_fields_example(FIELDS)
    print(f"Wrote {FIELDS}")


if __name__ == "__main__":
    main()
