"""Hover opens format choices; clicking the parent row opens its default."""
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import QMenu, QApplication


class DefaultOpenMenu(QMenu):
    primary_requested = Signal(str)

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.primary_targets = {}
        self._press_target = None

    def showEvent(self,event):
        super().showEvent(event)
        QApplication.instance().installEventFilter(self)

    def hideEvent(self,event):
        self._press_target = None
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self,watched,event):
        # An open child QMenu owns the popup mouse grab. Its events still need
        # to activate a click over the File/Board row in this parent menu.
        if self.isVisible() and event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease) and event.button()==Qt.LeftButton:
            point = self.mapFromGlobal(event.globalPosition().toPoint())
            action = self.actionAt(point) if self.rect().contains(point) else None
            target = self.primary_targets.get(action) if action is not None and action.isEnabled() else None
            if event.type()==QEvent.MouseButtonPress:
                self._press_target = target
                if target:
                    return True
            else:
                pressed,self._press_target = self._press_target,None
                if pressed and target==pressed:
                    self.primary_requested.emit(target)
                    return True
        return super().eventFilter(watched,event)

    def mousePressEvent(self, event):
        action = self.actionAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and action in self.primary_targets:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and action in self.primary_targets and action.isEnabled():
            self.primary_requested.emit(self.primary_targets[action])
            event.accept()
            return
        super().mouseReleaseEvent(event)
