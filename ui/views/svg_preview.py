"""Static SVG preview drawn by Qt, without a browser or external resources."""
import math
import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QSizePolicy, QWidget


def svg_bytes(source: str) -> bytes:
    if len(source.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('SVG слишком большой для предпросмотра (больше 2 МБ).')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', source, re.I):
        raise ValueError('Предпросмотр SVG не поддерживает DTD и внешние сущности.')
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise ValueError('SVG ещё не завершён или содержит ошибку XML: ' + str(error)) from error
    if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
        raise ValueError('В блоке должен быть корневой элемент <svg>.')
    for node in root.iter():
        for name, value in node.attrib.items():
            if name.rsplit('}', 1)[-1].lower() == 'href' and not value.startswith('#'):
                if not re.match(r'^data:image/(?:png|jpeg|gif|webp);base64,', value, re.I):
                    raise ValueError('Для предпросмотра используйте встроенные изображения и ссылки #id.')
        styles = '\n'.join(node.attrib.values()) + '\n' + (node.text or '')
        if re.search(r'@import\b', styles, re.I) or any(
                not value.strip(' \t\r\n\'\"').startswith('#')
                for value in re.findall(r'url\s*\((.*?)\)', styles, re.I | re.S)):
            raise ValueError('Внешние ресурсы в предпросмотре SVG не загружаются.')
    return source.encode('utf-8')


class SvgPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.renderer = QSvgRenderer(self)
        if hasattr(self.renderer, 'setOptions'):
            from PySide6.QtSvg import QtSvg
            option = getattr(QtSvg.Option, 'DisableAnimations', None)
            if option is not None:
                self.renderer.setOptions(option)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._aspect = 1.0

    def set_source(self, source):
        if not self.renderer.load(QByteArray(svg_bytes(source))) or not self.renderer.isValid():
            raise ValueError('Qt не удалось отрендерить этот SVG.')
        self.renderer.setFramesPerSecond(0)
        extent = self.renderer.viewBoxF()
        ratio = extent.width() / extent.height() if extent.height() > 0 else 1.0
        self._aspect = ratio if math.isfinite(ratio) and ratio > 0 else 1.0
        self._resize_preview()
        self.update()

    def sizeHint(self):
        return QSize(480, min(640, max(100, round(480 / self._aspect))))

    def _resize_preview(self):
        self.setFixedHeight(min(640, max(100, round(max(1, self.width() - 24) / self._aspect) + 24)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_preview()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#99999f'))
        for y in range(0, self.height(), 16):
            for x in range((y // 16 % 2) * 16, self.width(), 32):
                painter.fillRect(x, y, 16, 16, QColor('#7a7a82'))
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(12, 12, -12, -12)
        width = min(bounds.width(), bounds.height() * self._aspect)
        height = width / self._aspect
        target = QRectF(bounds.center().x() - width / 2, bounds.center().y() - height / 2, width, height)
        self.renderer.render(painter, target)
        painter.end()
