"""Копирование полного сообщения об ошибке без сокращения текста."""
from ui.icons import get as get_icon, set_icon
from ui.styles.binding import form_field
from ui.i18n import set_text

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QToolButton, QStyle


def copy_error_button(parent, text):
    button = QToolButton(parent)
    button.setObjectName('ErrorCopyBtn')
    set_icon(button, 'copy', size=18)
    button.setFixedSize(28, 28)
    set_text(button, 'ui.error_actions.copy_complete_error_df89', setter='setToolTip')
    button.setCursor(Qt.PointingHandCursor)
    button.clicked.connect(lambda: QApplication.clipboard().setText(text()))
    return button


class ErrorDetailsPanel(QFrame):
    dismissed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(copy_error_button(self, self.toPlainText))
        close = QToolButton(self)
        set_icon(close, 'close', square=True)
        set_text(close, 'ui.top_bar.close_a7a4', setter='setToolTip')
        close.setFixedSize(22, 22)
        close.clicked.connect(self._dismiss)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.editor = form_field(QPlainTextEdit(self))
        self.editor.setReadOnly(True)
        layout.addWidget(self.editor)

    def setPlainText(self, text):
        self.editor.setPlainText(text)

    def toPlainText(self):
        return self.editor.toPlainText()

    def _dismiss(self):
        self.hide()
        self.dismissed.emit()
