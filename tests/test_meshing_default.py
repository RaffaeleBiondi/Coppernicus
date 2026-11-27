"""Nothing changes by default: importing the mesher seam installs nothing, and the repository's examples
solve to the same bits whether or not it was imported (each in a fresh process)."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Solves demo's rails and power_chain's tree; prints a digest of every mesh and field, and the scalars.
SCRIPT = r'''
import hashlib, json, sys
import numpy as np
mode, root = sys.argv[1], sys.argv[2]
if mode == "seam":
    import coppernicus.engine.meshing as meshing
    from coppernicus.engine.meshing import pipeline, protocol, registry, triangle_backend  # noqa: F401
    assert meshing.installed() is None
from coppernicus.engine.board import Board
from coppernicus.engine.solve import solve_rail
from coppernicus.engine.tree import solve_project_tree
from coppernicus.project import Project


def digest(res):
    h = hashlib.sha256()
    for layer in res.layers:
        for a in (layer.points, layer.tris, layer.voltage, layer.current_density):
            h.update(np.ascontiguousarray(a).tobytes())
    return [h.hexdigest(), repr(res.max_drop), repr(res.power_loss), repr(res.loss_vias)]


out = {}
p = Project.load(f"{root}/examples/demo/demo.coppernicus.yml")
b = Board.open(f"{root}/examples/demo/{p.board}")
for rail in p.rails:
    out["demo:" + rail.net] = digest(solve_rail(b, rail, p.settings, project=p))
p = Project.load(f"{root}/examples/power_chain/power_chain.coppernicus.yml")
b = Board.open(f"{root}/examples/power_chain/{p.board}")
for name, group in sorted(solve_project_tree(b, p).groups.items()):
    out["tree:" + name] = digest(group)
if mode == "plain":
    assert "coppernicus.engine.meshing" not in sys.modules
# KiPI's pools exactly as KiPI has them, and none started for these small boards.
import multiprocessing, os
import kipi.core.mesh.mesher as M, kipi.core.mesh.parallel as P, kipi.core.workers as W
assert M.mesh_polygon.__module__ == "kipi.core.mesh.mesher"
assert P.mesh_layers.__module__ == "kipi.core.mesh.parallel"
assert W._start.__module__ == "kipi.core.workers" and W.BoardPool.__module__ == "kipi.core.workers"
assert "KIPI_SERIAL" not in os.environ and not multiprocessing.active_children()
print(json.dumps(out))
'''


def _solve(mode):
    env = {k: v for k, v in os.environ.items() if k != "KIPI_SERIAL"}
    env["PYTHONIOENCODING"] = "utf-8"
    run = subprocess.run([sys.executable, "-c", SCRIPT, mode, str(ROOT)], capture_output=True, text=True,
                         encoding="utf-8", timeout=600, env=env)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_importing_the_seam_installs_nothing():
    """Nor does it touch KiPI's pools: the layer pool and the 3D view's (their initializer, their
    BoardPool, their worker counts) stay KiPI's own."""
    import kipi.core.mesh.mesher as M
    import kipi.core.mesh.parallel as P
    import kipi.core.viz.meshes as V
    import kipi.core.viz.parallel as VP
    import kipi.core.workers as W
    def hooks():
        return (M.mesh_polygon, P.mesh_layers, os.environ.get("KIPI_SERIAL"), sys.modules.get("triangle"),
                W._start, W.BoardPool, W.workers, W.can_spawn, P.Pooled, VP.BoardPool, VP.workers,
                V._triangle)
    before = hooks()
    import coppernicus.engine.meshing as meshing
    from coppernicus.engine.meshing import pipeline, protocol, registry, triangle_backend  # noqa: F401
    assert meshing.installed() is None
    assert hooks() == before
    assert M.mesh_polygon.__module__ == "kipi.core.mesh.mesher"
    assert W._start.__module__ == "kipi.core.workers" and VP.BoardPool is W.BoardPool


def test_the_examples_solve_to_the_same_bits_with_and_without_the_seam_imported():
    plain, seam = _solve("plain"), _solve("seam")
    assert plain and plain == seam
