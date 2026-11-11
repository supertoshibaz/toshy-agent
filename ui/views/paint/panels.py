"""Canvas creation overlay and an edge-attached/floating tools panel."""
from ui.styles.binding import watch_paint
from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, localize, set_text
from ui.styles.binding import apply_style
from ui.icons import set_icon


from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
    QScrollArea,
)

from .storage import MAX_DIMENSION

# ISO A-series, millimetres converted to pixels at 300 DPI.
PRESETS = [
    (f"A{i}", round(w / 25.4 * 300), round(h / 25.4 * 300))
    for i, w, h in [
        (4, 210, 297),
        (5, 148, 210),
        (6, 105, 148),
    ]
]
PRESETS += [(f"{n}×{n}", n, n) for n in (256, 512, 1024, 2048, 4096)]


class CanvasSettings(QWidget):
    create_requested = Signal(str, int, int)
    image_requested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("PaintCanvasSettingsOverlay")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        self.panel = QFrame(self)
        self.panel.setFixedWidth(360)
        self.panel.setObjectName("PaintCanvasSettings")
        apply_style(self.panel, 'QFrame#PaintCanvasSettings {background:@background.panel@; border:1px solid @border.subtle@; border-radius:8px;} QLabel {color:@text.primary@; background:transparent;}')
        layout.addWidget(self.panel, 0, Qt.AlignCenter)
        fields = QVBoxLayout(self.panel)
        fields.setContentsMargins(20, 18, 20, 18)
        fields.setSpacing(12)
        title = localize(QLabel('', self.panel), 'ui.panels.new_canvas_f32b')
        fields.addWidget(title)
        self.preset = QToolButton(self.panel)
        set_text(self.preset, 'ui.panels.custom_d024')
        self.preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.preset.setMinimumHeight(30)
        self.preset.clicked.connect(self._menu)
        fields.addWidget(self.preset)
        self.mode = "finite"
        self.width_spin, self.height_spin = QSpinBox(), QSpinBox()
        self.width_field, self.height_field = QStackedWidget(), QStackedWidget()
        for label, spin, stack in [
            ("Width", self.width_spin, self.width_field),
            ("Height", self.height_spin, self.height_field),
        ]:
            spin.setRange(1, MAX_DIMENSION)
            spin.setValue(1024)
            bind_caption(spin, " px", setter='setSuffix')
            spin.valueChanged.connect(self._custom)
            stack.addWidget(spin)
            infinity = QLineEdit("∞")
            infinity.setReadOnly(True)
            infinity.setEnabled(False)
            infinity.setAlignment(Qt.AlignCenter)
            stack.addWidget(infinity)
            row = QHBoxLayout()
            row.addWidget(bind_caption(QLabel(''), label), 1)
            row.addWidget(stack, 2)
            fields.addLayout(row)
        row = QHBoxLayout()
        cancel = localize(QPushButton(''), 'ui.panels.cancel_1976')
        cancel.clicked.connect(self.hide)
        create = localize(QPushButton(''), 'ui.panels.create_4759')
        create.clicked.connect(
            lambda: self.create_requested.emit(
                self.mode, self.width_spin.value(), self.height_spin.value()
            )
        )
        row.addWidget(cancel)
        row.addWidget(create)
        fields.addLayout(row)
        self.hide()
        self._active_menu = None

    def _custom(self, *_):
        self.mode = "finite"
        self.width_field.setCurrentIndex(0)
        self.height_field.setCurrentIndex(0)
        set_text(self.preset, 'ui.panels.custom_d024')

    def select_preset(self, name, width, height):
        self._custom()
        self.width_spin.blockSignals(True)
        self.height_spin.blockSignals(True)
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)
        self.width_spin.blockSignals(False)
        self.height_spin.blockSignals(False)
        bind_caption(self.preset, (f"{name} ({width}×{height})" if name.startswith("A") else name) + " ▾")

    def select_endless(self):
        self.mode = "endless"
        self.width_field.setCurrentIndex(1)
        self.height_field.setCurrentIndex(1)
        set_text(self.preset, 'ui.panels.endless_030a')

    def _menu(self):
        if self._active_menu:
            self._active_menu.close()
            self._active_menu.deleteLater()
        menu = QMenu(self.preset)
        menu.setObjectName("FileManagerContextMenu")
        localize(add_caption_action(menu, 'addAction', "Custom", self._custom), 'ui.panels.custom_494c')
        presets = localize(add_caption_action(menu, 'addMenu', "Preset"), 'ui.panels.preset_7252', setter='setTitle')
        for name, width, height in PRESETS:
            add_caption_action(presets, 'addAction', f"{name} ({width}×{height} px)" if name.startswith("A") else name, lambda n=name, w=width, h=height: self.select_preset(n, w, h))
        localize(add_caption_action(menu, 'addAction', "Endless", self.select_endless), 'ui.panels.endless_adf7')
        localize(add_caption_action(menu, 'addAction', "Open Image…", self.image_requested.emit), 'ui.panels.open_image_5213')
        self._active_menu = menu
        menu.popup(self.preset.mapToGlobal(QPoint(0, self.preset.height())))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        self.panel.setFixedWidth(min(360, max(200, self.width() - 24)))
        super().resizeEvent(event)


class _PanelHandle(QToolButton):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._press = None
        self._origin = None
        self._dragged = False
        self.setObjectName('PaintPanelHandle')
        set_icon(self, 'grip', size=18)
        apply_style(self, 'QToolButton#PaintPanelHandle {padding:0;background:@background.surface@;border:1px solid @border.subtle@;border-radius:5px;} QToolButton#PaintPanelHandle:hover {background:@control.selected@;border-color:@accent.primary@;}')
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        set_text(self, 'ui.panels.detach_attach_tools_panel_drag_here_af61', setter='setToolTip')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._origin = self.panel.pos()
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and self.panel.floating:
            delta = event.globalPosition().toPoint() - self._press
            if delta.manhattanLength() > 4:
                self._dragged = True
                self.panel.move_clamped(self._origin + delta)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None:
            self._press = None
            self.setDown(False)
            if not self._dragged:
                self.panel.toggle_floating()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ToolsPanel(QFrame):
    EDGE_WIDTH = 25

    def __init__(self, parent, preferences_path=None):
        super().__init__(parent)
        watch_paint(self)
        self.setObjectName("PaintToolsPanel")
        self.setFixedSize(252, 700)
        apply_style(self, 'QFrame#PaintToolsPanel {background:transparent; border:none;} QLabel {color:@text.primary@; background:transparent;} QToolButton {border-radius:5px; padding:3px; color:@text.primary@;} QToolButton:hover {background:@control.hover@;}')
        self.floating = False
        self.expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 28, 10)
        layout.setSpacing(6)
        self.handle = _PanelHandle(self)
        layout.addWidget(self.handle)
        from .toolbox import ToolControls

        self.controls = ToolControls(self, preferences_path)
        self.controls.installEventFilter(self)
        scroll = QScrollArea(self)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.controls)
        layout.addWidget(scroll, 1)
        self._animation = QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(140)
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.timeout.connect(self._check_leave)
        self.position()

    def toggle_floating(self):
        self._animation.stop()
        self.floating = not self.floating
        self.handle.setCursor(
            Qt.SizeAllCursor if self.floating else Qt.PointingHandCursor
        )
        if self.floating:
            self.expanded = True
            self.move_clamped(QPoint(16, max(0, self.y())))
        else:
            self.expanded = False
            self.position()
        self.update()

    def eventFilter(self, watched, event):
        if watched is self.controls and event.type() == QEvent.LayoutRequest:
            self.position()
        return super().eventFilter(watched, event)

    def move_clamped(self, point):
        bounds = self.parentWidget().rect()
        self.move(
            max(0, min(point.x(), max(0, bounds.width() - self.width()))),
            max(0, min(point.y(), max(0, bounds.height() - self.height()))),
        )

    def position(self):
        self._animation.stop()
        margins = self.layout().contentsMargins()
        content_height = (
            self.controls.sizeHint().height()
            + self.handle.height()
            + margins.top()
            + margins.bottom()
            + self.layout().spacing()
        )
        self.setFixedHeight(
            max(180, min(content_height, self.parentWidget().height() - 2))
        )
        if self.floating:
            self.move_clamped(self.pos())
        else:
            self.move(
                0 if self.expanded else -self.width() + self.EDGE_WIDTH,
                max(0, (self.parentWidget().height() - self.height()) // 2),
            )

    def expand(self, expanded):
        if self.floating:
            return
        if not expanded:
            self.controls.commit_inputs()
        self.expanded = expanded
        self._animation.stop()
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(
            QPoint(
                0 if expanded else -self.width() + self.EDGE_WIDTH,
                max(0, (self.parentWidget().height() - self.height()) // 2),
            )
        )
        self._animation.start()

    def hideEvent(self, event):
        self.controls.commit_inputs()
        super().hideEvent(event)

    def showEvent(self, event):
        self.position()
        super().showEvent(event)

    def enterEvent(self, event):
        self._leave_timer.stop()
        self.expand(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._leave_timer.start(180)
        super().leaveEvent(event)

    def _check_leave(self):
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self.expand(False)

    def paintEvent(self, event):
        # Match the pipeline cards' body, header band and continuous outline.
        # The attached edge is square and extends to the workspace boundary.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
        shape = QPainterPath()
        radius = 8.0
        if self.floating:
            shape.addRoundedRect(rect, radius, radius)
        else:
            left, top, right, bottom = -1.0, rect.top(), rect.right(), rect.bottom()
            shape.moveTo(left, top)
            shape.lineTo(right - radius, top)
            shape.quadTo(right, top, right, top + radius)
            shape.lineTo(right, bottom - radius)
            shape.quadTo(right, bottom, right - radius, bottom)
            shape.lineTo(left, bottom)
            shape.closeSubpath()
        painter.fillPath(shape, theme_manager.color('background.panel'))
        painter.save()
        painter.setClipPath(shape)
        painter.fillRect(
            QRectF(self.width() - self.EDGE_WIDTH, 0, self.EDGE_WIDTH, self.height()),
            theme_manager.color('background.surface'),
        )
        painter.restore()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1.2))
        painter.drawPath(shape)
