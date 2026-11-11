"""Optional LRU eviction of Paint pixels, sessions and previews."""

from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QPainterPath

from ui.tab_memory import performance_preferences
from .raster import default_layers
from .runtime import close_session


class PaintTabMemoryManager(QObject):
    def __init__(self, owner, preferences=None):
        super().__init__(owner)
        self.owner = owner
        self.preferences = preferences or performance_preferences
        self._order = {}
        self._clock = 0
        self._save_failed = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.prune)
        self.preferences.changed.connect(self._preferences_changed)
        owner.io.activity_changed.connect(self.schedule)

    def _preferences_changed(self):
        self._save_failed.clear()
        self.schedule()

    def schedule(self):
        if not self.owner.io.closed:
            self._timer.start(0)

    def touch(self, document):
        self._clock += 1
        self._order[document] = self._clock
        self._save_failed.discard(document)
        self.schedule()

    def forget(self, document):
        self._order.pop(document, None)
        self._save_failed.discard(document)

    def prune(self):
        owner = self.owner
        option = self.preferences.values["paint"]
        # Readers (thumbnails, minimaps, snapshots) can hold a session too.
        # Retry on completion instead of closing its database under a worker.
        if not option["enabled"] or owner.io.closed or owner.io._jobs:
            return
        live = sorted(
            (d for d in owner.documents if d.meta is not None or d.busy),
            key=lambda d: self._order.get(d, 0),
        )
        for document in live:
            if len(live) <= option["limit"]:
                break
            canvas = document.canvas
            if (
                document is owner.current_document()
                or document.busy
                or document.error
                or document.pending_edits
                or canvas._stroke_id is not None
                or canvas._gesture
                or canvas._text_box
                or canvas._pan is not None
                or canvas._anchor is not None
                or not document.file_path
                or not Path(document.file_path).is_file()
            ):
                continue
            if document.dirty:
                if (
                    self.preferences.values["unsaved"] != "save"
                    or document in self._save_failed
                ):
                    continue

                def saved(success, doc=document):
                    if not success:
                        self._save_failed.add(doc)
                    self.schedule()

                # Do not open Save As for unnamed drafts during navigation.
                owner.save_document(document, done=saved)
                return
            self._unload(document)
            live = [d for d in live if d is not document]

    def _unload(self, document):
        from core.logger import core_logger
        core_logger.event('resources', 'TAB_UNLOAD', f'paint: {document.file_path}')
        owner, canvas = self.owner, document.canvas
        document.camera_state = canvas.camera()
        document.canvas_state = {
            "tool": canvas.tool,
            "active": canvas.active_layer,
            "selected": set(canvas.selected_layers),
            "selection": (
                QPainterPath(canvas.pixel_selection)
                if canvas.pixel_selection is not None
                else None
            ),
        }
        popup = getattr(canvas, "agent_popup", None)
        if popup:
            popup.cancel()
            popup.deleteLater()
            canvas.agent_popup = None
        canvas.release()
        canvas.cancel_text()
        canvas._edge_images.clear()
        canvas._edge_bytes = 0
        canvas.meta = None
        canvas.path = None
        canvas.pixel_selection = None
        canvas.selected_layers.clear()
        canvas.edit_enabled = False
        owner._thumbnail_pending.pop(document, None)
        source = document.source
        owner.cache.discard_document(source)
        thumbnails = getattr(owner.tools.controls, "_thumbnail_images", {})
        for key in list(thumbnails):
            if key[0] == source:
                thumbnails.pop(key)
        close_session(source)
        owner._runtime_paths.discard(source)
        if source != document.file_path and source in owner._draft_paths:
            owner._cleanup_paths.add(source)
        document.source = document.file_path
        document.meta = None
        document.working = False
        document.failed_strokes.clear()
        owner._cleanup_drafts()

    def restore(self, document):
        state = document.canvas_state
        if state is None:
            return
        from core.logger import core_logger
        core_logger.event('resources', 'TAB_RESTORE', f'paint: {document.file_path}')
        canvas = document.canvas
        ids = {layer["id"] for layer in default_layers(document.meta)}
        if state["active"] in ids:
            canvas.active_layer = state["active"]
        canvas.selected_layers = state["selected"] & ids
        canvas.pixel_selection = state["selection"]
        canvas.tool = state["tool"]
        document.canvas_state = None
        canvas.prepare_preview()
