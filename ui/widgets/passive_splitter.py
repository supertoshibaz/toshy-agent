"""Разделители без явного захвата мыши, с завершением жеста при смене вкладки."""
import weakref

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication, QSplitter, QSplitterHandle, QStyleFactory, QWidget
from shiboken6 import isValid


class PassiveHandle(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        # Breeze добавляет SplitterProxy с явным grabMouse(), запрещённым в Wayland.
        # Меняем стиль только ручки до её polish; оформление приложения сохраняется.
        self._handle_style = QStyleFactory.create('Fusion')
        self._handle_style.setParent(self)
        self.setStyle(self._handle_style)
        self._dragging = False
        self._moving = False
        self._owner = weakref.ref(parent)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(80)
        self._watchdog.timeout.connect(self._check_drag)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._finish()
        splitter = self._owner()
        if splitter is None or not isValid(splitter) or splitter.indexOf(self) <= 0:
            return
        self._origin = event.globalPosition().toPoint()
        self._position = self.x() if self.orientation() == Qt.Horizontal else self.y()
        self._dragging = True
        QApplication.instance().installEventFilter(self)
        self._watchdog.start()
        event.accept()

    def event(self, event):
        if event.type() == QEvent.UngrabMouse:
            self._finish(release=False)
            return super().event(event)
        if event.type() in (QEvent.Hide, QEvent.HideToParent, QEvent.ParentAboutToChange,
                            QEvent.DeferredDelete):
            self._finish()
        elif event.type() == QEvent.EnabledChange and not self.isEnabled():
            self._finish()
        return super().event(event)

    def _check_drag(self):
        # Wayland может не доставить отпускание за пределами окна.
        if not isValid(self):
            self._finish()
            return
        if (self._dragging and (not self.isVisible() or not self.isEnabled()
                or not QApplication.mouseButtons() & Qt.LeftButton)):
            self._finish()

    def eventFilter(self, watched, event):
        if not self._dragging or not isValid(self):
            return False
        kind = event.type()
        if kind == QEvent.MouseButtonPress:
            # Новый клик завершает потерянный жест, но сам проходит к своей кнопке.
            self._finish()
            return False
        if kind == QEvent.ApplicationDeactivate:
            self._finish()
            return False
        if kind == QEvent.WindowDeactivate:
            window = self.window()
            if watched is window or watched is window.windowHandle():
                self._finish()
            return False
        if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self._finish()
            return True
        if kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._finish()
            # Qt и сторонние фильтры тоже должны завершить свой жест.
            return False
        if kind != QEvent.MouseMove:
            return False
        # Сначала Qt должен доставить нативное событие QWindow его QWidget.
        # Иначе обходится учёт implicit grab и Enter/Leave в QWidgetWindow.
        if not isinstance(watched, QWidget):
            return False
        if not event.buttons() & Qt.LeftButton or not self.isVisible():
            self._finish()
            return False
        window = self.window()
        if watched.window() is not window:
            self._finish()
            return False
        if self._moving:
            return False
        splitter = self._owner()
        if splitter is None or not isValid(splitter) or splitter.indexOf(self) <= 0:
            self._finish()
            return False
        delta = event.globalPosition().toPoint() - self._origin
        offset = delta.x() if self.orientation() == Qt.Horizontal else delta.y()
        position = self._position + offset
        current = self.x() if self.orientation() == Qt.Horizontal else self.y()
        if position != current:
            self._moving = True
            try:
                # Метод ручки учитывает RTL и не запускает нативный mouse grab.
                self.moveSplitter(position)
            finally:
                self._moving = False
        return True

    def _finish(self, *, release=True):
        if getattr(self, '_finishing', False):
            return
        self._finishing = True
        try:
            self._dragging = False
            timer = getattr(self, '_watchdog', None)
            if timer is not None and isValid(timer):
                timer.stop()
            app = QApplication.instance()
            if app is not None and isValid(app) and isValid(self):
                app.removeEventFilter(self)
                # UngrabMouse уже находится внутри нативного снятия захвата.
                if release and QWidget.mouseGrabber() is self:
                    self.releaseMouse()
        finally:
            self._finishing = False

    def mouseReleaseEvent(self, event):
        self._finish()
        event.accept()


class PassiveSplitter(QSplitter):
    def createHandle(self):
        return PassiveHandle(self.orientation(), self)
