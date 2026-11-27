"""The guided start: four steps to a first solve, shown only on the first run after the app is
installed, until the first solve (or "Don't show again"), then never again.

The card sits at the top of the page column. Each step ticks itself as it gets done (a board
open, a net picked, a source and a load set, a solve), so it reads as a checklist, not a
lesson. Whether it is done is one setting (:data:`SETTING`, in the app's settings).
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app import settings as app_settings

GUIDE_WIDTH = 600                   # px
SETTING = "view/guide_done"         # true once the first solve is done or the guide is dismissed

STEPS = (
    ("Open a board or a project",
     "A KiCad board (.kicad_pcb), or a Coppernicus project (.coppernicus.yml) that remembers your "
     "setup."),
    ("Pick a net",
     "The power net in the list on the left. Rails described with PI_ fields in KiCad are already "
     "set up."),
    ("Add a source and the loads",
     "In Rail setup: the regulator output or connector that holds the voltage, and the parts that "
     "draw current. Type them, or use Pick on board and click their pads."),
    ("Solve",
     "Solve rail (<kbd>F5</kbd>). The map shows the drop; Problems lists what to look at first. "
     "Without limits nothing is judged: add one to get a verdict."),
)


def done() -> bool:
    return str(app_settings().value(SETTING, "false")).lower() in ("true", "1")


def set_done() -> None:
    app_settings().setValue(SETTING, True)


class GuideCard(QFrame):
    """The four steps, each ticked when done; Open board… and Open project… on the first."""

    open_board = Signal()
    open_project = Signal()
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("guide")
        self.setFixedWidth(GUIDE_WIDTH)           # its labels wrap at a known width: never clipped
        title = QLabel("Check a power net in four steps")
        title.setObjectName("guideTitle")
        intro = QLabel("Coppernicus finds where the copper of a power net drops voltage or crowds "
                       "current. It reads your board; it never writes KiCad files.")
        intro.setWordWrap(True)
        intro.setProperty("role", "muted")
        self.intro = intro
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        self.marks: List[QLabel] = []
        self.texts: List[QLabel] = []
        for i, (head, body) in enumerate(STEPS):
            mark = QLabel(str(i + 1))
            mark.setObjectName("guideStep")
            mark.setAlignment(Qt.AlignCenter)
            mark.setFixedSize(24, 24)
            text = QLabel(f"<b>{head}</b><br>{body}")
            text.setWordWrap(True)
            text.setTextFormat(Qt.RichText)
            grid.addWidget(mark, i, 0, Qt.AlignTop)
            grid.addWidget(text, i, 1)
            self.marks.append(mark)
            self.texts.append(text)
        board, project = QPushButton("Open board…"), QPushButton("Open project…")
        board.setObjectName("guideOpen")
        board.clicked.connect(self.open_board.emit)
        project.clicked.connect(self.open_project.emit)
        self.open_buttons = QWidget()
        buttons = QHBoxLayout(self.open_buttons)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(board)
        buttons.addWidget(project)
        buttons.addStretch(1)
        grid.addWidget(self.open_buttons, len(STEPS), 1)
        keys = QLabel("Keys: <kbd>+</kbd> <kbd>−</kbd> change the copper layer, as in KiCad.")
        keys.setProperty("role", "muted")
        dismiss = QPushButton("Don't show again")
        dismiss.clicked.connect(self._dismiss)
        foot = QHBoxLayout()
        foot.addWidget(keys, 1)
        foot.addWidget(dismiss)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(grid)
        layout.addLayout(foot)
        self.dismiss_button = dismiss
        self.set_progress([False] * len(STEPS))

    def set_progress(self, ticked: Sequence[bool]) -> None:
        """Tick the steps done; the first not done is the current one. Done steps keep their
        heading only, and the introduction and the Open buttons go once a board is open: the
        card shrinks as it is followed."""
        current: Optional[int] = next((i for i, t in enumerate(ticked) if not t), None)
        for i, mark in enumerate(self.marks):
            state = "done" if ticked[i] else "current" if i == current else ""
            mark.setText("✓" if ticked[i] else str(i + 1))
            mark.setProperty("state", state)
            mark.style().unpolish(mark)
            mark.style().polish(mark)
            head, body = STEPS[i]
            self.texts[i].setText(f"<b>{head}</b>" if ticked[i] else f"<b>{head}</b><br>{body}")
        self.intro.setVisible(not ticked[0])
        self.open_buttons.setVisible(not ticked[0])
        self.fit_height()

    def fit_height(self) -> None:
        """As tall as its text needs at its width: the page column gives way, never the card."""
        layout = self.layout()
        layout.activate()
        need = (layout.totalHeightForWidth(GUIDE_WIDTH) if layout.hasHeightForWidth()
                else layout.totalSizeHint().height())
        self.setFixedHeight(need)
        row = self.parentWidget()
        while row is not None and not isinstance(row, QScrollArea):
            row = row.parentWidget()
        if row is not None:
            row.updateGeometry()

    def progress(self) -> List[bool]:
        return [m.property("state") == "done" for m in self.marks]

    def _dismiss(self):
        set_done()
        self.hide()
        self.dismissed.emit()


class GuideRow(QScrollArea):
    """``card`` centred in a row of the page column. It scrolls when the window is short (a
    laptop screen), so the card is never clipped and never makes the window's minimum taller."""

    MIN_HEIGHT = 120

    def __init__(self, card: GuideCard, parent=None):
        super().__init__(parent)
        inner = QWidget()
        layout = QHBoxLayout(inner)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.addStretch(1)
        layout.addWidget(card)
        layout.addStretch(1)
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)   # never more than the card
        self._card = card

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(self._card.maximumHeight() + 12)   # the whole card (its fixed height) when room
        return hint


def placed(card: GuideCard) -> "GuideRow":
    return GuideRow(card)
