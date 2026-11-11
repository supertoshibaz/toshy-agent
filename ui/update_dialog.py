"""Application and plugin updates, with all network/installation IO off the GUI."""
from pathlib import Path
import os
import subprocess
from threading import Event
from PySide6.QtCore import QObject, Signal, Qt, QUrl
from ui.settings import QSettings
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit, QTabWidget, QWidget, QCheckBox, QScrollArea, QFrame, QProgressBar)
from ui.widgets.dialogs import Dialog, MessageBox
from ui.i18n import tr, set_text
from ui.styles.binding import apply_style
from ui.core_bridge.async_runner import AsyncRunner
from core.version import __version__
from core.paths import APP_PATHS
from release_tools import client, layout
from release_tools.network import ReleaseError


class Relay(QObject):
    progress = Signal(str)
    download = Signal(str, int)


class ProgressRelay:
    def __init__(self, relay):
        self.relay = relay

    def __call__(self, value):
        self.relay.download.emit('', -1)
        self.relay.progress.emit(str(value)[-3000:])

    def download(self, name, count, total=None, *, finished=False):
        self.relay.download.emit(name, min(100, int(count * 100 / total)) if total else -1)

    def raw(self, value):
        self.relay.progress.emit(str(value).strip()[-3000:])


class UpdatesDialog(Dialog):
    def __init__(self, host, *, tab=0):
        super().__init__(host)
        set_text(self, 'updates.title', setter='setWindowTitle')
        self.setMinimumWidth(520)
        self.cancel = Event()
        from ui.shutdown import on_shutdown
        on_shutdown(self.cancel.set)
        cancel_event = self.cancel
        self.destroyed.connect(lambda *_: cancel_event.set())
        self.runner = AsyncRunner(self, max_threads=1)
        self.relay = Relay(self)
        self.relay.progress.connect(self._message)
        self.relay.download.connect(self._download_progress)
        self.busy = False
        self.release = None
        self.root = client.installation_root()
        self.tabs = QTabWidget(self)
        outer = QVBoxLayout(self)
        outer.addWidget(self.tabs)
        app_page = QWidget(self)
        app = QVBoxLayout(app_page)
        self.tabs.addTab(app_page, tr('updates.application'))
        app.addWidget(QLabel(tr('updates.installed', version=__version__), self))
        self.repository = QLineEdit(client.settings().get('repository', ''), self)
        set_text(self.repository, 'updates.repository', setter='setPlaceholderText')
        app.addWidget(self.repository)
        buttons = QHBoxLayout()
        self.check_button = QPushButton(tr('updates.check'), self)
        self.check_button.clicked.connect(self.check_application)
        self.github_button = QPushButton('GitHub', self)
        self.github_button.clicked.connect(self.open_repository)
        self.update_button = QPushButton(tr('updates.update'), self)
        self.update_button.setEnabled(bool(self.root))
        self.update_button.clicked.connect(lambda: self.update_application())
        for button in (self.check_button, self.github_button, self.update_button):
            buttons.addWidget(button)
        app.addLayout(buttons)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.hide()
        app.addWidget(self.progress_bar)
        self.notes = QPlainTextEdit(self)
        self.notes.setReadOnly(True)
        self.notes.setFixedHeight(210)
        self.notes.setPlaceholderText(tr('updates.notes'))
        app.addWidget(self.notes)
        self.auto = QCheckBox(tr('updates.automatic'), self)
        self.auto.setChecked(bool(client.settings().get('automatic', True)))
        self.auto.toggled.connect(lambda value: client.save_settings(automatic=value))
        app.addWidget(self.auto)
        if not self.root:
            notice = QLabel(tr('updates.source_checkout'), self)
            notice.setWordWrap(True)
            app.addWidget(notice)
        plugin_page = QWidget(self)
        plugins = QVBoxLayout(plugin_page)
        self.tabs.addTab(plugin_page, tr('updates.plugins'))
        from ui.plugin_library import DEFAULT_PLUGIN_REPOSITORY
        self.catalog = QLineEdit(str(QSettings('Toshy', 'Toshy').value('plugins/repository_url', DEFAULT_PLUGIN_REPOSITORY)), self)
        set_text(self.catalog, 'updates.catalog', setter='setPlaceholderText')
        plugins.addWidget(self.catalog)
        self.plugin_check = QPushButton(tr('updates.check'), self)
        self.plugin_check.clicked.connect(self.check_plugins)
        plugins.addWidget(self.plugin_check)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(260)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        self.rows = QVBoxLayout(body)
        self.rows.setAlignment(Qt.AlignTop)
        scroll.setWidget(body)
        plugins.addWidget(scroll)
        self.message = QPlainTextEdit(self)
        self.message.setReadOnly(True)
        self.message.setFixedHeight(70)
        outer.addWidget(self.message)
        self.stop_button = QPushButton(tr('updates.cancel'), self)
        self.stop_button.clicked.connect(self.cancel.set)
        self.stop_button.hide()
        outer.addWidget(self.stop_button)
        apply_style(self, 'QPushButton {border:1px solid @border.subtle@;border-radius:6px;padding:6px;} QLineEdit,QPlainTextEdit {background:@background.input@;border:1px solid @border.subtle@;border-radius:6px;}')
        self.tabs.setCurrentIndex(tab)

    def _download_progress(self, name, percent):
        self.progress_bar.setRange(0, 100 if percent >= 0 else 0)
        self.progress_bar.setValue(max(0, percent))
        self._message(tr('updates.downloading_component', name=name))

    def _message(self, value):
        self.message.setPlainText(str(value))

    def job(self, function, success, failure=None):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.stop_button.show()
        self.check_button.setEnabled(False)
        self.plugin_check.setEnabled(False)
        self.update_button.setEnabled(False)
        def done(value=None, error=None):
            self.busy = False
            self.stop_button.hide()
            self.check_button.setEnabled(True)
            self.plugin_check.setEnabled(True)
            self.update_button.setEnabled(bool(self.root))
            self.progress_bar.hide()
            if error is not None:
                if failure is not None:
                    failure(error)
                else:
                    self._message(error)
            else:
                success(value)
        self.runner.submit(function, lambda value: done(value), lambda error: done(error=error))

    def check_application(self):
        try:
            from release_tools.source import repository
            value = repository(self.repository.text())
            client.save_settings(repository=value)
        except ReleaseError as e:
            self._message(e); return
        def done(release):
            self.release = release
            self.notes.setPlainText(release['version'] + ' — ' + release['published_at'] + '\n\n' + release['notes'])
            self._message(tr('updates.available' if release['newer'] else 'updates.current'))
            self.update_button.setEnabled(bool(self.root and release['newer']))
        self.job(lambda: client.check(cancel=self.cancel), done)

    def open_repository(self):
        try:
            from release_tools.source import repository
            url = 'https://github.com/' + repository(self.repository.text())
            QDesktopServices.openUrl(QUrl(url))
        except ReleaseError as e:
            self._message(e)

    def update_application(self, *, system_consent=False):
        if not self.root or self.busy:
            return
        try:
            from release_tools.source import repository
            repo = repository(self.repository.text())
            client.save_settings(repository=repo)
        except ReleaseError as error:
            self._message(error)
            return
        from release_tools.engine import Installer
        from release_tools.components import SystemDependenciesRequired
        root = self.root
        relay = self.relay
        cancel = self.cancel
        def update():
            state = layout.owned(root)
            pending = state.get('pending') or {}
            reusable = False
            if pending.get('repository') == repo and not pending.get('needs_environment') and pending.get('environment'):
                from release_tools.source import verify, version
                from release_tools.environments import environment
                from release_tools.engine import python_in
                if (version(pending['version']) > version(__version__)
                        and python_in(environment(root, pending, state)).is_file()):
                    verify(root / 'runtime/installer/pending', pending['version'])
                    reusable = True
            try:
                release = client.check(cancel=cancel)
            except (ReleaseError, OSError):
                if reusable and not cancel.is_set():
                    return dict(pending, newer=True)
                raise
            if reusable and release.get('commit') == pending.get('commit'):
                return dict(release, newer=True)
            if not release['newer']:
                return release
            engine = Installer(root, cancel=cancel, progress=ProgressRelay(relay), wait=True)
            engine.system_consent = system_consent
            state = layout.owned(root)
            engine.prepare(repository=repo, components=state.get('components', []),
                           skip_system=True, release_info=release)
            return release
        def done(release):
            self.release = release
            self.notes.setPlainText(release['version'] + '\n\n' + release['notes'])
            if release['newer']:
                self.apply_application()
            else:
                self._message(tr('updates.current'))
        def failed(error):
            if isinstance(error, SystemDependenciesRequired):
                if MessageBox.question(self, tr('updates.system_title'),
                        tr('updates.system_prompt', packages=', '.join(error.packages)),
                        MessageBox.Yes | MessageBox.No, MessageBox.No) == MessageBox.Yes:
                    self.update_application(system_consent=True)
                    return
            self._message(error)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.job(update, done, failed)

    def apply_application(self):
        if not self.root or self.busy:
            return
        # Reuse the app's existing close flow for unsaved files and active runs.
        # The helper waits for BOTH UI and core, and fails instead of killing a
        # core that the user deliberately kept running.
        host = self.parentWidget()
        host._apply_update_after_close = True
        self.accept()
        if not host.close() and getattr(host, '_close_review', None) is None:
            host._apply_update_after_close = False
            self.show()

    def check_plugins(self):
        value = self.catalog.text().strip()
        if not value:
            self._message(tr('updates.catalog_required')); return
        settings = QSettings('Toshy', 'Toshy')
        settings.setValue('plugins/repository_url', value)
        settings.sync()
        from core.plugins.store import PluginStore
        from core.plugins.updates import check_updates
        store = PluginStore(APP_PATHS.root / 'plugins')
        self.job(lambda: check_updates(store, value, cancel=self.cancel), self.render_plugins)

    def render_plugins(self, records):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for record in records:
            panel = QFrame(self)
            row = QHBoxLayout(panel)
            label = QLabel(record['id'] + '  ' + record['installed'], panel)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            if record['status'] == 'available':
                entry = record['entry']
                button = QPushButton(tr('updates.install_plugin', version=entry['version']), panel)
                button.clicked.connect(lambda _=False, e=entry: self.update_plugin(e))
                row.addWidget(button)
            else:
                row.addWidget(QLabel(tr('updates.' + record['status']), panel))
            from core.plugins.updates import previous_version
            from core.plugins.store import PluginStore
            if previous_version(PluginStore(APP_PATHS.root / 'plugins'), record['id']):
                rollback_button = QPushButton(tr('updates.rollback_plugin'), panel)
                rollback_button.clicked.connect(lambda _=False, key=record['id']: self.rollback_plugin(key))
                row.addWidget(rollback_button)
            self.rows.addWidget(panel)
        self._message(tr('updates.no_plugins') if not records else '')

    def update_plugin(self, entry):
        from core.plugins.store import PluginStore
        from core.plugins.updates import install_update
        store = PluginStore(APP_PATHS.root / 'plugins')
        value = self.catalog.text().strip()
        self.job(lambda: install_update(store, entry, value, cancel=self.cancel),
                 lambda _: self._message(tr('updates.plugin_installed')))

    def rollback_plugin(self, identity):
        from core.plugins.store import PluginStore
        from core.plugins.updates import rollback
        self.job(lambda: rollback(PluginStore(APP_PATHS.root / 'plugins'), identity),
                 lambda _: self._message(tr('updates.plugin_installed')))

    def reject(self):
        self.cancel.set()
        if self.busy:
            self._message(tr('updates.cancelling'))
            return
        super().reject()


from ui.update_notifications import offer_automatic_check as offer_automatic_check


def launch_prepared_update(root):
    import shutil, uuid, json, re
    from release_tools.environments import environment
    from release_tools.engine import python_in
    from ui.i18n import i18n
    from ui.themes.manager import theme_manager
    root = Path(root)
    state = layout.owned(root)
    from release_tools.portable import workspace
    directory = workspace(root)
    for old in directory.glob('handoff-*'):
        if old.is_symlink() or not re.fullmatch('handoff-[a-f0-9]{32}', old.name):
            continue
        try:
            receipt = layout.read_json(old / '.toshy-handoff.json')
            if receipt.get('product') != 'Toshy update helper' or not receipt.get('pid'):
                continue
            os.kill(int(receipt['pid']), 0)
        except ProcessLookupError:
            shutil.rmtree(old, ignore_errors=True)
        except (OSError, ValueError):
            pass
    helper = directory / ('handoff-' + uuid.uuid4().hex)
    helper.mkdir(parents=True)
    # Freeze updater code before it starts replacing the active source tree.
    shutil.copytree(APP_PATHS.code_root / 'release_tools', helper / 'release_tools',
                    ignore=shutil.ignore_patterns('__pycache__'))
    colors = [theme_manager.color(key).name() for key in ('background.window', 'text.primary', 'accent.primary')]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(helper)
    with (root / 'update-handoff.log').open('ab') as output:
        process = subprocess.Popen([str(python_in(environment(root, state['current'], state))),
                          '-m', 'release_tools.update_window', '--root', str(root),
                          '--language', i18n.current_lang if i18n.current_lang in {'en', 'ru'} else 'en',
                          '--colors', json.dumps(colors)], cwd=helper, env=env,
                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                         start_new_session=os.name != 'nt',
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    layout.write_json(helper / '.toshy-handoff.json', {'product': 'Toshy update helper', 'pid': process.pid})
