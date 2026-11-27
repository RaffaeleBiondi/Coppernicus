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

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app import theme
from coppernicus.app.theme import WHITE
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


STALE_TIP = "The setup changed since this solve: solve again"


def set_badge(label: QLabel, verdict: Optional[str], stale: bool = False) -> None:
    """``label`` as the badge of ``verdict`` ("fail", "pass", "not_judged"; None: hidden).
    ``stale``: the setup changed since the solve, the badge outlined dashed in the verdict's
    colour instead of filled, and its tooltip says to solve again."""
    label.setVisible(verdict in BADGE_WORDS)
    label.setText(BADGE_WORDS.get(verdict, ""))
    label.setProperty("badge", verdict or "")
    label.setProperty("stale", "true" if stale and verdict in BADGE_WORDS else "")
    label.setToolTip(STALE_TIP if stale else "")
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

    def show_data(self, data: CardData, stale: bool = False) -> None:
        self.key.setText(data.key)
        self.value.setText(data.value)
        self.detail.setText(data.detail)
        self.detail.setVisible(bool(data.detail))
        self.setProperty("state", data.state)
        self.setProperty("stale", "true" if stale else "")
        self.setToolTip(STALE_TIP if stale else "")
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

    def show_verdict(self, verdict: Optional[str], cards: Sequence[CardData], stale: bool = False) -> None:
        """The badge of ``verdict`` (None: none) and up to four cards (the others hidden);
        ``stale``: the result is from before a setup change (dashed badge, solve again)."""
        set_badge(self.badge, verdict, stale)
        for i, card in enumerate(self.cards):
            if i < len(cards):
                card.show_data(cards[i], stale)
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


# ---------------------------------------------------------------------- badges in lists

BADGE_ROLE = Qt.UserRole + 7      # on a navigator or net-list item: (kind, text), or None
# kind: "fail", "pass", "attention" (filled, white text), "nj" (not judged: outlined), "ns" (not
# solved: dashed outline), "note" (plain muted words, such as "off" or "no baseline").
VERDICT_KIND = {"fail": "fail", "pass": "pass", "not_judged": "nj"}
RANK = {"fail": 3, "not_judged": 2, "pass": 1}


def worst(verdicts) -> Optional[str]:
    """FAIL > not judged > PASS over ``verdicts`` (None and "" left out); None when empty."""
    known = [v for v in verdicts if v in RANK]
    return max(known, key=RANK.get) if known else None


def badge(verdict: Optional[str], text: Optional[str] = None, stale: bool = False):
    """The (kind, text[, stale]) of a verdict: "not solved" when None; ``stale`` adds the mark
    of a result from before a setup change."""
    if verdict is None:
        return ("ns", "not solved")
    out = (VERDICT_KIND.get(verdict, "nj"), text or BADGE_WORDS.get(verdict, "not judged"))
    return out + (True,) if stale else out


class BadgeDelegate(QStyledItemDelegate):
    """Draws an item as usual, then its badge (BADGE_ROLE) at the right end of the row, in the
    theme's colours at paint time (so a theme switch needs nothing more than a repaint)."""

    def paint(self, painter: QPainter, option, index):
        data = index.data(BADGE_ROLE)
        if not data:
            super().paint(painter, option, index)
            return
        kind, text = data[0], data[1]
        stale = len(data) > 2 and bool(data[2])
        font = QFont(option.font)
        font.setPointSizeF(max(font.pointSizeF() * 0.85, 7.0))
        font.setWeight(QFont.DemiBold if kind in ("fail", "pass", "attention") else QFont.Normal)
        fm = QFontMetrics(font)
        w, h = fm.horizontalAdvance(text) + 12, fm.height() + 2
        r = option.rect
        pill = QRectF(r.right() - w - 6, r.center().y() - h / 2 + 0.5, w, h)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.rect = r.adjusted(0, 0, -int(w) - 10, 0)          # the item's text stops before the badge
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        bg_opt = QStyleOptionViewItem(option)                    # the row's background, full width
        self.initStyleOption(bg_opt, index)
        bg_opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, bg_opt, painter, widget)
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)    # text and icon
        selected = bool(option.state & QStyle.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(font)
        white = QColor(WHITE)
        if stale and kind in ("fail", "pass", "attention", "nj"):
            # A result from before a setup change: the verdict's colour, outlined dashed, never filled.
            token = {"fail": "fail", "pass": "pass", "attention": "warn", "nj": "muted"}[kind]
            pen = QPen(white if selected else theme.qcolor(token), 1)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            painter.setPen(white if selected else theme.qcolor(token))
        elif kind in ("fail", "pass", "attention"):
            painter.setPen(QPen(white, 1) if selected else Qt.NoPen)
            painter.setBrush(theme.qcolor(f"badge-{kind}"))
            painter.drawRoundedRect(pill, 4, 4)
            painter.setPen(white)
        elif kind in ("nj", "ns"):
            pen = QPen(white if selected else theme.qcolor("outline" if kind == "ns" else "line"), 1)
            if kind == "ns":
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            painter.setPen(white if selected else theme.qcolor("muted"))
        else:                                                    # a plain note
            painter.setPen(white if selected else theme.qcolor("muted"))
        painter.drawText(pill, Qt.AlignCenter, text)
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.data(BADGE_ROLE):
            size.setHeight(max(size.height(), QFontMetrics(option.font).height() + 8))
        return size



# ---------------------------------------------------------------------- the AC and noise cards

def ac_cards(r) -> List[CardData]:
    """The cards of an impedance result, of its worst probe (a failing one first, else the
    largest |Z| over its target, else the first): its worst |Z| and where, its target, its
    lowest and highest |Z|. Only a probe that fails its target gets a red border."""
    from coppernicus.impedance import hz_text, ohm_text
    if not r.probes:
        return []
    probes = list(r.probes)
    p = next((q for q in probes if q.passed is False), None) or max(
        probes, key=lambda q: q.worst_ratio if q.worst_ratio is not None else -1.0)
    ratio = f", {p.worst_ratio:.3g}× the target" if p.worst_ratio is not None else ""
    mag = p.mag_ohm
    target = _ac_target(p)
    return [CardData(f"Worst |Z|, {p.name}", ohm_text(p.worst_z_ohm), f"at {hz_text(p.worst_hz)}{ratio}",
                     "fail" if p.passed is False else ""),
            CardData(f"Target, {p.name}", target or "none", "" if target else "ranked, not judged"),
            CardData("Lowest |Z|", ohm_text(float(mag.min()))),
            CardData("Highest |Z|", ohm_text(float(mag.max())))]


def _ac_target(p) -> str:
    from coppernicus.app.ac_panel import _target_text
    return _target_text(p)


def noise_cards(r) -> List[CardData]:
    """The cards of a noise result: the worst victim (the only one judged, against its margin),
    the ripple, the first phase's hot loop and the radiated estimate (never judged: never a
    coloured border)."""
    from coppernicus.impedance import hz_text
    cards: List[CardData] = []
    if r.victims:
        v = max(r.victims, key=lambda x: (x.ratio if x.ratio is not None else -1.0, x.peak_v))
        detail = (f"{100 * v.ratio:.3g} % of its {v.margin_v * 1e3:.4g} mV margin" if v.ratio is not None
                  else "no margin set: ranked")
        cards.append(CardData("Worst victim", f"{v.name} {v.peak_v * 1e3:.4g} mV", detail,
                              "fail" if v.passed is False else ""))
    if r.ripple:
        rp = r.ripple[0]
        cards.append(CardData(f"Ripple at {rp.name}", f"{rp.pp_bw_v * 1e3:.4g} mV p-p",
                              "through 20 MHz, never judged"))
    if r.hot_loops:
        h = r.hot_loops[0]
        ring = f"rings at {hz_text(h.f_ring_hz)}" if h.f_ring_hz else "no ringing"
        cards.append(CardData(f"Hot loop, phase {h.phase}", f"{h.loop_l_h * 1e9:.3g} nH", ring))
    e = getattr(r, "emi", None)
    if e is not None and getattr(e, "radiated_dbuv_m", None) is not None:
        cards.append(CardData("Radiated estimate", f"{e.radiated_dbuv_m:.1f} dBµV/m",
                              f"at {hz_text(e.radiated_hz)}, {e.distance_m:g} m: for scale, never judged"))
    return cards[:VerdictHeader.MAX_CARDS]


def tolerance_cards(row) -> List[CardData]:
    """The cards of the selected load's stack-up: each check (total, DC, AC) as below / above
    in mV against what it allows (red only when it fails), and KiPI's transient estimate (shown,
    never judged)."""
    names = {"total": "Total", "dc": "DC", "ac": "AC"}
    cards: List[CardData] = []
    for kind in ("total", "dc", "ac"):
        c = row.checks.get(kind)
        if c is None:
            continue
        value = f"{c.below_v * 1e3:.4g} / {c.above_v * 1e3:.4g} mV"
        if c.limit and c.allowed_below_v is not None and c.allowed_above_v is not None:
            detail = (f"{c.limit}: allowed {c.allowed_below_v * 1e3:.4g} / {c.allowed_above_v * 1e3:.4g} mV"
                      + ("" if c.complete else ", parts missing"))
        else:
            detail = c.reason or (c.limit or "no tolerance set")
        cards.append(CardData(f"{names[kind]}, {c.limit or 'no limit'}", value, detail,
                              "fail" if c.passed is False else ""))
    est = getattr(row, "transient_estimate_v", None)
    if est:
        cards.append(CardData("KiPI transient", f"{est[0] * 1e3:.4g} / {est[1] * 1e3:.4g} mV",
                              "KiPI's estimate, never judged"))
    return cards[:VerdictHeader.MAX_CARDS]
