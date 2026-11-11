from ui.styles.binding import themed_color
from ui.styles.binding import watch_paint
from ui.themes.manager import theme_manager
from ui.styles.binding import apply_style
from ui.icons import get as get_icon

import uuid
import math
from typing import Optional
from PySide6.QtWidgets import (
    QGraphicsObject, QGraphicsProxyWidget, QGraphicsItem, QPlainTextEdit,
    QFrame, QApplication, QToolButton, QSizePolicy
)
from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QEvent, QLineF, QTimer
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QCursor,
    QTextOption, QTextBlockFormat, QTextCursor, QFocusEvent
)

from ui.highlighters import ObsidianLivePreviewHighlighter
from ui.views.markdown_handler import MarkdownHandler
from ui.views.markdown_render import MarkdownRenderView
from .overview import overview_cache, paint_symbol

GRID_SIZE = 24
BORDER_MARGIN = 8
CORNER_ZONE = 14
PORT_RADIUS = 5.0
PORT_HIT_RADIUS = 18.0
RESIZE_CORNER_OFFSET = 16.0
RESIZE_CORNER_LENGTH = 10.0
RESIZE_CORNER_HIT_RADIUS = 8.0
RESIZE_HANDLE_MAX_SCALE = 4.5
# Constant bounds keep the scene index valid when only the camera changes.
RESIZE_OUTER_MARGIN = 112.0
MIN_WIDTH = GRID_SIZE * 2
MIN_HEIGHT = GRID_SIZE * 2


class AgentContextBadge(QGraphicsItem):
    """Visible above card text/images without intercepting card interaction."""
    def __init__(self, card):
        super().__init__(card)
        self.setAcceptedMouseButtons(Qt.NoButton)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations)
        self.setZValue(10000)
        self.icon = get_icon('send', color='#22c55e')
        self.hide()

    def boundingRect(self):
        return QRectF(-18, -18, 36, 36)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#22c55e'), 1))
        painter.setBrush(theme_manager.color('background.panel'))
        painter.drawRoundedRect(self.boundingRect().adjusted(.5, .5, -.5, -.5), 7, 7)
        self.icon.paint(painter, -12, -12, 24, 24)


class CanvasPreviewProxy(QGraphicsProxyWidget):
    """Let the real code-copy buttons work without stealing normal card clicks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._forwarding_button = False
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def _copy_button_at(self, pos: QPointF):
        root = self.widget()
        if root is None:
            return None
        child = root.childAt(pos.toPoint())
        while child is not None and child is not root:
            if isinstance(child, QToolButton):
                return child
            child = child.parentWidget()
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
            root = self.widget()
            reference = root.link_at_global(event.screenPos()) if hasattr(root, 'link_at_global') else root.markdown_handler.get_url_at_pos(event.screenPos())
            if reference:
                from ui.views.inline_images import open_reference
                from ui.views.inline_images import document_path
                open_reference(reference, getattr(root, "base_path", None) or document_path(root))
                event.accept()
                return
        self._forwarding_button = (
            getattr(self.parentItem(), "_is_editing", False)
            or (event.button() == Qt.LeftButton and self._copy_button_at(event.pos()) is not None)
        )
        if self._forwarding_button:
            super().mousePressEvent(event)
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self._forwarding_button or getattr(self.parentItem(), "_is_editing", False):
            super().mouseMoveEvent(event)
        else:
            event.ignore()

    def mouseReleaseEvent(self, event):
        if self._forwarding_button:
            super().mouseReleaseEvent(event)
            self._forwarding_button = False
        else:
            event.ignore()

    def mouseDoubleClickEvent(self, event):
        if self._copy_button_at(event.pos()) is not None or getattr(self.parentItem(), "_is_editing", False):
            super().mouseDoubleClickEvent(event)
        else:
            event.ignore()


class CanvasCardEditor(QPlainTextEdit):
    """Текстовый редактор с поддержкой Live Preview Markdown."""
    editing_finished = Signal()

    def __init__(self, parent=None, *, live_preview=True):
        super().__init__(parent)
        watch_paint(self)
        self.is_markdown = True
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.setFrameShape(QFrame.NoFrame)
        apply_style(self, """
            QPlainTextEdit {
                background-color: transparent;
                color: @text.primary@;
                border: none;
                font-size: 13px;
                padding: 4px;
            }
        """)

        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setCursor(Qt.ArrowCursor)

        self.highlighter = None
        self.markdown_handler = None
        if live_preview:
            self.enable_live_preview()

    def enable_live_preview(self):
        if self.highlighter is not None:
            return
        self.highlighter = ObsidianLivePreviewHighlighter(self.document(), self)
        self.markdown_handler = MarkdownHandler(self)
        self.markdown_handler.set_enabled(True)
        self.markdown_handler.set_visual_enabled(True)
        self.markdown_handler.parse_code_blocks()

    def paintEvent(self, event):
        if self.is_markdown and self.markdown_handler is not None:
            p = QPainter()
            if p.begin(self.viewport()):
                try:
                    p.setRenderHint(QPainter.Antialiasing)
                    self.markdown_handler.paint_markdown_elements(p)
                finally:
                    p.end()
        super().paintEvent(event)
        if self.is_markdown and self.markdown_handler is not None:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing)
            self.markdown_handler.paint_markdown_markers(painter)
            painter.end()

    def toPlainText(self):
        return self.markdown_handler.source_text() if self.markdown_handler else super().toPlainText()

    def createMimeDataFromSelection(self):
        if self.markdown_handler:
            from ui.markdown_clipboard import selection_mime
            return selection_mime(self.markdown_handler, self.textCursor())
        return super().createMimeDataFromSelection()

    def canInsertFromMimeData(self, mime):
        from ui.image_mime import can_import
        return (not self.isReadOnly() and can_import(mime)) or super().canInsertFromMimeData(mime)

    def insertFromMimeData(self, mime):
        self.enable_live_preview()
        if not self.markdown_handler.images.insert_mime(mime):
            super().insertFromMimeData(mime)

    def setPlainText(self, text: str):
        self._setting_plain_text = True
        if self.markdown_handler:
            self.markdown_handler.ordered_lists.reset()
        try:
            super().setPlainText(text)
        finally:
            self._setting_plain_text = False
        if self.markdown_handler is not None and self.is_markdown:
            self.markdown_handler.parse_code_blocks()

    def mousePressEvent(self, event):
        self.enable_live_preview()
        if self.markdown_handler.handle_mouse_press(event):
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self.enable_live_preview()
        from ui.views.text_selection import wrap_selected_text
        if wrap_selected_text(self, event):
            return
        if self.markdown_handler.handle_key_press(event):
            event.accept()
            return

        if event.text() and event.text().isprintable() and not event.text().isspace() and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
            cursor = self.textCursor()
            if not cursor.hasSelection() and cursor.position() > 0 and self.document().characterAt(cursor.position() - 1).isspace():
                cursor.beginEditBlock()
                cursor.insertText(event.text())
                cursor.endEditBlock()
                event.accept()
                return

        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if hasattr(self, "highlighter") and self.highlighter:
            self.highlighter._schedule_sync()

    def focusOutEvent(self, event):
        if event.reason() == Qt.ActiveWindowFocusReason:
            super().focusOutEvent(event)
            return

        super().focusOutEvent(event)
        cursor = self.textCursor()
        if cursor.hasSelection():
            cursor.clearSelection()
            self.setTextCursor(cursor)

        if hasattr(self, "highlighter") and self.highlighter:
            self.highlighter._schedule_sync()

        self.editing_finished.emit()

    def set_alignment(self, align: Qt.AlignmentFlag):
        opt = self.document().defaultTextOption()
        opt.setAlignment(align)
        self.document().setDefaultTextOption(opt)

        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)
        fmt = QTextBlockFormat()
        fmt.setAlignment(align)
        cursor.mergeBlockFormat(fmt)

    def wheelEvent(self, event):
        vbar = self.verticalScrollBar()
        if vbar and vbar.maximum() > 0:
            delta = event.angleDelta().y()
            if delta != 0:
                steps = delta / 120.0
                step_size = max(1, vbar.singleStep()) * 3
                vbar.setValue(vbar.value() - int(round(steps * step_size)))

        event.accept()

class CanvasCardItem(QGraphicsObject):
    """Плитка доски: свободное перетаскивание за любую точку, редактирование по повторному клику."""
    geometry_changed = Signal()

    def paint_resize_rulers(self, painter):
        """Scene foreground overlay, independent of editor/preview visibility."""
        if not self._resizing or not self._active_handle:
            return
        painter.save()
        try:
            painter.setTransform(self.sceneTransform(), True)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(theme_manager.color('accent.primary'), 1.3))
            painter.setBrush(QBrush(themed_color('background.surface', 225)))
            font = painter.font(); font.setPointSize(8); painter.setFont(font)
            if any(part in self._active_handle for part in ('left', 'right')):
                painter.drawLine(QPointF(11.0, 16.0), QPointF(self._width - 11.0, 16.0))
                label = str(max(1, int(round(self._width / GRID_SIZE))))
                width = max(28.0, painter.fontMetrics().horizontalAdvance(label) + 10.0)
                box = QRectF((self._width - width) / 2.0, 7.0, width, 18.0)
                painter.drawRoundedRect(box, 3, 3)
                painter.drawText(box, Qt.AlignCenter, label)
            if any(part in self._active_handle for part in ('top', 'bottom')):
                painter.drawLine(QPointF(16.0, 11.0), QPointF(16.0, self._height - 11.0))
                label = str(max(1, int(round(self._height / GRID_SIZE))))
                width = max(28.0, painter.fontMetrics().horizontalAdvance(label) + 10.0)
                box = QRectF(7.0, (self._height - 18.0) / 2.0, width, 18.0)
                painter.drawRoundedRect(box, 3, 3)
                painter.drawText(box, Qt.AlignCenter, label)
        finally:
            painter.restore()

    def __init__(self, x: float = 0, y: float = 0, w: float = 240, h: float = 192, node_id: str = None, parent=None):
        super().__init__(parent)
        self.node_id = node_id or uuid.uuid4().hex[:16]
        self.setPos(x, y)

        self._width = max(MIN_WIDTH, round(w / GRID_SIZE) * GRID_SIZE)
        self._height = max(MIN_HEIGHT, round(h / GRID_SIZE) * GRID_SIZE)

        self.setFlags(
            QGraphicsObject.ItemIsMovable |
            QGraphicsObject.ItemIsSelectable |
            QGraphicsObject.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setCursor(QCursor(Qt.OpenHandCursor))
        # Child widgets (live editor / rendered preview) are clipped by the
        # card itself, so overflowing text disappears under the card border.
        self.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)

        self.snap_to_grid = True
        self._resizing = False
        self._active_handle = None
        self._resize_start_scene_pos = QPointF()
        self._orig_geom = QRectF()

        self._is_editing = False
        self._was_selected_before_press = False
        self._press_pos = QPointF()
        self.connected_arrows = []
        self.color: Optional[str] = None
        self.text_align: str = "left"
        # Separate, non-interactive highlight used by the Agent context picker.
        # It must not reuse QGraphicsItem selection because normal selection is
        # still needed for moving/editing cards while the picker is open.
        self.agent_context_marked = False
        self.search_match_marked = False
        self.search_current_match = False
        self._search_text_options = ('', False, False, False)
        self._connection_target_hover = False
        self._connection_target_port = None

        self._center_timer = QTimer(self)
        self._center_timer.setSingleShot(True)
        self._center_timer.timeout.connect(self._center_text)
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._follow_edit_cursor)
        self._preview_active = False
        self._preview_dirty = True
        self._preview_key = None
        self._source_text = ""
        self._overview_active = False
        self._overview_key = None
        self._overview_images = 0
        self.editor = CanvasCardEditor(live_preview=False)
        self.editor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.editor.card_item = self
        self.editor.document().contentsChange.connect(self._on_content_changed)
        self.editor.editing_finished.connect(self._on_editing_finished)

        self.proxy = CanvasPreviewProxy(self)
        self.proxy.setWidget(self.editor)
        self.proxy.setPos(6, 6)
        self.proxy.setAcceptedMouseButtons(Qt.LeftButton)

        # Unselected cards use the shared widget Markdown renderer. Ordinary
        # Markdown remains rich text, while fenced code is the exact same Qt
        # CodeBlockWidget used by chat (rounded card + real copy button).
        self.preview = MarkdownRenderView(
            compact=True,
            text_align=self.text_align,
            interactive_text=False,
            vertical_scrollbar=False,
        )
        self.preview.setDocumentMargin(2)
        self.preview.images_removable = True
        self.preview.image_removed.connect(self._remove_image)
        self.preview.viewport().setStyleSheet("background: transparent;")

        self.preview_proxy = CanvasPreviewProxy(self)
        self.preview_proxy.setWidget(self.preview)
        self.preview_proxy.setPos(6, 6)

        self.agent_badge = AgentContextBadge(self)
        self._update_editor_size()
        self._refresh_preview()
        self._sync_render_state()

        self.editor.viewport().installEventFilter(self)

    def _update_editor_size(self):
        self._overview_key = None
        if hasattr(self, 'agent_badge'):
            self.agent_badge.setPos(self._width / 2, self._height / 2)
        w = max(40.0, self._width - 12)
        h = max(30.0, self._height - 12)
        self.proxy.resize(w, h)
        self._center_timer.stop()
        self._center_text()
        if hasattr(self, "preview_proxy"):
            self.preview_proxy.resize(w, h)
        scene = self.scene()
        if scene is not None and hasattr(scene, "card_geometry_changed"):
            scene.card_geometry_changed(self)

    def _on_content_changed(self, _position, removed, added):
        if not (removed or added):
            return
        self._source_text = self.editor.toPlainText()
        self._overview_key = None
        if self.scene() is not None and hasattr(self.scene(), 'schedule_previews'):
            self.scene().schedule_previews()
        self._center_timer.start(0)
        if self._is_editing:
            self._scroll_timer.start(0)
        self._preview_dirty = True
        self.geometry_changed.emit()
        self._refresh_preview()
        self.geometry_changed.emit()

    def set_preview_active(self, active):
        active = bool(active)
        if self._preview_active == active:
            return
        self._preview_active = active
        self._sync_render_state()

    def _refresh_preview(self):
        self._preview_dirty = True
        if not hasattr(self, "preview") or not self._preview_active:
            return
        if self._is_editing:
            return
        key = (self._source_text, self.text_align)
        if key != self._preview_key:
            self.preview.set_markdown(self._source_text, text_align=self.text_align, compact=True)
            self._preview_key = key
        self._preview_dirty = False
        self.set_search_text(*self._search_text_options)

    def set_overview_active(self, active):
        active = bool(active)
        if self._overview_active != active:
            self._overview_active = active
            self.update()

    def build_overview(self):
        self._overview_key, self._overview_images = overview_cache.build(
            self._source_text, self._width - 16, self._height - 16, self.text_align, self.preview.font(),
            tuple((w.pixmap.width(), w.pixmap.height()) for w in self.preview._image_chunks)
            if self._preview_key and self._preview_key[0] == self._source_text else (),
            base_path=self.preview.base_path)
        self.update()

    def _sync_render_state(self, selected_override=None):
        if not hasattr(self, "preview_proxy"):
            return
        selected = self.isSelected() if selected_override is None else bool(selected_override)
        show_preview = not self._is_editing
        if not show_preview:
            self.editor.enable_live_preview()
        elif self._preview_dirty and self._preview_active:
            # ItemSelectedChange arrives before Qt changes isSelected().
            key = (self._source_text, self.text_align)
            if key != self._preview_key:
                self.preview.set_markdown(self._source_text, text_align=self.text_align, compact=True)
                self._preview_key = key
            self._preview_dirty = False
        self.preview_proxy.setVisible(show_preview and self._preview_active)
        self.proxy.setVisible(not show_preview)

    def _resize_handle_scale(self):
        scene = self.scene()
        views = scene.views() if scene is not None else []
        zoom = abs(views[0].transform().m11()) if views else 1.0
        return max(1.0, min(RESIZE_HANDLE_MAX_SCALE,
                            (1.0 / max(zoom, 0.0001)) ** 0.75))

    def _resize_corner_points(self) -> dict:
        o = RESIZE_CORNER_OFFSET * self._resize_handle_scale()
        return {
            "top_left": QPointF(-o, -o),
            "top_right": QPointF(self._width + o, -o),
            "bottom_left": QPointF(-o, self._height + o),
            "bottom_right": QPointF(self._width + o, self._height + o),
        }

    def shape(self):
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self._width, self._height), 8, 8)
        # External diagonal resize handles are part of the hit shape only while
        # the card is selected. They sit outside the enlarged connector radius.
        if (self.isSelected() or self._is_editing) and not self.is_read_only():
            r = RESIZE_CORNER_HIT_RADIUS * self._resize_handle_scale()
            for pt in self._resize_corner_points().values():
                path.addRect(QRectF(pt.x() - r, pt.y() - r, 2 * r, 2 * r))
        return path

    def boundingRect(self) -> QRectF:
        m = RESIZE_OUTER_MARGIN
        return QRectF(-m, -m, self._width + 2 * m, self._height + 2 * m)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, 0, self._width, self._height)

        if self.agent_context_marked and not self.isSelected():
            painter.setBrush(QBrush(theme_manager.color('background.input')))
            painter.setPen(QPen(QColor("#22c55e"), 2.6))
        elif self.color:
            base_c = QColor(self.color)
            painter.setBrush(QBrush(theme_manager.color('background.input')))
            if self.isSelected():
                painter.setPen(QPen(base_c.lighter(130), 2.5))
            else:
                painter.setPen(QPen(base_c, 2.0))
        else:
            painter.setBrush(QBrush(theme_manager.color('background.input')))
            if self.isSelected():
                painter.setPen(QPen(theme_manager.color('accent.primary'), 2.5))
            else:
                painter.setPen(QPen(theme_manager.color('border.subtle'), 2.0))

        painter.drawRoundedRect(rect, 8, 8)

        if self._overview_active and not self._is_editing and self._source_text.strip():
            image = overview_cache.get(self._overview_key)
            if image is not None:
                painter.save()
                painter.setClipRect(rect.adjusted(8, 8, -8, -8))
                painter.translate(8, 8)
                painter.setPen(Qt.NoPen)
                painter.setBrush(theme_manager.color('text.secondary'))
                if image:
                    painter.drawRects(image)
                painter.restore()
            zoom = max(.01, abs(painter.transform().m11()))
            content = rect.adjusted(8, 8, -8, -8)
            if image is not None and self._overview_images:
                painter.save()
                painter.setClipRect(content)
                painter.setPen(Qt.NoPen)
                painter.setBrush(theme_manager.color('background.window', shift=-0.04))
                for area in self._overview_images:
                    visible = area.translated(8, 8).intersected(content)
                    if visible.isEmpty():
                        continue
                    painter.drawRect(visible)
                    height = min(visible.height(), visible.width(), 28 / zoom)
                    width = height
                    badge = QRectF(0, 0, width, height)
                    badge.moveCenter(visible.center())
                    paint_symbol(painter, badge, image=True)
                painter.restore()

        # Search is a non-interactive overlay, independent of scene selection.
        if self.search_match_marked:
            inset = 3.0
            col = QColor("#fbbf24") if self.search_current_match else QColor("#60a5fa")
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(col, 2.2 if self.search_current_match else 1.5))
            painter.drawRoundedRect(rect.adjusted(inset, inset, -inset, -inset), 6, 6)

        # Connection ports: normal selection shows them, but while an arrow is
        # dragged every nearby target card exposes a much easier hit surface.
        if (self.isSelected() or self._connection_target_hover) and not self._is_editing and not self.is_read_only():
            r = PORT_RADIUS + (1.5 if self._connection_target_hover else 0.0)
            for side, pt in self.get_ports_local().items():
                active = self._connection_target_hover and side == self._connection_target_port
                painter.setBrush(QBrush(QColor("#fbbf24") if active else theme_manager.color('accent.primary')))
                painter.setPen(QPen(theme_manager.color('text.primary'), 1.4 if active else 1.1))
                rr = r + (2.0 if active else 0.0)
                painter.drawEllipse(QRectF(pt.x() - rr, pt.y() - rr, rr * 2, rr * 2))

        # Dedicated external 90-degree corner handles for diagonal resizing.
        # They do not compete with the enlarged corner connector hit areas.
        if (self.isSelected() or self._is_editing) and not self.is_read_only():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(theme_manager.color('accent.primary'), 2.0 * self._resize_handle_scale(), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            length = RESIZE_CORNER_LENGTH * self._resize_handle_scale()
            for name, pt in self._resize_corner_points().items():
                x, y = pt.x(), pt.y()
                if name == "top_left":
                    painter.drawLine(QPointF(x, y), QPointF(x + length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y + length))
                elif name == "top_right":
                    painter.drawLine(QPointF(x, y), QPointF(x - length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y + length))
                elif name == "bottom_left":
                    painter.drawLine(QPointF(x, y), QPointF(x + length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y - length))
                else:
                    painter.drawLine(QPointF(x, y), QPointF(x - length, y))
                    painter.drawLine(QPointF(x, y), QPointF(x, y - length))

    def set_agent_context_marked(self, marked: bool):
        marked = bool(marked)
        if self.agent_context_marked == marked:
            return
        self.agent_context_marked = marked
        self.agent_badge.setVisible(marked)
        self.update()

    def set_search_marked(self, marked: bool, current: bool = False):
        marked = bool(marked)
        current = bool(current and marked)
        if self.search_match_marked == marked and self.search_current_match == current:
            return
        self.search_match_marked = marked
        self.search_current_match = current
        self.update()

    def set_search_text(self, query, case_sensitive=False, whole_word=False, current=False):
        self._search_text_options = (query, case_sensitive, whole_word, current)
        from ui.search_highlight import highlight_editor, highlight_rendered
        highlight_editor(self.editor, *self._search_text_options)
        highlight_rendered(self.preview, *self._search_text_options)

    def set_connection_target(self, active: bool, side: Optional[str] = None):
        active = bool(active)
        side = side if active else None
        if self._connection_target_hover == active and self._connection_target_port == side:
            return
        self._connection_target_hover = active
        self._connection_target_port = side
        self.update()

    def _get_handle_at(self, pos: QPointF) -> str | None:
        if self.is_read_only():
            return None

        x, y = pos.x(), pos.y()
        w, h = self._width, self._height
        b = BORDER_MARGIN

        # Diagonal resizing is intentionally available only through the
        # external corner handles. The actual card corners belong to arrows.
        if self.isSelected() or self._is_editing:
            for name, pt in self._resize_corner_points().items():
                if math.hypot(x - pt.x(), y - pt.y()) <= RESIZE_CORNER_HIT_RADIUS * self._resize_handle_scale():
                    return name

        if self._is_editing:
            return None

        if -b <= y <= b and 0 <= x <= w:
            return "top"
        if h - b <= y <= h + b and 0 <= x <= w:
            return "bottom"
        if -b <= x <= b and 0 <= y <= h:
            return "left"
        if w - b <= x <= w + b and 0 <= y <= h:
            return "right"

        return None

    def hoverMoveEvent(self, event):
        # 1. СТРОГИЙ ПРИОРИТЕТ: Порты подключения стрелочек
        if self.get_port_at(event.pos()):
            self.setCursor(QCursor(Qt.CrossCursor))
            super().hoverMoveEvent(event)
            return

        # 2. Грани изменения размера (только если не в зоне порта)
        handle = self._get_handle_at(event.pos())
        if handle in ("top", "bottom"):
            self.setCursor(QCursor(Qt.SizeVerCursor))
        elif handle in ("left", "right"):
            self.setCursor(QCursor(Qt.SizeHorCursor))
        elif handle in ("top_left", "bottom_right"):
            self.setCursor(QCursor(Qt.SizeFDiagCursor))
        elif handle in ("top_right", "bottom_left"):
            self.setCursor(QCursor(Qt.SizeBDiagCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor if self.is_read_only() else Qt.OpenHandCursor))
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
            from ui.views.inline_images import open_reference, document_path
            reference = self.preview.link_at_global(event.screenPos()) if self.preview_proxy.isVisible() else None
            if not reference:
                self.editor.enable_live_preview()
                reference = self.editor.markdown_handler.get_url_at_pos(event.screenPos())
            if reference:
                open_reference(reference, document_path(self.editor))
                event.accept()
                return
        if event.button() == Qt.LeftButton:
            self._press_scene_pos = event.scenePos()
            self._press_pos = event.pos()
            self._was_selected_before_press = self.isSelected()
            self._move_start_pos = self.pos()

            # 1. СТРОГИЙ ПРИОРИТЕТ: Протягивание стрелочки от порта
            port = self.get_port_at(event.pos())
            if port and self.scene():
                views = self.scene().views()
                if views and hasattr(views[0], "start_connection"):
                    self.setSelected(True)
                    views[0].start_connection(self, port, event.scenePos())
                    event.accept()
                    return

            # 2. Фиксация начальных позиций для отката перемещения
            if self.scene():
                self._selected_start_positions = {
                    it: it.pos() for it in self.scene().selectedItems() if isinstance(it, CanvasCardItem)
                }
                self._selected_start_positions[self] = self.pos()

            # 3. Изменение размера карточки (только если клик не по порту)
            handle = self._get_handle_at(event.pos())
            if handle:
                self._resizing = True
                self._active_handle = handle
                self._resize_start_scene_pos = event.scenePos()
                self._orig_geom = QRectF(self.pos().x(), self.pos().y(), self._width, self._height)
                self.update()
                event.accept()
                return

            if not self._is_editing and not self.is_read_only():
                self.setCursor(QCursor(Qt.ClosedHandCursor))

        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton and self.scene() and not self._resizing:
            self._selected_start_positions = {
                card: QPointF(card.pos()) for card in self.scene().selectedItems()
                if isinstance(card, CanvasCardItem)
            }

    def _finish_resize_interaction(self):
        """Finish a resize even if Qt lost the original mouse-release event."""
        if not self._resizing:
            return

        self._resizing = False
        self._active_handle = None
        self.update()
        new_geom = QRectF(self.pos().x(), self.pos().y(), self._width, self._height)
        if new_geom != self._orig_geom:
            from .canvas_view import ResizeCardCommand
            views = self.scene().views() if self.scene() else []
            if views and hasattr(views[0], "undo_stack"):
                views[0].undo_stack.push(
                    ResizeCardCommand(self, self._orig_geom, new_geom, self.scene())
                )
        self.geometry_changed.emit()
        if not self._is_editing:
            self.setCursor(QCursor(Qt.OpenHandCursor))
        if self.scene() and hasattr(self.scene(), "clear_guides"):
            self.scene().clear_guides()

    def _record_stale_move_if_needed(self):
        """Commit a card move whose release was swallowed by a proxy/widget."""
        starts = getattr(self, "_selected_start_positions", None)
        if not starts:
            return

        moves = []
        for card, old_p in list(starts.items()):
            if card.scene() is self.scene() and (card.pos() - old_p).manhattanLength() >= 2:
                moves.append((card, old_p, card.pos()))

        if moves and self.scene():
            from .canvas_view import MoveCardsCommand
            views = self.scene().views()
            if views and hasattr(views[0], "undo_stack"):
                views[0].undo_stack.push(MoveCardsCommand(moves, self.scene()))
            self.geometry_changed.emit()

        try:
            del self._selected_start_positions
        except AttributeError:
            pass

    def recover_pointer_state(self):
        """Reset stale drag/resize state after a missed mouse release."""
        if self._resizing:
            self._finish_resize_interaction()
        elif not self._is_editing and self.cursor().shape() == Qt.ClosedHandCursor:
            self._record_stale_move_if_needed()

        if not self._is_editing:
            self.setCursor(QCursor(Qt.OpenHandCursor))
        if self.scene() and hasattr(self.scene(), "clear_guides"):
            self.scene().clear_guides()

    def mouseMoveEvent(self, event):
        if self.scene() and any(getattr(view, '_connecting_card', None) is self for view in self.scene().views()):
            event.accept()
            return
        if self._resizing:
            # A resize owns the mouse while LMB is pressed.  If the release was
            # lost (for example after a proxy-widget handoff), do not keep
            # shrinking the card forever on later hover/move events.
            if not (event.buttons() & Qt.LeftButton):
                self._finish_resize_interaction()
                event.accept()
                return

            dx = event.scenePos().x() - self._resize_start_scene_pos.x()
            dy = event.scenePos().y() - self._resize_start_scene_pos.y()

            if self.snap_to_grid:
                dx = round(dx / GRID_SIZE) * GRID_SIZE
                dy = round(dy / GRID_SIZE) * GRID_SIZE

            orig_x = self._orig_geom.x()
            orig_y = self._orig_geom.y()
            orig_w = self._orig_geom.width()
            orig_h = self._orig_geom.height()

            new_x, new_y = orig_x, orig_y
            new_w, new_h = orig_w, orig_h

            if "left" in self._active_handle:
                dx = min(dx, orig_w - MIN_WIDTH)
                new_x = orig_x + dx
                new_w = orig_w - dx
            elif "right" in self._active_handle:
                new_w = max(MIN_WIDTH, orig_w + dx)

            if "top" in self._active_handle:
                dy = min(dy, orig_h - MIN_HEIGHT)
                new_y = orig_y + dy
                new_h = orig_h - dy
            elif "bottom" in self._active_handle:
                new_h = max(MIN_HEIGHT, orig_h + dy)

            self.prepareGeometryChange()
            self.setPos(new_x, new_y)
            self._width = new_w
            self._height = new_h
            self._update_editor_size()
            for arrow in self.connected_arrows:
                arrow.update_path()
            self.update()
            event.accept()
            return

        starts = getattr(self, '_selected_start_positions', {})
        if (len(starts) > 1 and self in starts and event.buttons() & Qt.LeftButton
                and not self._is_editing and not self.is_read_only()):
            delta = event.scenePos() - self._press_scene_pos
            anchor = starts[self]
            corrected = self.itemChange(QGraphicsObject.ItemPositionChange, anchor + delta)
            delta = corrected - anchor
            from .movement import place_cards
            place_cards(self.scene(), ((card, start + delta) for card, start in starts.items()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._finish_resize_interaction()
            event.accept()
            return

        if self.scene() and hasattr(self.scene(), "clear_guides"):
            self.scene().clear_guides()

        super().mouseReleaseEvent(event)

        if not self._is_editing:
            self.setCursor(QCursor(Qt.OpenHandCursor))

        if event.button() == Qt.LeftButton and not self.is_read_only():
            dist = (event.pos() - self._press_pos).manhattanLength()
            if dist >= 4:
                # Карточки переместили — регистрируем команду отката перемещения
                moves = []
                if hasattr(self, "_selected_start_positions"):
                    for card, old_p in self._selected_start_positions.items():
                        if (card.pos() - old_p).manhattanLength() >= 2:
                            moves.append((card, old_p, card.pos()))
                elif (self.pos() - self._move_start_pos).manhattanLength() >= 2:
                    moves.append((self, self._move_start_pos, self.pos()))

                if moves:
                    from .canvas_view import MoveCardsCommand
                    views = self.scene().views() if self.scene() else []
                    if views and hasattr(views[0], "undo_stack"):
                        views[0].undo_stack.push(MoveCardsCommand(moves, self.scene()))

                self.geometry_changed.emit()
            elif self._was_selected_before_press:
                # Только повторный клик по уже выделенной карточке запускает редактирование
                self.start_editing(event.pos())

        # The drag snapshot is only valid for this press/release pair.
        if hasattr(self, "_selected_start_positions"):
            del self._selected_start_positions

        # Обязательно стираем направляющие выравнивания после завершения движения/ресайза
        if self.scene() and hasattr(self.scene(), "clear_guides"):
            self.scene().clear_guides()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and not self.is_read_only():
            self.start_editing()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsObject.ItemPositionChange and self.scene():
            new_pos = QPointF(value)
            scene = self.scene()
            if getattr(scene, '_exact_card_move', False):
                return new_pos
            guides = []
            snap_x = None
            snap_y = None

            is_aligning = getattr(scene, "smart_alignment", True) and not self._resizing

            # Расчет умных направляющих относительно других карточек
            if is_aligning:
                SNAP_THRESH = 6.0
                my_x, my_y = new_pos.x(), new_pos.y()
                my_w, my_h = self._width, self._height

                my_xs = [my_x, my_x + my_w / 2.0, my_x + my_w]
                my_ys = [my_y, my_y + my_h / 2.0, my_y + my_h]

                best_dx = None
                best_dy = None
                guide_x_line = None
                guide_y_line = None

                candidates = scene.alignment_candidates(my_xs, my_ys, SNAP_THRESH) if hasattr(scene, "alignment_candidates") else scene.items()
                for it in candidates:
                    if isinstance(it, CanvasCardItem) and it != self and not it.isSelected():
                        ox, oy = it.pos().x(), it.pos().y()
                        ow, oh = it._width, it._height

                        o_xs = [ox, ox + ow / 2.0, ox + ow]
                        o_ys = [oy, oy + oh / 2.0, oy + oh]

                        # Сопоставление по X (вертикальные направляющие)
                        for mx in my_xs:
                            for ox_val in o_xs:
                                diff = ox_val - mx
                                if abs(diff) <= SNAP_THRESH:
                                    if best_dx is None or abs(diff) < abs(best_dx):
                                        best_dx = diff
                                        min_y = min(my_y, oy) - 30
                                        max_y = max(my_y + my_h, oy + oh) + 30
                                        guide_x_line = QLineF(ox_val, min_y, ox_val, max_y)

                        # Сопоставление по Y (горизонтальные направляющие)
                        for my in my_ys:
                            for oy_val in o_ys:
                                diff = oy_val - my
                                if abs(diff) <= SNAP_THRESH:
                                    if best_dy is None or abs(diff) < abs(best_dy):
                                        best_dy = diff
                                        min_x = min(my_x, ox) - 30
                                        max_x = max(my_x + my_w, ox + ow) + 30
                                        guide_y_line = QLineF(min_x, oy_val, max_x, oy_val)

                if best_dx is not None:
                    snap_x = my_x + best_dx
                    if guide_x_line:
                        guides.append(guide_x_line)

                if best_dy is not None:
                    snap_y = my_y + best_dy
                    if guide_y_line:
                        guides.append(guide_y_line)

                if hasattr(scene, "set_guides"):
                    scene.set_guides(guides)

            # Привязка к сетке для невыровненных осей
            if self.snap_to_grid and not self._resizing:
                final_x = snap_x if snap_x is not None else round(new_pos.x() / GRID_SIZE) * GRID_SIZE
                final_y = snap_y if snap_y is not None else round(new_pos.y() / GRID_SIZE) * GRID_SIZE
            else:
                final_x = snap_x if snap_x is not None else new_pos.x()
                final_y = snap_y if snap_y is not None else new_pos.y()

            return QPointF(final_x, final_y)

        elif change == QGraphicsObject.ItemPositionHasChanged:
            for arrow in self.connected_arrows:
                arrow.update_path()
            scene = self.scene()
            if scene is not None and hasattr(scene, "card_geometry_changed"):
                scene.card_geometry_changed(self)
        elif change == QGraphicsObject.ItemZValueHasChanged:
            scene = self.scene()
            if scene is not None and hasattr(scene, "card_order_changed"):
                scene.card_order_changed()
        elif change == QGraphicsObject.ItemSelectedChange:
            if not value:
                # Scene selection can change without the embedded editor losing
                # focus (menus, rubber bands, programmatic clearSelection).
                if self._is_editing:
                    self._on_editing_finished()
                if self._resizing:
                    self._finish_resize_interaction()
            self._sync_render_state(selected_override=value)
            self.update()
        return super().itemChange(change, value)

    def eventFilter(self, watched, event):
        from shiboken6 import isValid
        if not isValid(self.editor):
            return False
        if watched == self.editor.viewport():
            if not self._is_editing:
                if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                    self.setSelected(True)
                    return False
                elif event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                    self.start_editing()
                    return True
        return super().eventFilter(watched, event)

    def start_editing(self, click_pos: QPointF = None):
        if self.is_read_only():
            return

        self.setSelected(True)
        self._is_editing = True
        self._sync_render_state(selected_override=True)
        self.proxy.setAcceptedMouseButtons(Qt.LeftButton | Qt.RightButton | Qt.MiddleButton)
        self.editor.setReadOnly(False)
        self.editor.setCursor(QCursor(Qt.IBeamCursor))
        self.setCursor(QCursor(Qt.IBeamCursor))
        self.editor.setCursorWidth(2)

        # Передаем активный фокус окну, сцене и виджету
        if self.scene():
            self.scene().setFocusItem(self.proxy)
            views = self.scene().views()
            if views:
                views[0].setFocus()

        self.proxy.setFocus()
        self.editor.setFocus(Qt.MouseFocusReason)

        # Переносим каретку в место клика
        if click_pos is not None:
            local_p = self.proxy.mapFromItem(self, click_pos).toPoint()
            cursor = self.editor.cursorForPosition(local_p)
            cursor.clearSelection()
            self.editor.setTextCursor(cursor)
        else:
            cursor = self.editor.textCursor()
            cursor.clearSelection()
            self.editor.setTextCursor(cursor)

        self.editor.ensureCursorVisible()

        # Начинаем чистую сессию отката текста для этой карточки
        if hasattr(self.editor.document(), "clearUndoRedoStacks"):
            self.editor.document().clearUndoRedoStacks()

        # Принудительно шлем FocusIn, чтобы Qt гарантированно включил таймер моргания каретки
        ev = QFocusEvent(QEvent.FocusIn, Qt.MouseFocusReason)
        QApplication.sendEvent(self.editor, ev)

        if hasattr(self.editor, "highlighter") and self.editor.highlighter:
            self.editor.highlighter._schedule_sync()

        self.update()

    def set_document_path(self, path):
        self.editor.file_path = path
        self.preview.base_path = path
        self._preview_key = None
        self._refresh_preview()

    def _remove_image(self, source):
        if self.is_read_only():
            return
        self.editor.enable_live_preview()
        self.editor.markdown_handler.images.remove_source(source)
        self._refresh_preview()

    def _center_text(self):
        if self.text_align != 'card_center':
            self.editor.setMinimumHeight(0)
            self.editor.setMaximumHeight(16777215)
            self.proxy.setPos(6, 6)
            self.proxy.resize(max(40, self._width-12), max(30, self._height-12))
            return
        height = 8 + self.editor.document().documentMargin()*2
        block = self.editor.document().firstBlock()
        while block.isValid():
            height += self.editor.blockBoundingRect(block).height()
            block = block.next()
        available = max(30, self._height-12)
        height = min(available, max(30, height))
        self.editor.setFixedHeight(round(height))
        self.proxy.resize(max(40, self._width-12), height)
        self.proxy.setPos(6, 6+(available-height)/2)

    def _follow_edit_cursor(self):
        if not self._is_editing:
            return
        # Complete wrapping/centering before Qt computes the visible caret and
        # scroll range; otherwise the range may lag behind the key event.
        self._center_timer.stop()
        self._center_text()
        self.editor.blockBoundingRect(self.editor.textCursor().block())
        self.editor.ensureCursorVisible()

    def _on_editing_finished(self):
        self._is_editing = False
        self.proxy.setAcceptedMouseButtons(Qt.LeftButton)
        self.editor.setTextInteractionFlags(Qt.NoTextInteraction)
        self.editor.setCursor(QCursor(Qt.ArrowCursor))
        self.setCursor(QCursor(Qt.OpenHandCursor))

        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            cursor.clearSelection()
            self.editor.setTextCursor(cursor)

        self._refresh_preview()
        self._sync_render_state()
        self.update()

    def to_dict(self) -> dict:
        d = {
            "id": self.node_id,
            "type": "text",
            "text": self._source_text,
            "x": int(self.pos().x()),
            "y": int(self.pos().y()),
            "width": int(self._width),
            "height": int(self._height)
        }
        if self.color:
            d["color"] = self.color
        if self.text_align != "left":
            d["textAlign"] = self.text_align
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'CanvasCardItem':
        card = cls(
            x=data.get("x", 0),
            y=data.get("y", 0),
            w=data.get("width", 240),
            h=data.get("height", 192),
            node_id=data.get("id")
        )
        card.color = data.get("color")
        card.set_text_align(data.get("textAlign", "left"))
        card.editor.setPlainText(data.get("text", ""))
        return card

    def get_ports_local(self) -> dict:
        w, h = self._width, self._height
        return {
            "top_left": QPointF(0, 0),
            "top": QPointF(w / 2.0, 0),
            "top_right": QPointF(w, 0),
            "right": QPointF(w, h / 2.0),
            "bottom_right": QPointF(w, h),
            "bottom": QPointF(w / 2.0, h),
            "bottom_left": QPointF(0, h),
            "left": QPointF(0, h / 2.0),
        }

    def get_port_scene_pos(self, side: str) -> QPointF:
        ports = self.get_ports_local()
        pt = ports.get(side, QPointF(self._width / 2.0, self._height / 2.0))
        return self.mapToScene(pt)

    def get_port_at(self, pos: QPointF) -> str | None:
        if self.is_read_only():
            return None
        for name, pt in self.get_ports_local().items():
            if math.hypot(pos.x() - pt.x(), pos.y() - pt.y()) <= PORT_HIT_RADIUS:
                return name
        return None

    def get_closest_port(self, scene_pos: QPointF) -> str:
        local_p = self.mapFromScene(scene_pos)
        best_side = "top"
        best_dist = float("inf")
        for side, pt in self.get_ports_local().items():
            dist = math.hypot(local_p.x() - pt.x(), local_p.y() - pt.y())
            if dist < best_dist:
                best_dist = dist
                best_side = side
        return best_side

    def is_read_only(self) -> bool:
        """Проверяет, включен ли режим только чтение на холсте."""
        return bool(self.scene() and getattr(self.scene(), "read_only", False))

    def set_color(self, color_hex: Optional[str]):
        self.color = color_hex
        self.update()
        self.geometry_changed.emit()

    def set_text_align(self, align_str: str):
        self._overview_key = None
        self.text_align = align_str
        align_map = {
            "left": Qt.AlignLeft,
            "center": Qt.AlignHCenter,
            "right": Qt.AlignRight,
            "card_center": Qt.AlignHCenter
        }
        updates_enabled = self.editor.updatesEnabled()
        self.editor.setUpdatesEnabled(False)
        try:
            self.editor.set_alignment(align_map.get(align_str, Qt.AlignLeft))
            self._center_timer.stop()
            self._center_text()
            self._refresh_preview()
        finally:
            self.editor.setUpdatesEnabled(updates_enabled)
        self.geometry_changed.emit()
