"""Custom Module assembly workspace: packages, independent pipelines and databases."""
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text, add_item, add_row
from ui.styles.binding import apply_style, form_field
from ui.icons import set_icon, symbol_button

from pathlib import Path
import shutil
import json
import sqlite3
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QComboBox,
    QFormLayout, QSpinBox, QGridLayout, QFrame, QToolButton, QFileDialog,
    QTabBar, QStackedWidget, QScrollArea, QMenu, QSizePolicy)
from core.paths import APP_PATHS
from modules.pipelines.packages import PackageStore, MODES
from modules.pipelines.storage import load_pipeline_script, save_pipeline_script
from ui.event_bus import event_bus
from ui.widgets.resource_list import ResourceList, ResourceItem
from ui.core_bridge.module_manager import ModuleConfigSync
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.async_runner import AsyncRunner
from ui.widgets import inline_editor as inline
from ui.widgets.mcp_widget import McpWidget


class EqualWidthTabBar(QTabBar):
    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        hint.setWidth(max(super(EqualWidthTabBar, self).tabSizeHint(i).width()
                          for i in range(self.count())))
        return hint

    def minimumTabSizeHint(self, index):
        hint = super().minimumTabSizeHint(index)
        hint.setWidth(20)
        return hint


class ModuleView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = PackageStore()
        self.runner = AsyncRunner(self, max_threads=1)
        self.module = None
        self._populating = False
        self._pipeline_selections = {}
        self.setObjectName('ModuleWorkspace')
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 8, 2, 8)
        root.setSpacing(8)
        self.module_button = localize(QPushButton('', self), 'ui.module_view.select_module_92a9')
        self.module_button.setObjectName('ModuleSelectorButton')
        self.module_button.setFixedHeight(40)
        self.module_button.setCursor(Qt.PointingHandCursor)
        self.module_button.setStyleSheet('text-align: left; padding: 4px 10px; font-size: 17px; font-weight: 700;')
        self.module_button.setMinimumWidth(0)
        self.module_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.module_button.clicked.connect(self.show_module_menu)
        self.overview = QWidget(self)
        body = QHBoxLayout(self.overview)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        root.addWidget(self.overview, 1)
        self.settings_column = QWidget(self.overview)
        self.settings_column.setMinimumWidth(0)
        column = QVBoxLayout(self.settings_column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self.module_button)
        self.settings_frame = QFrame(self.settings_column)
        self.settings_frame.setObjectName('ModuleSettingsFrame')
        settings_layout = QVBoxLayout(self.settings_frame)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(6)
        self.section_tabs = EqualWidthTabBar(self.settings_frame)
        self.section_tabs.setObjectName('ModuleSectionTabs')
        self.section_tabs.setShape(QTabBar.RoundedNorth)
        self.section_tabs.setDrawBase(False)
        self.section_tabs.setExpanding(True)
        self.section_tabs.setUsesScrollButtons(False)
        self.section_tabs.setElideMode(Qt.ElideRight)
        for title in ('General', 'Databases', 'Pipelines', 'Skills', 'MCP'):
            self.section_tabs.addTab(title)
        settings_layout.addWidget(self.section_tabs)
        settings_scroll = QScrollArea(self.settings_frame)
        settings_scroll.setObjectName('ModuleSettingsBody')
        apply_style(self.settings_frame, """
            QFrame#ModuleSettingsFrame {background:transparent;border:0;}
            QScrollArea#ModuleSettingsBody {background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;border-top-left-radius:0;border-top-right-radius:0;}
            QTabBar#ModuleSectionTabs::tab {background:@background.surface@;color:@text.secondary@;border:1px solid @border.subtle@;border-bottom:0;border-radius:5px;border-bottom-left-radius:0;border-bottom-right-radius:0;margin:0 1px 0 0;padding:7px 8px;}
            QTabBar#ModuleSectionTabs::tab:selected {background:@control.selected@;color:@text.primary@;}
        """)
        settings_layout.setSpacing(0)
        settings_scroll.setFrameShape(QFrame.StyledPanel)
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_content = QWidget()
        settings_scroll.setWidget(settings_content)
        settings_inner = QVBoxLayout(settings_content)
        settings_inner.setContentsMargins(3, 3, 3, 3)
        self.tabs = QStackedWidget(settings_content)
        self.tabs.setObjectName('ModuleSettingsStack')
        settings_inner.addWidget(self.tabs, 1)
        self.section_tabs.currentChanged.connect(self.tabs.setCurrentIndex)
        settings_layout.addWidget(settings_scroll, 1)
        column.addWidget(self.settings_frame, 1)
        self.save_module_btn = localize(QPushButton('', self.settings_column), 'ui.module_view.save_module_4b27')
        self.save_module_btn.clicked.connect(self.save_overview)
        column.addWidget(self.save_module_btn)
        body.addWidget(self.settings_column, 1)

        general = QWidget()
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(0, 0, 0, 0)
        self.name = QLineEdit()
        set_text(self.name, 'ui.module_view.module_name_e841', setter='setPlaceholderText')
        general_layout.addWidget(localize(QLabel(''), 'ui.subagents_panel.name_0918'))
        general_layout.addWidget(self.name)
        color_row = QHBoxLayout()
        color_row.addWidget(localize(QLabel(''), 'ui.module_view.color_92ab'))
        self.color_button = QPushButton()
        self.color_button.setFixedSize(56, 24)
        self.color_button.clicked.connect(self.choose_color)
        color_row.addWidget(self.color_button)
        color_row.addStretch(1)
        general_layout.addLayout(color_row)
        self.summary = localize(QLabel('', general), 'ui.module_view.dependencies_none_5e79')
        self.summary.setWordWrap(True)
        self.summary.setObjectName('ModuleOverviewSummary')
        general_layout.addWidget(self.summary)
        self.description = QPlainTextEdit()
        set_text(self.description, 'ui.module_view.module_description_8d8f', setter='setPlaceholderText')
        self.description.setMinimumHeight(120)
        general_layout.addWidget(localize(QLabel(''), 'ui.plugin_manager_pages.description_b368'))
        general_layout.addWidget(self.description, 1)
        self.system_prompt = QPlainTextEdit()
        set_text(self.system_prompt, 'ui.module_view.module_system_prompt_1869', setter='setPlaceholderText')
        self.system_prompt.setMinimumHeight(120)
        general_layout.addWidget(localize(QLabel(''), 'ui.module_view.system_prompt_2074'))
        general_layout.addWidget(self.system_prompt, 1)
        self.tabs.addWidget(general)
        self._set_color('#ec4899')

        self.resource_column = QWidget(self.overview)
        resource_layout = QVBoxLayout(self.resource_column)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        resource_layout.setSpacing(6)
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        resource_layout.addLayout(grid, 1)
        body.addWidget(self.resource_column, 1)

        self.pipelines = ResourceList()
        self.pipelines.currentItemChanged.connect(self._select_pipeline)
        self.pipelines.itemClicked.connect(lambda *_: self.section_tabs.setCurrentIndex(2))
        self.pipelines.itemDoubleClicked.connect(self.open_pipeline)
        grid.addWidget(self._resource_panel('Pipelines', self.pipelines, self.add_pipeline), 0, 0)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.pipeline_name = QLineEdit()
        set_text(self.pipeline_name, 'ui.module_view.pipeline_name_23b0', setter='setPlaceholderText')
        self.pipeline_description = QLineEdit()
        set_text(self.pipeline_description, 'ui.module_view.pipeline_description_4085', setter='setPlaceholderText')
        self.mode = QComboBox()
        for mode, label in MODES.items():
            add_caption_item(self.mode, label, mode)
        self.mode.setMinimumContentsLength(12)
        self.mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode.currentIndexChanged.connect(self.save_pipeline_settings)
        layout.addWidget(localize(QLabel(''), 'ui.module_view.pipeline_settings_64e7'))
        layout.addWidget(self.pipeline_name)
        layout.addWidget(self.pipeline_description)
        layout.addWidget(localize(QLabel(''), 'ui.module_view.pipeline_usage_mode_cd34'))
        layout.addWidget(self.mode)
        for label, handler in [(tr('ui.caption.611704832f73'), self.add_pipeline), (tr('ui.caption.ea1f40824607'), self.open_pipeline),
                               (tr('common.save'), self.save_pipeline_settings), (tr('ui.module_view.delete_pipeline_1530'), self.remove_pipeline)]:
            button = bind_caption(QPushButton(''), label)
            button.clicked.connect(handler)
            layout.addWidget(button)
        layout.addStretch(1)
        self.tabs.addWidget(page)
        self._build_database_page()
        database_page = self.tabs.widget(2)
        self.tabs.removeWidget(database_page)
        self.tabs.insertWidget(1, database_page)
        self.databases.itemClicked.connect(lambda *_: self.section_tabs.setCurrentIndex(1))
        grid.addWidget(self._resource_panel('Databases', self.databases, self.new_database), 0, 1)
        self.skills = ResourceList()
        from ui.widgets.skills_pane import SkillsPane
        self.skills_pane = SkillsPane(self.store, self.skills, self)
        self.skills.itemClicked.connect(lambda *_: self.section_tabs.setCurrentIndex(3))
        grid.addWidget(self._resource_panel('Skills', self.skills, self._add_skill), 1, 0)
        self.tabs.addWidget(self.skills_pane)
        self.mcp = ResourceList()
        self.mcp.itemClicked.connect(lambda *_: self.section_tabs.setCurrentIndex(4))
        grid.addWidget(self._resource_panel('MCP', self.mcp, self._add_mcp), 1, 1)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.mcp_pane = McpWidget(page, owner=None)
        self.mcp_pane.inventory_changed.connect(self._mcp_inventory)
        self.mcp.itemClicked.connect(lambda item: self.mcp_pane.select_server(item.data(Qt.UserRole)))
        layout.addWidget(self.mcp_pane)
        self.tabs.addWidget(page)
        # Settings must fit their share of the overview at narrow center widths.
        # Keep the scroll direction vertical instead of clipping wide controls.
        for cls in (QPushButton, QLineEdit, QComboBox):
            for control in settings_content.findChildren(cls):
                control.setMinimumWidth(0)
                control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for control in settings_content.findChildren(QPlainTextEdit):
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        for label in settings_content.findChildren(QLabel):
            label.setWordWrap(not bool(label.property('keepSingleLine')))
        self.color_button.setFixedSize(56, 24)
        self.mcp_pane.file_button.setFixedWidth(60)
        self.mcp_pane.manual_button.setFixedWidth(28)
        self.section_tabs.setCurrentIndex(0)
        self.tabs.setCurrentIndex(0)
        event_bus.pipeline_resources_changed.connect(self.reload)
        event_bus.module_status_changed.connect(lambda *_: self.reload())
        self.reload()

    def _plus_button(self, tooltip, handler):
        button = QToolButton(self)
        button.setObjectName('FileManagerBrowseBtn')
        symbol_button(button, '+')
        button.setFixedSize(26, 26)
        button.setCursor(Qt.PointingHandCursor)
        bind_caption(button, tooltip, setter='setToolTip')
        button.clicked.connect(handler)
        return button

    def _add_skill(self):
        self.section_tabs.setCurrentIndex(3)
        self.skills_pane.import_skill(anchor=self.sender())

    def _add_mcp(self):
        self.section_tabs.setCurrentIndex(4)
        self.mcp_pane.reference.setFocus()

    def _mcp_inventory(self, data):
        skill_inventory = [(server.get('id'), server.get('skills'), server.get('skills_error'))
                           for group in data.get('groups', []) if group.get('id') == self.module
                           for server in group.get('servers', [])]
        signature = json.dumps([self.module, skill_inventory], ensure_ascii=False, sort_keys=True)
        if signature != getattr(self, '_skills_mcp_signature', None):
            self._skills_mcp_signature = signature
            if not self._populating:
                self.skills_pane.reload()
        blocker = QSignalBlocker(self.mcp)
        self.mcp.clear()
        for group in data.get('groups', []):
            if group['id'] == self.module:
                for server in group['servers']:
                    item = ResourceItem(server['name'])
                    item.setData(Qt.UserRole, server['id'])
                    bind_caption(item, server.get('access_reason') or server.get('connection_error') or server['status'], setter='setToolTip', index=0)
                    add_caption_item(self.mcp, item)
        del blocker

    def _resource_panel(self, title, tree, add=None):
        panel = QFrame(self)
        panel.setObjectName('ModuleResourcePanel')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        header = QHBoxLayout()
        label = bind_caption(QLabel(''), title)
        label.setObjectName('FileManagerSessionLabel')
        header.addWidget(label, 1)
        if add:
            header.addWidget(self._plus_button(tr('ui.widget.add_7103'), add))
        layout.addLayout(header)
        layout.addWidget(tree, 1)
        tree.setMinimumSize(0, 0)
        return panel

    def _set_color(self, color):
        value = QColor(color)
        if not value.isValid():
            value = QColor('#ec4899')
        self.selected_color = value.name()
        text = '#171717' if value.lightness() > 150 else '#ffffff'
        bind_caption(self.color_button, '')
        bind_caption(self.color_button, self.selected_color, setter='setToolTip')
        self.color_button.setStyleSheet(f'background-color: {self.selected_color}; color: {text};')

    def choose_color(self):
        from ui.widgets.colour_palette import ColourPalette
        from PySide6.QtWidgets import QWidgetAction
        previous = getattr(self, '_color_menu', None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        menu = QMenu(self)
        palette = ColourPalette(self.selected_color, menu)
        action = QWidgetAction(menu)
        action.setDefaultWidget(palette)
        menu.addAction(action)
        def apply(colour):
            self._set_color(colour or '#ec4899')
            menu.close()
        palette.applied.connect(apply)
        self._color_menu = menu
        menu.popup(self.color_button.mapToGlobal(self.color_button.rect().bottomLeft()))

    def build_module_menu(self):
        menu = QMenu(self)
        menu.setObjectName('FileManagerContextMenu')
        for data in self.store.list():
            action = add_caption_action(menu, 'addAction', data['name'])
            action.setCheckable(True)
            action.setChecked(data['id'] == self.module)
            action.setData(data['id'])
            action.triggered.connect(lambda checked=False, mid=data['id']: self._select_module(mid))
        menu.addSeparator()
        localize(add_caption_action(menu, 'addAction', '+ Создать модуль'), 'ui.module_view.create_module_bc11').triggered.connect(self.create_module)
        if self.module:
            menu.addSeparator()
            localize(add_caption_action(menu, 'addAction', 'Переименовать'), 'ui.module_view.rename_1943').triggered.connect(self.rename_module)
            enabled = set(ModuleConfigSync().get_enabled_modules_from_config()['custom'])
            label = tr('ui.caption.a1b0c23413a0') if self.module in enabled else tr('btn_enable_module')
            add_caption_action(menu, 'addAction', label).triggered.connect(self.toggle_module)
            localize(add_caption_action(menu, 'addAction', 'Удалить модуль'), 'ui.module_view.delete_module_fa16').triggered.connect(self.delete_module)
        return menu

    def show_module_menu(self):
        menu = self.build_module_menu()
        menu.exec(self.module_button.mapToGlobal(self.module_button.rect().bottomLeft()))
        menu.deleteLater()

    def rename_module(self):
        module = self.module
        def rename(name):
            if self.module != module:
                return
            bind_caption(self.name, name)
            self.save_overview()
        inline.text_input(self, tr('ui.module_view.module_name_e841'), self.name.text(), rename)

    def new_database(self):
        self.section_tabs.setCurrentIndex(1)
        self.kb_name.clear()
        self.kb_manifest.clear()
        self.kb_source.clear()
        self.kb_raw_text.clear()
        self.kb_source_kind.setCurrentIndex(0)
        self.kb_status.clear()
        self.kb_name.setFocus()

    def reload(self):
        modules = self.store.list()
        ids = {data['id'] for data in modules}
        module = self.module if self.module in ids else (modules[0]['id'] if modules else None)
        self._select_module(module)

    def _select_module(self, module, *_):
        # clear() emits currentItemChanged before the old items are destroyed.
        # Block all list signals for the whole rebuild, then select from the new tree.
        trees = (self.pipelines, self.databases, self.mcp)
        blockers = [QSignalBlocker(tree) for tree in trees]
        was_populating = self._populating
        self._populating = True
        try:
            self.module = module
            data = self._read_current()
            if data is None:
                self.module = None
            # MCP may emit inventory_changed synchronously while changing owner.
            # Publish the validated owner to both panes before processing it.
            self.skills_pane.set_owner(self.module)
            self.mcp_pane.set_owner(self.module)
            for panel in (self.settings_frame, self.save_module_btn, self.resource_column):
                panel.setEnabled(data is not None)
            for tree in trees:
                tree.clear()
            if data is None:
                set_text(self.module_button, 'ui.module_view.select_module_92a9')
                bind_caption(self.module_button, '', setter='setToolTip')
                self.name.clear(); self.description.clear(); self.system_prompt.clear()
                set_text(self.summary, 'ui.module_view.dependencies_none_5e79')
                self._clear_pipeline_fields()
                return
            bind_caption(self.name, data['name'])
            bind_caption(self.module_button, data['name'] + ' ▾')
            bind_caption(self.module_button, data['name'], setter='setToolTip')
            self.description.setPlainText(data.get('description', ''))
            self.system_prompt.setPlainText(data.get('system_prompt', data.get('description', '')))
            from modules.pipelines.storage import load_layout
            ui_config = self.store.path(module) / 'ui' / 'config.json'
            self._set_color(data.get('color') or load_layout(ui_config).get('color', '#ec4899'))
            selected = self._pipeline_selections.get(module)
            current = None
            for pid, pipeline in data.get('pipelines', {}).items():
                entry = ResourceItem(f"{pipeline['name']} — {MODES.get(pipeline.get('mode'), pipeline.get('mode'))}")
                entry.setData(Qt.UserRole, pid)
                entry.setData(Qt.UserRole + 1, module)
                add_caption_item(self.pipelines, entry)
                if pid == selected:
                    current = entry
            if self.pipelines.count():
                self.pipelines.setCurrentItem(current or self.pipelines.item(0))
            deps = data.get('dependencies', [])
            cfg = ModuleConfigSync().get_enabled_modules_from_config()
            enabled = set(cfg['custom']) | set(cfg['system'])
            bind_caption(self.summary, tr('ui.caption.81397fca9c28') + (', '.join(('✓ ' if d in enabled else '✕ ') + d for d in deps) or tr('ui.caption.dc9337ff5b42')))
            knowledge_dir = self.store.path(module) / 'knowledge'
            for path in sorted([*knowledge_dir.glob('*.sqlite'), *knowledge_dir.glob('*.json')]):
                try:
                    if path.suffix.lower() == '.sqlite':
                        from core.sqlite_storage import SQLiteStore
                        meta = SQLiteStore(path, 'knowledge').read_knowledge_metadata()
                    else:
                        meta = json.loads(path.read_text(encoding='utf-8')).get('__toshy_kb__', {})
                except (ValueError, OSError, sqlite3.Error):
                    meta = {}
                item = ResourceItem(meta.get('name') or path.stem)
                item.setData(Qt.UserRole, path.name)
                bind_caption(item, meta.get('manifest') or path.name, setter='setToolTip', index=0)
                add_caption_item(self.databases, item)
            self._refresh_embedding_info()
        finally:
            self._populating = was_populating
            for blocker in blockers:
                blocker.unblock()
        self._select_pipeline(self.pipelines.currentItem())

    def _read_current(self):
        if not self.module:
            return None
        try:
            return self.store.read(self.module)
        except (OSError, ValueError):
            return None

    def _clear_pipeline_fields(self):
        self.pipeline_name.clear()
        self.pipeline_description.clear()
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(-1)

    def _pipeline_entry(self, item=None):
        if item is None:
            item = self.pipelines.currentItem()
        if item is None or not self.module:
            return None
        try:
            if item.data(Qt.UserRole + 1) != self.module:
                return None
            pid = item.data(Qt.UserRole)
        except RuntimeError:  # a previously queued item wrapper may already be deleted
            return None
        data = self._read_current()
        if data is None or pid not in data.get('pipelines', {}):
            return None
        return pid, data, data['pipelines'][pid]

    def create_module(self):
        def create(name):
            self.module = self.store.create(name)
            event_bus.pipeline_resources_changed.emit()
        inline.text_input(self, tr('ui.module_view.create_custom_module_2051'), '', create)

    def delete_module(self):
        data = self._read_current()
        if data is None:
            return
        module = self.module
        def remove():
            enabled = set(ModuleConfigSync().get_enabled_modules_from_config()['custom'])
            if module in enabled:
                raise ValueError('Сначала отключите модуль и завершите его запуски.')
            path = self.store.path(module)
            panel = getattr(self.window(), 'bottom_panel', None)
            if panel:
                for document in list(panel.modules_view.open_pipelines.values()):
                    if document.pipeline_dir.is_relative_to(path):
                        panel.modules_view._close_pipeline(document.pipeline_dir)
                        if str(document.pipeline_dir) in panel.modules_view.open_pipelines:
                            raise ValueError('Не удалось сохранить открытый pipeline.')
            shutil.rmtree(path)
            if self.module == module:
                self.module = None
            self._pipeline_selections.pop(module, None)
            event_bus.pipeline_resources_changed.emit()
        inline.confirm(self, tr('ui.module_view.delete_module_fa16'), tr('ui.module_view.delete_v0_and_all_its_contents_a121', v0=data['name']), remove)

    def toggle_module(self):
        if self.module:
            event_bus.toggle_module_requested.emit(self.module, 'custom')

    def save_overview(self):
        if not self.module:
            return
        data = self._read_current()
        if data is None:
            return
        data.update(name=self.name.text().strip() or data['name'], description=self.description.toPlainText(),
                    system_prompt=self.system_prompt.toPlainText(), color=self.selected_color)
        self.store.write(self.module, data)
        event_bus.pipeline_resources_changed.emit()

    def _select_pipeline(self, item, *_):
        if self._populating:
            return
        selected = self._pipeline_entry(item) if item is not None else None
        if selected is None:
            self._clear_pipeline_fields()
            return
        pid, _, pipeline = selected
        self._pipeline_selections[self.module] = pid
        bind_caption(self.pipeline_name, pipeline['name'])
        bind_caption(self.pipeline_description, pipeline.get('description', ''))
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(self.mode.findData(pipeline.get('mode', 'user')))

    def save_pipeline_settings(self, *_):
        if self._populating:
            return
        selected = self._pipeline_entry()
        if selected is None:
            return
        pid, data, entry = selected
        entry.update(name=self.pipeline_name.text().strip() or entry['name'], description=self.pipeline_description.text(), mode=self.mode.currentData())
        path = self.store.resolve(self.module, pid)
        panel = getattr(self.window(), 'bottom_panel', None)
        document = panel.modules_view.open_pipelines.get(str(path)) if panel else None
        if document and document.is_dirty and not document.save_pipeline():
            return
        p = load_pipeline_script(path / 'pipeline.py'); p.name = entry['name']; save_pipeline_script(path / 'pipeline.py', p)
        from modules.pipelines.contracts import dependencies
        entry['dependencies'] = dependencies(p)
        data['dependencies'] = sorted({d for x in data['pipelines'].values() for d in x.get('dependencies', [])})
        self.store.write(self.module, data)
        if document:
            panel.modules_view.reload_open_pipeline(path)
        event_bus.pipeline_resources_changed.emit()

    def open_pipeline(self, *_):
        selected = self._pipeline_entry()
        if selected is not None:
            try:
                event_bus.pipeline_open_requested.emit(str(self.store.resolve(self.module, selected[0])))
            except (OSError, ValueError) as exc:
                inline.notice(self, tr('ui.module_view.pipeline_37e1'), str(exc))

    def add_pipeline(self):
        if not self.module:
            return
        module = self.module
        panel = getattr(self.window(), 'bottom_panel', None)
        if panel:
            for document in panel.modules_view.open_pipelines.values():
                if document.is_dirty and not document.save_pipeline():
                    return
        paths = sorted((APP_PATHS.modules / 'pipelines').glob('*/pipeline.py'))
        if not paths:
            inline.notice(self, tr('ui.module_view.pipeline_37e1'), tr('ui.module_view.create_a_pipeline_in_the_pipeline_b9c7'))
            return
        def add(path):
            from ui.widgets.mcp_widget import last_mcp_catalog
            definition = load_pipeline_script(Path(path) / 'pipeline.py')
            if any(n.type_id.startswith('mcp.tool.') for n in definition.nodes) and not last_mcp_catalog.get('available'):
                inline.notice(self, tr('ui.module_view.mcp_53f1'), last_mcp_catalog.get('reason') or tr('ui.caption.e8c34d3d311b'))
                return
            def copied(_result):
                event_bus.pipeline_resources_changed.emit()
                event_bus.mcp_changed.emit()
            self.runner.submit(lambda: self.store.add_pipeline(module, Path(path)), on_success=copied,
                on_error=lambda error: inline.notice(self, tr('ui.module_view.pipeline_37e1'), str(error)))
        inline.choose_item(self, tr('ui.caption.2980708f2507'),
                           [(p.parent.name, str(p.parent)) for p in paths], add)

    def remove_pipeline(self):
        selected = self._pipeline_entry()
        if selected is None:
            return
        pid, _, entry = selected
        module = self.module
        def remove():
            data = self.store.read(module)
            if pid not in data.get('pipelines', {}):
                self.reload(); return
            path = self.store.resolve(module, pid)
            panel = getattr(self.window(), 'bottom_panel', None)
            if panel:
                panel.modules_view._close_pipeline(path)
                if str(path) in panel.modules_view.open_pipelines:
                    raise ValueError('Не удалось сохранить открытый pipeline.')
            data['pipelines'].pop(pid)
            data['dependencies'] = sorted({d for x in data['pipelines'].values() for d in x.get('dependencies', [])})
            self.store.write(module, data)
            shutil.rmtree(path)
            event_bus.pipeline_resources_changed.emit()
        inline.confirm(self, tr('ui.module_view.delete_pipeline_1530'), tr('ui.module_view.delete_the_v0_copy_from_this_a593', v0=entry['name']), remove)

    def _build_database_page(self):
        page = QWidget(); layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.databases = ResourceList()
        self.kb_name = QLineEdit(); set_text(self.kb_name, 'ui.module_view.database_name_c1fa', setter='setPlaceholderText')
        self.kb_manifest = form_field(QPlainTextEdit()); set_text(self.kb_manifest, 'ui.module_view.database_description_optional_d862', setter='setPlaceholderText'); self.kb_manifest.setMaximumHeight(80)
        self.kb_mode = QComboBox()
        for label, value in [(tr('ui.caption.9d8a501639d6'), 'text'), (tr('ui.caption.3494fc5800b6'), 'vector'), (tr('ui.caption.f8f07b02c182'), 'both')]:
            add_caption_item(self.kb_mode, label, value)
        self.kb_source = QLineEdit(); set_text(self.kb_source, 'ui.module_view.file_path_or_https_5fa8', setter='setPlaceholderText')
        self.chunk = QSpinBox(); self.chunk.setRange(100, 50000); self.chunk.setValue(1500)
        self.overlap = QSpinBox(); self.overlap.setRange(0, 49999); self.overlap.setValue(150)
        form = QFormLayout(); form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        # Attach before setVisible(): otherwise rows in the unparented form
        # briefly become independent native windows during startup.
        layout.addLayout(form)
        add_row(form, 'ui.module_view.name_046a', self.kb_name); add_row(form, 'ui.module_view.description_4eab', self.kb_manifest)
        add_row(form, 'ui.module_view.record_type_b056', self.kb_mode)
        self.embedding_info = QLabel()
        self.embedding_info.setWordWrap(True)
        form.addRow(self.embedding_info)
        self.kb_source_kind = QComboBox()
        for title, value in [(tr('ui.caption.259fa41f954b'), 'file'), (tr('ui.caption.4a559e963466'), 'text'), (tr('ui.caption.f0e23b0b7765'), 'temporary')]:
            add_caption_item(self.kb_source_kind, title, value)
        add_row(form, 'ui.module_view.source_bb74', self.kb_source_kind)
        self.kb_source_panel = QWidget()
        source_row = QHBoxLayout(self.kb_source_panel)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.addWidget(self.kb_source, 1)
        self.kb_browse = QToolButton()
        set_icon(self.kb_browse, 'more', square=True); self.kb_browse.setFixedSize(26, 26)
        self.kb_browse.setObjectName('FileManagerBrowseBtn')
        set_text(self.kb_browse, 'ui.module_view.choose_file_e366', setter='setToolTip'); self.kb_browse.clicked.connect(self.choose_source)
        source_row.addWidget(self.kb_browse)
        add_row(form, 'ui.module_view.file_url_970d', self.kb_source_panel)
        self.kb_raw_text = form_field(QPlainTextEdit())
        set_text(self.kb_raw_text, 'ui.module_view.paste_the_knowledge_base_source_text_fb2b', setter='setPlaceholderText')
        self.kb_raw_text.setMaximumHeight(160)
        add_row(form, 'ui.module_view.text_9f10', self.kb_raw_text)
        self.kb_temp_panel = QWidget()
        temp_layout = QVBoxLayout(self.kb_temp_panel)
        temp_layout.setContentsMargins(0, 0, 0, 0)
        self.kb_temp_session = QComboBox()
        set_text(self.kb_temp_session, 'ui.module_view.temporary_documents_of_the_selected_chat_43b6', setter='setToolTip')
        self.kb_temp_doc = QComboBox()
        set_text(self.kb_temp_doc, 'ui.module_view.document_loaded_by_the_file_module_0b76', setter='setToolTip')
        refresh_sources = localize(QPushButton(''), 'ui.plugin_library.refresh_list_163a')
        refresh_sources.clicked.connect(self._refresh_temp_sources)
        temp_layout.addWidget(self.kb_temp_session)
        temp_layout.addWidget(self.kb_temp_doc)
        temp_layout.addWidget(refresh_sources)
        add_row(form, 'ui.module_view.session_document_2d82', self.kb_temp_panel)
        self._kb_source_rows = [(self.kb_source_panel, form.labelForField(self.kb_source_panel), 'file'),
                                (self.kb_raw_text, form.labelForField(self.kb_raw_text), 'text'),
                                (self.kb_temp_panel, form.labelForField(self.kb_temp_panel), 'temporary')]
        self._kb_temp_generation = 0
        self.kb_source_kind.currentIndexChanged.connect(self._kb_source_changed)
        self.kb_temp_session.currentIndexChanged.connect(self._refresh_temp_sources)
        self.page_size = QSpinBox(); self.page_size.setRange(100, 50000); self.page_size.setValue(1000)
        self.page_extra_limit = QSpinBox(); self.page_extra_limit.setRange(0, 50000); self.page_extra_limit.setValue(1500)
        self._kb_setting_rows = []
        for name, description, control, modes in [
            (tr('ui.caption.60dc0a188f32'), tr('ui.caption.01319e54660e'), self.chunk, {'vector', 'both'}),
            (tr('ui.caption.898b791dedb0'), tr('ui.caption.13e9fd348762'), self.overlap, {'vector', 'both'}),
            (tr('ui.caption.3ff6b7f9fb52'), tr('ui.caption.e0d2cdc0aec6'), self.page_size, {'text', 'both'}),
            (tr('ui.caption.da026879e3ff'), tr('ui.caption.143aa2c13adb'), self.page_extra_limit, {'text', 'both'}),
        ]:
            label = QWidget(); labels = QHBoxLayout(label); labels.setContentsMargins(0, 0, 0, 0)
            title = bind_caption(QLabel(''), name + ':'); title.setProperty('keepSingleLine', True)
            title.setFixedWidth(145); labels.addWidget(title)
            hint = bind_caption(QLabel(''), description); hint.setWordWrap(True); apply_style(hint, 'color:@text.secondary@; font-size:11px;')
            labels.addWidget(hint, 1)
            control.setFixedWidth(96); labels.addWidget(control)
            form.addRow(label)
            self._kb_setting_rows.append((label, control, modes))
        def update_mode():
            mode = self.kb_mode.currentData()
            for label, control, modes in self._kb_setting_rows:
                label.setVisible(mode in modes); control.setVisible(mode in modes)
            self.embedding_info.setVisible(mode in {'vector', 'both'})
        self.kb_mode.currentIndexChanged.connect(update_mode)
        update_mode()
        row = QVBoxLayout()
        self.create_kb_btn = localize(QPushButton(''), 'ui.module_view.create_new_6c05'); self.create_kb_btn.clicked.connect(self.create_database); row.addWidget(self.create_kb_btn)
        self.import_kb_btn = localize(QPushButton(''), 'ui.module_view.import_existing_df68'); self.import_kb_btn.clicked.connect(self.import_database); row.addWidget(self.import_kb_btn)
        remove = localize(QPushButton(''), 'ui.widget.delete_be99'); remove.clicked.connect(self.remove_database); row.addWidget(remove)
        layout.addLayout(row)
        self.kb_status = QLabel(); self.kb_status.setWordWrap(True); layout.addWidget(self.kb_status)
        layout.addStretch(1)
        self.tabs.addWidget(page)
        self._refresh_embedding_info()
        self.section_tabs.currentChanged.connect(lambda *_: self._refresh_embedding_info())
        self._kb_source_changed()

    def _kb_source_changed(self, *_):
        kind = self.kb_source_kind.currentData()
        self._kb_temp_generation += 1
        for widget, label, source_kind in self._kb_source_rows:
            widget.setVisible(kind == source_kind)
            label.setVisible(kind == source_kind)
        if kind == 'temporary':
            self._refresh_temp_sources()

    def _refresh_temp_sources(self, *_):
        if self.kb_source_kind.currentData() != 'temporary':
            return
        if not self.module or not core_process.is_running():
            set_text(self.kb_status, 'ui.module_view.start_the_core_to_select_temporary_9f76'); return
        from ui.app_state import app_state
        module = self.module
        session = self.kb_temp_session.currentData() or app_state.session_id or ''
        previous_document = self.kb_temp_doc.currentData()
        self.kb_temp_doc.clear()
        self._kb_temp_generation += 1
        generation = self._kb_temp_generation
        def read():
            import httpx
            from core.security import auth_headers, load_or_create_core_token
            with httpx.Client(timeout=15) as client:
                response = client.get(f'{core_process.base_url}/v1/packages/{module}/knowledge/sources',
                    params={'session_id': session}, headers=auth_headers(load_or_create_core_token()))
                if response.status_code == 400 and session:
                    response = client.get(f'{core_process.base_url}/v1/packages/{module}/knowledge/sources',
                        headers=auth_headers(load_or_create_core_token()))
                if response.is_error:
                    raise ValueError(response.json().get('detail', response.text))
                return response.json()
        def done(result):
            if generation != self._kb_temp_generation or module != self.module:
                return
            resolved_session = result['session_id']
            with QSignalBlocker(self.kb_temp_session):
                self.kb_temp_session.clear()
                for identity in result['sessions']:
                    add_caption_item(self.kb_temp_session, identity, identity)
                index = self.kb_temp_session.findData(resolved_session)
                if index >= 0:
                    self.kb_temp_session.setCurrentIndex(index)
            if not resolved_session and self.kb_temp_session.currentData():
                self._refresh_temp_sources(); return
            add_item(self.kb_temp_doc, 'ui.module_view.select_document_df0e', '')
            for document in result['documents']:
                add_caption_item(self.kb_temp_doc, str(document['source']) + ' · ' + document['id'], document['id'])
            index = self.kb_temp_doc.findData(previous_document)
            if index >= 0:
                self.kb_temp_doc.setCurrentIndex(index)
            bind_caption(self.kb_status, tr('ui.caption.09f0af8e8040') + str(len(result['documents'])))
        def failed(error):
            if generation == self._kb_temp_generation and module == self.module:
                bind_caption(self.kb_status, str(error))
        self.runner.submit(read, done, failed)

    def _refresh_embedding_info(self):
        from core.embeddings import (get_embedding_config, embedding_provider_from_config,
                                     BUILTIN_PROVIDER, BUILTIN_MODEL_NAME, _remote_settings)
        cfg = get_embedding_config()
        name = (BUILTIN_MODEL_NAME if embedding_provider_from_config(cfg) == BUILTIN_PROVIDER
                else _remote_settings(cfg)[1])
        bind_caption(self.embedding_info, 'Embedding model: ' + name)

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Источник базы данных', self._file_dialog_start(),
            'Documents (*.txt *.md *.json *.csv *.html *.htm *.pdf *.docx);;All files (*)')
        if path:
            bind_caption(self.kb_source, path)

    def _file_dialog_start(self):
        current = self.kb_source.text().strip()
        if current and '://' not in current:
            return str(Path(current).expanduser())
        return str(APP_PATHS.workspace)

    def _kb_request(self, action, payload):
        import httpx
        from core.security import auth_headers, load_or_create_core_token
        with httpx.Client(timeout=None) as client:
            response = client.post(f'{core_process.base_url}/v1/packages/{payload.pop("module")}/knowledge/{action}',
                json=payload, headers=auth_headers(load_or_create_core_token()))
            if response.is_error:
                raise ValueError(response.json().get('detail', response.text))
            return response.json()

    def create_database(self):
        if not self.module or not core_process.is_running():
            set_text(self.kb_status, 'ui.module_view.start_the_core_to_create_a_2cb4'); return
        payload = {'module': self.module, 'name': self.kb_name.text().strip(), 'manifest': self.kb_manifest.toPlainText(),
            'mode': self.kb_mode.currentData(), 'source': self.kb_source.text(), 'chunk_size': self.chunk.value(), 'overlap': self.overlap.value(),
            'page_size': self.page_size.value(), 'page_extra_limit': self.page_extra_limit.value()}
        payload['source_kind'] = self.kb_source_kind.currentData()
        if payload['source_kind'] == 'text':
            payload['raw_text'] = self.kb_raw_text.toPlainText()
        elif payload['source_kind'] == 'temporary':
            payload.update(session_id=self.kb_temp_session.currentData() or '', doc_id=self.kb_temp_doc.currentData() or '')
        set_text(self.kb_status, 'ui.module_view.creating_database_1c59'); self.create_kb_btn.setEnabled(False)
        def done(result):
            self.create_kb_btn.setEnabled(True); bind_caption(self.kb_status, result.get('message', tr('ui.caption.5e83beef3dca')))
            self.reload()
        def error(exc):
            self.create_kb_btn.setEnabled(True); bind_caption(self.kb_status, str(exc))
        self.runner.submit(lambda: self._kb_request('create', payload), done, error)

    def import_database(self):
        if not self.module:
            return
        module = self.module
        def import_file(path):
            def operation():
                from modules.pipelines.knowledge import import_database
                result = import_database(self.store, module, path)
                if core_process.is_running():
                    self._kb_request('refresh', {'module': module})
                return result
            self.runner.submit(operation, lambda _: self.reload(), lambda e: bind_caption(self.kb_status, str(e)))
        path, _ = QFileDialog.getOpenFileName(self, 'Импорт базы данных', self._file_dialog_start(), 'Базы Toshy (*.sqlite *.json);;All files (*)')
        if path:
            import_file(path)

    def remove_database(self):
        item = self.databases.currentItem()
        if not item or not self.module:
            return
        module, filename, name = self.module, item.data(Qt.UserRole), item.text()
        def remove():
            (self.store.path(module) / 'knowledge' / filename).unlink(missing_ok=True)
            if core_process.is_running():
                self.runner.submit(lambda: self._kb_request('refresh', {'module': module}))
            self.reload()
        inline.confirm(self, tr('ui.module_view.delete_database_6a15'), tr('ui.module_view.delete_v0_7bc3', v0=name), remove)
