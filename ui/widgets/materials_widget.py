"""Материалы используют такое же дерево и поиск, как Project Folder."""
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text, i18n

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMenu, QSizePolicy, QLabel, QHBoxLayout
from shiboken6 import isValid
from ui.widgets.file_manager_widget import FileManagerWidget
from ui.app_state import app_state
from ui.event_bus import event_bus
from ui.core_bridge.session_client import SessionApiClient
from ui.core_bridge.process_manager import core_process
from ui.widgets import inline_editor as inline




class MaterialsWidget(FileManagerWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('MaterialsWidget')
        self._storage_label_text = ''
        self.session_label.setWordWrap(False)
        self.session_label.setFixedHeight(self.session_label.sizeHint().height())
        self.session_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.extensions_label = localize(QLabel('', self), 'ui.materials_widget.md_canvas_tpaint_e3f1')
        self.extensions_label.setObjectName('FileManagerSessionLabel')
        self.extensions_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.extensions_label.setFixedHeight(self.session_label.height())
        self.extensions_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.layout().takeAt(0)
        header.addWidget(self.session_label, 1)
        header.addWidget(self.extensions_label)
        self.layout().insertLayout(0, header)
        self.client = SessionApiClient(base_url=core_process.base_url)
        self.path_input.setReadOnly(True)
        set_text(self.path_input, 'ui.materials_widget.materials_location_for_the_selected_project_c9d4', setter='setToolTip')
        self.fs_model.setNameFilters(['*.canvas', '*.tpaint', '*.md'])
        self.fs_model.setNameFilterDisables(False)
        self.import_extensions = {'.canvas', '.tpaint', '.md'}
        app_state.project_changed.connect(self.refresh)
        app_state.workspace_changed.connect(self.refresh)
        app_state.materials_changed.connect(self.refresh)
        event_bus.sessions_updated.connect(self.refresh)
        i18n.language_changed.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        identity = app_state.project_id
        self.set_project_name(identity, app_state.project_name)
        assets = app_state.projects.get(identity, {}).get('assets') or {}
        self._storage_label_text = {
            'toshy': tr('ui.caption.8cababe49eb3'),
            'project': 'Project Folder',
            'custom': tr('ui.caption.b6a93b2ec7ce'),
        }.get(assets.get('mode'), tr('ui.caption.8cababe49eb3')) if identity else tr('materials.inbox')
        self._update_storage_label()
        try:
            root = app_state.view_workspace('materials')
            if root:
                Path(root).mkdir(parents=True, exist_ok=True)
            bind_caption(self.session_label, self._storage_label_text, setter='setToolTip')
        except (OSError, ValueError) as error:
            root = ''
            bind_caption(self.session_label, str(error), setter='setToolTip')
            bind_caption(self.placeholder_label, str(error))
        if root != self.current_path:
            self.set_workspace_path(root)
        available = bool(self.current_path)
        self.import_button.setEnabled(available)
        self.search_input.setEnabled(available)
        self.path_input.setEnabled(available)

    def _update_storage_label(self):
        bind_caption(self.session_label, self.session_label.fontMetrics().elidedText(
            self._storage_label_text, Qt.ElideRight, self.session_label.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_storage_label_text'):
            self._update_storage_label()

    def _search_files(self):
        if self.current_path and self.search_input.text().strip():
            self.search_results.search(self.current_path, self.search_input.text().strip(), {'.canvas','.tpaint','.md'})

    def _open_search_file(self, path):
        suffix = Path(path).suffix.lower()
        target = 'board' if suffix == '.canvas' else 'sketch' if suffix == '.tpaint' else 'file'
        event_bus.open_file_in_requested.emit(path, target)

    def _on_item_double_clicked(self, index):
        source = self.proxy_model.mapToSource(index)
        path = self.fs_model.filePath(source)
        if Path(path).is_file():
            self._open_search_file(path)

    def _browse_directory(self):
        if not app_state.project_id:
            return
        menu = QMenu(self)
        choices = {}
        choices[localize(add_caption_action(menu, 'addAction', 'В данных Toshy'), 'ui.materials_widget.in_toshy_data_0c72')] = {'mode':'toshy', 'path':''}
        projects = localize(add_caption_action(menu, 'addMenu', 'Внутри Project Folder'), 'ui.materials_widget.inside_the_project_folder_5007', setter='setTitle')
        for identity, record in app_state.projects.items():
            path = record.get('workspace')
            action = add_caption_action(projects, 'addAction', (record.get('name') or identity) + ' — ' + (path or tr('ui.caption.3d0c40721c6b')))
            action.setEnabled(bool(path and Path(path).is_dir()))
            choices[action] = {'mode':'project','project_id':identity,'path':path}
        custom = localize(add_caption_action(menu, 'addAction', 'Другая папка…'), 'ui.materials_widget.another_folder_b6b3')
        action = menu.exec(self.browse_btn.mapToGlobal(self.browse_btn.rect().bottomLeft()))
        choice = choices.get(action)
        if action == custom:
            path = QFileDialog.getExistingDirectory(self, 'Папка материалов', self.current_path,
                QFileDialog.ShowDirsOnly | QFileDialog.DontUseNativeDialog)
            if not path:
                return
            choice = {'mode':'custom', 'path':path}
        if choice:
            self._save_location(choice)

    def _save_location(self, choice):
        identity = app_state.project_id
        if not identity:
            return
        def save():
            result = self.client.update_project_folder(identity, settings={'assets':choice})
            if not result.get('success'):
                raise ValueError(result.get('message') or 'Папка материалов не сохранена.')
        def saved(_):
            app_state.projects.setdefault(identity,{})['assets'] = choice
            app_state.materials_changed.emit()
            event_bus.sessions_updated.emit()
        self.async_runner.submit(save, saved, lambda error: inline.notice(self, tr('ui.materials_widget.materials_9de9'), str(error)) if isValid(self) else None)

    def _import_filter(self):
        return tr('ui.files.materials_filter')
