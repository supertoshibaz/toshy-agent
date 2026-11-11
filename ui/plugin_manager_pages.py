"""Plugin tiles, animated priorities and local authoring/import controls."""
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text, add_row
from ui.styles.binding import apply_style, watch_paint
from ui.icons import set_icon, set_label_icon
from ui.themes.manager import color

import json
from pathlib import Path
from shiboken6 import isValid
from PySide6.QtCore import Qt, Signal, QPoint, QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QTimer
from PySide6.QtGui import QPainter, QPixmap, QIcon, QColor
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QToolButton, QHBoxLayout, QVBoxLayout, QFormLayout, QScrollArea, QFileDialog, QLineEdit, QComboBox, QPlainTextEdit, QMenu, QCheckBox)
from ui.widgets.dialogs import MessageBox as QMessageBox
from core.plugins.manifest import tags_for, PluginError
from core.plugins.package import inspect_package, inspect_directory, export_package, source_digest, package_digest
from core.plugins.resolver import resolve_plugins
from core.plugins.store import PluginStore
from core.plugins.runtime import get_runtime
from ui.plugin_library import PluginLibraryPages




class PluginSwitch(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setFixedSize(38, 24)
        watch_paint(self)
        set_text(self, 'ui.plugin_manager_pages.enable_plugin_after_restart_cfc0', setter='setAccessibleName')
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(color('accent.subtle' if self.isChecked() else 'control.disabled'))
        p.drawRoundedRect(1, 3, 36, 18, 9, 9)
        p.setBrush(color('text.primary')); p.drawEllipse(21 if self.isChecked() else 4, 6, 12, 12)
        if self.hasFocus():
            p.setPen(color('border.focus')); p.setBrush(Qt.NoBrush); p.drawRoundedRect(0, 1, 37, 22, 10, 10)


class PluginTile(QFrame):
    moved = Signal(object, int)
    finished = Signal()
    def __init__(self, manifest, directory, parent=None):
        super().__init__(parent)
        self.identity = manifest['id']; self.manifest = manifest
        try:
            self.source = json.loads((Path(directory) / '.toshy-receipt.json').read_text(encoding='utf-8')).get('source', '')
        except (OSError, ValueError, AttributeError):
            self.source = ''
        self.setObjectName('PluginTile'); self.setFixedHeight(108)
        apply_style(self, 'QFrame#PluginTile {background:@background.panel@;border:1px solid @border.subtle@;border-radius:8px;} QFrame#PluginTile[dragging="true"] {border-color:@accent.primary@;background:@background.surface@;}')
        row = QHBoxLayout(self); row.setContentsMargins(8, 10, 12, 10); row.setSpacing(10)
        # Same handle and motion threshold as the pipeline Router list.
        from ui.views.modules.inspector import LoopConditionDragHandle
        self.handle = LoopConditionDragHandle(self)
        set_text(self.handle, 'ui.plugin_manager_pages.drag_to_reorder_higher_items_have_2d47', setter='setToolTip')
        self.handle.drag_moved.connect(lambda y: self.moved.emit(self, y))
        self.handle.drag_finished.connect(self.finished)
        row.addWidget(self.handle)
        picture = QLabel(self); picture.setFixedSize(56, 56)
        pixmap = QPixmap(str(Path(directory) / manifest['image']))
        if not pixmap.isNull():
            picture.setPixmap(pixmap.scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            set_label_icon(picture, 'module'); picture.setAlignment(Qt.AlignCenter)
        row.addWidget(picture)
        body = QVBoxLayout(); body.setSpacing(4)
        heading = QHBoxLayout(); heading.setSpacing(6)
        self.name = QLabel(manifest['name']); self.name.setTextFormat(Qt.PlainText)
        apply_style(self.name, 'font-weight:600;color:@text.primary@;'); heading.addWidget(self.name)
        self.warning = QToolButton(self); set_icon(self.warning, 'warning', size=18)
        self.warning.setFixedSize(22, 22); self.warning.setStyleSheet('border:none;background:transparent;')
        heading.addWidget(self.warning)
        tags = QLabel(' · '.join(tags_for(manifest)), self); tags.setWordWrap(True)
        tags.setStyleSheet('font-size:10px;color:#a5b4fc;'); heading.addWidget(tags, 1)
        body.addLayout(heading)
        description = QLabel(manifest['description'], self); description.setTextFormat(Qt.PlainText)
        description.setWordWrap(True); description.setMaximumHeight(32)
        bind_caption(description, manifest['description'], setter='setToolTip'); body.addWidget(description)
        self.status = QLabel(self); apply_style(self.status, 'font-size:10px;color:@text.secondary@;')
        body.addWidget(self.status)
        row.addLayout(body, 1)
        self.toggle = PluginSwitch(self); row.addWidget(self.toggle)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def update_status(self, planned, actual=None):
        messages = list(planned.warnings)
        if actual is not None:
            messages.extend(actual.warnings)
        messages = list(dict.fromkeys(messages))
        self.warning.setVisible(bool(messages))
        bind_caption(self.warning, '\n\n'.join(messages), setter='setToolTip')
        names = {'disabled': tr('ui.caption.5a577d905660'), 'blocked': tr('ui.caption.78157ae0c597'), 'partial': tr('ui.caption.c33f1894e0f1'), 'ready': tr('ui.caption.ee4e39727b2e')}
        text = f"{self.manifest['version']} · {names.get(planned.state, planned.state)}"
        if actual is not None:
            text += tr('ui.caption.57559792a7c6', v0=names.get(actual.state, actual.state))
        bind_caption(self.status, text)
        bind_caption(self, self.identity + (tr('ui.caption.b78f3885ffea') + self.source if self.source else ''), setter='setToolTip')


class PluginPriorityList(QWidget):
    reordered = Signal(list)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PluginPriorityHost")
        self.rows = []; self.drag = None; self.animation = None
        self.spacing = 8; self.setMinimumHeight(1)

    def replace_rows(self, rows):
        self.cancel_drag()
        for old in self.rows:
            old.deleteLater()
        self.rows = rows
        for row in rows:
            row.setParent(self); row.moved.connect(self.move_drag); row.finished.connect(self.finish_drag); row.show()
        self.arrange()

    def arrange(self, skip=None, animate=False):
        if self.animation is not None:
            self.animation.stop(); self.animation.deleteLater(); self.animation = None
        total = sum(r.height() + self.spacing for r in self.rows)
        self.setMinimumHeight(max(1, total))
        group = QParallelAnimationGroup(self)
        y = 0
        for row in self.rows:
            row.resize(self.width(), row.height())
            target = QPoint(0, y); y += row.height() + self.spacing
            if row is skip:
                continue
            if animate and row.pos() != target:
                motion = QPropertyAnimation(row, b'pos', group)
                motion.setDuration(115); motion.setStartValue(row.pos()); motion.setEndValue(target)
                motion.setEasingCurve(QEasingCurve.OutCubic); group.addAnimation(motion)
            else:
                row.move(target)
        if group.animationCount():
            self.animation = group; group.start()
        else:
            group.deleteLater()

    def move_drag(self, row, global_y):
        if self.drag is None:
            self.drag = (row, global_y - row.mapToGlobal(QPoint()).y())
            row.setProperty('dragging', True); row.style().unpolish(row); row.style().polish(row)
        if self.drag[0] is not row:
            return
        y = max(0, min(global_y - self.mapToGlobal(QPoint()).y() - self.drag[1], self.height() - row.height()))
        row.move(0, y); row.raise_()
        others = [r for r in self.rows if r is not row]
        index = max(0, min(round(y / (row.height() + self.spacing)), len(others)))
        others.insert(index, row)
        if others != self.rows:
            self.rows = others; self.arrange(skip=row, animate=True)
        # Scroll the list when its drag handle approaches either viewport edge.
        scroll = self.parentWidget().parentWidget()
        if isinstance(scroll, QScrollArea):
            local = scroll.viewport().mapFromGlobal(QPoint(0, global_y)).y()
            bar = scroll.verticalScrollBar()
            if local < 28: bar.setValue(bar.value() - 18)
            elif local > scroll.viewport().height() - 28: bar.setValue(bar.value() + 18)

    def finish_drag(self):
        if self.drag is None:
            return
        row = self.drag[0]; self.drag = None
        row.setProperty('dragging', False); row.style().unpolish(row); row.style().polish(row)
        self.arrange(animate=True)
        self.reordered.emit([r.identity for r in self.rows])

    def cancel_drag(self):
        if self.drag is not None:
            row = self.drag[0]; row.setProperty('dragging', False); self.drag = None
        if self.animation is not None:
            self.animation.stop(); self.animation.deleteLater(); self.animation = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.drag is None:
            self.arrange()


class PluginManagerPages(PluginLibraryPages):
    def setup_manager(self):
        from core.paths import APP_PATHS
        self.runtime = getattr(self.host_window, 'plugin_runtime', None) or get_runtime()
        self.store = self.runtime.store if self.runtime is not None else PluginStore(APP_PATHS.root / 'plugins')
        from ui.core_bridge.async_runner import AsyncRunner
        self.runner = AsyncRunner(self.host_window, max_threads=1)
        self._busy = False; self._source_fingerprint = None; self._diagnostic_signature = None
        installed = self.tabs.widget(0); layout = QVBoxLayout(installed)
        bar = QHBoxLayout()
        self.restart_label = QLabel(installed); self.restart_label.setWordWrap(True)
        bar.addWidget(self.restart_label, 1)
        self.updates_button = QPushButton(tr('updates.check'), installed)
        self.updates_button.clicked.connect(self.open_plugin_updates)
        bar.addWidget(self.updates_button)
        layout.addLayout(bar)
        self.scroll = QScrollArea(installed); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.priority_list = PluginPriorityList(); self.scroll.setWidget(self.priority_list)
        self.priority_list.reordered.connect(self.reorder_plugins); layout.addWidget(self.scroll, 1)
        self.empty_label = localize(QLabel('', installed), 'ui.plugin_manager_pages.no_plugins_installed_yet_import_them_134c')
        self.empty_label.setWordWrap(True); layout.addWidget(self.empty_label)
        self.manager_message = QLabel(installed); self.manager_message.setWordWrap(True)
        layout.addWidget(self.manager_message)
        self.setup_library()
        self.setup_export()
        self.refresh_installed()
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(1500)
        self.status_timer.timeout.connect(self.refresh_diagnostics)
        self.status_timer.start()

    def refresh_diagnostics(self):
        if self._busy or self.priority_list.drag is not None or self.runtime is None:
            return
        warnings = self.runtime.core_warnings()
        signature = (repr(warnings), repr([(p, status.state, status.warnings) for p, status in self.runtime.plan.plugins.items()]))
        if signature == self._diagnostic_signature:
            return
        self._diagnostic_signature = signature
        try:
            self.update_statuses()
        except Exception as error:
            self.error_message(error)
            self.status_timer.stop()
            return
        for row in self.priority_list.rows:
            if row.identity in warnings:
                message = '\n\n'.join(warnings[row.identity])
                existing = row.warning.toolTip()
                if message and message not in existing:
                    bind_caption(row.warning, existing + ('\n\n' if existing else '') + message, setter='setToolTip')
                    row.warning.show()

    def error_message(self, error):
        if isValid(self) and not self._dismissed:
            bind_caption(self.manager_message, str(error)); self.manager_message.setStyleSheet('color:#fbbf24;')
            if self.tabs.currentIndex() == 1:
                self.library_message(error, True)
            if self.tabs.currentIndex() == 2:
                self.export_preview.appendPlainText(tr('ui.caption.37eceb9f52a5') + str(error))

    def run_job(self, function, success):
        if self._busy:
            return
        self.manager_message.clear()
        self._busy = True; self.set_job_controls(False)
        def done(value=None, error=None):
            if not isValid(self) or self._dismissed:
                if hasattr(value, 'close'): value.close()
                return
            token = self.network_cancel
            self.network_cancel = None
            self.download_progress.hide(); self.download_cancel.hide()
            self._busy = False; self.set_job_controls(True)
            if token is not None and token.is_set() and error is None:
                if hasattr(value, 'close'): value.close()
                self.library_message(tr('ui.caption.588c29cbe7b8')); return
            if error is not None:
                self.error_message(error)
            else:
                try: success(value)
                except Exception as callback_error:
                    if hasattr(value, 'close'): value.close()
                    self.error_message(callback_error)
        self.runner.submit(function, lambda value: done(value), lambda error: done(error=error))

    def open_plugin_updates(self):
        from ui.update_dialog import UpdatesDialog
        panel = UpdatesDialog(self.host_window, tab=1)
        panel.exec()
        self.refresh_installed()

    def set_job_controls(self, enabled):
        for button in (self.import_button, self.github_button, self.catalog_refresh,
                       self.repository_save, self.repository_edit):
            button.setEnabled(enabled)
        self.export_button.setEnabled(enabled and self._source_fingerprint is not None)
        self.render_catalog()

    def refresh_installed(self):
        try:
            state = self.store.state(); manifests = self.store.manifests(state)
            plan = resolve_plugins(manifests, state['order'], state['enabled'])
            by_id = {m['id']: m for m in manifests}; rows = []
            for identity in state['order']:
                m = by_id[identity]
                row = PluginTile(m, self.store.root / 'packages' / identity / m['version'])
                row.toggle.setChecked(identity in state['enabled'])
                row.toggle.toggled.connect(lambda on, identity=identity: self.toggle_plugin(identity, on))
                row.customContextMenuRequested.connect(lambda pos, row=row: self.tile_menu(row, pos))
                actual = self.runtime.plan.plugins.get(identity) if self.runtime else None
                row.update_status(plan.plugins[identity], actual)
                rows.append(row)
            self.priority_list.replace_rows(rows); self.empty_label.setVisible(not rows)
            self.refresh_restart()
        except Exception as error:
            self.error_message(error)

    def refresh_restart(self):
        pending = self.runtime is not None and self.runtime.pending_restart
        bind_caption(self.restart_label, tr('ui.caption.7444287236e2') if pending else tr('ui.caption.cb3d9468de97'))
        self.restart_label.setStyleSheet('color:#fbbf24;' if pending else 'color:#a1a1aa;')

    def update_statuses(self):
        state = self.store.state(); plan = resolve_plugins(self.store.manifests(state), state['order'], state['enabled'])
        for row in self.priority_list.rows:
            actual = self.runtime.plan.plugins.get(row.identity) if self.runtime else None
            row.update_status(plan.plugins[row.identity], actual)
        self.refresh_restart()

    def toggle_plugin(self, identity, enabled):
        try:
            self.store.set_enabled(identity, enabled); self.update_statuses()
        except Exception as error:
            self.error_message(error); self.refresh_installed()

    def reorder_plugins(self, order):
        try:
            self.store.reorder(order); self.update_statuses()
        except Exception as error:
            self.error_message(error); self.refresh_installed()

    def import_archive(self):
        self.import_local_archive()

    def tile_menu(self, row, position):
        menu = QMenu(self)
        source = localize(add_caption_action(menu, 'addAction', 'Выбрать для экспорта'), 'ui.plugin_manager_pages.select_for_export_4d36')
        remove = localize(add_caption_action(menu, 'addAction', 'Удалить плагин…'), 'ui.plugin_manager_pages.remove_plugin_cb0a')
        action = menu.exec(row.mapToGlobal(position))
        if action is source:
            bind_caption(self.source_edit, str(self.store.root / 'packages' / row.identity / row.manifest['version']))
            self.tabs.setCurrentIndex(2); self.preview_source()
        elif action is remove:
            if self.runtime is not None and row.identity in self.runtime.state['enabled']:
                # Keep the frozen package available to a core launched later.
                self.error_message(tr('ui.caption.207546f3dcba')); return
            self.confirm_remove(row)

    def confirm_remove(self, row):
        try:
            directory = self.store.data_directory(row.identity)
            question = QMessageBox(self)
            set_text(question, 'ui.plugin_manager_pages.remove_plugin_60d2', setter='setWindowTitle')
            question.setIcon(QMessageBox.Question)
            question.setText(tr('ui.plugins.remove_question', name=row.manifest['name']))
            question.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
            question.setDefaultButton(QMessageBox.Cancel)
            keep = localize(QCheckBox(question), 'ui.plugins.keep_data')
            keep.setChecked(True)
            question.setCheckBox(keep)
            def describe(checked):
                question.setInformativeText(tr('ui.plugins.data_kept_at' if checked else 'ui.plugins.data_deleted_at', path=str(directory)))
            keep.toggled.connect(describe)
            describe(True)
            try:
                if question.exec() != QMessageBox.Yes:
                    return
                self.store.remove(row.identity, keep_data=keep.isChecked())
                self.refresh_installed()
            finally:
                question.deleteLater()
        except Exception as error:
            self.error_message(error)

    def setup_export(self):
        page = self.tabs.widget(2); layout = QVBoxLayout(page)
        form = QFormLayout(); self.source_edit = QLineEdit(page)
        row = QHBoxLayout(); row.addWidget(self.source_edit, 1)
        browse = localize(QPushButton('', page), 'ui.plugin_manager_pages.folder_6420'); browse.clicked.connect(self.browse_source); row.addWidget(browse)
        add_row(form, 'ui.plugin_manager_pages.plugin_source_9776', row)
        self.source_edit.textChanged.connect(self.source_changed)
        self.template_kind = QComboBox(page)
        for label, value in [(tr('ui.caption.afaa863ccd28'), (None,'add',None,None)),
            (tr('ui.caption.af28df96a997'), ('custom-module','add',None,None)),
            (tr('ui.caption.bffa3c8b1b0f'), ('system-module','add',None,None)),
            (tr('ui.caption.aa7b9fdf081c'), ('widget','add',None,None)),
            (tr('ui.caption.4820f03bad81'), ('workspace','add',None,None)),
            (tr('ui.caption.7212e4cc2e3d'), ('topbar','add',None,None)),
            (tr('nav.core'), ('core','add',None,None))]:
            add_caption_item(self.template_kind, label, value)
        self.plugin_id = QLineEdit('author.my-plugin', page); self.plugin_name = QLineEdit(page)
        set_text(self.plugin_name, 'plugins.default_name', setter='setPlaceholderText')
        self.plugin_author = QLineEdit(page); self.plugin_description = QLineEdit(page)
        self.image_edit = QLineEdit(page)
        image_row = QHBoxLayout(); image_row.addWidget(self.image_edit, 1)
        image_button = localize(QPushButton('', page), 'ui.plugin_manager_pages.png_17eb'); image_button.clicked.connect(self.browse_image); image_row.addWidget(image_button)
        add_row(form, 'ui.plugin_manager_pages.template_fff8', self.template_kind); add_row(form, 'ui.plugin_manager_pages.id_3843', self.plugin_id)
        add_row(form, 'ui.subagents_panel.name_0918', self.plugin_name); add_row(form, 'ui.plugin_manager_pages.author_e828', self.plugin_author)
        add_row(form, 'ui.plugin_manager_pages.description_b368', self.plugin_description); add_row(form, 'ui.plugin_manager_pages.image_a029', image_row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        create = localize(QPushButton('', page), 'ui.plugin_manager_pages.create_source_9b9d'); create.clicked.connect(self.create_source); buttons.addWidget(create)
        preview = localize(QPushButton('', page), 'ui.plugin_manager_pages.inspect_package_539d'); preview.clicked.connect(self.preview_source); buttons.addWidget(preview)
        self.export_button = localize(QPushButton('', page), 'ui.plugin_manager_pages.export_zip_4a06'); self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_source); buttons.addWidget(self.export_button)
        layout.addLayout(buttons)
        self.export_preview = QPlainTextEdit(page); self.export_preview.setReadOnly(True)
        set_text(self.export_preview, 'ui.plugin_manager_pages.select_a_folder_with_plugin_json_aca6', setter='setPlaceholderText')
        layout.addWidget(self.export_preview, 1)

    def browse_source(self):
        folder = QFileDialog.getExistingDirectory(self, 'Исходники плагина')
        if folder: bind_caption(self.source_edit, folder)

    def browse_image(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Картинка плагина', '', 'PNG (*.png)')
        if filename: bind_caption(self.image_edit, filename)

    def source_changed(self):
        self._source_fingerprint = None; self.export_button.setEnabled(False)

    def create_source(self):
        parent = QFileDialog.getExistingDirectory(self, 'Где создать папку плагина')
        if not parent: return
        kind, mode, target, operation = self.template_kind.currentData()
        module_source = None; module_name = None
        if kind in ('system-module','custom-module'):
            module_source = QFileDialog.getExistingDirectory(self, 'Выберите исходный модуль')
            if not module_source: return
            module_name = Path(module_source).name
        destination = Path(parent) / self.plugin_id.text().strip()
        from core.plugins.authoring import create_scaffold
        values = dict(identity=self.plugin_id.text().strip(), name=self.plugin_name.text().strip() or 'My plugin',
            author=self.plugin_author.text().strip(), description=self.plugin_description.text().strip(),
            kind=kind, mode=mode, target=target, operation=operation, image=self.image_edit.text().strip() or None,
            module_source=module_source, module_name=module_name)
        def complete(_):
            bind_caption(self.source_edit, str(destination))
            if kind is None:
                self.export_preview.setPlainText(tr('ui.caption.c173fcc61035'))
            else:
                self.preview_source()
        self.run_job(lambda: create_scaffold(destination, **values), complete)

    def preview_source(self):
        source = Path(self.source_edit.text()).expanduser()
        self._source_fingerprint = None; self.export_button.setEnabled(False)
        def inspect():
            manifest, files = inspect_directory(source)
            return manifest, sorted(files), source_digest(source)
        def complete(value):
            if Path(self.source_edit.text()).expanduser() != source:
                return
            m, files, fingerprint = value
            self._source_fingerprint = fingerprint
            self.export_preview.setPlainText(tr('ui.caption.a776d72713f5', v0=m['name'], v1=m['version'], v2=m['id'], v3=m['license']) + '\n'.join(files))
            self.export_button.setEnabled(True)
        self.run_job(inspect, complete)

    def export_source(self):
        source = Path(self.source_edit.text()).expanduser(); fingerprint = self._source_fingerprint
        filename, _ = QFileDialog.getSaveFileName(self, 'Экспорт плагина', source.name + '.zip', 'ZIP (*.zip)')
        if not filename: return
        if not filename.lower().endswith('.zip'): filename += '.zip'
        def export():
            if source_digest(source) != fingerprint:
                raise PluginError('Исходники изменились. Повторите проверку состава файлов.')
            return export_package(source, filename)
        self.run_job(export, lambda _: self.export_preview.appendPlainText(tr('ui.caption.a77733c9dfc7') + filename + tr('ui.caption.7a3d1fc370e4')))
