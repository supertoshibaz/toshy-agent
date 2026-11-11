"""Image MIME handling for Qt image objects, encoded clipboard data and files."""
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QImageReader, QPixmap


def can_import(mime):
    if mime is None:
        return False
    from ui.markdown_clipboard import payload
    data = payload(mime)
    return (bool(data and data['images']) or mime.hasImage() or any(fmt.startswith('image/') for fmt in mime.formats())
            or any(url.isLocalFile() and QImageReader(url.toLocalFile()).canRead()
                   for url in mime.urls()))


def sources(mime):
    """Return owned QImages or source paths; never retain clipboard-owned objects."""
    if mime is None:
        return []
    from ui.markdown_clipboard import image_paths
    private_paths = image_paths(mime)
    if private_paths:return [Path(path) for path in private_paths]
    paths = [Path(url.toLocalFile()) for url in mime.urls()
             if url.isLocalFile() and Path(url.toLocalFile()).is_file()
             and QImageReader(url.toLocalFile()).canRead()]
    if paths:
        return paths
    if mime.hasImage():
        image = mime.imageData()
        if isinstance(image, QPixmap):
            image = image.toImage()
        if isinstance(image, QImage) and not image.isNull():
            return [QImage(image)]
    for fmt in mime.formats():
        if not fmt.startswith('image/'):
            continue
        buffer = QBuffer()
        buffer.setData(mime.data(fmt))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()
        if not image.isNull():
            return [image]
    return []
