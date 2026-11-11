"""One live tooltip policy for native help events and explicit showText calls."""
import json
from pathlib import Path
from weakref import ref

from PySide6.QtCore import QObject, QEvent, QPoint, QRect, QTimer, Signal, QSaveFile, QIODevice, Qt
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory, QToolTip, QWidget
from shiboken6 import isValid
from core.paths import APP_PATHS


class TooltipPreferences(QObject):
    changed = Signal()

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path or APP_PATHS.runtime / 'tooltips.json')
        self.enabled, self.delay_ms = True, 500
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.enabled = data.get('enabled', True) is not False
            self.delay_ms = max(0, min(5000, int(data.get('delay_ms', 500))))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def update(self, *, enabled=None, delay_ms=None):
        enabled = self.enabled if enabled is None else bool(enabled)
        delay_ms = self.delay_ms if delay_ms is None else max(0, min(5000, int(delay_ms)))
        target = QSaveFile(str(self.path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not target.open(QIODevice.WriteOnly):
            raise OSError(target.errorString())
        data = json.dumps({'enabled': enabled, 'delay_ms': delay_ms}, indent=2).encode('utf-8')
        if target.write(data) != len(data) or not target.commit():
            raise OSError(target.errorString())
        self.enabled, self.delay_ms = enabled, delay_ms
        self.changed.emit()


tooltip_preferences = TooltipPreferences()
_show_text = QToolTip.showText
_hide_text = QToolTip.hideText


class TooltipStyle(QProxyStyle):
    def __init__(self, preferences):
        super().__init__(QStyleFactory.create('Fusion'))
        self.preferences = preferences

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return self.preferences.delay_ms
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            return 0  # Moving between controls must also respect the chosen delay.
        return super().styleHint(hint, option, widget, returnData)


class TooltipController(QObject):
    def __init__(self, app, preferences):
        super().__init__(app)
        self.preferences = preferences
        self.pending = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._show_pending)
        self.style = TooltipStyle(preferences)
        app.setStyle(self.style)
        app.installEventFilter(self)
        preferences.changed.connect(self._changed)
        # Every module imports the same Qt class, so explicit canvas/image
        # tooltips obey the policy without per-widget replacements.
        QToolTip.showText = self.show_text
        QToolTip.hideText = self.hide_text

    def hide_text(self):
        self.timer.stop()
        self.pending = None
        _hide_text()

    def show_text(self, pos, text, w=None, rect=QRect(), msecShowTime=-1):
        if not text or not self.preferences.enabled:
            self.hide_text()
            return
        previous = self.pending
        owner = ref(w) if w is not None else None
        self.pending = (QPoint(pos), text, owner, QRect(rect), msecShowTime)
        if previous and previous[1] == text and (previous[2]() if previous[2] else None) is w:
            return  # Pointer motion within one hotspot does not restart its wait.
        self.timer.start(self.preferences.delay_ms)

    def _show_pending(self):
        request = self.pending
        if request is None or not self.preferences.enabled:
            return
        pos, text, owner, rect, duration = request
        widget = owner() if owner else None
        if owner and (widget is None or not isValid(widget) or not widget.isVisible()):
            self.pending = None
            return
        _show_text(pos, text, widget, rect, duration)

    def _changed(self):
        self.hide_text()
        if not self.preferences.enabled:
            for widget in QApplication.topLevelWidgets():
                if widget.windowType() == Qt.ToolTip:
                    widget.hide()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.ToolTip and not self.preferences.enabled:
            event.ignore()
            return True
        if kind == QEvent.Show and isinstance(watched, QWidget) and watched.windowType() == Qt.ToolTip:
            if not self.preferences.enabled:
                watched.hide()
                QTimer.singleShot(0, watched.hide)
                return True
        if kind in (QEvent.Leave, QEvent.Hide, QEvent.Destroy):
            owner = self.pending[2] if self.pending else None
            if owner and owner() is watched:
                self.hide_text()
        if kind in (QEvent.MouseButtonPress, QEvent.Wheel, QEvent.ApplicationDeactivate):
            self.hide_text()
        return False


def install_tooltips(app):
    if not hasattr(app, '_toshy_tooltips'):
        app._toshy_tooltips = TooltipController(app, tooltip_preferences)
    return app._toshy_tooltips
