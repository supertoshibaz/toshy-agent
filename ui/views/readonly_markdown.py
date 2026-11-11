"""Board-style documentation, cooperative rendering and source-based navigation."""
from ui.styles.binding import apply_style
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QTextEdit, QWidget

from .document_render import DocumentRenderView


class ReadOnlyMarkdownEditor(QWidget):
    loading_changed = Signal(bool)

    def __init__(self, parent=None, *, context_menu_handler=None):
        super().__init__(parent)
        self._context_menu_handler = context_menu_handler
        from .file_view import MarkdownOutlineRail, EDITOR_LINE_NUMBER_GAP
        from .minimap import CodeMinimap
        self._source = None
        self._source_document = QTextDocument(self)
        self._heading_targets = {}
        self._heading_entries = []
        self._heading_flash_block = None
        self._highlighted_chunk = None
        self._pending_heading = None
        self._pending_row = None
        self._heading_flash_timer = QTimer(self)
        self._heading_flash_timer.setSingleShot(True)
        self._heading_flash_timer.setInterval(900)
        self._heading_flash_timer.timeout.connect(self._clear_heading_flash)
        self.setObjectName('ReadOnlyMarkdownEditor')
        self.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(self, 'QWidget#ReadOnlyMarkdownEditor {background:@background.window~+0.025@;}')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(EDITOR_LINE_NUMBER_GAP)
        self.outline_rail = MarkdownOutlineRail(self)
        self.outline_rail.setFixedWidth(CodeMinimap.WIDTH)
        layout.addWidget(self.outline_rail)
        self.renderer = DocumentRenderView(self)
        self.renderer.setDocumentMargin(8)
        self.renderer.setStyleSheet(self.renderer.styleSheet() + 'QTextBrowser {font-size:14px;}')
        self.renderer.setContextMenuPolicy(Qt.CustomContextMenu)
        self.renderer.customContextMenuRequested.connect(self._show_context_menu)
        self.renderer.part_ready.connect(self._part_ready)
        self.renderer.layout_ready.connect(self._layout_ready)
        self.renderer.loading_changed.connect(self.loading_changed.emit)
        layout.addWidget(self.renderer, 1)
        self.minimap = CodeMinimap(self)
        layout.addWidget(self.minimap)
        self.renderer.verticalScrollBar().valueChanged.connect(self.minimap.update)
        self.outline_rail.set_enabled(True)
        if context_menu_handler is None:
            from PySide6.QtGui import QShortcut, QKeySequence
            self._find_shortcut = QShortcut(QKeySequence.Find, self)
            self._find_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            self._find_shortcut.activated.connect(self.open_search)

    def open_search(self, pos=None):
        if not hasattr(self, '_search_controller'):
            from ui.widgets.readonly_search import ReadOnlySearch
            self._search_controller = ReadOnlySearch(self)
        self._search_controller.open(pos)

    def document(self):
        return self._source_document

    def viewport(self):
        return self.renderer.viewport()

    def verticalScrollBar(self):
        return self.renderer.verticalScrollBar()

    def lineWrapMode(self):
        # One source line per miniature row; rendering never rewrites the source.
        return QPlainTextEdit.NoWrap

    def tabStopDistance(self):
        return self.fontMetrics().horizontalAdvance(' ') * 4

    def isReadOnly(self):
        return True

    def toPlainText(self):
        return self._source or ''

    def set_markdown(self, content, *, base_path=None, line_alignments=None):
        source = str(content or '').replace('\r\n', '\n').replace('\r', '\n')
        alignments = dict(line_alignments or {})
        if source == self._source and base_path == self.renderer.base_path and alignments == getattr(self.renderer, 'line_alignments', {}):
            return
        self.renderer.line_alignments = alignments
        self._clear_heading_flash()
        self._pending_heading = self._pending_row = None
        self._heading_targets.clear()
        self._heading_entries.clear()
        self._source = source
        self._source_document.setPlainText(source)
        self.minimap.invalidate_document()
        self.renderer.base_path = base_path
        self.renderer._last_context_widget = None
        self.renderer.set_markdown(source)
        for kind, payload, start in self.renderer.parts:
            if kind == 'text':
                for index, line in enumerate(payload.splitlines()):
                    match = re.match(r'^(#{1,5})(?!#)\s+\S', line)
                    if match:
                        self._heading_entries.append((start + index, len(match[1])))
        self._source_document.setModified(False)
        self.outline_rail.refresh_headings()
        self.renderer.verticalScrollBar().setValue(0)
        self.minimap.update()

    def _part_ready(self, start, end, widget):
        if not hasattr(widget, 'document'):
            return
        block = widget.document().firstBlock()
        while block.isValid():
            fragment = block.begin()
            while not fragment.atEnd():
                for name in fragment.fragment().charFormat().anchorNames():
                    match = re.fullmatch(r'toshy-heading-(\d+)', name)
                    if match:
                        self._heading_targets[int(match[1])] = (widget, block)
                fragment += 1
            block = block.next()

    def _layout_ready(self):
        self.minimap.update()
        if self._pending_heading in self._heading_targets:
            line, self._pending_heading = self._pending_heading, None
            self.navigate_to_heading(line)
        if self._pending_row is not None:
            row, center = self._pending_row
            self.minimap_scroll_to_row(row, center=center)

    def heading_entries(self):
        return self._heading_entries

    def navigate_to_heading(self, source_line):
        source_line = int(source_line)
        target = self._heading_targets.get(source_line)
        if target is None:
            if self.renderer.loading:
                self._pending_heading = source_line
            return
        self._pending_heading = None
        self._clear_heading_flash()
        chunk, block = target
        cursor = QTextCursor(block)
        chunk.setTextCursor(cursor)
        position = chunk.viewport().mapTo(self.renderer._content, chunk.cursorRect(cursor).topLeft())
        self.verticalScrollBar().setValue(position.y() - self.viewport().height() // 2)
        chunk.setFocus()
        self._heading_flash_block = source_line
        flash = QTextEdit.ExtraSelection()
        flash.cursor = QTextCursor(block)
        flash.format.setProperty(QTextFormat.FullWidthSelection, True)
        flash.format.setBackground(QColor(56, 189, 248, 72))
        chunk.setExtraSelections([flash])
        self._highlighted_chunk = chunk
        self._heading_flash_timer.start()

    def _clear_heading_flash(self):
        self._heading_flash_timer.stop()
        self._heading_flash_block = None
        if self._highlighted_chunk is not None:
            self._highlighted_chunk.setExtraSelections([])
            self._highlighted_chunk = None

    def _line_at_y(self, y):
        for start, end, widget in self.renderer.sections:
            if y < widget.y() + widget.height():
                fraction = max(0.0, min(1.0, (y - widget.y()) / max(1, widget.height())))
                return start + fraction * (end - start)
        return float(self.renderer.sections[-1][1]) if self.renderer.sections else 0.0

    def minimap_viewport_rows(self):
        top = self.verticalScrollBar().value()
        first = self._line_at_y(top)
        last = self._line_at_y(top + self.viewport().height())
        return first, max(1.0, last - first)

    def minimap_scroll_to_row(self, row, *, center=True):
        for start, end, widget in self.renderer.sections:
            if start <= row < end:
                self._pending_row = None
                fraction = (row - start) / max(1, end - start)
                y = widget.y() + fraction * widget.height()
                self.verticalScrollBar().setValue(round(y - (self.viewport().height() / 2 if center else 0)))
                return
        self._pending_row = (row, center) if self.renderer.loading else None

    def wheelEvent(self, event):
        self.renderer.wheelEvent(event)

    def _show_context_menu(self, position):
        if self._context_menu_handler is not None:
            self._context_menu_handler(self.renderer, position)
            return
        menu = self.renderer.createStandardContextMenu()
        menu.exec(self.renderer.mapToGlobal(position))
        menu.deleteLater()
