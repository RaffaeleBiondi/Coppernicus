"""The app's theme: the HTML report's colour tokens (read from report.css), dark by default,
light on request (toolbar button, View menu, Ctrl+Shift+L), kept in the settings; every colour
already on screen follows a switch."""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from coppernicus.app import theme  # noqa: E402

APP = Path(theme.__file__).resolve().parent
REPORT_CSS = APP.parent / "report_assets" / "report.css"


def luminance(hex_colour: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_shared_tokens_are_the_reports_own():
    css = REPORT_CSS.read_text(encoding="utf-8")
    dark, light = theme.tokens("dark"), theme.tokens("light")
    for name in ("bg", "fg", "muted", "line", "panel", "map", "accent", "fail", "pass", "warn"):
        assert f"--{name}:{dark[name]}" in css.split(':root[data-theme="light"]')[0], name
        assert f"--{name}:{light[name]}" in css.split(':root[data-theme="light"]')[1], name
    assert dark["bg"] == "#16191c" and light["bg"] == "#ffffff"
    for name in ("selected", "badge-fail", "badge-pass", "badge-attention"):   # set once, for both
        assert dark[name] == light[name], name
    assert set(theme.APP_TOKENS["dark"]) == set(theme.APP_TOKENS["light"])
    with pytest.raises(ValueError):
        theme.tokens("sepia")


def test_state_colours_are_distinct_so_a_switch_swaps_each_one_to_one():
    values = [theme.color(k, t) for t in theme.THEMES for k in theme.STATE_TOKENS]
    assert len(values) == len(set(values)), values
    there_and_back = {**theme.swaps("dark", "light"), **theme.swaps("light", "dark")}
    assert all(there_and_back[there_and_back[v]] == v for v in values)


@pytest.mark.parametrize("name", theme.THEMES)
def test_text_reads_on_every_background_it_is_drawn_on(name):
    """At least 4.5:1 (WCAG AA) for each pair the app draws: labels and table text on the
    window, panels, inputs, tooltips and menus; table text on every cell state; verdict words
    on the colour bar; white on the selected row."""
    t = theme.tokens(name)
    pairs = [(fg, bg) for fg in ("fg", "muted", "fail", "warn", "pass")
             for bg in ("bg", "panel", "input", "pop")]
    pairs += [("fg", bg) for bg in ("chrome", "cell-readonly", "cell-problem", "cell-error", "cell-missing")]
    pairs += [("muted", "chrome")]
    low = {(a, b): round(contrast(t[a], t[b]), 2) for a, b in pairs if contrast(t[a], t[b]) < 4.5}
    assert not low, low
    assert contrast(theme.WHITE, t["selected"]) >= 4.5


def test_no_rich_text_or_widget_style_carries_a_colour_of_its_own():
    """Verdict words and notes take theme.span(...) or a role in the style sheet, so a theme
    switch reaches them; only the maps and the (light) plots keep fixed colours."""
    for path in APP.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"style='color:#", text), path.name
        assert not re.search(r'setStyleSheet\("color', text), path.name
        assert not re.search(r'QColor\("#(c0392b|b9770e|f0f0f0|fff3cd|fdecea|f8c9c4)"\)', text), path.name


def run_in_window(body: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    prelude = textwrap.dedent('''
        import os, re, sys, tempfile
        from PySide6 import QtCore, QtWidgets
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.IniFormat)       # never the user's
        QtCore.QSettings.setPath(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, tempfile.mkdtemp())
        from coppernicus.app import theme
        from coppernicus.app.main_window import MainWindow
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        def stored():
            return QtCore.QSettings(QtCore.QSettings.IniFormat, QtCore.QSettings.UserScope, "Coppernicus",
                                    "Coppernicus").value(theme.SETTING)
    ''')
    out = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(body)], env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stdout + out.stderr


def test_dark_by_default_light_on_the_button_and_kept_for_the_next_start():
    run_in_window('''
        from PySide6.QtGui import QPalette
        window = MainWindow()
        window.dirty = False
        assert theme.current() == "dark" and stored() is None
        assert app.property(theme.STYLE_PROPERTY) == "Fusion"         # the same look everywhere
        assert app.style().metaObject().className() == "QStyleSheetStyle"   # over Fusion
        assert app.palette().color(QPalette.Window).name() == "#16191c"
        assert window.view.plotter.background_color.hex_rgb == theme.color("map")
        assert window.theme_action.text() == "☀ Light"
        assert window.theme_action in window.view_menu.actions()
        assert window.theme_action.shortcut().toString() == "Ctrl+Shift+L"
        window.theme_action.trigger()
        assert theme.current() == "light" and stored() == "light"
        assert app.palette().color(QPalette.Window).name() == "#ffffff"
        assert window.view.plotter.background_color.hex_rgb == theme.color("map", "light")
        assert window.theme_action.text() == "☾ Dark"
        window.close()

        again = MainWindow()                                          # the next start
        again.dirty = False
        assert theme.current() == "light" and again.theme_action.text() == "☾ Dark"
        again.theme_action.trigger()
        assert theme.current() == "dark" and stored() == "dark"
    ''')


def test_a_switch_recolours_what_is_already_on_screen():
    """A FAIL row, a refused cell and a verdict line written in dark read in the light theme's
    colours after the switch, and back; nothing of the other theme stays behind."""
    run_in_window('''
        from PySide6.QtWidgets import QLabel, QTableWidget, QTableWidgetItem
        window = MainWindow()
        window.dirty = False
        table = window.findChildren(QTableWidget)[0]
        table.setRowCount(1)
        fail, refused = QTableWidgetItem("FAIL"), QTableWidgetItem("50 x")
        fail.setForeground(theme.brush("fail"))
        refused.setBackground(theme.brush("cell-error"))
        table.setItem(0, 0, fail)
        table.setItem(0, 1, refused)
        label = QLabel(window)
        label.setText("+3V3: " + theme.span("fail", "<b>FAIL</b>") + " · " + theme.span("warn", "not judged"))

        def left_over(name):
            """State colours of theme ``name`` still on screen."""
            hexes = {theme.color(k, name) for k in theme.STATE_TOKENS}
            found = set()
            for lab in window.findChildren(QLabel):
                found |= {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", lab.text())} & hexes
            for t in window.findChildren(QTableWidget):
                for r in range(t.rowCount()):
                    for c in range(t.columnCount()):
                        item = t.item(r, c)
                        if item is not None:
                            shown = {item.foreground().color().name(), item.background().color().name()}
                            found |= shown & hexes
            return found

        assert left_over("dark")
        window.toggle_theme()
        assert theme.current() == "light"
        assert fail.foreground().color().name() == theme.color("fail", "light")
        assert refused.background().color().name() == theme.color("cell-error", "light")
        assert theme.color("fail", "light") in label.text() and theme.color("warn", "light") in label.text()
        assert not left_over("dark"), left_over("dark")
        window.toggle_theme()
        assert fail.foreground().color().name() == theme.color("fail", "dark")
        assert not left_over("light"), left_over("light")
        assert refused.background().color() == theme.qcolor("cell-error")    # still recognised as refused
    ''')
