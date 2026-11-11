"""Lossless selection paths shared by canvas input and the Undo journal."""
from PySide6.QtCore import QByteArray, QDataStream, QIODevice
from PySide6.QtGui import QPainterPath


def encode(path):
    if path is None:
        return None
    data = QByteArray()
    stream = QDataStream(data, QIODevice.WriteOnly)
    stream.setVersion(QDataStream.Qt_6_0)
    stream << path
    return bytes(data.toHex()).decode('ascii')


def decode(value):
    if value is None:
        return None
    data = QByteArray.fromHex(str(value).encode('ascii'))
    stream = QDataStream(data, QIODevice.ReadOnly)
    stream.setVersion(QDataStream.Qt_6_0)
    path = QPainterPath()
    stream >> path
    return path if stream.status() == QDataStream.Ok else None
