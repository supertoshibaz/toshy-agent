from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.icons import symbol_button, set_icon

import os
import shutil
import time
from pathlib import Path
from typing import List

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QToolButton, QTreeView, QFileDialog, QStackedWidget, QFrame, QFileSystemModel, QHeaderView, QToolTip, QMenu, QAbstractItemView, QApplication)
from ui.widgets.dialogs import MessageBox as QMessageBox
from PySide6.QtCore import Qt, QDir, Signal, QSortFilterProxyModel, QModelIndex, QPoint, QUrl, QItemSelectionModel, QTimer
from PySide6.QtGui import QDesktopServices, QGuiApplication, QDropEvent, QDragEnterEvent, QDragMoveEvent, QMouseEvent

from ui.event_bus import event_bus
from ui.core_bridge.async_runner import AsyncRunner
from ui.startup import begin_startup_activity, end_startup_activity

from ui.file_opening import IGNORED_BINARY_EXTS, OPEN_TARGETS, file_targets, is_text_file


class RootFolderProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._root_path = ""
        self._root_path_prefix = ""

    def set_root_folder(self, path: str):
        if path and os.path.exists(path):
            clean = os.path.normpath(os.path.abspath(path))
            self._root_path = clean
            self._root_path_prefix = clean + os.sep
        else:
            self._root_path = ""
            self._root_path_prefix = ""
        self.invalidateFilter()

    def _is_ancestor(self, candidate: str, target: str) -> bool:
        if not candidate or not target:
            return False
        if candidate == "/" or candidate == "\\":
            return True
        if len(candidate) in (2, 3) and candidate[1] == ":":
            return target.lower().startswith(candidate[:2].lower())

        prefix = candidate.rstrip(os.sep) + os.sep
        return target.startswith(prefix)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._root_path:
            return True

        source_model = self.sourceModel()
        if not source_model:
            return True

        idx = source_model.index(source_row, 0, source_parent)
        item_path = source_model.filePath(idx)
        if not item_path:
            return True

        item_norm = os.path.normpath(item_path)

        if item_norm == self._root_path:
            return True

        if item_norm.startswith(self._root_path_prefix):
            return True

        if self._is_ancestor(item_norm, self._root_path):
            return True

        return False


class FileTreeView(QTreeView):
    """Древо файлов: 2 клика — открыть, 3 клика — переименовать файл прямо в строке."""
    items_dropped = Signal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self._click_count = 0
        self._last_click_time = 0.0
        self._last_click_pos = QPoint()

    def _register_click(self, pos: QPoint, is_double_click: bool = False) -> int:
        now = time.time() * 1000.0
        double_click_interval = QApplication.doubleClickInterval()

        is_close_enough = (pos - self._last_click_pos).manhattanLength() < 8
        is_fast_enough = (now - self._last_click_time < double_click_interval)

        if is_fast_enough and is_close_enough:
            if is_double_click:
                self._click_count = 2
            else:
                self._click_count += 1
        else:
            self._click_count = 2 if is_double_click else 1

        self._last_click_time = now
        self._last_click_pos = pos
        return self._click_count

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            count = self._register_click(event.pos(), is_double_click=False)

            if count == 3:
                idx = self.indexAt(event.pos())
                if idx.isValid():
                    self.edit(idx)
                    self._click_count = 0
                    event.accept()
                    return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._register_click(event.pos(), is_double_click=True)
        super().mouseDoubleClickEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            src_paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
            if src_paths:
                pos = event.position().toPoint()
                index = self.indexAt(pos)

                widget = self.window().findChild(QWidget, "FileManagerWidget")
                root_ws = widget.current_path if widget and hasattr(widget, "current_path") else ""

                if index.isValid():
                    proxy_model = self.model()
                    src_model = proxy_model.sourceModel()
                    src_idx = proxy_model.mapToSource(index)
                    target_file = src_model.filePath(src_idx)
                    target_dir = target_file if os.path.isdir(target_file) else str(Path(target_file).parent)
                else:
                    target_dir = root_ws

                if target_dir and os.path.isdir(target_dir):
                    self.items_dropped.emit(src_paths, target_dir)
                    event.acceptProposedAction()
                    return

        super().dropEvent(event)


class HistoryFileSystemModel(QFileSystemModel):
    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole:
            return super().setData(index, value, role)
        from ui.file_saving import record_file_save
        @record_file_save(lambda: [self.filePath(index), str(Path(self.filePath(index)).with_name(str(value)))],
                          label=tr('ui.caption.c6ad1dacc5a5'))
        def rename():
            return super(HistoryFileSystemModel, self).setData(index, value, role)
        try:
            return rename()
        except Exception as error:
            QMessageBox.warning(None, tr('ui.file_manager_widget.rename_eaf7'), str(error))
            return False


class FileManagerWidget(QWidget):
    workspace_changed = Signal(str)
    initial_load_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FileManagerWidget")

        self.current_session = "default"
        self.current_project = ""
        self._clipboard_paths: List[str] = []
        self._expanded_paths: set = set()
        self._pending_expand_paths: set = set()
        self.async_runner = AsyncRunner(self)
        self.initial_load_complete = False
        self._startup_fs_token = None
        self._startup_fs_path = ""

        # Корень проекта из ui/widgets/: parents[0]=widgets, parents[1]=ui, parents[2]=project_root
        from core.paths import APP_PATHS
        root_dir = APP_PATHS.root
        default_ws = root_dir / "workspace"
        default_ws.mkdir(parents=True, exist_ok=True)
        self.current_path = str(default_ws)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 1. Заголовок сессии
        self.session_label = localize(QLabel('', self), 'ui.project_history.select_project_04bb')
        self.session_label.setObjectName("FileManagerSessionLabel")
        layout.addWidget(self.session_label)

        # 2. Строка выбора директории
        path_box = QFrame(self)
        path_box.setObjectName("FileManagerPathContainer")
        path_layout = QHBoxLayout(path_box)
        path_layout.setContentsMargins(4, 2, 4, 2)
        path_layout.setSpacing(4)

        self.path_input = QLineEdit(self.current_path, path_box)
        self.path_input.setObjectName("FileManagerPathInput")
        set_text(self.path_input, 'ui.file_manager_widget.project_folder_path_d370', setter='setPlaceholderText')
        self.path_input.returnPressed.connect(self._on_path_entered)

        self.browse_btn = QToolButton(self)
        self.browse_btn.setObjectName("FileManagerBrowseBtn")
        symbol_button(self.browse_btn, "📁")
        set_text(self.browse_btn, 'ui.file_manager_widget.select_project_folder_2210', setter='setToolTip')
        self.browse_btn.setFixedSize(26, 26)
        self.browse_btn.setCursor(Qt.PointingHandCursor)
        self.browse_btn.clicked.connect(self._browse_directory)

        path_layout.addWidget(self.path_input, 1)
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        path_row.addWidget(path_box, 1)
        self.import_button = set_icon(QToolButton(self), 'import', size=18, square=True)
        self.import_button.setObjectName('FileManagerImportBtn')
        self.import_button.setFixedSize(26, 26)
        set_text(self.import_button, 'ui.files.import', setter='setToolTip')
        self.import_button.clicked.connect(self._import_files)
        path_row.addWidget(self.browse_btn)
        path_row.addWidget(self.import_button)
        layout.addLayout(path_row)

        self.search_input = QLineEdit(self)
        self.search_input.setObjectName("FileManagerSearchInput")
        path_box.ensurePolished()
        self.search_input.setFixedHeight(max(path_box.sizeHint().height(), self.browse_btn.height()))
        set_text(self.search_input, 'ui.file_manager_widget.search_filenames_0626', setter='setPlaceholderText')
        self.search_input.setClearButtonEnabled(True)
        layout.addWidget(self.search_input)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._search_files)
        self.search_input.textChanged.connect(self._search_changed)

        # 3. Стек древа
        self.tree_stack = QStackedWidget(self)

        self.fs_model = HistoryFileSystemModel(self)
        from ui.file_icons import register_file_model
        register_file_model(self.fs_model)
        self.fs_model.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
        self.fs_model.setReadOnly(False)
        self.fs_model.setResolveSymlinks(False)
        self.fs_model.directoryLoaded.connect(self._on_directory_loaded)
        self.fs_model.fileRenamed.connect(self._on_file_renamed)

        self.proxy_model = RootFolderProxyModel(self)
        self.proxy_model.setSourceModel(self.fs_model)

        self.tree_view = FileTreeView(self)
        self.tree_view.setObjectName("FileManagerTreeView")
        self.tree_view.setModel(self.proxy_model)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setAnimated(False)
        self.tree_view.setIndentation(14)

        self.tree_view.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_view.header().setStretchLastSection(True)

        self.tree_view.hideColumn(1)
        self.tree_view.hideColumn(2)
        self.tree_view.hideColumn(3)

        self.tree_view.doubleClicked.connect(self._on_item_double_clicked)
        self.tree_view.items_dropped.connect(self._on_items_dropped)
        self.tree_view.expanded.connect(self._on_item_expanded)
        self.tree_view.collapsed.connect(self._on_item_collapsed)

        self.tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)

        self.tree_stack.addWidget(self.tree_view)

        self.placeholder_widget = QFrame(self)
        self.placeholder_widget.setObjectName("FileManagerPlaceholderFrame")
        ph_layout = QVBoxLayout(self.placeholder_widget)
        ph_layout.setAlignment(Qt.AlignCenter)

        self.placeholder_label = localize(QLabel('', self.placeholder_widget), 'ui.file_manager_widget.choose_a_project_folder_f3c3')
        self.placeholder_label.setObjectName("FileManagerPlaceholderLabel")
        self.placeholder_label.setAlignment(Qt.AlignCenter)
        ph_layout.addWidget(self.placeholder_label)

        self.tree_stack.addWidget(self.placeholder_widget)
        from ui.widgets.file_search import FileSearchResults
        self.search_results = FileSearchResults(self)
        self.search_results.opened.connect(self._open_search_file)
        self.tree_stack.addWidget(self.search_results)
        layout.addWidget(self.tree_stack, 1)

        self.set_workspace_path(self.current_path)

    def _search_changed(self, text):
        self.search_results.stop()
        if text.strip() and self.current_path:
            self.tree_stack.setCurrentWidget(self.search_results)
            set_text(self.search_results.status, 'ui.file_manager_widget.search_cf30')
            self.search_timer.start()
        else:
            self.search_timer.stop()
            self.tree_stack.setCurrentWidget(self.tree_view if self.current_path else self.placeholder_widget)

    def _search_files(self):
        if self.current_path and self.search_input.text().strip():
            self.search_results.search(self.current_path, self.search_input.text().strip())

    def _open_search_file(self, path):
        event_bus.open_file_requested.emit(path)
        suffix = Path(path).suffix.lower()
        event_bus.switch_view_tab_requested.emit(3 if suffix == '.canvas' else 4 if suffix == '.tpaint' else 2)

    def _on_file_renamed(self, path: str, old_name: str, new_name: str):
        old_path = os.path.abspath(os.path.join(path, old_name))
        new_path = os.path.abspath(os.path.join(path, new_name))
        event_bus.file_renamed.emit(old_path, new_path)

    def _on_item_expanded(self, proxy_idx: QModelIndex):
        src_idx = self.proxy_model.mapToSource(proxy_idx)
        p = self.fs_model.filePath(src_idx)
        if p and os.path.isdir(p):
            self._expanded_paths.add(os.path.abspath(p))

    def _on_item_collapsed(self, proxy_idx: QModelIndex):
        src_idx = self.proxy_model.mapToSource(proxy_idx)
        p = self.fs_model.filePath(src_idx)
        if p:
            abs_p = os.path.abspath(p)
            self._expanded_paths.discard(abs_p)
            self._expanded_paths = {x for x in self._expanded_paths if not x.startswith(abs_p + os.sep)}

    def get_expanded_folders(self) -> List[str]:
        return sorted(list(self._expanded_paths))

    def restore_expanded_folders(self, paths: List[str]):
        self._expanded_paths = set()
        self._pending_expand_paths = {os.path.abspath(p) for p in paths if os.path.exists(p)}
        self._apply_pending_expands()

    def _apply_pending_expands(self):
        if not self._pending_expand_paths:
            return

        for p in list(self._pending_expand_paths):
            src_idx = self.fs_model.index(p)
            if src_idx.isValid():
                proxy_idx = self.proxy_model.mapFromSource(src_idx)
                if proxy_idx.isValid():
                    self.tree_view.expand(proxy_idx)
                    self._expanded_paths.add(p)
                    self._pending_expand_paths.discard(p)
                    self.fs_model.fetchMore(src_idx)

    def _on_items_dropped(self, src_paths: List[str], target_dir: str):
        ws_root = os.path.abspath(self.current_path)

        def perform_operations():
            for src in src_paths:
                src_path = Path(src).resolve()
                dest_path = Path(target_dir).resolve() / src_path.name

                if src_path == dest_path or src_path == Path(target_dir).resolve():
                    continue

                is_internal = src_path.is_relative_to(Path(ws_root).resolve())
                if is_internal:
                    shutil.move(str(src_path), str(dest_path))
                else:
                    if src_path.is_dir():
                        shutil.copytree(str(src_path), str(dest_path))
                    else:
                        shutil.copy2(str(src_path), str(dest_path))

        self.async_runner.submit(
            perform_operations,
            on_error=lambda error: QMessageBox.warning(
                self, tr('ui.file_manager_widget.move_error_63aa'), tr('ui.file_manager_widget.could_not_complete_file_operation_v0_e7fd', v0=error)
            ),
        )

    def _get_selected_paths(self) -> List[str]:
        indexes = self.tree_view.selectionModel().selectedRows(0)
        paths = []
        for idx in indexes:
            src_idx = self.proxy_model.mapToSource(idx)
            p = self.fs_model.filePath(src_idx)
            if p and os.path.exists(p):
                paths.append(p)
        return paths

    def _create_child_folder(self, parent):
        from ui.widgets import inline_editor as inline
        def create(name):
            name = name.strip()
            if not name or name in {'.','..'} or any(char in name for char in ('/', '\\', '\0')):
                QMessageBox.warning(self, tr('ui.files.new_folder'), tr('ui.files.invalid_folder'))
                return
            try:
                (Path(parent) / name).mkdir()
            except OSError as error:
                QMessageBox.warning(self, tr('ui.files.new_folder'), str(error))
        inline.text_input(self, tr('ui.files.new_folder'), '', create)

    def _show_context_menu(self, pos: QPoint):
        clicked = self.tree_view.indexAt(pos)
        selection = self.tree_view.selectionModel()
        if clicked.isValid() and not selection.isSelected(clicked):
            selection.select(clicked, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self.tree_view.setCurrentIndex(clicked)
        selected_paths = self._get_selected_paths()
        target_path = selected_paths[0] if selected_paths else None

        menu = QMenu(self)
        menu.setObjectName("FileManagerContextMenu")
        source_index = self.proxy_model.mapToSource(clicked) if clicked.isValid() else QModelIndex()
        clicked_path = self.fs_model.filePath(source_index) if source_index.isValid() else self.current_path
        folder = Path(clicked_path) if clicked_path else None
        if folder is not None and folder.is_file():
            folder = folder.parent
        act_new_folder = add_caption_action(menu, 'addAction', tr('ui.files.new_folder'))
        act_new_folder.setEnabled(folder is not None and folder.is_dir())
        menu.addSeparator()

        open_actions = {}
        chosen_default = {'target':None}
        if selected_paths and all(os.path.isfile(path) for path in selected_paths):
            from ui.widgets.default_open_menu import DefaultOpenMenu
            open_menu = DefaultOpenMenu(tr('ui.file_opening.open_in_f43d'),menu)
            add_caption_action(menu, 'addMenu', open_menu)
            capabilities = [file_targets(path) for path in selected_paths]
            options = getattr(self.window(),'workspace_options',{})
            def enabled(target):
                workspace = 'paint' if target=='sketch' else 'board' if target.startswith('board') else None
                return all(item[target] for item in capabilities) and (workspace is None or options.get(workspace,True))
            def primary(target):
                chosen_default['target'] = target
                open_menu.close()
                menu.close()
            open_menu.primary_requested.connect(primary)
            for default,label,variants in (
                ('file','File',(('file_text',tr('ui.caption.7a131f19291a')),('file_image',tr('ui.caption.96f5f7884011')))),
                ('board','Board',(('board_text',tr('ui.caption.7a131f19291a')),('board_image',tr('ui.caption.96f5f7884011'))))):
                submenu = add_caption_action(open_menu, 'addMenu', label)
                submenu.menuAction().setEnabled(enabled(default))
                open_menu.primary_targets[submenu.menuAction()] = default
                for target,text in variants:
                    action = add_caption_action(submenu, 'addAction', text)
                    action.setEnabled(enabled(target))
                    open_actions[action] = target
            action = localize(add_caption_action(open_menu, 'addAction', 'Sketch'), 'ui.paint_view.sketch_0f9b')
            action.setEnabled(enabled('sketch'))
            open_actions[action] = 'sketch'
            menu.addSeparator()
        act_history = localize(add_caption_action(menu, 'addAction', 'История файла'), 'ui.file_view.file_history_a5e6')
        act_history.setEnabled(len(selected_paths)==1 and bool(target_path) and os.path.isfile(target_path))

        act_copy = localize(add_caption_action(menu, 'addAction', "Копировать"), 'ui.console.copy_1ecd')
        act_paste = localize(add_caption_action(menu, 'addAction', "Вставить"), 'ui.console.paste_ec79')
        menu.addSeparator()
        act_copy_rel = localize(add_caption_action(menu, 'addAction', "Копировать путь по проекту"), 'ui.file_manager_widget.copy_project_relative_path_2582')
        act_copy_abs = localize(add_caption_action(menu, 'addAction', "Копировать абсолютный путь"), 'ui.file_manager_widget.copy_absolute_path_f53f')
        menu.addSeparator()
        act_reveal_sys = localize(add_caption_action(menu, 'addAction', "Показать в файловой системе"), 'ui.file_manager_widget.show_in_file_manager_d2a5')
        menu.addSeparator()
        act_delete = localize(add_caption_action(menu, 'addAction', "Удалить навсегда"), 'ui.file_manager_widget.delete_permanently_abac')

        has_selection = bool(selected_paths)
        act_copy.setEnabled(has_selection)
        act_copy_rel.setEnabled(has_selection)
        act_copy_abs.setEnabled(has_selection)
        act_reveal_sys.setEnabled(has_selection)
        act_delete.setEnabled(has_selection)

        can_paste = bool(self._clipboard_paths and any(os.path.exists(p) for p in self._clipboard_paths))
        act_paste.setEnabled(can_paste)

        action = menu.exec(self.tree_view.viewport().mapToGlobal(pos))
        target = chosen_default['target'] or open_actions.get(action)
        if target:
            for path in selected_paths:
                event_bus.open_file_in_requested.emit(path,target)
            return
        if not action:
            return

        if action == act_new_folder:
            self._create_child_folder(folder)
        elif action == act_history:
            from ui.history_integration import open_history
            open_history(self.window(), path=target_path)

        elif action == act_copy:
            self._clipboard_paths = list(selected_paths)

        elif action == act_paste and can_paste:
            target_dir = self.current_path
            if target_path and os.path.isdir(target_path):
                target_dir = target_path
            elif target_path:
                target_dir = str(Path(target_path).parent)

            clipboard_paths = list(self._clipboard_paths)
            planned = []

            def paste_targets():
                planned.clear()
                reserved = set()
                for src_str in clipboard_paths:
                    src = Path(src_str)
                    if not src.exists():continue
                    dest = Path(target_dir) / src.name
                    counter = 1
                    while dest.exists() or dest in reserved:
                        dest = Path(target_dir) / f'{src.stem}_copy{counter}{src.suffix}'
                        counter += 1
                    reserved.add(dest)
                    planned.append((src,dest))
                return [str(dest) for _,dest in planned]

            from ui.file_saving import record_file_save
            @record_file_save(paste_targets, label=tr('ui.caption.c46e2de6d0d6'))
            def paste_items():
                for src,dest in planned:
                    if src.is_dir():
                        shutil.copytree(str(src), str(dest))
                    else:
                        shutil.copy2(str(src), str(dest))

            self.async_runner.submit(
                paste_items,
                on_error=lambda error: QMessageBox.warning(self, tr('ui.file_manager_widget.error_bf22'), tr('ui.file_manager_widget.could_not_paste_v0_e659', v0=error)),
            )

        elif action == act_copy_rel and target_path:
            try:
                rel = os.path.relpath(target_path, self.current_path)
            except Exception:
                rel = Path(target_path).name
            QGuiApplication.clipboard().setText(rel.replace("\\", "/"))

        elif action == act_copy_abs and target_path:
            QGuiApplication.clipboard().setText(os.path.abspath(target_path))

        elif action == act_reveal_sys and target_path:
            p = Path(target_path).resolve()
            folder_to_open = p if p.is_dir() else p.parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_to_open)))

        elif action == act_delete and selected_paths:
            count = len(selected_paths)
            title = (
                tr('ui.caption.1a2c4fd91917', v0=count)
                if count > 1
                else tr('ui.caption.9a8aaec5b0bf', v0=Path(target_path).name)
            )
            confirm = QMessageBox.question(
                self,
                tr('ui.file_manager_widget.delete_303c'),
                title + tr('ui.caption.7194116faefa'),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm == QMessageBox.Yes:
                raw_paths = [Path(path).resolve() for path in selected_paths]
                # If a selected directory already contains another selected path,
                # delete only the directory to avoid duplicate operations.
                delete_paths = []
                for candidate in sorted(raw_paths, key=lambda path: len(path.parts)):
                    if any(parent == candidate or parent in candidate.parents for parent in delete_paths):
                        continue
                    delete_paths.append(candidate)

                from ui.file_saving import record_file_save
                @record_file_save(lambda: delete_paths, label=tr('ui.caption.9e20f951abdd'))
                def delete_permanently():
                    for path in delete_paths:
                        if not path.exists() and not path.is_symlink():
                            continue
                        if path.is_dir() and not path.is_symlink():
                            shutil.rmtree(path)
                        else:
                            path.unlink()

                self.async_runner.submit(
                    delete_permanently,
                    on_error=lambda error: QMessageBox.warning(
                        self, tr('ui.file_manager_widget.deletion_error_d6df'), tr('ui.file_manager_widget.could_not_delete_selected_items_v0_4025', v0=error)
                    ),
                )

    def _is_binary_file(self, file_path: str) -> bool:
        return not is_text_file(file_path)

    def _on_item_double_clicked(self, proxy_index: QModelIndex):
        src_index = self.proxy_model.mapToSource(proxy_index)
        file_path = self.fs_model.filePath(src_index)

        if os.path.isfile(file_path):
            paint_file = Path(file_path).suffix.lower() in {".tpaint", ".png", ".jpg", ".jpeg"}
            if not paint_file and self._is_binary_file(file_path):
                rect = self.tree_view.visualRect(proxy_index)
                pos = self.tree_view.mapToGlobal(rect.center())
                QToolTip.showText(pos, tr("binary_file_warning", "Бинарные файлы пока не поддерживаются"), self.tree_view)
                return

            event_bus.open_file_requested.emit(file_path)
            if paint_file:
                event_bus.switch_view_tab_requested.emit(4)
            elif file_path.lower().endswith(".canvas"):
                event_bus.switch_view_tab_requested.emit(3)
            else:
                event_bus.switch_view_tab_requested.emit(2)

    def set_project_name(self, project_id, name=''):
        self.current_project = project_id or ''
        self.search_input.setEnabled(bool(project_id))
        bind_caption(self.session_label, 'Project: ' + name if project_id else tr('ui.project_history.select_project_04bb'))
        self.path_input.setEnabled(bool(project_id))
        self.browse_btn.setEnabled(bool(project_id))
        self.import_button.setEnabled(bool(project_id))

    def set_session_name(self, session_name: str):
        self.current_session = session_name or "default"
        set_text(self.session_label, 'ui.file_manager_widget.session_v0_123b', v0=self.current_session)

    def _begin_startup_fs_wait(self, path: str):
        normalized = os.path.normpath(path or "")
        if self._startup_fs_token and normalized == self._startup_fs_path:
            return
        if self._startup_fs_token:
            end_startup_activity(self._startup_fs_token)
        self._startup_fs_path = normalized
        self._startup_fs_token = begin_startup_activity(
            f"filesystem:{normalized or 'empty-workspace'}"
        )

    def _finish_startup_fs_wait(self):
        token = self._startup_fs_token
        self._startup_fs_token = None
        self._startup_fs_path = ""
        end_startup_activity(token)

    def set_workspace_path(self, path: str):
        self.search_results.stop()
        self.search_input.clear()
        clean_path = (path or "").strip()
        if clean_path and os.path.exists(clean_path) and os.path.isdir(clean_path):
            abs_path = os.path.abspath(clean_path)
            if os.path.normpath(abs_path) != os.path.normpath(self.current_path or ""):
                self.initial_load_complete = False
            self._begin_startup_fs_wait(abs_path)
            self.current_path = abs_path
            bind_caption(self.path_input, abs_path)

            parent_path = str(Path(abs_path).parent)
            self.fs_model.setRootPath(parent_path)
            self.proxy_model.set_root_folder(abs_path)

            parent_src_idx = self.fs_model.index(parent_path)
            parent_proxy_idx = self.proxy_model.mapFromSource(parent_src_idx)
            self.tree_view.setRootIndex(parent_proxy_idx)

            src_idx = self.fs_model.index(abs_path)
            proxy_idx = self.proxy_model.mapFromSource(src_idx)
            if proxy_idx.isValid():
                self.tree_view.expand(proxy_idx)
                if self.fs_model.canFetchMore(src_idx):
                    self.fs_model.fetchMore(src_idx)

            self.tree_stack.setCurrentIndex(0)
            self.workspace_changed.emit(abs_path)
        else:
            self.current_path = ""
            self.path_input.clear()
            self.tree_stack.setCurrentIndex(1)
            self.workspace_changed.emit("")
            self._finish_startup_fs_wait()
            if not self.initial_load_complete:
                self.initial_load_complete = True
                self.initial_load_finished.emit()

    def _on_directory_loaded(self, loaded_path: str):
        if not self.current_path:
            return

        parent_path = str(Path(self.current_path).parent)
        loaded_norm = os.path.normpath(loaded_path)
        current_norm = os.path.normpath(self.current_path)

        if loaded_norm in (current_norm, os.path.normpath(parent_path)):
            parent_src_idx = self.fs_model.index(parent_path)
            parent_proxy_idx = self.proxy_model.mapFromSource(parent_src_idx)
            if parent_proxy_idx.isValid() and self.tree_view.rootIndex() != parent_proxy_idx:
                self.tree_view.setRootIndex(parent_proxy_idx)

            src_idx = self.fs_model.index(self.current_path)
            proxy_idx = self.proxy_model.mapFromSource(src_idx)
            if proxy_idx.isValid():
                self.tree_view.expand(proxy_idx)
                if self.fs_model.canFetchMore(src_idx):
                    self.fs_model.fetchMore(src_idx)

        self._apply_pending_expands()
        # The parent directory being available is not enough: wait until the
        # selected workspace itself has been populated by QFileSystemModel.
        if not self.initial_load_complete and loaded_norm == current_norm:
            self.initial_load_complete = True
            self.initial_load_finished.emit()
            self._finish_startup_fs_wait()

    def _import_filter(self):
        return tr('ui.files.all_filter')

    def _import_files(self):
        if not self.current_path or not Path(self.current_path).is_dir():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, tr('ui.files.import'), self.current_path,
            self._import_filter(), options=QFileDialog.DontUseNativeDialog)
        if paths:
            self._import_paths(paths)

    def _import_paths(self, paths):
        destination = Path(self.current_path)
        if not destination.is_dir():
            return
        allowed = getattr(self, 'import_extensions', None)
        planned, reserved = [], set()
        for path in paths:
            source = Path(path)
            if not source.is_file() or (allowed is not None and source.suffix.lower() not in allowed):
                continue
            target = destination / source.name
            if source.resolve() == target.resolve():
                continue
            number = 1
            while target.exists() or target in reserved:
                target = destination / f'{source.stem}_copy{number}{source.suffix}'
                number += 1
            planned.append((source, target))
            reserved.add(target)
        if not planned:
            return
        from ui.file_saving import record_file_save
        @record_file_save(lambda source, target: str(target), label=tr('ui.files.import'))
        def copy_file(source, target):
            shutil.copy2(source, target)
        def copy_files():
            for source, target in planned:
                copy_file(source, target)
        self.async_runner.submit(copy_files,
            on_error=lambda error: QMessageBox.warning(self, tr('ui.file_manager_widget.error_bf22'), str(error)))

    def _browse_directory(self):
        start_dir = self.current_path if (self.current_path and os.path.exists(self.current_path)) else QDir.homePath()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Выбрать Project Folder",
            start_dir,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks | QFileDialog.DontUseNativeDialog
        )
        if chosen:
            self.set_workspace_path(chosen)

    def _on_path_entered(self):
        entered = self.path_input.text().strip()
        self.set_workspace_path(entered)
