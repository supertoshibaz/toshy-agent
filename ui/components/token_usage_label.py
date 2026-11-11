"""Compact usage indicator for the selected chat session."""
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr, localize, set_text, i18n

from html import escape

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStyle, QVBoxLayout, QWidget
from shiboken6 import isValid

from core.token_usage import read_usage, studio_id
from ui.app_state import app_state
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.session_client import SessionApiClient
from ui.event_bus import event_bus


def compression_caption(data):
    message = str(data.get('message') or '')
    # Legacy core snapshots contain Russian progress captions. Translate them
    # at the display boundary; keep unknown provider details verbatim.
    captions = (
        ('Сжатие начнётся после завершения генерации.', 'queued'),
        ('Подготовка сжатия…', 'preparing'),
        ('Сжатие контекста…', 'running'),
        ('Сжатие отменено; история сохранена.', 'cancelled'),
        ('Контекст сжат; полная история сохранена.', 'completed'),
        ('Сжатие было прервано вместе с ядром.', 'interrupted'),
        ('Сжимать пока нечего: первый запрос и последние задания сохраняются целиком. Продолжите диалог ещё на один обмен или уменьшите число сохраняемых заданий в Startup → Core → Context.', 'unchanged'),
        ('Сжатие не выполнено; прежний контекст сохранён. ', 'error_previous'),
        ('Сжатие не выполнено; история сохранена. ', 'error'),
        ('Нет материала для резюме.', 'no_material'),
        ('Модель не вернула завершённое текстовое резюме.', 'no_summary'),
        ('Резюме увеличило контекст вместо сжатия.', 'increased'),
        ('Сжатие не освободило место. Продолжите диалог, уменьшите число сохраняемых заданий в Startup → Core → Context или выберите другую модель резюмирования.', 'no_space'),
    )
    def translated(text):
        for source, key in captions:
            if text.startswith(source):
                return tr('ui.compression.' + key) + translated(text[len(source):])
        return text
    if message:
        return translated(message)
    return tr('ui.compression.' + data['status']) if data.get('status') in {'queued', 'running'} else ''


def number(value):
    return '—' if value is None else f'{int(value):,}'.replace(',', '\u202f')


class _TokenUsagePopup(QFrame):
    def __init__(self, anchor):
        super().__init__(anchor, Qt.ToolTip | Qt.FramelessWindowHint)
        self.anchor = anchor
        self._data = {}
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.timeout.connect(self.place)
        theme_manager.theme_changed.connect(self._refresh_theme)
        i18n.language_changed.connect(self._refresh_theme)
        watch_paint(self)
        self.setObjectName('TokenUsagePopup')
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_StyledBackground)
        apply_style(self, '\n            QFrame#TokenUsagePopup {background:transparent;border:none;}\n            QLabel {background:transparent;border:none;color:@text.primary@;font-size:12px;}\n            QLabel#TokenUsagePopupTitle {font-size:14px;font-weight:600;}\n            QLabel#TokenUsagePopupCaption {color:@text.secondary@;font-size:11px;}\n            QFrame#TokenUsagePopupDivider {background:@background.surface@;border:none;}\n            QPushButton {background:@background.surface@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:5px;padding:6px 10px;}\n            QPushButton:hover {background:@control.hover@;}\n            QPushButton:disabled {color:@text.disabled@;background:@background.surface@;border-color:@border.subtle@;}\n            QScrollArea, QScrollArea > QWidget > QWidget {background:transparent;border:none;}\n            QScrollBar:vertical {background:transparent;width:6px;margin:0;}\n            QScrollBar::handle:vertical {background:@text.disabled@;border-radius:3px;min-height:20px;}\n            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0;}\n            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:transparent;}\n        ')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(self.scroll)
        self.body = QWidget(self.scroll)
        self.scroll.setWidget(self.body)
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = self._label(tr('ui.context_settings.context_a6e6'), name='TokenUsagePopupTitle')
        self.context_total = self._label('', right=True)
        header.addWidget(title)
        header.addWidget(self.context_total, 1)
        layout.addLayout(header)

        context_grid = QGridLayout()
        context_grid.setContentsMargins(0, 0, 0, 0)
        context_grid.setHorizontalSpacing(12)
        context_grid.setVerticalSpacing(3)
        self.context_cells = {}
        for row, (key, text) in enumerate((('input', tr('ui.caption.9acdbd9a812d')), ('output', tr('ui.caption.b1da04be3d6e')), ('total', tr('ui.caption.a06b79c885ea')))):
            context_grid.addWidget(self._label(text), row, 0)
            cell = self._label('', right=True)
            context_grid.addWidget(cell, row, 1)
            self.context_cells[key] = cell
        context_grid.setColumnStretch(0, 1)
        layout.addLayout(context_grid)
        self.pending = self._label(tr('ui.caption.08866dbeacb0'), name='TokenUsagePopupCaption')
        layout.addWidget(self.pending)
        self.compression_status = self._label('', name='TokenUsagePopupCaption')
        self.compression_status.setWordWrap(True)
        layout.addWidget(self.compression_status)
        from ui.components.context_breakdown import ContextBreakdown
        self.breakdown = ContextBreakdown(self.body)
        self.breakdown.expanded_changed.connect(self.place)
        layout.addWidget(self.breakdown)
        self.compact_button = localize(QPushButton('', self.body), 'ui.token_usage_label.compact_context_3c10')
        set_text(self.compact_button, 'ui.token_usage_label.summarize_older_conversation_history_the_complete_3662', setter='setToolTip')
        self.compact_button.clicked.connect(anchor.compact_context)
        layout.addWidget(self.compact_button)
        self._divider(layout)

        layout.addWidget(self._label(tr('ui.caption.def85ee51d16'), name='TokenUsagePopupTitle'))
        spending_grid = QGridLayout()
        spending_grid.setContentsMargins(0, 0, 0, 0)
        spending_grid.setHorizontalSpacing(12)
        spending_grid.setVerticalSpacing(4)
        for column, text in ((1, tr('ui.caption.edbf4d4f2f15')), (2, tr('ui.caption.4d61f9756368'))):
            spending_grid.addWidget(self._label(text, right=True, name='TokenUsagePopupCaption'), 0, column)
        self.spending_cells = {}
        for row, (key, text) in enumerate((('input', tr('ui.caption.777b381f2f61')), ('output', tr('ui.caption.597bfd4f0711')), ('total', tr('ui.caption.6913fa675844'))), 1):
            spending_grid.addWidget(self._label(text), row, 0)
            for column, scope in ((1, 'current'), (2, 'lifetime')):
                cell = self._label('', right=True)
                spending_grid.addWidget(cell, row, column)
                self.spending_cells[scope, key] = cell
        spending_grid.setColumnStretch(0, 1)
        layout.addLayout(spending_grid)
        self._divider(layout)

        speed_row = QHBoxLayout()
        speed_row.addWidget(self._label(tr('ui.caption.a8642a5fc3c9')))
        self.speed = self._label('', right=True)
        speed_row.addWidget(self.speed, 1)
        layout.addLayout(speed_row)
        self.model = self._label('', name='TokenUsagePopupCaption')
        self.source = self._label('', name='TokenUsagePopupCaption')
        for label in (self.model, self.source):
            label.setWordWrap(True)
            layout.addWidget(label)
        self._divider(layout)
        reset = self._label(tr('ui.caption.7495196a1d3b'), name='TokenUsagePopupCaption')
        reset.setWordWrap(True)
        layout.addWidget(reset)
        partial = self._label(tr('ui.caption.4556da381e7b'))
        apply_style(partial, 'color:@text.secondary@;font-size:11px;')
        partial.setWordWrap(True)
        layout.addWidget(partial)

    def _label(self, text, *, right=False, name=''):
        label = bind_caption(QLabel('', self.body), text)
        label.setTextFormat(Qt.PlainText)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        if name:
            label.setObjectName(name)
        if right:
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return label

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.setBrush(theme_manager.color('background.surface'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 10, 10)

    def _divider(self, layout):
        line = QFrame(self.body)
        line.setObjectName('TokenUsagePopupDivider')
        line.setFixedHeight(1)
        layout.addWidget(line)

    def _refresh_theme(self, *_):
        self.update_data(self._data)

    def update_data(self, data):
        self._data = data
        self.breakdown.update_data(data)
        context = data.get('context') or {}
        bind_caption(self.context_total, f'{number(context.get("total"))} / {number(data.get("limit"))}')
        for key, cell in self.context_cells.items():
            bind_caption(cell, number(context.get(key)))
        for (scope, key), cell in self.spending_cells.items():
            values = data.get(scope) or {}
            bind_caption(cell, number(values.get(key)))
            shade = ('#8a5900' if theme_manager.current['foundation'] == 'light' else '#eab308') if values.get(key+'_partial') and values.get(key) is not None else theme_manager.value('text.primary')
            cell.setStyleSheet(f'color:{shade};')
        speed = data.get('average_speed')
        bind_caption(self.speed, '—' if speed is None else tr('ui.caption.c71c9776d6c7', v0=speed))
        bind_caption(self.model, tr('ui.caption.40952bc27279') + str(data.get('model') or '—'))
        bind_caption(self.source, tr('ui.caption.f30d1d487948') + str(data.get('source') or '—'))
        self.pending.setVisible(bool(data.get('pending')))
        compression = data.get('compression') or {}
        running = compression.get('status') in {'queued','running'}
        bind_caption(self.compression_status, compression_caption(compression))
        self.compression_status.setVisible(bool(self.compression_status.text()))
        bind_caption(self.compact_button, tr('ui.caption.60a1b4c092db') if running and compression.get('manual') else tr('ui.token_usage_label.compact_context_3c10'))
        self.compact_button.setEnabled(core_process.is_running() and not self.anchor._compact_request
                                      and (bool(compression.get('manual')) or not running))
        if self.isVisible():
            self.place()
            self._place_timer.start(0)

    @Slot()
    def place(self):
        available = self.anchor.screen().availableGeometry().adjusted(8, 8, -8, -8)
        self.setFixedWidth(min(390, available.width()))
        margins = self.layout().contentsMargins()
        body_width = max(1,self.width()-margins.left()-margins.right()-self.scroll.verticalScrollBar().sizeHint().width())
        # Reserve scrollbar width in both states; its appearance must not rewrap labels.
        self.body.setFixedWidth(body_width)
        self.body.layout().activate()
        height=self.body.layout().totalHeightForWidth(body_width)
        if height < 0:height=self.body.layout().sizeHint().height()
        height=max(1,height)
        self.body.setFixedHeight(height)
        viewport_height=min(height,max(1,available.height()-margins.top()-margins.bottom()))
        self.scroll.setFixedHeight(viewport_height)
        self.setFixedHeight(viewport_height+margins.top()+margins.bottom())
        bottom = self.anchor.mapToGlobal(QPoint(self.anchor.width()//2, self.anchor.height()+6))
        x = max(available.left(), min(bottom.x()-self.width()//2, available.right()-self.width()+1))
        y = bottom.y()
        if y+self.height()-1 > available.bottom():
            y = self.anchor.mapToGlobal(QPoint(0, -6)).y()-self.height()
        y = max(available.top(), min(y, available.bottom()-self.height()+1))
        self.move(x, y)

    def enterEvent(self, event):
        self.anchor._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.anchor._hide_timer.start()
        super().leaveEvent(event)


class TokenUsageLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('TopBarTokenUsage')
        self.setAlignment(Qt.AlignCenter)
        self.setTextFormat(Qt.RichText)
        self.setMouseTracking(True)
        apply_style(self, '#TopBarTokenUsage {color:@text.secondary@;font-size:12px;background:transparent;}')
        set_text(self, 'ui.token_usage_label.selected_session_s_context_from_the_b1d4', setter='setAccessibleName')
        self.app_id = studio_id()
        self.data = {}
        theme_manager.theme_changed.connect(self.render)
        self._text_rect = QRect()
        self._popup = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(250)
        self._hover_timer.timeout.connect(self.show_popup)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(180)
        self._hide_timer.timeout.connect(self._hide_if_outside)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._busy = False
        self._compact_request = False
        self._revision = 0
        self._refresh_model = True
        self.runner = AsyncRunner(self)
        self.client = SessionApiClient(core_process.base_url)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        app_state.session_changed.connect(self.invalidate)
        event_bus.model_profiles_changed.connect(self.invalidate)
        event_bus.session_model_changed.connect(self.model_changed)
        core_process.status_changed.connect(self._core_status_changed)
        self.invalidate()
        self.timer.start()

    def _core_status_changed(self, *_):
        self._refresh_model = True
        self.refresh()

    def model_changed(self, session_id):
        if session_id == app_state.session_id:
            self.invalidate()

    def invalidate(self, *_):
        self._revision += 1
        self._refresh_model = True
        self.data = {}
        self.render()
        self.refresh()

    def refresh(self):
        if self._busy or not app_state.session_id:
            return
        session, branch = app_state.session_id, app_state.branch
        revision = self._revision
        online = core_process.is_running()
        client, app_id = self.client, self.app_id
        refresh_model = self._refresh_model
        self._refresh_model = False
        self._busy = True

        def read():
            if online:
                return client.token_usage(session, branch, app_id, refresh_model=refresh_model)
            from core.session_manager import SessionManager
            store = SessionManager().storage(session)
            result = read_usage(store, app_id, branch)
            from core.context_archive import get_status
            from core.context_settings import load_settings
            result['compression'] = get_status(store,branch)
            result['limit'] = result.get('limit') or load_settings()['context_limit'] or None
            return result

        def ready(data):
            if not isValid(self):
                return
            self._busy = False
            if revision == self._revision and session == app_state.session_id and branch == app_state.branch:
                self.data = data
                event_bus.context_usage_changed.emit(session, branch, data)
                self.render()
            else:
                self.refresh()

        def failed(error):
            if not isValid(self):
                return
            self._busy = False
            # A slow/unreachable core does not imply that token usage was lost.
            if revision != self._revision:
                self.refresh()

        self.runner.submit(read, ready, failed)

    def compact_context(self):
        if self._compact_request or not app_state.session_id or not core_process.is_running():
            return
        session,branch=app_state.session_id,app_state.branch
        compression=self.data.get('compression') or {}
        cancel=bool(compression.get('manual') and compression.get('status') in {'queued','running'})
        self._compact_request=True
        self.render()
        def ready(result):
            if not isValid(self):return
            self._compact_request=False
            if (session,branch)==(app_state.session_id,app_state.branch):
                self.data['compression']=dict(status='cancelled' if cancel else 'queued' if result.get('queued') else 'running',manual=not cancel,
                    message=tr('ui.caption.1ffeb5d8abc3') if cancel else tr('ui.caption.82f833a7e612') if result.get('queued') else tr('ui.caption.78bc3b564c5d'))
                self.render();self.refresh()
        def failed(error):
            if not isValid(self):return
            self._compact_request=False
            if (session,branch)==(app_state.session_id,app_state.branch):
                self.data['compression']=dict(status='error',message=str(error))
                self.render()
        self.runner.submit(lambda:self.client.compact_context(session,branch,cancel=cancel),ready,failed)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.render()

    def enterEvent(self, event):
        self._update_text_hover(event.position().toPoint())
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        self._update_text_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def _update_text_hover(self, position):
        if self._text_rect.contains(position):
            self._hide_timer.stop()
            if (self._popup is None or not self._popup.isVisible()) and not self._hover_timer.isActive():
                self._hover_timer.start()
        else:
            self._hover_timer.stop()
            if self._popup is not None and self._popup.isVisible() and not self._hide_timer.isActive():
                self._hide_timer.start()

    def leaveEvent(self, event):
        self._hover_timer.stop()
        self._hide_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._text_rect.contains(event.position().toPoint()):
            self.show_popup()
            event.accept()
            return
        super().mousePressEvent(event)

    def hideEvent(self, event):
        self.hide_popup()
        super().hideEvent(event)

    def popup_size(self):
        if self._popup is None:
            self._popup = _TokenUsagePopup(self)
        self._popup.update_data(self.data)
        self._popup.place()
        return self._popup.size()

    def show_popup(self):
        self._hover_timer.stop()
        if not self.isVisible() or not self._text_rect.contains(self.mapFromGlobal(QCursor.pos())):
            return
        self._refresh_model = True
        self.refresh()
        self.popup_size()
        self._popup.show()
        self._popup.raise_()

    def hide_popup(self):
        self._hover_timer.stop()
        self._hide_timer.stop()
        if self._popup is not None:
            self._popup.hide()

    def _hide_if_outside(self):
        position = QCursor.pos()
        if self._text_rect.contains(self.mapFromGlobal(position)):
            return
        if self._popup is not None and self._popup.isVisible() and self._popup.rect().contains(self._popup.mapFromGlobal(position)):
            return
        self.hide_popup()

    def eventFilter(self, watched, event):
        if self._popup is not None and isValid(self._popup) and self._popup.isVisible():
            kind = event.type()
            if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.hide_popup()
                return True
            if watched is self.window() and kind in (QEvent.Move, QEvent.Resize, QEvent.Hide, QEvent.Close, QEvent.WindowDeactivate):
                self.hide_popup()
            elif kind == QEvent.ApplicationDeactivate:
                self.hide_popup()
            elif kind == QEvent.MouseButtonPress:
                self._hide_if_outside()
        return super().eventFilter(watched, event)

    def render(self):
        data = self.data
        context = data.get('context') or {}
        tokens, maximum = context.get('total'), data.get('limit')
        total, limit = number(tokens), number(maximum)
        if (data.get('compression') or {}).get('status') in {'queued','running'}:
            total = '↻ '+total
        color = '#ef4444' if tokens is not None and maximum is not None and tokens > maximum else theme_manager.value('text.secondary')
        metrics = self.fontMetrics()
        available = max(0, self.width()-4)
        limit_text = metrics.elidedText(limit, Qt.ElideMiddle, max(20, available//2))
        total_text = metrics.elidedText(total, Qt.ElideMiddle,
            max(0, available-metrics.horizontalAdvance(' / '+limit_text)))
        self._text_rect = QStyle.alignedRect(self.layoutDirection(), self.alignment(),
            QSize(metrics.horizontalAdvance(total_text+' / '+limit_text), metrics.height()), self.contentsRect())
        bind_caption(self, f'<span style="color:{color}">{escape(total_text)} / {escape(limit_text)}</span>')
        bind_caption(self, tr('ui.caption.87bee28d2d05', v0=total, v1=limit), setter='setAccessibleDescription')
        if self._popup is not None:
            self._popup.update_data(data)
