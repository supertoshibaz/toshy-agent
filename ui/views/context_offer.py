"""Согласие на сжатие поверх остальных элементов панели ввода."""
from ui.styles.binding import apply_style
from ui.i18n import tr, localize, set_text

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton
from ui.app_state import app_state
from ui.event_bus import event_bus
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.session_client import SessionApiClient
from ui.core_bridge.process_manager import core_process
from shiboken6 import isValid


class ContextCompressionOffer(QFrame):
    def __init__(self, chat):
        super().__init__(chat.center_col)
        self.chat = chat
        self.runner = AsyncRunner(self)
        self.client = SessionApiClient(core_process.base_url)
        self.key = None
        self.ratio = 0
        self.deferred = {}
        self.reported = {}
        self.requesting = False
        self.setObjectName('ContextCompressionOffer')
        apply_style(self, '#ContextCompressionOffer {background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;}')
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 8, 8)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.accept = localize(QPushButton('', self), 'ui.context_offer.compact_a3a6')
        self.accept.clicked.connect(self.compress)
        row.addWidget(self.accept)
        decline = localize(QPushButton('', self), 'ui.context_offer.not_now_d4fe')
        decline.clicked.connect(self.dismiss)
        row.addWidget(decline)
        self.hide()
        event_bus.context_usage_changed.connect(self.usage)
        app_state.session_changed.connect(self.reset)

    def reset(self, *_):
        self.key = None
        self.hide()
        self.chat._schedule_overlay_update()

    def usage(self, session, branch, data):
        if (session, branch) != (self.chat.current_session_id, self.chat.current_branch):
            return
        key = (session, branch)
        self.key = key
        state = data.get('compression') or {}
        if state.get('status') in {'error', 'unchanged', 'cancelled'}:
            marker = (state.get('updated'), state.get('status'), state.get('message'))
            if marker != self.reported.get(key) and state.get('message'):
                self.reported[key] = marker
                self.chat._show_error_panel(state['message'])
        options = data.get('compression_offer') or {}
        tokens = (data.get('context') or {}).get('total')
        limit = data.get('limit')
        if not options.get('enabled') or tokens is None or not limit or state.get('status') in {'queued', 'running'}:
            self.hide()
        else:
            self.ratio = tokens * 100 / limit
            floor = max(options.get('threshold', 75), self.deferred.get(key, 0))
            # После реального уменьшения контекста можно вновь предложить сжатие на пороге.
            if self.ratio < options.get('threshold', 75) - 5:
                self.deferred.pop(key, None)
            if self.ratio >= floor and not self.requesting:
                suffix = tr('ui.caption.73d2d3fb7787') if data.get('pending') else ''
                set_text(self.label, 'ui.context_offer.context_is_v0_0f_full_compact_a9e1', v0=self.ratio, v1=suffix)
                self.show()
            else:
                self.hide()
        self.chat._schedule_overlay_update()

    def dismiss(self):
        if self.key:
            self.deferred[self.key] = self.ratio + 10
        self.hide()
        self.chat._schedule_overlay_update()

    def compress(self):
        if self.requesting or not self.key:
            return
        session, branch = self.key
        self.dismiss()
        self.requesting = True
        def ready(_):
            if not isValid(self):
                return
            self.requesting = False
        def failed(error):
            if not isValid(self):
                return
            self.requesting = False
            if (session, branch) == (self.chat.current_session_id, self.chat.current_branch):
                self.chat._show_error_panel(str(error))
        self.runner.submit(lambda: self.client.compact_context(session, branch), ready, failed)

    def position(self):
        if self.isHidden():
            return
        chat = self.chat
        composer = chat._composer_rect()
        y = composer.y()
        panels = [chat.attachments_panel, chat.error_panel, chat.tags_bar]
        if hasattr(chat, 'chat_references'):
            panels.append(chat.chat_references.reply_bar)
        for panel in panels:
            if panel.isVisible():
                y = min(y, panel.y())
        width = max(1, composer.width())
        self.setFixedWidth(width)
        height = max(44, self.layout().heightForWidth(width))
        self.setGeometry(composer.x(), max(0, y - height - 8), width, height)
        self.raise_()
