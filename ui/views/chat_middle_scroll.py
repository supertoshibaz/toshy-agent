"""Прокрутка истории мышью после нажатия средней кнопки."""
import math

from PySide6.QtCore import QObject, QEvent, QTimer, QElapsedTimer, QPoint, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


class ScrollMarker(QWidget):
    def __init__(self, viewport, anchor):
        super().__init__(viewport)
        self.anchor = viewport.mapFromGlobal(anchor)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeVerCursor)
        self.setGeometry(viewport.rect())
        self.show()
        self.raise_()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#d4d4d8'), 1))
        painter.setBrush(QColor(30, 30, 35, 220))
        painter.drawEllipse(self.anchor, 11, 11)
        painter.setBrush(QColor('#d4d4d8'))
        x, y = self.anchor.x(), self.anchor.y()
        for direction in (-1, 1):
            painter.drawPolygon(QPolygon([QPoint(x, y + direction * 7),
                QPoint(x - 3, y + direction * 3), QPoint(x + 3, y + direction * 3)]))


class MiddleScroll(QObject):
    def __init__(self, area):
        super().__init__(area)
        self.area = area
        self.active = False
        self.marker = None
        self._swallow_release = False
        self._fraction = 0.0
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        QApplication.instance().installEventFilter(self)

    def stop(self):
        self.active = False
        self.timer.stop()
        if self.marker is not None:
            if isValid(self.marker):
                self.marker.hide()
                self.marker.deleteLater()
            self.marker = None

    def eventFilter(self, watched, event):
        if not isValid(self.area):
            return False
        kind = event.type()
        if self.active and watched is self.area and kind in (QEvent.Hide, QEvent.Close):
            self.stop()
        if self.active and kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.stop()
            return True
        if kind == QEvent.MouseButtonRelease and event.button() == Qt.MiddleButton and self._swallow_release:
            self._swallow_release = False
            return True
        if kind != QEvent.MouseButtonPress:
            return False
        viewport = self.area.viewport()
        inside = (isinstance(watched, QWidget) and
                  (watched is viewport or viewport.isAncestorOf(watched)) and
                  viewport.rect().contains(viewport.mapFromGlobal(event.globalPosition().toPoint())))
        if self.active:
            self.stop()
            self._swallow_release = event.button() == Qt.MiddleButton
            return inside
        if inside and event.button() == Qt.MiddleButton:
            self.active = True
            self.anchor = event.globalPosition().toPoint()
            self._swallow_release = True
            self._fraction = 0.0
            self.marker = ScrollMarker(viewport, self.anchor)
            self.clock.start()
            self.timer.start()
            self.area.navigation_started.emit()
            return True
        return False

    def tick(self):
        viewport = self.area.viewport()
        position = QCursor.pos()
        if not self.area.isVisible():
            self.stop()
            return
        self.marker.setGeometry(viewport.rect())
        elapsed = min(0.05, self.clock.restart() / 1000)
        delta = position.y() - self.anchor.y()
        distance = max(0, abs(delta) - 14)
        if not distance:
            self._fraction = 0.0
            return
        speed = math.copysign(min(1800, 25 + distance * 4 + distance ** 2 * 0.025), delta)
        self._fraction += speed * elapsed
        pixels = math.trunc(self._fraction)
        self._fraction -= pixels
        self.area.navigation_started.emit()
        bar = self.area.verticalScrollBar()
        bar.setValue(bar.value() + pixels)
        if delta < 0 and bar.value() <= 40:
            self.area.older_requested.emit()
