from __future__ import annotations
from ui.themes.manager import theme_manager
from ..presentation import node_caption, metadata_caption
from ui.i18n import i18n
from ui.i18n import bind_caption, tr, set_text
from ui.styles.binding import apply_style


import copy
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QSize, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsProxyWidget,
    QPlainTextEdit,
)

from ui.views.canvas.canvas_card import GRID_SIZE
from modules.pipelines.control import normalize_argument_definitions

from .specs import (
    STREAM_EXEC,
    STREAM_ARG,
    STREAM_PROMPT,
    PipelineNodeSpec,
    PipelinePortSpec,
    array_arg_in,
    array_arg_out,
    arg_in,
    exec_port,
    prompt_in,
    prompt_arg_in,
    stream_color,
)


NODE_WIDTH = float(GRID_SIZE * 10)
HEADER_HEIGHT = float(GRID_SIZE)
CONTENT_ROW_HEIGHT = float(GRID_SIZE)
BOTTOM_BLANK_HEIGHT = float(GRID_SIZE)
PORT_RADIUS = 6.0
PORT_HIT_RADIUS = 12.0
PORT_ROW_HEIGHT = float(GRID_SIZE)
DEFAULT_CONTENT_ROWS = 3
CONTENT_RESIZE_HANDLE_W = 18.0
CONTENT_RESIZE_HANDLE_H = 12.0
_NO_INPUT_VALUE = object()


class _InlineContentEditor(QPlainTextEdit):
    """Small editor embedded directly into a selected pipeline card."""

    commit_requested = Signal()
    cancel_requested = Signal()

    def minimumSizeHint(self):
        # Card geometry owns this editor, including compact single-row fields.
        # QTextEdit's normal minimum is intended for stand-alone form controls.
        return QSize(1, 1)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.commit_requested.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancel_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


def coerce_binary_input_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value in {"true", "True", "false", "False"}:
        return value.casefold() == "true"
    return None


def _blend_colors(base: QColor, accent: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, float(ratio)))
    inv = 1.0 - ratio
    return QColor(
        int(base.red() * inv + accent.red() * ratio),
        int(base.green() * inv + accent.green() * ratio),
        int(base.blue() * inv + accent.blue() * ratio),
        int(base.alpha() * inv + accent.alpha() * ratio),
    )


def _initial_node_settings(spec: PipelineNodeSpec) -> Dict[str, Any]:
    defaults = spec.metadata.get("default_settings")
    if isinstance(defaults, dict):
        return copy.deepcopy(defaults)

    value_type = str(spec.metadata.get("value_type") or "")
    if value_type == "prompt":
        return {"text": str(spec.metadata.get("default_value") or "")}
    if value_type == "array":
        return {
            "value": [],
            "literal_text": "",
            "item_type": str(spec.metadata.get("default_item_type") or "string"),
        }
    if spec.kind == "content":
        return {"value": spec.metadata.get("default_value")}
    return {}


def initial_node_settings(spec):
    settings = _initial_node_settings(spec)
    if any(p.stream == STREAM_EXEC for p in spec.inputs) and spec.type_id != 'graph.shutdown':
        settings.setdefault('_error_policy', {'mode': 'stop', 'retries': 1, 'delay': 0, 'exhausted': 'stop'})
    return settings


class PipelineNodeItem(QGraphicsObject):
    def __init__(
        self,
        spec: PipelineNodeSpec,
        x: float,
        y: float,
        settings: Optional[Dict[str, Any]] = None,
        node_id: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.node_id = str(node_id or uuid.uuid4().hex[:16])
        self.spec = spec
        i18n.language_changed.connect(self._retranslate_chrome)
        self.settings: Dict[str, Any] = dict(settings or {})
        self.edges: List[Any] = []
        self.input_ports: List[PipelinePortSpec] = list(spec.inputs)
        self.output_ports: List[PipelinePortSpec] = list(spec.outputs)

        # Card-content scrolling is presentation-only state.  It is deliberately
        # not serialized into pipeline.py/layout.json.
        self._preview_cache = None
        self._parallel_chain = False
        self._parallel_owner = None
        self._parallel_header_hover = False
        self._parallel_delete_pressed = False
        self._content_scroll_y = 0.0
        self._content_scroll_max = 0.0
        self._content_document_height = 0.0
        self._content_scroll_drag_offset = None
        self._content_rows = DEFAULT_CONTENT_ROWS
        self._content_resize_start_scene_y = None
        self._content_resize_start_rows = None
        self._inline_editor_proxy = None
        self._inline_editor = None
        self._inline_editor_original_text = ""
        self._inline_edit_closing = False

        prompt_count = int(self.settings.pop("_prompt_input_count", 1) or 1)
        if self.is_executable:
            self.set_prompt_input_count(max(1, prompt_count), geometry=False)
            self.sync_fixed_argument_ports(geometry=False)

        prompt_arg_count = int(self.settings.pop("_prompt_arg_input_count", 1) or 1)
        if self.value_type == "prompt":
            self.set_prompt_arg_input_count(max(1, prompt_arg_count), geometry=False)

        array_count = int(self.settings.pop("_array_input_count", 1) or 1)
        if self.is_array:
            self.set_array_input_count(max(1, array_count), geometry=False)
            self.set_array_item_type(
                str(self.settings.get("item_type") or "string"),
                geometry=False,
            )

        if self.is_graph_router:
            try:
                route_count = max(1, int(self.settings.get("route_count", 1) or 1))
            except (TypeError, ValueError):
                route_count = 1
            self.set_router_output_count(route_count, geometry=False)

        self.sync_content_input_port(geometry=False)
        self.sync_loop_argument_port(geometry=False)
        self.sync_router_argument_port(geometry=False)
        self.sync_python_code_ports(geometry=False)
        self.sync_human_in_loop_ports(geometry=False)
        self.sync_pipeline_interface_ports(geometry=False)

        self.sync_execution_ports(geometry=False)
        self.setPos(x, y)
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)

    @property
    def is_executable(self) -> bool:
        return self.spec.kind in {"module", "tool"}

    @property
    def value_type(self) -> str:
        return str(self.spec.metadata.get("value_type") or "")

    @property
    def is_array(self) -> bool:
        return str(self.spec.metadata.get("value_type") or "") == "array"

    @property
    def is_graph_loop(self) -> bool:
        return str(self.spec.metadata.get("graph_role") or "") == "loop"

    @property
    def is_graph_start(self) -> bool:
        return str(self.spec.metadata.get("graph_role") or "") == "start"

    @property
    def is_graph_router(self) -> bool:
        return str(self.spec.metadata.get("graph_role") or "") == "router"

    @property
    def is_python_code(self) -> bool:
        return str(self.spec.metadata.get("inspector_type") or "") == "python_code"

    @property
    def is_human_in_loop(self) -> bool:
        return str(self.spec.metadata.get("graph_role") or "") == "human_in_loop"

    @property
    def is_protected(self) -> bool:
        return bool(self.spec.metadata.get("protected"))

    @property
    def is_single_content_node(self) -> bool:
        return self.spec.kind == "content" and not self.is_array

    def local_content_is_empty(self) -> bool:
        if not self.is_single_content_node:
            return False
        if self.value_type == "prompt":
            return not str(self.settings.get("text") or "").strip()
        if self.value_type == "string":
            return str(self.settings.get("value") or "") == ""
        return self.settings.get("value") is None

    def content_input_connected(self) -> bool:
        if not self.is_single_content_node:
            return False
        valid_keys = {port.key for port in self.input_ports}
        return any(
            edge.target is self and edge.target_port in valid_keys
            for edge in self.edges
        )

    def sync_content_input_port(self, *, geometry: bool = True):
        """Keep Prompt's Arg input permanent; other scalar inputs remain exclusive."""
        if not self.is_single_content_node:
            return

        if self.value_type == "prompt":
            # Prompt owns a dynamic family of always-visible Arg inputs. Do not
            # rebuild them from the immutable one-slot spec here.
            if not self.input_ports:
                self.set_prompt_arg_input_count(1, geometry=geometry)
            return

        should_show = self.local_content_is_empty() or self.content_input_connected()
        rebuilt = list(self.spec.inputs) if should_show else []
        if rebuilt == self.input_ports:
            return

        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def resolved_content_value(self, incoming_value: Any = _NO_INPUT_VALUE):
        """Resolve this editor node's value without executing the graph.

        Runtime compilation is still separate, but keeping the coercion here makes
        the intended semantics explicit for the compiler.
        """
        if self.value_type == "prompt":
            local = str(self.settings.get("text") or "")
            incoming_values = []
            if incoming_value is not _NO_INPUT_VALUE:
                if isinstance(incoming_value, dict):
                    # A runtime/compiler may pass values keyed by stable input port.
                    for port in self.input_ports:
                        if not self._is_prompt_arg_port_key(port.key):
                            continue
                        value = incoming_value.get(port.key, _NO_INPUT_VALUE)
                        if value is not _NO_INPUT_VALUE and value is not None:
                            incoming_values.append(value)
                elif isinstance(incoming_value, (list, tuple)):
                    incoming_values.extend(
                        value for value in incoming_value if value is not None
                    )
                elif incoming_value is not None:
                    incoming_values.append(incoming_value)

            parts = []
            if local:
                parts.append(local)
            parts.extend(str(value) for value in incoming_values if str(value))
            return "\n".join(parts) if parts else None
        elif self.value_type == "binary":
            local = self.settings.get("value")
            if local is not None:
                return bool(local)
            if incoming_value is _NO_INPUT_VALUE:
                return None
            return coerce_binary_input_value(incoming_value)
        elif self.is_single_content_node:
            local = self.settings.get("value")
            if not self.local_content_is_empty():
                return local

        if incoming_value is _NO_INPUT_VALUE:
            return None
        return incoming_value

    def accent_color(self) -> Optional[QColor]:
        raw = str(self.spec.metadata.get("color") or "").strip()
        if not raw:
            return None
        color = QColor(raw)
        return color if color.isValid() else None

    def header_height(self) -> float:
        return HEADER_HEIGHT

    def content_row_top(self) -> float:
        return HEADER_HEIGHT

    def supports_resizable_content_field(self) -> bool:
        if self.spec.kind == "content" and self.value_type in {"prompt", "string", "array"}:
            return True
        if self.is_python_code:
            return True
        return (
            self.spec.kind == "graph_control"
            and str(self.spec.metadata.get("inspector_type") or "") == "graph_message"
        )

    def content_row_count(self) -> int:
        if self.supports_resizable_content_field():
            return max(DEFAULT_CONTENT_ROWS, int(self._content_rows or DEFAULT_CONTENT_ROWS))
        return 1

    def set_content_row_count(self, rows: int, *, geometry: bool = True):
        if not self.supports_resizable_content_field():
            return
        rows = max(DEFAULT_CONTENT_ROWS, int(rows))
        if rows == self.content_row_count():
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self._content_rows = rows
        self._content_scroll_y = max(0.0, min(self._content_scroll_y, self._content_scroll_max))
        if geometry:
            self._update_inline_editor_geometry()
            self.update()
            for edge in list(self.edges):
                edge.update_path()
            parent = self.parentItem()
            if parent is not None and hasattr(parent, 'arrange'): parent.arrange()

    def content_height(self) -> float:
        return float(self.content_row_count()) * CONTENT_ROW_HEIGHT

    def uses_scrollable_content_field(self) -> bool:
        return self.supports_resizable_content_field()

    def content_field_rect(self) -> QRectF:
        # The field height is always a whole number of grid rows. Insets are
        # visual only, so resizing stays aligned to the canvas grid.
        return QRectF(
            7.0,
            HEADER_HEIGHT + 5.0,
            self.card_width() - 14.0,
            self.content_height() - 10.0,
        )

    def _content_resize_handle_rect(self) -> QRectF:
        field = self.content_field_rect()
        return QRectF(
            field.center().x() - CONTENT_RESIZE_HANDLE_W / 2.0,
            field.bottom() + 2.0,
            CONTENT_RESIZE_HANDLE_W,
            CONTENT_RESIZE_HANDLE_H,
        )

    def _content_scrollbar_track_rect(self) -> QRectF:
        field = self.content_field_rect()
        return QRectF(field.right() - 6.0, field.top() + 6.0, 3.0, max(0.0, field.height() - 12.0))

    def _content_scrollbar_thumb_rect(self) -> QRectF:
        track = self._content_scrollbar_track_rect()
        if self._content_scroll_max <= 0.0 or self._content_document_height <= 0.0:
            return QRectF()
        viewport_height = max(1.0, self.content_field_rect().height() - 12.0)
        ratio = min(1.0, viewport_height / max(viewport_height, self._content_document_height))
        thumb_height = max(12.0, track.height() * ratio)
        travel = max(0.0, track.height() - thumb_height)
        progress = self._content_scroll_y / self._content_scroll_max if self._content_scroll_max > 0.0 else 0.0
        return QRectF(
            track.x(),
            track.y() + travel * max(0.0, min(1.0, progress)),
            track.width(),
            thumb_height,
        )

    def supports_inline_content_edit(self) -> bool:
        if self.spec.kind == "content":
            if self.value_type == "prompt":
                return True
            if self.value_type == "string":
                return not self.content_input_connected()
            if self.value_type == "array":
                return str(self.settings.get("item_type") or "string") != "binary"
        if self.is_python_code:
            return True
        return (
            self.spec.kind == "graph_control"
            and str(self.spec.metadata.get("inspector_type") or "") == "graph_message"
        )

    def _inline_content_text(self) -> str:
        if self.value_type == "prompt":
            return str(self.settings.get("text") or "")
        if self.value_type == "string":
            return str(self.settings.get("value") or "")
        if self.value_type == "array":
            return str(self.settings.get("literal_text") or "")
        if self.is_python_code:
            return str(self.settings.get("code") or "")
        if (
            self.spec.kind == "graph_control"
            and str(self.spec.metadata.get("inspector_type") or "") == "graph_message"
        ):
            return str(self.settings.get("message") or "")
        return ""

    @staticmethod
    def _parse_inline_array_literal(text: str, item_type: str) -> List[Any]:
        values: List[Any] = []
        for raw in str(text or "").split(","):
            token = raw.strip()
            if not token:
                continue
            if item_type == "string":
                values.append(token)
            elif item_type == "integer":
                clean = "".join(char for char in token if char.isdigit())
                if clean:
                    values.append(int(clean, 10))
            elif item_type == "float":
                clean_chars = []
                dot_seen = False
                for char in token:
                    if char.isdigit():
                        clean_chars.append(char)
                    elif char == "." and not dot_seen:
                        clean_chars.append(char)
                        dot_seen = True
                clean = "".join(clean_chars)
                if clean and clean != ".":
                    try:
                        value = float(("0" + clean) if clean.startswith(".") else clean)
                    except ValueError:
                        continue
                    if math.isfinite(value):
                        values.append(value)
        return values

    def _set_inline_content_text(self, text: str):
        text = str(text or "")
        if self.value_type == "prompt":
            self.settings["text"] = text
        elif self.value_type == "string":
            self.settings["value"] = text
        elif self.value_type == "array":
            item_type = str(self.settings.get("item_type") or "string")
            if item_type == "binary":
                return
            self.settings["literal_text"] = text
            self.settings["value"] = self._parse_inline_array_literal(text, item_type)
        elif self.is_python_code:
            self.settings["code"] = text
        elif (
            self.spec.kind == "graph_control"
            and str(self.spec.metadata.get("inspector_type") or "") == "graph_message"
        ):
            self.settings["message"] = text
        else:
            return

        self.sync_content_input_port()
        self.update()
        for edge in list(self.edges):
            if edge.source is self:
                try:
                    edge.target.update()
                except RuntimeError:
                    pass

    def _update_inline_editor_geometry(self):
        proxy = self._inline_editor_proxy
        if proxy is None:
            return
        # Keep the live editor strictly inside the painted preview frame.
        # The previous geometry covered the frame itself, so selecting a card
        # made the border appear to disappear underneath the editor widget.
        field = self.content_field_rect().adjusted(2.0, 2.0, -2.0, -2.0)
        editor = self._inline_editor
        if editor is not None:
            editor.ensurePolished()
            editor.setMinimumSize(0, 0)
            editor.setMaximumSize(max(1, int(field.width())), max(1, int(field.height())))
        proxy.setPos(field.topLeft())
        proxy.resize(field.size())

    def _start_inline_content_edit(self):
        if not self.supports_inline_content_edit() or self._inline_editor_proxy is not None:
            return

        field = self.content_field_rect()
        editor = _InlineContentEditor()
        editor.setObjectName("PipelineInlineCardEditor")
        editor.setFrameShape(QFrame.NoFrame)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        apply_style(editor, "QPlainTextEdit#PipelineInlineCardEditor {"
            " background: @background.window@; color: @text.primary@; border: none; border-radius: 4px;"
            " min-width: 0px; min-height: 0px; padding: 4px 6px; selection-background-color: @control.selected@; }")
        original = self._inline_content_text()
        editor.setPlainText(original)

        proxy = QGraphicsProxyWidget(self)
        proxy.setWidget(editor)
        proxy.setZValue(50.0)
        self._inline_editor_proxy = proxy
        self._inline_editor = editor
        self._inline_editor_original_text = original
        self._update_inline_editor_geometry()

        editor.textChanged.connect(self._inline_editor_text_changed)
        editor.commit_requested.connect(lambda: self._finish_inline_content_edit(cancel=False))
        editor.cancel_requested.connect(lambda: self._finish_inline_content_edit(cancel=True))
        editor.setFocus(Qt.MouseFocusReason)
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.End)
        editor.setTextCursor(cursor)

    def _inline_editor_text_changed(self):
        editor = self._inline_editor
        if editor is None or self._inline_edit_closing:
            return
        self._set_inline_content_text(editor.toPlainText())
        view = self._pipeline_view()
        if view is not None:
            try:
                view.modified.emit()
            except RuntimeError:
                pass

    def _finish_inline_content_edit(self, *, cancel: bool):
        if self._inline_editor_proxy is None or self._inline_edit_closing:
            return
        self._inline_edit_closing = True
        try:
            if cancel:
                self._set_inline_content_text(self._inline_editor_original_text)
            else:
                editor = self._inline_editor
                if editor is not None:
                    self._set_inline_content_text(editor.toPlainText())

            proxy = self._inline_editor_proxy
            editor = self._inline_editor
            self._inline_editor_proxy = None
            self._inline_editor = None
            if proxy is not None:
                proxy.setVisible(False)
                proxy.setParentItem(None)
                if proxy.scene() is not None:
                    proxy.scene().removeItem(proxy)
                proxy.deleteLater()
            elif editor is not None:
                editor.deleteLater()

            view = self._pipeline_view()
            if view is not None:
                try:
                    view.modified.emit()
                    view.node_selection_changed.emit(self)
                except RuntimeError:
                    pass
            self.update()
        finally:
            self._inline_edit_closing = False

    def _static_arg_preview_value(self, visited=None):
        visited = set(visited or ())
        if self.node_id in visited:
            return None
        visited.add(self.node_id)

        if self.spec.kind != "content":
            return None
        if self.is_array:
            value = self.settings.get("value")
            return value if isinstance(value, list) else None
        if self.value_type == "prompt":
            return None
        if not self.local_content_is_empty():
            return self.settings.get("value")

        for edge in self.edges:
            if edge.target is self and edge.target_port == "arg:in":
                return edge.source._static_arg_preview_value(visited)
        return None

    @staticmethod
    def _is_prompt_arg_port_key(port_key: str) -> bool:
        key = str(port_key or "")
        return key == "arg:in" or key.startswith("arg:in:")

    @staticmethod
    def _prompt_arg_port_index(port_key: str) -> int:
        key = str(port_key or "")
        if key == "arg:in":
            return 1
        if key.startswith("arg:in:"):
            try:
                return max(1, int(key.rsplit(":", 1)[1]))
            except (TypeError, ValueError):
                pass
        return 0

    def prompt_arg_input_count(self) -> int:
        if self.value_type != "prompt":
            return 0
        return sum(
            1 for port in self.input_ports if self._is_prompt_arg_port_key(port.key)
        )

    def set_prompt_arg_input_count(self, count: int, *, geometry: bool = True):
        if self.value_type != "prompt":
            return
        count = max(1, int(count))
        rebuilt = [prompt_arg_in(index) for index in range(1, count + 1)]
        if rebuilt == self.input_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def normalize_prompt_arg_ports(self):
        if self.value_type != "prompt":
            return
        highest_connected = 0
        for edge in self.edges:
            if edge.target is not self or not self._is_prompt_arg_port_key(edge.target_port):
                continue
            highest_connected = max(
                highest_connected,
                self._prompt_arg_port_index(edge.target_port),
            )
        # As soon as the last visible slot is occupied, expose the next one.
        self.set_prompt_arg_input_count(max(1, highest_connected + 1))

    def prompt_arg_preview_text(self) -> str:
        if self.value_type != "prompt":
            return ""
        values = []
        for port in self.input_ports:
            if not self._is_prompt_arg_port_key(port.key):
                continue
            for edge in self.edges:
                if edge.target is not self or edge.target_port != port.key:
                    continue
                value = edge.source._static_arg_preview_value({self.node_id})
                if value is None:
                    continue
                if isinstance(value, list):
                    values.append(", ".join(str(item) for item in value))
                else:
                    values.append(str(value))
                break
        return "\n".join(value for value in values if value)

    def _raise_above_pipeline_nodes(self):
        if not self.scene():
            return
        # Keep the z range bounded so temporary connection lines (z=20) remain
        # above cards even after thousands of selections. The most recently
        # selected/clicked card is the only one that needs to sit above peers.
        for item in self.scene().items():
            if isinstance(item, PipelineNodeItem) and item is not self:
                item.setZValue(2.0 if isinstance(item.parentItem(), PipelineNodeItem) else 0.0)
        self.setZValue(10.0)
        if isinstance(self.parentItem(), PipelineNodeItem):
            self.parentItem().setZValue(10.0)

    def first_port_center_y(self) -> float:
        # Every visual band is aligned to the canvas grid: one header row, one
        # or three content rows, then one row per connector slot.
        return HEADER_HEIGHT + self.content_height() + PORT_ROW_HEIGHT / 2.0

    def _ports(self, direction: str) -> List[PipelinePortSpec]:
        return self.input_ports if direction == "input" else self.output_ports

    def port_spec(
        self,
        direction: str,
        port_key: str,
    ) -> Optional[PipelinePortSpec]:
        for port in self._ports(direction):
            if port.key == port_key:
                return port
        return None

    def _fixed_argument_port_keys(self) -> set[str]:
        mapping = self.spec.metadata.get("fixed_args_from_settings")
        if not isinstance(mapping, dict):
            return set()

        hidden: set[str] = set()
        for arg_name, settings_key in mapping.items():
            value = self.settings.get(str(settings_key))
            is_fixed = value is not None
            if isinstance(value, (str, list, tuple, dict, set)):
                is_fixed = bool(value)
            if is_fixed:
                hidden.add(f"arg:{arg_name}")
        return hidden

    def sync_fixed_argument_ports(self, *, geometry: bool = True):
        """Hide model/Arg inputs whose value is explicitly fixed by node settings."""
        if not self.is_executable:
            return
        mapping = self.spec.metadata.get("fixed_args_from_settings")
        if not isinstance(mapping, dict) or not mapping:
            return

        hidden = self._fixed_argument_port_keys()
        prompt_count = max(1, self.prompt_input_count())
        exec_ports = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        other_ports = [
            port
            for port in self.spec.inputs
            if port.stream not in {STREAM_EXEC, STREAM_PROMPT}
            and port.key not in hidden
        ]
        rebuilt = [
            *exec_ports,
            *[prompt_in(index) for index in range(1, prompt_count + 1)],
            *other_ports,
        ]
        if rebuilt == self.input_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def prompt_input_count(self) -> int:
        return sum(
            1
            for port in self.input_ports
            if port.stream == STREAM_PROMPT and port.key.startswith("prompt:")
        )

    def set_prompt_input_count(self, count: int, *, geometry: bool = True):
        if not self.is_executable:
            return

        count = max(1, int(count))
        hidden = self._fixed_argument_port_keys()
        # Rebuild from the immutable spec so an argument hidden by settings can
        # be restored later when that setting is cleared.
        exec_ports = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        other_ports = [
            port
            for port in self.spec.inputs
            if port.stream not in {STREAM_EXEC, STREAM_PROMPT}
            and port.key not in hidden
        ]
        rebuilt = [
            *exec_ports,
            *[prompt_in(index) for index in range(1, count + 1)],
            *other_ports,
        ]

        if rebuilt == self.input_ports:
            return

        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def normalize_prompt_ports(self):
        if not self.is_executable:
            return

        highest_connected = 0
        for edge in self.edges:
            if edge.target is not self:
                continue
            if not edge.target_port.startswith("prompt:"):
                continue
            try:
                highest_connected = max(
                    highest_connected,
                    int(edge.target_port.split(":", 1)[1]),
                )
            except (TypeError, ValueError):
                continue

        # Always retain exactly one free prompt port after the highest used one.
        self.set_prompt_input_count(max(1, highest_connected + 1))

    def array_input_count(self) -> int:
        return sum(1 for port in self.input_ports if port.key.startswith("array:"))

    def set_array_input_count(self, count: int, *, geometry: bool = True):
        if not self.is_array:
            return

        count = max(1, int(count))
        item_type = str(self.settings.get("item_type") or "string")
        rebuilt = [array_arg_in(index, item_type) for index in range(1, count + 1)]

        if rebuilt == self.input_ports:
            return

        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def set_array_item_type(self, item_type: str, *, geometry: bool = True):
        if not self.is_array:
            return

        allowed = tuple(self.spec.metadata.get("item_types") or ())
        item_type = str(item_type or "string").casefold()
        if allowed and item_type not in allowed:
            item_type = str(self.spec.metadata.get("default_item_type") or "string")

        self.settings["item_type"] = item_type
        count = max(1, self.array_input_count())

        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = [array_arg_in(index, item_type) for index in range(1, count + 1)]
        self.output_ports = [array_arg_out(item_type)]
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def normalize_array_ports(self):
        if not self.is_array:
            return

        highest_connected = 0
        for edge in self.edges:
            if edge.target is not self:
                continue
            if not edge.target_port.startswith("array:"):
                continue
            try:
                highest_connected = max(
                    highest_connected,
                    int(edge.target_port.split(":", 1)[1]),
                )
            except (TypeError, ValueError):
                continue

        # Same rule as Prompt inputs: one empty input is always available after
        # the highest connected array item.
        self.set_array_input_count(max(1, highest_connected + 1))

    @staticmethod
    def _router_output_port_index(port_key: str) -> int:
        key = str(port_key or "")
        if not key.startswith("exec:route:"):
            return -1
        try:
            return max(0, int(key.rsplit(":", 1)[1]))
        except (TypeError, ValueError):
            return -1

    def router_output_count(self) -> int:
        if not self.is_graph_router:
            return 0
        return sum(1 for port in self.output_ports if port.key.startswith("exec:route:"))

    def set_router_output_count(self, count: int, *, geometry: bool = True):
        if not self.is_graph_router:
            return
        count = max(1, int(count))
        rebuilt = [exec_port(f"route:{index}", f"Exec · {index}") for index in range(count)]
        if rebuilt == self.output_ports:
            self.settings["route_count"] = count
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.output_ports = rebuilt
        self.settings["route_count"] = count
        descriptions = self.settings.get("route_descriptions")
        if not isinstance(descriptions, dict):
            descriptions = {}
        self.settings["route_descriptions"] = {
            str(index): str(descriptions.get(str(index), "") or "")
            for index in range(count)
        }
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def normalize_router_ports(self):
        if not self.is_graph_router:
            return
        count = max(1, self.router_output_count())
        highest_connected = -1
        for edge in self.edges:
            if edge.source is not self:
                continue
            index = self._router_output_port_index(edge.source_port)
            highest_connected = max(highest_connected, index)

        # Router grows monotonically. Once a numbered route exists, keep it so
        # Python/agent decisions that refer to route numbers never get silently
        # renumbered after a disconnect. Occupying the last visible route exposes
        # one fresh route below it.
        if highest_connected >= count - 1:
            count = highest_connected + 2
        self.set_router_output_count(count)

    def _router_argument_mechanism_enabled(self) -> bool:
        if not self.is_graph_router:
            return False
        for item in list(self.settings.get("mechanisms") or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") == "argument":
                return bool(item.get("enabled"))
        return False

    def sync_router_argument_port(self, *, geometry: bool = True, force_visible: bool = False):
        if not self.is_graph_router:
            return

        connected = any(
            edge.target is self and edge.target_port == "arg:route"
            for edge in self.edges
        )
        should_show = bool(force_visible or connected or self._router_argument_mechanism_enabled())
        exec_ports = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        rebuilt = list(exec_ports)
        if should_show:
            rebuilt.append(arg_in("route", {"type": "integer"}, required=True))
        if rebuilt == self.input_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    @staticmethod
    def _dynamic_arg_port(prefix: str, item: Dict[str, Any], *, output: bool = False) -> PipelinePortSpec:
        item_id = str(item.get("id") or "arg")
        name = str(item.get("name") or "arg")
        schema_type = str(item.get("type") or "string")
        direction = "out" if output else "in"
        return PipelinePortSpec(
            key=f"arg:{prefix}:{direction}:{item_id}",
            label=f"Arg · {name}",
            stream=STREAM_ARG,
            required=not output,
            arg_name=name,
            schema_type=schema_type,
        )

    def python_input_definitions(self) -> List[Dict[str, Any]]:
        if not self.is_python_code:
            return []
        normalized = normalize_argument_definitions(self.settings.get("inputs"))
        self.settings["inputs"] = normalized
        return normalized

    def python_output_definitions(self) -> List[Dict[str, Any]]:
        if not self.is_python_code:
            return []
        normalized = normalize_argument_definitions(self.settings.get("outputs"))
        self.settings["outputs"] = normalized
        return normalized

    def sync_python_code_ports(self, *, geometry: bool = True):
        if not self.is_python_code:
            return
        exec_inputs = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        exec_outputs = [port for port in self.spec.outputs if port.stream == STREAM_EXEC]
        inputs = [
            self._dynamic_arg_port("python", item, output=False)
            for item in self.python_input_definitions()
        ]
        outputs = [
            self._dynamic_arg_port("python", item, output=True)
            for item in self.python_output_definitions()
        ]
        rebuilt_inputs = [*exec_inputs, *inputs]
        rebuilt_outputs = [*exec_outputs, *outputs]
        if rebuilt_inputs == self.input_ports and rebuilt_outputs == self.output_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt_inputs
        self.output_ports = rebuilt_outputs
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def human_output_definitions(self) -> List[Dict[str, Any]]:
        if not self.is_human_in_loop:
            return []
        normalized = normalize_argument_definitions(
            self.settings.get("outputs"),
            with_description=True,
        )
        self.settings["outputs"] = normalized
        return normalized

    def sync_human_in_loop_ports(self, *, geometry: bool = True):
        if not self.is_human_in_loop:
            return
        exec_inputs = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        exec_outputs = [port for port in self.spec.outputs if port.stream == STREAM_EXEC]
        outputs: List[PipelinePortSpec] = []
        if bool(self.settings.get("arguments_enabled")):
            outputs = [
                self._dynamic_arg_port("human", item, output=True)
                for item in self.human_output_definitions()
            ]
        rebuilt_inputs = list(exec_inputs)
        rebuilt_outputs = [*exec_outputs, *outputs]
        if rebuilt_inputs == self.input_ports and rebuilt_outputs == self.output_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt_inputs
        self.output_ports = rebuilt_outputs
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def _loop_argument_condition_enabled(self) -> bool:
        if not self.is_graph_loop:
            return False
        for item in list(self.settings.get("conditions") or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") == "argument":
                return bool(item.get("enabled"))
        return False

    def sync_loop_argument_port(self, *, geometry: bool = True, force_visible: bool = False):
        """Expose Loop's Boolean Arg input only when the mechanism needs it.

        A disabled mechanism keeps the port temporarily while an existing edge is
        still attached, preventing a dangling saved connection. Disconnecting that
        edge removes the inactive port on the next normalization pass.
        """
        if not self.is_graph_loop:
            return

        connected = any(
            edge.target is self and edge.target_port == "arg:repeat"
            for edge in self.edges
        )
        should_show = bool(force_visible or connected or self._loop_argument_condition_enabled())
        exec_ports = [port for port in self.spec.inputs if port.stream == STREAM_EXEC]
        rebuilt = list(exec_ports)
        if should_show:
            rebuilt.append(arg_in("repeat", {"type": "boolean"}, required=True))
        if rebuilt == self.input_ports:
            return
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = rebuilt
        if geometry:
            self.update()
            for edge in list(self.edges):
                edge.update_path()

    def normalize_dynamic_ports(self):
        self.sync_pipeline_interface_ports()
        self.normalize_prompt_ports()
        self.sync_fixed_argument_ports()
        self.normalize_prompt_arg_ports()
        self.normalize_array_ports()
        self.sync_content_input_port()
        self.sync_loop_argument_port()
        self.normalize_router_ports()
        self.sync_router_argument_port()
        self.sync_python_code_ports()
        self.sync_human_in_loop_ports()
        self.sync_execution_ports()
        self.update()

    def sync_pipeline_interface_ports(self, *, geometry=True):
        t = self.spec.type_id
        if t not in {"graph.start", "graph.end_subgraph", "pipeline.call"}:
            return
        from .specs import exec_in, exec_out, arg_result
        from modules.pipelines.contracts import definitions
        from modules.pipelines.dsl import PipelineNodeDefinition
        definition = PipelineNodeDefinition(self.node_id, t, self.settings)
        if geometry and self.scene():
            self.prepareGeometryChange()
        self.input_ports = [] if t == "graph.start" else [exec_in()]
        self.output_ports = [] if t == "graph.end_subgraph" else [exec_out()]
        prefix = "start" if t == "graph.start" else ("end" if t == "graph.end_subgraph" else "call")
        for section, direction in (("inputs", "input"), ("outputs", "output")):
            if t == "graph.start" or (t == "pipeline.call" and section == "inputs") or (t == "graph.end_subgraph" and section == "outputs"):
                continue
            ports = self.input_ports if direction == "input" else self.output_ports
            ports.extend(self._dynamic_arg_port(prefix, item, output=direction == "output") for item in definitions(definition, section))
        if t == "pipeline.call":
            self.output_ports.append(arg_result())
        if geometry:
            self.update()
            for edge in self.edges:
                edge.update_path()

    def sync_execution_ports(self, *, geometry=True):
        from modules.pipelines.execution import error_ports, HANDOFF
        ins = list(self.input_ports)
        outs = [p for p in self.output_ports if p.key not in {'exec:error', 'arg:error'}]
        owned = bool(self.settings.get('_parallel_parent') or self._parallel_chain)
        if self.spec.kind == 'tool':
            ins = [p for p in ins if p.stream != STREAM_EXEC]
            if not self.settings.get('_parallel_parent'):
                # A handoff successor keeps only the chain input, never an
                # ordinary control-flow entry to the parallel job.
                from dataclasses import replace
                ins = [replace(p, label='Subagent · Exec') if self._parallel_chain else p
                       for p in self.spec.inputs if p.stream == STREAM_EXEC] + ins
            if not owned and not any(p.key == 'exec:out' for p in outs):
                outs.insert(0, exec_port('out', 'Exec'))
        if owned:
            outs = [p for p in outs if p.stream != STREAM_EXEC or p.key == HANDOFF]
        elif error_ports(self.settings):
            outs.extend((exec_port('error', 'Exec · Error'), PipelinePortSpec('arg:error', 'Arg · error', STREAM_ARG, schema_type='string')))
        if (ins, outs) != (self.input_ports, self.output_ports):
            if geometry: self.prepareGeometryChange()
            self.input_ports, self.output_ports = ins, outs
            if geometry:
                self.update()
                for edge in self.edges: edge.update_path()

    def card_width(self):
        return NODE_WIDTH

    def serialized_settings(self) -> Dict[str, Any]:
        payload = dict(self.settings)
        if self.is_executable:
            payload["_prompt_input_count"] = self.prompt_input_count()
        if self.value_type == "prompt":
            payload["_prompt_arg_input_count"] = self.prompt_arg_input_count()
        if self.is_array:
            payload["_array_input_count"] = self.array_input_count()
        return payload

    def body_height(self) -> float:
        port_rows = max(len(self.input_ports), len(self.output_ports))
        return (
            HEADER_HEIGHT
            + self.content_height()
            + port_rows * PORT_ROW_HEIGHT
            + BOTTOM_BLANK_HEIGHT
        )

    def boundingRect(self) -> QRectF:
        return QRectF(
            -PORT_HIT_RADIUS,
            -2.0,
            self.card_width() + PORT_HIT_RADIUS * 2.0,
            self.body_height() + 4.0,
        )

    def _body_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, self.card_width(), self.body_height())

    def _port_y(self, direction: str, port_key: str) -> float:
        ports = self._ports(direction)
        index = next(
            (idx for idx, port in enumerate(ports) if port.key == port_key),
            0,
        )
        return self.first_port_center_y() + index * PORT_ROW_HEIGHT

    def port_local_pos(self, direction: str, port_key: str) -> QPointF:
        x = 0.0 if direction == "input" else self.card_width()
        return QPointF(x, self._port_y(direction, port_key))

    def port_scene_pos(self, direction: str, port_key: str) -> QPointF:
        return self.mapToScene(self.port_local_pos(direction, port_key))

    def port_at(self, local_pos: QPointF) -> Optional[Tuple[str, str]]:
        for direction in ("input", "output"):
            for port in self._ports(direction):
                if not self.port_is_enabled(direction, port.key):
                    continue
                point = self.port_local_pos(direction, port.key)
                if (
                    math.hypot(
                        local_pos.x() - point.x(),
                        local_pos.y() - point.y(),
                    )
                    <= PORT_HIT_RADIUS
                ):
                    return direction, port.key
        return None

    def port_is_enabled(self, direction, port_key):
        if direction == 'output':
            contract = self.spec.metadata.get('pipeline_contract', {})
            for group in contract.get('exclusive_outputs', ()):
                if port_key in group and any(
                    edge.source is self and edge.source_port != port_key and edge.source_port in group
                    for edge in self.edges
                ):
                    return False
        return True

    def connection_error(self, port_key, target, target_port):
        from modules.pipelines.extensions import connection_error
        issue = connection_error(self.spec.metadata.get('pipeline_contract', {}),
            port_key, target.spec.type_id, target_port,
            tuple(edge.source_port for edge in self.edges if edge.source is self))
        if issue: return issue
        from modules.pipelines.execution import HANDOFF, PARALLEL
        target_container = target.settings.get('_parallel_parent') or getattr(target, '_parallel_owner', None)
        owner = self.settings.get('_parallel_parent') or (self._parallel_chain and 'chain')
        target_owner = target.settings.get('_parallel_parent') or (target._parallel_chain and 'chain')
        if port_key.startswith('exec:') and (owner or target_owner):
            if port_key != HANDOFF or not owner:
                return tr('ui.caption.2ca93a05d4b9')
            if target.settings.get('_parallel_parent'):
                return tr('ui.caption.d713e8f9cdea')
            if target.outgoing_edges('exec:out') or any(e.target is target and e.target_port == 'exec:in' and e.source_port != HANDOFF for e in target.edges):
                return tr('ui.caption.fa8f922de142')
        return None

    def incoming_edges(self, port_key: str) -> List[Any]:
        return [
            edge
            for edge in self.edges
            if edge.target is self and edge.target_port == port_key
        ]

    def outgoing_edges(self, port_key: str) -> List[Any]:
        return [
            edge
            for edge in self.edges
            if edge.source is self and edge.source_port == port_key
        ]

    def is_parallel_delegate(self):
        from modules.pipelines.execution import HANDOFF
        return bool(self.settings.get('_parallel_parent') and HANDOFF in self.spec.metadata.get('pipeline_contract', {}).get('output_targets', {}))

    def parallel_close_rect(self):
        side = self.header_height() - 4
        return QRectF(self.card_width() - side - 3, 2, side, side)

    def parallel_limit_rect(self):
        close = self.parallel_close_rect()
        return close.translated(-close.width() - 3, 0)

    def _retranslate_chrome(self, *_):
        self._preview_cache = None
        self.update()

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)

        selected = self.isSelected()
        body = self._body_rect()
        accent = self.accent_color()

        body_fill = theme_manager.color('background.panel')
        body_border = theme_manager.color('border.subtle')
        header_fill = theme_manager.color('background.panel', shift=-0.025)
        subtitle_color = theme_manager.color('text.secondary')

        if accent is not None:
            body_border = _blend_colors(theme_manager.color('border.subtle'), accent, 0.42)
            header_fill = _blend_colors(theme_manager.color('background.surface'), accent, 0.35)
            subtitle_color = _blend_colors(theme_manager.color('text.secondary'), accent.lighter(115), 0.55)

        painter.setPen(QPen(body_border, 1.2))
        painter.setBrush(body_fill)
        painter.drawRoundedRect(body, 8.0, 8.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(header_fill)
        painter.drawRoundedRect(
            QRectF(0.0, 0.0, self.card_width(), self.header_height()),
            8.0,
            8.0,
        )
        painter.drawRect(QRectF(0.0, self.header_height() - 8.0, self.card_width(), 8.0))

        # The header fill belongs inside the card; paint the continuous outline
        # after it so the top edge remains visible on unselected cards too.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(body_border, 1.2))
        painter.drawRoundedRect(body, 8.0, 8.0)

        title_font = QFont()
        title_font.setBold(True)
        title_font.setPointSize(9)
        painter.setFont(title_font)
        painter.setPen(theme_manager.color('text.primary'))
        reserve = (self.header_height() - 2) * (1 + int(self.is_parallel_delegate())) if self.settings.get('_parallel_parent') else 0
        if self.spec.type_id == 'graph.parallel': reserve = self.header_height()
        title_rect = QRectF(12.0, 0.0, self.card_width() - 24.0 - reserve, self.header_height())
        painter.drawText(title_rect, Qt.AlignVCenter, painter.fontMetrics().elidedText(node_caption(self.spec), Qt.ElideRight, int(title_rect.width())))
        if self.settings.get('_parallel_parent'):
            if self.is_parallel_delegate():
                badge = self.parallel_limit_rect()
                painter.setPen(QPen(theme_manager.color('accent.primary'), 1))
                painter.setBrush(theme_manager.color('control.selected'))
                painter.drawRoundedRect(badge, 4, 4)
                painter.drawText(badge, Qt.AlignCenter, '1')
            if self._parallel_header_hover:
                close = self.parallel_close_rect()
                painter.setPen(QPen(theme_manager.color('text.primary'), 1.4))
                painter.drawLine(close.topLeft()+QPointF(6,6), close.bottomRight()-QPointF(6,6))
                painter.drawLine(close.topRight()+QPointF(-6,6), close.bottomLeft()+QPointF(6,-6))

        content_rect = QRectF(0.0, HEADER_HEIGHT, self.card_width(), self.content_height())
        content_text_rect = content_rect.adjusted(14.0, 0.0, -14.0, 0.0)

        painter.setFont(QFont())
        metrics = painter.fontMetrics()

        def draw_preview(text: str, color: QColor, *, multiline: bool = False, segments=None):
            painter.setPen(color)
            text = str(text or "")
            if not text and not segments:
                return

            if not multiline:
                normalized = " ".join(text.replace("\n", " ").split())
                rendered = metrics.elidedText(
                    normalized,
                    Qt.ElideRight,
                    int(content_text_rect.width()),
                )
                painter.drawText(
                    content_text_rect,
                    Qt.AlignLeft | Qt.AlignVCenter,
                    rendered,
                )
                return

            field_rect = self.content_field_rect()
            painter.setPen(QPen(theme_manager.color('background.surface'), 1.0))
            painter.setBrush(theme_manager.color('background.input'))
            painter.drawRoundedRect(field_rect, 6.0, 6.0)

            text_rect = field_rect.adjusted(8.0, 6.0, -13.0, -6.0)
            cache_key = (text, color.rgba(), painter.font().toString(), round(text_rect.width(), 3),
                         tuple((str(t), c.rgba()) for t, c in segments) if segments else ())
            if self._preview_cache is not None and self._preview_cache[0] == cache_key:
                document = self._preview_cache[1]
            else:
                document = QTextDocument()
                document.setDocumentMargin(0.0)
                document.setDefaultFont(painter.font())
                option = document.defaultTextOption()
                option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
                document.setDefaultTextOption(option)
                document.setTextWidth(max(1.0, text_rect.width()))

                if segments:
                    cursor = QTextCursor(document)
                    first = True
                    for segment_text, segment_color in segments:
                        segment_text = str(segment_text or "")
                        if not segment_text:
                            continue
                        if not first:
                            cursor.insertText("\n")
                        fmt = QTextCharFormat()
                        fmt.setForeground(segment_color)
                        cursor.insertText(segment_text, fmt)
                        first = False
                else:
                    document.setPlainText(text)
                    cursor = QTextCursor(document)
                    cursor.select(QTextCursor.Document)
                    char_format = QTextCharFormat()
                    char_format.setForeground(color)
                    cursor.mergeCharFormat(char_format)
                self._preview_cache = (cache_key, document)

            document_height = float(document.size().height())
            self._content_document_height = document_height
            self._content_scroll_max = max(0.0, document_height - text_rect.height())
            self._content_scroll_y = max(
                0.0,
                min(float(self._content_scroll_y), self._content_scroll_max),
            )

            painter.save()
            painter.setClipRect(text_rect)
            painter.translate(
                text_rect.x(),
                text_rect.y() - self._content_scroll_y,
            )
            document.drawContents(
                painter,
                QRectF(
                    0.0,
                    self._content_scroll_y,
                    text_rect.width(),
                    text_rect.height(),
                ),
            )
            painter.restore()

            if self._content_scroll_max > 0.0:
                track = self._content_scrollbar_track_rect()
                thumb = self._content_scrollbar_thumb_rect()
                painter.setPen(Qt.NoPen)
                painter.setBrush(theme_manager.color('background.surface'))
                painter.drawRoundedRect(track, 1.5, 1.5)
                painter.setBrush(theme_manager.color('text.secondary'))
                painter.drawRoundedRect(thumb, 1.5, 1.5)

            # The resize arrow sits a few pixels below the field border. It does
            # not reserve a grid row and intentionally overlaps the next band.
            handle = self._content_resize_handle_rect()
            cx = handle.center().x()
            top = handle.top() + 2.0
            painter.setPen(QPen(theme_manager.color('text.secondary'), 1.4))
            painter.drawLine(QPointF(cx - 4.0, top), QPointF(cx, top + 4.0))
            painter.drawLine(QPointF(cx, top + 4.0), QPointF(cx + 4.0, top))

        if self.spec.kind == "content":
            if self.value_type == "prompt":
                local_text = str(self.settings.get("text") or "")
                incoming_text = self.prompt_arg_preview_text()
                segments = []
                if local_text:
                    segments.append((local_text, theme_manager.color('text.primary')))
                if incoming_text:
                    segments.append((incoming_text, theme_manager.color('text.secondary')))
                if segments:
                    draw_preview(
                        "",
                        theme_manager.color('text.primary'),
                        multiline=True,
                        segments=segments,
                    )
                else:
                    draw_preview(
                        tr('nodes.preview.empty_prompt'),
                        theme_manager.color('text.secondary'),
                        multiline=True,
                    )
            elif self.is_array:
                literal_text = str(self.settings.get("literal_text") or "").strip()
                item_type = str(self.settings.get("item_type") or "string")
                preview = literal_text or tr('nodes.preview.empty_array', type=item_type)
                draw_preview(
                    preview,
                    theme_manager.color('text.primary') if literal_text else theme_manager.color('text.secondary'),
                    multiline=True,
                )
            else:
                value = self.settings.get("value")
                if self.value_type == "binary" and value is None:
                    preview = tr('ui.inspector.none_dc93')
                    has_value = False
                elif value is None or (isinstance(value, str) and not value.strip()):
                    preview = tr('nodes.preview.empty_value', type=node_caption(self.spec))
                    has_value = False
                else:
                    preview = str(value)
                    has_value = True
                draw_preview(
                    preview,
                    theme_manager.color('text.primary') if has_value else theme_manager.color('text.secondary'),
                    multiline=self.content_row_count() > 1,
                )
        elif self.spec.kind in {"module", "tool"}:
            subtitle = (
                tr('nodes.preview.module')
                if self.spec.kind == "module"
                else str(self.spec.metadata.get("module") or tr('nodes.preview.tool'))
            )
            draw_preview(subtitle, subtitle_color)
        elif self.is_python_code:
            code = str(self.settings.get("code") or "")
            draw_preview(
                code if code else tr('ui.inspector.python_code_9134'),
                theme_manager.color('text.primary') if code else subtitle_color,
                multiline=True,
            )
        elif self.spec.kind == "graph_control":
            if self.is_human_in_loop:
                message = str(self.settings.get("message") or "").strip()
                subtitle = message or node_caption(self.spec)
                preview_color = theme_manager.color('text.primary') if message else subtitle_color
            elif str(self.spec.metadata.get("inspector_type") or "") == "graph_message":
                message = str(self.settings.get("message") or "")
                subtitle = message.strip() or metadata_caption(self.spec, 'message_placeholder', node_caption(self.spec))
                preview_color = theme_manager.color('text.primary') if message else subtitle_color
            else:
                subtitle = node_caption(self.spec)
                preview_color = subtitle_color
            draw_preview(
                subtitle,
                preview_color,
                multiline=self.content_row_count() > 1,
            )

        # Selection above the card body/header/text, below connector circles.
        if selected:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(theme_manager.color('accent.primary'), 2.0))
            painter.drawRoundedRect(body, 8.0, 8.0)

        painter.setFont(QFont())
        for direction in ("input", "output"):
            for port in self._ports(direction):
                point = self.port_local_pos(direction, port.key)
                color = stream_color(port.stream)
                if not self.port_is_enabled(direction, port.key):
                    color = theme_manager.color('border.subtle')

                painter.setPen(QPen(theme_manager.color('background.input'), 1.0))
                painter.setBrush(color)
                painter.drawEllipse(point, PORT_RADIUS, PORT_RADIUS)

                painter.setPen(color.lighter(135))
                row_top = point.y() - PORT_ROW_HEIGHT / 2.0
                label_rect = (
                    QRectF(12.0, row_top, self.card_width() / 2.0 - 18.0, PORT_ROW_HEIGHT)
                    if direction == "input"
                    else QRectF(self.card_width() / 2.0 + 6.0, row_top, self.card_width() / 2.0 - 18.0, PORT_ROW_HEIGHT)
                )
                alignment = (
                    Qt.AlignLeft | Qt.AlignVCenter
                    if direction == "input"
                    else Qt.AlignRight | Qt.AlignVCenter
                )
                label = port.label
                if direction == "output":
                    stream_name = {
                        STREAM_EXEC: "Exec",
                        STREAM_ARG: "Arg",
                        STREAM_PROMPT: "Prompt",
                    }.get(port.stream, "")
                    prefix = f"{stream_name} · " if stream_name else ""
                    if prefix and label.startswith(prefix):
                        semantic = label[len(prefix):].strip()
                        label = f"{semantic} · {stream_name}" if semantic else stream_name
                painter.drawText(label_rect, alignment, label)

    def _pipeline_view(self):
        if not self.scene():
            return None
        views = self.scene().views()
        return views[0] if views else None

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged and bool(value):
            self._raise_above_pipeline_nodes()

        if change == QGraphicsItem.ItemPositionChange and self.scene():
            new_pos = QPointF(value)
            view = self._pipeline_view()
            if view is not None and bool(getattr(view, "snap_to_grid", False)):
                new_pos = QPointF(
                    round(new_pos.x() / GRID_SIZE) * GRID_SIZE,
                    round(new_pos.y() / GRID_SIZE) * GRID_SIZE,
                )
            return new_pos

        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged:
            view = self._pipeline_view()
            if view is not None and hasattr(view, "warning_manager"): view.warning_manager.reposition()
            for edge in list(self.edges):
                edge.update_path()
        return result

    def _set_content_scroll_from_thumb_y(self, thumb_top: float):
        track = self._content_scrollbar_track_rect()
        thumb = self._content_scrollbar_thumb_rect()
        travel = max(0.0, track.height() - thumb.height())
        if travel <= 0.0 or self._content_scroll_max <= 0.0:
            self._content_scroll_y = 0.0
        else:
            clamped_top = max(track.top(), min(float(thumb_top), track.bottom() - thumb.height()))
            progress = (clamped_top - track.top()) / travel
            self._content_scroll_y = progress * self._content_scroll_max
        self.update()

    def hoverEnterEvent(self, event):
        self._parallel_header_hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):
        self._parallel_header_hover = True
        self.update()
        hit = self.port_at(event.pos())
        if hit:
            port = self.port_spec(*hit)
            kind = port.schema_type or (tr('ui.caption.6353ad2650bd') if hit[1] == 'arg:result' else tr('ui.caption.9d8cf70dc46c'))
            if self.spec.type_id == 'graph.parallel' and hit[1] == 'arg:result':
                kind = tr('ui.caption.2b14fc592091')
            set_text(self, 'ui.base.v0_type_v1_1a21', setter='setToolTip', v0=port.label, v1=kind)
        elif self.settings.get('_parallel_parent') and self.parallel_close_rect().contains(event.pos()):
            set_text(self, 'ui.base.delete_tool_and_its_connections_can_8288', setter='setToolTip')
        elif self.is_parallel_delegate() and self.parallel_limit_rect().contains(event.pos()):
            set_text(self, 'ui.base.a_block_can_contain_only_one_dbaf', setter='setToolTip')
        else: bind_caption(self, '', setter='setToolTip')
        if (
            self.supports_resizable_content_field()
            and self._content_resize_handle_rect().contains(event.pos())
        ):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):
        self._parallel_header_hover = False
        self.update()
        if self._content_resize_start_scene_y is None:
            self.unsetCursor()
        super().hoverLeaveEvent(event)

    def wheelEvent(self, event):
        if (
            self.uses_scrollable_content_field()
            and self.content_field_rect().contains(event.pos())
            and self._content_scroll_max > 0.0
        ):
            delta = float(event.delta())
            step = CONTENT_ROW_HEIGHT
            self._content_scroll_y = max(
                0.0,
                min(
                    self._content_scroll_max,
                    self._content_scroll_y - (delta / 120.0) * step,
                ),
            )
            self.update()
            event.accept()
            return
        event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.settings.get('_parallel_parent') and self.parallel_close_rect().contains(event.pos()):
            self._parallel_delete_pressed = True
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self.supports_resizable_content_field()
            and self._content_resize_handle_rect().contains(event.pos())
        ):
            self._content_resize_start_scene_y = float(event.scenePos().y())
            self._content_resize_start_rows = int(self.content_row_count())
            self.setCursor(Qt.SizeVerCursor)
            event.accept()
            return

        if (
            event.button() == Qt.LeftButton
            and self.uses_scrollable_content_field()
            and self._content_scroll_max > 0.0
        ):
            track = self._content_scrollbar_track_rect().adjusted(-4.0, 0.0, 4.0, 0.0)
            if track.contains(event.pos()):
                thumb = self._content_scrollbar_thumb_rect()
                if thumb.contains(event.pos()):
                    self._content_scroll_drag_offset = event.pos().y() - thumb.top()
                else:
                    self._content_scroll_drag_offset = thumb.height() / 2.0
                    self._set_content_scroll_from_thumb_y(
                        event.pos().y() - self._content_scroll_drag_offset
                    )
                event.accept()
                return

        if event.button() == Qt.LeftButton:
            port = self.port_at(event.pos())
            if port is not None and self.scene():
                view = self._pipeline_view()
                if view is not None and hasattr(view, "start_pipeline_connection"):
                    direction, port_key = port
                    view.start_pipeline_connection(self, direction, port_key)
                    event.accept()
                    return

        if event.button() != Qt.LeftButton or not self.scene():
            super().mousePressEvent(event)
            return

        was_already_selected = self.isSelected()
        self._raise_above_pipeline_nodes()
        if (
            was_already_selected
            and self.supports_inline_content_edit()
            and self.content_field_rect().contains(event.pos())
        ):
            self._start_inline_content_edit()
            event.accept()
            return

        self._pipeline_press_scene_pos = QPointF(event.scenePos())
        self._pipeline_press_modifiers = event.modifiers()
        self._pipeline_dragged = False
        self._pipeline_was_selected = self.isSelected()

        view = self._pipeline_view()
        if view is not None:
            begin_deferred = getattr(view, "begin_deferred_pipeline_selection", None)
            if callable(begin_deferred):
                begin_deferred()
                self._pipeline_selection_deferred = True
            view.setFocus(Qt.MouseFocusReason)

        if not self._pipeline_was_selected:
            additive = bool(
                self._pipeline_press_modifiers
                & (Qt.ControlModifier | Qt.ShiftModifier)
            )
            if not additive:
                self.scene().clearSelection()

            self.setFlag(QGraphicsItem.ItemIsSelectable, False)
            try:
                super().mousePressEvent(event)
            finally:
                self.setFlag(QGraphicsItem.ItemIsSelectable, True)

            move_nodes = [self]
        else:
            super().mousePressEvent(event)
            move_nodes = [
                item
                for item in self.scene().selectedItems()
                if isinstance(item, PipelineNodeItem)
            ]
            if self not in move_nodes:
                move_nodes.append(self)

        self._pipeline_move_start_positions = {
            item: QPointF(item.pos()) for item in move_nodes
        }

    def mouseMoveEvent(self, event):
        if (
            self._content_resize_start_scene_y is not None
            and self._content_resize_start_rows is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            delta = float(event.scenePos().y()) - float(self._content_resize_start_scene_y)
            steps = int(round(delta / float(GRID_SIZE)))
            self.set_content_row_count(int(self._content_resize_start_rows) + steps)
            event.accept()
            return

        if (
            self._content_scroll_drag_offset is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            self._set_content_scroll_from_thumb_y(
                event.pos().y() - float(self._content_scroll_drag_offset)
            )
            event.accept()
            return

        view = self._pipeline_view()
        if (
            view is not None
            and getattr(view, "_pipeline_source_node", None) is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            view.update_pipeline_connection(event.scenePos())
            event.accept()
            return

        press_pos = getattr(self, "_pipeline_press_scene_pos", None)
        if press_pos is not None and (event.buttons() & Qt.LeftButton):
            delta = event.scenePos() - press_pos
            if delta.manhattanLength() >= 4.0:
                if not getattr(self,'_pipeline_dragged',False) and view is not None:
                    suspend = getattr(view,'suspend_node_inspector_for_drag',None)
                    if callable(suspend):
                        suspend()
                self._pipeline_dragged = True

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._parallel_delete_pressed:
            self._parallel_delete_pressed = False
            if event.button() == Qt.LeftButton and self.parallel_close_rect().contains(event.pos()):
                view = self._pipeline_view()
                if view is not None: view.delete_parallel_tool(self)
            event.accept()
            return
        if self._content_resize_start_scene_y is not None and event.button() == Qt.LeftButton:
            self._content_resize_start_scene_y = None
            self._content_resize_start_rows = None
            self.unsetCursor()
            view = self._pipeline_view()
            if view is not None:
                signal = getattr(view, "layout_changed", None)
                if signal is not None:
                    try:
                        signal.emit()
                    except RuntimeError:
                        pass
            event.accept()
            return

        if self._content_scroll_drag_offset is not None and event.button() == Qt.LeftButton:
            self._content_scroll_drag_offset = None
            event.accept()
            return

        view = self._pipeline_view()

        if (
            view is not None
            and getattr(view, "_pipeline_source_node", None) is not None
            and event.button() == Qt.LeftButton
        ):
            view.finish_pipeline_connection(event.scenePos())
            event.accept()
            return

        super().mouseReleaseEvent(event)

        if event.button() == Qt.LeftButton and self.scene():
            dragged = bool(getattr(self, "_pipeline_dragged", False))
            was_selected = bool(getattr(self, "_pipeline_was_selected", False))
            modifiers = getattr(self, "_pipeline_press_modifiers", Qt.NoModifier)

            if not was_selected:
                additive = bool(modifiers & (Qt.ControlModifier | Qt.ShiftModifier))
                if not additive:
                    self.scene().clearSelection()
                self.setSelected(True)

        starts = getattr(self, "_pipeline_move_start_positions", None)
        if event.button() == Qt.LeftButton and starts and view is not None:
            commit_moves = getattr(view, "commit_pipeline_node_moves", None)
            if callable(commit_moves):
                commit_moves(starts)

        if bool(getattr(self, "_pipeline_selection_deferred", False)) and view is not None:
            end_deferred = getattr(view, "end_deferred_pipeline_selection", None)
            if callable(end_deferred):
                end_deferred()

        for attr in (
            "_pipeline_press_scene_pos",
            "_pipeline_press_modifiers",
            "_pipeline_dragged",
            "_pipeline_was_selected",
            "_pipeline_move_start_positions",
            "_pipeline_selection_deferred",
        ):
            if hasattr(self, attr):
                delattr(self, attr)
