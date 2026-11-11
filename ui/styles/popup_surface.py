"""Allow rounded popup surfaces to paint into transparent window corners."""
import sys
from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QWidget, QMenu


class PopupSurfaces(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Polish and isinstance(watched, QWidget):
            if isinstance(watched, QMenu) or (watched.isWindow() and watched.windowType() in (Qt.Popup, Qt.ToolTip)):
                if sys.platform == 'win32':
                    # Windows requires a frameless native window for per-pixel
                    # alpha. Set this before the first show/handle creation;
                    # the native rectangular menu shadow also ignores QSS corners.
                    watched.setWindowFlags(watched.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
                watched.setAttribute(Qt.WA_TranslucentBackground, True)
                watched.setAutoFillBackground(False)
        return False


def install_popup_surfaces(app):
    if not hasattr(app, '_toshy_popup_surfaces'):
        app._toshy_popup_surfaces = PopupSurfaces(app)
        app.installEventFilter(app._toshy_popup_surfaces)
