"""tools/make_docs_screenshots.py: no process the run starts comes to the front. Each
``coppernicus`` command runs with matplotlib's file-only backend, and with a real display the
application in front is read while it runs: the run stops if the command or the run itself
came to the front."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "make_docs_screenshots.py"


def _tool():
    spec = importlib.util.spec_from_file_location("make_docs_screenshots", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_command_gets_the_file_backend_and_stops_the_run_if_it_comes_to_the_front(monkeypatch):
    tool = _tool()
    backend = [sys.executable, "-c", "import os; print(os.environ['MPLBACKEND'])"]
    code, out, _, read = tool.run_child(backend, 60)
    assert (code, out.strip(), read) == (0, "Agg", 0)          # not watched: no sample taken
    sleep = [sys.executable, "-c", "import time; time.sleep(1.2)"]
    monkeypatch.setattr(tool.gui_check(), "front_app", lambda: ("Safari", 1))
    code, _, _, read = tool.run_child(sleep, 60, watch_front=True)
    assert code == 0 and read >= 2                              # samples while it runs, and after
    monkeypatch.setattr(tool.gui_check(), "front_app", lambda: ("Python", os.getpid()))
    with pytest.raises(tool.Unfinished, match="came to the front"):
        tool.run_child(sleep, 60, watch_front=True)
