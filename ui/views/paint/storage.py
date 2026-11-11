"""Versioned SQLite documents. No full-sized bitmap is allocated for a blank canvas.

Level 0 contains original pixels; level n contains pixels reduced by 2**n.
Each level uses 512-pixel tiles. White is implicit, including outside stored tiles.
Database connections and QImages belong to the calling worker, never to a widget.
"""

import json
import math
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRect, QSize, Qt, QSaveFile
from PySide6.QtGui import QColor, QImage, QImageReader, QImageWriter, QPainter

TILE_SIZE = 512
FORMAT_VERSION = 1
MAX_DIMENSION = 32768
WORLD_LIMIT = 2**24
MAX_EXPORT_BYTES = 512 * 1024 * 1024
_HEAVY_OPERATION_LOCK = threading.Lock()


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Operation cancelled")


def connect_read(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def read_metadata(path):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.metadata()
    with closing(connect_read(path)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != 0x54504E54:
            raise ValueError("This is not a Paint document.")
        raw = db.execute(
            "SELECT value FROM metadata WHERE key=?", ("document",)
        ).fetchone()
        if raw is None:
            raise ValueError("Missing canvas description.")
        meta = json.loads(raw[0])
        if not isinstance(meta, dict):
            raise ValueError("Invalid canvas description.")
        if meta.get("version") not in (1, 2) or meta.get("tile_size") != TILE_SIZE:
            raise ValueError("Unsupported Paint document version.")
        if meta.get("mode") not in ("finite", "endless"):
            raise ValueError("Invalid canvas mode.")
        for key in ("width", "height"):
            if type(meta.get(key)) is not int or not 1 <= meta[key] <= MAX_DIMENSION:
                raise ValueError("Invalid canvas dimensions.")
        if type(meta.get("max_level")) is not int or not 0 <= meta["max_level"] <= 16:
            raise ValueError("Invalid preview levels.")
        if type(meta.get("tile_count")) is not int or meta["tile_count"] < 0:
            raise ValueError("Invalid tile count.")
        bounds = meta.get("content_bounds")
        if bounds is not None:
            if (
                not isinstance(bounds, list)
                or len(bounds) != 4
                or any(type(value) is not int for value in bounds)
                or not all(-WORLD_LIMIT <= value <= WORLD_LIMIT for value in bounds[:2])
                or not all(1 <= value <= 2 * WORLD_LIMIT for value in bounds[2:])
            ):
                raise ValueError("Invalid content bounds.")
        elif meta["tile_count"]:
            raise ValueError("Missing content bounds.")
        return meta


def create_document(path, mode="finite", width=1024, height=1024, cancel=None):
    _check(cancel)
    if mode not in ("finite", "endless") or not (
        1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION
    ):
        raise ValueError("Invalid canvas dimensions.")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    meta = dict(
        version=FORMAT_VERSION,
        tile_size=TILE_SIZE,
        mode=mode,
        width=int(width),
        height=int(height),
        max_level=0,
        tile_count=0,
        background="#ffffff",
        content_bounds=None,
        dots_per_meter=[11811, 11811],
    )  # 300 DPI.
    if mode == "endless":
        meta["show_grid"] = True
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA application_id=1414549076")
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE tiles (level INTEGER NOT NULL, x INTEGER NOT NULL, "
            "y INTEGER NOT NULL, png BLOB NOT NULL, PRIMARY KEY(level,x,y)) WITHOUT ROWID"
        )
        db.execute("INSERT INTO metadata VALUES (?,?)", ("document", json.dumps(meta)))
    return meta


def encode_png(image):
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise IOError("Could not encode a canvas tile.")
    return bytes(buffer.data())


def decode_tile(png):
    buffer = QBuffer()
    buffer.setData(png)
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer, b"PNG")
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > TILE_SIZE:
        raise ValueError("Invalid canvas tile.")
    image = reader.read()
    if image.isNull():
        raise ValueError("Invalid canvas tile.")
    return image.convertToFormat(QImage.Format_ARGB32_Premultiplied)


def import_image(source, target, cancel=None):
    """One initial decode; the original and subsequent previews are released afterwards."""
    with _HEAVY_OPERATION_LOCK:
        return _import_image(source, target, cancel)


def _import_image(source, target, cancel=None):
    _check(cancel)
    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid():
        raise IOError(reader.errorString())
    if max(size.width(), size.height()) > MAX_DIMENSION:
        raise ValueError(
            f"Images larger than {MAX_DIMENSION} pixels per side are not supported."
        )
    needed = size.width() * size.height() * 4
    if needed > MAX_EXPORT_BYTES:
        raise ValueError(
            "This image is too large to import. Use an image with up to 128 million pixels."
        )
    # Qt's default 256 MiB limit is below A1 at 300 DPI. Qt only exposes a
    # global limit: extend it just for this bounded initial decode, then restore
    # it. Other Paint decoders validate their small tile dimensions first.
    previous_limit = QImageReader.allocationLimit()
    if previous_limit and needed > previous_limit * 1024 * 1024:
        QImageReader.setAllocationLimit(math.ceil(needed / (1024 * 1024)))
    try:
        image = reader.read()
    finally:
        if QImageReader.allocationLimit() != previous_limit:
            QImageReader.setAllocationLimit(previous_limit)
    if image.isNull():
        raise IOError(reader.errorString())
    if max(image.width(), image.height()) > MAX_DIMENSION:
        raise ValueError(
            f"Images larger than {MAX_DIMENSION} pixels per side are not supported."
        )
    try:
        meta = create_document(
            target, width=image.width(), height=image.height(), cancel=cancel
        )
        from .raster import initialize_image

        return initialize_image(target, image, cancel)
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise


def load_tiles(path, keys, cancel=None, *, budget_ms=None):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.load_tiles(keys, cancel, budget_ms)
    result = []
    import time

    deadline = time.monotonic() + budget_ms / 1000 if budget_ms is not None else None
    with closing(connect_read(path)) as db:
        for level, x, y in keys:
            _check(cancel)
            if result and deadline is not None and time.monotonic() >= deadline:
                break
            row = db.execute(
                "SELECT png FROM tiles WHERE level=? AND x=? AND y=?", (level, x, y)
            ).fetchone()
            image = None
            if row:
                image = decode_tile(row[0])
            result.append(((level, x, y), image))
    return result


def save_copy(source, destination, cancel=None):
    """Backup to an adjacent temporary file, then replace atomically."""
    destination = Path(destination)
    if Path(source).resolve() == destination.resolve():
        read_metadata(source)
        return str(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".paint-save-", suffix=".tmp", dir=destination.parent
    )
    os.close(fd)
    try:
        with closing(connect_read(source)) as src, closing(
            sqlite3.connect(temporary)
        ) as dst:
            src.backup(dst, pages=64, progress=lambda *_: _check(cancel))
        _check(cancel)
        # Windows FlushFileBuffers requires a writable handle. r+b preserves
        # the completed SQLite snapshot without truncating it.
        with open(temporary, "r+b") as saved:
            os.fsync(saved.fileno())
        os.replace(temporary, destination)
        return str(destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_region(
    source, rect, destination, format_name="PNG", quality=90, cancel=None
):
    with _HEAVY_OPERATION_LOCK:
        return _export_region(source, rect, destination, format_name, quality, cancel)


def _export_region(
    source, rect, destination, format_name="PNG", quality=90, cancel=None
):
    rect = QRect(rect)
    if rect.width() < 1 or rect.height() < 1:
        raise ValueError("Select an area with a positive width and height.")
    if rect.width() * rect.height() * 4 > MAX_EXPORT_BYTES:
        raise ValueError(
            "The selected area is too large for export. Select a smaller area "
            "(up to 128 million pixels)."
        )
    _check(cancel)
    meta = read_metadata(source)
    from .runtime import get_session

    session = get_session(source)
    if session is not None:
        image = session.render(rect, cancel)
    elif meta.get("version") == 2:
        from .raster import render_region

        with closing(connect_read(source)) as db:
            image = render_region(db, meta, rect, cancel=cancel)
    else:
        image = QImage(rect.size(), QImage.Format_RGB32)
        image.fill(Qt.white)
        painter = QPainter(image)
        try:
            with closing(connect_read(source)) as db:
                rows = db.execute(
                    "SELECT x,y,png FROM tiles WHERE level=0 AND x BETWEEN ? AND ? "
                    "AND y BETWEEN ? AND ?",
                    (
                        rect.left() // TILE_SIZE,
                        rect.right() // TILE_SIZE,
                        rect.top() // TILE_SIZE,
                        rect.bottom() // TILE_SIZE,
                    ),
                )
                for x, y, png in rows:
                    _check(cancel)
                    painter.drawImage(
                        x * TILE_SIZE - rect.x(),
                        y * TILE_SIZE - rect.y(),
                        decode_tile(png),
                    )
        finally:
            painter.end()
    if image.isNull():
        raise MemoryError("Not enough memory to export the selected area.")
    if format_name.upper() in ("JPEG", "JPG"):
        opaque = QImage(rect.size(), QImage.Format_RGB32)
        opaque.fill(Qt.white)
        painter = QPainter(opaque)
        painter.drawImage(0, 0, image)
        painter.end()
        image = opaque
    resolution = meta.get("dots_per_meter", [11811, 11811])
    if (
        isinstance(resolution, list)
        and len(resolution) == 2
        and all(type(value) is int and 0 < value < 1000000 for value in resolution)
    ):
        image.setDotsPerMeterX(resolution[0])
        image.setDotsPerMeterY(resolution[1])
    _check(cancel)
    output = QSaveFile(str(destination))
    if not output.open(QIODevice.WriteOnly):
        raise IOError(output.errorString())
    writer = QImageWriter(output, format_name.upper().encode("ascii"))
    if format_name.upper() in ("JPEG", "JPG"):
        writer.setQuality(max(1, min(100, int(quality))))
    if not writer.write(image):
        output.cancelWriting()
        raise IOError(writer.errorString())
    if cancel is not None and cancel.is_set():
        output.cancelWriting()
        raise InterruptedError("Operation cancelled")
    if not output.commit():
        raise IOError(output.errorString())
    return str(destination)
