"""Selection wrapping shared by file and card editors."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor


def wrap_selected_text(editor, event):
    if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
        return False
    pairs = {'(': ('(', ')'), ')': ('(', ')'), '"': ('"', '"')}
    pair = pairs.get(event.text())
    cursor = editor.textCursor()
    if not pair or not cursor.hasSelection() or editor.isReadOnly():
        return False
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    cursor.beginEditBlock()
    cursor.setPosition(end)
    cursor.insertText(pair[1])
    cursor.setPosition(start)
    cursor.insertText(pair[0])
    cursor.endEditBlock()
    cursor.setPosition(start + 1)
    cursor.setPosition(end + 1, QTextCursor.KeepAnchor)
    editor.setTextCursor(cursor)
    event.accept()
    return True


def xor_intervals(base_intervals, start, end):
    """The same scattered-selection toggle for editor context and chat quotes."""
    if end <= start:
        return list(base_intervals)
    events = {}
    for a, b in list(base_intervals) + [(start, end)]:
        events[a] = events.get(a, 0) ^ 1
        events[b] = events.get(b, 0) ^ 1
    result, active, segment_start = [], False, None
    for pos in sorted(events):
        if not events[pos]:
            continue
        if active:
            if segment_start is not None and pos > segment_start:
                result.append((segment_start, pos))
            segment_start = None
        else:
            segment_start = pos
        active = not active
    return result
