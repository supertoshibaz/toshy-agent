"""Optional LRU document unloading. Tab labels survive; Qt documents do not."""
import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot, QTimer, QSaveFile, QIODevice
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QWidget, QLabel, QPlainTextEdit

from core.paths import APP_PATHS
from core.logger import core_logger
from ui.qt_deferred import defer


class PerformancePreferences(QObject):
    changed = Signal()

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = Path(path or APP_PATHS.runtime / "app_performance.json")
        self.values = {kind: {"enabled": False, "limit": 1}
                       for kind in ("text", "board", "pipeline", "paint")}
        self.values["unsaved"] = "hold"
        self.values['sessions'] = {'keep_loaded': True, 'page_size': 10, 'paged': True}
        self.values['markdown'] = {'auto_pairs': True}
        self.values['board_cards'] = {'width': 10, 'height': 8, 'raise_selected': True}
        self.values['board_rendering'] = {'enabled': True, 'threshold': 40}
        self.values['terminal'] = {'lines': 10000}
        from ui.workspace_settings import load_workspace_options
        self.values['workspaces'] = load_workspace_options(self.path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for kind in ("text", "board", "pipeline", "paint"):
                value = data.get(kind, {})
                self.values[kind] = {
                    "enabled": value.get("enabled") is True,
                    "limit": max(1, int(value.get("limit", 1))),
                }
            self.values["unsaved"] = "save" if data.get("unsaved") == "save" else "hold"
            options = data.get('sessions') or {}
            self.values['sessions'] = {'keep_loaded': options.get('keep_loaded', True) is not False,
                'paged': options.get('paged', True) is not False,
                'page_size': max(1, min(500, int(options.get('page_size', 10))))}
            self.values['markdown']['auto_pairs'] = (data.get('markdown') or {}).get('auto_pairs', True) is not False
            for key, default in (('width', 10), ('height', 8)):
                self.values['board_cards'][key] = max(2, min(200, int((data.get('board_cards') or {}).get(key, default))))
            self.values['board_cards']['raise_selected'] = (data.get('board_cards') or {}).get('raise_selected', True) is not False
            options = data.get('board_rendering') or {}
            self.values['board_rendering'] = {
                'enabled': options.get('enabled', True) is not False,
                'threshold': max(1, min(100, int(options.get('threshold', 40))))}
            self.values['terminal']['lines'] = max(100, min(200000,
                int((data.get('terminal') or {}).get('lines', 10000))))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def update(self, kind, key, value):
        data = json.loads(json.dumps(self.values))
        if kind == "unsaved":
            data["unsaved"] = "save" if value == "save" else "hold"
        elif kind == 'sessions':
            data[kind][key] = max(1, min(500, int(value))) if key == 'page_size' else bool(value)
        elif kind == 'markdown':
            data[kind][key] = bool(value)
        elif kind == 'board_cards':
            data[kind][key] = bool(value) if key == 'raise_selected' else max(2, min(200, int(value)))
        elif kind == 'board_rendering':
            data[kind][key] = bool(value) if key == 'enabled' else max(1, min(100, int(value)))
        elif kind == 'terminal':
            data[kind][key] = max(100, min(200000, int(value)))
        elif kind == 'workspaces':
            if key not in ('board', 'paint'):
                raise ValueError('Неизвестное рабочее пространство: ' + str(key))
            data[kind][key] = bool(value)
        else:
            data[kind][key] = bool(value) if key == "enabled" else max(1, int(value))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = QSaveFile(str(self.path))
        if not target.open(QIODevice.WriteOnly):
            raise OSError(target.errorString())
        if target.write(json.dumps(data, indent=2).encode("utf-8")) < 0 or not target.commit():
            raise OSError(target.errorString())
        self.values = data
        self.changed.emit()


performance_preferences = PerformancePreferences()


class SuspendedTab(QWidget):
    """A small path/state holder, with no editor, scene, source text or undo stack."""
    def __init__(self, document, kind, state, parent):
        super().__init__(parent)
        self.kind, self.saved_state = kind, state
        self.is_dirty = False
        self._load_ok = True
        self.canvas = None
        self.path_label = QLabel(self)
        self.path_label.hide()
        self._auto_save_timer = QTimer(self)
        if kind == "pipeline":
            self.pipeline_dir = document.pipeline_dir
            self.script_path = document.script_path
            self.layout_path = document.layout_path
            self._loaded_name = document.pipeline_name
        else:
            self.file_path = document.file_path

    @property
    def pipeline_name(self):
        return self._loaded_name

    def update_file_path(self, path):
        self.file_path = str(path)

    def refresh_editor_layout(self):
        pass

    def cancel_agent_prompt(self):
        pass

    def save_file(self):
        return True

    save_board = save_file
    save_pipeline = save_file


class TabMemoryManager(QObject):
    def __init__(self, owner, kind, stack, documents, factory, bind, preferences=None):
        super().__init__(owner)
        self.owner, self.kind, self.stack = owner, kind, stack
        self.documents, self.factory, self.bind = documents, factory, bind
        self.preferences = preferences or performance_preferences
        self._order = {}
        self._clock = 0
        self._replacing = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.prune)
        self.preferences.changed.connect(self.schedule)

    @Slot()
    @Slot(bool)
    def schedule(self, *_):
        self._timer.start(0)

    def touch(self, document):
        if document in self.documents.values():
            self._clock += 1
            self._order[document] = self._clock
            self.schedule()

    def bind_document(self, document):
        self.bind(document)
        document.dirty_state_changed.connect(self.schedule)

    def forget(self, document):
        self._order.pop(document, None)

    def ensure_loaded(self, document):
        if not isinstance(document, SuspendedTab):
            return document
        path = document.pipeline_dir if self.kind == "pipeline" else document.file_path
        existing = self.documents.get(str(path))
        if existing is not None and existing is not document:
            return existing
        loaded = self.factory(path, self.owner)
        self.bind_document(loaded)
        self._restore(loaded, document.saved_state)
        self._replace(document, loaded)
        core_logger.event('resources', 'TAB_RESTORE', f'{self.kind}: {path}')
        return loaded

    def _replace(self, old, new):
        self._replacing = True
        try:
            for key, value in list(self.documents.items()):
                if value is old:
                    self.documents[key] = new
            index = self.stack.indexOf(old)
            current = self.stack.currentWidget() is old
            self.stack.insertWidget(max(0, index), new)
            for i in range(self.owner.tab_bar.count()):
                if self.owner.tab_bar.tabData(i) is old:
                    self.owner.tab_bar.setTabData(i, new)
                    break
            self.stack.removeWidget(old)
            if current:
                self.stack.setCurrentWidget(new)
            self._order[new] = self._order.pop(old, 0)
            old.deleteLater()
        finally:
            self._replacing = False

    def prune(self):
        option = self.preferences.values[self.kind]
        if self._replacing or not option["enabled"]:
            return
        live = [d for d in self.documents.values() if not isinstance(d, SuspendedTab)]
        live.sort(key=lambda d: self._order.get(d, 0))
        for document in live:
            if sum(not isinstance(d, SuspendedTab) for d in self.documents.values()) <= option["limit"]:
                break
            if document is self.stack.currentWidget() or self._busy(document):
                continue
            if document.is_dirty:
                if self.preferences.values["unsaved"] != "save":
                    continue
                # A save may emit signals, but eviction stays deferred to this pass.
                if not getattr(document, "save_" + ("file" if self.kind == "text" else self.kind))():
                    continue
            state = self._snapshot(document)
            suspended = SuspendedTab(document, self.kind, state, self.owner)
            if self.kind == "text":
                from elements.lsp import lsp_manager
                lsp_manager.notify_close(document.file_path)
                document.cancel_agent_prompt()
            for timer in document.findChildren(QTimer):
                timer.stop()
            self._replace(document, suspended)
            core_logger.event('resources', 'TAB_UNLOAD', f'{self.kind}: {getattr(suspended, "file_path", None) or getattr(suspended, "pipeline_dir", "")}')

    @staticmethod
    def _busy(document):
        if not getattr(document, "_load_ok", True) or getattr(document, "_external_conflict", False):
            return True
        path = getattr(document, "script_path", None) or getattr(document, "file_path", None)
        if path is not None and not Path(path).is_file():
            return True
        canvas = getattr(document, "canvas", None)
        if canvas is not None:
            if not getattr(canvas, "_load_ok", True):
                return True
            if any(getattr(canvas, name, None) for name in (
                "_connecting_card", "_endpoint_grab", "_is_panning", "_pipeline_source_node",
            )):
                return True
            if any(getattr(item, "_is_editing", False) or getattr(item, "_resizing", False)
                   for item in canvas.canvas_scene.selectedItems()):
                return True
        return False

    def _snapshot(self, document):
        if self.kind == "text":
            editor = document.editor
            cursor = editor.textCursor()
            return {"position": cursor.position(), "anchor": cursor.anchor(),
                    "vertical": editor.verticalScrollBar().value(),
                    "horizontal": editor.horizontalScrollBar().value(),
                    "font": editor.font().toString(), "wrap": editor.lineWrapMode().value,
                    "mode": document.editor_mode, "lsp": document.lsp_enabled,
                    "markdown_edit_mode": document._last_editable_markdown_mode,
                    "markdown_preview_row": (document._read_only_view.minimap_viewport_rows()[0]
                        if document.editor_mode == 'read_only' else None)}
        canvas = document.canvas
        center = canvas.mapToScene(canvas.viewport().rect().center())
        state = {"camera": canvas._pending_camera or ([center.x(), center.y()], canvas._current_zoom),
                 "snap": canvas.snap_to_grid, "align": canvas.smart_alignment,
                 "read_only": canvas.read_only,
                 "selected": [str(getattr(item, "node_id", ""))
                              for item in canvas.canvas_scene.selectedItems()]}
        if self.kind == "pipeline":
            state["splitter"] = document.splitter.sizes()
        return state

    def _restore(self, document, state):
        if self.kind == "text":
            editor = document.editor
            font = QFont()
            font.fromString(state["font"])
            editor.setFont(font)
            document._toggle_word_wrap(state["wrap"] != QPlainTextEdit.NoWrap.value)
            document._set_markdown_mode(state["mode"])
            document._last_editable_markdown_mode = state.get(
                'markdown_edit_mode', document._last_editable_markdown_mode)
            if document.lsp_enabled != state["lsp"]:
                document._toggle_lsp(state["lsp"])
            cursor = QTextCursor(editor.document())
            end = editor.document().characterCount() - 1
            cursor.setPosition(min(state["anchor"], end))
            cursor.setPosition(min(state["position"], end), QTextCursor.KeepAnchor)
            editor.setTextCursor(cursor)
            # Layout needs the final visible geometry before restoring scroll.
            def restore_scroll():
                document.refresh_editor_layout()
                editor.verticalScrollBar().setValue(state["vertical"])
                editor.horizontalScrollBar().setValue(state["horizontal"])
                row = state.get('markdown_preview_row')
                if document.editor_mode == 'read_only' and row is not None:
                    document._read_only_view.minimap_scroll_to_row(row, center=False)
            defer(0, document, restore_scroll)
        else:
            canvas = document.canvas
            canvas.set_snap_to_grid(state["snap"])
            canvas.smart_alignment = canvas.canvas_scene.smart_alignment = state["align"]
            canvas.set_read_only(state["read_only"])
            canvas._pending_camera = state["camera"]
            canvas._camera_restore_timer.start()
            selected = set(state["selected"])
            for item in canvas.canvas_scene.items():
                if str(getattr(item, "node_id", "")) in selected:
                    item.setSelected(True)
            if self.kind == "pipeline":
                document.splitter.setSizes(state["splitter"])
