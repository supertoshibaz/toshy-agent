"""Bounded, cooperative overview for the Endless canvas."""
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, set_text
from ui.icons import set_icon


import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .raster import background_colour
from .storage import TILE_SIZE, load_tiles
from .toolbox import ColourWheel




def overview_bounds(meta):
    bounds = meta.get("content_bounds")
    if not bounds:
        return QRectF(-128, -128, 256, 256)
    rect = QRectF(*bounds)
    side = max(16.0, rect.width(), rect.height()) * 1.12
    center = rect.center()
    return QRectF(center.x() - side / 2, center.y() - side / 2, side, side)


def overview_keys(meta, bounds):
    if not meta.get("tile_count"):
        return []
    level = min(
        meta["max_level"], max(0, math.ceil(math.log2(bounds.width() / TILE_SIZE)))
    )
    span = TILE_SIZE * 2**level
    return [
        (level, x, y)
        for y in range(
            math.floor(bounds.top() / span), math.ceil(bounds.bottom() / span)
        )
        for x in range(
            math.floor(bounds.left() / span), math.ceil(bounds.right() / span)
        )
    ]


class MiniMap(QWidget):
    center_requested = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(176, 176)
        watch_paint(self)
        self.bounds = QRectF(-128, -128, 256, 256)
        self.image = None
        self.colour = QColor("white")
        self.canvas = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#d0d0d0"))
        painter.fillRect(self.rect(), self.colour)
        if self.image is not None:
            painter.drawImage(self.rect(), self.image)
        if self.canvas is not None:
            view = QRectF(
                self.canvas.world_at(QPointF()),
                self.canvas.world_at(
                    QPointF(self.canvas.width(), self.canvas.height())
                ),
            )
            scale = self.width() / self.bounds.width()
            target = QRectF(
                (view.x() - self.bounds.x()) * scale,
                (view.y() - self.bounds.y()) * scale,
                view.width() * scale,
                view.height() * scale,
            )
            ratio = self.devicePixelRatioF()
            snap = lambda value: (round(value * ratio) + 0.5) / ratio
            target = QRectF(
                QPointF(snap(target.left()), snap(target.top())),
                QPointF(snap(target.right()), snap(target.bottom())),
            )
            pen = QPen(QColor("#38bdf8"), 1.0)
            pen.setCosmetic(True)
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(pen)
            painter.setBrush(QColor(56, 189, 248, 24))
            painter.drawRect(target)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _navigate(self, position):
        position.setX(max(0, min(self.width(), position.x())))
        position.setY(max(0, min(self.height(), position.y())))
        self.center_requested.emit(
            self.bounds.topLeft() + position * (self.bounds.width() / self.width())
        )

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._navigate(event.position())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._navigate(event.position())


class BackgroundPicker(QFrame):
    colour_selected = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("PaintBackgroundPicker")
        apply_style(self, 'QFrame#PaintBackgroundPicker {background:@background.panel@; border:1px solid @border.subtle@; border-radius:8px;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        self.wheel = ColourWheel(self)
        layout.addWidget(self.wheel, 0, Qt.AlignHCenter)
        self.hex = QLineEdit(self)
        self.hex.setMaxLength(7)
        self.hex.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        self.apply_button = QToolButton(self)
        self.apply_button.setFixedSize(26, 26)
        set_icon(self.apply_button, 'apply', size=18)
        set_text(self.apply_button, 'ui.navigation.apply_background_colour_ff8e', setter='setToolTip')
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.hex, 1)
        row.addWidget(self.apply_button)
        layout.addLayout(row)
        self.wheel.changed.connect(lambda colour: bind_caption(self.hex, colour.name()))
        self.hex.editingFinished.connect(self._hex_changed)
        self.hex.returnPressed.connect(self.apply)
        self.hide()

    def _hex_changed(self):
        colour = QColor(self.hex.text())
        if colour.isValid():
            self.wheel.set_colour(colour)
        else:
            bind_caption(self.hex, self.wheel.colour.name())

    def open(self, colour):
        self.wheel.set_colour(colour)
        self.show()

    def apply(self):
        self._hex_changed()
        self.colour_selected.emit(QColor(self.wheel.colour))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.apply()
        elif event.key() == Qt.Key_Escape:
            self.parentWidget().close_picker()
        else:
            super().keyPressEvent(event)


class NavigationPanel(QFrame):
    settings_requested = Signal(object, object)

    def __init__(self, parent, io):
        super().__init__(parent)
        self.io = io
        self.canvas = None
        self._picker_canvas = None
        self._serial = 0
        self._job = None
        self._images = {}
        self._pending = []
        self._source = None
        self._bounds = QRectF()
        self._continuation = False
        self.setObjectName("PaintNavigationPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(self, 'QFrame#PaintNavigationPanel {background:@background.panel@; border:1px solid @border.subtle@; border-radius:8px;} QToolButton {color:@text.primary@; background:@background.surface@; border:1px solid @border.subtle@; border-radius:4px;} QToolButton:checked {background:@control.selected@; border-color:@accent.primary@;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.minimap = MiniMap(self)
        self.minimap.center_requested.connect(self._center)
        layout.addWidget(self.minimap)
        self.viewport_size = QLabel(self)
        self.viewport_size.setAlignment(Qt.AlignCenter)
        set_text(self.viewport_size, 'ui.navigation.current_view_width_height_in_canvas_ec3d', setter='setToolTip')
        apply_style(self.viewport_size, "color:@text.primary@; background:transparent;")
        layout.addWidget(self.viewport_size)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.center_button = self._button(
            "center", "Return to origin", lambda: self._center(QPointF())
        )
        self.axes_button = self._button(
            "axes",
            "Show axes",
            lambda checked: self.settings_requested.emit(
                self.canvas, {"show_axes": checked}
            ),
            True,
        )
        self.grid_button = self._button(
            "grid",
            "Show dotted grid",
            lambda checked: self.settings_requested.emit(
                self.canvas, {"show_grid": checked}
            ),
            True,
        )
        self.background_button = self._button(
            "background", "Canvas background colour", self._background, True
        )
        for button in (
            self.center_button,
            self.axes_button,
            self.grid_button,
            self.background_button,
        ):
            row.addWidget(button)
        layout.addLayout(row)
        self.picker = BackgroundPicker(self)
        layout.addWidget(self.picker)
        self.picker.colour_selected.connect(self._apply_background)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self.adjustSize()
        self.hide()

    def _button(self, text, tooltip, callback, checkable=False):
        button = QToolButton(self)
        button.setFixedSize(38, 38)
        set_icon(button, text, size=20)
        bind_caption(button, tooltip, setter='setToolTip')
        button.setCheckable(checkable)
        button.clicked.connect(callback)
        return button

    def position(self):
        self.move(max(0, self.parentWidget().width() - self.width() - 12), 12)

    def _center(self, point):
        if self.canvas is not None:
            self.canvas.center = QPointF(point)
            self.canvas._changed()

    def _background(self):
        if not self.picker.isHidden():
            self.close_picker()
        elif self.canvas is not None:
            self._picker_canvas = self.canvas
            self.picker.open(background_colour(self.canvas.meta))
            self.background_button.setChecked(True)
            self._resize_panel()

    def _resize_panel(self):
        self.layout().activate()
        self.adjustSize()
        self.position()

    def close_picker(self):
        self.picker.hide()
        self.background_button.setChecked(False)
        self._picker_canvas = None
        self._resize_panel()

    def _apply_background(self, colour):
        canvas = self._picker_canvas
        self.close_picker()
        if canvas is not None:
            self.settings_requested.emit(canvas, {"background": colour.name()})

    def set_canvas(self, canvas):
        if canvas is self.canvas:
            return
        self.close_picker()
        self.pause()
        self.canvas = canvas
        self.minimap.canvas = canvas
        self.minimap.image = None
        self.camera_changed()
        self.setVisible(canvas is not None)
        if canvas is not None:
            self.position()
            self.raise_()
            self.refresh()

    def pause(self):
        self._serial += 1
        self._timer.stop()
        if self._job is not None:
            self.io.cancel(self._job)
            self._job = None
        self._images.clear()
        self._pending.clear()
        self._continuation = False

    def refresh(self):
        if self.canvas is None or not self.canvas.meta:
            return
        self.update_settings()
        self.pause()
        self._timer.start(60)
        self.minimap.update()

    def update_settings(self):
        if self.canvas is None or not self.canvas.meta:
            return
        meta = self.canvas.meta
        self.minimap.colour = background_colour(meta)
        for button, key in (
            (self.axes_button, "show_axes"),
            (self.grid_button, "show_grid"),
        ):
            button.setChecked(bool(meta.get(key, False)))

    def _tick(self):
        if self._continuation:
            self._slice()
        else:
            self._start()

    def camera_changed(self):
        if self.canvas is None:
            self.viewport_size.clear()
        else:
            width = max(1, round(self.canvas.width() / self.canvas.zoom))
            height = max(1, round(self.canvas.height() / self.canvas.zoom))
            bind_caption(self.viewport_size, f"{width} × {height}")
        self.minimap.update()

    def _start(self):
        canvas = self.canvas
        if canvas is None or canvas._stroke_id is not None:
            return
        self._bounds = overview_bounds(canvas.meta)
        self._source = canvas.path
        self._pending = overview_keys(canvas.meta, self._bounds)
        self._continuation = True
        self._slice()

    def _slice(self):
        if not self._pending:
            self._publish()
            return
        serial, source = self._serial, self._source
        keys = list(self._pending)

        def complete(result, error):
            if serial != self._serial:
                return
            self._job = None
            if error:
                self._images.clear()
                return
            for key, image in result:
                self._images[key] = image
                self._pending.remove(key)
            # Yield between slices; a new stroke cancels this request.
            self._timer.start(12)

        self._job = self.io.submit(
            lambda cancel: load_tiles(source, keys, cancel, budget_ms=8),
            complete,
            priority=1,
        )

    def _publish(self):
        self._continuation = False
        image = QImage(176, 176, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        scale = 176 / self._bounds.width()
        for (level, x, y), tile in self._images.items():
            if tile is not None:
                span = TILE_SIZE * 2**level
                painter.drawImage(
                    QRectF(
                        (x * span - self._bounds.x()) * scale,
                        (y * span - self._bounds.y()) * scale,
                        span * scale,
                        span * scale,
                    ),
                    tile,
                )
        painter.end()
        self.minimap.image = image
        self.minimap.bounds = self._bounds
        self._images.clear()
        self.minimap.update()
