from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text, i18n
from ui.styles.binding import apply_style
from ui.icons import symbol_button

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QTabBar,
    QGridLayout, QLabel, QToolButton, QMenu
)
from PySide6.QtCore import Signal, Qt, QPoint, QTimer, QVariantAnimation, QSize, QRectF, QEvent, QAbstractAnimation
from PySide6.QtGui import QMouseEvent, QColor, QPainter, QPixmap, QIcon, QPen
from PySide6.QtSvgWidgets import QSvgWidget
from ui.application_icon import LOGO_PATH
from collections import deque
from ui.app_state import app_state
from ui.generation_status import STATES
from ui.core_bridge.process_manager import core_process

BUTTON_HEIGHT = 28  # Единая фиксированная высота

GENERATION_STATES = STATES


class StatusFlash(QWidget):
    """A faint tint over the header, without intercepting its controls."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet('background: transparent; border: none;')
        self.color = QColor()
        self.opacity = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(1650)
        self.animation.setStartValue(0.0)
        self.animation.setKeyValueAt(0.12, 0.10)
        self.animation.setEndValue(0.0)
        self.animation.valueChanged.connect(self._set_opacity)
        self.pending = deque()
        self.animation.finished.connect(self._next)
        self.hide()

    def _set_opacity(self, value):
        self.opacity = value
        self.update()

    def start(self, color):
        self.pending.append(color)
        if self.animation.state() != QAbstractAnimation.Running:
            self._next()

    def _next(self):
        if not self.pending:
            self.hide()
            return
        color = self.pending.popleft()
        self.color = QColor(color)
        self.opacity = 0.0
        self.setGeometry(self.parentWidget().rect())
        self.show(); self.raise_()
        self.animation.start()

    def paintEvent(self, event):
        color = QColor(self.color)
        color.setAlphaF(self.opacity)
        painter = QPainter(self)
        painter.fillRect(self.rect(), color)


class TopBar(QWidget):
    view_tab_changed = Signal(int)
    view_tab_clicked = Signal(int)
    panel_toggle_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setAttribute(Qt.WA_StyledBackground, True)

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        grid_container = QWidget(self)
        grid_layout = QGridLayout(grid_container)
        grid_layout.setContentsMargins(5, 0, 10, 0)
        grid_layout.setSpacing(0)

        # 1. Левая часть: название, меню приложения и статус генерации.
        left_box = QWidget(grid_container)
        left_layout = QHBoxLayout(left_box)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self.title_label = localize(QLabel('', left_box), 'ui.top_bar.toshy_agent_studio_355a')
        self.title_label.setObjectName("TopBarTitleLabel")
        apply_style(self.title_label, "color: @text.primary@; font-weight: bold; font-size: 12px; margin-right: 2px;")
        bind_caption(self.title_label, 'by supertoshibaz', setter='setToolTip')

        self.logo_placeholder = QSvgWidget(left_box)
        self.logo_placeholder.setObjectName("TopBarLogo")
        self.logo_placeholder.setFixedSize(32, 32)
        set_text(self.logo_placeholder, 'ui.top_bar.application_logo_1a41', setter='setToolTip')
        set_text(self.logo_placeholder, 'ui.top_bar.toshy_agent_studio_355a', setter='setAccessibleName')
        if LOGO_PATH.is_file():
            self.logo_placeholder.load(str(LOGO_PATH))

        left_layout.addWidget(self.logo_placeholder)
        left_layout.addWidget(self.title_label)
        self.app_menu_button = QToolButton(left_box)
        self.app_menu_button.setObjectName('TopBarAppBtn')
        self.app_menu_button.setFixedSize(28, BUTTON_HEIGHT)
        self.app_menu_button.setStyleSheet('#TopBarAppBtn {padding: 0px;}')
        from ui.icons import set_icon
        set_icon(self.app_menu_button, 'info', size=18)
        set_text(self.app_menu_button, 'ui.top_bar.about_this_application_4b54', setter='setAccessibleName')
        set_text(self.app_menu_button, 'ui.top_bar.application_menu_6d1c', setter='setToolTip')
        self.app_menu_button.setCursor(Qt.PointingHandCursor)
        self.app_menu = QMenu(self)
        from ui.icons import bind_popup
        bind_popup(self.app_menu, self.app_menu_button)
        self.app_menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self._reset_app_menu_button))
        self.about_action = add_caption_action(self.app_menu, 'addAction', tr('btn_about', 'About'))
        self.help_action = add_caption_action(self.app_menu, 'addAction', tr('btn_help', 'Help'))
        self.onboarding_action = add_caption_action(self.app_menu, 'addAction', tr('docs.onboarding'))
        self.toshy_mcp_action = add_caption_action(self.app_menu, 'addAction', tr('ui.menu.toshy_mcp'))
        self.plugins_action = localize(add_caption_action(self.app_menu, 'addAction', 'Plugins'), 'ui.top_bar.plugins_9514')
        self.update_action = localize(add_caption_action(self.app_menu, 'addAction', 'Update'), 'ui.top_bar.update_c1c1')
        self.app_menu_button.clicked.connect(self._show_app_menu)
        left_layout.addWidget(self.app_menu_button)

        self.left_panel_btn = QToolButton(left_box)
        self.left_panel_btn.setObjectName("TopBarPanelToggleBtn")
        symbol_button(self.left_panel_btn, "◀")
        self.left_panel_btn.setFixedSize(28, 28)
        self.left_panel_btn.setCursor(Qt.PointingHandCursor)
        set_text(self.left_panel_btn, 'ui.top_bar.collapse_left_sidebar_937d', setter='setToolTip')
        self.left_panel_btn.clicked.connect(lambda: self.panel_toggle_requested.emit("left"))
        left_layout.addWidget(self.left_panel_btn)

        # 2. Центральная часть: Вкладки рабочих областей
        center_box = QWidget(grid_container)
        center_layout = QHBoxLayout(center_box)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)

        self.view_tabs = QTabBar(center_box)
        self.view_tabs.setObjectName("TopWorkspaceTabs")
        self.view_tabs.setFixedHeight(40)
        self.view_tabs.setDrawBase(False)
        self.view_tabs.addTab(tr("tab_chat"))
        self.view_tabs.addTab(tr("tab_projects", "Projects"))
        self.view_tabs.addTab(tr("tab_files"))
        self.view_tabs.addTab(tr("tab_board"))
        self.view_tabs.addTab(tr("tab_paint"))
        self.view_tabs.currentChanged.connect(self.view_tab_changed.emit)
        # Unlike currentChanged, this also fires when the already active tab
        # is clicked again.
        self.view_tabs.tabBarClicked.connect(self.view_tab_clicked.emit)
        center_layout.addWidget(self.view_tabs)

        # 3. Правая часть: кнопка правой панели и управление окном
        right_box = QWidget(grid_container)
        right_layout = QHBoxLayout(right_box)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.right_panel_btn = QToolButton(right_box)
        self.right_panel_btn.setObjectName("TopBarPanelToggleBtn")
        symbol_button(self.right_panel_btn, "▶")
        self.right_panel_btn.setFixedSize(28, 28)
        self.right_panel_btn.setCursor(Qt.PointingHandCursor)
        set_text(self.right_panel_btn, 'ui.top_bar.collapse_right_sidebar_b085', setter='setToolTip')
        self.right_panel_btn.clicked.connect(lambda: self.panel_toggle_requested.emit("right"))
        right_layout.addWidget(self.right_panel_btn)

        # Оконные кнопки управления
        self.btn_min = QToolButton(right_box)
        self.btn_min.setObjectName("WinMinBtn")
        symbol_button(self.btn_min, "—")
        set_text(self.btn_min, 'ui.top_bar.minimize_36c2', setter='setToolTip')
        self.btn_min.clicked.connect(self._minimize)

        self.btn_max = QToolButton(right_box)
        self.btn_max.setObjectName("WinMaxBtn")
        symbol_button(self.btn_max, "□")
        set_text(self.btn_max, 'ui.top_bar.maximize_restore_d813', setter='setToolTip')
        self.btn_max.clicked.connect(self._toggle_maximize)

        self.btn_close = QToolButton(right_box)
        self.btn_close.setObjectName("WinCloseBtn")
        symbol_button(self.btn_close, "✕")
        set_text(self.btn_close, 'ui.top_bar.close_a7a4', setter='setToolTip')
        self.btn_close.clicked.connect(self._close_window)

        right_layout.addWidget(self.btn_min)
        right_layout.addWidget(self.btn_max)
        right_layout.addWidget(self.btn_close)

        apply_style(self, """
            #WinMinBtn, #WinMaxBtn, #WinCloseBtn {
                background: transparent;
                border: 1px solid @border.subtle@;
                border-radius: 6px;
                color: @text.secondary@;
                font-size: 13px;
                min-width: 26px;
                max-width: 26px;
                min-height: 26px;
                max-height: 26px;
                padding: 0px;
            }
            #WinMinBtn:hover, #WinMaxBtn:hover {
                background-color: @control.hover@;
                color: @text.primary@;
            }
            #WinCloseBtn:hover {
                background-color: #ef4444;
                color: @text.primary@;
            }
        """)

        # Side controls stay in the normal layout. Workspace tabs are an
        # overlay and therefore remain at the true geometric center regardless
        # of how much space the left/right controls occupy.
        grid_layout.addWidget(left_box, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)
        grid_layout.addWidget(right_box, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        grid_layout.setColumnStretch(0, 0)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.setColumnStretch(2, 0)

        self.plugin_slots = {}
        for name, parent_box, layout in (('ui.topbar.left', left_box, left_layout), ('ui.topbar.right', right_box, right_layout)):
            slot = QWidget(parent_box)
            slot_layout = QHBoxLayout(slot)
            slot_layout.setContentsMargins(0, 0, 0, 0)
            slot_layout.setSpacing(4)
            layout.insertWidget(0 if name.endswith('right') else layout.count(), slot)
            self.plugin_slots[name] = slot_layout
            from ui.plugin_host import populate_slot
            populate_slot(name, slot_layout, parent=slot)
        self._right_box = right_box
        self._left_box = left_box
        self._grid_container = grid_container
        self._workspace_tabs_box = center_box
        self._workspace_tabs_box.raise_()

        outer_layout.addWidget(grid_container)
        from ui.components.token_usage_label import TokenUsageLabel
        self.token_usage_label = TokenUsageLabel(grid_container)
        self.token_usage_label.hide()
        from ui.components.session_updates import SessionUpdatesLabel
        self.session_updates_label = SessionUpdatesLabel(self.token_usage_label, grid_container)
        self.view_tabs.currentChanged.connect(self._position_workspace_tabs)
        app_state.session_changed.connect(self._position_workspace_tabs)
        self.status_flash = StatusFlash(self)
        self._core_failure_flashed = False
        core_process.failure_changed.connect(self._on_core_failure_changed)
        if core_process.has_failed():
            self._on_core_failure_changed(True)
        self.left_panel_btn.installEventFilter(self)
        self._left_box.installEventFilter(self)
        self._right_box.installEventFilter(self)
        self.right_panel_btn.installEventFilter(self)
        self.view_tabs.installEventFilter(self)

        i18n.language_changed.connect(self._retranslate)
        QTimer.singleShot(0, self._position_workspace_tabs)

    def _position_workspace_tabs(self):
        from shiboken6 import isValid
        if not isValid(self) or not isValid(self._workspace_tabs_box):
            return
        if not hasattr(self, "_grid_container") or not hasattr(self, "_workspace_tabs_box"):
            return
        container = self._grid_container
        box = self._workspace_tabs_box
        hint = box.sizeHint()
        width = min(max(0, hint.width()), container.width())

        top_level = self.window()
        desired_center = container.mapFrom(
            top_level, QPoint(top_level.width() // 2, 0)
        ).x()
        x = int(round(desired_center - width / 2.0))
        x = max(0, min(x, max(0, container.width() - width)))

        box.setGeometry(x, 0, width, container.height())
        box.layout().activate()
        chat_left = self.view_tabs.mapTo(container, self.view_tabs.tabRect(0).topLeft()).x()
        # Keep the title/sidebar controls clear of the centred workspace tabs.
        available = max(0, chat_left - 10 - 16)
        static_width = 22 + 28 + 28 + 3 * 4
        title_width = min(self.title_label.fontMetrics().horizontalAdvance('Toshy Agent Studio') + 4,
                          max(0, available - static_width - 120))
        self.title_label.setFixedWidth(title_width)
        bind_caption(self.title_label, self.title_label.fontMetrics().elidedText('Toshy Agent Studio', Qt.ElideRight, title_width))
        bind_caption(self.title_label, 'by supertoshibaz', setter='setToolTip')
        box.raise_()
        self._position_token_usage(chat_left)
        self._position_session_updates()
        self.status_flash.setGeometry(self.rect())
        if self.status_flash.isVisible():
            self.status_flash.raise_()

    def _position_session_updates(self):
        if not hasattr(self, 'session_updates_label'):
            return
        container = self._grid_container
        rect = self.view_tabs.tabRect(self.view_tabs.count() - 1)
        left = self.view_tabs.mapTo(container, rect.topRight()).x() + 7
        right = self.right_panel_btn.mapTo(container, QPoint()).x() - 6
        plugin = self.plugin_slots['ui.topbar.right'].parentWidget()
        if plugin.width() > 0:
            right = min(right, plugin.mapTo(container, QPoint()).x() - 6)
        self.session_updates_label.setGeometry(left, 0, max(0, right - left), container.height())
        self.session_updates_label.setVisible(right - left >= 40)
        self.session_updates_label.raise_()

    def _position_token_usage(self, chat_left):
        if not hasattr(self, 'token_usage_label'):
            return
        label = self.token_usage_label
        if self.view_tabs.currentIndex() != 0 or not app_state.session_id:
            label.hide()
            return
        left = self.left_panel_btn.mapTo(self._grid_container, QPoint(self.left_panel_btn.width(), 0)).x()
        right = chat_left
        midpoint = (left+right)/2
        free_left = left+6
        slot = self.plugin_slots['ui.topbar.left'].parentWidget()
        if slot.width() > 0:
            free_left = max(free_left, slot.mapTo(self._grid_container, QPoint(slot.width(), 0)).x()+6)
        width = max(0, int(2*min(midpoint-free_left, right-6-midpoint)))
        label.setGeometry(round(midpoint-width/2), 0, width, self._grid_container.height())
        label.setVisible(width >= 50)
        label.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_workspace_tabs()

    def eventFilter(self, watched, event):
        if watched in (self.left_panel_btn, self.right_panel_btn, self.view_tabs, self._left_box, self._right_box) and event.type() in (QEvent.Move, QEvent.Resize):
            # Qt can place layout children after the header's resizeEvent.
            QTimer.singleShot(0, self._position_workspace_tabs)
        return super().eventFilter(watched, event)

    def set_active_tab(self, index: int):
        if 0 <= index < self.view_tabs.count() and self.view_tabs.isTabVisible(index):
            self.view_tabs.setCurrentIndex(index)

    def _retranslate(self):
        bind_caption(self.about_action, tr("btn_about", "About"))
        bind_caption(self.help_action, tr("btn_help", "Help"))
        bind_caption(self.onboarding_action, tr("docs.onboarding"))
        bind_caption(self.view_tabs, tr("tab_chat"), setter='setTabText', index=0)
        bind_caption(self.view_tabs, tr("tab_projects", "Projects"), setter='setTabText', index=1)
        bind_caption(self.view_tabs, tr("tab_files"), setter='setTabText', index=2)
        bind_caption(self.view_tabs, tr("tab_board"), setter='setTabText', index=3)
        bind_caption(self.view_tabs, tr("tab_paint"), setter='setTabText', index=4)
        QTimer.singleShot(0, self._position_workspace_tabs)

    def _reset_app_menu_button(self):
        from PySide6.QtGui import QCursor
        button = self.app_menu_button
        button.setDown(False)
        button.setAttribute(Qt.WA_UnderMouse, button.rect().contains(button.mapFromGlobal(QCursor.pos())))
        button.update()

    def _show_app_menu(self):
        if self.app_menu.isVisible():
            self.app_menu.hide()
        else:
            self.app_menu.popup(self.app_menu_button.mapToGlobal(QPoint(0, self.app_menu_button.height())))

    def set_generation_status(self, state):
        if core_process.has_failed():
            self._on_core_failure_changed(True)
            return
        if state in GENERATION_STATES:
            self.status_flash.start(GENERATION_STATES[state][0])

    def _on_core_failure_changed(self, failed):
        if not failed:
            self._core_failure_flashed = False
            return
        if self._core_failure_flashed:
            return
        self._core_failure_flashed = True
        flash = self.status_flash
        color = QColor(GENERATION_STATES["error"][0])
        already_red = (
            flash.animation.state() == QAbstractAnimation.Running and flash.color == color
        )
        flash.pending.clear()
        if not already_red:
            flash.animation.stop()
            flash.start(color)

    def set_panel_collapsed(self, side: str, collapsed: bool):
        if side == "left":
            symbol_button(self.left_panel_btn, "▶" if collapsed else "◀")
            bind_caption(self.left_panel_btn, "Expand left sidebar" if collapsed else "Collapse left sidebar", setter='setToolTip')
        elif side == "right":
            symbol_button(self.right_panel_btn, "◀" if collapsed else "▶")
            bind_caption(self.right_panel_btn, "Expand right sidebar" if collapsed else "Collapse right sidebar", setter='setToolTip')

    # =========================================================================
    # ПЕРЕМЕЩЕНИЕ И УПРАВЛЕНИЕ БЕЗРАМОЧНЫМ ОКНОМ
    # =========================================================================

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            win = self.window()
            if win.windowHandle():
                win.windowHandle().startSystemMove()
            else:
                self._drag_pos = event.globalPosition().toPoint() - win.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if hasattr(self, "_drag_pos") and (event.buttons() & Qt.LeftButton):
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if hasattr(self, "_drag_pos"):
            delattr(self, "_drag_pos")
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _minimize(self):
        self.window().showMinimized()

    def _toggle_maximize(self):
        win = self.window()
        if win.isMaximized():
            win.showNormal()
        else:
            win.showMaximized()
        self.update_max_button()

    def _close_window(self):
        self.window().close()

    def update_max_button(self):
        win = self.window()
        if win.isMaximized():
            symbol_button(self.btn_max, "🗗")
        else:
            symbol_button(self.btn_max, "□")
