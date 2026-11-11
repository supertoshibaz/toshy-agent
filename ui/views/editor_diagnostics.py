"""Индекс диагностик: при прокрутке и движении курсора работаем только с экраном."""
from bisect import bisect_right
from collections.abc import Mapping

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QTextEdit


class DiagnosticStatuses(Mapping):
    def __init__(self, index, suppressed):
        self.index, self.suppressed = index, suppressed

    def __getitem__(self, line):
        status = self.index.status[line]
        if self.suppressed is None or line > self.suppressed:
            return status
        levels = [d.get('severity', 1) for d in self.index.by_start[line]
                  if d.get('range', {}).get('end', {}).get('line', line) < self.suppressed]
        if 1 in levels:
            return 'error'
        if 2 in levels:
            return 'warning'
        raise KeyError(line)

    def __iter__(self):
        return (line for line in self.index.status if self.get(line) is not None)

    def __len__(self):
        return sum(1 for _ in self)


class DiagnosticIndex:
    def __init__(self, diagnostics=()):
        self.entries = []
        self.by_start, self.status = {}, {}
        for diagnostic in diagnostics:
            span = diagnostic.get('range', {})
            start = max(0, int(span.get('start', {}).get('line', 0)))
            end = max(start, int(span.get('end', {}).get('line', start)))
            self.entries.append((start, end, diagnostic))
            self.by_start.setdefault(start, []).append(diagnostic)
            severity = diagnostic.get('severity', 1)
            if severity == 1:
                self.status[start] = 'error'
            elif severity == 2:
                self.status.setdefault(start, 'warning')
        self.entries.sort(key=lambda entry: entry[0])
        self.starts, self.max_ends = [], []
        maximum = -1
        for start, end, _ in self.entries:
            self.starts.append(start)
            maximum = max(maximum, end)
            self.max_ends.append(maximum)

    def intersecting(self, first, last):
        index = bisect_right(self.starts, last) - 1
        found = []
        while index >= 0 and self.max_ends[index] >= first:
            start, end, diagnostic = self.entries[index]
            if end >= first:
                found.append(diagnostic)
            index -= 1
        return reversed(found)

    def selections(self, editor, first, last, suppressed, statuses):
        doc = editor.document()
        active = []
        for diagnostic in self.intersecting(first, last):
            span = diagnostic.get('range', {})
            start = span.get('start', {})
            end = span.get('end', {})
            lo = start.get('line', 0)
            hi = end.get('line', lo)
            if suppressed is None or not lo <= suppressed <= hi:
                active.append(diagnostic)
        selections = []
        for line in {d.get('range', {}).get('start', {}).get('line', 0) for d in active}:
            status = statuses.get(line)
            block = doc.findBlockByNumber(line)
            if status is None or not first <= line <= last or not block.isValid() or not block.isVisible():
                continue
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(block)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.format.setBackground(QColor(239, 68, 68, 38) if status == 'error' else QColor(234, 179, 8, 30))
            selections.append(selection)
        for diagnostic in active:
            span = diagnostic.get('range', {})
            start, end = span.get('start', {}), span.get('end', {})
            lo = start.get('line', 0)
            hi = end.get('line', lo)
            block_start = doc.findBlockByNumber(max(first, lo))
            block_end = doc.findBlockByNumber(min(last, hi))
            if not block_start.isValid() or not block_end.isValid():
                continue
            if not block_start.isVisible() and not block_end.isVisible():
                continue
            if editor.markdown_handler.images.source_at(block_start.position()) or editor.markdown_handler.images.source_at(block_end.position()):
                continue
            col_start = start.get('character', 0) if lo >= first else 0
            col_end = end.get('character', col_start) if hi <= last else block_end.length() - 1
            cursor = QTextCursor(doc)
            cursor.setPosition(block_start.position() + max(0, min(col_start, block_start.length() - 1)))
            cursor.setPosition(block_end.position() + max(0, min(col_end, block_end.length() - 1)), QTextCursor.KeepAnchor)
            if not cursor.hasSelection():
                # Конец многострочного диапазона в колонке 0 не выделяет эту строку.
                if lo != hi or start.get('character', 0) != end.get('character', start.get('character', 0)):
                    continue
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            error = diagnostic.get('severity', 1) == 1
            selection.format.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            selection.format.setUnderlineColor(QColor('#ef4444' if error else '#eab308'))
            selection.format.setBackground(QColor(239, 68, 68, 55) if error else QColor(234, 179, 8, 45))
            selections.append(selection)
        return selections
