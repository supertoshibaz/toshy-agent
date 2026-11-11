"""Render a multi-resolution Windows icon directly from the vector artwork."""
from pathlib import Path
import os
import struct
import sys
import tempfile


def write_icon(source, destination):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    application = QGuiApplication.instance() or QGuiApplication(['toshy-icon'])
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError('The Toshy SVG icon is invalid.')
    images = []
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        image = QImage(size, size, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            renderer.render(painter)
        finally:
            painter.end()
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.WriteOnly):
            raise OSError('Could not allocate the icon image buffer.')
        try:
            if not image.save(buffer, 'PNG'):
                raise OSError('Could not encode the icon image.')
        finally:
            buffer.close()
        images.append((size, bytes(data)))
    # Vista and newer support PNG images in ICO directories. Every frame is
    # rendered at its actual size, including fractional-DPI desktop sizes.
    offset = 6 + 16 * len(images)
    directory = []
    for size, data in images:
        dimension = size if size < 256 else 0
        directory.append(struct.pack('<BBBBHHII', dimension, dimension, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.toshy-icon-', suffix='.ico', delete=False) as output:
            temporary = Path(output.name)
            output.write(struct.pack('<HHH', 0, 1, len(images)))
            output.write(b''.join(directory))
            for _, data in images:
                output.write(data)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: windows_icon.py SOURCE.svg DESTINATION.ico')
    write_icon(sys.argv[1], sys.argv[2])
