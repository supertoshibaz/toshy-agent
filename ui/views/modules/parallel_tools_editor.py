"""Ordered tool rows with the same drag handle as Router mechanisms."""
from ui.i18n import bind_caption, tr, set_text, i18n
from .presentation import node_caption
from ui.styles.binding import apply_style

from PySide6.QtCore import Qt, QPoint, QPropertyAnimation, QParallelAnimationGroup, QEasingCurve
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from shiboken6 import isValid


class ParallelToolsEditor(QWidget):
    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self.view = node._pipeline_view()
        self.rows = {}
        self.order = []
        self.drag_state = None
        self.animation = None
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(5)
        self.warning = QLabel(self)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color:#fbbf24;')
        self.box.addWidget(self.warning)
        self.rows_host = QWidget(self)
        self.rows_box = QVBoxLayout(self.rows_host)
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(5)
        self.box.addWidget(self.rows_host)
        self.refresh()
        i18n.language_changed.connect(self.refresh)
        if self.view is not None:
            self.view.undo_stack.indexChanged.connect(self.refresh)

    def refresh(self, *_):
        if not isValid(self.node) or self.view is None or not isValid(self.view) or self.node.scene() is not self.view.canvas_scene:
            self.setEnabled(False)
            return
        # Import after the inspector class has been constructed (avoids a cycle).
        from .inspector import LoopConditionRow
        self.stop_animation()
        self.drag_state = None
        children = self.node.children()
        self.order = [n.node_id for n in children]
        for row in self.rows.values():
            self.rows_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.rows = {}
        count = sum(n.is_parallel_delegate() for n in children)
        bind_caption(self.warning, tr('ui.caption.e6d8227d3604') if count > 1
                             else tr('ui.caption.6281dfd9b252') if not children else '')
        self.warning.setVisible(count > 1 or not children)
        for child in children:
            caption = node_caption(child.spec)
            row = LoopConditionRow(child.node_id, caption, self.rows_host)
            bind_caption(row.title_label, caption+'\n'+child.node_id, setter='setToolTip')
            set_text(row.remove_button, 'ui.parallel_tools_editor.delete_tool_and_its_connections_330d', setter='setToolTip')
            row.selected.connect(self.select)
            row.remove_requested.connect(self.remove)
            row.drag_moved.connect(self.drag)
            row.drag_finished.connect(self.finish)
            apply_style(row, 'QFrame {background:@background.surface@;border:1px solid @border.subtle@;border-radius:5px;}'
                              'QLabel {border:none;background:transparent;}'
                              'QToolButton {border:none;color:@text.primary@;background:transparent;}')
            self.rows_box.addWidget(row)
            self.rows[child.node_id] = row
        self.rows_host.setFixedHeight(max(0, len(children)*45-5))

    def select(self, identity):
        for key, row in self.rows.items():
            row.set_selected(key == identity)

    def remove(self, identity):
        if not isValid(self.node): return
        child = next((n for n in self.node.children() if n.node_id == identity), None)
        if child is not None and self.view is not None:
            self.view.delete_parallel_tool(child)

    def stop_animation(self):
        group = self.animation
        self.animation = None
        if group is not None and isValid(group):
            group.stop()
            group.deleteLater()

    def animate_slots(self, *, skip=None, finished=None):
        self.stop_animation()
        group = QParallelAnimationGroup(self)
        self.animation = group
        for index, key in enumerate(self.order):
            row = self.rows[key]
            target = QPoint(0, index*45)
            if row is skip or row.pos() == target:
                continue
            animation = QPropertyAnimation(row, b'pos', group)
            animation.setDuration(105 if finished else 115)
            animation.setStartValue(row.pos())
            animation.setEndValue(target)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)
        def done():
            if self.animation is group:
                self.animation = None
            group.deleteLater()
            if finished:
                finished()
        if group.animationCount():
            group.finished.connect(done)
            group.start()
        else:
            done()

    def drag(self, identity, global_y):
        if identity not in self.order or len(self.order) < 2:
            return
        row = self.rows[identity]
        if self.drag_state is None:
            self.stop_animation()
            geometries = {key: item.geometry() for key, item in self.rows.items()}
            for item in self.rows.values():
                self.rows_box.removeWidget(item)
            for key, item in self.rows.items():
                item.setGeometry(geometries[key])
                item.show()
            self.drag_state = {'identity': identity, 'offset': global_y-row.mapToGlobal(QPoint()).y()}
            row.setProperty('dragging', True)
            row.style().unpolish(row)
            row.style().polish(row)
        if self.drag_state['identity'] != identity:
            return
        self.select(identity)
        y = global_y-self.rows_host.mapToGlobal(QPoint()).y()-self.drag_state['offset']
        y = max(0, min(y, self.rows_host.height()-row.height()))
        row.move(0, y)
        row.raise_()
        target = max(0, min(round(y/45), len(self.order)-1))
        previous = self.order.index(identity)
        if target != previous:
            self.order.pop(previous)
            self.order.insert(target, identity)
            self.animate_slots(skip=row)

    def finish(self):
        if self.drag_state is None:
            return
        def commit():
            state, self.drag_state = self.drag_state, None
            if state is None or not isValid(self.node):
                return
            for key in self.order:
                row = self.rows[key]
                row.setProperty('dragging', False)
                row.style().unpolish(row)
                row.style().polish(row)
                self.rows_box.addWidget(row)
            self.rows_box.activate()
            children = {n.node_id: n for n in self.node.children()}
            if self.view is not None and set(children) == set(self.order):
                self.view.reorder_parallel_tools(self.node, [children[key] for key in self.order])
        self.animate_slots(finished=commit)
