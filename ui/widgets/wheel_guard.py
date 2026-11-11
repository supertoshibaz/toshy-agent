"""Require a continuous hover over one value row before changing it by wheel."""
from time import monotonic
from weakref import ref
from PySide6.QtCore import QObject, QEvent, QPointF, QPoint, QCoreApplication, Slot
from PySide6.QtGui import QWheelEvent,QCursor
from PySide6.QtWidgets import (QWidget,QAbstractSpinBox,QComboBox,QAbstractSlider,
    QScrollBar,QAbstractItemView,QAbstractScrollArea)
from shiboken6 import isValid


class ValueWheelGuard(QObject):
    def __init__(self, app, delay_ms=1000):
        super().__init__(app)
        self.delay = delay_ms / 1000
        self.forwarding = False
        self.active = None
        self.since = 0.0
        self.anchor = None

    @staticmethod
    def control(widget):
        while isinstance(widget,QWidget):
            if isinstance(widget,(QScrollBar,QAbstractItemView)):return None
            if isinstance(widget,(QAbstractSpinBox,QComboBox,QAbstractSlider)):return widget
            widget=widget.parentWidget()
        return None

    @staticmethod
    def row(widget):
        while isinstance(widget,QWidget):
            if widget.objectName() in {'StartupSettingRow','SettingRow'}:return widget
            widget=widget.parentWidget()
        return None

    @Slot()
    def reset(self):
        self.active=None
        self.since=0.0
        self.anchor=None

    def watch_scroll(self, widget):
        if not getattr(widget,'_wheel_hover_reset',False):
            widget._wheel_hover_reset=True
            widget.verticalScrollBar().valueChanged.connect(self.scroll_changed)
            widget.horizontalScrollBar().valueChanged.connect(self.scroll_changed)

    @Slot(int)
    def scroll_changed(self, value):
        scope=self.active() if self.active is not None else None
        if scope is None or not isValid(scope):
            self.reset()
            return
        owner=self.sender()
        while isinstance(owner,QWidget):
            if isinstance(owner,QAbstractScrollArea):
                if owner.isAncestorOf(scope):self.reset()
                break
            owner=owner.parentWidget()

    def enter(self, scope):
        anchor=(scope.mapToGlobal(QPoint()),scope.size())
        current=self.active() if self.active is not None else None
        if current is not scope or self.anchor != anchor:
            self.active=ref(scope)
            self.anchor=anchor
            self.since=monotonic()

    def forward(self, control, event):
        target=control.parentWidget()
        ancestor=target
        while ancestor is not None:
            if isinstance(ancestor,QAbstractScrollArea):
                self.watch_scroll(ancestor)
                target=ancestor.viewport()
                break
            ancestor=ancestor.parentWidget()
        if target is None:
            proxy=control.graphicsProxyWidget()
            if proxy is not None and proxy.scene() is not None:
                target=next((view.viewport() for view in proxy.scene().views() if view.isVisible()),None)
        if target is not None:
            forwarded=QWheelEvent(QPointF(target.mapFromGlobal(event.globalPosition().toPoint())),
                event.globalPosition(),event.pixelDelta(),event.angleDelta(),event.buttons(),
                event.modifiers(),event.phase(),event.inverted(),event.source(),event.pointingDevice())
            self.forwarding=True
            try:QCoreApplication.sendEvent(target,forwarded)
            finally:self.forwarding=False

    def eventFilter(self, watched, event):
        if self.forwarding:return False
        kind=event.type()
        if kind==QEvent.ApplicationDeactivate:self.reset()
        if not isinstance(watched,QWidget):return False
        if kind==QEvent.Show and isinstance(watched,QAbstractScrollArea):self.watch_scroll(watched)
        if kind not in (QEvent.Enter,QEvent.Leave,QEvent.Hide,QEvent.Wheel):return False
        control=self.control(watched)
        scope=self.row(watched) or control
        if scope is None:
            if kind in (QEvent.Enter,QEvent.Wheel):self.reset()
            return False
        if kind in (QEvent.Leave,QEvent.Hide):
            current=self.active() if self.active is not None else None
            if current is scope and (kind==QEvent.Hide or
                    not scope.rect().contains(scope.mapFromGlobal(QCursor.pos()))):self.reset()
        elif kind==QEvent.Enter:self.enter(scope)
        elif control is not None:
            if not isValid(scope) or not scope.rect().contains(scope.mapFromGlobal(event.globalPosition().toPoint())):
                self.reset()
            else:self.enter(scope)
            if self.active is None or monotonic()-self.since < self.delay:
                self.forward(control,event)
                self.reset()
                event.accept()
                return True
        return False


def install_wheel_guard(app):
    if not hasattr(app,'_toshy_wheel_guard'):
        app._toshy_wheel_guard=ValueWheelGuard(app)
        app.installEventFilter(app._toshy_wheel_guard)
        for widget in app.allWidgets():
            if isinstance(widget,QAbstractScrollArea):app._toshy_wheel_guard.watch_scroll(widget)
