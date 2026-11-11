"""One application event filter for Ctrl-hover links, including proxy widgets."""
import weakref

from PySide6.QtCore import QObject, QEvent, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


class LinkCursorManager(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.targets = weakref.WeakKeyDictionary()
        self.current = None
        self.position = None
        self.overrides = []
        self.control = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self._update(self.control))
        app.installEventFilter(self)

    def register(self, viewport, resolver):
        viewport.setMouseTracking(True)
        entry = (weakref.ref(viewport), weakref.WeakMethod(resolver))
        self.targets[viewport] = entry
        # Qt forwards unconsumed viewport moves to the owning scroll view.
        # Both events represent the same target, not leaving the link.
        self.targets[viewport.parentWidget()] = entry

    def _restore(self):
        for reference, cursor in self.overrides:
            target = reference()
            if target is not None and isValid(target):
                target.setCursor(cursor)
        self.overrides.clear()

    def _update(self, control):
        widget = self.current() if self.current else None
        if widget is None or not isValid(widget) or not widget.isVisible():
            self._restore()
            return
        entry = self.targets.get(widget)
        resolver = entry[1]() if entry else None
        link = resolver(self.position) if control and resolver and self.position is not None else None
        extra = []
        if isinstance(link, tuple):
            link, extra = link
        if link:
            targets = list(dict.fromkeys([widget, *extra]))
            if [ref() for ref, _ in self.overrides] != targets:
                self._restore()
                self.overrides = [(weakref.ref(target), QCursor(target.cursor())) for target in targets]
            for target in targets:
                target.setCursor(Qt.PointingHandCursor)
        else:
            self._restore()

    def _target_at(self, widget):
        while widget is not None and widget not in self.targets:
            widget = widget.parentWidget()
        return self.targets[widget][0]() if widget is not None else None

    def _select_target(self, target):
        current = self.current() if self.current else None
        if target is not current:
            self._restore()
            self.current = weakref.ref(target) if target else None

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.MouseMove and isinstance(watched, QWidget):
            target = self._target_at(watched)
            self._select_target(target)
            self.position = event.globalPosition().toPoint()
            self.control = bool(event.modifiers() & Qt.ControlModifier)
            self._update(self.control)
            if target is not None:
                # Native text widgets/proxies also adjust their cursor during
                # mouse dispatch. Reapply after that dispatch, once per turn.
                self.timer.start(0)
        elif kind in {QEvent.KeyPress, QEvent.KeyRelease} and event.key() == Qt.Key_Control:
            # A modifier change must update a stationary pointer as well.
            if self.position is not None:
                widget = QApplication.widgetAt(self.position)
                if widget is not None:
                    self._select_target(self._target_at(widget))
            self.control = kind == QEvent.KeyPress
            self._update(self.control)
            self.timer.start(0)
        elif kind == QEvent.Leave and self.current and watched is self.current():
            self._restore()
            self.current = None
        elif kind == QEvent.ApplicationDeactivate:
            self.control = False
            self._restore()
        return False


_manager = None


def register_link_cursor(viewport, resolver):
    global _manager
    if _manager is None:
        _manager = LinkCursorManager(QApplication.instance())
    _manager.register(viewport, resolver)
