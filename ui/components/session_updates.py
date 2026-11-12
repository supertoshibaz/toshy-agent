"""Unviewed generation updates, presented independently of the active chat."""
from PySide6.QtCore import Qt, QEvent, QPoint, QRect, QRectF, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QHBoxLayout, QToolButton, QWidget, QSizePolicy)
from ui.icons import set_icon
from shiboken6 import isValid

from ui.app_state import app_state
from ui.event_bus import event_bus
from ui.generation_status import generation_status, STATES
from ui.i18n import i18n, tr, set_text
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager

STATUS_KEYS = {
    'running': 'ui.caption.61f5c7e1a025',
    'finished': 'ui.caption.ef05d57959cf',
    'error': 'ui.caption.cc5a2561f251',
    'stopped': 'ui.caption.3028fa0907c0',
}


class SessionUpdateButton(QPushButton):
    """A two-line row whose height follows its labels, including wrapped titles."""
    def __init__(self, parent=None):
        super().__init__(parent)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def sizeHint(self):
        return self.layout().totalSizeHint() if self.layout() is not None else super().sizeHint()

    def minimumSizeHint(self):
        return self.layout().totalMinimumSize() if self.layout() is not None else super().minimumSizeHint()

    def heightForWidth(self, width):
        height = self.layout().totalHeightForWidth(width)
        return height if height >= 0 else self.sizeHint().height()


class SessionUpdatesPopup(QFrame):
    def __init__(self, anchor):
        # Interactive controls need a tool window, not a native tooltip that
        # Windows can dismiss as soon as the user presses a button.
        super().__init__(anchor, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.anchor = anchor
        self.setObjectName('SessionUpdatesPopup')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        watch_paint(self)
        apply_style(self, '''
            QFrame#SessionUpdatesPopup {background:transparent;border:0;}
            QLabel {background:transparent;border:0;}
            QLabel#SessionUpdatesTitle {color:@text.primary@;font-size:14px;font-weight:600;}
            QFrame#SessionUpdateCard {background:@background.panel@;border:0;border-radius:6px;}
            QFrame#SessionUpdateCard:hover {background:@control.hover@;}
            QPushButton#SessionUpdateRow {background:transparent;border:1px solid transparent;border-radius:6px;padding:0;}
            QPushButton#SessionUpdateRow:hover {background:@control.hover@;}
            QScrollArea, QScrollArea > QWidget > QWidget {background:transparent;border:0;}
            QScrollBar::handle:vertical {background:@border.subtle@;border-radius:4px;min-height:20px;}
            QScrollBar::handle:vertical:hover {background:@text.secondary@;}
            QToolButton#SessionReadButton {background:transparent;border:0;border-radius:5px;padding:0;min-width:28px;max-width:28px;min-height:28px;max-height:28px;}
            QToolButton#SessionReadButton:hover {background:@control.pressed@;}
            QToolButton#SessionReadAllButton {background:transparent;border:0;border-radius:5px;padding:0;min-width:28px;max-width:28px;min-height:28px;max-height:28px;}
            QToolButton#SessionReadAllButton:hover {background:@control.hover@;}
        ''')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        title = QLabel(self)
        title.setObjectName('SessionUpdatesTitle')
        title.setWordWrap(True)
        set_text(title, 'ui.session_updates.title')
        heading = QHBoxLayout()
        heading.addWidget(title, 1)
        self.clear_button = QToolButton(self)
        self.clear_button.setObjectName('SessionReadAllButton')
        self.clear_button.setFixedSize(28, 28)
        self.clear_button.setCursor(Qt.PointingHandCursor)
        set_text(self.clear_button, 'ui.session_updates.mark_all_read', setter='setToolTip')
        set_icon(self.clear_button, 'check', size=18)
        self.clear_button.clicked.connect(lambda: self.anchor.mark_read(list(self.anchor.updates)))
        heading.addWidget(self.clear_button)
        layout.addLayout(heading)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(self.scroll, 1)
        self.body = QWidget(self.scroll)
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(6)
        self.scroll.setWidget(self.body)

    def refresh(self):
        bar = self.scroll.verticalScrollBar()
        position = bar.value()
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        for key, (state, _order) in self.anchor.pending():
            card = QFrame(self.body)
            card.setObjectName('SessionUpdateCard')
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 5, 0)
            card_layout.setSpacing(2)
            row = SessionUpdateButton(card)
            row.setObjectName('SessionUpdateRow')
            row.setCursor(Qt.PointingHandCursor)
            content = QVBoxLayout(row)
            content.setContentsMargins(8, 7, 8, 7)
            content.setSpacing(3)
            title = app_state.display_session(*key)
            status = tr(STATUS_KEYS[state])
            row.setAccessibleName(title + ': ' + status)
            row.setToolTip(title + '\n' + status)
            for text in (title, status):
                label = QLabel(text, row)
                label.setTextFormat(Qt.PlainText)
                label.setWordWrap(True)
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
                label.setStyleSheet('background:transparent;border:0;color:' + STATES[state][0] + ';font-size:12px;')
                content.addWidget(label)
            row.clicked.connect(lambda _checked=False, target=key: self.open_session(target))
            card_layout.addWidget(row, 1)
            read = QToolButton(card)
            read.setObjectName('SessionReadButton')
            read.setFixedSize(28, 28)
            read.setCursor(Qt.PointingHandCursor)
            set_text(read, 'ui.session_updates.mark_read', setter='setToolTip')
            set_icon(read, 'eye', size=18)
            read.clicked.connect(lambda _checked=False, target=key: self.anchor.mark_read([target]))
            card_layout.addWidget(read, 0, Qt.AlignVCenter)
            self.rows.addWidget(card)
        self.rows.addStretch(1)
        bar.setValue(position)

    def open_session(self, key):
        self.anchor.hide_popup()
        event_bus.session_selected.emit(*key)
        event_bus.switch_view_tab_requested.emit(0)

    def place(self):
        available = self.anchor.screen().availableGeometry().adjusted(8, 8, -8, -8)
        # Both popups use the same measured size, including screen limits.
        self.setFixedSize(self.anchor.reference.popup_size())
        point = self.anchor.mapToGlobal(QPoint(self.anchor.width() // 2, self.anchor.height() + 6))
        x = max(available.left(), min(point.x() - self.width() // 2, available.right() - self.width() + 1))
        y = max(available.top(), min(point.y(), available.bottom() - self.height() + 1))
        self.move(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.setBrush(theme_manager.color('background.surface'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)

    def enterEvent(self, event):
        self.anchor._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.anchor._hide_timer.start()
        super().leaveEvent(event)


class SessionUpdatesLabel(QLabel):
    def __init__(self, reference, parent=None, *, status_source=None):
        super().__init__(parent)
        self.reference = reference
        self.source = status_source if status_source is not None else generation_status
        self.updates = {key: value for key, value in self.source.states.items()
                        if key not in self.source.seen and key not in self.source.acknowledged and value[0] in STATES}
        self.view_index = self.source.view_index
        self._popup = None
        self._state_color = None
        self._text_rect = QRect()
        self.setObjectName('TopBarSessionUpdates')
        self.setAlignment(Qt.AlignCenter)
        self.setTextFormat(Qt.PlainText)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self.setMinimumWidth(0)
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(250)
        self._hover_timer.timeout.connect(self.show_popup)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(180)
        self._hide_timer.timeout.connect(self._hide_if_outside)
        self.source.changed.connect(self._changed)
        app_state.session_changed.connect(self._viewed)
        app_state.titles_changed.connect(self.render)
        event_bus.workspace_view_changed.connect(self._select_view)
        event_bus.session_renamed.connect(self._rename_session)
        event_bus.branch_renamed.connect(self._rename_branch)
        i18n.language_changed.connect(self.render)
        theme_manager.theme_changed.connect(self.render)
        QApplication.instance().installEventFilter(self)
        self._viewed()

    def mark_read(self, keys):
        self.source.acknowledge(keys)
        for key in keys:
            self.updates.pop(key, None)
        self.render()

    def pending(self):
        return sorted(self.updates.items(), key=lambda item: item[1][1], reverse=True)

    def _changed(self, session, branch, state):
        key = (session, branch or 'main')
        if state in STATES and branch:
            self.updates[key] = self.source.states.get(key, (state, self.source.sequence))
        elif not state:
            self.updates.pop(key, None)
        self._viewed()

    def _viewed(self, *_):
        if self.view_index == 0:
            self.updates.pop((app_state.session_id or '', app_state.branch or 'main'), None)
        self.render()

    def _select_view(self, index):
        self.view_index = index
        self._viewed()

    def _rename_session(self, old, new):
        self.updates = {(new if sid == old else sid, branch): value
                        for (sid, branch), value in self.updates.items()}
        self._viewed()

    def _rename_branch(self, session, old, new):
        key = (session, old)
        if key in self.updates:
            self.updates[(session, new)] = self.updates.pop(key)
        self._viewed()

    def render(self, *_):
        pending = self.pending()
        if pending:
            key, (state, _order) = pending[0]
            full = app_state.display_session(*key) + ' · ' + tr(STATUS_KEYS[state])
            self.setAccessibleName(full)
            color = STATES[state][0]
            if color != self._state_color:
                self._state_color = color
                self.setStyleSheet('#TopBarSessionUpdates {background:transparent;border:0;color:' + color + ';font-size:12px;}')
            metrics = self.fontMetrics()
            available = max(0, self.width() - 10)
            suffix = ' · ' + tr(STATUS_KEYS[state])
            title_width = available - metrics.horizontalAdvance(suffix)
            text = (metrics.elidedText(app_state.display_session(*key), Qt.ElideRight, title_width) + suffix
                    if title_width >= 20 else metrics.elidedText(tr(STATUS_KEYS[state]), Qt.ElideRight, available))
            self.setText(text)
            width = min(available, metrics.horizontalAdvance(text))
            group_width = width
            left = max(0, (self.width() - group_width) // 2)
            self._text_rect = QRect(left, (self.height() - metrics.height()) // 2, width, metrics.height())
            self.update()
        else:
            self.clear()
            self.setAccessibleName('')
            self._text_rect = QRect()
            self.setCursor(Qt.ArrowCursor)
            self.hide_popup()
        if self._popup is not None and self._popup.isVisible():
            self._popup.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render()

    def paintEvent(self, event):
        if self.text():
            painter = QPainter(self)
            painter.setFont(self.font())
            painter.setPen(QColor(self._state_color))
            painter.drawText(self._text_rect, Qt.AlignCenter, self.text())

    def _update_text_hover(self, point):
        self.setCursor(Qt.PointingHandCursor if self.updates and self._text_rect.contains(point) else Qt.ArrowCursor)
        if self.updates and self._text_rect.contains(point):
            self._hide_timer.stop()
            if (self._popup is None or not self._popup.isVisible()) and not self._hover_timer.isActive():
                self._hover_timer.start()
        else:
            self._hover_timer.stop()
            if self._popup is not None and self._popup.isVisible():
                self._hide_timer.start()

    def enterEvent(self, event):
        self._update_text_hover(event.position().toPoint())
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        self._update_text_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_timer.stop()
        self._hide_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.updates and self._text_rect.contains(event.position().toPoint()):
            self.show_popup()
            event.accept()
            return
        super().mousePressEvent(event)

    def show_popup(self):
        self._hover_timer.stop()
        if not self.isVisible() or not self.updates or not self._text_rect.contains(self.mapFromGlobal(QCursor.pos())):
            return
        if self._popup is None:
            self._popup = SessionUpdatesPopup(self)
        self._popup.refresh()
        self._popup.place()
        self._popup.show()
        self._popup.raise_()

    def hide_popup(self):
        self._hover_timer.stop()
        self._hide_timer.stop()
        if self._popup is not None:
            self._popup.hide()

    def _hide_if_outside(self):
        grabber = QWidget.mouseGrabber()
        if self._popup is not None and grabber is not None and (grabber is self._popup or self._popup.isAncestorOf(grabber)):
            return
        position = QCursor.pos()
        if self._text_rect.contains(self.mapFromGlobal(position)):
            return
        if self._popup is not None and self._popup.isVisible() and self._popup.rect().contains(self._popup.mapFromGlobal(position)):
            return
        self.hide_popup()

    def _hide_if_inactive(self):
        if self._popup is None or not isValid(self._popup) or not self._popup.isVisible():
            return
        # Activation may move from the main window to this panel on a click.
        active = QApplication.activeWindow()
        if (QApplication.applicationState() != Qt.ApplicationActive or
                (active is not None and active is not self.window() and active is not self._popup
                 and not self._popup.isAncestorOf(active))):
            self.hide_popup()

    def hideEvent(self, event):
        self.hide_popup()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if self._popup is not None and isValid(self._popup) and self._popup.isVisible():
            kind = event.type()
            if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.hide_popup()
                return True
            if watched is self.window() and kind in (QEvent.Move, QEvent.Resize, QEvent.Hide, QEvent.Close):
                self.hide_popup()
            elif (kind == QEvent.ApplicationDeactivate or
                    (kind == QEvent.WindowDeactivate and (watched is self.window() or watched is self._popup))):
                QTimer.singleShot(0, self._hide_if_inactive)
            elif kind == QEvent.MouseButtonPress:
                position = QCursor.pos()
                if not self.rect().contains(self.mapFromGlobal(position)) and not self._popup.rect().contains(self._popup.mapFromGlobal(position)):
                    self.hide_popup()
        return super().eventFilter(watched, event)
