"""Shared MCP controls for the global sidebar and the selected custom package."""
from __future__ import annotations
from ui.i18n import add_caption_action, bind_caption, add_caption_item, set_caption_headers, tr, localize, set_text, add_item
from ui.styles.binding import apply_style, form_field


import json
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, Signal, QSize
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QScrollArea, QFrame, QFormLayout, QComboBox,
    QDoubleSpinBox, QSpinBox, QPlainTextEdit, QFileDialog, QSizePolicy, QToolButton, QCheckBox as NativeCheckBox)

from ui.qt_deferred import defer
from ui.icons import set_icon, get as icon
from ui.app_state import app_state
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.transport import core_http_client
from core.security import auth_headers
from ui.event_bus import event_bus
from ui.widgets import inline_editor as inline

BASE = '/v1/modules/mcp_client/service'

last_mcp_catalog = {'available': False, 'reason': tr('ui.caption.9715efcdee1e')}

STATUS = {'stopped': tr('ui.caption.415b29cc9d10'), 'connecting': tr('ui.caption.e2449ea8e46e'), 'connected': tr('ui.caption.eb6fff96f3ca'),
          'stopping': tr('ui.caption.f3671ef1f9a7'), 'authorizing': tr('ui.caption.ac6ff08f1313'), 'reconnecting': tr('ui.caption.8744455b4674'), 'error': tr('ui.file_manager_widget.error_bf22')}
INSTALL = {'pending': tr('ui.caption.5ea5c68f20d4'), 'failed': tr('ui.caption.aa29e3d05233'), 'needs_command': tr('ui.caption.afe562c0a5f5')}


class McpClient:
    def scope(self):
        values = {'branch': app_state.branch or 'main'}
        if app_state.session_id:
            values['session_id'] = app_state.session_id
        elif app_state.project_id:
            values['project_id'] = app_state.project_id
        return values

    def request(self, method, path=BASE, body=None, scope=None):
        with core_http_client('http://127.0.0.1:5000', timeout=620 if method == 'POST' and path.count('/') == BASE.count('/') + 1 else 90) as client:
            response = client.request(method, 'http://127.0.0.1:5000' + path,
                params=scope if scope is not None else self.scope(), json=body, headers=auth_headers())
            if response.is_error:
                try:
                    message = response.json().get('detail', response.text)
                except ValueError:
                    message = response.text
                raise RuntimeError(str(message))
            return response.json()


class WrappedCheckBox(NativeCheckBox):
    """Keep long translated capability labels inside narrow module panes."""
    def __init__(self, text='', parent=None):
        if isinstance(text, QWidget):parent, text = text, ''
        super().__init__(parent)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text):
        self.label.setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()

    def text(self):return self.label.text()
    def minimumSizeHint(self):return QSize(24, 24)
    def sizeHint(self):return QSize(220, self.heightForWidth(220))
    def heightForWidth(self, width):return max(24, self.label.heightForWidth(max(1,width-25)))
    def hitButton(self, pos):return self.rect().contains(pos)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.label.setGeometry(25,0,max(1,self.width()-25),self.height())


class ServerCard(QFrame):
    activated = Signal()

    def __init__(self, record, state, toggle, parent=None):
        super().__init__(parent)
        self.setObjectName('McpServerCard')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(0)
        self.setCursor(Qt.PointingHandCursor)
        apply_style(self, """
            QFrame#McpServerCard {background:@background.surface@;border:1px solid @border.subtle@;border-radius:6px;}
            QFrame#McpServerCard:hover {background:@control.hover@;}
            QFrame#McpServerCard[selected="true"] {border-color:@accent.primary@;background:@control.selected@;}
            QLabel {background:transparent;border:0;}
        """)
        box = QHBoxLayout(self)
        box.setContentsMargins(8,4,8,4)
        box.setSpacing(7)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        self.name = QLabel(record['name'],self)
        self.name.setTextFormat(Qt.PlainText)
        self.name.setMinimumWidth(0)
        self.name.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.name.setToolTip(record['name'])
        self.state = bind_caption(QLabel('',self),state)
        self.state.setTextFormat(Qt.PlainText)
        self.state.setMinimumWidth(0)
        self.state.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        apply_style(self.state,'color:@text.secondary@;font-size:11px;')
        for label in (self.name,self.state):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            labels.addWidget(label)
        bind_caption(self,state + ('\n'+record['access_reason'] if record.get('access_reason') else ''),setter='setToolTip')
        box.addLayout(labels,1)
        self.toggle = toggle
        box.addWidget(toggle)

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:self.activated.emit()
        super().mousePressEvent(event)


class ConfigField(QWidget):
    def __init__(self, spec, value, *, credential=False, parent=None):
        super().__init__(parent)
        self.spec = spec
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.kind = spec.get('type', 'string')
        self.multiple = spec.get('multiple', False)
        if spec.get('choices') and not self.multiple:
            self.input = QComboBox(self)
            for option in spec['choices']:
                self.input.addItem(str(option), option)
            self.input.setCurrentIndex(max(0, self.input.findData(value)))
        elif self.kind == 'boolean' and not self.multiple:
            self.input = WrappedCheckBox(self)
            self.input.setChecked(bool(value))
        elif self.kind == 'integer' and not self.multiple:
            self.input = QSpinBox(self)
            self.input.setRange(max(-2147483647, int(spec.get('minimum', -2147483647))),
                                min(2147483647, int(spec.get('maximum', 2147483647))))
            self.input.setValue(int(value or 0))
        elif self.kind == 'number' and not self.multiple:
            self.input = QDoubleSpinBox(self)
            self.input.setRange(float(spec.get('minimum', spec.get('min', -1e12))),
                                float(spec.get('maximum', spec.get('max', 1e12))))
            self.input.setDecimals(3)
            self.input.setValue(float(value or 0))
        else:
            self.input = QLineEdit(self)
            self.input.setMinimumWidth(0)
            if spec.get('sensitive'):
                self.input.setEchoMode(QLineEdit.Password)
                bind_caption(self.input, tr('ui.caption.8036769ac837') if credential else tr('ui.caption.7468534a857d'), setter='setPlaceholderText')
            else:
                bind_caption(self.input, json.dumps(value or [], ensure_ascii=False) if self.multiple else str(value or ''))
                if self.multiple:
                    set_text(self.input, 'ui.widget.json_array_of_values_b162', setter='setPlaceholderText')
        layout.addWidget(self.input)
        if self.kind in {'directory', 'file'}:
            button = localize(QPushButton('', self), 'ui.widget.choose_a413')
            button.clicked.connect(self.browse)
            layout.addWidget(button)
        if spec.get('description'):
            help_text = QLabel(spec['description'], self)
            help_text.setWordWrap(True)
            apply_style(help_text, 'color: @text.secondary@; font-size: 11px;')
            layout.addWidget(help_text)
        if spec.get('sensitive'):
            self.clear_secret = localize(WrappedCheckBox('', self), 'ui.widget.delete_saved_credential_2ff5')
            layout.addWidget(self.clear_secret)

    def browse(self):
        if self.kind == 'directory':
            value = QFileDialog.getExistingDirectory(self, 'Каталог MCP')
        else:
            value, _ = QFileDialog.getOpenFileName(self, 'Файл MCP')
        if value:
            if self.multiple:
                try:
                    items = json.loads(self.input.text() or '[]')
                except ValueError:
                    items = []
                items.append(value)
                bind_caption(self.input, json.dumps(items, ensure_ascii=False))
            else:
                bind_caption(self.input, value)

    def value(self):
        if isinstance(self.input, NativeCheckBox):
            return self.input.isChecked()
        if isinstance(self.input, (QDoubleSpinBox, QSpinBox)):
            return self.input.value()
        if isinstance(self.input, QComboBox):
            return self.input.currentData()
        text = self.input.text()
        if self.spec.get('sensitive'):
            if self.clear_secret.isChecked():
                return ''
            return text or None
        if self.multiple:
            result = json.loads(text or '[]')
            if not isinstance(result, list):
                raise ValueError('Нужен JSON-массив')
            return result
        return text


class StatusLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(str(text).strip()))


class McpWidget(QWidget):
    inventory_changed = Signal(object)
    def __init__(self, parent=None, *, owner=None, all_groups=False):
        super().__init__(parent)
        self.owner, self.all_groups = owner, all_groups
        self.client = McpClient()
        self.runner = AsyncRunner(self, max_threads=1)
        self.busy = False
        self.catalog = {}
        self.selected = None
        self.selected_record = None
        self._form_key = None
        self._polling = False
        self._poll_failures = 0
        self._next_refresh_at = 0.0
        self._last_poll_error = ''
        self._generation = 0
        self._rendering = False
        self._tree_signature = None
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)
        self.apps_host = QWidget(self)
        self.apps_layout = QVBoxLayout(self.apps_host)
        self.apps_layout.setContentsMargins(0, 0, 0, 0)
        self._app_buttons = {}
        root.addWidget(self.apps_host)
        self.warning = QLabel(self)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet('color: #fbbf24;')
        root.addWidget(self.warning)
        self.installer = QWidget(self)
        install_layout = QVBoxLayout(self.installer)
        install_layout.setContentsMargins(0, 0, 0, 0)
        self.reference = QLineEdit(self.installer)
        set_text(self.reference, 'ui.widget.url_npm_package_or_pypi_package_4a50', setter='setPlaceholderText')
        set_text(self.reference, 'ui.widget.mcpb_server_json_npm_pypi_github_c69c', setter='setToolTip')
        self.reference.setMinimumWidth(0)
        install_layout.addWidget(self.reference)
        buttons = QHBoxLayout()
        self.add_button = localize(QPushButton('', self.installer), 'ui.widget.download_connect_21ef')
        self.add_button.clicked.connect(self.add)
        self.file_button = localize(QPushButton('', self.installer), 'ui.widget.file_e758')
        self.file_button.clicked.connect(self.choose_file)
        buttons.addWidget(self.add_button, 1)
        buttons.addWidget(self.file_button)
        self.manual_button = QPushButton('⋯', self.installer)
        self.manual_button.setFixedWidth(28)
        set_text(self.manual_button, 'ui.widget.connect_a_local_command_manually_e610', setter='setToolTip')
        self.manual_button.clicked.connect(self.manual)
        buttons.addWidget(self.manual_button)
        install_layout.addLayout(buttons)
        root.addWidget(self.installer)
        self.installer.setVisible(bool(owner) and owner != 'global')
        self.panels = QWidget(self)
        panels_layout = QVBoxLayout(self.panels)
        panels_layout.setContentsMargins(0, 0, 0, 0)
        panels_layout.setSpacing(6)
        root.addWidget(self.panels, 1)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setObjectName('McpServersTree')
        self.tree.setColumnCount(1)
        self.tree.setIndentation(0)
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.itemClicked.connect(self.toggle_group)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.setMinimumWidth(0)
        apply_style(self.tree, 'QTreeWidget#McpServersTree {background:transparent;border:0;} QTreeWidget#McpServersTree::item {background:transparent;border:0;padding:2px 0;} QTreeWidget#McpServersTree::item:selected {background:transparent;}')
        from PySide6.QtWidgets import QHeaderView
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setMinimumHeight(0)
        self.tree.currentItemChanged.connect(self.select)
        self.list_panel = QFrame(self)
        self.list_panel.setObjectName('McpListPanel')
        list_box = QVBoxLayout(self.list_panel)
        list_box.setContentsMargins(8, 8, 8, 8)
        list_box.addWidget(localize(QLabel('', self.list_panel), 'ui.mcp.server_list'))
        list_box.addWidget(self.tree)
        self.empty_servers = QWidget(self.list_panel)
        empty_box = QVBoxLayout(self.empty_servers)
        empty_box.addStretch(1)
        empty_label = QLabel(self.empty_servers)
        set_text(empty_label, 'mcp.empty')
        empty_label.setWordWrap(True)
        empty_label.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(empty_label)
        self.empty_add_button = QToolButton(self.empty_servers)
        set_icon(self.empty_add_button, 'plus', square=True)
        self.empty_add_button.setFixedSize(28, 28)
        set_text(self.empty_add_button, 'mcp.open_modules', setter='setToolTip')
        self.empty_add_button.clicked.connect(event_bus.module_workspace_requested.emit)
        self.empty_add_button.setVisible(self.all_groups)
        empty_box.addWidget(self.empty_add_button, 0, Qt.AlignCenter)
        empty_box.addStretch(1)
        list_box.addWidget(self.empty_servers, 1)
        self.tree.hide()
        apply_style(self.list_panel, 'QFrame#McpListPanel {background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;}')
        self.list_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        panels_layout.addWidget(self.list_panel, 1)
        self.settings_panel = QFrame(self)
        self.settings_panel.setObjectName('McpSettingsPanel')
        settings_box = QVBoxLayout(self.settings_panel)
        settings_box.setContentsMargins(8, 8, 8, 8)
        settings_box.setAlignment(Qt.AlignTop)
        settings_title = localize(QLabel('', self.settings_panel), 'ui.mcp.server_settings')
        settings_title.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        settings_box.addWidget(settings_title)
        self.settings_hint = localize(QLabel('', self.settings_panel), 'ui.widget.select_an_mcp_server_to_configure_f1cb')
        self.settings_hint.setWordWrap(True)
        self.settings_hint.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        settings_hint_alignment = Qt.AlignLeft | Qt.AlignTop
        self.settings_hint.setAlignment(settings_hint_alignment)
        settings_box.addWidget(self.settings_hint)
        apply_style(self.settings_panel, 'QFrame#McpSettingsPanel {background:@background.panel@;border:1px solid @border.subtle@;border-radius:6px;}')
        scroll = self.details_scroll = QScrollArea(self.settings_panel)
        scroll.setObjectName('McpSettingsScroll')
        apply_style(scroll, 'QScrollBar::handle:vertical {background:@border.subtle@;} QScrollBar::handle:vertical:hover {background:@text.secondary@;}')
        scroll.setMinimumWidth(0)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.details = QWidget()
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.detail_layout = QVBoxLayout(self.details)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_layout.setSpacing(6)
        self.detail_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.details)
        scroll.hide()
        settings_box.addWidget(scroll, 1)
        panels_layout.addWidget(self.settings_panel, 1)
        self.message = StatusLabel(self)
        self.message.hide()
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.message)
        for button in (self.add_button,self.file_button):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        app_state.session_changed.connect(self.invalidate)
        app_state.project_changed.connect(self.invalidate)
        event_bus.sessions_updated.connect(self.refresh)
        event_bus.module_status_changed.connect(self.invalidate)
        event_bus.mcp_changed.connect(self.invalidate)
        core_process.status_changed.connect(self.invalidate)
        self.refresh()

    def open_app(self, identity):
        try:
            from .apps.window import open_app
            open_app(self.client, identity, self.client.scope(), self.window())
        except ImportError as error:
            bind_caption(self.message, tr('ui.caption.f5185708ae30') + str(error))

    def set_owner(self, owner):
        if self.owner != owner:
            self.owner = owner
            self.selected = None
            self.selected_record = None
            self._form_key = None
            self.build_details(None)
            self.invalidate()
        self.installer.setVisible(bool(owner) and owner != 'global')

    def invalidate(self, *_):
        self._generation += 1
        self._next_refresh_at = 0.0
        self.catalog['available'] = False
        self.catalog['management_available'] = False
        self.apply_enabled()
        last_mcp_catalog['available'] = False
        last_mcp_catalog['reason'] = tr('ui.caption.18e3afc89be5')
        self._tree_signature = None
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self, *_):
        if self.busy or self._polling:
            return
        if not core_process.is_running():
            from modules.system.mcp_client.store import McpStore
            store = McpStore()
            groups = store.owners()
            for group in groups:
                group['servers'] = [{**store.public(owner, record), 'owner': owner, 'status': 'stopped',
                    'access_reason': tr('ui.caption.c5aa77b8f8b7'), 'connection_error': ''} for owner, record in store.records(group['id'])]
            reason = tr('ui.caption.1bbb531a7947')
            self.render({'available': False, 'reason': reason, 'groups': groups})
            bind_caption(self.warning, reason)
            self.apply_enabled()
            return
        if time.monotonic() < self._next_refresh_at:
            return
        generation = self._generation
        scope = self.client.scope()
        self._polling = True
        def received(data):
            self._polling = False
            if generation != self._generation:
                self.refresh()
                return
            if self._poll_failures:
                from core.logger import core_logger
                core_logger.event('modules', 'MCP_SERVICE_RESTORED', 'MCP service connection restored')
            self._poll_failures = 0
            self._next_refresh_at = 0.0
            self._last_poll_error = ''
            self.render(data)
        def failed(error):
            self._polling = False
            if generation != self._generation:
                self.refresh()
                return
            self._poll_failures = min(4, self._poll_failures + 1)
            delay = min(30, 3 * 2 ** self._poll_failures)
            self._next_refresh_at = time.monotonic() + delay
            if str(error) != self._last_poll_error:
                from core.logger import core_logger
                core_logger.event('modules', 'MCP_SERVICE_UNAVAILABLE', str(error), level='WARN',
                                  details={'scope': scope, 'retry_seconds': delay})
                self._last_poll_error = str(error)
            from modules.system.mcp_client.store import McpStore
            store = McpStore()
            groups = store.owners()
            for group in groups:
                group['servers'] = [{**store.public(owner, record), 'owner': owner, 'status': 'stopped',
                    'access_reason': str(error), 'connection_error': ''} for owner, record in store.records(group['id'])]
            self.render({'available': False, 'reason': str(error), 'groups': groups})
            set_text(self.warning, 'ui.widget.v0_checking_again_in_v1_s_1724', v0=error, v1=delay)
            self.apply_enabled()
        self.runner.submit(lambda: self.client.request('GET', scope=scope), on_success=received, on_error=failed)

    def render(self, data):
        self.catalog = data
        apps = {a['id']: a for a in data.get('apps', []) if self.all_groups or a['owner'] == self.owner}
        for identity in set(self._app_buttons) - set(apps):
            self._app_buttons.pop(identity).deleteLater()
        for identity, app in apps.items():
            if identity not in self._app_buttons:
                button = bind_caption(QPushButton('', self.apps_host), tr('ui.caption.af5dfbd45004') + app['title'])
                button.clicked.connect(lambda _checked=False, key=identity: self.open_app(key))
                self.apps_layout.addWidget(button)
                self._app_buttons[identity] = button
        self.apps_host.setVisible(bool(apps))
        last_mcp_catalog.clear()
        last_mcp_catalog.update(data)
        warning = data.get('management_reason','') or data.get('reason','')
        bind_caption(self.warning,warning)
        self.warning.setVisible(bool(warning))
        # Use the same filtered collection for the sidebar and module view.
        # Empty owners remain in the service catalog for adding servers, but
        # must never leave an empty category in the server list.
        visible_groups = [group for group in data.get('groups', [])
                          if group.get('servers') and
                          (self.all_groups or group['id'] == self.owner)]
        self.tree.setVisible(bool(visible_groups))
        self.empty_servers.setVisible(not visible_groups)
        signature = json.dumps([{**group, 'servers':[{k:v for k,v in record.items() if k!='log_tail'} for record in group['servers']]} for group in visible_groups], ensure_ascii=False, sort_keys=True)
        if signature != self._tree_signature:
            self._tree_signature = signature
            expanded = {self.tree.topLevelItem(i).data(0, Qt.UserRole): self.tree.topLevelItem(i).isExpanded()
                        for i in range(self.tree.topLevelItemCount())}
            self._rendering = True
            self.tree.clear()
            selected_item = None
            for group in visible_groups:
                root = QTreeWidgetItem(self.tree, [group['name']])
                font = root.font(0); font.setBold(True); root.setFont(0,font)
                root.setData(0, Qt.UserRole, group['id'])
                root.setExpanded(expanded.get(group['id'], True))
                root.setIcon(0,icon('down' if root.isExpanded() else 'right'))
                for record in group['servers']:
                    identity = group['id'], record['id']
                    state = INSTALL.get(record.get('installation')) or STATUS.get(record['status'], record['status'])
                    if record.get('pending'):
                        state += tr('ui.caption.35155831ecfc')
                    item = QTreeWidgetItem(root)
                    item.setData(0, Qt.UserRole, record)
                    bind_caption(item, state + ('\n' + record['access_reason'] if record.get('access_reason') else ''), setter='setToolTip', index=0)
                    toggle = QToolButton(self.tree)
                    active = record['status'] in {'connected','authorizing','reconnecting'}
                    toggle.setCheckable(True)
                    toggle.setChecked(active)
                    set_icon(toggle,'stop' if active else 'run',size=18)
                    toggle.setFixedSize(28,28)
                    apply_style(toggle, 'QToolButton {background:@background.panel@;border:1px solid @border.subtle@;border-radius:5px;padding:0;min-width:26px;max-width:26px;min-height:26px;max-height:26px;} QToolButton:hover {background:@control.hover@;}')
                    self.update_toggle(toggle,record)
                    toggle.clicked.connect(lambda checked,key=identity:self.action(key,'start' if checked else 'stop'))
                    card = ServerCard(record,state,toggle,self.tree)
                    card.activated.connect(lambda item=item:self.tree.setCurrentItem(item))
                    item.setSizeHint(0,QSize(0,48))
                    self.tree.setItemWidget(item,0,card)
                    if record.get('pending') and self.selected is None:
                        self.selected = identity
                    if identity == self.selected:
                        selected_item = item
                        self.selected_record = record
            if selected_item:
                self.tree.setCurrentItem(selected_item)
            elif self.selected:
                self.selected = None
                self.selected_record = None
                self._form_key = None
                self.build_details(None)
            self._rendering = False
        # Logs change frequently without rebuilding server cards or editor drafts.
        records = {(g['id'],record['id']):record for g in data.get('groups',[]) for record in g['servers']}
        if self.selected in records:self.selected_record = records[self.selected]
        self._mark_selected()
        if self.selected_record is not None and self._form_key != self.selected:
            self.build_details(self.selected_record)
        self.apply_enabled()
        if self.selected_record is not None:
            self.update_state()
        self._fit_server_list()
        self.inventory_changed.emit(data)

    def _fit_server_list(self):
        opened = self.selected_record is not None
        self.details_scroll.setVisible(opened)
        self.settings_hint.setVisible(not opened)
        self.tree.setMinimumHeight(0)
        self.tree.setMaximumHeight(16777215)

    def toggle_group(self,item,column=0):
        if item.parent() is None and not isinstance(item.data(0,Qt.UserRole),dict):
            item.setExpanded(not item.isExpanded())
            item.setIcon(0,icon('down' if item.isExpanded() else 'right'))
            self._fit_server_list()

    def _mark_selected(self):
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            for child in range(root.childCount()):
                item = root.child(child)
                card = self.tree.itemWidget(item,0)
                if card is not None:
                    record = item.data(0,Qt.UserRole)
                    selected = isinstance(record,dict) and (record['owner'],record['id']) == self.selected
                    if card.property('selected') != selected:
                        card.setProperty('selected',selected)
                        card.style().unpolish(card);card.style().polish(card);card.update()

    def select_server(self, identity):
        for index in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(index)
            for child in range(root.childCount()):
                item = root.child(child)
                record = item.data(0, Qt.UserRole)
                if record['id'] == identity:
                    self.tree.setCurrentItem(item)
                    return

    def select(self, item, previous=None):
        if self._rendering:
            return
        record = item.data(0, Qt.UserRole) if item else None
        if item is not None and item.parent() is None:return
        if not isinstance(record, dict):
            self.selected = None
            self.selected_record = None
            self._form_key = None
            self.build_details(None)
            self._fit_server_list()
            return
        self.selected = record['owner'], record['id']
        self.selected_record = record
        self._mark_selected()
        if self._form_key != self.selected:
            self.build_details(record)
        self._fit_server_list()

    def clear_details(self):
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def build_details(self, record):
        self.clear_details()
        self._form_key = self.selected
        if record is None:
            label = localize(QLabel('', self.details), 'ui.widget.select_an_mcp_server_to_configure_f1cb')
            label.setWordWrap(True)
            self.detail_layout.addWidget(label)
            return
        owner = next((x['name'] for x in self.catalog.get('groups', []) if x['id'] == record['owner']), record['owner'])
        title = bind_caption(QLabel('', self.details), record['name'] + tr('ui.caption.82f349d10a1c') + owner)
        title.setWordWrap(True)
        self.detail_layout.addWidget(title)
        self.delete_button = localize(QPushButton('', self.details), 'ui.widget.remove_server_from_this_device_d1ac')
        set_text(self.delete_button, 'ui.widget.remove_the_server_from_the_module_2cd8', setter='setToolTip')
        self.delete_button.clicked.connect(self.delete)
        self.detail_layout.addWidget(self.delete_button)
        self.state_label = QLabel(self.details)
        self.state_label.setWordWrap(True)
        self.detail_layout.addWidget(self.state_label)
        self.login_button = localize(QPushButton('', self.details), 'ui.widget.sign_in_through_your_browser_526c')
        self.login_button.clicked.connect(self.login)
        self.detail_layout.addWidget(self.login_button)
        self.auto = localize(WrappedCheckBox('', self.details), 'ui.widget.start_this_instance_automatically_4084')
        self.auto.setChecked(bool(record.get('autostart')))
        self.auto.toggled.connect(self.save_autostart)
        self.detail_layout.addWidget(self.auto)
        self.settings_host = QWidget(self.details)
        box = QVBoxLayout(self.settings_host)
        box.setContentsMargins(0, 0, 0, 0)
        box.setAlignment(Qt.AlignTop)
        box.addWidget(localize(QLabel('', self.settings_host), 'ui.widget.tool_access_4299'))
        self.visibility = QComboBox(self.settings_host)
        add_item(self.visibility, 'ui.widget.model_and_pipelines_562a', 'model')
        add_item(self.visibility, 'ui.widget.pipelines_only_48d5', 'pipeline')
        self.visibility.setCurrentIndex(max(0, self.visibility.findData(record.get('visibility', 'model'))))
        box.addWidget(self.visibility)
        self.fields = {}
        for key, spec in record.get('user_config', {}).items():
            label = bind_caption(QLabel('', self.settings_host), str(spec.get('title') or key) + (' *' if spec.get('required') else ''))
            label.setWordWrap(True)
            box.addWidget(label)
            field = ConfigField(spec, record.get('values', {}).get(key, spec.get('default')),
                                credential=record.get('credential_fields', {}).get('config:' + key, False), parent=self.settings_host)
            self.fields[key] = field
            box.addWidget(field)
        self.advanced_button = localize(QPushButton('', self.settings_host), 'ui.widget.advanced_settings_039f')
        self.advanced_button.setCheckable(True)
        box.addWidget(self.advanced_button)
        self.advanced = QWidget(self.settings_host)
        advanced_box = QVBoxLayout(self.advanced)
        advanced_box.setContentsMargins(0, 0, 0, 0)
        advanced_box.setAlignment(Qt.AlignTop)
        self.transport = QComboBox(self.advanced)
        for text, value in [(tr('ui.caption.9567ae72db18'), 'stdio'), ('Streamable HTTP', 'streamable-http'), ('Legacy SSE', 'sse')]:
            add_caption_item(self.transport, text, value)
        self.oauth = localize(WrappedCheckBox('', self.advanced), 'ui.widget.oauth_for_the_remote_server_3270')
        self.oauth.setChecked(bool(record.get('oauth')))
        advanced_box.addWidget(self.oauth)
        self.advanced_help(advanced_box, self.oauth, 'oauth')
        self.capability_boxes = {}
        for key, title in [('reconnect', tr('ui.caption.61956cf1d9ba')), ('sampling', tr('ui.caption.f6dc29bb7739')),
                           ('elicitation', tr('ui.caption.97941c31e8ae')), ('roots', tr('ui.caption.07c83143f3df')),
                           ('apps', tr('ui.caption.037315f988f9'))]:
            checkbox = bind_caption(WrappedCheckBox('', self.advanced), title)
            checkbox.setChecked(bool(record.get(key, True)))
            self.capability_boxes[key] = checkbox
            advanced_box.addWidget(checkbox)
            self.advanced_help(advanced_box, checkbox, key)
        self.transport.setCurrentIndex(max(0, self.transport.findData(record.get('transport', 'stdio'))))
        self.command = QLineEdit(record.get('command', ''), self.advanced)
        self.url = QLineEdit(record.get('url', ''), self.advanced)
        self.args = form_field(QPlainTextEdit(json.dumps(record.get('args', []), ensure_ascii=False), self.advanced))
        self.args.setMaximumHeight(75)
        self.env = form_field(QPlainTextEdit('{}', self.advanced))
        self.headers = form_field(QPlainTextEdit('{}', self.advanced))
        for widget in (self.env, self.headers):
            widget.setMaximumHeight(70)
        self.timeout = QDoubleSpinBox(self.advanced)
        self.timeout.setRange(1, 600)
        self.timeout.setValue(float(record.get('timeout', 60)))
        for title, widget in [(tr('ui.caption.20f9adf72966'), self.transport), (tr('ui.caption.8b372c12c268'), self.command), (tr('ui.widget.arguments_json_array_9c85'), self.args),
                              (tr('ui.caption.4289b1713783'), self.url), (tr('ui.caption.fdd969f9d45f'), self.timeout),
                              (tr('ui.caption.69ae1d858bd7'), self.env), (tr('ui.caption.162a8643d3c0'), self.headers)]:
            advanced_box.addWidget(bind_caption(QLabel('', self.advanced), title))
            widget.setMinimumWidth(0)
            advanced_box.addWidget(widget)
            key = next(name for name in ('transport', 'command', 'args', 'url', 'timeout', 'env', 'headers') if getattr(self, name) is widget)
            self.advanced_help(advanced_box, widget, key)
        current = bind_caption(QLabel('', self.advanced), tr('ui.caption.d2cad06cad02') + ', '.join(record.get('env', {})) + tr('ui.caption.58f9fbd44283') + ', '.join(record.get('headers', {})))
        current.setWordWrap(True)
        advanced_box.addWidget(current)
        self.advanced.hide()
        self.advanced_button.toggled.connect(self.advanced.setVisible)
        box.addWidget(self.advanced)
        self.save_button = localize(QPushButton('', self.settings_host), 'ui.widget.save_settings_d325')
        self.save_button.clicked.connect(self.save)
        box.addWidget(self.save_button)
        self.detail_layout.addWidget(self.settings_host)
        self.detail_layout.addWidget(localize(QLabel('', self.details), 'ui.widget.server_tools_123c'))
        self.tool_list = QTreeWidget(self.details)
        set_caption_headers(self.tool_list, [tr('ui.caption.a8ba5e181272'), tr('ui.plugin_manager_pages.description_b368')])
        from PySide6.QtWidgets import QHeaderView
        self.tool_list.header().setSectionResizeMode(QHeaderView.Stretch)
        self.tool_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tool_list.setMinimumHeight(0)
        self.tool_list.setUniformRowHeights(True)
        self.tool_list.itemChanged.connect(self.toggle_tool)
        self._tools_signature = None
        self.detail_layout.addWidget(self.tool_list)
        self.refresh_button = localize(QPushButton('', self.details), 'ui.widget.refresh_tool_list_3f22')
        self.refresh_button.clicked.connect(lambda: self.action(self.selected, 'refresh') if self.selected else None)
        self.detail_layout.addWidget(self.refresh_button)
        self.pending_host = QWidget(self.details)
        self.pending_layout = QVBoxLayout(self.pending_host)
        self.pending_layout.setContentsMargins(0, 0, 0, 0)
        self._pending_widgets = {}
        self.detail_layout.addWidget(self.pending_host)
        self.update_tools()
        source = bind_caption(QLabel('', self.details), tr('ui.caption.f30d1d487948') + str(record.get('source', {}).get('reference', '')) + tr('ui.caption.44926870bd57') + str(record.get('installed_version') or tr('ui.caption.6d50be0a122e')))
        source.setWordWrap(True)
        self.detail_layout.addWidget(source)
        self.log_button = localize(QPushButton('', self.details), 'ui.widget.server_log_1945')
        self.log_button.setCheckable(True)
        self.detail_layout.addWidget(self.log_button)
        self.log = QPlainTextEdit(self.details)
        self.log.setReadOnly(True)
        apply_style(self.log,'QPlainTextEdit {background:@background.input@;color:@text.primary@;border:1px solid @border.subtle@;border-radius:6px;padding:6px;}')
        self.log.setMinimumHeight(120)
        self.log.setMaximumHeight(220)
        self.log.hide()
        self.log_button.toggled.connect(self.show_log)
        self.detail_layout.addWidget(self.log)
        self.update_state()
        self.apply_enabled()
        self.fit_details()

    def advanced_help(self, layout, control, key):
        caption = tr('ui.mcp.help.' + key)
        bind_caption(control, caption, setter='setToolTip')
        label = bind_caption(QLabel('', self.advanced), caption)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setTextFormat(Qt.PlainText)
        apply_style(label, 'QLabel {color:@text.secondary@;font-size:11px;background:transparent;border:0;}')
        layout.addWidget(label)

    def fit_details(self):
        for widget in self.details.findChildren(QWidget):
            if isinstance(widget,(QPushButton,QLineEdit,QComboBox,QPlainTextEdit)):
                widget.setMinimumWidth(0)
                policy = widget.sizePolicy()
                policy.setHorizontalPolicy(QSizePolicy.Ignored)
                widget.setSizePolicy(policy)
            if isinstance(widget,QLabel):
                widget.setWordWrap(True)
            # The scroll content fills its viewport, but its text rows and
            # nested groups must keep their natural height, including wrapping.
            if isinstance(widget, (QLabel, WrappedCheckBox)) or isinstance(widget.layout(), QVBoxLayout):
                policy = widget.sizePolicy()
                policy.setVerticalPolicy(QSizePolicy.Maximum)
                widget.setSizePolicy(policy)
        self.details.updateGeometry()

    def show_log(self, expanded):
        self.log.setVisible(expanded)
        self.details.layout().activate()
        if expanded:
            log = self.log
            defer(0,log,lambda:self.details_scroll.ensureWidgetVisible(log))

    def update_tools(self):
        if not hasattr(self, 'tool_list') or self.selected_record is None:
            return
        tools = self.selected_record.get('tools', [])
        disabled = set(self.selected_record.get('disabled_tools', []))
        signature = json.dumps([tools, sorted(disabled)], sort_keys=True, ensure_ascii=False)
        if signature != self._tools_signature:
            self._tools_signature = signature
            self.tool_list.blockSignals(True)
            self.tool_list.clear()
            for tool in tools:
                item = QTreeWidgetItem(self.tool_list, [tool['name'], tool.get('description', '')])
                item.setData(0, Qt.UserRole, tool['name'])
                bind_caption(item, tool['name'], setter='setToolTip', index=0)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked if tool['name'] in disabled else Qt.Checked)
                bind_caption(item, tool.get('description', ''), setter='setToolTip', index=1)
            self.tool_list.blockSignals(False)
        self.tool_list.doItemsLayout()
        row_height = max(self.tool_list.fontMetrics().height()+6, self.tool_list.sizeHintForRow(0))
        height = self.tool_list.header().sizeHint().height() + max(1, len(tools))*row_height + 2*self.tool_list.frameWidth()+4
        self.tool_list.setFixedHeight(min(320, height))
        self.tool_list.setEnabled(self.management_available() and not self.busy)
        self.refresh_button.setEnabled(self.management_available() and not self.busy
            and self.selected_record.get('status') == 'connected')

    def toggle_tool(self, item, column):
        if column != 0 or not self.selected or self.busy:
            return
        disabled = set(self.selected_record.get('disabled_tools', []))
        name = item.data(0, Qt.UserRole)
        if item.checkState(0) == Qt.Checked:
            disabled.discard(name)
        else:
            disabled.add(name)
        identity, scope = self.selected, self.client.scope()
        self.submit(lambda: self.client.request('PATCH', f'{BASE}/{identity[0]}/{identity[1]}',
            {'disabled_tools': sorted(disabled)}, scope))

    def update_pending(self):
        if not hasattr(self, 'pending_host') or self.selected_record is None:
            return
        requests = self.selected_record.get('pending', [])
        active = {item['request_id'] for item in requests}
        for key in set(self._pending_widgets) - active:
            self._pending_widgets.pop(key).deleteLater()
        for request in requests:
            key = request['request_id']
            if key in self._pending_widgets:
                continue
            panel = QWidget(self.pending_host)
            layout = QVBoxLayout(panel)
            title = bind_caption(QLabel('', panel), tr('ui.caption.7d2c0fe16b27') + request['server_name'] + '\n' + request['message'])
            title.setTextFormat(Qt.PlainText)
            title.setWordWrap(True)
            layout.addWidget(title)
            fields = {}
            if request['mode'] == 'form':
                schema = request['schema']
                for name, spec in schema.get('properties', {}).items():
                    label = bind_caption(QLabel('', panel), str(spec.get('title') or name) + (' *' if name in schema.get('required', []) else ''))
                    label.setTextFormat(Qt.PlainText)
                    layout.addWidget(label)
                    config = dict(spec)
                    if spec.get('type') == 'array':
                        config['multiple'] = True
                    if spec.get('enum'):
                        config['choices'] = spec['enum']
                    fields[name] = ConfigField(config, spec.get('default'), parent=panel)
                    layout.addWidget(fields[name])
            else:
                url = QLabel(request['url'], panel)
                url.setTextFormat(Qt.PlainText)
                url.setWordWrap(True)
                layout.addWidget(url)
                open_button = localize(QPushButton('', panel), 'ui.widget.open_service_page_41e1')
                open_button.clicked.connect(lambda _=False, address=request['url']: self.open_url(address))
                layout.addWidget(open_button)
            row = QHBoxLayout()
            for action, label in [('accept', tr('ui.canvas_view.send_ceb6') if request['mode'] == 'form' else tr('ui.caption.ef05d57959cf')), ('decline', tr('ui.caption.899c5d6de891')), ('cancel', tr('common.cancel'))]:
                button = bind_caption(QPushButton('', panel), label)
                button.clicked.connect(lambda _=False, r=request, a=action, f=fields: self.respond(r, a, f))
                row.addWidget(button)
            layout.addLayout(row)
            self._pending_widgets[key] = panel
            self.pending_layout.addWidget(panel)
        self.pending_host.setVisible(bool(requests))
        self.pending_host.setEnabled(bool(self.catalog.get('available')) and not self.busy)

    @staticmethod
    def open_url(address):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(address))

    def respond(self, request, action, fields):
        try:
            content = {name: widget.value() for name, widget in fields.items() if widget.value() is not None} if action == 'accept' else {}
        except Exception as error:
            bind_caption(self.message, str(error))
            return
        scope = {'session_id': request['session_id'], 'branch': request['branch']} if request['session_id'] else {'branch': request['branch']}
        self.submit(lambda: self.client.request('POST',
            f"{BASE}/{request['owner']}/{request['server_id']}/respond/{request['request_id']}",
            {'action': action, 'content': content}, scope))

    def update_state(self):
        record = self.selected_record
        if record is None or not hasattr(self, 'state_label'):
            return
        message = INSTALL.get(record.get('installation')) or STATUS.get(record['status'], record['status'])
        for detail in (record.get('access_reason'), record.get('connection_error'), record.get('error')):
            if detail:
                message += '\n' + detail
        if record['status'] in {'connected', 'authorizing', 'reconnecting'}:
            message += tr('ui.caption.a29c4842f042')
        bind_caption(self.state_label, message)
        if record['status'] == 'reconnecting':
            message += tr('ui.caption.26796355bf9a') + str(record.get('retry_count', 0))
            bind_caption(self.state_label, message)
        if hasattr(self, 'log'):
            log_text = record.get('log_tail') or tr('ui.caption.ace88d803107')
            if self.log.toPlainText() != log_text:
                self.log.setPlainText(log_text)
        self.update_tools()
        self.update_pending()
        self.login_button.setVisible(bool(record.get('authorization_url')))
        self.login_button.setEnabled(self.management_available() and not self.busy)
        self.settings_host.setEnabled(self.management_available() and not self.busy and record['status'] in {'stopped', 'error'})

    def management_available(self):
        return bool(self.catalog.get('management_available', self.catalog.get('available')))

    def update_toggle(self, toggle, record):
        reason = self.catalog.get('management_reason', '')
        if not self.management_available():
            reason = reason or self.catalog.get('reason') or tr('ui.caption.68161735e375')
        reason = reason or record.get('management_reason', record.get('access_reason', ''))
        if not reason and record.get('installation') != 'ready':
            reason = record.get('error') or INSTALL.get(record.get('installation')) or tr('ui.caption.7dcc28bf77e4')
        if not reason and record['status'] in {'connecting', 'stopping'}:
            reason = STATUS[record['status']]
        if not reason and self.busy:
            reason = tr('ui.caption.1a08fb997b4d')
        toggle.setEnabled(not bool(reason))
        bind_caption(toggle, reason or (tr('ui.caption.70533f61112c') if toggle.isChecked() else tr('ui.caption.9b566f3da964')), setter='setToolTip')

    def apply_enabled(self):
        enabled = self.management_available() and not self.busy
        self.installer.setEnabled(enabled and bool(self.owner))
        self.tree.setEnabled(not self.busy)
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child in range(group.childCount()):
                item = group.child(child)
                card = self.tree.itemWidget(item,0)
                toggle = card.toggle if card is not None else None
                record = item.data(0, Qt.UserRole)
                if toggle and isinstance(record, dict):
                    self.update_toggle(toggle, record)
        if self.selected_record is not None and hasattr(self, 'auto'):
            self.auto.setEnabled(enabled)
            self.delete_button.setEnabled(not self.busy and (enabled or not core_process.is_running()))
            self.update_state()

    def submit(self, function, *, installation=False, offline=False):
        if self.busy or not (self.management_available() or offline and not core_process.is_running()):
            return
        self.busy = True
        bind_caption(self.message, tr('ui.caption.8a64548513d7') if installation else tr('ui.caption.176e44dd22ac'))
        self.apply_enabled()
        def completed(result):
            self.busy = False
            self._form_key = None
            if installation and result.get('server'):
                record = result['server']
                self.selected = self.owner, record['id']
                bind_caption(self.message, record.get('error') or (tr('ui.caption.d64b5ad222d0') if record['installation'] == 'needs_command' else tr('ui.caption.554dfb07229a')))
            else:
                bind_caption(self.message, result.get('message', tr('ui.caption.12c711122847')))
            event_bus.mcp_changed.emit()
            self.refresh()
        def failed(error):
            self.busy = False
            bind_caption(self.message, str(error))
            self._tree_signature = None
            self.apply_enabled()
            self.refresh()
        self.runner.submit(function, on_success=completed, on_error=failed)

    def login(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        url = (self.selected_record or {}).get('authorization_url')
        if url and self.management_available():
            QDesktopServices.openUrl(QUrl(url))

    def manual(self):
        if not self.management_available() or self.busy or not self.owner:
            return
        menu = inline.ContextMenu(self, tr('ui.caption.75fc6c2b403e'), self.manual_button)
        command = QLineEdit(menu)
        set_text(command, 'ui.widget.python_node_uvx_npx_or_an_8dca', setter='setPlaceholderText')
        arguments = form_field(QPlainTextEdit('[]', menu))
        arguments.setMaximumHeight(90)
        menu.add_editor(command)
        menu.add_editor(localize(QLabel('', menu), 'ui.widget.arguments_json_array_9c85'))
        menu.add_editor(arguments)
        owner, scope = self.owner, self.client.scope()
        def submit():
            try:
                values = json.loads(arguments.toPlainText() or '[]')
                cmd = command.text().strip()
                if not cmd or not isinstance(values, list) or not all(isinstance(x, str) for x in values):
                    raise ValueError('Укажите команду и JSON-массив строк')
            except Exception as error:
                menu.set_error(str(error))
                return
            menu.close()
            self.submit(lambda: self.client.request('POST', BASE + '/' + owner,
                {'reference': 'manual:' + cmd, 'command': cmd, 'args': values, 'name': Path(cmd).name}, scope), installation=True)
        localize(add_caption_action(menu, 'addAction', 'Добавить'), 'ui.widget.add_7103').triggered.connect(submit)
        self._manual_menu = menu
        menu.show()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Добавить MCP', '', 'MCP (*.mcpb *.zip *.json);;Все файлы (*)')
        if path:
            bind_caption(self.reference, path)

    def add(self):
        reference = self.reference.text().strip()
        if not reference:
            set_text(self.message, 'ui.widget.enter_the_server_source_4bb6')
            return
        owner, scope = self.owner, self.client.scope()
        self.submit(lambda: self.client.request('POST', BASE + '/' + owner, {'reference': reference}, scope), installation=True)

    def action(self, identity, action):
        scope = self.client.scope()
        self.submit(lambda: self.client.request('POST', f'{BASE}/{identity[0]}/{identity[1]}/{action}', scope=scope))

    def save_autostart(self, enabled):
        if self._rendering or not self.selected:
            return
        identity, scope = self.selected, self.client.scope()
        self.submit(lambda: self.client.request('PATCH', f'{BASE}/{identity[0]}/{identity[1]}', {'autostart': enabled}, scope))

    def save(self):
        if not self.selected:
            return
        try:
            args = json.loads(self.args.toPlainText() or '[]')
            env = json.loads(self.env.toPlainText() or '{}')
            headers = json.loads(self.headers.toPlainText() or '{}')
            if not isinstance(args, list) or not isinstance(env, dict) or not isinstance(headers, dict):
                raise ValueError('Аргументы — массив; env и headers — объекты JSON')
            values = {key: field.value() for key, field in self.fields.items()}
            payload = {'visibility': self.visibility.currentData(), 'command': self.command.text(), 'args': args,
                       'transport': self.transport.currentData(), 'url': self.url.text(), 'timeout': self.timeout.value(),
                       **{key: checkbox.isChecked() for key, checkbox in self.capability_boxes.items()},
                       'oauth': self.oauth.isChecked(), 'values': {k: v for k, v in values.items() if v is not None}, 'env': env, 'headers': headers}
        except Exception as error:
            bind_caption(self.message, str(error))
            return
        identity, scope = self.selected, self.client.scope()
        self.submit(lambda: self.client.request('PATCH', f'{BASE}/{identity[0]}/{identity[1]}', payload, scope))

    def delete(self):
        if not self.selected or self.busy:
            return
        identity = self.selected
        def remove():
            if core_process.is_running():
                self.action(identity, 'delete')
            else:
                def offline_remove():
                    if core_process.is_running():
                        raise RuntimeError('Ядро запустилось. Повторите удаление через работающий MCP-клиент.')
                    from ..store import McpStore
                    McpStore().remove(*identity)
                    return {'message': tr('ui.caption.fc7dc2ec3dec')}
                self.submit(offline_remove, offline=True)
        inline.confirm(self, tr('ui.widget.remove_mcp_server_d4db'),
            tr('ui.widget.remove_this_server_from_the_module_b90d'), remove)
