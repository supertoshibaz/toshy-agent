"""Lazy chat pages and application-lifetime retention of opened chat widgets."""
from PySide6.QtCore import QTimer
from ui.tab_memory import performance_preferences


class SessionHistoryMixin:
    def _init_session_history(self):
        self._session_cache = {}
        self._history_jump_target = None
        self._history_before = 0
        self._history_anchor = None
        self._history_fetching = False
        self._history_prepending = False
        self._history_positioning = False
        self._history_scroll_passes = 0
        self._history_scroll_timer = QTimer(self)
        self._history_scroll_timer.setSingleShot(True)
        self._history_scroll_timer.timeout.connect(self._settle_history_end)
        self._session_metadata = {}
        performance_preferences.changed.connect(self._history_preferences_changed)

    def _history_preferences_changed(self):
        if not performance_preferences.values['sessions']['keep_loaded']:
            for key in list(self._session_cache):
                self._drop_cached_session(key)

    def _drop_cached_session(self, key):
        entry = self._session_cache.pop(key, None)
        if entry:
            for widget in entry['widgets']:
                widget.deleteLater()

    def _stash_session(self):
        key = (self.current_session_id, self.current_branch)
        if not key[0] or self._session_loading:
            return
        if not performance_preferences.values['sessions']['keep_loaded']:
            return
        self._drop_cached_session(key)
        widgets = []
        while self.turns_layout.count() > 1:
            widget = self.turns_layout.takeAt(0).widget()
            if widget:
                widget.hide()
                widgets.append(widget)
        self._session_cache[key] = {'widgets': widgets, 'users': list(self.user_turn_widgets),
            'history': list(self.messages_history), 'metadata': dict(self._session_metadata),
            'before': self._history_before, 'anchor': self._history_anchor,
            'timeline': list(self.timeline_rail.history_markers),
            'scroll': self.scroll_area.verticalScrollBar().value()}

    def _restore_cached_session(self, entry):
        self._history_jump_target = None
        self._history_before = entry['before']
        self._history_anchor = entry['anchor']
        self._apply_session_metadata(self.current_session_id, entry['metadata'], self.current_branch)
        self.messages_history = entry['history']
        self.user_turn_widgets = entry['users']
        self.timeline_rail.set_history_markers(entry.get('timeline', []))
        for widget in entry['widgets']:
            self.turns_layout.insertWidget(self.turns_layout.count() - 1, widget)
            widget.show()
        self._auto_scroll_to_bottom = True
        self._finish_loaded_session()
        self.message_selection._turns_key = None

    def _position_history_at_end(self):
        self._history_positioning = True
        self._history_scroll_passes = 0
        self._auto_scroll_to_bottom = True
        self.turns_layout.activate()
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._history_scroll_timer.start(0)

    def _settle_history_end(self):
        if not self._history_positioning:
            return
        self.turns_layout.activate()
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._history_scroll_passes += 1
        if self._history_scroll_passes < 3:
            self._history_scroll_timer.start(16)
        else:
            self._history_positioning = False
            self._auto_scroll_to_bottom = True

    def _fetch_session_for_ui(self, session_id, branch, retain_before=None):
        options = performance_preferences.values['sessions']
        if not options['paged']:
            return self.session_manager.load_session(session_id, branch=branch)
        data = self.session_manager.load_session_page(session_id, branch=branch, limit=options['page_size'])
        # Refresh a changed/finished run without dropping pages already opened
        # in this application. Every database read still obeys the page size.
        while retain_before is not None and data.get('page', {}).get('before', 0) > retain_before:
            before = data['page']['before']
            page = self.session_manager.load_session_page(session_id, branch=branch,
                limit=min(options['page_size'], before - retain_before), before=before)
            if page['page']['before'] >= before:
                break
            data['messages'] = page['messages'] + data['messages']
            data['page']['before'] = page['page']['before']
        return data

    def _prepend_history_page(self, payload):
        old_widgets = [self.turns_layout.itemAt(i).widget() for i in range(self.turns_layout.count() - 1)]
        old_history = list(self.messages_history)
        old_users = list(self.user_turn_widgets)
        bar = self.scroll_area.verticalScrollBar()
        anchor = next((w for w in old_widgets if w and w.mapTo(self.scroll_area.viewport(), w.rect().topLeft()).y() + w.height() > 0), None)
        anchor_y = anchor.mapTo(self.scroll_area.viewport(), anchor.rect().topLeft()).y() if anchor else 0
        self._history_prepending = True
        self._auto_scroll_to_bottom = False
        try:
            self.messages_history = []
            self.user_turn_widgets = []
            for message in payload['messages']:
                self._render_saved_message(message)
            old_set = set(old_widgets)
            new_widgets = [self.turns_layout.itemAt(i).widget() for i in range(self.turns_layout.count() - 1)
                           if self.turns_layout.itemAt(i).widget() not in old_set]
            # Detach the entire new page before inserting it. Qt changes indexes
            # after each remove/insert, so iterating a moving suffix is unsafe.
            for widget in new_widgets:
                self.turns_layout.removeWidget(widget)
            for index, widget in enumerate(new_widgets):
                self.turns_layout.insertWidget(index, widget)
            if self._generation_insert_index is not None:
                self._generation_insert_index += len(new_widgets)
            self.messages_history.extend(old_history)
            self.user_turn_widgets.extend(old_users)
            self._history_before = payload.get('page', {}).get('before', 0)
            if 'timeline' in payload:
                self.timeline_rail.set_history_markers(payload['timeline'])
            self._history_anchor = (payload['messages'][0].additional_kwargs.get('chat_id') if payload['messages'] else self._history_anchor)
            self.turns_layout.invalidate()
            self.turns_layout.activate()
            if anchor:
                bar.setValue(bar.value() + anchor.mapTo(self.scroll_area.viewport(), anchor.rect().topLeft()).y() - anchor_y)
            self.timeline_rail.update_turns(self.user_turn_widgets)
            self.message_selection._turns_key = None
            self.chat_references.refresh()
        finally:
            self._history_prepending = False

    def load_older_messages(self):
        if self._history_before <= 0 or self._history_fetching or self._history_prepending or self._session_loading or not self.current_session_id:
            return
        self._cancel_history_positioning()
        self._history_fetching = True
        generation = self._session_load_generation
        session, branch, before = self.current_session_id, self.current_branch, self._history_before
        size = performance_preferences.values['sessions']['page_size']
        def received(payload):
            if generation != self._session_load_generation:
                return
            previous_before = self._history_before
            try:
                self._prepend_history_page(payload)
            finally:
                self._history_fetching = False
            if self._history_before < previous_before:
                self._continue_history_jump()
            else:
                self._history_jump_target = None
        def failed(error):
            if generation == self._session_load_generation:
                self._history_fetching = False
                self._history_jump_target = None
                self._show_error_panel('Не удалось загрузить предыдущие сообщения: ' + str(error))
        self.session_load_runner.submit(lambda: self.session_manager.load_session_page(
            session, branch=branch, limit=size, before=before), received, failed)

    def jump_to_history_message(self, marker):
        self._cancel_history_positioning()
        self._history_jump_target = marker
        self._continue_history_jump()

    def _continue_history_jump(self):
        marker = self._history_jump_target
        if not marker:
            return
        turn = self.chat_references.lookup(marker.get('chat_id')) if marker.get('chat_id') else None
        if turn is not None:
            self._history_jump_target = None
            self.chat_references.jump(turn, align='top')
        elif self._history_before > marker['position']:
            QTimer.singleShot(0, lambda wanted=marker: self.load_older_messages()
                if self._history_jump_target is wanted else None)
        else:
            self._history_jump_target = None

    def _on_history_wheel(self):
        if not self._history_prepending:
            self._history_jump_target = None
            self._cancel_history_positioning()
            if self.scroll_area.verticalScrollBar().value() <= 40:
                self.load_older_messages()

    def _cancel_history_positioning(self):
        self._history_positioning = False
        self._history_scroll_timer.stop()
        self._auto_scroll_to_bottom = False

    def _on_history_slider_moved(self, value):
        self._cancel_history_positioning()
        if value <= 40:
            self.load_older_messages()

    def _history_prefix(self):
        """Operations such as retry/move need full snapshots; ordinary paging does not."""
        if not self._history_before:
            return []
        pages, before = [], self._history_before
        size = performance_preferences.values['sessions']['page_size']
        while before > 0:
            data = self.session_manager.load_session_page(self.current_session_id,
                branch=self.current_branch, limit=size, before=before)
            next_before = data['page']['before']
            if next_before >= before:
                raise RuntimeError('История изменилась во время чтения. Откройте сессию заново.')
            pages.append(data['messages'])
            before = next_before
        return [message for page in reversed(pages) for message in page]
