"""Use the editor's XOR selection for fragments of one replied-to message."""
from ui.themes.manager import theme_manager
from ui.styles.binding import watch_paint
from ui.i18n import tr
import uuid
from weakref import WeakValueDictionary

from shiboken6 import isValid
from PySide6.QtCore import QObject, Qt, QRectF
from PySide6.QtGui import QTextDocument, QTextCursor, QPainter, QColor
from PySide6.QtWidgets import QLabel, QTextEdit

from ui.views.quote_input import utf16_slice
from ui.views.text_selection import xor_intervals


class QuoteSelectableLabel(QLabel):
    """Keep QLabel's native text/links; paint persistent fragment backgrounds."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        watch_paint(self)
        self.quote_ranges = []
        self._quote_layout_key = None
        self._quote_document = None
        self._quote_rects_key = None
        self._quote_rects = []

    def set_quote_ranges(self, ranges):
        if self.quote_ranges != ranges:
            self.quote_ranges = list(ranges)
            self.update()

    def quote_document(self):
        key = (self.text(), self.font().toString(), self.contentsRect().width())
        if key != self._quote_layout_key:
            document = QTextDocument()
            document.setDocumentMargin(0)
            document.setDefaultFont(self.font())
            document.setHtml(self.text())
            document.setTextWidth(max(1, self.contentsRect().width()))
            document.size()  # Materialize Qt's line positions once per source/width.
            self._quote_document, self._quote_layout_key = document, key
        return self._quote_document

    def paintEvent(self, event):
        if self.quote_ranges:
            document = self.quote_document()
            key = (self._quote_layout_key, tuple(self.quote_ranges))
            if self._quote_rects_key != key:
                rects = []
                for start, end in self.quote_ranges:
                    block = document.findBlock(start)
                    while block.isValid() and block.position() < end:
                        layout = block.layout()
                        origin = document.documentLayout().blockBoundingRect(block).topLeft()
                        lo = max(0, start - block.position())
                        hi = min(block.length() - 1, end - block.position())
                        first = layout.lineForTextPosition(lo)
                        last = layout.lineForTextPosition(max(lo, hi - 1))
                        if first.isValid() and last.isValid() and hi > lo:
                            for index in range(first.lineNumber(), last.lineNumber() + 1):
                                line = layout.lineAt(index)
                                a = max(lo, line.textStart())
                                b = min(hi, line.textStart() + line.textLength())
                                x1, x2 = line.cursorToX(a)[0], line.cursorToX(b)[0]
                                rects.append(QRectF(origin.x() + min(x1, x2), origin.y() + line.y(),
                                                    max(1, abs(x2 - x1)), line.height()))
                        block = block.next()
                self._quote_rects, self._quote_rects_key = rects, key
            painter = QPainter(self)
            painter.setPen(Qt.NoPen)
            tint = theme_manager.color('accent.primary')
            tint.setAlpha(75)
            painter.setBrush(tint)
            painter.translate(self.contentsRect().topLeft())
            for rect in self._quote_rects:
                if rect.intersects(QRectF(event.rect())):
                    painter.drawRect(rect)
            painter.end()
        super().paintEvent(event)


class QuoteSelectionSession(QObject):
    def __init__(self, references):
        super().__init__(references)
        self.references = references
        self.input = references.chat.input_edit
        self.sources = WeakValueDictionary()
        self._syncing = False
        self.input.quotes_changed.connect(self.sync_highlights)
        theme_manager.theme_changed.connect(self.sync_highlights)

    def _source_key(self, widget):
        for key, candidate in self.sources.items():
            if candidate is widget:
                return key
        key = uuid.uuid4().hex
        self.sources[key] = widget
        return key

    def add_initial(self, target, quote, selection):
        key, start, end = None, 0, 0
        if selection is not None:
            widget = selection[0]
            key = self._source_key(widget)
            if isinstance(widget, QLabel):
                start, end = selection[1], selection[1] + selection[2]
            else:
                start, end = selection[1].selectionStart(), selection[1].selectionEnd()
        self.input.add_quote(quote, target=target, source=key, start=start, end=end)
        self.sync_highlights()

    def toggle(self, turn, widget):
        references = self.references
        objects = self.input.quote_objects()
        if not objects or references.pending is None:
            return
        from core.chat_references import CHAT_ID
        from ui.views.chat_references import turn_metadata
        if turn_metadata(turn)[CHAT_ID] != references.pending["target"]:
            return
        quote, selection = references.selected_text(turn, widget)
        if not quote or selection is None or selection[0] is not widget:
            return
        key = self._source_key(widget)
        if isinstance(widget, QLabel):
            start, end = selection[1], selection[1] + selection[2]
            source = widget.quote_document().toPlainText() if isinstance(widget, QuoteSelectableLabel) else None
        else:
            start, end = selection[1].selectionStart(), selection[1].selectionEnd()
            source = widget.toPlainText()
        if source is None:
            return
        old = [obj for obj in objects if obj["source"] == key]
        intervals = xor_intervals([(obj["start"], obj["end"]) for obj in old], start, end)
        unchanged = {(obj["start"], obj["end"]): obj for obj in old}
        self._syncing = True
        edit = self.input.textCursor()
        edit.beginEditBlock()
        try:
            for obj in old:
                if (obj["start"], obj["end"]) not in intervals:
                    self.input.remove_quote(obj["uid"])
            for a, b in intervals:
                if (a, b) not in unchanged:
                    self.input.add_quote(utf16_slice(source, a, b),
                                         target=references.pending["target"], source=key, start=a, end=b)
        finally:
            edit.endEditBlock()
            self._syncing = False
        # Hide the transient native selection; all persistent ranges stay visible.
        if isinstance(widget, QLabel):
            widget.setSelection(0, 0)
        else:
            cursor = widget.textCursor()
            cursor.clearSelection()
            widget.setTextCursor(cursor)
        self.sync_highlights()

    def sync_highlights(self):
        if self._syncing:
            return
        objects = self.input.quote_objects()
        # Native Undo can restore quotes after cancelling a reply. The objects
        # retain their target ID, so restore the reference along with the text.
        if objects and (self.references.pending is None or
                        self.references.pending['target'] != objects[0]['target']):
            target_id = objects[0]['target']
            target = self.references.lookup(target_id)
            self.references.pending = {
                'target': target_id,
                'author': self.references.author(target) if target is not None else tr('ui.inspector.message_2f77'),
                'preview': self.references.text(target)[:200] if target is not None else objects[0]['text'][:200],
            }
            self.references.schedule_refresh()
        for key, widget in self.sources.items():
            if not isValid(widget):
                continue
            ranges = [(obj["start"], obj["end"]) for obj in objects if obj["source"] == key]
            if isinstance(widget, QuoteSelectableLabel):
                widget.set_quote_ranges(ranges)
            elif isinstance(widget, QTextEdit):
                from ui.views.chat_prose import apply_quote_ranges
                apply_quote_ranges(widget, ranges)


    def clear(self, forget_sources=False):
        self._syncing = True
        try:
            for widget in self.sources.values():
                if isValid(widget):
                    if isinstance(widget, QuoteSelectableLabel):
                        widget.set_quote_ranges([])
                    elif isinstance(widget, QTextEdit):
                        widget._chat_quote_ranges = []
                        widget.setExtraSelections([])
            if forget_sources:
                self.sources.clear()
            self.input.remove_all_quotes()
        finally:
            self._syncing = False
