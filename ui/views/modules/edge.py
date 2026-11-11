from __future__ import annotations
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr

import math
import uuid

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

from .nodes.base import PORT_RADIUS, PipelineNodeItem
from .nodes.specs import STREAM_EXEC, stream_color


_EDGE_ACTION_SIZE = 24.0
_EDGE_ACTION_GAP = 6.0


class PipelineEdgeItem(QGraphicsPathItem):
    def __init__(
        self,
        source: "PipelineNodeItem",
        source_port: str,
        target: "PipelineNodeItem",
        target_port: str,
    ):
        super().__init__()
        self.edge_id = uuid.uuid4().hex[:16]
        self.source = source
        self.source_port = str(source_port)
        self.target = target
        self.target_port = str(target_port)
        self._arrow_head = QPolygonF()
        self._hover_action = None

        source.edges.append(self)
        target.edges.append(self)

        self.setZValue(-5.0)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        # Keep a generous transparent hit area around the visible edge.
        self.setPen(QPen(Qt.transparent, 14.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setAcceptHoverEvents(True)
        self.update_path()

    @property
    def stream(self) -> str:
        port = self.source.port_spec("output", self.source_port)
        return port.stream if port is not None else ""

    def attach(self):
        if self not in self.source.edges:
            self.source.edges.append(self)
        if self not in self.target.edges:
            self.target.edges.append(self)
        self.source.normalize_dynamic_ports()
        self.target.normalize_dynamic_ports()
        self.update_path()

    def detach(self):
        if self in self.source.edges:
            self.source.edges.remove(self)
        if self in self.target.edges:
            self.target.edges.remove(self)
        self.source.normalize_dynamic_ports()
        self.target.normalize_dynamic_ports()

    def update_path(self):
        self.prepareGeometryChange()
        source_center = self.mapFromScene(self.source.port_scene_pos("output", self.source_port))
        target_center = self.mapFromScene(self.target.port_scene_pos("input", self.target_port))

        start = QPointF(source_center.x() + PORT_RADIUS, source_center.y())
        end = QPointF(target_center.x() - PORT_RADIUS, target_center.y())

        dx = end.x() - start.x()
        offset = max(55.0, min(abs(dx) * 0.45, 180.0))

        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x() + offset, start.y()),
            QPointF(end.x() - offset, end.y()),
            end,
        )
        self.setPath(path)

        tangent_start = QPointF(end.x() - offset, end.y())
        tangent = end - tangent_start
        angle = math.atan2(tangent.y(), tangent.x())
        size = 8.0
        left = QPointF(
            end.x() - math.cos(angle - 0.55) * size,
            end.y() - math.sin(angle - 0.55) * size,
        )
        right = QPointF(
            end.x() - math.cos(angle + 0.55) * size,
            end.y() - math.sin(angle + 0.55) * size,
        )
        self._arrow_head = QPolygonF([end, left, right])
        self.update()

    def get_tip_scene_pos(self) -> QPointF:
        return self.mapToScene(self.path().pointAtPercent(1.0))

    def is_near_tip(self, scene_pos: QPointF, threshold: float = 18.0) -> bool:
        tip = self.get_tip_scene_pos()
        return math.hypot(scene_pos.x() - tip.x(), scene_pos.y() - tip.y()) <= threshold

    def _action_rects(self):
        center = self.path().pointAtPercent(0.5)
        total = _EDGE_ACTION_SIZE * 2.0 + _EDGE_ACTION_GAP
        left = center.x() - total / 2.0
        top = center.y() - _EDGE_ACTION_SIZE / 2.0
        return {
            "delete": QRectF(left, top, _EDGE_ACTION_SIZE, _EDGE_ACTION_SIZE),
            "reassign": QRectF(
                left + _EDGE_ACTION_SIZE + _EDGE_ACTION_GAP,
                top,
                _EDGE_ACTION_SIZE,
                _EDGE_ACTION_SIZE,
            ),
        }

    def _action_at(self, pos: QPointF):
        if not self.isSelected():
            return None
        for action, rect in self._action_rects().items():
            if rect.contains(pos):
                return action
        return None

    def boundingRect(self) -> QRectF:
        rect = super().boundingRect()
        tip = self.path().pointAtPercent(1.0)
        rect = rect.united(QRectF(tip.x() - 18, tip.y() - 18, 36, 36))
        for button in self._action_rects().values():
            rect = rect.united(button.adjusted(-2.0, -2.0, 2.0, 2.0))
        return rect

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14.0)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        result = stroker.createStroke(self.path())
        result.addEllipse(self.path().pointAtPercent(1.0), 18, 18)
        if self.isSelected():
            for rect in self._action_rects().values():
                result.addRoundedRect(rect, 5.0, 5.0)
        return result

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
            if action == "delete":
                self._draw_delete_icon(painter, rect, icon)
            else:
                self._draw_reassign_icon(painter, rect, icon)

    def paint(self, painter: QPainter, option, widget=None):
        base = stream_color(self.stream)
        if self.isSelected():
            base = base.lighter(125)

        pen = QPen(
            base,
            2.4 if self.stream == STREAM_EXEC else 1.9,
            Qt.SolidLine,
            Qt.RoundCap,
            Qt.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(base))
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
                    handler = getattr(view, "delete_pipeline_edge", None)
                    if callable(handler):
                        handler(self)
                        event.accept()
                        return
                elif action == "reassign":
                    handler = getattr(view, "reassign_pipeline_edge", None)
                    if callable(handler):
                        handler(self, event.scenePos())
                        event.accept()
                        return
        super().mousePressEvent(event)
