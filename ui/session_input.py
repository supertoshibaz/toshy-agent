"""Session/branch-owned composer preferences, independent of history paging."""
from copy import deepcopy

from PySide6.QtCore import QObject, QTimer, Signal

from ui.core_bridge.process_manager import core_process
from ui.core_bridge.async_runner import AsyncRunner
from ui.event_bus import event_bus


class ScopedTagsState(QObject):
    """Share tag names, but never another workspace's active selection."""
    changed = Signal()

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.active_tags = []
        catalog.changed.connect(self._catalog_changed)

    @property
    def all_tags(self):
        return self.catalog.all_tags

    def _catalog_changed(self):
        self.active_tags = [tag for tag in self.active_tags if tag in self.all_tags]
        self.changed.emit()

    def set_active_tags(self, tags):
        active = list(dict.fromkeys(tag for tag in tags if tag in self.all_tags))
        if active != self.active_tags:
            self.active_tags = active
            self.changed.emit()

    def set_tag_active(self, tag, active):
        self.set_active_tags([*self.active_tags, tag] if active else [x for x in self.active_tags if x != tag])

    def add_new_tag(self):
        return self.catalog.add_new_tag()

    def delete_tag(self, tag):
        self.catalog.delete_tag(tag)

    def rename_tag(self, old, new):
        active = [new if tag == old else tag for tag in self.active_tags]
        self.catalog.rename_tag(old, new)
        self.set_active_tags(active)


class SessionInputState(QObject):
    def __init__(self, chat):
        super().__init__(chat)
        self.chat = chat
        self.states = {}
        self.pending = {}
        self.runners = {}
        self.applying = False
        from ui.chat_drafts import DraftStore
        self.drafts = DraftStore()
        self.draft_dirty = False
        chat.input_edit.textChanged.connect(self.draft_changed)
        chat.input_edit.quotes_changed.connect(self.draft_changed)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.flush)
        chat.tags_bar.tag_state_changed.connect(self.changed)
        chat.raw_request_btn.toggled.connect(self.changed)
        chat.input_controls.full_web_action.toggled.connect(self.changed)
        chat.input_controls.skills.changed.connect(self.changed)
        core_process.status_changed.connect(lambda *_: self.flush())
        event_bus.sessions_updated.connect(self.flush)
        event_bus.session_renamed.connect(self.renamed)
        event_bus.branch_renamed.connect(self.branch_renamed)

    def draft_changed(self, *_):
        if not self.applying and not self.chat._session_loading:
            self.draft_dirty = True
            self.timer.start()

    def save_draft(self):
        chat = self.chat
        if self.applying or chat._session_loading or not chat.current_session_id:return
        from ui.chat_drafts import capture
        try:
            self.drafts.write(chat.current_session_id,chat.current_branch,capture(chat))
            self.draft_dirty = False
        except Exception as error:
            from core.logger import core_logger
            core_logger.warn('CHAT_DRAFT_SAVE',str(error))

    def options(self):
        return {'full_web_pages': self.chat.input_controls.full_web_action.isChecked(),
                'raw_request': self.chat.raw_request_btn.isChecked(),
                'skills': sorted(self.chat.input_controls.skills.selected),
                'tags': list(self.chat.tags_bar.get_active_tags()),
                'pipeline': self.chat.pipeline_selector.selected,
                'tags_visible': not self.chat.tags_bar.isHidden()}

    def capture(self):
        return {'reasoning_effort': self.chat.reasoning_selector.value,
                'active_tags': list(self.chat.tags_bar.get_active_tags()),
                'selected_pipeline': self.chat.pipeline_selector.selected,
                'input_options': self.options()}

    def remember(self):
        chat = self.chat
        if self.applying or chat._session_loading or not chat.current_session_id:
            return
        key = (chat.current_session_id, chat.current_branch)
        state = self.capture()
        self.states[key] = deepcopy(state)
        chat._session_metadata.update(deepcopy(state))
        cached = chat._session_cache.get(key)
        if cached is not None:
            cached['metadata'].update(deepcopy(state))
        return key, state

    def changed(self, *_):
        if self.applying:
            return
        remembered = self.remember()
        if remembered is not None:
            key, state = remembered
            self.pending[key] = deepcopy(state)
            self.timer.start()

    def stash(self):
        self.save_draft()
        self.changed()
        self.flush()

    def flush(self):
        self.timer.stop()
        if self.draft_dirty:self.save_draft()
        if not core_process.is_running():
            return
        manager = self.chat.session_manager
        for key, value in list(self.pending.items()):
            if any(active[0] == key[0] for active in self.chat._active_runs):
                continue  # Core owns this session lock until the run finishes.
            self.pending.pop(key, None)
            state = deepcopy(value)

            def save(k=key, data=state):
                if k not in self.states:
                    return  # Renamed while waiting in this session's queue.
                return manager.update_metadata(k[0], data, branch=k[1])

            def failed(error, k=key, data=state):
                # Do not resurrect an older snapshot after a later change or a
                # rename. An offline core is retried on its next status change.
                if self.states.get(k) == data:
                    self.pending.setdefault(k, deepcopy(data))
                if (self.chat.current_session_id, self.chat.current_branch) == k:
                    self.chat._show_error_panel('Не удалось сохранить настройки запроса: ' + str(error))

            runner = self.runners.get(key[0])
            if runner is None:
                runner = self.runners[key[0]] = AsyncRunner(self, max_threads=1)
            runner.submit(save, on_error=failed)

    def restore(self, session_id, branch, metadata):
        key = (session_id, branch)
        try:draft = self.drafts.read(session_id,branch) if session_id else {}
        except Exception as error:
            from core.logger import core_logger
            core_logger.warn('CHAT_DRAFT_RESTORE',str(error))
            draft = {}
        state = self.states.get(key) or draft.get('settings')
        if state is None:
            options = metadata.get('input_options')
            state = {'reasoning_effort': metadata.get('reasoning_effort', ''),
                     'active_tags': list(metadata.get('active_tags') or []),
                     'selected_pipeline': metadata.get('selected_pipeline') or '',
                     'input_options': deepcopy(options) if isinstance(options, dict) else {}}
        self.applying = True
        try:
            chat = self.chat
            chat.reasoning_selector.restore(state.get('reasoning_effort', ''))
            options = state.get('input_options') or {}
            tags = options.get('tags', state['active_tags'])
            pipeline = options.get('pipeline', state['selected_pipeline'])
            chat.tags_bar.set_active_tags(tags)
            chat.tags_bar.setVisible(bool(options.get('tags_visible', False)))
            chat.raw_request_btn.setChecked(bool(options.get('raw_request', False)))
            chat.input_controls.full_web_action.setChecked(bool(options.get('full_web_pages', False)))
            chat.input_controls.skills.set_selected(options.get('skills') or [])
            chat.pipeline_selector.set_session(session_id, branch, pipeline)
            chat.input_controls.refresh_tools()
            chat._session_metadata.update(deepcopy(state))
            if session_id:
                self.states[key] = deepcopy(state)
            from ui.chat_drafts import restore
            try:restore(chat,draft)
            except Exception as error:
                from core.logger import core_logger
                core_logger.warn('CHAT_DRAFT_RESTORE',str(error))
                restore(chat,{})
            self.draft_dirty = False
        finally:
            self.applying = False

    def reset(self):
        self.restore(None, 'main', {})

    def adopt(self):
        """A new session inherits only its own welcome-screen selections."""
        chat = self.chat
        chat.pipeline_selector.set_session(chat.current_session_id, chat.current_branch,
                                           chat.pipeline_selector.selected)
        chat.input_controls.skills.set_selected(chat.input_controls.skills.selected, clear_catalog=False)
        self.remember()

    def renamed(self, old, new):
        for mapping in (self.states, self.pending):
            for key in list(mapping):
                if key[0] == old:
                    mapping[(new, key[1])] = mapping.pop(key)
        if old in self.runners:
            self.runners[new] = self.runners.pop(old)
        for key, state in self.states.items():
            if key[0] == new:
                self.pending[key] = deepcopy(state)
        if self.pending:
            self.timer.start()

    def branch_renamed(self,session,old,new):
        for mapping in (self.states,self.pending):
            if (session,old) in mapping:mapping[(session,new)] = mapping.pop((session,old))
