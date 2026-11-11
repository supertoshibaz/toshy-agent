"""Small log pages indexed off the GUI thread, with uniform colour labels."""
from ui.styles.binding import apply_style, form_field
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr, set_text

import json
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, QAbstractListModel, QModelIndex, QSize
from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QListView, QToolButton, QLabel, QSpinBox, QStyledItemDelegate, QPlainTextEdit, QStyle)
from ui.widgets.dialogs import ToolDialog as QDialog

from core.paths import APP_PATHS
from core.log_events import CATEGORIES
from .log_index import LogIndex

TAG_WIDTH = 120
TAG_HEIGHT = 26
_closing_futures=set()


def _track_close(future):
    _closing_futures.add(future)
    future.add_done_callback(_closing_futures.discard)
    return future


def wait_log_workers(milliseconds):
    from concurrent.futures import wait
    _,pending=wait(list(_closing_futures),timeout=max(0,milliseconds)/1000)
    _closing_futures.intersection_update(pending)
    return not pending


class EventModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.events = []
        self.lines = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.events)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid(): return None
        if role == Qt.UserRole: return self.events[index.row()]
        if role in (Qt.DisplayRole, Qt.ToolTipRole): return self.lines[index.row()]

    def replace(self, events):
        self.beginResetModel()
        self.events = events
        self.lines = []
        for event in events:
            scope = ' '.join(f'{key}={event[key]}' for key in ('session', 'branch', 'run') if event.get(key))
            message = str(event.get('message', '')).replace('\n', ' ')
            self.lines.append(f"{event.get('time', '').replace('T', ' ')}  {event.get('level', 'INFO')}  {scope}  {event.get('tag', '')}: {message}")
        self.endResetModel()


class EventDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._font_key = None
        self._column_width = 0

    def sizeHint(self, option, index):
        return QSize(500, TAG_HEIGHT+4)

    def paint(self, painter, option, index):
        event = index.data(Qt.UserRole)
        category = event.get('category', 'app')
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, theme_manager.color('control.selected'))
        painter.setFont(option.font)
        painter.setPen(theme_manager.color('text.primary') if theme_manager.current['foundation'] == 'light' else QColor(CATEGORIES.get(category, CATEGORIES['app'])))
        fm = QFontMetrics(option.font)
        if self._font_key != option.font.key():
            self._font_key = option.font.key()
            self._column_width = max(fm.horizontalAdvance(name) for name in CATEGORIES)
        rect = option.rect.adjusted(2, 2, 0, -2); rect.setWidth(self._column_width)
        painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, category)
        message_rect = option.rect.adjusted(self._column_width+8, 0, -4, 0)
        painter.setPen(QColor('#f87171') if event.get('level') == 'ERROR' else QColor('#fbbf24') if event.get('level') == 'WARN' else theme_manager.color('text.primary'))
        painter.drawText(message_rect, Qt.AlignVCenter, fm.elidedText(index.data(), Qt.ElideRight, max(0, message_rect.width())))
        painter.restore()


class LiveLogView(QWidget):
    def __init__(self, parent=None, log_dir=None):
        super().__init__(parent)
        from ui.views.chat_view import FlowLayout
        from core.log_session import session_log_path
        self.store = LogIndex(log_dir or session_log_path())
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='toshy-log')
        self.future = None
        self.revision = 0
        self.request_revision = 0
        self.page_number = 1
        self.pages = 1
        self.follow = True
        self.categories = set(CATEGORIES)
        self.display_key = None
        self.display_revision = -1
        self.closed = False
        layout = QVBoxLayout(self); layout.setContentsMargins(8, 6, 8, 10); layout.setSpacing(6)
        controls = QWidget(self)
        flow = FlowLayout(controls, margin=0, h_spacing=5, v_spacing=5)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.buttons = {}
        for category, color in CATEGORIES.items():
            button = self._button(category, controls); button.setFont(font)
            button.setCheckable(True); button.setChecked(True)
            apply_style(button, f'QToolButton {{padding: 0; border: 1px solid @border.subtle@; border-radius: 8px; color: @text.disabled@; background: transparent;}} QToolButton:checked {{color: @text.primary@; border-color: {color}; background: @background.surface@;}}')
            button.toggled.connect(lambda enabled, name=category: self.set_category(name, enabled))
            flow.addWidget(button); self.buttons[category] = button
        layout.addWidget(controls)
        self.model = EventModel(self)
        self.list = QListView(self); self.list.setModel(self.model)
        self.list.setItemDelegate(EventDelegate(self.list)); self.list.setUniformItemSizes(True)
        self.list.setFont(font); self.list.setStyleSheet('QListView {background: transparent; border: none;}')
        self.list.doubleClicked.connect(self.show_details); layout.addWidget(self.list)
        navigation = QHBoxLayout()
        self.first_button = self._button(tr('ui.caption.b959ba6eeab0'), self)
        self.previous_button = self._button(tr('ui.caption.1a9fb1f3cf8e'), self)
        self.next_button = self._button(tr('ui.caption.fe23379de9dc'), self)
        self.latest_button = self._button(tr('ui.caption.ba95cee156de'), self)
        self.latest_button.setCheckable(True); self.latest_button.setChecked(True)
        self.page_input = QSpinBox(self); self.page_input.setMinimum(1)
        self.page_input.setFixedSize(TAG_WIDTH, TAG_HEIGHT)
        self.page_label = QLabel(self)
        for widget in (self.first_button, self.previous_button, self.page_input, self.next_button, self.latest_button): navigation.addWidget(widget)
        for widget in (self.first_button, self.previous_button, self.page_input, self.next_button, self.latest_button):
            widget.setFixedHeight(32)
        navigation.addWidget(self.page_label); navigation.addStretch(1); layout.addLayout(navigation)
        self.first_button.clicked.connect(lambda:self.set_page(1))
        self.previous_button.clicked.connect(lambda:self.set_page(self.page_number-1))
        self.next_button.clicked.connect(lambda:self.set_page(self.page_number+1))
        self.page_input.valueChanged.connect(self.set_page)
        self.latest_button.toggled.connect(self.set_follow)
        self.timer = QTimer(self); self.timer.setInterval(250); self.timer.timeout.connect(self._poll)
        executor, store = self.executor, self.store
        def dispose(*_):
            try:_track_close(executor.submit(store.close))
            except RuntimeError: pass
            executor.shutdown(wait=False)
        self.destroyed.connect(dispose)
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown, wait=self.wait_for_done)
        self._poll()

    @staticmethod
    def _button(text, parent):
        button = QToolButton(parent); bind_caption(button, text)
        button.setFixedSize(TAG_WIDTH, TAG_HEIGHT); button.setCursor(Qt.PointingHandCursor)
        return button

    def set_category(self, category, enabled):
        if enabled: self.categories.add(category)
        else: self.categories.discard(category)
        self._request()

    def set_page(self, number):
        self.page_number = max(1, min(number, self.pages))
        self.follow = False
        self.latest_button.blockSignals(True); self.latest_button.setChecked(False); self.latest_button.blockSignals(False)
        self._request()

    def set_follow(self, enabled):
        self.follow = enabled
        self._request()

    def _request(self):
        self.revision += 1
        self._poll()

    def _poll(self):
        if self.closed: return
        if self.future is not None:
            if not self.future.done(): return
            future, self.future = self.future, None
            try: result = future.result()
            except Exception as error:
                set_text(self.page_label, 'ui.live_log.could_not_read_log_v0_7ed2', v0=error)
            else:
                if self.request_revision == self.revision: self._show_page(result)
        self.request_revision = self.revision
        self.future = self.executor.submit(self.store.page, tuple(self.categories), self.page_number, self.follow)

    def _show_page(self, result):
        self.display_revision = self.request_revision
        key = (tuple(event.get('id') for event in result['events']), result['page'], tuple(sorted(self.categories)))
        if key != self.display_key:
            bar = self.list.verticalScrollBar(); old = bar.value()
            same_page = self.display_key is not None and self.display_key[1:] == key[1:]
            self.model.replace(result['events']); self.display_key = key
            if self.follow: self.list.scrollToBottom()
            elif same_page: bar.setValue(old)
            else: self.list.scrollToTop()
        self.page_number, self.pages = result['page'], result['pages']
        self.page_input.blockSignals(True); self.page_input.setMaximum(self.pages); self.page_input.setValue(self.page_number); self.page_input.blockSignals(False)
        self.first_button.setEnabled(self.page_number > 1); self.previous_button.setEnabled(self.page_number > 1)
        self.next_button.setEnabled(self.page_number < self.pages)
        bind_caption(self.page_label, tr('ui.caption.9af4aa84b966', v0=self.page_number, v1=self.pages, v2=result["total"]) + (tr('ui.caption.620a7ba281c9') if result['pending'] else ''))

    def showEvent(self, event):
        super().showEvent(event); self.timer.start(); self._poll()

    def hideEvent(self, event):
        self.timer.stop(); super().hideEvent(event)

    def show_details(self, index):
        event = index.data(Qt.UserRole)
        dialog = QDialog(self); bind_caption(dialog, f"{event.get('category')} · {event.get('tag')}", setter='setWindowTitle')
        dialog.resize(850, 550)
        layout = QVBoxLayout(dialog); text = form_field(QPlainTextEdit(dialog)); text.setReadOnly(True)
        text.setPlainText(json.dumps(event, ensure_ascii=False, indent=2)); layout.addWidget(text)
        dialog.exec()

    def toPlainText(self):
        return '\n'.join(self.model.lines)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.store.cancel.set()
        try:
            self._close_future=_track_close(self.executor.submit(self.store.close))
        except RuntimeError:
            pass
        self.executor.shutdown(wait=False)

    def wait_for_done(self,milliseconds=5000):
        from concurrent.futures import TimeoutError
        future=getattr(self,'_close_future',None)
        if future is not None:
            try:future.result(timeout=max(0,milliseconds)/1000)
            except TimeoutError:return False
        self.executor.shutdown(wait=False)
        return True
