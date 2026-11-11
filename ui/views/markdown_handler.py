from ui.themes.manager import theme_manager
from ui.icons import set_icon
from ui.i18n import set_text

import re
from bisect import bisect_left, bisect_right
import math
from pathlib import Path
from typing import List, Tuple, Optional

from ui.qt_deferred import defer

from PySide6.QtWidgets import QToolButton, QPlainTextEdit
from PySide6.QtCore import Qt, QRect, QRectF, QSize, QSizeF, QPoint, QPointF, QTimer, QUrl, QObject
from PySide6.QtGui import (
    QKeySequence, QShortcut, QColor, QPainter, QPen, QBrush,
    QPainterPath, QTextCursor, QKeyEvent, QGuiApplication, QDesktopServices, QMouseEvent,
    QTextOption, QTextFormat, QTextCharFormat, QTextObjectInterface, QFont, QFontMetricsF
)

try:
    from PySide6.QtGui import QPyTextObject
except ImportError:  # Older PySide6 builds: use the documented interface directly.
    QPyTextObject = None


class CodeBlockCopyButton(QToolButton):
    """Плавающая кнопка копирования блока кода в Markdown."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CodeBlockCopyBtn")
        self.setFixedSize(20, 20)
        set_icon(self, 'copy', square=True)
        self.setCursor(Qt.PointingHandCursor)
        set_text(self, 'ui.console.copy_1ecd', setter='setToolTip')
        self.code_text = ""
        self.code_text_getter = None
        self.clicked.connect(self._do_copy)

    def _do_copy(self):
        if self.code_text_getter is not None:
            self.code_text = self.code_text_getter()
        if self.code_text:
            QGuiApplication.clipboard().setText(self.code_text)


_OBJECT_REPLACEMENT = "\ufffc"
_TABLE_OBJECT_TYPE = (QTextFormat.UserObject.value if hasattr(QTextFormat.UserObject, "value") else int(QTextFormat.UserObject)) + 37
_TABLE_SOURCE_PROPERTY = (QTextFormat.UserProperty.value if hasattr(QTextFormat.UserProperty, "value") else int(QTextFormat.UserProperty)) + 37
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


def _split_pipe_row(line: str) -> List[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]

    cells: List[str] = []
    buf: List[str] = []
    escaped = False
    for ch in text:
        if ch == "|" and not escaped:
            cells.append("".join(buf).strip())
            buf = []
            continue
        if ch == "\\" and not escaped:
            escaped = True
            buf.append(ch)
            continue
        escaped = False
        buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def _parse_table_source(source: str):
    lines = source.splitlines()
    if len(lines) < 2 or not _TABLE_SEPARATOR_RE.match(lines[1]):
        return None

    headers = _split_pipe_row(lines[0])
    separators = _split_pipe_row(lines[1])
    rows = [_split_pipe_row(line) for line in lines[2:] if line.strip()]
    column_count = max([1, len(headers), len(separators)] + [len(row) for row in rows])

    headers = headers + [""] * (column_count - len(headers))
    separators = separators + [""] * (column_count - len(separators))
    rows = [row + [""] * (column_count - len(row)) for row in rows]

    aligns = []
    for cell in separators:
        c = cell.strip()
        if c.startswith(":") and c.endswith(":"):
            aligns.append(Qt.AlignHCenter)
        elif c.endswith(":"):
            aligns.append(Qt.AlignRight)
        else:
            aligns.append(Qt.AlignLeft)
    return headers, separators, rows, aligns


def _plain_cell_text(text: str) -> str:
    # The editor table object is a compact visual preview, not a second full
    # Markdown renderer.  Strip the most common inline delimiters only so cell
    # widths and text stay readable without pulling in an HTML engine.
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*\*(.*?)\*\*\*", r"\1", text)
    text = re.sub(r"___(.*?)___", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text.replace(r"\|", "|")


class _MarkdownTablePainter:
    """Shared implementation used by QPyTextObject/QTextObjectInterface."""

    H_PAD = 9.0
    V_PAD = 5.0
    MIN_COL_W = 54.0
    MAX_COL_W = 300.0
    OUTER_GAP = 4.0

    def __init__(self, editor: QPlainTextEdit):
        self.editor = editor

    def _table_layout(self, source: str, max_width: Optional[float] = None):
        parsed = _parse_table_source(source)
        if parsed is None:
            return None
        headers, _separators, rows, aligns = parsed
        all_rows = [headers, *rows]

        base_font = QFont(self.editor.font())
        header_font = QFont(base_font)
        header_font.setWeight(QFont.DemiBold)
        base_fm = QFontMetricsF(base_font)
        header_fm = QFontMetricsF(header_font)

        col_count = len(headers)
        preferred = [self.MIN_COL_W] * col_count
        for row_index, row in enumerate(all_rows):
            fm = header_fm if row_index == 0 else base_fm
            for col, raw in enumerate(row):
                text = _plain_cell_text(raw)
                w = fm.horizontalAdvance(text) + (self.H_PAD * 2.0)
                preferred[col] = max(preferred[col], min(self.MAX_COL_W, w))

        if max_width is None:
            max_width = max(120.0, float(self.editor.viewport().width()) - 12.0)
        max_width = max(80.0, float(max_width))

        total = sum(preferred) + 1.0
        widths = list(preferred)
        if total > max_width:
            target = max(1.0, max_width - 1.0)
            minimum_total = self.MIN_COL_W * col_count
            if minimum_total >= target:
                widths = [target / col_count] * col_count
            else:
                shrinkable = sum(max(0.0, w - self.MIN_COL_W) for w in preferred)
                extra_target = target - minimum_total
                scale = 0.0 if shrinkable <= 0 else min(1.0, extra_target / shrinkable)
                widths = [self.MIN_COL_W + max(0.0, w - self.MIN_COL_W) * scale for w in preferred]

        row_heights: List[float] = []
        for row_index, row in enumerate(all_rows):
            fm = header_fm if row_index == 0 else base_fm
            row_h = fm.height() + self.V_PAD * 2.0
            for col, raw in enumerate(row):
                text = _plain_cell_text(raw)
                text_w = max(1.0, widths[col] - self.H_PAD * 2.0)
                bounds = fm.boundingRect(
                    QRectF(0.0, 0.0, text_w, 10000.0),
                    int(Qt.TextWordWrap | Qt.AlignLeft),
                    text,
                )
                row_h = max(row_h, bounds.height() + self.V_PAD * 2.0)
            row_heights.append(row_h)

        return {
            "headers": headers,
            "rows": rows,
            "aligns": aligns,
            "widths": widths,
            "row_heights": row_heights,
            "width": sum(widths) + 1.0,
            "height": sum(row_heights) + 1.0,
            "base_font": base_font,
            "header_font": header_font,
        }

    def intrinsic_size(self, fmt) -> QSizeF:
        source = str(fmt.property(_TABLE_SOURCE_PROPERTY) or "")
        layout = self._table_layout(source)
        if layout is None:
            return QSizeF(1.0, float(self.editor.fontMetrics().height()))
        return QSizeF(layout["width"], layout["height"] + self.OUTER_GAP)

    def draw(self, painter: QPainter, rect: QRectF, fmt):
        source = str(fmt.property(_TABLE_SOURCE_PROPERTY) or "")
        layout = self._table_layout(source, max_width=rect.width())
        if layout is None:
            return

        widths = layout["widths"]
        heights = layout["row_heights"]
        rows = [layout["headers"], *layout["rows"]]
        aligns = layout["aligns"]

        card = QRectF(rect.left(), rect.top() + self.OUTER_GAP / 2.0, layout["width"], layout["height"])
        radius = 5.0

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(card, radius, radius)
        painter.setClipPath(path)
        painter.fillRect(card, theme_manager.color('background.input'))

        if heights:
            header_rect = QRectF(card.left(), card.top(), card.width(), heights[0])
            painter.fillRect(header_rect, theme_manager.color('background.surface'))

        painter.setPen(QPen(theme_manager.color('border.subtle'), 1.0))
        y = card.top()
        for row_h in heights[:-1]:
            y += row_h
            painter.drawLine(QPointF(card.left(), y), QPointF(card.right(), y))
        x = card.left()
        for col_w in widths[:-1]:
            x += col_w
            painter.drawLine(QPointF(x, card.top()), QPointF(x, card.bottom()))

        y = card.top()
        for row_index, row in enumerate(rows):
            row_h = heights[row_index]
            x = card.left()
            is_header = row_index == 0
            painter.setFont(layout["header_font"] if is_header else layout["base_font"])
            painter.setPen(theme_manager.color('text.primary') if is_header else theme_manager.color('text.primary'))
            for col, raw in enumerate(row):
                cell_w = widths[col]
                cell_rect = QRectF(
                    x + self.H_PAD,
                    y + self.V_PAD,
                    max(1.0, cell_w - self.H_PAD * 2.0),
                    max(1.0, row_h - self.V_PAD * 2.0),
                )
                horizontal = aligns[col] if col < len(aligns) else Qt.AlignLeft
                painter.drawText(
                    cell_rect,
                    int(horizontal | Qt.AlignVCenter | Qt.TextWordWrap),
                    _plain_cell_text(raw),
                )
                x += cell_w
            y += row_h

        painter.setClipping(False)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1.0))
        painter.drawRoundedRect(card, radius, radius)
        painter.restore()


if QPyTextObject is not None:
    class MarkdownTableTextObject(QPyTextObject):
        def __init__(self, editor: QPlainTextEdit):
            super().__init__()
            self._impl = _MarkdownTablePainter(editor)

        def intrinsicSize(self, doc, posInDocument, format):
            return self._impl.intrinsic_size(format)

        def drawObject(self, painter, rect, doc, posInDocument, format):
            self._impl.draw(painter, rect, format)
else:
    class MarkdownTableTextObject(QObject, QTextObjectInterface):
        def __init__(self, editor: QPlainTextEdit):
            super().__init__(editor)
            self._impl = _MarkdownTablePainter(editor)

        def intrinsicSize(self, doc, posInDocument, format):
            return self._impl.intrinsic_size(format)

        def drawObject(self, painter, rect, doc, posInDocument, format):
            self._impl.draw(painter, rect, format)


class MarkdownHandler:
    """
    Изолированный обработчик всей логики Markdown для редактора:
    - Умные списки (-, +, *, 1., 1))
    - Хоткеи форматирования (Ctrl+B, Ctrl+I, Ctrl+K и др.)
    - Блоки кода с кнопками копирования
    - Горизонтальные разделители (---)
    - Переход по ссылкам (Ctrl+Click)
    """
    def __init__(self, editor: QPlainTextEdit):
        self.editor = editor
        # `enabled` controls Markdown editing helpers (smart pairs, lists, shortcuts).
        # `visual_enabled` controls only Live-mode decorations.  Keeping these
        # separate lets Raw mode remain a proper Markdown editor instead of
        # silently losing the original typing behaviour.
        self.enabled = False
        self.visual_enabled = False
        self.code_block_ranges: List[Tuple[int, int]] = []
        self._code_starts = []
        self._code_ends = []
        self._code_closings = set()
        self._source_revision = 0
        self._parsed_key = None
        self.editor.document().contentsChange.connect(self._contents_changed)
        self.copy_buttons: List[CodeBlockCopyButton] = []
        self.render_buttons = []
        self.fold_buttons = []
        self._code_languages = {}
        self.shortcuts: List[QShortcut] = []
        # Live-only visual inset for fenced code blocks. 8 px mirrors the
        # cellpadding used by File Preview. This moves QTextLine geometry only;
        # source Markdown and undo history remain untouched.
        self.code_body_inset_px = 8.0
        self._code_inset_blocks = set()
        self._code_inset_refs = {}

        # QPlainTextEdit uses QPlainTextDocumentLayout, which does not apply
        # QTextBlockFormat paragraph margins while wrapping.  Keep our own
        # per-block hanging-indent geometry and lay out only matching blocks
        # with a narrower continuation width.
        self._hanging_indent_widths = {}
        self._custom_hanging_blocks = set()
        self._custom_hanging_refs = {}
        self._syncing_hanging_indents = False
        self._original_word_wrap_mode = self.editor.wordWrapMode()

        # Share source analysis between direct host calls and queued refreshes.
        # Cursor changes need another scan only when inline table objects are
        # enabled and that gesture actually changes the source representation.
        self._layout_refresh_timer = QTimer(self.editor)
        self._layout_refresh_timer.setSingleShot(True)
        self._layout_refresh_timer.setInterval(0)
        self._layout_refresh_timer.timeout.connect(self.parse_code_blocks)
        self.editor.cursorPositionChanged.connect(self._schedule_layout_refresh)
        self._visual_refresh_timer = QTimer(self.editor)
        self._visual_refresh_timer.setSingleShot(True)
        self._visual_refresh_timer.setInterval(20)
        self._visual_refresh_timer.timeout.connect(self._refresh_visible_layout)

        # Live table cards are real QTextDocument inline objects.  The source
        # Markdown is stored on the object's QTextCharFormat and restored when
        # the user clicks the card, switches to Raw, or disables Markdown.
        self._syncing_table_objects = False
        self._table_renderer = None
        self._table_objects_enabled = bool(
            getattr(self.editor, "enable_live_table_objects", False)
        )
        if self._table_objects_enabled:
            try:
                self._table_renderer = MarkdownTableTextObject(self.editor)
                self.editor.document().documentLayout().registerHandler(
                    _TABLE_OBJECT_TYPE, self._table_renderer
                )
            except Exception:
                # The rest of Markdown editing must remain usable even on an odd
                # PySide build where custom text objects are unavailable.
                self._table_renderer = None

        from ui.views.inline_images import InlineImages
        from ui.views.ordered_lists import OrderedLists
        self.images = InlineImages(self)
        self.ordered_lists = OrderedLists(self)
        from ui.views.editor_code_blocks import LiveCodeBlocks
        self.code_objects = LiveCodeBlocks(self)
        from ui.views.link_cursor import register_link_cursor
        register_link_cursor(self.editor.viewport(), self.get_url_at_pos)
        self._setup_shortcuts()

    def is_in_code_block(self, block_num: int) -> bool:
        """Проверяет, находится ли строка внутри многострочного блока кода ```."""
        index = bisect_right(self._code_starts, block_num) - 1
        return index >= 0 and block_num <= self._code_ends[index]

    def _contents_changed(self, _position, removed, added):
        if removed or added:
            # Block handles follow insertions/deletions; stored block numbers
            # alone could reset the wrong layout after lines shifted.
            self._code_inset_refs = {b.blockNumber(): b for b in self._code_inset_refs.values() if b.isValid()}
            self._code_inset_blocks = set(self._code_inset_refs)
            self._custom_hanging_refs = {b.blockNumber(): b for b in self._custom_hanging_refs.values() if b.isValid()}
            self._custom_hanging_blocks = set(self._custom_hanging_refs)
            self._source_revision += 1
            self._schedule_layout_refresh()

    def _visible_blocks(self):
        block = self.editor.firstVisibleBlock()
        offset = self.editor.contentOffset()
        while block.isValid():
            rect = self.editor.blockBoundingGeometry(block).translated(offset)
            if rect.top() > self.editor.viewport().height(): break
            if block.isVisible() and rect.bottom() >= 0: yield block
            block = block.next()

    def _visible_code_ranges(self, *, opening_only=False):
        visible = list(self._visible_blocks())
        if not visible: return []
        first, last = visible[0].blockNumber(), visible[-1].blockNumber()
        lo = (bisect_left(self._code_starts, first) if opening_only
              else bisect_left(self._code_ends, first))
        hi = bisect_right(self._code_starts, last)
        return self.code_block_ranges[lo:hi]

    def _refresh_visible_layout(self):
        self._apply_code_body_insets()
        self.position_copy_buttons()
        self.editor.viewport().update()

    def _schedule_layout_refresh(self):
        if (
            self.enabled
            and not self._syncing_hanging_indents
            and not self._syncing_table_objects
        ):
            self._layout_refresh_timer.start()

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        if not self.enabled:
            self.code_objects.restore_all()
        self.images.sync()
        for sc in self.shortcuts:
            sc.setEnabled(self.enabled)

        if not self.enabled:
            self._restore_all_table_objects()
            self._apply_code_body_insets(False)
            self._sync_hanging_indents(force_clear=True)
            self.editor.setWordWrapMode(self._original_word_wrap_mode)
            self.code_block_ranges.clear()
            self._code_starts.clear()
            self._code_ends.clear()
            self._code_closings.clear()
            self._parsed_key = None
            self._layout_refresh_timer.stop()
            self._visual_refresh_timer.stop()
            self.visual_enabled = False
            for btn in [*self.copy_buttons, *self.render_buttons, *self.fold_buttons]:
                btn.hide()
            self.editor.viewport().update()
        else:
            # Prefer word boundaries for normal prose, but still allow Qt to
            # split an overlong token instead of moving the whole token below
            # a Markdown marker.
            self.editor.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            self.parse_code_blocks()

    def set_visual_enabled(self, enabled: bool):
        """Enable/disable only Live Markdown decorations.

        Editing helpers intentionally stay active in Raw mode.
        """
        self.visual_enabled = bool(enabled) and self.enabled
        if not self.visual_enabled:
            self.code_objects.restore_all()
        self.images.sync()
        if self.visual_enabled:
            self.parse_code_blocks()
            self._visual_refresh_timer.start()
        else:
            self._restore_all_table_objects()
            self._apply_code_body_insets(False)
            for btn in [*self.copy_buttons, *self.render_buttons, *self.fold_buttons]:
                btn.hide()
        self.editor.viewport().update()

    def _setup_shortcuts(self):
        pairs = [
            ("Ctrl+B", "**", "**"),
            ("Ctrl+I", "*", "*"),
            ("Ctrl+K", "[", "](url)"),
            ("Ctrl+Shift+X", "~~", "~~"),
            ("Ctrl+`", "`", "`"),
        ]
        for key, p, s in pairs:
            sc = QShortcut(QKeySequence(key), self.editor)
            sc.activated.connect(lambda prefix=p, suffix=s: self.wrap_selection(prefix, suffix))
            sc.setEnabled(False)
            self.shortcuts.append(sc)

    def wrap_selection(self, prefix: str, suffix: str):
        if not self.enabled:
            return
        cursor = self.editor.textCursor()
        if prefix != '`' and self._format_full_lines(prefix, suffix):
            return
        if cursor.hasSelection():
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            sel_text = self.selected_source_text(cursor)
            cursor.insertText(f"{prefix}{sel_text}{suffix}")
            cursor.setPosition(start + len(prefix))
            cursor.setPosition(start + len(prefix) + len(sel_text.encode('utf-16-le')) // 2, QTextCursor.KeepAnchor)
            self.editor.setTextCursor(cursor)
        else:
            pos = cursor.position()
            cursor.insertText(f"{prefix}{suffix}")
            cursor.setPosition(pos + len(prefix))
            self.editor.setTextCursor(cursor)

    def _format_full_lines(self, prefix, suffix='', *, line_prefix=False):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return False
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        document = self.editor.document()
        first, last = document.findBlock(start), document.findBlock(end)
        if line_prefix and prefix == '# ':
            last = document.findBlock(max(start, end - 1))
            start, end = first.position(), last.position() + last.length() - 1
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
        elif first.position() != start or first == last:
            return False
        if end not in {last.position(), last.position() + last.length() - 1}:
            return False
        source = self.selected_source_text(cursor)
        tail = '\n' if source.endswith('\n') else ''
        lines = source[:-1].split('\n') if tail else source.split('\n')
        result = []
        for line in lines:
            indent = line[:len(line) - len(line.lstrip(' \t'))]
            content = line[len(indent):]
            if not content:
                result.append(line)
            elif line_prefix:
                marker = '#' if prefix == '# ' and re.match(r'^#+\s', content) else prefix
                result.append(indent + marker + content)
            else:
                result.append(indent + prefix + content + suffix)
        text = '\n'.join(result) + tail
        cursor.beginEditBlock()
        cursor.insertText(text)
        cursor.endEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(start + len(text.encode('utf-16-le')) // 2, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self._line_edit_state = {'start': start, 'end': cursor.selectionEnd(), 'text': text,
            'source': source, 'prefix': prefix, 'suffix': suffix, 'line_prefix': line_prefix}
        return True

    def _handle_multiline_space(self):
        cursor = self.editor.textCursor()
        state = getattr(self, '_line_edit_state', None)
        if not state or not cursor.hasSelection() or (cursor.selectionStart(), cursor.selectionEnd()) != (state['start'], state['end']):
            return False
        if self.selected_source_text(cursor) != state['text']:
            return False
        if state['line_prefix']:
            return True  # The marker already includes its separating space.
        if state['prefix'] != '*' or state['suffix'] != '*' or any(line.strip().startswith('*') for line in state['source'].splitlines()):
            return False
        start = cursor.selectionStart()
        cursor.insertText(state['source'])
        cursor.setPosition(start)
        cursor.setPosition(start + len(state['source'].encode('utf-16-le')) // 2, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        return self._format_full_lines('* ', line_prefix=True)

    def _handle_table_enter(self) -> bool:
        cursor = self.editor.textCursor()
        block = cursor.block()
        line = block.text().strip()

        if "|" in line:
            # Нажатие Enter на пустой строке таблицы (|   |   |) — выход из таблицы
            if re.match(r"^\|(\s*\|\s*)+$", line):
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                return True

            pipes_count = line.count("|")
            if pipes_count >= 2:
                num_cols = pipes_count - 1 if (line.startswith("|") and line.endswith("|")) else pipes_count
                new_row = "| " + " | ".join([" "] * max(1, num_cols)) + " |"
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.EndOfBlock)
                cursor.insertText(f"\n{new_row}")
                cursor.endEditBlock()

                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, 2)
                self.editor.setTextCursor(cursor)
                self.editor.ensureCursorVisible()
                return True
        return False

    def _handle_table_tab(self, is_shift: bool) -> bool:
        cursor = self.editor.textCursor()
        block = cursor.block()
        line = block.text()
        pos_in_block = cursor.positionInBlock()

        if "|" in line:
            if not is_shift:
                next_pipe = line.find("|", pos_in_block)
                if next_pipe != -1 and next_pipe + 1 < len(line):
                    target_pos = block.position() + next_pipe + 1
                    if next_pipe + 1 < len(line) and line[next_pipe + 1] == " ":
                        target_pos += 1
                    cursor.setPosition(target_pos)
                    self.editor.setTextCursor(cursor)
                    return True
                else:
                    next_b = block.next()
                    if next_b.isValid() and "|" in next_b.text():
                        first_pipe = next_b.text().find("|")
                        target_pos = next_b.position() + first_pipe + 1
                        if first_pipe + 1 < len(next_b.text()) and next_b.text()[first_pipe + 1] == " ":
                            target_pos += 1
                        cursor.setPosition(target_pos)
                        self.editor.setTextCursor(cursor)
                        return True
            else:
                prev_pipe = line.rfind("|", 0, max(0, pos_in_block - 1))
                if prev_pipe != -1:
                    target_pos = block.position() + prev_pipe + 1
                    if prev_pipe + 1 < len(line) and line[prev_pipe + 1] == " ":
                        target_pos += 1
                    cursor.setPosition(target_pos)
                    self.editor.setTextCursor(cursor)
                    return True
        return False

    # =========================================================================
    # ОБРАБОТКА ВВОДА С КЛАВИАТУРЫ ДЛЯ MARKDOWN
    # =========================================================================

    def handle_key_press(self, event: QKeyEvent) -> bool:
        if not self.enabled:
            return False
        if self.code_objects.handle_key(event):
            return True

        cursor = self.editor.textCursor()
        b_num = cursor.blockNumber()

        # Never let Backspace/Delete destroy a rendered table object and lose
        # its source. Reveal the Markdown first; a second key press performs
        # the actual edit in normal source form.
        if self.visual_enabled and not cursor.hasSelection():
            pos = cursor.position()
            candidate = None
            if event.key() == Qt.Key_Backspace:
                candidate = pos - 1
            elif event.key() == Qt.Key_Delete:
                candidate = pos
            if candidate is not None and self._table_object_source_at(candidate) is not None:
                self._restore_table_object_at(candidate, place_cursor=True)
                defer(0, self, self.parse_code_blocks)
                return True

        # Inside fenced code blocks Markdown smart formatting must not mutate
        # code.  Fence lines themselves are still handled normally.
        if self.is_in_code_block(b_num):
            block_text = cursor.block().text().strip()
            if not block_text.startswith("```"):
                return False

        # Enter in lists/tables.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() == Qt.ShiftModifier:
                match = re.match(r'^([ \t]*)(?:[-+*]|\d+[.)])\s+', cursor.block().text())
                if match:
                    cursor.insertText('\n' + match.group(1) + '\t')
                    self.editor.setTextCursor(cursor)
                    return True
            if not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier)):
                if self._handle_smart_enter():
                    return True
                if self._handle_table_enter():
                    return True

        # Backspace: first remove an empty auto-pair as one edit, then fall
        # through to the existing smart-list behaviour.
        elif event.key() == Qt.Key_Backspace:
            if not (event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier)):
                if not cursor.hasSelection():
                    pos = cursor.position()
                    doc = self.editor.document()
                    pair_chars = {"*", "_", "`", "~", "="}
                    if 0 < pos < doc.characterCount() - 1:
                        before = doc.characterAt(pos - 1)
                        after = doc.characterAt(pos)
                        if before == after and before in pair_chars:
                            edit = QTextCursor(doc)
                            edit.beginEditBlock()
                            edit.setPosition(pos - 1)
                            edit.setPosition(pos + 1, QTextCursor.KeepAnchor)
                            edit.removeSelectedText()
                            edit.endEditBlock()
                            edit.setPosition(pos - 1)
                            self.editor.setTextCursor(edit)
                            return True
                if self._handle_smart_backspace():
                    return True

        # Tab / Shift+Tab in lists/tables.
        elif event.key() == Qt.Key_Tab:
            if self._handle_smart_tab(is_shift=False):
                return True
            if self._handle_table_tab(is_shift=False):
                return True
        elif event.key() == Qt.Key_Backtab:
            if self._handle_smart_tab(is_shift=True):
                return True
            if self._handle_table_tab(is_shift=True):
                return True

        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return False

        key_text = event.text()
        if key_text in {'-', '+', '>', '#'} and self._format_full_lines(key_text + ' ', line_prefix=True):
            return True
        if key_text == ' ' and self._handle_multiline_space():
            return True
        if key_text == ' ' and not cursor.hasSelection():
            pos = cursor.positionInBlock()
            line = cursor.block().text()
            if pos and re.fullmatch(r'[ \t]*\*', line[:pos]) and line[pos:] == '*':
                cursor.deleteChar()
                cursor.insertText(' ')
                self.editor.setTextCursor(cursor)
                return True
        char_pairs = {
            '*': ('*', '*'),
            '_': ('_', '_'),
            '`': ('`', '`'),
            '~': ('~', '~'),
            '=': ('=', '='),
        }

        # Selected text: typing a Markdown delimiter wraps the selection instead
        # of replacing it.  Repeating the delimiter grows the pair (e.g. * ->
        # **), while the third backtick turns ``selection`` into a fenced block.
        if cursor.hasSelection() and key_text in char_pairs:
            if key_text == '`':
                doc = self.editor.document()
                start = cursor.selectionStart()
                end = cursor.selectionEnd()

                has_two_before = (start >= 2 and doc.characterAt(start - 1) == '`' and doc.characterAt(start - 2) == '`')
                has_two_after = (end + 1 < doc.characterCount() and doc.characterAt(end) == '`' and doc.characterAt(end + 1) == '`')

                if has_two_before and has_two_after:
                    sel_text = self.selected_source_text(cursor)
                    char_before = doc.characterAt(start - 3) if start >= 3 else '\n'
                    lead_nl = "" if (start == 2 or char_before == '\n') else "\n"
                    replacement = f"{lead_nl}```\n{sel_text}\n```"

                    edit_cursor = QTextCursor(doc)
                    edit_cursor.beginEditBlock()
                    edit_cursor.setPosition(start - 2)
                    edit_cursor.setPosition(end + 2, QTextCursor.KeepAnchor)
                    edit_cursor.insertText(replacement)
                    edit_cursor.endEditBlock()

                    new_start = start - 2 + len(lead_nl) + 4
                    new_end = new_start + len(sel_text.encode('utf-16-le')) // 2
                    final_cursor = self.editor.textCursor()
                    final_cursor.setPosition(new_start)
                    final_cursor.setPosition(new_end, QTextCursor.KeepAnchor)
                    self.editor.setTextCursor(final_cursor)
                    self.parse_code_blocks()
                    return True

            p, suffix = char_pairs[key_text]
            self.wrap_selection(p, suffix)
            return True

        # No selection: insert an auto-pair and leave the caret between it. If
        # the caret is already between a matching pair, repeating the delimiter
        # expands both sides.  Thus ** and ~~ are effortless, and typing a third
        # backtick converts ``|`` into a complete fenced block.
        if key_text in char_pairs and not cursor.hasSelection():
            from ui.tab_memory import performance_preferences
            if not performance_preferences.values['markdown']['auto_pairs']:
                return False
            doc = self.editor.document()
            pos = cursor.position()
            before_count = 0
            while pos - before_count - 1 >= 0 and doc.characterAt(pos - before_count - 1) == key_text:
                before_count += 1
            after_count = 0
            while pos + after_count < doc.characterCount() and doc.characterAt(pos + after_count) == key_text:
                after_count += 1

            # Third backtick: make a real fenced block immediately.
            if key_text == '`' and before_count >= 2 and after_count >= 2:
                replace_start = pos - 2
                replace_end = pos + 2
                edit = QTextCursor(doc)
                edit.beginEditBlock()
                edit.setPosition(replace_start)
                edit.setPosition(replace_end, QTextCursor.KeepAnchor)
                edit.insertText("```\n\n```")
                edit.endEditBlock()
                edit.setPosition(replace_start + 4)  # after opening fence + newline
                self.editor.setTextCursor(edit)
                self.parse_code_blocks()
                return True

            # When text was entered inside an auto-pair, pressing the closing
            # delimiter should step over the already-present closing character
            # instead of creating a second pair.
            if before_count == 0 and after_count > 0:
                cursor.setPosition(pos + 1)
                self.editor.setTextCursor(cursor)
                return True

            cursor.beginEditBlock()
            cursor.insertText(key_text + key_text)
            cursor.endEditBlock()
            cursor.setPosition(pos + 1)
            self.editor.setTextCursor(cursor)
            return True

        return False

    def _handle_smart_enter(self) -> bool:
        cursor = self.editor.textCursor()
        block = cursor.block()
        line_text = block.text()
        pos_in_block = cursor.positionInBlock()

        # Block quote behaves like a flat list: Enter continues with `> `,
        # while Enter on an empty quote removes the marker and exits the quote.
        quote_prefix = line_text[:pos_in_block]
        quote_match = re.match(r"^\s*>\s?(.*)$", quote_prefix)
        if quote_match:
            whole_quote = re.match(r"^\s*>\s*$", line_text)
            content = quote_match.group(1).strip()

            if whole_quote and not content:
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                return True

            cursor.beginEditBlock()
            cursor.insertText("\n> ")
            cursor.endEditBlock()
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()
            return True

        unord_match = re.match(r"^(\s*)([-*+])(?!\*|-|\+)\s+(.*)$", line_text[:pos_in_block])
        if unord_match:
            indent = unord_match.group(1)
            marker = unord_match.group(2)
            content = unord_match.group(3).strip()

            if not content:
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                return True
            else:
                cursor.beginEditBlock()
                cursor.insertText(f"\n{indent}{marker} ")
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                self.editor.ensureCursorVisible()
                return True

        ord_match = re.match(r"^(\s*)(\d+)([\.\)])\s+(.*)$", line_text[:pos_in_block])
        if ord_match:
            indent = ord_match.group(1)
            num_str = ord_match.group(2)
            delim = ord_match.group(3)
            content = ord_match.group(4).strip()

            if not content:
                cursor.beginEditBlock()
                cursor.movePosition(QTextCursor.StartOfBlock)
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                return True
            else:
                next_num = int(num_str) + 1
                cursor.beginEditBlock()
                cursor.insertText(f"\n{indent}{next_num}{delim} ")
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
                self.editor.ensureCursorVisible()
                return True

        return False

    def _handle_smart_backspace(self) -> bool:
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            return False

        block = cursor.block()
        line_text = block.text()
        pos_in_block = cursor.positionInBlock()

        quote_match = re.match(r"^\s*>\s$", line_text[:pos_in_block])
        if quote_match:
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.StartOfBlock)
            cursor.setPosition(block.position() + pos_in_block, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.endEditBlock()
            self.editor.setTextCursor(cursor)
            return True

        unord_match = re.match(r"^(\s*)([-*+])(?!\*|-|\+)\s$", line_text[:pos_in_block])
        if unord_match:
            indent = unord_match.group(1)
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.StartOfBlock)
            cursor.setPosition(block.position() + pos_in_block, QTextCursor.KeepAnchor)
            if indent:
                cursor.insertText(re.sub(r'(?:\t| {1,4})$', '', indent) + unord_match.group(2) + " ")
            else:
                cursor.removeSelectedText()
            cursor.endEditBlock()
            self.editor.setTextCursor(cursor)
            return True

        ord_match = re.match(r"^(\s*)(\d+[\.\)]\s)$", line_text[:pos_in_block])
        if ord_match:
            indent = ord_match.group(1)
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.StartOfBlock)
            cursor.setPosition(block.position() + pos_in_block, QTextCursor.KeepAnchor)
            if indent:
                cursor.insertText(re.sub(r'(?:\t| {1,4})$', '', indent) + ord_match.group(2))
            else:
                cursor.removeSelectedText()
            cursor.endEditBlock()
            self.editor.setTextCursor(cursor)
            return True

        return False

    def _handle_smart_tab(self, is_shift: bool) -> bool:
        cursor = self.editor.textCursor()
        first = cursor.document().findBlock(cursor.selectionStart())
        last = cursor.document().findBlock(max(cursor.selectionStart(), cursor.selectionEnd() - 1)) if cursor.hasSelection() else first
        blocks = []
        block = first
        while block.isValid():
            blocks.append(block)
            if block == last:
                break
            block = block.next()
        if not any(re.match(r"^[ \t]*([*+-]|\d+[.)])\s+", block.text()) for block in blocks):
            return False
        cursor.beginEditBlock()
        for block in blocks:
            edit = QTextCursor(block)
            if not is_shift:
                edit.insertText('\t')
            else:
                text = block.text()
                count = 1 if text.startswith('\t') else min(4, len(text) - len(text.lstrip(' ')))
                if count:
                    edit.setPosition(block.position() + count, QTextCursor.KeepAnchor)
                    edit.removeSelectedText()
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        return True

    # =========================================================================
    # LIVE TABLE OBJECTS
    # =========================================================================

    def source_text(self) -> str:
        """Return Markdown source even while Live mode contains table objects."""
        # Bypass VSCodeEditor.toPlainText() to avoid recursion when the editor
        # delegates its public text representation back to this handler.
        raw = QPlainTextEdit.toPlainText(self.editor)
        if _OBJECT_REPLACEMENT not in raw and not (getattr(self,'images',None) and self.images.widgets):
            return raw

        cursor = QTextCursor(self.editor.document())
        cursor.select(QTextCursor.Document)
        return self.selected_source_text(cursor)

    def selected_source_text(self, cursor):
        """Expand selected objects for copy, formatting and agent context."""
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        text = cursor.selectedText().replace('\u2029', '\n')
        block = self.editor.document().findBlock(start)
        replacements = []
        from ui.views.inline_images import IMAGE_OBJECT, IMAGE_SOURCE, IMAGE_PADDING
        from ui.views.editor_code_blocks import CODE_OBJECT, CODE_SOURCE
        while block.isValid() and block.position() < end:
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                fmt = fragment.charFormat()
                source = fmt.property(_TABLE_SOURCE_PROPERTY) if fmt.objectType() == _TABLE_OBJECT_TYPE else fmt.property(IMAGE_SOURCE) if fmt.objectType() == IMAGE_OBJECT else fmt.property(CODE_SOURCE) if fmt.objectType() == CODE_OBJECT else None
                if fmt.property(IMAGE_PADDING):
                    lo=max(start,fragment.position());hi=min(end,fragment.position()+fragment.length())
                    for position in range(lo,hi):
                        if cursor.document().characterAt(position) == '\u2028':
                            replacements.append((position-start,''))
                if source:
                    position = fragment.position()
                    for char in fragment.text():
                        if char == _OBJECT_REPLACEMENT and start <= position < end:
                            replacements.append((position-start, str(source)))
                        position += 2 if ord(char) > 0xffff else 1
                iterator += 1
            block = block.next()
        # Offsets from QTextCursor count UTF-16 units, including emoji.
        data = text.encode('utf-16-le')
        for position, source in reversed(replacements):
            data = data[:position*2]+source.encode('utf-16-le')+data[(position+1)*2:]
        return data.decode('utf-16-le')

    def _table_object_position_in_block(self, block) -> Optional[int]:
        if not block.isValid() or block.text() != _OBJECT_REPLACEMENT:
            return None
        pos = block.position()
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(pos)
        cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        if cursor.charFormat().objectType() == _TABLE_OBJECT_TYPE:
            return pos
        return None

    def _table_object_source_at(self, pos: int) -> Optional[str]:
        doc = self.editor.document()
        if pos < 0 or pos >= doc.characterCount() - 1:
            return None
        if doc.characterAt(pos) != _OBJECT_REPLACEMENT:
            return None
        cursor = QTextCursor(doc)
        cursor.setPosition(pos)
        cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        fmt = cursor.charFormat()
        if fmt.objectType() != _TABLE_OBJECT_TYPE:
            return None
        source = fmt.property(_TABLE_SOURCE_PROPERTY)
        return str(source) if source else None

    def _restore_table_object_at(self, pos: int, *, place_cursor: bool = False) -> bool:
        source = self._table_object_source_at(pos)
        if source is None:
            return False

        doc = self.editor.document()
        was_modified = doc.isModified()
        self._syncing_table_objects = True
        try:
            cursor = QTextCursor(doc)
            cursor.setPosition(pos)
            cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
            cursor.insertText(source)
            if place_cursor:
                cursor.setPosition(pos)
                self.editor.setTextCursor(cursor)
            doc.setModified(was_modified)
        finally:
            self._syncing_table_objects = False

        self.editor.viewport().update()
        return True

    def _restore_all_table_objects(self):
        doc = self.editor.document()
        positions: List[int] = []
        block = doc.firstBlock()
        while block.isValid():
            pos = self._table_object_position_in_block(block)
            if pos is not None:
                positions.append(pos)
            block = block.next()

        if not positions:
            return

        was_modified = doc.isModified()
        self._syncing_table_objects = True
        try:
            # Work backwards so earlier positions remain stable.
            for pos in reversed(positions):
                source = self._table_object_source_at(pos)
                if source is None:
                    continue
                cursor = QTextCursor(doc)
                cursor.setPosition(pos)
                cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
                cursor.insertText(source)
            doc.setModified(was_modified)
        finally:
            self._syncing_table_objects = False
        self.editor.viewport().update()

    def _table_source_ranges(self):
        """Yield (start_pos, end_pos, source) for source-form GFM tables.

        The scanner tracks fenced code itself so it remains correct before
        code_block_ranges are rebuilt after a table object changed block count.
        """
        doc = self.editor.document()
        block = doc.firstBlock()
        in_code = False

        while block.isValid():
            text = block.text()
            stripped = text.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                block = block.next()
                continue
            if in_code or text == _OBJECT_REPLACEMENT:
                block = block.next()
                continue

            separator = block.next()
            if (
                "|" not in text
                or not separator.isValid()
                or not _TABLE_SEPARATOR_RE.match(separator.text())
            ):
                block = block.next()
                continue

            start_block = block
            end_block = separator
            probe = separator.next()
            while probe.isValid():
                probe_text = probe.text()
                if not probe_text.strip() or "|" not in probe_text:
                    break
                # Do not greedily absorb a new fenced block after a malformed
                # table row containing a pipe.
                if probe_text.strip().startswith("```"):
                    break
                end_block = probe
                probe = probe.next()

            start_pos = start_block.position()
            end_pos = end_block.position() + len(end_block.text())
            cursor = QTextCursor(doc)
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.KeepAnchor)
            source = cursor.selectedText().replace("\u2029", "\n")
            if _parse_table_source(source) is not None:
                yield start_pos, end_pos, source

            block = end_block.next()

    def _sync_table_objects(self) -> bool:
        """Replace inactive source tables with one custom QText object.

        Returns True when document block structure changed, so callers know
        they must rebuild code-block ranges against the transformed document.
        """
        if (
            not self.enabled
            or not self.visual_enabled
            or self._table_renderer is None
            or self._syncing_table_objects
        ):
            return False

        doc = self.editor.document()
        cursor = self.editor.textCursor()
        active_start = cursor.selectionStart()
        active_end = cursor.selectionEnd()
        if not cursor.hasSelection():
            active_end = active_start

        replacements = []
        for start_pos, end_pos, source in self._table_source_ranges():
            # While editing a table, keep its raw Markdown visible. Read-only
            # documents have no editable active table, so render all of them.
            if not self.editor.isReadOnly():
                if cursor.hasSelection():
                    if start_pos <= active_end and end_pos >= active_start:
                        continue
                elif start_pos <= active_start <= end_pos:
                    continue
            replacements.append((start_pos, end_pos, source))

        if not replacements:
            return False

        was_modified = doc.isModified()
        self._syncing_table_objects = True
        try:
            # Back-to-front replacement keeps all stored source positions valid.
            for start_pos, end_pos, source in reversed(replacements):
                edit = QTextCursor(doc)
                edit.setPosition(start_pos)
                edit.setPosition(end_pos, QTextCursor.KeepAnchor)
                fmt = QTextCharFormat()
                fmt.setObjectType(_TABLE_OBJECT_TYPE)
                fmt.setProperty(_TABLE_SOURCE_PROPERTY, source)
                edit.insertText(_OBJECT_REPLACEMENT, fmt)
            doc.setModified(was_modified)
        finally:
            self._syncing_table_objects = False

        self.editor.viewport().update()
        return True

    def _table_object_at_viewport_pos(self, pos: QPoint) -> Optional[int]:
        cursor = self.editor.cursorForPosition(pos)
        doc = self.editor.document()
        candidates = [cursor.position(), cursor.position() - 1]
        for candidate in candidates:
            if candidate < 0:
                continue
            if doc.characterAt(candidate) == _OBJECT_REPLACEMENT:
                if self._table_object_source_at(candidate) is not None:
                    return candidate
        return None

    def refresh_table_object_geometry(self):
        if not self.visual_enabled or self._table_renderer is None:
            return
        doc = self.editor.document()
        if _OBJECT_REPLACEMENT not in QPlainTextEdit.toPlainText(self.editor):
            return
        # intrinsicSize() depends on the viewport width. Marking contents dirty
        # makes QPlainTextDocumentLayout ask the object handler for a new size
        # after editor resizes.
        try:
            doc.markContentsDirty(0, max(0, doc.characterCount() - 1))
        except AttributeError:
            pass
        self.editor.viewport().update()

    # =========================================================================
    # ССЫЛКИ MARKDOWN
    # =========================================================================

    def get_url_at_pos(self, global_pos: QPoint) -> Optional[str]:
        if not self.enabled:
            return None
        viewport_pos = self.editor.viewport().mapFromGlobal(global_pos)
        if not self.editor.viewport().rect().contains(viewport_pos):
            return None
        cursor = self.editor.cursorForPosition(viewport_pos)
        block = cursor.block()
        if self.is_in_code_block(block.blockNumber()):
            return None
        geometry = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
        line = block.layout().lineForTextPosition(cursor.positionInBlock())
        if not line.isValid() or not line.naturalTextRect().translated(geometry.topLeft()).contains(QPointF(viewport_pos)):
            return None
        line_text = block.text()
        col = cursor.positionInBlock()

        for m in re.finditer(r"\[([^\]]+)\]\(([^\)]+)\)", line_text):
            if m.start() <= col <= m.end():
                return m.group(2).strip()

        for m in re.finditer(r"https?://[^\s\)]+", line_text):
            if m.start() <= col <= m.end():
                return m.group(0).strip()

        return None

    def handle_mouse_press(self, event: QMouseEvent) -> bool:
        if not self.enabled:
            return False

        if event.button() == Qt.LeftButton and self.visual_enabled:
            viewport_pos = event.position().toPoint()
            table_pos = self._table_object_at_viewport_pos(viewport_pos)
            if table_pos is not None:
                # One click turns the card back into its Markdown source for
                # editing.  Leaving the table renders it as a card again.
                if self._restore_table_object_at(table_pos, place_cursor=True):
                    self.editor.setFocus()
                    defer(0, self, self.parse_code_blocks)
                    return True

        if event.button() == Qt.LeftButton and (event.modifiers() & Qt.ControlModifier):
            url = self.get_url_at_pos(event.globalPosition().toPoint())
            if url:
                from ui.views.inline_images import open_reference, document_path
                open_reference(url, document_path(self.editor))
                return True
        return False

    # =========================================================================
    # ГЕОМЕТРИЯ ПЕРЕНОСОВ MARKDOWN (СПИСКИ / ЦИТАТЫ)
    # =========================================================================

    def _prefix_width_px(self, prefix: str) -> float:
        """Return the visual width of a Markdown prefix in editor pixels.

        Tabs are expanded using the editor's configured tab stop so nested
        list indentation keeps the same geometry as the source line.
        """
        fm = self.editor.fontMetrics()
        tab = max(1.0, float(self.editor.tabStopDistance()))
        x = 0.0
        chunk = []

        def flush_chunk():
            nonlocal x, chunk
            if chunk:
                x += float(fm.horizontalAdvance("".join(chunk)))
                chunk = []

        for ch in prefix:
            if ch == "\t":
                flush_chunk()
                x = (int(x // tab) + 1) * tab
            else:
                chunk.append(ch)
        flush_chunk()
        return x

    def _hanging_prefix(self, text: str) -> Optional[str]:
        """Return the source prefix whose width continuation lines reserve."""
        # Block quotes. Keep all indentation and separator whitespace.
        m = re.match(r"^(\s*>\s*)", text)
        if m:
            return m.group(1)

        # Bulleted lists. Do not reinterpret bold/italic Markdown as a list.
        m = re.match(r"^(\s*[-*+]\s+)", text)
        if m and not re.match(r"^\s*(\*\*|__)", text):
            return m.group(1)

        # Numbered lists: `1. foo` and `1) foo`.
        m = re.match(r"^(\s*\d+[\.\)]\s+)", text)
        if m:
            return m.group(1)

        return None

    def _sync_hanging_indents(self, force_clear: bool = False):
        """Cache desired hanging-indent widths without touching source text.

        QPlainTextDocumentLayout deliberately performs a very small plain-text
        layout and ignores QTextBlockFormat left/text indents.  The actual
        hanging geometry is therefore applied directly to QTextLayout lines in
        _apply_hanging_line_layouts().
        """
        if self._syncing_hanging_indents:
            return

        desired = {}
        if self.enabled and not force_clear:
            block = self.editor.document().firstBlock()
            while block.isValid():
                num = block.blockNumber()
                if not self.is_in_code_block(num):
                    prefix = self._hanging_prefix(block.text())
                    if prefix:
                        desired[num] = self._prefix_width_px(prefix)
                block = block.next()

        self._hanging_indent_widths = desired
        stale = self._custom_hanging_blocks - desired.keys()
        for num in stale:
            block = self.editor.document().findBlockByNumber(num)
            if block.isValid(): block.layout().clearLayout()
        self._custom_hanging_blocks.difference_update(stale)
        for num in stale: self._custom_hanging_refs.pop(num, None)

        if force_clear:
            self.editor.viewport().update()

    def _plain_layout_available_width(self) -> float:
        """Mirror QPlainTextDocumentLayout's usable line width."""
        doc = self.editor.document()
        margin = float(doc.documentMargin())
        width = float(self.editor.viewport().width()) - (2.0 * margin)

        option = doc.defaultTextOption()
        try:
            if option.flags() & QTextOption.AddSpaceForLineAndParagraphSeparators:
                width -= float(self.editor.fontMetrics().horizontalAdvance('↵'))
        except AttributeError:
            # Enum spelling differs across a few Qt/PySide releases.  This
            # flag is normally disabled, so falling back is harmless.
            pass

        return max(1.0, width)

    def _layout_block_with_hanging_indent(self, block, indent_px: float):
        """Re-layout one QPlainTextEdit block with a true hanging indent.

        The first visual line keeps the normal full width and x-position.
        Every continuation line starts at `indent_px` and loses the same
        amount of usable width, so nothing is clipped at the right edge.
        """
        layout = block.layout()
        doc = self.editor.document()
        margin = float(doc.documentMargin())
        full_width = self._plain_layout_available_width()
        continuation_width = max(1.0, full_width - float(indent_px))

        # If Qt has already produced exactly this custom layout, keep it.
        # This makes paint events cheap while still detecting relayouts after
        # resize/text changes (Qt recreates them with every line at `margin`).
        if layout.lineCount() > 1:
            first = layout.lineAt(0)
            second = layout.lineAt(1)
            if (
                abs(first.x() - margin) <= 0.1
                and abs(first.width() - full_width) <= 0.5
                and abs(second.x() - (margin + indent_px)) <= 0.1
                and abs(second.width() - continuation_width) <= 0.5
            ):
                return
        elif layout.lineCount() == 1:
            # A single line needs no visible continuation indent.  Leave the
            # native layout alone; if it later wraps, the check above fails
            # and we rebuild it.
            return

        option = QTextOption(doc.defaultTextOption())

        # WrapAtWordBoundaryOrAnywhere still prefers moving a single giant
        # token wholesale to the continuation line after `> ` / `- `.  If the
        # first token cannot fit in the content width at all, force Anywhere
        # for this one block so the first visual line keeps part of the word.
        prefix = self._hanging_prefix(block.text()) or ""
        content = block.text()[len(prefix):]
        first_token = re.match(r"\S+", content)
        if first_token:
            token_w = float(self.editor.fontMetrics().horizontalAdvance(first_token.group(0)))
            if token_w > continuation_width:
                option.setWrapMode(QTextOption.WrapAnywhere)

        layout.clearLayout()
        layout.setTextOption(option)
        layout.beginLayout()
        try:
            y = 0.0
            line_index = 0
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break

                line.setLeadingIncluded(True)
                if line_index == 0:
                    line.setLineWidth(full_width)
                    line.setPosition(QPointF(margin, y))
                else:
                    line.setLineWidth(continuation_width)
                    line.setPosition(QPointF(margin + indent_px, y))

                y += line.height()
                if line.leading() < 0:
                    y += math.ceil(line.leading())
                line_index += 1
        finally:
            layout.endLayout()

        # QPlainTextEdit's vertical bookkeeping is based on QTextBlock's line
        # count.  Keep it synchronized with our rebuilt QTextLayout.
        block.setLineCount(layout.lineCount() if block.isVisible() else 0)

    def _apply_hanging_line_layouts(self):
        """Apply/reset custom wrapping geometry before QPlainTextEdit paints."""
        if not self.enabled:
            return

        doc = self.editor.document()
        desired = self._hanging_indent_widths

        for block in self._visible_blocks():
            num = block.blockNumber()
            indent_px = desired.get(num)
            if indent_px is None: continue

            # Force Qt to ensure a native layout first.  blockBoundingRect()
            # does this lazily and gives us a valid QTextLayout to replace.
            self.editor.blockBoundingRect(block)
            self._layout_block_with_hanging_indent(block, indent_px)
            if block.layout().lineCount() > 1:
                self._custom_hanging_blocks.add(num)
                self._custom_hanging_refs[num] = block
            else:
                self._custom_hanging_blocks.discard(num)
                self._custom_hanging_refs.pop(num, None)

    # =========================================================================
    # ОТРИСОВКА БЛОКОВ КОДА И ГОРИЗОНТАЛЬНЫХ ЛИНИЙ (ТОЛЬКО ДЛЯ MARKDOWN)
    # =========================================================================

    def parse_code_blocks(self):
        self._layout_refresh_timer.stop()
        if not self.enabled:
            self.code_block_ranges.clear()
            return

        # Render inactive pipe tables first. Replacing several source blocks by
        # one inline object changes block numbers, so code ranges must always be
        # scanned *after* this step.
        if self.visual_enabled:
            self._sync_table_objects()

        key = (self._source_revision, self.editor.font().toString(),
               self.editor.tabStopDistance())
        if key == self._parsed_key:
            return

        doc = self.editor.document()
        from ui.views.chat_markdown import fenced_blocks
        # QTextDocument block numbers exclude soft line separators used to
        # reserve image height. toPlainText() turns those separators into LF,
        # shifting every fence below an image away from its real block.
        lines = []
        block = doc.firstBlock()
        while block.isValid():
            lines.append(block.text().replace('\u2028', ' '))
            block = block.next()
        fences = fenced_blocks('\n'.join(lines))
        ranges = [(block.start, block.end) for block in fences]

        self.code_block_ranges = ranges
        self._code_starts = [start for start, _end in ranges]
        self._code_ends = [end for _start, end in ranges]
        self._code_closings = {block.end for block in fences if block.closed}
        self._code_languages = {block.start: block.language for block in fences}
        self._sync_hanging_indents()
        # Drop layouts that left a fence. Stable block handles above preserve
        # their identities when edits shift block numbers.
        stale = {num for num in self._code_inset_blocks
                 if not self.is_in_code_block(num) or num in self._code_closings}
        for num in stale:
            block = doc.findBlockByNumber(num)
            if block.isValid(): block.layout().clearLayout()
        self._code_inset_blocks.difference_update(stale)
        for num in stale: self._code_inset_refs.pop(num, None)
        self._parsed_key = key
        if self.visual_enabled:
            self._visual_refresh_timer.start()
        elif self._code_inset_blocks:
            self._apply_code_body_insets(False)

    def _apply_code_body_insets(self, enabled: Optional[bool] = None):
        """Visually inset the visible contents of fenced code blocks.

        This includes the opening fence line because in Live mode its fence
        markers are hidden while the optional language/header text remains
        visible.  The closing fence stays at its normal position.  Moving the
        already-laid-out QTextLine objects is visual-only: it changes neither
        the Markdown source nor the undo history.
        """
        use_inset = self.visual_enabled if enabled is None else bool(enabled)
        doc = self.editor.document()
        margin = float(doc.documentMargin())

        blocks = (list(self._visible_blocks()) if use_inset else
                  [doc.findBlockByNumber(num) for num in self._code_inset_blocks])
        for block in blocks:
            if not block.isValid(): continue
            block_num = block.blockNumber()
            in_code = (use_inset and self.is_in_code_block(block_num)
                       and block_num not in self._code_closings)

            # Ensure QPlainTextDocumentLayout has created QTextLine objects.
            self.editor.blockBoundingRect(block)
            layout = block.layout()
            if not in_code and block_num not in self._code_inset_blocks: continue
            target_x = margin + (self.code_body_inset_px if in_code else 0.0)

            for idx in range(layout.lineCount()):
                line = layout.lineAt(idx)
                pos = line.position()
                if abs(pos.x() - target_x) > 0.1:
                    line.setPosition(QPointF(target_x, pos.y()))

            if in_code:
                self._code_inset_blocks.add(block_num)
                self._code_inset_refs[block_num] = block
            else:
                self._code_inset_blocks.discard(block_num)
                self._code_inset_refs.pop(block_num, None)
        if not use_inset:
            self._code_inset_blocks.clear()
            self._code_inset_refs.clear()

    def _code_text(self, start_num, end_num):
        lines = []
        block = self.editor.document().findBlockByNumber(start_num).next()
        boundary = end_num if end_num in self._code_closings else end_num + 1
        while block.isValid() and block.blockNumber() < boundary:
            lines.append(block.text())
            block = block.next()
        return '\n'.join(lines)

    def _present_code(self, button, *, folded=False, rendered=False):
        # The viewport refresh is deferred. Its buttons can still be clicked
        # immediately after text was inserted above a fence; follow a cursor,
        # never the former line number stored when that button was painted.
        self.parse_code_blocks()
        start = button.code_anchor.blockNumber()
        target = next(((a, b) for a, b in self.code_block_ranges if a == start), None)
        if target is not None:
            self.code_objects.convert(*target, folded=folded, rendered=rendered)

    def position_copy_buttons(self):
        if not self.enabled or not self.visual_enabled:
            for btn in [*self.copy_buttons, *self.render_buttons, *self.fold_buttons]:
                btn.hide()
            return

        btn_idx = 0
        doc = self.editor.document()
        viewport_w = self.editor.viewport().width()

        total_w = min(viewport_w - 12.0, viewport_w * (1.0 / 1.0))
        left_x = 2.0
        right_x = left_x + total_w

        for start_num, end_num in self._visible_code_ranges(opening_only=True):
            start_b = doc.findBlockByNumber(start_num)
            if not start_b.isValid():
                continue

            rect = self.editor.blockBoundingGeometry(start_b).translated(self.editor.contentOffset())
            if rect.bottom() < 0 or rect.top() > self.editor.viewport().height():
                continue

            if btn_idx >= len(self.copy_buttons):
                new_btn = CodeBlockCopyButton(self.editor.viewport())
                self.copy_buttons.append(new_btn)
                render = QToolButton(self.editor.viewport())
                render.setObjectName('CodeBlockCopyBtn')
                set_text(render, 'ui.code_block.render_76ba')
                set_text(render, 'ui.markdown_handler.render_svg_9b58', setter='setToolTip')
                render.setCursor(Qt.PointingHandCursor)
                render.clicked.connect(lambda checked=False, b=render: self._present_code(b, rendered=True))
                self.render_buttons.append(render)
                fold = QToolButton(self.editor.viewport())
                fold.setObjectName('CodeBlockCopyBtn')
                set_icon(fold, 'down', square=True)
                set_text(fold, 'ui.code_block.collapse_code_block_7205', setter='setToolTip')
                fold.setCursor(Qt.PointingHandCursor)
                fold.clicked.connect(lambda checked=False, b=fold: self._present_code(b, folded=True))
                self.fold_buttons.append(fold)

            btn = self.copy_buttons[btn_idx]
            render, fold = self.render_buttons[btn_idx], self.fold_buttons[btn_idx]
            render.code_anchor = QTextCursor(start_b)
            fold.code_anchor = QTextCursor(start_b)
            btn.code_text_getter = lambda start=start_num, end=end_num: self._code_text(start, end)

            btn_size = max(16, min(28, int(rect.height() * 0.75)))
            btn.setFixedSize(btn_size, btn_size)

            btn_y = int(rect.top() + (rect.height() - btn_size) / 2.0)
            fold.setFixedSize(btn_size, btn_size)
            fold.move(max(0, int(right_x - btn_size - 8)), max(0, btn_y))
            fold.show()
            fold.raise_()
            btn_x = int(right_x - 2 * btn_size - 12)
            btn.move(max(0, btn_x), max(0, btn_y))
            btn.show()
            btn.raise_()
            render.setFixedSize(max(60, render.fontMetrics().horizontalAdvance(render.text()) + 12), btn_size)
            render.move(max(0, btn_x - render.width() - 4), max(0, btn_y))
            render.setVisible(self._code_languages.get(start_num, '').casefold() == 'svg')
            render.raise_()
            btn_idx += 1

        for i in range(btn_idx, len(self.copy_buttons)):
            self.copy_buttons[i].hide()
            self.render_buttons[i].hide()
            self.fold_buttons[i].hide()

    def paint_markdown_elements(self, painter: QPainter):
        if not self.enabled or not self.visual_enabled:
            return

        # QPlainTextEdit recreates QTextLine geometry after edits/resizes.
        # Re-apply true hanging indents first, then the independent fenced-code
        # inset, immediately before the base editor paints its text.
        self._apply_hanging_line_layouts()
        self._apply_code_body_insets()
        from .text_alignment import apply_visible
        apply_visible(self.editor)

        doc = self.editor.document()
        fm = self.editor.fontMetrics()
        corner_r = max(4.0, fm.height() * 0.28)
        viewport_w = self.editor.viewport().width()

        # 1. Отрисовка карточек кода
        total_w = min(viewport_w - 8.0, viewport_w * (1.0 / 1.0))
        left_x = 4.0
        right_x = left_x + total_w

        for start_num, end_num in self._visible_code_ranges():
            start_b = doc.findBlockByNumber(start_num)
            end_b = doc.findBlockByNumber(end_num)
            if not start_b.isValid() or not end_b.isValid():
                continue

            start_geom = self.editor.blockBoundingGeometry(start_b).translated(self.editor.contentOffset())
            end_geom = self.editor.blockBoundingGeometry(end_b).translated(self.editor.contentOffset())

            top_y = start_geom.top() + 1
            bot_y = end_geom.bottom() - 1

            if bot_y < 0 or top_y > self.editor.viewport().height():
                continue

            box_rect = QRectF(left_x, top_y, total_w, max(0.0, bot_y - top_y))

            # Match the shared chat/code-block card: dark body, a distinct
            # header strip for the opening fence/language line, and the same
            # subtle separator/border colors used by ChatCodeBlockWidget.
            painter.setBrush(QBrush(theme_manager.color('background.input')))
            painter.setPen(QPen(theme_manager.color('background.panel'), 1))
            painter.drawRoundedRect(box_rect, corner_r, corner_r)

            # The opening fence block is the visual header row. Clip the fill
            # to the card path so the top corners stay rounded.
            header_bottom = min(box_rect.bottom(), start_geom.bottom())
            if header_bottom > box_rect.top():
                card_path = QPainterPath()
                card_path.addRoundedRect(box_rect, corner_r, corner_r)

                painter.save()
                painter.setClipPath(card_path)
                header_rect = QRectF(
                    box_rect.left(),
                    box_rect.top(),
                    box_rect.width(),
                    header_bottom - box_rect.top(),
                )
                painter.fillRect(header_rect, theme_manager.color('background.surface'))
                painter.restore()

                painter.setPen(QPen(QColor("#222225"), 1))
                painter.drawLine(
                    QPointF(box_rect.left(), header_bottom),
                    QPointF(box_rect.right(), header_bottom),
                )

            # Repaint the outer edge last so the header fill cannot cover it.
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(theme_manager.color('background.panel'), 1))
            painter.drawRoundedRect(box_rect, corner_r, corner_r)

    def paint_markdown_markers(self, painter: QPainter):
        if not self.enabled or not self.visual_enabled:
            return
        fm = self.editor.fontMetrics()
        viewport_w = self.editor.viewport().width()

        def active_source(block):
            cursor = self.editor.textCursor()
            if cursor.hasSelection():
                if block.position() < cursor.selectionEnd() and block.position() + block.length() > cursor.selectionStart():
                    return True
            # Native highlighting can run before its queued active-state sync,
            # especially after edits that move blocks. Paint from the actual
            # applied format, not the cached block number: never draw a second
            # marker while the source glyph is already visible.
            marker = len(block.text())-len(block.text().lstrip())
            for span in reversed(block.layout().formats()):
                if span.start <= marker < span.start+span.length:
                    brush = span.format.foreground()
                    return brush.style() == Qt.NoBrush or brush.color().alpha() != 0
            return True

        block = self.editor.firstVisibleBlock()
        while block.isValid():
            b_num = block.blockNumber()

            if self.is_in_code_block(b_num):
                block = block.next()
                continue

            text = block.text()
            stripped = text.strip()

            block_geom = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
            if block_geom.bottom() < 0 or block_geom.top() > self.editor.viewport().height():
                block = block.next()
                continue

            center_y = int(block_geom.top() + block_geom.height() / 2.0)

            # Горизонтальный разделитель
            if re.match(r"^(---|___|\*\*\*)$", stripped):
                painter.setPen(QPen(theme_manager.color('background.surface'), 1))
                painter.drawLine(16, center_y, viewport_w - 16, center_y)

            # Markdown table separator rows (e.g. | --- | --- |) stay as text.
            # They must not be painted as a full-width horizontal rule.
            elif re.match(r"^\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?$", stripped):
                pass

            # Цитаты (>)
            elif re.match(r"^\s*>", text):
                if not active_source(block):
                    layout = block.layout()
                    char_pos = 0.0
                    if layout and layout.lineCount() > 0:
                        l0 = layout.lineForTextPosition(0)
                        if l0.isValid():
                            char_pos = l0.cursorToX(0)[0]
                    x = int(block_geom.left() + char_pos + fm.horizontalAdvance('>') / 2.0)
                    painter.setPen(QPen(theme_manager.color('accent.primary'), 3, Qt.SolidLine, Qt.RoundCap))
                    painter.drawLine(QPointF(x, block_geom.top() + 2), QPointF(x, block_geom.bottom() - 2))

            # Списки маркированные (-, +, *)
            elif re.match(r"^(\s*)([-*+])(\s+)(.*)$", text) and not re.match(r"^\s*(\*\*|__)", text):
                list_match = re.match(r"^(\s*)([-*+])(\s+)(.*)$", text)
                if list_match and not active_source(block):
                    char_idx = len(list_match.group(1))
                    layout = block.layout()
                    indent_x = self._prefix_width_px(list_match.group(1))

                    if layout and layout.lineCount() > 0:
                        line = layout.lineForTextPosition(char_idx)
                        if line.isValid():
                            line_rect = line.naturalTextRect()
                            x_marker_end = line.cursorToX(char_idx + 1)[0]
                            x = int(block_geom.left() + x_marker_end - 6)
                            first_line_center_y = int(block_geom.top() + line_rect.top() + line_rect.height() / 2.0)
                        else:
                            x = int(block_geom.left() + indent_x + 8)
                            first_line_center_y = int(block_geom.top() + fm.height() / 2.0)
                    else:
                        x = int(block_geom.left() + indent_x + 8)
                        first_line_center_y = int(block_geom.top() + fm.height() / 2.0)

                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QBrush(theme_manager.color('accent.primary')))
                    painter.drawEllipse(QPointF(x, first_line_center_y), 2.0, 2.0)

            # Списки нумерованные (1., 2.)
            elif re.match(r"^(\s*)(\d+[\.\)])(\s+)(.*)$", text):
                num_match = re.match(r"^(\s*)(\d+[\.\)])(\s+)(.*)$", text)
                if num_match and not active_source(block):
                    num_str = num_match.group(2)
                    char_idx = len(num_match.group(1))
                    num_w = fm.horizontalAdvance(num_str)
                    indent_x = self._prefix_width_px(num_match.group(1))
                    layout = block.layout()

                    if layout and layout.lineCount() > 0:
                        line = layout.lineForTextPosition(char_idx)
                        if line.isValid():
                            line_rect = line.naturalTextRect()
                            x_num_end = line.cursorToX(char_idx + len(num_str))[0]
                            x = int(block_geom.left() + x_num_end - num_w - 4)
                            first_line_top = int(block_geom.top() + line_rect.top())
                            first_line_h = int(line_rect.height())
                        else:
                            x = int(block_geom.left() + indent_x + 2)
                            first_line_top = int(block_geom.top())
                            first_line_h = int(fm.height())
                    else:
                        x = int(block_geom.left() + indent_x + 2)
                        first_line_top = int(block_geom.top())
                        first_line_h = int(fm.height())

                    painter.setPen(QPen(theme_manager.color('accent.primary')))
                    painter.setFont(self.editor.font())
                    painter.drawText(
                        QRect(x, first_line_top, num_w + 4, first_line_h),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        num_str
                    )

            block = block.next()
