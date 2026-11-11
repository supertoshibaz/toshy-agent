"""A real graph container: independent tool cards share one outer Exec boundary."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsItem
from .base import PipelineNodeItem, NODE_WIDTH, HEADER_HEIGHT
from modules.pipelines.execution import PARALLEL
from ui.views.canvas.canvas_card import GRID_SIZE


class ParallelNodeItem(PipelineNodeItem):
    def children(self):
        return sorted((item for item in self.childItems() if isinstance(item, PipelineNodeItem)), key=lambda n: (int(n.settings.get('_parallel_order', 0)), n.node_id))

    def is_column(self):
        return self.settings.get('_parallel_layout', 'column') != 'row'

    def card_width(self):
        return getattr(self, '_layout_width', NODE_WIDTH + 48)

    def body_height(self):
        return max(super().body_height(), getattr(self, '_layout_height', 120))

    def arrange(self):
        children = self.children()
        width = NODE_WIDTH+48 if self.is_column() else max(NODE_WIDTH+48,24+len(children)*(NODE_WIDTH+24))
        content = sum(child.body_height()+24 for child in children) if self.is_column() else max((child.body_height() for child in children),default=0)+24
        height = 96+GRID_SIZE+content
        if (width,height) != (getattr(self,'_layout_width',None),getattr(self,'_layout_height',None)):
            self.prepareGeometryChange()
            self._layout_width,self._layout_height = width,height
        y = 96.0+GRID_SIZE
        for index, child in enumerate(self.children()):
            child.setFlag(QGraphicsItem.ItemIsMovable, False)
            child.setZValue(10 if child.isSelected() else 2)
            child.setPos(24 if self.is_column() else 24 + index*(NODE_WIDTH+24), y if self.is_column() else 96+GRID_SIZE)
            y += child.body_height()+24
        for edge in self.edges: edge.update_path()
        self.update()

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == QGraphicsItem.ItemPositionHasChanged:
            # Child local positions stay unchanged when the container moves.
            # Their external connections still need new scene coordinates.
            view = self._pipeline_view()
            if view is not None and hasattr(view, "warning_manager"): view.warning_manager.reposition()
            for child in self.children():
                for edge in child.edges: edge.update_path()
        return result

    def plus_rect(self):
        side = HEADER_HEIGHT - 4
        return QRectF(self.card_width() - side - 4, 2, side, side)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        painter.setBrush(QColor('#363640')); painter.setPen(QPen(QColor('#aaaab7'), 1))
        painter.drawRoundedRect(self.plus_rect(), 4, 4)
        painter.drawText(self.plus_rect(), Qt.AlignCenter, '+')

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.plus_rect().contains(event.pos()):
            self.open_tool_menu(); event.accept(); return
        super().mousePressEvent(event)

    def open_tool_menu(self):
        view = self._pipeline_view()
        if view is None: return
        from ..library import NodeLibraryMenu
        from PySide6.QtGui import QCursor
        from modules.pipelines.execution import HANDOFF
        has_delegate = any(n.is_parallel_delegate() for n in self.children())
        specs = [spec for spec in view.node_registry.specs() if spec.kind == 'tool' and spec.type_id != 'advanced.agent_decision' and not spec.metadata.get('library_hidden')
                 and not (has_delegate and HANDOFF in spec.metadata.get('pipeline_contract', {}).get('output_targets', {}))]
        menu = NodeLibraryMenu(specs, lambda spec: view.add_parallel_tool(self, spec), view)
        menu.exec(QCursor.pos())


def make_node(spec, *args, **kwargs):
    return (ParallelNodeItem if spec.type_id == PARALLEL else PipelineNodeItem)(spec, *args, **kwargs)
