"""Phase 5 of the restyle: the guided start (first run only, ticked step by step, gone for good
after the first solve or "Don't show again"), the shortcuts in sight (tooltips, Help →
Keyboard shortcuts), and the icons (Phosphor, MIT: their licence in the bundle's notices)."""

import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / "src" / "coppernicus" / "app" / "resources" / "icons"
DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo", "demo.coppernicus.yml")


def _packaging(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_icon_the_app_names_is_shipped_with_its_licence():
    from coppernicus.app import main_window as mw
    names = set(mw.TOOLBAR_ICONS.values()) | set(mw.MENU_ICONS.values()) | set(mw.NAV_ICONS.values())
    names |= {"arrows-clockwise"}
    missing = [n for n in sorted(names) if not (ICONS / f"{n}.svg").is_file()]
    assert not missing, missing
    licence = (ICONS / "LICENSE-phosphor.txt").read_text(encoding="utf-8")
    assert "MIT License" in licence and "Phosphor Icons" in licence


def test_the_bundles_notices_hold_the_icons_licence_or_the_check_fails(tmp_path):
    notices, check_bundle = _packaging("notices"), _packaging("check_bundle")
    out = tmp_path / "THIRD_PARTY_NOTICES.txt"
    notices.write(out, ["numpy/__init__.py"], "0.0")
    text = out.read_text(encoding="utf-8")
    licence = (ICONS / "LICENSE-phosphor.txt").read_text(encoding="utf-8")
    assert "Phosphor Icons" in text.split("\n\n\n")[0]                  # in the list on top
    assert check_bundle.icon_notice_problems(text, licence) == []
    assert check_bundle.icon_notice_problems(text.replace("Phosphor Icons", "Someone"), licence) != []
    assert check_bundle.icon_notice_problems(None, licence) == ["no THIRD_PARTY_NOTICES.txt in the bundle"]


def test_the_guide_ticks_its_steps_and_the_first_solve_ends_it_for_good():
    run_in_window(f'''
        import coppernicus.app.main_window as mw
        from coppernicus.app import guide

        class HeldWorker(mw.SolveWorker):
            def start(self):
                pass

        mw.SolveWorker = HeldWorker
        window.resize(1440, 900)
        window.show()
        window.refresh_badges()
        app.processEvents()
        assert not guide.done() and window.guide_row.isVisible()
        assert window.guide.progress() == [False, False, False, False]
        assert not window.map_pane.isVisible()                 # before a board the guide takes its place
        card = window.guide
        need = card.layout().totalHeightForWidth(card.width())
        assert card.height() >= need, (card.height(), need)   # its text is never clipped
        window.open_project({DEMO!r})
        window.nets.select("+3V3")
        app.processEvents()
        window.refresh_badges()
        app.processEvents()
        assert window.guide.progress() == [True, True, True, False]
        assert window.map_pane.isVisible()
        window.solve()
        window.worker.run()
        app.processEvents()
        window.refresh_badges()
        app.processEvents()
        assert guide.done() and window.guide_row.isHidden()
        window.close()
        again = MainWindow()
        again.dirty = False
        assert again.guide_row.isHidden()                      # never again
    ''')


def test_dont_show_again_ends_the_guide():
    run_in_window('''
        from coppernicus.app import guide
        window.show()
        app.processEvents()
        assert window.guide_row.isVisible(), (window.guide_row.isHidden(), guide.done())
        window.guide.dismiss_button.click()
        app.processEvents()
        assert guide.done() and window.guide_row.isHidden() and window.map_pane.isVisible()
    ''')


def test_the_shortcuts_are_in_sight():
    run_in_window('''
        from PySide6.QtGui import QKeySequence
        acts = window.toolbar_actions
        f5 = QKeySequence("F5").toString(QKeySequence.NativeText)
        assert acts["solve_rail"].toolTip() == f"Solve rail ({f5})"
        ctrl_i = QKeySequence("Ctrl+I").toString(QKeySequence.NativeText)
        assert acts["solve_impedance"].toolTip().endswith(f"({ctrl_i})"), acts["solve_impedance"].toolTip()
        native = QKeySequence(QKeySequence.Open).toString(QKeySequence.NativeText)
        assert f"Open board… ({native})" in window.toolbar_menus["Open"].toolTip()
        rows = dict(window.shortcuts_rows())
        assert rows["Solve rail"] == f5 and rows["Keyboard shortcuts"] == "F1"
        assert rows["Next / previous copper layer (as in KiCad)"] == "+ / −"
        menus = [a.text() for a in window.menuBar().actions()]
        assert menus[-1] == "&Help" and "View" in menus, menus
        assert window.shortcuts_action in window.help_menu.actions()
        for key, act in acts.items():
            assert not act.icon().isNull(), key
    ''')
