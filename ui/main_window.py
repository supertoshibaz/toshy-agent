from ui.i18n import tr, set_text, i18n

import os
from pathlib import Path
from typing import List, Tuple

from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QSplitter, QStackedWidget)
from ui.widgets.dialogs import MessageBox as QMessageBox
from PySide6.QtCore import Qt, Signal, Slot, QPoint, QTimer
from shiboken6 import isValid
from PySide6.QtGui import (
    QCloseEvent, QResizeEvent, QDragEnterEvent,
    QDragMoveEvent, QDropEvent
)

from ui.state_manager import StateManager
from ui.event_bus import event_bus
from ui.app_state import app_state
from ui.widgets.passive_splitter import PassiveSplitter as QSplitter
from ui.components.top_bar import TopBar
from ui.components.bottom_panel import BottomPanel, HEADER_HEIGHT
from ui.dock_system.dock_manager import DockManager
from ui.dock_system.dock_panel import DockPanel
from ui.dock_system.types import DockSlotPosition
from ui.dock_system.module_ui_loader import ModuleUILoader
from ui.core_bridge.module_manager import ModuleConfigSync
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge import core_process, SessionApiClient
from ui.task_status import run_dirty_task_status_checks

from ui.dock_system.vertical_splitter import VerticalDockSplitter
from ui.panels import (
    SubagentsPanel,
    McpPanel,
    TerminalPanel,
    SessionsPanel,
    TasksPanel,
    FileManagerPanel
)
from ui.views.chat_view import ChatView
from ui.views.projects_view import ProjectsView
from ui.views.file_view import FileView
from ui.workspace_geometry import editor_workspace_width, centered_workspace_geometry

MIN_SIDE_PANEL_WIDTH = 340
MAX_SIDE_PANEL_WIDTH = MIN_SIDE_PANEL_WIDTH * 2  # 680px
MIN_CENTER_WIDTH = 300
MAX_CHAT_CENTER_WIDTH = 1100
MAX_PROJECTS_CENTER_WIDTH = editor_workspace_width()

class CentralWorkspaceContainer(QWidget):
    """Контейнер центральной рабочей области с разной шириной для разных режимов."""
    CHAT_VIEW_INDEX = 0
    PROJECTS_VIEW_INDEX = 1
    FILE_VIEW_INDEX = 2
    BOARD_VIEW_INDEX = 3
    PAINT_VIEW_INDEX = 4

    def __init__(self, central_stack: QWidget, bottom_panel: BottomPanel, parent=None):
        super().__init__(parent)
        self.setObjectName("WorkspaceSurface")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.central_stack = central_stack
        self.bottom_panel = bottom_panel

        self.central_stack.setParent(self)
        self.bottom_panel.setParent(self)
        self.bottom_panel.raise_()
        self.central_stack.currentChanged.connect(self._current_changed)

    @Slot(int)
    def _current_changed(self, _index):
        self.update_workspace_geometry()

    def update_workspace_geometry(self):
        # Сигнал стека может прийти при разборке окна; Slot привязан к QObject.
        if (not isValid(self) or not isValid(self.central_stack) or not isValid(self.bottom_panel)
                or self.central_stack.currentIndex()<0 or getattr(self,'_updating_geometry',False)):
            return
        self._updating_geometry = True
        try:
            self._layout_workspace_geometry()
        finally:
            self._updating_geometry = False

    def _layout_workspace_geometry(self):
        w = self.width()
        h = self.height()
        workspace_h = max(0, h - HEADER_HEIGHT)

        current_index = self.central_stack.currentIndex()

        # Board and File paint their surface edge-to-edge. FileView keeps the
        # actual editor in its own centered reading column.
        if current_index in (self.FILE_VIEW_INDEX, self.BOARD_VIEW_INDEX, self.PAINT_VIEW_INDEX):
            target_w = w
            x = 0
        else:
            # Chat and Projects aim at the geometric center of the whole
            # application window. Side panels affect them only when that ideal
            # placement no longer fits inside the remaining center region.
            max_width = (
                MAX_PROJECTS_CENTER_WIDTH
                if current_index == self.PROJECTS_VIEW_INDEX
                else MAX_CHAT_CENTER_WIDTH
            )
            x, target_w = centered_workspace_geometry(self, w, max_width)

        self.central_stack.setGeometry(x, 0, target_w, workspace_h)
        self.bottom_panel.update_layout_geometry(w, h)

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self.update_workspace_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.update_workspace_geometry()

    def wheelEvent(self, event):
        # The chat column is capped at 1100px, but the empty workspace around
        # it belongs to Chat too. Side docks have a different parent and never
        # reach this handler. Leave the bottom panel's own wheel behavior alone.
        if (self.central_stack.currentIndex() == self.CHAT_VIEW_INDEX
                and not (self.bottom_panel.isVisible()
                         and self.bottom_panel.geometry().contains(event.position().toPoint()))):
            chat = self.central_stack.currentWidget()
            if isinstance(chat, ChatView) and chat.scroll_area.isVisible():
                chat.wheelEvent(event)
                return
        super().wheelEvent(event)

class DockColumnContainer(QWidget):
    """Контейнер боковой колонки с поддержкой Drag-and-Drop."""
    def __init__(self, inner_splitter: QSplitter, side: str = "left", min_w: int = MIN_SIDE_PANEL_WIDTH, max_w: int = MAX_SIDE_PANEL_WIDTH, parent=None):
        super().__init__(parent)
        self.setObjectName("DockColumnContainer")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.side = side
        self.min_w = min_w
        self.max_w = max_w
        self.setMinimumWidth(min_w)
        self.setMaximumWidth(max_w)
        self.setAcceptDrops(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        layout.addWidget(inner_splitter)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasFormat("application/x-dock-panel-id"):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasFormat("application/x-dock-panel-id"):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasFormat("application/x-dock-panel-id"):
            panel_id = event.mimeData().data("application/x-dock-panel-id").data().decode("utf-8")
            y = event.position().y()
            half_h = self.height() / 2.0

            if self.side == "left":
                target_pos = DockSlotPosition.LEFT_TOP if y < half_h else DockSlotPosition.LEFT_BOTTOM
            else:
                target_pos = DockSlotPosition.RIGHT_TOP if y < half_h else DockSlotPosition.RIGHT_BOTTOM

            main_win = self.window()
            if hasattr(main_win, "dock_manager"):
                main_win.dock_manager.move_panel(panel_id, target_pos)

            event.acceptProposedAction()
        else:
            super().dropEvent(event)

class MainWindow(QMainWindow):
    def changeEvent(self, event):
        if event.type() == event.Type.WindowStateChange:
            if hasattr(self, "top_bar"):
                self.top_bar.update_max_button()
        super().changeEvent(event)

    def __init__(self, startup_tracker=None):
        super().__init__()
        from ui.startup_process import report_startup_phase
        report_startup_phase(tr('ui.caption.572ecb0f9ff7'))
        from ui.tab_memory import performance_preferences
        # Freeze availability for this application run. Settings changes are
        # persisted immediately, but affect construction only after restart.
        self.workspace_options = dict(performance_preferences.values['workspaces'])
        from ui.plugin_host import start_plugins
        start_plugins(self)
        self._startup_tracker = startup_tracker
        self._close_review = None
        self._plugins_panel = None
        self._close_approved = False
        self.state_manager = StateManager()
        self._first_launch = not self.state_manager.state_path.exists()
        from ui.project_workspaces import restore_project_identity
        restore_project_identity(self.state_manager.state)
        self.async_runner = AsyncRunner(self)
        self.task_client = SessionApiClient(base_url=core_process.base_url)
        from core.paths import APP_PATHS
        self.root_dir = APP_PATHS.root

        # 1. Восстановление языка
        saved_lang = self.state_manager.get("language", "en")
        i18n.set_language(saved_lang)
        from ui.themes.manager import theme_manager
        i18n.language_changed.connect(self._save_appearance)
        theme_manager.selection_changed.connect(self._save_appearance)

        set_text(self, 'ui.main_window.your_workspace_4d02', setter='setWindowTitle')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        available = self.screen().availableGeometry()
        self.resize(min(1400, available.width()-80), min(860, available.height()-80))
        geometry = self.state_manager.get("window_geometry")
        if isinstance(geometry,str) and geometry:
            from PySide6.QtCore import QByteArray
            self.restoreGeometry(QByteArray.fromHex(geometry.encode("ascii")))
        # A saved maximized/full-screen flag must not change windowed startup.
        normal = self.normalGeometry()
        self.setWindowState(Qt.WindowNoState)
        if normal.isValid():
            self.setGeometry(normal)

        root_widget = QWidget(self)
        root_widget.setObjectName("ApplicationSurface")
        root_widget.setAttribute(Qt.WA_StyledBackground, True)
        self.setCentralWidget(root_widget)
        from ui.window_resize import WindowResizeController
        self._window_resize = WindowResizeController(self)

        root_layout = QVBoxLayout(root_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 2. Верхняя панель навигации
        from ui.plugin_host import build_widget
        self.top_bar = build_widget('topbar.main', TopBar, self)
        root_layout.addWidget(self.top_bar)

        # 3. Основной горизонтальный сплиттер
        self.main_horizontal_splitter = QSplitter(Qt.Horizontal, self)
        self.main_horizontal_splitter.setObjectName("MainDockSplitter")
        self.main_horizontal_splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.main_horizontal_splitter, 1)

        self.left_vertical_splitter = VerticalDockSplitter(self)
        self.left_vertical_splitter.setChildrenCollapsible(False)

        self.right_vertical_splitter = VerticalDockSplitter(self)
        self.right_vertical_splitter.setChildrenCollapsible(False)

        self.left_container = DockColumnContainer(
            self.left_vertical_splitter,
            side="left",
            min_w=MIN_SIDE_PANEL_WIDTH,
            max_w=MAX_SIDE_PANEL_WIDTH,
            parent=self
        )
        self.right_container = DockColumnContainer(
            self.right_vertical_splitter,
            side="right",
            min_w=MIN_SIDE_PANEL_WIDTH,
            max_w=MAX_SIDE_PANEL_WIDTH,
            parent=self
        )

        # Центральный стек рабочих пространств (Chat / Projects / File / Board)
        self.central_stack = QStackedWidget(self)
        self.chat_view = build_widget('workspace.chat', ChatView, self)
        self.projects_view = build_widget('workspace.projects', ProjectsView, self)
        self.file_view = build_widget('workspace.files', FileView, self)
        self.file_view.agent_request_submitted.connect(self._on_editor_agent_request)
        self.chat_view.system_prompt_requested.connect(self._on_system_prompt_requested)
        compressed_signal = getattr(self.chat_view, 'compressed_context_requested', None)
        if compressed_signal is not None:
            compressed_signal.connect(self._on_compressed_context_requested)

        self.central_stack.addWidget(self.chat_view)
        self.central_stack.addWidget(self.projects_view)
        self.central_stack.addWidget(self.file_view)
        self.board_view = None
        self.paint_view = None
        if self.workspace_options['board']:
            from ui.views.board_view import BoardView
            self.board_view = build_widget('workspace.board', BoardView, self)
            self.board_view.agent_request_submitted.connect(self._on_board_agent_request)
        if self.workspace_options['paint']:
            from ui.views.paint_view import PaintView
            self.paint_view = build_widget('workspace.paint', PaintView, self)
            self.paint_view.agent_request_submitted.connect(self._on_paint_agent_request)
            self.paint_view.setObjectName("PaintWorkspace")
        # Empty slots preserve the public workspace indices used by navigation
        # and plugins. They contain no editor, document or background service.
        self.central_stack.addWidget(self.board_view if self.board_view is not None else QWidget(self))
        self.central_stack.addWidget(self.paint_view if self.paint_view is not None else QWidget(self))
        self.top_bar.view_tabs.setTabVisible(3, self.workspace_options['board'])
        self.top_bar.view_tabs.setTabVisible(4, self.workspace_options['paint'])

        # Нижняя панель
        self.bottom_panel = BottomPanel(self)

        # Контейнер центральной области: рабочая плоскость + оверлей
        self.center_container = CentralWorkspaceContainer(self.central_stack, self.bottom_panel, self)
        self.center_container.setMinimumWidth(MIN_CENTER_WIDTH)

        self.main_horizontal_splitter.addWidget(self.left_container)
        self.main_horizontal_splitter.addWidget(self.center_container)
        self.main_horizontal_splitter.addWidget(self.right_container)

        self.main_horizontal_splitter.setStretchFactor(0, 0)
        self.main_horizontal_splitter.setStretchFactor(1, 1)
        self.main_horizontal_splitter.setStretchFactor(2, 0)

        # 4. Менеджер доков
        self.dock_manager = DockManager(
            left_splitter=self.left_vertical_splitter,
            right_splitter=self.right_vertical_splitter,
            left_container=self.left_container,
            right_container=self.right_container,
            main_splitter=self.main_horizontal_splitter
        )

        self._setup_dynamic_panels()
        event_bus.subagent_settings_requested.connect(self._show_subagents_panel)
        event_bus.subagent_api_settings_requested.connect(self._open_subagent_api_settings)
        event_bus.subagent_prompt_requested.connect(self._on_subagent_prompt_requested)

        saved_view = self.state_manager.get("active_view_index", 0)
        # One-time migration from the old Chat/File/Board index layout.
        if not self.state_manager.get("projects_tab_layout_v1", False):
            if saved_view >= 1:
                saved_view += 1
            self.state_manager.set("projects_tab_layout_v1", True)
            self.state_manager.set("active_view_index", saved_view)
        saved_view = max(0, min(int(saved_view), self.central_stack.count() - 1))
        if not self._workspace_index_enabled(saved_view):
            saved_view = CentralWorkspaceContainer.CHAT_VIEW_INDEX
        self.central_stack.setCurrentIndex(saved_view)
        self.top_bar.set_active_tab(saved_view)

        # 5. Размеры боковых панелей
        saved_sizes = self.state_manager.get("splitter_sizes", [MIN_SIDE_PANEL_WIDTH, 720, MIN_SIDE_PANEL_WIDTH])
        raw_l = saved_sizes[0] if len(saved_sizes) > 0 else MIN_SIDE_PANEL_WIDTH
        raw_r = saved_sizes[2] if len(saved_sizes) > 2 else MIN_SIDE_PANEL_WIDTH

        clamped_l = max(MIN_SIDE_PANEL_WIDTH, min(raw_l, MAX_SIDE_PANEL_WIDTH)) if self.left_container.isVisible() else 0
        clamped_r = max(MIN_SIDE_PANEL_WIDTH, min(raw_r, MAX_SIDE_PANEL_WIDTH)) if self.right_container.isVisible() else 0
        clamped_c = max(MIN_CENTER_WIDTH, self.width() - clamped_l - clamped_r)

        self.main_horizontal_splitter.setSizes([clamped_l, clamped_c, clamped_r])

        # Запоминаемые размеры при сворачивании
        self.left_saved_width = clamped_l if clamped_l > 0 else MIN_SIDE_PANEL_WIDTH
        self.right_saved_width = clamped_r if clamped_r > 0 else MIN_SIDE_PANEL_WIDTH
        self.left_collapsed = False
        self.right_collapsed = False

        # 6. Высота нижней панели
        saved_bottom_h = self.state_manager.get("bottom_panel_height", -1)
        if saved_bottom_h == -1:
            self.bottom_panel.current_panel_height = None
            self.bottom_panel.is_full_height = True
        else:
            self.bottom_panel.current_panel_height = max(HEADER_HEIGHT, saved_bottom_h)
            self.bottom_panel.is_full_height = False

        # 7. Подключение сигналов навигации и темы
        self.top_bar.view_tab_changed.connect(self._on_view_changed)
        self.top_bar.view_tab_clicked.connect(self._on_view_tab_clicked)
        self.top_bar.about_action.triggered.connect(lambda: self._open_builtin_doc("about.md"))
        self.top_bar.help_action.triggered.connect(lambda: self._open_builtin_doc("help.md"))
        self.top_bar.onboarding_action.triggered.connect(lambda: self._open_builtin_doc("onboarding.md"))
        event_bus.module_workspace_requested.connect(self.bottom_panel.reveal_module)
        self.top_bar.plugins_action.triggered.connect(self._open_plugins)
        self.top_bar.update_action.triggered.connect(self._open_updates)
        from ui.update_dialog import offer_automatic_check
        tracker = self._startup_tracker
        if tracker is not None and not tracker.is_ready:
            tracker.ready.connect(lambda: QTimer.singleShot(500, lambda: offer_automatic_check(self)))
        else:
            QTimer.singleShot(2000, lambda: offer_automatic_check(self))
        from ui.views.toshy_mcp import open_settings as open_toshy_mcp
        self.top_bar.toshy_mcp_action.triggered.connect(lambda: open_toshy_mcp(self))
        from ui.generation_status import generation_status
        generation_status.changed.connect(self._on_generation_status_changed)
        event_bus.switch_view_tab_requested.connect(self._on_view_changed)
        event_bus.switch_view_tab_requested.connect(self.top_bar.set_active_tab)
        event_bus.open_file_requested.connect(self._on_disabled_workspace_file_requested)
        event_bus.open_file_in_requested.connect(self._open_file_in)
        event_bus.toggle_module_requested.connect(self._on_toggle_module_requested)
        event_bus.task_status_check_requested.connect(self._on_task_status_check_requested)
        event_bus.task_list_send_requested.connect(self._on_task_list_send_requested)
        i18n.language_changed.connect(self._retranslate)
        event_bus.workspace_view_changed.emit(saved_view)

        # Кнопки боковых панелей теперь постоянно находятся в верхней панели.
        self.top_bar.panel_toggle_requested.connect(self._on_toggle_panel)
        self.top_bar.set_panel_collapsed("left", self.left_collapsed)
        self.top_bar.set_panel_collapsed("right", self.right_collapsed)

        file_mgr = self.findChild(QWidget, "FileManagerWidget")
        from ui.project_workspaces import ProjectWorkspaces
        report_startup_phase(tr('ui.caption.e4964f3406ef'))
        self.project_workspaces = ProjectWorkspaces(self, file_mgr)

        from ui.plugin_host import add_components
        add_components(self)
        self.plugin_runtime.emit('ui.ready', {'window': self})
        core_process.start_monitoring()
        QTimer.singleShot(0, self._show_initial_workspace)

    def _show_initial_workspace(self):
        if not self._first_launch:
            self.bottom_panel.reveal_startup()
            return
        for filename in ("help.md", "about.md", "pipelienes_help.md", "onboarding.md"):
            self._open_builtin_doc(filename)
        self.bottom_panel.collapse_panel()
        self.state_manager.save_state({"onboarding_seen": True})

    def _save_appearance(self, *_):
        from ui.themes.manager import theme_manager
        self.state_manager.save_state({'language': i18n.current_lang, 'theme': theme_manager.selected_id})

    def _open_plugins(self):
        if self._close_review is not None:
            return
        if self._plugins_panel is None:
            from ui.plugins_panel import PluginsPanel
            self._plugins_panel = PluginsPanel(self)
            self._plugins_panel.dismissed.connect(self._plugins_dismissed)
        else:
            self._plugins_panel.raise_()
            self._plugins_panel.close_button.setFocus()

    def _plugins_dismissed(self):
        self._plugins_panel = None

    def restore_last_chat(self):
        saved = self.state_manager.get('active_chat') or {}
        if self.chat_view.current_session_id or not saved.get('session'):return
        if core_process.is_running():
            self.state_manager.set('active_chat',{})
            self.chat_view.open_session(saved['session'],saved.get('branch') or 'main')

    def _on_generation_status_changed(self, _session, _branch, state):
        self.top_bar.set_generation_status(state)

    def _workspace_index_enabled(self, index):
        kind = {3: 'board', 4: 'paint'}.get(index)
        return kind is None or self.workspace_options[kind]

    def _on_disabled_workspace_file_requested(self, path):
        extension = Path(path).suffix.lower()
        kind = ('board' if extension == '.canvas' else
                'paint' if extension in {'.tpaint', '.png', '.jpg', '.jpeg'} else None)
        if kind is not None and not self.workspace_options[kind]:
            from ui.widgets.inline_editor import notice
            title = 'Board' if kind == 'board' else 'Sketch'
            notice(self, title + tr('ui.caption.3e2364e4194a'),
                   tr('ui.main_window.enable_v0_in_startup_app_workspaces_dfbc', v0=title, v1=title))

    def _open_file_in(self, path, target):
        from ui.file_opening import open_file_in
        open_file_in(self, path, target)

    def _on_view_changed(self, index: int):
        if not self._workspace_index_enabled(index):
            return
        self.central_stack.setCurrentIndex(index)
        self.center_container.update_workspace_geometry()
        self.bottom_panel.collapse_panel()
        if index in (2,3,4):
            view = self.central_stack.currentWidget()
            if hasattr(view, 'tab_bar'):
                from ui.history_focus import focus_document
                focus_document(view.tab_bar.tabData(view.tab_bar.currentIndex()))
        event_bus.workspace_view_changed.emit(index)
        self.plugin_runtime.emit('ui.workspace.changed', {'index': index, 'widget': self.central_stack.currentWidget()})

    def _on_view_tab_clicked(self, index: int):
        """Repeated click on the active top workspace dismisses BottomPanel."""
        if index != self.central_stack.currentIndex():
            return
        if self.bottom_panel.height() > (HEADER_HEIGHT + 5):
            self.bottom_panel.collapse_panel()

    def _on_editor_agent_request(self, message: str, file_context: object):
        # The user sees a friendly workspace path, while Chat adds the exact
        # filesystem path only to the hidden model payload.
        self.chat_view.send_external_file_message(message, dict(file_context or {}))
        self._on_view_changed(CentralWorkspaceContainer.CHAT_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.CHAT_VIEW_INDEX)

    def _on_paint_agent_request(self, message, context):
        self.chat_view.send_external_image_message(message, context["path"], context.get("finished"))
        self._on_view_changed(CentralWorkspaceContainer.CHAT_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.CHAT_VIEW_INDEX)

    def _on_board_agent_request(self, message: str, canvas_context: object):
        # Board sends a structured directed-graph snapshot. Chat owns both the
        # session write and the lightweight visual projection of that snapshot.
        self.chat_view.send_external_canvas_message(message, dict(canvas_context or {}))
        self._on_view_changed(CentralWorkspaceContainer.CHAT_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.CHAT_VIEW_INDEX)

    def _on_task_status_check_requested(self, session_id: str, branch: str, tasks: object):
        sent = self.chat_view.send_task_status_check(
            str(session_id or ""),
            str(branch or "main"),
            list(tasks or []),
        )
        if sent:
            self._on_view_changed(CentralWorkspaceContainer.CHAT_VIEW_INDEX)
            self.top_bar.set_active_tab(CentralWorkspaceContainer.CHAT_VIEW_INDEX)

    def _on_task_list_send_requested(self, session_id: str, branch: str, tasks: object):
        sent = self.chat_view.send_task_list(
            str(session_id or ""),
            str(branch or "main"),
            list(tasks or []),
        )
        if sent:
            self._on_view_changed(CentralWorkspaceContainer.CHAT_VIEW_INDEX)
            self.top_bar.set_active_tab(CentralWorkspaceContainer.CHAT_VIEW_INDEX)

    def _on_system_prompt_requested(self, session_id: str, branch: str):
        if self.chat_view.raw_request_btn.isChecked():
            from core.raw_request import context_preview
            self._open_system_prompt_preview(context_preview(session_id, branch))
            return
        if not core_process.is_running():
            QMessageBox.warning(self, tr('ui.main_window.system_prompt_692b'), tr('ui.main_window.the_core_is_not_running_6efe'))
            return

        self.async_runner.submit(
            lambda: self.chat_view.session_manager.get_system_prompt_preview(
                session_id or None, branch or "main"
            ),
            self._open_system_prompt_preview,
            lambda error: QMessageBox.warning(
                self, tr('ui.main_window.system_prompt_692b'), tr('ui.main_window.could_not_retrieve_the_system_prompt_9fbf', v0=error)
            ),
        )

    def _open_subagent_api_settings(self, identity):
        self.bottom_panel.open_startup_settings("api")
        page = self.bottom_panel.startup_view.settings_panel._pages["api"]
        identity = identity or self.chat_view.model_selector.resolved_profile_id()
        if identity in page._cards:
            page._expand_connection(identity, True)
            page.ensureWidgetVisible(page._cards[identity])

    def _on_subagent_prompt_requested(self, identity, config):
        from ui.app_state import app_state

        scope = (app_state.session_id, app_state.branch, app_state.workspace)

        def work():
            if core_process.is_running():
                return self.chat_view.session_manager._request(
                    "POST",
                    "/v1/subagents/" + identity + "/prompt-preview",
                    json={"config": config, "session_id": scope[0], "branch": scope[1]},
                    timeout=30,
                )
            from core.context import ExecutionContext
            from modules.system.subagent_manager.history import subagent_system_prompt
            from core.system_prompt import _fenced_text

            context = ExecutionContext.create(workspace_root=scope[2])
            text = (
                "# Системный промпт субагента "
                + str(config["name"])
                + "\n\n"
                + _fenced_text(subagent_system_prompt(config, context))
            )
            text += "\n\nПри запуске в запрос добавляются инструкции доступных модулей и задание."
            return {"markdown": text, "name": config["name"], "id": identity}

        def ready(payload):
            self.file_view.open_temporary_markdown(
                key="subagent_prompt_" + identity,
                title=tr('ui.caption.f79d467744c0') + str(payload["name"]) + ".md",
                content=payload["markdown"],
                breadcrumb="Temporary / Subagent Prompt / " + str(payload["name"]),
            )
            self._on_view_changed(CentralWorkspaceContainer.FILE_VIEW_INDEX)
            self.top_bar.set_active_tab(CentralWorkspaceContainer.FILE_VIEW_INDEX)

        self.async_runner.submit(
            work,
            ready,
            lambda error: QMessageBox.warning(self, tr('ui.main_window.subagent_prompt_2d9c'), str(error)),
        )

    def _open_system_prompt_preview(self, payload):
        markdown = str((payload or {}).get("markdown") or "")
        session_id = str((payload or {}).get("session_id") or "new-chat")
        self.file_view.open_temporary_markdown(
            key="system_prompt_preview",
            title="System Prompt.md",
            content=markdown,
            breadcrumb=f"Temporary / System Prompt / {session_id}",
        )
        self._on_view_changed(CentralWorkspaceContainer.FILE_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.FILE_VIEW_INDEX)

    def _on_compressed_context_requested(self, session_id: str, branch: str):
        def read():
            if core_process.is_running():
                return self.chat_view.session_manager.get_compressed_context_preview(session_id or None, branch or 'main')
            from core.context_api import compressed_context_preview
            from core.session_manager import SessionManager
            return compressed_context_preview(SessionManager(), session_id or None, branch or 'main')

        self.async_runner.submit(read, self._open_compressed_context_preview,
            lambda error: QMessageBox.warning(self, tr('ui.main_window.compacted_context_16f4'), tr('ui.main_window.could_not_open_compacted_context_v0_ab83', v0=error)))

    def _open_compressed_context_preview(self, payload):
        payload = payload or {}
        session_id = str(payload.get('session_id') or 'new-chat')
        branch = str(payload.get('branch') or 'main')
        self.file_view.open_temporary_markdown(
            key='compressed_context_preview', title='Compressed Context.md',
            content=str(payload.get('markdown') or ''),
            breadcrumb=f'Temporary / Compressed Context / {session_id} / {branch}',
        )
        self._on_view_changed(CentralWorkspaceContainer.FILE_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.FILE_VIEW_INDEX)

    def _open_builtin_doc(self, filename: str):
        from ui.builtin_docs import document_path
        try:
            doc_path = document_path(filename)
            content = doc_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError):
            return False

        # Built-in documentation is intentionally read-only. Reuse the same
        # in-memory Markdown tab as System Prompt preview so it can never be
        # accidentally written back into ui/docs.
        self.file_view.open_temporary_markdown(
            key=f"builtin_doc:{filename}",
            title=doc_path.name,
            content=content,
            breadcrumb=f"Built-in / {doc_path.name}",
            base_path=str(doc_path),
        )
        self._on_view_changed(CentralWorkspaceContainer.FILE_VIEW_INDEX)
        self.top_bar.set_active_tab(CentralWorkspaceContainer.FILE_VIEW_INDEX)
        return True

    def _show_subagents_panel(self, identity):
        panel = self.dock_manager.panels.get('subagents_panel')
        if panel is None:return
        slot = self.dock_manager.slots[panel.current_position]
        slot.setCurrentWidget(panel)
        side = 'left' if panel.current_position.value.startswith('left') else 'right'
        if getattr(self, side+'_collapsed', False):self._on_toggle_panel(side)

    def _setup_dynamic_panels(self):
        saved_positions = self.state_manager.get("dock_positions", {})
        registered_panels: List[Tuple[DockPanel, DockSlotPosition]] = []

        # Список панелей приложения с дефолтными позициями
        from ui.plugin_host import build_widget
        from ui.panels.materials_panel import MaterialsPanel
        system_panels = [
            (build_widget('widget.file_manager', FileManagerPanel, self), DockSlotPosition.LEFT_BOTTOM),
            (MaterialsPanel(self), DockSlotPosition.LEFT_BOTTOM),
            (build_widget('widget.sessions', SessionsPanel, self), DockSlotPosition.LEFT_TOP),
            (build_widget('widget.tasks', TasksPanel, self), DockSlotPosition.RIGHT_TOP),
            (build_widget('widget.subagents', SubagentsPanel, self), DockSlotPosition.RIGHT_BOTTOM),
            (build_widget('widget.mcp', McpPanel, self), DockSlotPosition.RIGHT_BOTTOM),
            (build_widget('widget.terminal', TerminalPanel, self), DockSlotPosition.RIGHT_TOP),
        ]
        registered_panels.extend(system_panels)

        # UI активных модулей подключается тем загрузчиком, который раньше существовал,
        # но не участвовал в сборке окна.
        enabled_cfg = ModuleConfigSync().get_enabled_modules_from_config()
        enabled_modules = {
            *(("system", name) for name in enabled_cfg["system"]),
            *(("custom", name) for name in enabled_cfg["custom"]),
        }
        existing_panel_ids = {panel.panel_id for panel, _ in registered_panels}
        for panel, default_pos in ModuleUILoader.discover_module_panels(self.root_dir, enabled_modules, existing_panel_ids):
            if panel.panel_id not in existing_panel_ids:
                registered_panels.append((panel, default_pos))
                existing_panel_ids.add(panel.panel_id)

        for panel, default_pos in registered_panels:
            pos_str = saved_positions.get(panel.panel_id, default_pos.value)
            try:
                target_pos = DockSlotPosition(pos_str)
            except Exception:
                target_pos = default_pos
            self.dock_manager.register_panel(panel, target_pos)

        active_tabs = self.state_manager.get("active_tabs", {})
        self.dock_manager.restore_active_tabs(active_tabs)

    def _on_toggle_module_requested(self, mod_name: str, category: str):
        sync = ModuleConfigSync()
        self.async_runner.submit(
            lambda: sync.toggle_module(mod_name, category, is_core_online=core_process.is_running()),
            lambda enabled: self._on_module_toggle_finished(mod_name, enabled),
        )

    def _on_module_toggle_finished(self, mod_name: str, enabled: bool):
        event_bus.module_status_changed.emit(mod_name, enabled)
        self.bottom_panel.core_hub.reload_modules()

    def _retranslate(self):
        set_text(self, 'ui.main_window.your_workspace_4d02', setter='setWindowTitle')
        self.dock_manager.retranslate_all()

    def _open_updates(self):
        from ui.update_dialog import UpdatesDialog
        self._updates_dialog = UpdatesDialog(self)
        self._updates_dialog.exec()

    def closeEvent(self, event: QCloseEvent):
        if self._plugins_panel is not None:
            self._plugins_panel.dismiss()
        if not self._close_approved:
            event.ignore()
            if self._close_review is None:
                from ui.close_panel import CloseCoordinator
                self._close_review = CloseCoordinator(self, run_dirty_task_status_checks)
            else:
                self._close_review.panel.raise_()
            self._close_review.panel.activate_review()
            return
        self.bottom_panel.modules_view._save_tab_state()

        from ui.shutdown import shutdown_background
        shutdown_background()
        core_process.stop_monitoring()

        layout_state = self.dock_manager.get_layout_state()
        sizes = self.main_horizontal_splitter.sizes()
        safe_l = max(MIN_SIDE_PANEL_WIDTH, min(sizes[0], MAX_SIDE_PANEL_WIDTH)) if len(sizes) > 0 else MIN_SIDE_PANEL_WIDTH
        safe_r = max(MIN_SIDE_PANEL_WIDTH, min(sizes[2], MAX_SIDE_PANEL_WIDTH)) if len(sizes) > 2 else MIN_SIDE_PANEL_WIDTH
        safe_c = sizes[1] if len(sizes) > 1 else 720

        bottom_h_save = -1 if self.bottom_panel.is_full_height else self.bottom_panel.height()

        file_view_state = self.file_view.get_open_state()

        file_mgr = self.findChild(QWidget, "FileManagerWidget")
        expanded_folders = file_mgr.get_expanded_folders() if file_mgr and hasattr(file_mgr, "get_expanded_folders") else []
        workspace_path = file_mgr.current_path if file_mgr and hasattr(file_mgr, "current_path") else ""

        state_to_save = {
            "window_geometry":bytes(self.saveGeometry().toHex()).decode("ascii"),
            "active_chat":{"session":self.chat_view.current_session_id,"branch":self.chat_view.current_branch},
            "language": i18n.current_lang,
            "active_view_index": self.central_stack.currentIndex(),
            "dock_positions": layout_state["dock_positions"],
            "active_tabs": layout_state["active_tabs"],
            "splitter_sizes": [safe_l, safe_c, safe_r],
            "bottom_panel_height": bottom_h_save,
            "workspace_path": workspace_path,
            "expanded_folders": expanded_folders,
            "open_files": file_view_state.get("open_files", []),
            "active_file_index": file_view_state.get("active_file_index", 0)
        }
        for view in (self.board_view, self.paint_view):
            if view is not None:
                state_to_save.update(view.get_open_state())
        state_to_save.update(self.project_workspaces.snapshot())
        self.state_manager.save_state(state_to_save)
        if self.paint_view is not None:
            self.paint_view.shutdown()
        if getattr(self, '_apply_update_after_close', False):
            from release_tools.client import installation_root
            from ui.update_dialog import launch_prepared_update
            try:
                launch_prepared_update(installation_root())
                self._apply_update_after_close = False
            except Exception as error:
                from core.logger import core_logger
                core_logger.warn('UPDATE_HANDOFF', str(error))
                from ui.widgets.dialogs import MessageBox
                MessageBox.warning(self, tr('updates.title'), tr('updates.handoff_failed', error=str(error)))
        event.accept()
        if getattr(self, '_keep_core_running', False):
            from elements.lsp import lsp_manager
            lsp_manager.shutdown_all()
            self.deleteLater()

    def _on_toggle_panel(self, side: str):
        sizes = self.main_horizontal_splitter.sizes()
        if side == "left":
            if self.left_collapsed:
                self.left_collapsed = False
                self.left_container.show()
            else:
                if len(sizes) > 0 and sizes[0] > 0:
                    self.left_saved_width = max(MIN_SIDE_PANEL_WIDTH, min(sizes[0], MAX_SIDE_PANEL_WIDTH))
                self.left_collapsed = True
                self.left_container.hide()
        else:
            if self.right_collapsed:
                self.right_collapsed = False
                self.right_container.show()
            else:
                if len(sizes) > 2 and sizes[2] > 0:
                    self.right_saved_width = max(MIN_SIDE_PANEL_WIDTH, min(sizes[2], MAX_SIDE_PANEL_WIDTH))
                self.right_collapsed = True
                self.right_container.hide()

        l_w = self.left_saved_width if not self.left_collapsed and self.left_container.isVisible() else 0
        r_w = self.right_saved_width if not self.right_collapsed and self.right_container.isVisible() else 0
        c_w = max(MIN_CENTER_WIDTH, self.width() - l_w - r_w)
        self.main_horizontal_splitter.setSizes([l_w, c_w, r_w])

        self.top_bar.set_panel_collapsed(side, self.left_collapsed if side == "left" else self.right_collapsed)
