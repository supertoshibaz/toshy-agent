"""Квадратные кнопки управления генерацией с векторными значками."""
from ui.i18n import bind_caption, localize
from ui.icons import set_icon
from PySide6.QtCore import QObject, QEvent, QRectF, QPointF, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor




class _ButtonHeight(QObject):
    def __init__(self, button, send):
        super().__init__(button)
        self.button, self.send = button, send
        send.installEventFilter(self)
        self.sync()

    def sync(self):
        side = max(1, self.send.height() if self.send.isVisible() else self.send.sizeHint().height())
        self.button.setFixedSize(side, side)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.StyleChange):
            self.sync()
        return False


def configure_generation_button(button, send, *, thinking):
    bind_caption(button, '')
    set_icon(button, 'stop-thinking' if thinking else 'stop', size=20)
    button.setIconSize(QSize(20, 20))
    button.setStyleSheet('padding:0;')
    localize(button, 'generation.finish_thinking' if thinking else 'generation.stop', setter='setAccessibleName')
    button._height_sync = _ButtonHeight(button, send)
