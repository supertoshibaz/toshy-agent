from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr
import uuid
import math
from typing import Optional

from PySide6.QtWidgets import QGraphicsItem
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QPainterPath,
    QPainterPathStroker,
    QPolygonF,
)


_ACTION_SIZE = 24.0
_ACTION_GAP = 6.0


class CanvasArrowItem(QGraphicsItem):
    """Стрелочка-связь между двумя карточками с поддержкой 8 точек привязки."""

    def __init__(
        self,
        from_card,
        from_side: str,
        to_card,
        to_side: str,
        edge_id: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.edge_id = edge_id or uuid.uuid4().hex[:16]
        self.from_card = from_card
        self.from_side = from_side
        self.to_card = to_card
        self.to_side = to_side
        self.agent_context_marked = False
        self._hover_action = None
        self._action_scale = 1.0

        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)

        self._path = QPainterPath()
        self._arrow_head = QPolygonF()
        self._geometry_key = None
        self._bounds = QRectF()
        self._base_shape = QPainterPath()
        self._selected_shape = None

        self.from_card.connected_arrows.append(self)
        self.to_card.connected_arrows.append(self)
        self.update_path()

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemSceneHasChanged and self.scene() is not None:
            views = self.scene().views()
            if views:
                self.update_control_scale(views[0].transform().m11())
        return result

    def _action_rects(self):
        center = self._path.pointAtPercent(0.5)
        size = _ACTION_SIZE * self._action_scale
        gap = _ACTION_GAP * self._action_scale
        total = size * 2.0 + gap
        left = center.x() - total / 2.0
        top = center.y() - size / 2.0
        return {
            "delete": QRectF(left, top, size, size),
            "reassign": QRectF(
                left + size + gap,
                top,
                size,
                size,
            ),
        }

    def update_control_scale(self, zoom):
        scale = min(1.6, max(1.0, max(0.15, zoom) ** -0.25))
        if abs(scale - self._action_scale) < 1e-6:
            return
        self.prepareGeometryChange()
        self._action_scale = scale
        self._geometry_key = None
        self.update_path()

    def _action_at(self, pos: QPointF):
        if not self.isSelected():
            return None
        for action, rect in self._action_rects().items():
            if rect.contains(pos):
                return action
        return None

    def boundingRect(self) -> QRectF:
        return QRectF(self._bounds)

    def shape(self) -> QPainterPath:
        if not self.isSelected():
            return QPainterPath(self._base_shape)
        if self._selected_shape is None:
            result = QPainterPath(self._base_shape)
            for rect in self._action_rects().values():
                result.addRoundedRect(rect, 5.0, 5.0)
            self._selected_shape = result
        return QPainterPath(self._selected_shape)

    def update_path(self):
        p1 = self.from_card.get_port_scene_pos(self.from_side)
        p2 = self.to_card.get_port_scene_pos(self.to_side)
        key = (p1.x(), p1.y(), p2.x(), p2.y(), self.from_side, self.to_side)
        if key == self._geometry_key:
            return
        self.prepareGeometryChange()
        self._geometry_key = key

        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        dist = math.hypot(dx, dy)
        offset = max(30.0, min(dist * 0.4, 120.0))

        v1 = self._get_side_vector(self.from_side, offset)
        v2 = self._get_side_vector(self.to_side, offset)

        ctrl1 = p1 + v1
        ctrl2 = p2 + v2

        path = QPainterPath(p1)
        path.cubicTo(ctrl1, ctrl2, p2)
        self._path = path

        tangent = p2 - ctrl2
        angle = math.atan2(tangent.y(), tangent.x())
        arrow_size = 11.0

        sin_a = math.sin(angle)
        cos_a = math.cos(angle)

        p_left = QPointF(
            p2.x() - arrow_size * cos_a + (arrow_size / 2.0) * sin_a,
            p2.y() - arrow_size * sin_a - (arrow_size / 2.0) * cos_a,
        )
        p_right = QPointF(
            p2.x() - arrow_size * cos_a - (arrow_size / 2.0) * sin_a,
            p2.y() - arrow_size * sin_a + (arrow_size / 2.0) * cos_a,
        )
        self._arrow_head = QPolygonF([p2, p_left, p_right])
        rect = self._path.boundingRect().adjusted(-20, -20, 20, 20)
        for button in self._action_rects().values():
            rect = rect.united(button.adjusted(-2, -2, 2, 2))
        self._bounds = rect
        stroker = QPainterPathStroker()
        stroker.setWidth(14.0)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        self._base_shape = stroker.createStroke(self._path)
        self._base_shape.addEllipse(p2, 18, 18)
        self._selected_shape = None
        self.update()

    def _get_side_vector(self, side: str, mag: float) -> QPointF:
        d = mag * 0.7071
        vectors = {
            "top": QPointF(0, -mag),
            "bottom": QPointF(0, mag),
            "left": QPointF(-mag, 0),
            "right": QPointF(mag, 0),
            "top_left": QPointF(-d, -d),
            "top_right": QPointF(d, -d),
            "bottom_left": QPointF(-d, d),
            "bottom_right": QPointF(d, d),
        }
        return vectors.get(side, QPointF(0, 0))

    @staticmethod
    def _draw_delete_icon(painter: QPainter, rect: QRectF, color: QColor):
        cx = rect.center().x()
        top = rect.top() + 6.0
        painter.setPen(QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(cx - 5.0, top + 2.0), QPointF(cx + 5.0, top + 2.0))
        painter.drawLine(QPointF(cx - 2.5, top), QPointF(cx + 2.5, top))
        painter.drawRoundedRect(QRectF(cx - 4.0, top + 4.0, 8.0, 8.0), 1.0, 1.0)

    @staticmethod
    def _draw_reassign_icon(painter: QPainter, rect: QRectF, color: QColor):
        c = rect.center()
        painter.setPen(QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(QPointF(c.x() - 6.0, c.y() + 3.0), QPointF(c.x() + 4.0, c.y() - 4.0))
        painter.drawLine(QPointF(c.x() + 4.0, c.y() - 4.0), QPointF(c.x() + 1.0, c.y() - 4.0))
        painter.drawLine(QPointF(c.x() + 4.0, c.y() - 4.0), QPointF(c.x() + 3.0, c.y() - 1.0))

    def _draw_actions(self, painter: QPainter):
        if not self.isSelected():
            return
        for action, rect in self._action_rects().items():
            hovered = self._hover_action == action
            painter.setPen(QPen(theme_manager.color('border.subtle'), 1.0))
            painter.setBrush(theme_manager.color('background.surface') if hovered else theme_manager.color('background.surface'))
            painter.drawRoundedRect(rect, 5.0, 5.0)
            icon = theme_manager.color('text.primary') if hovered else theme_manager.color('text.primary')
            painter.save()
            center = rect.center()
            painter.translate(center)
            painter.scale(self._action_scale, self._action_scale)
            local = QRectF(-12, -12, 24, 24)
            if action == "delete":
                self._draw_delete_icon(painter, local, icon)
            else:
                self._draw_reassign_icon(painter, local, icon)
            painter.restore()

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)

        if self.isSelected():
            pen = QPen(theme_manager.color('accent.primary'), 2.4)
            brush = QBrush(theme_manager.color('accent.primary'))
        elif self.agent_context_marked:
            pen = QPen(QColor("#22c55e"), 2.5)
            brush = QBrush(QColor("#22c55e"))
        else:
            pen = QPen(theme_manager.color('text.secondary'), 1.8)
            brush = QBrush(theme_manager.color('text.secondary'))

        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self._path)

        painter.setBrush(brush)
        painter.drawPolygon(self._arrow_head)
        self._draw_actions(painter)

    def hoverMoveEvent(self, event):
        action = self._action_at(event.pos())
        if action != self._hover_action:
            self._hover_action = action
            bind_caption(self, tr('common.delete') if action == "delete" else tr('ui.caption.685e03b76bf7') if action == "reassign" else "", setter='setToolTip')
            self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        if self._hover_action is not None:
            self._hover_action = None
            bind_caption(self, "", setter='setToolTip')
            self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        action = self._action_at(event.pos()) if event.button() == Qt.LeftButton else None
        if action is not None:
            views = self.scene().views() if self.scene() is not None else []
            view = views[0] if views else None
            if view is not None:
                if action == "delete":
                    handler = getattr(view, "delete_arrow", None)
                    if callable(handler):
                        handler(self)
                        event.accept()
                        return
                elif action == "reassign":
                    handler = getattr(view, "reassign_arrow", None)
                    if callable(handler):
                        handler(self, event.scenePos())
                        event.accept()
                        return
        super().mousePressEvent(event)

    def set_agent_context_marked(self, marked: bool):
        marked = bool(marked)
        if self.agent_context_marked == marked:
            return
        self.agent_context_marked = marked
        self.update()

    def detach(self):
        if self in self.from_card.connected_arrows:
            self.from_card.connected_arrows.remove(self)
        if self in self.to_card.connected_arrows:
            self.to_card.connected_arrows.remove(self)

    def to_dict(self) -> dict:
        return {
            **getattr(self, '_canvas_extra', {}),
            "id": self.edge_id,
            "fromNode": self.from_card.node_id,
            "fromSide": self.from_side,
            "toNode": self.to_card.node_id,
            "toSide": self.to_side,
        }

    def get_tip_scene_pos(self) -> QPointF:
        return self.to_card.get_port_scene_pos(self.to_side)

    def is_near_tip(self, scene_pos: QPointF, threshold: float = 18.0) -> bool:
        tip = self.get_tip_scene_pos()
        return math.hypot(scene_pos.x() - tip.x(), scene_pos.y() - tip.y()) <= threshold
