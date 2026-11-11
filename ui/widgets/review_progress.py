"""Передача хода проверки из рабочего потока в интерфейс."""
from PySide6.QtCore import QObject, Signal


class ReviewProgress(QObject):
    message = Signal(str)


def review_icon():
    from ui.icons import get
    return get('task')
