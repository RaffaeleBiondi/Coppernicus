"""An install without the rust mesher (coppermesh removed or left out): ``import coppermesh``
blocked in a fresh process. A command that names no mesher meshes with Triangle and says so in its
warnings (the AC mesh edge then Triangle's default, 1.0 mm, with no warning about rust's); a command
that names rust is one clear error, mesher_unavailable. CI also runs this file in a job that leaves
coppermesh out (uv sync --no-install-package coppermesh), where it is really missing."""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo" / "demo.coppernicus.yml"
PDN = ROOT / "examples" / "pdn" / "pdn.coppernicus.yml"

# Blocks coppermesh before anything imports it, then runs the CLI with the arguments given.
SCRIPT = r'''
import importlib.abc, sys

class NoCoppermesh(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "coppermesh" or name.startswith("coppermesh."):
            raise ModuleNotFoundError(f"No module named {name!r} (blocked by the test)", name=name)
        return None

sys.meta_path.insert(0, NoCoppermesh())
from coppernicus.cli import main
sys.exit(main(sys.argv[1:]))
'''


def _cli(*args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    run = subprocess.run([sys.executable, "-c", SCRIPT, *args], capture_output=True, text=True,
                         encoding="utf-8", timeout=900, env=env)
    return run.returncode, (json.loads(run.stdout) if run.stdout.strip() else None), run.stderr


def _project(src: Path, folder: Path, **ac) -> Path:
    """``src`` copied into ``folder`` with its board by absolute path and ``ac`` keys set."""
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    data["board"] = str(src.parent / data["board"])
    if ac:
        data["ac"] = {**(data.get("ac") or {}), **ac}
    out = folder / src.name
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


def test_without_coppermesh_the_default_falls_back_to_triangle_and_rust_is_an_error(tmp_path):
    from coppernicus.engine.meshing.session import FALLBACK_WARNING
    code, doc, err = _cli("solve", str(DEMO), "--json", "--no-maps", "--out", str(tmp_path / "dc"))
    assert code in (0, 1), err[-2000:]
    assert doc["mesher"]["name"] == "triangle" and doc["mesher"]["fallback_from"] == "rust"
    assert FALLBACK_WARNING in doc["warnings"] and FALLBACK_WARNING in doc["rails"][0]["warnings"]
    assert doc["rails"][0]["settings"]["mesher"] == "triangle"

    code, doc, err = _cli("solve", str(DEMO), "--json", "--no-maps", "--mesher", "rust",
                          "--out", str(tmp_path / "rust"))
    assert code == 2 and doc["error"]["code"] == "mesher_unavailable", err[-2000:]
    assert "pip install coppermesh" in doc["error"]["hint"] and "Traceback" not in err


def test_without_coppermesh_the_ac_edge_is_triangles_default_with_no_rust_warning(tmp_path):
    from coppernicus.engine.meshing.session import FALLBACK_WARNING
    code, doc, err = _cli("ac", str(_project(PDN, tmp_path)), "--json", "--out", str(tmp_path / "ac"))
    assert code in (0, 1, 3), err[-2000:]
    assert doc["mesher"]["name"] == "triangle" and FALLBACK_WARNING in doc["warnings"]
    assert doc["ac"] == {"max_edge_mm_used": 1.0, "max_edge_mm_from": "triangle default"}
    [rail] = doc["rails"]
    assert rail["ac"] == doc["ac"]
    line = "AC mesh: largest edge 1 mm, KiPI's default, with Triangle"
    assert any(a.startswith(line) for a in rail["assumptions"])
    assert not any("coarser than the rust mesher" in w for w in doc["warnings"] + rail["warnings"])
