from __future__ import annotations

from typing import Sequence, Tuple
from copy import deepcopy

from PySide6.QtCore import QPointF
from PySide6.QtGui import QUndoCommand

from .edge import PipelineEdgeItem
from .nodes.base import PipelineNodeItem


class ChangePipelineSettingsCommand(QUndoCommand):
    """Change a port-defining setting and its obsolete wires as one action."""
    def __init__(self, view, node, settings):
        super().__init__('Change pipeline node settings')
        self.view,self.node=view,node
        self.before,self.after=deepcopy(node.settings),deepcopy(settings)
        self.keys={key for key in self.before.keys()|self.after.keys()
                   if key not in self.before or key not in self.after or self.before[key]!=self.after[key]}
        self.removed=[]
        self.first=True

    def apply(self,settings):
        for key in self.keys:
            if key in settings:self.node.settings[key]=deepcopy(settings[key])
            else:self.node.settings.pop(key,None)
        self.node.normalize_dynamic_ports()

    def redo(self):
        self.apply(self.after)
        if self.first:
            self.removed=[e for e in self.node.edges if
                (e.source is self.node and self.node.port_spec('output',e.source_port) is None)
                or (e.target is self.node and self.node.port_spec('input',e.target_port) is None)]
        for edge in self.removed:
            if edge.scene() is self.view.canvas_scene:self.view.canvas_scene.removeItem(edge)
            edge.detach()
        if not self.first:self.view.node_settings_changed.emit(self.node)
        self.first=False

    def undo(self):
        self.apply(self.before)
        for edge in self.removed:
            edge.attach()
            if edge.scene() is None:self.view.canvas_scene.addItem(edge)
            edge.update_path()
        self.view.node_settings_changed.emit(self.node)


class AddPipelineNodeCommand(QUndoCommand):
    def __init__(self, scene, node: PipelineNodeItem):
        super().__init__("Add pipeline node")
        self.scene = scene
        self.node = node
        self.parent = node.parentItem()

    def redo(self):
        if self.parent is not None: self.node.setParentItem(self.parent)
        if self.node.scene() is None:
            self.scene.addItem(self.node)

    def undo(self):
        if self.node.parentItem() is not None:
            self.parent = self.node.parentItem()
            self.node.setParentItem(None)
        if self.node.scene() is self.scene:
            self.scene.removeItem(self.node)

class AddPipelineEdgeCommand(QUndoCommand):
    def __init__(self, scene, edge: PipelineEdgeItem):
        super().__init__("Connect pipeline nodes")
        self.scene = scene
        self.edge = edge

    def redo(self):
        self.edge.attach()
        if self.edge.scene() is None:
            self.scene.addItem(self.edge)
        self.edge.source.normalize_dynamic_ports()
        self.edge.target.normalize_dynamic_ports()
        self.edge.update_path()

    def undo(self):
        if self.edge.scene() is self.scene:
            self.scene.removeItem(self.edge)
        self.edge.detach()
        self.edge.source.normalize_dynamic_ports()
        self.edge.target.normalize_dynamic_ports()

class DeletePipelineSelectionCommand(QUndoCommand):
    def __init__(
        self,
        scene,
        nodes: Sequence[PipelineNodeItem],
        edges: Sequence[PipelineEdgeItem],
    ):
        super().__init__("Delete pipeline selection")
        self.scene = scene
        self.nodes = list(nodes)
        self.parents = {node: node.parentItem() for node in self.nodes}
        self.edges = list(dict.fromkeys(edges))

    def redo(self):
        touched = set()
        for edge in self.edges:
            touched.add(edge.source)
            touched.add(edge.target)
            if edge.scene() is self.scene:
                self.scene.removeItem(edge)
            edge.detach()
        for node in self.nodes:
            if node.parentItem() is not None: node.setParentItem(None)
            if node.scene() is self.scene:
                self.scene.removeItem(node)
        for node in touched:
            node.normalize_dynamic_ports()

    def undo(self):
        touched = set()
        for node in self.nodes:
            if node.scene() is None:
                self.scene.addItem(node)
        for node, parent in self.parents.items():
            if parent is not None: node.setParentItem(parent)
        for edge in self.edges:
            touched.add(edge.source)
            touched.add(edge.target)
            edge.attach()
            if edge.scene() is None:
                self.scene.addItem(edge)
            edge.update_path()
        for node in touched:
            node.normalize_dynamic_ports()

class MovePipelineNodesCommand(QUndoCommand):
    def __init__(
        self,
        moves: Sequence[Tuple[PipelineNodeItem, QPointF, QPointF]],
    ):
        super().__init__("Move pipeline nodes")
        self.moves = [
            (node, QPointF(old_pos), QPointF(new_pos))
            for node, old_pos, new_pos in moves
        ]

    def redo(self):
        for node, _old_pos, new_pos in self.moves:
            node.setPos(new_pos)

    def undo(self):
        for node, old_pos, _new_pos in self.moves:
            node.setPos(old_pos)


class ReorderParallelNodesCommand(QUndoCommand):
    def __init__(self, parent, children):
        super().__init__('Reorder parallel tools')
        self.parent = parent
        self.before = {n: n.settings.get('_parallel_order', 0) for n in parent.children()}
        self.after = {n: index for index, n in enumerate(children)}

    def apply(self, values):
        for node, order in values.items():
            node.settings['_parallel_order'] = order
        self.parent.arrange()

    def redo(self):
        self.apply(self.after)

    def undo(self):
        self.apply(self.before)
