from typing import Dict, Any, Optional
from PySide6.QtWidgets import (
    QSplitter, QWidget, QApplication, QTabWidget
)
from PySide6.QtCore import (
    Qt, QObject, QEvent, QPoint, QRect
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPixmap
)

from .types import DockSlotPosition
from .dock_slot import DockSlot
from .dock_panel import DockPanel


class LiveDragBadge(QWidget):
    """Точный визуальный слепок вкладки с легкой полупрозрачностью."""
    def __init__(self, tab_pixmap: QPixmap, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.pixmap = tab_pixmap
        self.setFixedSize(self.pixmap.size())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(0.9)  # 90% непрозрачность
        painter.drawPixmap(0, 0, self.pixmap)


class DockManager(QObject):
    """
    Менеджер док-системы с поддержкой бесшовного живого встраивания (Live Docking).
    """
    def __init__(
        self,
        left_splitter: QSplitter,
        right_splitter: QSplitter,
        left_container: Optional[QWidget] = None,
        right_container: Optional[QWidget] = None,
        main_splitter: Optional[QSplitter] = None
    ):
        super().__init__()
        self.left_splitter = left_splitter
        self.right_splitter = right_splitter
        self.left_container = left_container
        self.right_container = right_container
        self.main_splitter = main_splitter

        self.slots: Dict[DockSlotPosition, DockSlot] = {
            DockSlotPosition.LEFT_TOP: DockSlot(DockSlotPosition.LEFT_TOP),
            DockSlotPosition.LEFT_BOTTOM: DockSlot(DockSlotPosition.LEFT_BOTTOM),
            DockSlotPosition.RIGHT_TOP: DockSlot(DockSlotPosition.RIGHT_TOP),
            DockSlotPosition.RIGHT_BOTTOM: DockSlot(DockSlotPosition.RIGHT_BOTTOM),
        }

        self.panels: Dict[str, DockPanel] = {}

        # Переменные живого перетаскивания
        self._active_drag_panel: Optional[DockPanel] = None
        self._drag_badge: Optional[LiveDragBadge] = None

        self.left_splitter.addWidget(self.slots[DockSlotPosition.LEFT_TOP])
        self.left_splitter.addWidget(self.slots[DockSlotPosition.LEFT_BOTTOM])

        self.right_splitter.addWidget(self.slots[DockSlotPosition.RIGHT_TOP])
        self.right_splitter.addWidget(self.slots[DockSlotPosition.RIGHT_BOTTOM])

    def register_panel(self, panel: DockPanel, initial_pos: DockSlotPosition, *, index: int = -1):
        self.panels[panel.panel_id] = panel
        panel.request_move.connect(self.move_panel)
        self.slots[initial_pos].add_dock_panel(panel, index=index)
        self._adjust_splitters()

    def unregister_panel(self, panel_id: str):
        """Remove a dynamically created dock and release its Qt widget."""
        panel = self.panels.get(panel_id)
        if panel is None:
            return
        if self._active_drag_panel is panel:
            self._finish_live_drag()
        self.panels.pop(panel_id, None)
        self.slots[panel.current_position].remove_dock_panel(panel)
        try:
            panel.request_move.disconnect(self.move_panel)
        except (RuntimeError, TypeError):
            pass
        panel.hide()
        panel.setParent(None)
        panel.deleteLater()
        self._adjust_splitters()

    def move_panel(self, panel_id: str, target_pos: DockSlotPosition, target_index: int = -1):
        if panel_id not in self.panels:
            return

        panel = self.panels[panel_id]
        current_pos = panel.current_position

        # Если перемещение внутри того же окна (меняем индекс)
        if current_pos == target_pos:
            old_idx = self.slots[target_pos].indexOf(panel)
            count = self.slots[target_pos].count()
            clamped_idx = max(0, min(target_index, count - 1)) if target_index >= 0 else old_idx

            if clamped_idx != old_idx:
                self.slots[target_pos].tabBar().moveTab(old_idx, clamped_idx)
            self.slots[target_pos].setCurrentWidget(panel)
            return

        # Бесшовный перенос в другой слот
        self.slots[current_pos].remove_dock_panel(panel)
        self.slots[target_pos].add_dock_panel(panel, index=target_index)
        self._adjust_splitters()

    # =========================================================================
    # ДВИЖОК ЖИВОГО ПЕРЕНОСА (LIVE DOCKING ENGINE)
    # =========================================================================

    def start_live_drag(self, panel: DockPanel, tab_pixmap: QPixmap, start_global_pos: QPoint):
        self._active_drag_panel = panel

        # Создаем бейдж с точным графическим слепком вкладки
        self._drag_badge = LiveDragBadge(tab_pixmap)
        self._drag_badge.move(
            start_global_pos.x() - self._drag_badge.width() // 2,
            start_global_pos.y() - self._drag_badge.height() // 2
        )
        self._drag_badge.show()
        self._drag_badge.raise_()

        # Захватываем глобальные события мыши
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._active_drag_panel is not None:
            if event.type() == QEvent.MouseMove:
                global_pos = event.globalPosition().toPoint()
                self._handle_live_drag_move(global_pos)
                return True
            elif event.type() in (QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
                self._finish_live_drag()
                return True
        return super().eventFilter(watched, event)

    def _handle_live_drag_move(self, global_pos: QPoint):
        if not self._active_drag_panel:
            return

        if self._drag_badge:
            self._drag_badge.move(
                global_pos.x() - self._drag_badge.width() // 2,
                global_pos.y() - self._drag_badge.height() // 2
            )

        panel = self._active_drag_panel
        panel_id = panel.panel_id

        # 1. Поиск слота под курсором
        target_slot: Optional[DockSlot] = None
        for slot in self.slots.values():
            if slot.isVisible():
                local_p = slot.mapFromGlobal(global_pos)
                if slot.rect().contains(local_p):
                    target_slot = slot
                    break

        if target_slot:
            local_p = target_slot.mapFromGlobal(global_pos)
            target_idx = target_slot._find_drop_index(local_p)

            # Если мышь вошла в другое окно — мгновенно встраиваем вкладку на лету
            if panel.current_position != target_slot.slot_position:
                self.move_panel(panel_id, target_slot.slot_position, target_index=target_idx)
            else:
                # Если двигаем внутри окна — переставляем вкладки на лету
                old_idx = target_slot.indexOf(panel)
                count = target_slot.count()
                clamped_idx = max(0, min(target_idx, count - 1))
                if clamped_idx != old_idx:
                    target_slot.tabBar().moveTab(old_idx, clamped_idx)
            return

        # 2. Если навели на пустую/монолитную боковую колонку
        for container, side in [(self.left_container, "left"), (self.right_container, "right")]:
            if container and container.isVisible():
                c_local = container.mapFromGlobal(global_pos)
                if container.rect().contains(c_local):
                    y = c_local.y()
                    half_h = container.height() / 2.0
                    if side == "left":
                        target_pos = DockSlotPosition.LEFT_TOP if y < half_h else DockSlotPosition.LEFT_BOTTOM
                    else:
                        target_pos = DockSlotPosition.RIGHT_TOP if y < half_h else DockSlotPosition.RIGHT_BOTTOM

                    if panel.current_position != target_pos:
                        self.move_panel(panel_id, target_pos)
                    return

    def _finish_live_drag(self):
        QApplication.instance().removeEventFilter(self)

        if self._drag_badge:
            self._drag_badge.hide()
            self._drag_badge.deleteLater()
            self._drag_badge = None

        if self._active_drag_panel:
            current_slot = self.slots[self._active_drag_panel.current_position]
            current_slot.setCurrentWidget(self._active_drag_panel)
            self._active_drag_panel = None

        self._adjust_splitters()

    # =========================================================================
    # СОСТОЯНИЕ И СПЛИТТЕРЫ
    # =========================================================================

    def get_layout_state(self) -> Dict[str, Any]:
        positions = {}
        for p_id, panel in self.panels.items():
            positions[p_id] = panel.current_position.value

        active_tabs = {}
        for pos, slot in self.slots.items():
            active_tabs[pos.value] = slot.currentIndex()

        return {
            "dock_positions": positions,
            "active_tabs": active_tabs
        }

    def restore_active_tabs(self, active_tabs: Dict[str, int]):
        for pos_str, idx in active_tabs.items():
            try:
                pos = DockSlotPosition(pos_str)
                if pos in self.slots and 0 <= idx < self.slots[pos].count():
                    self.slots[pos].setCurrentIndex(idx)
            except Exception:
                pass

    def retranslate_all(self):
        for slot in self.slots.values():
            slot.retranslate_tabs()

    def _adjust_splitters(self):
        # 1. Левая колонка
        left_top_count = self.slots[DockSlotPosition.LEFT_TOP].count()
        left_bot_count = self.slots[DockSlotPosition.LEFT_BOTTOM].count()
        left_total = left_top_count + left_bot_count

        if self.left_container:
            self.left_container.setVisible(left_total > 0)

        if left_total > 0:
            if left_top_count > 0 and left_bot_count > 0:
                self.left_splitter.setSizes([500, 500])
            elif left_top_count > 0:
                self.left_splitter.setSizes([1000, 0])
            elif left_bot_count > 0:
                self.left_splitter.setSizes([0, 1000])

        # 2. Правая колонка
        right_top_count = self.slots[DockSlotPosition.RIGHT_TOP].count()
        right_bot_count = self.slots[DockSlotPosition.RIGHT_BOTTOM].count()
        right_total = right_top_count + right_bot_count

        if self.right_container:
            self.right_container.setVisible(right_total > 0)

        if right_total > 0:
            if right_top_count > 0 and right_bot_count > 0:
                self.right_splitter.setSizes([500, 500])
            elif right_top_count > 0:
                self.right_splitter.setSizes([1000, 0])
            elif right_bot_count > 0:
                self.right_splitter.setSizes([0, 1000])

        # 3. Горизонтальный сплиттер
        if self.main_splitter:
            sizes = self.main_splitter.sizes()
            if len(sizes) == 3:
                l_vis = self.left_container.isVisible() if self.left_container else True
                r_vis = self.right_container.isVisible() if self.right_container else True

                new_l = max(340, min(sizes[0], 680)) if l_vis else 0
                new_r = max(340, min(sizes[2], 680)) if r_vis else 0
                total_w = sum(sizes) if sum(sizes) > 0 else 1400
                new_c = max(300, total_w - new_l - new_r)

                self.main_splitter.setSizes([new_l, new_c, new_r])
