"""In-window plugin manager shell. Local installation and authoring; no GitHub account required."""
from ui.styles.binding import apply_style
from ui.i18n import add_caption_tab, tr, localize

from weakref import ref
from ui.plugin_manager_pages import PluginManagerPages
from shiboken6 import isValid
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                              QPushButton, QTabWidget, QVBoxLayout, QWidget)


_INPUT_EVENTS = frozenset((QEvent.MouseButtonPress, QEvent.MouseButtonRelease,
    QEvent.MouseButtonDblClick, QEvent.Wheel, QEvent.KeyPress, QEvent.KeyRelease,
    QEvent.ContextMenu, QEvent.Drop, QEvent.DragEnter, QEvent.DragMove))


class PluginsPanel(QWidget, PluginManagerPages):
    dismissed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.host_window = window
        self._dismissed = False
        previous = QApplication.focusWidget()
        self._previous_focus = ref(previous) if previous is not None else lambda: None
        self.setObjectName('PluginsOverlay')
        self.setAttribute(Qt.WA_StyledBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        apply_style(self, '\n            QWidget#PluginsOverlay {background:rgba(0,0,0,65);}\n            QFrame#PluginsReview {background:@background.panel@;border:1px solid @text.disabled@;border-radius:8px;}\n            QFrame#PluginsReview QLabel {background:transparent;border:none;color:@text.primary@;}\n            QTabWidget#PluginsTabs::pane {border:1px solid @border.subtle@;border-radius:8px;background:@background.window@;}\n            QTabBar::tab {background:@background.panel@;color:@text.secondary@;padding:9px 14px;border:none;}\n            QTabBar::tab:selected {background:@control.selected@;color:@text.primary@;}\n            QScrollArea {background:@background.window@;border:none;}\n            QWidget#PluginPriorityHost {background:@background.window@;}\n            QFrame#PluginsReview QPushButton {background:@background.surface@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:6px;padding:5px 9px;}\n            QFrame#PluginsReview QPushButton:disabled {color:@text.disabled@;background:@background.panel@;}\n            QFrame#PluginsReview QPushButton:hover {background:@control.hover@;}\n            QFrame#PluginsReview QLineEdit, QFrame#PluginsReview QPlainTextEdit {background:@background.window@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:4px;padding:4px;}\n            QFrame#PluginsReview QComboBox {background:@background.panel@;color:@text.primary@;padding:4px;}\n            QPushButton#PluginsClose {padding:6px 12px;border-radius:7px;}\n        ')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        self.frame = QFrame(self)
        self.frame.setObjectName('PluginsReview')
        inside = QVBoxLayout(self.frame)
        inside.setContentsMargins(18, 18, 18, 18)
        heading = QHBoxLayout()
        title = localize(QLabel('', self.frame), 'ui.plugins_panel.plugins_68dd')
        title.setStyleSheet('font-size:14px;font-weight:600;')
        heading.addWidget(title)
        heading.addStretch()
        self.close_button = localize(QPushButton('', self.frame), 'ui.top_bar.close_a7a4')
        from ui.icons import set_icon
        set_icon(self.close_button, 'close', size=18, square=True)
        self.close_button.setObjectName('PluginsClose')
        self.close_button.clicked.connect(self.dismiss)
        heading.addWidget(self.close_button)
        inside.addLayout(heading)
        self.tabs = QTabWidget(self.frame)
        self.tabs.setObjectName('PluginsTabs')
        for name, identity in ((tr('ui.caption.4cf2db727881'), 'installed'), (tr('ui.caption.d659e27ebecd'), 'library'), (tr('ui.caption.d2ab5a1bb639'), 'export')):
            page = QWidget(self.tabs)
            page.setObjectName('PluginsPage_' + identity)
            add_caption_tab(self.tabs, page, name)
        inside.addWidget(self.tabs, 1)
        self.setup_manager()
        from ui.plugin_authoring import install_authoring_ui
        install_authoring_ui(self)
        outer.addWidget(self.frame, 0, Qt.AlignHCenter)
        outer.addStretch()
        self.setGeometry(window.rect())
        self._size_frame()
        QApplication.instance().installEventFilter(self)
        self.show(); self.raise_(); self.close_button.setFocus()

    def _size_frame(self):
        self.frame.setFixedSize(max(1, min(960, self.width() - 48)),
                                max(1, min(650, self.height() - 48)))

    def eventFilter(self, watched, event):
        if self._dismissed:
            return False
        kind = event.type()
        if watched is self.host_window and kind == QEvent.Resize:
            self.setGeometry(self.host_window.rect()); self._size_frame(); self.raise_()
        if kind not in _INPUT_EVENTS and kind not in (QEvent.Shortcut, QEvent.ShortcutOverride):
            return False
        # Confine interception to our host window, including its popup windows.
        # Other application windows keep their own shortcut/input handling.
        belongs = watched is self.host_window.windowHandle()
        current = watched
        while current is not None:
            if current is self.host_window:
                belongs = True
                break
            current = current.parent()
        if not belongs:
            return False
        if QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None:
            return False
        if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.dismiss(); event.accept(); return True
        if kind in (QEvent.Shortcut, QEvent.ShortcutOverride):
            event.accept(); return True
        if kind in _INPUT_EVENTS and isinstance(watched, QWidget) and watched is not self and not self.isAncestorOf(watched):
            return True
        if kind == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            forward = event.key() != Qt.Key_Backtab and not event.modifiers() & Qt.ShiftModifier
            start = QApplication.focusWidget() or self.close_button
            current = start
            while True:
                current = current.nextInFocusChain() if forward else current.previousInFocusChain()
                if current is start:
                    break
                if self.isAncestorOf(current) and current.isVisible() and current.isEnabled() and current.focusPolicy() & Qt.TabFocus:
                    current.setFocus(Qt.TabFocusReason if forward else Qt.BacktabFocusReason)
                    break
            return True
        return False

    def dismiss(self):
        if self._dismissed:
            return
        self.priority_list.cancel_drag()
        if hasattr(self, 'cancel_network'):
            self.cancel_network()
        if hasattr(self, 'icon_cancel'):
            self.icon_cancel.set()
        self._dismissed = True
        QApplication.instance().removeEventFilter(self)
        self.hide()
        self.dismissed.emit()
        previous = self._previous_focus()
        if previous is not None and isValid(previous) and previous.isVisible():
            previous.setFocus()
        self.deleteLater()
