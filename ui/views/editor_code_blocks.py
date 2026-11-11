"""Fenced-code presentation objects for live editors; source stays lossless."""
import math

from PySide6.QtCore import QObject, QEvent, QSizeF, Qt, QTimer
from PySide6.QtGui import QTextCursor, QTextCharFormat, QTextFormat, QTextObjectInterface
from PySide6.QtWidgets import QPlainTextEdit
try:
    from PySide6.QtGui import QPyTextObject
except ImportError:
    class QPyTextObject(QObject, QTextObjectInterface):
        pass

from ui.views.chat_markdown import fenced_blocks
from ui.views.code_block import CodeBlockWidget

CODE_OBJECT = QTextFormat.UserObject.value + 39
# Inline images reserve +38/+39 (source/padding), quote input reserves +41.
# Sharing +39 made the image-padding cleanup delete a rendered code object.
CODE_SOURCE = QTextFormat.UserProperty.value + 50
CODE_FOLDED = QTextFormat.UserProperty.value + 51
CODE_RENDERED = QTextFormat.UserProperty.value + 52


class CodeTextObject(QPyTextObject):
    def __init__(self, controller):
        super().__init__(controller.editor)
        self.controller = controller

    def intrinsicSize(self, document, position, fmt):
        widget = self.controller.widget_for(position, fmt)
        return self.controller.extent(widget)

    def drawObject(self, painter, rect, document, position, fmt):
        pass  # A real widget on the viewport paints the image and buttons.


class LiveCodeBlocks(QObject):
    def __init__(self, handler):
        super().__init__(handler.editor)
        self.handler, self.editor = handler, handler.editor
        self.widgets = []
        self.syncing = False
        self._needs_scan = True
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.position_widgets)
        self.renderer = CodeTextObject(self)
        self.editor.document().documentLayout().registerHandler(CODE_OBJECT, self.renderer)
        self.editor.document().contentsChange.connect(self.changed)
        self.editor.updateRequest.connect(lambda *_: self.timer.start())
        self.editor.viewport().installEventFilter(self)

    def active(self):
        return self.handler.enabled and self.handler.visual_enabled

    def available_width(self):
        return max(1, int(self.editor.viewport().width() - 2 * self.editor.document().documentMargin() - 8))

    def extent(self, widget):
        width = self.available_width()
        line = max(1, self.editor.fontMetrics().lineSpacing())
        height = math.ceil(widget.presentation_height(width) / line) * line
        return QSizeF(width, height)

    def changed(self, position, removed, added):
        if removed or added:
            self._needs_scan = True
        if not self.syncing:
            self.timer.start()

    def source_at(self, position):
        doc = self.editor.document()
        if position < 0 or position >= doc.characterCount() - 1 or doc.characterAt(position) != '\ufffc':
            return None
        cursor = QTextCursor(doc)
        cursor.setPosition(position)
        cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
        fmt = cursor.charFormat()
        return str(fmt.property(CODE_SOURCE) or '') if fmt.objectType() == CODE_OBJECT else None

    def convert(self, start, end, *, folded=False, rendered=False):
        doc = self.editor.document()
        first, last = doc.findBlockByNumber(start), doc.findBlockByNumber(end)
        if not first.isValid() or not last.isValid():
            return
        cursor = QTextCursor(doc)
        cursor.setPosition(first.position())
        cursor.setPosition(last.position() + last.length() - 1, QTextCursor.KeepAnchor)
        source = cursor.selectedText().replace('\u2029', '\n')
        parsed = fenced_blocks(source)
        if len(parsed) != 1 or parsed[0].start != 0 or parsed[0].end != source.count('\n'):
            return
        fmt = QTextCharFormat()
        fmt.setObjectType(CODE_OBJECT)
        fmt.setProperty(CODE_SOURCE, source)
        fmt.setProperty(CODE_FOLDED, folded)
        fmt.setProperty(CODE_RENDERED, rendered)
        modified = doc.isModified()
        self.syncing = True
        try:
            cursor.joinPreviousEditBlock()
            cursor.insertText('\ufffc', fmt)
            cursor.endEditBlock()
            doc.setModified(modified)
        finally:
            self.syncing = False
        self.handler.parse_code_blocks()
        self.position_widgets()

    def widget_for(self, position, fmt):
        source = str(fmt.property(CODE_SOURCE) or '')
        for widget, anchor in self.widgets:
            if anchor.position() == position and widget.property('code_source') == source:
                return widget
        parsed = fenced_blocks(source)
        code = parsed[0] if parsed else None
        widget = CodeBlockWidget(code.code if code else source,
                                 language=code.language if code else '', parent=self.editor.viewport())
        widget.setProperty('code_source', source)
        widget._preview_requested = bool(fmt.property(CODE_RENDERED))
        widget.set_folded(bool(fmt.property(CODE_FOLDED)))
        anchor = QTextCursor(self.editor.document())
        anchor.setPosition(position)
        self.widgets.append((widget, anchor))
        widget.presentation_changed.connect(lambda w=widget, a=anchor: self.presentation_changed(w, a))
        self.timer.start()
        return widget

    def presentation_changed(self, widget, anchor):
        if self.syncing or not self.source_at(anchor.position()):
            return
        if not widget.folded and not widget._preview_requested:
            self.restore(anchor.position())
            return
        self.editor.document().markContentsDirty(anchor.position(), 1)
        self.timer.start()

    def restore(self, position, *, place_cursor=False):
        source = self.source_at(position)
        if not source:
            return False
        doc = self.editor.document()
        modified = doc.isModified()
        self.syncing = True
        try:
            cursor = QTextCursor(doc)
            cursor.setPosition(position)
            cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
            cursor.joinPreviousEditBlock()
            cursor.insertText(source, QTextCharFormat())
            cursor.endEditBlock()
            if place_cursor:
                cursor.setPosition(position)
                self.editor.setTextCursor(cursor)
            doc.setModified(modified)
        finally:
            self.syncing = False
        self.handler.parse_code_blocks()
        self.timer.start()
        return True

    def restore_all(self):
        doc = self.editor.document()
        positions = []
        block = doc.firstBlock()
        while block.isValid():
            if self.source_at(block.position()):
                positions.append(block.position())
            block = block.next()
        for position in reversed(positions):
            self.restore(position)
        self.position_widgets()

    def position_widgets(self):
        doc = self.editor.document()
        # Undo may have restored an object after its old widget was destroyed.
        if self._needs_scan:
            self._needs_scan = False
            block = doc.firstBlock()
            while block.isValid():
                if self.active() and self.source_at(block.position()):
                    cursor = QTextCursor(block)
                    cursor.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
                    self.widget_for(block.position(), cursor.charFormat())
                block = block.next()
        for widget, anchor in list(self.widgets):
            if not self.active() or self.source_at(anchor.position()) != widget.property('code_source'):
                widget.hide()
                widget.deleteLater()
                self.widgets.remove((widget, anchor))
                continue
            rect = self.editor.blockBoundingGeometry(anchor.block()).translated(self.editor.contentOffset())
            size = self.extent(widget)
            widget.setGeometry(round(doc.documentMargin()), round(rect.top()), round(size.width()), round(size.height()))
            widget.setVisible(rect.bottom() >= 0 and rect.top() <= self.editor.viewport().height())
            widget.raise_()

        # Floating editor controls must stay above embedded image/code widgets.
        from PySide6.QtWidgets import QWidget
        for overlay in self.editor.viewport().findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
            if overlay.isVisible() and overlay.property('editorOverlay'):
                overlay.raise_()
                tags = getattr(overlay, 'tags_bar', None)
                if tags is not None and tags.isVisible():
                    tags.raise_()

    def handle_key(self, event):
        if not self.active() or event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return False
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            return False
        position = cursor.position() - 1 if event.key() == Qt.Key_Backspace else cursor.position()
        if self.source_at(position) and (event.text() or event.key() in {Qt.Key_Backspace, Qt.Key_Delete, Qt.Key_Return, Qt.Key_Enter}):
            return self.restore(position, place_cursor=True)
        return False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            for _widget, anchor in self.widgets:
                self.editor.document().markContentsDirty(anchor.position(), 1)
            self.timer.start()
        return False
