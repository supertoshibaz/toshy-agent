from ui.i18n import i18n, set_text
from ui.icons import symbol_button

from pathlib import Path

from PySide6.QtWidgets import (
    QFrame,
    QWidget,
    QVBoxLayout,
    QGridLayout,
    QToolButton,
    QTabBar,
    QStackedWidget,
    QPlainTextEdit,
)
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QMouseEvent, QFontDatabase, QTextCursor, QTextOption

from core.paths import APP_PATHS
from ui.views.startup_view import StartupView
from ui.views.graph_view import GraphView
from ui.views.modules_view import ModulesView
from ui.views.module_view import ModuleView
from ui.workspace_geometry import editor_workspace_width, centered_workspace_geometry
from ui.core_bridge.process_manager import core_process

HEADER_HEIGHT = 41


from ui.components.live_log import LiveLogView


class BottomPanelHeader(QFrame):
    """Resizable lower-panel header with workspace-like tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BottomPanelHeader")
        self.setFixedHeight(HEADER_HEIGHT)
        self.setCursor(Qt.SizeVerCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

        # Both controls are overlays. Workspace tabs use the application-window
        # center as their anchor and ignore every other control in this header.
        self.tabs = QTabBar(self)
        self.tabs.setObjectName("BottomPanelTabs")
        self.tabs.setDrawBase(False)
        self.tabs.setCursor(Qt.PointingHandCursor)
        for key in ('tab_startup', 'tab_graph', 'tab_pipeline', 'tab_module', 'tab_log'):
            index = self.tabs.addTab('')
            set_text(self.tabs, key, setter='setTabText', index=index)

        self.toggle_btn = QToolButton(self)
        self.toggle_btn.setObjectName('BottomPanelToggleBtn')
        from ui.icons import set_icon
        set_icon(self.toggle_btn, 'down', size=18)
        self.toggle_btn.setFixedSize(28, 28)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        set_text(self.toggle_btn, 'ui.bottom_panel.collapse_expand_3887', setter='setToolTip')
        set_text(self.toggle_btn, 'ui.bottom_panel.collapse_expand_3887', setter='setAccessibleName')
        self.tabs.raise_()
        self.toggle_btn.raise_()
        QTimer.singleShot(0, self._position_overlay_controls)
        i18n.language_changed.connect(self._schedule_tab_layout)

        self._is_dragging = False
        self._start_global_y = 0

    def _schedule_tab_layout(self):
        QTimer.singleShot(0, self._position_overlay_controls)

    def _position_overlay_controls(self):
        # Workspace tabs: target the geometric center of the top-level window.
        hint = self.tabs.sizeHint()
        tabs_w = min(max(0, hint.width()), self.width())
        top_border = 0 if self.property('sharesTopBorder') else 1
        tabs_h = max(0, self.height()-top_border-1)

        top_level = self.window()
        desired_center = self.mapFrom(
            top_level, QPoint(top_level.width() // 2, 0)
        ).x()
        tabs_x = int(round(desired_center - tabs_w / 2.0))
        tabs_x = max(0, min(tabs_x, max(0, self.width() - tabs_w)))
        tabs_y = top_border
        self.tabs.setGeometry(tabs_x, tabs_y, tabs_w, tabs_h)
        self.toggle_btn.move(max(0, self.width()-self.toggle_btn.width()-10),
                             max(0, (self.height()-self.toggle_btn.height())//2))
        self.toggle_btn.raise_()
        self.tabs.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_overlay_controls()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._position_overlay_controls()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._position_overlay_controls)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._is_dragging = True
            self._start_global_y = event.globalPosition().y()
            self.parentWidget()._on_drag_start(self._start_global_y)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._is_dragging:
            self.parentWidget().queue_drag_move(event.globalPosition().y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._is_dragging:
            self.parentWidget().flush_drag_move(event.globalPosition().y())
            self._is_dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class BottomPanel(QFrame):
    """Resizable lower panel with Startup, Log and Graph tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BottomPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = BottomPanelHeader(self)
        self.header.toggle_btn.clicked.connect(self.toggle_panel)
        layout.addWidget(self.header)

        self.content_area = QFrame(self)
        self.content_area.setObjectName("BottomPanelContent")
        self.content_area.setAttribute(Qt.WA_StyledBackground, True)
        content_layout = QVBoxLayout(self.content_area)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.content_stack = QStackedWidget(self.content_area)
        self.content_stack.setObjectName("BottomPanelStack")
        content_layout.addWidget(self.content_stack)

        # Startup: dedicated core-control area above a Godot-style settings browser.
        startup_page = QWidget(self.content_stack)
        startup_layout = QVBoxLayout(startup_page)
        startup_layout.setContentsMargins(0, 0, 0, 0)
        startup_layout.setSpacing(0)
        from ui.plugin_host import build_widget
        self.startup_view = build_widget('workspace.startup', StartupView, startup_page)
        # Keep the historical attribute used by tab/open animations.
        self.core_hub = self.startup_view.core_hub
        startup_layout.addWidget(self.startup_view)
        self.content_stack.addWidget(startup_page)

        # Graph: runtime structure visualized on the reusable canvas mechanics.
        graph_page = QWidget(self.content_stack)
        graph_page.setObjectName("BottomGraphPage")
        graph_layout = QVBoxLayout(graph_page)
        graph_layout.setContentsMargins(0, 0, 0, 0)
        graph_layout.setSpacing(0)
        self.graph_view = build_widget('workspace.graph', GraphView, graph_page)
        graph_layout.addWidget(self.graph_view)
        self.content_stack.addWidget(graph_page)

        # Modules: visual constructor for custom-module pipelines.
        modules_page = QWidget(self.content_stack)
        modules_page.setObjectName("BottomModulesPage")
        modules_layout = QVBoxLayout(modules_page)
        modules_layout.setContentsMargins(0, 0, 0, 0)
        modules_layout.setSpacing(0)
        self.modules_view = build_widget('workspace.pipelines', ModulesView, modules_page)
        modules_layout.addWidget(self.modules_view)
        self.content_stack.addWidget(modules_page)

        # Module has a full-height surface clipped by this viewport. Lowering
        # the panel moves that surface below the window instead of compressing it.
        self.module_page = QWidget(self.content_stack)
        self.module_view = build_widget('workspace.module', ModuleView, self.module_page)
        self.content_stack.addWidget(self.module_page)

        # Log: same live log stream that used to live in a right-side dock.
        log_page = QWidget(self.content_stack)
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)
        self.log_view = build_widget('workspace.log', LiveLogView, log_page)
        log_layout.addWidget(self.log_view)
        self.content_stack.addWidget(log_page)

        layout.addWidget(self.content_area, 1)

        self.header.tabs.currentChanged.connect(self.content_stack.setCurrentIndex)
        self.header.tabs.currentChanged.connect(self._on_content_tab_changed)
        self.header.tabs.tabBarClicked.connect(self._on_tab_clicked)
        self.content_stack.setCurrentIndex(self.header.tabs.currentIndex())

        self.is_full_height = True
        self.current_panel_height = None
        self._drag_initial_height = HEADER_HEIGHT
        self._pending_drag_y = None
        self._drag_timer = QTimer(self)
        self._drag_timer.setSingleShot(True)
        self._drag_timer.setInterval(16)
        self._drag_timer.timeout.connect(self.flush_drag_move)
        self._core_failure_revealed = False
        core_process.failure_changed.connect(self._on_core_failure_changed)
        if core_process.has_failed():
            QTimer.singleShot(0, self, lambda: self._on_core_failure_changed(core_process.has_failed()))

    def _on_core_failure_changed(self, failed):
        if not failed:
            self._core_failure_revealed = False
            return
        if self._core_failure_revealed:
            return
        self._core_failure_revealed = True
        self.header.tabs.setCurrentIndex(0)
        parent = self.parentWidget()
        self.current_panel_height = parent.height() if parent else 800
        self.is_full_height = True
        self.content_area.show()
        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self.core_hub.play_spawn_animation()
        self._sync_keyboard_scope(focus_modules=False)

    def reveal_startup(self):
        self.header.tabs.setCurrentIndex(0)
        self.content_stack.setCurrentIndex(0)
        parent = self.parentWidget()
        self.current_panel_height = parent.height() if parent else 800
        self.is_full_height = True
        self.content_area.show()
        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self.core_hub.play_spawn_animation()
        self._sync_keyboard_scope(focus_modules=False)

    def reveal_module(self):
        self.header.tabs.setCurrentIndex(3)
        self.content_stack.setCurrentIndex(3)
        parent = self.parentWidget()
        self.current_panel_height = parent.height() if parent else 800
        self.is_full_height = True
        self.content_area.show()
        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self._sync_keyboard_scope(focus_modules=False)

    def open_startup_settings(self, key):
        self.header.tabs.setCurrentIndex(0)
        self._on_tab_clicked(0)
        parent = self.parentWidget()
        self.current_panel_height = parent.height() if parent else 800
        self.is_full_height = True
        self.content_area.show()
        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        settings = self.startup_view.settings_panel
        settings.search_edit.clear()
        item = settings._page_item(key)
        if item is not None:
            ancestor = item.parent()
            while ancestor is not None:
                ancestor.setExpanded(True)
                ancestor = ancestor.parent()
            settings.tree.setCurrentItem(item)
            settings.tree.scrollToItem(item)
            settings._selection_changed()

    def _modules_keyboard_scope_active(self) -> bool:
        return (
            self.header.tabs.currentIndex() == 2
            and self.content_area.isVisible()
            and self.height() > (HEADER_HEIGHT + 5)
        )

    def _set_board_keyboard_enabled(self, enabled: bool):
        main_window = self.window()
        board_view = getattr(main_window, "board_view", None)
        if board_view is None:
            return

        for document in list(getattr(board_view, "open_boards", {}).values()):
            canvas = getattr(document, "canvas", None)
            setter = getattr(canvas, "set_keyboard_commands_enabled", None)
            if callable(setter):
                setter(enabled)

    def _sync_keyboard_scope(self, *, focus_modules: bool = False):
        modules_active = self._modules_keyboard_scope_active()

        # Board remains mounted under BottomPanel, so visibility/focus alone is
        # not a reliable ownership boundary. Disable both of its command paths
        # explicitly while Modules is the active overlay.
        self._set_board_keyboard_enabled(not modules_active)

        if modules_active and focus_modules:
            QTimer.singleShot(0, self.modules_view.focus_canvas)

    def _on_content_tab_changed(self, _index: int):
        parent = self.parentWidget()
        if parent:
            self._position_module_workspace(parent.width(), parent.height())
        self._sync_keyboard_scope(focus_modules=True)

    def _on_tab_clicked(self, index: int):
        self.content_stack.setCurrentIndex(index)

        # A tab click should reveal its contents even when the lower panel is
        # currently collapsed to the header. This also works when the user
        # clicks the tab that is already selected (currentChanged would not).
        if self.height() <= (HEADER_HEIGHT + 5):
            parent = self.parentWidget()
            max_h = parent.height() if parent else 800
            self.current_panel_height = max_h
            self.is_full_height = True
            self.content_area.setVisible(True)
            if parent:
                self.update_layout_geometry(parent.width(), parent.height())

        if index == 0 and self.content_area.isVisible():
            self.core_hub.play_spawn_animation()
        elif index == 1 and self.content_area.isVisible():
            self.graph_view.refresh_graph(force=True)

        self._sync_keyboard_scope(focus_modules=(index == 2))

    def _on_drag_start(self, start_y: float):
        self._drag_timer.stop()
        self._pending_drag_y = None
        self._drag_initial_height = self.height()
        self.raise_()

    def queue_drag_move(self, current_y):
        self._pending_drag_y = current_y
        if not self._drag_timer.isActive():
            self._drag_timer.start()

    def flush_drag_move(self, current_y=None):
        self._drag_timer.stop()
        position = self._pending_drag_y if current_y is None else current_y
        self._pending_drag_y = None
        if position is not None:
            self._on_drag_move(position)

    def _on_drag_move(self, current_y: float):
        delta = self.header._start_global_y - current_y
        target_h = int(self._drag_initial_height + delta)

        parent = self.parentWidget()
        max_h = parent.height() if parent else 800
        clamped_h = max(HEADER_HEIGHT, min(target_h, max_h))

        if clamped_h == self.height():
            return
        self.current_panel_height = clamped_h
        self.is_full_height = clamped_h >= max_h

        is_collapsed = clamped_h <= HEADER_HEIGHT + 2
        self.content_area.setVisible(not is_collapsed)

        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self._sync_keyboard_scope(focus_modules=False)

    def collapse_panel(self):
        self.current_panel_height = HEADER_HEIGHT
        self.is_full_height = False
        self.content_area.setVisible(False)

        parent = self.parentWidget()
        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self._sync_keyboard_scope(focus_modules=False)

    def toggle_panel(self):
        parent = self.parentWidget()
        max_h = parent.height() if parent else 800

        if self.height() <= (HEADER_HEIGHT + 5):
            self.current_panel_height = max_h
            self.is_full_height = True
            self.content_area.setVisible(True)
            if self.header.tabs.currentIndex() == 0:
                self.core_hub.play_spawn_animation()
        else:
            self.collapse_panel()

        if parent:
            self.update_layout_geometry(parent.width(), parent.height())
        self._sync_keyboard_scope(
            focus_modules=self._modules_keyboard_scope_active()
        )

    def update_layout_geometry(self, container_w: int, container_h: int):
        if self.current_panel_height is None or self.is_full_height:
            h = container_h
        else:
            h = max(HEADER_HEIGHT, min(self.current_panel_height, container_h))

        is_collapsed = h <= HEADER_HEIGHT + 2
        self.content_area.setVisible(not is_collapsed)

        y = container_h - h
        self.setGeometry(0, y, container_w, h)
        if not self.header._is_dragging:
            self.raise_()
        shared = h >= container_h
        if self.header.property('sharesTopBorder') != shared:
            self.header.setProperty('sharesTopBorder', shared)
            for widget in (self.header, self.header.tabs):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.updateGeometry()
                widget.update()
        self.header._position_overlay_controls()
        self._position_module_workspace(container_w, container_h)
        self._sync_keyboard_scope(focus_modules=False)
        expanded = h > HEADER_HEIGHT+2
        if self.header.toggle_btn.property('expanded') != expanded:
            self.header.toggle_btn.setProperty('expanded', expanded)
            from ui.icons import set_icon
            set_icon(self.header.toggle_btn, 'down' if expanded else 'up', size=18)

    def _position_module_workspace(self, container_w, container_h):
        # Keep one width and one global anchor when docks open/close. Clamp
        # only when the remaining area can no longer contain that position.
        x, target_w = centered_workspace_geometry(self.module_page, container_w, editor_workspace_width())
        self.module_view.setGeometry(x, 0, target_w, max(0, container_h - HEADER_HEIGHT))

