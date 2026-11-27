"""The verdict line and the cards at the top of a result (layout C, the DC pages).

:class:`VerdictHeader` shows the verdict as a badge (FAIL / PASS / not judged), the result's
summary line beside it, and a row of :class:`Card`: one number each, with what it is above and
where it comes from under it. The numbers are the result's own (:func:`dc_cards`); the badge is
the verdict the summary states, computed once by the caller. Colours come from the theme's
style sheet (``badge`` and ``state`` properties), never from colours written into the text, so
a theme switch needs no recolouring here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from coppernicus.problems import ATTENTION, FAIL, drop_words

BADGE_WORDS = {"fail": "FAIL", "pass": "PASS", "not_judged": "not judged"}


@dataclass(frozen=True)
class CardData:
    """One card: what it is (``key``), its number (``value``), where it comes from
    (``detail``) and its state: "fail", "attention" or "" (a plain number)."""
    key: str
    value: str
    detail: str = ""
    state: str = ""


def _restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_badge(label: QLabel, verdict: Optional[str]) -> None:
    """``label`` as the badge of ``verdict`` ("fail", "pass", "not_judged"; None: hidden)."""
    label.setVisible(verdict in BADGE_WORDS)
    label.setText(BADGE_WORDS.get(verdict, ""))
    label.setProperty("badge", verdict or "")
    _restyle(label)


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.key, self.value, self.detail = QLabel(), QLabel(), QLabel()
        self.key.setProperty("role", "muted")
        self.detail.setProperty("role", "muted")
        self.value.setObjectName("cardValue")
        self.detail.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 7)
        layout.setSpacing(1)
        for w in (self.key, self.value, self.detail):
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(w)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

    def show_data(self, data: CardData) -> None:
        self.key.setText(data.key)
        self.value.setText(data.value)
        self.detail.setText(data.detail)
        self.detail.setVisible(bool(data.detail))
        self.setProperty("state", data.state)
        _restyle(self)


class VerdictHeader(QWidget):
    """The badge, the summary line (``summary``, a label owned by the caller) and the cards."""

    MAX_CARDS = 4

    def __init__(self, summary: QLabel, parent=None):
        super().__init__(parent)
        self.setObjectName("verdictHeader")
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        self.summary = summary
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(self.badge, 0, Qt.AlignTop)
        line.addWidget(summary, 1)
        self.cards = [Card() for _ in range(self.MAX_CARDS)]
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for card in self.cards:
            row.addWidget(card, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(6)
        layout.addLayout(line)
        layout.addLayout(row)
        self.show_verdict(None, [])

    def show_verdict(self, verdict: Optional[str], cards: Sequence[CardData]) -> None:
        """The badge of ``verdict`` (None: none) and up to four cards (the others hidden)."""
        set_badge(self.badge, verdict)
        for i, card in enumerate(self.cards):
            if i < len(cards):
                card.show_data(cards[i])
                card.show()
            else:
                card.hide()

    def shown_cards(self) -> List[CardData]:
        """The cards shown, as data (for checks)."""
        return [CardData(c.key.text(), c.value.text(), c.detail.text(), c.property("state") or "")
                for c in self.cards if not c.isHidden()]


def dc_cards(res, problems: Sequence, limits=None) -> List[CardData]:
    """The cards of a DC result (a rail solved alone or a power-tree group), from its own
    numbers and its problem list: the worst load, the largest drop (or rise) on the copper, the
    copper loss and the strongest current crowding. Cards with nothing to show are left out."""
    cards: List[CardData] = []
    fails = [p for p in problems if p.severity == FAIL]
    if res.loads:
        worst = max(res.loads, key=res.drop)                 # by mV: a % is NaN at 0 V
        pct = res.drop_pct(worst)
        detail = f"{pct:.2f} % of the source voltage" if pct == pct else ""
        limit = getattr(limits, "max_drop_pct", None) if limits is not None else None
        if limit is not None and detail:
            detail += f", limit {limit:g} %"
        failed = any(p.kind == "load" and p.data.get("name") == worst.name for p in fails)
        cards.append(CardData("Worst load", f"{worst.name}: {res.drop(worst) * 1e3:.2f} mV", detail,
                              "fail" if failed else ""))
    what, where = drop_words(res.max_drop, res.v_ref, res.sign, res.is_rise())
    mv = abs(res.max_drop) * 1e3 if res.is_rise() else res.max_drop * 1e3
    rise_failed = any(p.kind == "rise" for p in fails)
    cards.append(CardData(f"Largest {what} on the copper", f"{mv:.2f} mV", where,
                          "fail" if rise_failed else ""))
    shares = res.loss_breakdown()
    total = sum(shares.values())
    if total > 0:
        top, part = max(shares.items(), key=lambda kv: kv[1])
        cards.append(CardData("Copper loss", f"{res.power_loss * 1e3:.3g} mW",
                              f"most on {top} ({part / total:.0%})"))
    else:
        cards.append(CardData("Copper loss", f"{res.power_loss * 1e3:.3g} mW"))
    spots = [p for p in problems if p.kind == "hotspot"]
    if spots:
        spot = max(spots, key=lambda p: p.value)
        state = "fail" if spot.severity == FAIL else "attention" if spot.severity == ATTENTION else ""
        where = f"{spot.layer} at ({spot.x:.2f}, {spot.y:.2f}) mm" if spot.layer else \
            f"at ({spot.x:.2f}, {spot.y:.2f}) mm"
        cards.append(CardData("Strongest current crowding", f"{spot.value:.3g} A/mm² mean", where, state))
    return cards
