"""The app's colours: dark by default, light on request, as the HTML report.

The shared tokens are read from the report's own style sheet (``report_assets/report.css``, its
two ``:root`` blocks), so the app and the report never drift apart. The few tokens only the app
needs (window chrome, inputs, table cell states) are below, with a value for each theme.

:func:`apply` sets the Fusion style (the same look on macOS, Windows and Linux, and the same
offscreen as on screen), a palette and one style sheet. Widgets that draw their own colours ask
:func:`color` and listen to :data:`signals` ``.changed``. Colours already written into table
cells and rich-text labels are swapped by :func:`recolor`, which only knows :data:`STATE_TOKENS`:
their values are distinct across both themes, so each swap is one-to-one (a test checks it).
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPalette

THEMES = ("dark", "light")
DEFAULT = "dark"
SETTING = "view/theme"            # QSettings key: "dark" or "light"
APP_NAME = "Coppernicus"
STYLE_PROPERTY = "coppernicus_style"   # on the QApplication: the Qt style apply() set

# Only the app's: the window chrome (toolbar, dock titles), inputs, hover, focus, outlines, the
# pop-up label, and the states of table cells (read-only or draft, a problem, a refused value,
# missing or a KiPI stand-in). Values from the design mock-ups (Round 2, kept out of the repository).
APP_TOKENS: Dict[str, Dict[str, str]] = {
    "dark": {"chrome": "#111316", "input": "#121518", "input-line": "#3a424a", "hover": "#262b30",
             "focus": "#6aa5e8", "outline": "#6b7580", "pop": "#0e1013",
             "cell-readonly": "#1b1f23", "cell-problem": "#3b2326", "cell-error": "#5c2a2c",
             "cell-missing": "#3a3220"},
    "light": {"chrome": "#e9edf1", "input": "#ffffff", "input-line": "#bfc8d0", "hover": "#e2e8ee",
              "focus": "#2f6fb2", "outline": "#8a949e", "pop": "#ffffff",
              "cell-readonly": "#f0f0f0", "cell-problem": "#fdecea", "cell-error": "#f8c9c4",
              "cell-missing": "#fff3cd"},
}
# The tokens that end up inside cells and rich text, swapped by recolor() on a theme change.
STATE_TOKENS = ("fail", "warn", "pass", "muted",
                "cell-readonly", "cell-problem", "cell-error", "cell-missing")
WHITE = "#ffffff"                 # text on the selected row and on the verdict badges, both themes
# The tick of a checked box (white, drawn on --selected), for the style sheet.
CHECK_IMAGE = (Path(__file__).resolve().parent / "resources" / "check.png").as_posix()

_ROOT = re.compile(r":root(\[data-theme=\"light\"\])?\s*\{([^}]*)\}")
_VAR = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})")


@lru_cache(maxsize=1)
def report_tokens() -> Dict[str, Dict[str, str]]:
    """The report's tokens per theme, from report.css: light inherits what it does not set
    (--selected and the badge colours are the same in both)."""
    css = files("coppernicus").joinpath("report_assets", "report.css").read_text(encoding="utf-8")
    dark: Dict[str, str] = {}
    light_only: Dict[str, str] = {}
    for light, body in _ROOT.findall(css):
        (light_only if light else dark).update((k, v.lower()) for k, v in _VAR.findall(body))
    if not dark:
        raise RuntimeError("report.css has no :root colour tokens")
    return {"dark": dark, "light": {**dark, **light_only}}


def tokens(theme: str) -> Dict[str, str]:
    """Every token of ``theme``: the report's, then the app's."""
    if theme not in THEMES:
        raise ValueError(f"theme is one of {THEMES}, not {theme!r}")
    return {**report_tokens()[theme], **APP_TOKENS[theme]}


class _Signals(QObject):
    changed = Signal(str)         # the new theme, after the palette and style sheet are set


signals = _Signals()
_current = DEFAULT


def current() -> str:
    """The theme in use: "dark" or "light"."""
    return _current


def color(token: str, theme: Optional[str] = None) -> str:
    """A token's colour as "#rrggbb", in ``theme`` (default: the one in use)."""
    return tokens(theme or _current)[token]


def qcolor(token: str, theme: Optional[str] = None) -> QColor:
    return QColor(color(token, theme))


def brush(token: str, theme: Optional[str] = None) -> QBrush:
    return QBrush(qcolor(token, theme))


def span(token: str, html: str) -> str:
    """``html`` in a token's colour, for rich-text labels (recolor() keeps it in step)."""
    return f"<span style='color:{color(token)}'>{html}</span>"


def _settings() -> QSettings:
    return QSettings(QSettings.defaultFormat(), QSettings.UserScope, APP_NAME, APP_NAME)


def saved() -> str:
    """The theme chosen last time, else the default (dark)."""
    value = _settings().value(SETTING, DEFAULT)
    return value if value in THEMES else DEFAULT


def save(theme: str) -> None:
    _settings().setValue(SETTING, theme)


def palette(theme: str) -> QPalette:
    t = tokens(theme)
    c = {k: QColor(v) for k, v in t.items()}
    p = QPalette()
    for role, token in ((QPalette.Window, "bg"), (QPalette.WindowText, "fg"), (QPalette.Base, "input"),
                        (QPalette.AlternateBase, "panel"), (QPalette.Text, "fg"), (QPalette.Button, "panel"),
                        (QPalette.ButtonText, "fg"), (QPalette.ToolTipBase, "pop"),
                        (QPalette.ToolTipText, "fg"), (QPalette.PlaceholderText, "muted"),
                        (QPalette.Link, "accent"), (QPalette.LinkVisited, "accent"),
                        (QPalette.BrightText, "fail"), (QPalette.Highlight, "selected"),
                        (QPalette.Light, "hover"), (QPalette.Midlight, "line"), (QPalette.Mid, "input-line"),
                        (QPalette.Dark, "outline"), (QPalette.Shadow, "chrome")):
        p.setColor(role, c[token])
    p.setColor(QPalette.HighlightedText, QColor(WHITE))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c["muted"])
    p.setColor(QPalette.Disabled, QPalette.Base, c["panel"])
    p.setColor(QPalette.Disabled, QPalette.Button, c["bg"])
    p.setColor(QPalette.Disabled, QPalette.Highlight, c["line"])
    return p


def style_sheet(theme: str) -> str:
    """The app's style sheet: the window chrome, buttons, tabs, table headers, tooltips and the
    muted labels (``setProperty("role", "muted")``); the rest comes from the palette."""
    t = tokens(theme)
    return f"""
QMainWindow, QDialog {{ background: {t['bg']}; }}
QToolBar {{ background: {t['chrome']}; border: none; border-bottom: 1px solid {t['line']};
  spacing: 2px; padding: 3px 6px; }}
QToolBar::separator {{ background: {t['line']}; width: 1px; margin: 4px 6px; }}
QToolButton {{ color: {t['fg']}; background: transparent; border: 1px solid transparent;
  border-radius: 6px; padding: 3px 8px; }}
QToolButton:hover {{ background: {t['hover']}; }}
QToolButton:pressed, QToolButton:checked {{ background: {t['line']}; }}
QToolButton:disabled {{ color: {t['muted']}; }}
QDockWidget {{ color: {t['muted']}; }}
QDockWidget::title {{ background: {t['chrome']}; padding: 4px 8px; border-bottom: 1px solid {t['line']}; }}
QMenuBar {{ background: {t['chrome']}; color: {t['fg']}; }}
QMenuBar::item:selected {{ background: {t['hover']}; }}
QMenu {{ background: {t['pop']}; color: {t['fg']}; border: 1px solid {t['line']}; padding: 4px; }}
QMenu::item {{ padding: 4px 18px 4px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['selected']}; color: {WHITE}; }}
QMenu::item:disabled {{ color: {t['muted']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 6px; }}
QPushButton {{ color: {t['fg']}; background: {t['panel']}; border: 1px solid {t['input-line']};
  border-radius: 6px; padding: 4px 12px; min-height: 18px; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed, QPushButton:checked {{ background: {t['line']}; border-color: {t['outline']}; }}
QPushButton:focus {{ border-color: {t['focus']}; }}
QPushButton:disabled {{ color: {t['muted']}; background: {t['bg']}; border-color: {t['line']}; }}
QPushButton:default {{ background: {t['selected']}; border-color: {t['selected']}; color: {WHITE}; }}
QTabWidget::pane {{ border: 1px solid {t['line']}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ color: {t['muted']}; background: transparent; border: 1px solid transparent;
  border-bottom: 2px solid transparent; padding: 5px 12px; }}
QTabBar::tab:hover {{ color: {t['fg']}; background: {t['hover']}; }}
QTabBar::tab:selected {{ color: {t['fg']}; border-bottom: 2px solid {t['accent']}; }}
QHeaderView::section {{ color: {t['muted']}; background: {t['panel']}; border: none;
  border-right: 1px solid {t['line']}; border-bottom: 1px solid {t['line']}; padding: 3px 6px;
  font-weight: 600; }}
QTableView, QTableWidget, QListView, QTreeView {{ gridline-color: {t['line']};
  border: 1px solid {t['line']}; selection-background-color: {t['selected']};
  selection-color: {WHITE}; }}
QTableCornerButton::section {{ background: {t['panel']}; border: none; }}
QToolTip {{ color: {t['fg']}; background: {t['pop']}; border: 1px solid {t['line']}; padding: 4px 6px; }}
QStatusBar {{ background: {t['chrome']}; color: {t['muted']}; border-top: 1px solid {t['line']}; }}
QSplitter::handle {{ background: {t['line']}; }}
QLineEdit {{ background: {t['input']}; border: 1px solid {t['input-line']}; border-radius: 4px;
  padding: 2px 4px; selection-background-color: {t['selected']}; selection-color: {WHITE}; }}
QLineEdit:focus {{ border-color: {t['focus']}; }}
QLineEdit[state="error"] {{ border-color: {t['fail']}; background: {t['cell-error']}; }}
QLineEdit:disabled, QLineEdit:read-only {{ background: {t['panel']}; color: {t['muted']}; }}
QCheckBox::indicator, QGroupBox::indicator, QAbstractItemView::indicator {{ width: 13px; height: 13px;
  border: 1px solid {t['outline']}; border-radius: 3px; background: {t['input']}; }}
QCheckBox::indicator:hover, QGroupBox::indicator:hover {{ border-color: {t['focus']}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked, QAbstractItemView::indicator:checked {{
  background: {t['selected']}; border-color: {t['selected']}; image: url("{CHECK_IMAGE}"); }}
QCheckBox::indicator:disabled {{ background: {t['panel']}; border-color: {t['line']}; }}
QGroupBox {{ border: 1px solid {t['line']}; border-radius: 6px; margin-top: 1.1em; padding-top: 4px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {t['fg']}; }}
QProgressBar {{ border: 1px solid {t['line']}; border-radius: 4px; background: {t['input']};
  color: {t['fg']}; text-align: center; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
QWidget#setupBar {{ background: {t['chrome']}; border-bottom: 1px solid {t['line']}; }}
QLabel#setupTitle {{ color: {t['fg']}; font-weight: 600; }}
QWidget#setupFoot {{ border-top: 1px solid {t['line']}; }}
QWidget#setupFoot QPushButton {{ background: {t['selected']}; border-color: {t['selected']}; color: {WHITE};
  font-weight: 600; }}
QWidget#setupFoot QPushButton:hover {{ border-color: {t['fg']}; }}
QWidget#setupFoot QPushButton:disabled {{ background: {t['panel']}; border-color: {t['line']};
  color: {t['muted']}; }}
QTreeWidget#navigator {{ background: {t['bg']}; border: none; }}
QTreeWidget#navigator::item {{ padding: 3px 4px; border-radius: 4px; }}
QTreeWidget#navigator::item:hover {{ background: {t['hover']}; }}
QTreeWidget#navigator::item:selected {{ background: {t['selected']}; color: {WHITE}; }}
QLabel[role="muted"] {{ color: {t['muted']}; }}
QLabel#badge {{ border-radius: 4px; padding: 1px 6px; font-weight: 600; }}
QLabel#badge[badge="fail"] {{ background: {t['badge-fail']}; color: {WHITE}; }}
QLabel#badge[badge="pass"] {{ background: {t['badge-pass']}; color: {WHITE}; }}
QLabel#badge[badge="not_judged"] {{ border: 1px solid {t['line']}; color: {t['muted']}; }}
QFrame#card {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 8px; }}
QFrame#card[state="fail"] {{ border-color: {t['fail']}; }}
QFrame#card[state="attention"] {{ border-color: {t['warn']}; }}
QFrame#card QLabel {{ background: transparent; border: none; }}
QLabel#cardValue {{ color: {t['fg']}; font-size: 17px; font-weight: 600; }}
QLabel#detailStrip {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 4px;
  padding: 5px 8px; }}
QLabel[role="fail"] {{ color: {t['fail']}; }}
QLabel[role="warning"] {{ color: {t['fg']}; background: {t['cell-missing']}; border: 1px solid {t['warn']};
  border-radius: 4px; padding: 4px; }}
"""


def apply(theme: str, app=None) -> None:
    """Make ``theme`` the app's: Fusion style, palette, style sheet, the platform's colour
    scheme (window title bars, native dialogs), then :data:`signals` ``.changed``."""
    global _current
    from PySide6.QtWidgets import QApplication

    tokens(theme)                                  # refuses an unknown name before changing anything
    app = app or QApplication.instance()
    if app is None:
        raise RuntimeError("theme.apply needs a QApplication")
    if app.property(STYLE_PROPERTY) != "Fusion":       # once: the style sheet then wraps it
        app.setStyle("Fusion")
        app.setProperty(STYLE_PROPERTY, "Fusion")
    _current = theme
    app.setPalette(palette(theme))
    app.setStyleSheet(style_sheet(theme))
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "setColorScheme"):           # Qt 6.8 and later; PySide6 6.6 runs without it
        hints.setColorScheme(Qt.ColorScheme.Dark if theme == "dark" else Qt.ColorScheme.Light)
    signals.changed.emit(theme)


def swaps(old: str, new: str) -> Dict[str, str]:
    """"#rrggbb" of each state token in ``old`` -> its value in ``new``."""
    a, b = tokens(old), tokens(new)
    return {a[k]: b[k] for k in STATE_TOKENS}


def _swap_text(text: str, table: Dict[str, str]) -> str:
    return re.sub(r"#[0-9a-fA-F]{6}\b", lambda m: table.get(m.group(0).lower(), m.group(0)), text)


def recolor(root, old: str, new: str) -> int:
    """Swap the state colours already written under ``root`` (a widget) from theme ``old`` to
    ``new``: table, tree and list cells (text and background) and rich-text labels. Returns
    how many were changed."""
    from PySide6.QtWidgets import QLabel, QListWidget, QTableWidget, QTreeWidget, QTreeWidgetItemIterator

    if old == new:
        return 0
    table = swaps(old, new)
    qtable = {QColor(k).rgb(): QColor(v) for k, v in table.items()}
    n = 0

    def item_colors(item, columns):
        nonlocal n
        for col in columns:
            for get, put in ((item.foreground, item.setForeground), (item.background, item.setBackground)):
                b = get(col) if col is not None else get()
                if b.style() == Qt.NoBrush:
                    continue
                swap = qtable.get(b.color().rgb())
                if swap is not None:
                    (put(col, QBrush(swap)) if col is not None else put(QBrush(swap)))
                    n += 1

    for t in root.findChildren(QTableWidget):
        for r in range(t.rowCount()):
            for c in range(t.columnCount()):
                item = t.item(r, c)
                if item is not None:
                    item_colors(item, [None])
    for t in root.findChildren(QTreeWidget):
        it = QTreeWidgetItemIterator(t)
        while it.value():
            item_colors(it.value(), range(t.columnCount()))
            it += 1
    for t in root.findChildren(QListWidget):
        for r in range(t.count()):
            item_colors(t.item(r), [None])
    for label in root.findChildren(QLabel):
        text = label.text()
        if "#" in text:
            swapped = _swap_text(text, table)
            if swapped != text:
                label.setText(swapped)
                n += 1
    return n
