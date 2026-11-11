"""All model connection settings live in Startup → API. No endpoint presets."""
from __future__ import annotations
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text, add_item
from ui.styles.binding import apply_style
from ui.icons import set_icon


import copy
import json
import uuid

import httpx
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget)
from ui.widgets.dialogs import InputDialog as QInputDialog
from ui.widgets.dialogs import MessageBox as QMessageBox
from core.model_connections import (load_settings, save_settings, remove_connection, store_key,
                                    remember_runtime_key, resolve_key)
from core.security import auth_headers
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.transport import core_http_client
from ui.views.startup_view import SettingsPage, SettingRow


class CollapsiblePanel(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName('ApiConnectionCard')
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(8, 8, 8, 8)
        self.layout_box.setSpacing(4)
        self.header_layout = QHBoxLayout()
        self.toggle = QToolButton(self)
        bind_caption(self.toggle, title)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        self.toggle.setMinimumHeight(28)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        set_icon(self.toggle, 'right', size=16, icon_only=False)
        self.header_layout.addWidget(self.toggle, 1)
        self.layout_box.addLayout(self.header_layout)
        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 4, 0, 0)
        self.body_layout.setSpacing(0)
        self.layout_box.addWidget(self.body)
        self.body.hide()
        self.toggle.toggled.connect(self.set_expanded)

    def set_expanded(self, expanded):
        self.toggle.blockSignals(True)
        self.toggle.setChecked(expanded)
        self.toggle.blockSignals(False)
        set_icon(self.toggle, ('down' if expanded else 'right'), size=16, icon_only=False)
        self.body.setVisible(expanded)


class ConnectionList(QWidget):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.layout_box = QVBoxLayout(self)
        self.layout_box.setContentsMargins(12, 12, 12, 12)
        self.layout_box.setSpacing(10)

    def matches(self, tokens):
        # Search metadata, never the decrypted key field.
        text = 'конфигурации подключение шаблон api key key advanced model parameters request settings переменная ключа заголовок префикс ' + ' '.join(
            row._title + ' ' + row._description for row in self.page._editor_rows)
        text += ' ' + ' '.join(str(record.get(field, '')) for record in self.page.settings['connections']
                              for field in ('name', 'base_url', 'model', 'protocol', 'key_env'))
        return all(token in text.casefold() for token in tokens)


class ApiSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__('API', category_path=('API',), description=(
            tr('ui.caption.37c516533f37')), parent=parent)
        self.body_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.runner = AsyncRunner(self)
        self.settings = load_settings()
        self._loaded_id = None
        self._expanded_id = None
        self._key_drafts = {}
        self._loading_key = False
        self._key_visible = False
        self._reveal_request = 0
        self._busy_flag = False
        self._cards = {}
        self._editor_rows = []
        self.mode = QComboBox(self)
        add_item(self.mode, 'ui.api_settings.local_model_1ad4', 'local')
        add_item(self.mode, 'ui.api_settings.api_provider_23dc', 'api')
        self.name, self.url, self.model = (QLineEdit(self) for _ in range(3))
        self.protocol = QComboBox(self)
        from core.model_providers import providers
        self.provider = QComboBox(self)
        add_caption_item(self.provider, tr('api.provider.manual'), '')
        for identity, provider in providers().items():
            add_caption_item(self.provider, provider.title, identity)
        for title, value in [('OpenAI Chat Completions', 'chat_completions'), ('OpenAI Responses', 'responses'),
                             ('Anthropic Messages', 'anthropic_messages'), ('Gemini GenerateContent', 'gemini')]:
            add_caption_item(self.protocol, title, value)
        self.compatibility = CollapsiblePanel(tr('ui.caption.79b5bfb4673a'), self)
        self.reasoning_mode = QComboBox(self)
        for label, value in [(tr('ui.caption.353cf589e95b'), 'auto'), (tr('ui.caption.87f5ddbe0987'), 'reasoning_content'),
                             (tr('ui.caption.06d356badaa8'), 'reasoning'), (tr('ui.caption.a69a3573e4f5'), 'inline'),
                             (tr('ui.caption.cc6781f4b92b'), 'omit')]:
            add_caption_item(self.reasoning_mode, label, value)
        self.compat_tools, self.compat_images = QComboBox(self), QComboBox(self)
        for control in (self.compat_tools, self.compat_images):
            for label, value in [(tr('ui.caption.353cf589e95b'), 'auto'), (tr('ui.startup_view.enabled_92c1'), 'on'), (tr('ui.caption.3e95800065e0'), 'off')]:
                add_caption_item(control, label, value)
        self.key = QLineEdit(self)
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setReadOnly(True)
        set_text(self.key, 'ui.api_settings.credential_hidden_click_show_to_reveal_9925', setter='setPlaceholderText')
        self.key_env = QLineEdit(self)
        set_text(self.key_env, 'ui.api_settings.environment_variable_name_available_to_the_20c4', setter='setPlaceholderText')
        self.require_key = localize(QCheckBox('', self), 'ui.api_settings.credential_required_a2ff')
        self.key_storage = QComboBox(self)
        add_item(self.key_storage, 'ui.api_settings.secure_os_credential_vault_98c5', 'os')
        add_item(self.key_storage, 'ui.api_settings.until_the_application_closes_30f7', 'runtime')
        self.auth_header, self.auth_prefix = QLineEdit(self), QLineEdit(self)
        set_text(self.auth_header, 'ui.api_settings.automatic_by_protocol_ec6f', setter='setPlaceholderText')
        set_text(self.auth_prefix, 'ui.api_settings.automatic_by_protocol_ec6f', setter='setPlaceholderText')
        self.max_tokens = QSpinBox(self)
        self.max_tokens.setRange(0, 10000000)
        bind_caption(self.max_tokens, tr('ui.caption.52171cc53dbd'), setter='setSpecialValueText')
        self.effort_levels = QLineEdit(self)
        set_text(self.effort_levels, 'ui.api_settings.automatic_or_low_medium_high_69ee', setter='setPlaceholderText')
        self.temperature = QLineEdit(self)
        set_text(self.temperature, 'ui.api_settings.do_not_send_empty_1b1e', setter='setPlaceholderText')
        self.token_parameter = QLineEdit(self)
        set_text(self.token_parameter, 'ui.api_settings.max_tokens_c522', setter='setPlaceholderText')
        self.usage = localize(QCheckBox('', self), 'ui.api_settings.request_token_usage_statistics_947b')
        self.env_proxy = localize(QCheckBox('', self), 'ui.api_settings.use_system_proxy_settings_fe57')
        self.extra_body, self.extra_headers = QPlainTextEdit(self), QPlainTextEdit(self)
        for editor in (self.extra_body, self.extra_headers):
            editor.setMaximumHeight(110)
            bind_caption(editor, '{}', setter='setPlaceholderText')
        set_text(self.extra_body, 'ui.api_settings.json_for_example_reasoning_thinking_options_0f5a', setter='setToolTip')
        apply_style(self, '\n            QFrame#ApiConnectionCard { border: 1px solid @border.subtle@; border-radius: 6px; background: @background.panel@; }\n            QToolButton { border: none; padding: 5px; text-align: left; }\n            QWidget#ApiSaveBar, QLabel#ApiSaveStatus { background: transparent; border: none; }\n        ')

        def row(title, description, control, layout=None, group=None):
            control.setMinimumWidth(350)
            item = SettingRow(title, description, control, parent=self)
            if layout is None:
                self.add_row(item)
            else:
                layout.addWidget(item)
                self._editor_rows.append(item)
            if group is not None:
                group.append(item)
            return item

        self.connection_list = ConnectionList(self)
        self.add_row(self.connection_list)
        list_header = QWidget(self)
        header_layout = QHBoxLayout(list_header)
        header_layout.setContentsMargins(0, 0, 0, 4)
        header_layout.addWidget(localize(QLabel('', self), 'ui.api_settings.connections_93dc'), 1)
        self.create_button = localize(QPushButton('', self), 'ui.api_settings.create_template_798f')
        header_layout.addWidget(self.create_button)
        self.connection_list.layout_box.addWidget(list_header)
        self.empty_connections = localize(QLabel('', self), 'ui.api_settings.create_a_local_or_cloud_connection_ef8b')
        self.empty_connections.setWordWrap(True)
        self.connection_list.layout_box.addWidget(self.empty_connections)
        self.active_group = QButtonGroup(self)
        self.active_group.setExclusive(True)
        self.editor = QWidget(self)
        self.editor_layout = QVBoxLayout(self.editor)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        self.editor_layout.setSpacing(0)
        self.editor.hide()
        row(tr('ui.caption.3f53fb873461'), tr('ui.caption.b7153d1f0595'), self.mode, self.editor_layout)
        row(tr('api.provider.title'), tr('api.provider.description'), self.provider, self.editor_layout)
        self.catalog_button = localize(QPushButton('', self), 'ui.api_settings.fetch_model_list_7687')
        for title, description, widget in [
            (tr('appearance.name'), tr('ui.caption.14d2a3fc4214'), self.name),
            (tr('ui.caption.b2ab03201ab8'), tr('ui.caption.afd4636a337e'), self.url),
            (tr('ui.caption.b3680d4b2071'), tr('ui.caption.abef0e1cc9f9'), self.protocol),
            (tr('ui.subagents_panel.model_f3bd'), tr('ui.caption.900228f97e30'), self._model_control(self.model, self.catalog_button)),
        ]:
            row(title, description, widget, self.editor_layout)
        self.api_key_panel = CollapsiblePanel(tr('api.key_section'), self)
        self.editor_layout.addWidget(self.api_key_panel)
        row(tr('ui.caption.59664d4bbf35'), tr('ui.caption.f0a542582a84'),
            self.require_key, self.api_key_panel.body_layout)
        key_control = QWidget(self)
        key_layout = QHBoxLayout(key_control)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.key, 1)
        self.reveal_key_button = localize(QPushButton('', self), 'ui.api_settings.show_1e5b')
        self.forget_button = localize(QPushButton('', self), 'ui.api_settings.delete_credential_43c8')
        key_layout.addWidget(self.reveal_key_button)
        key_layout.addWidget(self.forget_button)
        row(tr('ui.caption.7a63ad80c1fc'), tr('ui.caption.070e1cf27410'),
            key_control, self.api_key_panel.body_layout)
        row(tr('ui.caption.16e0de655a13'), tr('ui.caption.d8aa63113f5f'),
            self.key_storage, self.api_key_panel.body_layout)
        self.key_advanced = CollapsiblePanel(tr('api.key_advanced'), self)
        self.editor_layout.addWidget(self.key_advanced)
        for title, description, widget in [
            (tr('ui.caption.37ac91884083'), tr('ui.caption.4e2754c43ded'), self.key_env),
            (tr('ui.caption.431400d0b6f2'), tr('ui.caption.e56cc6688b53'), self.auth_header),
            (tr('ui.caption.b22866836b06'), tr('ui.caption.e2a175f99a3d'), self.auth_prefix),
        ]:
            row(title, description, widget, self.key_advanced.body_layout)
        self.model_parameters = CollapsiblePanel(tr('api.model_parameters'), self)
        self.editor_layout.addWidget(self.model_parameters)
        for title, description, widget in [
            (tr('ui.caption.75f2a7f7d2e5'), tr('ui.caption.de546601d182'), self.max_tokens),
            (tr('ui.caption.7091d5edb00c'), tr('ui.caption.6960b1fd0112'), self.token_parameter),
            ('Temperature', tr('ui.caption.7ee452d2be49'), self.temperature),
            (tr('ui.caption.fe024288d980'), tr('ui.caption.16a5271e74b8'), self.usage),
        ]:
            row(title, description, widget, self.model_parameters.body_layout)
        self.request_settings = CollapsiblePanel(tr('api.request_settings'), self)
        self.editor_layout.addWidget(self.request_settings)
        for title, description, widget in [
            (tr('ui.caption.a567776c7974'), tr('ui.caption.9ca7c2266186'), self.env_proxy),
            (tr('ui.caption.f58f7cac3611'), tr('ui.caption.3aa993afec4f'), self.effort_levels),
            (tr('ui.caption.eb3ed679dc2f'), tr('ui.caption.93336e3e3f14'), self.extra_body),
            (tr('ui.caption.3e056bc5b5b1'), tr('ui.caption.4e3cbbee2f54'), self.extra_headers),
        ]:
            row(title, description, widget, self.request_settings.body_layout)
        self.editor_layout.addWidget(self.compatibility)
        for title, description, widget in [
            (tr('ui.caption.d891a2ae33f0'), tr('ui.caption.6acbdc592942'), self.reasoning_mode),
            (tr('ui.execution_settings.tools_4e90'), tr('ui.caption.b47af5b24882'), self.compat_tools),
            (tr('ui.caption.0a6258a5f30f'), tr('ui.caption.20a1d05c2804'), self.compat_images),
        ]:
            row(title, description, widget, self.compatibility.body_layout)
        self.save_button = localize(QPushButton('', self), 'ui.settings.save_b4d3')
        actions = QWidget(self)
        actions.setObjectName('ApiSaveBar')
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 12, 0, 0)
        actions_layout.setSpacing(12)
        actions_layout.addWidget(self.save_button)
        self.save_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.status = QLabel(actions)
        self.status.setObjectName('ApiSaveStatus')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        actions_layout.addWidget(self.status, 1)
        self.body_layout.insertWidget(self.body_layout.indexOf(self.rows_host) + 1, actions)
        self._refresh_connections()
        self.mode.currentIndexChanged.connect(self._update_visibility)
        from PySide6.QtWidgets import QMenu
        create_menu = QMenu(self.create_button)
        for title, kind in [(tr('ui.api_settings.local_model_1ad4'), 'local'), (tr('ui.api_settings.api_provider_23dc'), 'api')]:
            add_caption_action(create_menu, 'addAction', title).triggered.connect(lambda checked=False, kind=kind: self._add_connection(mode=kind))
        self.create_button.setMenu(create_menu)
        self.save_button.clicked.connect(lambda: self._save_then())
        self.catalog_button.clicked.connect(lambda: self._save_then('models'))
        self.forget_button.clicked.connect(self._forget_key)
        self.reveal_key_button.clicked.connect(lambda: self._toggle_key())
        self.api_key_panel.toggle.toggled.connect(lambda expanded: None if expanded else self._hide_key())
        self.key.textChanged.connect(self._key_edited)
        self.name.textChanged.connect(self._update_card_title)
        self.url.textChanged.connect(self._update_catalog_buttons)
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self._update_visibility()

    def showEvent(self, event):
        super().showEvent(event)
        # A plugin widget can add a profile while this settings page exists.
        # Merge additions without replacing unsaved drafts in existing cards.
        if not hasattr(self, 'settings') or self._busy_flag:
            return
        fresh = load_settings()
        known = {item['id'] for item in self.settings['connections']}
        added = [item for item in fresh['connections'] if item['id'] not in known]
        if added:
            try:
                self._capture()
            except (ValueError, TypeError):
                return  # Keep the invalid unsaved draft for the user to correct.
            self.settings['connections'].extend(added)
            if not self.settings.get('selected'):
                self.settings['selected'] = fresh.get('selected')
            self._refresh_connections()

    def _provider_changed(self):
        from core.model_providers import providers
        provider = providers().get(self.provider.currentData())
        if provider is not None:
            defaults = provider.configure({})
            self.url.setText(defaults.get('base_url', ''))
            self.protocol.setCurrentIndex(max(0, self.protocol.findData(defaults.get('protocol', 'responses'))))
            self.require_key.setChecked(defaults.get('require_key', False))
        self._update_visibility()

    def _model_control(self, editor, button):
        control = QWidget(self)
        layout = QVBoxLayout(control)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        editor.setMinimumHeight(26)
        button.setMinimumHeight(28)
        layout.addWidget(editor)
        layout.addWidget(button)
        return control

    def _refresh_connections(self):
        self.editor.hide()
        self.editor.setParent(self)
        for card in self._cards.values():
            self.active_group.removeButton(card.active)
            card.hide()
            self.connection_list.layout_box.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        records = self.settings['connections']
        self.empty_connections.setVisible(not records)
        for record in records:
            identity = record['id']
            card = CollapsiblePanel(record.get('name') or tr('ui.settings.connection_ab71'), self.connection_list)
            card.active = localize(QRadioButton('', card), 'ui.api_settings.default_fd05')
            card.active.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.active_group.addButton(card.active)
            card.active.setChecked(identity == self.settings.get('selected'))
            card.header_layout.addWidget(card.active)
            remove = localize(QPushButton('', card), 'ui.widget.delete_be99')
            remove.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            card.header_layout.addWidget(remove)
            card.toggle.toggled.connect(lambda expanded, identity=identity: self._expand_connection(identity, expanded))
            card.active.toggled.connect(lambda checked, identity=identity: self._select_connection(identity, checked))
            remove.clicked.connect(lambda checked=False, identity=identity: self._remove_connection(identity))
            self.connection_list.layout_box.addWidget(card)
            self._cards[identity] = card
        identity = self._loaded_id if self._loaded_id in self._cards else self.settings.get('selected')
        if identity not in self._cards and records:
            identity = records[0]['id']
        self._switch_connection(identity)
        if identity:
            self._expand_connection(identity, True)
        self._update_catalog_buttons()

    def _select_connection(self, identity, checked):
        if checked:
            self.settings['selected'] = identity

    def _expand_connection(self, identity, expanded):
        if not expanded:
            self._cards[identity].set_expanded(False)
            if self._expanded_id == identity:
                self.editor.hide()
                self._hide_key()
                self._expanded_id = None
            return
        if self._loaded_id != identity and not self._switch_connection(identity):
            self._cards[identity].set_expanded(False)
            return
        for card_id, card in self._cards.items():
            card.set_expanded(card_id == identity)
        card = self._cards[identity]
        card.body_layout.addWidget(self.editor)
        self.editor.show()
        self._expanded_id = identity

    def _record(self):
        return next((item for item in self.settings['connections'] if item['id'] == self._loaded_id), None)

    def _capture(self):
        record = self._record()
        if record is None:
            return
        # Parse before mutation so an invalid draft cannot partially overwrite a profile.
        temperature = self.temperature.text().strip()
        updates = dict(name=self.name.text().strip(), base_url=self.url.text().strip(), model=self.model.text().strip(),
                       compat_reasoning=self.reasoning_mode.currentData(), compat_tools=self.compat_tools.currentData(),
                       compat_images=self.compat_images.currentData(), mode=self.mode.currentData(), protocol=self.protocol.currentData(), key_env=self.key_env.text().strip(), require_key=self.require_key.isChecked(),
                       max_tokens=self.max_tokens.value(), include_usage=self.usage.isChecked(), trust_env=self.env_proxy.isChecked(),
                       key_storage=self.key_storage.currentData(), temperature=float(temperature) if temperature else None)
        levels = [level.strip() for level in self.effort_levels.text().split(',') if level.strip()]
        if levels:
            record['reasoning_effort_levels'] = levels
        else:
            record.pop('reasoning_effort_levels', None)
        for field, editor in [('extra_body', self.extra_body), ('extra_headers', self.extra_headers)]:
            updates[field] = json.loads(editor.toPlainText().strip() or '{}')
        record.update(updates)
        record['provider'] = (self.provider.currentData() or '') if self.mode.currentData() == 'api' else ''
        for field, editor in [('auth_header', self.auth_header), ('auth_prefix', self.auth_prefix), ('token_parameter', self.token_parameter)]:
            if editor.text():
                record[field] = editor.text()
            else:
                record.pop(field, None)

    def _switch_connection(self, identity=None):
        try:
            self._capture()
        except (ValueError, TypeError) as error:
            bind_caption(self.status, tr('ui.caption.82ba17684d1d') + str(error))
            return False
        self._hide_key()
        for panel in (self.api_key_panel, self.key_advanced, self.model_parameters, self.request_settings):
            panel.set_expanded(False)
        self._loaded_id = identity
        record = self._record() or {}
        provider_id = record.get('provider', '')
        if provider_id and self.provider.findData(provider_id) < 0:
            add_caption_item(self.provider, provider_id + ' (' + tr('api.provider.unavailable') + ')', provider_id)
        self.provider.blockSignals(True)
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider_id)))
        self.provider.blockSignals(False)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(record.get('mode', 'api'))))
        self.mode.blockSignals(False)
        bind_caption(self.name, record.get('name', ''))
        bind_caption(self.url, record.get('base_url', ''))
        bind_caption(self.model, record.get('model', ''))
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(record.get('protocol', 'chat_completions'))))
        for control, field in ((self.reasoning_mode, 'compat_reasoning'), (self.compat_tools, 'compat_tools'), (self.compat_images, 'compat_images')):
            control.setCurrentIndex(max(0, control.findData(record.get(field, 'auto'))))
        self.compatibility.set_expanded(False)
        bind_caption(self.key_env, record.get('key_env', ''))
        self.key_storage.setCurrentIndex(max(0, self.key_storage.findData(record.get('key_storage', 'os'))))
        self.require_key.setChecked(record.get('require_key', True))
        self.max_tokens.setValue(int(record.get('max_tokens') or 0))
        self.usage.setChecked(record.get('include_usage', True))
        self.env_proxy.setChecked(record.get('trust_env', True))
        bind_caption(self.effort_levels, ', '.join(record.get('reasoning_effort_levels') or []))
        bind_caption(self.temperature, '' if record.get('temperature') is None else str(record['temperature']))
        for field, editor in [('auth_header', self.auth_header), ('auth_prefix', self.auth_prefix), ('token_parameter', self.token_parameter)]:
            bind_caption(editor, record.get(field, ''))
        self.extra_body.setPlainText(json.dumps(record.get('extra_body') or {}, ensure_ascii=False, indent=2))
        self.extra_headers.setPlainText(json.dumps(record.get('extra_headers') or {}, ensure_ascii=False, indent=2))
        self._update_visibility()
        return True

    def _update_card_title(self):
        card = self._cards.get(self._loaded_id)
        if card:
            bind_caption(card.toggle, self.name.text().strip() or tr('ui.settings.connection_ab71'))

    def _add_connection(self, checked=False, *, mode='api'):
        try:
            self._capture()
        except (ValueError, TypeError) as error:
            bind_caption(self.status, tr('ui.caption.a6785942fedb') + str(error))
            return
        record = {'id': uuid.uuid4().hex, 'name': tr('ui.api_settings.local_model_1ad4') if mode == 'local' else tr('ui.settings.connection_ab71'),
                  'mode': mode, 'protocol': 'chat_completions', 'require_key': mode == 'api'}
        self.settings['connections'].append(record)
        if not self.settings.get('selected'):
            self.settings['selected'] = record['id']
        self._refresh_connections()
        self._expand_connection(record['id'], True)

    def _remove_connection(self, identity=None):
        identity = identity or self._loaded_id
        if identity != self._loaded_id:
            try:
                self._capture()
            except (ValueError, TypeError) as error:
                bind_caption(self.status, tr('ui.caption.a6785942fedb') + str(error))
                return
        if not identity:
            return
        try:
            remove_connection(identity)
        except Exception as error:
            bind_caption(self.status, tr('ui.caption.d4c8d9f33807') + str(error))
            return
        self.settings['connections'] = [item for item in self.settings['connections'] if item['id'] != identity]
        self._key_drafts.pop(identity, None)
        if self._loaded_id == identity:
            self._loaded_id = None
        if self.settings.get('selected') == identity:
            self.settings['selected'] = (self.settings['connections'][0]['id'] if self.settings['connections'] else None)
        self._refresh_connections()
        from ui.event_bus import event_bus
        event_bus.model_profiles_changed.emit()
        set_text(self.status, 'api.connection_removed')

    def _update_visibility(self):
        local = self.mode.currentData() == 'local'
        if local:
            self._hide_key()
        for panel in (self.api_key_panel, self.key_advanced, self.model_parameters, self.request_settings):
            panel.setVisible(not local)
        from core.model_providers import providers
        provider = providers().get(self.provider.currentData())
        if provider is not None and getattr(provider, 'owns_auth', False):
            self.api_key_panel.hide()
            self.key_advanced.hide()
        self.provider.parentWidget().setVisible(not local)
        self.protocol.parentWidget().setVisible(not local)
        self.compatibility.setVisible(True)
        self.catalog_button.setVisible(not local)
        self._update_catalog_buttons()

    def _update_catalog_buttons(self):
        if self._busy_flag:
            return
        self.catalog_button.setEnabled(self.mode.currentData() == 'api' and bool(self.url.text().strip()) and self._record() is not None)

    def apply_filter(self, query):
        return super().apply_filter(query)

    def _key_edited(self):
        if not self._loading_key and self._loaded_id:
            self._key_drafts[self._loaded_id] = self.key.text()

    def _hide_key(self):
        self._reveal_request += 1
        self._key_visible = False
        set_text(self.reveal_key_button, 'ui.api_settings.show_1e5b')
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setReadOnly(True)
        set_text(self.key, 'ui.api_settings.credential_hidden_click_show_to_reveal_9925', setter='setPlaceholderText')
        self._loading_key = True
        self.key.clear()
        self._loading_key = False

    def hideEvent(self, event):
        self._hide_key()
        super().hideEvent(event)

    def _toggle_key(self):
        if self._key_visible:
            self._hide_key()
            return
        answer = QMessageBox.question(self, tr('ui.api_settings.reveal_api_credential_d3b4'),
                                      tr('ui.api_settings.the_credential_will_be_shown_as_b5f3'),
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        identity = self._loaded_id
        if identity in self._key_drafts:
            self._show_key(self._key_drafts[identity])
            return
        record = copy.deepcopy(self._record() or {})
        # The input edits the saved/runtime key. The optional environment override remains in Key advanced.
        record.pop('key_env', None)
        self._reveal_request += 1
        request = self._reveal_request
        self._busy(True, tr('ui.caption.107bbfd51414'))
        def revealed(value):
            if request == self._reveal_request:
                self._show_key(value)
            else:
                self._busy(False, '')
        self.runner.submit(lambda: resolve_key(record), revealed, self._failed)

    def _show_key(self, value):
        self._busy(False, '')
        self._loading_key = True
        bind_caption(self.key, value)
        self._loading_key = False
        self._key_visible = True
        self.key.setEchoMode(QLineEdit.Normal)
        self.key.setReadOnly(False)
        set_text(self.key, 'ui.api_settings.enter_api_credential_eb1f', setter='setPlaceholderText')
        set_text(self.reveal_key_button, 'ui.api_settings.hide_0a2a')

    def _save_then(self, operation=None):
        if operation == 'models' and self.mode.currentData() != 'api':
            return
        try:
            self._capture()
            if (operation or self.settings['connections']) and not self.settings.get('selected'):
                raise ValueError(tr('api.create_connection_required'))
            if operation == 'models':
                from core.model_connections import validate_connection
                validate_connection(dict(self._record(), model='catalog'))
            self.settings = save_settings(self.settings)
        except Exception as error:
            bind_caption(self.status, tr('ui.caption.d4c8d9f33807') + str(error))
            return
        records = {item['id']: copy.deepcopy(item) for item in self.settings['connections']}
        drafts = dict(self._key_drafts)
        identity = self._loaded_id
        def execute():
            from core.model_connections import push_runtime_keys
            cleared = []
            for key_id, key in drafts.items():
                if records[key_id].get('key_storage', 'os') == 'os':
                    store_key(key_id, key)
                if not key:
                    cleared.append(key_id)
                remember_runtime_key(key_id, key)
            if drafts:
                push_runtime_keys(clear=cleared)
            if not operation:
                return None
            payload = {'operation': operation}
            if identity:
                payload['connection_id'] = identity
            with core_http_client('http://127.0.0.1:5000', timeout=90) as client:
                response = client.post('http://127.0.0.1:5000/v1/model/check', json=payload, headers=auth_headers())
                response.raise_for_status()
                return operation, response.json()
        self._busy(True, tr('ui.caption.0a3568631d12') if operation else tr('ui.caption.bd97450c8556'))
        self.runner.submit(execute, self._saved, self._failed)

    def _busy(self, busy, text=None):
        self._busy_flag = busy
        self.rows_host.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        bind_caption(self.status, text if text is not None else (tr('ui.caption.bd97450c8556') if busy else tr('ui.backup_settings.settings_saved_db02')))
        if not busy:
            self._update_catalog_buttons()

    def _saved(self, result):
        self._busy(False)
        from ui.event_bus import event_bus
        event_bus.model_profiles_changed.emit()
        self._key_drafts.clear()
        self._hide_key()
        if result is None:
            return
        operation, data = result
        if operation == 'models':
            models = data.get('models') or []
            if not models:
                set_text(self.status, 'ui.api_settings.the_server_returned_no_model_list_5d43')
                return
            model, accepted = QInputDialog.getItem(self, tr('ui.api_settings.server_models_48a9'), tr('ui.api_settings.select_model_c4da'), models, 0, False)
            if accepted:
                bind_caption(self.model, model)
                self._save_then()

    def _failed(self, error):
        self._busy(False)
        message = str(error)
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                payload = {}
            detail = (payload.get('detail') or payload.get('error') or payload.get('message')) if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('detail')
            message = f'HTTP {response.status_code}: ' + (str(detail) if detail else tr('ui.caption.457662987ce6'))
        # The core redacts provider errors. Also protect unsaved GUI keys when
        # a credential or local endpoint error echoes an entered value.
        for secret in [self.key.text(), *self._key_drafts.values()]:
            if secret:
                message = message.replace(secret, tr('ui.caption.b6740277873f'))
        bind_caption(self.status, message[:4000])

    def _forget_key(self):
        identity = self._loaded_id
        if not identity:
            return
        self._busy(True, tr('ui.caption.435327fb884f'))
        storage = self.key_storage.currentData()
        def forget():
            if storage == 'os':
                store_key(identity, '')
            remember_runtime_key(identity, '')
            from core.model_connections import push_runtime_keys
            push_runtime_keys(clear=[identity])
        def forgotten(_):
            self._key_drafts.pop(identity, None)
            self._hide_key()
            self._busy(False, tr('api.key_removed'))
        self.runner.submit(forget, forgotten, self._failed)
