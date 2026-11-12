"""Non-intercepting, theme-aware spotlight for onboarding actions.

Targets use live Qt widget geometries rather than screen coordinates. For tab
bars, only the individual tab rectangles are painted; for the Startup hub only
the central circular start button is highlighted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QTabBar, QWidget
from shiboken6 import isValid

from ui.themes.onboarding_theme import onboarding_colors
from ui.themes.manager import theme_manager


@dataclass
class SpotlightTarget:
    widget: QWidget
    tab: Optional[int] = None
    shape: str = 'rect'  # rect, outline, circle


class OnboardingHighlight(QWidget):
    """Transient transparent paint layer that never consumes mouse input."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setObjectName('OnboardingSpotlight')
        self.targets: list[SpotlightTarget] = []
        self.opacity = 0.0
        self._elapsed = 0
        self._dismissing = False
        self._interval_ms = 30
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._tick)
        theme_manager.theme_changed.connect(self.update)
        self.hide()

    def present(self, targets: list[SpotlightTarget]):
        """Replace the previous spotlight and show it for five seconds."""
        self._timer.stop()
        self.targets = [target for target in targets if target.widget is not None and isValid(target.widget)]
        self._elapsed = 0
        self._dismissing = False
        self.opacity = 0.0
        if not self.targets:
            self.hide()
            return
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._timer.start()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
            app.installEventFilter(self)
        self.update()

    def dismiss(self):
        if self.targets:
            self._dismissing = True
            self._elapsed = max(self._elapsed, 5000)
            self._timer.start()

    def clear(self):
        self._timer.stop()
        self.targets = []
        self.opacity = 0.0
        self.hide()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _tick(self):
        self._elapsed += self._interval_ms
        if self._dismissing or self._elapsed >= 5000:
            self.opacity = max(0.0, self.opacity - 0.13)
            if self.opacity <= 0:
                self.clear()
                return
        else:
            self.opacity = min(1.0, self.opacity + 0.18)
        if isValid(self.parentWidget()):
            self.setGeometry(self.parentWidget().rect())
        self.update()

    def _rect_for(self, target: SpotlightTarget):
        widget = target.widget
        if not isValid(widget) or not widget.isVisible() or widget.window() is not self.window():
            return None
        if target.tab is not None:
            if not isinstance(widget, QTabBar) or not (0 <= target.tab < widget.count()):
                return None
            if not widget.isTabVisible(target.tab):
                return None
            local = widget.tabRect(target.tab)
            # Ignore tabs clipped out of an overflowing scrollable tab bar.
            local = local.intersected(widget.rect())
        elif target.shape == 'circle':
            side = min(widget.width(), widget.height())
            radius = float(getattr(widget, 'r_btn', side / 2))
            local = QRectF(widget.width() / 2 - radius, widget.height() / 2 - radius,
                           radius * 2, radius * 2).toRect()
        else:
            local = widget.rect()
        if local.isEmpty():
            return None
        top_left = widget.mapTo(self, local.topLeft())
        return QRectF(top_left.x(), top_left.y(), local.width(), local.height())

    def paintEvent(self, _event):
        colors = onboarding_colors()
        accent = QColor(colors['outline'])
        fill = QColor(colors['highlight'])
        fill.setAlphaF(0.22 * self.opacity)
        outline = QColor(accent)
        outline.setAlphaF(0.90 * self.opacity)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for target in self.targets:
            rect = self._rect_for(target)
            if rect is None:
                continue
            rect = rect.adjusted(1.5, 1.5, -1.5, -1.5)
            path = QPainterPath()
            if target.shape == 'circle':
                path.addEllipse(rect)
            else:
                path.addRoundedRect(rect, 4, 4)
            painter.setPen(QPen(outline, 2.0))
            painter.setBrush(Qt.NoBrush if target.shape == 'outline' else fill)
            painter.drawPath(path)
        painter.end()

    def eventFilter(self, watched, event):
        if self.targets and event.type() == QEvent.MouseButtonRelease and isinstance(watched, QWidget):
            for target in self.targets:
                w = target.widget
                if not isValid(w):
                    continue
                # Only fade on a click inside the actual highlighted target.
                if target.tab is not None and watched is w:
                    if w.tabAt(event.position().toPoint()) == target.tab:
                        self.dismiss()
                        break
                elif target.shape == 'circle' and watched is w:
                    local = event.position()
                    radius = float(getattr(w, 'r_btn', min(w.width(), w.height()) / 2))
                    dx, dy = local.x() - w.width() / 2, local.y() - w.height() / 2
                    if dx * dx + dy * dy <= radius * radius:
                        self.dismiss()
                        break
                elif target.shape == 'rect' and (watched is w or w.isAncestorOf(watched)):
                    self.dismiss()
                    break
        return False
