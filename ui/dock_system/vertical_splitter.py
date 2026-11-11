"""A small centre detent for the two vertically stacked sidebar widgets."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QSplitterHandle


class CentreHandle(QSplitterHandle):
    snap_distance = 8

    def centre(self):
        splitter = self.splitter()
        if splitter.count() != 2 or any(splitter.widget(i).isHidden() for i in range(2)):
            return None
        target = splitter.contentsRect().top() + (splitter.contentsRect().height()-self.height())//2
        minimum, maximum = splitter.getRange(1)
        return target if minimum <= target <= maximum else None

    def snap(self):
        centre = self.centre()
        if centre is not None and abs(self.y()-centre) <= self.snap_distance:
            self.splitter().moveSplitter(centre, 1)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if event.buttons() & Qt.LeftButton:
            self.snap()
        self.update()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self.snap()
        self.update()



class VerticalDockSplitter(QSplitter):
    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)

    def createHandle(self):
        return CentreHandle(Qt.Vertical, self)
