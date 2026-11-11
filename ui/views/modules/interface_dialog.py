"""Pipeline argument configuration through context menus."""
from ui.styles.binding import apply_style
from ui.icons import set_icon
from ui.i18n import bind_caption, add_caption_item, tr, localize, set_text

import keyword
import uuid
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QComboBox, QFrame, QScrollArea, QPushButton, QWidget
from PySide6.QtGui import QCursor
from modules.pipelines.control import normalize_argument_definitions, GRAPH_START_TYPE
from modules.pipelines.contracts import interface
from modules.pipelines.packages import PackageStore
from modules.pipelines.storage import load_pipeline_script
from ui.widgets import inline_editor as inline


class InterfaceEditor(QFrame):
    """An input panel, not an action menu: editing never closes its popup chain."""
    def __init__(self, items, parent, callback):
        super().__init__(parent, Qt.Popup)
        self.setAttribute(Qt.WA_NoMouseReplay, True)
        self.callback, self.rows = callback, []
        self.setObjectName('PipelineInterfaceEditor')
        self.setMinimumWidth(390)
        apply_style(self, '\n            QFrame#PipelineInterfaceEditor {background:@background.surface@; border:1px solid @border.subtle@; border-radius:6px;}\n            #PipelineInterfaceEditor QLabel {color:@text.primary@; border:0;}\n            #PipelineInterfaceEditor QLineEdit, #PipelineInterfaceEditor QComboBox {background:@background.window@; color:@text.primary@; border:1px solid @border.subtle@; border-radius:4px; padding:5px;}\n            #PipelineInterfaceEditor QComboBox QAbstractItemView {background:@background.window@;color:@text.primary@;selection-background-color:#3f51b5;}\n            #PipelineInterfaceEditor QPushButton {background:@background.surface@; color:@text.primary@; border:1px solid @border.subtle@; padding:6px; border-radius:4px;}\n        ')
        layout = QVBoxLayout(self); layout.setContentsMargins(12,12,12,12)
        layout.addWidget(localize(QLabel('', self), 'ui.interface_dialog.subgraph_output_arguments_b9aa'))
        scroll = QScrollArea(self); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMaximumHeight(380); scroll.setMinimumHeight(160)
        self.host = QWidget(scroll); self.form = QVBoxLayout(self.host); self.form.setContentsMargins(0,0,0,0)
        self.form.addStretch(1); scroll.setWidget(self.host); layout.addWidget(scroll)
        self.error_label = QLabel(self); self.error_label.setWordWrap(True); self.error_label.hide(); layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        add = localize(QPushButton('', self), 'ui.interface_dialog.argument_c40f'); add.clicked.connect(lambda: self.add_row())
        save = localize(QPushButton('', self), 'ui.settings.save_b4d3'); save.clicked.connect(self.check_accept)
        cancel = localize(QPushButton('', self), 'ui.close_panel.cancel_8fbe'); cancel.clicked.connect(self.close)
        for button in (add, save, cancel): buttons.addWidget(button)
        layout.addLayout(buttons)
        for item in items: self.add_row(item)

    def show(self):
        self.adjustSize()
        point = QCursor.pos()
        screen = (QApplication.screenAt(point) or self.screen()).availableGeometry()
        self.move(max(screen.left(), min(point.x(), screen.right()-self.width())), max(screen.top(), min(point.y(), screen.bottom()-self.height())))
        super().show()

    def mousePressEvent(self, event): event.accept()
    def mouseReleaseEvent(self, event): event.accept()

    def add_row(self, item=None):
        if item is None:
            index = len(self.rows)+1; names = {r['name'].text() for r in self.rows}
            while f'arg_{index}' in names: index += 1
            item = {'id':uuid.uuid4().hex[:10], 'name':f'arg_{index}', 'type':'string'}
        host = QFrame(self.host); box = QVBoxLayout(host); box.setContentsMargins(4,4,4,8)
        line = QHBoxLayout(); name = QLineEdit(item['name'], host); set_text(name, 'ui.interface_dialog.argument_name_9187', setter='setPlaceholderText')
        kind = QComboBox(host)
        from modules.pipelines.control import ARGUMENT_SCHEMA_TYPES
        for value in ARGUMENT_SCHEMA_TYPES: add_caption_item(kind, value, value)
        kind.setCurrentIndex(max(0, kind.findData(item['type'])))
        remove = set_icon(QPushButton('', host), 'close', square=True); remove.setFixedSize(30,30); set_text(remove, 'ui.interface_dialog.delete_argument_1ba6', setter='setToolTip')
        line.addWidget(name,1); line.addWidget(kind); line.addWidget(remove); box.addLayout(line)
        description = QLineEdit(item.get('description',''),host); set_text(description, 'ui.plugin_manager_pages.description_b368', setter='setPlaceholderText'); box.addWidget(description)
        row = {'id':item['id'], 'name':name, 'kind':kind, 'description':description, 'host':host, 'remove':remove}
        self.rows.append(row); self.form.insertWidget(self.form.count()-1,host)
        remove.clicked.connect(lambda: self._remove_argument(row))
        name.returnPressed.connect(self.check_accept); description.returnPressed.connect(self.check_accept)
        return row

    def _remove_argument(self, row):
        if row not in self.rows: return
        self.rows.remove(row); self.form.removeWidget(row['host']); row['host'].hide(); row['host'].deleteLater()

    def items(self):
        return [{'id':r['id'], 'name':r['name'].text().strip(), 'type':r['kind'].currentData(),
                 'description':r['description'].text()} for r in self.rows]

    def check_accept(self):
        items = self.items(); names = [x['name'] for x in items]
        if len(names) != len(set(names)) or any(not n.isidentifier() or keyword.iskeyword(n) for n in names):
            set_text(self.error_label, 'ui.interface_dialog.names_must_be_unique_python_identifiers_d67b'); self.error_label.show(); return
        try: self.callback(items)
        except Exception as error:
            bind_caption(self.error_label, str(error)); self.error_label.show(); return
        self.close()


def configure_interface(inspector):
    node = inspector._node
    if node is None or node.spec.type_id == GRAPH_START_TYPE:
        return
    # Keep popup ownership on the document so canvas geometry stays unchanged.
    host = inspector.parentWidget().parentWidget()
    def update(settings):
        if inspector._node is not node or node.scene() is None:
            raise ValueError('Выберите карточку заново: выбор изменился во время редактирования.')
        node._pipeline_view().change_node_settings(node,dict(node.settings,**settings))
        node.update(); inspector.modified.emit()
    if node.spec.type_id == 'pipeline.call':
        view = node._pipeline_view()
        directory = getattr(view, 'pipeline_dir', None)
        from modules.pipelines.management import package_owner
        mid, _ = package_owner(directory) if directory is not None else (None, None)
        if mid is None:
            inline.notice(host, tr('ui.interface_dialog.nested_pipeline_2aa1'), tr('ui.interface_dialog.configure_internal_pipeline_calls_in_the_d673')); return
        store = PackageStore()
        try:
            data = store.read(mid)
            items = [(pid, entry) for pid, entry in data['pipelines'].items()
                     if entry.get('mode') == 'internal' and store.resolve(mid, pid) != directory]
        except (OSError, ValueError) as exc:
            inline.notice(host, tr('ui.interface_dialog.nested_pipeline_2aa1'), str(exc)); return
        if not items:
            inline.notice(host, tr('ui.interface_dialog.nested_pipeline_2aa1'), tr('ui.interface_dialog.add_another_pipeline_to_this_module_b1d4')); return
        def choose(pid):
            inputs, outputs = interface(store.internal_definition(mid, pid))
            update({'pipeline': pid, 'inputs': inputs, 'outputs': outputs})
        inline.choose_item(host, tr('ui.caption.a0f4389116f5'), [(entry['name'], pid) for pid, entry in items], choose)
    else:
        section = 'outputs' if node.spec.type_id == GRAPH_START_TYPE else 'inputs'
        previous = getattr(host, '_inline_editor', None)
        if previous is not None:
            previous.close()
        panel = InterfaceEditor(normalize_argument_definitions(node.settings.get(section), with_description=True),
                                host, lambda items: update({section: items}))
        host._inline_editor = panel
        panel.destroyed.connect(lambda: setattr(host, '_inline_editor', None)
                                if getattr(host, '_inline_editor', None) is panel else None)
        panel.show()
