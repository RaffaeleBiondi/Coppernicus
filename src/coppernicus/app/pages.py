"""The results pages of the main window and the navigator that chooses one (layout C).

The pages are grouped as the analyses are: Rail (Problems, Loss by layer, Compare), Power tree
(Tree, Temperature), AC (Impedance, Ripple & noise) and Tolerance. :class:`PageStack` shows one
page at a time and keeps the calls of the QTabWidget it replaced (``addTab``-like
:meth:`PageStack.add_page`, ``count``, ``tabText``, ``widget``, ``indexOf``, ``currentWidget``,
``setCurrentWidget``, ``setCurrentIndex``, ``removeTab``, the ``currentChanged`` signal), so code
that asked the tabs for a page still works. :class:`Navigator` lists the groups and their pages;
choosing a group shows its first page.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from coppernicus.app import shown_label
from coppernicus.app.verdict import BADGE_ROLE, STALE_TIP, BadgeDelegate

PAGE_ROLE = Qt.UserRole          # on a navigator item: the page widget's id (None on a group)


class Navigator(QTreeWidget):
    """Groups and their pages; one page selected, as the stack shows it."""

    page_chosen = Signal(object)          # the page widget

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setRootIsDecorated(False)
        self.setIndentation(14)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setObjectName("navigator")
        self._groups: Dict[str, QTreeWidgetItem] = {}
        self._items: Dict[int, QTreeWidgetItem] = {}       # id(page) -> its item
        self._pages: Dict[int, QWidget] = {}
        self.currentItemChanged.connect(self._current_changed)
        self.setItemDelegate(BadgeDelegate(self))

    def set_badge(self, page: QWidget, data) -> None:
        """``page``'s badge, (kind, text) as :data:`BADGE_ROLE` holds it, or None for none."""
        item = self._items.get(id(page))
        if item is not None:
            item.setData(0, BADGE_ROLE, data)
            item.setToolTip(0, STALE_TIP if data and len(data) > 2 and data[2] else "")

    def set_group_badge(self, group: str, data) -> None:
        item = self._groups.get(group)
        if item is not None:
            item.setData(0, BADGE_ROLE, data)
            item.setToolTip(0, STALE_TIP if data and len(data) > 2 and data[2] else "")

    def badge_of(self, page: QWidget):
        item = self._items.get(id(page))
        return item.data(0, BADGE_ROLE) if item is not None else None

    def group_badge(self, group: str):
        item = self._groups.get(group)
        return item.data(0, BADGE_ROLE) if item is not None else None

    def add(self, page: QWidget, label: str, group: Optional[str]) -> None:
        """``page`` under ``group`` (a top-level page of its own when ``group`` is None or the
        same words as ``label``)."""
        text = shown_label(label)
        if group is None or group == text:
            item = QTreeWidgetItem([text])
            self.addTopLevelItem(item)
            self._bold(item)
        else:
            parent = self._groups.get(group)
            if parent is None:
                parent = QTreeWidgetItem([group])
                parent.setFlags(parent.flags() & ~Qt.ItemIsSelectable)
                self._bold(parent)
                self.addTopLevelItem(parent)
                self._groups[group] = parent
            item = QTreeWidgetItem(parent, [text])
            parent.setExpanded(True)
        item.setData(0, PAGE_ROLE, id(page))
        self._items[id(page)] = item
        self._pages[id(page)] = page

    def remove(self, page: QWidget) -> None:
        item = self._items.pop(id(page), None)
        self._pages.pop(id(page), None)
        if item is None:
            return
        parent = item.parent()
        if parent is None:
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        else:
            parent.removeChild(item)
            if parent.childCount() == 0:
                self.takeTopLevelItem(self.indexOfTopLevelItem(parent))
                self._groups = {k: v for k, v in self._groups.items() if v is not parent}

    def select(self, page: QWidget) -> None:
        item = self._items.get(id(page))
        if item is not None and self.currentItem() is not item:
            self.blockSignals(True)
            self.setCurrentItem(item)
            self.blockSignals(False)

    def item_of(self, page: QWidget) -> Optional[QTreeWidgetItem]:
        return self._items.get(id(page))

    def groups(self) -> List[str]:
        """The top-level entries, in order (a group's name or a page of its own)."""
        return [self.topLevelItem(i).text(0) for i in range(self.topLevelItemCount())]

    def _current_changed(self, item: Optional[QTreeWidgetItem], _previous):
        if item is None:
            return
        if item.data(0, PAGE_ROLE) is None and item.childCount():      # a group: its first page
            self.setCurrentItem(item.child(0))
            return
        page = self._pages.get(item.data(0, PAGE_ROLE))
        if page is not None:
            self.page_chosen.emit(page)

    @staticmethod
    def _bold(item: QTreeWidgetItem):
        font = item.font(0)
        font.setWeight(font.Weight.DemiBold)
        item.setFont(0, font)


class PageStack(QStackedWidget):
    """The results pages, one at a time, with the calls of the tab widget it replaced."""

    def __init__(self, navigator: Navigator, parent=None):
        super().__init__(parent)
        self.navigator = navigator
        self._labels: Dict[int, str] = {}
        navigator.page_chosen.connect(self.setCurrentWidget)
        self.currentChanged.connect(self._follow)

    def add_page(self, page: QWidget, label: str, group: Optional[str] = None) -> int:
        """Add ``page``; ``label`` as the old tab said it ("&&" for a shown "&")."""
        index = self.addWidget(page)
        self._labels[id(page)] = label
        self.navigator.add(page, label, group)
        if self.count() == 1:
            self.navigator.select(page)
        return index

    def tabText(self, index: int) -> str:
        page = self.widget(index)
        return self._labels.get(id(page), "") if page is not None else ""

    def removeTab(self, index: int) -> None:
        page = self.widget(index)
        if page is None:
            return
        self.navigator.remove(page)
        self._labels.pop(id(page), None)
        self.removeWidget(page)
        page.hide()

    def setCurrentWidget(self, page: QWidget) -> None:
        super().setCurrentWidget(page)
        self.navigator.select(page)

    def setCurrentIndex(self, index: int) -> None:
        super().setCurrentIndex(index)
        page = self.widget(index)
        if page is not None:
            self.navigator.select(page)

    def _follow(self, index: int):
        page = self.widget(index)
        if page is not None:
            self.navigator.select(page)


class SetupDock(QDockWidget):
    """The right panel (layout C): the setup of the page shown, the same place and look for
    every analysis, its solve button at the foot. Hidden with the button in its title bar;
    View → Setup panel, or the "Setup" button in the status bar, shows it again.

    Each setup is added once (:meth:`add_setup`) with its foot buttons; :meth:`show_setup`
    picks the setup, its title and the foot button the page asks for."""

    def __init__(self, parent=None):
        super().__init__("Setup", parent)
        self.setObjectName("Setup")
        self.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.title = QLabel("Setup")
        self.title.setObjectName("setupTitle")
        self.hide_button = QToolButton()
        self.hide_button.setText("⇥")
        self.hide_button.setToolTip("Hide the setup panel (View → Setup panel shows it again)")
        self.hide_button.setAutoRaise(True)
        self.hide_button.clicked.connect(self.hide)
        bar = QWidget()
        bar.setObjectName("setupBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(10, 4, 4, 4)
        bl.addWidget(self.title, 1)
        bl.addWidget(self.hide_button)
        self.setTitleBarWidget(bar)
        self.stack = QStackedWidget()
        self.toggleViewAction().setText("Setup panel")
        self._setups: Dict[str, Tuple[QWidget, Dict[str, QWidget]]] = {}
        self._key: Optional[str] = None

    def set_header(self, widget: QWidget) -> None:
        """``widget`` above every setup (the Mesher box: it applies to every solve)."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(widget)
        layout.addWidget(self.stack, 1)
        self.setWidget(box)
        self.header = widget

    def add_setup(self, key: str, area: QWidget, foot: Dict[str, QWidget]) -> None:
        """``area`` (scrolls on its own) above ``foot``: the buttons by name, one shown at a time."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(area, 1)
        footer = QWidget()
        footer.setObjectName("setupFoot")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(8, 8, 8, 8)
        for button in foot.values():
            button.setMinimumHeight(32)
            fl.addWidget(button)
        layout.addWidget(footer)
        self.stack.addWidget(page)
        if self.widget() is None:
            self.setWidget(self.stack)
        self._setups[key] = (page, dict(foot))

    def show_setup(self, key: str, title: str, button: Optional[str] = None) -> None:
        """Show the setup ``key`` under ``title``, with its foot button ``button`` (the first one
        when None)."""
        page, foot = self._setups[key]
        self.stack.setCurrentWidget(page)
        self.title.setText(title)       # not the window title: it would rename View → Setup panel
        chosen = button if button in foot else next(iter(foot), None)
        for name, widget in foot.items():
            widget.setVisible(name == chosen)
        self._key = key

    def current(self) -> Optional[str]:
        return self._key

    def foot_button(self, key: str, name: str) -> QWidget:
        return self._setups[key][1][name]
