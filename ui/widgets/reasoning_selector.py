"""Режим ответа и effort текущей сессии; возможности проверяются без генерации."""
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.icons import bind_popup
from ui.styles.binding import apply_style
from ui.views.chat_input_controls import ChatIconButton

from PySide6.QtWidgets import QMenu
from core.reasoning_effort import normalize_levels
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.transport import core_http_client
from core.security import auth_headers, load_or_create_core_token
from ui.event_bus import event_bus


class ReasoningSelector(ChatIconButton):
    def __init__(self, chat):
        super().__init__('adjustments', tr('ui.reasoning_selector.reasoning_mode_5b12'), chat.input_card)
        self.chat, self.value, self.levels = chat, '', []
        self.non_reasoning = False
        self._hint = tr('ui.caption.4cdcb81e6f85')
        self.setObjectName('ChatInputAuxBtn')
        self.runner = AsyncRunner(self, max_threads=1)
        self._cap_profile = None
        self._generation = 0
        self.pending = False
        self.clicked.connect(self.open_menu)
        chat.model_selector.display_changed.connect(self.refresh)
        event_bus.model_profiles_changed.connect(self.invalidate)
        core_process.status_changed.connect(self.invalidate)
        from ui.qt_deferred import defer
        defer(0, self, self.refresh)
        self._label()

    def restore(self, value):
        self.value = str(value or '')
        self._label()
        self.refresh()

    def _available(self, value):
        if self._cap_profile != self.chat.model_selector.resolved_profile_id():
            return False
        return self.non_reasoning if value == 'none' else value in self.levels

    def effective_value(self):
        return self.value if self._available(self.value) else ''

    def _label(self):
        value = self.effective_value()
        selected = tr('ui.caption.2fa4370c998c') if value == 'none' else value or tr('ui.caption.fe7ef244bbd0')
        text = tr('ui.caption.fddddf975b43') + selected + tr('ui.caption.656059a47273')
        if self._hint:
            text += '\n' + self._hint
        bind_caption(self, tr('ui.caption.fddddf975b43') + selected, setter='setAccessibleDescription')
        bind_caption(self, text, setter='setToolTip')
        self.set_active(bool(value))
        menu = getattr(self, '_menu', None)
        if menu is not None:
            for action in menu.actions():
                value = action.data()
                if value is None:
                    continue
                action.setEnabled(value == '' or self._available(value))
                action.setChecked(self.value == value)

    def _clear_capabilities(self, hint):
        self._cap_profile = None
        self.levels = []
        self.non_reasoning = False
        self._hint = hint
        self._label()
        self._refresh_menu()

    def _refresh_menu(self):
        menu = getattr(self, '_menu', None)
        if menu is not None:
            self._populate_menu(menu)

    def invalidate(self, *_):
        self._generation += 1
        self._clear_capabilities(tr('ui.caption.4cdcb81e6f85'))
        self.refresh()

    def refresh(self, *_):
        from shiboken6 import isValid
        if not isValid(self): return
        # Общая кнопка доступна всегда; блокируются только пункты меню.
        if not core_process.is_running():
            self._clear_capabilities(tr('ui.caption.164d76e732a7'))
            return
        profile = self.chat.model_selector.resolved_profile_id()
        if profile != self._cap_profile:
            self._clear_capabilities(tr('ui.caption.4cdcb81e6f85'))
        if self.pending:
            return
        self.pending = True
        generation = self._generation
        def request():
            with core_http_client(core_process.base_url, timeout=5) as client:
                response = client.post(core_process.base_url + '/v1/model/check',
                    json={'operation': 'reasoning', 'connection_id': profile},
                    headers=auth_headers(load_or_create_core_token()))
                response.raise_for_status()
                return response.json()
        def done(result):
            self.pending = False
            if generation != self._generation or profile != self.chat.model_selector.resolved_profile_id():
                self.refresh()
                return
            if not core_process.is_running():
                self.refresh()
                return
            self._cap_profile = profile
            self.levels = normalize_levels(result.get('levels'))
            self.non_reasoning = result.get('non_reasoning') is True or 'none' in self.levels
            self._hint = '' if self.levels or self.non_reasoning else result.get('hint', tr('ui.caption.2a005a12ff84'))
            self._label()
            self._refresh_menu()
        def failed(error):
            self.pending = False
            if generation != self._generation or profile != self.chat.model_selector.resolved_profile_id():
                self.refresh()
                return
            self._clear_capabilities(tr('ui.caption.aa8cd30c9b12') + str(error))
        self.runner.submit(request, done, failed)

    def open_menu(self):
        self.refresh()
        from ui.views.chat_input_controls import popup_above
        previous = getattr(self, '_menu', None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        menu = bind_popup(QMenu(self), self)
        menu.setToolTipsVisible(True)
        apply_style(menu, 'QMenu::item:disabled {color:@text.disabled@;}')
        self._populate_menu(menu)
        self._menu = menu
        popup_above(menu, self)

    def _populate_menu(self, menu):
        menu.clear()
        entries = [(tr('ui.api_settings.default_fd05'), ''), (tr('ui.caption.2fa4370c998c'), 'none')]
        entries += [(level, level) for level in self.levels if level != 'none']
        # Сохранённый режим другой модели виден, но не отправляется без поддержки.
        if self.value and self.value != 'none' and self.value not in self.levels:
            entries.append((self.value + tr('ui.caption.7cbe50f0cb3c'), self.value))
        for index, (label, level) in enumerate(entries):
            if index == 2:
                menu.addSeparator()
            action = add_caption_action(menu, 'addAction', label.replace('&', '&&'))
            action.setData(level)
            action.setCheckable(True)
            action.setChecked(self.value == level)
            available = level == '' or self._available(level)
            action.setEnabled(available)
            bind_caption(action, tr('ui.caption.32b8e6061428') if available else
                              tr('ui.caption.afffd2454b8d'), setter='setToolTip')
            action.triggered.connect(lambda checked=False, value=level: self.choose(value))
        if not any(level != 'none' for level in self.levels):
            menu.addSeparator()
            notice = localize(add_caption_action(menu, 'addAction', 'Уровни effort не определены'), 'ui.reasoning_selector.effort_levels_are_unavailable_080f')
            notice.setEnabled(False)
        menu.adjustSize()

    def choose(self, value):
        if value and not self._available(value):
            return
        self.value = value
        self._label()
        chat = self.chat
        if hasattr(chat, 'input_state'):
            chat.input_state.changed()
