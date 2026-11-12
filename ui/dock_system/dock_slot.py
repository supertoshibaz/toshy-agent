from ui.i18n import bind_caption, add_caption_tab, set_text
from ui.styles.binding import apply_style
from ui.icons import symbol_button

from PySide6.QtWidgets import QTabWidget, QToolButton, QTabBar, QApplication, QWidget
from PySide6.QtCore import Qt, QPoint, Signal, QRect, QEvent, QSize
from ui.qt_deferred import defer
from PySide6.QtGui import QMouseEvent, QPainter, QColor, QPen
from ui.themes.manager import theme_manager
from ui.themes.onboarding_theme import onboarding_colors

from .dock_panel import DockPanel
from .dock_position_popup import DockPositionPopup
from .types import DockSlotPosition


class DockCornerSpace(QWidget):
    def sizeHint(self):
        return QSize(28, 28)

    def minimumSizeHint(self):
        return self.sizeHint()


class DockTabBar(QTabBar):
    """Таб-бар, инициирующий живой интерактивный перенос через DockManager."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start_pos = QPoint()
        self._drag_tab_idx = -1
        self.setMovable(False)  # Перемещением полностью управляет живой движок DockManager
        theme_manager.theme_changed.connect(self._theme_changed)

    def _theme_changed(self, *_):
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # QSS has no per-index tab fill. Draw only the onboarding tab on top
        # of the standard, fully theme-controlled dock tabs.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colors = onboarding_colors()
        for index in range(self.count()):
            if self.tabData(index) != "onboarding":
                continue
            rect = self.tabRect(index).adjusted(0, 0, -2, -1)
            if not rect.isValid() or not rect.intersects(event.rect()):
                continue
            painter.save()
            painter.setClipRect(rect)
            painter.setBrush(QColor(colors["tab"]))
            painter.setPen(QPen(QColor(colors["outline"]), 1.2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, 0), 4, 4)
            painter.setPen(QColor(colors["tab_text"]))
            painter.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignCenter | Qt.TextSingleLine, self.tabText(index))
            painter.restore()
        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.pos()
            self._drag_tab_idx = self.tabAt(event.pos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if event.buttons() & Qt.LeftButton and self._drag_tab_idx >= 0:
            dist = (event.pos() - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                dock_slot = self.parent()
                if isinstance(dock_slot, QTabWidget):
                    panel = dock_slot.widget(self._drag_tab_idx)
                    if isinstance(panel, DockPanel):
                        main_win = self.window()
                        if hasattr(main_win, "dock_manager"):
                            # Снимаем точный визуальный слепок вкладки прямо с таб-бара
                            tab_rect = self.tabRect(self._drag_tab_idx)
                            tab_pixmap = self.grab(tab_rect)

                            main_win.dock_manager.start_live_drag(
                                panel=panel,
                                tab_pixmap=tab_pixmap,
                                start_global_pos=event.globalPosition().toPoint()
                            )
                            self._drag_tab_idx = -1
                            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_tab_idx = -1
        super().mouseReleaseEvent(event)


class DockSlot(QTabWidget):
    """Слот док-панели с поддержкой расчета проекции вкладок."""
    def __init__(self, slot_position: DockSlotPosition, parent=None):
        super().__init__(parent)
        self.slot_position = slot_position
        self.setDocumentMode(True)
        self.setProperty('toshyDock', True)
        self.setMinimumHeight(140)

        # Кастомный таб-бар
        self.setTabBar(DockTabBar(self))
        self.tabBar().setProperty('toshyDockTabs', True)

        tab_bar = self.tabBar()
        tab_bar.setUsesScrollButtons(True)
        tab_bar.setElideMode(Qt.ElideNone)

        self.pos_button = QToolButton(self)
        symbol_button(self.pos_button, "⛶")
        set_text(self.pos_button, 'ui.dock_slot.dock_position_fdf4', setter='setToolTip')
        apply_style(self.pos_button, "border: none; padding: 2px 4px; font-size: 13px; color: @text.secondary@;")
        self.pos_button.clicked.connect(self._show_position_popup)
        self._position_corner = DockCornerSpace(self)
        self._position_corner.setFixedSize(28, 28)
        self.setCornerWidget(self._position_corner, Qt.TopRightCorner)
        self.tabBar().installEventFilter(self)
        self._position_corner.installEventFilter(self)

        self.currentChanged.connect(self._on_tab_changed)
        self._update_visibility()

    def _position_button(self):
        bar = self.tabBar()
        if self.count() == 0:
            return
        rect = bar.tabRect(max(0, bar.currentIndex()))
        center = bar.mapTo(self, rect.center())
        corner = self._position_corner.mapTo(self, QPoint())
        self.pos_button.move(corner.x(), center.y() - self.pos_button.height() // 2)
        self.pos_button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        defer(0, self, self._position_button)

    def eventFilter(self, watched, event):
        if watched in (self.tabBar(), self._position_corner) and event.type() in (QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            defer(0, self, self._position_button)
        return super().eventFilter(watched, event)

    def _find_drop_index(self, local_pos: QPoint) -> int:
        """Определяет точный индекс вставки вкладки по координате X."""
        tab_bar = self.tabBar()
        count = tab_bar.count()
        if count == 0:
            return 0

        tab_bar_x = tab_bar.mapFrom(self, local_pos).x()

        for i in range(count):
            rect = tab_bar.tabRect(i)
            if tab_bar_x < rect.center().x():
                return i

        return count - 1

    def add_dock_panel(self, panel: DockPanel, index: int = -1):
        panel.current_position = self.slot_position
        if 0 <= index <= self.count():
            idx = self.insertTab(index, panel, panel.get_title())
        else:
            idx = add_caption_tab(self, panel, panel.get_title())
        if panel.panel_id == "onboarding_panel":
            self.tabBar().setTabData(idx, "onboarding")
        self.setCurrentIndex(idx)
        self._update_visibility()

    def remove_dock_panel(self, panel: DockPanel):
        idx = self.indexOf(panel)
        if idx != -1:
            self.removeTab(idx)
        self._update_visibility()

    def retranslate_tabs(self):
        for idx in range(self.count()):
            widget = self.widget(idx)
            if isinstance(widget, DockPanel):
                bind_caption(self, widget.get_title(), setter='setTabText', index=idx)

    def _update_visibility(self):
        has_tabs = self.count() > 0
        self.setVisible(has_tabs)
        self.pos_button.setVisible(has_tabs)
        if has_tabs:
            defer(0, self, self._position_button)

    def _on_tab_changed(self, index: int):
        self._update_visibility()

    def _show_position_popup(self):
        current_panel = self.currentWidget()
        if not isinstance(current_panel, DockPanel):
            return

        popup = DockPositionPopup(self.slot_position, self)

        def on_pos_changed(new_pos: DockSlotPosition):
            current_panel.request_move.emit(current_panel.panel_id, new_pos)

        popup.position_changed.connect(on_pos_changed)

        btn_global_pos = self.pos_button.mapToGlobal(QPoint(0, self.pos_button.height()))
        popup.move(btn_global_pos.x() - 140, btn_global_pos.y() + 4)
        popup.show()
