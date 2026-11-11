from __future__ import annotations
from ui.themes.manager import theme_manager
from ui.icons import get as ui_icon
from ui.i18n import add_caption_action, bind_caption, add_caption_tab, tr, localize, set_text, add_item, add_row
from ui.icons import set_icon
from ui.styles.binding import apply_style


from datetime import datetime
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QTimer, QRect
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QPlainTextEdit, QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QStyle, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QToolButton)
from ui.widgets.dialogs import InputDialog as QInputDialog
from ui.widgets.dialogs import MessageBox as QMessageBox

from ui.core_bridge import SessionApiClient, core_process
from ui.core_bridge.async_runner import AsyncRunner
from ui.event_bus import event_bus
from ui.widgets.passive_splitter import PassiveSplitter as QSplitter
from shiboken6 import isValid
from ui.qt_deferred import defer
from ui.widgets.memory_mode_dialog import (
    MEMORY_MODE_SWITCH_WARNING,
    choose_memory_mode,
    memory_mode_label,
)


_AUTO_PARENT = object()


from ui.widgets.passive_splitter import PassiveSplitter as QSplitter

class ProjectTree(QTreeWidget):
    move_requested = Signal(str, str, object)  # item_type, entity_id, target_folder_id|None
    sessions_move_requested = Signal(object, object)  # session_ids, target_folder_id|None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProjectsTree")
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

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
        target_folder_id = None
        if target is not None:
            target_data = target.data(0, Qt.UserRole) or {}
            if target_data.get("type") == "folder":
                target_folder_id = target_data.get("id")
            else:
                parent = target.parent()
                if parent is not None:
                    parent_data = parent.data(0, Qt.UserRole) or {}
                    if parent_data.get("type") == "folder":
                        target_folder_id = parent_data.get("id")

        if item_type == "session" and not target_folder_id:
            event.ignore()
            return

        selected_sessions = []
        for selected_item in self.selectedItems():
            selected_data = selected_item.data(0, Qt.UserRole) or {}
            if selected_data.get("type") == "session":
                session_id = str(selected_data.get("session_id") or selected_data.get("id") or "")
                if session_id:
                    selected_sessions.append(session_id)
        if item_type == "session" and len(selected_sessions) > 1:
            self.sessions_move_requested.emit(list(dict.fromkeys(selected_sessions)), target_folder_id)
        else:
            self.move_requested.emit(item_type, str(entity_id), target_folder_id)
        event.acceptProposedAction()


class ModuleSettingsBox(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.inherit = localize(QCheckBox('', self), 'ui.projects_view.inherit_module_selection_edeb')
        self.inherit.toggled.connect(self._sync_enabled)
        layout.addWidget(self.inherit)

        self.list = QListWidget(self)
        self.list.setObjectName("ProjectModulesList")
        self.list.setMinimumHeight(130)
        layout.addWidget(self.list)
        self._available: List[Dict[str, Any]] = []

    def set_value(self, available: List[Dict[str, Any]], value: Optional[List[str]], effective: Optional[List[str]] = None):
        self._available = list(available or [])
        self.list.clear()
        inherited = value is None
        self.inherit.setChecked(inherited)
        checked = set((effective if inherited else value) or [])

        for module in sorted(self._available, key=lambda item: (str(item.get("category", "")), str(item.get("name", "")))):
            name = str(module.get("name") or "")
            if not name:
                continue
            enabled = bool(module.get("enabled", False))
            label = name if enabled else tr('ui.caption.0932b61a7cb2', v0=name)
            item = QListWidgetItem(label, self.list)
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in checked else Qt.Unchecked)
            if not enabled:
                item.setForeground(QBrush(theme_manager.color('text.disabled')))
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
        self._sync_enabled()

    def _sync_enabled(self):
        self.list.setEnabled(not self.inherit.isChecked())

    def value(self) -> Optional[List[str]]:
        if self.inherit.isChecked():
            return None
        result = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.checkState() == Qt.Checked:
                result.append(str(item.data(Qt.UserRole)))
        return result


class SettingsEditor(QWidget):
    save_requested = Signal(dict)

    def __init__(
        self,
        prompt_help: str,
        parent=None,
        *,
        show_memory_mode: bool = False,
        show_prompt_inheritance: bool = False,
        show_store_both_formats: bool = False,
        show_thoughts_in_context: bool = False,
        allow_memory_mode_inherit: bool = False,
        allow_store_both_inherit: bool = False,
        show_workspace: bool = True,
    ):
        super().__init__(parent)
        self._show_memory_mode = bool(show_memory_mode)
        self._show_prompt_inheritance = bool(show_prompt_inheritance)
        self._show_store_both_formats = bool(show_store_both_formats)
        self._show_thoughts_in_context = bool(show_thoughts_in_context)
        self._allow_memory_mode_inherit = bool(allow_memory_mode_inherit)
        self._allow_store_both_inherit = bool(allow_store_both_inherit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit(self)
        add_row(form, 'ui.subagents_panel.name_0918', self.name_edit)

        self.memory_mode_combo = QComboBox(self)
        if self._allow_memory_mode_inherit:
            add_item(self.memory_mode_combo, 'ui.projects_view.inherit_from_parent_session_22bd', None)
        add_item(self.memory_mode_combo, 'ui.projects_view.pages_text_memory_9567', "text")
        add_item(self.memory_mode_combo, 'ui.projects_view.vectors_embeddings_4859', "vector")
        self.memory_mode_combo.setVisible(self._show_memory_mode)
        self.memory_mode_label = localize(QLabel('', self), 'ui.projects_view.memory_type_3e05')
        self.memory_mode_label.setVisible(self._show_memory_mode)
        if self._show_memory_mode:
            form.addRow(self.memory_mode_label, self.memory_mode_combo)

        self.store_both_inherit = localize(QCheckBox('', self), 'ui.projects_view.inherit_dual_storage_setting_cb01')
        self.store_both_inherit.setVisible(self._show_store_both_formats and self._allow_store_both_inherit)
        self.store_both_inherit.toggled.connect(
            lambda checked: self.store_both_formats.setEnabled(not checked)
        )
        if self._show_store_both_formats and self._allow_store_both_inherit:
            form.addRow("", self.store_both_inherit)

        self.store_both_formats = localize(QCheckBox(
            '',
            self,
        ), 'ui.projects_view.store_temporary_memory_uploads_in_both_7b73')
        self.store_both_formats.setChecked(False)
        self.store_both_formats.setVisible(self._show_store_both_formats)
        if self._show_store_both_formats:
            form.addRow("", self.store_both_formats)

        self.thoughts_in_context = localize(QCheckBox('', self), 'ui.chat_view.include_model_thoughts_in_context_8f9a')
        self.thoughts_in_context.setChecked(False)
        set_text(self.thoughts_in_context, 'ui.projects_view.session_wide_setting_changes_model_history_ae81', setter='setToolTip')
        self.thoughts_in_context.setVisible(self._show_thoughts_in_context)
        if self._show_thoughts_in_context:
            form.addRow("", self.thoughts_in_context)

        self.replied_thoughts_in_context = localize(QCheckBox('', self), 'ui.chat_view.include_thoughts_from_the_model_message_c577')
        set_text(self.replied_thoughts_in_context, 'ui.chat_view.when_general_thought_inclusion_is_disabled_2a44', setter='setToolTip')
        self.replied_thoughts_in_context.setVisible(self._show_thoughts_in_context)
        self.replied_thoughts_in_context.setEnabled(not self.thoughts_in_context.isChecked())
        self.thoughts_in_context.toggled.connect(lambda checked: self.replied_thoughts_in_context.setEnabled(not checked))
        if self._show_thoughts_in_context:
            form.addRow("", self.replied_thoughts_in_context)

        self.prompt_edit = QPlainTextEdit(self)
        self.prompt_edit.setMinimumHeight(150)
        set_text(self.prompt_edit, 'ui.projects_view.additional_system_prompt_2800', setter='setPlaceholderText')
        add_row(form, 'ui.module_view.system_prompt_2074', self.prompt_edit)

        self.inherit_prompt = localize(QCheckBox(
            '',
            self,
        ), 'ui.projects_view.inherit_the_system_prompt_in_addition_f26f')
        self.inherit_prompt.setChecked(True)
        self.inherit_prompt.setVisible(self._show_prompt_inheritance)
        if self._show_prompt_inheritance:
            form.addRow("", self.inherit_prompt)
        layout.addLayout(form)

        self.memory_warning = QLabel(MEMORY_MODE_SWITCH_WARNING, self)
        self.memory_warning.setWordWrap(True)
        self.memory_warning.setStyleSheet("color:#d6a82d; font-size:11px;")
        self.memory_warning.setVisible(self._show_memory_mode)
        if self._show_memory_mode:
            layout.addWidget(self.memory_warning)

        help_label = QLabel(prompt_help, self)
        help_label.setWordWrap(True)
        help_label.setObjectName("ProjectSettingsHint")
        apply_style(help_label, "color:@text.secondary@; font-size:11px;")
        layout.addWidget(help_label)

        self.workspace_box = QWidget(self)
        workspace_row = QHBoxLayout(self.workspace_box)
        workspace_row.setContentsMargins(0, 0, 0, 0)
        self.workspace_edit = QLineEdit(self)
        set_text(self.workspace_edit, 'ui.projects_view.empty_inherit_use_the_current_project_1628', setter='setPlaceholderText')
        self.workspace_browse = set_icon(QPushButton('', self), 'more', square=True)
        self.workspace_browse.setFixedWidth(34)
        self.workspace_browse.clicked.connect(self._browse_workspace)
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(self.workspace_browse)
        self.workspace_label = localize(QLabel('', self), 'ui.chat_attachments.project_folder_31bf')
        layout.addWidget(self.workspace_label)
        layout.addWidget(self.workspace_box)
        self.set_workspace_editable(show_workspace)

        self.modules_label = localize(QLabel('', self), 'ui.projects_view.modules_2891')
        layout.addWidget(self.modules_label)
        self.modules_box = ModuleSettingsBox(self)
        layout.addWidget(self.modules_box)

        self.save_btn = localize(QPushButton('', self), 'ui.settings.save_b4d3')
        self.save_btn.clicked.connect(self._emit_save)
        layout.addWidget(self.save_btn, 0, Qt.AlignRight)
        layout.addStretch(1)

    def set_workspace_editable(self, enabled):
        self._workspace_editable = enabled
        self.workspace_box.setVisible(enabled)
        self.workspace_label.setVisible(enabled)

    def set_modules_editable(self, enabled):
        self._modules_editable = enabled
        self.modules_label.setVisible(enabled)
        self.modules_box.setVisible(enabled)

    def _browse_workspace(self):
        start = self.workspace_edit.text().strip()
        path = QFileDialog.getExistingDirectory(self, "Выберите Project Folder", start or "")
        if path:
            bind_caption(self.workspace_edit, path)

    def _emit_save(self):
        settings = {
            "system_prompt": self.prompt_edit.toPlainText(),
            "modules": self.modules_box.value(),
        }
        if not getattr(self, "_modules_editable", True):
            settings.pop("modules", None)
        if self._workspace_editable:
            settings["workspace_path"] = self.workspace_edit.text().strip()
        if self._show_prompt_inheritance:
            settings["inherit_system_prompt"] = self.inherit_prompt.isChecked()
        if self._show_store_both_formats:
            if self._allow_store_both_inherit and self.store_both_inherit.isChecked():
                settings["store_temp_memory_both"] = None
            else:
                settings["store_temp_memory_both"] = self.store_both_formats.isChecked()

        payload = {
            "name": self.name_edit.text().strip(),
            "settings": settings,
        }
        if self._show_memory_mode:
            current_mode = self.memory_mode_combo.currentData()
            payload["memory_mode"] = (
                None if self._allow_memory_mode_inherit and current_mode is None
                else str(current_mode or "text")
            )
        if self._show_thoughts_in_context:
            payload["include_thoughts_in_context"] = self.thoughts_in_context.isChecked()
            payload["include_replied_thoughts_in_context"] = self.replied_thoughts_in_context.isChecked()
        self.save_requested.emit(payload)

    def load(
        self,
        name: str,
        settings: Dict[str, Any],
        modules: List[Dict[str, Any]],
        effective_modules=None,
        memory_mode: Optional[str] = None,
        effective_store_both: bool = False,
        include_thoughts_in_context: bool = True,
        include_replied_thoughts_in_context: bool = False,
    ):
        bind_caption(self.name_edit, name or "")
        self.prompt_edit.setPlainText(str(settings.get("system_prompt") or ""))
        if self._show_prompt_inheritance:
            self.inherit_prompt.setChecked(bool(settings.get("inherit_system_prompt", True)))
        if self._show_memory_mode:
            raw_mode = settings.get("memory_mode", None) if self._allow_memory_mode_inherit else memory_mode
            if self._allow_memory_mode_inherit and raw_mode is None:
                index = self.memory_mode_combo.findData(None)
            else:
                resolved_mode = "vector" if str(raw_mode or memory_mode or "").lower() == "vector" else "text"
                index = self.memory_mode_combo.findData(resolved_mode)
            self.memory_mode_combo.setCurrentIndex(max(0, index))
            if self._allow_memory_mode_inherit:
                set_text(self.memory_mode_combo, 'ui.projects_view.effective_mode_v0_7c58', setter='setToolTip', v0=memory_mode_label(str(memory_mode or 'text')))
        if self._show_store_both_formats:
            stored_value = settings.get("store_temp_memory_both", None)
            if self._allow_store_both_inherit:
                inherited = stored_value is None
                self.store_both_inherit.setChecked(inherited)
                self.store_both_formats.setEnabled(not inherited)
            self.store_both_formats.setChecked(
                bool(effective_store_both) if stored_value is None else bool(stored_value)
            )
        if self._show_thoughts_in_context:
            self.thoughts_in_context.setChecked(bool(include_thoughts_in_context))
            self.replied_thoughts_in_context.setChecked(bool(include_replied_thoughts_in_context))
        bind_caption(self.workspace_edit, str(settings.get("workspace_path") or ""))
        self.modules_box.set_value(modules, settings.get("modules"), effective_modules)


class TaskTextEdit(QPlainTextEdit):
    """Multiline task editor that follows the height of the actually wrapped text."""

    editingFinished = Signal()
    _MAX_HEIGHT = 180

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._height_sync_pending = False
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setPlainText(str(text or ""))
        self.textChanged.connect(self._schedule_height_sync)
        QTimer.singleShot(0, self._sync_height)

    def _schedule_height_sync(self):
        if self._height_sync_pending:
            return
        self._height_sync_pending = True
        QTimer.singleShot(0, self._sync_height)

    def _sync_height(self):
        self._height_sync_pending = False

        # QPlainTextDocumentLayout.documentSize() is not reliable here: with
        # WidgetWidth wrapping it can keep reporting essentially a one-line
        # height even though the viewport already contains several visual lines.
        # Measure the text against the real viewport width instead.
        metrics = self.fontMetrics()
        available_width = max(32, self.viewport().width() - 4)
        text = self.toPlainText() or " "
        measured = metrics.boundingRect(
            QRect(0, 0, available_width, 100000),
            Qt.TextWordWrap | Qt.TextExpandTabs,
            text,
        )

        margins = self.contentsMargins()
        document_margin = int(round(float(self.document().documentMargin())))
        chrome = (
            margins.top() + margins.bottom()
            + self.frameWidth() * 2
            + document_margin * 2
            + 4
        )
        one_line = metrics.lineSpacing() + chrome
        wanted = max(one_line, measured.height() + chrome)
        limited = min(self._MAX_HEIGHT, wanted)

        if self.height() != limited:
            self.setFixedHeight(limited)
            self.updateGeometry()
            parent = self.parentWidget()
            if parent is not None:
                parent.updateGeometry()

        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if wanted > self._MAX_HEIGHT else Qt.ScrollBarAlwaysOff
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A width change can add/remove wrapped visual lines even when the text
        # itself did not change. Recalculate after Qt has applied the new width.
        self._schedule_height_sync()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editingFinished.emit()


class SessionOverview(QScrollArea):
    open_chat_requested = Signal(str)
    task_add_requested = Signal(str, str)
    task_update_requested = Signal(str, str, object)
    task_delete_requested = Signal(str, str)
    task_status_check_requested = Signal(str, str, object)
    task_list_send_requested = Signal(str, str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.body = QWidget(self)
        self.layout = QVBoxLayout(self.body)
        self.layout.setContentsMargins(5, 5, 7, 5)
        self.layout.setSpacing(12)
        self.setWidget(self.body)
        self._session_id = ""
        self._active_branch = "main"
        self._loaded_signature = None

    @staticmethod
    def _dispose_layout(layout):
        """Detach an old dynamic UI tree without letting teardown signals mutate tasks."""
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            if child_layout is not None:
                SessionOverview._dispose_layout(child_layout)
                child_layout.setParent(None)
                child_layout.deleteLater()
                continue
            widget = item.widget()
            if widget is not None:
                # QLineEdit.editingFinished / QComboBox signals must not fire while
                # a refresh is merely destroying the previous projection.
                widget.blockSignals(True)
                for child in widget.findChildren(QWidget):
                    child.blockSignals(True)
                widget.setParent(None)
                widget.deleteLater()

    def _clear(self):
        self._dispose_layout(self.layout)

    def _section(self, title: str, value: Any):
        title_label = bind_caption(QLabel('', self.body), title)
        apply_style(title_label, "font-weight:600; color:@text.primary@;")
        self.layout.addWidget(title_label)
        if isinstance(value, list):
            if not value:
                value = ["—"]
            for entry in value:
                if isinstance(entry, dict):
                    status = str(entry.get("status") or "").strip()
                    text = str(entry.get("title") or entry.get("text") or entry)
                    prefix = f"[{status}] " if status else ""
                    line = bind_caption(QLabel('', self.body), f"• {prefix}{text}")
                else:
                    line = bind_caption(QLabel('', self.body), f"• {entry}")
                line.setWordWrap(True)
                line.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self.layout.addWidget(line)
        else:
            label = QLabel(str(value or "—"), self.body)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.layout.addWidget(label)

    def _render_tasks(self, tasks: List[Dict[str, Any]]):
        header = QHBoxLayout()
        title = localize(QLabel('', self.body), 'ui.projects_view.tasks_03cc')
        apply_style(title, "font-weight:600; color:@text.primary@;")
        add_btn = set_icon(QPushButton('', self.body), 'add', square=True)
        add_btn.setFixedWidth(34)
        set_text(add_btn, 'ui.projects_view.add_task_5a9b', setter='setToolTip')
        send_btn = localize(QPushButton('', self.body), 'ui.projects_view.send_task_list_to_the_model_abcf')
        update_btn = localize(QPushButton('', self.body), 'ui.projects_view.update_task_statuses_0c42')
        active_tasks = [dict(task) for task in tasks if str(task.get("status") or "") == "in_progress"]
        send_btn.setEnabled(bool(active_tasks))
        set_text(send_btn, 'ui.projects_view.send_the_current_in_progress_tasks_562e', setter='setToolTip')
        update_btn.setEnabled(bool(active_tasks))
        set_text(update_btn, 'ui.projects_view.ask_the_model_to_review_unfinished_e692', setter='setToolTip')
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(add_btn)
        header.addWidget(send_btn)
        header.addWidget(update_btn)
        self.layout.addLayout(header)

        add_btn.clicked.connect(
            lambda _checked=False: self.task_add_requested.emit(self._session_id, "")
        )
        send_btn.clicked.connect(
            lambda _checked=False, items=active_tasks: self.task_list_send_requested.emit(
                self._session_id, self._active_branch, items
            )
        )
        update_btn.clicked.connect(
            lambda _checked=False, items=active_tasks: self.task_status_check_requested.emit(
                self._session_id, self._active_branch, items
            )
        )

        if not tasks:
            empty = localize(QLabel('', self.body), 'ui.projects_view.the_task_list_is_empty_96f1')
            apply_style(empty, "color:@text.disabled@;")
            self.layout.addWidget(empty)
            return

        for task in tasks:
            task_id = str(task.get("id") or "")
            row = QFrame(self.body)
            row.setObjectName("ProjectTaskRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setSpacing(8)

            id_label = QLabel(task_id, row)
            apply_style(id_label, "color:@text.disabled@; font-size:11px;")
            id_label.setMinimumWidth(62)
            text_edit = TaskTextEdit(str(task.get("text") or "").replace("\u200b", ""), row)
            status_combo = QComboBox(row)
            add_item(status_combo, 'ui.projects_view.in_progress_59a2', "in_progress")
            add_item(status_combo, 'ui.projects_view.completed_b459', "completed")
            status_index = status_combo.findData(
                "completed" if str(task.get("status") or "") == "completed" else "in_progress"
            )
            status_combo.setCurrentIndex(max(0, status_index))
            delete_btn = set_icon(QPushButton('', row), 'close', square=True)
            delete_btn.setFixedWidth(32)
            set_text(delete_btn, 'ui.projects_view.delete_task_2462', setter='setToolTip')

            row_layout.addWidget(id_label)
            row_layout.addWidget(text_edit, 1)
            row_layout.addWidget(status_combo)
            row_layout.addWidget(delete_btn)
            self.layout.addWidget(row)

            original_text = str(task.get("text") or "").replace("\u200b", "").strip()

            def save_text(edit=text_edit, tid=task_id, original=original_text):
                value = edit.toPlainText().strip()
                if value != original:
                    self.task_update_requested.emit(self._session_id, tid, {"text": value})

            text_edit.editingFinished.connect(save_text)
            status_combo.currentIndexChanged.connect(
                lambda _idx, combo=status_combo, tid=task_id: self.task_update_requested.emit(
                    self._session_id, tid, {"status": str(combo.currentData() or "in_progress")}
                )
            )
            delete_btn.clicked.connect(
                lambda _checked=False, tid=task_id: self.task_delete_requested.emit(self._session_id, tid)
            )

    def load(self, session: Dict[str, Any], branch: Optional[Dict[str, Any]] = None):
        session_id = str(session.get("session_id") or "")
        branch_info = dict(branch or {})
        active_branch = str(branch_info.get("name") or session.get("active_branch") or "main")
        memory_mode = str(
            branch_info.get("memory_mode")
            or session.get("memory_mode")
            or "text"
        )
        updated_raw = str(branch_info.get("updated_at") or session.get("updated_at") or "")
        message_count = (
            branch_info.get("messages_count")
            if branch_info
            else session.get("message_count", 0)
        )
        digest = session.get("digest") if isinstance(session.get("digest"), dict) else {}
        summary = digest.get("summary") or tr('ui.caption.84486469f13c')
        tasks = list(session.get("tasks") or [])
        recent = branch_info.get("recent_user_messages") if branch_info else session.get("recent_user_messages")
        recent_rows = list(recent or [])[-3:]

        # Projects can receive refreshes merely because the workspace became
        # visible. Do not tear down and recreate the dynamic description pane
        # when the actual projection shown to the user has not changed.
        signature = (
            session_id,
            session.get('title'), branch_info.get('title'),
            active_branch,
            memory_mode,
            updated_raw,
            int(message_count or 0),
            str(summary),
            tuple(
                (
                    str(task.get("id") or ""),
                    str(task.get("text") or ""),
                    str(task.get("status") or ""),
                    str(task.get("updated_at") or ""),
                )
                for task in tasks
                if isinstance(task, dict)
            ),
            tuple(str(item) for item in recent_rows),
        )
        if signature == self._loaded_signature:
            return

        self._clear()
        self._session_id = session_id
        self._active_branch = active_branch

        heading_text = str(session.get('title') or self._session_id)
        if branch_info and self._active_branch != "main":
            heading_text += f" / {branch_info.get('title') or self._active_branch}"
        heading = QLabel(heading_text, self.body)
        heading.setStyleSheet("font-size:18px; font-weight:700;")
        self.layout.addWidget(heading)

        memory = localize(QLabel(
            '',
            self.body,
        ), 'ui.projects_view.memory_v0_8ed0', v0=memory_mode_label(memory_mode))
        apply_style(memory, "color:@text.secondary@;")
        self.layout.addWidget(memory)

        try:
            updated = datetime.fromisoformat(updated_raw).strftime("%Y-%m-%d %H:%M") if updated_raw else "—"
        except Exception:
            updated = updated_raw or "—"
        meta = localize(QLabel(
            '',
            self.body,
        ), 'ui.projects_view.messages_v0_last_change_v1_cc2d', v0=message_count or 0, v1=updated)
        apply_style(meta, "color:@text.secondary@;")
        self.layout.addWidget(meta)

        self._section(tr('ui.caption.78eedb35fc1a'), summary)
        self._render_tasks(tasks)
        # Defensive UI cap as well as the backend cap: old snapshots / an old
        # still-running core must never make this section render more than 3 rows.
        self._section(tr('ui.caption.760595fd6272'), recent_rows)
        self.layout.addStretch(1)
        self._loaded_signature = signature


class ProjectsView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProjectsView")
        self.client = SessionApiClient(base_url=core_process.base_url)
        self.async_runner = AsyncRunner(self)
        self.snapshot: Dict[str, Any] = {"folders": [], "sessions": []}
        self.modules: List[Dict[str, Any]] = []
        self._rebuilding = False
        self._refresh_in_flight = False
        self._refresh_pending = False
        self._modules_in_flight = False
        self._modules_loaded = False
        self._initial_snapshot_loaded = False
        self._selected_type: Optional[str] = None
        self._selected_id: Optional[str] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter)

        left = QWidget(splitter)
        left.setMinimumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(3, 3, 3, 3)
        left_layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self.new_folder_btn = set_icon(QToolButton(left),'folder-open',size=18)
        set_text(self.new_folder_btn,'ui.sessions_widget.create_project_49ed',setter='setToolTip')
        self.new_child_folder_btn = set_icon(QToolButton(left),'folder-plus',size=18)
        set_text(self.new_child_folder_btn,'ui.sessions_widget.create_folder_in_the_selected_project_9b19',setter='setToolTip')
        self.new_session_btn = set_icon(QToolButton(left),'file',size=18)
        set_text(self.new_session_btn,'ui.sessions_widget.create_new_session_9da1',setter='setToolTip')
        self.refresh_btn = set_icon(QToolButton(left),'refresh',size=18)
        toolbar.addWidget(self.new_folder_btn)
        toolbar.addWidget(self.new_child_folder_btn)
        toolbar.addWidget(self.new_session_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.refresh_btn)
        set_text(self.refresh_btn, 'ui.projects_view.refresh_the_project_tree_and_selected_fbca', setter='setToolTip')
        self.review_tasks_btn = QToolButton(left)
        from ui.widgets.review_progress import review_icon
        self.review_tasks_btn.setIcon(review_icon())
        self.review_tasks_btn.setFixedSize(34, 34)
        set_text(self.review_tasks_btn, 'ui.projects_view.update_task_statuses_in_all_sessions_7d35', setter='setToolTip')
        toolbar.addWidget(self.review_tasks_btn)
        for button in (self.new_folder_btn,self.new_child_folder_btn,self.new_session_btn,self.refresh_btn,self.review_tasks_btn):
            button.setObjectName('ProjectToolbarButton')
            button.setFixedSize(28,28)
            button.setCursor(Qt.PointingHandCursor)
            apply_style(button,'QToolButton {background:@background.surface@;border:1px solid @border.subtle@;border-radius:5px;padding:0;min-width:26px;max-width:26px;min-height:26px;max-height:26px;} QToolButton:hover {background:@control.hover@;}')
        left_layout.addLayout(toolbar)
        from ui.widgets.error_actions import ErrorDetailsPanel
        self.task_review_status = ErrorDetailsPanel(left)
        self.task_review_status.setMaximumHeight(160)
        self.task_review_status.hide()
        left_layout.addWidget(self.task_review_status)

        self.tree = ProjectTree(left)
        apply_style(self.tree,'QTreeWidget#ProjectsTree {background:@background.surface@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:6px;padding:3px;} QTreeWidget#ProjectsTree::item {border:0;}')
        from ui.generation_status import install_session_status
        install_session_status(self.tree)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        from ui.widgets.project_tree import ProjectTreeRules
        self.project_tree = ProjectTreeRules(self.tree, self.new_session_btn, exclusive=False)
        left_layout.addWidget(self.tree, 1)

        self.details = QStackedWidget(splitter)
        self.placeholder = localize(QLabel('', self.details), 'ui.projects_view.select_a_folder_session_or_branch_b959')
        self.placeholder.setAlignment(Qt.AlignCenter)
        apply_style(self.placeholder, "color:@text.disabled@;")
        self.details.addWidget(self.placeholder)

        self.folder_editor = SettingsEditor(
            tr('ui.caption.9e89f9b20272'),
            self.details,
            show_prompt_inheritance=True,
            show_store_both_formats=True,
        )
        self.folder_tabs = QTabWidget(self.details)
        self.folder_tabs.setObjectName('ProjectFolderTabs')
        add_caption_tab(self.folder_tabs, self.folder_editor, tr('ui.caption.985b5e0f2ccf'))
        self.history = self._create_history()
        add_caption_tab(self.folder_tabs, self.history, tr('ui.backup_settings.history_1417'))
        self.details.addWidget(self.folder_tabs)

        self.session_tabs = QTabWidget(self.details)
        self.session_tabs.setObjectName("ProjectSessionTabs")
        self.session_overview = SessionOverview(self.session_tabs)
        self.session_editor = SettingsEditor(
            tr('ui.caption.6e0451c103a0'),
            self.session_tabs,
            show_memory_mode=True,
            show_store_both_formats=True,
            show_thoughts_in_context=True,
            show_workspace=False,
        )
        self.session_editor.set_modules_editable(False)
        add_caption_tab(self.session_tabs, self.session_overview, tr('ui.plugin_manager_pages.description_b368'))
        add_caption_tab(self.session_tabs, self.session_editor, tr('ui.caption.985b5e0f2ccf'))
        self.details.addWidget(self.session_tabs)

        self.branch_tabs = QTabWidget(self.details)
        self.branch_tabs.setObjectName("ProjectBranchTabs")
        self.branch_overview = SessionOverview(self.branch_tabs)
        self.branch_editor = SettingsEditor(
            tr('ui.caption.7daa9542dd08'),
            self.branch_tabs,
            show_memory_mode=True,
            show_prompt_inheritance=True,
            show_store_both_formats=True,
            allow_memory_mode_inherit=True,
            allow_store_both_inherit=True,
            show_workspace=False,
        )
        self.branch_editor.set_modules_editable(False)
        add_caption_tab(self.branch_tabs, self.branch_overview, tr('ui.plugin_manager_pages.description_b368'))
        add_caption_tab(self.branch_tabs, self.branch_editor, tr('ui.caption.985b5e0f2ccf'))
        self.details.addWidget(self.branch_tabs)

        splitter.addWidget(left)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 860])

        self.refresh_btn.clicked.connect(self.refresh)
        self.review_tasks_btn.clicked.connect(self._review_all_tasks)
        self.new_folder_btn.clicked.connect(self._create_folder)
        self.new_child_folder_btn.clicked.connect(self._create_child_folder)
        self.tree.itemSelectionChanged.connect(self._sync_creation_buttons)
        self.tree.itemExpanded.connect(self._sync_creation_buttons)
        self.tree.itemCollapsed.connect(self._sync_creation_buttons)
        self._sync_creation_buttons()
        self.new_session_btn.clicked.connect(self._create_session)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(self._begin_rename)
        from ui.widgets.commit_on_outside import OutsideCommitDelegate
        self.tree.setItemDelegate(OutsideCommitDelegate(self.tree))
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.move_requested.connect(self._move_entity)
        self.tree.sessions_move_requested.connect(self._move_sessions)
        self.folder_editor.save_requested.connect(self._save_folder)
        self.session_editor.save_requested.connect(self._save_session)
        self.branch_editor.save_requested.connect(self._save_branch)
        for overview in (self.session_overview, self.branch_overview):
            overview.open_chat_requested.connect(self._open_chat)
            overview.task_add_requested.connect(self._add_task)
            overview.task_update_requested.connect(self._update_task)
            overview.task_delete_requested.connect(self._delete_task)
            overview.task_status_check_requested.connect(self._check_task_statuses)
            overview.task_list_send_requested.connect(self._send_task_list)

        event_bus.sessions_updated.connect(self.refresh)
        event_bus.module_status_changed.connect(lambda *_: self._refresh_modules())
        core_process.status_changed.connect(
            lambda online, _msg: self.refresh(force=not self._initial_snapshot_loaded) if online else None
        )
        # Load the hidden Projects workspace during application startup too.
        # AsyncRunner makes this part of the global startup barrier automatically.
        QTimer.singleShot(0, lambda: self.refresh(force=True))
        self._catalog_timer = QTimer(self)
        self._catalog_timer.setInterval(1000)
        self._catalog_timer.timeout.connect(self.refresh)
        self._catalog_timer.start()

    def showEvent(self, event):
        self._ensure_history()
        super().showEvent(event)
        self.refresh()

    def _review_all_tasks(self):
        if getattr(self, '_task_review_running', False):
            return
        if not core_process.is_running():
            self.task_review_status.setPlainText(tr('ui.caption.cf2cd8e4bc1d'))
            self.task_review_status.show()
            return
        from ui.widgets.task_review import TaskReviewPanel
        from shiboken6 import isValid
        panel = getattr(self, '_task_review_panel', None)
        if panel is not None and isValid(panel) and panel.isVisible():
            panel.raise_()
            return
        self._task_review_panel = TaskReviewPanel(self)

    def _create_history(self):
        from ui.views.project_history import ProjectHistory
        history = ProjectHistory(self.folder_tabs)
        history.rebuild_requested.connect(self._schedule_history_rebuild)
        return history

    def _schedule_history_rebuild(self):
        # Не заменяем страницу внутри её showEvent или обработки события Qt.
        defer(0, self, self._ensure_history)

    def _ensure_history(self):
        old = self.history
        if old.widgets_valid():
            return
        index = self.folder_tabs.indexOf(old) if isValid(old) else -1
        active = index >= 0 and self.folder_tabs.currentIndex() == index
        search = old.search.text() if isValid(old.search) else ''
        session = old.session_filter.currentData() if isValid(old.session_filter) else ''
        sessions = (sorted(old.allowed_sessions) if old.allowed_sessions is not None else
                    [str(item['session_id']) for item in self.snapshot.get('sessions', [])
                     if any(folder.get('id') == old.project for folder in
                            (item.get('effective_settings') or {}).get('folder_chain', []))])
        old.retire()
        if index >= 0:
            self.folder_tabs.removeTab(index)
        self.history = self._create_history()
        self.folder_tabs.insertTab(index if index >= 0 else 1, self.history, tr('ui.backup_settings.history_1417'))
        self.history.set_project(old.project, folder=old.scope, sessions=sessions)
        self.history.run_scope = old.run_scope
        self.history.focus_run = old.focus_run
        self.history.focus_snapshot = old.focus_snapshot
        self.history._preview_height = old._preview_height
        self.history.session_filter.setCurrentIndex(max(0,self.history.session_filter.findData(session)))
        bind_caption(self.history.search, search)
        if active:
            self.folder_tabs.setCurrentWidget(self.history)

    def refresh(self, force: bool = False):
        if (not force and not self.isVisible()) or not core_process.is_running():
            return
        if force or not self._modules_loaded:self._refresh_modules()
        if self._refresh_in_flight:
            self._refresh_pending = True
            return
        self._refresh_in_flight = True
        self.async_runner.submit(
            self.client.get_projects,
            self._loaded,
            self._load_failed,
        )

    def _refresh_modules(self):
        if self._modules_in_flight or not core_process.is_running():return
        self._modules_in_flight = True
        self.async_runner.submit(self.client.list_modules, self._modules_ready, self._modules_failed)

    def _modules_ready(self, modules):
        self._modules_in_flight = False
        self._modules_loaded = True
        if modules != self.modules:
            self.modules = modules
            self._restore_details()

    def _modules_failed(self, _error):
        self._modules_in_flight = False

    @staticmethod
    def _tree_structure(snapshot):
        return ([(f.get('id'), f.get('parent_id'), f.get('name')) for f in snapshot.get('folders', [])],
                [(s.get('session_id'), s.get('folder_id'), s.get('title'),
                  [(b.get('name'), b.get('title')) for b in s.get('branches', [])]) for s in snapshot.get('sessions', [])])

    def _selected_projection(self, snapshot):
        if self._selected_type == 'folder':
            # Parent settings and project membership affect the selected folder.
            return (snapshot.get('folders'), [(s.get('session_id'), s.get('folder_id')) for s in snapshot.get('sessions', [])])
        identity = (self._selected_id or '').split(':', 1)[0]
        return next((s for s in snapshot.get('sessions', []) if s.get('session_id') == identity), None)

    def _finish_refresh(self):
        if self._refresh_pending:
            self._refresh_pending = False
            defer(0, self, self.refresh)

    def _loaded(self, new_snapshot):
        self._refresh_in_flight = False
        self._initial_snapshot_loaded = True
        snapshot_changed = new_snapshot != self.snapshot
        if not snapshot_changed:
            self._finish_refresh()
            return
        tree_changed = self._tree_structure(new_snapshot) != self._tree_structure(self.snapshot)
        details_changed = self._selected_projection(new_snapshot) != self._selected_projection(self.snapshot)
        self.snapshot = new_snapshot
        from ui.app_state import app_state
        app_state.update_catalog(new_snapshot)
        if tree_changed:
            self._rebuild_tree()
        if details_changed or tree_changed:self._restore_details()
        self._finish_refresh()

    def _load_failed(self, error):
        self._refresh_in_flight = False
        self._initial_snapshot_loaded = True
        bind_caption(self.tree, str(error), setter='setToolTip')
        self._finish_refresh()

    def _folders_by_id(self) -> Dict[str, Dict[str, Any]]:
        return {str(item.get("id")): item for item in self.snapshot.get("folders", []) if item.get("id")}

    def _sessions_by_id(self) -> Dict[str, Dict[str, Any]]:
        return {str(item.get("session_id")): item for item in self.snapshot.get("sessions", []) if item.get("session_id")}

    def _rebuild_tree(self):
        from ui.app_state import app_state
        app_state.update_catalog(self.snapshot)
        selected = (self._selected_type, self._selected_id)
        self._rebuilding = True
        previous_signal_state = self.tree.blockSignals(True)
        try:
            self.tree.clear()
            folders = self._folders_by_id()
            sessions = list(self.snapshot.get("sessions", []))
            items: Dict[str, QTreeWidgetItem] = {}

            def add_folder(folder_id: str, stack=None):
                if folder_id in items:
                    return items[folder_id]
                stack = set(stack or ())
                if folder_id in stack:
                    return None
                stack.add(folder_id)
                folder = folders.get(folder_id)
                if not folder:
                    return None
                parent_id = folder.get("parent_id")
                parent_item = add_folder(str(parent_id), stack) if parent_id and str(parent_id) in folders else None
                item = QTreeWidgetItem(parent_item or self.tree)
                bind_caption(item, str(folder.get("name") or "Folder"), setter='setText', index=0)
                item.setIcon(0, ui_icon('folder'))
                item.setData(0, Qt.UserRole, {"type": "folder", "id": folder_id})
                item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
                items[folder_id] = item
                return item

            for folder_id in folders:
                add_folder(folder_id)

            for session in sorted(sessions, key=lambda item: str(item.get("session_id", ""))):
                session_id = str(session.get("session_id") or "")
                if not session_id:
                    continue
                folder_id = session.get("folder_id")
                parent_item = items.get(str(folder_id)) if folder_id else None
                if parent_item is None:
                    continue
                item = QTreeWidgetItem(parent_item)
                bind_caption(item, str(session.get('title') or session_id), setter='setText', index=0)
                item.setIcon(0, ui_icon('document'))
                item.setData(0, Qt.UserRole, {"type": "session", "id": session_id, "session_id": session_id, "branch": "main"})
                item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled)

                for branch in session.get("branches", []) or []:
                    branch_name = str(branch.get("name") or "")
                    if not branch_name or branch_name == "main":
                        continue
                    branch_item = QTreeWidgetItem(item)
                    bind_caption(branch_item, str(branch.get('title') or branch_name), setter='setText', index=0)
                    branch_item.setIcon(0, ui_icon('branch'))
                    branch_item.setData(
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
            self._select_entity(*selected)
            self._sync_creation_buttons()
        finally:
            self.tree.blockSignals(previous_signal_state)
            self._rebuilding = False

    def _select_entity(self, entity_type: Optional[str], entity_id: Optional[str]):
        if not entity_type or not entity_id:
            return
        iterator = self.tree.invisibleRootItem()
        stack = [iterator.child(i) for i in range(iterator.childCount())]
        while stack:
            item = stack.pop()
            data = item.data(0, Qt.UserRole) or {}
            if data.get("type") == entity_type and data.get("id") == entity_id:
                self.tree.setCurrentItem(item)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _selection_changed(self):
        if self._rebuilding:
            return
        selected = self.tree.selectedItems()
        if len(selected) > 1:
            if all((item.data(0, Qt.UserRole) or {}).get("type") == "session" for item in selected):
                self._selected_type, self._selected_id = "sessions", ""
                set_text(self.placeholder, 'ui.projects_view.selected_sessions_v0_54b7', v0=len(selected))
                self.details.setCurrentWidget(self.placeholder)
                return
            current = self.tree.currentItem()
            if current is not None:
                blocked = self.tree.blockSignals(True)
                try:
                    self.tree.clearSelection()
                    current.setSelected(True)
                finally:
                    self.tree.blockSignals(blocked)
                selected = [current]

        item = selected[0] if selected else self.tree.currentItem()
        data = item.data(0, Qt.UserRole) if item else None
        if not data:
            self._selected_type = self._selected_id = None
            set_text(self.placeholder, 'ui.projects_view.select_a_folder_session_or_branch_b959')
            self.details.setCurrentWidget(self.placeholder)
            return
        self._selected_type = data.get("type")
        self._selected_id = str(data.get("id") or "")
        set_text(self.placeholder, 'ui.projects_view.select_a_folder_session_or_branch_b959')
        self._restore_details()

    def _restore_details(self):
        self._ensure_history()
        if self._selected_type == "folder":
            folder = self._folders_by_id().get(self._selected_id)
            if not folder:
                self.details.setCurrentWidget(self.placeholder)
                return
            self.folder_editor.set_workspace_editable(not folder.get("parent_id"))
            effective_modules = self._effective_folder_modules(self._selected_id)
            self.folder_editor.load(
                str(folder.get("name") or "Folder"),
                folder.get("settings") or {},
                self.modules,
                effective_modules,
                effective_store_both=self._effective_folder_store_both(self._selected_id),
            )
            root = folder
            folders = self._folders_by_id()
            seen = set()
            while root.get('parent_id') and root['id'] not in seen:
                seen.add(root['id'])
                root = folders.get(root['parent_id'], root)
            root_id = root['id']
            related = [session['session_id'] for session in self.snapshot.get('sessions', [])
                       if any(item.get('id') == self._selected_id for item in
                              (session.get('effective_settings') or {}).get('folder_chain', []))]
            self.history.set_project(root_id, folder=self._selected_id, sessions=related)
            self.details.setCurrentWidget(self.folder_tabs)
        elif self._selected_type == "session":
            session = self._sessions_by_id().get(self._selected_id)
            if not session:
                self.details.setCurrentWidget(self.placeholder)
                return
            self.session_overview.load(session)
            settings = session.get("project_settings") or {}
            effective_modules = (session.get("effective_settings") or {}).get("modules")
            if effective_modules is None:
                effective_modules = [str(item.get("name")) for item in self.modules if item.get("enabled")]
            self.session_editor.load(
                self._selected_id,
                settings,
                self.modules,
                effective_modules,
                memory_mode=str(session.get("memory_mode") or "text"),
                effective_store_both=bool(
                    (session.get("effective_settings") or {}).get("store_temp_memory_both", False)
                ),
                include_thoughts_in_context=bool(
                    session.get("include_thoughts_in_context", True)
                ),
                include_replied_thoughts_in_context=bool(session.get("include_replied_thoughts_in_context", False)),
            )
            self.details.setCurrentWidget(self.session_tabs)
        elif self._selected_type == "branch":
            if ":" not in self._selected_id:
                self.details.setCurrentWidget(self.placeholder)
                return
            session_id, branch_name = self._selected_id.split(":", 1)
            session = self._sessions_by_id().get(session_id)
            if not session:
                self.details.setCurrentWidget(self.placeholder)
                return
            branch = next(
                (dict(item) for item in (session.get("branches") or []) if str(item.get("name") or "") == branch_name),
                None,
            )
            if not branch:
                self.details.setCurrentWidget(self.placeholder)
                return
            self.branch_overview.load(session, branch=branch)
            settings = branch.get("project_settings") or {}
            effective = branch.get("effective_settings") or {}
            effective_modules = effective.get("modules")
            if effective_modules is None:
                effective_modules = [str(item.get("name")) for item in self.modules if item.get("enabled")]
            self.branch_editor.load(
                branch_name,
                settings,
                self.modules,
                effective_modules,
                memory_mode=str(branch.get("memory_mode") or session.get("memory_mode") or "text"),
                effective_store_both=bool(effective.get("store_temp_memory_both", False)),
            )
            self.details.setCurrentWidget(self.branch_tabs)
        else:
            self.details.setCurrentWidget(self.placeholder)

    def _effective_folder_modules(self, folder_id: str) -> Optional[List[str]]:
        folders = self._folders_by_id()
        chain = []
        current = folder_id
        seen = set()
        while current and current in folders and current not in seen:
            seen.add(current)
            chain.append(folders[current])
            current = folders[current].get("parent_id")
        chain.reverse()
        modules = None
        for folder in chain:
            value = (folder.get("settings") or {}).get("modules")
            if value is not None:
                modules = list(value or [])
        if modules is None:
            modules = [str(item.get("name")) for item in self.modules if item.get("enabled")]
        return modules

    def _effective_folder_store_both(self, folder_id: str) -> bool:
        folders = self._folders_by_id()
        chain = []
        current = folder_id
        seen = set()
        while current and current in folders and current not in seen:
            seen.add(current)
            chain.append(folders[current])
            current = folders[current].get("parent_id")
        chain.reverse()

        value = False
        for folder in chain:
            setting = (folder.get("settings") or {}).get("store_temp_memory_both", None)
            if setting is not None:
                value = bool(setting)
        return value

    def _parent_for_creation(self) -> Optional[str]:
        item = self.tree.currentItem()
        if not item:
            return None
        data = item.data(0, Qt.UserRole) or {}
        if data.get("type") == "folder":
            return data.get("id")
        parent = item.parent()
        if parent:
            parent_data = parent.data(0, Qt.UserRole) or {}
            if parent_data.get("type") == "folder":
                return parent_data.get("id")
        return None

    def _submit(self, action, on_done=None):
        self.setEnabled(False)
        def done(result):
            self.setEnabled(True)
            if isinstance(result, dict) and result.get("success") is False:
                QMessageBox.warning(
                    self,
                    tr('ui.projects_view.projects_04e2'),
                    str(result.get("message") or tr('ui.caption.2697cfe09ee1')),
                )
                self.refresh()
                return
            if on_done:
                on_done(result)
            event_bus.sessions_updated.emit()
        def failed(error):
            self.setEnabled(True)
            QMessageBox.warning(self, tr('ui.projects_view.projects_04e2'), str(error))
        self.async_runner.submit(action, done, failed)

    def _sync_creation_buttons(self, *_):
        self.new_child_folder_btn.setEnabled(bool(self.project_tree.creation_folder()))

    def _create_child_folder(self):
        parent = self.project_tree.creation_folder()
        if parent:self._create_folder(parent_id=parent)

    def _create_folder(self, _checked=False, parent_id: Any = _AUTO_PARENT):
        target_parent = None if parent_id is _AUTO_PARENT else parent_id
        name = 'New Project' if target_parent is None else 'New Folder'
        def created(result):
            if target_parent is None:
                from ui.app_state import app_state
                app_state.select_project(result['folder']['id'], name)
        self._submit(lambda: self.client.create_project_folder(name, target_parent), created)

    def _create_session(self, _checked=False, folder_id: Any = _AUTO_PARENT):
        target_folder = self.project_tree.creation_folder(None if folder_id is _AUTO_PARENT else folder_id)
        if not target_folder:
            return
        memory_mode = choose_memory_mode(self)
        if memory_mode is None:
            return
        metadata = {
            "temp_memory_mode": memory_mode,
            "limit_ranking": True,
            "include_thoughts_in_context": True,
        }
        self._submit(lambda: self.client.create_session(metadata, folder_id=target_folder))

    def _begin_rename(self, item, _column):
        data = item.data(0, Qt.UserRole) or {}
        if data.get("type") == "folder":
            self.tree.editItem(item, 0)
        elif data.get("type") == "session":
            self._open_chat(str(data.get("id") or ""), "main")
        elif data.get("type") == "branch":
            self._open_chat(
                str(data.get("session_id") or ""),
                str(data.get("branch") or "main"),
            )

    def _rename_tree_item(self, item):
        if item is not None:
            self.tree.editItem(item, 0)

    def _item_changed(self, item, _column):
        if self._rebuilding:
            return
        data = item.data(0, Qt.UserRole) or {}
        new_name = item.text(0).strip()
        if not new_name:
            self.refresh()
            return
        if data.get("type") == "folder":
            folder_id = str(data.get("id"))
            old = self._folders_by_id().get(folder_id, {}).get("name")
            if new_name != old:
                self._selected_type, self._selected_id = "folder", folder_id
                self._submit(lambda: self.client.update_project_folder(folder_id, name=new_name))
        elif data.get("type") == "session":
            old_id = str(data.get("id"))
            if new_name != old_id:
                def renamed(result):
                    if result.get("success"):
                        actual = str(result.get("new_session_id") or new_name)
                        self._selected_type, self._selected_id = "session", actual
                        event_bus.session_renamed.emit(old_id, actual)
                self._submit(lambda: self.client.rename_session(old_id, new_name), renamed)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            add_folder = localize(add_caption_action(menu, 'addAction', "New Folder"), 'ui.projects_view.new_folder_c75b')
            add_session = localize(add_caption_action(menu, 'addAction', "New Session"), 'ui.projects_view.new_session_fc0b')
            add_session.setEnabled(self.new_session_btn.isEnabled())
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

        selected_sessions = []
        for selected_item in self.tree.selectedItems():
            selected_data = selected_item.data(0, Qt.UserRole) or {}
            if selected_data.get("type") == "session":
                session_id = str(selected_data.get("session_id") or selected_data.get("id") or "")
                if session_id:
                    selected_sessions.append(session_id)
        selected_sessions = list(dict.fromkeys(selected_sessions))
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
            if item_type == "branch":
                self._rename_branch_from_tree(data)
            else:
                self._rename_tree_item(item)
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
                    self._selected_type = self._selected_id = None
                    self._submit(lambda: self.client.delete_project_folder(folder_id))
            return

        session_id = str(data.get("session_id") or data.get("id") or "")
        branch = str(data.get("branch") or "main")
        if chosen == open_chat:
            self._open_chat(session_id, branch)
            return
        if chosen != delete:
            return

        if item_type == "session":
            if QMessageBox.question(self, tr('ui.projects_view.projects_04e2'), tr('ui.projects_view.delete_session_v0_5611', v0=session_id)) == QMessageBox.Yes:
                self._selected_type = self._selected_id = None
                self._submit(lambda: self.client.delete_session(session_id))
        else:
            if QMessageBox.question(self, tr('ui.projects_view.projects_04e2'), tr('ui.projects_view.delete_branch_v0_7253', v0=branch)) == QMessageBox.Yes:
                self._selected_type = self._selected_id = None
                self._submit(lambda: self.client.delete_branch(session_id, branch))

    def _rename_branch_from_tree(self, data: Dict[str, Any]):
        session_id = str(data.get("session_id") or "")
        old_branch = str(data.get("branch") or "")
        from ui.app_state import app_state
        old_title = app_state.branch_titles.get((session_id,old_branch),old_branch)
        new_name, ok = QInputDialog.getText(self, tr('ui.projects_view.rename_branch_5af6'), tr('ui.projects_view.branch_name_bcb8'), text=old_title)
        if not ok or not new_name.strip() or new_name.strip() == old_title:
            return
        clean = new_name.strip()
        if not clean:
            return

        def renamed(result):
            actual = str((result or {}).get("branch") or clean)
            self._selected_type, self._selected_id = "branch", f"{session_id}:{actual}"
            event_bus.branch_renamed.emit(session_id, old_branch, actual)

        self._submit(lambda: self.client.rename_branch(session_id, old_branch, clean), renamed)

    def _move_entity(self, item_type: str, entity_id: str, target_folder_id):
        if item_type == "session" and not target_folder_id:
            return
        if item_type == "session":
            self._submit(lambda: self.client.update_project_session(entity_id, folder_id=target_folder_id))
        else:
            self._submit(lambda: self.client.update_project_folder(entity_id, parent_id=target_folder_id))

    def _move_sessions(self, session_ids, target_folder_id):
        if not target_folder_id:
            return
        ids = [str(value) for value in (session_ids or []) if str(value)]
        if not ids:
            return

        def action():
            for session_id in ids:
                result = self.client.update_project_session(session_id, folder_id=target_folder_id)
                if isinstance(result, dict) and result.get("success") is False:
                    return result
            return {"success": True}

        self._submit(action)

    def _delete_sessions(self, session_ids):
        ids = [str(value) for value in (session_ids or []) if str(value)]
        if not ids:
            return
        if QMessageBox.question(
            self,
            tr('ui.projects_view.projects_04e2'),
            tr('ui.projects_view.permanently_delete_selected_sessions_v0_and_f255', v0=len(ids)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        self._selected_type = self._selected_id = None

        def action():
            for session_id in ids:
                result = self.client.delete_session(session_id)
                if isinstance(result, dict) and result.get("success") is False:
                    return result
            return {"success": True}

        self._submit(action)

    def _save_folder(self, payload: Dict[str, Any]):
        folder_id = self._selected_id if self._selected_type == "folder" else None
        if not folder_id:
            return
        name = payload.get("name") or "Folder"
        settings = payload.get("settings") or {}
        self._submit(lambda: self.client.update_project_folder(folder_id, name=name, settings=settings))

    def _save_session(self, payload: Dict[str, Any]):
        session_id = self._selected_id if self._selected_type == "session" else None
        if not session_id:
            return
        name = payload.get("name") or session_id
        settings = payload.get("settings") or {}
        memory_mode = "vector" if payload.get("memory_mode") == "vector" else "text"
        include_thoughts = bool(payload.get("include_thoughts_in_context", True))
        include_replied_thoughts = bool(payload.get("include_replied_thoughts_in_context", False))

        def action():
            current_id = session_id
            rename_result = None
            if name != session_id:
                rename_result = self.client.rename_session(session_id, name)
                if not rename_result.get("success"):
                    return rename_result
                current_id = str(rename_result.get("new_session_id") or name)
            settings_result = self.client.update_project_session(
                current_id,
                settings=settings,
                memory_mode=memory_mode,
                include_thoughts_in_context=include_thoughts,
                include_replied_thoughts_in_context=include_replied_thoughts,
            )
            return {"success": True, "session_id": current_id, "rename": rename_result, "settings": settings_result}

        def saved(result):
            new_id = str(result.get("session_id") or session_id)
            if new_id != session_id:
                event_bus.session_renamed.emit(session_id, new_id)
            self._selected_type, self._selected_id = "session", new_id
            event_bus.session_memory_mode_changed.emit(new_id, memory_mode)
            event_bus.session_thoughts_context_changed.emit(new_id, include_thoughts)
            event_bus.session_replied_thoughts_context_changed.emit(new_id, include_replied_thoughts)

        self._submit(action, saved)

    def _save_branch(self, payload: Dict[str, Any]):
        if self._selected_type != "branch" or ":" not in self._selected_id:
            return
        session_id, branch_name = self._selected_id.split(":", 1)
        requested_name = str(payload.get("name") or branch_name).strip() or branch_name
        settings = dict(payload.get("settings") or {})
        settings["memory_mode"] = payload.get("memory_mode")

        def action():
            current_branch = branch_name
            rename_result = None
            if requested_name != branch_name:
                rename_result = self.client.rename_branch(session_id, branch_name, requested_name)
                if not rename_result.get("success"):
                    return rename_result
                current_branch = str(rename_result.get("branch") or requested_name)
            settings_result = self.client.update_project_branch(
                session_id, current_branch, settings
            )
            if not settings_result.get("success"):
                return settings_result
            return {
                "success": True,
                "session_id": session_id,
                "branch": current_branch,
                "rename": rename_result,
                "settings": settings_result,
            }

        def saved(result):
            current_branch = str(result.get("branch") or branch_name)
            if current_branch != branch_name:
                event_bus.branch_renamed.emit(session_id, branch_name, current_branch)
            self._selected_type, self._selected_id = "branch", f"{session_id}:{current_branch}"

        self._submit(action, saved)

    def _add_task(self, session_id: str, text: str):
        if str(text or "") == "":
            self._submit(lambda: self.client.create_empty_task(session_id))
        else:
            self._submit(lambda: self.client.add_tasks(session_id, [text]))

    def _update_task(self, session_id: str, task_id: str, updates: Dict[str, Any]):
        self._submit(lambda: self.client.update_task(session_id, task_id, **dict(updates or {})))

    def _delete_task(self, session_id: str, task_id: str):
        if QMessageBox.question(self, tr('ui.projects_view.tasks_03cc'), tr('ui.projects_view.delete_task_v0_9365', v0=task_id)) != QMessageBox.Yes:
            return
        self._submit(lambda: self.client.delete_task(session_id, task_id))

    def _send_task_list(self, session_id: str, branch: str, tasks: List[Dict[str, Any]]):
        if not tasks:
            return
        event_bus.task_list_send_requested.emit(session_id, branch or "main", list(tasks))

    def _check_task_statuses(self, session_id: str, branch: str, tasks: List[Dict[str, Any]]):
        if not tasks:
            return
        if QMessageBox.question(
            self,
            tr('ui.projects_view.update_task_statuses_0c42'),
            tr('ui.projects_view.ask_the_model_to_review_v0_f7d7', v0=len(tasks)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return
        event_bus.task_status_check_requested.emit(session_id, branch or "main", list(tasks))

    def _open_chat(self, session_id: str, branch: str = "main"):
        event_bus.session_selected.emit(session_id, branch or "main")
        event_bus.switch_view_tab_requested.emit(0)
