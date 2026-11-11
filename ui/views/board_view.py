from ui.document_paths import document_path, show_document_path
from ui.i18n import bind_caption, tr, set_text
from ui.icons import symbol_button

import os
import json
from pathlib import Path
from typing import Dict, Optional
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QToolButton, QStackedWidget, QFrame, QTabBar, QPushButton, QFileDialog
)
from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QKeySequence, QShortcut

from ui.tab_memory import TabMemoryManager, SuspendedTab
from ui.event_bus import event_bus
from ui.views.file_view import FileTabBar, NewTabButton, TabCloseButton
from ui.views.canvas.canvas_view import CanvasView
from ui.state_manager import StateManager
from ui.app_state import app_state


class BoardDocumentTab(QWidget):
    """Контейнер отдельной открытой доски (.canvas)."""
    dirty_state_changed = Signal(bool)
    agent_request_submitted = Signal(str, object)

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = os.path.abspath(file_path)
        self.is_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Строка пути (хлебные крошки)
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("FileDocToolbar")
        self.toolbar.setFixedHeight(22)
        tb_layout = QHBoxLayout(self.toolbar)
        tb_layout.setContentsMargins(16, 2, 8, 2)
        tb_layout.setSpacing(6)

        rel_path = document_path(self.file_path)
        self.path_label = QLabel(rel_path, self.toolbar)
        self.path_label.setObjectName("FileDocBreadcrumb")
        self.path_label.setTextFormat(Qt.PlainText)
        tb_layout.addWidget(self.path_label)
        tb_layout.addStretch()
        layout.addWidget(self.toolbar)

        # Холст доски
        self.canvas = CanvasView(self)
        self.canvas.load_from_file(self.file_path)
        self.canvas.modified.connect(self._on_modified)
        self.canvas.camera_changed.connect(self._on_camera_changed)
        self.canvas.agent_request_submitted.connect(self.agent_request_submitted.emit)
        layout.addWidget(self.canvas, 1)

        # Таймер автосохранения (дебаунс 250 мс для исключения спама диска при вводе текста)
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(750)
        self._auto_save_timer.timeout.connect(self.save_board)
        self._camera_save_timer = QTimer(self)
        self._camera_save_timer.setSingleShot(True)
        self._camera_save_timer.setInterval(250)
        self._camera_save_timer.timeout.connect(self.canvas.save_camera_state)

        # Ручное сохранение по Ctrl+S сохраняется в качестве дубликата
        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_board)

    def _on_modified(self):
        if not self.is_dirty:
            self.is_dirty = True
            self.dirty_state_changed.emit(True)
        self._auto_save_timer.start()

    def _on_camera_changed(self):
        self._camera_save_timer.start()

    def save_board(self):
        self._auto_save_timer.stop()
        if self.canvas.save_to_file(self.file_path):
            self.is_dirty = False
            self.dirty_state_changed.emit(False)
            return True
        return False


class BoardView(QWidget):
    """Рабочая область «Доска» с поддержкой вкладок .canvas и плейсхолдера."""
    agent_request_submitted = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BoardViewRoot")
        self.open_boards: Dict[str, BoardDocumentTab] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Шапка с вкладками
        self.header_bar = QFrame(self)
        self.header_bar.setObjectName("FileEditorHeaderBar")
        self.header_bar.setFixedHeight(32)
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(0, 0, 8, 0)
        header_layout.setSpacing(6)
        header_layout.setAlignment(Qt.AlignVCenter)

        self.tab_bar = FileTabBar(self.header_bar)
        self.tab_bar.currentChanged.connect(self._on_current_tab_changed)
        header_layout.addWidget(self.tab_bar)

        self.new_tab_btn = NewTabButton(self.header_bar)
        set_text(self.new_tab_btn, 'ui.board_view.create_a_new_board_canvas_b9b9', setter='setToolTip')
        # У доски один тип файла, поэтому кнопка сразу создаёт .canvas.
        self.new_tab_btn.menu_requested.connect(self._create_new_untitled_board)
        header_layout.addWidget(self.new_tab_btn)

        header_layout.addStretch(1)
        root_layout.addWidget(self.header_bar)

        # Стек вкладок / плейсхолдер
        self.stack = QStackedWidget(self)
        self.stack.setObjectName("FileEditorStack")

        # Плашка при отсутствии открытых досок
        from ui.widgets.empty_workspace import EmptyWorkspace
        self.no_boards_frame = EmptyWorkspace(self, new_key='workspace.new_board',
            create=self._create_new_untitled_board, open_file=self._open_dialog)
        self.placeholder_new_btn = self.no_boards_frame.new_button
        self.placeholder_open_btn = self.no_boards_frame.open_button

        self.stack.addWidget(self.no_boards_frame)
        root_layout.addWidget(self.stack, 1)

        self.header_bar.show()
        self.tab_memory = TabMemoryManager(
            self, "board", self.stack, self.open_boards, BoardDocumentTab, self._bind_document
        )

        event_bus.open_file_requested.connect(self.open_board)
        event_bus.file_renamed.connect(self.rename_board)

    def _bind_document(self, document):
        document.dirty_state_changed.connect(
            lambda dirty, d=document: self._update_tab_title(d.file_path, dirty))
        document.agent_request_submitted.connect(self.agent_request_submitted.emit)
        panel = getattr(self.window(), "bottom_panel", None)
        if panel is not None:
            document.canvas.set_keyboard_commands_enabled(not panel._modules_keyboard_scope_active())

    def _get_workspace_dir(self) -> Path:
        ws_dir = Path(app_state.view_workspace("board"))
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir

    def _on_current_tab_changed(self, index: int):
        if index < 0 or index >= self.tab_bar.count():
            return
        doc_widget = self.tab_bar.tabData(index)
        if self.isVisible():
            from ui.history_focus import focus_document
            focus_document(doc_widget)
        if isinstance(doc_widget, SuspendedTab):
            doc_widget = self.tab_memory.ensure_loaded(doc_widget)
        if doc_widget and isinstance(doc_widget, BoardDocumentTab):
            self.stack.setCurrentWidget(doc_widget)
            self.tab_memory.touch(doc_widget)

    def open_board(self, file_path: str, switch_tab: bool = True):
        from ui.startup_process import report_startup_phase
        report_startup_phase(tr('ui.caption.e94e952093b5') + Path(file_path).name)
        if not file_path.lower().endswith(".canvas"):
            return

        abs_path = os.path.abspath(file_path)

        if abs_path in self.open_boards:
            doc_widget = self.open_boards[abs_path]
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_widget:
                    self.tab_bar.setCurrentIndex(i)
                    doc_widget = self.tab_memory.ensure_loaded(doc_widget)
                    self.stack.setCurrentWidget(doc_widget)
                    self.tab_memory.touch(doc_widget)
                    break
            event_bus.switch_view_tab_requested.emit(3)
            return

        doc_tab = BoardDocumentTab(abs_path, self)
        self.tab_memory.bind_document(doc_tab)

        file_name = Path(abs_path).name
        idx = self.tab_bar.addTab(file_name)
        self.tab_bar.setTabData(idx, doc_tab)
        self.open_boards[abs_path] = doc_tab

        self._attach_close_button(idx, abs_path)

        self.stack.addWidget(doc_tab)
        self.tab_bar.setCurrentIndex(idx)
        self.stack.setCurrentWidget(doc_tab)
        self.tab_memory.touch(doc_tab)
        self.tab_bar.updateGeometry()
        self.header_bar.show()

        # Автоматическое переключение на вкладку «Доска»
        if switch_tab:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: event_bus.switch_view_tab_requested.emit(3))

    def _attach_close_button(self, idx: int, file_path: str):
        btn = TabCloseButton(self.tab_bar)
        btn.clicked.connect(lambda _, p=file_path: self._close_tab_by_path(p))
        self.tab_bar.setTabButton(idx, QTabBar.RightSide, btn)

    def _close_tab_by_path(self, file_path: str):
        if file_path not in self.open_boards:
            return

        doc_tab = self.open_boards[file_path]
        if doc_tab.is_dirty:
            from ui.widgets.unsaved_popup import confirm_close
            if not confirm_close(self, doc_tab, title=Path(file_path).name,
                    tab_bar=self.tab_bar, save=doc_tab.save_board):
                return
        doc_tab._auto_save_timer.stop()

        self.tab_memory.forget(doc_tab)
        self.open_boards.pop(file_path)
        tab_idx = -1
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) == doc_tab:
                tab_idx = i
                break

        if tab_idx != -1:
            self.tab_bar.removeTab(tab_idx)
            self.tab_bar.updateGeometry()

        self.stack.removeWidget(doc_tab)
        doc_tab.deleteLater()

        for i in range(self.tab_bar.count()):
            w = self.tab_bar.tabData(i)
            if isinstance(w, (BoardDocumentTab, SuspendedTab)):
                self._attach_close_button(i, w.file_path)

        if not self.open_boards:
            self.header_bar.show()
            self.stack.setCurrentWidget(self.no_boards_frame)

    def _open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("ui.paint_view.open_1f1d"), str(app_state.view_workspace("board")), "Board (*.canvas)")
        for path in paths:
            self.open_board(path)

    def _create_new_untitled_board(self):
        ws_dir = self._get_workspace_dir()
        idx_count = 1
        while True:
            candidate = ws_dir / f"Untitled_{idx_count}.canvas"
            if not candidate.exists() and str(candidate) not in self.open_boards:
                new_file = candidate
                break
            idx_count += 1

        from ui.file_saving import write_text
        write_text(new_file, json.dumps({"nodes": [], "edges": []}, indent=2))

        self.open_board(str(new_file))

    def _update_tab_title(self, file_path: str, is_dirty: bool):
        if file_path in self.open_boards:
            doc_widget = self.open_boards[file_path]
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_widget:
                    base_name = Path(file_path).name
                    title = f"* {base_name}" if is_dirty else base_name
                    bind_caption(self.tab_bar, title, setter='setTabText', index=i)
                    break

    def rename_board(self, old_path: str, new_path: str):
        old_abs = os.path.abspath(old_path)
        new_abs = os.path.abspath(new_path)
        if old_abs in self.open_boards:
            doc_tab = self.open_boards.pop(old_abs)
            self.open_boards[new_abs] = doc_tab
            doc_tab.file_path = new_abs
            if doc_tab.canvas is not None:
                doc_tab.canvas.file_path = new_abs
            show_document_path(doc_tab.path_label, new_abs)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_tab:
                    bind_caption(self.tab_bar, Path(new_abs).name, setter='setTabText', index=i)
                    self._attach_close_button(i, new_abs)
                    break

    def get_open_state(self) -> dict:
        paths,states = [],{}
        for i in range(self.tab_bar.count()):
            w = self.tab_bar.tabData(i)
            if isinstance(w, (BoardDocumentTab, SuspendedTab)):
                paths.append(w.file_path)
                states[w.file_path] = w.saved_state if isinstance(w,SuspendedTab) else self.tab_memory._snapshot(w)
        return {
            "open_boards":[{"path":path,"state":states[path]} for path in paths],
            "active_board_index": self.tab_bar.currentIndex()
        }

    def restore_open_state(self, open_boards: list, active_index: int = 0):
        if not open_boards:
            return

        for entry in open_boards:
            f_path = entry.get('path') if isinstance(entry,dict) else entry
            if f_path and os.path.isfile(f_path):
                self.open_board(f_path,switch_tab=False)
                if isinstance(entry,dict) and entry.get('state') and f_path in self.open_boards:
                    self.tab_memory._restore(self.open_boards[f_path],entry['state'])

        if 0 <= active_index < self.tab_bar.count():
            self.tab_bar.setCurrentIndex(active_index)
