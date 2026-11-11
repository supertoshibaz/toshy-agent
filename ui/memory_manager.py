"""Searchable memory records with an explicit, cancellable deletion countdown."""
import time
from urllib.parse import quote
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QHBoxLayout, QTabWidget, QScrollArea, QLineEdit, QPlainTextEdit, QSizePolicy)
from shiboken6 import isValid
from ui.i18n import tr, bind_caption, set_text
from ui.icons import set_icon
from ui.styles.binding import apply_style, form_field
from ui.widgets.overlay_panel import OverlayPanel
from ui.core_bridge import core_process
from ui.core_bridge.async_runner import AsyncRunner


class MemoryText(QPlainTextEdit):
    """Grow to wrapped text, then scroll at the former 220px limit."""
    def __init__(self, parent):
        super().__init__(parent)
        self.document().contentsChanged.connect(self.fit_text)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fitting = False

    def fit_text(self):
        if self._fitting:
            return
        self._fitting = True
        try:
            document = self.document()
            width = max(1, self.viewport().width())
            document.setTextWidth(width)
            lines = 0
            block = document.begin()
            while block.isValid():
                # Force wrapped line layout before reading its line count.
                self.blockBoundingRect(block)
                lines += max(1, block.layout().lineCount())
                block = block.next()
            extra = int(document.documentMargin()*2) + self.frameWidth()*2 + 8
            height = min(220, max(44, lines*self.fontMetrics().lineSpacing()+extra))
            if self.height() != height:
                self.setFixedHeight(height)
        finally:
            self._fitting = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_text()


class DeletionShade(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.progress = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(self.rect()), 6, 6)
        painter.setClipPath(outline)
        width = self.width() * self.progress
        painter.fillRect(QRectF(0, 0, width, self.height()), QColor(0, 0, 0, 65))
        painter.end()


class MemorySummaryLabel(QLabel):
    MAX_CHARACTERS = 64

    def __init__(self, text, parent):
        super().__init__(parent)
        compact = ' '.join(str(text).split())
        self.summary = compact[:self.MAX_CHARACTERS].rstrip() + ('…' if len(compact) > self.MAX_CHARACTERS else '')
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setIndent(0)
        self.setMargin(0)
        self.setContentsMargins(0, 0, 12, 0)
        self.update_summary()

    def update_summary(self):
        width = max(0, self.contentsRect().width())
        self.setText(self.fontMetrics().elidedText(self.summary, Qt.ElideRight, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_summary()


class MemoryRecord(QFrame):
    def __init__(self, record, page):
        super().__init__(page.host)
        self.record, self.page = record, page
        self.started = None
        self.setObjectName('MemoryRecord')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        apply_style(self, '''QFrame#MemoryRecord {background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;}
            QToolButton {border:0;background:transparent;border-radius:4px;padding:0;}
            QToolButton:hover {background:@control.hover@;}
            QLabel {border:0;background:transparent;}''')
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 6, 6, 6)
        row = QHBoxLayout()
        self.toggle = QToolButton(self)
        self.toggle.setCheckable(True)
        set_icon(self.toggle, 'right', square=True, size=16)
        self.toggle.setFixedSize(28, 28)
        self.toggle.toggled.connect(self.expand)
        row.addWidget(self.toggle)
        text = str(record.get('text') or '')
        title = str(record.get('source') or text.split('\n', 1)[0] or record['id'])
        self.title = MemorySummaryLabel(title, self)
        row.addWidget(self.title, 1)
        self.remove = QToolButton(self)
        set_icon(self.remove, 'close', square=True, size=16)
        self.remove.setFixedSize(28, 28)
        set_text(self.remove, 'ui.memory_manager.delete', setter='setToolTip')
        self.remove.clicked.connect(self.toggle_delete)
        row.addWidget(self.remove)
        box.addLayout(row)
        self.body = None
        self.shade = DeletionShade(self)
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.tick)
        self.search_text = (title + '\n' + text + '\n' + record['id']).casefold()

    def expand(self, expanded):
        set_icon(self.toggle, 'down' if expanded else 'right', square=True, size=16)
        if expanded and self.body is None:
            self.body = form_field(MemoryText(self))
            self.body.setReadOnly(True)
            self.body.setPlainText(str(self.record.get('timestamp') or '') + '\n'
                                   + self.record['id'] + '\n\n' + str(self.record.get('text') or ''))
            self.body.fit_text()
            self.layout().addWidget(self.body)
        if self.body is not None:
            self.body.setVisible(expanded)
        self.shade.raise_()
        self.remove.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'shade'):
            self.shade.setGeometry(self.rect())

    def toggle_delete(self):
        if self.started is not None:
            self.cancel_delete()
        else:
            self.started = time.monotonic()
            set_icon(self.remove, 'stop', square=True, size=16)
            self.shade.progress = 0
            self.shade.setGeometry(self.rect())
            self.shade.show()
            self.shade.raise_()
            self.remove.raise_()
            self.timer.start()

    def cancel_delete(self):
        self.started = None
        self.timer.stop()
        set_icon(self.remove, 'close', square=True, size=16)
        self.shade.hide()

    def tick(self):
        self.shade.progress = min(1, (time.monotonic()-self.started)/2)
        self.shade.update()
        if self.shade.progress >= 1:
            self.timer.stop()
            self.remove.setEnabled(False)
            self.page.delete(self)


class MemoryPage(QWidget):
    def __init__(self, panel, kind):
        super().__init__(panel.frame)
        self.panel, self.kind = panel, kind
        self.cards = []
        self.loaded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.search = QLineEdit(self)
        set_text(self.search, 'ui.memory_manager.search', setter='setPlaceholderText')
        self.search.textChanged.connect(self.filter)
        layout.addWidget(self.search)
        if kind == 'temporary' and panel.session:
            session_label = QLabel(tr('ui.memory_manager.temporary', session=panel.session), self)
            session_label.setTextFormat(Qt.PlainText)
            session_label.setWordWrap(True)
            layout.addWidget(session_label)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.records_panel = QFrame(self)
        self.records_panel.setObjectName('MemoryRecordsPanel')
        apply_style(self.records_panel, 'QFrame#MemoryRecordsPanel {background:@background.input@;border:1px solid @border.subtle@;border-radius:6px;}')
        records_layout = QVBoxLayout(self.records_panel)
        records_layout.setContentsMargins(5, 5, 5, 5)
        scroll = QScrollArea(self.records_panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.host = QWidget(scroll)
        self.rows = QVBoxLayout(self.host)
        self.rows.setContentsMargins(2, 2, 2, 2)
        self.rows.setSpacing(6)
        self.rows.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.host)
        records_layout.addWidget(scroll)
        layout.addWidget(self.records_panel, 1)

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        if self.kind == 'temporary' and not self.panel.session:
            set_text(self.status, 'ui.memory_manager.no_session')
            return
        set_text(self.status, 'ui.memory_manager.loading')
        self.panel.runner.submit(lambda: self.panel.request('GET', self.kind), self.render,
            lambda error: set_text(self.status, 'ui.memory_manager.error', error=str(error)))

    def render(self, result):
        for record in result['records']:
            card = MemoryRecord(record, self)
            self.rows.addWidget(card)
            self.cards.append(card)
        self.filter()

    def filter(self, *_):
        query = self.search.text().strip().casefold()
        visible = 0
        for card in self.cards:
            matches = query in card.search_text
            card.setVisible(matches)
            visible += matches
        self.status.setText('' if visible else tr('ui.memory_manager.empty'))

    def delete(self, card):
        identity = card.record['id']
        def done(_):
            if card in self.cards:
                self.cards.remove(card)
                self.rows.removeWidget(card)
                card.hide()
                card.deleteLater()
                self.filter()
        def failed(error):
            card.cancel_delete()
            card.remove.setEnabled(True)
            set_text(self.status, 'ui.memory_manager.delete_error', error=str(error))
        self.panel.runner.submit(lambda: self.panel.request('DELETE', self.kind, identity), done, failed)


class MemoryManagerPanel(OverlayPanel):
    def __init__(self, chat):
        self.session = chat.current_session_id
        self.client = chat.session_manager
        super().__init__(chat.window(), tr('ui.memory_manager.title'), width=850, height=650)
        self.runner = AsyncRunner(self, max_threads=1)
        self.tabs = QTabWidget(self.frame)
        self.tabs.setObjectName('MemoryTabs')
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setElideMode(Qt.ElideRight)
        apply_style(self.tabs, 'QTabWidget#MemoryTabs > QTabBar::tab {min-width:140px;max-width:10000px;padding:6px 20px;}')
        self.pages = [MemoryPage(self, kind) for kind in ('permanent', 'temporary')]
        self.tabs.addTab(self.pages[0], tr('ui.memory_manager.permanent'))
        self.tabs.addTab(self.pages[1], tr('ui.memory_manager.temporary_tab'))
        self.tabs.currentChanged.connect(lambda index: self.pages[index].load())
        self.content.addWidget(self.tabs, 1)
        close = QPushButton(tr('ui.memory_manager.close'), self.frame)
        close.clicked.connect(self.dismiss)
        self.content.addWidget(close, 0, Qt.AlignRight)
        self.pages[0].load()

    def request(self, method, kind, identity=None):
        if core_process.is_running():
            path = '/v1/memory/records' + ('/' + quote(identity, safe='') if identity else '')
            return self.client._request(method, path,
                params={'kind': kind, 'session_id': self.session or ''}, timeout=120)
        from core.memory_records import list_records, delete_record
        return (list_records(kind, self.session) if method == 'GET'
                else delete_record(kind, identity, self.session))

    def dismiss(self):
        for page in self.pages:
            for card in page.cards:
                card.cancel_delete()
        self.runner.shutdown()
        super().dismiss()


def open_memory_manager(chat):
    window = chat.window()
    panel = getattr(window, '_memory_manager_panel', None)
    if panel is not None and isValid(panel) and panel.isVisible():
        panel.raise_()
        return
    window._memory_manager_panel = MemoryManagerPanel(chat)
