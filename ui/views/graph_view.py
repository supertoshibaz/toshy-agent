from __future__ import annotations
from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style


import math
import json
from core.paths import APP_PATHS
from typing import Dict, List, Optional

import httpx
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsSimpleTextItem,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QButtonGroup,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from core.security import auth_headers
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.module_manager import ModuleConfigSync
from ui.core_bridge.process_manager import core_process
from ui.event_bus import event_bus
from ui.views.canvas.canvas_view import CanvasControlPanel, CanvasScene
from ui.views.canvas.canvas_card import GRID_SIZE
from .graph_layout import GRAPH_NODE_SIDE, GRAPH_MODULE_SIDE, GRAPH_CORE_SIDE


CORE_NODE_ID = "core:toshy"
CORE_TOOLS_MODULE_ID = "module:__core_tools__"


def _mix_color(color: QColor, target: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, amount))
    return QColor(
        int(color.red() + (target.red() - color.red()) * amount),
        int(color.green() + (target.green() - color.green()) * amount),
        int(color.blue() + (target.blue() - color.blue()) * amount),
        int(color.alpha() + (target.alpha() - color.alpha()) * amount),
    )


class GraphEdgeItem(QGraphicsPathItem):
    def __init__(
        self,
        source: "GraphNodeItem",
        target: "GraphNodeItem",
        *,
        kind: str,
        enabled: bool = True,
        rest_length: float = 180.0,
    ):
        super().__init__()
        self.source = source
        self.target = target
        self.kind = kind
        self.enabled = enabled
        self.rest_length = float(rest_length)
        self.setZValue(-10.0)
        self.setAcceptedMouseButtons(Qt.NoButton)
        source.edges.append(self)
        target.edges.append(self)
        self.update_path()

    def detach(self):
        if self in self.source.edges:
            self.source.edges.remove(self)
        if self in self.target.edges:
            self.target.edges.remove(self)

    def update_path(self):
        a = self.source.pos()
        b = self.target.pos()
        dx = b.x() - a.x()
        dy = b.y() - a.y()
        distance = math.hypot(dx, dy)
        if distance < 0.001:
            distance = 0.001

        # Start/end exactly on the square node perimeter instead of under the card.
        ux = dx / distance
        uy = dy / distance
        src_r = self.source.connection_distance(ux, uy)
        dst_r = self.target.connection_distance(-ux, -uy)
        start = QPointF(a.x() + ux * src_r, a.y() + uy * src_r)
        end = QPointF(b.x() - ux * dst_r, b.y() - uy * dst_r)

        path = QPainterPath(start)
        # A small perpendicular bend keeps dense graphs readable without making
        # links look like arrows that imply execution direction.
        perp_x, perp_y = -uy, ux
        bend = 0. if self.kind == 'pipeline-edge' else min(28.0, max(0.0, distance * 0.08))
        c1 = QPointF(
            start.x() + (end.x() - start.x()) * 0.42 + perp_x * bend,
            start.y() + (end.y() - start.y()) * 0.42 + perp_y * bend,
        )
        c2 = QPointF(
            start.x() + (end.x() - start.x()) * 0.58 + perp_x * bend,
            start.y() + (end.y() - start.y()) * 0.58 + perp_y * bend,
        )
        if self.source is self.target:
            start = self.source.pos() + QPointF(0, -self.source._height/2)
            end = self.source.pos() + QPointF(self.source._width/2, 0)
            path = QPainterPath(start)
            path.cubicTo(start + QPointF(0, -65), end + QPointF(65, 0), end)
        else:
            path.cubicTo(c1, c2, end)
        if self.kind in ('pipeline-edge', 'pipeline-call'):
            tangent = end - path.pointAtPercent(.97)
            angle = math.atan2(tangent.y(), tangent.x())
            for delta in (-.5, .5):
                path.moveTo(end)
                path.lineTo(end - QPointF(10*math.cos(angle+delta), 10*math.sin(angle+delta)))
        self.setPath(path)

        if self.kind == "module-tool":
            color = theme_manager.color('border.subtle') if self.enabled else theme_manager.color('border.subtle')
            width = 1.25
        else:
            color = theme_manager.color('text.secondary') if self.enabled else theme_manager.color('border.subtle')
            width = 1.7
        self.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))


class GraphNodeItem(QGraphicsObject):
    def __init__(
        self,
        view: "GraphView",
        node_id: str,
        title: str,
        *,
        kind: str,
        subtitle: str = "",
        color: str = "#818cf8",
        enabled: bool = True,
        description: str = "",
        width: float | None = None,
        height: float | None = None,
    ):
        super().__init__()
        self.view = view
        self.node_id = node_id
        self.title = title
        self.kind = kind
        self.subtitle = subtitle
        self.base_color = QColor(color) if color else QColor()
        self.enabled = bool(enabled)
        self.description = description
        self.edges: List[GraphEdgeItem] = []
        self.dragging = False

        # Graph nodes are deliberately square.  The hierarchy is encoded in
        # size: core > module > tool.
        if kind == "core":
            side = GRAPH_CORE_SIDE
        elif kind == "module":
            side = GRAPH_MODULE_SIDE
        else:
            side = GRAPH_NODE_SIDE
        self._width = float(width or side)
        self._height = float(height or side)

        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setCacheMode(QGraphicsItem.DeviceCoordinateCache)
        self.setZValue(-15.0 if kind == 'pipeline-parallel' else 5.0 if kind == "core" else (4.0 if kind == "module" else 3.0))
        if description:
            bind_caption(self, description, setter='setToolTip')

    def update_data(
        self,
        *,
        title: str,
        subtitle: str,
        color: str,
        enabled: bool,
        description: str = "",
        width: float | None = None,
        height: float | None = None,
    ):
        dimensions = (float(width or self._width), float(height or self._height))
        resized = dimensions != (self._width,self._height)
        if resized:
            self.prepareGeometryChange()
            self._width,self._height = dimensions
            for edge in self.edges: edge.update_path()
        changed = (
            resized or self.title != title
            or self.subtitle != subtitle
            or self.base_color.name() != QColor(color).name()
            or self.enabled != bool(enabled)
            or self.description != description
        )
        self.title = title
        self.subtitle = subtitle
        self.base_color = QColor(color) if color else QColor()
        self.enabled = bool(enabled)
        self.description = description
        bind_caption(self, description, setter='setToolTip')
        if changed:
            self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(-self._width / 2.0, -self._height / 2.0, self._width, self._height)

    def connection_distance(self, ux: float, uy: float) -> float:
        """Distance from the centre to the square perimeter along a unit ray."""
        candidates = []
        if abs(ux) > 1e-6:
            candidates.append((self._width * 0.5) / abs(ux))
        if abs(uy) > 1e-6:
            candidates.append((self._height * 0.5) / abs(uy))
        if not candidates:
            return self._width * 0.5
        return min(candidates) * 0.98

    def collision_radius(self) -> float:
        return math.hypot(self._width, self._height) * 0.5 + 10.0

    @staticmethod
    def _fitted_font(
        painter: QPainter,
        text: str,
        rect: QRectF,
        *,
        max_size: float,
        min_size: float,
        bold: bool,
    ) -> QFont:
        """Pick the largest font that keeps wrapped text inside *rect*."""
        base = QFont(painter.font())
        flags = int(Qt.AlignCenter | Qt.TextWordWrap)
        size = float(max_size)
        while size >= min_size:
            font = QFont(base)
            font.setPointSizeF(size)
            font.setBold(bold)
            bounds = QFontMetricsF(font).boundingRect(rect, flags, text)
            if bounds.width() <= rect.width() + 0.5 and bounds.height() <= rect.height() + 0.5:
                return font
            size -= 0.5

        font = QFont(base)
        font.setPointSizeF(min_size)
        font.setBold(bold)
        return font

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.boundingRect()
        radius = 12.0 if self.kind == "core" else (10.0 if self.kind == "module" else 8.0)

        neutral = not self.base_color.isValid()
        accent = theme_manager.color('border.subtle') if neutral else self.base_color
        if neutral:
            fill = theme_manager.color('background.input')
            border = theme_manager.color('border.subtle')
        elif self.kind == "core":
            fill = theme_manager.color('background.panel')
            border = theme_manager.color('text.primary')
        elif self.enabled:
            fill = _mix_color(accent, theme_manager.color('background.input'), 0.82)
            border = _mix_color(accent, theme_manager.color('text.primary'), 0.12)
        else:
            fill = theme_manager.color('background.input')
            border = _mix_color(accent, theme_manager.color('background.panel'), 0.72)

        if self.isSelected():
            border = theme_manager.color('text.primary')

        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(border, 1.5 if self.kind != "tool" else 1.0))
        painter.drawRoundedRect(rect, radius, radius)

        if self.kind == 'pipeline-parallel':
            return

        if not neutral:
            # Keep a small module-colour marker without stealing much room from the
            # square's text area. Disabled nodes retain only a muted hint.
            strip_color = accent if self.enabled else _mix_color(accent, theme_manager.color('border.subtle'), 0.75)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(strip_color))
            strip_margin = 9.0 if self.kind != "tool" else 7.0
            painter.drawRoundedRect(
                QRectF(rect.left() + 6.0, rect.top() + strip_margin, 3.0, rect.height() - strip_margin * 2.0),
                1.5,
                1.5,
            )

        # Text is centred inside the node and is allowed to wrap anywhere as a
        # fallback, so even long Python-style tool names remain inside the square.
        pad = 15.0 if self.kind == "core" else (13.0 if self.kind == "module" else 10.0)
        text_rect = rect.adjusted(pad, pad, -pad, -pad)

        if self.subtitle:
            title_ratio = 0.66 if self.kind != "tool" else 0.70
            title_h = max(1.0, text_rect.height() * title_ratio)
            title_rect = QRectF(text_rect.left(), text_rect.top(), text_rect.width(), title_h)
            subtitle_rect = QRectF(
                text_rect.left(),
                text_rect.top() + title_h,
                text_rect.width(),
                max(1.0, text_rect.height() - title_h),
            )
        else:
            title_rect = text_rect
            subtitle_rect = QRectF()

        if self.kind == "core":
            title_max, title_min = 13.0, 8.0
        elif self.kind == "module":
            title_max, title_min = 10.5, 7.0
        else:
            title_max, title_min = 9.0, 6.0

        title_font = self._fitted_font(
            painter,
            self.title,
            title_rect,
            max_size=title_max,
            min_size=title_min,
            bold=self.kind != "tool",
        )
        painter.setFont(title_font)
        painter.setPen(theme_manager.color('text.primary') if self.enabled or self.kind == "core" else theme_manager.color('text.secondary'))
        title_flags = int(Qt.AlignCenter | Qt.TextWordWrap)
        if any(painter.fontMetrics().horizontalAdvance(word) > title_rect.width() for word in self.title.split()):
            title_flags |= int(Qt.TextWrapAnywhere)
        painter.drawText(title_rect, title_flags, self.title)

        if self.subtitle:
            subtitle_max = 7.5 if self.kind == "core" else (7.0 if self.kind == "module" else 6.5)
            subtitle_min = 5.5 if self.kind == "tool" else 6.0
            subtitle_font = self._fitted_font(
                painter,
                self.subtitle,
                subtitle_rect,
                max_size=subtitle_max,
                min_size=subtitle_min,
                bold=False,
            )
            painter.setFont(subtitle_font)
            painter.setPen(theme_manager.color('text.secondary') if self.enabled else theme_manager.color('border.subtle'))
            subtitle_flags = int(Qt.AlignCenter | Qt.TextWordWrap | Qt.TextWrapAnywhere)
            painter.drawText(subtitle_rect, subtitle_flags, self.subtitle)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in list(self.edges):
                edge.update_path()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):
        if self.kind == 'pipeline':
            self.view.focus_pipeline(self.node_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

class GraphView(QWidget):
    """Обзор системных модулей и кастомных пайплайнов без физики."""

    def _refresh_theme(self, *_):
        for edge in self.edges:
            edge.update_path()
        for node in self.nodes.values():
            node.update()
        if self._status_item is not None:
            self._status_item.setBrush(theme_manager.color('text.secondary'))
        self.scene.update()
        self.view.viewport().update()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RuntimeGraphView")

        self.scene = CanvasScene(self)
        self.scene.smart_alignment = False
        self.scene.read_only = True

        from PySide6.QtWidgets import QGraphicsView

        self.view = QGraphicsView(self.scene, self)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.view.setDragMode(QGraphicsView.RubberBandDrag)
        self.view.viewport().installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view)

        self._current_zoom = 1.0
        self._is_panning = False
        self._pan_start_pos = QPoint()
        self._refresh_in_flight = False
        self._core_online = False
        self._first_layout = True
        self._initial_fit_timer = QTimer(self)
        self._initial_fit_timer.setSingleShot(True)
        self._initial_fit_timer.setInterval(50)
        self._initial_fit_timer.timeout.connect(self._fit_initial_view)
        self._layout_signature = None

        theme_manager.theme_changed.connect(self._refresh_theme)
        self.nodes: Dict[str, GraphNodeItem] = {}
        self.edges: List[GraphEdgeItem] = []
        self._status_item: Optional[QGraphicsSimpleTextItem] = None

        self.mode = 'system'
        self._payload = None
        self._layout_signature = None

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(5000)
        self.refresh_timer.timeout.connect(self.refresh_graph)
        self.refresh_timer.start()

        self.async_runner = AsyncRunner(self, max_threads=1)
        self.module_sync = ModuleConfigSync()

        # Reuse the exact board control panel; Graph supplies its own settings menu.
        self.control_panel = CanvasControlPanel(self, self, show_history=False)
        set_text(self.control_panel.btn_settings, 'ui.graph_view.graph_layout_153f', setter='setToolTip')
        self.mode_panel = QFrame(self)
        apply_style(self.mode_panel, 'QFrame {background:@background.panel@; border:1px solid @border.subtle@; border-radius:6px;}'
            'QToolButton {color:@text.secondary@; border:none; padding:6px 10px;} QToolButton:checked {background:@control.selected@; color:@text.primary@;}')
        mode_layout = QHBoxLayout(self.mode_panel)
        mode_layout.setContentsMargins(4, 4, 4, 4)
        self.mode_buttons = QButtonGroup(self)
        self.mode_options = {}
        for key, title in (('system', tr('ui.caption.72c46e349af6')), ('custom', tr('ui.caption.4dc7f6223583'))):
            button = QToolButton(self.mode_panel)
            bind_caption(button, title)
            button.setCheckable(True)
            button.setChecked(key == self.mode)
            self.mode_buttons.addButton(button)
            self.mode_options[key] = button
            button.clicked.connect(lambda _checked=False, value=key: self.set_mode(value))
            mode_layout.addWidget(button)
        self.mode_panel.adjustSize()
        self._update_panel_position()

        event_bus.module_status_changed.connect(lambda _name, _enabled: self.refresh_graph(force=True))
        core_process.status_changed.connect(self._on_core_status_changed)

        self._set_offline_state()
        QTimer.singleShot(0, self.refresh_graph)

    # ------------------------------------------------------------------
    # Board control-panel compatibility / camera
    # ------------------------------------------------------------------
    def zoom_in(self):
        self._apply_zoom(1.15)

    def zoom_out(self):
        self._apply_zoom(1.0 / 1.15)

    def _apply_zoom(self, factor: float):
        new_zoom = self._current_zoom * factor
        if 0.01 <= new_zoom <= 4.0:
            if self.nodes and self.isVisible():
                self._first_layout = False
                self._initial_fit_timer.stop()
            self._current_zoom = new_zoom
            self.view.scale(factor, factor)
            self._ensure_camera_space()

    def _ensure_camera_space(self):
        # Hidden scroll bars still clamp to the scene rect. Keep free space
        # around the camera even when the entire graph fits in the viewport.
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        margin = max(500., visible.width(), visible.height())
        self.view.setSceneRect(self.view.sceneRect().united(visible.adjusted(-margin,-margin,margin,margin)))

    def _stop_pan(self):
        self._is_panning = False
        self.view.viewport().unsetCursor()

    def _fit_initial_view(self):
        if not self._first_layout or not self.isVisible() or not self.nodes:
            return
        if self.view.viewport().width() < 2 or self.view.viewport().height() < 2:
            return
        self.reset_view()
        fitted_zoom = self._current_zoom
        center = self.view.mapToScene(self.view.viewport().rect().center())
        zoom = max(.65, min(1., fitted_zoom))
        if zoom != fitted_zoom:
            self.view.resetTransform()
            self.view.scale(zoom, zoom)
            self._current_zoom = zoom
            self.view.centerOn(self.nodes[CORE_NODE_ID].pos() if fitted_zoom < .65 else center)
            self._ensure_camera_space()

    def showEvent(self, event):
        super().showEvent(event)
        if self._first_layout:self._initial_fit_timer.start()

    def reset_view(self):
        if not self.isVisible():
            self._first_layout = True
            return
        self.view.resetTransform()
        self._current_zoom = 1.0
        if self.nodes:
            self.view.fitInView(self.scene.itemsBoundingRect().adjusted(-70, -70, 70, 70), Qt.KeepAspectRatio)
            self._current_zoom = self.view.transform().m11()
            self._first_layout = False
            self._initial_fit_timer.stop()
        else:
            self.view.centerOn(0, 0)
        self._ensure_camera_space()

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self._update_panel_position()
        if self._first_layout:self._initial_fit_timer.start()

    def _update_panel_position(self):
        margin = 16
        self.control_panel.adjustSize()
        self.control_panel.move(
            self.width() - self.control_panel.sizeHint().width() - margin,
            margin,
        )
        self.control_panel.raise_()
        if hasattr(self, 'mode_panel'):
            self.mode_panel.move(16, 16)
            self.mode_panel.raise_()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport():
            if event.type() == QEvent.Type.Wheel:
                self._handle_wheel(event)
                return True
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MiddleButton:
                if self.nodes:
                    self._first_layout = False
                    self._initial_fit_timer.stop()
                self._is_panning = True
                self._pan_start_pos = event.pos()
                self._ensure_camera_space()
                self.view.viewport().setCursor(Qt.ClosedHandCursor)
                return True
            if event.type() == QEvent.Type.MouseMove and self._is_panning:
                if not event.buttons() & Qt.MiddleButton:
                    self._stop_pan()
                    return False
                self._ensure_camera_space()
                delta = event.pos() - self._pan_start_pos
                self._pan_start_pos = event.pos()
                self.view.horizontalScrollBar().setValue(self.view.horizontalScrollBar().value() - delta.x())
                self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().value() - delta.y())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MiddleButton and self._is_panning:
                self._stop_pan()
                return True
            if event.type() in (QEvent.Type.Hide, QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse):
                self._stop_pan()
        return super().eventFilter(watched, event)

    def _handle_wheel(self, event: QWheelEvent):
        if self._is_panning:
            event.accept()
            return
        delta = event.angleDelta().y()
        if delta:
            self._apply_zoom(1.12 if delta > 0 else 1.0 / 1.12)
        event.accept()

    def populate_settings_menu(self, menu):
        localize(add_caption_action(menu, 'addAction', 'Перестроить расположение', self.relayout), 'ui.graph_view.rebuild_layout_ed9b')

    def set_mode(self, mode):
        if self.mode != mode:
            self.mode = mode
            self.mode_options[mode].setChecked(True)
            self._layout_signature = None
            if self._payload is not None:
                self._apply_graph_snapshot(self._payload)
            self._first_layout = True
            self._initial_fit_timer.start()

    def focus_pipeline(self, identity):
        members = [node for key, node in self.nodes.items() if key == identity or key.startswith(identity + ':node:')]
        if members:
            area = members[0].sceneBoundingRect()
            for node in members[1:]: area = area.united(node.sceneBoundingRect())
            self.view.fitInView(area.adjusted(-70, -70, 70, 70), Qt.KeepAspectRatio)
            self._current_zoom = self.view.transform().m11()
            self._ensure_camera_space()

    def relayout(self):
        self._layout_signature = None
        if self._payload is not None:
            self._apply_graph_snapshot(self._payload)
        self.reset_view()

    def _on_core_status_changed(self, online: bool, _status_key: str):
        self._core_online = bool(online)
        if online:
            self.refresh_graph(force=True)
        else:
            self._set_offline_state()

    def refresh_graph(self, force: bool = False):
        if not force and not self.isVisible():
            return
        if self._refresh_in_flight:
            return
        if not core_process.is_running():
            self._core_online = False
            self._set_offline_state()
            return

        self._refresh_in_flight = True
        self.async_runner.submit(self._fetch_graph_snapshot, self._apply_graph_snapshot, self._refresh_failed)

    def _fetch_graph_snapshot(self):
        with httpx.Client(timeout=1.6) as client:
            response = client.get(
                f"{core_process.base_url}/v1/graph",
                headers=auth_headers(core_process.core_token),
            )
            response.raise_for_status()
            payload = response.json()
        visual = self._module_visual_metadata()
        for module in payload.get('modules') or []:
            metadata = visual.get(module['name'], {})
            module.setdefault('display_name', metadata.get('display_name') or module['name'])
            module.setdefault('color', metadata.get('color') or '#818cf8')
        return payload

    def _refresh_failed(self, _error):
        self._refresh_in_flight = False
        if not core_process.is_running():
            self._set_offline_state()

    def _module_visual_metadata(self) -> Dict[str, dict]:
        result = {}
        try:
            for meta in self.module_sync.get_system_modules_data() + self.module_sync.get_custom_modules_data():
                manifest = APP_PATHS.modules / 'custom' / meta['name'] / 'module.json'
                if manifest.is_file():
                    try:
                        meta = dict(meta, display_name=json.loads(manifest.read_text(encoding='utf-8')).get('name') or meta.get('display_name'))
                    except (OSError, ValueError):
                        pass
                result[meta["name"]] = meta
        except Exception:
            pass
        return result

    def _apply_graph_snapshot(self, payload):
        from .graph_layout import ring_centers, star_positions, radial_boxes, box_bounds
        from .graph_topology import pipeline_cluster, node_title
        self._refresh_in_flight = False
        self._core_online = True
        self._payload = payload
        self._clear_status_item()
        specs, links, groups = {}, [], []
        specs[CORE_NODE_ID] = dict(title='Toshy Core', kind='core', subtitle=tr('ui.caption.df860d7e34dd'), color='#e4e4e7', enabled=True)
        modules = sorted((m for m in [*(payload.get('modules') or []), *(payload.get('core_components') or [])]
                          if m['name'] != 'toshy-mcp'), key=lambda m: m['name'])
        if self.mode == 'system':
            modules = [dict(name='__core_tools__', display_name='Context tools', enabled=True, required=True,
                            tools=payload.get('core_tools') or [])] + [m for m in modules if m.get('required') or m.get('category') == 'Системный']
        else:
            modules = [m for m in modules if not m.get('required') and m.get('category') != 'Системный']
        for module in modules:
            name = module['name']
            mid = 'module:' + name
            enabled = bool(module.get('enabled'))
            color = module.get('color') or ('#60a5fa' if self.mode == 'system' else '#ec4899')
            specs[mid] = dict(title=module.get('display_name') or name, kind='module', subtitle=self.mode + ('' if enabled else ' · off'),
                              color=color, enabled=enabled, description=name)
            links.append((CORE_NODE_ID, mid, 'core-module', enabled))
            local = {mid: (0., 0., GRAPH_MODULE_SIDE,GRAPH_MODULE_SIDE)}
            children = []
            pipeline_tools = {entry.get('tool_name') for entry in module.get('pipelines') or []}
            for tool in sorted(module.get('tools') or [], key=lambda t: t['name']):
                if tool['name'] in pipeline_tools: continue
                tid = f"tool:{name}:{tool['name']}"
                specs[tid] = dict(title=tool['name'], kind='tool', subtitle='tool', color=color, enabled=enabled,
                                  description=tool.get('description') or '')
                links.append((mid, tid, 'module-tool', enabled))
                children.append({tid: (0., 0., GRAPH_NODE_SIDE,GRAPH_NODE_SIDE)})
            pipelines = module.get('pipelines') or []
            for pipeline in pipelines:
                pid = f"pipeline:{name}:{pipeline['id']}"
                specs[pid] = dict(title=pipeline['name'], kind='pipeline', subtitle=tr('ui.caption.251db456dd0e'),
                    color=color, enabled=enabled and not pipeline.get('error'),
                    description=pipeline.get('error') or pipeline.get('description') or tr('ui.caption.65bb3ce9a3b9'))
                links.append((mid, pid, 'module-pipeline', enabled))
                cluster = {pid: (0., 0., GRAPH_NODE_SIDE,GRAPH_NODE_SIDE)}
                nodes, edges, boxes = pipeline_cluster(pipeline,padding=GRID_SIZE)
                for node in nodes.values():
                    nid = pid + ':node:' + node['id']
                    cluster[nid] = boxes[node['id']]
                    parallel = node['type'] == 'graph.parallel'
                    specs[nid] = dict(title=node_title(node), kind='pipeline-parallel' if parallel else 'pipeline-node', subtitle='',
                                      color=self._pipeline_node_color(node), enabled=enabled, description=node_title(node) if parallel else node['type'])
                    if node.get('pipeline'):
                        links.append((nid, f"pipeline:{name}:{node['pipeline']}", 'pipeline-call', enabled))
                for node in nodes.values():
                    if node['type'] == 'graph.start':
                        links.append((pid, pid + ':node:' + node['id'], 'pipeline-edge', enabled))
                for edge in edges:
                    links.append((pid + ':node:' + edge['source'], pid + ':node:' + edge['target'], 'pipeline-edge', enabled))
                children.append(cluster)
            if self.mode == 'system':
                radii = [max(math.hypot(x,y)+math.hypot(w,h)/2 for x,y,w,h in cluster.values()) for cluster in children]
                for cluster, (cx,cy) in zip(children, ring_centers(radii,center_radius=85.,gap=45.)):
                    local.update({key: (cx+x,cy+y,w,h) for key,(x,y,w,h) in cluster.items()})
            else:
                heights = [box_bounds(cluster)[3]-box_bounds(cluster)[1] for cluster in children]
                y = -(sum(heights)+65.*max(0,len(children)-1))/2
                for cluster,height in zip(children,heights):
                    bounds = box_bounds(cluster)
                    offset_y = y-bounds[1]
                    local.update({key:(x+180.,cy+offset_y,w,h) for key,(x,cy,w,h) in cluster.items()})
                    y += height+65.
            groups.append((mid, local))
        # Состояние on/off и текст подсказок не переставляют узлы.
        if self.mode == 'custom':
            boxes = radial_boxes(groups)
            positions = {key:(x,y) for key,(x,y,w,h) in boxes.items()}
            for key,(x,y,w,h) in boxes.items():
                if specs[key]['kind'] == 'pipeline-parallel': specs[key].update(width=w,height=h)
        else:
            positions = star_positions([(key,{nid:(x,y,math.hypot(w,h)/2) for nid,(x,y,w,h) in values.items()}) for key,values in groups])
        positions[CORE_NODE_ID] = (0., 0.)
        signature = (self.mode, tuple(sorted((key,spec.get('width'),spec.get('height')) for key,spec in specs.items())),
                     tuple(sorted((a,b,k) for a,b,k,_ in links)), tuple(sorted(positions.items())))
        self._sync_nodes(specs, links)
        if signature != self._layout_signature:
            self._layout_signature = signature
            for identity, pos in positions.items():
                self.nodes[identity].setPos(*pos)
            self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-300, -300, 300, 300))
            self.view.setSceneRect(self.scene.sceneRect())
            self._ensure_camera_space()
        if self._first_layout:
            self._initial_fit_timer.start()

    def _sync_nodes(self, specs: Dict[str, dict], links: List[tuple]):
        old_ids = set(self.nodes)
        new_ids = set(specs)

        for node_id in old_ids - new_ids:
            node = self.nodes.pop(node_id)
            for edge in list(node.edges):
                if edge in self.edges:
                    self.edges.remove(edge)
                edge.detach()
                if edge.scene() is self.scene:
                    self.scene.removeItem(edge)
            self.scene.removeItem(node)

        for node_id, spec in specs.items():
            node = self.nodes.get(node_id)
            if node is None:
                node = GraphNodeItem(self, node_id, **spec)
                self.nodes[node_id] = node
                self.scene.addItem(node)
            else:
                node.update_data(
                    title=spec["title"],
                    subtitle=spec["subtitle"],
                    color=spec["color"],
                    enabled=spec["enabled"],
                    description=spec.get("description", ""),
                    width=spec.get('width'), height=spec.get('height'),
                )

        for edge in self.edges:
            edge.detach()
            self.scene.removeItem(edge)
        self.edges.clear()

        for source_id, target_id, kind, enabled in links:
            source = self.nodes.get(source_id)
            target = self.nodes.get(target_id)
            if not source or not target:
                continue
            edge = GraphEdgeItem(
                source,
                target,
                kind=kind,
                enabled=enabled,
            )
            self.scene.addItem(edge)
            self.edges.append(edge)

    @staticmethod
    def _pipeline_node_color(node):
        from .modules.nodes.providers.modules import _module_color
        parts = str(node.get('type') or '').split('.')
        if len(parts) >= 3 and parts[0] in {'system', 'custom'} and parts[1] in {'module', 'tool'}:
            from core.plugins.modules import module_locations
            directory = module_locations(category=parts[0]).get((parts[0], parts[2]))
            color = _module_color(directory) if directory is not None else ''
            if color and QColor(color).isValid():
                return color
        return ''

    def _set_offline_state(self):
        self._clear_graph_items()
        self._clear_status_item()
        item = QGraphicsSimpleTextItem(tr('ui.caption.cbc1e8d42144'))
        item.setBrush(QBrush(theme_manager.color('text.secondary')))
        font = QFont()
        font.setPointSize(11)
        item.setFont(font)
        rect = item.boundingRect()
        item.setPos(-rect.width() / 2.0, -rect.height() / 2.0)
        self.scene.addItem(item)
        self._status_item = item
        self.view.centerOn(0, 0)

    def _clear_status_item(self):
        if self._status_item is not None:
            if self._status_item.scene() is self.scene:
                self.scene.removeItem(self._status_item)
            self._status_item = None

    def _clear_graph_items(self):
        for edge in self.edges:
            edge.detach()
            if edge.scene() is self.scene:
                self.scene.removeItem(edge)
        self.edges.clear()
        for node in self.nodes.values():
            if node.scene() is self.scene:
                self.scene.removeItem(node)
        self.nodes.clear()
        self._first_layout = True
        self._layout_signature = None

