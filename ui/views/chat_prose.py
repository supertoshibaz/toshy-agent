"""Native selection and layout for a continuous rich-text prose section."""
from math import ceil

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor, QTextOption
from PySide6.QtWidgets import QTextBrowser, QFrame, QSizePolicy

from ui.styles.binding import apply_style


def apply_quote_ranges(widget, ranges):
    from ui.themes.manager import theme_manager
    widget._chat_quote_ranges = list(ranges)
    selections = []
    end = widget.document().characterCount() - 1
    for a, b in ranges:
        selection = QTextBrowser.ExtraSelection()
        selection.cursor = QTextCursor(widget.document())
        selection.cursor.setPosition(max(0, min(a, end)))
        selection.cursor.setPosition(max(0, min(b, end)), QTextCursor.KeepAnchor)
        tint = theme_manager.color('accent.primary'); tint.setAlpha(75)
        selection.format.setBackground(tint)
        selections.append(selection)
    widget.setExtraSelections(selections)


class ChatProseView(QTextBrowser):
    geometry_changed = Signal()

    def __init__(self, parent, open_reference):
        super().__init__(parent)
        self._chat_quote_ranges = []
        self._markup = None
        self.setObjectName('ChatMessageTextLabel')
        self.setFrameShape(QFrame.NoFrame)
        self.setReadOnly(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setOpenExternalLinks(False); self.setOpenLinks(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.anchorClicked.connect(lambda url: open_reference(url.toString()))
        self.setFont(parent.font())
        self.setContentsMargins(0, 0, 0, 0)
        apply_style(self, 'QTextBrowser {background:transparent; border:none; padding:0;}')
        self.document().setDocumentMargin(0)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        policy.setHeightForWidth(True); self.setSizePolicy(policy)
        self.setFixedHeight(0)

    def set_markup(self, markup):
        if markup == self._markup:
            return
        cursor = self.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        selected = cursor.selectedText()
        self._markup = markup
        self.setHtml(markup)
        if selected:
            cursor = QTextCursor(self.document())
            last = self.document().characterCount() - 1
            cursor.setPosition(min(anchor, last))
            cursor.setPosition(min(position, last), QTextCursor.KeepAnchor)
            if cursor.selectedText() == selected:
                self.setTextCursor(cursor)
        apply_quote_ranges(self, self._chat_quote_ranges)
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        self.updateGeometry()

    def heightForWidth(self, width):
        self.document().setTextWidth(max(1, width))
        return max(0, ceil(self.document().size().height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.geometry_changed.emit()

    def wheelEvent(self, event):
        event.ignore()
