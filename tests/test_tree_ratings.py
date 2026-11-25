"""A regulator's rated output current (``Source.current_limit_a``: the KiCad field PI_IMAX, or
``current_limit_a`` in the project file) next to the current it delivers in the power tree.

On the synthetic power chain (U1 delivers about 2.5 A, U2 2 A) and on a synthetic KiCad buck
whose PI_IMAX is on the IC, not on the inductor that holds the rail. Window checks run in a
child process, offscreen (the pattern of tests/test_app_window.py)."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402
from test_board_fields_library import buck_board  # noqa: E402

from coppernicus.project import Project  # noqa: E402

CHAIN = Path(__file__).resolve().parents[1] / "examples" / "power_chain"

SOLVE = '''
        def solve_tree(project):
            window.dirty = False
            assert window.open_project(project), project
            window.solve_tree()
            window.tree_worker.wait(120000)
            app.processEvents()
            app.processEvents()
            assert window.tree is not None, window.tree_panel.summary.text()
            return window.tree

        def vrm_table():
            table = window.tree_panel.vrms
            headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
            return headers, {row[0]: dict(zip(headers, row)) for row in window.tree_panel.vrm_rows()}
'''


def chain_project(folder: Path, ratings=None) -> Path:
    """The power-chain example in ``folder``, each source of ``ratings`` (net -> A) rated."""
    shutil.copytree(CHAIN, folder)
    project = Project.load(folder / "power_chain.coppernicus.yml")
    for net, amps in (ratings or {}).items():
        project.rail(net).sources[0].current_limit_a = amps
    return project.save(folder / "power_chain.coppernicus.yml")


def rated_buck(folder: Path, imax: str = "2A") -> Path:
    """The synthetic buck of tests/test_board_fields_library.py with PI_IMAX on its IC (U1;
    the rail is held at the inductor L1's pad), and an empty project file next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    board = buck_board(folder / "buck.kicad_pcb")
    text = board.read_text(encoding="utf-8")
    assert text.count('(property "PI_KIND" "smps"') == 1
    board.write_text(text.replace('(property "PI_KIND" "smps"',
                                  f'(property "PI_IMAX" "{imax}" (at 0 0 0) (layer "F.Fab")) '
                                  '(property "PI_KIND" "smps"'), encoding="utf-8")
    return Project(board="buck.kicad_pcb").save(folder / "buck.coppernicus.yml")


# ---------------------------------------------------------------------- the Power tree tab

def test_the_power_tree_tab_shows_i_max_next_to_i_out_only_when_a_project_has_ratings(tmp_path):
    plain = chain_project(tmp_path / "plain")
    rated = chain_project(tmp_path / "rated", {"+3V3": 3.0})
    buck = rated_buck(tmp_path / "buck")
    run_in_window(SOLVE + f'''
        solve_tree({str(plain)!r})
        headers, rows = vrm_table()
        assert "I max (A)" not in headers, headers                 # no rating: no column

        solve_tree({str(rated)!r})                                  # from the project file
        headers, rows = vrm_table()
        assert headers[headers.index("I out (A)") + 1] == "I max (A)", headers
        assert rows["U1"]["I max (A)"] == "3" and rows["J1"]["I max (A)"] == "—", rows
        assert rows["U2"]["I max (A)"] == "—", rows

        solve_tree({str(buck)!r})                                   # from KiCad field PI_IMAX on U1
        headers, rows = vrm_table()
        assert rows["U1"]["I max (A)"] == "2" and rows["J1"]["I max (A)"] == "—", rows
    ''')
