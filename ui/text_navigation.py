"""Shared boundary navigation for editable, multiline Qt controls."""
from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit


def move_at_text_boundary(editor, event):
    if (event.key() not in (Qt.Key_Up, Qt.Key_Down) or editor.isReadOnly()
            or event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
        return False
    cursor = editor.textCursor()
    probe = QTextCursor(cursor)
    direction = QTextCursor.Up if event.key() == Qt.Key_Up else QTextCursor.Down
    if probe.movePosition(direction):
        return False
    anchor = QTextCursor.KeepAnchor if event.modifiers() & Qt.ShiftModifier else QTextCursor.MoveAnchor
    cursor.movePosition(QTextCursor.Start if event.key() == Qt.Key_Up else QTextCursor.End, anchor)
    editor.setTextCursor(cursor)
    editor.ensureCursorVisible()
    event.accept()
    return True


class TextNavigation(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and isinstance(watched, (QPlainTextEdit, QTextEdit)):
            if getattr(watched, '_text_boundary_in_key_handler', False):
                return False
            return move_at_text_boundary(watched, event)
        return False


def install_text_navigation(application):
    if not hasattr(application, '_text_navigation'):
        application._text_navigation = TextNavigation(application)
        application.installEventFilter(application._text_navigation)
