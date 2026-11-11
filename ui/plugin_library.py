"""Plugin catalog UI. Public downloads run outside the GUI thread."""
from ui.styles.binding import apply_style
from ui.icons import set_label_icon, set_icon
from ui.i18n import bind_caption, add_caption_item, tr, localize, set_text, add_item

import json
from pathlib import Path
import shutil
import tempfile
from threading import Event
from packaging.version import Version
from shiboken6 import isValid
from PySide6.QtCore import QObject, Signal, Qt, QTimer
from ui.settings import QSettings
from PySide6.QtGui import QPixmap, QImageReader, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QComboBox, QProgressBar, QScrollArea, QWidget, QFrame, QFileDialog, QCheckBox)
from ui.widgets.dialogs import InputDialog as QInputDialog
from ui.widgets.dialogs import MessageBox as QMessageBox

from core.plugins.catalog import (CatalogClient, DownloadedPackage, DownloadCancelled,
    check_cancel, https_url, github_parts)
from core.plugins.package import inspect_package, package_digest, MAX_BYTES
from core.plugins.manifest import PluginError

from core.paths import APP_PATHS
try:
    DEFAULT_PLUGIN_REPOSITORY = json.loads((APP_PATHS.code_root / 'release.json').read_text()).get('plugin_catalog', '')
except (OSError, ValueError):
    DEFAULT_PLUGIN_REPOSITORY = ''
PLUGIN_REPOSITORY_KEY = 'plugins/repository_url'
OFFICIAL_PLUGIN_REPOSITORY = 'https://github.com/supertoshibaz/toshy-agent-plugins'


def is_official_plugin(entry):
    """The badge identifies the publisher's repository, never an author label."""
    try:
        official = tuple(part.casefold() for part in github_parts(OFFICIAL_PLUGIN_REPOSITORY)[:2])
        return all(tuple(part.casefold() for part in github_parts(url)[:2]) == official
                   for url in (entry['source'], entry['archive']['url']))
    except (ValueError, KeyError, TypeError):
        return False


class _Progress(QObject):
    changed = Signal(object, int, int, str)


class PluginLibraryPages:
    def setup_library(self):
        self.catalog_client = CatalogClient(self.store.root / 'cache')
        self.catalog_entries = []; self.catalog_source = ''; self._catalog_limit = 100
        self.network_cancel = None; self.icon_cancel = Event(); self._icon_pending = set()
        self._icon_attempted = set(); self.catalog_rows = []
        self.progress_relay = _Progress(self)
        self.progress_relay.changed.connect(self.network_progress)
        from ui.core_bridge.async_runner import AsyncRunner
        self.icon_runner = AsyncRunner(self.host_window, max_threads=2)
        page = self.tabs.widget(1); layout = QVBoxLayout(page)
        official_row = QHBoxLayout()
        official_row.addWidget(localize(QLabel('', page), 'plugins.official_repository'))
        self.official_repository_link = QLabel(page)
        self.official_repository_link.setObjectName('OfficialPluginRepository')
        self.official_repository_link.setText(
            f'<a href="{OFFICIAL_PLUGIN_REPOSITORY}">{OFFICIAL_PLUGIN_REPOSITORY}</a>')
        self.official_repository_link.setOpenExternalLinks(True)
        self.official_repository_link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.official_repository_link.setWordWrap(True)
        official_row.addWidget(self.official_repository_link, 1)
        layout.addLayout(official_row)
        layout.addWidget(localize(QLabel('', page), 'plugins.community_repository'))
        row = QHBoxLayout(); self.repository_edit = QLineEdit(page)
        self.repository_edit.setObjectName('PluginRepositoryUrl')
        set_text(self.repository_edit, 'ui.plugin_library.plugin_repository_url_2e4c', setter='setAccessibleName')
        set_text(self.repository_edit, 'ui.plugin_library.https_github_com_owner_repository_or_ac62', setter='setPlaceholderText')
        self.repository_edit.setMaxLength(2048)
        bind_caption(self.repository_edit, str(QSettings('Toshy', 'Toshy').value(PLUGIN_REPOSITORY_KEY, DEFAULT_PLUGIN_REPOSITORY)))
        row.addWidget(self.repository_edit, 1)
        self.repository_open = QToolButton(page)
        set_icon(self.repository_open, 'external', square=True)
        set_text(self.repository_open, 'plugins.open_registry', setter='setToolTip')
        self.repository_open.clicked.connect(self.open_repository)
        row.addWidget(self.repository_open)
        self.repository_save = localize(QPushButton('', page), 'ui.settings.save_b4d3'); self.repository_save.clicked.connect(self.save_repository)
        row.addWidget(self.repository_save)
        self.catalog_refresh = localize(QPushButton('', page), 'ui.plugin_library.refresh_list_163a'); self.catalog_refresh.clicked.connect(self.refresh_catalog)
        row.addWidget(self.catalog_refresh); layout.addLayout(row)
        self.repository_edit.returnPressed.connect(self.save_repository)
        self.repository_edit.textEdited.connect(self.repository_edited)
        row = QHBoxLayout(); self.import_button = localize(QPushButton('', page), 'ui.plugin_library.import_zip_d59c')
        self.import_button.clicked.connect(self.import_local_archive); row.addWidget(self.import_button)
        self.github_button = localize(QPushButton('', page), 'ui.plugin_library.import_from_github_2f16')
        self.github_button.clicked.connect(self.import_github); row.addWidget(self.github_button)
        row.addStretch(); layout.addLayout(row)
        row = QHBoxLayout(); self.catalog_search = QLineEdit(page)
        set_text(self.catalog_search, 'ui.plugin_library.search_names_and_descriptions_0cab', setter='setPlaceholderText')
        self.catalog_search.textChanged.connect(self.filter_catalog); row.addWidget(self.catalog_search, 1)
        self.catalog_tag = QComboBox(page); add_item(self.catalog_tag, 'ui.plugin_library.all_tags_25ba', '')
        self.catalog_tag.currentIndexChanged.connect(self.filter_catalog); row.addWidget(self.catalog_tag)
        layout.addLayout(row)
        self.repository_message = QLabel(page); self.repository_message.setTextFormat(Qt.PlainText)
        self.repository_message.setWordWrap(True); layout.addWidget(self.repository_message)
        row = QHBoxLayout(); self.download_progress = QProgressBar(page)
        row.addWidget(self.download_progress, 1)
        self.download_cancel = localize(QPushButton('', page), 'ui.close_panel.cancel_8fbe'); self.download_cancel.clicked.connect(self.cancel_network)
        row.addWidget(self.download_cancel); layout.addLayout(row)
        self.download_progress.hide(); self.download_cancel.hide()
        self.catalog_scroll = QScrollArea(page); self.catalog_scroll.setWidgetResizable(True)
        self.catalog_scroll.setFrameShape(QFrame.NoFrame)
        self.catalog_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.catalog_body = QWidget(); self.catalog_body.setObjectName('PluginCatalogHost')
        apply_style(self.catalog_body, 'QWidget#PluginCatalogHost {background:@background.window@;}')
        self.catalog_layout = QVBoxLayout(self.catalog_body)
        self.catalog_layout.setContentsMargins(0, 0, 0, 0)
        self.catalog_scroll.setWidget(self.catalog_body); layout.addWidget(self.catalog_scroll, 1)
        self.catalog_more = localize(QPushButton('', page), 'ui.plugin_library.show_more_c682'); self.catalog_more.clicked.connect(self.more_catalog)
        layout.addWidget(self.catalog_more)
        self.icon_timer = QTimer(self); self.icon_timer.setInterval(250)
        self.icon_timer.timeout.connect(self.load_visible_icons); self.icon_timer.start()
        self.load_cached_catalog()

    def library_message(self, message, error=False):
        bind_caption(self.repository_message, str(message))
        self.repository_message.setStyleSheet('color:#fbbf24;' if error else 'color:#a1a1aa;')

    def repository_edited(self):
        # Never leave another repository's tiles under an edited source address.
        self.catalog_source = ''; self.catalog_entries = []; self.render_catalog()
        self.library_message(tr('ui.caption.e3588eaf0c14'))

    def open_repository(self):
        try:
            address = https_url(self.repository_edit.text().strip())
        except ValueError as error:
            self.library_message(error, True)
            return
        QDesktopServices.openUrl(QUrl(address))

    def save_repository(self):
        value = self.repository_edit.text().strip()
        try:
            if value: https_url(value)
            settings = QSettings('Toshy', 'Toshy')
            settings.setValue(PLUGIN_REPOSITORY_KEY, value); settings.sync()
            if settings.status() != QSettings.NoError: raise OSError('Не удалось сохранить адрес.')
        except (ValueError, OSError) as error:
            self.library_message(error, True); return False
        bind_caption(self.repository_edit, value); self.load_cached_catalog()
        self.library_message(tr('ui.caption.0352b7c8f8fb') if value else tr('ui.caption.daf8af3f6292'))
        return True

    def load_cached_catalog(self):
        source = self.repository_edit.text().strip()
        cached = self.catalog_client.cached_catalog(source) if source else None
        self.set_catalog(cached or {'plugins': []}, source)
        self.library_message(tr('ui.caption.a4ce63468671')
            if cached is not None else tr('ui.caption.6aa4a3ad19b4'))

    def set_catalog(self, catalog, source):
        self.catalog_source = source; self.catalog_entries = catalog['plugins']; self._catalog_limit = 100
        selected = self.catalog_tag.currentData()
        self.catalog_tag.blockSignals(True); self.catalog_tag.clear(); add_item(self.catalog_tag, 'ui.plugin_library.all_tags_25ba', '')
        for tag in sorted({t for e in self.catalog_entries for t in e['tags']}): add_caption_item(self.catalog_tag, tag, tag)
        index = self.catalog_tag.findData(selected)
        self.catalog_tag.setCurrentIndex(max(0, index)); self.catalog_tag.blockSignals(False)
        self.render_catalog()

    def filter_catalog(self, *_):
        self._catalog_limit = 100; self.render_catalog()

    def more_catalog(self):
        self._catalog_limit += 100; self.render_catalog()

    def render_catalog(self):
        while self.catalog_layout.count():
            item = self.catalog_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.catalog_rows = []
        query = self.catalog_search.text().strip().casefold(); tag = self.catalog_tag.currentData()
        entries = [e for e in self.catalog_entries if (not tag or tag in e['tags']) and
            (not query or query in ' '.join(str(e[k]) for k in ('id','name','description','author')).casefold())]
        try: installed = self.store.state()['versions']
        except Exception as error:
            self.library_message(error, True); installed = {}
        for entry in entries[:self._catalog_limit]:
            frame = QFrame(self.catalog_body); frame.setObjectName('CatalogPluginTile')
            apply_style(frame, 'QFrame#CatalogPluginTile {background:@background.panel@;border:1px solid @border.subtle@;border-radius:8px;}')
            row = QHBoxLayout(frame); picture = set_label_icon(QLabel('', frame), 'module')
            picture.setAlignment(Qt.AlignCenter); picture.setFixedSize(48, 48); row.addWidget(picture)
            body = QVBoxLayout(); heading = bind_caption(QLabel('', frame), entry['name'] + '   ·   ' + ' · '.join(entry['tags']))
            heading.setTextFormat(Qt.PlainText); heading.setWordWrap(True)
            title_row = QHBoxLayout(); title_row.addWidget(heading, 1)
            if is_official_plugin(entry):
                badge = set_label_icon(QLabel(frame), 'circle-check', size=18)
                badge.setObjectName('OfficialPluginBadge')
                badge.setFixedSize(22, 22)
                set_text(badge, 'plugins.official_badge', setter='setToolTip')
                set_text(badge, 'plugins.official_badge', setter='setAccessibleName')
                title_row.addWidget(badge, 0, Qt.AlignTop)
            body.addLayout(title_row)
            description = QLabel(entry['description'], frame); description.setTextFormat(Qt.PlainText)
            description.setWordWrap(True); description.setMaximumHeight(48); bind_caption(description, entry['description'], setter='setToolTip')
            body.addWidget(description)
            current = installed.get(entry['id'])
            version = localize(QLabel('', frame), 'ui.plugin_library.installed_v0_available_v1_3e85', v0=current or 'нет', v1=entry['version'])
            body.addWidget(version); row.addLayout(body, 1)
            label = tr('ui.caption.b7cd6fae08d9') if current is None else tr('ui.project_history.refresh_603e') if Version(entry['version']) > Version(current) else tr('ui.caption.0b29a3640f4c') if Version(entry['version']) == Version(current) else tr('ui.caption.e5fc019c92ec')
            button = bind_caption(QPushButton('', frame), label)
            button.setEnabled(not self._busy and (current is None or Version(entry['version']) > Version(current)))
            button.clicked.connect(lambda checked=False, e=entry: self.install_catalog_entry(e))
            row.addWidget(button); set_text(frame, 'ui.plugin_library.v0_catalog_v1_source_v2_195f', setter='setToolTip', v0=entry['id'], v1=self.catalog_source, v2=entry['source'])
            self.catalog_layout.addWidget(frame); self.catalog_rows.append((frame, picture, entry['image']))
            self.set_icon(picture, self.catalog_client.cached_icon(entry['image']))
        if not entries:
            empty = bind_caption(QLabel('', self.catalog_body), tr('ui.caption.2fbd2f6fda54') if self.catalog_entries else tr('ui.caption.00ee3aab322b'))
            self.catalog_layout.addWidget(empty)
        self.catalog_layout.addStretch(); self.catalog_more.setVisible(len(entries) > self._catalog_limit)

    @staticmethod
    def set_icon(label, path):
        if path is None or not isValid(label): return
        reader = QImageReader(str(path)); size = reader.size()
        if size.isEmpty() or size.width() * size.height() > 4_000_000: return
        reader.setScaledSize(size.scaled(48, 48, Qt.KeepAspectRatio))
        image = reader.read()
        if not image.isNull(): label.setPixmap(QPixmap.fromImage(image))

    def load_visible_icons(self):
        if self._dismissed or not self.tabs.widget(1).isVisible(): return
        viewport = self.catalog_scroll.viewport()
        for frame, label, source in self.catalog_rows:
            if len(self._icon_pending) >= 2: break
            if source in self._icon_attempted or source in self._icon_pending: continue
            if not frame.isVisible() or not frame.rect().translated(frame.mapTo(viewport, frame.rect().topLeft())).intersects(viewport.rect()): continue
            cached = self.catalog_client.cached_icon(source)
            if cached is not None:
                self.set_icon(label, cached); self._icon_attempted.add(source); continue
            self._icon_pending.add(source); self._icon_attempted.add(source)
            def done(path=None, error=None, source=source):
                if not isValid(self) or self._dismissed: return
                self._icon_pending.discard(source)
                if path:
                    for _, picture, url in self.catalog_rows:
                        if url == source: self.set_icon(picture, path)
            self.icon_runner.submit(lambda source=source: self.catalog_client.fetch_icon(source, cancel=self.icon_cancel),
                lambda path, done=done: done(path), lambda error, done=done: done(error=error))

    def network_job(self, function, success):
        if self._busy: return
        cancel = Event(); self.network_cancel = cancel
        self.download_progress.setRange(0, 0); self.download_progress.show()
        self.download_cancel.setEnabled(True); self.download_cancel.show()
        relay = self.progress_relay
        def progress(done, total, phase):
            if isValid(relay): relay.changed.emit(cancel, done, total, phase)
        self.run_job(lambda: function(cancel, progress), success)

    def network_progress(self, cancel, done, total, phase):
        if cancel is not self.network_cancel or self._dismissed or cancel.is_set(): return
        self.download_progress.setRange(0, 100 if total else 0)
        if total: self.download_progress.setValue(min(100, int(100 * done / total)))
        self.library_message(phase + (tr('ui.caption.d74af7813f9c', v0=done // 1024, v1=total // 1024) if total else ''))

    def cancel_network(self):
        if self.network_cancel is not None:
            self.network_cancel.set(); self.download_cancel.setEnabled(False)
            self.library_message(tr('ui.caption.c2fb9decfb9b'))

    def refresh_catalog(self):
        if self._busy or not self.save_repository(): return
        source = self.repository_edit.text().strip()
        if not source: self.library_message(tr('ui.caption.bf4e03a66eff'), True); return
        def complete(catalog):
            self.set_catalog(catalog, source)
            self.library_message(tr('ui.caption.7edea87e9c8f', v0=len(self.catalog_entries)))
        self.network_job(lambda cancel, progress: self.catalog_client.fetch_catalog(source, cancel=cancel, progress=progress), complete)

    def install_catalog_entry(self, entry):
        if self._busy: return
        candidate = dict(url=entry['archive']['url'], size=entry['archive']['size'], sha256=entry['archive']['sha256'])
        origin = tr('ui.caption.86cd2662c275') + self.catalog_source
        self.network_job(lambda cancel, progress: self.catalog_client.download_package(candidate, entry=entry, cancel=cancel, progress=progress),
            lambda package: self.confirm_package(package, origin))

    def import_github(self):
        if self._busy: return
        source, accepted = QInputDialog.getText(self, tr('ui.plugin_library.import_from_github_b049'), tr('ui.plugin_library.public_repository_release_page_or_release_850c'))
        if not accepted or not source.strip(): return
        source = source.strip()
        def complete(candidates):
            candidate = candidates[0]
            if len(candidates) > 1:
                labels = [tr('ui.caption.deaa1bd25d17', v0=i+1, v1=c['name'], v2=(c['size'] or 0)//1024) for i,c in enumerate(candidates)]
                selected, ok = QInputDialog.getItem(self, tr('ui.plugin_library.release_files_b994'), tr('ui.plugin_library.select_a_ready_to_install_plugin_7987'), labels, 0, False)
                if not ok: return
                candidate = candidates[labels.index(selected)]
            self.network_job(lambda cancel, progress: self.catalog_client.download_package(candidate, cancel=cancel, progress=progress),
                lambda package: self.confirm_package(package, tr('ui.caption.fd1f681a73e3')))
        self.network_job(lambda cancel, progress: self.catalog_client.release_candidates(source, cancel=cancel, progress=progress), complete)

    def import_local_archive(self):
        if self._busy: return
        filename, _ = QFileDialog.getOpenFileName(self, 'Импорт плагина', '', 'Toshy plugin (*.zip)')
        if not filename: return
        def inspect():
            temporary = tempfile.TemporaryDirectory(prefix='toshy-plugin-')
            try:
                if Path(filename).stat().st_size > MAX_BYTES: raise PluginError('ZIP превышает 256 МиБ.')
                path = Path(temporary.name) / 'plugin.zip'; shutil.copyfile(filename, path)
                return DownloadedPackage(temporary, path, inspect_package(path), package_digest(path), str(Path(filename).resolve()))
            except BaseException:
                temporary.cleanup(); raise
        self.run_job(inspect, lambda package: self.confirm_package(package, tr('ui.caption.c33b18517216')))

    def confirm_package(self, package, origin):
        manifest = package.manifest
        try:
            current = self.store.state()['versions'].get(manifest['id'])
            warning = (tr('ui.caption.ef6e615f1847', v0=manifest['name'], v1=manifest['version'], v2=manifest['id'], v3=current or 'нет', v4=origin, v5=package.source))
            if current and Version(manifest['version']) < Version(current): warning += tr('ui.caption.f0af1933ed0f')
            question = QMessageBox(self); set_text(question, 'ui.plugin_library.install_third_party_code_6d30', setter='setWindowTitle')
            question.setTextFormat(Qt.PlainText); bind_caption(question, warning)
            question.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel); question.setDefaultButton(QMessageBox.Cancel)
            remember = localize(QCheckBox(question), 'ui.plugins.dont_show_again')
            question.setCheckBox(remember)
            preferences = QSettings('Toshy', 'Toshy')
            skip = preferences.value('plugins/skip_install_confirmation', False, type=bool)
            if not skip and question.exec() != QMessageBox.Yes:
                package.close(); self.library_message(tr('ui.caption.53b9d48aa1e8')); return
            if not skip and remember.isChecked():
                preferences.setValue('plugins/skip_install_confirmation', True)
            question.deleteLater()
        except BaseException:
            package.close(); raise
        def install():
            try:
                return self.store.install(package.path, expected_sha256=package.digest,
                    expected_plugin_id=manifest['id'], expected_version=manifest['version'], source=package.source)
            finally: package.close()
        def complete(_):
            self.refresh_installed(); self.render_catalog()
            self.library_message(tr('ui.caption.1d39a94228bc', v0=manifest['name'], v1=manifest['version']))
        self.library_message(tr('ui.caption.a0f970e37512'))
        self.run_job(install, complete)
