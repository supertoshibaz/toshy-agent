"""Compact native value controls and stacked tab overflow arrows."""
from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QSize, Slot, QRect
from PySide6.QtGui import QRegion
from shiboken6 import isValid
from PySide6.QtWidgets import (QApplication, QTabBar, QToolButton, QSpinBox,
    QDoubleSpinBox, QAbstractSpinBox, QTextEdit, QPlainTextEdit, QStylePainter, QStyleOptionTab, QStyle)
from ui.icons import set_icon
from ui.i18n import set_text
from ui.styles.binding import apply_style


class NumericButtons(QObject):
    def __init__(self, spin):
        super().__init__(spin)
        self.spin = spin
        spin._toshy_numeric_buttons = self
        spin.setMinimumWidth(max(spin.minimumWidth(), 82 if isinstance(spin, QDoubleSpinBox) else 72))
        spin.setProperty('toshyNumericButtons', True)
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.reflow_timer = QTimer(self)
        self.reflow_timer.setSingleShot(True)
        self.reflow_timer.timeout.connect(self.refresh)
        self.editor = spin.lineEdit()
        self.editor.setStyleSheet('QLineEdit {background:transparent;border:0;padding:0;}')
        self.editor.installEventFilter(self)
        self.buttons = []
        for name, step in [('up', 1), ('down', -1)]:
            button = QToolButton(spin)
            button.setObjectName('NumericStepButton')
            button.setFocusPolicy(Qt.NoFocus)
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(80)
            set_text(button, 'ui.value.increase' if step > 0 else 'ui.value.decrease', setter='setToolTip')
            set_icon(button, name, size=12)
            apply_style(button, 'QToolButton {background:transparent;border:0;border-radius:3px;padding:0;} QToolButton:hover {background:@control.hover@;} QToolButton:pressed {background:@control.pressed@;}')
            button.clicked.connect(lambda _checked=False, delta=step: self.step(delta))
            self.buttons.append(button)
            button.show()
        spin.installEventFilter(self)
        for name in ('valueChanged', 'editingFinished'):
            signal = getattr(spin, name, None)
            if signal is not None:
                signal.connect(self.refresh)
        self.refresh()
        self.reflow_timer.start(0)

    def step(self, delta):
        self.spin.setFocus(Qt.OtherFocusReason)
        self.spin.interpretText()
        self.spin.stepBy(delta)
        self.refresh()

    def refresh(self, *_):
        spin = self.spin
        self.editor.setGeometry(6, 1, max(1, spin.width() - 44), max(1, spin.height() - 2))
        enabled = spin.stepEnabled()
        for index, button in enumerate(self.buttons):
            button.setGeometry(spin.width() - 37 + index * 18, 1, 18, max(1, spin.height() - 2))
            flag = QAbstractSpinBox.StepUpEnabled if index == 0 else QAbstractSpinBox.StepDownEnabled
            button.setEnabled(spin.isEnabled() and not spin.isReadOnly() and bool(enabled & flag))
            button.raise_()

    def eventFilter(self, watched, event):
        timer = getattr(self, 'reflow_timer', None)
        if timer is not None and isValid(timer) and event.type() in (QEvent.Resize, QEvent.EnabledChange, QEvent.ReadOnlyChange, QEvent.StyleChange, QEvent.Enter, QEvent.FocusIn):
            timer.start(0)
        return False


class TabOverflow(QObject):
    def __init__(self, bar):
        self._ready = False
        super().__init__(bar)
        self.bar = bar
        bar._toshy_tab_overflow = self
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.position)
        self._ready = True
        bar.installEventFilter(self)
        self.timer.start(0)

    def eventFilter(self, watched, event):
        # Qt can dispatch virtual events while the QObject is being built
        # or torn down. Never touch its children before initialization ends.
        if not getattr(self, '_ready', False):
            return False
        bar = self.bar
        if not isValid(bar) or not isValid(self.timer):
            return False
        if watched is bar and event.type() == QEvent.Paint:
            right = bar.findChild(QToolButton, 'ScrollRightButton')
            edge = right.x() if right is not None and right.isVisible() else bar.width()
            self.clip_tab_buttons(edge)
            if edge < bar.width() and not bar.property('toshyCustomTabPaint'):
                self.paint_tabs(right.x())
                return True
        if event.type() in (QEvent.Move, QEvent.Resize, QEvent.Show, QEvent.LayoutRequest, QEvent.StyleChange, QEvent.ChildAdded):
            self.timer.start(0)
        return False

    def clip_tab_buttons(self, right_edge):
        bar = self.bar
        center = getattr(bar,'center_close_buttons',None)
        if center is not None:center()
        viewport = QRect(0, 0, max(0, right_edge), bar.height())
        for index in range(bar.count()):
            tab = bar.tabRect(index)
            for side in (QTabBar.LeftSide, QTabBar.RightSide):
                button = bar.tabButton(index, side)
                if button is None or button is getattr(bar, '_drag_close', None):
                    continue
                visible = viewport.intersected(tab).intersected(button.geometry())
                if not bar.isTabVisible(index) or visible.isEmpty():
                    mask = QRegion(-2, -2, 1, 1)
                elif visible == button.geometry():
                    if not button.mask().isEmpty():button.clearMask()
                    continue
                else:
                    mask = QRegion(visible.translated(-button.pos()))
                if button.mask() != mask:
                    button.setMask(mask)

    def paint_tabs(self, right_edge):
        bar = self.bar
        painter = QStylePainter(bar)
        painter.setClipRect(0, 0, max(0, right_edge), bar.height())
        indices = [i for i in range(bar.count()) if i != bar.currentIndex()] + [bar.currentIndex()]
        for index in indices:
            if index < 0 or not bar.isTabVisible(index):
                continue
            rect = bar.tabRect(index)
            if rect.right() < 0 or rect.left() >= right_edge:
                continue
            option = QStyleOptionTab()
            bar.initStyleOption(option, index)
            painter.drawControl(QStyle.CE_TabBarTab, option)
        painter.end()

    @Slot()
    def position(self):
        if not getattr(self, '_ready', False):
            return
        bar = self.bar
        if not isValid(bar) or not bar.usesScrollButtons():
            return
        buttons = {button.objectName(): button for button in bar.findChildren(QToolButton, options=Qt.FindDirectChildrenOnly)}
        right, left = buttons.get('ScrollRightButton'), buttons.get('ScrollLeftButton')
        if right is None or left is None:
            return
        side = max(8, min(20, (bar.height() - 2) // 2))
        bar.setProperty('toshyOverflowWidth', side)
        top = (bar.height() - side * 2) // 2
        for index, (button, icon) in enumerate(((right, 'right'), (left, 'left'))):
            if not getattr(button, '_toshy_scroll_styled', False):
                button._toshy_scroll_styled = True
                button.installEventFilter(self)
                set_icon(button, icon, size=max(8, side - 5))
                apply_style(button, 'QToolButton {padding:0;border:1px solid @border.subtle@;border-radius:3px;background:@background.surface@;} QToolButton:hover {background:@control.hover@;border-color:@accent.primary@;}')
            button.setArrowType(Qt.NoArrow)
            button.setIconSize(QSize(max(8, side - 5), max(8, side - 5)))
            button.setGeometry(max(0, bar.width() - side), top + index * side, side, side)
            button.raise_()
        self.clip_tab_buttons(right.x() if right.isVisible() else bar.width())


class ControlLayouts(QObject):
    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Polish, QEvent.Show) and isinstance(watched, QTabBar):
            # QSS owns the tabs and separators. The native base can otherwise
            # leave a light line below document-mode and standalone tab bars.
            watched.setDrawBase(False)
        if event.type() == QEvent.Show:
            if isinstance(watched, (QTextEdit, QPlainTextEdit)) and watched.property('uiField') is None:
                # Document editors and display panes own their backgrounds.
                if not watched.isReadOnly() and watched.__class__ in (QTextEdit, QPlainTextEdit):
                    watched.setProperty('uiField', True)
                    watched.style().unpolish(watched)
                    watched.style().polish(watched)
            if isinstance(watched, (QSpinBox, QDoubleSpinBox)) and not hasattr(watched, '_toshy_numeric_buttons'):
                NumericButtons(watched)
            elif isinstance(watched, QTabBar) and not hasattr(watched, '_toshy_tab_overflow'):
                TabOverflow(watched)
        return False


def install_control_layouts(app):
    if not hasattr(app, '_toshy_control_layouts'):
        app._toshy_control_layouts = ControlLayouts(app)
        app.installEventFilter(app._toshy_control_layouts)
