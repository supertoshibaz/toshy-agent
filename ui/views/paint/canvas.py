"""Single-widget raster viewport: cursor-centred zoom, pan and area export."""
from ui.themes.manager import theme_manager
from ui.styles.binding import watch_paint

import math
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QWidget

from .storage import TILE_SIZE, WORLD_LIMIT, load_tiles
from .edit_tools import CanvasTools
from .raster import background_colour

MIN_ZOOM = 0.15
MIN_ENDLESS_ZOOM = 0.10


class PaintCanvas(CanvasTools, QWidget):
    edit_requested = Signal(str, object)
    images_dropped = Signal(object, object)
    copy_requested = Signal()
    context_requested = Signal(object)
    sample_requested = Signal(object)
    tool_changed = Signal(str)
    text_layer_selected = Signal(str)
    area_selected = Signal(object)
    camera_changed = Signal()
    error = Signal(str)

    def __init__(self, cache, io, parent=None):
        super().__init__(parent)
        self.setObjectName("PaintCanvas")
        watch_paint(self)
        # paintEvent covers every exposed pixel with an opaque background.
        # Avoid clearing/repainting ancestor widgets under every brush patch.
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.cache, self.io = cache, io
        self._edge_images = OrderedDict()
        self._edge_bytes = 0
        self.path = None
        self.meta = None
        self.message = "Loading canvas…"
        self._fit_pending = False
        self.zoom = 1.0
        self.center = QPointF(0, 0)
        self._space = False
        self._pan = None
        self._anchor = None
        self.selection = None
        self.export_mode = False
        self._generation = 0
        self._pixel_revision = 0
        self._job = None
        self._failed = False
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.timeout.connect(self._fetch)
        self.setup_tools()
        checkers = QImage(32, 32, QImage.Format_RGB32)
        checkers.fill(QColor("#d0d0d0"))
        p = QPainter(checkers)
        p.fillRect(0, 0, 16, 16, QColor("#eeeeee"))
        p.fillRect(16, 16, 16, 16, QColor("#eeeeee"))
        p.end()
        self._checkers = QBrush(QPixmap.fromImage(checkers))

    def apply_edit(self, path, meta, images, affected, dirty_rect=None):
        """Publish immutable patches without resetting the camera/input/cache."""
        self.pause_fetch()
        self.path, self.meta = path, meta
        self._pixel_revision += 1
        if self.isVisible():
            self.cache.protect(path, self._visible_keys())
        self.cache.apply_patches(path, images, affected)
        for key, image in images:
            level, x, y = key
            span = TILE_SIZE * 2**level
            top = self.screen_at(QPointF(x * span, y * span))
            dirty = QRectF(
                top.x() - 2, top.y() - 2, span * self.zoom + 4, span * self.zoom + 4
            ).toAlignedRect()
            if dirty_rect is None:
                self.update(dirty.intersected(self.rect()))
        if dirty_rect is not None:
            dirty = (
                QRectF(
                    self.screen_at(dirty_rect.topLeft()),
                    self.screen_at(dirty_rect.bottomRight()),
                )
                .toAlignedRect()
                .adjusted(-2, -2, 2, 2)
            )
            self.update(dirty.intersected(self.rect()))
        elif not images:
            self.update()
        if self.isVisible() and self._job is None:
            self._fetch_timer.start(0)

    def pause_fetch(self):
        self._fetch_timer.stop()
        if self._job is not None:
            self.io.cancel(self._job)
            self._job = None

    def configure(self, path, meta, camera=None, *, preserve_input=False):
        # Committing a worker edit must not interrupt the next stroke or an
        # ongoing middle-button pan. The camera is taken at completion time.
        fields = (
            "_gesture",
            "_gesture_path",
            "_start",
            "_matrix",
            "_modifiers",
            "_base_bounds",
            "_pivot",
            "_corner",
            "_transform_kind",
            "_pan",
            "_space",
        )
        inputs = (
            {name: getattr(self, name) for name in fields if hasattr(self, name)}
            if preserve_input
            else {}
        )
        self.release()
        for name, value in inputs.items():
            setattr(self, name, value)
        self.path, self.meta = path, meta
        from .selection import decode
        self.pixel_selection = decode(meta.get("pixel_selection"))
        self.message = "Loading canvas…"
        self._failed = False
        self._fit_pending = camera is None
        if camera:
            try:
                z, x, y = map(float, camera)
                if not all(math.isfinite(v) for v in (z, x, y)):
                    raise ValueError()
                self.zoom = max(self.minimum_zoom(), min(64.0, z))
                self.center = QPointF(
                    max(-WORLD_LIMIT, min(WORLD_LIMIT, x)),
                    max(-WORLD_LIMIT, min(WORLD_LIMIT, y)),
                )
            except (ValueError, TypeError):
                self.fit_canvas()
        else:
            self.fit_canvas()
        if self.isVisible():
            self._fit_pending = False
        self._changed()

    def camera(self):
        return [self.zoom, self.center.x(), self.center.y()]

    def minimum_zoom(self):
        return (
            MIN_ENDLESS_ZOOM
            if self.meta and self.meta["mode"] == "endless"
            else MIN_ZOOM
        )

    def world_at(self, point):
        return (
            self.center
            + (QPointF(point) - QPointF(self.width() / 2, self.height() / 2))
            / self.zoom
        )

    def screen_at(self, point):
        return (QPointF(point) - self.center) * self.zoom + QPointF(
            self.width() / 2, self.height() / 2
        )

    def fit_canvas(self):
        if not self.meta:
            return
        if self.meta["mode"] == "endless":
            bounds = self.meta.get("content_bounds")
            if not bounds:
                self.zoom, self.center = 1.0, QPointF()
                self._changed()
                return
            rect = QRectF(*bounds)
        else:
            rect = QRectF(0, 0, self.meta["width"], self.meta["height"])
        self.center = rect.center()
        self.zoom = max(
            self.minimum_zoom(),
            min(
                1.0,
                min(
                    max(1, self.width() - 80) / rect.width(),
                    max(1, self.height() - 80) / rect.height(),
                ),
            ),
        )
        self._changed()

    def set_zoom(self, zoom, anchor=None):
        anchor = (
            QPointF(anchor)
            if anchor is not None
            else QPointF(self.width() / 2, self.height() / 2)
        )
        before = self.world_at(anchor)
        self.zoom = max(self.minimum_zoom(), min(64.0, zoom))
        self.center += before - self.world_at(anchor)
        self._changed()
        if self.tool in ('move', 'transform', 'text') and self._gesture is None and self._pending_transform is None:
            self.prepare_preview()

    def _changed(self):
        self.center.setX(max(-WORLD_LIMIT, min(WORLD_LIMIT, self.center.x())))
        self.center.setY(max(-WORLD_LIMIT, min(WORLD_LIMIT, self.center.y())))
        self._generation += 1
        self.pause_fetch()
        if self.path and self.isVisible():
            self.cache.protect(self.path, self._visible_keys())
        self.update()
        if self.isVisible() and self._job is None:
            self._fetch_timer.start(20)
        self.camera_changed.emit()

    def set_export_mode(self, enabled=True):
        self.export_mode = enabled
        self.selection = None
        self._anchor = None
        self.setCursor(Qt.CrossCursor if enabled else Qt.IBeamCursor if self.tool == "text" else Qt.ArrowCursor)
        self.setFocus()
        self.update()

    def _visible_keys(self, prefetch=False):
        if not self.meta or not self.meta["tile_count"]:
            return []
        level = min(
            self.meta["max_level"], max(0, int(math.floor(math.log2(1 / self.zoom))))
        )
        span = TILE_SIZE * 2**level
        a, b = self.world_at(QPointF()), self.world_at(
            QPointF(self.width(), self.height())
        )
        bounds = self.meta.get("content_bounds")
        if not bounds:
            return []
        left, top, width, height = bounds
        # At very large viewport sizes use a coarser preview rather than
        # continuously evicting and decoding more visible pixels than fit.
        budget = max(1, self.cache.limit // (TILE_SIZE * TILE_SIZE * 4))
        while level < self.meta["max_level"]:
            nx = max(
                0,
                math.ceil(min(b.x(), left + width) / span)
                - math.floor(max(a.x(), left) / span),
            )
            ny = max(
                0,
                math.ceil(min(b.y(), top + height) / span)
                - math.floor(max(a.y(), top) / span),
            )
            if nx * ny <= budget:
                break
            level += 1
            span *= 2
        margin = span if prefetch else 0
        x0, x1 = (
            math.floor(max(a.x() - margin, left) / span),
            math.ceil(min(b.x() + margin, left + width) / span) - 1,
        )
        y0, y1 = (
            math.floor(max(a.y() - margin, top) / span),
            math.ceil(min(b.y() + margin, top + height) / span) - 1,
        )
        if x1 < x0 or y1 < y0:
            return []
        # Bounding the candidates also bounds work for damaged preview metadata.
        cx, cy = self.center.x() / span, self.center.y() / span
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 1024:
            x0, x1 = max(x0, int(cx) - 16), min(x1, int(cx) + 16)
            y0, y1 = max(y0, int(cy) - 16), min(y1, int(cy) + 16)
        keys = [(level, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        keys.sort(key=lambda key: (key[1] + 0.5 - cx) ** 2 + (key[2] + 0.5 - cy) ** 2)
        return keys

    def _fetch(self):
        if (
            not self.isVisible()
            or self._job is not None
            or self._failed
            or not self.path
        ):
            return
        visible = self._visible_keys()
        self.cache.protect(self.path, visible)
        nearby = self._visible_keys(prefetch=True)
        # Prefetch only what fits after visible tiles. Unbounded prefetch would
        # evict visible tiles and cause perpetual background decoding.
        budget = max(1, self.cache.limit // (TILE_SIZE * TILE_SIZE * 4))
        admitted = list(dict.fromkeys(visible + nearby))[:budget]
        keys = [key for key in admitted if not self.cache.contains((self.path, *key))]
        keys = list(dict.fromkeys(keys))[:4]
        if not keys:
            return
        path, generation = self.path, self._generation
        revision = self._pixel_revision

        def completed(result, error):
            self._job = None
            if path != self.path:
                return
            if revision != self._pixel_revision or generation != self._generation:
                if self.isVisible():
                    self._fetch_timer.start(0)
                return
            if error:
                if not isinstance(error, InterruptedError):
                    self._failed = True
                    self.error.emit(str(error))
                return
            for key, image in result:
                self.cache.put((path, *key), image)
            if self.isVisible():
                self.update()
                self._fetch_timer.start(0 if result else 8)

        budget_ms = 4 if self._stroke_id is not None else 12
        self._job = self.io.submit(
            lambda cancel: load_tiles(path, keys, cancel, budget_ms=budget_ms),
            completed,
            priority=1,
        )

    def release(self):
        self.release_tools()
        self.pause_fetch()
        self._pan = None
        self._space = False

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_pending and self.meta:
            self.fit_canvas()
            self._fit_pending = False
        self._fetch_timer.start(0)

    def hideEvent(self, event):
        self.release()
        self._edge_images.clear()
        self._edge_bytes = 0
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._changed()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta and not event.pixelDelta().isNull():
            delta = event.pixelDelta().y() * 2
        if delta:
            self.set_zoom(
                self.zoom * 2 ** (max(-960, min(960, delta)) / 480), event.position()
            )
        event.accept()

    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.MiddleButton or (
            event.button() == Qt.LeftButton and self._space
        ):
            self._pan = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        elif event.button() == Qt.LeftButton and self.export_mode and self.meta:
            self._anchor = self.world_at(event.position())
            self.selection = QRectF(self._anchor, self._anchor)
            self.update()
            event.accept()
        else:
            if not self.tool_press(event):
                super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pan is not None:
            self.center -= (event.position() - self._pan) / self.zoom
            self._pan = event.position()
            self._changed()
        elif self._anchor is not None:
            self.selection = QRectF(
                self._anchor, self.world_at(event.position())
            ).normalized()
            self.update()
        else:
            if not self.tool_move(event):
                super().mouseMoveEvent(event)

    def selected_rect(self):
        if self.selection is None:
            return QRect()
        r = self.selection.normalized()
        return QRect(
            math.floor(r.left()),
            math.floor(r.top()),
            math.ceil(r.right()) - math.floor(r.left()),
            math.ceil(r.bottom()) - math.floor(r.top()),
        )

    def mouseReleaseEvent(self, event):
        if self._pan is not None and event.button() in (Qt.LeftButton, Qt.MiddleButton):
            self._pan = None
            self.setCursor(Qt.CrossCursor if self.export_mode else Qt.IBeamCursor if self.tool == "text" else Qt.ArrowCursor)
            event.accept()
        elif self._anchor is not None and event.button() == Qt.LeftButton:
            self._anchor = None
            rect = self.selected_rect()
            self.update()
            if rect.width() and rect.height():
                self.set_export_mode(False)
                self.area_selected.emit(rect)
            event.accept()
        else:
            if not self.tool_release(event):
                super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        self.context_requested.emit(event.pos())
        event.accept()

    def paste_images(self):
        from PySide6.QtWidgets import QApplication
        from ui.image_mime import sources
        if self.meta and self.edit_enabled and self._stroke_id is None:
            images = sources(QApplication.clipboard().mimeData())
            if images:
                self.images_dropped.emit(images,QPointF(self.center))
                return True
        return False

    def dragEnterEvent(self, event):
        from ui.image_mime import can_import
        if self.meta and self.edit_enabled and can_import(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        from ui.image_mime import can_import
        if self.meta and self.edit_enabled and can_import(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        from ui.image_mime import sources
        paths = sources(event.mimeData())
        if paths and self.meta and self.edit_enabled and self._stroke_id is None:
            self.images_dropped.emit(paths, self.world_at(event.position()))
            event.acceptProposedAction()

    def keyPressEvent(self, event):
        from PySide6.QtGui import QKeySequence
        if self._text_box is None and self.meta and self.edit_enabled:
            if event.matches(QKeySequence.Paste):
                if self.paste_images():
                    event.accept()
                    return
            if event.matches(QKeySequence.Copy) and self.pixel_selection is not None:
                self.copy_requested.emit()
                event.accept()
                return
        if self.tool_key(event):
            event.accept()
            return
        if event.key() == Qt.Key_Space:
            self._space = True
            self.setCursor(Qt.OpenHandCursor)
        elif event.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.set_zoom(self.zoom * 2**0.25)
        elif event.key() == Qt.Key_Minus:
            self.set_zoom(self.zoom / 2**0.25)
        elif event.key() == Qt.Key_Escape:
            self.set_export_mode(False)
        elif event.key() == Qt.Key_1 and not event.modifiers():
            self.set_zoom(1.0)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space = False
            if self._pan is None:
                self.setCursor(Qt.CrossCursor if self.export_mode else Qt.IBeamCursor if self.tool == "text" else Qt.ArrowCursor)
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._pointer_samples.release()
        self._space = False
        self._pan = None
        super().focusOutEvent(event)

    def _paint_backdrop(self, painter, rect, bg):
        if bg is None:
            painter.fillRect(rect, self._checkers)
        elif isinstance(bg, QColor):
            if bg.alpha() < 255:
                painter.fillRect(rect, self._checkers)
            painter.fillRect(rect, bg)
        elif not bg["visible"] or bg["opacity"] < 1:
            painter.fillRect(rect, self._checkers)
            if bg["visible"]:
                painter.fillRect(
                    rect, QColor(255, 255, 255, round(bg["opacity"] * 255))
                )
        else:
            painter.fillRect(rect, Qt.white)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme_manager.color('workspace.background'))
        if not self.meta:
            painter.setPen(theme_manager.color('text.secondary'))
            painter.drawText(self.rect(), Qt.AlignCenter, self.message)
            return
        finite = self.meta["mode"] == "finite"
        paper = QRectF(
            self.screen_at(QPointF()),
            self.screen_at(QPointF(self.meta["width"], self.meta["height"])),
        )
        bg = next((l for l in self.meta.get("layers", []) if l.get("background")), None)
        if not finite:
            bg = background_colour(self.meta)
        elif self.meta.get("version", 1) == 1:
            bg = {"visible": True, "opacity": 1.0}
        backdrop = QRectF(paper) if finite else QRectF(self.rect())
        self._paint_backdrop(painter, backdrop, bg)
        if not finite:
            self._paint_guides(painter, QRectF(event.rect()))
        if finite:
            painter.setClipRect(paper)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.zoom < 1)
        transforming = self.paint_transform_preview(painter)
        for level, x, y in (() if transforming else self._visible_keys()):
            span = TILE_SIZE * 2**level
            origin = self.screen_at(QPointF(x * span, y * span))
            tile_rect = QRectF(
                origin.x(), origin.y(), span * self.zoom, span * self.zoom
            )
            if not tile_rect.adjusted(-1, -1, 1, 1).intersects(QRectF(event.rect())):
                continue
            valid, image = self.cache.display_entry((self.path, level, x, y))
            if not valid:
                # A fallback must contain this document's current pixels.
                for parent_level in range(level + 1, self.meta["max_level"] + 1):
                    factor = 2 ** (parent_level - level)
                    px, py = x // factor, y // factor
                    present, parent = self.cache.display_entry(
                        (self.path, parent_level, px, py)
                    )
                    if present:
                        span = TILE_SIZE * 2**level
                        target = QRectF(
                            self.screen_at(QPointF(x * span, y * span)),
                            self.screen_at(QPointF((x + 1) * span, (y + 1) * span)),
                        )
                        if parent is not None:
                            self._draw_tile(
                                painter,
                                target,
                                parent,
                                (parent_level, px, py),
                                QRectF(
                                    (x % factor) * TILE_SIZE / factor,
                                    (y % factor) * TILE_SIZE / factor,
                                    TILE_SIZE / factor,
                                    TILE_SIZE / factor,
                                ),
                            )
                        break
                # During zoom-out, use finer tiles already in memory while a
                # cold overview is built. Reset their background first so an
                # eraser's transparent pixels cannot expose old fallback ink.
                painter.save()
                painter.setClipRect(tile_rect, Qt.IntersectClip)
                for (child_level, cx, cy), child in self.cache.finer_entries(
                    self.path, level, x, y
                ):
                    child_span = TILE_SIZE * 2**child_level
                    target = QRectF(
                        self.screen_at(QPointF(cx * child_span, cy * child_span)),
                        self.screen_at(
                            QPointF((cx + 1) * child_span, (cy + 1) * child_span)
                        ),
                    )
                    if not target.adjusted(-1, -1, 1, 1).intersects(
                        QRectF(event.rect())
                    ):
                        continue
                    self._paint_backdrop(painter, target, bg)
                    if not finite:
                        # Clearing an old overview must restore the guides
                        # under transparent pixels of the finer current tile.
                        painter.save()
                        painter.setClipRect(target, Qt.IntersectClip)
                        self._paint_guides(painter, target)
                        painter.restore()
                    if child is not None:
                        self._draw_tile(painter, target, child, (child_level, cx, cy))
                painter.restore()
                continue
            if image is None:
                continue
            scale = 2**level
            pos = self.screen_at(QPointF(x * TILE_SIZE * scale, y * TILE_SIZE * scale))
            self._draw_tile(
                painter,
                QRectF(
                    pos.x(),
                    pos.y(),
                    image.width() * scale * self.zoom,
                    image.height() * scale * self.zoom,
                ),
                image,
                (level, x, y),
            )
        live = self._live_stroke
        if live is not None:
            for (x, y), image in live.render().items():
                target = QRectF(self.screen_at(QPointF(x * TILE_SIZE, y * TILE_SIZE)),
                    self.screen_at(QPointF((x + 1) * TILE_SIZE, (y + 1) * TILE_SIZE)))
                if not target.intersects(QRectF(event.rect())):
                    continue
                self._paint_backdrop(painter, target, bg)
                if not finite:
                    self._paint_guides(painter, target.intersected(QRectF(event.rect())))
                self._draw_tile(painter, target, image, (0, x, y))
        painter.setClipping(False)
        if finite:
            painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
            painter.drawRect(paper)
        self.paint_tools(painter)
        if self.selection is not None:
            r = self.selected_rect()
            screen = QRectF(
                self.screen_at(QPointF(r.x(), r.y())),
                self.screen_at(QPointF(r.x() + r.width(), r.y() + r.height())),
            )
            painter.fillRect(screen, QColor(56, 189, 248, 32))
            painter.setPen(QPen(QColor("#1685bd"), 1, Qt.DashLine))
            painter.drawRect(screen)
            painter.setPen(theme_manager.color('text.primary'))
            width_label = QRectF(
                screen.left(), max(0, screen.top() - 24), max(100, screen.width()), 22
            )
            painter.fillRect(width_label, theme_manager.color('background.surface'))
            painter.drawText(width_label, Qt.AlignCenter, f"{r.width()} px")
            painter.save()
            painter.translate(max(20, screen.left() - 8), screen.center().y())
            painter.rotate(-90)
            height_label = QRectF(-50, -20, 100, 22)
            painter.fillRect(height_label, theme_manager.color('background.surface'))
            painter.drawText(height_label, Qt.AlignCenter, f"{r.height()} px")
            painter.restore()
        if self.export_mode:
            painter.setPen(theme_manager.color('text.primary'))
            hint = QRectF(20, self.height() - 38, 360, 26)
            painter.fillRect(hint, theme_manager.color('background.surface'))
            painter.drawText(
                hint, Qt.AlignCenter, "Drag an export area · Esc to cancel"
            )

    def _draw_tile(self, painter, target, image, key, source=None):
        if self.zoom >= 1:
            # Qt's nearest-neighbour blitter can omit a row at half-pixel
            # destinations (e.g. the centre of an odd-height workspace).
            # Shared boundaries must round to the same physical pixel.
            ratio = self.devicePixelRatioF()
            snap = lambda value: math.floor(value * ratio + 0.5) / ratio
            target = QRectF(
                QPointF(snap(target.left()), snap(target.top())),
                QPointF(snap(target.right()), snap(target.bottom())),
            )
            painter.drawImage(target, image, source or QRectF(image.rect()))
            return
        # Bilinear filtering needs one pixel beyond each tile. Independent
        # image edges otherwise blend toward transparent, revealing the grid.
        level, x, y = key
        neighbours = [
            self.cache.display_entry((self.path, level, x + dx, y + dy))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
        ]
        diagonals = [self.cache.display_entry((self.path,level,x+dx,y+dy))
                     for dx,dy in ((-1,-1),(1,-1),(-1,1),(1,1))]
        identity = (
            image.cacheKey(),
            tuple(
                (valid, neighbour.cacheKey() if neighbour is not None else 0)
                for valid, neighbour in neighbours+diagonals
            ),
        )
        padded = self._edge_images.get(identity)
        if padded is None:
            width, height = image.width(), image.height()
            padded = QImage(width + 2, height + 2, QImage.Format_ARGB32_Premultiplied)
            padded.fill(Qt.transparent)
            edge = QPainter(padded)
            edge.drawImage(1, 1, image)
            strips = [
                QRectF(0, 1, 1, height),
                QRectF(width + 1, 1, 1, height),
                QRectF(1, 0, width, 1),
                QRectF(1, height + 1, width, 1),
            ]
            own = [
                QRectF(0, 0, 1, height),
                QRectF(width - 1, 0, 1, height),
                QRectF(0, 0, width, 1),
                QRectF(0, height - 1, width, 1),
            ]
            for index, ((valid, neighbour), target_strip, own_strip) in enumerate(
                zip(neighbours, strips, own)
            ):
                if valid and neighbour is None:
                    continue
                if not valid:
                    edge.drawImage(target_strip, image, own_strip)
                    continue
                source_strip = (
                    QRectF(neighbour.width() - 1, 0, 1, height)
                    if index == 0
                    else (
                        QRectF(0, 0, 1, height)
                        if index == 1
                        else (
                            QRectF(0, neighbour.height() - 1, width, 1)
                            if index == 2
                            else QRectF(0, 0, width, 1)
                        )
                    )
                )
                edge.drawImage(target_strip, neighbour, source_strip)
            for (valid,neighbour),(px,py,sx,sy) in zip(diagonals,(
                (0,0,width-1,height-1),(width+1,0,0,height-1),
                (0,height+1,width-1,0),(width+1,height+1,0,0))):
                if valid and neighbour is None:
                    continue
                if valid:
                    edge.drawImage(QRectF(px,py,1,1),neighbour,QRectF(sx,sy,1,1))
                else:
                    edge.drawImage(QRectF(px,py,1,1),image,
                        QRectF(0 if px==0 else width-1,0 if py==0 else height-1,1,1))
            edge.end()
            size = padded.sizeInBytes()
            while self._edge_images and self._edge_bytes + size > 16 * 1024 * 1024:
                _, old = self._edge_images.popitem(last=False)
                self._edge_bytes -= old.sizeInBytes()
            self._edge_images[identity] = padded
            self._edge_bytes += size
        else:
            self._edge_images.move_to_end(identity)
        source = QRectF(source or image.rect())
        # Clip only the tile's ownership region; neighbours sample one another
        # but are never composited twice (important for translucent ink).
        scale_x, scale_y = (
            target.width() / source.width(),
            target.height() / source.height(),
        )
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing,False)
        ratio = self.devicePixelRatioF()
        snap = lambda value:math.floor(value*ratio+.5)/ratio
        ownership = QRectF(QPointF(snap(target.left()),snap(target.top())),
                           QPointF(snap(target.right()),snap(target.bottom())))
        painter.setClipRect(ownership, Qt.IntersectClip)
        painter.drawImage(
            target.adjusted(-scale_x, -scale_y, scale_x, scale_y),
            padded,
            source.translated(1, 1).adjusted(-1, -1, 1, 1),
        )
        painter.restore()

    def _paint_guides(self, painter, region=None):
        ratio = self.devicePixelRatioF()
        snap = lambda value: (math.floor(value * ratio) + 0.5) / ratio
        paper = background_colour(self.meta)
        light_neutral = (paper is not None and paper.alphaF() > .95
                         and min(paper.red(), paper.green(), paper.blue()) >= 235
                         and max(paper.red(), paper.green(), paper.blue()) - min(paper.red(), paper.green(), paper.blue()) <= 12)
        if self.meta.get("show_grid", False):
            # Adaptive spacing bounds both point count and work while zooming.
            spacing = 32 * 2 ** max(0, math.ceil(math.log2(24 / (32 * self.zoom))))
            # Restoring a finer fallback needs only its own grid points.
            # Include the pen radius for points crossing the clipped edge.
            area = QRectF(self.rect())
            if region is not None:
                area = area.intersected(region.adjusted(-2, -2, 2, 2))
            top = self.world_at(area.topLeft())
            bottom = self.world_at(area.bottomRight())
            points = QPolygonF(
                [
                    self.screen_at(QPointF(x * spacing, y * spacing))
                    for y in range(
                        math.ceil(top.y() / spacing),
                        math.floor(bottom.y() / spacing) + 1,
                    )
                    for x in range(
                        math.ceil(top.x() / spacing),
                        math.floor(bottom.x() / spacing) + 1,
                    )
                ]
            )
            points = QPolygonF(
                [
                    QPointF(
                        snap(p.x()),
                        snap(p.y()),
                    )
                    for p in points
                ]
            )
            painter.save()
            # Even-width dots need antialiasing to keep their visual center
            # on the same physical pixel center as the one-pixel axes.
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor(128, 128, 128, 120) if light_neutral else QColor(255, 255, 255, 185), 2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPoints(points)
            painter.restore()
        if self.meta.get("show_axes", False):
            origin = self.screen_at(QPointF())
            x, y = snap(origin.x()), snap(origin.y())
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, False)
            axes_colour = QColor(255,255,255,185) if self.meta.get('mode') == 'endless' and not light_neutral else QColor(128,128,128,140)
            pen = QPen(axes_colour, 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            painter.restore()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.edit_enabled and not self.export_mode and self.meta:
            point = self.world_at(event.position())
            if self.tool == 'text' or self.text_at(point):
                self.cancel_stroke()
                self._gesture = None
                self._gesture_path = None
                self.start_text(event.position(), point)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent

        if (
            self._text_box
            and watched is self._text_edit
            and event.type() == QEvent.KeyPress
        ):
            if self.tool_key(event):
                return True
        return super().eventFilter(watched, event)
