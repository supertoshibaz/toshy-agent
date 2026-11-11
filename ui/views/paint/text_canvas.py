"""Редактирование векторного текста в координатах холста."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform, QTextCharFormat
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsRectItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView

from .text_objects import transform


class CanvasTextItem(QGraphicsTextItem):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setDocument(editor.edit.document())
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.setTransform(transform(editor.item))
        self.setTextWidth(editor.item.get('width', 420))

    def boundingRect(self):
        rect = super().boundingRect()
        if hasattr(self, 'editor'):
            rect.setHeight(max(rect.height(), float(self.editor.item.get('height', 0))))
        return rect

    def shape(self):
        # Пустая часть растянутой рамки тоже принадлежит редактируемому тексту.
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def contains(self, point):
        return self.boundingRect().contains(point)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        painter.setPen(QPen(QColor('#38bdf8'), 1, Qt.DashLine))
        pen = painter.pen()
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.boundingRect())

    def keyPressEvent(self, event):
        if self.editor.canvas.tool_key(event):
            event.accept()
            return
        newline = event.key() in (Qt.Key_Return, Qt.Key_Enter)
        fmt = QTextCharFormat(self.textCursor().charFormat()) if newline else None
        super().keyPressEvent(event)
        if newline:
            # Новый абзац получает формат курсора, а не значения темы или документа.
            cursor = self.textCursor()
            if not event.modifiers() & Qt.ShiftModifier:
                cursor.setBlockCharFormat(fmt)
            cursor.mergeCharFormat(fmt)
            self.setTextCursor(cursor)
        self.editor.sync_cursor()

    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)
        self.editor.sync_cursor()

    def inputMethodEvent(self, event):
        super().inputMethodEvent(event)
        self.editor.sync_cursor()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.editor.sync_cursor()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self.editor.sync_cursor()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.editor.sync_cursor()


class CanvasTextView(QGraphicsView):
    def __init__(self, editor):
        super().__init__(editor.canvas)
        self.editor = editor
        self.canvas = editor.canvas
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet('background:transparent; border:none;')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.viewport().setAutoFillBackground(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setMouseTracking(True)
        self.setScene(QGraphicsScene(self))
        self.item = CanvasTextItem(editor)
        self.scene().addItem(self.item)
        layer = next((layer for layer in self.canvas.meta.get('layers', [])
                      if layer['id'] == self.canvas._text_target), {})
        self.item.setOpacity(layer.get('opacity', 1.0))
        if self.canvas.meta['mode'] == 'finite':
            clip = QGraphicsRectItem(0, 0, self.canvas.meta['width'], self.canvas.meta['height'])
            clip.setPen(Qt.NoPen)
            clip.setBrush(Qt.NoBrush)
            clip.setFlag(QGraphicsItem.ItemClipsChildrenToShape)
            self.scene().addItem(clip)
            self.item.setParentItem(clip)
        self._panning = False
        self._resizing = None
        self.canvas.camera_changed.connect(self.sync_camera)
        self.sync_camera()
        self.show()
        self.setFocus()
        self.item.setFocus()

    def sync_camera(self):
        self.setGeometry(self.canvas.rect())
        self.setTransform(QTransform.fromScale(self.canvas.zoom, self.canvas.zoom))
        self.setSceneRect(QRectF(self.canvas.world_at(QPointF()),
            self.canvas.world_at(QPointF(self.canvas.width(), self.canvas.height()))))
        self.centerOn(self.canvas.center)
        if self.canvas._text_box is not None:
            self.canvas.prepare_preview()

    def mousePressEvent(self, event):
        handle = self.handle_at(event.position()) if event.button() == Qt.LeftButton else None
        if handle:
            inverse, _ = self.item.transform().inverted()
            self._resizing = dict(handle=handle, matrix=QTransform(self.item.transform()),
                inverse=inverse, start=inverse.map(self.mapToScene(event.position().toPoint())),
                rect=QRectF(self.item.boundingRect()))
            event.accept()
        elif event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
        elif event.button() == Qt.LeftButton and not self.item.contains(
                self.item.mapFromScene(self.mapToScene(event.position().toPoint()))):
            self.canvas.commit_text()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            self.resize_to(self.mapToScene(event.position().toPoint()))
            event.accept()
        elif self._panning:
            self.canvas.center -= (event.position() - self._pan) / self.canvas.zoom
            self._pan = event.position()
            self.canvas._changed()
            event.accept()
        else:
            handle = self.handle_at(event.position())
            if handle:
                cursors = {'top':Qt.SizeVerCursor, 'bottom':Qt.SizeVerCursor,
                    'left':Qt.SizeHorCursor, 'right':Qt.SizeHorCursor,
                    'top_left':Qt.SizeFDiagCursor, 'bottom_right':Qt.SizeFDiagCursor,
                    'top_right':Qt.SizeBDiagCursor, 'bottom_left':Qt.SizeBDiagCursor}
                self.viewport().setCursor(cursors[handle])
            else:
                self.viewport().unsetCursor()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing and event.button() == Qt.LeftButton:
            self.resize_to(self.mapToScene(event.position().toPoint()))
            self._resizing = None
            event.accept()
        elif self._panning and event.button() == Qt.MiddleButton:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def frame_handles(self):
        rect = self.item.boundingRect()
        center = rect.center()
        points = {'top_left':rect.topLeft(), 'top':QPointF(center.x(),rect.top()),
            'top_right':rect.topRight(), 'right':QPointF(rect.right(),center.y()),
            'bottom_right':rect.bottomRight(), 'bottom':QPointF(center.x(),rect.bottom()),
            'bottom_left':rect.bottomLeft(), 'left':QPointF(rect.left(),center.y())}
        return {name:self.mapFromScene(self.item.mapToScene(point)) for name,point in points.items()}

    def handle_at(self, position):
        if not self.item.isVisible():
            return None
        candidates = [((QPointF(point)-position).manhattanLength(),name)
                      for name,point in self.frame_handles().items()]
        distance, name = min(candidates)
        return name if distance <= 9 else None

    def resize_to(self, point):
        state = self._resizing
        handle, rect = state['handle'], state['rect']
        delta = state['inverse'].map(point) - state['start']
        width, height = rect.width(), rect.height()
        if 'left' in handle:
            width -= delta.x()
        elif 'right' in handle:
            width += delta.x()
        if 'top' in handle:
            height -= delta.y()
        elif 'bottom' in handle:
            height += delta.y()
        width, height = self.editor.resize_frame(max(16.,min(32768.,width)),max(1.,min(32768.,height)))
        matrix = QTransform(state['matrix'])
        matrix.translate(rect.width()-width if 'left' in handle else 0,
                         rect.height()-height if 'top' in handle else 0)
        self.item.setTransform(matrix)
        self.editor.item['matrix'] = [matrix.m11(),matrix.m12(),matrix.m21(),matrix.m22(),matrix.dx(),matrix.dy()]
        self.viewport().update()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if not self.item.isVisible():
            return
        painter.save()
        painter.resetTransform()
        painter.setPen(QPen(QColor('#38bdf8'),1))
        painter.setBrush(QColor('#fafafa'))
        for point in self.frame_handles().values():
            painter.drawRect(QRectF(point.x()-3,point.y()-3,6,6))
        painter.restore()

    def wheelEvent(self, event):
        self.canvas.wheelEvent(event)

    def keyPressEvent(self, event):
        if not self.canvas.tool_key(event):
            super().keyPressEvent(event)
