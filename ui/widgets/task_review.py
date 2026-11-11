"""Выбор проверок по сессиям и веткам без отдельного нативного окна."""
from ui.icons import set_icon
from ui.i18n import bind_caption, set_caption_headers, tr, set_text

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QToolButton, QTreeWidget, QTreeWidgetItem
from ui.app_state import app_state


class TaskReviewSelection(QFrame):
    def __init__(self, sessions, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QToolButton(self)
        set_text(self.toggle, 'ui.task_review.sessions_and_branches_with_unfinished_tasks_9f75', v0=len(sessions))
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        set_icon(self.toggle, 'right', size=16, icon_only=False)
        layout.addWidget(self.toggle)
        self.body = QFrame(self)
        inner = QVBoxLayout(self.body)
        inner.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        for title, checked in [(tr('ui.caption.9a6e498b21ab'), True), (tr('ui.caption.29242be9ae30'), False)]:
            button = QToolButton(self.body)
            bind_caption(button, title)
            button.setObjectName('TaskReviewAction')
            button.clicked.connect(lambda _, value=checked: self.set_all(value))
            actions.addWidget(button)
        actions.addStretch()
        inner.addLayout(actions)
        self.tree = QTreeWidget(self.body)
        set_caption_headers(self.tree, [tr('ui.caption.2e6c8cb76d9b'), tr('ui.caption.e9646b5a08cc'), tr('ui.caption.4997a5143d9f')])
        from PySide6.QtWidgets import QHeaderView
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.setMinimumHeight(140)
        self.tree.setMaximumHeight(240)
        inner.addWidget(self.tree)
        self.items = {}
        groups = {}
        for record in sessions:
            session, branch = record['session_id'], record.get('active_branch', 'main')
            project = app_state.session_projects.get(session, '')
            if project not in groups:
                groups[project] = QTreeWidgetItem([app_state.projects.get(project, {}).get('name') or tr('ui.caption.7f8044f85387')])
                self.tree.addTopLevelItem(groups[project])
            item = QTreeWidgetItem([app_state.display_session(session, branch), str(len(record.get('tasks') or [])), ''])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            item.setData(0, Qt.UserRole, dict(record))
            bind_caption(item, session + ' / ' + branch, setter='setToolTip', index=0)
            groups[project].addChild(item)
            groups[project].setExpanded(True)
            self.items[(session, branch)] = item
        layout.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, expanded):
        set_icon(self.toggle, ('down' if expanded else 'right'), size=16, icon_only=False)
        self.body.setVisible(expanded)

    def set_all(self, enabled):
        for item in self.items.values():
            if not item.isDisabled():
                item.setCheckState(0, Qt.Checked if enabled else Qt.Unchecked)

    def selected(self):
        return [item.data(0, Qt.UserRole) for item in self.items.values()
                if not item.isDisabled() and item.checkState(0) == Qt.Checked]

    def apply_results(self, results):
        for result in results:
            item = self.items.get((result.get('session_id'), result.get('branch', 'main')))
            if item is None:
                continue
            bind_caption(item, tr('ui.caption.5d92460cb2d7') if result.get('success') else tr('ui.file_manager_widget.error_bf22'), setter='setText', index=2)
            bind_caption(item, result.get('error') or '', setter='setToolTip', index=2)
            if result.get('success'):
                item.setCheckState(0, Qt.Unchecked)
                item.setDisabled(True)
        self.toggle.setChecked(True)


from ui.widgets.overlay_panel import OverlayPanel
from ui.core_bridge.async_runner import AsyncRunner
from PySide6.QtWidgets import QLabel, QPushButton
from ui.styles.binding import apply_style


class TaskReviewPanel(OverlayPanel):
    def __init__(self, owner):
        super().__init__(owner.window(), tr('ui.task_review.title'), width=760, height=460)
        self.client = owner.client
        self.running = False
        self.selection = None
        self.runner = AsyncRunner(self, max_threads=1)
        self.status = QLabel(tr('ui.caption.74ff93822c75'), self.frame)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.content.addWidget(self.status)
        self.content.addStretch()
        buttons = QHBoxLayout()
        self.cancel = QPushButton(tr('ui.memory_manager.close'), self.frame)
        self.cancel.clicked.connect(self.dismiss)
        buttons.addWidget(self.cancel)
        buttons.addStretch()
        self.run = QPushButton(tr('ui.task_review.run'), self.frame)
        self.run.setEnabled(False)
        self.run.clicked.connect(self.update_selected)
        buttons.addWidget(self.run)
        self.content.addLayout(buttons)
        self.runner.submit(self.client.list_dirty_task_sessions, self.loaded, self.failed)

    def loaded(self, sessions):
        self.selection = TaskReviewSelection(sessions, self.frame)
        apply_style(self.selection, 'QToolButton#TaskReviewAction {border:1px solid @border.subtle@;border-radius:5px;padding:4px 8px;}')
        self.selection.toggle.setChecked(True)
        self.content.insertWidget(2, self.selection)
        self.selection.tree.itemChanged.connect(self.update_enabled)
        self.status.setText('' if sessions else tr('ui.caption.2334702ccb32'))
        self.update_enabled()

    def update_enabled(self, *_):
        self.run.setEnabled(not self.running and bool(self.selection and self.selection.selected()))

    def update_selected(self):
        selected = self.selection.selected() if self.selection else []
        if not selected or self.running:
            return
        from ui.task_status import run_dirty_task_status_checks
        from ui.widgets.review_progress import ReviewProgress
        self.running = True
        self.cancel.setEnabled(False)
        self.selection.setEnabled(False)
        self.update_enabled()
        self.progress = ReviewProgress(self)
        self.progress.message.connect(self.status.setText)
        self.runner.submit(lambda: run_dirty_task_status_checks(self.client, selected, self.progress.message.emit), self.done, self.failed)

    def done(self, results):
        self.running = False
        self.cancel.setEnabled(True)
        self.selection.setEnabled(True)
        self.selection.apply_results(results)
        errors = [r for r in results if not r.get('success')]
        self.status.setText('\n'.join(str(r.get('error') or '') for r in errors)
                            or tr('ui.caption.c6273fc562ba') + str(len(results)))
        self.update_enabled()
        from ui.event_bus import event_bus
        event_bus.sessions_updated.emit()

    def failed(self, error):
        self.running = False
        self.cancel.setEnabled(True)
        if self.selection:
            self.selection.setEnabled(True)
        self.status.setText(str(error))
        self.update_enabled()

    def dismiss(self):
        if not self.running:
            self.runner.shutdown()
            super().dismiss()
