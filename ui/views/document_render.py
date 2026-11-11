"""Cooperative Markdown widget construction; the full source is never paginated."""
from collections import deque
from time import perf_counter

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtGui import QTextCursor, QTextBlockFormat

from .markdown_render import MarkdownRenderView, _split_render_parts


class DocumentRenderView(MarkdownRenderView):
    part_ready = Signal(int, int, object)
    layout_ready = Signal()
    loading_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent, track_headings=True, vertical_scrollbar=False)
        self.sections = []
        self.parts = []
        self._pending_parts = deque()
        self.loading = False
        self._build_timer = QTimer(self)
        self._build_timer.setSingleShot(True)
        self._build_timer.timeout.connect(self._render_some)

    def set_markdown(self, markdown, **options):
        self._build_timer.stop()
        self._pending_parts.clear()
        self.sections.clear()
        super().set_markdown('', **options)
        self._markdown = markdown
        parts = _split_render_parts(markdown, with_source_lines=True)
        self.parts = parts
        line_count = markdown.count('\n') + 1
        for index, part in enumerate(parts):
            end = parts[index + 1][2] if index + 1 < len(parts) else line_count
            self._pending_parts.append((part, max(part[2] + 1, end)))
        self.loading = bool(self._pending_parts)
        self.loading_changed.emit(self.loading)
        if self.loading and self.isVisible():
            self._build_timer.start(1)

    def _render_some(self):
        deadline = perf_counter() + .006
        count = 0
        while self._pending_parts:
            part, end = self._pending_parts.popleft()
            widget = self._append_part(part)
            self.sections.append((part[2], end, widget))
            self.part_ready.emit(part[2], end, widget)
            count += 1
            if count >= 3 or perf_counter() >= deadline:
                break
        self._refresh_chunk_heights()
        self.layout_ready.emit()
        if self._pending_parts:
            if self.isVisible():
                self._build_timer.start(1)
        else:
            self.loading = False
            self.loading_changed.emit(False)

    def _append_part(self, part):
        widget = super()._append_part(part)
        alignments = getattr(self, 'line_alignments', {})
        flags = {'left': Qt.AlignLeft, 'center': Qt.AlignHCenter, 'right': Qt.AlignRight}
        if part[0] == 'table':
            # Documentation can use the full reading column for wrapped cells.
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            self._layout.setAlignment(widget, Qt.AlignmentFlag(0))
            grid = widget.layout()
            for row in range(grid.rowCount()):
                source = part[2] + (0 if row == 0 else row + 1)
                alignment = flags.get(alignments.get(source))
                if alignment is not None:
                    for column in range(grid.columnCount()):
                        cell = grid.itemAtPosition(row, column)
                        if cell is not None:
                            cell.widget().setAlignment(alignment | Qt.AlignVCenter)
        elif part[0] == 'code':
            block = widget.code_edit.document().firstBlock()
            while block.isValid():
                alignment = flags.get(alignments.get(part[2] + 1 + block.blockNumber()))
                if alignment is not None:
                    cursor = QTextCursor(block)
                    fmt = QTextBlockFormat()
                    fmt.setAlignment(alignment)
                    cursor.mergeBlockFormat(fmt)
                block = block.next()
        elif part[0] == 'image':
            alignment = flags.get(alignments.get(part[2]))
            if alignment is not None:
                self._layout.setAlignment(widget, alignment)
        return widget

    def showEvent(self, event):
        super().showEvent(event)
        if self._pending_parts:
            self._build_timer.start(1)

    def hideEvent(self, event):
        self._build_timer.stop()
        self._height_timer.stop()
        super().hideEvent(event)
