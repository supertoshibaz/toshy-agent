from __future__ import annotations
from .presentation import node_caption
from ui.widgets.workspace_menu import standardize_workspace_menu
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text, i18n
from ui.styles.binding import apply_style
from ui.document_paths import show_document_path
from ui.icons import set_icon, symbol_button

from ui.tab_memory import TabMemoryManager, SuspendedTab

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QByteArray, QMimeData, QPointF, QRectF, Qt, Signal, Slot, QTimer, QEvent
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QMouseEvent, QPen, QShortcut
from PySide6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.views.canvas.canvas_card import GRID_SIZE
from core.paths import APP_PATHS
from modules.pipelines.dsl import Pipeline
from modules.pipelines.control import GRAPH_START_TYPE
from ui.views.canvas.canvas_view import CanvasControlPanel, CanvasView
from ui.views.file_view import FileTabBar, NewTabButton, TabCloseButton, TabRenameLineEdit

from .commands import (
    AddPipelineEdgeCommand,
    AddPipelineNodeCommand,
    DeletePipelineSelectionCommand,
    MovePipelineNodesCommand,
)
from .edge import PipelineEdgeItem
from .inspector import PipelineInspector
from .library import NodeLibraryMenu
from .nodes.parallel import make_node, ParallelNodeItem
from .nodes.base import PORT_RADIUS, PipelineNodeItem, initial_node_settings
from .nodes.registry import build_default_node_registry
from .nodes.specs import STREAM_EXEC, PipelineNodeSpec, stream_color
from .persistence import (
    PipelineLoadError,
    create_empty_pipeline,
    ensure_pipeline_root,
    load_layout,
    load_pipeline_script,
    pipeline_layout_path,
    pipeline_script_path,
    save_layout,
    save_pipeline_script,
)


PIPELINE_CLIPBOARD_MIME = "application/x-toshy-pipeline-selection"


class PipelineCanvasView(CanvasView):
    node_selection_changed = Signal(object)
    node_settings_changed = Signal(object)
    layout_changed = Signal()
    _tip_grab_item_type = PipelineEdgeItem

    def __init__(self, parent=None):
        super().__init__(parent)

        # Pipeline nodes are not free-form board cards. Reuse the board scene,
        # navigation, grid and camera controls, but not its card toolbar.
        self.selection_toolbar.hide()
        # Pipeline canvas keeps the board's rectangular area selection.
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

        old_panel = self.control_panel
        self.control_panel = CanvasControlPanel(self, self, show_history=True)
        old_panel.hide()
        old_panel.deleteLater()
        self._update_panel_position()

        # CanvasView creates QWidget-scoped QShortcuts. In the bottom panel
        # they can become ambiguous with other active widgets. Route pipeline
        # editing keys through this QGraphicsView directly instead.
        for shortcut in (
            self.shortcut_copy,
            self.shortcut_paste,
            self.shortcut_undo,
            self.shortcut_redo,
            self.shortcut_del,
        ):
            shortcut.setEnabled(False)

        self.setFocusPolicy(Qt.StrongFocus)
        self._pipeline_clipboard: Optional[Dict[str, object]] = None
        self._paste_generation = 0

        self.node_registry = build_default_node_registry(mcp_owner_getter=self._mcp_owner)
        event_bus.mcp_changed.connect(self._refresh_node_library)
        self._library_specs = self.node_registry.specs()
        self._pending_library_pos = QPointF(0.0, 0.0)

        self._pipeline_source_node: Optional[PipelineNodeItem] = None
        self._pipeline_source_direction: Optional[str] = None
        self._pipeline_source_port: Optional[str] = None

        # Selection itself may change during mouse press (Qt does this before a
        # card drag), but opening/switching the inspector is intentionally a
        # mouse-release action. This prevents the settings panel from jumping
        # while the user is merely starting a drag or adjusting a selection.
        self._defer_pipeline_selection_signal = False
        self._pipeline_node_drag_active = False

        # QGraphicsScene owns the C++ graphics items, but a loaded QGraphicsObject
        # still needs a live Python wrapper on PySide. Newly created nodes are
        # incidentally retained by undo commands; nodes restored from disk are not.
        # In particular an unconnected restored node could therefore be collected
        # during a later GUI event (zooming under the cursor made this reproducible),
        # which removes it from the scene and the next autosave then persists that
        # accidental deletion. Keep explicit Python-side ownership for the lifetime
        # of this canvas. Deleted items may stay retained until the next full reload;
        # that is intentional because undo can still reinsert them.
        self._pipeline_node_refs: Dict[str, PipelineNodeItem] = {}
        self._pipeline_edge_refs = set()
        from .warnings import PipelineWarnings
        self.warning_manager = PipelineWarnings(self)
        self.check_graph_button = QToolButton(self.control_panel)
        self.check_graph_button.setObjectName('CheckPipelineGraph')
        symbol_button(self.check_graph_button, '✓')
        self.check_graph_button.setFixedSize(28,28)
        set_text(self.check_graph_button, 'ui.warnings.check_graph_for_errors_9dbd', setter='setToolTip')
        self.check_graph_button.setCursor(Qt.PointingHandCursor)
        self.check_graph_button.clicked.connect(self.warning_manager.run_check)
        self.control_panel.layout().insertWidget(1,self.check_graph_button)
        self.help_button = QToolButton(self.control_panel)
        self.help_button.setObjectName('PipelineHelpButton')
        set_icon(self.help_button, 'book', square=True)
        self.help_button.setFixedSize(28, 28)
        set_text(self.help_button, 'pipelines.help.open', setter='setToolTip')
        self.help_button.setCursor(Qt.PointingHandCursor)
        self.help_button.clicked.connect(self._open_pipeline_help)
        self.control_panel.layout().insertWidget(2, self.help_button)
        self.control_panel.adjustSize()
        self._update_panel_position()

        self.canvas_scene.selectionChanged.connect(self._emit_pipeline_selection)
        # Undo/redo mutates the graph without going through the original user
        # action handlers, so persistence must also observe stack changes.
        self.undo_stack.indexChanged.connect(self._on_undo_stack_index_changed)
        self.horizontalScrollBar().valueChanged.connect(
            self._on_layout_scroll_value_changed
        )
        self.verticalScrollBar().valueChanged.connect(
            self._on_layout_scroll_value_changed
        )

    def _open_pipeline_help(self):
        from ui.pipeline_help import FILENAME, document_path
        opener = getattr(self.window(), '_open_builtin_doc', None)
        if opener is not None:
            if not opener(FILENAME):
                inline.notice(self, tr('pipelines.help.open'), tr('pipelines.help.unavailable'))
        else:
            event_bus.open_file_requested.emit(str(document_path()))

    @Slot(int)
    def _on_undo_stack_index_changed(self, _index: int):
        # Edge undo/redo may change dynamic-port families (Prompt Args, module
        # Prompts, Arrays). Keep the visible spare slot in sync with the actual
        # connections after every history mutation.
        self.refresh_parallel_groups()
        self.modified.emit()

    @Slot(int)
    def _on_layout_scroll_value_changed(self, _value: int):
        self.camera_changed.emit()

    def _emit_pipeline_selection(self):
        if self._defer_pipeline_selection_signal or getattr(self,"_pipeline_node_drag_active",False):
            return
        selected = [
            item
            for item in self.canvas_scene.selectedItems()
            if isinstance(item, PipelineNodeItem)
        ]
        # Multi-selection is valid on the canvas for moving/copying. The
        # inspector intentionally opens only for one selected node.
        self.node_selection_changed.emit(selected[0] if len(selected) == 1 else None)

    def begin_deferred_pipeline_selection(self):
        self._defer_pipeline_selection_signal = True

    def suspend_node_inspector_for_drag(self):
        if not getattr(self,'_pipeline_node_drag_active',False):
            self._pipeline_node_drag_active = True
            self.node_selection_changed.emit(None)

    def end_deferred_pipeline_selection(self):
        self._pipeline_node_drag_active = False
        self._defer_pipeline_selection_signal = False
        self._emit_pipeline_selection()

    def _mcp_owner(self):
        path = getattr(self, 'pipeline_dir', None)
        from modules.pipelines.management import package_owner
        owner, _ = package_owner(path) if path is not None else (None, None)
        return owner or 'global'

    def _refresh_node_library(self):
        self.node_registry.refresh_providers()
        self._library_specs = self.node_registry.specs()

    def register_node_provider(self, provider):
        """Attach a dynamic node source without changing the workspace/menu code."""
        self.node_registry.add_provider(provider)
        self._refresh_node_library()

    def node_participates_in_exec_cycle(self, node: PipelineNodeItem) -> bool:
        """Return True when an Exec path from node can eventually return to node."""
        if node is None or node.scene() is not self.canvas_scene:
            return False

        stack = []
        for edge in list(node.edges):
            if edge.source is not node:
                continue
            port = node.port_spec("output", edge.source_port)
            if port is not None and port.stream == STREAM_EXEC:
                stack.append(edge.target)

        visited = set()
        while stack:
            current = stack.pop()
            if current is node:
                return True
            marker = id(current)
            if marker in visited:
                continue
            visited.add(marker)
            for edge in list(getattr(current, "edges", ())):
                if edge.source is not current:
                    continue
                port = current.port_spec("output", edge.source_port)
                if port is not None and port.stream == STREAM_EXEC:
                    stack.append(edge.target)
        return False

    def populate_settings_menu(self, menu: QMenu):
        act_snap = localize(add_caption_action(menu, 'addAction', "Привязка к сетке"), 'ui.workspace.snap_to_grid_b33e')
        act_snap.setCheckable(True)
        act_snap.setChecked(self.snap_to_grid)
        act_snap.triggered.connect(self.set_snap_to_grid)

    def set_snap_to_grid(self, enabled: bool):
        self.snap_to_grid = bool(enabled)

    def reset_view(self):
        self.resetTransform()
        self._current_zoom = 1.0
        nodes = [
            item
            for item in self.canvas_scene.items()
            if isinstance(item, PipelineNodeItem)
        ]
        if nodes:
            rect = QRectF()
            for node in nodes:
                rect = rect.united(node.sceneBoundingRect())
            self.centerOn(rect.center())
        else:
            self.centerOn(0.0, 0.0)
        self.camera_changed.emit()

    def commit_pipeline_node_moves(self, starts):
        moves = []
        for node, old_pos in starts.items():
            if node.scene() is not self.canvas_scene:
                continue
            new_pos = QPointF(node.pos())
            if (new_pos - old_pos).manhattanLength() >= 1.0:
                moves.append((node, old_pos, new_pos))
        if not moves:
            return
        self.undo_stack.push(MovePipelineNodesCommand(moves))
        self.modified.emit()
        self.layout_changed.emit()

    def _retain_pipeline_node(self, node: PipelineNodeItem):
        self._pipeline_node_refs[str(node.node_id)] = node
        return node

    def _retain_pipeline_edge(self, edge: PipelineEdgeItem):
        self._pipeline_edge_refs.add(edge)
        return edge

    def create_pipeline_node(self, spec: PipelineNodeSpec, scene_pos: QPointF):
        if bool(spec.metadata.get("singleton")):
            for item in self.canvas_scene.items():
                if isinstance(item, PipelineNodeItem) and item.spec.type_id == spec.type_id:
                    return item

        x, y = scene_pos.x(), scene_pos.y()
        if self.snap_to_grid:
            x = round(x / GRID_SIZE) * GRID_SIZE
            y = round(y / GRID_SIZE) * GRID_SIZE

        settings = initial_node_settings(spec)
        node = make_node(spec, x, y, settings=settings)
        self._retain_pipeline_node(node)
        self.undo_stack.push(AddPipelineNodeCommand(self.canvas_scene, node))

        # Creation must not change selection or camera position.
        node.setSelected(False)
        self.modified.emit()
        return node

    def add_parallel_tool(self, parent, spec):
        if spec.type_id == 'advanced.agent_decision':
            return None
        from modules.pipelines.execution import HANDOFF
        if HANDOFF in spec.metadata.get('pipeline_contract', {}).get('output_targets', {}) and any(n.is_parallel_delegate() for n in parent.children()):
            return None
        settings = initial_node_settings(spec)
        settings.update(_parallel_parent=parent.node_id, _parallel_max_calls=1,
                        _parallel_order=max((int(n.settings.get('_parallel_order', 0)) for n in parent.children()), default=-1)+1)
        node = make_node(spec, 0, 0, settings=settings)
        self._retain_pipeline_node(node)
        self.undo_stack.push(AddPipelineNodeCommand(self.canvas_scene, node))
        self.refresh_parallel_groups()
        self.modified.emit()
        return node

    def reorder_parallel_tools(self, parent, children):
        from .commands import ReorderParallelNodesCommand
        if children != parent.children() and set(children) == set(parent.children()):
            self.undo_stack.push(ReorderParallelNodesCommand(parent, children))

    def delete_parallel_tool(self, node):
        if not node.settings.get('_parallel_parent') or node.scene() is not self.canvas_scene:
            return
        self.undo_stack.push(DeletePipelineSelectionCommand(self.canvas_scene, [node], list(node.edges)))
        self._emit_pipeline_selection()

    def change_node_settings(self, node, settings):
        from .commands import ChangePipelineSettingsCommand
        if node.settings != settings:
            self.undo_stack.push(ChangePipelineSettingsCommand(self,node,settings))

    def refresh_parallel_groups(self):
        from modules.pipelines.execution import parallel_owners
        nodes = {item.node_id: item for item in self.canvas_scene.items() if isinstance(item, PipelineNodeItem)}
        try:
            owners = parallel_owners(self.build_pipeline_definition(''))
        except ValueError:
            owners = {}
        for node in nodes.values():
            parent = nodes.get(node.settings.get('_parallel_parent'))
            if isinstance(parent, ParallelNodeItem) and node.parentItem() is not parent:
                node.setParentItem(parent)
            node._parallel_owner = owners.get(node.node_id) or node.settings.get('_parallel_parent')
            node._parallel_chain = node.node_id in owners and not node.settings.get('_parallel_parent')
            node.sync_execution_ports()
        for node in nodes.values():
            if isinstance(node, ParallelNodeItem): node.arrange()
        # Connections touching nested cards live above their container background
        # and below the cards, rather than in the scene's background edge layer.
        for edge in list(self.canvas_scene.items()):
            if not isinstance(edge, PipelineEdgeItem): continue
            parent = edge.target.parentItem() or edge.source.parentItem()
            if not isinstance(parent, ParallelNodeItem): parent = None
            if edge.parentItem() is not parent:
                edge.setParentItem(parent); edge.setPos(0, 0)
            edge.setZValue(1 if parent else -5)
            edge.update_path()

    def _node_under_view_pos(self, view_pos) -> Optional[PipelineNodeItem]:
        item = self.itemAt(view_pos)
        while item is not None:
            if isinstance(item, PipelineNodeItem):
                return item
            item = item.parentItem()
        return None

    def _edge_under_view_pos(self, view_pos) -> Optional[PipelineEdgeItem]:
        item = self.itemAt(view_pos)
        while item is not None:
            if isinstance(item, PipelineEdgeItem):
                return item
            item = item.parentItem()
        return None

    def _disconnect_edge(self, edge: PipelineEdgeItem):
        if edge.scene() is not self.canvas_scene:
            return
        self.undo_stack.push(
            DeletePipelineSelectionCommand(
                self.canvas_scene,
                [],
                [edge],
            )
        )
        self.modified.emit()
        self._emit_pipeline_selection()

    def delete_pipeline_edge(self, edge: PipelineEdgeItem):
        """Delete one selected edge from the compact edge toolbar."""
        self._disconnect_edge(edge)

    def reassign_pipeline_edge(self, edge: PipelineEdgeItem, scene_pos: QPointF):
        """Detach an edge and continue dragging its original output to a new target."""
        if edge.scene() is not self.canvas_scene:
            return
        source = edge.source
        source_port = edge.source_port
        self._disconnect_edge(edge)
        self.start_pipeline_connection(source, "output", source_port)
        self.update_pipeline_connection(scene_pos)

    def _reroute_endpoint(self, item, scene_pos):
        self.reassign_pipeline_edge(item, scene_pos)

    def start_pipeline_connection(
        self,
        node: PipelineNodeItem,
        direction: str,
        port_key: str,
    ):
        if direction != "output":
            return

        port = node.port_spec("output", port_key)
        if port is None or not node.port_is_enabled(direction, port_key):
            return

        self._cancel_pipeline_connection()
        self._pipeline_source_node = node
        self._pipeline_source_direction = direction
        self._pipeline_source_port = str(port_key)

        output_center = node.port_scene_pos("output", port_key)
        start = QPointF(output_center.x() + PORT_RADIUS, output_center.y())
        self._temp_line_item = QGraphicsLineItem()
        self._temp_line_item.setPen(
            QPen(
                stream_color(port.stream),
                2.2 if port.stream == STREAM_EXEC else 1.8,
                Qt.DashLine,
            )
        )
        self._temp_line_item.setZValue(20.0)
        self.canvas_scene.addItem(self._temp_line_item)
        self._temp_line_item.setLine(
            start.x(), start.y(), start.x(), start.y()
        )

    def update_pipeline_connection(self, scene_pos: QPointF):
        if self._pipeline_source_node is None or self._temp_line_item is None:
            return

        port_key = str(self._pipeline_source_port or "")
        if not port_key:
            return

        output_center = self._pipeline_source_node.port_scene_pos(
            "output",
            port_key,
        )
        start = QPointF(output_center.x() + PORT_RADIUS, output_center.y())
        self._temp_line_item.setLine(
            start.x(), start.y(), scene_pos.x(), scene_pos.y()
        )

    def finish_pipeline_connection(self, scene_pos: QPointF):
        source = self._pipeline_source_node
        source_port_key = str(self._pipeline_source_port or "")
        if source is None or not source_port_key:
            self._cancel_pipeline_connection()
            return

        source_port = source.port_spec("output", source_port_key)
        if source_port is None or not source.port_is_enabled('output', source_port_key):
            self._cancel_pipeline_connection()
            return

        target = None
        candidates = []
        for item in self.canvas_scene.items(scene_pos):
            current = item
            while current is not None:
                if isinstance(current, PipelineNodeItem) and current not in candidates:
                    candidates.append(current)
                current = current.parentItem()
        def depth(item):
            count = 0
            while item.parentItem() is not None: count += 1; item = item.parentItem()
            return count
        for candidate in sorted(candidates, key=depth, reverse=True):
            if candidate is source: continue
            hit = candidate.port_at(candidate.mapFromScene(scene_pos))
            if hit and hit[0] == 'input':
                port = candidate.port_spec('input', hit[1])
                if port and port.stream == source_port.stream:
                    target = candidate; break

        if target is not None and target is not source:
            local_pos = target.mapFromScene(scene_pos)
            target_hit = target.port_at(local_pos)

            if target_hit is not None and target_hit[0] == "input":
                target_port_key = target_hit[1]
                target_port = target.port_spec("input", target_port_key)

                # Stream types are strict. Exec, Arg and Prompt never silently
                # coerce into one another.
                compatible = (
                    target_port is not None
                    and target_port.stream == source_port.stream
                    and source.connection_error(source_port_key, target, target_port_key) is None
                )

                duplicate = any(
                    isinstance(item, PipelineEdgeItem)
                    and item.source is source
                    and item.source_port == source_port_key
                    and item.target is target
                    and item.target_port == target_port_key
                    for item in self.canvas_scene.items()
                )

                # Arg/Prompt inputs have one producer. Exec inputs intentionally
                # allow unlimited fan-in so Loop/Router paths can converge on the
                # same next node. Exec fan-out is still forbidden per output port.
                occupied = (
                    target_port is not None
                    and target_port.stream != STREAM_EXEC
                    and bool(target.incoming_edges(target_port_key))
                )
                source_occupied = (
                    source_port.stream == STREAM_EXEC
                    and bool(source.outgoing_edges(source_port_key))
                )

                if compatible and not duplicate and not occupied and not source_occupied:
                    edge = PipelineEdgeItem(
                        source,
                        source_port_key,
                        target,
                        target_port_key,
                    )
                    self._retain_pipeline_edge(edge)
                    self.undo_stack.push(
                        AddPipelineEdgeCommand(self.canvas_scene, edge)
                    )
                    source.normalize_dynamic_ports()
                    target.normalize_dynamic_ports()
                    self.modified.emit()
                    self._emit_pipeline_selection()

        self._cancel_pipeline_connection()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._move_endpoint_grab(event):
            return
        if self._pipeline_source_node is not None:
            if not (event.buttons() & Qt.LeftButton):
                self._cancel_pipeline_connection()
                event.accept()
                return
            self.update_pipeline_connection(self.mapToScene(event.pos()))
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._finish_endpoint_grab(event):
            return
        if (
            self._pipeline_source_node is not None
            and event.button() == Qt.LeftButton
        ):
            self.finish_pipeline_connection(self.mapToScene(event.pos()))
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _cancel_pipeline_connection(self):
        if self._temp_line_item is not None:
            try:
                if self._temp_line_item.scene() is self.canvas_scene:
                    self.canvas_scene.removeItem(self._temp_line_item)
            except RuntimeError:
                pass
            self._temp_line_item = None

        self._pipeline_source_node = None
        self._pipeline_source_direction = None
        self._pipeline_source_port = None
        self._connecting_card = None
        self._connecting_side = None

    def mousePressEvent(self, event: QMouseEvent):
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self.copy_selected_cards()
            event.accept()
            return
        if event.matches(QKeySequence.Paste):
            self.paste_cards()
            event.accept()
            return
        if event.matches(QKeySequence.Undo):
            self.undo_stack.undo()
            event.accept()
            return
        if event.matches(QKeySequence.Redo):
            self.undo_stack.redo()
            event.accept()
            return
        if event.key() == Qt.Key_Delete:
            self._delete_selected_cards()
            event.accept()
            return

        super().keyPressEvent(event)

    def copy_selected_cards(self):
        selected_nodes = [
            item
            for item in self.canvas_scene.selectedItems()
            if isinstance(item, PipelineNodeItem) and not item.is_protected
        ]
        if not selected_nodes:
            return

        for parent in list(selected_nodes):
            if isinstance(parent, ParallelNodeItem):
                selected_nodes.extend(child for child in parent.children() if child not in selected_nodes)
        selected_ids = {node.node_id for node in selected_nodes}

        nodes_payload = []
        for node in selected_nodes:
            nodes_payload.append(
                {
                    "source_id": node.node_id,
                    "type_id": node.spec.type_id,
                    "x": node.scenePos().x(),
                    "y": node.scenePos().y(),
                    "settings": node.serialized_settings(),
                }
            )

        edges_payload = []
        for item in self.canvas_scene.items():
            if not isinstance(item, PipelineEdgeItem):
                continue
            if (
                item.source.node_id in selected_ids
                and item.target.node_id in selected_ids
            ):
                edges_payload.append(
                    {
                        "source_id": item.source.node_id,
                        "source_port": item.source_port,
                        "target_id": item.target.node_id,
                        "target_port": item.target_port,
                    }
                )

        payload = {
            "version": 2,
            "nodes": nodes_payload,
            "edges": edges_payload,
        }
        self._pipeline_clipboard = payload
        self._paste_generation = 0

        try:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            mime = QMimeData()
            mime.setData(PIPELINE_CLIPBOARD_MIME, QByteArray(encoded))

            # Keep ordinary clipboard viewers readable without exposing the
            # internal JSON format.
            labels = [node_caption(node.spec) for node in selected_nodes]
            mime.setText("\n".join(labels))
            QGuiApplication.clipboard().setMimeData(mime)
        except Exception:
            # Internal clipboard remains a complete fallback.
            pass

    def paste_cards(self):
        payload = None

        try:
            mime = QGuiApplication.clipboard().mimeData()
            if mime is not None and mime.hasFormat(PIPELINE_CLIPBOARD_MIME):
                raw = bytes(mime.data(PIPELINE_CLIPBOARD_MIME))
                decoded = json.loads(raw.decode("utf-8"))
                if isinstance(decoded, dict) and decoded.get("nodes"):
                    payload = decoded
        except Exception:
            payload = None

        if payload is None:
            payload = self._pipeline_clipboard

        if not payload:
            return

        self._refresh_node_library()
        by_type = {spec.type_id: spec for spec in self._library_specs}
        nodes_payload = list(payload.get("nodes") or [])
        if not nodes_payload:
            return

        min_x = min(float(raw.get("x") or 0.0) for raw in nodes_payload)
        min_y = min(float(raw.get("y") or 0.0) for raw in nodes_payload)

        # Same placement rule as the ordinary Board canvas: paste the copied
        # group under the current mouse cursor while preserving the relative
        # arrangement of all nodes.
        target_pos = None
        vp_pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(vp_pos):
            target_pos = self.mapToScene(vp_pos)

        if target_pos is not None:
            offset_x = target_pos.x() - min_x
            offset_y = target_pos.y() - min_y
        else:
            # Cursor is outside this canvas: keep Board's fallback and place a
            # copy two grid cells away from the original selection.
            offset_x = GRID_SIZE * 2
            offset_y = GRID_SIZE * 2

        if self.snap_to_grid:
            offset_x = round(offset_x / GRID_SIZE) * GRID_SIZE
            offset_y = round(offset_y / GRID_SIZE) * GRID_SIZE

        created: Dict[str, PipelineNodeItem] = {}

        self.undo_stack.beginMacro("Paste pipeline nodes")
        try:
            for raw in nodes_payload:
                spec = by_type.get(str(raw.get("type_id") or ""))
                if spec is None:
                    continue
                if bool(spec.metadata.get("singleton")) and any(
                    isinstance(item, PipelineNodeItem) and item.spec.type_id == spec.type_id
                    for item in self.canvas_scene.items()
                ):
                    continue

                node = make_node(
                    spec,
                    float(raw.get("x") or 0.0) + offset_x,
                    float(raw.get("y") or 0.0) + offset_y,
                    settings=dict(raw.get("settings") or {}),
                )
                self._retain_pipeline_node(node)
                self.undo_stack.push(
                    AddPipelineNodeCommand(self.canvas_scene, node)
                )
                created[str(raw.get("source_id") or "")] = node

            for raw in nodes_payload:
                node = created.get(str(raw.get('source_id') or ''))
                if node is None: continue
                old_parent = node.settings.get('_parallel_parent')
                if old_parent:
                    if old_parent in created: node.settings['_parallel_parent'] = created[old_parent].node_id
                    else: node.settings.pop('_parallel_parent', None)
                node.sync_execution_ports()
            for raw in list(payload.get("edges") or []):
                source = created.get(str(raw.get("source_id") or ""))
                target = created.get(str(raw.get("target_id") or ""))
                if source is None or target is None:
                    continue

                source_port = str(raw.get("source_port") or "")
                target_port = str(raw.get("target_port") or "")

                # Backward compatibility with v1 clipboard payloads that stored
                # integer port indexes.
                if source_port.isdigit():
                    idx = int(source_port)
                    if idx >= len(source.output_ports):
                        continue
                    source_port = source.output_ports[idx].key
                if target_port.isdigit():
                    idx = int(target_port)
                    if idx >= len(target.input_ports):
                        continue
                    target_port = target.input_ports[idx].key

                source_spec = source.port_spec("output", source_port)
                target_spec = target.port_spec("input", target_port)
                if source_spec is None or target_spec is None:
                    continue
                if source_spec.stream != target_spec.stream:
                    continue
                if source.connection_error(source_port, target, target_port):
                    continue
                if (
                    target_spec.stream != STREAM_EXEC
                    and target.incoming_edges(target_port)
                ):
                    continue
                if (
                    source_spec.stream == STREAM_EXEC
                    and source.outgoing_edges(source_port)
                ):
                    # Keep the same single-flow rule as an interactive
                    # connection: every individual Exec output can select only
                    # one next step. Loop is allowed two named Exec outputs, but
                    # each one is still single-target.
                    continue

                edge = PipelineEdgeItem(
                    source,
                    source_port,
                    target,
                    target_port,
                )
                self._retain_pipeline_edge(edge)
                self.undo_stack.push(
                    AddPipelineEdgeCommand(self.canvas_scene, edge)
                )
        finally:
            self.undo_stack.endMacro()

        if created:
            self.modified.emit()

    def _delete_selected_cards(self):
        selected = list(self.canvas_scene.selectedItems())
        nodes = [
            item
            for item in selected
            if isinstance(item, PipelineNodeItem) and not item.is_protected
        ]
        for parent in list(nodes):
            if isinstance(parent, ParallelNodeItem):
                nodes.extend(child for child in parent.children() if child not in nodes)
        edges_to_delete = {
            item for item in selected if isinstance(item, PipelineEdgeItem)
        }

        for node in nodes:
            edges_to_delete.update(node.edges)

        if not nodes and not edges_to_delete:
            return

        self.undo_stack.push(
            DeletePipelineSelectionCommand(
                self.canvas_scene,
                nodes,
                list(edges_to_delete),
            )
        )
        self.modified.emit()
        self._emit_pipeline_selection()

    def build_pipeline_definition(self, name: str) -> Pipeline:
        """Return the logical graph only; canvas coordinates are deliberately omitted."""
        pipeline = Pipeline(str(name or ""))
        nodes = sorted(
            (
                item
                for item in self.canvas_scene.items()
                if isinstance(item, PipelineNodeItem)
            ),
            key=lambda node: node.node_id,
        )
        refs = {}
        for node in nodes:
            refs[node.node_id] = pipeline.node(
                node.spec.type_id,
                node_id=node.node_id,
                settings=node.serialized_settings(),
            )

        edges = sorted(
            (
                item
                for item in self.canvas_scene.items()
                if isinstance(item, PipelineEdgeItem)
            ),
            key=lambda edge: (
                edge.source.node_id,
                edge.source_port,
                edge.target.node_id,
                edge.target_port,
            ),
        )
        for edge in edges:
            source = refs.get(edge.source.node_id)
            target = refs.get(edge.target.node_id)
            if source is None or target is None:
                continue
            pipeline.connect(
                source,
                edge.source_port,
                target,
                edge.target_port,
            )
        return pipeline

    def layout_payload(self) -> Dict[str, object]:
        center = self.mapToScene(self.viewport().rect().center())
        nodes = {}
        for node in self.canvas_scene.items():
            if not isinstance(node, PipelineNodeItem):
                continue
            payload = {
                "x": float(node.scenePos().x()),
                "y": float(node.scenePos().y()),
            }
            if node.supports_resizable_content_field():
                payload["content_rows"] = int(node.content_row_count())
            nodes[node.node_id] = payload
        return {
            "nodes": nodes,
            "viewport": {
                "zoom": float(self.transform().m11()),
                "center": {"x": float(center.x()), "y": float(center.y())},
            },
        }

    def _clear_pipeline_items(self):
        self.warning_manager.clear()
        self._cancel_pipeline_connection()
        for item in list(self.canvas_scene.items()):
            if isinstance(item, PipelineEdgeItem):
                item.detach()
                self.canvas_scene.removeItem(item)
        for item in list(self.canvas_scene.items()):
            if isinstance(item, PipelineNodeItem):
                self.canvas_scene.removeItem(item)
        self._pipeline_edge_refs.clear()
        self._pipeline_node_refs.clear()
        self.canvas_scene.clearSelection()
        self.node_selection_changed.emit(None)

    @staticmethod
    def _ensure_saved_target_port(node: PipelineNodeItem, port_key: str):
        if node.port_spec("input", port_key) is not None:
            return
        if node.is_executable and str(port_key).startswith("prompt:"):
            try:
                index = int(str(port_key).split(":", 1)[1])
            except (TypeError, ValueError):
                return
            node.set_prompt_input_count(max(node.prompt_input_count(), index), geometry=False)
        elif node.value_type == "prompt" and node._is_prompt_arg_port_key(str(port_key)):
            index = node._prompt_arg_port_index(str(port_key))
            if index > 0:
                node.set_prompt_arg_input_count(
                    max(node.prompt_arg_input_count(), index),
                    geometry=False,
                )
        elif node.is_array and str(port_key).startswith("array:"):
            try:
                index = int(str(port_key).split(":", 1)[1])
            except (TypeError, ValueError):
                return
            node.set_array_input_count(max(node.array_input_count(), index), geometry=False)
        elif node.is_graph_loop and str(port_key) == "arg:repeat":
            node.sync_loop_argument_port(geometry=False, force_visible=True)
        elif node.is_graph_router and str(port_key) == "arg:route":
            node.sync_router_argument_port(geometry=False, force_visible=True)

    @staticmethod
    def _ensure_saved_source_port(node: PipelineNodeItem, port_key: str):
        if node.port_spec("output", port_key) is not None:
            return
        if node.is_graph_router and str(port_key).startswith("exec:route:"):
            try:
                index = int(str(port_key).rsplit(":", 1)[1])
            except (TypeError, ValueError):
                return
            node.set_router_output_count(
                max(node.router_output_count(), index + 1),
                geometry=False,
            )

    def _auto_layout_all(self, nodes_by_id, pipeline: Pipeline):
        """Small deterministic layout. Layout is presentation only, never pipeline logic."""
        node_ids = list(nodes_by_id)
        if not node_ids:
            return

        outgoing = {node_id: [] for node_id in node_ids}
        indegree = {node_id: 0 for node_id in node_ids}
        for edge in pipeline.edges:
            if not (
                str(edge.source_port).startswith("exec:")
                and str(edge.target_port).startswith("exec:")
            ):
                continue
            if edge.source_id not in outgoing or edge.target_id not in indegree:
                continue
            outgoing[edge.source_id].append(edge.target_id)
            indegree[edge.target_id] += 1

        queue = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
        layer = {node_id: 0 for node_id in queue}
        remaining = dict(indegree)
        cursor = 0
        while cursor < len(queue):
            source_id = queue[cursor]
            cursor += 1
            for target_id in sorted(outgoing[source_id]):
                layer[target_id] = max(layer.get(target_id, 0), layer[source_id] + 1)
                remaining[target_id] -= 1
                if remaining[target_id] == 0:
                    queue.append(target_id)

        # Cycles have no topological root. Put any leftovers in deterministic
        # layers after the acyclic part instead of trying to infer semantics.
        next_layer = max(layer.values(), default=-1) + 1
        for node_id in sorted(node_ids):
            if node_id not in layer:
                layer[node_id] = next_layer
                next_layer += 1

        buckets: Dict[int, list] = {}
        for node_id, layer_index in layer.items():
            buckets.setdefault(layer_index, []).append(node_id)

        x_step = float(GRID_SIZE * 14)
        y_step = float(GRID_SIZE * 9)
        for layer_index in sorted(buckets):
            for row, node_id in enumerate(sorted(buckets[layer_index])):
                nodes_by_id[node_id].setPos(layer_index * x_step, row * y_step)

    def load_pipeline_definition(
        self,
        pipeline: Pipeline,
        layout: Optional[Dict[str, object]] = None,
    ) -> list[str]:
        """Replace the canvas from a logical definition and optional visual layout."""
        from modules.pipelines.storage import remove_graph_inputs
        remove_graph_inputs(pipeline)
        self._clear_pipeline_items()
        self._refresh_node_library()
        by_type = {spec.type_id: spec for spec in self._library_specs}
        layout = dict(layout or {})
        raw_positions = layout.get("nodes")
        raw_positions = raw_positions if isinstance(raw_positions, dict) else {}

        errors = []
        nodes_by_id: Dict[str, PipelineNodeItem] = {}
        positioned = set()
        seen_singletons = set()

        for definition in pipeline.nodes:
            spec = by_type.get(definition.type_id)
            if spec is None:
                errors.append(f"Unknown node type: {definition.type_id}")
                continue
            if bool(spec.metadata.get("singleton")):
                if spec.type_id in seen_singletons:
                    errors.append(f"Duplicate singleton node type: {spec.type_id}")
                    continue
                seen_singletons.add(spec.type_id)

            x = 0.0
            y = 0.0
            raw_pos = raw_positions.get(definition.node_id)
            if isinstance(raw_pos, dict):
                try:
                    x = float(raw_pos.get("x", 0.0))
                    y = float(raw_pos.get("y", 0.0))
                    positioned.add(definition.node_id)
                except (TypeError, ValueError):
                    x = y = 0.0

            settings = initial_node_settings(spec)
            if '_error_policy' not in definition.settings:
                settings.pop('_error_policy', None)  # Loading never changes legacy semantics.
            settings.update(dict(definition.settings or {}))
            node = make_node(
                spec,
                x,
                y,
                settings=settings,
                node_id=definition.node_id,
            )
            if isinstance(raw_pos, dict) and node.supports_resizable_content_field():
                try:
                    node.set_content_row_count(int(raw_pos.get("content_rows", 3)), geometry=False)
                except (TypeError, ValueError):
                    pass
            self.canvas_scene.addItem(node)
            self._retain_pipeline_node(node)
            nodes_by_id[definition.node_id] = node

        from modules.pipelines.execution import parallel_owners
        try: owned = parallel_owners(pipeline)
        except ValueError as error: errors.append(str(error)); owned = {}
        for nid, node in nodes_by_id.items():
            node._parallel_owner = owned.get(nid) or node.settings.get('_parallel_parent')
            node._parallel_chain = nid in owned and not node.settings.get('_parallel_parent')
            node.sync_execution_ports()
        for definition in pipeline.edges:
            source = nodes_by_id.get(definition.source_id)
            target = nodes_by_id.get(definition.target_id)
            if source is None or target is None:
                continue

            self._ensure_saved_source_port(source, definition.source_port)
            self._ensure_saved_target_port(target, definition.target_port)
            source_port = source.port_spec("output", definition.source_port)
            target_port = target.port_spec("input", definition.target_port)
            if source_port is None or target_port is None:
                errors.append(
                    "Missing port: "
                    f"{definition.source_id}.{definition.source_port} -> "
                    f"{definition.target_id}.{definition.target_port}"
                )
                continue
            if source_port.stream != target_port.stream:
                errors.append(
                    "Incompatible streams: "
                    f"{definition.source_id}.{definition.source_port} -> "
                    f"{definition.target_id}.{definition.target_port}"
                )
                continue
            issue = source.connection_error(definition.source_port, target, definition.target_port)
            if issue:
                errors.append(f'{definition.source_id}.{definition.source_port}: {issue}')
                continue
            if (
                target_port.stream != STREAM_EXEC
                and target.incoming_edges(definition.target_port)
            ):
                errors.append(
                    "Occupied input: "
                    f"{definition.target_id}.{definition.target_port}"
                )
                continue
            if source_port.stream == STREAM_EXEC and source.outgoing_edges(definition.source_port):
                errors.append(
                    "Exec fan-out is disabled: "
                    f"{definition.source_id}.{definition.source_port} has more than one target"
                )
                continue

            edge = PipelineEdgeItem(
                source,
                definition.source_port,
                target,
                definition.target_port,
            )
            self.canvas_scene.addItem(edge)
            self._retain_pipeline_edge(edge)

        for node in nodes_by_id.values():
            node.normalize_dynamic_ports()

        missing = [node_id for node_id in nodes_by_id if node_id not in positioned]
        if nodes_by_id and len(missing) == len(nodes_by_id):
            self._auto_layout_all(nodes_by_id, pipeline)
        elif missing:
            existing_x = [
                nodes_by_id[node_id].pos().x()
                for node_id in positioned
                if node_id in nodes_by_id
            ]
            remaining_missing = list(missing)
            # Migrated Graph Start belongs on the left of an already laid-out
            # graph rather than being treated like a newly appended node.
            for node_id in list(remaining_missing):
                node = nodes_by_id.get(node_id)
                if node is not None and node.spec.type_id == GRAPH_START_TYPE:
                    left_x = (min(existing_x) if existing_x else 0.0) - float(GRID_SIZE * 14)
                    node.setPos(left_x, 0.0)
                    remaining_missing.remove(node_id)
            start_x = (max(existing_x) if existing_x else 0.0) + float(GRID_SIZE * 14)
            for row, node_id in enumerate(sorted(remaining_missing)):
                nodes_by_id[node_id].setPos(start_x, row * float(GRID_SIZE * 9))

        self.refresh_parallel_groups()
        viewport = layout.get("viewport")
        restored_view = False
        if isinstance(viewport, dict):
            center = viewport.get("center")
            try:
                zoom = float(viewport.get("zoom", 1.0))
                center_x = float(center.get("x", 0.0)) if isinstance(center, dict) else 0.0
                center_y = float(center.get("y", 0.0)) if isinstance(center, dict) else 0.0
                zoom = max(0.05, min(8.0, zoom))
                self.resetTransform()
                self.scale(zoom, zoom)
                self._current_zoom = zoom
                self.centerOn(center_x, center_y)
                restored_view = True
            except (TypeError, ValueError):
                restored_view = False

        if not restored_view:
            self.reset_view()

        self.undo_stack.clear()
        self._emit_pipeline_selection()
        return errors

    def wheelEvent(self, event):
        # Do not zoom the view while an edge endpoint is attached to the mouse.
        # QGraphicsView's AnchorUnderMouse changes the scene mapping during the
        # drag, which makes the temporary connection jump violently.
        if self._pipeline_source_node is not None:
            event.accept()
            return
        super().wheelEvent(event)
        self.camera_changed.emit()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        # CanvasView uses a double click on empty space to create a generic
        # text card. Pipeline editor has no such card type.
        if event.button() == Qt.LeftButton and self.itemAt(event.pos()) is None:
            event.accept()
            return

        # Bypass CanvasView.mouseDoubleClickEvent so pipeline interactions do
        # not accidentally invoke generic board-card creation.
        QGraphicsView.mouseDoubleClickEvent(self, event)

    def contextMenuEvent(self, event):
        node = self._node_under_view_pos(event.pos())
        if node is not None:
            if not node.isSelected():
                self.canvas_scene.clearSelection()
                node.setSelected(True)

            # Graph Start is a structural singleton. It may be moved and have
            # its edge rerouted, but it cannot be copied or deleted.
            if node.is_protected:
                return

            menu = QMenu(self)
            menu.setObjectName("PipelineNodeContextMenu")
            copy_action = localize(add_caption_action(menu, 'addAction', "Copy"), 'ui.workspace.copy_e21f')
            menu.addSeparator()
            delete_action = localize(add_caption_action(menu, 'addAction', "Delete"), 'ui.workspace.delete_e2d0')
            chosen = standardize_workspace_menu(menu).exec(event.globalPos())
            if chosen == copy_action:
                self.copy_selected_cards()
            elif chosen == delete_action:
                self._delete_selected_cards()
            return

        self._pending_library_pos = self.mapToScene(event.pos())
        self._refresh_node_library()
        menu = NodeLibraryMenu(
            self._library_specs,
            lambda spec: self.create_pipeline_node(spec, self._pending_library_pos),
            self,
        )
        standardize_workspace_menu(menu).exec(event.globalPos())

class PipelineDocumentTab(QWidget):
    """One persistent pipeline directory: pipeline.py + layout.json."""

    dirty_state_changed = Signal(bool)

    def __init__(self, pipeline_dir: Path, parent=None):
        super().__init__(parent)
        self.pipeline_dir = Path(pipeline_dir).resolve()
        from modules.pipelines.management import package_owner
        self.module_id, self.pipeline_id = package_owner(self.pipeline_dir)
        self.script_path = pipeline_script_path(self.pipeline_dir)
        self.layout_path = pipeline_layout_path(self.pipeline_dir)
        self.is_dirty = False
        self._load_ok = False
        self._suspend_autosave = True
        self._clean_document_state = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        path_bar = QFrame(self)
        path_bar.setObjectName('FileDocToolbar')
        path_bar.setFixedHeight(22)
        path_layout = QHBoxLayout(path_bar)
        path_layout.setContentsMargins(16, 2, 8, 2)
        self.path_label = QLabel(path_bar)
        self.path_label.setObjectName('FileDocBreadcrumb')
        show_document_path(self.path_label, self.script_path)
        path_layout.addWidget(self.path_label)
        root.addWidget(path_bar)

        self.error_bar = QLabel("", self)
        self.error_bar.setObjectName("PipelineLoadErrorBar")
        self.error_bar.setWordWrap(True)
        self.error_bar.setContentsMargins(12, 6, 12, 6)
        self.error_bar.hide()
        root.addWidget(self.error_bar)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setObjectName("ModulesPipelineSplitter")
        self.splitter.setChildrenCollapsible(False)

        self.canvas = PipelineCanvasView(self.splitter)
        self.canvas.pipeline_dir = self.pipeline_dir
        self.inspector = PipelineInspector(self)
        self.canvas.installEventFilter(self)
        self.canvas.node_selection_changed.connect(self._set_inspector_node)
        self.canvas.node_settings_changed.connect(self._refresh_changed_settings)
        self.canvas.modified.connect(self._on_modified)
        self.canvas.layout_changed.connect(self._on_modified)
        self.canvas.camera_changed.connect(self._on_camera_modified)
        self.inspector.modified.connect(self._on_modified)
        self.inspector.modified.connect(self._refresh_inspected_preview)

        self.splitter.addWidget(self.canvas)
        self.splitter.setStretchFactor(0, 1)
        root.addWidget(self.splitter, 1)

        self._dirty_compare_timer = QTimer(self)
        self._dirty_compare_timer.setSingleShot(True)
        self._dirty_compare_timer.setInterval(120)
        self._dirty_compare_timer.timeout.connect(self._compare_dirty_state)
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.setInterval(650)
        self._auto_save_timer.timeout.connect(self.save_pipeline)
        self._camera_save_timer = QTimer(self)
        self._camera_save_timer.setSingleShot(True)
        self._camera_save_timer.setInterval(650)
        self._camera_save_timer.timeout.connect(self._save_camera_layout)

        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_pipeline)

        self.load_pipeline()
        self._suspend_autosave = False

    def _refresh_inspected_preview(self):
        node = self.inspector._node
        if node is None: return
        node.update()
        if isinstance(node.parentItem(), ParallelNodeItem): node.parentItem().arrange()
        for edge in node.edges:
            if edge.source is node: edge.target.update()

    def _refresh_changed_settings(self, node):
        if self.inspector._node is node:self.inspector.set_node(node)

    def _position_inspector(self):
        origin = self.canvas.mapTo(self, self.canvas.rect().topLeft())
        width = max(self.inspector.minimumWidth(), min(320, self.canvas.width()))
        self.inspector.setGeometry(origin.x() + self.canvas.width() - width,
            origin.y(), width, self.canvas.height())
        if self.inspector.isVisible():
            self.inspector.raise_()

    def _set_inspector_node(self, node):
        self.inspector.set_node(node)
        self._position_inspector()

    def eventFilter(self, watched, event):
        if watched is self.canvas and event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show):
            self._position_inspector()
        return super().eventFilter(watched, event)

    @property
    def pipeline_name(self) -> str:
        if self.module_id is not None:
            return getattr(self, "_loaded_name", self.pipeline_dir.name)
        return self.pipeline_dir.name

    def _set_error(self, message: str):
        message = str(message or "").strip()
        bind_caption(self.error_bar, message)
        self.error_bar.setVisible(bool(message))

    def load_pipeline(self) -> bool:
        self._suspend_autosave = True
        self._auto_save_timer.stop()
        try:
            pipeline = load_pipeline_script(self.script_path)
            self._loaded_name = pipeline.name
            layout = load_layout(self.layout_path)
            errors = self.canvas.load_pipeline_definition(pipeline, layout)
            if errors:
                self._load_ok = False
                self._set_error(
                    "Pipeline loaded incompletely; autosave is disabled to avoid data loss.\n"
                    + "\n".join(errors)
                )
            else:
                self._load_ok = True
                self._set_error("")
            self._clean_document_state = self._document_state() if self._load_ok else None
            self.is_dirty = False
            self.dirty_state_changed.emit(False)
            return self._load_ok
        except PipelineLoadError as exc:
            self._load_ok = False
            self._set_error(str(exc))
            return False
        finally:
            self._suspend_autosave = False

    def _document_state(self):
        from modules.pipelines.storage import render_pipeline_script
        pipeline = self.canvas.build_pipeline_definition(self.pipeline_name)
        return (render_pipeline_script(pipeline), json.dumps(self.canvas.layout_payload()['nodes'], sort_keys=True))

    def _on_camera_modified(self):
        if self._load_ok and not self._suspend_autosave and self.isVisible():
            self._camera_save_timer.start()

    def _save_camera_layout(self):
        if self._load_ok and not self.is_dirty:
            try:
                layout = load_layout(self.layout_path)
                layout['viewport'] = self.canvas.layout_payload()['viewport']
                save_layout(self.layout_path, layout)
            except OSError as error:
                self._set_error(str(error))

    def _on_modified(self):
        if self._suspend_autosave or not self._load_ok: return
        if not self.is_dirty:
            self.is_dirty = True
            self.dirty_state_changed.emit(True)
        self._dirty_compare_timer.start()
        self._auto_save_timer.start()

    def _compare_dirty_state(self):
        if self._suspend_autosave or not self._load_ok: return
        dirty = self._document_state() != self._clean_document_state
        if self.is_dirty != dirty:
            self.is_dirty = dirty
            self.dirty_state_changed.emit(dirty)
        if not dirty: self._auto_save_timer.stop()

    def save_pipeline(self) -> bool:
        self._dirty_compare_timer.stop()
        self._auto_save_timer.stop()
        if not self._load_ok:
            return False
        try:
            pipeline = self.canvas.build_pipeline_definition(self.pipeline_name)
            save_pipeline_script(self.script_path, pipeline)
            save_layout(self.layout_path, self.canvas.layout_payload())
            if self.module_id is not None:
                from modules.pipelines.packages import PackageStore
                from modules.pipelines.contracts import dependencies
                store = PackageStore()
                mid = self.module_id
                data = store.read(mid)
                pid = self.pipeline_id
                if pid in data.get("pipelines", {}):
                    data["pipelines"][pid]["dependencies"] = [x for x in dependencies(pipeline) if x != mid]
                    data["dependencies"] = sorted({d for x in data["pipelines"].values() for d in x.get("dependencies", [])})
                    store.write(mid, data)
        except Exception as exc:
            self._set_error(f"Failed to save pipeline: {exc}")
            return False

        self._clean_document_state = self._document_state()
        self.is_dirty = False
        self.dirty_state_changed.emit(False)
        self._set_error("")
        return True

    def closeEvent(self, event):
        if self.is_dirty and self._load_ok:
            self.save_pipeline()
        elif self._camera_save_timer.isActive():
            self._camera_save_timer.stop()
            self._save_camera_layout()
        super().closeEvent(event)


class PipelineTabBar(FileTabBar):
    """Same tab UX as the board, but a rename targets the pipeline directory."""

    rename_requested = Signal(object, str)

    def _start_tab_rename(self, index: int):
        document = self.tabData(index)
        if not isinstance(document, (PipelineDocumentTab, SuspendedTab)):
            return

        old_name = document.pipeline_name
        rect = self.tabRect(index).adjusted(6, 3, -24, -3)
        editor = TabRenameLineEdit(old_name, self)
        self._rename_editor = editor
        editor.setGeometry(rect)
        editor.show()
        editor.setFocus()
        editor.selectAll()

        def on_finished(new_name: str):
            new_name = str(new_name or "").strip()
            if new_name and new_name != old_name:
                self.rename_requested.emit(document, new_name)

        editor.editing_finished.connect(on_finished)


class ModulesView(QWidget):
    """Persistent pipeline editor. Every pipeline directory is kept open as a tab."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ModulesPipelineView")
        self.pipeline_root = ensure_pipeline_root(APP_PATHS.modules / "pipelines")
        self.open_pipelines: Dict[str, PipelineDocumentTab] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header_bar = QFrame(self)
        self.header_bar.setObjectName("FileEditorHeaderBar")
        self.header_bar.setFixedHeight(32)
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(0, 0, 8, 0)
        header_layout.setSpacing(6)
        header_layout.setAlignment(Qt.AlignVCenter)

        self.tab_bar = PipelineTabBar(self.header_bar)
        self.tab_bar.currentChanged.connect(self._on_current_tab_changed)
        self.tab_bar.rename_requested.connect(self._rename_pipeline)
        header_layout.addWidget(self.tab_bar)

        self.new_tab_btn = NewTabButton(self.header_bar)
        set_text(self.new_tab_btn, 'ui.workspace.create_new_pipeline_726d', setter='setToolTip')
        self.new_tab_btn.menu_requested.connect(self._create_new_pipeline)
        header_layout.addWidget(self.new_tab_btn)
        header_layout.addStretch(1)
        root.addWidget(self.header_bar)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("ModulesPipelineStack")

        self.no_pipelines_frame = QFrame(self)
        self.no_pipelines_frame.setObjectName("NoFilesPlaceholderFrame")
        empty_layout = QVBoxLayout(self.no_pipelines_frame)
        empty_layout.setAlignment(Qt.AlignCenter)
        empty_layout.setSpacing(12)

        empty_label = localize(QLabel(
            '',
            self.no_pipelines_frame,
        ), 'ui.workspace.no_pipelines_yet_click_to_create_2a45')
        empty_label.setObjectName("NoFilesPlaceholderLabel")
        empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_label)

        empty_button = QToolButton(self.no_pipelines_frame)
        empty_button.setObjectName("PlaceholderNewTabButton")
        symbol_button(empty_button, "+")
        empty_button.setFixedSize(28, 28)
        empty_button.setCursor(Qt.PointingHandCursor)
        set_text(empty_button, 'ui.workspace.create_new_pipeline_726d', setter='setToolTip')
        empty_button.clicked.connect(self._create_new_pipeline)
        empty_layout.addWidget(empty_button, 0, Qt.AlignCenter)

        self.stack.addWidget(self.no_pipelines_frame)
        root.addWidget(self.stack, 1)

        self.tab_memory = TabMemoryManager(
            self, "pipeline", self.stack, self.open_pipelines, PipelineDocumentTab, self._bind_document
        )
        self._load_all_pipelines()

    def _bind_document(self, document):
        document.dirty_state_changed.connect(
            lambda dirty, d=document: self._update_tab_title_for_document(d, dirty))

    @property
    def canvas(self) -> Optional[PipelineCanvasView]:
        document = self.current_document()
        return document.canvas if document is not None else None

    def current_document(self) -> Optional[PipelineDocumentTab]:
        index = self.tab_bar.currentIndex()
        if index < 0 or index >= self.tab_bar.count():
            return None
        document = self.tab_bar.tabData(index)
        return document if isinstance(document, PipelineDocumentTab) else None

    def _pipeline_dirs(self):
        try:
            entries = sorted(self.pipeline_root.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            return []
        return [
            path
            for path in entries
            if path.is_dir() and pipeline_script_path(path).is_file()
        ]

    def _load_all_pipelines(self):
        for pipeline_dir in self._pipeline_dirs():
            self._open_pipeline(pipeline_dir, make_current=False)

        if self.tab_bar.count() > 0:
            self.tab_bar.setCurrentIndex(0)
            self._on_current_tab_changed(0)
        else:
            self.stack.setCurrentWidget(self.no_pipelines_frame)

    def _open_pipeline(self, pipeline_dir: Path, *, make_current: bool = True):
        from ui.startup_process import report_startup_phase
        report_startup_phase(tr('ui.caption.a2b64b54087a') + Path(pipeline_dir).name)
        pipeline_dir = Path(pipeline_dir).resolve()
        key = str(pipeline_dir)
        existing = self.open_pipelines.get(key)
        if existing is not None:
            if make_current:
                for index in range(self.tab_bar.count()):
                    if self.tab_bar.tabData(index) is existing:
                        self.tab_bar.setCurrentIndex(index)
                        break
            return self.tab_memory.ensure_loaded(existing) if make_current else existing

        document = PipelineDocumentTab(pipeline_dir, self)
        self.tab_memory.bind_document(document)
        self.open_pipelines[key] = document
        self.stack.addWidget(document)

        index = self.tab_bar.addTab(pipeline_dir.name)
        self.tab_bar.setTabData(index, document)
        self._attach_delete_button(index, pipeline_dir)
        self.tab_bar.updateGeometry()

        if make_current:
            self.tab_bar.setCurrentIndex(index)
            self.stack.setCurrentWidget(document)
            self.tab_memory.touch(document)
        else:
            self.tab_memory.schedule()
        return document

    def _attach_delete_button(self, index: int, pipeline_dir: Path):
        button = TabCloseButton(self.tab_bar)
        set_text(button, 'ui.module_view.delete_pipeline_1530', setter='setToolTip')
        button.clicked.connect(
            lambda _checked=False, p=Path(pipeline_dir): self._delete_pipeline(p)
        )
        self.tab_bar.setTabButton(index, QTabBar.RightSide, button)

    def _on_current_tab_changed(self, index: int):
        if index < 0 or index >= self.tab_bar.count():
            self.stack.setCurrentWidget(self.no_pipelines_frame)
            return
        document = self.tab_bar.tabData(index)
        if isinstance(document, SuspendedTab):
            document = self.tab_memory.ensure_loaded(document)
        if isinstance(document, PipelineDocumentTab):
            self.stack.setCurrentWidget(document)
            self.tab_memory.touch(document)

    def _next_pipeline_dir(self) -> Path:
        index = 1
        while True:
            candidate = self.pipeline_root / f"Untitled_{index}"
            if not candidate.exists():
                return candidate
            index += 1

    def _create_new_pipeline(self):
        pipeline_dir = self._next_pipeline_dir()
        try:
            create_empty_pipeline(pipeline_dir, pipeline_dir.name)
        except Exception as exc:
            inline.notice(self, tr('ui.module_view.pipeline_37e1'), tr('ui.workspace.could_not_create_pipeline_v0_6f47', v0=exc))
            return
        self._open_pipeline(pipeline_dir, make_current=True)

    def _rename_pipeline(self, document: PipelineDocumentTab, new_name: str):
        document = self.tab_memory.ensure_loaded(document)
        new_name = str(new_name or "").strip()
        if (
            not new_name
            or new_name in {".", ".."}
            or "/" in new_name
            or "\\" in new_name
        ):
            inline.notice(self, tr('ui.module_view.pipeline_37e1'), tr('ui.workspace.invalid_pipeline_name_6007'))
            return

        old_dir = document.pipeline_dir
        new_dir = (self.pipeline_root / new_name).resolve()
        if new_dir == old_dir:
            return
        if new_dir.exists():
            inline.notice(self, tr('ui.module_view.pipeline_37e1'), tr('ui.workspace.a_pipeline_with_this_name_already_9ca7'))
            return

        if document.is_dirty and not document.save_pipeline():
            return

        try:
            old_dir.rename(new_dir)
        except Exception as exc:
            inline.notice(self, tr('ui.module_view.pipeline_37e1'), tr('ui.workspace.could_not_rename_pipeline_v0_8198', v0=exc))
            return

        old_key = str(old_dir)
        new_key = str(new_dir)
        self.open_pipelines.pop(old_key, None)
        self.open_pipelines[new_key] = document
        document.pipeline_dir = new_dir
        document.canvas.pipeline_dir = new_dir
        document.script_path = pipeline_script_path(new_dir)
        show_document_path(document.path_label, document.script_path)
        document.layout_path = pipeline_layout_path(new_dir)

        for index in range(self.tab_bar.count()):
            if self.tab_bar.tabData(index) is document:
                bind_caption(self.tab_bar, new_name, setter='setTabText', index=index)
                self._attach_delete_button(index, new_dir)
                break

        # Keep the canonical script name in sync with the directory name.
        document.save_pipeline()

    def _delete_pipeline(self, pipeline_dir: Path):
        raise NotImplementedError('Resource deletion is handled by the pipeline manager')

    def _update_tab_title_for_document(
        self,
        document: PipelineDocumentTab,
        dirty: bool,
    ):
        for index in range(self.tab_bar.count()):
            if self.tab_bar.tabData(index) is document:
                title = document.pipeline_name
                bind_caption(self.tab_bar, f"* {title}" if dirty else title, setter='setTabText', index=index)
                break

    def focus_canvas(self):
        canvas = self.canvas
        if canvas is not None:
            canvas.setFocus(Qt.OtherFocusReason)

    def closeEvent(self, event):
        for document in list(self.open_pipelines.values()):
            if document.is_dirty and document._load_ok:
                document.save_pipeline()
        super().closeEvent(event)

# Keep the existing canvas/document implementation; add resource management around it.
from PySide6.QtWidgets import QPushButton
from ui.widgets import inline_editor as inline
from modules.pipelines.packages import PackageStore, MODES
from ui.event_bus import event_bus
from ui.widgets.resource_list import ResourceList, ResourceItem
from modules.pipelines.management import PipelineLibrary, LibraryError, package_owner
from .pipeline_manager import PipelineManagerTree

_LegacyModulesView = ModulesView

class ModulesView(_LegacyModulesView):
    """Pipeline editor with persistent open tabs and an independent resource list."""
    def __init__(self, parent=None):
        self._restoring = True
        self.tab_state_path = APP_PATHS.runtime / 'pipeline_tabs.json'
        self.library = PipelineLibrary()
        super().__init__(parent)
        self.manager_btn = QToolButton(self.header_bar)
        self.manager_btn.setObjectName('PipelineManagerToggle')
        set_icon(self.manager_btn, 'menu-2', square=True)
        set_text(self.manager_btn, 'ui.workspace.pipeline_manager_e6ba', setter='setToolTip')
        self.manager_btn.setFixedSize(28, 28)
        self.manager_btn.setCursor(Qt.PointingHandCursor)
        self.header_bar.layout().insertWidget(0, self.manager_btn)
        self.manager = QFrame(self.stack)
        self.manager.setObjectName('PipelineManagerPanel')
        self.manager.setAutoFillBackground(True)
        apply_style(self.manager, 'QFrame#PipelineManagerPanel { background: @background.panel@; border: 1px solid @border.subtle@; }')
        ml = QVBoxLayout(self.manager)
        ml.setContentsMargins(6, 6, 6, 6)
        ml.setSpacing(6)
        manager_header = QHBoxLayout()
        label = localize(QLabel(''), 'ui.workspace.pipeline_manager_e6ba')
        label.setObjectName('FileManagerSessionLabel')
        manager_header.addWidget(label, 1)
        ml.addLayout(manager_header)
        self.pipeline_list = PipelineManagerTree(self.manager)
        self.pipeline_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.pipeline_list.open_requested.connect(self._open_pipeline)
        self.pipeline_list.create_requested.connect(self._create_new_pipeline)
        self.pipeline_list.move_requested.connect(self._move_pipeline)
        self.pipeline_list.customContextMenuRequested.connect(self._manager_menu)
        ml.addWidget(self.pipeline_list, 1)
        self.new_tab_btn.show()
        self.manager.setMinimumWidth(200)
        self.manager.setMaximumWidth(360)
        self.manager.hide()
        self.stack.installEventFilter(self)
        self.manager_btn.clicked.connect(self._toggle_manager)
        self.tab_bar.setMovable(False)
        self.tab_bar.tabMoved.connect(self._save_tab_state)
        event_bus.pipeline_open_requested.connect(self.open_module_pipeline)
        event_bus.pipeline_resources_changed.connect(self._refresh_resources)
        i18n.language_changed.connect(self.refresh_manager)
        self._restoring = False
        self.refresh_manager()
        self._save_tab_state()

    def _position_manager(self):
        if hasattr(self, 'manager'):
            width = min(300, self.stack.width())
            self.manager.setGeometry(0, 0, width, self.stack.height())
            if self.manager.isVisible():
                self.manager.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_manager()

    def eventFilter(self, watched, event):
        if watched is self.stack and event.type() == QEvent.Resize:
            self._position_manager()
        return super().eventFilter(watched, event)

    def _toggle_manager(self):
        self.manager.setVisible(not self.manager.isVisible())
        self._position_manager()

    def _load_all_pipelines(self):
        state = load_layout(self.tab_state_path)
        paths = state.get('open', [])
        from core.plugins.modules import resolve_module_reference
        for relative in paths:
            path = resolve_module_reference(relative)
            if path is not None and (path / 'pipeline.py').is_file():
                self._open_pipeline(path, make_current=False)
        active = state.get('active')
        for i in range(self.tab_bar.count()):
            d = self.tab_bar.tabData(i)
            if self._relative(d.pipeline_dir) == active:
                self.tab_bar.setCurrentIndex(i)
                break
        self._on_current_tab_changed(self.tab_bar.currentIndex())

    def _relative(self, path):
        from core.plugins.modules import module_reference
        return module_reference(path)

    def _save_tab_state(self, *_args):
        if self._restoring:
            return
        current = self.current_document()
        save_layout(self.tab_state_path, {'open': [self._relative(self.tab_bar.tabData(i).pipeline_dir) for i in range(self.tab_bar.count()) if isinstance(self.tab_bar.tabData(i), (PipelineDocumentTab, SuspendedTab))],
            'active': self._relative(current.pipeline_dir) if current else None})

    def _on_current_tab_changed(self, index):
        super()._on_current_tab_changed(index)
        self._save_tab_state()
        self._position_manager()

    def _open_pipeline(self, pipeline_dir, *, make_current=True):
        d = super()._open_pipeline(pipeline_dir, make_current=make_current)
        self._update_tab_title_for_document(d, d.is_dirty)
        self._save_tab_state()
        self._position_manager()
        return d

    def _attach_delete_button(self, index, pipeline_dir):
        button = TabCloseButton(self.tab_bar)
        set_text(button, 'ui.session_tabs.close_tab_adea', setter='setToolTip')
        button.clicked.connect(lambda _checked=False, p=Path(pipeline_dir): self._close_pipeline(p))
        self.tab_bar.setTabButton(index, QTabBar.RightSide, button)

    def _close_pipeline(self, path):
        d = self.open_pipelines.get(str(Path(path).resolve()))
        if d is None:
            return
        if d.is_dirty:
            from ui.widgets.unsaved_popup import confirm_close
            if not confirm_close(self, d, title=d.pipeline_name, tab_bar=self.tab_bar,
                                 save=d.save_pipeline):
                return
        d._auto_save_timer.stop()
        self.tab_memory.forget(d)
        self.open_pipelines.pop(str(d.pipeline_dir), None)
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) is d:
                self.tab_bar.removeTab(i)
                break
        self.stack.removeWidget(d)
        d.deleteLater()
        self._on_current_tab_changed(self.tab_bar.currentIndex())
        self._save_tab_state()

    def refresh_manager(self, *_args, selected=None):
        if not hasattr(self, 'pipeline_list'):
            return
        self.pipeline_list.populate(self.library.groups(), selected)

    def _refresh_resources(self):
        self.refresh_manager()
        for document in self.open_pipelines.values():
            self._update_tab_title_for_document(document, document.is_dirty)
            canvas = document.canvas
            if canvas is None:
                continue
            canvas._refresh_node_library()
            specs = {spec.type_id: spec for spec in canvas._library_specs}
            for node in canvas.canvas_scene.items():
                if isinstance(node, PipelineNodeItem) and node.spec.type_id in specs:
                    current = specs[node.spec.type_id]
                    node.spec = replace(node.spec, title=current.title, metadata=dict(current.metadata))
                    node.update()

    def reload_open_pipeline(self, path):
        document = self.open_pipelines.get(str(Path(path).resolve()))
        if document is None:
            return
        if isinstance(document, SuspendedTab):
            # Module settings may rename the definition while its editor is
            # unloaded. Update its caption without bringing the scene back.
            document._loaded_name = load_pipeline_script(document.script_path).name
            self._update_tab_title_for_document(document, False)
        else:
            document.load_pipeline()

    def _create_new_pipeline(self, module=None):
        if isinstance(module, bool):
            module = None
        try:
            path = self.library.create(module)
            self._open_pipeline(path)
            self.refresh_manager(selected=str(path))
            event_bus.pipeline_resources_changed.emit()
        except Exception as error:
            self._library_notice(error)

    def _rename_pipeline(self, document, new_name):
        document = self.tab_memory.ensure_loaded(document)
        module, pid, _ = self.library.locate(document.pipeline_dir)
        if module is None:
            super()._rename_pipeline(document, new_name)
        else:
            name = str(new_name).strip()
            if not name:
                return
            if document.is_dirty and not document.save_pipeline():
                return
            previous = document._loaded_name
            document._loaded_name = name
            if not document.save_pipeline():
                document._loaded_name = previous
                return
            data = self.library.store.read(module)
            data['pipelines'][pid]['name'] = name
            self.library.store.write(module, data)
            self._update_tab_title_for_document(document, False)
        self.refresh_manager()
        self._save_tab_state()
        event_bus.pipeline_resources_changed.emit()

    def _delete_pipeline(self, path):
        path = Path(path).resolve()
        try:
            _, _, entry = self.library.locate(path)
        except Exception as error:
            self._library_notice(error)
            return
        def remove():
            document = self.open_pipelines.get(str(path))
            if document is not None:
                document = self.tab_memory.ensure_loaded(document)
                if document.is_dirty and not document.save_pipeline():
                    return
                document._auto_save_timer.stop()
                document._camera_save_timer.stop()
            try:
                self.library.remove(path)
                if document is not None:
                    document.is_dirty = False
                    self._close_pipeline(path)
                self.refresh_manager()
                event_bus.pipeline_resources_changed.emit()
            except Exception as error:
                self._library_notice(error)
        inline.confirm(self, tr('ui.module_view.delete_pipeline_1530'),
                       tr('ui.workspace.delete_v0_completely_4372', v0=entry['name']), remove)

    def _manager_menu(self, pos):
        item = self.pipeline_list.itemAt(pos)
        if item is None:
            return
        value = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.setObjectName('FileManagerContextMenu')
        if value['kind'] == 'group':
            add = add_caption_action(menu, 'addAction', tr('ui.workspace.create_pipeline_75bd'))
            if menu.exec(self.pipeline_list.mapToGlobal(pos)) is add:
                self._create_new_pipeline(value['id'])
            return
        path = Path(value['path'])
        actions = {key: add_caption_action(menu, 'addAction', tr(label)) for key, label in (
            ('open', 'ui.caption.ea1f40824607'), ('rename', 'ui.projects_view.rename_3064'),
            ('duplicate', 'ui.caption.00fea8a656bd'))}
        move_menu = menu.addMenu(tr('pipelines.library.move_to'))
        destinations = {}
        for group in self.library.groups():
            action = move_menu.addAction(group['name'] if group['id'] is not None else tr('pipelines.library.general'))
            action.setEnabled(group['id'] != value['module'])
            destinations[action] = group['id']
        modes = {}
        if value['module'] is not None:
            modes_menu = menu.addMenu(tr('ui.module_view.pipeline_usage_mode_cd34'))
            _, _, entry = self.library.locate(path)
            for mode, label in MODES.items():
                action = modes_menu.addAction(i18n.source(label))
                action.setCheckable(True); action.setChecked(entry.get('mode', 'user') == mode)
                modes[action] = mode
        actions['delete'] = add_caption_action(menu, 'addAction', tr('common.delete'))
        choice = menu.exec(self.pipeline_list.mapToGlobal(pos))
        try:
            if choice is actions['open']:
                self._open_pipeline(path)
            elif choice is actions['rename']:
                inline.text_input(self, tr('ui.workspace.new_pipeline_name_5a6f'), value['name'],
                                  lambda name: self._rename_pipeline(self._open_pipeline(path), name))
            elif choice is actions['duplicate']:
                document = self.open_pipelines.get(str(path))
                if document is not None and document.is_dirty and not document.save_pipeline():
                    return
                target = self.library.create(value['module'])
                try:
                    shutil.copytree(path, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                    definition = load_pipeline_script(target / 'pipeline.py')
                    _, _, entry = self.library.locate(target)
                    definition.name = entry['name']
                    save_pipeline_script(target / 'pipeline.py', definition)
                    created = self._open_pipeline(target)
                    created.save_pipeline()
                except Exception:
                    self.library.remove(target)
                    raise
                self.refresh_manager(selected=str(target))
                event_bus.pipeline_resources_changed.emit()
            elif choice in destinations:
                self._move_pipeline(path, destinations[choice])
            elif choice in modes:
                self.library.set_mode(path, modes[choice])
                self.refresh_manager()
                event_bus.pipeline_resources_changed.emit()
            elif choice is actions['delete']:
                self._delete_pipeline(path)
        except Exception as error:
            self._library_notice(error)

    def add_to_module(self, path):
        document = self.open_pipelines.get(str(Path(path).resolve()))
        if document and document.is_dirty and not document.save_pipeline():
            return
        store = PackageStore()
        modules = store.list()
        def add(module):
            store.add_pipeline(module, path)
            event_bus.pipeline_resources_changed.emit()
        if not modules:
            inline.text_input(self, tr('ui.workspace.new_module_name_6888'), '',
                              lambda name: add(store.create(name)))
        else:
            inline.choose_item(self, tr('ui.caption.33013602be14'),
                               [(m['name'], m['id']) for m in modules], add)

    def open_module_pipeline(self, path):
        # Flush pending autosave before refreshing a tab opened by Module.
        existing = self.open_pipelines.get(str(Path(path).resolve()))
        if existing and existing.is_dirty:
            existing.save_pipeline()
        document = self._open_pipeline(Path(path))
        try:
            pipeline = load_pipeline_script(Path(path) / 'pipeline.py')
            module_id = Path(path).parent.parent.name
            manifest = PackageStore().read(module_id)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) is document:
                    bind_caption(self.tab_bar, f'{pipeline.name} [{manifest["name"]}]', setter='setTabText', index=i)
        except Exception:
            pass
        panel = getattr(self.window(), 'bottom_panel', None)
        if panel:
            panel.header.tabs.setCurrentIndex(2)
            panel._on_tab_clicked(2)

    def closeEvent(self, event):
        self._save_tab_state()
        super().closeEvent(event)

    def _library_notice(self, error):
        text = tr(error.key, **error.parameters) if isinstance(error, LibraryError) else str(error)
        inline.notice(self, tr('ui.workspace.pipeline_manager_e6ba'), text)

    def _update_tab_title_for_document(self, document, dirty):
        title = document.pipeline_name
        module = getattr(document, 'module_id', None)
        if isinstance(document, SuspendedTab):
            module, _ = package_owner(document.pipeline_dir)
        if module is not None:
            try:
                title += ' [' + self.library.store.read(module)['name'] + ']'
            except (OSError, ValueError, KeyError):
                pass
        for index in range(self.tab_bar.count()):
            if self.tab_bar.tabData(index) is document:
                bind_caption(self.tab_bar, '* ' + title if dirty else title, setter='setTabText', index=index)
                return

    def _move_pipeline(self, source, destination):
        old = Path(source).resolve()
        document = self.open_pipelines.get(str(old))
        if document is not None:
            document = self.tab_memory.ensure_loaded(document)
            if document.is_dirty and not document.save_pipeline():
                return
            for name in ('_auto_save_timer', '_camera_save_timer', '_dirty_compare_timer'):
                getattr(document, name).stop()
        try:
            target = self.library.move(old, destination)
        except Exception as error:
            self._library_notice(error)
            return
        if target == old:
            return
        if document is not None:
            self.open_pipelines.pop(str(old), None)
            self.open_pipelines[str(target)] = document
            document.pipeline_dir = target
            document.script_path = pipeline_script_path(target)
            document.layout_path = pipeline_layout_path(target)
            document.module_id, document.pipeline_id = package_owner(target)
            document.canvas.pipeline_dir = target
            show_document_path(document.path_label, document.script_path)
            document.canvas._refresh_node_library()
            document.inspector.set_node(None)
            document._clean_document_state = document._document_state()
            for index in range(self.tab_bar.count()):
                if self.tab_bar.tabData(index) is document:
                    old_button = self.tab_bar.tabButton(index, QTabBar.RightSide)
                    self._attach_delete_button(index, target)
                    if old_button is not None:
                        old_button.deleteLater()
                    break
            self._update_tab_title_for_document(document, document.is_dirty)
        self._save_tab_state()
        self.refresh_manager(selected=str(target))
        event_bus.pipeline_resources_changed.emit()
