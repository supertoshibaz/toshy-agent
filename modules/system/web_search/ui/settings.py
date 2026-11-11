"""Startup settings for the Web Search module, without importing the module."""
from ui.i18n import bind_caption, tr, localize, set_text


import asyncio
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QFileDialog,
    QSpinBox,
    QDoubleSpinBox,
    QPushButton,
    QWidget,
)

from ..settings import (
    load_settings,
    normalize_settings,
    save_settings,
    connection_identity,
    CRAWLER_LIMITS,
)
from ..network import probe_proxy
from core.paths import APP_PATHS
from ui.core_bridge.async_runner import AsyncRunner
from ui.views.startup_view import SettingsPage, SettingRow


class WebSearchSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(
            "Web Search",
            tr('ui.caption.4e87f900346b'),
            ("Core", "System Modules", "Web Search"),
            parent,
        )
        self.runner = AsyncRunner(self)
        self._busy = False
        self._forget_password = False
        self._saved = normalize_settings({})
        self._applying = False
        self._https_values = ("", 0)
        self.enabled = localize(QCheckBox('', self), 'ui.settings.use_proxy_6d44')
        self.add_row(
            SettingRow(
                tr('ui.settings.manual_configuration_f9b6'),
                tr('ui.settings.the_proxy_is_selected_by_the_0286'),
                self.enabled,
                parent=self,
            )
        )
        self.proxies = {}
        self.same_proxy = localize(QCheckBox('', self), 'ui.settings.use_the_http_proxy_for_https_096e')
        for kind, title, description in (
            ("http", tr('ui.caption.90c737452b79'), tr('ui.caption.2390ed22212c')),
            (
                "https",
                tr('ui.caption.a61b93faa165'),
                tr('ui.caption.56ef280063f7'),
            ),
            (
                "socks5",
                "SOCKS5",
                tr('ui.caption.41a3d820569c'),
            ),
        ):
            editor = QWidget(self)
            line = QHBoxLayout(editor)
            line.setContentsMargins(0, 0, 0, 0)
            host = QLineEdit(editor)
            host.setMinimumWidth(180)
            set_text(host, 'ui.settings.proxy_address_ef48', setter='setPlaceholderText')
            set_text(host, 'ui.settings.server_hostname_or_ip_address_the_42ac', setter='setToolTip')
            port = QSpinBox(editor)
            port.setRange(0, 65535)
            bind_caption(port, "—", setter='setSpecialValueText')
            port.setFixedWidth(86)
            set_text(port, 'ui.settings.proxy_port_leave_empty_for_the_693f', setter='setToolTip')
            line.addWidget(host)
            line.addWidget(localize(QLabel('', editor), 'ui.settings.port_9094'))
            line.addWidget(port)
            self.proxies[kind] = (host, port)
            self.add_row(
                SettingRow(
                    title,
                    description,
                    editor,
                    search_terms=("proxy http https socks прокси адрес порт"),
                    parent=self,
                )
            )
            if kind == "http":
                self.add_row(
                    SettingRow(
                        tr('ui.settings.shared_proxy_b4df'),
                        tr('ui.settings.use_the_http_proxy_for_https_fed3'),
                        self.same_proxy,
                        parent=self,
                    )
                )
        self.remote_dns = localize(QCheckBox('', self), 'ui.settings.resolve_dns_through_the_socks5_proxy_6c70')
        self.add_row(
            SettingRow(
                tr('ui.settings.dns_for_socks5_c2e5'),
                tr('ui.settings.send_the_hostname_to_the_proxy_294e'),
                self.remote_dns,
                parent=self,
            )
        )
        self.username = QLineEdit(self)
        set_text(self.username, 'ui.settings.optional_f6d9', setter='setPlaceholderText')
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.Password)
        password_host = QWidget(self)
        password_layout = QHBoxLayout(password_host)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.addWidget(self.password)
        self.clear_password = localize(QPushButton('', password_host), 'ui.settings.reset_407f')
        self.clear_password.clicked.connect(self._clear_password)
        password_layout.addWidget(self.clear_password)
        self.add_row(
            SettingRow(
                tr('ui.settings.username_5be1'),
                tr('ui.settings.shared_username_for_the_configured_proxies_d7aa'),
                self.username,
                parent=self,
            )
        )
        self.add_row(
            SettingRow(
                tr('ui.settings.password_cb1a'),
                tr('ui.settings.shared_proxy_password_stored_in_the_58b4'),
                password_host,
                parent=self,
            )
        )
        self.crawler_controls = {}
        fields = (
            (
                "concurrency",
                tr('ui.caption.efe0658e30b0'),
                tr('ui.caption.0e64094a5787'),
            ),
            (
                "delay",
                tr('ui.caption.b9883bb569e3'),
                tr('ui.caption.244d8d6185b3'),
            ),
            (
                "timeout",
                tr('ui.caption.3783b81c31b7'),
                tr('ui.caption.7bd5ff53866c'),
            ),
            (
                "retries",
                tr('ui.caption.d6dfed5b7a46'),
                tr('ui.caption.93e70ccce434'),
            ),
            (
                "retry_backoff",
                tr('ui.caption.93340fcd0904'),
                tr('ui.caption.c797ad503532'),
            ),
            (
                "max_page_mb",
                tr('ui.caption.7ee6a17f9af2'),
                tr('ui.caption.5ca4869dfe90'),
            ),
            (
                "max_pages",
                tr('ui.caption.de8a41193d6d'),
                tr('ui.caption.cca43145fb6c'),
            ),
            (
                "search_results",
                tr('ui.caption.d095d83a9b0b'),
                tr('ui.caption.e1aee9e8e7ce'),
            ),
        )
        for key, title, description in fields:
            default, minimum, maximum = CRAWLER_LIMITS[key]
            control = (
                QSpinBox(self) if isinstance(default, int) else QDoubleSpinBox(self)
            )
            control.setRange(minimum, maximum)
            control.setKeyboardTracking(False)
            if isinstance(control, QDoubleSpinBox):
                control.setDecimals(2)
                control.setSingleStep(0.1)
            self.crawler_controls[key] = control
            self.add_row(
                SettingRow(
                    title,
                    description,
                    control,
                    search_terms=("crawler crawl network краулер обход загрузка"),
                    parent=self,
                )
            )
        self.browser_enabled = localize(QCheckBox('', self), 'ui.settings.use_a_local_browser_3f61')
        self.add_row(
            SettingRow(
                tr('ui.settings.complex_pages_0212'),
                tr('ui.settings.disabled_by_default_after_the_http_4696'),
                self.browser_enabled,
                parent=self,
            )
        )
        browser_host = QWidget(self)
        browser_row = QHBoxLayout(browser_host)
        browser_row.setContentsMargins(0, 0, 0, 0)
        self.browser_path = QLineEdit(browser_host)
        self.browser_path.setMinimumWidth(240)
        set_text(self.browser_path, 'ui.settings.elements_browsers_installed_browser_40b6', setter='setPlaceholderText')
        self.browser_browse = localize(QPushButton('', browser_host), 'ui.settings.browse_9045')
        self.browser_browse.clicked.connect(self._choose_browser)
        browser_row.addWidget(self.browser_path)
        browser_row.addWidget(self.browser_browse)
        self.add_row(
            SettingRow(
                tr('ui.settings.browser_executable_c3d2'),
                tr('ui.settings.installed_chromium_chrome_edge_or_chrome_415f'),
                browser_host,
                parent=self,
            )
        )
        self.browser_timeout = QSpinBox(self)
        self.browser_timeout.setRange(5, 120)
        self.add_row(
            SettingRow(
                tr('ui.settings.browser_reading_timeout_s_ebfc'),
                tr('ui.settings.time_to_wait_for_page_content_5b92'),
                self.browser_timeout,
                parent=self,
            )
        )
        self.browser_enabled.toggled.connect(self._update_enabled)
        actions = QWidget(self)
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        self.save_button = localize(QPushButton('', actions), 'ui.settings.save_b4d3')
        self.test_button = localize(QPushButton('', actions), 'ui.settings.test_connection_d6d8')
        self.save_button.clicked.connect(self.save)
        self.test_button.clicked.connect(self.test)
        row.addWidget(self.save_button)
        row.addWidget(self.test_button)
        self.add_row(
            SettingRow(
                tr('ui.settings.connection_ab71'),
                tr('ui.settings.the_test_uses_the_current_fields_8310'),
                actions,
                parent=self,
            )
        )
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.rows_layout.addWidget(self.status)
        try:
            self._apply(load_settings())
        except (ValueError, OSError) as error:
            bind_caption(self.status, str(error))
        self.enabled.toggled.connect(self._update_enabled)
        self.same_proxy.toggled.connect(self._share_changed)
        self.remote_dns.toggled.connect(self._password_placeholder)
        for kind, (host, port) in self.proxies.items():
            host.textChanged.connect(self._password_placeholder)
            port.valueChanged.connect(self._password_placeholder)
            if kind == "http":
                host.textChanged.connect(self._mirror_http)
                port.valueChanged.connect(self._mirror_http)
        self.username.textChanged.connect(self._password_placeholder)
        self._update_enabled()

    def _apply(self, settings):
        self._applying = True
        try:
            self._saved = settings
            self.enabled.setChecked(settings["enabled"])
            for kind, controls in self.proxies.items():
                parsed = urlsplit(settings[kind])
                host = parsed.hostname or ""
                if ":" in host:
                    host = "[" + host + "]"
                if parsed.scheme == "https":
                    host = "https://" + host
                bind_caption(controls[0], host)
                controls[1].setValue(parsed.port or 0)
            self._https_values = self._endpoint_values("https")
            self.same_proxy.setChecked(settings["use_http_for_https"])
            self.remote_dns.setChecked(settings["socks_remote_dns"])
            bind_caption(self.username, settings["username"])
            self.password.clear()
            self._forget_password = False
            self.browser_enabled.setChecked(settings["browser"]["enabled"])
            bind_caption(self.browser_path, settings["browser"]["path"])
            self.browser_timeout.setValue(round(settings["browser"]["timeout"]))
            for key, control in self.crawler_controls.items():
                control.setValue(settings["crawler"][key])
        finally:
            self._applying = False
        self._mirror_http()
        self._password_placeholder()

    def _choose_browser(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выбрать браузер",
            self.browser_path.text() or str(APP_PATHS.browsers),
            "Все файлы (*)",
        )
        if path:
            bind_caption(self.browser_path, path)

    def _endpoint_values(self, kind):
        host, port = self.proxies[kind]
        return host.text().strip(), port.value()

    def _mirror_http(self, *args):
        if self._applying or not self.same_proxy.isChecked():
            return
        host, port = self._endpoint_values("http")
        bind_caption(self.proxies["https"][0], host)
        self.proxies["https"][1].setValue(port)

    def _share_changed(self, checked):
        if self._applying:
            return
        if checked:
            self._https_values = self._endpoint_values("https")
            self._mirror_http()
        else:
            bind_caption(self.proxies["https"][0], self._https_values[0])
            self.proxies["https"][1].setValue(self._https_values[1])
        self._password_placeholder()
        self._update_enabled()

    def _password_placeholder(self, *args):
        if self._applying:
            return
        try:
            current, _ = self._values()
            same = connection_identity(current) == connection_identity(self._saved)
        except ValueError:
            same = False
        saved = self._saved["password_saved"] and same and not self._forget_password
        bind_caption(self.password, tr('ui.caption.0087e18c0d15') if saved else tr('ui.settings.optional_f6d9'), setter='setPlaceholderText')

    def _clear_password(self):
        self._forget_password = True
        self.password.clear()
        self._password_placeholder()
        set_text(self.status, 'ui.settings.the_password_will_be_deleted_when_6438')

    def _update_enabled(self, *args):
        self.enabled.setEnabled(not self._busy)
        active = self.enabled.isChecked() and not self._busy
        for kind, controls in self.proxies.items():
            for control in controls:
                control.setEnabled(
                    active and not (kind == "https" and self.same_proxy.isChecked())
                )
        for control in (
            self.same_proxy,
            self.remote_dns,
            self.username,
            self.password,
            self.clear_password,
            self.test_button,
        ):
            control.setEnabled(active)
        self.save_button.setEnabled(not self._busy)
        for control in self.crawler_controls.values():
            control.setEnabled(not self._busy)
        self.browser_enabled.setEnabled(not self._busy)
        for control in (self.browser_path, self.browser_browse, self.browser_timeout):
            control.setEnabled(not self._busy and self.browser_enabled.isChecked())

    def _values(self):
        value = {
            "enabled": self.enabled.isChecked(),
            "use_http_for_https": self.same_proxy.isChecked(),
            "socks_remote_dns": self.remote_dns.isChecked(),
            "username": self.username.text(),
            "browser": {
                "enabled": self.browser_enabled.isChecked(),
                "path": self.browser_path.text(),
                "timeout": self.browser_timeout.value(),
            },
            "crawler": {
                key: control.value() for key, control in self.crawler_controls.items()
            },
        }
        for kind in self.proxies:
            host, port = (
                self._https_values
                if kind == "https" and self.same_proxy.isChecked()
                else self._endpoint_values(kind)
            )
            if not host:
                value[kind] = ""
                continue
            scheme = (
                ("socks5h" if self.remote_dns.isChecked() else "socks5")
                if kind == "socks5"
                else "http"
            )
            try:
                parsed = urlsplit(host if "://" in host else scheme + "://" + host)
                if parsed.port is not None:
                    raise ValueError("Укажите порт прокси в отдельном поле «Порт».")
            except ValueError:
                raise ValueError(
                    "Укажите адрес прокси без порта; порт задаётся отдельно."
                ) from None
            address = parsed.netloc + (f":{port}" if port else "")
            value[kind] = urlunsplit(parsed._replace(netloc=address))
        settings = normalize_settings(value)
        password = self.password.text()
        if not password and not self._forget_password:
            password = None
        return settings, password

    def _run(self, operation, callback):
        if self._busy:
            return
        try:
            settings, password = self._values()
        except ValueError as error:
            bind_caption(self.status, str(error))
            return
        self._busy = True
        self._update_enabled()
        set_text(self.status, 'ui.settings.working_09e8')

        def done(result):
            self._busy = False
            callback(result)
            self._update_enabled()

        def failed(error):
            self._busy = False
            self._update_enabled()
            bind_caption(self.status, str(error))

        self.runner.submit(lambda: operation(settings, password), done, failed)

    def save(self):
        def saved(settings):
            self._apply(settings)
            set_text(self.status, 'ui.settings.saved_new_web_search_calls_will_2474')

        self._run(
            lambda settings, password: save_settings(settings, password),
            saved,
        )

    def test(self):
        self._run(
            lambda settings, password: asyncio.run(probe_proxy(settings, password)),
            self.status.setText,
        )


def create_settings_page(parent=None):
    return WebSearchSettingsPage(parent)
