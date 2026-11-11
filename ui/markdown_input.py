"""Markdown keystrokes for QTextEdit chat inputs, without preview/layout objects."""
import re
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from ui.views.markdown_handler import MarkdownHandler


class MarkdownInputKeys:
    enabled = True
    _format_full_lines = MarkdownHandler._format_full_lines
    _handle_multiline_space = MarkdownHandler._handle_multiline_space
    wrap_selection = MarkdownHandler.wrap_selection
    _handle_smart_tab = MarkdownHandler._handle_smart_tab
    _handle_smart_enter = MarkdownHandler._handle_smart_enter

    def __init__(self, editor):
        self.editor = editor
        editor.setTabStopDistance(editor.fontMetrics().horizontalAdvance(' ') * 4)

    @staticmethod
    def selected_source_text(cursor):
        return cursor.selectedText().replace('\u2029', '\n').replace('\u2028', '\n')

    def handle(self, event):
        cursor = self.editor.textCursor()
        prefix = QTextCursor(cursor.document())
        prefix.setPosition(cursor.position(), QTextCursor.KeepAnchor)
        before_cursor = self.selected_source_text(prefix)
        if len(re.findall(r'(?m)^\s*```', before_cursor)) % 2:
            return False
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.ShiftModifier:
            match = re.match(r'^([ \t]*)(?:[-+*]|\d+[.)])\s+', cursor.block().text())
            cursor.insertText('\n' + (match.group(1) + '\t' if match else ''))
            self.editor.setTextCursor(cursor)
            return True
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.NoModifier:
            return self._handle_smart_enter()
        if event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            return self._handle_smart_tab(event.key() == Qt.Key_Backtab or bool(event.modifiers() & Qt.ShiftModifier))
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return False
        # Chat composers and message editors use literal punctuation. Editor
        # preferences for paired delimiters apply only to document workspaces.
        if event.text() in {'*', '_', '`', '~', '='} or event.key() == Qt.Key_Backspace:
            return False
        text = event.text()
        if text in {'-', '+', '>', '#'} and self._format_full_lines(text + ' ', line_prefix=True):
            return True
        if text == ' ' and self._handle_multiline_space():
            return True
        return False



def handle_markdown_input(editor, event):
    handler = getattr(editor, '_markdown_input_keys', None)
    if handler is None:
        handler = editor._markdown_input_keys = MarkdownInputKeys(editor)
    return handler.handle(event)
