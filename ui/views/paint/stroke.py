"""Incremental stroke geometry and a bounded, immediate raster preview."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from .storage import TILE_SIZE as S, _check


def stroke_path(points, previous=None, raw_previous=None, radius=0, finish=False):
    path = QPainterPath()
    for px, py in points:
        point = QPointF(px, py)
        if previous is None:
            path.moveTo(point)
            path.lineTo(point + QPointF(.001, .001))
        else:
            if not path.elementCount():
                path.moveTo(previous)
            if radius:
                delta = point - raw_previous
                distance = math.hypot(delta.x(), delta.y())
                lag = min(.2, radius / distance) if distance else 0
                endpoint = point - delta * lag
                path.quadTo(raw_previous, endpoint)
                previous = endpoint
            else:
                path.lineTo(point)
                previous = point
        if raw_previous is None:
            previous = point
        raw_previous = point
    if finish and radius and previous is not None and raw_previous != previous:
        if not path.elementCount():
            path.moveTo(previous)
        path.quadTo(raw_previous, raw_previous)
        previous = raw_previous
    return path, previous, raw_previous


def capture_bases(session, layer_id, keys, cancel=None):
    """Worker-only snapshot, before applying the first point of a stroke."""
    from . import raster
    with session.lock:
        meta = session.metadata()
        layers = meta['layers']
        active = next((i for i, layer in enumerate(layers) if layer['id'] == layer_id), None)
        if active is None or not layers[active]['visible']:
            return {}
        result = {}
        empty = raster.blank()
        for x, y in keys[:8]:
            _check(cancel)
            groups = [None, None, None]
            for index, layer in enumerate(layers):
                if layer.get('background') or not layer['visible'] or not layer['opacity']:
                    continue
                row = session.db.execute('SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?', (layer['id'], x, y)).fetchone()
                if not row:
                    continue
                image = session.get(row[0])
                group = 0 if index < active else 2 if index > active else 1
                if group == 1:
                    groups[1] = image
                else:
                    if groups[group] is None:
                        groups[group] = raster.blank()
                    painter = QPainter(groups[group])
                    painter.setOpacity(layer['opacity'])
                    painter.drawImage(0, 0, image)
                    painter.end()
            if groups[1] is None:
                groups[1] = QImage(empty)
            result[(x, y)] = (*groups, layers[active]['opacity'])
        return result


class LiveStroke:
    """GUI-owned preview; no database, IO, Undo or mutable worker objects."""
    def __init__(self, identity, settings, meta, visible):
        self.identity, self.settings, self.meta = identity, dict(settings), dict(meta)
        self.pending = []
        self.previous = self.raw_previous = None
        self.finished = False
        self.bases = None
        self.masks, self.images = {}, {}
        if meta['mode'] == 'finite':
            visible = visible.intersected(QRectF(0, 0, meta['width'], meta['height']))
        if visible.isEmpty():
            self.keys = []
            return
        keys = [(x, y) for y in range(math.floor(visible.top() / S), math.floor(visible.bottom() / S) + 1)
                for x in range(math.floor(visible.left() / S), math.floor(visible.right() / S) + 1)]
        center = visible.center()
        keys.sort(key=lambda key: (key[0] * S + S / 2 - center.x()) ** 2 + (key[1] * S + S / 2 - center.y()) ** 2)
        self.keys = keys[:8]

    def append(self, point):
        self.pending.append((point.x(), point.y()))

    def render(self):
        if self.bases is None or not self.pending:
            return self.images
        from . import raster
        path, self.previous, self.raw_previous = stroke_path(self.pending, self.previous, self.raw_previous,
            self.settings.get('smoothing_radius', 0), self.finished)
        self.pending = []
        width = self.settings['width']
        area = path.boundingRect().adjusted(-width / 2 - 1, -width / 2 - 1, width / 2 + 1, width / 2 + 1)
        for (x, y), (under, layer, over, opacity) in self.bases.items():
            tile = QRectF(x * S, y * S, S, S)
            if not area.intersects(tile):
                continue
            mask = self.masks.get((x, y))
            if mask is None:
                mask = self.masks[(x, y)] = raster.blank()
            painter = QPainter(mask)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.translate(-x * S, -y * S)
            painter.setPen(QPen(Qt.white, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(path)
            painter.end()
            # At high zoom a new segment may touch only a few source pixels.
            # Rebuild that patch, not four complete 512x512 tile images per tick.
            patch = area.intersected(tile).translated(-x * S, -y * S).toAlignedRect().intersected(mask.rect())
            colored = mask.copy(patch)
            color = QColor('white' if self.settings['tool'] == 'eraser' else self.settings['color'])
            color.setAlphaF(self.settings['opacity'])
            painter = QPainter(colored)
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            painter.fillRect(colored.rect(), color)
            painter.end()
            ink = layer.copy(patch)
            painter = QPainter(ink)
            painter.translate(-x * S - patch.x(), -y * S - patch.y())
            raster._clip(painter, self.meta, self.settings.get('selection'))
            if self.settings['tool'] == 'eraser':
                painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            painter.drawImage(x * S + patch.x(), y * S + patch.y(), colored)
            painter.end()
            image = self.images.get((x, y))
            if image is None:
                image = raster.blank()
                painter = QPainter(image)
                if under is not None:
                    painter.drawImage(0, 0, under)
                painter.setOpacity(opacity)
                painter.drawImage(0, 0, layer)
                painter.setOpacity(1)
                if over is not None:
                    painter.drawImage(0, 0, over)
                painter.end()
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            painter.fillRect(patch, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            if under is not None:
                painter.drawImage(QRectF(patch), under, QRectF(patch))
            painter.setOpacity(opacity)
            painter.drawImage(patch.topLeft(), ink)
            painter.setOpacity(1)
            if over is not None:
                painter.drawImage(QRectF(patch), over, QRectF(patch))
            painter.end()
            self.images[(x, y)] = image
        return self.images
