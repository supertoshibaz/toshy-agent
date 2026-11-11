"""Transactional sparse raster editing; compressed Undo lives on disk.

The original document is copied to a private working file on its first edit.
Every edit, including all affected layers, commits atomically. Blank pixels and
the white background remain implicit. Preview tiles never contain background.
"""

import json
import base64
import math
import sqlite3
import os
import tempfile
from pathlib import Path
from collections import deque
from contextlib import closing

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTransform,
)

from .storage import (
    TILE_SIZE as S,
    WORLD_LIMIT,
    _check,
    connect_read,
    create_document,
    decode_tile,
    encode_png,
    read_metadata,
    load_tiles,
    save_copy,
)

MAX_EDIT_PIXELS = 128 * 1024 * 1024
MAX_FILL_PIXELS = 1024 * 1024
FILL_WINDOW = 2048


def default_layers(meta):
    return meta.get("layers") or [
        dict(
            id="background",
            name="Background",
            background=True,
            visible=True,
            opacity=1.0,
            bounds=None,
        ),
        dict(
            id="paint",
            name="Layer 1",
            background=False,
            visible=True,
            opacity=1.0,
            bounds=meta.get("content_bounds"),
        ),
    ]


def blank(width=S, height=S):
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    return image


def _bounds(image):
    if image.isNull():
        return None
    if not image.hasAlphaChannel():
        return [0, 0, image.width(), image.height()]
    if image.format() in (QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied):
        # QRgb is a native-endian 0xAARRGGBB word on every Qt platform.
        # Working tiles already use this format; copying/converting a whole
        # tile for each alpha scan stalls high-frequency brush updates.
        pixels = np.frombuffer(image.constBits(), dtype=np.uint32).reshape(
            image.height(), image.bytesPerLine() // 4
        )[:, : image.width()]
        alpha = pixels >> 24
    else:
        rgba = image.convertToFormat(QImage.Format_RGBA8888)
        rows = np.frombuffer(rgba.constBits(), np.uint8).reshape(
            rgba.height(), rgba.bytesPerLine()
        )
        alpha = rows[:, 3 : rgba.width() * 4 : 4]
    ys = np.flatnonzero(alpha.any(axis=1))
    xs = np.flatnonzero(alpha.any(axis=0))
    return (
        [
            int(xs.min()),
            int(ys.min()),
            int(xs.max() - xs.min() + 1),
            int(ys.max() - ys.min() + 1),
        ]
        if len(xs)
        else None
    )


def _metadata(db):
    return json.loads(
        db.execute("SELECT value FROM metadata WHERE key='document'").fetchone()[0]
    )


def _put_meta(db, meta):
    db.execute("UPDATE metadata SET value=? WHERE key='document'", (json.dumps(meta),))


def initialize(path, original=None, cancel=None, *, create_background=True):
    with closing(sqlite3.connect(path)) as db, db:
        meta = _metadata(db)
        if meta.get("version") == 2:
            if original and not meta.get("baseline"):
                meta["baseline"] = original
                _put_meta(db, meta)
            if create_background:
                materialize_background(db, meta, cancel)
            return meta
        db.execute(
            "CREATE TABLE layer_tiles (layer TEXT,x INTEGER,y INTEGER,png BLOB,bounds TEXT, "
            "PRIMARY KEY(layer,x,y)) WITHOUT ROWID"
        )
        db.execute(
            "CREATE TABLE history (id INTEGER PRIMARY KEY, before TEXT, after TEXT)"
        )
        db.execute(
            "CREATE TABLE changes (id INTEGER,layer TEXT,x INTEGER,y INTEGER,before BLOB,after BLOB, "
            "PRIMARY KEY(id,layer,x,y)) WITHOUT ROWID"
        )
        db.execute(
            "CREATE TABLE dirty_tiles (layer TEXT,x INTEGER,y INTEGER,PRIMARY KEY(layer,x,y)) WITHOUT ROWID"
        )
        db.execute(
            "CREATE TABLE dirty_previews (level INTEGER,x INTEGER,y INTEGER,PRIMARY KEY(level,x,y)) WITHOUT ROWID"
        )
        # Legacy pixels are retained exactly; existing files stay compatible.
        for x, y, png in db.execute("SELECT x,y,png FROM tiles WHERE level=0"):
            _check(cancel)
            db.execute(
                "INSERT INTO layer_tiles VALUES (?,?,?,?,?)",
                ("paint", x, y, png, json.dumps([0, 0, S, S])),
            )
        meta.update(
            version=2,
            layers=default_layers(meta),
            active_layer="paint",
            undo_pos=0,
            undo_max=0,
            clean_pos=0,
            baseline=original or "",
        )
        _put_meta(db, meta)
        if create_background:
            materialize_background(db, meta, cancel)
        return meta


def materialize_background(db, meta, cancel=None):
    """Convert finite legacy backdrop into actual, editable tiled pixels.

    Uniform tiles share an encoded/image handle. Neither a whole-canvas bitmap
    nor one independent RAM allocation for every white tile is required.
    Endless uses a document backdrop, not a layer of unbounded pixels.
    """
    layers = default_layers(meta)
    backgrounds = [layer for layer in layers if layer.get("background")]
    if not backgrounds:
        return False
    old = backgrounds[0]
    if meta["mode"] == "endless":
        meta.update(
            background_visible=old["visible"], background_opacity=old["opacity"]
        )
        meta["layers"] = [layer for layer in layers if not layer.get("background")]
        if not meta["layers"]:
            meta["layers"] = [
                dict(
                    id="paint",
                    name="Layer 1",
                    background=False,
                    visible=True,
                    opacity=1.0,
                    bounds=None,
                )
            ]
        if meta.get("active_layer") not in {l["id"] for l in meta["layers"]}:
            meta["active_layer"] = meta["layers"][-1]["id"]
        _put_meta(db, meta)
        return True
    occupied = set(db.execute("SELECT DISTINCT x,y FROM layer_tiles"))
    width, height = meta["width"], meta["height"]
    old.update(background=False, bounds=[0, 0, width, height])
    meta["layers"] = layers
    meta["content_bounds"] = [0, 0, width, height]
    meta["max_level"] = max(
        meta["max_level"], math.ceil(math.log2(max(1, max(width, height) / S)))
    )
    _put_meta(db, meta)
    encoded = {}

    def uniform(w, h, alpha=255):
        key = (w, h, alpha)
        if key not in encoded:
            image = blank()
            p = QPainter(image)
            p.fillRect(0, 0, w, h, QColor(255, 255, 255, alpha))
            p.end()
            encoded[key] = _encode(db, image)
        return encoded[key]

    for y in range(math.ceil(height / S)):
        for x in range(math.ceil(width / S)):
            _check(cancel)
            w, h = min(S, width - x * S), min(S, height - y * S)
            db.execute(
                "INSERT OR REPLACE INTO layer_tiles VALUES (?,?,?,?,?)",
                (old["id"], x, y, uniform(w, h), json.dumps([0, 0, w, h])),
            )
            db.execute(
                "INSERT OR IGNORE INTO dirty_tiles VALUES (?,?,?)", (old["id"], x, y)
            )
    db.execute("DELETE FROM tiles")
    alpha = round(old["opacity"] * 255) if old["visible"] else 0
    if alpha:
        for level in range(meta["max_level"] + 1):
            w, h = math.ceil(width / 2**level), math.ceil(height / 2**level)
            for y in range(math.ceil(h / S)):
                for x in range(math.ceil(w / S)):
                    _check(cancel)
                    db.execute(
                        "INSERT INTO tiles VALUES (?,?,?,?)",
                        (
                            level,
                            x,
                            y,
                            uniform(min(S, w - x * S), min(S, h - y * S), alpha),
                        ),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO dirty_previews VALUES (?,?,?)",
                        (level, x, y),
                    )
    if getattr(db, "session", None) is not None:
        db.session.dirty_bounds.add(old["id"])
    _compose(db, meta, occupied, cancel)
    _put_meta(db, meta)
    return True


def background_colour(meta):
    if meta["mode"] != "endless":
        return None
    legacy = next((l for l in default_layers(meta) if l.get("background")), None)
    visible = meta.get("background_visible", legacy["visible"] if legacy else True)
    opacity = meta.get("background_opacity", legacy["opacity"] if legacy else 1.0)
    colour = QColor(meta.get("background", "#ffffff"))
    colour.setAlphaF(opacity if visible else 0)
    return colour


def prepare(source, target, original="", cancel=None):
    if source != target:
        save_copy(source, target, cancel)
    return initialize(target, original, cancel)


def _keys(rect):
    rect = QRect(rect)
    if rect.isEmpty():
        return []
    if rect.width() * rect.height() > MAX_EDIT_PIXELS:
        raise ValueError(
            "The operation is too large. Select a smaller area (up to 128 million pixels)."
        )
    if (
        max(abs(rect.left()), abs(rect.top()), abs(rect.right()), abs(rect.bottom()))
        > WORLD_LIMIT
    ):
        raise ValueError("The operation exceeds the canvas coordinate limit.")
    return [
        (x, y)
        for y in range(rect.top() // S, rect.bottom() // S + 1)
        for x in range(rect.left() // S, rect.right() // S + 1)
    ]


def _tile(db, layer, x, y):
    row = db.execute(
        "SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?", (layer, x, y)
    ).fetchone()
    return _decode(db, row[0]) if row else blank()


def _decode(db, value):
    """Working databases hold immutable image handles, files hold PNG bytes."""
    codec = getattr(db, "images", None)
    return codec.get(value) if codec is not None else decode_tile(value)


def _encode(db, image):
    codec = getattr(db, "images", None)
    return codec.put(image) if codec is not None else encode_png(image)


def _write(db, action, layer, x, y, image):
    old = db.execute(
        "SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?", (layer, x, y)
    ).fetchone()
    before = old[0] if old else None
    bounds = _bounds(image)
    if getattr(db, "images", None) is not None and before and bounds:
        if image == _decode(db, before):
            return False
    after = _encode(db, image) if bounds else None
    if before == after:
        return False
    if action is not None:
        row = db.execute(
            "SELECT before FROM changes WHERE id=? AND layer=? AND x=? AND y=?",
            (action, layer, x, y),
        ).fetchone()
        db.execute(
            "INSERT OR REPLACE INTO changes VALUES (?,?,?,?,?,?)",
            (action, layer, x, y, row[0] if row else before, after),
        )
    if after:
        db.execute(
            "INSERT OR REPLACE INTO layer_tiles VALUES (?,?,?,?,?)",
            (layer, x, y, after, json.dumps(bounds)),
        )
    else:
        db.execute(
            "DELETE FROM layer_tiles WHERE layer=? AND x=? AND y=?", (layer, x, y)
        )
    db.execute("INSERT OR IGNORE INTO dirty_tiles VALUES (?,?,?)", (layer, x, y))
    if getattr(db, "session", None) is not None:
        db.session.dirty_bounds.add(layer)
        db.session.retire(before)
    return True


def path_keys(path, width=0, filled=False):
    """Visit actual geometry, including antialiased edges, not its whole box."""
    if filled:
        outline = path
    else:
        stroker = QPainterPathStroker()
        stroker.setWidth(width + 2)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        outline = stroker.createStroke(path)
    return [
        key
        for key in _keys(outline.boundingRect().adjusted(-1, -1, 1, 1).toAlignedRect())
        if outline.intersects(QRectF(key[0] * S - 1, key[1] * S - 1, S + 2, S + 2))
    ]


def _refresh_bounds(db, meta):
    if getattr(db, "session", None) is not None:
        return db.session.refresh_bounds(meta)
    all_bounds = []
    for layer in meta["layers"]:
        bounds = None
        if not layer.get("background"):
            for x, y, raw in db.execute(
                "SELECT x,y,bounds FROM layer_tiles WHERE layer=?", (layer["id"],)
            ):
                b = json.loads(raw)
                r = QRect(x * S + b[0], y * S + b[1], b[2], b[3])
                bounds = r if bounds is None else bounds.united(r)
        layer["bounds"] = (
            [bounds.x(), bounds.y(), bounds.width(), bounds.height()]
            if bounds
            else None
        )
        if bounds:
            if meta["mode"] == "finite":
                thumb_bounds = QRect(0, 0, meta["width"], meta["height"])
            else:
                side = max(bounds.width(), bounds.height())
                thumb_bounds = QRect(
                    round(bounds.center().x() - side / 2),
                    round(bounds.center().y() - side / 2),
                    side,
                    side,
                )
            thumb = _preview_layers(
                db,
                meta,
                [layer["id"]],
                bounds_override=thumb_bounds,
                max_side=64,
                include_hidden=True,
                apply_opacity=False,
            )[0]
            layer["thumbnail"] = base64.b64encode(encode_png(thumb)).decode("ascii")
        else:
            layer.pop("thumbnail", None)
        if bounds:
            all_bounds.append(bounds)
    bounds = None
    for r in all_bounds:
        bounds = r if bounds is None else bounds.united(r)
    meta["content_bounds"] = (
        [bounds.x(), bounds.y(), bounds.width(), bounds.height()] if bounds else None
    )
    extent = max(bounds.width(), bounds.height()) if bounds else 1
    return min(16, max(0, math.ceil(math.log2(max(1, extent / S)))))


def _put_preview(db, level, x, y, image):
    session = getattr(db, 'session', None)
    if session is not None and session.stroke is not None:
        previous = db.execute('SELECT png FROM tiles WHERE level=? AND x=? AND y=?', (level, x, y)).fetchone()
        if previous:
            session.retire(previous[0])
    present = _bounds(image) is not None
    if present:
        db.execute(
            "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
            (level, x, y, _encode(db, image)),
        )
    else:
        db.execute("DELETE FROM tiles WHERE level=? AND x=? AND y=?", (level, x, y))
    db.execute("INSERT OR IGNORE INTO dirty_previews VALUES (?,?,?)", (level, x, y))
    return present


def _compose(db, meta, coords, cancel=None):
    if getattr(db, "session", None) is not None:
        return db.session.compose(meta, coords, cancel)
    old_max = meta.get("max_level", 0)
    max_level = _refresh_bounds(db, meta)
    for x, y in coords:
        _check(cancel)
        image = blank()
        painter = QPainter(image)
        for layer in meta["layers"]:
            if layer.get("background") or not layer["visible"] or not layer["opacity"]:
                continue
            row = db.execute(
                "SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?",
                (layer["id"], x, y),
            ).fetchone()
            if row:
                painter.setOpacity(layer["opacity"])
                painter.drawImage(0, 0, _decode(db, row[0]))
        painter.end()
        _put_preview(db, 0, x, y, image)
    parents = set(coords)
    for level in range(1, max(old_max, max_level) + 1):
        if level > old_max:
            parents.update(
                db.execute("SELECT x,y FROM tiles WHERE level=?", (level - 1,))
            )
        parents = {(x // 2, y // 2) for x, y in parents}
        for x, y in parents:
            _check(cancel)
            image = blank()
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            for dy in (0, 1):
                for dx in (0, 1):
                    row = db.execute(
                        "SELECT png FROM tiles WHERE level=? AND x=? AND y=?",
                        (level - 1, x * 2 + dx, y * 2 + dy),
                    ).fetchone()
                    if row:
                        painter.drawImage(
                            QRectF(dx * S / 2, dy * S / 2, S / 2, S / 2),
                            _decode(db, row[0]),
                        )
            painter.end()
            _put_preview(db, level, x, y, image)
    meta["max_level"] = max(old_max, max_level)
    meta["tile_count"] = db.execute(
        "SELECT count(*) FROM tiles WHERE level=0"
    ).fetchone()[0]


def initialize_image(path, image, cancel=None):
    initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        meta = _metadata(db)
        meta["dots_per_meter"] = [image.dotsPerMeterX(), image.dotsPerMeterY()]
        coords = []
        for y in range(math.ceil(image.height() / S)):
            for x in range(math.ceil(image.width() / S)):
                _check(cancel)
                tile = blank()
                p = QPainter(tile)
                p.drawImage(-x * S, -y * S, image)
                p.end()
                _write(db, None, "paint", x, y, tile)
                coords.append((x, y))
        _compose(db, meta, coords, cancel)
        db.execute("DELETE FROM dirty_tiles")
        db.execute("DELETE FROM dirty_previews")
        _put_meta(db, meta)
        return meta


def render_region(db, meta, rect, layer=None, cancel=None):
    image = blank(rect.width(), rect.height())
    p = QPainter(image)
    if layer is None:
        colour = background_colour(meta)
        if colour is not None:
            p.fillRect(image.rect(), colour)
        elif meta["mode"] == "finite":
            legacy = next(
                (l for l in default_layers(meta) if l.get("background")), None
            )
            if legacy and legacy["visible"]:
                p.fillRect(
                    image.rect(), QColor(255, 255, 255, round(legacy["opacity"] * 255))
                )
        rows = db.execute(
            "SELECT x,y,png FROM tiles WHERE level=0 AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (rect.left() // S, rect.right() // S, rect.top() // S, rect.bottom() // S),
        )
    else:
        rows = db.execute(
            "SELECT x,y,png FROM layer_tiles WHERE layer=? AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
            (
                layer,
                rect.left() // S,
                rect.right() // S,
                rect.top() // S,
                rect.bottom() // S,
            ),
        )
    try:
        for x, y, png in rows:
            _check(cancel)
            p.drawImage(x * S - rect.x(), y * S - rect.y(), _decode(db, png))
    finally:
        p.end()
    return image


def _clip(p, meta, selection, clip_canvas=True):
    if clip_canvas and meta["mode"] == "finite":
        p.setClipRect(QRectF(0, 0, meta["width"], meta["height"]))
    if selection is not None:
        p.setClipPath(selection, Qt.IntersectClip)


def _paint(
    db, meta, action, layer, rect, callback, selection=None, cancel=None, keys=None,
    *, clip_canvas=True, antialias=True
):
    if clip_canvas and meta["mode"] == "finite":
        rect = rect.intersected(QRect(0, 0, meta["width"], meta["height"]))
    if selection is not None:
        rect = rect.intersected(selection.boundingRect().toAlignedRect())
    coords = (
        _keys(rect)
        if keys is None
        else [
            key for key in keys if QRect(key[0] * S, key[1] * S, S, S).intersects(rect)
        ]
    )
    changed = set()
    for x, y in coords:
        _check(cancel)
        image = _tile(db, layer, x, y)
        p = QPainter(image)
        p.setRenderHint(QPainter.Antialiasing,antialias)
        p.translate(-x * S, -y * S)
        _clip(p, meta, selection, clip_canvas)
        try:
            callback(p)
        finally:
            p.end()
        if _write(db, action, layer, x, y, image):
            changed.add((x, y))
    return changed


def _fill(db, meta, payload, cancel):
    x, y = map(math.floor, payload["point"])
    rect = QRect(x - FILL_WINDOW // 2, y - FILL_WINDOW // 2, FILL_WINDOW, FILL_WINDOW)
    canvas = QRect(0, 0, meta["width"], meta["height"])
    if meta["mode"] == "finite":
        if not canvas.contains(x, y):
            raise ValueError("The fill point is outside the canvas.")
        rect = rect.intersected(canvas)
    image = render_region(db, meta, rect, cancel=cancel).convertToFormat(
        QImage.Format_RGBA8888
    )
    pixels = np.frombuffer(image.constBits(), np.uint8).reshape(
        rect.height(), image.bytesPerLine()
    )[:, : rect.width() * 4]
    pixels = pixels.reshape(rect.height(), rect.width(), 4)
    sx, sy = x - rect.x(), y - rect.y()
    target = pixels[sy, sx].astype(np.int16)
    allowed = np.max(np.abs(pixels.astype(np.int16) - target), axis=2) <= 8
    selection = payload.get("selection")
    if selection is not None:
        mask = blank(rect.width(), rect.height())
        p = QPainter(mask)
        p.translate(-rect.x(), -rect.y())
        p.fillPath(selection, Qt.white)
        p.end()
        allowed &= (
            np.frombuffer(mask.constBits(), np.uint8).reshape(
                rect.height(), mask.bytesPerLine()
            )[:, 3 : rect.width() * 4 : 4]
            > 0
        )
    if not allowed[sy, sx]:
        return rect, blank(rect.width(), rect.height())
    reached = np.zeros(allowed.shape, np.bool_)
    queue = deque([(sx, sy)])
    count = 0
    while queue:
        _check(cancel)
        cx, cy = queue.popleft()
        if reached[cy, cx] or not allowed[cy, cx]:
            continue
        # Find the enclosing span in native code, rather than Python per pixel.
        blocked = np.flatnonzero(~allowed[cy] | reached[cy])
        boundary = int(np.searchsorted(blocked, cx))
        left = int(blocked[boundary - 1]) + 1 if boundary else 0
        right = int(blocked[boundary]) - 1 if boundary < len(blocked) else rect.width() - 1
        reached[cy, left : right + 1] = True
        count += right - left + 1
        if count > MAX_FILL_PIXELS:
            raise ValueError("Fill cancelled: the region exceeds 1,048,576 pixels.")
        escaped = (left == 0 and (meta["mode"] == "endless" or rect.left() > 0)) or (
            right == rect.width() - 1
            and (meta["mode"] == "endless" or rect.right() < canvas.right())
        )
        escaped |= (cy == 0 and (meta["mode"] == "endless" or rect.top() > 0)) or (
            cy == rect.height() - 1
            and (meta["mode"] == "endless" or rect.bottom() < canvas.bottom())
        )
        if escaped:
            raise ValueError(
                "Fill cancelled: the region is open or exceeds the safe search area."
            )
        for ny in (cy - 1, cy + 1):
            if 0 <= ny < rect.height():
                candidates = (
                    allowed[ny, left : right + 1] & ~reached[ny, left : right + 1]
                )
                starts = np.flatnonzero(candidates & ~np.r_[False, candidates[:-1]])
                queue.extend((left + int(start), ny) for start in starts)
    rgba = np.zeros((rect.height(), rect.width(), 4), np.uint8)
    color = QColor(payload["color"])
    color.setAlphaF(payload["opacity"])
    rgba[reached] = [color.red(), color.green(), color.blue(), color.alpha()]
    fill = QImage(
        rgba.data, rect.width(), rect.height(), rect.width() * 4, QImage.Format_RGBA8888
    ).copy()
    return rect, fill


def _transform_layers(db, meta, action, targets, kind, payload, cancel):
    """Snapshot compressed source tiles on disk, then clear and transform them.

    Iterating a snapshot prevents overlapping moves from reading pixels already
    moved by an earlier tile. Widely separated marks never need one huge image.
    """
    selection = payload.get("selection")
    transform = QTransform(*payload.get("matrix", [1, 0, 0, 1, 0, 0]))
    coords = set()
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS transform_source (x INTEGER,y INTEGER,png BLOB,bounds TEXT,PRIMARY KEY(x,y)) WITHOUT ROWID"
    )
    for identity in targets:
        _check(cancel)
        layer = next(x for x in meta['layers'] if x['id']==identity)
        if layer.get('kind') == 'vector':
            from .text_objects import move
            coords |= move(db, meta, action, layer, kind, payload, cancel)
            continue
        db.execute("DELETE FROM transform_source")
        query = "INSERT INTO transform_source SELECT x,y,png,bounds FROM layer_tiles WHERE layer=?"
        args = (identity,)
        if selection is not None:
            bounds = selection.boundingRect().toAlignedRect()
            query += " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?"
            args += (
                bounds.left() // S,
                bounds.right() // S,
                bounds.top() // S,
                bounds.bottom() // S,
            )
        db.execute(query, args)
        for x, y in db.execute("SELECT x,y FROM transform_source"):
            _check(cancel)
            if selection is None:
                _write(db, action, identity, x, y, blank())
                coords.add((x, y))
            else:
                rect = QRect(x * S, y * S, S, S)

                def erase(p):
                    p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                    p.fillRect(rect, Qt.white)

                # Use the same binary path coverage as source extraction.
                # Mixing an antialiased cut with a hard mask leaves edge debris.
                coords |= _paint(
                    db, meta, action, identity, rect, erase, selection, cancel, clip_canvas=False, antialias=False
                )
        if kind == "delete_selection":
            continue
        # Render each destination tile once from an immutable source mosaic.
        # Bilinear filtering can cross source tile edges without sampling their
        # artificial transparent borders or compositing translucent ink twice.
        source_bounds = QRect()
        for x, y, raw in db.execute("SELECT x,y,bounds FROM transform_source"):
            rect = QRect(*json.loads(raw)).translated(x*S, y*S)
            source_bounds = rect if source_bounds.isEmpty() else source_bounds.united(rect)
        if selection is not None:
            source_bounds = source_bounds.intersected(selection.boundingRect().toAlignedRect())
        if source_bounds.isEmpty():
            continue
        inverse, invertible = transform.inverted()
        if not invertible:
            raise ValueError("The selection transform is not invertible.")
        destination = transform.mapRect(QRectF(source_bounds)).toAlignedRect()
        for dx, dy in _keys(destination):
            _check(cancel)
            tile_rect = QRect(dx*S, dy*S, S, S)
            source_rect = inverse.mapRect(QRectF(tile_rect)).adjusted(-2,-2,2,2).toAlignedRect().intersected(source_bounds)
            if source_rect.isEmpty():
                continue
            _keys(source_rect)  # Bound allocation even for extreme downscaling.
            pixels = blank(source_rect.width(), source_rect.height())
            painter = QPainter(pixels)
            painter.translate(-source_rect.x(), -source_rect.y())
            if selection is not None:
                painter.setClipPath(selection)
            try:
                for x,y,png in db.execute(
                    "SELECT x,y,png FROM transform_source WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ?",
                    (source_rect.left()//S,source_rect.right()//S,source_rect.top()//S,source_rect.bottom()//S)):
                    _check(cancel)
                    painter.drawImage(x*S,y*S,_decode(db,png))
            finally:
                painter.end()
            image = _tile(db,identity,dx,dy)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.translate(-dx*S,-dy*S)
            painter.setTransform(transform,True)
            painter.drawImage(source_rect.topLeft(),pixels)
            painter.end()
            if _write(db,action,identity,dx,dy,image):
                coords.add((dx,dy))
    db.execute("DELETE FROM transform_source")
    return coords


def operate(path, kind, payload, cancel=None):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.operate(kind, payload, cancel)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA temp_store=FILE")
        return _operate(db, kind, payload, cancel)


def _operate(db, kind, payload, cancel=None):
    meta = _metadata(db)
    # Navigation/backdrop preferences are document metadata, not paint actions.
    if kind == "canvas":
        if meta["mode"] != "endless":
            raise ValueError("Canvas backdrop settings require Endless mode.")
        changed = False
        for key in ("background", "show_axes", "show_grid"):
            if key in payload and meta.get(key) != payload[key]:
                meta[key] = payload[key]
                changed = True
        if "background" in payload:
            meta.update(background_visible=True, background_opacity=1.0)
        if changed:
            meta["settings_revision"] = meta.get("settings_revision", 0) + 1
            _put_meta(db, meta)
        return meta
    if kind in ("undo", "redo"):
        pos = meta["undo_pos"]
        step = -1 if kind == "undo" else 1
        identity = pos if step < 0 else pos + 1
        if identity < 1 or identity > meta["undo_max"]:
            return meta
        row = db.execute(
            "SELECT before,after FROM history WHERE id=?", (identity,)
        ).fetchone()
        result = json.loads(row[0 if step < 0 else 1])
        coords = set()
        for layer, x, y, before, after in db.execute(
            "SELECT layer,x,y,before,after FROM changes WHERE id=?", (identity,)
        ):
            png = before if step < 0 else after
            _write(db, None, layer, x, y, _decode(db, png) if png else blank())
            coords.add((x, y))
        # Layer visibility/order changes can affect every stored tile.
        common = {layer["id"] for layer in result["layers"]} & {
            layer["id"] for layer in meta["layers"]
        }

        def composition(layers):
            return [
                (layer["id"], layer["visible"], layer["opacity"])
                for layer in layers
                if layer["id"] in common and not layer.get("background")
            ]

        if composition(result["layers"]) != composition(meta["layers"]):
            coords.update(db.execute("SELECT DISTINCT x,y FROM layer_tiles"))
            coords.update(db.execute("SELECT x,y FROM tiles WHERE level=0"))
        result.update(
            undo_pos=pos + step,
            undo_max=meta["undo_max"],
            clean_pos=meta["clean_pos"],
            baseline=meta.get("baseline", ""),
        )
        for key in (
            "background",
            "background_visible",
            "background_opacity",
            "show_axes",
            "show_grid",
            "settings_revision",
            "settings_clean_revision",
        ):
            if key in meta:
                result[key] = meta[key]
        _compose(db, result, coords, cancel)
        _put_meta(db, result)
        return result
    old = json.dumps(meta)
    action = meta["undo_pos"] + 1
    db.execute("DELETE FROM changes WHERE id>=?", (action,))
    db.execute("DELETE FROM history WHERE id>=?", (action,))
    if meta["clean_pos"] >= action:
        meta["clean_pos"] = -1
    layers = meta["layers"]
    layer_id = payload.get("layer", meta["active_layer"])
    coords = set()
    layer = next((l for l in layers if l["id"] == layer_id), None)
    if kind not in ('move', 'transform'):
        meta.pop('selection_transform', None)
    if kind == 'selection':
        meta['pixel_selection'] = payload.get('selection_after')
    elif kind == 'text_object':
        from .text_objects import edit
        coords = edit(db, meta, action, payload, cancel)
    elif kind == "image":
        from PySide6.QtGui import QImageReader

        reader = QImageReader(payload["path"]) if "path" in payload else None
        if reader is not None:
            reader.setAutoTransform(True)
        supplied = payload.get("image")
        size = reader.size() if reader is not None else supplied.size()
        from .storage import MAX_EXPORT_BYTES, MAX_DIMENSION

        if (
            not size.isValid()
            or max(size.width(), size.height()) > MAX_DIMENSION
            or size.width() * size.height() * 4 > MAX_EXPORT_BYTES
        ):
            raise ValueError("The image is too large or unsupported.")
        image = reader.read() if reader is not None else QImage(supplied)
        if image.isNull():
            raise ValueError(reader.errorString() if reader is not None else "Invalid clipboard image.")
        identity = payload["id"] if payload.get("new_layer", True) else layer_id
        if not payload.get("new_layer", True) and (layer is None or layer.get("kind") == "vector" or not layer["visible"]):
            raise ValueError("Choose a visible raster layer.")
        if payload.get("new_layer", True):
            layers.append(dict(id=identity, name=payload["name"], background=False,
                               visible=True, opacity=1.0, bounds=None))
        meta["active_layer"] = identity
        meta["pixel_selection"] = None
        point = QPointF(*payload["position"])
        rect = QRect(
            round(point.x() - image.width() / 2),
            round(point.y() - image.height() / 2),
            image.width(),
            image.height(),
        )
        coords = _paint(
            db,
            meta,
            action,
            identity,
            rect,
            lambda painter: painter.drawImage(rect.topLeft(), image),
            None,
            cancel,
            clip_canvas=False,
        )
    elif kind == "layer":
        command = payload["command"]
        if layer is None and command not in ("add", "reorder"):
            raise ValueError("The selected layer no longer exists.")
        if command == "add":
            layers.append(
                dict(
                    id=payload["id"],
                    name=payload.get("name", "Layer"),
                    kind=payload.get("kind", "raster"),
                    objects=[],
                    background=False,
                    visible=True,
                    opacity=1.0,
                    bounds=None,
                )
            )
            meta["active_layer"] = payload["id"]
        elif command == "delete":
            targets = set(payload.get("layers") or [layer_id])
            if not targets <= {item["id"] for item in layers}:
                raise ValueError("One of the selected layers no longer exists.")
            for identity in targets:
                for x, y in list(
                    db.execute("SELECT x,y FROM layer_tiles WHERE layer=?", (identity,))
                ):
                    _write(db, action, identity, x, y, blank())
                    coords.add((x, y))
            layers[:] = [item for item in layers if item["id"] not in targets]
            if not layers:
                layers.append(
                    dict(
                        id="paint",
                        name="Layer 1",
                        background=False,
                        visible=True,
                        opacity=1.0,
                        bounds=None,
                    )
                )
            meta["active_layer"] = layers[-1]["id"]
        elif command == "reorder":
            order = payload["order"]
            if len(order) != len(layers) or set(order) != {l["id"] for l in layers}:
                raise ValueError("Invalid layer order.")
            meta["layers"] = layers = [
                next(l for l in layers if l["id"] == identity) for identity in order
            ]
        elif command in ("visible", "opacity", "name"):
            value = payload["value"]
            if command == "opacity":
                value = max(0.0, min(1.0, float(value)))
            if command == "name":
                value = str(value).strip()[:120] or "Layer"
            targets = set(payload.get('layers') or [layer_id]) if command in ('visible','opacity') else {layer_id}
            if not targets <= {item['id'] for item in layers}:
                raise ValueError('One of the selected layers no longer exists.')
            for item in layers:
                if item['id'] in targets:
                    item[command] = value
                    if command in ('visible','opacity') and not item.get('background'):
                        coords.update(db.execute('SELECT x,y FROM layer_tiles WHERE layer=?',(item['id'],)))
        if command == "reorder":
            coords.update(db.execute("SELECT DISTINCT x,y FROM layer_tiles"))
            coords.update(db.execute("SELECT x,y FROM tiles WHERE level=0"))
    else:
        if layer is None:
            raise ValueError("The selected layer no longer exists.")
        targets = payload.get("layers") or [layer_id]
        if not set(targets) <= {item["id"] for item in layers}:
            raise ValueError("One of the selected layers no longer exists.")
        if any(
            next(l for l in layers if l["id"] == identity).get("background")
            for identity in targets
        ):
            raise ValueError("Choose a paint layer; the white background is locked.")
        if (
            kind not in ("move", "transform", "delete_selection")
            and not layer["visible"]
        ):
            raise ValueError("The active layer is hidden.")
        selection = payload.get("selection")
        if kind == "fill":
            rect, image = _fill(db, meta, payload, cancel)
            coords = _paint(
                db,
                meta,
                action,
                layer_id,
                rect,
                lambda p: p.drawImage(rect.topLeft(), image),
                selection,
                cancel,
            )
        elif kind in ("move", "transform", "delete_selection"):
            transform_payload = dict(payload)
            if kind in ('move', 'transform') and selection is not None:
                from .selection import encode
                meta['pixel_selection'] = encode(QTransform(*payload['matrix']).map(selection))
            raster_only = all(next(item for item in layers if item['id']==identity).get('kind') != 'vector' for identity in targets)
            if kind in ('move', 'transform') and not payload.get('object_id') and raster_only:
                from .selection import encode, decode
                anchor = meta.get('selection_transform') or {}
                same = (anchor.get('targets') == sorted(targets)
                        and anchor.get('current') == encode(selection)
                        and db.execute("SELECT 1 FROM history WHERE id=?", (anchor.get('action',-1),)).fetchone())
                matrix = QTransform(*payload['matrix'])
                if same:
                    # Restore only coordinates touched since lifting. _write
                    # records the immediate previous state for this Undo step.
                    originals = {}
                    for identity,x,y,before in db.execute(
                        "SELECT layer,x,y,before FROM changes WHERE id>=? AND id<? ORDER BY id DESC",
                        (anchor['action'],action)):
                        if identity in targets:
                            originals[identity,x,y] = before
                    for (identity,x,y),before in originals.items():
                        _check(cancel)
                        _write(db,action,identity,x,y,_decode(db,before) if before is not None else blank())
                        coords.add((x,y))
                    matrix = QTransform(*anchor['matrix']) * matrix
                    transform_payload['selection'] = decode(anchor['source'])
                else:
                    anchor = dict(action=action, targets=sorted(targets), source=encode(selection))
                anchor['matrix'] = [matrix.m11(),matrix.m12(),matrix.m21(),matrix.m22(),matrix.dx(),matrix.dy()]
                anchor['current'] = encode(QTransform(*payload['matrix']).map(selection)) if selection is not None else None
                meta['pixel_selection'] = anchor['current']
                meta['selection_transform'] = anchor
                transform_payload['matrix'] = anchor['matrix']
            else:
                meta.pop('selection_transform', None)
            coords |= _transform_layers(db, meta, action, targets, kind, transform_payload, cancel)
        else:
            path = payload["path"]
            width = payload["width"]
            rect = (
                path.boundingRect()
                .adjusted(-width, -width, width, width)
                .toAlignedRect()
            )
            color = QColor(payload["color"])
            color.setAlphaF(payload["opacity"])

            def draw(p):
                if kind == "eraser":
                    p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                if kind == "text":
                    p.setPen(Qt.NoPen)
                    p.setBrush(color)
                else:
                    p.setPen(
                        QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                    )
                    p.setBrush(Qt.NoBrush)
                p.drawPath(path)

            coords = _paint(
                db,
                meta,
                action,
                layer_id,
                rect,
                draw,
                selection,
                cancel,
                keys=path_keys(path, width, filled=kind == "text"),
            )
    # Focusing a layer name, erasing empty pixels and unchanged properties
    # must not mark the document dirty or discard a pending Redo branch.
    before_meta = json.loads(old)
    style = lambda items: [
        {k: v for k, v in l.items() if k not in ("bounds", "thumbnail")} for l in items
    ]
    changed = db.execute(
        "SELECT 1 FROM changes WHERE id=? LIMIT 1", (action,)
    ).fetchone()
    canvas_style = lambda m: tuple(
        m.get(k)
        for k in (
            "background",
            "background_visible",
            "background_opacity",
            "show_axes",
            "show_grid",
        )
    )
    if (
        not changed
        and style(meta["layers"]) == style(before_meta["layers"])
        and canvas_style(meta) == canvas_style(before_meta)
        and meta.get("pixel_selection") == before_meta.get("pixel_selection")
    ):
        db.rollback()
        return before_meta
    if kind not in ("layer", "canvas", "image", "selection"):
        meta["active_layer"] = layer_id
    _check(cancel)
    _compose(db, meta, coords, cancel)
    meta.update(undo_pos=action, undo_max=action)
    db.execute("INSERT INTO history VALUES (?,?,?)", (action, old, json.dumps(meta)))
    _put_meta(db, meta)
    return meta


def save_working(path, destination, cancel=None):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.save(destination, cancel)
    """Incremental save to the previous destination; Save As uses a full backup."""
    meta = read_metadata(path)
    _check(cancel)
    same = (
        meta.get("baseline")
        and Path(meta["baseline"]).resolve() == Path(destination).resolve()
        and Path(destination).exists()
        and read_metadata(destination)["version"] == 2
    )
    if same:
        with closing(sqlite3.connect(destination)) as dst, dst:
            dst.execute("ATTACH DATABASE ? AS work", (path,))
            for table, key in [
                ("layer_tiles", ("layer", "x", "y")),
                ("tiles", ("level", "x", "y")),
            ]:
                dirty = "dirty_tiles" if table == "layer_tiles" else "dirty_previews"
                match = " AND ".join(f"{table}.{column}=d.{column}" for column in key)
                dst.execute(
                    f"DELETE FROM {table} WHERE EXISTS(SELECT 1 FROM work.{dirty} d WHERE {match})"
                )
                match = " AND ".join(f"t.{column}=d.{column}" for column in key)
                dst.execute(
                    f"INSERT INTO {table} SELECT t.* FROM work.{table} t JOIN work.{dirty} d ON {match}"
                )
            saved = dict(meta)
            saved.update(
                undo_pos=0,
                undo_max=0,
                clean_pos=0,
                baseline="",
                settings_clean_revision=meta.get("settings_revision", 0),
            )
            _put_meta(dst, saved)
            _check(cancel)
    else:
        # A compact snapshot excludes the working Undo journal. Replace only
        # after both pixels and metadata have been written successfully.
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".paint-save-", suffix=".tmp", dir=destination_path.parent
        )
        os.close(fd)
        try:
            create_document(
                temporary, meta["mode"], meta["width"], meta["height"], cancel
            )
            initialize(temporary, cancel=cancel, create_background=False)
            with closing(sqlite3.connect(temporary)) as dst, dst:
                dst.execute("ATTACH DATABASE ? AS work", (path,))
                dst.execute("INSERT INTO layer_tiles SELECT * FROM work.layer_tiles")
                _check(cancel)
                dst.execute("INSERT INTO tiles SELECT * FROM work.tiles")
                saved = dict(meta)
                saved.update(
                    undo_pos=0,
                    undo_max=0,
                    clean_pos=0,
                    baseline="",
                    settings_clean_revision=meta.get("settings_revision", 0),
                )
                _put_meta(dst, saved)
                _check(cancel)
            with open(temporary, "r+b") as saved_file:
                os.fsync(saved_file.fileno())
            _check(cancel)
            os.replace(temporary, destination_path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        meta["baseline"] = str(Path(destination).resolve())
        meta["clean_pos"] = meta["undo_pos"]
        meta["settings_clean_revision"] = meta.get("settings_revision", 0)
        db.execute("DELETE FROM dirty_tiles")
        db.execute("DELETE FROM dirty_previews")
        _put_meta(db, meta)
    return str(Path(destination).resolve())


def sample_color(path, point):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.sample_color(point)
    rect = QRect(math.floor(point.x()), math.floor(point.y()), 1, 1)
    meta = read_metadata(path)
    with closing(connect_read(path)) as db:
        return render_region(db, meta, rect).pixelColor(0, 0)


def preview_layers(path, targets, selection=None, cancel=None):
    from .runtime import get_session

    session = get_session(path)
    if session is not None:
        return session.preview_layers(targets, selection, cancel)
    meta = read_metadata(path)
    with closing(connect_read(path)) as db:
        return _preview_layers(db, meta, targets, selection, cancel)


def _preview_layers(
    db,
    meta,
    targets,
    selection=None,
    cancel=None,
    *,
    bounds_override=None,
    max_side=1024,
    include_hidden=False,
    apply_opacity=True,
    tile_overrides=None,
):
    bounds = None
    for layer in default_layers(meta):
        layer_bounds = layer.get("bounds")
        if layer.get("background") and meta["mode"] == "finite":
            layer_bounds = [0, 0, meta["width"], meta["height"]]
        if layer["id"] in targets and layer_bounds:
            rect = QRect(*layer_bounds)
            bounds = rect if bounds is None else bounds.united(rect)
    if bounds_override is not None:
        bounds = QRect(bounds_override)
    if bounds is None:
        return None
    if selection is not None:
        bounds = bounds.intersected(selection.boundingRect().toAlignedRect())
    if bounds.isEmpty():
        return None
    factor = min(1.0, max_side / max(bounds.width(), bounds.height()))
    image = blank(
        max(1, math.ceil(bounds.width() * factor)),
        max(1, math.ceil(bounds.height() * factor)),
    )
    p = QPainter(image)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.scale(factor, factor)
    p.translate(-bounds.x(), -bounds.y())
    if selection is not None:
        p.setClipPath(selection)
    try:
        for layer in default_layers(meta):
            if layer["id"] not in targets or (
                not layer["visible"] and not include_hidden
            ):
                continue
            p.setOpacity(layer["opacity"] if apply_opacity else 1.0)
            if layer.get("background"):
                if meta["mode"] == "finite":
                    p.fillRect(QRectF(0, 0, meta["width"], meta["height"]), Qt.white)
                continue
            query = (
                "SELECT x,y,png FROM layer_tiles WHERE layer=?"
                if meta["version"] == 2
                else "SELECT x,y,png FROM tiles WHERE level=0"
            )
            args = (layer["id"],) if meta["version"] == 2 else ()
            query += " AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?"
            args += (
                bounds.left() // S,
                bounds.right() // S,
                bounds.top() // S,
                bounds.bottom() // S,
            )
            tiles = {(x,y):png for x,y,png in db.execute(query,args)}
            for (identity,x,y),png in (tile_overrides or {}).items():
                if identity == layer['id'] and bounds.intersects(QRect(x*S,y*S,S,S)):
                    tiles[x,y] = png
            for (x,y),png in tiles.items():
                _check(cancel)
                if png is not None:
                    p.drawImage(x*S,y*S,_decode(db,png))
    finally:
        p.end()
    return image, bounds


def prefetch_viewport(path, meta, viewport, zoom, budget, cancel=None):
    """At most sixteen decoded tiles accompany a commit, avoiding a blank frame.

    viewport is a world rectangle captured by the GUI before dispatch; the
    worker never reads a QWidget. Later camera changes use normal lazy loading.
    """
    if not meta["tile_count"] or not meta.get("content_bounds"):
        return []
    area = QRectF(*viewport).intersected(QRectF(*meta["content_bounds"]))
    if area.isEmpty():
        return []
    level = min(meta["max_level"], max(0, math.floor(math.log2(1 / zoom))))
    while True:
        span = S * 2**level
        x0, x1 = math.floor(area.left() / span), math.ceil(area.right() / span) - 1
        y0, y1 = math.floor(area.top() / span), math.ceil(area.bottom() / span) - 1
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= budget or level >= meta["max_level"]:
            break
        level += 1
    cx, cy = area.center().x() / span, area.center().y() / span
    if (x1 - x0 + 1) * (y1 - y0 + 1) > 1024:
        x0, x1 = max(x0, int(cx) - 16), min(x1, int(cx) + 16)
        y0, y1 = max(y0, int(cy) - 16), min(y1, int(cy) + 16)
    keys = [(level, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    keys.sort(key=lambda key: (key[1] + 0.5 - cx) ** 2 + (key[2] + 0.5 - cy) ** 2)
    return load_tiles(path, keys[: min(16, budget)], cancel)


def _floating_source(db,meta,targets,selection):
    from .selection import encode,decode
    anchor = meta.get('selection_transform') or {}
    if (selection is None or anchor.get('current') != encode(selection)
            or anchor.get('targets') != sorted(targets)
            or not db.execute("SELECT 1 FROM history WHERE id=?",(anchor.get('action',-1),)).fetchone()):
        return None,selection,QTransform()
    originals = {}
    for identity,x,y,before in db.execute(
        "SELECT layer,x,y,before FROM changes WHERE id>=? AND id<=? ORDER BY id DESC",
        (anchor['action'],meta['undo_pos'])):
        if identity in targets:
            originals[identity,x,y] = before
    return originals,decode(anchor['source']),QTransform(*anchor['matrix'])


def _selection_preview(db,meta,targets,selection,cancel,*,max_side=1024,full_resolution=False):
    originals,source,matrix = _floating_source(db,meta,targets,selection)
    if originals is None:
        return _preview_layers(db,meta,targets,selection,cancel,max_side=max_side,
            bounds_override=selection.boundingRect().toAlignedRect() if selection is not None else None)
    bounds = selection.boundingRect().toAlignedRect()
    if bounds.isEmpty():
        return None
    # Keep source precision while bounding drag previews; copying uses full
    # resolution. The final raster edit always uses original source tiles.
    original_bounds = source.boundingRect().toAlignedRect()
    if full_resolution:
        _keys(original_bounds)
    moving = _preview_layers(db,meta,targets,source,cancel,bounds_override=original_bounds,
        max_side=max(original_bounds.width(),original_bounds.height()) if full_resolution else max_side, tile_overrides=originals)
    if moving is None:
        return None
    pixels,area = moving
    factor = min(1.,max_side/max(bounds.width(),bounds.height()))
    image = blank(max(1,math.ceil(bounds.width()*factor)),max(1,math.ceil(bounds.height()*factor)))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.scale(image.width()/bounds.width(),image.height()/bounds.height())
    painter.translate(-bounds.x(),-bounds.y())
    painter.setTransform(matrix,True)
    painter.drawImage(QRectF(area),pixels)
    painter.end()
    return image,bounds


def selection_image(path,targets,selection,cancel=None):
    """Copy only selected layer pixels, with original alpha and no backdrop."""
    if selection is None or selection.isEmpty():
        return None
    area = selection.boundingRect().toAlignedRect()
    _keys(area)
    def build(db,meta):
        result = _selection_preview(db,meta,targets,selection,cancel,
            max_side=max(area.width(),area.height()),full_resolution=True)
        return result[0] if result is not None else None
    from .runtime import get_session
    session = get_session(path)
    if session is not None:
        with session.lock:
            return build(session.db,_metadata(session.db))
    with closing(connect_read(path)) as db:
        return build(db,read_metadata(path))


def transform_preview(path, targets, selection=None, cancel=None, *, object_id=None, max_side=2048, viewport=None):
    """Слои превью без фона: неподвижная часть и перемещаемые пиксели по порядку."""
    from .runtime import get_session
    def build(db, meta):
        from .text_objects import bounds as text_bounds, render_objects
        from PySide6.QtCore import QSize
        visible = QRectF(*viewport).toAlignedRect() if viewport else QRect(*meta.get('content_bounds', [0, 0, 1, 1]))
        if visible.isEmpty():
            return None
        result = []
        frozen = None
        for layer in default_layers(meta):
            _check(cancel)
            if not layer['visible'] or not layer['opacity']:
                continue
            identity = layer['id']
            is_target = identity in targets
            only_object = next((obj for obj in layer.get('objects', []) if obj['id'] == object_id), None) if object_id and is_target else None
            stationary = None
            moving = None
            if not is_target or selection is not None or object_id:
                if layer.get('kind') == 'vector':
                    factor = min(1., max_side / max(1, visible.width(), visible.height()))
                    size = QSize(max(1, math.ceil(visible.width()*factor)), max(1, math.ceil(visible.height()*factor)))
                    stationary = render_objects(layer, visible, size, exclude=object_id if only_object else None, cancel=cancel)
                else:
                    rendered = _preview_layers(db, meta, {identity}, cancel=cancel, bounds_override=visible,
                                               max_side=max_side, apply_opacity=False)
                    stationary = rendered[0] if rendered else None
                if is_target and selection is not None and stationary is not None and not object_id:
                    cut = QPainter(stationary)
                    cut.scale(stationary.width()/visible.width(), stationary.height()/visible.height())
                    cut.translate(-visible.x(), -visible.y())
                    cut.setClipPath(selection)
                    cut.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                    cut.fillRect(QRectF(visible), Qt.white)
                    cut.end()
            if is_target:
                if only_object:
                    area = text_bounds(only_object).adjusted(-2, -2, 2, 2).toAlignedRect()
                    factor = min(1., max_side / max(1, area.width(), area.height()))
                    size = QSize(max(1, math.ceil(area.width()*factor)), max(1, math.ceil(area.height()*factor)))
                    moving = render_objects(layer, area, size, only=object_id, cancel=cancel), area
                elif not object_id:
                    moving = _preview_layers(db, meta, {identity}, selection, cancel, max_side=max_side, apply_opacity=False)
            if moving is None:
                # Соседние неподвижные слои держим одним изображением, а не N копиями холста.
                if stationary is not None:
                    if frozen is None:
                        frozen = blank(stationary.width(), stationary.height())
                    painter = QPainter(frozen)
                    painter.setOpacity(layer['opacity'])
                    painter.drawImage(0, 0, stationary)
                    painter.end()
            else:
                if frozen is not None:
                    result.append({'stationary': frozen, 'rect': visible, 'moving': None, 'opacity': 1.})
                    frozen = None
                result.append({'stationary': stationary, 'rect': visible, 'moving': moving, 'opacity': layer['opacity']})
        if frozen is not None:
            result.append({'stationary': frozen, 'rect': visible, 'moving': None, 'opacity': 1.})
        return result or None
    session = get_session(path)
    if session is not None:
        with session.lock:
            return build(session.db, _metadata(session.db))
    with closing(connect_read(path)) as db:
        return build(db, read_metadata(path))
