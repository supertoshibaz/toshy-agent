from ui.styles.binding import watch_paint, themed_color
from ui.themes.manager import theme_manager
import re
from typing import Tuple, List
from PySide6.QtWidgets import QWidget, QPlainTextEdit
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QMouseEvent,
    QWheelEvent,
    QTextLayout,
    QTextOption,
)


class CodeMinimap(QWidget):
    """Scrollable code minimap inspired by Godot 4.7's TextEdit minimap.

    The old implementation always squeezed every visual row into the widget
    height. On a large file this eventually made rows sub-pixel thin and the
    minimap stopped being useful.

    This version keeps a fixed miniature row pitch (2 px glyph + 1 px spacing,
    matching Godot's default idea) and renders only the window of document rows
    that belongs around the current editor scroll position. The projected
    editor viewport moves through that window; when the document is long enough,
    the minimap content itself scrolls as the editor scrolls.
    """

    WIDTH = 115

    # Godot 4.7 TextEdit uses minimap_char_size.y = 2 and
    # minimap_line_spacing = 1, i.e. a stable 3 px row pitch.
    CHAR_HEIGHT = 2.0
    LINE_SPACING = 1.0
    PITCH = CHAR_HEIGHT + LINE_SPACING

    REFERENCE_TEXT_WIDTH = 960.0

    def __init__(self, editor, parent=None):
        super().__init__(parent or editor)
        watch_paint(self)
        self.editor = editor
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.ArrowCursor)
        self.setMouseTracking(True)

        self._is_dragging = False
        self._hovering_viewport = False
        self._drag_offset_y = 0.0

        self._rows_cache_key = None
        self._rows_cache = []
        self._block_rows = None
        self._layout_key = None
        self._row_starts = {}
        self.editor.document().contentsChange.connect(self._contents_changed)

    def invalidate_document(self):
        """Reset a projection after replacing a standalone source document."""
        self._rows_cache_key = None
        self._block_rows = None
        self._row_starts.clear()
        self.update()

    def _contents_changed(self, position, removed, added):
        # Formatting changes do not alter this source-based projection. Qt's
        # positions are UTF-16 offsets, so findBlock also handles emoji safely.
        if not (removed or added):
            return
        self._rows_cache_key = None
        if self._block_rows is None:
            return
        doc = self.editor.document()
        first = doc.findBlock(min(position, doc.characterCount() - 1)).blockNumber()
        last = doc.findBlock(min(position + added, doc.characterCount() - 1)).blockNumber()
        delta = doc.blockCount() - len(self._block_rows)
        # The changed suffix lies in new block coordinates. Subtract inserted
        # blocks (or add deleted ones) to locate the old cache slice to replace.
        old_end = last + 1 - delta
        if first < 0 or old_end < first or old_end > len(self._block_rows):
            self._block_rows = None
            return
        wrap, width, _font, _tabs = self._layout_key
        blocks = []
        block = doc.findBlockByNumber(first)
        for _ in range(first, last + 1):
            blocks.append(self._rows_for_text(block.text(), wrap, width))
            block = block.next()
        self._block_rows[first:old_end] = blocks
        if len(self._block_rows) != doc.blockCount():
            self._block_rows = None

    def _rows_for_text(self, text, wrap, width):
        if not wrap or not text:
            return [(text, 0, len(text))]
        layout = QTextLayout(text, self.editor.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        option.setTabStopDistance(max(1.0, float(self.editor.fontMetrics().horizontalAdvance(' ')) * 4.0))
        layout.setTextOption(option)
        layout.beginLayout()
        rows = []
        while True:
            line = layout.createLine()
            if not line.isValid(): break
            line.setLineWidth(width)
            rows.append((text, max(0, line.textStart()), max(0, line.textLength())))
        layout.endLayout()
        return rows or [(text, 0, len(text))]

    # ------------------------------------------------------------------
    # Document projection
    # ------------------------------------------------------------------

    def _visual_rows(self) -> List[tuple]:
        """Return document rows in the same visual wrapping model as the editor."""
        doc = self.editor.document()
        wrap = self.editor.lineWrapMode() != QPlainTextEdit.NoWrap
        wrap_width = (
            float(self.editor.wrap_width_pixels())
            if hasattr(self.editor, "wrap_width_pixels")
            else max(
                1.0,
                float(self.editor.viewport().width())
                - 2.0 * float(doc.documentMargin()),
            )
        )
        key = (bool(wrap), wrap_width, self.editor.font().toString(), self.editor.tabStopDistance())
        if key == self._rows_cache_key:
            return self._rows_cache
        if self._block_rows is None or key != self._layout_key:
            self._block_rows = []
            block = doc.begin()
            while block.isValid():
                self._block_rows.append(self._rows_for_text(block.text(), wrap, wrap_width))
                block = block.next()
            self._layout_key = key
        # Renumber lightweight tuples after a structural edit; expensive text
        # wrapping above is retained for every unchanged paragraph.
        rows = [(number, text, start, length)
                for number, block_rows in enumerate(self._block_rows)
                for text, start, length in block_rows]
        self._row_starts = {}
        for index, row in enumerate(rows):
            self._row_starts.setdefault(row[0], index)
        self._rows_cache_key = key
        self._rows_cache = rows or [(0, "", 0, 0)]
        return self._rows_cache

    def _editor_visible_rows(self, total_rows: int) -> float:
        """Approximate how many minimap rows the editor viewport currently shows."""
        viewport_h = float(max(1, self.editor.viewport().height()))
        line_h = max(1.0, float(self.editor.fontMetrics().lineSpacing()))
        visible = max(1.0, viewport_h / line_h)
        return visible

    def _viewport_rows(self, total_rows):
        provider = getattr(self.editor, 'minimap_viewport_rows', None)
        if provider is not None:
            return provider()
        block = self.editor.firstVisibleBlock()
        if not block.isValid():
            return 0.0, self._editor_visible_rows(total_rows)
        top = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset()).top()
        layout = block.layout()
        line_height = (layout.lineAt(0).height() if layout and layout.lineCount()
                       else self.editor.fontMetrics().lineSpacing())
        first = self._row_starts.get(block.blockNumber(), 0) + max(0.0, -top) / max(1.0, line_height)
        return first, self._editor_visible_rows(total_rows)

    def _get_geometry_info(self) -> Tuple[int, int, float, float, float, float]:
        rows = self._visual_rows()
        total_rows = max(1, len(rows))
        height = float(max(1, self.height()))
        pitch = float(self.PITCH)
        capacity = max(1, int(height // pitch))
        first, visible = self._viewport_rows(total_rows)
        visible = max(1.0, visible)
        progress = min(1.0, max(0.0, first / max(1.0, total_rows - visible)))
        # The document window stops at its last row. The viewport projection
        # keeps moving when Qt scrolls into the blank area after the text.
        normal_slider_y = progress * max(0.0, height - visible * pitch)
        start = max(0, min(int(round(first - normal_slider_y / pitch)),
                           max(0, total_rows - capacity)))
        slider_y = (first - start) * pitch
        slider_h = max(pitch, visible * pitch)
        track_height = min(height, total_rows * pitch)
        return start, capacity, track_height, slider_y, slider_h, progress

    def _get_slider_rect(self) -> QRectF:
        _, _, _, slider_y, slider_h, _ = self._get_geometry_info()
        return QRectF(2.0, slider_y, float(self.WIDTH) - 4.0, slider_h)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = float(self.width())
        height = float(self.height())

        painter.fillRect(QRectF(0, 0, width, height), theme_manager.color('background.window', shift=-0.02))
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setRenderHint(QPainter.Antialiasing, True)

        rows = self._visual_rows()
        (
            window_start,
            window_capacity,
            _track_height,
            slider_y,
            slider_h,
            _scroll_ratio,
        ) = self._get_geometry_info()

        pitch = float(self.PITCH)
        row_height = float(self.CHAR_HEIGHT)

        padding_left = 6.0
        usable_w = max(1.0, width - 12.0)
        avg_char_px = max(
            1.0,
            float(self.editor.fontMetrics().averageCharWidth()),
        )

        # Keep the existing Toshy horizontal projection: a stable scale based
        # on the normal reading-column width. Squeezing the editor horizontally
        # therefore does not stretch minimap tokens.
        reference_width = max(
            1.0,
            self.REFERENCE_TEXT_WIDTH
            - 2.0 * float(self.editor.document().documentMargin()),
        )
        visible_chars = max(1.0, reference_width / avg_char_px)
        char_w = usable_w / visible_chars

        line_status = getattr(self.editor, "line_status_map", {})
        op_chars = set("+-*/%=<>!&|^~:;,()[]{}.@#")
        doc = self.editor.document()

        end_row = min(
            len(rows),
            window_start + window_capacity + 1,
        )

        for row_idx in range(window_start, end_row):
            block_num, text, start, length = rows[row_idx]
            y = float(row_idx - window_start) * pitch
            if y > height:
                break

            status = line_status.get(block_num)
            if status == "error":
                painter.fillRect(
                    QRectF(0, y, width, max(2.0, pitch)),
                    QColor(239, 68, 68, 85),
                )
                painter.fillRect(
                    QRectF(width - 4, y, 3, max(2.0, pitch)),
                    QColor("#ef4444"),
                )
            elif status == "warning":
                painter.fillRect(
                    QRectF(0, y, width, max(2.0, pitch)),
                    QColor(234, 179, 8, 65),
                )
                painter.fillRect(
                    QRectF(width - 4, y, 3, max(2.0, pitch)),
                    QColor("#eab308"),
                )

            segment = text[start : start + length]
            if not segment.strip():
                continue

            # Base word silhouettes.
            for match in re.finditer(r"\S+", segment):
                word_x = padding_left + match.start() * char_w
                word_w = max(1.2, len(match.group()) * char_w)
                if word_x < width - 6:
                    painter.fillRect(
                        QRectF(
                            word_x,
                            y,
                            min(word_w, width - 6 - word_x),
                            row_height,
                        ),
                        theme_manager.color('border.subtle'),
                    )

            # Syntax-highlight colors over the neutral silhouettes.
            block = doc.findBlockByNumber(block_num)
            layout = block.layout() if block.isValid() else None
            formats = layout.formats() if layout else []

            for fr in formats:
                fr_start = fr.start
                fr_end = fr.start + fr.length
                seg_end = start + length

                overlap_start = max(start, fr_start)
                overlap_end = min(seg_end, fr_end)
                if overlap_end <= overlap_start:
                    continue

                token_str = text[overlap_start:overlap_end].strip()
                if (
                    not getattr(self.editor, "is_markdown", False)
                    and token_str
                    and all(ch in op_chars for ch in token_str)
                ):
                    continue

                col = fr.format.foreground().color()
                if not col.isValid() or col.alpha() <= 0:
                    continue

                if getattr(self.editor, "is_markdown", False) and not (
                    col.red() > 245
                    and col.green() > 245
                    and col.blue() > 245
                ):
                    continue

                token_x = padding_left + (overlap_start - start) * char_w
                token_w = max(1.0, (overlap_end - overlap_start) * char_w)
                if token_x < width - 6:
                    painter.fillRect(
                        QRectF(
                            token_x,
                            y,
                            min(token_w, width - 6 - token_x),
                            row_height,
                        ),
                        theme_manager.color('text.primary')
                        if getattr(self.editor, "is_markdown", False)
                        else col,
                    )

        # Project the editor viewport over the moving minimap window.
        slider_rect = QRectF(2.0, slider_y, width - 4.0, slider_h)

        if self._is_dragging:
            fill_alpha = 64   # ~25 %, Godot-like drag emphasis.
        elif self._hovering_viewport:
            fill_alpha = 45   # ~17.5 %.
        else:
            fill_alpha = 26   # ~10 %.

        painter.setBrush(QBrush(themed_color('text.primary', fill_alpha)))
        painter.setPen(QPen(themed_color('text.primary', 75), 1.0))
        painter.drawRect(slider_rect)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _scroll_to_document_row(self, target_row: float, *, center: bool = True):
        rows = self._visual_rows()
        total = max(1, len(rows))
        target_row = max(0.0, min(float(total - 1), float(target_row)))
        provider = getattr(self.editor, 'minimap_scroll_to_row', None)
        if provider is not None:
            provider(target_row, center=center)
            return
        _, visible = self._viewport_rows(total)
        first = max(0.0, target_row - visible / 2.0 if center else target_row)
        bar = self.editor.verticalScrollBar()
        row = rows[min(total - 1, int(round(first)))]
        document = self.editor.document()
        block = document.findBlockByNumber(row[0])
        # Qt lays out wrapped paragraphs lazily. Its scrollbar counts the lines
        # laid out so far, which can differ from our complete source projection.
        document.documentLayout().blockBoundingRect(block)
        line = block.layout().lineForTextPosition(row[2])
        offset = line.lineNumber() if line.isValid() else 0
        bar.setValue(block.firstLineNumber() + offset)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        click_y = float(event.position().y())
        (
            window_start,
            _window_capacity,
            _track_height,
            slider_y,
            slider_h,
            _scroll_ratio,
        ) = self._get_geometry_info()

        slider_rect = self._get_slider_rect()
        if slider_rect.contains(event.position()):
            # Drag the viewport rectangle exactly as a scrollbar thumb.
            self._is_dragging = True
            self._drag_offset_y = click_y - slider_y
        else:
            # Godot-style click: pick the document row represented by this
            # minimap position and center the editor viewport around it.
            target_row = float(window_start) + click_y / float(self.PITCH)
            self._scroll_to_document_row(target_row, center=True)

            # Continue as a centered thumb drag if the mouse remains held.
            self._is_dragging = True
            self._drag_offset_y = slider_h / 2.0

        self._hovering_viewport = True
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._is_dragging and (event.buttons() & Qt.LeftButton):
            curr_y = float(event.position().y())
            total = len(self._visual_rows())
            _, visible = self._viewport_rows(total)
            top = curr_y - self._drag_offset_y
            if total * self.PITCH > self.height():
                travel = max(1.0, self.height() - visible * self.PITCH)
                target_row = top / travel * max(1.0, total - visible)
            else:
                target_row = top / self.PITCH
            # Use an absolute position: deriving the target from the moving
            # minimap window makes repeated mouse events drift through a file.
            self._scroll_to_document_row(target_row, center=False)

            self._hovering_viewport = True
            self.setCursor(Qt.SizeVerCursor)
            self.update()
            event.accept()
            return

        hovering = self._get_slider_rect().contains(event.position())
        if hovering != self._hovering_viewport:
            self._hovering_viewport = hovering
            self.update()

        self.setCursor(Qt.SizeVerCursor if hovering else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._is_dragging = False
            self._hovering_viewport = self._get_slider_rect().contains(event.position())
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if not self._is_dragging and self._hovering_viewport:
            self._hovering_viewport = False
            self.update()
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        # Keep the minimap as a navigation surface: the wheel scrolls the real
        # editor, which in turn moves the minimap window via its scrollbar signal.
        self.editor.wheelEvent(event)
