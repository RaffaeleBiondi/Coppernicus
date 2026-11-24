"""Write the committed examples: synthetic boards and their project files.

    uv run python examples/make_examples.py

then try them:

    uv run coppernicus solve examples/demo/demo.coppernicus.yml --out results/demo
    uv run coppernicus gui examples/demo/demo.coppernicus.yml
    uv run coppernicus gui examples/power_chain/power_chain.coppernicus.yml

``demo``: one rail, three loads, a tight drop budget. ``power_chain``: a whole power tree
(12 V input, switcher, ferrite, remote-sensed LDO, ground pins), copied from KiPI's
power-chain validation board.

``demo.coppernicus.yml`` has a deliberately tight 0.25 % drop budget, so it fails;
``demo-relaxed.coppernicus.yml`` is the same rail with a 1 % budget, so it passes. The
GitHub Action's self-test runs both.
"""

from pathlib import Path

from coppernicus.engine.synthetic import demo_board, power_chain_board, power_chain_project, save_snapshot
from coppernicus.project import Limits, Load, Project, Rail, Source

ROOT = Path(__file__).resolve().parent
HERE = ROOT / "demo"
CHAIN = ROOT / "power_chain"


def demo_project(max_drop_pct: float) -> Project:
    return Project(board="demo.kipi.json.gz", rails=[Rail(
        "+3V3",
        sources=[Source("J1", 3.3, name="Supply")],
        loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)],
        limits=Limits(max_drop_pct=max_drop_pct),
    )])


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


if __name__ == "__main__":
    main()
