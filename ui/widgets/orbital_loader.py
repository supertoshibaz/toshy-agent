"""Toshy's eight-sector orbital mark, without its central core."""
import math
import time
from functools import lru_cache

from PySide6.QtCore import Qt, QRectF, QPointF, QSize, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton
from ui.themes.manager import theme_manager


# Exact paths from the supplied eight-sector SVG: outer clockwise, then inner.
# Render the explicit SVG contours instead of boolean path intersections, which
# can leave unwanted closing lines across the otherwise transparent centre.
_SECTORS = (
    'M 184.925 30.081 A 150 150 0 0 1 282.526241 70.508758 L 262.006709 91.028289 A 121 121 0 0 0 184.925 59.1 Z',
    'M 289.491242 77.473759 A 150 150 0 0 1 329.919 175.075001 L 300.9 175.075 A 121 121 0 0 0 268.971711 97.993291 Z',
    'M 329.919 184.925 A 150 150 0 0 1 289.491242 282.526241 L 268.971711 262.006709 A 121 121 0 0 0 300.9 184.925 Z',
    'M 282.526241 289.491242 A 150 150 0 0 1 184.924999 329.919 L 184.925 300.9 A 121 121 0 0 0 262.006709 268.971711 Z',
    'M 175.075 329.919 A 150 150 0 0 1 77.473759 289.491242 L 97.993291 268.971711 A 121 121 0 0 0 175.075 300.9 Z',
    'M 70.508758 282.526241 A 150 150 0 0 1 30.081 184.924999 L 59.1 184.925 A 121 121 0 0 0 91.028289 262.006709 Z',
    'M 30.081 175.075 A 150 150 0 0 1 70.508758 77.473759 L 91.028289 97.993291 A 121 121 0 0 0 59.1 175.075 Z',
    'M 77.473759 70.508758 A 150 150 0 0 1 175.075001 30.081 L 175.075 59.1 A 121 121 0 0 0 97.993291 91.028289 Z',
    'M 184.925 70.11 A 110 110 0 0 1 254.221463 98.813535 L 231.558691 121.476307 A 78 78 0 0 0 184.925 102.16 Z',
    'M 261.186465 105.778537 A 110 110 0 0 1 289.89 175.075 L 257.84 175.075 A 78 78 0 0 0 238.523693 128.441309 Z',
    'M 289.89 184.925 A 110 110 0 0 1 261.186465 254.221463 L 238.523693 231.558691 A 78 78 0 0 0 257.84 184.925 Z',
    'M 254.221463 261.186465 A 110 110 0 0 1 184.925 289.89 L 184.925 257.84 A 78 78 0 0 0 231.558691 238.523693 Z',
    'M 175.075 289.89 A 110 110 0 0 1 105.778537 261.186465 L 128.441309 238.523693 A 78 78 0 0 0 175.075 257.84 Z',
    'M 98.813535 254.221463 A 110 110 0 0 1 70.11 184.925 L 102.16 184.925 A 78 78 0 0 0 121.476307 231.558691 Z',
    'M 70.11 175.075 A 110 110 0 0 1 98.813535 105.778537 L 121.476307 128.441309 A 78 78 0 0 0 102.16 175.075 Z',
    'M 105.778537 98.813535 A 110 110 0 0 1 175.075 70.11 L 175.075 102.16 A 78 78 0 0 0 128.441309 121.476307 Z',
)


def sector_levels(elapsed, step=.36):
    """Outer: top-right clockwise. Inner: top-left counterclockwise.

    A sector fades in for one step, stays lit for two, then fades out while
    the fourth one appears. The very first sector starts fully lit.
    """
    phase = max(0., float(elapsed)) / step
    head = math.floor(phase)
    outer, inner = [0.] * 8, [0.] * 8
    for index in range(max(0, head-3), head+1):
        age = phase-index
        smooth = lambda x: x*x*(3-2*x)
        level = (1. if index == 0 or age >= 1 else smooth(age)) if age < 3 else 1-smooth(age-3)
        outer[index % 8] = level
        inner[(7-index) % 8] = level
    return outer, inner


@lru_cache(maxsize=24)
def _render_layers(side, pixel_ratio, light, accent=None):
    """Cache vector renders at the widget's actual display resolution."""
    off, lit = (207, 66) if light else (48, 189)
    outline=f'#{lit:02x}{lit:02x}{lit:02x}'
    active=accent or outline

    def render(paths, colour, contour):
        contours = ''.join(f'<path d="{path}"/>' for path in paths)
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="28 28 304 304">'
               f'<g fill="{colour}" stroke="{contour}" '
               f'stroke-width="1.6" stroke-linejoin="round">{contours}</g></svg>')
        pixels = math.ceil(side * pixel_ratio)
        image = QImage(pixels, pixels, QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(pixel_ratio)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing)
            QSvgRenderer(svg.encode('utf-8')).render(painter, QRectF(0, 0, side, side))
        finally:
            painter.end()
        return QPixmap.fromImage(image)

    return (render(_SECTORS, f'#{off:02x}{off:02x}{off:02x}', outline), render(_SECTORS, active, active),
            tuple(render((path,), active, active) for path in _SECTORS))


class OrbitalLoader(QAbstractButton):
    def __init__(self, parent=None, *, size=20, state='running'):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.state = ''
        self._started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self.update)
        theme_manager.theme_changed.connect(self.update)
        self.set_state(state)

    def sizeHint(self):
        return QSize(20, 20)

    def set_state(self, state):
        if state not in {'running', 'completed', 'interrupted'}:
            raise ValueError('Unknown orbital state: ' + str(state))
        if state == self.state:return
        self.state = state
        if state == 'running':self._started = time.monotonic()
        self._timer.setInterval(25)
        if state == 'running' and self.isVisible():self._timer.start()
        else:self._timer.stop()
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self.state == 'running':self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        side = min(self.width(), self.height())
        origin = QPointF((self.width()-side)/2, (self.height()-side)/2)
        light = theme_manager.current['foundation'] == 'light'
        accent=theme_manager.color('accent.primary').name()
        off, completed, sectors = _render_layers(side, self.devicePixelRatioF(), light, accent)
        painter.drawPixmap(origin, completed if self.state == 'completed' else off)
        if self.state == 'running':
            outer, inner = sector_levels(time.monotonic()-self._started)
            for pixmap, level in zip(sectors, outer+inner):
                if level > 0:
                    painter.setOpacity(level)
                    painter.drawPixmap(origin, pixmap)
