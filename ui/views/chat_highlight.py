"""Paint message selection behind content, excluding the actions footer."""
from shiboken6 import isValid
from PySide6.QtCore import QEvent, QRect, Qt, QTimer
from PySide6.QtWidgets import QFrame


def content_rect(turn):
    bottom = max(0, turn.height() - turn.layout().contentsMargins().bottom())
    bubble = getattr(turn, 'bubble', None)
    if bubble is not None:
        bottom = bubble.geometry().bottom() + 1
    else:
        bar = getattr(turn, 'actions_bar', None)
        if bar is not None and not bar.isHidden():
            bottom = max(0, bar.y() - turn.layout().spacing())
    return QRect(0, 0, turn.width(), bottom)


class MessageHighlight(QFrame):
    def __init__(self, turn):
        super().__init__(turn)
        self.turn = turn
        self.setObjectName('ChatMessageHighlight')
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_StyledBackground)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        for widget in (turn, getattr(turn, 'bubble', None), getattr(turn, 'actions_bar', None)):
            if widget is not None:
                widget.installEventFilter(self)
        self.lower()
        self.hide()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Move, QEvent.LayoutRequest, QEvent.Show, QEvent.Hide):
            if self.isVisible():
                self.timer.start(0)
        return False

    def fit(self):
        if isValid(self.turn):
            self.setGeometry(content_rect(self.turn))
            self.lower()


def set_highlight(turn, color):
    highlight = getattr(turn, 'message_highlight', None)
    if color is None:
        if highlight is not None:
            highlight.hide()
            highlight.timer.stop()
        return
    if highlight is None:
        highlight = turn.message_highlight = MessageHighlight(turn)
    from ui.views.chat_model_blocks import outline_rule
    rule = ('#ChatMessageHighlight {border:none; border-radius:8px; background-color:'
            + color + ';' + outline_rule(turn) + '}')
    if highlight.styleSheet() != rule:
        highlight.setStyleSheet(rule)
    highlight.fit()
    highlight.show()
