"""Grouped pipeline cards with explicit, internal-only drag and drop."""
import json
import uuid

from PySide6.QtCore import Qt, Signal, QMimeData, QSize
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QToolButton, QAbstractItemView, QSizePolicy

from ui.widgets.resource_list import ResourceList, ResourceItem
from ui.icons import set_icon, set_label_icon
from ui.i18n import i18n, tr, localize, set_text
from ui.styles.binding import apply_style
from modules.pipelines.packages import MODES

MIME = 'application/x-toshy-pipeline-location'


class PipelineManagerTree(ResourceList):
    create_requested = Signal(object)
    open_requested = Signal(str)
    move_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._token = uuid.uuid4().hex
        self._entries = {}
        self.setObjectName('PipelineLibraryTree')
        self.setIndentation(0)
        self.setItemsExpandable(False)
        self.setExpandsOnDoubleClick(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        apply_style(self, 'QTreeWidget#PipelineLibraryTree {background:transparent;border:0;} '
                    'QTreeWidget#PipelineLibraryTree::item {background:transparent;border:0;padding:2px 0;} '
                    'QTreeWidget#PipelineLibraryTree::item:selected {background:transparent;}')
        self.itemClicked.connect(self._clicked)
        self.itemDoubleClicked.connect(self._open)
        self.currentItemChanged.connect(self._mark_selected)

    def populate(self, groups, selected=None):
        expanded = {self.topLevelItem(i).data(Qt.UserRole)['id']: self.topLevelItem(i).isExpanded()
                    for i in range(self.topLevelItemCount())}
        if selected is None and self.currentItem() is not None:
            selected = self.currentItem().data(Qt.UserRole).get('path')
        self._entries = {}
        self.clear()
        for group in groups:
            module = group['id']
            root = ResourceItem('')
            root.setData(Qt.UserRole, {'kind': 'group', 'id': module})
            root.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
            root.setSizeHint(0, QSize(0, 34))
            self.addTopLevelItem(root)
            root.setExpanded(expanded.get(module, True))
            header = QFrame(self)
            box = QHBoxLayout(header)
            box.setContentsMargins(4, 0, 2, 0)
            arrow = QToolButton(header)
            arrow.setFixedSize(24, 24)
            set_icon(arrow, 'chevron-down' if root.isExpanded() else 'chevron-right', square=True)
            arrow.clicked.connect(lambda _=False, item=root: self._toggle(item))
            label = QLabel(group['name'], header) if module is not None else localize(QLabel(header), 'pipelines.library.general')
            label.setTextFormat(Qt.PlainText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            apply_style(label, 'font-weight:600; background:transparent; color:@text.secondary@;'
                        if module is None or not group['pipelines']
                        else 'font-weight:600; background:transparent; color:@text.primary@;')
            add = QToolButton(header)
            set_icon(add, 'plus', square=True)
            add.setFixedSize(26, 26)
            set_text(add, 'ui.workspace.create_pipeline_75bd', setter='setToolTip')
            add.clicked.connect(lambda _=False, owner=module: self.create_requested.emit(owner))
            box.addWidget(arrow); box.addWidget(label, 1); box.addWidget(add)
            root._arrow = arrow
            self.setItemWidget(root, 0, header)
            for entry in group['pipelines']:
                path = str(entry['path'].resolve())
                item = ResourceItem('', root)
                item.setData(Qt.UserRole, {'kind': 'pipeline', 'path': path, 'module': module, 'name': entry['name']})
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
                item.setSizeHint(0, QSize(0, 52))
                card = QFrame(self)
                card.setObjectName('PipelineResourceCard')
                card.setAttribute(Qt.WA_TransparentForMouseEvents)
                apply_style(card, 'QFrame#PipelineResourceCard {background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;} '
                            'QFrame#PipelineResourceCard[active="true"] {background:@control.selected@;border-color:@accent.primary@;} '
                            'QFrame#PipelineResourceCard QLabel {background:transparent;}')
                row = QHBoxLayout(card)
                row.setContentsMargins(10, 5, 8, 5); row.setSpacing(8)
                labels = QVBoxLayout()
                labels.setContentsMargins(0, 0, 0, 0); labels.setSpacing(1)
                title = QLabel(entry['name'], card); title.setTextFormat(Qt.PlainText)
                subtitle = QLabel(i18n.source(MODES.get(entry.get('mode'), '')) if module is not None
                                 else tr('pipelines.library.unassigned'), card)
                apply_style(subtitle, 'color:@text.secondary@;font-size:11px;')
                for label in (title, subtitle):
                    label.setMinimumWidth(0)
                    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                labels.addWidget(title); labels.addWidget(subtitle)
                row.addLayout(labels, 1)
                handle = QLabel(card)
                handle.setObjectName('PipelineDragHandle')
                handle.setFixedSize(20, 20)
                handle.setAlignment(Qt.AlignCenter)
                set_label_icon(handle, 'menu-2', size=18)
                row.addWidget(handle, 0, Qt.AlignVCenter)
                self.setItemWidget(item, 0, card)
                self._entries[path] = item
                if path == str(selected):
                    self.setCurrentItem(item)
                    root.setExpanded(True)
                    set_icon(arrow, 'chevron-down', square=True)
        self._mark_selected()

    def _toggle(self, item):
        item.setExpanded(not item.isExpanded())
        set_icon(item._arrow, 'chevron-down' if item.isExpanded() else 'chevron-right', square=True)

    def _clicked(self, item, _column=0):
        if item.data(Qt.UserRole)['kind'] == 'group':
            self._toggle(item)

    def _open(self, item, _column=0):
        value = item.data(Qt.UserRole)
        if value['kind'] == 'pipeline':
            self.open_requested.emit(value['path'])

    def _mark_selected(self, *_):
        for item in self._entries.values():
            card = self.itemWidget(item, 0)
            active = item is self.currentItem()
            if card is not None and card.property('active') != active:
                card.setProperty('active', active)
                card.style().unpolish(card); card.style().polish(card); card.update()

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None or item.data(Qt.UserRole)['kind'] != 'pipeline':
            return
        payload = {'token': self._token, 'path': item.data(Qt.UserRole)['path']}
        mime = QMimeData(); mime.setData(MIME, json.dumps(payload).encode())
        drag = QDrag(self); drag.setMimeData(mime)
        rect = self.visualItemRect(item)
        if rect.isValid():
            drag.setPixmap(self.viewport().grab(rect))
        drag.exec(Qt.MoveAction)

    def drop_destination(self, mime, point):
        if not mime.hasFormat(MIME):
            return None
        try:
            data = json.loads(bytes(mime.data(MIME)))
            path = data['path']
            source = self._entries.get(path)
            item = self.itemAt(point)
            if data.get('token') != self._token or source is None or item is None:
                return None
            target = item.data(Qt.UserRole)
            owner = target['id'] if target['kind'] == 'group' else target['module']
            if source.data(Qt.UserRole)['module'] == owner:
                return None
            return path, owner
        except (ValueError, KeyError, TypeError):
            return None

    def mimeTypes(self):
        return [MIME]

    def dragEnterEvent(self, event):
        super().dragEnterEvent(event)
        if event.mimeData().hasFormat(MIME):
            event.setDropAction(Qt.MoveAction); event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        # Let Qt run its edge auto-scroll; only our dropEvent commits a move.
        super().dragMoveEvent(event)
        if self.drop_destination(event.mimeData(), event.position().toPoint()) is not None:
            event.setDropAction(Qt.MoveAction); event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        destination = self.drop_destination(event.mimeData(), event.position().toPoint())
        if destination is None:
            event.ignore(); return
        self.move_requested.emit(*destination)
        event.setDropAction(Qt.MoveAction); event.accept()
