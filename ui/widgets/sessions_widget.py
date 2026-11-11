from __future__ import annotations
from ui.themes.manager import theme_manager
from ui.icons import get as ui_icon
from ui.icons import set_icon
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style


from typing import Any, Dict, Optional

from PySide6.QtCore import QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QMenu, QStackedWidget, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QApplication)
from ui.widgets.dialogs import MessageBox as QMessageBox

from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.session_client import SessionApiClient
from ui.event_bus import event_bus


_AUTO_FOLDER = object()


class SidebarProjectTree(QTreeWidget):
    move_requested = Signal(str, str, object)  # item_type, entity_id, target_folder_id|None
    sessions_move_requested = Signal(object, object)  # session_ids, target_folder_id|None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SessionsTreeWidget")
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self._pressed_disclosure = False

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        self._pressed_disclosure = False
        if item is not None and event.button() == Qt.LeftButton:
            rect = self.visualItemRect(item)
            self._pressed_disclosure = (event.position().x() > rect.right()
                if self.layoutDirection() == Qt.RightToLeft else event.position().x() < rect.left())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._pressed_disclosure = False

    @staticmethod
    def _target_folder_id(item: Optional[QTreeWidgetItem]) -> Optional[str]:
        current = item
        while current is not None:
            data = current.data(0, Qt.UserRole) or {}
            if data.get("type") == "folder":
                return str(data.get("id") or "") or None
            current = current.parent()
        return None

    def dropEvent(self, event):
        source = self.currentItem()
        if source is None:
            event.ignore()
            return

        data = source.data(0, Qt.UserRole) or {}
        item_type = data.get("type")
        entity_id = data.get("id")
        if item_type not in {"folder", "session"} or not entity_id:
            event.ignore()
            return

        target = self.itemAt(event.position().toPoint())
        target_folder = self._target_folder_id(target)
        if item_type == "session" and not target_folder:
            event.ignore()
            return
        selected_sessions = []
        for item in self.selectedItems():
            item_data = item.data(0, Qt.UserRole) or {}
            if item_data.get("type") == "session":
                session_id = str(item_data.get("session_id") or item_data.get("id") or "")
                if session_id:
                    selected_sessions.append(session_id)
        if item_type == "session" and len(selected_sessions) > 1:
            self.sessions_move_requested.emit(list(dict.fromkeys(selected_sessions)), target_folder)
        else:
            self.move_requested.emit(str(item_type), str(entity_id), target_folder)
        event.acceptProposedAction()


class SessionsWidget(QWidget):
    PROJECTS_VIEW_INDEX = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SessionsWidgetRoot")
        theme_manager.theme_changed.connect(self._update_highlight)
        self.session_manager = SessionApiClient(base_url=core_process.base_url)
        self.active_session_id: Optional[str] = None
        self.active_branch: str = "main"
        self.async_runner = AsyncRunner(self)
        self._refresh_in_flight = False
        self._last_snapshot: Dict[str, Any] = {"folders": [], "sessions": []}
        self._projects_space_active = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        self.header_widget = QWidget(self)
        header = QHBoxLayout(self.header_widget)
        header.setContentsMargins(4, 2, 4, 2)

        title = bind_caption(QLabel('', self), tr("panel_projects", "Проекты"))
        apply_style(title, "font-weight: bold; color: @text.secondary@; font-size: 11px;")

        self.btn_new_folder = QToolButton(self)
        self.btn_new_folder.setObjectName("SessionsNewBtn")
        set_icon(self.btn_new_folder, 'folder-open', size=18)
        self.btn_new_folder.setFixedSize(26, 26)
        set_text(self.btn_new_folder, 'ui.sessions_widget.create_project_49ed', setter='setToolTip')
        self.btn_new_folder.setCursor(Qt.PointingHandCursor)
        self.btn_new_folder.clicked.connect(self._create_folder)

        self.btn_new = QToolButton(self)
        self.btn_new.setObjectName("SessionsNewBtn")
        set_icon(self.btn_new, 'file', size=18)
        self.btn_new.setFixedSize(26, 26)
        set_text(self.btn_new, 'ui.sessions_widget.create_new_session_9da1', setter='setToolTip')
        self.btn_new.setCursor(Qt.PointingHandCursor)
        self.btn_new.clicked.connect(self._create_session)

        self.btn_add_folder = QToolButton(self)
        self.btn_add_folder.setObjectName('SessionsNewBtn')
        set_icon(self.btn_add_folder, 'folder-plus', size=18)
        self.btn_add_folder.setFixedSize(26, 26)
        set_text(self.btn_add_folder, 'ui.sessions_widget.create_folder_in_the_selected_project_9b19', setter='setToolTip')
        self.btn_add_folder.setCursor(Qt.PointingHandCursor)
        self.btn_add_folder.clicked.connect(self._create_child_folder)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.btn_new_folder)
        header.addWidget(self.btn_add_folder)
        header.addWidget(self.btn_new)
        layout.addWidget(self.header_widget)

        self.content_stack = QStackedWidget(self)
        layout.addWidget(self.content_stack, 1)

        self.tree = SidebarProjectTree(self.content_stack)
        from ui.generation_status import install_session_status
        install_session_status(self.tree)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemSelectionChanged.connect(self._normalize_multi_selection)
        self.tree.itemSelectionChanged.connect(lambda: self.btn_add_folder.setEnabled(bool(self._parent_for_creation())))
        self.btn_add_folder.setEnabled(False)
        self.tree.move_requested.connect(self._move_entity)
        self.tree.sessions_move_requested.connect(self._move_sessions)
        from ui.widgets.project_tree import ProjectTreeRules
        self.project_tree = ProjectTreeRules(self.tree, self.btn_new)
        self.content_stack.addWidget(self.tree)

        self.inactive_label = localize(QLabel('', self.content_stack), 'ui.sessions_widget.not_active_in_projects_space_3458')
        self.inactive_label.setAlignment(Qt.AlignCenter)
        self.inactive_label.setWordWrap(True)
        apply_style(self.inactive_label, "color:@text.disabled@; padding:16px;")
        self.content_stack.addWidget(self.inactive_label)
        self.content_stack.setCurrentWidget(self.tree)

        # The core owns the hierarchy. The GUI only polls the public API snapshot.
        self.sessions_poll_timer = QTimer(self)
        self.sessions_poll_timer.setInterval(2000)
        self.sessions_poll_timer.timeout.connect(self._poll_session_catalog)
        self.sessions_poll_timer.start()

        event_bus.sessions_updated.connect(self._poll_session_catalog)
        event_bus.session_selected.connect(self._on_session_selected_externally)
        event_bus.workspace_view_changed.connect(self._on_workspace_view_changed)
        core_process.status_changed.connect(
            lambda online, _: self._poll_session_catalog() if online else None
        )

        self._poll_session_catalog()

    # ------------------------------------------------------------------
    # Snapshot / view state
    # ------------------------------------------------------------------
    def _on_workspace_view_changed(self, index: int):
        active = int(index) == self.PROJECTS_VIEW_INDEX
        if self._projects_space_active == active:
            return
        self._projects_space_active = active
        self.header_widget.setVisible(not active)
        self.btn_new.setVisible(not active)
        self.btn_new_folder.setVisible(not active)
        self.content_stack.setCurrentWidget(self.inactive_label if active else self.tree)
        if not active:
            self._last_snapshot = {}
            self._poll_session_catalog()

    def _poll_session_catalog(self):
        if self._projects_space_active or not core_process.is_running() or self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.async_runner.submit(
            self.session_manager.get_projects,
            self._on_catalog_loaded,
            self._on_catalog_error,
        )

    def _on_catalog_loaded(self, snapshot):
        self._refresh_in_flight = False
        snapshot = snapshot if isinstance(snapshot, dict) else {"folders": [], "sessions": []}
        sessions = list(snapshot.get("sessions", []))
        existing_sessions = {
            str(item.get("session_id"))
            for item in sessions
            if item.get("session_id")
        }

        if self.active_session_id and self.active_session_id not in existing_sessions:
            self.active_session_id = None
            self.active_branch = "main"
            event_bus.new_chat_requested.emit()

        changed = snapshot != self._last_snapshot
        self._last_snapshot = snapshot
        if changed and not getattr(self, "_finish_inline_rename", None):
            self.refresh_sessions(snapshot)
        else:
            bind_caption(self.tree, "", setter='setToolTip')

    def _on_catalog_error(self, error):
        self._refresh_in_flight = False
        set_text(self.tree, 'ui.sessions_widget.could_not_refresh_sessions_v0_e845', setter='setToolTip', v0=error)

    def refresh_sessions(self, snapshot=None):
        if snapshot is None:
            self._poll_session_catalog()
            return
        if self._projects_space_active:
            return

        from ui.app_state import app_state
        app_state.update_catalog(snapshot)
        blocked = self.tree.blockSignals(True)
        self.tree.clear()
        bind_caption(self.tree, "", setter='setToolTip')
        folders = {
            str(item.get("id")): item
            for item in snapshot.get("folders", [])
            if item.get("id")
        }
        folder_items: Dict[str, QTreeWidgetItem] = {}

        def add_folder(folder_id: str, stack=None):
            if folder_id in folder_items:
                return folder_items[folder_id]
            stack = set(stack or ())
            if folder_id in stack:
                return None
            stack.add(folder_id)
            folder = folders.get(folder_id)
            if not folder:
                return None
            parent_id = folder.get("parent_id")
            parent_item = (
                add_folder(str(parent_id), stack)
                if parent_id and str(parent_id) in folders
                else None
            )
            item = QTreeWidgetItem(parent_item or self.tree)
            bind_caption(item, str(folder.get("name") or "Folder"), setter='setText', index=0)
            item.setIcon(0, ui_icon('folder'))
            item.setData(0, Qt.UserRole, {"type": "folder", "id": folder_id})
            item.setFlags(item.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
            folder_items[folder_id] = item
            return item

        for folder_id in folders:
            add_folder(folder_id)

        for session in sorted(
            snapshot.get("sessions", []),
            key=lambda item: str(item.get("session_id", "")),
        ):
            session_id = str(session.get("session_id") or "")
            if not session_id:
                continue
            folder_id = session.get("folder_id")
            parent_item = folder_items.get(str(folder_id)) if folder_id else None
            if parent_item is None:
                continue
            item = QTreeWidgetItem(parent_item)
            bind_caption(item, str(session.get('title') or session_id), setter='setText', index=0)
            item.setIcon(0, ui_icon('document'))
            item.setData(
                0,
                Qt.UserRole,
                {"type": "session", "id": session_id, "session_id": session_id, "branch": "main"},
            )
            item.setFlags(item.flags() | Qt.ItemIsDragEnabled)

            for branch in session.get("branches", []) or []:
                branch_name = str(branch.get("name") or "")
                if not branch_name or branch_name == "main":
                    continue
                child = QTreeWidgetItem(item)
                bind_caption(child, str(branch.get('title') or branch_name), setter='setText', index=0)
                child.setIcon(0, ui_icon('branch'))
                child.setData(
                    0,
                    Qt.UserRole,
                    {
                        "type": "branch",
                        "id": f"{session_id}:{branch_name}",
                        "session_id": session_id,
                        "branch": branch_name,
                    },
                )
            item.setExpanded(True)

        self.project_tree.restore()
        self._update_highlight()
        self.tree.blockSignals(blocked)

    def _iter_items(self):
        root = self.tree.invisibleRootItem()
        stack = [root.child(i) for i in range(root.childCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _update_highlight(self):
        active_color = QBrush(theme_manager.color('accent.primary'))
        default_color = QBrush(theme_manager.color('text.primary'))
        font_bold = QFont()
        font_bold.setBold(True)
        font_normal = QFont()
        font_normal.setBold(False)

        for item in self._iter_items():
            data = item.data(0, Qt.UserRole) or {}
            item_type = data.get("type")
            is_active = False
            if item_type == "session":
                is_active = (
                    data.get("session_id") == self.active_session_id
                    and self.active_branch == "main"
                )
            elif item_type == "branch":
                is_active = (
                    data.get("session_id") == self.active_session_id
                    and data.get("branch") == self.active_branch
                )
            from ui.widgets.project_tree import folder_color
            color = QBrush(QColor(folder_color(item))) if item_type == "folder" else default_color
            item.setForeground(0, active_color if is_active else color)
            item.setFont(0, font_bold if is_active else font_normal)

    def _run_session_action(self, action, on_success=None):
        self.tree.setEnabled(False)

        def finished(result):
            self.tree.setEnabled(True)
            if isinstance(result, dict) and result.get("success") is False:
                QMessageBox.warning(
                    self,
                    tr('ui.file_manager_widget.error_bf22'),
                    str(result.get("message") or tr('ui.caption.ed52c4ee54a0')),
                )
                self._poll_session_catalog()
                return
            if on_success:
                on_success(result)
            event_bus.sessions_updated.emit()

        def failed(error):
            self.tree.setEnabled(True)
            QMessageBox.warning(self, tr('ui.file_manager_widget.error_bf22'), tr('ui.sessions_widget.session_operation_failed_v0_9685', v0=error))

        self.async_runner.submit(action, finished, failed)

    # ------------------------------------------------------------------
    # Selection / open
    # ------------------------------------------------------------------
    def _on_session_selected_externally(self, session_id: str, branch: str):
        self.active_session_id = session_id
        self.active_branch = branch
        self._update_highlight()

    def _normalize_multi_selection(self):
        selected = self.tree.selectedItems()
        if len(selected) <= 1:
            return
        if all((item.data(0, Qt.UserRole) or {}).get("type") == "session" for item in selected):
            return
        current = self.tree.currentItem()
        if current is None:
            return
        blocked = self.tree.blockSignals(True)
        try:
            self.tree.clearSelection()
            current.setSelected(True)
        finally:
            self.tree.blockSignals(blocked)

    def _selected_session_ids(self):
        result = []
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.UserRole) or {}
            if data.get("type") != "session":
                continue
            session_id = str(data.get("session_id") or data.get("id") or "")
            if session_id:
                result.append(session_id)
        return list(dict.fromkeys(result))

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int):
        if QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
            return
        data = item.data(0, Qt.UserRole) or {}
        if data.get('type') == 'folder' and item.parent() is None:
            # Стрелка сохраняет штатное сворачивание; тело строки выбирает проект.
            if self.tree._pressed_disclosure:
                return
            from ui.app_state import app_state
            app_state.select_project(str(data['id']))
            self.project_tree.sync()
            return
        if data.get("type") not in {"session", "branch"}:
            return
        self._open_chat(str(data.get("session_id") or ""), str(data.get("branch") or "main"))

    def _open_chat(self, session_id: str, branch: str = "main"):
        if not session_id:
            return
        self.active_session_id = session_id
        self.active_branch = branch or "main"
        self._update_highlight()
        event_bus.session_selected.emit(session_id, self.active_branch)
        event_bus.switch_view_tab_requested.emit(0)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int):
        self._rename_item(item)

    # ------------------------------------------------------------------
    # Creation / hierarchy
    # ------------------------------------------------------------------
    @staticmethod
    def _folder_for_item(item: Optional[QTreeWidgetItem]) -> Optional[str]:
        current = item
        while current is not None:
            data = current.data(0, Qt.UserRole) or {}
            if data.get("type") == "folder":
                return str(data.get("id") or "") or None
            current = current.parent()
        return None

    def _parent_for_creation(self) -> Optional[str]:
        return self._folder_for_item(self.tree.currentItem())

    def _create_folder(self, _checked=False, parent_id: Any = _AUTO_FOLDER):
        target_parent = None if parent_id is _AUTO_FOLDER else parent_id
        name = 'New Project' if target_parent is None else 'New Folder'
        def created(result):
            if target_parent is None and result.get('success'):
                from ui.app_state import app_state
                app_state.select_project(result['folder']['id'], name)
        self._run_session_action(lambda: self.session_manager.create_project_folder(name, target_parent), created)

    def _create_child_folder(self):
        parent = self._parent_for_creation()
        if parent:
            self._create_folder(parent_id=parent)

    def _create_session(self, _checked=False, folder_id: Any = _AUTO_FOLDER):
        target_folder = self.project_tree.creation_folder(None if folder_id is _AUTO_FOLDER else folder_id)
        if not target_folder:
            return
        event_bus.new_chat_in_folder_requested.emit(target_folder)
        event_bus.switch_view_tab_requested.emit(0)

    def _move_entity(self, item_type: str, entity_id: str, target_folder_id):
        if item_type == "session":
            if not target_folder_id:
                return
            self._run_session_action(
                lambda: self.session_manager.update_project_session(
                    entity_id, folder_id=target_folder_id
                )
            )
        elif item_type == "folder":
            self._run_session_action(
                lambda: self.session_manager.update_project_folder(
                    entity_id, parent_id=target_folder_id
                )
            )

    def _move_sessions(self, session_ids, target_folder_id):
        if not target_folder_id:
            return
        ids = [str(value) for value in (session_ids or []) if str(value)]
        if not ids:
            return

        def action():
            for session_id in ids:
                result = self.session_manager.update_project_session(
                    session_id, folder_id=target_folder_id
                )
                if isinstance(result, dict) and result.get("success") is False:
                    return result
            return {"success": True}

        self._run_session_action(action)

    # ------------------------------------------------------------------
    # Rename / context menu / delete
    # ------------------------------------------------------------------
    def _rename_item(self, item: QTreeWidgetItem):
        active = getattr(self, '_finish_inline_rename', None)
        if active:
            active()
        data = item.data(0, Qt.UserRole) or {}
        item_type = data.get("type")
        if item_type not in {"folder", "session", "branch"}:
            return

        if item_type == "folder":
            old_name = item.text(0)
        elif item_type == "session":
            old_name = item.text(0)
        else:
            old_name = item.text(0).removeprefix('🌿 ')

        rect = self.tree.visualItemRect(item)
        editor = QLineEdit(old_name, self.tree)
        editor.setObjectName("InlineSessionRename")
        editor.setGeometry(rect.adjusted(24, 1, -4, -1))
        editor.selectAll()
        editor.show()
        editor.setFocus()

        finished_once = False

        def finish_rename():
            nonlocal finished_once
            if finished_once:
                return
            finished_once = True
            self._finish_inline_rename = None
            self._last_snapshot = None
            new_name = editor.text().strip()
            editor.deleteLater()
            if not new_name or new_name == old_name:
                return

            if item_type == "folder":
                folder_id = str(data.get("id") or "")
                self._run_session_action(
                    lambda: self.session_manager.update_project_folder(folder_id, name=new_name)
                )
                return

            session_id = str(data.get("session_id") or "")
            if item_type == "session":
                def renamed(result):
                    if result.get("success"):
                        actual = str(result.get("new_session_id") or new_name)
                        if self.active_session_id == session_id:
                            self.active_session_id = actual
                        event_bus.session_renamed.emit(session_id, actual)

                self._run_session_action(
                    lambda: self.session_manager.rename_session(session_id, new_name),
                    renamed,
                )
                return

            old_branch = str(data.get("branch") or "")
            clean_branch = new_name
            if not clean_branch:
                return

            def branch_renamed(result):
                if result.get("success"):
                    actual = str(result.get("branch") or clean_branch)
                    if self.active_session_id == session_id and self.active_branch == old_branch:
                        self.active_branch = actual
                    event_bus.branch_renamed.emit(session_id, old_branch, actual)

            self._run_session_action(
                lambda: self.session_manager.rename_branch(
                    session_id, old_branch, clean_branch
                ),
                branch_renamed,
            )

        editor.editingFinished.connect(finish_rename)
        self._finish_inline_rename = finish_rename
        def cancel_rename():
            bind_caption(editor, old_name)
            finish_rename()
        from ui.widgets.commit_on_outside import OutsideCommit
        editor._outside_commit = OutsideCommit(editor, finish_rename, cancel_rename)

    def _show_context_menu(self, pos: QPoint):
        if self._projects_space_active:
            return
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        menu.setObjectName("SessionsContextMenu")

        if item is None:
            add_folder = localize(add_caption_action(menu, 'addAction', "New Folder"), 'ui.projects_view.new_folder_c75b')
            add_session = localize(add_caption_action(menu, 'addAction', "New Session"), 'ui.projects_view.new_session_fc0b')
            add_session.setEnabled(self.btn_new.isEnabled())
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen == add_folder:
                self._create_folder(parent_id=None)
            elif chosen == add_session:
                self._create_session()
            return

        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
            self.tree.setCurrentItem(item)

        selected_sessions = self._selected_session_ids()
        if len(selected_sessions) > 1:
            delete_selected = localize(add_caption_action(menu, 'addAction', f"Delete selected sessions ({len(selected_sessions)})"), 'ui.projects_view.delete_selected_sessions_v0_fd8e', v0=len(selected_sessions))
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen == delete_selected:
                self._delete_sessions(selected_sessions)
            return

        data = item.data(0, Qt.UserRole) or {}
        item_type = data.get("type")
        rename = localize(add_caption_action(menu, 'addAction', "Rename"), 'ui.projects_view.rename_3064')

        if item_type == "folder":
            new_sub = localize(add_caption_action(menu, 'addAction', "New subfolder"), 'ui.projects_view.new_subfolder_1f05')
            new_session = localize(add_caption_action(menu, 'addAction', "New session here"), 'ui.projects_view.new_session_here_5c08')
            delete = localize(add_caption_action(menu, 'addAction', "Delete folder"), 'ui.projects_view.delete_folder_39f3')
        elif item_type == "session":
            open_chat = localize(add_caption_action(menu, 'addAction', "Open in Chat"), 'ui.projects_view.open_in_chat_1d52')
            delete = localize(add_caption_action(menu, 'addAction', "Delete session"), 'ui.projects_view.delete_session_ba2b')
        elif item_type == "branch":
            open_chat = localize(add_caption_action(menu, 'addAction', "Open in Chat"), 'ui.projects_view.open_in_chat_1d52')
            delete = localize(add_caption_action(menu, 'addAction', "Delete branch"), 'ui.projects_view.delete_branch_1c3f')
        else:
            return

        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == rename:
            self._rename_item(item)
            return

        if item_type == "folder":
            folder_id = str(data.get("id") or "")
            if chosen == new_sub:
                self._create_folder(parent_id=folder_id)
            elif chosen == new_session:
                self._create_session(folder_id=folder_id)
            elif chosen == delete:
                if QMessageBox.question(
                    self,
                    tr('ui.projects_view.projects_04e2'),
                    tr('ui.projects_view.delete_this_folder_subfolders_and_sessions_ac4f'),
                ) == QMessageBox.Yes:
                    self._run_session_action(
                        lambda: self.session_manager.delete_project_folder(folder_id)
                    )
            return

        session_id = str(data.get("session_id") or "")
        branch = str(data.get("branch") or "main")
        if chosen == open_chat:
            self._open_chat(session_id, branch)
        elif chosen == delete:
            self._delete_item(item)

    def _delete_sessions(self, session_ids):
        ids = [str(value) for value in (session_ids or []) if str(value)]
        if not ids:
            return
        reply = QMessageBox.question(
            self,
            tr('ui.sessions_widget.delete_sessions_10e9'),
            tr('ui.projects_view.permanently_delete_selected_sessions_v0_and_f255', v0=len(ids)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        active_was_deleted = self.active_session_id in set(ids)

        def action():
            for session_id in ids:
                result = self.session_manager.delete_session(session_id)
                if isinstance(result, dict) and result.get("success") is False:
                    return result
            return {"success": True}

        def done(_result):
            if active_was_deleted:
                self.active_session_id = None
                self.active_branch = "main"
                event_bus.new_chat_requested.emit()

        self._run_session_action(action, done)

    def _delete_item(self, item: QTreeWidgetItem):
        data = item.data(0, Qt.UserRole) or {}
        session_id = str(data.get("session_id") or "")
        item_type = data.get("type")

        if item_type == "session":
            reply = QMessageBox.question(
                self,
                tr('ui.sessions_widget.delete_session_52a9'),
                tr('ui.sessions_widget.permanently_delete_session_v0_and_all_59c3', v0=session_id),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                def session_deleted(_result):
                    if self.active_session_id == session_id:
                        self.active_session_id = None
                        self.active_branch = "main"
                        event_bus.new_chat_requested.emit()

                self._run_session_action(
                    lambda: self.session_manager.delete_session(session_id),
                    session_deleted,
                )
            return

        if item_type == "branch":
            branch = str(data.get("branch") or "main")
            reply = QMessageBox.question(
                self,
                tr('ui.sessions_widget.delete_branch_635e'),
                tr('ui.sessions_widget.delete_branch_v0_in_session_v1_3a91', v0=branch, v1=session_id),
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                def branch_deleted(_result):
                    if self.active_session_id == session_id and self.active_branch == branch:
                        self.active_branch = "main"
                        event_bus.session_selected.emit(session_id, "main")

                self._run_session_action(
                    lambda: self.session_manager.delete_branch(session_id, branch),
                    branch_deleted,
                )
