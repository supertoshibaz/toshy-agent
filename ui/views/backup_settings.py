"""Data archive settings and idle-time scheduling in the desktop UI."""
from ui.icons import set_icon
from ui.i18n import bind_caption, add_caption_item, tr, localize, set_text

import time
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QLabel,QLineEdit,QHBoxLayout,
    QPushButton,QSpinBox,QCheckBox,QFileDialog,QListWidget)
from ui.views.startup_view import SettingsPage,SettingRow
from ui.core_bridge import SessionApiClient,core_process
from ui.core_bridge.async_runner import AsyncRunner


class BackupSettings(SettingsPage):
    def __init__(self,parent=None):
        super().__init__(tr('nav.backups'),tr('ui.caption.906d565b705c'),('App','Workspaces','Projects','Backups'),parent)
        self.client=SessionApiClient(base_url=core_process.base_url)
        self.runner=AsyncRunner(self,max_threads=1);self.busy=False;self.value={};self._loaded=False
        self.directory=QLineEdit(self)
        chooser=QWidget(self);row=QHBoxLayout(chooser);row.setContentsMargins(0,0,0,0)
        row.addWidget(self.directory);browse=set_icon(QPushButton('',chooser), 'more', square=True);row.addWidget(browse)
        browse.clicked.connect(self._browse)
        self.add_row(SettingRow(tr('ui.backup_settings.backup_folder_7267'),'',chooser,parent=self))
        self.hours=QSpinBox(self);self.hours.setRange(0,720);bind_caption(self.hours, tr('ui.caption.e34d8851244f'), setter='setSuffix');bind_caption(self.hours, tr('ui.caption.20494f41aba3'), setter='setSpecialValueText')
        self.add_row(SettingRow(tr('ui.backup_settings.automatic_backup_interval_0062'),tr('ui.backup_settings.while_toshy_is_running_active_runs_aacd'),self.hours,parent=self))
        self.keep=QSpinBox(self);self.keep.setRange(1,1000);self.keep.setValue(10)
        self.add_row(SettingRow(tr('ui.backup_settings.recent_archives_to_keep_58e4'),'',self.keep,parent=self))
        self.history=localize(QCheckBox('',self), 'ui.backup_settings.include_internal_file_history_13c4');self.history.setChecked(True)
        self.add_row(SettingRow(tr('ui.backup_settings.history_1417'),'',self.history,parent=self))
        controls=QWidget(self);buttons=QHBoxLayout(controls);buttons.setContentsMargins(0,0,0,0)
        save=localize(QPushButton('',controls), 'ui.widget.save_settings_d325');save.clicked.connect(self._save);buttons.addWidget(save)
        create=localize(QPushButton('',controls), 'ui.backup_settings.create_backup_now_bfe7');create.clicked.connect(self._create);buttons.addWidget(create)
        refresh=localize(QPushButton('',controls), 'ui.plugin_library.refresh_list_163a');refresh.clicked.connect(self.reload);buttons.addWidget(refresh)
        self.add_row(SettingRow(tr('ui.backup_settings.actions_cad1'),'',controls,parent=self))
        self.list=QListWidget(self);self.list.setMinimumHeight(120)
        self.add_row(SettingRow(tr('ui.backup_settings.available_backups_c8bb'),'',self.list,parent=self))
        self.note=QLabel('',self);self.note.setWordWrap(True);self.note.setTextFormat(Qt.PlainText)
        self.add_row(SettingRow(tr('ui.backup_settings.status_ca28'),'',self.note,parent=self))
        help_text=localize(QLabel('',self), 'ui.backup_settings.restore_from_the_toshy_directory_while_c856')
        help_text.setWordWrap(True);self.add_row(SettingRow(tr('ui.backup_settings.restore_8442'),'',help_text,parent=self))
        self.timer=QTimer(self);self.timer.setInterval(60000);self.timer.timeout.connect(self._tick);self.timer.start()
        core_process.status_changed.connect(lambda online,_:self.reload() if online and not self._loaded else None)
        QTimer.singleShot(0,self.reload)

    def _request(self,action,body=None):
        try:return self.client._request('POST','/v1/backups/'+action,json=body or {},timeout=1800)
        except Exception as error:
            response=getattr(error,'response',None)
            if response is not None:
                try:message=response.json().get('detail')
                except ValueError:message=None
                if message:raise ValueError(message) from error
            raise

    def showEvent(self,event):
        super().showEvent(event)
        if not self._loaded:self.reload()

    def _browse(self):
        directory=QFileDialog.getExistingDirectory(self,'Папка резервных копий',self.directory.text())
        if directory:bind_caption(self.directory, directory)

    def _run(self,fn,done,freeze=False):
        if self.busy:return
        self.busy=True;window=self.window()
        if freeze:window.setEnabled(False)
        def finish(value,error=None):
            self.busy=False
            if freeze:window.setEnabled(True)
            if error:bind_caption(self.note, str(error))
            else:done(value)
        self.runner.submit(fn,lambda value:finish(value),lambda error:finish(None,error))

    def reload(self):
        if self.busy or not core_process.is_running():return
        def loaded(value):
            self.value=value['settings'];self._loaded=True
            bind_caption(self.directory, self.value['directory']);self.hours.setValue(self.value['interval_hours'])
            self.keep.setValue(self.value['keep']);self.history.setChecked(self.value['include_history'])
            self.list.clear()
            for item in value['items']:add_caption_item(self.list, item['path']+tr('ui.caption.e6a41a6c09cb', v0=item['size']//1024))
        self._run(lambda:self._request('status'),loaded)

    def _save(self):
        body=dict(directory=self.directory.text(),interval_hours=self.hours.value(),keep=self.keep.value(),include_history=self.history.isChecked())
        def saved(value):self.value=value;set_text(self.note, 'ui.backup_settings.settings_saved_db02')
        self._run(lambda:self._request('settings',body),saved)

    def _idle(self):
        window=self.window()
        from ui.generation_status import generation_status
        if any(state[0]=='running' for state in generation_status.states.values()):return False
        for name,attribute in (('file_view','open_docs'),('board_view','open_boards')):
            documents=getattr(getattr(window,name,None),attribute,{})
            if any(getattr(doc,'is_dirty',False) for doc in documents.values()):return False
        for doc in getattr(getattr(window,'paint_view',None),'documents',[]):
            if doc.dirty or doc.busy:return False
        return True

    def _create(self,*_,automatic=False):
        if self.busy:return
        from PySide6.QtWidgets import QApplication
        if not self.window().isEnabled() or automatic and QApplication.activeModalWidget() is not None:return
        if not self._idle():
            if not automatic:set_text(self.note, 'ui.backup_settings.finish_generations_and_save_documents_before_a891')
            return
        set_text(self.note, 'ui.backup_settings.creating_and_verifying_archive_a3dc')
        def done(result):
            self.value['last_success']=time.time();self.reload();bind_caption(self.note, tr('ui.caption.edab3f735581')+result['path'])
        body=dict(directory=self.directory.text(),interval_hours=self.hours.value(),keep=self.keep.value(),include_history=self.history.isChecked())
        def create():
            if not automatic:self._request('settings',body)
            return self._request('create')
        self._run(create,done,freeze=True)

    def _tick(self):
        if not self._loaded:self.reload();return
        interval=self.value.get('interval_hours',0)*3600
        if interval and time.time()-self.value.get('last_success',0)>=interval:self._create(automatic=True)
