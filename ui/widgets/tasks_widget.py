from __future__ import annotations
from ui.icons import set_icon
from ui.i18n import tr, localize, set_text, add_item
from ui.styles.binding import apply_style


from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)
from ui.widgets.dialogs import MessageBox as QMessageBox

from ui.app_state import app_state
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.session_client import SessionApiClient
from ui.event_bus import event_bus


class TasksWidget(QWidget):
    """Compact editable projection of the active session task list."""

    PROJECTS_VIEW_INDEX = 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TasksWidgetRoot")
        self.client = SessionApiClient(base_url=core_process.base_url)
        self.runner = AsyncRunner(self)
        self.session_id = str(app_state.session_id or "")
        self.branch = str(app_state.branch or "main")
        self.tasks: List[Dict[str, Any]] = []
        self._projects_space_active = False
        self._refresh_in_flight = False
        self._refresh_pending = False

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        controls = QHBoxLayout()
        self.add_btn = set_icon(QPushButton('', self), 'add', square=True)
        self.add_btn.setObjectName("TaskIconBtn")
        self.add_btn.setFixedSize(28, 28)
        set_text(self.add_btn, 'ui.projects_view.add_task_5a9b', setter='setToolTip')
        self.send_btn = localize(QPushButton('', self), 'ui.tasks_widget.send_to_model_9011')
        set_text(self.send_btn, 'ui.tasks_widget.send_in_progress_tasks_to_the_4c36', setter='setToolTip')
        self.check_btn = localize(QPushButton('', self), 'ui.tasks_widget.update_statuses_4919')
        set_text(self.check_btn, 'ui.tasks_widget.ask_the_model_to_review_in_7541', setter='setToolTip')
        controls.addWidget(self.add_btn)
        controls.addWidget(self.send_btn)
        controls.addWidget(self.check_btn)
        root.addLayout(controls)

        self.stack = QStackedWidget(self)
        root.addWidget(self.stack, 1)

        self.scroll = QScrollArea(self.stack)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget(self.scroll)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 2, 2, 2)
        self.body_layout.setSpacing(6)
        self.scroll.setWidget(self.body)
        self.stack.addWidget(self.scroll)

        self.inactive_label = localize(QLabel('', self.stack), 'ui.sessions_widget.not_active_in_projects_space_3458')
        self.inactive_label.setAlignment(Qt.AlignCenter)
        self.inactive_label.setWordWrap(True)
        apply_style(self.inactive_label, "color:@text.disabled@; padding:16px;")
        self.stack.addWidget(self.inactive_label)

        self.add_btn.clicked.connect(self._add_task)
        self.send_btn.clicked.connect(self._send_tasks)
        self.check_btn.clicked.connect(self._check_tasks)

        app_state.session_changed.connect(self._on_session_changed)
        event_bus.sessions_updated.connect(self.refresh)
        event_bus.workspace_view_changed.connect(self._on_workspace_view_changed)
        core_process.status_changed.connect(lambda online, _msg: self.refresh() if online else None)

        self._sync_controls()
        self.refresh()

    def _on_workspace_view_changed(self, index: int):
        active = int(index) == self.PROJECTS_VIEW_INDEX
        if self._projects_space_active == active:
            return
        self._projects_space_active = active
        self.stack.setCurrentWidget(self.inactive_label if active else self.scroll)
        self.add_btn.setVisible(not active)
        self.send_btn.setVisible(not active)
        self.check_btn.setVisible(not active)
        if not active:
            self.refresh()

    def _on_session_changed(self, session_id: str, branch: str):
        new_session = str(session_id or "")
        new_branch = str(branch or "main")
        if new_session == self.session_id and new_branch == self.branch:
            return
        self.session_id = new_session
        self.branch = new_branch
        self.tasks = []
        self._render()
        self.refresh()

    def _clear_rows(self):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Refreshing the projection is not a user edit. In particular,
                # do not let QLineEdit.editingFinished re-submit the old task.
                widget.blockSignals(True)
                for child in widget.findChildren(QWidget):
                    child.blockSignals(True)
                widget.setParent(None)
                widget.deleteLater()

    def _sync_controls(self):
        active = [task for task in self.tasks if str(task.get("status") or "") == "in_progress"]
        has_session = bool(self.session_id) and not self._projects_space_active
        self.add_btn.setEnabled(has_session)
        self.send_btn.setEnabled(has_session and bool(active))
        self.check_btn.setEnabled(has_session and bool(active))

    def refresh(self):
        if self._projects_space_active:
            return
        if self._refresh_in_flight:
            self._refresh_pending = True
            return
        if not self.session_id or not core_process.is_running():
            if self.tasks or self.session_id:
                self.tasks = []
                self._render()
            else:
                self._render()
            return

        target_session = self.session_id
        target_branch = self.branch
        self._refresh_in_flight = True

        def done(payload):
            self._refresh_in_flight = False
            refresh_again = self._refresh_pending
            self._refresh_pending = False
            if (target_session, target_branch) != (self.session_id, self.branch):
                self.refresh()
                return
            new_tasks = [dict(task) for task in (payload or {}).get("tasks", [])]
            if new_tasks != self.tasks:
                self.tasks = new_tasks
                self._render()
            else:
                self._sync_controls()
            if refresh_again:
                self.refresh()

        def failed(_error):
            self._refresh_in_flight = False
            refresh_again = self._refresh_pending
            self._refresh_pending = False
            if (target_session, target_branch) != (self.session_id, self.branch):
                self.refresh()
                return
            self.tasks = []
            self._render()
            if refresh_again:
                self.refresh()

        self.runner.submit(lambda: self.client.get_tasks(target_session, target_branch), done, failed)

    def _render(self, error: str = ""):
        self._clear_rows()
        self._sync_controls()
        if error:
            label = QLabel(error, self.body)
            label.setWordWrap(True)
            label.setStyleSheet("color:#ef4444;")
            self.body_layout.addWidget(label)
            self.body_layout.addStretch(1)
            return
        if not self.tasks:
            label = localize(QLabel('', self.body), 'ui.projects_view.the_task_list_is_empty_96f1')
            label.setAlignment(Qt.AlignCenter)
            apply_style(label, "color:@text.disabled@; padding:12px;")
            self.body_layout.addWidget(label)
            self.body_layout.addStretch(1)
            return

        for task in self.tasks:
            task_id = str(task.get("id") or "")
            row = QFrame(self.body)
            row.setObjectName("SidebarTaskRow")
            row.setAttribute(Qt.WA_StyledBackground)
            apply_style(row, '''
                QFrame#SidebarTaskRow {background:@background.surface@;border:0;border-radius:6px;}
                QFrame#SidebarTaskRow:hover {background:@background.surface@;}
                QFrame#SidebarTaskRow QLabel {background:transparent;border:0;}
            ''')
            layout = QVBoxLayout(row)
            layout.setContentsMargins(5, 5, 5, 5)
            layout.setSpacing(5)
            header = QHBoxLayout()
            header.setSpacing(5)

            id_label = QLabel(task_id, row)
            apply_style(id_label, "color:@text.disabled@; font-size:10px;")
            id_label.setMinimumWidth(32)
            text_edit = QLineEdit(str(task.get("text") or "").replace("\u200b", ""), row)
            text_edit.setCursorPosition(0)
            text_edit.deselect()
            status = QComboBox(row)
            add_item(status, 'ui.projects_view.in_progress_59a2', "in_progress")
            add_item(status, 'ui.projects_view.completed_b459', "completed")
            status.setCurrentIndex(max(0, status.findData(
                "completed" if str(task.get("status") or "") == "completed" else "in_progress"
            )))
            send_btn = set_icon(QPushButton('', row), 'send', square=True)
            send_btn.setObjectName('TaskIconBtn')
            send_btn.setFixedSize(28, 28)
            set_text(send_btn, 'ui.tasks_widget.send_one', setter='setToolTip')
            send_btn.setEnabled(str(task.get('status') or '') == 'in_progress')
            delete_btn = set_icon(QPushButton('', row), 'trash', square=True)
            delete_btn.setObjectName('TaskIconBtn')
            delete_btn.setFixedSize(28, 28)
            set_text(delete_btn, 'ui.file_manager_widget.delete_permanently_abac', setter='setToolTip')

            header.addWidget(id_label)
            header.addWidget(status, 1)
            header.addWidget(send_btn)
            header.addWidget(delete_btn)
            layout.addLayout(header)
            layout.addWidget(text_edit)
            send_btn.clicked.connect(lambda _checked=False, tid=task_id, edit=text_edit: self._send_task(tid, edit.text()))
            self.body_layout.addWidget(row)

            original_text = str(task.get("text") or "").replace("\u200b", "").strip()
            text_edit.editingFinished.connect(
                lambda edit=text_edit, tid=task_id, original=original_text: (
                    self._save_text(tid, edit.text())
                    if edit.text().strip() != original else None
                )
            )
            status.currentIndexChanged.connect(
                lambda _idx, combo=status, tid=task_id: self._update_task(
                    tid, {"status": str(combo.currentData() or "in_progress")}
                )
            )
            delete_btn.clicked.connect(
                lambda _checked=False, tid=task_id: self._delete_task(tid)
            )
        self.body_layout.addStretch(1)

    def _submit(self, action):
        def done(result):
            if isinstance(result, dict) and result.get("success") is False:
                QMessageBox.warning(self, tr('ui.projects_view.tasks_03cc'), str(result.get("message") or tr('ui.caption.2697cfe09ee1')))
                return
            event_bus.sessions_updated.emit()

        self.runner.submit(
            action,
            done,
            lambda error: QMessageBox.warning(self, tr('ui.projects_view.tasks_03cc'), str(error)),
        )

    def _add_task(self):
        if not self.session_id:
            return
        self._submit(lambda sid=self.session_id, branch=self.branch: self.client.create_empty_task(sid, branch))

    def _save_text(self, task_id: str, text: str):
        self._update_task(task_id, {"text": str(text or "").strip()})

    def _update_task(self, task_id: str, updates: Dict[str, Any]):
        if self.session_id:
            self._submit(lambda sid=self.session_id, branch=self.branch: self.client.update_task(sid, task_id, branch=branch, **dict(updates or {})))

    def _delete_task(self, task_id: str):
        if not self.session_id:
            return
        if QMessageBox.question(self, tr('ui.projects_view.tasks_03cc'), tr('ui.projects_view.delete_task_v0_9365', v0=task_id)) != QMessageBox.Yes:
            return
        self._submit(lambda sid=self.session_id, branch=self.branch: self.client.delete_task(sid, task_id, branch))

    def _active_tasks(self) -> List[Dict[str, Any]]:
        return [dict(task) for task in self.tasks if str(task.get("status") or "") == "in_progress"]

    def _send_task(self, task_id, text):
        task = next((dict(item) for item in self.tasks if str(item.get('id') or '') == task_id), None)
        if task is not None:
            task['text'] = str(text or '').replace('\u200b', '').strip()
        if self.session_id and task and task.get('status') == 'in_progress' and task['text']:
            event_bus.task_list_send_requested.emit(self.session_id, self.branch or 'main', [task])

    def _send_tasks(self):
        tasks = self._active_tasks()
        if self.session_id and tasks:
            event_bus.task_list_send_requested.emit(self.session_id, self.branch or "main", tasks)

    def _check_tasks(self):
        tasks = self._active_tasks()
        if not self.session_id or not tasks:
            return
        if QMessageBox.question(
            self,
            tr('ui.projects_view.update_task_statuses_0c42'),
            tr('ui.projects_view.ask_the_model_to_review_v0_f7d7', v0=len(tasks)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        event_bus.task_status_check_requested.emit(self.session_id, self.branch or "main", tasks)
