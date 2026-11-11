"""Lightweight card overviews. Reuse image dimensions without decoding pixels."""
from ui.themes.manager import theme_manager
import hashlib
import re
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QTextLayout, QTextOption, QImageReader

from ui.views.inline_images import image_reference, resolve_image


class OverviewCache:
    def __init__(self, limit=32 * 1024 * 1024):
        self.limit = limit
        self.bytes_used = 0
        self.entries = OrderedDict()
        self.image_rects = {}

    def get(self, key):
        geometry = self.entries.get(key)
        if geometry is not None:
            self.entries.move_to_end(key)
        return geometry

    def build(self, source, width, height, alignment, font=None, image_sizes=(), base_path=None):
        # Cache vector rectangles in document coordinates. Zoom never rebuilds
        # the geometry or stretches a tiny raster to an unrelated card height.
        lines = source[:16000].splitlines()[:256]
        font = QFont(font) if font is not None else QFont()
        width, height = max(1, width), max(1, height)
        key = (hashlib.sha256(source[:16000].encode('utf-8')).digest(), width, height, alignment, font.toString(), tuple(image_sizes), str(resolve_image(".", base_path)))
        if self.get(key) is not None:
            return key, self.image_rects.get(key, ())
        rects, images, y, image_index = [], [], 0.0, 0
        fence = None
        for raw in lines:
            marker = re.match(r'^\s*(`{3,}|~{3,})', raw)
            if marker:
                if fence is None:
                    fence = marker[1][0]
                elif marker[1][0] == fence:
                    fence = None
                continue
            if fence is None and image_reference(raw):
                iw, ih = image_sizes[image_index] if image_index < len(image_sizes) else (0, 0)
                if iw <= 0:
                    # A card first seen at a distance may never have decoded
                    # its preview. Read only the image header on a cache miss.
                    path = resolve_image(image_reference(raw)[1], base_path)
                    size = QImageReader(str(path)).size() if path else None
                    if size is not None and size.isValid():
                        iw, ih = size.width(), size.height()
                image_index += 1
                image_width = min(width, iw) if iw > 0 else width
                image_height = image_width * ih / iw if iw > 0 else QFontMetricsF(font).lineSpacing()*2
                x = width-image_width if alignment == 'right' else (width-image_width)/2 if alignment in ('center', 'card_center') else 0
                images.append(QRectF(x, y, image_width, image_height))
                y += image_height + 4
                if y >= height:
                    break
                continue
            line_font = QFont(font)
            heading = re.match(r'^(#{1,5})\s+(.*)', raw)
            if heading:
                line_font.setPixelSize({1:22, 2:19, 3:17, 4:15, 5:14}[len(heading[1])])
                line_font.setBold(True)
                raw = heading[2]
            text = re.sub(r'[\*_`~]', '', raw).expandtabs(4)
            metrics = QFontMetricsF(line_font)
            if not text:
                y += metrics.lineSpacing()
                continue
            layout = QTextLayout(text, line_font)
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            layout.setTextOption(option)
            encoded = text.encode('utf-16-le')
            layout.beginLayout()
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                start, count = line.textStart(), line.textLength()
                segment = encoded[start*2:(start+count)*2].decode('utf-16-le')
                row_width = line.naturalTextWidth()
                x = width-row_width if alignment == 'right' else (width-row_width)/2 if alignment in ('center', 'card_center') else 0
                thickness = max(1., metrics.xHeight() * .7)
                offsets = [0]
                for character in segment:
                    offsets.append(offsets[-1] + (2 if ord(character) > 0xffff else 1))
                for run in re.finditer(r'\S+', segment):
                    left = line.cursorToX(start + offsets[run.start()])[0]
                    right = line.cursorToX(start + offsets[run.end()])[0]
                    rects.append(QRectF(x+min(left,right), y+(metrics.height()-thickness)/2,
                                       max(1.,abs(right-left)), thickness))
                y += line.height()
                if y >= height:
                    break
            layout.endLayout()
            if y >= height:
                break
        geometry = tuple(rects)
        cost = 128 + (len(geometry) + len(images))*64
        while self.entries and self.bytes_used + cost > self.limit:
            old_key, old = self.entries.popitem(last=False)
            old_images = self.image_rects.pop(old_key, ())
            self.bytes_used -= 128 + (len(old) + len(old_images))*64
        if cost <= self.limit:
            self.entries[key] = geometry
            self.image_rects[key] = tuple(images)
            self.bytes_used += cost
        return key, tuple(images)


overview_cache = OverviewCache()


def paint_symbol(painter, rect, image=False):
    """Use the shared icon set for the overview badge."""
    if image:
        from ui.icons import paint
        paint(painter, rect, 'image')
        return
    painter.save()
    painter.setBrush(theme_manager.color('background.surface'))
    pen = QPen(theme_manager.color('border.subtle'))
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.drawRoundedRect(rect, rect.height() * .16, rect.height() * .16)
    inner = rect.adjusted(rect.width() * .23, rect.height() * .2, -rect.width() * .23, -rect.height() * .2)
    painter.setBrush(Qt.NoBrush)
    pen.setColor(theme_manager.color('text.secondary'))
    painter.setPen(pen)
    painter.drawRect(inner)
    if image:
        painter.drawEllipse(inner.topLeft() + QPointF(inner.width() * .72, inner.height() * .25), inner.width() * .08, inner.height() * .08)
        painter.drawLine(inner.bottomLeft(), inner.center())
        painter.drawLine(inner.center(), inner.bottomRight())
    else:
        for fraction in (.3, .5, .7):
            y = inner.top() + inner.height() * fraction
            painter.drawLine(QPointF(inner.left() + inner.width() * .15, y), QPointF(inner.right() - inner.width() * .15, y))
    painter.restore()
