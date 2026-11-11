"""The built-in Terminal dock hosts this module-owned command console.

Intentionally not named ModuleWidget: avoid a second automatically discovered dock.
"""
from __future__ import annotations
from ui.i18n import i18n, add_caption_action, bind_caption, add_caption_item, add_caption_items, add_caption_tab, tr, localize, set_text, add_item
from ui.styles.binding import form_field
from ui.icons import set_icon, symbol_button


import json
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSize
from ui.settings import QSettings
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox, QPushButton, QToolButton, QStackedWidget, QPlainTextEdit, QLineEdit, QCheckBox, QDoubleSpinBox, QSpinBox, QScrollArea, QTabWidget, QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QGroupBox, QMenu, QSizePolicy, QLayout)
from ui.widgets.dialogs import MessageBox as QMessageBox
from ui.widgets.dialogs import Dialog as QDialog

from core.security import auth_headers
from ui.app_state import app_state
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.transport import core_http_client
from ui.event_bus import event_bus
from shiboken6 import isValid
from ..store import ContainerStore
from ..runtime import TERMINAL_STATES
from .console import TerminalConsole, TerminalRail, ContainerSelector
from .feed_client import FeedClient
from ui.tab_memory import performance_preferences

BASE = '/v1/modules/terminal_sandbox/service'


def profile_caption(profile):
    defaults = {'shared': ('Shared terminal', 'ui.caption.33c8bc0b777a'),
                'local': ('This computer', 'terminal.this_computer')}
    default = defaults.get(profile.get('id'))
    name = profile.get('name', '')
    return tr(default[1]) if default and name == default[0] else name


class TerminalClient:
    def request(self, method='GET', path='', body=None, scope=None):
        params = dict(scope or {})
        with core_http_client(core_process.base_url, timeout=1860 if path == 'images/import' else 25) as client:
            response = client.request(method, core_process.base_url + BASE + ('/' + path if path else ''),
                                      params=params, json=body, headers=auth_headers())
        if response.is_error:
            try:
                detail = response.json().get('detail', response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(str(detail))
        return response.json()


class _SettingCheckboxLabel(QLabel):
    def __init__(self, checkbox, parent):
        super().__init__(parent)
        self.checkbox = checkbox

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.checkbox.isEnabled():
            self.checkbox.click()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TerminalWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.store, self.client = ContainerStore(), TerminalClient()
        self.runner = AsyncRunner(self)
        self.feed_client = FeedClient(self)
        self.completion_client = FeedClient(self)
        self.ui_preferences = QSettings(str(self.store.root/'terminal-ui.ini'),QSettings.IniFormat)
        self.catalog,self.current_id,self.form_id = {},str(self.ui_preferences.value('profile','')),''
        self.auto_select = self.ui_preferences.value('auto',True,type=bool)
        self.polling, self.reading, self.action_busy = False, False, False
        self.generation = 0
        self.cursors, self.drafts = {}, {}
        self.job_states = {}
        self.feed_ready = False
        self.revision = None
        self.feed_epoch = None
        self.approval_dialogs = {}
        self._sent_credentials = set()
        self._available = False
        self._module_enabled=self.configured_module_enabled()
        self.editing_new = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self.top_panel = QWidget(self)
        self.top_panel.setObjectName('TerminalTop')
        top = QHBoxLayout(self.top_panel)
        top.setContentsMargins(3,3,3,3)
        top.setSpacing(4)
        self.selector = ContainerSelector(self)
        self.selector.setMinimumWidth(0)
        self.selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.selector.currentIndexChanged.connect(self.select_container)
        top.addWidget(self.selector, 1)
        self.console_button, self.settings_button = QToolButton(self), QToolButton(self)
        for button, settings in ((self.console_button, False), (self.settings_button, True)):
            button.setCheckable(True)
            button.setFixedSize(28, 28)
            set_icon(button, 'settings' if settings else 'terminal', size=18)
            button.setObjectName('TerminalToolbarButton')
            bind_caption(button, tr('ui.caption.985b5e0f2ccf') if settings else tr('ui.caption.a58f47dbc474'), setter='setToolTip')
            top.addWidget(button)
        self.add_button = QToolButton(self)
        set_icon(self.add_button, 'module', size=18, square=True)
        self.add_button.setObjectName('TerminalToolbarButton')
        self.add_button.setFixedSize(28,28)
        set_text(self.add_button, 'ui.widget.add_connection_19bb', setter='setToolTip')
        self.add_button.clicked.connect(self.new_menu)
        top.insertWidget(2,self.add_button)
        self.console_button.clicked.connect(lambda: self.set_mode(0))
        self.settings_button.clicked.connect(lambda: self.set_mode(1))
        layout.addWidget(self.top_panel)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(88)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.status.hide()
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)
        self.build_console()
        self.build_settings()
        self.set_mode(self.ui_preferences.value("mode",0,type=int))
        performance_preferences.changed.connect(self.preferences_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        app_state.session_changed.connect(self.context_changed)
        app_state.project_changed.connect(self.context_changed)
        app_state.workspace_changed.connect(self.context_changed)
        core_process.status_changed.connect(self.context_changed)
        event_bus.module_status_changed.connect(self.context_changed)
        event_bus.session_renamed.connect(self.session_renamed)
        QTimer.singleShot(0, self.poll)

    def build_console(self):
        from .session_tabs import SessionTabs
        self.terminal_tabs=SessionTabs(self)
        self.stack.addWidget(self.terminal_tabs)










    def preferences_changed(self):
        self.terminal_tabs.preferences_changed()


    def notice(self, text):
        if not isValid(self) or not isValid(self.status):
            return
        bind_caption(self.status, str(text))
        self.status.setVisible(bool(text))

    def build_settings(self):
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        page = QWidget(scroll)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget(page)
        layout.addWidget(tabs)
        general = QWidget(tabs)
        settings = QVBoxLayout(general)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.name = QLineEdit(general)
        self._default_name_profile = None
        self.name.textEdited.connect(lambda _: setattr(self, '_default_name_profile', None))
        i18n.language_changed.connect(self._refresh_default_name)
        self.active = localize(QCheckBox(''), 'ui.widget.active_allow_command_execution_af89')
        set_text(self.active, 'ui.widget.enabling_this_does_not_start_a_7784', setter='setToolTip')
        self.save_history = localize(QCheckBox(''), 'ui.widget.keep_history_between_launches_20aa')
        set_text(self.save_history, 'ui.widget.when_disabled_the_feed_is_cleared_da1f', setter='setToolTip')
        self.timeout = QSpinBox(general); self.timeout.setRange(1, 86400); bind_caption(self.timeout, tr('ui.caption.ddae8eb8bf21'), setter='setSuffix')
        set_text(self.timeout, 'ui.widget.timeout_for_ordinary_commands_background_and_5d9b', setter='setToolTip')
        for title, field in [(tr('ui.caption.1da7e9372e2f'),self.name),('',self.active),('',self.save_history),(tr('ui.caption.3db4ee128449'),self.timeout)]:
            form.addRow(title,field)
        settings.addLayout(form)
        self.docker_group = localize(QGroupBox('', general), 'ui.widget.docker_e616', setter='setTitle')
        docker = QFormLayout(self.docker_group)
        docker.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.preserve = localize(QCheckBox(''), 'ui.widget.keep_container_environment_3928')
        self.network = localize(QCheckBox(''), 'ui.widget.allow_container_network_access_ad5b')
        set_text(self.network, 'ui.widget.disabled_by_default_enabling_this_permits_43a6', setter='setToolTip')
        self.image = QComboBox(general); self.image.setEditable(True); self.image.setMinimumWidth(0)
        image_row = QWidget(general); row = QHBoxLayout(image_row); row.setContentsMargins(0,0,0,0)
        row.addWidget(self.image,1)
        refresh, load = set_icon(QPushButton(''), 'refresh', square=True), localize(QPushButton(''), 'ui.widget.import_f684')
        refresh.setFixedWidth(28); refresh.clicked.connect(self.refresh_images); load.clicked.connect(self.import_image)
        row.addWidget(refresh); row.addWidget(load)
        self.cpus = QDoubleSpinBox(general); self.cpus.setRange(.1,256); self.cpus.setSingleStep(.5)
        self.memory = QSpinBox(general); self.memory.setRange(64,1048576); bind_caption(self.memory, ' MiB', setter='setSuffix')
        self.pids = QSpinBox(general); self.pids.setRange(16,65536)
        self.run_as_root = localize(QCheckBox(''), 'ui.widget.root_inside_the_container_a5c4')
        set_text(self.run_as_root, 'ui.widget.enabled_by_default_to_allow_system_e3d0', setter='setToolTip')
        self.restrict_writes = localize(QCheckBox(''), 'ui.widget.write_only_to_the_home_directory_2d8b')
        set_text(self.restrict_writes, 'ui.widget.makes_container_system_directories_read_only_3e39', setter='setToolTip')
        hint = localize(QLabel(''), 'ui.widget.the_shared_terminal_keeps_its_mounted_cf87')
        hint.setWordWrap(True)
        self.environment = form_field(QPlainTextEdit(general)); set_text(self.environment, 'ui.widget.name_value_one_variable_per_line_48e3', setter='setPlaceholderText')
        self.environment.setMaximumHeight(110)
        for title, field in [(tr('ui.caption.8faae1200ce5'),image_row),('',self.preserve),('',self.network),('CPU',self.cpus),
            (tr('nav.memory'),self.memory),(tr('ui.caption.5237b416ea0e'),self.pids),('',self.run_as_root),('',self.restrict_writes),('',hint),(tr('ui.caption.9dfff1c7db53'),self.environment)]:
            docker.addRow(title,field)
        settings.addWidget(self.docker_group)
        self.ssh_group = localize(QGroupBox('', general), 'ui.widget.ssh_01c4', setter='setTitle')
        ssh = QFormLayout(self.ssh_group); ssh.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.ssh_host, self.ssh_user, self.ssh_key, self.ssh_root = [QLineEdit(general) for _ in range(4)]
        self.ssh_port = QSpinBox(general); self.ssh_port.setRange(1,65535)
        self.target_os = QComboBox(general); add_item(self.target_os, 'ui.widget.linux_4828', 'linux'); add_item(self.target_os, 'ui.widget.windows_d598', 'windows')
        self.ssh_password = QLineEdit(general); self.ssh_password.setEchoMode(QLineEdit.Password)
        set_text(self.ssh_password, 'ui.widget.password_or_key_passphrase_leave_empty_0c01', setter='setPlaceholderText')
        set_text(self.ssh_password, 'ui.widget.stored_in_the_os_credential_vault_a5b8', setter='setToolTip')
        set_text(self.ssh_key, 'ui.widget.private_key_path_leave_empty_for_11c6', setter='setPlaceholderText')
        set_text(self.ssh_root, 'ui.widget.remote_directory_leave_empty_for_the_8f58', setter='setPlaceholderText')
        for title,field in [(tr('ui.caption.da82e80563d7'),self.ssh_host),(tr('ui.settings.port_9094'),self.ssh_port),(tr('ui.caption.2a5c42af7c00'),self.ssh_user),(tr('ui.caption.e8f445570eec'),self.target_os),
            (tr('ui.caption.fc8c758ee643'),self.ssh_key),(tr('ui.settings.password_cb1a'),self.ssh_password),(tr('ui.caption.93a81a906787'),self.ssh_root)]:
            ssh.addRow(title,field)
        forget = localize(QPushButton(''), 'ui.widget.forget_verified_server_key_49fa')
        forget.clicked.connect(lambda: self.run_action(lambda: self.client.request('POST','forget-host',{'profile_id':self.current_id}),message=''))
        ssh.addRow(forget)
        ssh_hint = localize(QLabel(''), 'ui.widget.the_ssh_server_must_permit_commands_97b2')
        ssh_hint.setWordWrap(True); ssh.addRow(ssh_hint)
        settings.addWidget(self.ssh_group)
        self.host_group = localize(QGroupBox('', general), 'ui.widget.command_execution_and_review_f22b', setter='setTitle')
        host = QFormLayout(self.host_group); host.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.host_warning = localize(QLabel(''), 'ui.widget.commands_run_with_the_selected_user_af25')
        self.host_warning.setWordWrap(True); host.addRow(self.host_warning)
        self.shell = QLineEdit(general); set_text(self.shell, 'ui.widget.automatic_bash_for_linux_powershell_for_ea64', setter='setPlaceholderText')
        self.approval_mode = QComboBox(general)
        for title,value in [(tr('ui.caption.f8ab49295571'),'smart'),(tr('ui.caption.e8956a789368'),'manual'),(tr('ui.caption.6455766f8d35'),'deny'),(tr('ui.caption.9488039800fe'),'off')]:
            add_caption_item(self.approval_mode, title, value)
        self.approval_model = QComboBox(general); add_item(self.approval_model, 'ui.widget.current_run_s_model_0852', '')
        try:
            from core.model_connections import load_settings
            for record in load_settings().get('connections',[]):
                self.approval_model.addItem(record.get('name') or record['id'],record['id'])
        except Exception:
            pass
        self.deny_rules = form_field(QPlainTextEdit(general)); self.deny_rules.setMaximumHeight(85)
        set_text(self.deny_rules, 'ui.widget.denied_patterns_one_per_line_git_3f56', setter='setPlaceholderText')
        self.allow_commands = form_field(QPlainTextEdit(general)); self.allow_commands.setMaximumHeight(85)
        set_text(self.allow_commands, 'ui.widget.permanently_allowed_commands_exact_text_one_0af5', setter='setPlaceholderText')
        for title,field in [(tr('ui.caption.baba778a13c6'),self.shell),(tr('ui.caption.e56a412d1285'),self.approval_mode),(tr('ui.caption.9bff2a61bfa9'),self.approval_model),
            (tr('ui.caption.0509852546c4'),self.deny_rules),(tr('ui.caption.a90886c97dab'),self.allow_commands)]:
            host.addRow(title,field)
        settings.addWidget(self.host_group)
        add_caption_tab(tabs, general, tr('ui.caption.11355a292cbb'))
        bindings = QWidget(tabs); box=QVBoxLayout(bindings)
        info=localize(QLabel(''), 'ui.widget.selecting_a_folder_assigns_this_terminal_33ac')
        info.setWordWrap(True); info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.bindings_info = info
        box.addWidget(info)
        self.bindings=QTreeWidget(bindings); self.bindings.setHeaderHidden(True)
        self.bindings.itemChanged.connect(self.binding_changed)
        box.addWidget(self.bindings); add_caption_tab(tabs, bindings, tr('ui.caption.70e1cbc1e4e8'))
        self.save_button=localize(QPushButton(''), 'ui.widget.save_settings_d325'); self.save_button.clicked.connect(self.save); layout.addWidget(self.save_button)
        self.stop_button,self.reset_button,self.delete_button=[set_icon(QToolButton(page), name, square=True) for name in ('stop', 'refresh', 'trash')]
        for button, key in zip((self.stop_button,self.reset_button,self.delete_button), ('ui.widget.stop_3396','ui.widget.recreate_669f','ui.widget.delete_be99')):
            button.setObjectName('TerminalToolbarButton')
            button.setFixedSize(28, 28)
            set_text(button, key, setter='setToolTip')
        self.control_row=QHBoxLayout()
        self.control_row.setSpacing(4)
        for button,action in [(self.stop_button,'stop'),(self.reset_button,'reset'),(self.delete_button,'delete')]:
            button.clicked.connect(lambda checked=False,action=action:self.control(action)); self.control_row.addWidget(button)
        self.control_row.addStretch()
        layout.insertLayout(0, self.control_row)
        layout.addStretch()
        self._fit_settings_width(page)
        scroll.setWidget(page); self.stack.addWidget(scroll)

    def _fit_settings_width(self, page):
        # Form rows may wrap vertically, but no child may demand a wider dock.
        for form in page.findChildren(QFormLayout):
            form.setRowWrapPolicy(QFormLayout.WrapAllRows)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.LabelRole)
                if item is not None and isinstance(item.widget(), QLabel):
                    item.widget().setWordWrap(True)
        for widget in [page, *page.findChildren(QWidget)]:
            if isinstance(widget, (QLineEdit, QComboBox, QPlainTextEdit, QGroupBox, QTabWidget, QStackedWidget, QSpinBox, QDoubleSpinBox)) or widget is page:
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Ignored, widget.sizePolicy().verticalPolicy())
            if isinstance(widget, QComboBox):
                widget.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                widget.setMinimumContentsLength(0)
            if isinstance(widget, QTabWidget):
                widget.tabBar().setUsesScrollButtons(True)
                widget.tabBar().setMinimumWidth(0)
                widget.tabBar().setExpanding(False)
        for checkbox in (self.active, self.save_history, self.preserve, self.network, self.run_as_root, self.restrict_writes):
            parent = checkbox.parentWidget()
            form = next((form for form in page.findChildren(QFormLayout) if form.indexOf(checkbox) >= 0), None)
            if form is None:
                continue
            row, role = form.getWidgetPosition(checkbox)
            form.takeRow(row)
            wrapper = QWidget(parent)
            box = QHBoxLayout(wrapper)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            key = i18n.source_key(checkbox.text())
            caption = tr(key) if key else checkbox.text()
            label = bind_caption(_SettingCheckboxLabel(checkbox, wrapper), caption)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setToolTip(checkbox.toolTip())
            bind_caption(checkbox, caption, setter='setAccessibleName')
            checkbox.setText('')
            box.addWidget(checkbox, 0, Qt.AlignTop)
            box.addWidget(label, 1)
            form.insertRow(row, wrapper)
        page.layout().setSizeConstraint(QLayout.SetNoConstraint)

    def set_mode(self, index):
        index = 1 if index==1 else 0
        self.ui_preferences.setValue("mode",index)
        self.stack.setCurrentIndex(index)
        self.console_button.setChecked(index == 0)
        self.settings_button.setChecked(index == 1)

    def context_changed(self, *args):
        if not isValid(self):
            return
        self.generation += 1
        if len(args)==2 and args[0]=='terminal_sandbox' and isinstance(args[1],bool):
            self._module_enabled=args[1]
        else:self._module_enabled=self.configured_module_enabled()
        if not core_process.is_running() or not self._module_enabled or not app_state.session_id:
            self._available=False
        self.terminal_tabs.generation+=1;self.terminal_tabs.feed.cancel()
        self.terminal_tabs.update_availability()
        self.revision = None
        self.feed_client.cancel(); self.reading = False
        self.completion_client.cancel()
        self._sent_credentials.clear()
        if not self.polling:
            self.poll()

    def scope(self):
        return {'session_id': app_state.session_id or '', 'project_id': app_state.project_id or '', 'workspace': app_state.workspace}

    @staticmethod
    def configured_module_enabled():
        import yaml
        from core.paths import APP_PATHS
        try:
            return 'terminal_sandbox' in (yaml.safe_load(APP_PATHS.config.read_text(encoding='utf-8')) or {}).get('system_modules',[])
        except (OSError,ValueError,yaml.YAMLError):return False

    def availability_message(self):
        missing=[]
        if not core_process.is_running():missing.append(tr('ui.caption.c0eae63520e3'))
        if not app_state.session_id:missing.append(tr('ui.caption.b4f3e8fe1b39'))
        if not self._module_enabled:missing.append(tr('ui.caption.ebe17cc698f3'))
        if missing:return tr('ui.caption.ab46b7cfe28c')+'\n'.join(missing)
        if not self._available:return tr('ui.caption.fe538ee5e386')
        return ''

    def terminal_ready(self):
        return not self.availability_message()

    def offline_catalog(self):
        from core.session_manager import SessionManager
        sessions = SessionManager()
        path = Path(sessions.projects_catalog_path)
        catalog = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'folders': {}, 'session_folders': {}}
        folder = catalog.get('session_folders', {}).get(app_state.session_id) if app_state.session_id else app_state.project_id
        chain = sessions._folder_chain(folder, catalog)
        profiles = self.store.profiles()
        return {'profiles': profiles, 'resolved_id': self.store.resolve(app_state.session_id or '',
            chain, chain[0]['id'] if chain else app_state.project_id)['id'],
            'session_folders': catalog.get('session_folders', {}),
            'folders': [dict(value, id=key) for key, value in catalog.get('folders', {}).items()],
            'sessions': sorted(p.name for p in Path(sessions.sessions_dir).iterdir() if p.is_dir() and (p / 'session.sqlite').is_file())}

    def poll(self):
        if not isValid(self):
            return
        if self.polling or self.action_busy:
            return
        self.polling = True
        generation, scope = self.generation, self.scope()
        online = core_process.is_running() and self._module_enabled and bool(app_state.session_id)
        def get_catalog():
            if online:
                try:
                    catalog = self.client.request(scope=scope)
                    from ..ssh_support import password
                    for profile in catalog.get('profiles',[]):
                        if profile.get('backend') == 'ssh' and profile['id'] not in self._sent_credentials:
                            secret = password(self.store.owner + ':' + profile['id'])
                            if secret:
                                self.client.request('POST','credentials',{'profile_id':profile['id'],'password':secret})
                            self._sent_credentials.add(profile['id'])
                    return True, catalog, ''
                except Exception as error:
                    return False, self.offline_catalog(), str(error)
            return False, self.offline_catalog(), ''
        def done(result):
            if not isValid(self):
                return
            self.polling = False
            if generation != self.generation:
                return
            self._available, catalog, error = result
            self.apply_catalog(catalog)
            if not self._available:
                self.notice(tr('ui.caption.6672ab77dc93') + ('\n' + error if error else ''))
                self.timer.setInterval(6000)
            else:
                self.timer.setInterval(1000)
                self.read_output()
        self.runner.submit(get_catalog, done, lambda error: self.poll_failed(error))

    def poll_failed(self, error):
        if not isValid(self):
            return
        self.polling = False
        self._available=False
        self.terminal_tabs.update_availability()
        self.notice(str(error))
        self.timer.setInterval(6000)

    def apply_catalog(self, catalog):
        if (self.revision is not None and catalog.get('epoch') == self.feed_epoch and
                catalog.get('revision',self.revision) < self.revision):
            return
        self.catalog = catalog
        identity = catalog['resolved_id'] if self.auto_select else self.current_id
        profiles = catalog.get('profiles', [])
        if identity not in {p['id'] for p in profiles}:
            identity, self.auto_select = catalog['resolved_id'], True
        self.selector.blockSignals(True)
        self.selector.clear()
        resolved = next((p for p in profiles if p['id'] == catalog['resolved_id']), {})
        add_caption_item(self.selector, tr('ui.caption.ac24f97c95f8') + (profile_caption(resolved) or tr('ui.caption.33c8bc0b777a')), None)
        for profile in profiles:
            add_caption_item(self.selector, profile_caption(profile) + ('' if profile['active'] else tr('ui.caption.111a4e391195')), profile['id'])
        self.selector.setCurrentIndex(0 if self.auto_select else max(0, self.selector.findData(identity)))
        self.selector.blockSignals(False)
        changed = self.current_id != identity
        if changed:
            self.drafts[self.current_id] = self.console.draft()
        self.current_id = identity
        if changed:
            self.reset_feed()
        profile = self.profile()
        if not profile:
            return
        backend = profile.get('backend','docker')
        self.top_panel.setStyleSheet('QWidget#TerminalTop {background:#685418;border:1px solid #b99127;border-radius:5px;}' if backend=='local' else '')
        self.selector.setStyleSheet('QComboBox {background:#685418;color:#fff0b3;padding-right:36px;border:1px solid #b99127;}' if backend=='local' else 'QComboBox {padding-right:36px;}')
        busy = profile.get('agent_busy', False)
        self.terminal_tabs.set_profile(profile)
        state = 'busy' if busy and profile.get('state') != 'error' else profile.get('state', 'stopped')
        self.selector.set_state(state, profile.get('state_error', ''))
        self.notice('')
        if self.form_id != identity and not self.editing_new:
            self.load_form(profile)

    def profile(self):
        return next((p for p in self.catalog.get('profiles', []) if p['id'] == self.current_id), None)

    def reset_feed(self):
        self.terminal_tabs.generation+=1
        self.terminal_tabs.feed.cancel();self.terminal_tabs.revision=None


    def select_container(self):
        self.drafts[self.current_id] = self.console.draft()
        value = self.selector.currentData()
        self.auto_select = value is None
        self.current_id = value or self.catalog.get('resolved_id', 'shared')
        self.ui_preferences.setValue('auto',self.auto_select)
        self.ui_preferences.setValue('profile',self.current_id)
        self.form_id, self.editing_new = '', False
        self.reset_feed()
        if self.catalog:
            self.apply_catalog(self.catalog)
            self.read_output()

    def read_output(self):
        self.terminal_tabs.poll()


    def complete(self,line,position):
        pane=self.terminal_tabs.pane()
        if pane:self.terminal_tabs.send(pane,line,'tab')


    def show_approvals(self,requests):
        known={entry['id'] for entry in requests}
        for identity,dialog in list(self.approval_dialogs.items()):
            if identity not in known:
                self.approval_dialogs.pop(identity,None); dialog.blockSignals(True); dialog.close(); dialog.deleteLater()
        for entry in requests:
            identity=entry['id']
            if identity in self.approval_dialogs: continue
            dialog=QDialog(self.window()); set_text(dialog, 'ui.widget.terminal_confirmation_be23', setter='setWindowTitle')
            box=QVBoxLayout(dialog)
            label=QLabel(entry['reason']); label.setWordWrap(True); box.addWidget(label)
            context=bind_caption(QLabel(('')), tr('ui.caption.b260e58151e5')+entry.get('session_id','')+tr('ui.caption.8fd6a420c4ad')+entry.get('cwd',''))
            context.setTextInteractionFlags(Qt.TextSelectableByMouse); box.addWidget(context)
            text=form_field(QPlainTextEdit(dialog)); text.setReadOnly(True)
            text.setPlainText(json.dumps(entry['details'],ensure_ascii=False,indent=2) if entry['kind']=='host_key' else entry['command'])
            box.addWidget(text)
            buttons=QHBoxLayout(); box.addLayout(buttons)
            answered=[False]
            def answer(decision,identity=identity,dialog=dialog,answered=answered):
                if answered[0]: return
                answered[0]=True
                self.runner.submit(lambda:self.client.request('POST','approval/'+identity,{'decision':decision}),
                    lambda result:None,lambda error:self.notice(str(error)))
                self.approval_dialogs.pop(identity,None); dialog.close(); dialog.deleteLater()
            choices=[(tr('ui.caption.83691ee29d85'),'once'),(tr('common.cancel'),'deny')] if entry['kind']=='host_key' else [
                (tr('ui.caption.86f9011592e0'),'once'),(tr('ui.caption.8a6f0f8dd1e1'),'session'),(tr('ui.caption.22f51c4c12bf'),'always'),(tr('ui.caption.500e21b81e00'),'deny')]
            for title,decision in choices:
                button=bind_caption(QPushButton('',dialog), title); button.clicked.connect(lambda checked=False,d=decision,fn=answer:fn(d)); buttons.addWidget(button)
            dialog.rejected.connect(lambda fn=answer:fn('deny'))
            dialog.resize(590,300); self.approval_dialogs[identity]=dialog; dialog.show(); dialog.raise_()

    def run_action(self, fn, success=None, message=tr('ui.settings.working_09e8')):
        if self.action_busy:
            return
        self.action_busy = True
        self.notice(message)
        self.console.set_input_enabled(False)
        self.save_button.setEnabled(False)
        def done(result):
            if not isValid(self):
                return
            self.action_busy = False
            self.save_button.setEnabled(True)
            if success:
                success(result)
            self.poll()
        def failed(error):
            if not isValid(self):
                return
            self.action_busy = False
            self.save_button.setEnabled(True)
            self.notice(str(error))
            self.timer.setInterval(6000)
            self.console.set_input_enabled(getattr(self, 'allow_input', False))
        self.runner.submit(fn, done, failed)

    def send(self, command=None):
        pane=self.terminal_tabs.pane()
        if pane:self.terminal_tabs.send(pane,pane.console.draft() if command is None else command)


    def cancel_job(self):
        pane=self.terminal_tabs.pane()
        if pane:self.terminal_tabs.send(pane,'','ctrl+c')


    def _refresh_default_name(self, *_):
        profile = getattr(self, '_default_name_profile', None)
        if profile is not None and isValid(self.name):
            self.name.setText(str(profile_caption(profile)))

    def load_form(self, profile):
        self.form_id=profile['id']; self.form_backend=profile.get('backend','docker')
        self.name.setText(str(profile_caption(profile)))
        self._default_name_profile = dict(profile) if (profile.get('id') in {'shared','local'}
            and profile.get('name') in {'Shared terminal','This computer'}) else None
        for field in ('active','preserve','network','run_as_root','restrict_writes','save_history'):
            getattr(self,field).setChecked(profile.get(field,field in {'save_history','run_as_root'}))
        self.image.setCurrentText(profile['image']); self.cpus.setValue(profile['cpus'])
        self.memory.setValue(profile['memory_mb']); self.pids.setValue(profile['pids']); self.timeout.setValue(profile['timeout'])
        self.environment.setPlainText('\n'.join(f'{k}={v}' for k,v in profile['environment'].items()))
        for field in ('ssh_host','ssh_user','ssh_key','ssh_root','shell'):
            bind_caption(getattr(self,field), profile.get(field,''))
        self.ssh_password.clear(); self.ssh_port.setValue(profile.get('ssh_port',22))
        self.target_os.setCurrentIndex(max(0,self.target_os.findData(profile.get('target_os','linux'))))
        self.approval_mode.setCurrentIndex(max(0,self.approval_mode.findData(profile.get('approval_mode','smart'))))
        self.approval_model.setCurrentIndex(max(0,self.approval_model.findData(profile.get('approval_model',''))))
        self.deny_rules.setPlainText('\n'.join(profile.get('deny_rules',[])))
        self.allow_commands.setPlainText('\n'.join(profile.get('allow_commands',[])))
        self.docker_group.setVisible(self.form_backend=='docker')
        self.ssh_group.setVisible(self.form_backend=='ssh'); self.host_group.setVisible(self.form_backend!='docker')
        self.reset_button.setVisible(self.form_backend=='docker')
        self.stop_button.setVisible(self.form_backend!='local')
        self.delete_button.setEnabled(profile['id'] not in {'shared','local'})
        self.build_bindings(profile)

    def build_bindings(self, profile):
        self.bindings.blockSignals(True); self.bindings.clear()
        selected={(b['kind'],b['target']) for b in profile['bindings']}
        folders={f['id']:f for f in self.catalog.get('folders',[])}
        items={}
        def make(identity, seen=None):
            if identity in items: return items[identity]
            seen=set(seen or ())
            if identity in seen: return None
            seen.add(identity); f=folders[identity]; parent=make(f['parent_id'],seen) if f.get('parent_id') in folders else None
            item=QTreeWidgetItem([f['name']]); item.setData(0,Qt.UserRole,{'kind':'folder' if f.get('parent_id') else 'project','target':identity})
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(0,Qt.Unchecked)
            if parent: parent.addChild(item)
            else: self.bindings.addTopLevelItem(item)
            items[identity]=item; return item
        for identity in folders: make(identity)
        locations=self.catalog.get('session_folders',{})
        for session in self.catalog.get('sessions',[]):
            item=QTreeWidgetItem([session]); item.setData(0,Qt.UserRole,{'kind':'session','target':session})
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(0,Qt.Unchecked)
            parent=items.get(locations.get(session))
            if parent: parent.addChild(item)
            else: self.bindings.addTopLevelItem(item)
        def mark(item, inherited=False):
            data=item.data(0,Qt.UserRole); explicit=(data['kind'],data['target']) in selected
            checked=inherited or explicit
            if not explicit and any(data in p.get('bindings',[]) for p in self.catalog.get('profiles',[]) if p['id']!=profile['id']):
                checked=False
            item.setCheckState(0,Qt.Checked if checked else Qt.Unchecked)
            for i in range(item.childCount()): mark(item.child(i),checked)
            self.update_parent_check(item)
        for i in range(self.bindings.topLevelItemCount()): mark(self.bindings.topLevelItem(i))
        shared = profile['id'] == 'shared'
        bind_caption(self.bindings_info, tr('ui.caption.7dc81f3555b3')
            if shared else
            tr('ui.widget.selecting_a_folder_assigns_this_terminal_33ac'))
        self.bindings.setVisible(not shared)
        self.bindings.setEnabled(not shared)
        self.bindings.expandAll()
        self.bindings.blockSignals(False)

    def update_parent_check(self,item):
        if item.childCount():
            states=[item.child(i).checkState(0) for i in range(item.childCount())]
            item.setCheckState(0,Qt.Checked if all(s==Qt.Checked for s in states) else Qt.Unchecked)

    def binding_changed(self,item,column):
        self.bindings.blockSignals(True)
        def cascade(node,state):
            node.setCheckState(0,state)
            for i in range(node.childCount()): cascade(node.child(i),state)
        cascade(item,item.checkState(0))
        parent=item.parent()
        while parent:
            self.update_parent_check(parent); parent=parent.parent()
        self.bindings.blockSignals(False)

    def new_menu(self):
        menu=QMenu(self)
        localize(add_caption_action(menu, 'addAction', 'Docker-контейнер', lambda:self.new_profile('docker')), 'ui.widget.docker_container_aa72')
        localize(add_caption_action(menu, 'addAction', 'SSH-подключение', lambda:self.new_profile('ssh')), 'ui.widget.ssh_connection_2dab')
        menu.popup(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))
        self._new_menu=menu

    def new_profile(self,backend='docker'):
        self.editing_new=True; profile=self.store.default(uuid.uuid4().hex); profile['backend']=backend
        profile['name']=tr('ui.widget.ssh_connection_2dab') if backend=='ssh' else tr('ui.caption.9969ec93be68')
        if app_state.session_id: profile['bindings']=[{'kind':'session','target':app_state.session_id}]
        elif app_state.project_id: profile['bindings']=[{'kind':'project','target':app_state.project_id}]
        self.load_form(profile); self.set_mode(1); self.notice(tr('ui.caption.6e4bf679f554'))

    def form_value(self):
        environment={}
        for line in self.environment.toPlainText().splitlines():
            if not line.strip(): continue
            key,separator,value=line.partition('=')
            if not separator: raise ValueError('Переменные окружения: NAME=value, по одной на строку.')
            environment[key.strip()]=value
        bindings=[]
        def collect(item):
            if item.checkState(0)==Qt.Checked:
                bindings.append(item.data(0,Qt.UserRole)); return
            for i in range(item.childCount()): collect(item.child(i))
        for i in range(self.bindings.topLevelItemCount()): collect(self.bindings.topLevelItem(i))
        old=next((p for p in self.catalog.get('profiles',[]) if p['id']==self.form_id),{})
        value={**self.store.default(self.form_id),**old,'id':self.form_id,'backend':self.form_backend,
            'name':self.name.text(),'active':self.active.isChecked(),'save_history':self.save_history.isChecked(),
            'preserve':self.preserve.isChecked(),'network':self.network.isChecked(),'image':self.image.currentText(),
            'cpus':self.cpus.value(),'memory_mb':self.memory.value(),'pids':self.pids.value(),'timeout':self.timeout.value(),
            'run_as_root':self.run_as_root.isChecked(),
            'restrict_writes':self.restrict_writes.isChecked(),'environment':environment,'bindings':bindings,
            'ssh_port':self.ssh_port.value(),'target_os':self.target_os.currentData(),
            'approval_mode':self.approval_mode.currentData(),'approval_model':self.approval_model.currentData(),
            'deny_rules':self.deny_rules.toPlainText().splitlines(),'allow_commands':self.allow_commands.toPlainText().splitlines()}
        if old and self.name.text() == str(profile_caption(old)):
            value['name'] = old['name']
        for field in ('ssh_host','ssh_user','ssh_key','ssh_root','shell'): value[field]=getattr(self,field).text()
        return value

    def save(self):
        try:
            value = self.store.normalize(self.form_value())
        except (ValueError, AttributeError) as error:
            self.notice(str(error))
            return
        def saved(profile):
            self.editing_new = False
            self.current_id, self.auto_select, self.form_id = profile['id'], False, ''
            self.ssh_password.clear()
            self._sent_credentials.discard(profile['id'])
        online = self._available
        secret = self.ssh_password.text()
        if secret:
            value['ssh_password'] = secret
        def store_offline():
            profile = self.store.save(value,self.catalog)
            if secret:
                from ..ssh_support import password
                password(self.store.owner + ':' + profile['id'],secret)
            return profile
        def save_action():
            if online:
                result = self.client.request('POST','profiles',value)
                if secret:
                    from ..ssh_support import password
                    password(self.store.owner + ':' + result['id'],secret)
                return result
            return store_offline()
        self.run_action(save_action, saved,
                        tr('ui.caption.f0bac8710a31'))

    def control(self, action):
        identity = self.form_id
        if identity != self.current_id:
            self.notice(tr('ui.caption.5671a37def87'))
            return
        if action in {'reset', 'delete'}:
            answer = QMessageBox.question(self, tr('ui.widget.delete_environment_e023'),
                tr('ui.widget.internal_container_files_and_installed_packages_8647'),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        if not self._available:
            self.notice(tr('ui.caption.2f0a418565ee'))
            return
        self.run_action(lambda: self.client.request('POST', 'control/' + identity, {'action': action, 'confirm': True}),
                        message=tr('ui.caption.aca3ad439a0a'))

    def refresh_images(self):
        if not self._available:
            self.notice(tr('ui.caption.b10cf3ea2c58'))
            return
        self.run_action(lambda: self.client.request(path='images'), self.images_loaded, tr('ui.caption.0b9db6d414e2'))

    def images_loaded(self, result):
        value = self.image.currentText()
        self.image.clear()
        add_caption_items(self.image, result.get('images', []))
        self.image.setCurrentText(value)

    def import_image(self):
        if not self._available:
            self.notice(tr('ui.caption.12f8a287ebe1'))
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Архив Docker-образа (docker save)', '', 'Docker archives (*.tar *.tar.gz *.tgz *.tar.xz *.tar.zst);;All files (*)')
        if path:
            self.run_action(lambda: self.client.request('POST', 'images/import', {'path': path}), self.images_loaded,
                            tr('ui.caption.c32d51054e2a'))

    def session_renamed(self, old, new):
        online = self._available
        self.runner.submit(lambda: self.client.request('POST', 'rename-session', {'old': old, 'new': new}) if online
                           else self.store.rename_session(old, new), lambda _: self.context_changed(),
                           lambda error: self.notice(str(error)))
