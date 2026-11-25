"""tools/gui_check.py removes its temporary output folder when every check passes, unless
--keep or --out is given: the scripted GUI check leaves nothing behind."""

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "gui_check.py"


def _tool():
    spec = importlib.util.spec_from_file_location("gui_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_temporary_folder_goes_only_when_every_check_passes_and_nothing_asks_to_keep_it():
    remove = _tool().remove_output
    assert remove(failed=0, out_given=False, keep=False) is True
    assert remove(failed=1, out_given=False, keep=False) is False      # kept to look at
    assert remove(failed=0, out_given=True, keep=False) is False       # the user's own folder
    assert remove(failed=0, out_given=False, keep=True) is False
