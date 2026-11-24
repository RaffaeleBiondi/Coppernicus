"""Write the committed example: a synthetic two-layer board and its project file.

    uv run python examples/make_examples.py

then try it:

    uv run coppernicus solve examples/demo/demo.coppernicus.yml --out results/demo
    uv run coppernicus gui examples/demo/demo.coppernicus.yml
"""

from pathlib import Path

from coppernicus.engine.synthetic import demo_board, save_snapshot
from coppernicus.project import Limits, Load, Project, Rail, Source

HERE = Path(__file__).resolve().parent / "demo"


def main():
    HERE.mkdir(exist_ok=True)
    save_snapshot(demo_board(), HERE / "demo.kipi.json.gz")
    project = Project(board="demo.kipi.json.gz", rails=[Rail(
        "+3V3",
        sources=[Source("J1", 3.3, name="Supply")],
        loads=[Load("U1", 2.0), Load("U2", 1.5), Load("U3", 0.3)],
        # A deliberately tight drop budget, so the demo shows a failing load.
        limits=Limits(max_drop_pct=0.25),
    )])
    project.save(HERE / "demo.coppernicus.yml")
    print(f"Wrote {HERE}")


if __name__ == "__main__":
    main()
