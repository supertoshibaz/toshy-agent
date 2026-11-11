from ui.i18n import set_caption_table_headers, bind_caption, tr, localize, set_text, add_item, add_row
"""Settings window only; no model tools or normal module registration."""

import hashlib
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QListWidget, QListWidgetItem, QComboBox, QFileDialog, QFormLayout, QApplication, QAbstractItemView)
from ui.widgets.dialogs import ToolDialog as QDialog
from ui.styles.binding import apply_style
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.transport import core_http_client
from core.security import auth_headers

_windows = []


class ToshyMcpWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        set_text(self, 'ui.toshy_mcp.toshy_mcp_core_settings_a964', setter='setWindowTitle')
        self.resize(850, 720)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.runner = AsyncRunner(self)
        self.data = {}
        root = QVBoxLayout(self)
        self.enabled = localize(QCheckBox('', self), 'ui.toshy_mcp.expose_selected_toshy_capabilities_through_mcp_e076')
        self.agent_enabled = localize(QCheckBox('', self), 'ui.toshy_mcp.allow_ask_toshy_an_agent_task_be35')
        root.addWidget(self.enabled); root.addWidget(self.agent_enabled)
        form = QFormLayout()
        self.url = QLineEdit(self); self.url.setReadOnly(True)
        self.token = QLineEdit(self); self.token.setReadOnly(True); self.token.setEchoMode(QLineEdit.Password)
        connection = QHBoxLayout(); connection.addWidget(self.token)
        copy = localize(QPushButton('', self), 'ui.toshy_mcp.copy_token_fd08')
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.token.text()))
        connection.addWidget(copy)
        add_row(form, 'ui.toshy_mcp.streamable_http_e885', self.url); add_row(form, 'ui.toshy_mcp.bearer_token_b22a', connection)
        self.workspace = QLineEdit(self)
        choose = localize(QPushButton('', self), 'ui.toshy_mcp.directory_0a65'); choose.clicked.connect(self.choose_workspace)
        folder = QHBoxLayout(); folder.addWidget(self.workspace); folder.addWidget(choose)
        add_row(form, 'ui.chat_attachments.project_folder_31bf', folder)
        self.model = QComboBox(self); add_row(form, 'ui.subagents_panel.model_f3bd', self.model)
        root.addLayout(form)
        root.addWidget(localize(QLabel('', self), 'ui.toshy_mcp.modules_for_external_runs_normal_toshy_bd8c'))
        self.modules = QListWidget(self); self.modules.setMaximumHeight(130); root.addWidget(self.modules)
        apply_style(self.modules,'QListWidget QScrollBar::handle:vertical {background:@border.subtle@;border-radius:4px;} QListWidget QScrollBar::handle:vertical:hover {background:@text.secondary@;}')
        root.addWidget(localize(QLabel('', self), 'ui.toshy_mcp.published_pipelines_names_and_descriptions_can_997e'))
        self.table = QTableWidget(0, 4, self)
        set_caption_table_headers(self.table, [tr('ui.caption.d2ab5a1bb639'), tr('ui.caption.4a11b55556c7'), tr('ui.plugin_manager_pages.description_b368'), tr('ui.caption.9f165e8d9d37')])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        root.addWidget(self.table, 1)
        self.status = QLabel(self); self.status.setWordWrap(True); root.addWidget(self.status)
        row = QHBoxLayout()
        refresh = localize(QPushButton('', self), 'ui.project_history.refresh_603e'); refresh.clicked.connect(self.refresh)
        self.save = localize(QPushButton('', self), 'ui.settings.save_b4d3'); self.save.clicked.connect(self.apply)
        row.addWidget(refresh); row.addStretch(); row.addWidget(self.save); root.addLayout(row)
        self.refresh()

    @staticmethod
    def request(method='GET', body=None):
        with core_http_client('http://127.0.0.1:5000', timeout=30) as client:
            response = client.request(method, 'http://127.0.0.1:5000/v1/toshy-mcp', json=body, headers=auth_headers())
            if response.is_error:
                raise RuntimeError(response.json().get('detail', response.text))
            return response.json()

    def choose_workspace(self):
        path = QFileDialog.getExistingDirectory(self, 'Project Folder MCP', self.workspace.text())
        if path:
            bind_caption(self.workspace, path)

    def refresh(self):
        self.save.setEnabled(False)
        self.runner.submit(self.request, self.render, self.failed)

    def failed(self, error):
        bind_caption(self.status, str(error)); self.save.setEnabled(True)

    def render(self, data):
        self.data = data
        settings = data['settings']
        self.enabled.setChecked(settings['enabled']); self.agent_enabled.setChecked(settings['agent_enabled'])
        bind_caption(self.workspace, settings['workspace_root'])
        bind_caption(self.url, data['url']); bind_caption(self.token, data['token'])
        self.model.clear(); add_item(self.model, 'ui.toshy_mcp.toshy_model_selected_at_launch_8229', '')
        for profile in data.get('profiles', []):
            self.model.addItem(profile['name'], profile['id'])
        self.model.setCurrentIndex(max(0, self.model.findData(settings['model_profile_id'])))
        self.modules.clear()
        for module in data['modules']:
            item = QListWidgetItem(module['name'] + ('' if module['enabled'] else tr('ui.caption.63fe4bf60c2c')), self.modules)
            item.setData(Qt.UserRole, module['name'])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if module['name'] in settings['modules'] else Qt.Unchecked)
        exports = {e['pipeline']: e for e in settings['exports']}
        catalog = {p['pipeline']: p for p in data['pipelines']}
        refs = list(dict.fromkeys([*catalog, *exports]))
        self.table.setRowCount(len(refs))
        for row, ref in enumerate(refs):
            pipeline = catalog.get(ref, {'name': ref, 'errors': ['Пайплайн не найден']})
            export = exports.get(ref, {})
            checkbox = QTableWidgetItem('')
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            checkbox.setData(Qt.UserRole, ref)
            checkbox.setCheckState(Qt.Checked if export.get('enabled') else Qt.Unchecked)
            self.table.setItem(row, 0, checkbox)
            self.table.setItem(row, 1, QTableWidgetItem(export.get('name') or 'pipeline_' + hashlib.sha256(ref.encode()).hexdigest()[:12]))
            self.table.setItem(row, 2, QTableWidgetItem(export.get('description') or pipeline['name']))
            label = QTableWidgetItem(ref)
            label.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            bind_caption(label, '\n'.join(pipeline.get('errors', [])), setter='setToolTip')
            self.table.setItem(row, 3, label)
        self.table.resizeColumnsToContents()
        unavailable = [e['pipeline'] for e in settings['exports'] if e['enabled'] and (e['pipeline'] not in catalog or catalog[e['pipeline']].get('errors'))]
        message = (tr('ui.caption.4dcd0ed93c85') + ', '.join(unavailable) + tr('ui.caption.a293d7320287')) if unavailable else ''
        bind_caption(self.status, data.get('error') or message or tr('ui.caption.a151c52417b3'))
        self.save.setEnabled(True)

    def apply(self):
        settings = dict(self.data.get('settings', {}))
        settings.update(enabled=self.enabled.isChecked(), agent_enabled=self.agent_enabled.isChecked(),
            workspace_root=self.workspace.text(), model_profile_id=self.model.currentData() or '',
            modules=[self.modules.item(i).data(Qt.UserRole) for i in range(self.modules.count())
                if self.modules.item(i).checkState() == Qt.Checked])
        settings['exports'] = [{'pipeline': self.table.item(i, 0).data(Qt.UserRole),
            'enabled': self.table.item(i, 0).checkState() == Qt.Checked, 'name': self.table.item(i, 1).text().strip(),
            'description': self.table.item(i, 2).text().strip()} for i in range(self.table.rowCount())
                if self.table.item(i, 0).checkState() == Qt.Checked or self.table.item(i, 0).data(Qt.UserRole) in
                {e['pipeline'] for e in self.data.get('settings', {}).get('exports', [])}]
        self.save.setEnabled(False)
        self.runner.submit(lambda: self.request('PUT', settings), self.render, self.failed)


def open_settings(parent=None):
    for window in _windows:
        if window.isVisible():
            window.raise_(); window.activateWindow(); return window
    window = ToshyMcpWindow(parent)
    _windows.append(window)
    window.destroyed.connect(lambda: _windows.remove(window) if window in _windows else None)
    window.show()
    return window
