"""Paint image context, reusing the Board agent composer and chat attachments."""
from ui.styles.binding import watch_paint
from ui.themes.manager import theme_manager
from ui.i18n import localize, set_text


import math
from contextlib import nullcontext

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui.views.canvas.canvas_view import CanvasAgentPopup
from . import raster, storage
from .navigation import overview_bounds
from .runtime import get_session


def render_snapshot(source, cancel=None, max_side=2048):
    """Bounded output and LOD reads: never allocate the full source canvas."""
    session = get_session(source)
    with session.lock if session else nullcontext():
        meta = session.metadata() if session else storage.read_metadata(source)
        bounds = (
            QRectF(0, 0, meta["width"], meta["height"])
            if meta["mode"] == "finite"
            else overview_bounds(meta)
        )
        factor = min(1.0, max_side / max(bounds.width(), bounds.height()))
        image = QImage(
            max(1, min(max_side, math.ceil(bounds.width() * factor))),
            max(1, min(max_side, math.ceil(bounds.height() * factor))),
            QImage.Format_ARGB32_Premultiplied,
        )
        storage._check(cancel)
        if image.isNull():
            raise MemoryError("Not enough memory to prepare the canvas image.")
        backdrop = raster.background_colour(meta)
        if meta["mode"] == "finite":
            legacy = next(
                (
                    layer
                    for layer in raster.default_layers(meta)
                    if layer.get("background")
                ),
                None,
            )
            if legacy:
                backdrop = QColor(Qt.white)
                backdrop.setAlphaF(legacy["opacity"] if legacy["visible"] else 0)
        image.fill(backdrop if backdrop is not None else Qt.transparent)
        if not meta.get("tile_count"):
            return image
        level = min(meta["max_level"], max(0, math.floor(math.log2(1 / factor))))
        span = storage.TILE_SIZE * 2**level
        painter = QPainter(image)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        try:
            # Read one tile at a time: the snapshot never retains a second full image.
            for y in range(
                math.floor(bounds.top() / span), math.ceil(bounds.bottom() / span)
            ):
                for x in range(
                    math.floor(bounds.left() / span), math.ceil(bounds.right() / span)
                ):
                    storage._check(cancel)
                    tiles = storage.load_tiles(source, [(level, x, y)], cancel)
                    tile = tiles[0][1]
                    if tile is not None:
                        painter.drawImage(
                            QRectF(
                                (x * span - bounds.x()) * factor,
                                (y * span - bounds.y()) * factor,
                                tile.width() * 2**level * factor,
                                tile.height() * 2**level * factor,
                            ),
                            tile,
                        )
        finally:
            painter.end()
        return image


class ImageContextPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        watch_paint(self)
        self.setMinimumWidth(260)
        self.setMinimumHeight(48)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.image = None
        self.selection = None
        self._anchor = None
        self.setCursor(Qt.CrossCursor)

    def heightForWidth(self, width):
        if self.image is None:
            return 180
        natural = round(width * self.image.height() / max(1, self.image.width()))
        popup = self.parentWidget()
        workspace = popup.parentWidget() if popup else None
        # A viewport-relative ceiling lets square/landscape images fill the
        # width, without a very tall document pushing the composer off-screen.
        limit = (
            max(48, min(round(workspace.height() * 0.65), workspace.height() - 180))
            if workspace
            else 480
        )
        return max(48, min(natural, limit))

    def sizeHint(self):
        return QSize(320, self.heightForWidth(320))

    def target(self):
        if self.image is None:
            return QRectF()
        size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF(
            (self.width() - size.width()) / 2,
            (self.height() - size.height()) / 2,
            size.width(),
            size.height(),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme_manager.color('background.panel'))
        if self.image is None:
            painter.setPen(theme_manager.color('text.secondary'))
            painter.drawText(self.rect(), Qt.AlignCenter, "Preparing image…")
            return
        target = self.target()
        for y in range(int(target.top()), math.ceil(target.bottom()), 8):
            for x in range(int(target.left()), math.ceil(target.right()), 8):
                painter.fillRect(
                    QRectF(x, y, 8, 8).intersected(target),
                    QColor("#eeeeee" if (x // 8 + y // 8) % 2 else "#cccccc"),
                )
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(target, self.image)
        if self.selection is not None:
            factor = target.width() / self.image.width()
            selected = QRectF(
                target.x() + self.selection.x() * factor,
                target.y() + self.selection.y() * factor,
                self.selection.width() * factor,
                self.selection.height() * factor,
            )
            painter.fillRect(selected, QColor(56, 189, 248, 30))
            painter.setPen(QPen(QColor("#38bdf8"), 1, Qt.DashLine))
            painter.drawRect(selected)

    def _point(self, point):
        target = self.target()
        x = (point.x() - target.x()) * self.image.width() / target.width()
        y = (point.y() - target.y()) * self.image.height() / target.height()
        return QPointF(
            max(0, min(self.image.width(), x)), max(0, min(self.image.height(), y))
        )

    def clear(self):
        self.selection = None
        self._anchor = None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.clear()
        elif (
            event.button() == Qt.LeftButton
            and self.image is not None
            and self.target().contains(event.position())
        ):
            self._anchor = self._point(event.position())
            self.selection = None
        event.accept()

    def mouseMoveEvent(self, event):
        if self._anchor is not None:
            self.selection = QRectF(
                self._anchor, self._point(event.position())
            ).normalized()
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._anchor is not None:
            self.mouseMoveEvent(event)
            if self.selection is not None and (
                self.selection.width() < 1 or self.selection.height() < 1
            ):
                self.selection = None
            self._anchor = None
        event.accept()

    def contextMenuEvent(self, event):
        self.clear()
        event.accept()

    def selected_image(self):
        if self.image is None:
            return None
        if self.selection is None:
            return QImage(self.image)
        return self.image.copy(
            self.selection.toAlignedRect().intersected(self.image.rect())
        )


class PaintAgentPopup(CanvasAgentPopup):
    def __init__(self, parent):
        # The Board composer only requires these four context adapter methods.
        super().__init__(self, parent)
        set_text(self.hint_label, 'ui.agent.select_an_image_region_or_send_fe1a')
        for widget in (self.pair_edges_button, self.tree_button):
            widget.hide()
        from PySide6.QtWidgets import QCheckBox
        self.whole_file_toggle = localize(QCheckBox('', self), 'ui.agent.include_entire_file_png_376b')
        set_text(self.whole_file_toggle, 'ui.agent.include_the_entire_drawing_regardless_of_c05d', setter='setToolTip')
        self.layout().insertWidget(2, self.whole_file_toggle)
        self.preview = ImageContextPreview(self)
        self.layout().insertWidget(2, self.preview)
        self.send_button.setEnabled(False)
        self.cancelled.connect(self.preview.clear)
        self.destroyed.connect(self.tags_bar.deleteLater)
        parent.installEventFilter(self)

    def open_at(self, anchor):
        super().open_at(anchor)
        self._resize_preview()

    def eventFilter(self, watched, event):
        if (
            watched is self.parentWidget()
            and event.type() == QEvent.Resize
            and self.isVisible()
        ):
            self._resize_preview()
        return super().eventFilter(watched, event)

    def _resize_preview(self):
        margins = self.layout().contentsMargins()
        width = max(1, self.width() - margins.left() - margins.right())
        self.preview.setFixedHeight(self.preview.heightForWidth(width))
        self.layout().invalidate()
        self.resize(self.width(), self.sizeHint().height())
        parent = self.parentWidget()
        if parent:
            self.move(
                max(6, min(self.x(), parent.width() - self.width() - 6)),
                max(6, min(self.y(), parent.height() - self.height() - 6)),
            )
        self._position_tags_bar()

    def reset_and_hide(self):
        self.whole_file_toggle.setChecked(False)
        super().reset_and_hide()
        # Release the bounded snapshot on Cancel/Send, rather than retaining
        # one 16 MiB bitmap for every document whose popup was once opened.
        if hasattr(self, "preview"):
            self.preview.image = None
            self.preview.setFixedHeight(180)
            self.preview.clear()
            self.send_button.setEnabled(False)

    def set_agent_pair_connections_enabled(self, value):
        pass

    def set_agent_tree_mode_enabled(self, value):
        pass

    def clear_agent_context(self):
        if hasattr(self, "preview"):
            self.preview.clear()

    def has_agent_context(self):
        return self.preview.image is not None

    def set_image(self, image):
        self.preview.image = image
        self.preview.clear()
        self.preview.updateGeometry()
        self._resize_preview()
        self.send_button.setEnabled(image is not None)

    def _submit(self):
        if self.send_button.isEnabled() and self.has_agent_context():
            super()._submit()
