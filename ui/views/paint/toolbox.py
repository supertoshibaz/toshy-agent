"""Compact paint controls: one colour, opacity/width and a layer list."""
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager
from ui.icons import set_icon, set_label_icon, get as get_icon


import math
import base64
import json
from pathlib import Path
import numpy as np

from PySide6.QtCore import (
    QPoint,
    QEvent,
    QEasingCurve,
    QPropertyAnimation,
    QParallelAnimationGroup,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
    QTimer,
    QSaveFile,
    QIODevice,
)
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QMenu, 
    QApplication,
    QStyledItemDelegate,
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QGraphicsOpacityEffect,
    QSizePolicy,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSlider, QStyle, QStyleOptionSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

TOOLS = [
    ("brush", tr('tools.8624ca0fcb')),
    ("eraser", tr('tools.686eee3c6c')),
    ("picker", tr('tools.01f082d10f')),
    ("fill", tr('tools.033955d469')),
    ("text", tr('tools.71988c4d8e')),
    ("select_rect", tr('tools.9fe4682e18')),
    ("select_lasso", tr('tools.e7ae927406')),
    ("move", tr('tools.2b6dbe7241')),
    ("transform", tr('tools.8a725a8693')),
    ("rectangle", tr('tools.0306996228')),
    ("ellipse", tr('tools.cb1ee8cb96')),
    ("line", tr('tools.d7852cd0d2')),
]

DEFAULT_SWATCHES = (
    '#000000', '#ffffff', '#e53935', '#fb8c00',
    '#fdd835', '#43a047', '#1e88e5', '#8e24aa',
)


def tool_icon(name):
    return get_icon(name)


class ColourWheel(QWidget):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(160, 160)
        self.colour = QColor("black")
        self.hue = 0.0
        self._image = None
        self._background_parts = None
        self._mode = None
        self._pending_pick = None
        self._pick_timer = QTimer(self)
        self._pick_timer.setSingleShot(True)
        self._pick_timer.setInterval(16)
        self._pick_timer.timeout.connect(self._flush_pick)
        self.setMouseTracking(True)

    def set_colour(self, colour, emit=True):
        self.colour = QColor(colour)
        if self.colour.hsvHueF() >= 0 and not math.isclose(self.colour.hsvHueF(), self.hue, abs_tol=1 / 65535):
            self.hue = self.colour.hsvHueF()
            self._image = None
        self.update()
        if emit:
            self.changed.emit(QColor(self.colour))

    TRIANGLE = (
        QPointF(80, 20),
        QPointF(80 - 30 * math.sqrt(3), 110),
        QPointF(80 + 30 * math.sqrt(3), 110),
    )

    def _weights(self, x, y):
        top = (110 - y) / 90
        black = (x - self.TRIANGLE[1].x()) / (60 * math.sqrt(3)) - top * 0.5
        return top, 1 - top - black, black

    def _background(self):
        scale = max(2, math.ceil(self.devicePixelRatioF() * 2))
        size = 160 * scale
        cached = self._background_parts
        if cached is None or cached[0] != scale:
            yy, xx = np.mgrid[:size, :size].astype(np.float32)
            x, y = (xx + 0.5) / scale, (yy + 0.5) / scale
            radius = np.hypot(x - 80, y - 80)
            phase = ((np.arctan2(y - 80, x - 80) / (2 * np.pi)) % 1) * 6
            sector = phase.astype(np.int32)
            fraction = phase - sector
            rgb = np.zeros((size, size, 3), np.float32)
            ramps = ((1, fraction, 0), (1-fraction, 1, 0), (0, 1, fraction),
                     (0, 1-fraction, 1), (fraction, 0, 1), (1, 0, 1-fraction))
            for i, channels in enumerate(ramps):
                mask = sector == i
                for j, value in enumerate(channels):
                    rgb[:, :, j][mask] = value[mask] if isinstance(value, np.ndarray) else value
            ring = np.minimum(np.clip((radius-66)*scale+.5, 0, 1),
                              np.clip((78-radius)*scale+.5, 0, 1))
            h, w, b = self._weights(x, y)
            triangle = np.clip(np.minimum(np.minimum(h, w), b)*90*scale+.5, 0, 1)
            inside = triangle > 0
            rgba = np.zeros((size, size, 4), np.uint8)
            rgba[:, :, :3] = np.rint(rgb*255).astype(np.uint8)
            rgba[:, :, 3] = np.rint(np.maximum(ring, triangle)*255).astype(np.uint8)
            cached = self._background_parts = scale, rgba, inside, h[inside, None], w[inside, None]
        _, base, inside, h, w = cached
        rgba = base.copy()
        colour = np.array(QColor.fromHsvF(self.hue, 1, 1).getRgbF()[:3], np.float32)
        rgba[inside, :3] = np.rint(np.clip(h*colour+w, 0, 1)*255).astype(np.uint8)
        image = QImage(rgba.data, size, size, size*4, QImage.Format_RGBA8888).copy()
        image.setDevicePixelRatio(scale)
        return image

    def paintEvent(self, event):
        if self._image is None or self._image.devicePixelRatio() != max(2, math.ceil(self.devicePixelRatioF() * 2)):
            self._image = self._background()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(0, 0, self._image)
        angle = self.hue * math.pi * 2
        p.setPen(QPen(Qt.white, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(
            QPointF(80 + 72 * math.cos(angle), 80 + 72 * math.sin(angle)), 4, 4
        )
        h = self.colour.hsvSaturationF() * self.colour.valueF()
        w = self.colour.valueF() - h
        b = 1 - h - w
        pos = self.TRIANGLE[0] * h + self.TRIANGLE[1] * w + self.TRIANGLE[2] * b
        p.setPen(QPen(Qt.black, 3))
        p.drawEllipse(pos, 3, 3)
        p.setPen(QPen(Qt.white, 1))
        p.drawEllipse(pos, 3, 3)

    def _pick(self, point):
        x, y = point.x(), point.y()
        if self._mode == "hue":
            self.hue = (math.atan2(y - 80, x - 80) / (math.pi * 2)) % 1
            self._image = None
            self.set_colour(
                QColor.fromHsvF(
                    self.hue, self.colour.hsvSaturationF(), self.colour.valueF()
                )
            )
        else:
            h, w, b = self._weights(x, y)
            weights = [max(0, h), max(0, w), max(0, b)]
            total = sum(weights) or 1
            h, w, b = [v / total for v in weights]
            v = h + w
            self.set_colour(QColor.fromHsvF(self.hue, h / v if v else 0, v))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            r = math.hypot(event.position().x() - 80, event.position().y() - 80)
            if 64 <= r <= 80:
                self._mode = "hue"
            elif min(self._weights(event.position().x(), event.position().y())) >= 0:
                self._mode = "triangle"
            else:
                return
            self._pick(event.position())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._mode:
            self._pending_pick = QPointF(event.position())
            if not self._pick_timer.isActive():
                self._pick_timer.start()
            event.accept()

    def _flush_pick(self):
        self._pick_timer.stop()
        point, self._pending_pick = self._pending_pick, None
        if point is not None and self._mode:
            self._pick(point)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._mode:
            self._pending_pick = QPointF(event.position())
            self._flush_pick()
            self._mode = None
            event.accept()


class _LayerDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # Index widgets render the complete row, including its active state.
        # Native selection/focus decorations otherwise leak at row corners.
        pass


class LayerList(QListWidget):
    reordered = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setItemDelegate(_LayerDelegate(self))
        self.setFocusPolicy(Qt.NoFocus)
        self._drag = None
        self._proxies = []
        self._animations = []
        self._drag_reset_timer = QTimer(self)
        self._drag_reset_timer.setSingleShot(True)
        self._drag_reset_timer.timeout.connect(self.clear_drag)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setFixedHeight(5 * 32 + 2 * self.frameWidth())
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model().rowsMoved.connect(self._moved)

    def clear_drag(self):
        self._drag_reset_timer.stop()
        self._drag = None
        for animation in self._animations:
            animation.stop()
            animation.deleteLater()
        self._animations.clear()
        QApplication.instance().removeEventFilter(self)
        for proxy in self._proxies:
            proxy.deleteLater()
        self._proxies.clear()
        for index in range(self.count()):
            widget = self.itemWidget(self.item(index))
            if widget:
                widget.show()

    def start_drag(self, identity, global_position):
        self.clear_drag()
        order = [self.item(i).data(Qt.UserRole) for i in range(self.count())]
        index = order.index(identity)
        rect = self.visualItemRect(self.item(index))
        selected = getattr(self,'group_getter',lambda:set())()
        group = [i for i,value in enumerate(order) if value in selected] if identity in selected else [index]
        self._drag = dict(
            identity=identity,
            order=order,
            group=group,
            index=index,
            offset=self.viewport().mapFromGlobal(global_position).y() - rect.y(),
        )
        for i in range(self.count()):
            widget = self.itemWidget(self.item(i))
            proxy = QLabel(self.viewport())
            proxy.setPixmap(widget.grab())
            proxy.setGeometry(self.visualItemRect(self.item(i)))
            proxy.setAttribute(Qt.WA_TransparentForMouseEvents)
            proxy.show()
            widget.hide()
            self._proxies.append(proxy)
        self._proxies[index].raise_()
        # Follow the pressed mouse within this application. Explicit grabs of
        # an ordinary widget are unsupported by Qt's Wayland platform plugin.
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._drag and not self._drag.get("finishing"):
            if event.type() == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                self.move_drag(event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.MouseMove and not event.buttons() & Qt.LeftButton:
                self.clear_drag()
            if (
                event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton
            ):
                self.finish_drag()
                return True
            if event.type() == QEvent.ApplicationDeactivate or (
                event.type() == QEvent.WindowDeactivate and watched is self.window()
            ):
                self.clear_drag()
        return super().eventFilter(watched, event)

    def move_drag(self, global_position):
        if not self._drag or self._drag.get("finishing"):
            return
        position = self.viewport().mapFromGlobal(global_position)
        bar = self.verticalScrollBar()
        if position.y() < 20:
            bar.setValue(bar.value() - 8)
        elif position.y() > self.viewport().height() - 20:
            bar.setValue(bar.value() + 8)
        drag = self._drag
        y = position.y() - drag["offset"] + bar.value()
        group = drag['group']
        offset = group.index(drag['index'])
        target = max(0,min(self.count()-len(group),round(y/32)-offset))
        order = [i for i in range(self.count()) if i not in group]
        order[target:target] = group
        drag['visual_order'] = order
        for animation in self._animations:
            animation.stop()
            animation.deleteLater()
        self._animations.clear()
        for slot, index in enumerate(order):
            proxy = self._proxies[index]
            if index in group:
                proxy.move(0,round(y-bar.value()+(group.index(index)-offset)*32))
            else:
                animation = QPropertyAnimation(proxy, b"pos", self)
                animation.setDuration(115)
                animation.setEasingCurve(QEasingCurve.OutCubic)
                animation.setStartValue(proxy.pos())
                animation.setEndValue(QPoint(0, slot * 32 - bar.value()))
                animation.start()
                self._animations.append(animation)
        drag["target"] = target

    def mouseMoveEvent(self, event):
        if self._drag:
            self.move_drag(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag:
            self.finish_drag()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def finish_drag(self):
        if not self._drag or self._drag.get("finishing"):
            return
        QApplication.instance().removeEventFilter(self)
        drag = self._drag
        drag["finishing"] = True
        visual = drag.get('visual_order',list(range(self.count())))
        if visual == list(range(self.count())):
            self.clear_drag()
            return
        order = [drag['order'][index] for index in visual]
        for animation in self._animations:
            animation.stop()
            animation.deleteLater()
        self._animations.clear()
        group = QParallelAnimationGroup(self)
        for slot, identity in enumerate(order):
            proxy = self._proxies[drag["order"].index(identity)]
            animation = QPropertyAnimation(proxy, b"pos", group)
            animation.setDuration(105)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            animation.setStartValue(proxy.pos())
            animation.setEndValue(
                QPoint(0, slot * 32 - self.verticalScrollBar().value())
            )
            group.addAnimation(animation)

        def finished():
            if group in self._animations:
                self._animations.remove(group)
            self._drag = None
            self.reordered.emit(list(reversed(order)))
            self._drag_reset_timer.start(1500)
            group.deleteLater()

        group.finished.connect(finished)
        self._animations.append(group)
        group.start()

    def _moved(self, *_):
        self.reordered.emit(
            [self.item(i).data(Qt.UserRole) for i in reversed(range(self.count()))]
        )


class BrushWidthInput(QDoubleSpinBox):
    def textFromValue(self, value):
        return self.locale().toString(value, 'f', 1).removesuffix(self.locale().decimalPoint() + '0')


class BrushWidthSlider(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 1000)
        watch_paint(self)

    @staticmethod
    def width_at(position):
        return round(1 + position * 99 / 500 if position <= 500 else 100 + (position - 500) * 400 / 500, 1)

    @staticmethod
    def position_for(width):
        return round((width - 1) * 500 / 99 if width <= 100 else 500 + (width - 100) * 500 / 400)

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        painter = QPainter(self)
        painter.setPen(QPen(theme_manager.color('text.secondary'), 1))
        x, y = groove.center().x(), groove.center().y()
        painter.drawLine(x, y - 5, x, y + 5)


class ToolControls(QWidget):
    tool_changed = Signal(str)
    colour_changed = Signal(object)
    opacity_changed = Signal(float)
    width_changed = Signal(float)
    active_changed = Signal(str)
    selection_changed = Signal(object)
    layer_command = Signal(object)

    def __init__(self, parent=None, preferences_path=None):
        super().__init__(parent)
        self.preferences_path = Path(preferences_path) if preferences_path else None
        self.default_swatches = list(DEFAULT_SWATCHES)
        self.swatches = list(self.default_swatches)
        self.active_swatch = 0
        state = {}
        if self.preferences_path and self.preferences_path.exists():
            try:
                state = json.loads(self.preferences_path.read_text())
                if not isinstance(state, dict):
                    raise ValueError("Invalid palette settings")
                colours = state.get("colours", [])
                if len(colours) == 8 and all(
                    isinstance(c, str) and QColor(c).isValid() for c in colours
                ):
                    self.swatches = [QColor(c).name() for c in colours]
                self.active_swatch = max(0, min(6, int(state.get("active", 0))))
            except (OSError, ValueError, TypeError):
                state = {}
        self.current_tool = str(state.get('tool') or 'brush')
        try:
            width = float(state.get('width',8))
            self._saved_width = max(1,min(500,width)) if math.isfinite(width) else 8
            self._saved_opacity = max(0,min(100,int(state.get('opacity',100))))
        except (ValueError,TypeError,OverflowError):self._saved_width,self._saved_opacity = 8,100
        self.text_defaults = state.get('text_defaults') or {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.flush_preferences)
        self._updating = False
        self.active = "paint"
        self.checked = set()
        self._layer_meta = []
        apply_style(self, 'QToolButton:checked {background:@control.selected@; border:1px solid @accent.primary@;} QCheckBox {background:transparent;border:none;} QCheckBox::indicator {width:12px; height:12px; border:1px solid @border.subtle@; border-radius:2px;} QCheckBox::indicator:checked {background:@accent.primary@;}')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignTop)
        self.wheel = ColourWheel()
        layout.addWidget(self.wheel, 0, Qt.AlignHCenter)
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(3)
        self.swatch_buttons = []
        for i in range(7):
            button = QToolButton()
            button.setFixedSize(20, 20)
            button.setCheckable(True)
            set_text(button, 'ui.toolbox.colour_v0_4bca', setter='setToolTip', v0=i + 1)
            button.clicked.connect(lambda _, index=i: self.select_swatch(index))
            self.swatch_buttons.append(button)
            swatch_row.addWidget(button)
        self.reset_swatches_btn = set_icon(QToolButton(self), 'refresh', size=16, square=True)
        self.reset_swatches_btn.setFixedSize(20, 20)
        apply_style(self.reset_swatches_btn, 'QToolButton {min-width:18px;max-width:18px;min-height:18px;max-height:18px;padding:0;background:@background.surface@;border:1px solid @border.subtle@;border-radius:3px;} QToolButton:hover {background:@control.hover@;border-color:@accent.primary@;}')
        set_text(self.reset_swatches_btn, 'ui.paint.reset_colors', setter='setToolTip')
        self.reset_swatches_btn.clicked.connect(self.reset_swatches)
        swatch_row.addWidget(self.reset_swatches_btn)
        self.wheel.set_colour(QColor(self.swatches[self.active_swatch]), False)
        self._refresh_swatches()
        theme_manager.theme_changed.connect(self._refresh_swatches)
        self.hex = QLineEdit(self.swatches[self.active_swatch])
        self.hex.setMaxLength(7)
        self.hex.setAlignment(Qt.AlignCenter)
        self.hex.editingFinished.connect(self._hex_changed)
        self.wheel.changed.connect(self._colour_changed)
        layout.addWidget(self.hex)
        layout.addLayout(swatch_row)
        opacity_group = QVBoxLayout()
        opacity_group.setSpacing(0)
        opacity_label = localize(QLabel(''), 'ui.toolbox.opacity_6062')
        opacity_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        opacity_group.addWidget(opacity_label)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setObjectName("PaintValueSlider")
        self.opacity.setRange(0, 100)
        self.opacity.setValue(self._saved_opacity)
        self.opacity_input = QSpinBox()
        self.opacity_input.setRange(0, 100)
        self.opacity_input.setValue(self._saved_opacity)
        self.opacity_input.setSuffix("")
        self.opacity.valueChanged.connect(self.opacity_input.setValue)
        self.opacity_input.valueChanged.connect(self.opacity.setValue)
        self.opacity.valueChanged.connect(
            lambda value: self.opacity_changed.emit(value / 100)
        )
        alpha_row = QHBoxLayout()
        alpha_row.addWidget(self.opacity, 1)
        alpha_row.addWidget(self.opacity_input)
        opacity_group.addLayout(alpha_row)
        layout.addLayout(opacity_group)
        width_group = QVBoxLayout()
        width_group.setSpacing(0)
        width_label = localize(QLabel(''), 'ui.toolbox.width_3019')
        width_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        width_group.addWidget(width_label)
        self.width = BrushWidthInput()
        self.width.setRange(1, 500)
        self.width.setDecimals(1)
        self.width.setValue(self._saved_width)
        self.width.setSuffix("")
        self.width.valueChanged.connect(self.width_changed.emit)
        self.width_slider = BrushWidthSlider()
        self.width_slider.setObjectName("PaintValueSlider")
        self.width_slider.setValue(self.width_slider.position_for(self._saved_width))
        self.width_slider.valueChanged.connect(lambda position: self.width.setValue(self.width_slider.width_at(position)))
        self.width.valueChanged.connect(lambda value: self._sync_width(value))
        width_row = QHBoxLayout()
        width_row.addWidget(self.width_slider, 1)
        width_row.addWidget(self.width)
        width_group.addLayout(width_row)
        layout.addLayout(width_group)
        for field in (self.width, self.opacity_input):
            field.setFixedSize(82, 26)
            field.setObjectName('PaintValueInput')
        grid = QGridLayout()
        grid.setSpacing(3)
        self.buttons = {}
        for i, (name, title) in enumerate(TOOLS):
            button = QToolButton()
            set_icon(button, name, size=20)
            button.setIconSize(QSize(24, 24))
            button.setFixedSize(34, 34)
            bind_caption(button, title, setter='setToolTip')
            button.setCheckable(True)
            button.clicked.connect(lambda _, n=name: self.choose(n))
            self.buttons[name] = button
            grid.addWidget(button, i // 4, i % 4)
        layout.addLayout(grid)
        self.choose(self.current_tool if self.current_tool in self.buttons else "brush", False)
        self.width.valueChanged.connect(lambda *_: self._save_timer.start(200))
        self.opacity.valueChanged.connect(lambda *_: self._save_timer.start(200))
        row = QHBoxLayout()
        row.addWidget(localize(QLabel(''), 'ui.toolbox.layers_0bcd'), 1)
        self.import_button = QToolButton()
        self.import_button.setFixedSize(24, 24)
        set_icon(self.import_button, 'import', size=20)
        set_text(self.import_button, 'ui.toolbox.import_image_as_layer_bb5e', setter='setToolTip')
        self.import_button.clicked.connect(
            lambda: self.layer_command.emit({"command": "import"})
        )
        self.add = QToolButton()
        self.add.setFixedSize(24, 24)
        set_icon(self.add, 'add', square=True)
        set_text(self.add, 'ui.toolbox.add_layer_3fc8', setter='setToolTip')
        self.add.clicked.connect(lambda: self.layer_command.emit({"command": "add"}))
        self.add.setContextMenuPolicy(Qt.CustomContextMenu)
        def add_menu(position):
            menu=QMenu(self.add)
            localize(add_caption_action(menu, 'addAction', 'Raster layer', lambda:self.layer_command.emit({'command':'add'})), 'ui.toolbox.raster_layer_cd46')
            localize(add_caption_action(menu, 'addAction', 'Vector text layer', lambda:self.layer_command.emit({'command':'add','kind':'vector'})), 'ui.toolbox.vector_text_layer_0395')
            menu.exec(self.add.mapToGlobal(position))
        self.add.customContextMenuRequested.connect(add_menu)
        row.addWidget(self.add)
        row.addWidget(self.import_button)
        self.remove = QToolButton()
        self.remove.setFixedSize(24, 24)
        set_icon(self.remove, 'trash', size=20)
        set_text(self.remove, 'ui.toolbox.delete_selected_layers_1c3a', setter='setToolTip')
        self.remove.clicked.connect(
            lambda: self.layer_command.emit(
                {
                    "command": "delete",
                    "layer": self.active,
                    "layers": list(self.checked),
                }
            )
        )
        row.addWidget(self.remove)
        layout.addLayout(row)
        self.layers = LayerList()
        self.layers.group_getter = lambda:set(self.checked)
        self.layers.currentItemChanged.connect(self._active)
        self.layers.reordered.connect(
            lambda order: self.layer_command.emit(
                {"command": "reorder", "order": order}
            )
        )
        layout.addWidget(self.layers, 1)

    def _refresh_swatches(self):
        for index, button in enumerate(self.swatch_buttons):
            button.setChecked(index == self.active_swatch)
            border = theme_manager.color('accent.primary' if index == self.active_swatch else 'border.subtle').name()
            style = f"QToolButton {{background:{self.swatches[index]};border:2px solid {border};padding:0;border-radius:3px;}}"
            if button.styleSheet() != style:
                button.setStyleSheet(style)

    def reset_swatches(self):
        self.swatches = list(self.default_swatches)
        self.wheel.set_colour(QColor(self.swatches[self.active_swatch]))
        self._refresh_swatches()
        self._save_timer.start(100)

    def select_swatch(self, index):
        self.active_swatch = index
        self.wheel.set_colour(QColor(self.swatches[index]))
        self._refresh_swatches()
        self._save_timer.start(100)

    def flush_preferences(self):
        self._save_timer.stop()
        if not self.preferences_path:
            return
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        file = QSaveFile(str(self.preferences_path))
        if file.open(QIODevice.WriteOnly):
            file.write(
                json.dumps(
                    {"colours":self.swatches,"active":self.active_swatch,"tool":self.current_tool,
                     "width":self.width.value(),"opacity":self.opacity.value(),"text_defaults":self.text_defaults}
                ).encode()
            )
            file.commit()

    def _sync_width(self, value):
        self.width_slider.blockSignals(True)
        self.width_slider.setValue(self.width_slider.position_for(value))
        self.width_slider.blockSignals(False)

    def _colour_changed(self, colour):
        bind_caption(self.hex, colour.name())
        self.swatches[self.active_swatch] = colour.name()
        self._refresh_swatches()
        self._save_timer.start(100)
        self.colour_changed.emit(colour)

    def _hex_changed(self):
        colour = QColor(self.hex.text())
        if colour.isValid():
            self.wheel.set_colour(colour)
        else:
            bind_caption(self.hex, self.wheel.colour.name())

    def choose(self, name, emit=True):
        self.current_tool = name
        self._save_timer.start(200)
        for key, button in self.buttons.items():
            button.setChecked(key == name)
        if emit:
            self.tool_changed.emit(name)

    def set_colour(self, colour):
        self.wheel.set_colour(colour)

    def _active(self, item, previous):
        if item and not self._updating:
            self.active = item.data(Qt.UserRole)
            for index in range(self.layers.count()):
                row = self.layers.itemWidget(self.layers.item(index))
                if row:
                    row.setProperty(
                        "active",
                        self.layers.item(index).data(Qt.UserRole) == self.active,
                    )
                    row.style().unpolish(row)
                    row.style().polish(row)
                    row.update()
            self.active_changed.emit(self.active)

    def _checked(self,identity,value):
        self._modifier_select(identity,QApplication.keyboardModifiers())

    def set_layers(self, layers, active=None):
        if self.layers._drag:
            return
        identity = active or self.active
        signature = (
            getattr(self, "document_key", None),
            identity,
            tuple(
                (
                    l["id"],
                    l["name"],
                    l["visible"],
                    l["opacity"],
                    l.get("background", False),
                    l.get("kind", "raster"),
                    l.get("thumbnail"),
                )
                for l in layers
            ),
        )
        if signature == getattr(self, "_layer_signature", None):
            self._layer_meta = layers
            return
        self._layer_signature = signature
        scroll = self.layers.verticalScrollBar().value()
        self._updating = True
        self._layer_meta = layers
        self.active = active or self.active
        self.layers.blockSignals(True)
        self.layers.clear_drag()
        self.layers.clear()
        self.layers.setFixedHeight(
            min(5, max(1, len(layers))) * 32 + 2 * self.layers.frameWidth()
        )
        self.checked.intersection_update(l["id"] for l in layers)
        for layer in reversed(layers):
            identity = layer["id"]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, identity)
            item.setSizeHint(QSize(180, 32))
            if layer.get("background"):
                item.setFlags(item.flags() & ~Qt.ItemIsDragEnabled)
            add_caption_item(self.layers, item)
            row = QWidget()
            row.setObjectName("PaintLayerRow")
            row.setProperty("active", identity == self.active)
            row.setProperty("paint_layer_id", identity)
            row.installEventFilter(self)
            apply_style(row, 'QWidget#PaintLayerRow {background:transparent; border-radius:3px;} QWidget#PaintLayerRow[active="true"] {background:@background.surface@;} QLineEdit {background:transparent; border:none; padding:0;}')
            layout = QHBoxLayout(row)
            layout.setContentsMargins(2, 0, 2, 0)
            layout.setSpacing(3)
            eye = QToolButton()
            set_icon(eye, 'eye' if layer['visible'] else 'eye_closed', size=18)
            eye.setIconSize(QSize(18, 18))
            set_text(eye, 'ui.toolbox.show_hide_layer_664b', setter='setToolTip')
            eye.setFixedSize(24, 24)
            eye.setObjectName("PaintLayerVisibility")
            if not layer["visible"]:
                shade = QGraphicsOpacityEffect(eye)
                shade.setOpacity(0.45)
                eye.setGraphicsEffect(shade)
            eye.clicked.connect(
                lambda _, i=identity, v=layer["visible"]: self.layer_command.emit(
                    {"command": "visible", "layer": i, "value": not v}
                )
            )
            layout.addWidget(eye)
            check = QCheckBox()
            check.setChecked(identity in self.checked)
            check.setEnabled(not layer.get("background"))
            check.toggled.connect(lambda value, i=identity: self._checked(i, value))
            layout.addWidget(check)
            thumb = QLabel("")
            thumb.setObjectName("PaintLayerThumbnail")
            thumb.setFixedSize(24, 24)
            thumb.setAlignment(Qt.AlignCenter)
            thumb.setStyleSheet("background:transparent; border:none;")
            thumb.setAttribute(Qt.WA_TransparentForMouseEvents)
            if layer.get("background"):
                bind_caption(thumb, "")
                apply_style(thumb, 'background:white; border:1px solid @border.subtle@;')
                thumb.setFixedHeight(22)
            if layer.get("thumbnail"):
                thumb.setPixmap(
                    QPixmap.fromImage(
                        QImage.fromData(base64.b64decode(layer["thumbnail"]), "PNG")
                    ).scaled(22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            layout.addWidget(thumb)
            if layer.get("kind") == "vector":
                marker = QLabel()
                marker.setObjectName("PaintVectorLayerIcon")
                marker.setPixmap(tool_icon("vector_text").pixmap(20, 20))
                set_text(marker, 'ui.toolbox.vector_text_layer_editable_text_ed34', setter='setToolTip')
                bind_caption(row, marker.toolTip(), setter='setToolTip')
                marker.setFixedSize(20, 24)
                marker.setAlignment(Qt.AlignCenter)
                marker.setAttribute(Qt.WA_TransparentForMouseEvents)
                layout.addWidget(marker)
            name = QLineEdit(layer["name"])
            name.setFrame(False)
            name.setMinimumWidth(20)
            name.setReadOnly(True)
            name.setFocusPolicy(Qt.NoFocus)
            name.setCursor(Qt.ArrowCursor)
            set_text(name, 'ui.toolbox.double_click_to_rename_2836', setter='setToolTip')
            name.setProperty("paint_layer_id", identity)
            name.setProperty("paint_original_name", layer["name"])
            name.installEventFilter(self)
            name.editingFinished.connect(
                lambda n=name, i=identity, old=layer["name"]: self._rename(n, i, old)
            )
            layout.addWidget(name, 1)
            name.selectionChanged.connect(lambda i=identity: self._select_item(i))
            alpha = QToolButton()
            bind_caption(alpha, str(round(layer["opacity"] * 100)))
            set_text(alpha, 'ui.toolbox.layer_opacity_81d1', setter='setToolTip')
            alpha.setFixedSize(28, 28)
            alpha.setStyleSheet("padding:0; font-size:11px;")
            alpha.clicked.connect(
                lambda _, i=identity, v=layer["opacity"]: self._opacity_menu(i, v)
            )
            layout.addWidget(alpha)
            drag = set_label_icon(QLabel(''), 'list')
            drag.setProperty("paint_layer_id", identity)
            drag.setProperty("paint_drag_handle", True)
            drag.installEventFilter(self)
            drag.setCursor(Qt.OpenHandCursor)
            drag.setFixedWidth(12)
            layout.addWidget(drag)
            if not layer.get("thumbnail"):
                self._empty_thumbnail(thumb)
            for control in (eye, check, alpha):
                control.setProperty("paint_layer_id", identity)
                control.installEventFilter(self)
            self.layers.setItemWidget(item, row)
            cached = getattr(self, "_thumbnail_images", {}).get(
                (getattr(self, "document_key", None), identity)
            )
            if cached is not None:
                thumb.setPixmap(
                    QPixmap.fromImage(cached).scaled(
                        22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
            if identity == self.active:
                self.layers.setCurrentItem(item)
        self.layers.blockSignals(False)
        self.layers.verticalScrollBar().setValue(scroll)
        self._updating = False

    def set_thumbnail(self, identity, image):
        if not hasattr(self, "_thumbnail_images"):
            self._thumbnail_images = {}
        key = (getattr(self, "document_key", None), identity)
        if image is None:
            self._thumbnail_images.pop(key, None)
        else:
            if len(self._thumbnail_images) > 256:
                self._thumbnail_images.pop(next(iter(self._thumbnail_images)))
            self._thumbnail_images[key] = QImage(image)
        for index in range(self.layers.count()):
            item = self.layers.item(index)
            if item.data(Qt.UserRole) == identity:
                thumb = self.layers.itemWidget(item).findChild(
                    QLabel, "PaintLayerThumbnail"
                )
                if image is None:
                    thumb.setPixmap(QPixmap())
                    bind_caption(thumb, "")
                    self._empty_thumbnail(thumb)
                else:
                    thumb.setPixmap(
                        QPixmap.fromImage(image).scaled(
                            22, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation
                        )
                    )
                break

    def _select_item(self, identity):
        if not self._updating:
            for i in range(self.layers.count()):
                if self.layers.item(i).data(Qt.UserRole) == identity:
                    self.layers.setCurrentRow(i)
                    break

    def _rename(self, field, identity, old):
        old = field.property("paint_original_name") or old
        value = field.text().strip()[:120] or "Layer"
        changed = not self._updating and value != old
        if changed:
            field.setProperty("paint_original_name", value)
            bind_caption(field, value)
        field.setReadOnly(True)
        field.setFocusPolicy(Qt.NoFocus)
        field.setCursor(Qt.ArrowCursor)
        field.deselect()
        field.clearFocus()
        if changed:
            self.layer_command.emit(
                {"command": "name", "layer": identity, "value": field.text()}
            )

    def _layer_menu(self, identity, position):
        from PySide6.QtWidgets import QMenu

        layer = next(
            (item for item in self._layer_meta if item["id"] == identity), None
        )
        if layer is None:
            return
        self._select_item(identity)
        menu = QMenu(self)
        menu.setObjectName("FileManagerContextMenu")
        localize(add_caption_action(menu, 'addAction', "Delete", lambda: self.layer_command.emit(
                {"command": "delete", "layer": identity, "layers": [identity]}
            )), 'ui.workspace.delete_e2d0')
        order = [item["id"] for item in self._layer_meta]
        selected = self.checked if identity in self.checked else {identity}
        indices = [i for i,value in enumerate(order) if value in selected]
        localize(add_caption_action(menu, 'addAction', "Move down", lambda:self._move_layer(identity,-1)), 'ui.toolbox.move_down_40bb').setEnabled(min(indices)>0)
        localize(add_caption_action(menu, 'addAction', "Move up", lambda:self._move_layer(identity,1)), 'ui.toolbox.move_up_c66f').setEnabled(max(indices)<len(order)-1)
        add_caption_action(menu, 'addAction', "Hide layer" if layer["visible"] else "Show layer", lambda: self._toggle_layer_visibility(identity))
        menu.aboutToHide.connect(menu.deleteLater)
        self._layer_popup = menu
        menu.popup(position)

    def _move_layer(self, identity, step):
        order = [item["id"] for item in self._layer_meta]
        if identity not in order:
            return
        selected = self.checked if identity in self.checked else {identity}
        indices = [i for i,value in enumerate(order) if value in selected]
        if not indices or (step<0 and min(indices)==0) or (step>0 and max(indices)==len(order)-1):
            return
        for i in (reversed(range(len(order)-1)) if step>0 else range(1,len(order))):
            other = i+step
            if order[i] in selected and order[other] not in selected:
                order[i],order[other] = order[other],order[i]
        self.layer_command.emit({'command':'reorder','order':order})

    def _toggle_layer_visibility(self, identity):
        layer = next(
            (item for item in self._layer_meta if item["id"] == identity), None
        )
        if layer is not None:
            self.layer_command.emit(
                {"command": "visible", "layer": identity, "value": not layer["visible"]}
            )

    def _opacity_menu(self, identity, value):
        from PySide6.QtWidgets import QMenu, QWidgetAction

        menu = QMenu(self)
        menu.setObjectName("FileManagerContextMenu")
        spin = QSlider(Qt.Horizontal)
        spin.setRange(0, 100)
        spin.setValue(round(value * 100))
        spin.setMinimumWidth(140)
        action = QWidgetAction(menu)
        action.setDefaultWidget(spin)
        add_caption_action(menu, 'addAction', action)
        spin.sliderReleased.connect(
            lambda: self.layer_command.emit(
                {"command": "opacity", "layer": identity, "value": spin.value() / 100}
            )
        )
        menu.aboutToHide.connect(
            lambda: (
                self.layer_command.emit(
                    {
                        "command": "opacity",
                        "layer": identity,
                        "value": spin.value() / 100,
                    }
                )
                if spin.value() != round(value * 100)
                else None
            )
        )
        menu.aboutToHide.connect(lambda: menu.deleteLater())
        self._opacity_popup = menu
        from PySide6.QtGui import QCursor

        menu.popup(QCursor.pos())

    def commit_inputs(self):
        self._hex_changed()
        self.width.interpretText()
        self.opacity_input.interpretText()
        for field in self.layers.findChildren(QLineEdit):
            identity = field.property("paint_layer_id")
            if identity:
                self._rename(field, identity, field.property("paint_original_name"))
        focused = QApplication.focusWidget()
        if focused and (focused is self or self.isAncestorOf(focused)):
            focused.clearFocus()

    def _empty_thumbnail(self, label):
        width, height = getattr(self, "canvas_dimensions", (1, 1))
        scale = min(22 / max(1, width), 22 / max(1, height))
        image = QPixmap(max(1, round(width * scale)), max(1, round(height * scale)))
        image.fill(QColor("#d0d0d0"))
        painter = QPainter(image)
        for y in range(0, image.height(), 4):
            for x in range(0, image.width(), 4):
                if (x // 4 + y // 4) % 2:
                    painter.fillRect(x, y, 4, 4, QColor("#eeeeee"))
        painter.end()
        label.setPixmap(image)

    def _modifier_select(self, identity, modifiers):
        order = [
            self.layers.item(i).data(Qt.UserRole) for i in range(self.layers.count())
        ]
        if modifiers & Qt.ShiftModifier:
            anchor = getattr(self, "_selection_anchor", self.active)
            if anchor in order and identity in order:
                a, b = sorted((order.index(anchor), order.index(identity)))
                area = set(order[a:b+1])
                if modifiers & Qt.ControlModifier:
                    self.checked.update(area)
                else:
                    self.checked = area
            else:
                self.checked = {identity}
        elif modifiers & Qt.ControlModifier:
            if identity in self.checked:
                self.checked.remove(identity)
            else:
                self.checked.add(identity)
            self._selection_anchor = identity
        else:
            self.checked = {identity}
            self._selection_anchor = identity
        for i in range(self.layers.count()):
            item = self.layers.item(i)
            check = self.layers.itemWidget(item).findChild(QCheckBox)
            check.blockSignals(True)
            check.setChecked(item.data(Qt.UserRole) in self.checked)
            check.blockSignals(False)
        self.selection_changed.emit(set(self.checked))
        self._select_item(identity)

    def eventFilter(self, watched, event):
        identity = watched.property("paint_layer_id")
        editing_name = isinstance(watched, QLineEdit) and not watched.isReadOnly()
        if identity and event.type() == QEvent.ContextMenu and not editing_name:
            self._layer_menu(identity, event.globalPos())
            return True
        if (
            identity
            and isinstance(watched, QLineEdit)
            and event.type() == QEvent.MouseButtonDblClick
            and event.button() == Qt.LeftButton
        ):
            if not event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
                self._select_item(identity)
                watched.setReadOnly(False)
                watched.setFocusPolicy(Qt.StrongFocus)
                watched.setCursor(Qt.IBeamCursor)
                watched.setFocus()
                watched.selectAll()
            return True
        if (
            identity
            and isinstance(watched, QLineEdit)
            and watched.isReadOnly()
            and event.type() == QEvent.MouseButtonPress
            and event.button() == Qt.RightButton
        ):
            return True
        if (
            identity
            and event.type() == QEvent.MouseButtonPress
            and event.button() == Qt.LeftButton
        ):
            if editing_name:
                return super().eventFilter(watched, event)
            modified = event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)
            keep_group = watched.property('paint_drag_handle') and not modified and identity in self.checked
            if not keep_group and (modified or isinstance(watched,(QLineEdit,QCheckBox))
                or watched.objectName() == "PaintLayerRow"
                or watched.property("paint_drag_handle")):
                self._modifier_select(identity,event.modifiers())
            if isinstance(watched,QCheckBox):
                return True
            if watched.property("paint_drag_handle"):
                self._drag_origin = event.globalPosition().toPoint()
                self._drag_identity = identity
                return True
            if isinstance(watched, QLineEdit) and watched.isReadOnly():
                return True
            if event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
                return True
        if watched.property("paint_drag_handle"):
            if event.type() == QEvent.MouseMove and event.buttons() & Qt.LeftButton:
                position = event.globalPosition().toPoint()
                if (
                    self.layers._drag is None
                    and hasattr(self, "_drag_origin")
                    and (position - self._drag_origin).manhattanLength()
                    >= QApplication.startDragDistance()
                ):
                    self.layers.start_drag(identity, self._drag_origin)
                self.layers.move_drag(position)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self.layers.finish_drag()
                return True
        if event.type() == QEvent.FocusIn and identity:
            self._select_item(identity)
        return super().eventFilter(watched, event)
