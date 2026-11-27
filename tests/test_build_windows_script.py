"""packaging/build_windows.ps1 runs only on Windows (release.yml): its helper is checked here as text.

``$program, $arguments = $args`` leaves ``$arguments`` a string when there is one argument, and
splatting a string passes its first character only (a release build failed on
``Invoke-Checked $Py tools\\mesher_rs\\check_source.py`` with "can't open file '…\\t'")."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "build_windows.ps1"


def test_invoke_checked_always_splats_a_list():
    text = SCRIPT.read_text(encoding="utf-8")
    helper = re.search(r"function Invoke-Checked \{(.*?)\n\}", text, re.S).group(1)
    assert "$program, $arguments = $args" not in helper
    assert "$arguments = @(" in helper and "& $program @arguments" in helper


def test_single_argument_calls_exist_and_go_through_the_helper():
    """The case that broke: a program with one argument."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^Invoke-Checked \$Py tools\\mesher_rs\\check_source\.py\s*$", text, re.M)
