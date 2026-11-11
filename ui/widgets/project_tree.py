"""Project creation rules with independent expansion policies for both trees."""
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QColor, QBrush
from ui.app_state import app_state
from ui.themes.manager import theme_manager
from ui.icons import get


def root_item(item):
    if item is None:
        return None
    while item.parent() is not None:
        item = item.parent()
    return item if (item.data(0, Qt.UserRole) or {}).get('type') == 'folder' else None


def folder_color(item):
    return theme_manager.value('text.primary')


def decorate_folder(widget, item):
    color = folder_color(item)
    item.setForeground(0, QBrush(QColor(color)))
    icon_color = '#facc15' if item.parent() is None else '#e4e4e7'
    item.setIcon(0, get('folder', icon_color))



class ProjectTreeRules(QObject):
    def __init__(self, tree, create_button, *, exclusive=True):
        super().__init__(tree)
        self.tree, self.create_button = tree, create_button
        self.exclusive = exclusive
        theme_manager.theme_changed.connect(self.refresh_theme)
        tree.itemExpanded.connect(self.expanded)
        tree.itemCollapsed.connect(self.collapsed)
        app_state.project_expansion_changed.connect(self.selected_root_changed)
        self.sync()

    def refresh_theme(self, *_):
        pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while pending:
            item = pending.pop()
            if (item.data(0, Qt.UserRole) or {}).get('type') == 'folder':
                decorate_folder(self.tree, item)
            pending.extend(item.child(i) for i in range(item.childCount()))
        self.tree.viewport().update()

    def selected_root_changed(self, identity):
        if not self.exclusive and identity:
            app_state.projects_expanded_ids.add(identity)
        self.sync()

    def expanded(self, item):
        data = item.data(0, Qt.UserRole) or {}
        if data.get('type') != 'folder':
            return
        identity = str(data['id'])
        if item.parent() is None:
            if not self.exclusive:
                app_state.projects_expanded_ids.add(identity)
            app_state.select_project(identity)
        else:
            app_state.expanded_subfolders.add(identity)
            app_state.collapsed_subfolders.discard(identity)
        self.sync()

    def collapsed(self, item):
        data = item.data(0, Qt.UserRole) or {}
        if data.get('type') != 'folder':
            return
        identity = str(data['id'])
        if item.parent() is None:
            if not self.exclusive:
                app_state.projects_expanded_ids.discard(identity)
            elif app_state.expanded_project_id == identity:
                app_state.set_expanded_project('')
        else:
            app_state.expanded_subfolders.discard(identity)
            app_state.collapsed_subfolders.add(identity)
        self.sync()

    def sync(self, *_):
        blocked = self.tree.blockSignals(True)
        found = False
        try:
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                identity = (item.data(0, Qt.UserRole) or {}).get('id')
                opened = (identity == app_state.expanded_project_id if self.exclusive
                          else identity in app_state.projects_expanded_ids)
                found |= opened
                item.setExpanded(opened)
        finally:
            self.tree.blockSignals(blocked)
        self.create_button.setEnabled(bool(found))

    def restore(self):
        blocked = self.tree.blockSignals(True)
        try:
            pending = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
            while pending:
                item = pending.pop()
                data = item.data(0, Qt.UserRole) or {}
                if data.get('type') == 'folder':
                    decorate_folder(self.tree, item)
                    if item.parent() is not None:
                        item.setExpanded(data['id'] not in app_state.collapsed_subfolders)
                pending.extend(item.child(index) for index in range(item.childCount()))
        finally:
            self.tree.blockSignals(blocked)
        self.sync()

    def creation_folder(self, preferred=None):
        if preferred:
            project = app_state.folder_projects.get(str(preferred))
            if not project:
                return None
            if not self.exclusive:
                app_state.projects_expanded_ids.add(project)
            app_state.select_project(project)
            self.sync()
            return str(preferred)
        item = self.tree.currentItem()
        root = root_item(item)
        if root and root.isExpanded():
            while item is not None:
                data = item.data(0, Qt.UserRole) or {}
                if data.get('type') == 'folder':
                    return str(data['id'])
                item = item.parent()
        if self.exclusive:
            return app_state.expanded_project_id or None
        roots = [self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        opened = [str(item.data(0, Qt.UserRole)['id']) for item in roots if item.isExpanded()]
        if app_state.project_id in opened:
            return app_state.project_id
        return opened[0] if opened else None
