"""One in-window close review; choices have no effects until Apply."""
from ui.styles.binding import apply_style
from ui.i18n import bind_caption, tr, localize, set_text

from pathlib import Path
from PySide6.QtCore import Qt, QEvent, QTimer, QObject
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QToolButton, QScrollArea, QGraphicsOpacityEffect, QLineEdit, QSizePolicy)
from shiboken6 import isValid
from ui.qt_deferred import defer


class ClosePanel(QWidget):
    def __init__(self, window, apply, cancel):
        super().__init__(window)
        self.window, self.apply_callback, self.cancel_callback = window, apply, cancel
        self._dismissed = False
        self.rows, self.choices = {}, {}
        self.ready, self.applying = False, False
        self.setObjectName('ApplicationCloseOverlay')
        self.setAttribute(Qt.WA_StyledBackground)
        apply_style(self, '\n            QWidget#ApplicationCloseOverlay {background:rgba(0,0,0,65);}\n            QFrame#CloseReview {background:@background.panel@;border:1px solid @text.disabled@;border-radius:8px;}\n            QFrame#CloseChoice {background:@background.surface@;border:1px solid @border.subtle@;border-radius:8px;}\n            QFrame#CloseChoice[decided="true"] {background:@background.panel@;border-color:@border.subtle@;}\n            QFrame#CloseReview QLabel {background:transparent;border:none;}\n            QPushButton {padding:6px 12px;border-radius:6px;border:1px solid @border.subtle@;}\n            QPushButton#CloseDecisionButton:checked {background:@control.selected@;color:@text.primary@;border:1px solid @accent.primary@;}\n            QPushButton#CloseDecisionButton:checked:hover {background:@control.hover@;}\n            QPushButton#CloseApplyButton:disabled {background:@background.surface@;color:@text.disabled@;border:1px solid @border.subtle@;}\n        ')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch()
        self.frame = QFrame(self)
        self.frame.setObjectName('CloseReview')
        self.frame.setMaximumWidth(760)
        inner = QVBoxLayout(self.frame)
        inner.setContentsMargins(18, 18, 18, 18)
        title = localize(QLabel('', self.frame), 'ui.close_panel.exit_application_3249')
        title.setStyleSheet('font-size:14px;font-weight:600;')
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        inner.addLayout(title_row)
        self.status = localize(QLabel('', self.frame), 'ui.close_panel.checking_unsaved_changes_and_tasks_046d')
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.status.setTextFormat(Qt.PlainText)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        from ui.widgets.error_actions import copy_error_button
        self.copy_status = copy_error_button(self.frame, self.status.text)
        self.copy_status.hide()
        status_row.addWidget(self.copy_status, 0, Qt.AlignTop)
        inner.addLayout(status_row)
        scroll = QScrollArea(self.frame)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll = scroll
        scroll.setMinimumHeight(0)
        self.content = QWidget(scroll)
        self.list = QVBoxLayout(self.content)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.content)
        inner.addWidget(scroll)
        self.content.installEventFilter(self)
        buttons = QHBoxLayout()
        self.cancel_button = localize(QPushButton('', self.frame), 'ui.close_panel.cancel_8fbe')
        self.cancel_button.clicked.connect(cancel)
        self.apply_button = localize(QPushButton('', self.frame), 'ui.close_panel.apply_and_exit_c8a7')
        self.apply_button.setObjectName('CloseApplyButton')
        self._apply_opacity = QGraphicsOpacityEffect(self.apply_button)
        self._apply_opacity.setOpacity(.35)
        self.apply_button.setGraphicsEffect(self._apply_opacity)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(apply)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(self.apply_button)
        inner.addLayout(buttons)
        outer.addWidget(self.frame, 0, Qt.AlignHCenter)
        outer.addStretch()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setGeometry(window.rect())
        self.frame.setFixedWidth(max(1, min(760, self.width()-48)))
        QApplication.instance().installEventFilter(self)
        self.show()
        self.raise_()
        self.setFocus()

    def _fit_review(self):
        if self._dismissed:
            return
        buttons = self.frame.findChildren(QPushButton)
        width = max((button.fontMetrics().horizontalAdvance(button.text()) + 28 for button in buttons), default=100)
        for button in buttons:
            button.setFixedSize(max(100, width), 34)
        natural = self.list.totalHeightForWidth(max(1, self.frame.width() - 38))
        if natural < 0:
            natural = self.list.sizeHint().height()
        # Measure the non-scroll rows directly. Subtracting the current scroll
        # height from a size hint mixes actual and preferred sizes and lets the
        # frame distribute spare space between text rows.
        layout = self.frame.layout()
        margins = layout.contentsMargins()
        inside_width = max(1, self.frame.width()-margins.left()-margins.right())
        chrome = margins.top()+margins.bottom()+max(0, layout.count()-1)*layout.spacing()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.widget() is self.scroll:
                continue
            row_height = item.heightForWidth(inside_width) if item.hasHeightForWidth() else -1
            chrome += row_height if row_height >= 0 else item.sizeHint().height()
        height = min(max(1, natural), max(1, self.height()-48-chrome))
        if self.scroll.height() != height:
            self.scroll.setFixedHeight(height)
        frame_height = min(max(1, self.height()-48), chrome+height)
        if self.frame.height() != frame_height or self.frame.minimumHeight() != frame_height:
            self.frame.setFixedHeight(frame_height)

    def activate_review(self):
        # Закрытие через панель задач может прийти неактивному/свёрнутому окну.
        window = self.window
        window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()
        handle = window.windowHandle()
        if handle is not None:
            handle.requestActivate()
        self.raise_()
        self._focus_review()
        defer(0, self, self._focus_review)
        QApplication.alert(window)

    def _focus_review(self):
        if not self._dismissed and self.isVisible():
            self.cancel_button.setFocus(Qt.OtherFocusReason)

    def add_item(self, key, title, detail, options, *, path=''):
        if key in self.rows:
            return
        card = QFrame(self.content)
        card.setObjectName('CloseChoice')
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignTop)
        label = bind_caption(QLabel('', card), title)
        label.setWordWrap(True)
        label.setStyleSheet('font-weight:600;')
        layout.addWidget(label)
        description = QLabel(detail, card)
        description.setWordWrap(True)
        layout.addWidget(description)
        path_edit = None
        if path:
            layout.addWidget(localize(QLabel('', card), 'ui.close_panel.save_path_an_existing_file_will_02cc'))
            path_edit = QLineEdit(path, card)
            layout.addWidget(path_edit)
        text_effects = []
        for text_label in card.findChildren(QLabel):
            text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
            effect = QGraphicsOpacityEffect(text_label)
            text_label.setGraphicsEffect(effect)
            effect.setOpacity(1)
            text_effects.append(effect)
        row = QHBoxLayout()
        row.addStretch()
        buttons, button_effects = {}, {}
        for value, text in options:
            button = bind_caption(QPushButton('', card), text)
            button.setObjectName('CloseDecisionButton')
            button.setCheckable(True)
            effect = QGraphicsOpacityEffect(button)
            effect.setOpacity(1)
            button.setGraphicsEffect(effect)
            buttons[value], button_effects[value] = button, effect
            button.clicked.connect(lambda checked=False, k=key, v=value: self.choose(k, v))
            row.addWidget(button)
        if options:layout.addLayout(row)
        self.rows[key] = {'widget': card, 'text_effects': text_effects, 'buttons': buttons,
                          'button_effects': button_effects, 'description': description, 'path': path_edit}
        self.list.addWidget(card)
        self.update_ready()
        defer(0, self, self._fit_review)

    def add_notice(self,key,title,detail):
        self.add_item(key,title,detail,[])

    def decisions_complete(self):
        return all(not row['buttons'] or key in self.choices for key,row in self.rows.items())

    def choose(self, key, value):
        if self.applying or key not in self.rows or value not in self.rows[key]['buttons']:
            return
        self.choices[key] = value
        self._render_choice(key)
        self.update_ready()

    def _render_choice(self, key):
        row = self.rows[key]
        selected = self.choices.get(key)
        decided = key in self.choices
        for effect in row['text_effects']:
            effect.setOpacity(.45 if decided else 1)
        for value, button in row['buttons'].items():
            button.setChecked(decided and value == selected)
            row['button_effects'][value].setOpacity(1 if not decided or value == selected else .45)
        card = row['widget']
        card.setProperty('decided', decided)
        card.style().unpolish(card)
        card.style().polish(card)

    def update_ready(self):
        enabled = self.ready and not self.applying and self.decisions_complete()
        self.apply_button.setEnabled(enabled)
        self._apply_opacity.setOpacity(1 if enabled else .35)

    def fail(self, key, text):
        self.applying = False
        self.cancel_button.setEnabled(True)
        self.content.setEnabled(True)
        bind_caption(self.status, text)
        self.copy_status.show()
        defer(0, self, self._fit_review)
        if key in self.rows:
            self.choices.pop(key, None)
            self._render_choice(key)
        self.update_ready()

    def eventFilter(self, watched, event):
        if self._dismissed:
            return False
        kind = event.type()
        if watched is self.content and kind == QEvent.LayoutRequest:
            defer(0, self, self._fit_review)
        if watched is self.window and kind == QEvent.WindowActivate:
            defer(0, self, self._focus_review)
        if watched is self.window and kind == QEvent.Resize:
            self.setGeometry(self.window.rect())
            self.frame.setFixedWidth(max(1, min(760, self.width()-48)))
            defer(0, self, self._fit_review)
            self.raise_()
        if kind in (QEvent.Shortcut, QEvent.ShortcutOverride):
            if kind == QEvent.Shortcut:
                return True
            event.accept()
            return True
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick,
                    QEvent.Wheel, QEvent.KeyPress, QEvent.KeyRelease, QEvent.ContextMenu, QEvent.Drop):
            if isinstance(watched, QWidget) and (watched is self.window or self.window.isAncestorOf(watched)):
                if watched is not self and not self.isAncestorOf(watched):
                    return True
        if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            if not self.applying:
                self.cancel_callback()
            return True
        return False

    def dismiss(self):
        if self._dismissed:
            return
        self._dismissed = True
        QApplication.instance().removeEventFilter(self)
        self.cancel_button.clicked.disconnect(self.cancel_callback)
        self.apply_button.clicked.disconnect(self.apply_callback)
        self.hide()
        self.window = self.apply_callback = self.cancel_callback = None
        self.deleteLater()


class CloseCoordinator(QObject):
    def __init__(self, window, check_tasks):
        super().__init__(window)
        self.window, self.check_tasks = window, check_tasks
        self._cancelled = False
        window._close_review = self
        self.actions, self.paused_timers = {}, []
        self.saved_actions = set()
        self.runs_stopped = False
        self.terminals_closed = False
        self.tasks = []
        self._tasks_ready=False
        self._terminals_ready=False
        self.panel = ClosePanel(window, self.apply, self.cancel)
        self.collect_documents()
        try:
            from modules.system.terminal_sandbox.lifecycle import shutdown_notice
        except ImportError:
            self._terminals_ready=True
        else:
            window.async_runner.submit(shutdown_notice,self.terminals_checked,
                                       lambda error:self.terminals_checked({}))
        from ui.core_bridge import core_process
        # Уже работающее ядро тоже принадлежит рабочему пространству пользователя.
        # Владение QProcess влияет лишь на сохранение процесса после закрытия GUI.
        if core_process.is_running() or core_process.is_starting() or core_process.owns_running_core():
            updating = getattr(window, '_apply_update_after_close', False)
            choices = [('stop', tr('ui.caption.8be2bbf39a83'))]
            if not updating:
                choices.append(('keep', tr('ui.caption.47231fc064fa')))
            self.panel.add_item('core', tr('ui.caption.bfe96f4d0e44'),
                                tr('updates.core_stop') if updating else tr('ui.caption.c3693ed41e56'), choices)
        if window.chat_view._active_runs:
            self.panel.add_item('runs', tr('ui.caption.7de6b3f7239d'), tr('ui.caption.706dffaf6cc5'),
                                [('stop', tr('ui.caption.f4713627683b'))])
        if core_process.is_running():
            window.async_runner.submit(window.task_client.list_dirty_task_sessions, self.tasks_loaded, self.tasks_failed)
        else:
            self.tasks_loaded([])

    def collect_documents(self):
        w = self.window
        groups = [('text', [*w.file_view.open_docs.values(), *w.file_view.virtual_docs.values()]),
                  ('board', w.board_view.open_boards.values() if w.board_view is not None else ()),
                  ('pipeline', w.bottom_panel.modules_view.open_pipelines.values()),
                  ('paint', w.paint_view.documents if w.paint_view is not None else ())]
        for kind, documents in groups:
            for document in list(documents):
                timer = getattr(document, '_auto_save_timer', None)
                if timer is not None and timer.isActive():
                    if all(timer is not old for old, _ in self.paused_timers):
                        self.paused_timers.append((timer, timer.remainingTime()))
                    timer.stop()
                dirty = document.dirty if kind == 'paint' else document.is_dirty
                if not dirty and not (kind == 'paint' and document.busy):
                    continue
                key = (kind, id(document))
                path = str(getattr(document, 'file_path', '') or getattr(document, 'pipeline_dir', ''))
                title = getattr(document, 'title', '') or Path(path).name
                detail = path or title
                if kind == 'text' and getattr(document, '_external_conflict', False):
                    detail += tr('ui.caption.3fcaa8701e75')
                if kind == 'paint' and document.busy:
                    detail += tr('ui.caption.aa20bd4a64ad')
                destination = ''
                if kind == 'paint' and not document.file_path:
                    from ui.app_state import app_state
                    destination = str(Path(getattr(document, 'materials_root', app_state.view_workspace('paint'))) / (Path(title).stem + '.tpaint'))
                self.actions[key] = (kind, document)
                labels = {'text': tr('ui.caption.3091aca226ef'), 'board': tr('nav.board'), 'pipeline': tr('ui.module_view.pipeline_37e1'), 'paint': tr('nav.sketch')}
                self.panel.add_item(key, f'{labels[kind]} · {title}', detail,
                                    [('save', tr('common.save')), ('discard', tr('common.discard'))], path=destination)

    def _is_current(self):
        return (not self._cancelled and isValid(self.window)
                and self.window._close_review is self)

    def tasks_loaded(self, tasks):
        if not self._is_current():
            return
        self.tasks = list(tasks or [])
        if self.tasks:
            self.panel.add_notice('tasks', tr('ui.caption.a81f014bae1f'), tr('ui.caption.a85cf59387fd'))
            from ui.widgets.task_review import TaskReviewSelection
            self.task_selection = TaskReviewSelection(self.tasks, self.panel.content)
            self.panel.rows['tasks']['widget'].layout().addWidget(self.task_selection)
        self._tasks_ready=True
        self.review_ready()

    def terminals_checked(self,status):
        if not self._is_current():
            return
        if status.get('docker'):
            self.panel.add_notice('terminals',tr('ui.caption.7af10ed55bcc'),
                tr('ui.caption.8b2429f7aa4f'))
        elif status.get('host'):
            self.panel.add_notice('terminals',tr('ui.caption.b94f9317fdfb'),
                tr('ui.caption.f7dd2656af54'))
        self._terminals_ready=True
        self.review_ready()

    def review_ready(self):
        if not self._is_current() or not self._tasks_ready or not self._terminals_ready:
            return
        set_text(self.panel.status, 'ui.close_panel.review_the_notices_and_choose_actions_631c')
        self.panel.ready = True
        self.panel.update_ready()
        if not self.panel.rows:
            defer(0, self, self.apply)

    def tasks_failed(self, error):
        if not self._is_current():
            return
        self.panel.add_item('task_error', tr('ui.caption.d33088fb1aeb'), str(error), [('skip', tr('ui.caption.b9c76113c69c'))])
        self.tasks_loaded([])

    def cancel(self):
        if not self._is_current() or self.panel.applying:
            return
        self._cancelled = True
        self.window._apply_update_after_close = False
        self.window._close_review = None
        self.panel.dismiss()
        # Вкладка могла выгрузиться, пока окно выхода ожидало ответы служб.
        for timer, remaining in self.paused_timers:
            if isValid(timer):
                timer.start(max(0, remaining))
        self.paused_timers.clear()
        self.actions.clear()
        self.deleteLater()

    def apply(self):
        if not self._is_current() or self.panel.applying:
            return
        self.collect_documents()
        if not self.panel.ready or not self.panel.decisions_complete():
            return
        self.panel.applying = True
        self.panel.cancel_button.setEnabled(False)
        self.panel.update_ready()
        self.panel.content.setEnabled(False)
        set_text(self.panel.status, 'ui.close_panel.applying_decisions_511a')
        if self.panel.choices.get('runs') == 'stop' and not self.runs_stopped:
            self.window.chat_view.shutdown_runs()
            self.runs_stopped = True
        self.queue = [key for key in self.actions if self.panel.choices.get(key) == 'save' and key not in self.saved_actions]
        QTimer.singleShot(0, self.save_next)

    def save_next(self):
        if not self.queue:
            self.update_tasks()
            return
        key = self.queue.pop(0)
        kind, document = self.actions[key]
        try:
            if kind == 'paint':
                path_edit = self.panel.rows[key]['path']
                destination = path_edit.text().strip() if path_edit is not None else document.file_path
                if not destination:
                    raise ValueError('Укажите путь для сохранения рисунка.')
                self.window.paint_view.save_document(document, destination=destination, quiet=True,
                    done=lambda success: self.saved(key, success))
            else:
                method = getattr(document, {'text': 'save_file', 'board': 'save_board', 'pipeline': 'save_pipeline'}[kind])
                success = method(confirm_external=False, quiet=True) if kind == 'text' else method()
                self.saved(key, success)
        except Exception as error:
            self.panel.fail(key, tr('ui.caption.e9600dd9d239', v0=error))

    def saved(self, key, success):
        if not success:
            kind, document = self.actions.get(key, (None, None))
            detail = getattr(document, 'error', '') if kind == 'paint' else ''
            self.panel.fail(key, tr('ui.caption.e9600dd9d239', v0=detail)
                            if detail else tr('ui.caption.8ffcf8495e29'))
            return
        self.saved_actions.add(key)
        QTimer.singleShot(0, self.save_next)

    def update_tasks(self):
        selected = self.task_selection.selected() if self.tasks and hasattr(self, 'task_selection') else []
        if selected:
            set_text(self.panel.status, 'ui.close_panel.updating_task_statuses_568b')
            self.window.async_runner.submit(
                lambda: self.check_tasks(self.window.task_client, selected), self.tasks_updated,
                lambda error: self.panel.fail('tasks', tr('ui.caption.0d5bc3efffdf', v0=error)))
        else:
            self.finish()

    def tasks_updated(self, results):
        if hasattr(self, "task_selection"):
            self.task_selection.apply_results(results or [])
        failed = [item for item in (results or []) if not item.get('success', False)]
        if failed:
            self.panel.fail('tasks', tr('ui.caption.6e71b86d284d') + '; '.join(
                str(item.get('error') or item.get('session_id') or tr('ui.caption.00550b0b8523')) for item in failed))
            return
        from ui.event_bus import event_bus
        event_bus.sessions_updated.emit()
        self.tasks = []
        self.finish()

    def finish(self):
        if self.panel.choices.get('terminal_error') == 'skip':
            self.terminals_closed = True
        try:
            from modules.system.terminal_sandbox.lifecycle import close_application
        except ImportError:
            self.terminals_closed = True
        if not self.terminals_closed:
            set_text(self.panel.status, 'ui.close_panel.stopping_terminals_065d')
            if not self.runs_stopped:
                self.window.chat_view.shutdown_runs()
                self.runs_stopped = True
            self.window.async_runner.submit(close_application, self.terminal_cleanup_done,
                self.terminal_cleanup_failed)
            return
        self.finish_application()

    def terminal_cleanup_done(self, result):
        self.terminals_closed = True
        self.finish_application()

    def terminal_cleanup_failed(self, error):
        self.panel.add_item('terminal_error', tr('ui.caption.3d5c5871474e'),
            tr('ui.caption.065c32347cfa'),
            [('cleanup', tr('common.retry')), ('skip', tr('ui.caption.23b7bb222e67'))])
        self.panel.fail('terminal_error', tr('ui.caption.a1b7f5050df6', v0=error))

    def finish_application(self):
        from ui.core_bridge import core_process
        from ui.generation_status import generation_status
        for key in self.actions:
            if self.panel.choices.get(key) == 'discard':
                kind, document = self.actions[key]
                if kind == 'pipeline':
                    document.is_dirty = False  # Do not autosave on child close.
        for key in list(self.window.chat_view._active_runs):
            generation_status.set(*key, 'stopped')
        if not self.runs_stopped:
            self.window.chat_view.shutdown_runs()
            self.runs_stopped = True
        if self.panel.choices.get('core') == 'stop':
            set_text(self.panel.status, 'ui.close_panel.stopping_the_core_1677')
            try:
                core_process.stop_core()
            except Exception as error:
                self.panel.fail('core', tr('ui.caption.4aef95ae4599') + str(error))
                return
        elif self.panel.choices.get('core') == 'keep':
            self.window._keep_core_running = core_process.keep_core_after_ui_close()
        self.window._close_approved = True
        self.panel.dismiss()
        QTimer.singleShot(0, self.window.close)
