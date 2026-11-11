from ui.document_paths import document_path, show_document_path
"""Paint workspace: lightweight tabs, document actions and sparse raster canvases."""
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style
from ui.icons import set_icon


import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (
    Signal,
    QEvent,
    QEventLoop,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QKeySequence, QPainterPath, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget, QWidgetAction)
from ui.widgets.dialogs import MessageBox as QMessageBox

from core.paths import APP_PATHS
from ui.app_state import app_state
from ui.event_bus import event_bus
from ui.views.file_view import FileTabBar, NewTabButton, TabCloseButton
from ui.views.paint.cache import PaintIO, TileCache
from ui.views.paint.canvas import PaintCanvas
from ui.views.paint.panels import CanvasSettings, ToolsPanel
from ui.views.paint.navigation import NavigationPanel
from ui.views.paint.tab_memory import PaintTabMemoryManager
from ui.views.paint import raster
from ui.views.paint.runtime import (
    ImagePool,
    prepare_session,
    get_session,
    close_session,
)
from ui.views.paint.storage import (
    create_document,
    export_region,
    import_image,
    read_metadata,
    save_copy,
)

PAINT_EXTENSIONS = {".tpaint", ".png", ".jpg", ".jpeg"}


class PaintTabBar(FileTabBar):
    def mouseDoubleClickEvent(self, event):
        # Native documents are renamed through Save As; inherited FileTabBar
        # assumes every tab is an ordinary text file on disk.
        event.accept()


@dataclass(eq=False)
class PaintDocument:
    canvas: PaintCanvas
    title: str
    source: str
    file_path: str = ""
    image_path: str = ""
    meta: dict = None
    camera_state: list = None
    dirty: bool = False
    busy: str = ""
    job: int = None
    error: str = ""
    working: bool = False
    pending_edits: list = field(default_factory=list)
    failed_strokes: set = field(default_factory=set)
    canvas_state: dict = None


class PaintView(QWidget):
    agent_request_submitted = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PaintWorkspace")
        self.cache = TileCache()
        self.io = PaintIO(self)
        self.pixels = ImagePool()
        self._shutdown_started = False
        self._resources_closed = False
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown, wait=self.finish_shutdown)
        self._runtime_paths = set()
        self._thumbnail_pending = {}
        self._thumbnail_jobs = {}
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setSingleShot(True)
        self._thumbnail_timer.timeout.connect(self._update_thumbnails)
        self._cleanup_paths = set()
        self._draft_paths = set()
        self.io.activity_changed.connect(self._cleanup_drafts)
        self.documents = []
        self.tab_memory = PaintTabMemoryManager(self)
        self.jpeg_quality = 90
        self._restoring = False
        self._confirming = False
        self._menu = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = QFrame(self)
        self.header.setObjectName("FileEditorHeaderBar")
        self.header.setFixedHeight(32)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 8, 0)
        header.setSpacing(6)
        header.setAlignment(Qt.AlignVCenter)
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        self.menu_button = QToolButton(self.header)
        self.menu_button.setObjectName("PipelineManagerToggle")
        self.menu_button.setFixedSize(28, 28)
        set_icon(self.menu_button, 'menu-2', square=True)
        set_text(self.menu_button, 'ui.paint_view.sketch_file_menu_b6d9', setter='setToolTip')
        self.menu_button.clicked.connect(self._file_menu)
        header.addWidget(self.menu_button)
        self.tab_bar = PaintTabBar(self.header)
        self.tab_bar.currentChanged.connect(self._current_changed)
        header.addWidget(self.tab_bar)
        self.new_button = NewTabButton(self.header)
        self.new_button.menu_requested.connect(self.show_canvas_settings)
        header.addWidget(self.new_button)
        header.addStretch(1)
        root.addWidget(self.header)
        self.path_bar = QFrame(self)
        self.path_bar.setObjectName("FileDocToolbar")
        self.path_bar.setFixedHeight(22)
        self.path_bar.setAttribute(Qt.WA_StyledBackground, True)
        path_layout = QHBoxLayout(self.path_bar)
        path_layout.setContentsMargins(16, 2, 8, 2)
        path_layout.setSpacing(6)
        self.path_label = QLabel(self.path_bar)
        self.path_label.setObjectName("FileDocBreadcrumb")
        self.path_label.setTextFormat(Qt.PlainText)
        self.path_label.setTextFormat(Qt.PlainText)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setMinimumWidth(0)
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        path_layout.addWidget(self.path_label, 1)
        self.path_bar.hide()
        root.addWidget(self.path_bar)
        self.body = QWidget(self)
        self.body.installEventFilter(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self.body)
        body_layout.addWidget(self.stack)
        from ui.widgets.empty_workspace import EmptyWorkspace
        self.placeholder = EmptyWorkspace(self.stack, new_key='ui.panels.new_canvas_f32b',
            create=self.show_canvas_settings, open_file=self.open_dialog)
        self.placeholder_new_btn = self.placeholder.new_button
        self.placeholder_open_btn = self.placeholder.open_button
        self.stack.addWidget(self.placeholder)
        root.addWidget(self.body, 1)
        self.tools = ToolsPanel(self.body, APP_PATHS.runtime / "paint_preferences.json")
        self.navigation = NavigationPanel(self.body, self.io)
        self.navigation.settings_requested.connect(self._canvas_settings)
        controls = self.tools.controls
        controls.tool_changed.connect(
            lambda name: self._canvas_action("set_tool", name)
        )
        controls.colour_changed.connect(
            lambda colour: self._tool_value("ink", QColor(colour))
        )
        controls.opacity_changed.connect(
            lambda value: self._tool_value("brush_opacity", value)
        )
        controls.width_changed.connect(
            lambda value: self._tool_value("brush_width", value)
        )
        controls.active_changed.connect(self._active_layer)
        controls.selection_changed.connect(self._selected_layers)
        controls.layer_command.connect(self._layer_command)
        self.settings = CanvasSettings(self.body)
        self.settings.create_requested.connect(self.create_canvas)
        self.settings.image_requested.connect(self.open_image_dialog)
        self.status = QLabel(self.body)
        apply_style(self.status, 'background:@background.panel@; color:@text.primary@; padding:6px; border-radius:4px;')
        self.status.hide()
        self.tools.hide()
        for key, callback in [
            ("Ctrl+N", self.show_canvas_settings),
            ("Ctrl+O", self.open_dialog),
            ("Ctrl+S", self.save_document),
            ("Ctrl+Shift+S", self.save_as),
            ("Ctrl+W", self.close_current),
            ("Ctrl+E", self.export_document),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Shift+Z", self.redo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+T", lambda: self._canvas_action("set_tool", "transform")),
        ]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
        event_bus.open_file_requested.connect(self.open_file)
        event_bus.file_renamed.connect(self.rename_file)

    def current_document(self):
        return self.tab_bar.tabData(self.tab_bar.currentIndex())

    def _canvas_action(self, method, *args):
        document = self.current_document()
        if document and document.meta:
            getattr(document.canvas, method)(*args)

    def _canvas_settings(self, canvas, payload):
        document = next((d for d in self.documents if d.canvas is canvas), None)
        if document and document.meta and document.meta["mode"] == "endless":
            self._edit_document(document, "canvas", payload)

    def _tool_value(self, name, value):
        document = self.current_document()
        if document:
            setattr(document.canvas, name, value)
            document.canvas.update()

    def _active_layer(self, identity):
        document = self.current_document()
        if document and document.meta:
            if document.canvas.active_layer != identity:
                document.canvas._selected_text = None
                document.canvas._text_hover = None
            document.canvas.active_layer = identity
            document.canvas.prepare_preview()

    def _selected_layers(self, identities):
        document = self.current_document()
        if document:
            document.canvas.selected_layers = set(identities)
            document.canvas.prepare_preview()

    def _sync_tools(self):
        document = self.current_document()
        canvas = (
            document.canvas
            if document and document.meta and document.meta["mode"] == "endless"
            else None
        )
        self.navigation.set_canvas(canvas)
        self.navigation.update_settings()
        self.navigation.setEnabled(
            bool(canvas and not document.busy and canvas._stroke_id is None)
        )
        controls = self.tools.controls
        controls.setEnabled(
            bool(
                document and document.meta and document.busy in ("", "Editing canvas…")
            )
        )
        if not document:
            return
        canvas = document.canvas
        canvas.ink = QColor(controls.wheel.colour)
        canvas.brush_width = controls.width.value()
        canvas.brush_opacity = controls.opacity.value() / 100
        canvas.edit_enabled = bool(
            document.meta and document.busy in ("", "Editing canvas…")
        )
        for widget in (
            controls.layers,
            controls.add,
            controls.remove,
            controls.import_button,
        ):
            widget.setEnabled(
                document.busy in ("", "Editing canvas…") and canvas._stroke_id is None
            )
        controls.checked = set(canvas.selected_layers)
        controls.document_key = document.source
        layers = [dict(layer) for layer in raster.default_layers(document.meta or {})]
        if document.meta and document.meta["mode"] == "finite":
            for layer in layers:
                layer["background"] = False
        elif document.meta:
            layers = [layer for layer in layers if not layer.get("background")]
        controls.canvas_dimensions = (
            (document.meta["width"], document.meta["height"])
            if document.meta and document.meta["mode"] == "finite"
            else (1, 1)
        )
        controls.set_layers(layers, canvas.active_layer)
        controls.choose(canvas.tool, False)

    def _layer_command(self, payload):
        document = self.current_document()
        if document:
            payload = dict(payload)
            if payload["command"] == "import":
                self.import_layer_images()
                return
            if payload["command"] in {"delete","visible","opacity"}:
                identity = payload.get('layer',document.canvas.active_layer)
                selected = set(document.canvas.selected_layers)
                targets = set(payload.get('layers') or [identity])
                if identity in selected and targets <= selected:
                    targets = selected
                payload['layers'] = sorted(targets)
            if payload["command"] == "delete":
                targets = set(
                    payload.get("layers")
                    or [payload.get("layer", document.canvas.active_layer)]
                )
                if (
                    len(targets) > 1
                    and QMessageBox.question(
                        self,
                        tr('ui.paint_view.delete_layers_2528'),
                        tr('ui.paint_view.delete_v0_selected_layers_276c', v0=len(targets)),
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    != QMessageBox.Yes
                ):
                    return
                payload["layers"] = list(targets)
            if payload["command"] == "add":
                layers = raster.default_layers(document.meta or {})
                number = len(layers)
                while f"Layer {number}" in {layer["name"] for layer in layers}:
                    number += 1
                payload.update(
                    id=uuid.uuid4().hex,
                    name=f"Layer {number}",
                )
            self._edit_document(document, "layer", payload)

    def import_layer_images(self, paths=None, position=None, document=None):
        document = document or self.current_document()
        if (
            not document
            or not document.meta
            or document.canvas._stroke_id is not None
            or document.busy not in ("", "Editing canvas…")
        ):
            return
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Import images as layers",
                "",
                "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)",
            )
        if not paths:
            return
        position = position if position is not None else document.canvas.center
        for source in paths:
            from PySide6.QtGui import QImage
            content = {'image':QImage(source)} if isinstance(source,QImage) else {'path':str(source)}
            self._edit_document(document, "image", dict(content,
                id=uuid.uuid4().hex,
                name="Clipboard image" if isinstance(source,QImage) else Path(source).stem,
                new_layer=True,
                position=[position.x(),position.y()]))

    def _copy_selection(self, document):
        canvas = document.canvas
        if document.busy or not document.meta or canvas.pixel_selection is None:
            return
        from .paint.raster import selection_image
        selection = QPainterPath(canvas.pixel_selection)
        targets = set(canvas.selected_layers) or {canvas.active_layer}
        def complete(image,error):
            if error:
                self._document_error(document,str(error))
            elif image is not None and not image.isNull():
                from PySide6.QtWidgets import QApplication
                QApplication.clipboard().setImage(image)
        self.io.submit(lambda cancel:selection_image(document.source,targets,selection,cancel),complete)

    def _paint_context_menu(self, document, position):
        if not document.meta or document.canvas._stroke_id is not None:
            return
        from ui.widgets.workspace_menu import WorkspaceContextMenu
        menu = WorkspaceContextMenu(document.canvas)
        menu.setObjectName('PaintContextMenu')
        menu.add_quick_action('sparkles',tr('ui.paint_view.ask_agent_about_this_image_1cbf'),
            lambda:self._open_agent_popup(document,position))
        localize(add_caption_action(menu, 'addAction', "Copy selection", lambda:self._copy_selection(document)), 'ui.paint_view.copy_selection_63a5').setEnabled(document.canvas.pixel_selection is not None and not document.busy)
        from ui.image_mime import can_import
        from PySide6.QtWidgets import QApplication
        localize(add_caption_action(menu, 'addAction', "Paste image…", document.canvas.paste_images), 'ui.paint_view.paste_image_cbf2').setEnabled(can_import(QApplication.clipboard().mimeData()))
        menu.addSeparator()
        localize(add_caption_action(menu, 'addAction', "Undo", self.undo), 'ui.paint_view.undo_a828').setEnabled(
            document.meta.get("undo_pos", 0) > 0
        )
        localize(add_caption_action(menu, 'addAction', "Redo", self.redo), 'ui.paint_view.redo_7427').setEnabled(
            document.meta.get("undo_pos", 0) < document.meta.get("undo_max", 0)
        )
        menu.aboutToHide.connect(menu.deleteLater)
        self._paint_menu = menu
        menu.popup(document.canvas.mapToGlobal(position))

    def _open_agent_popup(self, document, position):
        from ui.views.paint.agent import PaintAgentPopup, render_snapshot

        old = getattr(document.canvas, "agent_popup", None)
        if old:
            old.cancel()
            old.tags_bar.deleteLater()
            old.deleteLater()
        popup = PaintAgentPopup(document.canvas)
        document.canvas.agent_popup = popup
        popup.submitted.connect(
            lambda text: self._submit_paint_agent(document, popup, text)
        )
        popup.open_at(position)
        source = document.source

        def complete(image, error):
            if (
                document not in self.documents
                or document.canvas.agent_popup is not popup
                or popup.isHidden()
            ):
                return
            if error:
                bind_caption(popup.hint_label, str(error))
            else:
                popup.set_image(image)

        self.io.submit(lambda cancel: render_snapshot(source, cancel), complete)

    def _submit_paint_agent(self, document, popup, text):
        whole_file = popup.whole_file_toggle.isChecked()
        source = document.source
        image = (popup.preview.image if popup.whole_file_toggle.isChecked()
                 else popup.preview.selected_image())
        if image is None:
            return
        destination = (
            APP_PATHS.runtime / "paint_agent_images" / (uuid.uuid4().hex + ".png")
        )
        popup.send_button.setEnabled(False)

        def write(cancel):
            from PySide6.QtCore import QSaveFile, QIODevice

            destination.parent.mkdir(parents=True, exist_ok=True)
            if whole_file:
                # PNG в исходном разрешении, включая ещё не сохранённые мазки.
                from ui.views.paint import storage
                from ui.views.paint.runtime import get_session
                from ui.views.paint.navigation import overview_bounds
                session = get_session(source)
                meta = session.metadata() if session else storage.read_metadata(source)
                rect = (QRectF(0, 0, meta['width'], meta['height']) if meta['mode']=='finite'
                        else overview_bounds(meta)).toAlignedRect()
                export_region(source, rect, str(destination), 'PNG', 100, cancel)
                return str(destination)
            output = QSaveFile(str(destination))
            if not output.open(QIODevice.WriteOnly) or not image.save(output, "PNG"):
                output.cancelWriting()
                raise IOError("Could not prepare image for the agent.")
            if cancel.is_set() or not output.commit():
                output.cancelWriting()
                raise IOError("Image preparation interrupted.")
            return str(destination)

        def complete(path, error):
            if (
                document not in self.documents
                or document.canvas.agent_popup is not popup
            ):
                destination.unlink(missing_ok=True)
                return
            if error:
                popup.send_button.setEnabled(True)
                bind_caption(popup.hint_label, str(error))
                return

            def finished(success):
                destination.unlink(missing_ok=True)
                if document in self.documents and document.canvas.agent_popup is popup:
                    if success:
                        popup.cancel()
                    else:
                        popup.send_button.setEnabled(True)

            if not self.receivers("2agent_request_submitted(QString,PyObject)"):
                finished(False)
                set_text(popup.hint_label, 'ui.paint_view.chat_is_unavailable_94ee')
                return
            self.agent_request_submitted.emit(text, dict(path=path, finished=finished))

        self.io.submit(write, complete)

    def undo(self):
        document = self.current_document()
        if document and document.canvas._stroke_id is not None:
            document.canvas.cancel_stroke()
            document.canvas._gesture = None
            document.canvas._gesture_path = None
            return
        if document and (document.working or document.busy == "Editing canvas…"):
            self._edit_document(document, "undo", {})

    def redo(self):
        document = self.current_document()
        if document and document.canvas._stroke_id is not None:
            return
        if document and (document.working or document.busy == "Editing canvas…"):
            self._edit_document(document, "redo", {})

    def _sample(self, document, point):
        if not document.meta or document.busy:
            return

        def completed(colour, error):
            if (
                not error
                and document in self.documents
                and document is self.current_document()
            ):
                self.tools.controls.set_colour(colour)

        self.io.submit(
            lambda _: raster.sample_color(document.source, point), completed, priority=1
        )

    def _edit_document(self, document, kind, payload, *, prepared=False):
        if (
            document not in self.documents
            or not document.meta
            or document.busy not in ("", "Editing canvas…")
        ):
            return
        canvas = document.canvas
        payload = dict(payload)
        thumbnail = self._thumbnail_jobs.pop(document, None)
        if thumbnail is not None:
            self.io.cancel(thumbnail[0])
            self._thumbnail_pending.setdefault(document, set()).update(thumbnail[1])
        if kind == "stroke" and payload["stroke_id"] in document.failed_strokes:
            return
        if canvas is self.navigation.canvas:
            self.navigation.pause()
        if kind == "stroke":
            canvas.pause_fetch()
        if not prepared:
            payload.setdefault("layer", canvas.active_layer)
            payload.setdefault("color", canvas.ink.name())
            payload.setdefault("opacity", canvas.brush_opacity)
            payload.setdefault("width", canvas.brush_width)
            payload.setdefault(
                "selection",
                (
                    QPainterPath(canvas.pixel_selection)
                    if canvas.pixel_selection is not None
                    else None
                ),
            )
            if kind in ("move", "transform", "delete_selection"):
                payload.setdefault("layers", list(canvas.selected_layers) or [canvas.active_layer])
        payload.setdefault("selection_revision",canvas._selection_revision)
        if document.busy:
            if (
                kind == "stroke"
                and payload["phase"] == "update"
                and document.pending_edits
                and document.pending_edits[-1][0] == "stroke"
                and document.pending_edits[-1][1]["phase"] == "update"
                and document.pending_edits[-1][1]["stroke_id"] == payload["stroke_id"]
            ):
                document.pending_edits[-1][1]["points"].extend(payload["points"])
            else:
                document.pending_edits.append((kind, payload))
            return
        source = document.source
        target = (
            self._new_draft() if document.file_path and not document.working else source
        )
        original = document.file_path
        live = canvas._live_stroke
        preview_keys = list(live.keys) if kind == 'stroke' and payload['phase'] == 'begin' and live is not None and live.identity == payload['stroke_id'] else []
        document.busy = "Editing canvas…"
        # During a stroke only pixels change. Repeatedly synchronizing all
        # tool/layer widgets competes with mouse input and viewport painting.
        sync_controls = kind != "stroke" or payload["phase"] in ("begin", "end", "cancel")
        if sync_controls:
            self._sync_tools()
        self._refresh_status()

        def edit(cancel):
            try:
                session = prepare_session(source, target, original, self.pixels, cancel)
                from .paint.stroke import capture_bases
                bases = capture_bases(session, payload['layer'], preview_keys, cancel) if preview_keys else None
                meta = raster.operate(target, kind, payload, cancel)
                tiles, affected = session.take_updates()
                return meta, tiles, affected, session.dirty_rect, bases
            except Exception:
                if target != source:
                    close_session(target)
                    Path(target).unlink(missing_ok=True)
                raise

        def completed(result, error):
            if document not in self.documents:
                return
            if kind in ('move', 'transform'):
                canvas._pending_transform = None
                canvas.update()
            document.job = None
            document.busy = ""
            canvas.edit_enabled = True
            if error:
                if kind == "stroke":
                    if canvas._live_stroke is not None and canvas._live_stroke.identity == payload['stroke_id']:
                        canvas._live_stroke = None
                        canvas.update()
                    identity = payload["stroke_id"]
                    document.failed_strokes.add(identity)
                    document.pending_edits[:] = [
                        (k, p)
                        for k, p in document.pending_edits
                        if k != "stroke" or p["stroke_id"] != identity
                    ]
                    if canvas._stroke_id == identity:
                        canvas._pointer_samples.release()
                        canvas._stroke_timer.stop()
                        canvas._stroke_id = None
                        canvas._gesture = None
                        canvas._gesture_path = None
                session = get_session(source)
                if session is not None:
                    document.meta = session.metadata()
                    tiles, affected = session.take_updates()
                    canvas.apply_edit(source, document.meta, tiles, affected)
                    if canvas._selection_revision == payload["selection_revision"]:
                        from .paint.selection import decode
                        canvas.pixel_selection = decode(document.meta.get("pixel_selection"))
                        canvas.prepare_preview()
                        canvas.update()
                self._document_error(document, str(error))
            else:
                meta, tiles, affected, dirty_rect, bases = result
                self._runtime_paths.add(target)
                document.source = target
                document.working = True
                document.meta = meta
                document.dirty = (
                    not document.file_path
                    or meta["undo_pos"] != meta["clean_pos"]
                    or meta.get("settings_revision", 0)
                    != meta.get("settings_clean_revision", 0)
                )
                if canvas.meta["mode"] == "finite" and (
                    canvas.meta.get("version") == 1
                    or any(l.get("background") for l in canvas.meta.get("layers", []))
                ):
                    # Legacy previews omit the virtual backdrop. After its
                    # conversion, every preview belongs to a real pixel layer.
                    self.cache.discard_document(source)
                else:
                    self.cache.rekey_document(source, target)
                canvas.apply_edit(target, meta, tiles, affected, dirty_rect)
                if kind == 'stroke' and canvas._live_stroke is not None and canvas._live_stroke.identity == payload['stroke_id']:
                    if bases is not None:
                        canvas._live_stroke.bases = bases
                        canvas.update()
                    if payload['phase'] in ('end', 'cancel'):
                        canvas._live_stroke = None
                        canvas.update()
                identities = {layer["id"] for layer in meta["layers"]}
                if kind in ("undo", "redo", "image", "text_object") or (
                    kind == "layer" and payload["command"] in ("add", "delete")
                ):
                    canvas.active_layer = meta["active_layer"]
                if canvas.active_layer not in identities:
                    canvas.active_layer = meta["active_layer"]
                canvas.selected_layers.intersection_update(identities)
                if kind == 'text_object' or (kind == 'image' and payload.get('new_layer',True)):
                    canvas.selected_layers = {canvas.active_layer}
                if kind in ("undo", "redo", "selection", "move", "transform", "image") and canvas._selection_revision == payload["selection_revision"]:
                    from .paint.selection import decode
                    canvas.pixel_selection = decode(meta.get('pixel_selection'))
                    canvas.update()
                if kind in (
                    "move",
                    "transform",
                    "undo",
                    "redo",
                    "layer",
                    "delete_selection",
                    "selection",
                ):
                    canvas.prepare_preview()
                if (tiles and kind != "stroke") or (
                    kind == "stroke" and payload["phase"] == "end"
                ):
                    thumbnail_layers = (
                        set(payload.get("layers") or [payload["layer"]])
                        if kind not in ("layer", "undo", "redo", "image")
                        else {
                            layer["id"]
                            for layer in meta["layers"]
                            if not layer.get("background")
                        }
                    )
                    self._thumbnail_pending.setdefault(document, set()).update(thumbnail_layers)
                    self._thumbnail_timer.start(200)
            if sync_controls or error:
                self._refresh_title(document)
                self._sync_tools()
            if document.pending_edits:
                next_kind, next_payload = document.pending_edits.pop(0)
                self._edit_document(document, next_kind, next_payload, prepared=True)
            elif canvas is self.navigation.canvas and canvas._stroke_id is None:
                if error or kind != "stroke" or payload["phase"] in ("end", "cancel"):
                    self.navigation.refresh()

        document.job = self.io.submit(edit, completed)

    def _update_thumbnails(self):
        pending, self._thumbnail_pending = self._thumbnail_pending, {}
        for document, identities in pending.items():
            if document not in self.documents:
                continue
            if document.busy or document.pending_edits or document.canvas._stroke_id is not None:
                self._thumbnail_pending.setdefault(document, set()).update(identities)
                continue
            source = document.source
            session = get_session(source)
            if session is None:
                continue

            def complete(images, error, doc=document, path=source):
                self._thumbnail_jobs.pop(doc, None)
                if (
                    not error
                    and doc in self.documents
                    and doc.source == path
                    and self.current_document() is doc
                ):
                    for identity, image in images:
                        self.tools.controls.set_thumbnail(identity, image)

            job = self.io.submit(
                lambda cancel, s=session, ids=identities: [
                    (i, s.thumbnail(i, cancel)) for i in ids
                ],
                complete,
            )
            self._thumbnail_jobs[document] = (job, identities)
        if self._thumbnail_pending:
            self._thumbnail_timer.start(200)

    def eventFilter(self, watched, event):
        if watched is self.body and event.type() == QEvent.Resize:
            self.settings.setGeometry(self.body.rect())
            self.tools.position()
            self.navigation.position()
            self.status.adjustSize()
            self.status.move(max(0, (self.body.width() - self.status.width()) // 2), 12)
        return super().eventFilter(watched, event)

    def show_canvas_settings(self):
        self.settings.setGeometry(self.body.rect())
        self.settings.show()
        self.settings.raise_()
        self.settings.preset.setFocus()

    def _new_draft(self):
        directory = APP_PATHS.runtime / "paint_drafts"
        directory.mkdir(parents=True, exist_ok=True)
        path = str(directory / (uuid.uuid4().hex + ".tpaint"))
        self._draft_paths.add(path)
        return path

    def _add_document(self, title, source, file_path="", image_path="", camera=None):
        canvas = PaintCanvas(self.cache, self.io, self.stack)
        canvas.tool = self.tools.controls.current_tool
        from ui.views.paint.text_preferences import restore_defaults
        restore_defaults(canvas,self.tools.controls.text_defaults)
        document = PaintDocument(
            canvas,
            title,
            source,
            file_path=file_path,
            image_path=image_path,
            camera_state=camera,
        )
        document.materials_root = app_state.view_workspace("paint")
        self.documents.append(document)
        canvas.camera_changed.connect(
            lambda d=document: (
                self.navigation.camera_changed()
                if d.canvas is self.navigation.canvas
                else None
            )
        )
        canvas.area_selected.connect(
            lambda rect, d=document: self._export_area(d, rect)
        )
        canvas.error.connect(lambda text, d=document: self._document_error(d, text))
        canvas.edit_requested.connect(
            lambda kind, payload, d=document: self._edit_document(d, kind, payload)
        )
        canvas.copy_requested.connect(lambda d=document:self._copy_selection(d))
        canvas.images_dropped.connect(
            lambda paths, point, d=document: self.import_layer_images(paths, point, d)
        )
        canvas.context_requested.connect(
            lambda point, d=document: self._paint_context_menu(d, point)
        )
        canvas.sample_requested.connect(
            lambda point, d=document: self._sample(d, point)
        )
        canvas.text_layer_selected.connect(lambda _: self._sync_tools())
        canvas.tool_changed.connect(
            lambda name, d=document: (
                self.tools.controls.choose(name, False)
                if d is self.current_document()
                else None
            )
        )
        self.stack.addWidget(canvas)
        self.tab_bar.blockSignals(True)
        index = self.tab_bar.addTab(title)
        self.tab_bar.setTabData(index, document)
        close = TabCloseButton(self.tab_bar)
        close.clicked.connect(lambda _=False, d=document: self.close_document(d))
        self.tab_bar.setTabButton(index, QTabBar.RightSide, close)
        self.tab_bar.blockSignals(False)
        return document, index

    def _refresh_title(self, document):
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) is document:
                bind_caption(self.tab_bar, document.title, setter='setTabText', index=i)
                break
        self._refresh_status()

    def _refresh_path(self):
        document = self.current_document()
        self.path_bar.setVisible(document is not None)
        path = (document.file_path or document.image_path) if document else ''
        if path:
            show_document_path(self.path_label, path)
        else:
            bind_caption(self.path_label, (document.title + tr('ui.caption.49bc942f24fb')) if document else '')
            bind_caption(self.path_label, tr('ui.caption.7fc4e521fe62') if document else '', setter='setToolTip')

    def _refresh_status(self):
        self._refresh_path()
        document = self.current_document()
        message = (
            document.busy if document and document.busy != "Editing canvas…" else ""
        )
        if self.status.text() != message:
            bind_caption(self.status, message)
            self.status.setVisible(bool(message))
            if message:
                self.status.adjustSize()
                self.status.move(
                    max(0, (self.body.width() - self.status.width()) // 2), 12
                )
                self.status.raise_()
        self.tools.controls.setEnabled(
            bool(
                document and document.meta and document.busy in ("", "Editing canvas…")
            )
        )
        if document:
            document.canvas.edit_enabled = bool(
                document.meta and document.busy in ("", "Editing canvas…")
            )

    def _current_changed(self, index):
        document = self.tab_bar.tabData(index)
        if self.isVisible():
            from ui.history_focus import focus_document
            focus_document(document)
        if document:
            self.tab_memory.touch(document)
        self.stack.setCurrentWidget(document.canvas if document else self.placeholder)
        self.tools.setVisible(document is not None)
        self.tools.raise_()
        self._refresh_status()
        if self.settings.isVisible():
            self.settings.raise_()
        if (
            document
            and document.meta is None
            and not document.busy
            and not document.error
            and not self._restoring
        ):
            self._load_document(document)
        self._sync_tools()

    def _activate(self, index, switch=True):
        self.tab_bar.setCurrentIndex(index)
        self._current_changed(index)
        if switch:
            event_bus.switch_view_tab_requested.emit(4)

    def _load_document(self, document):
        document.busy = "Loading canvas…"
        source = document.source

        def completed(meta, error):
            if document not in self.documents:
                return
            document.job = None
            document.busy = ""
            if error:
                document.error = str(error)
                document.canvas.message = "Could not open this canvas."
                document.canvas.update()
                self._document_error(document, str(error))
            else:
                document.meta = meta
                document.canvas.configure(source, meta, document.camera_state)
                document.canvas.active_layer = meta.get("active_layer", "paint")
                self.tab_memory.restore(document)
                self._sync_tools()
            self._refresh_status()

        document.job = self.io.submit(
            lambda _: read_metadata(source), completed, priority=2
        )
        self._refresh_status()

    def create_canvas(self, mode, width, height):
        source = self._new_draft()
        document, index = self._add_document("Untitled", source)
        document.dirty = True
        document.busy = "Creating canvas…"
        self.settings.hide()
        self._activate(index)

        def completed(meta, error):
            if document not in self.documents:
                return
            document.job = None
            document.busy = ""
            if error:
                document.error = str(error)
                document.canvas.message = "Could not create this canvas."
                document.canvas.update()
                self._document_error(document, str(error))
            else:
                document.meta = meta
                document.canvas.configure(source, meta)
                self._sync_tools()
            self._refresh_title(document)

        document.job = self.io.submit(
            lambda cancel: self._create_paint_document(
                source, mode, width, height, cancel
            ),
            completed,
        )
        self._refresh_title(document)
        return document

    @staticmethod
    def _create_paint_document(source, mode, width, height, cancel):
        create_document(source, mode, width, height, cancel)
        return raster.initialize(source, cancel=cancel)

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Sketch file",
            app_state.view_workspace("paint"),
            "Sketch / Images (*.tpaint *.png *.jpg *.jpeg *.svg *.webp *.gif *.bmp);;Sketch (*.tpaint);;Images (*.png *.jpg *.jpeg *.svg *.webp *.gif *.bmp)",
        )
        if path:
            self.open_file(path, as_image=True)

    def open_image_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Image", app_state.view_workspace("paint"), "Images (*.png *.jpg *.jpeg *.svg *.webp *.gif *.bmp)"
        )
        if path:
            self.settings.hide()
            self.open_file(path, as_image=True)

    def open_file(self, path, *, switch=True, camera=None, as_image=False):
        suffix = Path(path).suffix.lower()
        if suffix not in PAINT_EXTENSIONS and not as_image:
            return
        path = str(Path(path).resolve())
        for i in range(self.tab_bar.count()):
            document = self.tab_bar.tabData(i)
            if path in (document.file_path, document.image_path):
                self._activate(i, switch)
                return document
        if suffix == ".tpaint":
            document, index = self._add_document(
                Path(path).name, path, file_path=path, camera=camera
            )
            if not self._restoring:
                self._activate(index, switch)
            return document
        source = self._new_draft()
        document, index = self._add_document(Path(path).stem, source, image_path=path)
        document.dirty = True
        document.busy = "Importing image…"
        self._activate(index, switch)

        def completed(meta, error):
            if document not in self.documents:
                return
            document.job = None
            document.busy = ""
            if error:
                document.error = str(error)
                document.canvas.message = "Could not import this image."
                document.canvas.update()
                self._document_error(document, str(error))
            else:
                document.meta = meta
                document.canvas.configure(source, meta)
                self._sync_tools()
            self._refresh_title(document)

        document.job = self.io.submit(
            lambda cancel: import_image(path, source, cancel), completed
        )
        self._refresh_title(document)
        return document

    def _document_error(self, document, text):
        if document in self.documents:
            document.busy = ""
            self._refresh_status()
            QMessageBox.warning(self, tr('ui.paint_view.sketch_0f9b'), text)

    def save_as(self):
        self.save_document(save_as=True)

    def save_document(self, document=None, *, save_as=False, done=None, destination=None, quiet=False):
        document = document or self.current_document()
        if document and document.canvas._text_box:
            document.canvas.commit_text()
            if document.busy or document.pending_edits:
                # Сначала завершаем правку текста, затем сохраняем результат работника.
                def after_text():
                    if document not in self.documents:
                        self.io.activity_changed.disconnect(after_text)
                        if done:
                            done(False)
                    elif not document.busy and not document.pending_edits:
                        self.io.activity_changed.disconnect(after_text)
                        self.save_document(document, save_as=save_as, done=done,
                                           destination=destination, quiet=quiet)
                self.io.activity_changed.connect(after_text)
                return True
        if (
            not document
            or not document.meta
            or document.busy
            or document.canvas._stroke_id is not None
        ):
            if done:
                done(False)
            return False
        explicit_destination = destination is not None
        destination = destination if explicit_destination else document.file_path
        if not explicit_destination and (save_as or not destination):
            if not destination:
                Path(document.materials_root).mkdir(parents=True, exist_ok=True)
            initial = destination or os.path.join(
                document.materials_root, document.title + ".tpaint"
            )
            destination, _ = QFileDialog.getSaveFileName(
                self, "Save Sketch file", initial, "Sketch (*.tpaint)"
            )
            if not destination:
                if done:
                    done(False)
                return False
            if not destination.lower().endswith(".tpaint"):
                destination += ".tpaint"
                if Path(destination).exists() and not self._confirm_replace(
                    destination
                ):
                    if done:
                        done(False)
                    return False
        destination = str(Path(destination).resolve())
        if document is self.current_document() and self.isVisible():
            from core.file_history import focus_user_file
            focus_user_file(destination)
        if explicit_destination and not destination.lower().endswith('.tpaint'):
            destination += '.tpaint'
        if any(
            d is not document and d.file_path == destination for d in self.documents
        ):
            if not quiet:
                QMessageBox.warning(
                    self, tr('ui.paint_view.sketch_0f9b'), tr('ui.paint_view.this_file_is_already_open_in_ab2a')
                )
            if done:
                done(False)
            return False
        old_source, old_file = document.source, document.file_path
        camera = document.canvas.camera()
        document.error = ""
        document.busy = "Saving canvas…"
        self._refresh_status()

        def completed(result, error):
            success = error is None and document in self.documents
            from core.logger import core_logger
            core_logger.event('storage', 'FILE_SAVE' if success else 'FILE_SAVE_ERROR', f'{destination}' + (f': {error}' if error else ''), level='INFO' if success else 'ERROR')
            if document in self.documents:
                document.job = None
                document.busy = ""
                if error:
                    if quiet:
                        document.error = str(error)
                        self._refresh_status()
                    else:
                        self._document_error(document, str(error))
                else:
                    if destination == old_file and document.file_path != old_file:
                        result = document.file_path  # Renamed during a native Save.
                    document.file_path = result
                    if document is self.current_document() and self.isVisible():
                        from ui.history_focus import focus_document
                        focus_document(document)
                    if not document.working:
                        document.source = result
                    else:
                        document.meta["clean_pos"] = document.meta["undo_pos"]
                        document.meta["settings_clean_revision"] = document.meta.get(
                            "settings_revision", 0
                        )
                        document.meta["baseline"] = result
                    document.title = Path(result).name
                    document.dirty = False
                    if document.canvas.path != document.source:
                        self.cache.rekey_document(old_source, document.source)
                        document.canvas.apply_edit(
                            document.source, document.meta, [], set()
                        )
                    else:
                        document.canvas.meta = document.meta
                    if not old_file and not document.working:
                        self._cleanup_paths.add(old_source)
                self._refresh_title(document)
            if done:
                done(success)

        save = raster.save_working if document.working else save_copy
        from core.file_history import record_file_save
        save = record_file_save(lambda source, target, cancel=None: target)(save)
        document.job = self.io.submit(
            lambda cancel: save(old_source, destination, cancel), completed
        )
        return True

    def export_document(self):
        document = self.current_document()
        if (
            not document
            or not document.meta
            or document.busy
            or document.canvas._stroke_id is not None
        ):
            return
        if document.meta["mode"] == "endless":
            self.select_export_area()
        else:
            self._export_area(
                document, QRect(0, 0, document.meta["width"], document.meta["height"])
            )

    def select_export_area(self):
        document = self.current_document()
        if (
            document
            and document.meta
            and not document.busy
            and document.canvas._stroke_id is None
        ):
            document.canvas.set_export_mode(True)

    def _export_area(self, document, rect):
        if document not in self.documents or document.busy:
            return
        document.canvas.set_export_mode(False)
        path, chosen_filter = QFileDialog.getSaveFileName(
            self,
            "Export image",
            os.path.join(app_state.view_workspace("paint"), Path(document.title).stem + ".png"),
            "PNG (*.png);;JPEG (*.jpg *.jpeg)",
        )
        if not path:
            return
        format_name = "JPEG" if chosen_filter.startswith("JPEG") else "PNG"
        suffix = Path(path).suffix.lower()
        if suffix in (".jpg", ".jpeg"):
            format_name = "JPEG"
        elif suffix == ".png":
            format_name = "PNG"
        else:
            path += ".jpg" if format_name == "JPEG" else ".png"
            if Path(path).exists() and not self._confirm_replace(path):
                return
        path = str(Path(path).resolve())
        if path == document.source or any(d.file_path == path for d in self.documents):
            QMessageBox.warning(
                self, tr('ui.paint_view.sketch_0f9b'), tr('ui.paint_view.choose_an_image_filename_not_an_5701')
            )
            return
        document.busy = "Exporting image…"
        self._refresh_status()
        source, quality = document.source, self.jpeg_quality

        def completed(_, error):
            if document not in self.documents:
                return
            document.job = None
            document.busy = ""
            if error:
                self._document_error(document, str(error))
            self._refresh_status()

        document.job = self.io.submit(
            lambda cancel: export_region(
                source, rect, path, format_name, quality, cancel
            ),
            completed,
        )

    def _file_menu(self):
        if self._menu:
            self._menu.close()
            self._menu.deleteLater()
        menu = QMenu(self.menu_button)
        menu.setObjectName("FileManagerContextMenu")
        localize(add_caption_action(menu, 'addAction', "New canvas…", self.show_canvas_settings), 'ui.paint_view.new_canvas_2ff2')
        localize(add_caption_action(menu, 'addAction', "Open…", self.open_dialog), 'ui.paint_view.open_1f1d')
        menu.addSeparator()
        document = self.current_document()
        enabled = bool(
            document
            and document.meta
            and not document.busy
            and document.canvas._stroke_id is None
        )
        localize(add_caption_action(menu, 'addAction', "Undo", self.undo), 'ui.paint_view.undo_a828').setEnabled(
            enabled and document.working and bool(document.meta.get("undo_pos"))
        )
        localize(add_caption_action(menu, 'addAction', "Redo", self.redo), 'ui.paint_view.redo_7427').setEnabled(
            enabled
            and document.working
            and document.meta.get("undo_pos", 0) < document.meta.get("undo_max", 0)
        )
        menu.addSeparator()
        document = self.current_document()
        enabled = bool(
            document
            and document.meta
            and not document.busy
            and document.canvas._stroke_id is None
        )
        for title, callback in [
            ("Save", self.save_document),
            ("Save As…", self.save_as),
            ("Export PNG / JPEG…", self.export_document),
            ("Export area…", self.select_export_area),
        ]:
            add_caption_action(menu, 'addAction', title, callback).setEnabled(enabled)
        quality_menu = localize(add_caption_action(menu, 'addMenu', "JPEG quality"), 'ui.paint_view.jpeg_quality_9c64', setter='setTitle')
        spin = QSpinBox(quality_menu)
        spin.setRange(1, 100)
        spin.setValue(self.jpeg_quality)
        spin.setPrefix("Quality: ")
        spin.valueChanged.connect(lambda value: setattr(self, "jpeg_quality", value))
        action = QWidgetAction(quality_menu)
        action.setDefaultWidget(spin)
        add_caption_action(quality_menu, 'addAction', action)
        menu.addSeparator()
        localize(add_caption_action(menu, 'addAction', "Fit canvas", lambda: self._canvas_action("fit_canvas")), 'ui.paint_view.fit_canvas_438c').setEnabled(enabled)
        add_caption_action(menu, 'addAction', "100%", lambda: self._canvas_action("set_zoom", 1.0)).setEnabled(
            enabled
        )
        self._menu = menu
        menu.popup(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height())))

    def _confirm_document(self, document):
        if document.busy and document.busy != "Loading canvas…":
            QMessageBox.information(
                self, tr('ui.paint_view.sketch_0f9b'), tr('ui.paint_view.wait_for_the_current_operation_to_5891')
            )
            return False
        if not document.dirty or (document.error and document.meta is None):
            return True
        from ui.widgets.unsaved_popup import confirm_close
        return confirm_close(self, document, title=document.title, tab_bar=self.tab_bar,
                             save=lambda: self._save_before_close(document))

    def _save_before_close(self, document):
        # Saving stays asynchronous, including during this close confirmation.
        # A nested event loop keeps repaint/navigation and worker delivery alive.
        loop = QEventLoop(self)
        result = []

        def done(success):
            result.append(success)
            loop.quit()

        started = self.save_document(document, done=done)
        if started and not result:
            loop.exec()
        loop.deleteLater()
        return bool(result and result[0])

    def _confirm_replace(self, path):
        return (
            QMessageBox.question(
                self,
                tr('ui.paint_view.sketch_0f9b'),
                tr('ui.paint_view.v0_already_exists_replace_it_91b2', v0=Path(path).name),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        )

    def close_current(self):
        document = self.current_document()
        if document:
            self.close_document(document)

    def close_document(self, document):
        if document not in self.documents or self._confirming:
            return
        self._confirming = True
        try:
            if not self._confirm_document(document):
                return
            document.canvas.release()
            thumbnail = self._thumbnail_jobs.pop(document, None)
            if thumbnail is not None:
                self.io.cancel(thumbnail[0])
            self._thumbnail_pending.pop(document, None)
            if document.job:
                self.io.cancel(document.job)
            self.documents.remove(document)
            self.tab_memory.forget(document)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) is document:
                    self.tab_bar.removeTab(i)
                    break
            self.stack.removeWidget(document.canvas)
            document.canvas.deleteLater()
            self.cache.discard_document(document.source)
            if not document.file_path or (
                document.working and document.source != document.file_path
            ):
                self._cleanup_paths.add(document.source)
                self._cleanup_drafts()
            self._current_changed(self.tab_bar.currentIndex())
        finally:
            self._confirming = False

    def confirm_close_all(self):
        if self._confirming:
            return False
        self._confirming = True
        try:
            return all(
                self._confirm_document(document) for document in list(self.documents)
            )
        finally:
            self._confirming = False

    def get_open_state(self):
        entries = []
        active = 0
        current = self.current_document()
        for i in range(self.tab_bar.count()):
            document = self.tab_bar.tabData(i)
            if document.file_path and Path(document.file_path).is_file():
                if document is current:
                    active = len(entries)
                camera = (
                    document.canvas.camera() if document.meta else document.camera_state
                )
                entries.append({"path":document.file_path,"camera":camera,"tool":document.canvas.tool})
        return {"open_paints": entries, "active_paint_index": active}

    def restore_open_state(self, entries, active_index=0):
        self._restoring = True
        try:
            for entry in entries if isinstance(entries, list) else []:
                path = entry.get("path") if isinstance(entry, dict) else entry
                if (
                    isinstance(path, str)
                    and Path(path).suffix.lower() == ".tpaint"
                    and Path(path).is_file()
                ):
                    self.open_file(
                        path,
                        switch=False,
                        camera=entry.get("camera") if isinstance(entry, dict) else None,
                    )
        finally:
            self._restoring = False
        for entry in entries if isinstance(entries,list) else []:
            if isinstance(entry,dict) and entry.get('tool') in self.tools.controls.buttons:
                document = next((d for d in self.documents if d.file_path==entry.get('path')),None)
                if document is not None:document.canvas.tool = entry['tool']
        if self.tab_bar.count():
            try:
                index = max(0, min(int(active_index), self.tab_bar.count() - 1))
            except (ValueError, TypeError):
                index = 0
            self._activate(index, switch=False)

    def rename_file(self, old, new):
        old, new = str(Path(old).resolve()), str(Path(new).resolve())
        for document in self.documents:
            if document.file_path == old:
                document.canvas.release()
                self.cache.discard_document(old)
                document.file_path = new
                session = get_session(document.source)
                if session is not None:
                    with session.lock:
                        if session.base_path == old:
                            session.base_path = new
                if not document.working:
                    document.source = new
                document.title = Path(new).name
                if document.meta:
                    document.canvas.configure(
                        document.source, document.meta, document.canvas.camera()
                    )
                elif document.busy == "Loading canvas…":
                    self.io.cancel(document.job)
                    document.job = None
                    document.busy = ""
                    document.error = ""
                    self._load_document(document)
                self._refresh_title(document)
            elif document.image_path == old:
                document.image_path = new
                if not document.file_path:
                    document.title = Path(new).stem
                self._refresh_title(document)

    def shutdown(self):
        if self._shutdown_started:return
        self._shutdown_started = True
        from ui.views.paint.text_preferences import capture_defaults
        document = self.current_document()
        if document:self.tools.controls.text_defaults = capture_defaults(document.canvas)
        self.tools.controls.flush_preferences()
        self.navigation.pause()
        self._thumbnail_timer.stop()
        for document in self.documents:
            document.canvas.release()
        self.io.shutdown()
        self._cleanup_paths.update(self._draft_paths)
        for document in self.documents:
            if (
                not document.file_path
                or (document.working and document.source != document.file_path)
            ) and not document.busy:
                self._cleanup_paths.add(document.source)
        # Viewport batches are bounded and check cancellation between tiles.
        # Close read handles before deleting drafts, particularly on Windows.
        finished = self.io.wait_for_done(2000)
        self._cleanup_drafts(force=finished)
        if finished:self.finish_shutdown()

    def finish_shutdown(self):
        if self._resources_closed:return
        self.io.finish_shutdown()
        self._cleanup_drafts(force=True)
        for path in self._runtime_paths:close_session(path)
        self._runtime_paths.clear()
        self.pixels.close()
        self._resources_closed = True

    def _cleanup_drafts(self, force=False):
        if self.io._jobs and not force:
            return
        for path in list(self._cleanup_paths):
            try:
                close_session(path)
                self._runtime_paths.discard(path)
                Path(path).unlink(missing_ok=True)
            except OSError:
                continue
            self._cleanup_paths.discard(path)
            self._draft_paths.discard(path)
