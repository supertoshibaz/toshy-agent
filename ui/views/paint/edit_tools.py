"""Canvas input and overlays; raster work is delegated to a worker transaction."""
from ui.themes.manager import theme_manager

import math
import uuid
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
)


class CanvasTools:
    def setup_tools(self):
        from .pointer import PointerSamples
        self._pointer_samples = PointerSamples(self)
        self.tool = "brush"
        self.ink = QColor("black")
        self.brush_width = 8.0
        self.brush_opacity = 1.0
        self.active_layer = "paint"
        self.selected_layers = set()
        self.pixel_selection = None
        self._selection_revision = 0
        self.edit_enabled = True
        self._gesture = None
        self._gesture_path = None
        self._start = None
        self._matrix = QTransform()
        self._text_box = None
        self._selected_text = None
        self._text_hover = None
        self._text_hits_meta = None
        self._text_hits = ()
        self._preview_job = None
        self._layer_preview = None
        self._pending_transform = None
        self._preview_stamp = 0
        self._preview_key = None
        self._stroke_id = None
        self._stroke_points = []
        self._stroke_settings = None
        self._live_stroke = None
        self._live_dirty = QRectF()
        self._live_update_timer = QTimer(self)
        self._live_update_timer.setSingleShot(True)
        self._live_update_timer.setInterval(8)
        self._live_update_timer.setTimerType(Qt.PreciseTimer)
        self._live_update_timer.timeout.connect(self._flush_live_preview)
        self._selection_clear_icon = None
        self._stroke_timer = QTimer(self)
        self._stroke_timer.setSingleShot(True)
        self._stroke_timer.timeout.connect(self.flush_stroke)

    def set_selection(self, path):
        from .selection import encode
        if self.pixel_selection == path:
            return
        self._selection_revision += 1
        self.pixel_selection = QPainterPath(path) if path is not None else None
        self.edit_requested.emit('selection', {'selection_after': encode(path)})
        self.prepare_preview()
        self.update()

    def flush_stroke(self, phase="update"):
        self._stroke_timer.stop()
        if self._stroke_id is None:
            return
        payload = dict(
            self._stroke_settings,
            phase=phase,
            stroke_id=self._stroke_id,
            points=self._stroke_points,
        )
        self._stroke_points = []
        self.edit_requested.emit("stroke", payload)
        if phase in ("end", "cancel"):
            self._pointer_samples.release()
            self._stroke_id = None
            self._stroke_settings = None

    def cancel_stroke(self):
        self._pointer_samples.release()
        self._live_stroke = None
        self._live_update_timer.stop()
        self._live_dirty = QRectF()
        if self._stroke_id is not None:
            self.flush_stroke("cancel")

    def set_tool(self, tool):
        self.cancel_stroke()
        if self._preview_job:
            self._preview_stamp += 1
            self.io.cancel(self._preview_job)
            self._preview_job = None
        self.commit_text()
        self.tool = tool
        self._gesture = None
        self._gesture_path = None
        self.set_export_mode(False)
        self.update()
        self.setFocus()
        self.tool_changed.emit(tool)
        if tool in ("move", "transform"):
            self.prepare_preview()
        else:
            self._layer_preview = None

    def tool_bounds(self):
        if self.tool == "text":
            hit = self.selected_text()
            return QRectF(hit[2]) if hit else QRectF()
        if self.pixel_selection is not None:
            return self.pixel_selection.boundingRect()
        if not self.meta:
            return QRectF()
        targets = self.selected_layers or {self.active_layer}
        rect = QRectF()
        from .raster import default_layers

        for layer in default_layers(self.meta):
            legacy_background = (
                layer.get("background") and self.meta["mode"] == "finite"
            )
            bounds = layer.get("bounds")
            if legacy_background:
                bounds = [0, 0, self.meta["width"], self.meta["height"]]
            if (
                layer["id"] in targets
                and bounds
                and (not layer.get("background") or legacy_background)
            ):
                r = QRectF(*bounds)
                rect = r if rect.isEmpty() else rect.united(r)
        return rect

    def prepare_preview(self):
        if not self.path or not self.meta or (not self._text_box and self.tool not in ("move", "transform", "text")):
            return
        from .raster import transform_preview

        path = self.path
        text = self.selected_text() if self.tool == "text" or self._text_box else None
        if self.tool == "text" and text is None:
            self._layer_preview = None
            return
        targets = set(self.selected_layers) or {self.active_layer}
        selection = (
            QPainterPath(self.pixel_selection)
            if self.pixel_selection is not None
            else None
        )

        object_id = text[1]['id'] if text else None
        if text:
            targets = {text[0]['id']}
            selection = None

        viewport = QRectF(self.world_at(QPointF()), self.world_at(QPointF(self.width(), self.height())))
        viewport = [viewport.x(), viewport.y(), viewport.width(), viewport.height()]
        max_side = min(4096, max(1536, math.ceil(max(self.width(), self.height()) * self.devicePixelRatioF())))
        from .selection import encode
        key = (path, self.meta.get('undo_pos'), self.meta.get('settings_revision'), tuple(sorted(targets)),
               encode(selection), object_id, tuple(viewport), max_side)
        if key == self._preview_key and (self._layer_preview is not None or self._preview_job is not None):
            return
        self._preview_key = key
        self._preview_stamp += 1
        stamp = self._preview_stamp
        if self._preview_job:
            self.io.cancel(self._preview_job)
            self._preview_job = None
        self._layer_preview = None
        if self._text_box and text:
            self._text_box.view.item.setVisible(False)

        def complete(result, error):
            if stamp == self._preview_stamp and path == self.path:
                self._preview_job = None
                self._layer_preview = None if error else result
                if self._text_box and text:
                    if error:
                        self.cancel_text()
                        self.error.emit(str(error))
                    else:
                        self._text_box.view.item.setVisible(True)
                self.update()

        self._preview_job = self.io.submit(
            lambda cancel: transform_preview(path, targets, selection, cancel, object_id=object_id,
                                             viewport=viewport, max_side=max_side),
            complete,
            priority=1,
        )

    def paint_transform_preview(self, painter):
        matrix = self._pending_transform if self._pending_transform is not None else self._matrix
        active = self._pending_transform is not None or self._gesture is not None
        editing = self._text_box is not None and not self._text_new_layer and self.selected_text() is not None
        if (not editing and (not active or matrix.isIdentity())) or not self._layer_preview:
            return False
        # Подложка уже нарисована в экранных координатах. Здесь только слои.
        painter.save()
        painter.translate(self.width()/2 - self.center.x()*self.zoom, self.height()/2 - self.center.y()*self.zoom)
        painter.scale(self.zoom, self.zoom)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for layer in self._layer_preview:
            combined = layer['opacity'] < 1 and layer['stationary'] is not None and layer['moving'] is not None
            if combined:
                # Непрозрачность применяется один раз к уже собранному слою.
                buffer = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
                buffer.fill(Qt.transparent)
                target = QPainter(buffer)
                target.setRenderHint(QPainter.SmoothPixmapTransform, True)
                target.setTransform(painter.worldTransform())
            else:
                target = painter
            target.setOpacity(1. if combined else layer['opacity'])
            if layer['stationary'] is not None:
                target.drawImage(QRectF(layer['rect']), layer['stationary'])
            if layer['moving'] is not None and not editing:
                image, area = layer['moving']
                target.save()
                target.setTransform(matrix, True)
                target.drawImage(QRectF(area), image)
                target.restore()
            if combined:
                target.end()
                painter.save()
                painter.resetTransform()
                painter.setOpacity(layer['opacity'])
                painter.drawImage(0, 0, buffer)
                painter.restore()
        painter.restore()
        return True

    def release_tools(self):
        self.cancel_stroke()
        self.cancel_text()
        self._preview_stamp += 1
        if self._preview_job:
            self.io.cancel(self._preview_job)
            self._preview_job = None
        self._layer_preview = None
        self._gesture = None
        self._gesture_path = None

    def tool_press(self, event):
        if event.button() != Qt.LeftButton or not self.meta or not self.edit_enabled:
            return False
        if self.selection_clear_rect().contains(event.position()):
            self.set_selection(None)
            return True
        point = self.world_at(event.position())
        self._start = point
        if self.tool == "picker":
            self.sample_requested.emit(point)
            return True
        active = next((x for x in self.meta.get('layers', []) if x['id']==self.active_layer), {})
        if active.get('kind') == 'vector' and self.tool in ('brush','eraser','fill','rectangle','ellipse','line'):
            self.error.emit('Этот векторный слой хранит редактируемый текст. Для рисования выберите растровый слой.')
            return True
        if self.tool == "fill":
            self.edit_requested.emit("fill", {"point": [point.x(), point.y()]})
            return True
        if self.tool == "text":
            if self._text_box:
                self.commit_text()
                return True
            self._matrix = QTransform()
            hit = self.text_at(point)
            if hit:
                self.select_text(hit)
                self._gesture = "text_click"
                self._base_bounds = QRectF(hit[2])
                self.prepare_preview()
            else:
                self._selected_text = None
                self._layer_preview = None
                self._gesture = "text_frame"
                self._gesture_path = QPainterPath(point)
            self.update()
            return True
        self._gesture = self.tool
        self._matrix = QTransform()
        self._modifiers = event.modifiers()
        self._gesture_path = QPainterPath(point)
        if self.tool in ("brush", "eraser"):
            self._pointer_samples.acquire()
            self._gesture_path.lineTo(point + QPointF(0.001, 0.001))
            self._stroke_id = uuid.uuid4().hex
            self._stroke_settings = dict(
                tool=self.tool,
                layer=self.active_layer,
                color=self.ink.name(),
                opacity=self.brush_opacity,
                width=self.brush_width,
                smoothing_radius=1.25 / self.zoom,
                selection=(
                    QPainterPath(self.pixel_selection)
                    if self.pixel_selection is not None
                    else None
                ),
            )
            self._stroke_points = [(point.x(), point.y())]
            if self.zoom >= 1:
                from .stroke import LiveStroke
                self._live_update_timer.stop()
                self._live_dirty = QRectF()
                self._live_stroke = LiveStroke(self._stroke_id, self._stroke_settings, self.meta,
                    QRectF(self.world_at(QPointF()), self.world_at(QPointF(self.width(), self.height()))))
                self._live_stroke.append(point)
                self._live_last_point = QPointF(point)
            self.flush_stroke("begin")
        if self.tool in ("move", "transform"):
            # A vector text object remains whole under a pixel-area selection.
            if self.pixel_selection is not None:
                from .text_objects import bounds
                for layer in self.meta.get('layers', []):
                    if layer.get('kind') == 'vector' and layer['id'] in (self.selected_layers or {self.active_layer}):
                        for obj in layer.get('objects', []):
                            rect = bounds(obj)
                            if self.pixel_selection.intersects(rect):
                                area = QPainterPath();area.addRect(rect)
                                self.set_selection(self.pixel_selection.united(area))
            self._base_bounds = self.tool_bounds()
            if self._base_bounds.isEmpty():
                self._gesture = None
                return True
            self._transform_kind = "move"
            self._pivot = self._base_bounds.center()
            if self.tool == "transform":
                rect = self._base_bounds
                for corner, opposite in [
                    (rect.topLeft(), rect.bottomRight()),
                    (rect.topRight(), rect.bottomLeft()),
                    (rect.bottomLeft(), rect.topRight()),
                    (rect.bottomRight(), rect.topLeft()),
                ]:
                    if (
                        self.screen_at(corner) - event.position()
                    ).manhattanLength() < 14:
                        self._transform_kind = "scale"
                        self._pivot = opposite
                        self._corner = corner
                        break
                rotation = self.screen_at(
                    QPointF(rect.center().x(), rect.top())
                ) - QPointF(0, 28)
                if (rotation - event.position()).manhattanLength() < 16:
                    self._transform_kind = "rotate"
            self.prepare_preview()
        self.update()
        return True

    def tool_move(self, event):
        if self._gesture is None:
            if self.tool == "text" and self.meta and not self.export_mode:
                hit = self.text_at(self.world_at(event.position()))
                hover = (hit[0]['id'], hit[1]['id']) if hit else None
                self.setCursor(Qt.SizeAllCursor if hit else Qt.IBeamCursor)
                if hover != self._text_hover:
                    self._text_hover = hover
                    self.update()
            return False
        point = self.world_at(event.position())
        kind = self._gesture
        if kind in ("brush", "eraser"):
            self._stroke_points.append((point.x(), point.y()))
            if self._live_stroke is not None:
                self._live_stroke.append(point)
                self._update_live_area(point)
            if not self._stroke_timer.isActive():
                self._stroke_timer.start(16)
            return True
        elif kind == "text_click":
            delta = point - self._start
            if delta.manhattanLength() * self.zoom >= 4:
                self._matrix = QTransform.fromTranslate(delta.x(), delta.y())
        elif kind == "select_lasso":
            self._gesture_path.lineTo(point)
        elif kind in ("rectangle", "ellipse", "select_rect", "line", "text_frame"):
            path = QPainterPath()
            if event.modifiers() & Qt.ShiftModifier and kind in (
                "rectangle",
                "ellipse",
            ):
                delta = point - self._start
                side = max(abs(delta.x()), abs(delta.y()))
                point = self._start + QPointF(
                    math.copysign(side, delta.x()), math.copysign(side, delta.y())
                )
            rect = QRectF(self._start, point).normalized()
            if kind == "ellipse":
                path.addEllipse(rect)
            elif kind == "line":
                path.moveTo(self._start)
                path.lineTo(point)
            else:
                path.addRect(rect)
            self._gesture_path = path
        else:
            pivot = self._pivot
            transform = QTransform()
            transform.translate(pivot.x(), pivot.y())
            if self._transform_kind == "rotate":
                a = math.atan2(self._start.y() - pivot.y(), self._start.x() - pivot.x())
                b = math.atan2(point.y() - pivot.y(), point.x() - pivot.x())
                angle = math.degrees(b - a)
                if event.modifiers() & Qt.ShiftModifier:
                    angle = round(angle / 15) * 15
                transform.rotate(angle)
            elif self._transform_kind == "scale":
                delta = self._corner - pivot
                sx = (point.x() - pivot.x()) / delta.x() if delta.x() else 1
                sy = (point.y() - pivot.y()) / delta.y() if delta.y() else 1
                if event.modifiers() & Qt.ShiftModifier:
                    sx = sy = max(abs(sx), abs(sy))
                transform.scale(max(0.01, min(100, sx)), max(0.01, min(100, sy)))
            else:
                transform.translate(
                    point.x() - self._start.x(), point.y() - self._start.y()
                )
            transform.translate(-pivot.x(), -pivot.y())
            self._matrix = transform
        self.update()
        return True

    def tool_release(self, event):
        if event.button() != Qt.LeftButton or self._gesture is None:
            return False
        kind = self._gesture
        path = self._gesture_path
        self._gesture = None
        self._gesture_path = None
        if kind == "text_frame":
            point = self.world_at(event.position())
            rect = QRectF(self._start, point).normalized()
            if (min(rect.width(), rect.height()) * self.zoom >= 4
                    and math.hypot(rect.width(), rect.height()) * self.zoom >= QApplication.startDragDistance()):
                self.start_text(self.screen_at(rect.topLeft()), rect.topLeft(), frame=rect, force_new=True)
        elif kind == "text_click":
            hit = self.selected_text()
            if hit and self._matrix.isIdentity():
                self.start_text(event.position(), self.world_at(event.position()))
            elif hit:
                m = self._matrix
                self._pending_transform = QTransform(m)
                self.edit_requested.emit("move", dict(layer=hit[0]['id'], object_id=hit[1]['id'],
                    layers=[hit[0]['id']], selection=None,
                    matrix=[m.m11(),m.m12(),m.m21(),m.m22(),m.dx(),m.dy()]))
        elif kind in ("brush", "eraser"):
            point = self.world_at(event.position())
            if self._stroke_id is not None:
                self._stroke_points.append((point.x(), point.y()))
                if self._live_stroke is not None:
                    self._live_stroke.append(point)
                    self._live_stroke.finished = True
                self.flush_stroke("end")
        elif kind in ("select_rect", "select_lasso"):
            if kind == "select_lasso":
                path.closeSubpath()
            if path.boundingRect().isEmpty():
                selection = None
            elif (
                self.pixel_selection is not None
                and self._modifiers & Qt.ShiftModifier
                and self._modifiers & Qt.AltModifier
            ):
                selection = self.pixel_selection.intersected(path)
            elif (
                self.pixel_selection is not None and self._modifiers & Qt.ShiftModifier
            ):
                selection = self.pixel_selection.united(path)
            elif self.pixel_selection is not None and self._modifiers & Qt.AltModifier:
                selection = self.pixel_selection.subtracted(path)
            else:
                selection = path
            self.set_selection(selection)
        elif kind in ("move", "transform"):
            if not self._matrix.isIdentity():
                m = self._matrix
                self._pending_transform = QTransform(m)
                selection = QPainterPath(self.pixel_selection) if self.pixel_selection is not None else None
                self._selection_revision += 1
                self.edit_requested.emit(
                    kind,
                    {"matrix": [m.m11(), m.m12(), m.m21(), m.m22(), m.dx(), m.dy()], "selection":selection},
                )
                if selection is not None:
                    self.pixel_selection = m.map(selection)
        else:
            self.edit_requested.emit(kind, {"path": QPainterPath(path)})
        self._matrix = QTransform()
        self.update()
        return True

    def _update_live_area(self, point):
        previous = getattr(self, '_live_last_point', point)
        self._live_last_point = QPointF(point)
        radius = self._live_stroke.settings['width'] / 2 + 2
        rect = QRectF(previous, point).normalized().adjusted(-radius, -radius, radius, radius)
        self._live_dirty = self._live_dirty.united(rect)
        if not self._live_update_timer.isActive():
            self._live_update_timer.start()

    def _flush_live_preview(self):
        rect, self._live_dirty = self._live_dirty, QRectF()
        if self._live_stroke is not None and not rect.isEmpty():
            self.update(QRectF(self.screen_at(rect.topLeft()), self.screen_at(rect.bottomRight())).toAlignedRect().adjusted(-2, -2, 2, 2).intersected(self.rect()))

    def tool_key(self, event):
        if self._text_box:
            if event.key() == Qt.Key_Escape:
                self.cancel_text()
                return True
            if (
                event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier
            ):
                self.commit_text()
                return True
            return False
        if event.key() == Qt.Key_Escape:
            self.cancel_stroke()
            self.set_selection(None)
            self._gesture = None
            self._gesture_path = None
            self.update()
            return False
        if event.modifiers() & Qt.ControlModifier:
            if event.key() == Qt.Key_D:
                self.set_selection(None)
                return True
            if event.key() == Qt.Key_A and self.meta and self.meta["mode"] == "finite":
                path = QPainterPath()
                path.addRect(0, 0, self.meta["width"], self.meta["height"])
                self.set_selection(path)
                return True
            return False
        if event.key() == Qt.Key_Delete and self.tool == "text" and self.selected_text():
            hit = self.selected_text()
            self.edit_requested.emit("delete_selection", dict(layer=hit[0]['id'],
                layers=[hit[0]['id']], object_id=hit[1]['id'], selection=None))
            return True
        if event.key() == Qt.Key_Delete and self.pixel_selection is not None:
            self.edit_requested.emit("delete_selection", {})
            return True
        keys = {
            Qt.Key_B: "brush",
            Qt.Key_E: "eraser",
            Qt.Key_I: "picker",
            Qt.Key_F: "fill",
            Qt.Key_T: "text",
            Qt.Key_V: "move",
        }
        if event.key() in keys:
            self.set_tool(keys[event.key()])
            return True
        return False

    def text_hits(self):
        # Metadata is replaced after an edit. Do not lay out every object on
        # every mouse move or every canvas repaint.
        if self._text_hits_meta is not self.meta:
            from .text_objects import bounds
            self._text_hits_meta = self.meta
            self._text_hits = tuple((layer, obj, bounds(obj))
                for layer in (self.meta or {}).get('layers', [])
                if layer.get('kind') == 'vector' and layer.get('visible', True)
                for obj in layer.get('objects', []))
        return self._text_hits

    def text_at(self, world):
        return next((hit for hit in reversed(self.text_hits())
                     if hit[0]['id'] == self.active_layer and hit[2].contains(world)), None)

    def selected_text(self):
        return next((hit for hit in self.text_hits()
                     if hit[0]['id'] == self.active_layer
                     and (hit[0]['id'], hit[1]['id']) == self._selected_text), None)

    def select_text(self, hit):
        if hit[0]['id'] != self.active_layer:
            return
        self._selected_text = (hit[0]['id'], hit[1]['id'])
        self.selected_layers = {self.active_layer}
        self.set_selection(None)
        self.text_layer_selected.emit(self.active_layer)

    def start_text(self, screen, world, *, frame=None, force_new=False):
        from .text_editor import PaintTextEditor
        self.cancel_text()
        hit = None if force_new else self.text_at(world)
        if hit:
            layer, obj, _ = hit
            self.select_text(hit)
            self._text_target = layer['id']
            self._text_new_layer = False
        else:
            self._selected_text = None
            self._layer_preview = None
            layer = next((x for x in self.meta.get('layers', []) if x['id']==self.active_layer), {})
            self._text_new_layer = layer.get('kind') != 'vector'
            self._text_target = uuid.uuid4().hex if self._text_new_layer else layer['id']
            obj = dict(id=uuid.uuid4().hex, html='', width=max(16,round(frame.width())) if frame is not None else 420, color=self.ink.name(),
                       height=max(0., frame.height()) if frame is not None else 0.,
                       matrix=[1,0,0,1,world.x(),world.y()])
        self._text_box = PaintTextEditor(self, obj, self.commit_text, self.cancel_text)
        self._text_edit = self._text_box.edit
        self._text_edit.installEventFilter(self)
        self._text_box.move(max(4,min(round(screen.x()),self.width()-self._text_box.width()-4)),
                            max(4,min(round(screen.y()),self.height()-self._text_box.height()-4)))
        self._text_box.show();self._text_box.raise_();self._text_box.focus_canvas()
        if hit and not self._layer_preview:
            self._text_box.view.item.setVisible(False)
        self.prepare_preview()
        self.update()

    def cancel_text(self):
        if self._text_box:
            self._preview_stamp += 1
            if self._preview_job:
                self.io.cancel(self._preview_job)
                self._preview_job = None
            self._preview_key = None
            self._layer_preview = None
            self._text_box.dispose()
            self._text_box.hide();self._text_box.deleteLater();self._text_box = None
            self._text_edit = None
            self.update()

    def commit_text(self):
        if not self._text_box:
            return
        obj = self._text_box.result()
        if self._text_new_layer and not self._text_edit.toPlainText().strip():
            self.cancel_text()
            return
        self._text_default_char = self._text_edit.currentCharFormat()
        self._text_default_block = self._text_edit.textCursor().blockFormat()
        self._selected_text = (self._text_target, obj['id'])
        payload = dict(object=obj, layer=self._text_target, new_layer=self._text_new_layer)
        self.cancel_text()
        self.edit_requested.emit('text_object', payload)
        self.setFocus()

    def paint_tools(self, p):
        if not self.meta:
            return
        p.save()
        p.translate(
            self.width() / 2 - self.center.x() * self.zoom,
            self.height() / 2 - self.center.y() * self.zoom,
        )
        p.scale(self.zoom, self.zoom)
        if self.meta["mode"] == "finite":
            p.setClipRect(QRectF(0, 0, self.meta["width"], self.meta["height"]))
        if self._gesture_path is not None and self._gesture not in (
            "move",
            "transform",
            "brush",
            "eraser",
        ):
            if self._gesture in ("select_rect", "select_lasso", "text_frame"):
                p.setPen(QPen(QColor("#1685bd"), 1 / self.zoom, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
            else:
                if self.pixel_selection is not None:
                    p.setClipPath(self.pixel_selection, Qt.IntersectClip)
                colour = QColor(self.ink)
                colour.setAlphaF(self.brush_opacity)
                if self._gesture == "eraser":
                    colour = QColor(150, 150, 150, 120)
                p.setPen(
                    QPen(
                        colour,
                        self.brush_width,
                        Qt.SolidLine,
                        Qt.RoundCap,
                        Qt.RoundJoin,
                    )
                )
                p.setBrush(Qt.NoBrush)
            p.drawPath(self._gesture_path)
        p.restore()
        p.save()
        p.translate(
            self.width() / 2 - self.center.x() * self.zoom,
            self.height() / 2 - self.center.y() * self.zoom,
        )
        p.scale(self.zoom, self.zoom)
        if self.pixel_selection is not None:
            p.setPen(QPen(QColor("#1685bd"), 1 / self.zoom, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawPath(self.pixel_selection)
        if self.tool == "text" and self._text_hover and not self._gesture and not self._text_box:
            hovered = next((hit for hit in self.text_hits()
                           if hit[0]['id'] == self.active_layer
                           and (hit[0]['id'], hit[1]['id']) == self._text_hover), None)
            if hovered:
                p.setPen(QPen(theme_manager.color('accent.primary'), 1 / self.zoom, Qt.DotLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(hovered[2])
        if not self._text_box and (self.tool in ("move", "transform", "text") or self._pending_transform is not None):
            matrix = self._pending_transform if self._pending_transform is not None else self._matrix
            moving = self._gesture is not None or self._pending_transform is not None
            bounds = self._base_bounds if self._pending_transform is not None else self.tool_bounds()
            if not bounds.isEmpty():
                frame = self._matrix.map(QPainterPath())
                frame.addRect(bounds)
                frame = matrix.map(frame) if moving else frame
                p.setPen(QPen(QColor("#1685bd"), 1 / self.zoom, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawPath(frame)
                if self.tool == "transform" and not moving:
                    p.setBrush(Qt.white)
                    size = 8 / self.zoom
                    for corner in (
                        bounds.topLeft(),
                        bounds.topRight(),
                        bounds.bottomLeft(),
                        bounds.bottomRight(),
                    ):
                        p.drawRect(
                            QRectF(
                                corner.x() - size / 2, corner.y() - size / 2, size, size
                            )
                        )
                    top = QPointF(bounds.center().x(), bounds.top())
                    rotation = top - QPointF(0, 28 / self.zoom)
                    p.drawLine(top, rotation)
                    p.drawEllipse(rotation, 4 / self.zoom, 4 / self.zoom)
        p.restore()
        button = self.selection_clear_rect()
        if not button.isEmpty():
            if self._selection_clear_icon is None:
                from .toolbox import tool_icon

                self._selection_clear_icon = tool_icon("trash")

            p.save()
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(theme_manager.color('border.subtle'), 1))
            p.setBrush(theme_manager.color('background.surface'))
            p.drawRoundedRect(button, 4, 4)
            self._selection_clear_icon.paint(p, button.adjusted(4, 4, -4, -4).toRect())
            p.restore()

    def selection_clear_rect(self):
        if (
            self.pixel_selection is None
            or self.pixel_selection.isEmpty()
            or self._gesture
            or self.export_mode
            or not self.edit_enabled
        ):
            return QRectF()
        bounds = self.pixel_selection.boundingRect()
        corner = self.screen_at(bounds.bottomRight())
        # Remain reachable when a large selection extends outside the viewport.
        x = max(2, min(corner.x() + 3, self.width() - 26))
        y = max(2, min(corner.y() + 3, self.height() - 26))
        return QRectF(x, y, 24, 24)
