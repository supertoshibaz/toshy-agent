from pathlib import Path
from typing import Optional
from PySide6.QtCore import QObject, Signal
from core.paths import APP_PATHS


class ApplicationState(QObject):
    """Current chat and project; workspace choices belong to root projects."""
    session_changed = Signal(str, str)
    workspace_changed = Signal(str)
    project_changed = Signal(str, str)
    materials_changed = Signal()
    project_expansion_changed = Signal(str)
    titles_changed = Signal()

    def __init__(self):
        super().__init__()
        self.session_id = None
        self.branch = 'main'
        self.project_id = ''
        self.project_name = ''
        self.expanded_project_id = ''
        self.projects_expanded_ids = set()
        self.expanded_subfolders = set()
        self.collapsed_subfolders = set()
        self._default_workspace = str(APP_PATHS.workspace.resolve())
        self._workspace = self._default_workspace
        self.projects = {}
        self.session_projects = {}
        self.folder_projects = {}
        self.session_titles = {}
        self.branch_titles = {}
        self._pending_workspaces = {}
        self._catalog_ready = False

    @property
    def workspace(self):
        return self._workspace

    def view_workspace(self, kind):
        if kind in {'board', 'paint', 'materials'}:
            from core.project_assets import assets_root
            return str(assets_root(self.project_id, self.workspace,
                self.projects.get(self.project_id, {}).get('assets'), self.projects))
        return self.workspace

    def update_catalog(self, snapshot):
        sessions = snapshot.get('sessions', [])
        session_titles = {s['session_id']:s.get('title') or s['session_id'] for s in sessions if s.get('session_id')}
        branch_titles = {(s['session_id'],b['name']):b.get('title') or b['name']
                         for s in sessions if s.get('session_id') for b in s.get('branches',[]) if b.get('name')}
        changed = (session_titles,branch_titles) != (self.session_titles,self.branch_titles)
        self.session_titles,self.branch_titles = session_titles,branch_titles
        folders = {str(item['id']): item for item in snapshot.get('folders', []) if item.get('id')}
        roots = {}
        folder_projects = {}
        for identity in folders:
            current, seen = identity, set()
            while current in folders and current not in seen:
                seen.add(current)
                parent = folders[current].get('parent_id')
                if not parent or parent not in folders:
                    folder_projects[identity] = current
                    roots[current] = {'name': folders[current].get('name', 'Project'),
                        'workspace': self._normalize_workspace((folders[current].get('settings') or {}).get('workspace_path')),
                        'assets': (folders[current].get('settings') or {}).get('assets')}
                    break
                current = parent
        previous_assets = self.view_workspace("board")
        self.projects = roots
        self.projects_expanded_ids.intersection_update(roots)
        self.folder_projects = folder_projects
        self.session_projects = {str(item['session_id']): folder_projects.get(str(item.get('folder_id')), '')
            for item in snapshot.get('sessions', []) if item.get('session_id')}
        self._catalog_ready = True
        active_project = self.session_projects.get(self.session_id)
        if active_project and active_project != self.project_id:
            self.select_project(active_project)
        elif self.project_id in roots:
            self.select_project(self.project_id, expand=False)
        elif self.project_id:
            self.select_project('', expand=False)
        if self.expanded_project_id and self.expanded_project_id not in roots:
            self.set_expanded_project('')
        if previous_assets != self.view_workspace("board"):
            self.materials_changed.emit()
        if changed:self.titles_changed.emit()

    def display_session(self, session_id, branch='main'):
        title = self.session_titles.get(session_id,session_id) or 'новая'
        if branch and branch!='main':
            title += ' / ' + self.branch_titles.get((session_id,branch),branch)
        return title

    def select_project(self, identity, name=None, workspace=None, *, expand=True):
        identity = str(identity or '')
        previous, previous_name = self.project_id, self.project_name
        record = self.projects.setdefault(identity, {}) if identity else {}
        if name is not None:
            record['name'] = name
        if workspace is not None:
            record['workspace'] = self._normalize_workspace(workspace)
        self.project_id = identity
        self.project_name = str(record.get('name') or '')
        self._set_workspace_value(self._pending_workspaces.get(identity) or record.get('workspace') or self._default_workspace)
        if (previous, previous_name) != (self.project_id, self.project_name):
            self.project_changed.emit(self.project_id, self.project_name)
        if expand:
            self.set_expanded_project(identity)

    def set_expanded_project(self, identity):
        identity = str(identity or '')
        if identity != self.expanded_project_id:
            self.expanded_project_id = identity
            self.project_expansion_changed.emit(identity)

    def select_session(self, session_id: Optional[str], branch='main', workspace=None):
        self.session_id = session_id
        self.branch = branch or 'main'
        project = self.session_projects.get(session_id)
        if project:
            self.select_project(project)
        # Legacy workspace arguments must never reintroduce per-chat folders.
        self.session_changed.emit(session_id or '', self.branch)

    def set_workspace(self, workspace, remember_for_session=True):
        normalized = self._normalize_workspace(workspace)
        if self.project_id:
            self.projects.setdefault(self.project_id, {})['workspace'] = normalized
        self._set_workspace_value(normalized)

    def remember_session_workspace(self, session_id, workspace):
        # Compatibility entry point: old per-session caches are retired.
        return

    def _set_workspace_value(self, workspace):
        if workspace != self._workspace:
            self._workspace = workspace
            self.workspace_changed.emit(workspace)

    def _normalize_workspace(self, workspace):
        return str(Path(workspace or self._default_workspace).expanduser().resolve())


app_state = ApplicationState()
