"""Find in rendered Markdown without changing its mode or source document."""
import re
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QTextDocument, QColor
from PySide6.QtWidgets import QTextEdit, QPlainTextEdit, QLabel
from ui.widgets.search_popup import SearchReplacePopup
from shiboken6 import isValid


class ReadOnlySearch(QObject):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.popup = SearchReplacePopup(editor, replace_enabled=False)
        self.matches, self.index = [], -1
        self.highlighted = None
        self.popup.search_changed.connect(self.search)
        self.popup.next_requested.connect(self.next)
        self.popup.closed.connect(self.clear)
        editor.renderer.layout_ready.connect(self._refresh_highlight)

    def open(self, pos=None):
        self.popup.open_at(pos)

    def search(self, query, case, whole):
        pattern = re.escape(query)
        if whole:
            pattern = r'(?<!\w)' + pattern + r'(?!\w)'
        self.matches = list(re.finditer(pattern, self.editor.toPlainText(), 0 if case else re.IGNORECASE)) if query else []
        self.index = 0 if self.matches else -1
        self.show_match()

    def next(self, backwards=False):
        if self.matches:
            self.index = (self.index + (-1 if backwards else 1)) % len(self.matches)
        self.show_match()

    def clear(self):
        if self.highlighted is not None and isValid(self.highlighted):
            if isinstance(self.highlighted, QLabel):self.highlighted.setSelection(0, 0)
            else:self.highlighted.setExtraSelections([])
        self.highlighted = None

    def _refresh_highlight(self):
        if self.popup.isVisible():
            self.show_match()

    def show_match(self):
        self.clear()
        self.popup.set_status(self.index, len(self.matches))
        if self.index < 0:
            return
        match = self.matches[self.index]
        source = self.editor.toPlainText()
        line = source.count('\n', 0, match.start())
        self.editor.minimap_scroll_to_row(line, center=True)
        for start, end, widget in self.editor.renderer.sections:
            if not start <= line < end:
                continue
            flags = QTextDocument.FindFlags()
            if self.popup.case_btn.isChecked():flags |= QTextDocument.FindCaseSensitively
            if self.popup.word_btn.isChecked():flags |= QTextDocument.FindWholeWords
            section_start = sum(len(row) for row in source.splitlines(keepends=True)[:start])
            occurrence = sum(1 for candidate in self.matches[:self.index]
                             if candidate.start() >= section_start)
            target = getattr(widget, 'code_edit', widget)
            targets = [target] if isinstance(target, (QTextEdit, QPlainTextEdit)) else widget.findChildren(QLabel)
            for target in targets:
                if isinstance(target, QLabel):
                    document = QTextDocument()
                    document.setHtml(target.text())
                else:document = target.document()
                found = document.find(self.popup.find_edit.text(), 0, flags)
                while not found.isNull():
                    if occurrence == 0:
                        if hasattr(widget, 'set_folded'):widget.set_folded(False)
                        if isinstance(target, QLabel):
                            target.setTextInteractionFlags(target.textInteractionFlags() | Qt.TextSelectableByMouse)
                            target.setSelection(found.selectionStart(), found.selectionEnd() - found.selectionStart())
                        else:
                            selection = QTextEdit.ExtraSelection()
                            selection.cursor = found
                            selection.format.setBackground(QColor('#facc15'))
                            selection.format.setForeground(QColor('#18181b'))
                            target.setExtraSelections([selection])
                            target.setTextCursor(found)
                        self.highlighted = target
                        return
                    occurrence -= 1
                    found = document.find(self.popup.find_edit.text(), found.selectionEnd(), flags)
            break
