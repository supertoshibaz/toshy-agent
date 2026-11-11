"""Project-owned text workspace and extensible state scopes for every editor."""
from ui.i18n import tr

from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import QObject
from ui.app_state import app_state
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.session_client import SessionApiClient
from ui.core_bridge.process_manager import core_process
from ui.event_bus import event_bus
from ui.widgets import inline_editor as inline


def restore_project_identity(state):
    app_state.projects = deepcopy(state.get('project_workspaces') or {})
    # Older UI states had one shared expanded root. Preserve it on upgrade;
    # an explicitly saved empty list means Projects was fully collapsed.
    app_state.projects_expanded_ids = set(state.get('projects_expanded_ids',
        [state.get('expanded_project_id')] if state.get('expanded_project_id') else []) or [])
    app_state.expanded_subfolders = set(state.get('expanded_project_subfolders') or [])
    app_state.collapsed_subfolders = set(state.get('collapsed_project_subfolders') or [])
    app_state.select_project(state.get('active_project_id') or '', expand=False)
    app_state.set_expanded_project(state.get('expanded_project_id') or '')


class ProjectWorkspaces(QObject):
    def __init__(self, window, manager):
        super().__init__(window)
        self.window, self.manager = window, manager
        self.state = window.state_manager
        self.client = SessionApiClient(base_url=core_process.base_url)
        self.runner = AsyncRunner(self, max_threads=1)
        self.current = app_state.project_id
        self.legacy_workspace = self.state.get("workspace_path", "")
        self.expansions = deepcopy(self.state.get('project_file_expansions', {}))
        self._setting_manager = False
        scopes = self.state.get('view_states') or {}
        legacy = not self.state.get('project_workspace_layout_v1', False)
        text_states = scopes.get('text') or {'_default':{
            'open_files': self.state.get('open_files', []),
            'active_file_index': self.state.get('active_file_index', 0)}}
        window.file_view.restore_project_states(text_states, self.current, legacy=legacy)
        board = (scopes.get('board') or {}).get(self.scope('board')) or {
            'open_boards': self.state.get('open_boards', []),
            'active_board_index': self.state.get('active_board_index', 0)}
        if window.board_view is not None:
            window.board_view.restore_open_state(board.get('open_boards', []), board.get('active_board_index', 0))
        paint = (scopes.get('paint') or {}).get(self.scope('paint')) or {
            'open_paints': self.state.get('open_paints', []),
            'active_paint_index': self.state.get('active_paint_index', 0)}
        if window.paint_view is not None:
            window.paint_view.restore_open_state(paint.get('open_paints', []), paint.get('active_paint_index', 0))
        self.view_states = deepcopy(scopes)
        app_state.project_changed.connect(self.project_changed)
        app_state.workspace_changed.connect(self.workspace_changed)
        if manager is not None:
            manager.workspace_changed.connect(self.folder_chosen)
            manager.set_project_name(self.current, app_state.project_name)
            self.sync_manager(app_state.workspace)
            manager.restore_expanded_folders(self.expansions.get(self.current, self.state.get('expanded_folders', [])))
        window.chat_view.set_workspace_root(app_state.workspace)

    def scope(self, kind):
        path = app_state.view_workspace(kind)
        return 'workspace' if path == app_state._default_workspace else 'path:' + path

    def sync_manager(self, path):
        if self.manager is None or self.manager.current_path == path:
            return
        self._setting_manager = True
        try:
            self.manager.set_workspace_path(path)
        finally:
            self._setting_manager = False

    def workspace_changed(self, path):
        if self.current == app_state.project_id:
            self.sync_manager(path)
            view = self.window.file_view
            view._on_current_tab_changed(view.tab_bar.currentIndex())
        self.window.chat_view.set_workspace_root(path)

    def project_changed(self, identity, name):
        if (identity and not self.current and self.window.file_view.project_tabs.migrate_legacy
                and app_state.workspace == app_state._default_workspace and self.legacy_workspace
                and Path(self.legacy_workspace).is_dir()):
            self.folder_chosen(self.legacy_workspace)
        chat = self.window.chat_view
        if (chat.current_session_id and not chat._session_loading
                and app_state.session_projects.get(chat.current_session_id) != identity):
            chat.reset_to_new_chat(identity)
        if self.manager is not None:
            if self.current != identity:
                self.expansions[self.current] = self.manager.get_expanded_folders()
                self.sync_manager(app_state.workspace)
                self.manager.restore_expanded_folders(self.expansions.get(identity, []))
            self.manager.set_project_name(identity, name)
        self.current = identity
        self.window.file_view.set_project(identity)
        self.window.chat_view.set_workspace_root(app_state.workspace)

    def folder_chosen(self, path):
        if self._setting_manager or app_state._normalize_workspace(path) == app_state.workspace:
            return
        project = app_state.project_id
        previous = app_state.workspace
        app_state.set_workspace(path)
        if not project:
            return
        normalized = app_state.workspace
        app_state._pending_workspaces[project] = normalized
        def save():
            result = self.client.update_project_folder(project, settings={'workspace_path':path or ''})
            if not result.get('success'):
                raise ValueError(result.get('message') or 'Не удалось сохранить папку проекта')
            return result
        def saved(result):
            if app_state._pending_workspaces.get(project) == normalized:
                app_state._pending_workspaces.pop(project, None)
            event_bus.sessions_updated.emit()
        def failed(error):
            if app_state._pending_workspaces.get(project) == normalized:
                app_state._pending_workspaces.pop(project, None)
                app_state.projects.setdefault(project, {})['workspace'] = previous
                if app_state.project_id == project:
                    app_state.set_workspace(previous)
            inline.notice(self.window, tr('ui.project_workspaces.project_folder_was_not_saved_a312'), str(error))
        self.runner.submit(save, saved, failed)

    def snapshot(self):
        if self.manager is not None:
            self.expansions[self.current] = self.manager.get_expanded_folders()
        result = deepcopy(self.view_states)
        result['text'] = self.window.file_view.get_project_states()
        if self.window.board_view is not None:
            result.setdefault('board', {})[self.scope('board')] = self.window.board_view.get_open_state()
        if self.window.paint_view is not None:
            result.setdefault('paint', {})[self.scope('paint')] = self.window.paint_view.get_open_state()
        projects = deepcopy(app_state.projects)
        for identity, path in app_state._pending_workspaces.items():
            projects.setdefault(identity, {})['workspace'] = path
        if app_state.project_id:
            projects.setdefault(app_state.project_id, {})['workspace'] = app_state.workspace
        return {'view_states':result, 'project_workspaces':projects,
            'active_project_id':app_state.project_id, 'expanded_project_id':app_state.expanded_project_id,
            'projects_expanded_ids':sorted(app_state.projects_expanded_ids),
            'expanded_project_subfolders':sorted(app_state.expanded_subfolders),
            'collapsed_project_subfolders':sorted(app_state.collapsed_subfolders),
            'project_file_expansions':deepcopy(self.expansions), 'project_workspace_layout_v1':True}
