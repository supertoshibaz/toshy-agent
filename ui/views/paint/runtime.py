"""Sparse working documents, immutable QImage versions and bounded raw swap.

SQLite in RAM indexes small image handles and transactional metadata. It never
compresses a brush update. The on-disk .tpaint format remains PNG/SQLite; only
opening a cold tile, saving and exporting need image codecs. A shared tile pool
accounts for layer pixels, compositions, stroke masks and Undo versions.
"""

import json
import base64
import math
import os
import sqlite3
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from . import storage

_sessions = {}
_registry_lock = threading.RLock()


def get_session(path):
    with _registry_lock:
        return _sessions.get(str(path))


def prepare_session(source, target, original, pool, cancel=None):
    """Read the index once, without copying/decoding the saved document."""
    session = get_session(target)
    if session is None:
        session = RasterSession(source, target, original, pool, cancel)
        with _registry_lock:
            _sessions[str(target)] = session
    return session


def close_session(path):
    with _registry_lock:
        session = _sessions.pop(str(path), None)
    if session is not None:
        session.close()


@dataclass
class _Image:
    owner: object
    image: QImage = None
    origin: tuple = None
    swap: tuple = None


class ImagePool:
    """LRU of immutable images; eviction writes raw bytes, never PNG.

    Each reader receives its own implicitly shared QImage. Writers detach through
    Qt before changing pixels. The GUI never reads mutable worker-owned objects.
    Swap slots are reclaimed when redo branches or documents are discarded.
    """

    def __init__(self, limit=128 * 1024 * 1024):
        self.limit = limit
        self.bytes_used = 0
        self._next = 0
        self._records = {}
        self._owners = {}
        self._resident = OrderedDict()
        self._swap = tempfile.TemporaryFile(prefix="paint-pixels-")
        self._free = {}
        self.lock = threading.RLock()

    def _token(self):
        self._next += 1
        return self._next.to_bytes(8, "little")

    def lazy(self, owner, origin):
        with self.lock:
            token = self._token()
            self._records[token] = _Image(owner, origin=origin)
            self._owners.setdefault(id(owner), set()).add(token)
            return token

    def put(self, owner, image):
        with self.lock:
            token = self._token()
            self._records[token] = _Image(owner, QImage(image))
            self._owners.setdefault(id(owner), set()).add(token)
            self._resident[token] = None
            self.bytes_used += image.sizeInBytes()
            self._trim()
            return token

    def get(self, token):
        with self.lock:
            record = self._records[token]
            if record.image is None:
                if record.origin is not None:
                    image = storage.decode_tile(record.owner.source_blob(record.origin))
                else:
                    offset, capacity, size, width, height, stride, compressed = (
                        record.swap
                    )
                    self._swap.seek(offset)
                    data = self._swap.read(size)
                    if len(data) != size:
                        raise IOError("Could not read swapped canvas pixels.")
                    if compressed:
                        data = zlib.decompress(data)
                    if len(data) != height * stride:
                        raise IOError("Invalid swapped canvas pixels.")
                    image = QImage(
                        data, width, height, stride, QImage.Format_ARGB32_Premultiplied
                    ).copy()
                record.image = image
                self.bytes_used += image.sizeInBytes()
                self._resident[token] = None
            self._resident.move_to_end(token)
            image = QImage(record.image)
            self._trim()
            return image

    def png(self, token):
        with self.lock:
            record = self._records[token]
            origin, owner = record.origin, record.owner
            image = self.get(token) if origin is None else None
        # PNG encoding must not block painting in another document.
        return owner.source_blob(origin) if origin is not None else storage.encode_png(image)

    def _trim(self):
        while self.bytes_used > self.limit and self._resident:
            token = next(iter(self._resident))
            record = self._records[token]
            image = record.image
            if record.origin is None and record.swap is None:
                raw = bytes(image.constBits())
                packed = zlib.compress(raw, 1)
                compressed = len(packed) < len(raw)
                data = packed if compressed else raw
                size = len(data)
                capacity = (size + 4095) // 4096 * 4096
                slots = self._free.get(capacity)
                if slots:
                    offset = slots.pop()
                else:
                    self._swap.seek(0, os.SEEK_END)
                    offset = self._swap.tell()
                    self._swap.truncate(offset + capacity)
                self._swap.seek(offset)
                if self._swap.write(data) != size:
                    raise IOError("Not enough space to swap canvas pixels.")
                self._swap.seek(offset + capacity)
                record.swap = (
                    offset,
                    capacity,
                    size,
                    image.width(),
                    image.height(),
                    image.bytesPerLine(),
                    compressed,
                )
            self._resident.pop(token)
            self.bytes_used -= image.sizeInBytes()
            record.image = None

    def collect(self, owner, keep):
        with self.lock:
            self.discard(owner, self._owners.get(id(owner), set()) - keep)

    def discard(self, owner, tokens):
        """Release known dead versions without scanning other documents/Undo."""
        with self.lock:
            owned = self._owners.get(id(owner), set())
            for token in tokens:
                record = self._records.get(token)
                if record is None or record.owner is not owner:
                    continue
                if record.image is not None:
                    self.bytes_used -= record.image.sizeInBytes()
                self._resident.pop(token, None)
                if record.swap is not None:
                    offset, capacity, *_ = record.swap
                    self._free.setdefault(capacity, []).append(offset)
                del self._records[token]
                owned.discard(token)
            if not owned:
                self._owners.pop(id(owner), None)

    def close(self):
        with self.lock:
            self._records.clear()
            self._owners.clear()
            self._resident.clear()
            self.bytes_used = 0
            self._swap.close()


class _Connection(sqlite3.Connection):
    pass


class _PreviewYield(Exception):
    """A viewport slice ended; completed dependencies can be reused next time."""


class RasterSession:
    def __init__(self, source, path, original, pool, cancel):
        from . import raster

        self.lock = threading.RLock()
        self.pool = pool
        self.path = str(path)
        self.base_path = str(source)
        self.db = sqlite3.connect(
            ":memory:", check_same_thread=False, factory=_Connection
        )
        self.db.images = self
        self.db.session = self
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.invalid = set()
        self.updates = {}
        self.affected = set()
        self.dirty_rect = None
        self.stroke = None
        self._retired = set()
        self.dirty_bounds = set()
        self.closed = False
        try:
            meta = storage.read_metadata(source)
            self.preview_level = meta["max_level"]
            with closing(storage.connect_read(source)) as src:
                # Copy indices, not compressed pixels or the saved Undo journal.
                for statement in (
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                    "CREATE TABLE layer_tiles(layer TEXT,x INTEGER,y INTEGER,png BLOB,bounds TEXT,PRIMARY KEY(layer,x,y)) WITHOUT ROWID",
                    "CREATE TABLE tiles(level INTEGER,x INTEGER,y INTEGER,png BLOB,PRIMARY KEY(level,x,y)) WITHOUT ROWID",
                    "CREATE TABLE history(id INTEGER PRIMARY KEY,before TEXT,after TEXT)",
                    "CREATE TABLE changes(id INTEGER,layer TEXT,x INTEGER,y INTEGER,before BLOB,after BLOB,PRIMARY KEY(id,layer,x,y)) WITHOUT ROWID",
                    "CREATE TABLE dirty_tiles(layer TEXT,x INTEGER,y INTEGER,PRIMARY KEY(layer,x,y)) WITHOUT ROWID",
                    "CREATE TABLE dirty_previews(level INTEGER,x INTEGER,y INTEGER,PRIMARY KEY(level,x,y)) WITHOUT ROWID",
                    "CREATE TABLE preview_dirty(level INTEGER,x INTEGER,y INTEGER,cx INTEGER,cy INTEGER,PRIMARY KEY(level,x,y,cx,cy)) WITHOUT ROWID",
                ):
                    self.db.execute(statement)
                for level, x, y in src.execute("SELECT level,x,y FROM tiles"):
                    storage._check(cancel)
                    token = pool.lazy(self, ("tiles", level, x, y))
                    self.db.execute(
                        "INSERT INTO tiles VALUES (?,?,?,?)", (level, x, y, token)
                    )
                    if meta["version"] == 1 and level == 0:
                        self.db.execute(
                            "INSERT INTO layer_tiles VALUES (?,?,?,?,?)",
                            (
                                "paint",
                                x,
                                y,
                                token,
                                json.dumps(
                                    [0, 0, storage.TILE_SIZE, storage.TILE_SIZE]
                                ),
                            ),
                        )
                if meta["version"] == 2:
                    for layer, x, y, bounds in src.execute(
                        "SELECT layer,x,y,bounds FROM layer_tiles"
                    ):
                        storage._check(cancel)
                        self.db.execute(
                            "INSERT INTO layer_tiles VALUES (?,?,?,?,?)",
                            (
                                layer,
                                x,
                                y,
                                pool.lazy(self, ("layer_tiles", layer, x, y)),
                                bounds,
                            ),
                        )
            meta.pop("selection_transform",None)
            meta.update(
                version=2,
                layers=raster.default_layers(meta),
                active_layer=meta.get("active_layer", "paint"),
                undo_pos=0,
                undo_max=0,
                clean_pos=0,
                baseline=original or "",
            )
            self.db.execute(
                "INSERT INTO metadata VALUES ('document',?)", (json.dumps(meta),)
            )
            raster.materialize_background(self.db, meta, cancel)
            self.db.commit()
            self.updates, self.affected = {}, set()
            if self.path != self.base_path:
                Path(self.path).touch()
        except BaseException:
            self.db.close()
            pool.collect(self, set())
            raise

    def source_blob(self, origin):
        table, first, x, y = origin
        field = "layer" if table == "layer_tiles" else "level"
        with closing(storage.connect_read(self.base_path)) as db:
            row = db.execute(
                f"SELECT png FROM {table} WHERE {field}=? AND x=? AND y=?",
                (first, x, y),
            ).fetchone()
        if row is None:
            raise IOError("The source image changed while its Paint tab was open.")
        return row[0]

    def get(self, token):
        return self.pool.get(token)

    def put(self, image):
        return self.pool.put(self, image)

    def retire(self, token):
        if token is not None and self.stroke is not None:
            self._retired.add(token)

    def metadata(self):
        from .raster import _metadata

        with self.lock:
            return _metadata(self.db)

    def take_updates(self):
        with self.lock:
            images, affected = list(self.updates.items()), self.affected
            self.updates, self.affected = {}, set()
            return images, affected

    def refresh_bounds(self, meta):
        """Bounds use compact tile metadata, no full-layer pixel scans/thumbnails."""
        from .raster import S

        total = None
        for layer in meta["layers"]:
            bounds = None
            if layer["id"] in self.dirty_bounds:
                old = QRect(*layer["bounds"]) if layer.get("bounds") else None
                interior = QRectF(old) if old is not None else QRectF()
                if self.stroke and self.stroke['settings']['tool'] == 'eraser':
                    interior = interior.adjusted(1, 1, -1, -1)
                if self.stroke and old is not None and self.dirty_rect is not None and interior.contains(self.dirty_rect):
                    bounds = old
                else:
                    for x, y, raw in self.db.execute(
                        "SELECT x,y,bounds FROM layer_tiles WHERE layer=?", (layer["id"],)
                    ):
                        r = QRect(*json.loads(raw)).translated(x * S, y * S)
                        bounds = r if bounds is None else bounds.united(r)
                layer["bounds"] = (
                    [bounds.x(), bounds.y(), bounds.width(), bounds.height()]
                    if bounds is not None
                    else None
                )
            elif layer.get("bounds"):
                bounds = QRect(*layer["bounds"])
            if bounds is not None:
                total = bounds if total is None else total.united(bounds)
            elif not layer.get("background"):
                layer.pop("thumbnail", None)
        meta["content_bounds"] = (
            [total.x(), total.y(), total.width(), total.height()]
            if total is not None
            else None
        )
        self.dirty_bounds.clear()
        extent = max(total.width(), total.height()) if total is not None else 1
        return min(16, max(0, math.ceil(math.log2(max(1, extent / S)))))

    def compose(self, meta, coords, cancel=None):
        from . import raster

        meta["max_level"] = max(meta["max_level"], self.refresh_bounds(meta))
        for x, y in coords:
            storage._check(cancel)
            image = raster.blank()
            p = QPainter(image)
            try:
                for layer in meta["layers"]:
                    if (
                        layer.get("background")
                        or not layer["visible"]
                        or not layer["opacity"]
                    ):
                        continue
                    row = self.db.execute(
                        "SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?",
                        (layer["id"], x, y),
                    ).fetchone()
                    if row:
                        p.setOpacity(layer["opacity"])
                        p.drawImage(0, 0, self.get(row[0]))
            finally:
                p.end()
            present = raster._put_preview(self.db, 0, x, y, image)
            key = (0, x, y)
            self.updates[key] = QImage(image) if present else None
            # A publication keeps its own QImage references until GUI delivery.
            # Bound this separately from the pixel pool and viewport cache.
            if len(self.updates) > 16:
                self.updates.pop(next(iter(self.updates)))
            self.affected.add(key)
            for level in range(1, max(meta["max_level"], self.preview_level) + 1):
                key = (level, x // (2**level), y // (2**level))
                self.invalid.add(key)
                self.affected.add(key)
                # Retain the old parent as a base for a small replacement. The
                # dependency journal participates in stroke rollback/Undo.
                self.db.execute(
                    "INSERT OR IGNORE INTO preview_dirty VALUES (?,?,?,?,?)",
                    (*key, x // (2 ** (level - 1)), y // (2 ** (level - 1))),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO dirty_previews VALUES (?,?,?)", key
                )
        meta["tile_count"] = self.db.execute(
            "SELECT count(*) FROM tiles WHERE level=0"
        ).fetchone()[0]

    def preview_tile(self, key, cancel=None, deadline=None):
        from . import raster

        storage._check(cancel)
        self.preview_level = max(self.preview_level, key[0])
        row = self.db.execute(
            "SELECT png FROM tiles WHERE level=? AND x=? AND y=?", key
        ).fetchone()
        dirty = (
            set(
                self.db.execute(
                    "SELECT cx,cy FROM preview_dirty WHERE level=? AND x=? AND y=?", key
                )
            )
            if key[0]
            else set()
        )
        if row and not dirty:
            return self.get(row[0])
        level, x, y = key
        if level == 0:
            return None
        if deadline is not None and time.monotonic() >= deadline:
            raise _PreviewYield()
        # Prune empty subtrees; distant marks never allocate the area between.
        span = 2**level
        exists = self.db.execute(
            "SELECT 1 FROM tiles WHERE level=0 AND x BETWEEN ? AND ? AND y BETWEEN ? AND ? LIMIT 1",
            (x * span, (x + 1) * span - 1, y * span, (y + 1) * span - 1),
        ).fetchone()
        image = None
        if exists:
            # Resolve children before opening a painter. If the slice yields,
            # already completed child images remain usable on the next slice.
            children = []
            for dy in (0, 1):
                for dx in (0, 1):
                    cx, cy = x * 2 + dx, y * 2 + dy
                    if row and (cx, cy) not in dirty:
                        continue
                    children.append(
                        (
                            dx,
                            dy,
                            self.preview_tile((level - 1, cx, cy), cancel, deadline),
                        )
                    )
            image = self.get(row[0]) if row else raster.blank()
            p = QPainter(image)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setCompositionMode(QPainter.CompositionMode_Source)
            try:
                for dx, dy, child in children:
                    target = QRectF(
                        dx * raster.S / 2, dy * raster.S / 2, raster.S / 2, raster.S / 2
                    )
                    if child is None:
                        p.fillRect(target, Qt.transparent)
                    else:
                        p.drawImage(target, child)
            finally:
                p.end()
            raster._put_preview(self.db, level, x, y, image)
        else:
            self.db.execute("DELETE FROM tiles WHERE level=? AND x=? AND y=?", key)
        self.db.execute("DELETE FROM preview_dirty WHERE level=? AND x=? AND y=?", key)
        self.invalid.discard(key)
        return image

    def load_tiles(self, keys, cancel=None, budget_ms=None):
        with self.lock:
            deadline = (
                time.monotonic() + budget_ms / 1000 if budget_ms is not None else None
            )
            result = []
            try:
                for key in keys:
                    storage._check(cancel)
                    if result and deadline is not None and time.monotonic() >= deadline:
                        break
                    try:
                        image = self.preview_tile(key, cancel, deadline)
                    except _PreviewYield:
                        break
                    result.append((key, image))
                return result
            finally:
                # Cancellation may happen after building some valid children.
                # Never leave a lazy read transaction open before a new stroke.
                if self.stroke is None:
                    self.db.commit()

    def operate(self, kind, payload, cancel=None):
        from . import raster

        with self.lock:
            self.updates = {}
            self.affected = set()
            self.dirty_rect = None
            try:
                if kind == "stroke":
                    result = self._stroke(payload, cancel)
                else:
                    if self.stroke is not None:
                        raise ValueError("Finish the current brush stroke first.")
                    with self.db:
                        result = raster._operate(self.db, kind, payload, cancel)
                if self.stroke is not None:
                    # The transaction's originals remain pinned for rollback.
                    self.pool.discard(self, self._retired - self.stroke["rollback_keep"])
                    self._retired.clear()
                else:
                    self.collect()
                return result
            except BaseException:
                coords = {(x, y) for level, x, y in self.affected if level == 0}
                if self.stroke is not None:
                    coords.update(self.stroke["before"])
                self.db.rollback()
                self.stroke = None
                self.updates, self.affected = {}, set()
                self.dirty_rect = None
                self.collect()
                if coords:
                    meta = raster._metadata(self.db)
                    self.compose(meta, coords)
                    raster._put_meta(self.db, meta)
                    self.db.commit()
                self.collect()
                raise

    def _stroke(self, payload, cancel):
        from . import raster

        phase = payload["phase"]
        if phase == "begin":
            if self.stroke is not None:
                raise ValueError("A brush stroke is already active.")
            meta = raster._metadata(self.db)
            layer = next(
                (l for l in meta["layers"] if l["id"] == payload["layer"]), None
            )
            if layer is None or layer.get("background") or not layer["visible"]:
                raise ValueError(
                    "Choose a visible paint layer; the white background is locked."
                )
            self.db.execute("BEGIN")
            action = meta["undo_pos"] + 1
            rollback_keep = self.references()
            self.stroke = dict(
                id=payload["stroke_id"],
                old=json.dumps(meta),
                meta=meta,
                action=action,
                settings=payload,
                before={},
                masks={},
                last=None,
                rollback_keep=rollback_keep,
            )
            meta.pop("selection_transform",None)
            self.db.execute("DELETE FROM changes WHERE id>=?", (action,))
            self.db.execute("DELETE FROM history WHERE id>=?", (action,))
            if meta["clean_pos"] >= action:
                meta["clean_pos"] = -1
        state = self.stroke
        if state is None or state["id"] != payload["stroke_id"]:
            raise ValueError("This brush stroke is no longer active.")
        if phase == "cancel":
            coords = set(state["before"])
            self.db.rollback()
            self.stroke = None
            meta = raster._metadata(self.db)
            self.compose(meta, coords, cancel)
            raster._put_meta(self.db, meta)
            self.db.commit()
            return meta
        settings, meta = state["settings"], state["meta"]
        from .stroke import stroke_path
        path, previous, raw_previous = stroke_path(
            payload.get("points", []), state["last"],
            state.get("last_raw", state["last"]), settings.get("smoothing_radius", 0),
            phase == "end")
        state["last_raw"] = raw_previous
        state["last"] = previous
        changed = set()
        if path.elementCount():
            padding = settings["width"] / 2 + 1
            self.dirty_rect = path.boundingRect().adjusted(
                -padding, -padding, padding, padding,
            )
            keys = raster.path_keys(path, settings["width"])
            selection = settings.get("selection")
            for x, y in keys:
                storage._check(cancel)
                tile_rect = QRect(x * raster.S, y * raster.S, raster.S, raster.S)
                if meta["mode"] == "finite" and not tile_rect.intersects(
                    QRect(0, 0, meta["width"], meta["height"])
                ):
                    continue
                if selection is not None and not selection.intersects(
                    QRectF(tile_rect)
                ):
                    continue
                key = (x, y)
                if key not in state["before"]:
                    if len(state["before"]) >= raster.MAX_EDIT_PIXELS // (
                        raster.S * raster.S
                    ):
                        raise ValueError(
                            "The brush stroke is too large. Use several smaller strokes."
                        )
                    row = self.db.execute(
                        "SELECT png FROM layer_tiles WHERE layer=? AND x=? AND y=?",
                        (settings["layer"], x, y),
                    ).fetchone()
                    state["before"][key] = row[0] if row else None
                mask = (
                    self.get(state["masks"][key])
                    if key in state["masks"]
                    else raster.blank()
                )
                p = QPainter(mask)
                p.setRenderHint(QPainter.Antialiasing)
                p.translate(-x * raster.S, -y * raster.S)
                p.setPen(
                    QPen(
                        Qt.white,
                        settings["width"],
                        Qt.SolidLine,
                        Qt.RoundCap,
                        Qt.RoundJoin,
                    )
                )
                p.drawPath(path)
                p.end()
                self.retire(state["masks"].get(key))
                state["masks"][key] = self.put(mask)
                color = QColor(
                    "white" if settings["tool"] == "eraser" else settings["color"]
                )
                color.setAlphaF(settings["opacity"])
                p = QPainter(mask)
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(mask.rect(), color)
                p.end()
                token = state["before"][key]
                image = self.get(token) if token is not None else raster.blank()
                p = QPainter(image)
                p.translate(-x * raster.S, -y * raster.S)
                raster._clip(p, meta, selection)
                if settings["tool"] == "eraser":
                    p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                p.drawImage(x * raster.S, y * raster.S, mask)
                p.end()
                if raster._write(
                    self.db, state["action"], settings["layer"], x, y, image
                ):
                    changed.add(key)
            self.compose(meta, changed, cancel)
            raster._put_meta(self.db, meta)
        if phase == "end":
            changed = self.db.execute(
                "SELECT 1 FROM changes WHERE id=? LIMIT 1", (state["action"],)
            ).fetchone()
            if changed:
                meta.update(
                    undo_pos=state["action"],
                    undo_max=state["action"],
                    active_layer=settings["layer"],
                )
                self.db.execute(
                    "INSERT INTO history VALUES (?,?,?)",
                    (state["action"], state["old"], json.dumps(meta)),
                )
                raster._put_meta(self.db, meta)
                self.db.commit()
            else:
                self.db.rollback()
                meta = raster._metadata(self.db)
            self.stroke = None
        return json.loads(json.dumps(meta))

    def references(self):
        keep = set()
        for table in ("layer_tiles", "tiles"):
            keep.update(row[0] for row in self.db.execute(f"SELECT png FROM {table}"))
        for before, after in self.db.execute("SELECT before,after FROM changes"):
            if before is not None:
                keep.add(before)
            if after is not None:
                keep.add(after)
        if self.stroke is not None:
            keep.update(self.stroke["rollback_keep"])
            keep.update(v for v in self.stroke["before"].values() if v is not None)
            keep.update(self.stroke["masks"].values())
        return keep

    def collect(self):
        self.pool.collect(self, self.references())
        self._retired.clear()

    def sample_color(self, point):
        from . import raster

        with self.lock:
            rect = QRect(math.floor(point.x()), math.floor(point.y()), 1, 1)
            return raster.render_region(
                self.db, raster._metadata(self.db), rect
            ).pixelColor(0, 0)

    def preview_layers(self, targets, selection=None, cancel=None):
        from . import raster

        with self.lock:
            return raster._preview_layers(
                self.db, raster._metadata(self.db), targets, selection, cancel
            )

    def render(self, rect, cancel=None):
        from . import raster

        with self.lock:
            return raster.render_region(
                self.db, raster._metadata(self.db), rect, cancel=cancel
            )

    def thumbnail(self, identity, cancel=None):
        from . import raster

        with self.lock:
            meta = raster._metadata(self.db)
            layer = next((l for l in meta["layers"] if l["id"] == identity), None)
            if layer is None or not layer.get("bounds"):
                return None
            if meta["mode"] == "finite":
                bounds = QRect(0, 0, meta["width"], meta["height"])
            else:
                bounds = QRect(*layer["bounds"])
                side = max(bounds.width(), bounds.height())
                bounds = QRect(
                    round(bounds.center().x() - side / 2),
                    round(bounds.center().y() - side / 2),
                    side,
                    side,
                )
            storage._check(cancel)
            result = raster._preview_layers(
                self.db,
                meta,
                [identity],
                cancel=cancel,
                bounds_override=bounds,
                max_side=64,
                include_hidden=True,
                apply_opacity=False,
            )
            return result[0] if result else None

    def save(self, destination, cancel=None):
        from . import raster

        with self.lock:
            storage._check(cancel)
            if self.stroke is not None:
                raise ValueError("Finish the current brush stroke before saving.")
            destination = str(Path(destination).resolve())
            meta = raster._metadata(self.db)
            same = (
                meta.get("baseline")
                and Path(meta["baseline"]).resolve() == Path(destination)
                and Path(destination).exists()
                and storage.read_metadata(destination)["version"] == 2
            )
            # Preserve the lazy source/old Undo pixels before replacing it.
            if Path(self.base_path).resolve() == Path(destination).resolve():
                if self.path == self.base_path:
                    raise ValueError(
                        "The working document must have a private filename."
                    )
                storage.save_copy(self.base_path, self.path, cancel)
                self.base_path = self.path
            for layer in meta["layers"]:
                if layer.get("background"):
                    continue
                thumbnail = self.thumbnail(layer["id"])
                if thumbnail is not None:
                    layer["thumbnail"] = base64.b64encode(
                        storage.encode_png(thumbnail)
                    ).decode("ascii")
                else:
                    layer.pop("thumbnail", None)
            parent = Path(destination).parent
            parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                if not same:
                    fd, temporary = tempfile.mkstemp(
                        prefix=".paint-save-", suffix=".tmp", dir=parent
                    )
                    os.close(fd)
                    storage.create_document(
                        temporary, meta["mode"], meta["width"], meta["height"], cancel
                    )
                    raster.initialize(temporary, cancel=cancel, create_background=False)
                # Generate only occupied ancestors. No giant composite bitmap.
                ancestors = set()
                ancestors.update(
                    self.db.execute("SELECT DISTINCT level,x,y FROM preview_dirty")
                )
                for x, y in self.db.execute("SELECT x,y FROM tiles WHERE level=0"):
                    for level in range(1, meta["max_level"] + 1):
                        ancestors.add((level, x // (2**level), y // (2**level)))
                for key in sorted(ancestors):
                    (
                        self.invalid.add(key)
                        if not self.db.execute(
                            "SELECT 1 FROM tiles WHERE level=? AND x=? AND y=?", key
                        ).fetchone()
                        else None
                    )
                    self.preview_tile(key, cancel)
                with closing(
                    sqlite3.connect(destination if same else temporary)
                ) as dst, dst:
                    if not same:
                        # initialize() creates an editable white layer for new
                        # finite documents. A snapshot supplies its own layers
                        # and previews, including removed/hidden backgrounds.
                        dst.execute("DELETE FROM layer_tiles")
                        dst.execute("DELETE FROM tiles")
                        dst.execute("DELETE FROM dirty_tiles")
                        dst.execute("DELETE FROM dirty_previews")
                    if same:
                        # The native document is a snapshot. Undo remains in the
                        # live session and must not leak into the saved file.
                        for table in (
                            "history",
                            "changes",
                            "dirty_tiles",
                            "dirty_previews",
                        ):
                            dst.execute(f"DELETE FROM {table}")
                        for layer, x, y in self.db.execute(
                            "SELECT layer,x,y FROM dirty_tiles"
                        ):
                            storage._check(cancel)
                            dst.execute(
                                "DELETE FROM layer_tiles WHERE layer=? AND x=? AND y=?",
                                (layer, x, y),
                            )
                        for level, x, y in self.db.execute(
                            "SELECT level,x,y FROM dirty_previews"
                        ):
                            storage._check(cancel)
                            dst.execute(
                                "DELETE FROM tiles WHERE level=? AND x=? AND y=?",
                                (level, x, y),
                            )
                    query = "SELECT t.layer,t.x,t.y,t.png,t.bounds FROM layer_tiles t"
                    if same:
                        query += " JOIN dirty_tiles d ON t.layer=d.layer AND t.x=d.x AND t.y=d.y"
                    for layer, x, y, token, bounds in self.db.execute(query):
                        storage._check(cancel)
                        dst.execute(
                            "INSERT INTO layer_tiles VALUES (?,?,?,?,?)",
                            (layer, x, y, self.pool.png(token), bounds),
                        )
                    query = "SELECT t.level,t.x,t.y,t.png FROM tiles t"
                    if same:
                        query += " JOIN dirty_previews d ON t.level=d.level AND t.x=d.x AND t.y=d.y"
                    for level, x, y, token in self.db.execute(query):
                        storage._check(cancel)
                        dst.execute(
                            "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)",
                            (level, x, y, self.pool.png(token)),
                        )
                    saved = json.loads(json.dumps(meta))
                    saved.update(
                        undo_pos=0,
                        undo_max=0,
                        clean_pos=0,
                        baseline="",
                        settings_clean_revision=meta.get("settings_revision", 0),
                    )
                    raster._put_meta(dst, saved)
                    storage._check(cancel)
                if temporary is not None:
                    with open(temporary, "r+b") as file:
                        os.fsync(file.fileno())
                    storage._check(cancel)
                    os.replace(temporary, destination)
            except BaseException:
                self.db.rollback()
                self.collect()
                raise
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            meta.update(
                baseline=destination,
                clean_pos=meta["undo_pos"],
                settings_clean_revision=meta.get("settings_revision", 0),
            )
            raster._put_meta(self.db, meta)
            self.db.execute("DELETE FROM dirty_tiles")
            self.db.execute("DELETE FROM dirty_previews")
            self.db.commit()
            self.collect()
            return destination

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.db.close()
                self.pool.collect(self, set())
