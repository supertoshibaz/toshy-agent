"""Search overlays without changing editor text, selection or undo history."""
from ui.i18n import bind_caption
import re
from PySide6.QtGui import QColor, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QTextEdit, QPlainTextEdit, QLabel

SEARCH_PROPERTY = int(QTextFormat.UserProperty) + 932


def ranges(text, query, case_sensitive=False, whole_word=False):
    if not query:
        return []
    pattern = re.escape(query)
    if whole_word:
        pattern = r'(?<!\w)' + pattern + r'(?!\w)'
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, 0 if case_sensitive else re.IGNORECASE)]


def qt_position(text, index):
    return len(text[:index].encode('utf-16-le')) // 2


def highlight_editor(editor, query, case_sensitive=False, whole_word=False, current=False):
    text = editor.document().toPlainText()
    selections = [s for s in editor.extraSelections() if not s.format.property(SEARCH_PROPERTY)]
    for start, end in ranges(text, query, case_sensitive, whole_word):
        selection = QTextEdit.ExtraSelection()
        selection.cursor = QTextCursor(editor.document())
        selection.cursor.setPosition(qt_position(text, start))
        selection.cursor.setPosition(qt_position(text, end), QTextCursor.KeepAnchor)
        selection.format.setBackground(QColor('#b7791f' if current else '#345579'))
        selection.format.setForeground(QColor('#ffffff'))
        selection.format.setProperty(SEARCH_PROPERTY, True)
        selections.append(selection)
    editor.setExtraSelections(selections)


def highlight_rendered(root, query, case_sensitive=False, whole_word=False, current=False):
    for editor in root.findChildren(QTextEdit) + root.findChildren(QPlainTextEdit):
        highlight_editor(editor, query, case_sensitive, whole_word, current)
    # Native Markdown table cells are labels. Format a temporary document so
    # matches crossing inline markup work and links retain their destinations.
    from ui.views.markdown_render import MarkdownTableWidget
    for table in root.findChildren(MarkdownTableWidget):
        for label in table.findChildren(QLabel):
            original = label.property('_search_original_html')
            if original is None:
                original = label.text()
                label.setProperty('_search_original_html', original)
            if not query:
                bind_caption(label, original)
                continue
            doc = QTextDocument()
            doc.setHtml(original)
            text = doc.toPlainText()
            for start, end in ranges(text, query, case_sensitive, whole_word):
                cursor = QTextCursor(doc)
                cursor.setPosition(qt_position(text, start))
                cursor.setPosition(qt_position(text, end), QTextCursor.KeepAnchor)
                fmt = cursor.charFormat()
                fmt.setBackground(QColor('#b7791f' if current else '#345579'))
                fmt.setForeground(QColor('#ffffff'))
                cursor.mergeCharFormat(fmt)
            bind_caption(label, doc.toHtml())
