from ui.i18n import bind_caption, tr, i18n
from ui.styles.binding import watch_paint
from ui.themes.manager import color
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QMouseEvent

from .types import DockSlotPosition


class DockPositionGridWidget(QWidget):
    position_selected = Signal(DockSlotPosition)

    def __init__(self, current_pos: DockSlotPosition, parent=None):
        super().__init__(parent)
        self.current_pos = current_pos
        self.hovered_pos = None
        self.setFixedSize(180, 140)
        self.setMouseTracking(True)
        watch_paint(self)

    def _get_slot_rects(self):
        w = self.width()
        h = self.height()
        margin = 8
        gap = 4

        col_w = 34
        center_w = w - 2 * margin - 2 * col_w - 2 * gap
        row_h = (h - 2 * margin - gap) / 2

        left_x = margin
        center_x = left_x + col_w + gap
        right_x = center_x + center_w + gap

        top_y = margin
        bottom_y = top_y + row_h + gap

        return {
            DockSlotPosition.LEFT_TOP: QRectF(left_x, top_y, col_w, row_h),
            DockSlotPosition.LEFT_BOTTOM: QRectF(left_x, bottom_y, col_w, row_h),
            DockSlotPosition.RIGHT_TOP: QRectF(right_x, top_y, col_w, row_h),
            DockSlotPosition.RIGHT_BOTTOM: QRectF(right_x, bottom_y, col_w, row_h),
            "center": QRectF(center_x, top_y, center_w, h - 2 * margin),
        }

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rects = self._get_slot_rects()

        # Центральная рабочая зона (темная подложка)
        painter.setBrush(QBrush(color('background.input')))
        painter.setPen(QPen(color('border.subtle'), 1))
        painter.drawRoundedRect(rects["center"], 3, 3)

        # 4 слота
        color_active = color('accent.primary')
        color_inactive = color('background.surface')
        color_hover = color('control.hover')

        for pos, rect in rects.items():
            if pos == "center":
                continue

            if pos == self.current_pos:
                painter.setBrush(QBrush(color_active))
            elif pos == self.hovered_pos:
                painter.setBrush(QBrush(color_hover))
            else:
                painter.setBrush(QBrush(color_inactive))

            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rect, 2, 2)

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position()
        rects = self._get_slot_rects()
        new_hover = None

        for slot_pos, rect in rects.items():
            if slot_pos != "center" and rect.contains(pos):
                new_hover = slot_pos
                break

        if new_hover != self.hovered_pos:
            self.hovered_pos = new_hover
            self.update()

    def leaveEvent(self, event):
        self.hovered_pos = None
        self.update()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            pos = event.position()
            rects = self._get_slot_rects()
            for slot_pos, rect in rects.items():
                if slot_pos != "center" and rect.contains(pos):
                    self.position_selected.emit(slot_pos)
                    break


class DockPositionPopup(QWidget):
    position_changed = Signal(DockSlotPosition)

    def __init__(self, current_pos: DockSlotPosition, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setObjectName("DockPositionPopup")
        self.setAttribute(Qt.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        self.title_label = bind_caption(QLabel('', self), tr("dock_position_title"))
        self.title_label.setObjectName("DockPositionTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

        self.grid_widget = DockPositionGridWidget(current_pos, self)
        self.grid_widget.position_selected.connect(self._on_selected)
        layout.addWidget(self.grid_widget)

        i18n.language_changed.connect(self._retranslate)

    def _on_selected(self, pos: DockSlotPosition):
        self.position_changed.emit(pos)
        self.close()

    def _retranslate(self):
        bind_caption(self.title_label, tr("dock_position_title"))
