"""A bounded shared cache and worker dispatcher with GUI-thread callbacks."""

import threading
import os
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QRectF, Qt, Signal, Slot
from PySide6.QtGui import QImage, QPainter
from .storage import TILE_SIZE


class TileCache:
    def __init__(self, limit=64 * 1024 * 1024):
        self.limit = limit
        self.bytes_used = 0
        self._entries = OrderedDict()
        self._stale = set()
        self._patched = set()
        self._protected = set()

    def contains(self, key):
        return key in self._entries and key not in self._stale

    def get(self, key):
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
            return value[0]
        return None

    def display_entry(self, key):
        """Only current pixels may enter a frame, including live approximations."""
        if key not in self._entries or (
            key in self._stale and key not in self._patched
        ):
            return False, None
        return True, self.get(key)

    def protect(self, path, keys):
        self._protected = {(path, *key) for key in keys}

    def finer_entries(self, path, level, x, y):
        result = []
        for key in list(self._entries):
            if key[0] != path or key[1] >= level:
                continue
            factor = 2 ** (level - key[1])
            if key[2] // factor == x and key[3] // factor == y:
                valid, image = self.display_entry(key)
                if valid:
                    result.append((key[1:], image))
        return sorted(result, key=lambda entry: -entry[0][0])

    def apply_patches(self, path, images, affected):
        """Replace cached overview rectangles before publishing base pixels.

        Approximate previews use current composited pixels (also for erasing).
        They remain pending for the exact pyramid filter, but never show an
        older document version. Missing patches make that overview unusable.
        """
        usable = {
            key
            for key in self._entries
            if key not in self._stale or key in self._patched
        }
        self.invalidate((path, *key) for key in affected)
        patches = dict(images)
        coordinates = {(x, y) for level, x, y in affected if level == 0}
        for key in list(self._entries):
            source, level, x, y = key
            if (
                key not in usable
                or source != path
                or level == 0
                or key not in self._protected
                or (level, x, y) not in affected
            ):
                continue
            factor = 2**level
            changes = [
                (cx, cy)
                for cx, cy in coordinates
                if cx // factor == x and cy // factor == y
            ]
            if not changes or any((0, cx, cy) not in patches for cx, cy in changes):
                continue
            original = self._entries[key][0]
            if original is None:
                image = QImage(TILE_SIZE, TILE_SIZE, QImage.Format_ARGB32_Premultiplied)
                image.fill(Qt.transparent)
            else:
                image = QImage(original)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            try:
                for cx, cy in changes:
                    target = QRectF(
                        (cx % factor) * TILE_SIZE / factor,
                        (cy % factor) * TILE_SIZE / factor,
                        TILE_SIZE / factor,
                        TILE_SIZE / factor,
                    )
                    patch = patches[(0, cx, cy)]
                    if patch is None:
                        painter.fillRect(target, Qt.transparent)
                    else:
                        painter.drawImage(target, patch)
            finally:
                painter.end()
            self.put(key, image)
            self._stale.add(key)
            self._patched.add(key)
        for key, image in images:
            self.put((path, *key), image)

    def put(self, key, image):
        self._stale.discard(key)
        self._patched.discard(key)
        old = self._entries.pop(key, None)
        if old:
            self.bytes_used -= old[1]
        size = int(image.sizeInBytes()) if image is not None else 0
        if size > self.limit:
            return
        self._entries[key] = (image, size)
        self.bytes_used += size
        while self.bytes_used > self.limit or len(self._entries) > 4096:
            victim = next(
                (k for k in self._entries if k not in self._protected),
                next(iter(self._entries)),
            )
            _, size = self._entries.pop(victim)
            self.bytes_used -= size
        self._stale.intersection_update(self._entries)
        self._patched.intersection_update(self._entries)

    def invalidate(self, keys):
        # Retain previous pixels as a base for live patches. display_entry()
        # rejects stale pixels unless all affected regions were replaced.
        present = {key for key in keys if key in self._entries}
        self._stale.update(present)
        self._patched.difference_update(present)

    def rekey_document(self, old, new):
        if old == new:
            return
        for key in list(self._entries):
            if key[0] != old:
                continue
            value = self._entries.pop(key)
            target = (new, *key[1:])
            previous = self._entries.pop(target, None)
            if previous:
                self.bytes_used -= previous[1]
            self._entries[target] = value
            if key in self._stale:
                self._stale.discard(key)
                self._stale.add(target)
            if key in self._patched:
                self._patched.discard(key)
                self._patched.add(target)
        self._protected = {
            (new, *key[1:]) if key[0] == old else key for key in self._protected
        }

    def discard_document(self, path):
        for key in list(self._entries):
            if key[0] == path:
                _, size = self._entries.pop(key)
                self.bytes_used -= size
                self._stale.discard(key)
                self._patched.discard(key)
        self._protected = {key for key in self._protected if key[0] != path}


class _Signals(QObject):
    completed = Signal(object)


class _Job(QRunnable):
    def __init__(self, identity, fn, cancel):
        super().__init__()
        self.identity, self.fn, self.cancel = identity, fn, cancel
        self.signals = _Signals()

    def run(self):
        try:
            if self.cancel.is_set():
                raise InterruptedError("Operation cancelled")
            outcome = (self.identity, self.fn(self.cancel), None)
        except Exception as error:
            outcome = (self.identity, None, error)
        try:
            self.signals.completed.emit(outcome)
        except RuntimeError:
            pass  # Application/window already destroyed.


class PaintIO(QObject):
    """Bounded workers, callbacks dispatched through a bound QObject slot.

    Viewports submit at most one batch at a time. Cancelling a hidden viewport
    also invalidates its callback; queued work checks its cancellation flag.
    """

    activity_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        # One document remains serialized; independent documents can progress.
        self.pool.setMaxThreadCount(min(2, max(1, (os.cpu_count() or 1) - 1)))
        self.viewport_pool = QThreadPool(self)
        self.viewport_pool.setMaxThreadCount(1)
        self._next = 0
        self._jobs = {}
        self.closed = False
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown, wait=self.finish_shutdown)

    def submit(self, fn, callback, priority=0):
        if self.closed:
            return None
        self._next += 1
        job = _Job(self._next, fn, threading.Event())
        self._jobs[self._next] = (job, callback)
        job.signals.completed.connect(self._completed)
        pool = self.viewport_pool if priority > 0 else self.pool
        pool.start(job, priority)
        return self._next

    def cancel(self, identity):
        entry = self._jobs.get(identity)
        if entry:
            entry[0].cancel.set()
            self._jobs[identity] = (entry[0], None)

    @Slot(object)
    def _completed(self, outcome):
        identity, result, error = outcome
        entry = self._jobs.pop(identity, None)
        if entry and entry[1] and not self.closed:
            entry[1](result, error)
        self.activity_changed.emit()

    def shutdown(self):
        self.closed = True
        for job, _ in self._jobs.values():
            job.cancel.set()
        # Queued jobs also need to emit completion so their Python references
        # and any deferred draft-file cleanup can be released.

    def wait_for_done(self, milliseconds=2000):
        deadline = time.monotonic() + milliseconds / 1000
        for pool in (self.pool, self.viewport_pool):
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if not pool.waitForDone(remaining):
                return False
        return True

    def finish_shutdown(self):
        self.pool.waitForDone(-1)
        self.viewport_pool.waitForDone(-1)
        self._jobs.clear()
