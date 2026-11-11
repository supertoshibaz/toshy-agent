"""Resize grips for the frameless desktop window."""
from PySide6.QtCore import QObject, QEvent, Qt, QRect
from PySide6.QtWidgets import QWidget, QLayout


class ResizeGrip(QWidget):
    def __init__(self, window, edges, cursor):
        super().__init__(window)
        self.edges = edges
        self.drag = None
        self.setCursor(cursor)
        self.setObjectName('WindowResizeGrip')
        self.setStyleSheet('background:transparent;border:0;')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            window = self.window()
            if not window.windowHandle().startSystemResize(self.edges):
                self.drag = (event.globalPosition().toPoint(), QRect(window.geometry()))
                self.grabMouse()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is None:
            return
        origin, geometry = self.drag
        delta = event.globalPosition().toPoint() - origin
        rect = QRect(geometry)
        if self.edges & Qt.LeftEdge:
            rect.setLeft(min(rect.right(), rect.left() + delta.x()))
        if self.edges & Qt.RightEdge:
            rect.setRight(max(rect.left(), rect.right() + delta.x()))
        if self.edges & Qt.TopEdge:
            rect.setTop(min(rect.bottom(), rect.top() + delta.y()))
        if self.edges & Qt.BottomEdge:
            rect.setBottom(max(rect.top(), rect.bottom() + delta.y()))
        self.window().setGeometry(rect)

    def mouseReleaseEvent(self, event):
        if self.drag is not None:
            self.drag = None
            self.releaseMouse()
        event.accept()


class WindowResizeController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.grips = [ResizeGrip(window, edges, cursor) for edges, cursor in (
            (Qt.LeftEdge, Qt.SizeHorCursor), (Qt.RightEdge, Qt.SizeHorCursor),
            (Qt.TopEdge, Qt.SizeVerCursor), (Qt.BottomEdge, Qt.SizeVerCursor),
            (Qt.LeftEdge | Qt.TopEdge, Qt.SizeFDiagCursor),
            (Qt.RightEdge | Qt.TopEdge, Qt.SizeBDiagCursor),
            (Qt.LeftEdge | Qt.BottomEdge, Qt.SizeBDiagCursor),
            (Qt.RightEdge | Qt.BottomEdge, Qt.SizeFDiagCursor),
        )]
        window.layout().setSizeConstraint(QLayout.SetNoConstraint)
        window.setMinimumSize(1, 1)
        window.installEventFilter(self)
        self.position()

    def position(self):
        window = self.window
        active = not (window.isMaximized() or window.isFullScreen())
        width, height, edge = window.width(), window.height(), 6
        key = active, width, height
        if key == getattr(self, "_position_key", None):
            return
        self._position_key = key
        rectangles = (
            (0, edge, edge, max(0, height - edge * 2)),
            (width - edge, edge, edge, max(0, height - edge * 2)),
            (edge, 0, max(0, width - edge * 2), edge),
            (edge, height - edge, max(0, width - edge * 2), edge),
            (0, 0, edge, edge), (width - edge, 0, edge, edge),
            (0, height - edge, edge, edge), (width - edge, height - edge, edge, edge),
        )
        for grip, geometry in zip(self.grips, rectangles):
            grip.setGeometry(*geometry)
            grip.setVisible(active)
            grip.raise_()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Show:
            self._position_key = None
        if event.type() in (QEvent.Resize, QEvent.WindowStateChange, QEvent.Show, QEvent.LayoutRequest):
            self.position()
        return False
