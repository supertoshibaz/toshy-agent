"""Context-owned deferred calls, including PySide versions without that overload."""
from PySide6.QtCore import QTimer


def defer(milliseconds, context, callback):
    timer = QTimer(context)
    timer.setSingleShot(True)

    def invoke():
        try:
            callback()
        finally:
            timer.deleteLater()

    timer.timeout.connect(invoke)
    timer.start(milliseconds)
    return timer
