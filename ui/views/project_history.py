"""Project-owned history: asynchronous navigation, comparison and reviewed restore."""
from ui.styles.binding import form_field
from ui.i18n import bind_caption, set_caption_headers, add_caption_tab, tr, localize, set_text, add_item, add_row

import base64
import difflib
import itertools
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QByteArray, QBuffer, QIODevice, QSize, Signal
from PySide6.QtGui import QPixmap, QColor, QTextCharFormat, QSyntaxHighlighter, QImageReader, QFontDatabase
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QComboBox, QSplitter, QTreeWidget, QTreeWidgetItem, QPlainTextEdit, QCheckBox, QSpinBox, QDialogButtonBox, QFormLayout, QTabWidget, QScrollArea)
from ui.widgets.dialogs import ToolDialog as QDialog, Dialog as SettingsDialog
from ui.widgets.dialogs import MessageBox as QMessageBox
from ui.widgets.dialogs import InputDialog as QInputDialog

from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge import SessionApiClient,core_process
from ui.event_bus import event_bus
from shiboken6 import isValid


from ui.widgets.passive_splitter import PassiveSplitter as QSplitter

class DiffColors(QSyntaxHighlighter):
    def highlightBlock(self,text):
        color='#73c991' if text.startswith('+') else '#ef8f8f' if text.startswith('-') else '#72b7e8' if text.startswith('@@') else ''
        if color:
            fmt=QTextCharFormat();fmt.setForeground(QColor(color))
            self.setFormat(0,len(text),fmt)


class ProjectHistory(QWidget):
    rebuild_requested = Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self._retired = False
        self._repair_pending = False
        self.setObjectName('ProjectFileHistory')
        self.client=SessionApiClient(base_url=core_process.base_url)
        self.runner=AsyncRunner(self,max_threads=1)
        self.project='';self.scope='';self.run_scope='';self.focus_run='';self.focus_snapshot='';self.allowed_sessions=None;self.rows=[];self.state={};self.revision=0;self.busy=False
        self._refresh_pending=False
        layout=QVBoxLayout(self)
        self.info=localize(QLabel('',self), 'ui.project_history.select_project_04bb');self.info.setWordWrap(True)
        layout.addWidget(self.info)
        toolbar=QHBoxLayout()
        self.enable=localize(QCheckBox('',self), 'ui.project_history.file_history_aab9')
        set_text(self.enable, 'ui.project_history.automatic_file_snapshots_the_project_s_9e2d', setter='setToolTip')
        self.enable.clicked.connect(self._toggle)
        toolbar.addWidget(self.enable)
        self.refresh_btn=localize(QPushButton('',self), 'ui.project_history.refresh_603e');self.refresh_btn.clicked.connect(self.refresh)
        self.snapshot_btn=localize(QPushButton('',self), 'ui.project_history.create_checkpoint_05b3');self.snapshot_btn.clicked.connect(self._snapshot)
        set_text(self.snapshot_btn, 'ui.project_history.save_the_current_state_of_all_8946', setter='setToolTip')
        self.settings_btn=localize(QPushButton('',self), 'ui.project_history.settings_10e2');self.settings_btn.clicked.connect(self._settings)
        for button in (self.snapshot_btn,self.refresh_btn,self.settings_btn):toolbar.addWidget(button)
        toolbar.addStretch(1);layout.addLayout(toolbar)
        filters=QHBoxLayout()
        self.session_filter=QComboBox(self);add_item(self.session_filter, 'ui.project_history.all_sessions_1565', '')
        self.session_filter.currentIndexChanged.connect(self._session_changed)
        self.search=QLineEdit(self);set_text(self.search, 'ui.project_history.search_file_paths_or_change_descriptions_f5c6', setter='setPlaceholderText')
        self.search.textChanged.connect(self._filter)
        reset=localize(QPushButton('',self), 'ui.project_history.reset_filters_cd45');reset.clicked.connect(self._reset_filters)
        filters.addWidget(self.session_filter);filters.addWidget(self.search,1);filters.addWidget(reset);layout.addLayout(filters)
        splitter=self.timeline_splitter=QSplitter(Qt.Horizontal,self);layout.addWidget(splitter,1)
        self.timeline=QTreeWidget(splitter);set_caption_headers(self.timeline, [tr('ui.caption.8e08476fe331'),tr('ui.caption.19f10f69e835')])
        self.timeline.setRootIsDecorated(False);self.timeline.setMinimumWidth(230);self.timeline.setColumnWidth(0,230)
        right=self.file_panel=QWidget(splitter);right_layout=QVBoxLayout(right);right_layout.setContentsMargins(0,0,0,0)
        self.file_splitter=QSplitter(Qt.Vertical,right);right_layout.addWidget(self.file_splitter)
        self.files=QTreeWidget(self.file_splitter);set_caption_headers(self.files, [tr('nav.file'),tr('ui.caption.9f0cda93578f')]);self.files.setRootIsDecorated(False)
        self.files.setMinimumHeight(80)
        drawer=self.preview_drawer=QWidget(self.file_splitter);drawer_layout=QVBoxLayout(drawer);drawer_layout.setContentsMargins(0,0,0,0);drawer_layout.setSpacing(2)
        self.preview_toggle=localize(QPushButton('',drawer), 'ui.project_history.file_preview_65af')
        self.preview_toggle.setStyleSheet('text-align:left;');drawer_layout.addWidget(self.preview_toggle)
        self.preview_toggle.clicked.connect(lambda:self._set_preview_expanded(not self._preview_expanded))
        self.preview=QTabWidget(drawer);drawer_layout.addWidget(self.preview,1)
        self._preview_expanded=False;self._preview_height=300;self._preview_key=None;self.preview.hide()
        self.file_splitter.setChildrenCollapsible(False);self.file_splitter.setStretchFactor(0,1)
        self.file_splitter.setStretchFactor(1,1);self.file_splitter.splitterMoved.connect(self._remember_preview_height)
        self.diff=form_field(QPlainTextEdit(self));self.diff.setReadOnly(True);self.diff.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.highlighter=DiffColors(self.diff.document())
        self.old=form_field(QPlainTextEdit(self));self.old.setReadOnly(True);self.old.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.new=form_field(QPlainTextEdit(self));self.new.setReadOnly(True);self.new.setLineWrapMode(QPlainTextEdit.NoWrap)
        for editor in (self.diff,self.old,self.new):editor.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.images=QWidget(self);images_layout=QHBoxLayout(self.images)
        self.image_before=QLabel(self.images);self.image_after=QLabel(self.images)
        self.image_scrolls=[]
        for label in (self.image_before,self.image_after):
            label.setAlignment(Qt.AlignCenter)
            scroll=QScrollArea(self.images);scroll.setWidgetResizable(True);scroll.setWidget(label);images_layout.addWidget(scroll)
            self.image_scrolls.append(scroll)
        for widget,title in ((self.diff,tr('ui.caption.859b180d5ed8')),(self.old,tr('ui.caption.4a38273155aa')),(self.new,tr('ui.caption.d39f228868d1')),(self.images,tr('ui.caption.0a6258a5f30f'))):
            add_caption_tab(self.preview, widget, title)
        splitter.setSizes([320,900]);splitter.setStretchFactor(1,1)
        self.timeline.currentItemChanged.connect(self._selected)
        self.files.currentItemChanged.connect(self._file_selected)
        actions=QHBoxLayout()
        self.open_chat=localize(QPushButton('',self), 'ui.project_history.open_session_76e6');self.open_chat.clicked.connect(self._chat)
        self.undo=localize(QPushButton('',self), 'ui.project_history.undo_change_98ad');self.undo.clicked.connect(lambda:self._plan('undo'))
        self.undo_run=localize(QPushButton('',self), 'ui.project_history.undo_run_c4c7');self.undo_run.clicked.connect(lambda:self._plan('run'))
        self.restore_file=localize(QPushButton('',self), 'ui.project_history.restore_file_1374');self.restore_file.clicked.connect(lambda:self._plan('file'))
        self.restore_state=localize(QPushButton('',self), 'ui.project_history.project_state_2d01');self.restore_state.clicked.connect(lambda:self._plan('state'))
        for button in (self.open_chat,self.undo,self.undo_run,self.restore_file,self.restore_state):actions.addWidget(button)
        layout.addLayout(actions)
        self.message=QLabel('',self);self.message.setWordWrap(True);self.message.setTextFormat(Qt.PlainText);layout.addWidget(self.message)
        self.timer=QTimer(self);self.timer.setInterval(2500);self.timer.timeout.connect(self._poll);self.timer.start()
        self._actions()

    def widgets_valid(self):
        """Python-ссылка может пережить удалённый объект Qt вместе с его детьми."""
        names=('timeline_splitter','file_panel','file_splitter','preview_drawer',
               'timeline','files','preview','diff','old','new','images',
               'image_before','image_after','enable','session_filter','search',
               'preview_toggle','info','message','refresh_btn','snapshot_btn','settings_btn',
               'open_chat','undo','undo_run','restore_file','restore_state')
        return isValid(self) and all(isValid(getattr(self,name)) for name in names)

    def retire(self):
        # Запросы заканчиваются сами; удаление их QThreadPool не должно задерживать UI.
        self._retired=True
        self.revision+=1
        if isValid(self.timer):self.timer.stop()
        if isValid(self):
            self.hide()
            if not self.busy:self.deleteLater()

    def _available(self):
        if self._retired or not isValid(self):return False
        if self.widgets_valid():return True
        if not self._repair_pending:
            self._repair_pending=True
            self.timer.stop()
            self.rebuild_requested.emit()
        return False

    def _remember_preview_height(self,*_):
        sizes=self.file_splitter.sizes()
        if self._preview_expanded and len(sizes)>1:self._preview_height=max(120,sizes[1])

    def _set_preview_expanded(self,expanded):
        expanded=bool(expanded)
        if self._preview_expanded==expanded:return
        self._remember_preview_height();self._preview_expanded=expanded
        self.preview.setVisible(expanded)
        bind_caption(self.preview_toggle, ('▾' if expanded else '▸')+tr('ui.caption.ec46a28e9ca3'))
        total=max(240,sum(self.file_splitter.sizes()))
        height=min(self._preview_height,max(120,total-80)) if expanded else self.preview_toggle.sizeHint().height()+4
        self.file_splitter.setSizes([max(80,total-height),height])

    def set_project(self,project,*,folder='',sessions=None,path=''):
        if not self._available():return
        changed=(project,folder)!=(self.project,self.scope)
        self.project,self.scope=project,folder
        self.allowed_sessions=set(sessions or []) if folder and folder!=project else None
        if changed:
            self.revision+=1;self.run_scope='';self.focus_run='';self.focus_snapshot='';self.rows=[];self.state={}
            self.timeline.clear();self.files.clear();self._clear_preview();self.enable.setChecked(False)
            self._preview_key=None;self._set_preview_expanded(False)
            set_text(self.info, 'ui.project_history.loading_selected_project_s_history_335c');self._actions()
        selected=self.session_filter.currentData()
        self.session_filter.blockSignals(True);self.session_filter.clear();add_item(self.session_filter, 'ui.project_history.all_sessions_1565', '')
        for session in sessions or []:
            self.session_filter.addItem(str(session),str(session))
        self.session_filter.setCurrentIndex(max(0,self.session_filter.findData(selected)))
        self.session_filter.blockSignals(False)
        if path:bind_caption(self.search, path)
        if self.isVisible():self.refresh()

    def showEvent(self,event):
        super().showEvent(event);self.refresh()

    def _call(self,project,action,body=None):
        try:
            return self.client._request('POST',f'/v1/file-history/{project}/{action}',json=body or {},timeout=180)
        except Exception as error:
            response=getattr(error,'response',None)
            if response is not None:
                try:raise ValueError(response.json().get('detail') or str(error)) from error
                except (ValueError,TypeError) as detail:
                    if str(detail):raise
            raise

    def _submit(self,fn,done,freeze=False):
        if not self._available() or self.busy:return
        self.busy=True;self._actions()
        window=self.window()
        if freeze:window.setEnabled(False)
        revision=self.revision
        def success(value):
            if freeze and isValid(window):window.setEnabled(True)
            if self._retired:
                self.busy=False
                if isValid(self):self.deleteLater()
                return
            if not self._available():
                self.busy=False
                return
            self.busy=False;self._actions()
            if revision==self.revision:done(value)
            elif self.isVisible():self.refresh()
            if self._refresh_pending and not self.busy:
                self._refresh_pending=False
                QTimer.singleShot(0,self.refresh)
        def failed(error):
            if freeze and isValid(window):window.setEnabled(True)
            if self._retired:
                self.busy=False
                if isValid(self):self.deleteLater()
                return
            if not self._available():
                self.busy=False
                return
            self.busy=False;self._actions()
            if revision==self.revision:
                bind_caption(self.message, str(error));self.enable.setChecked(bool(self.state.get('settings',{}).get('enabled')))
            elif self.isVisible():self.refresh()
            self._refresh_pending=False
        self.runner.submit(fn,success,failed)

    def _poll(self):
        if self._available() and self.isVisible() and not self.busy:self.refresh()

    def refresh(self,*_):
        if not self._available():return
        if not self.project or not core_process.is_running():return
        if self.busy:
            self._refresh_pending=True
            return
        self._refresh_pending=False
        if getattr(self, '_poll_in_flight', False):return
        self._poll_in_flight=True
        project,revision=self.project,self.revision
        def load():
            return self._call(project,'status'),self._call(project,'list')
        def loaded(result):
            self._poll_in_flight=False
            if not self._available() or revision!=self.revision or project!=self.project:return
            if self.busy:
                self._refresh_pending=True
                return
            state,payload=result
            unchanged=self.rows==payload['items'] and self.state==state
            self.state=state;self.rows=payload['items']
            self.enable.setChecked(self.state['settings']['enabled'])
            bind_caption(self.info, self.state['root']+tr('ui.caption.c0bae2841d2d'))
            active=self.state['active']
            runs={item['run'] for item in active if not item['run'].startswith('user:')}
            bind_caption(self.message, tr('ui.caption.02f76ae41178') if not self.state['git_available'] else
                tr('ui.caption.0c01c8a287ad', v0=len(runs)) if runs else
                tr('ui.caption.5212526a8876') if active else '')
            bind_caption(self.message, '\n'.join(sorted({
                tr('ui.caption.42a25178f4b9') if item['run'].startswith('user:') else
                (tr('ui.caption.b260e58151e5')+item['session']+' · ' if item['session'] else '')+tr('ui.caption.cea1694bd7be')+item['run']
                for item in active})), setter='setToolTip')
            if not unchanged or self.focus_run or self.focus_snapshot:self._filter()
            self._actions()
        def failed(error):
            self._poll_in_flight=False
            if self._available() and revision==self.revision and project==self.project:
                self.refresh_btn.setToolTip(str(error))
        # Reading the timeline never disables buttons or impersonates a user action.
        self.runner.submit(load,loaded,failed)

    def _actions(self):
        enabled=bool(self.state.get('settings',{}).get('enabled'))
        usable=bool(self.project) and not self.busy
        point=self._point()
        self.enable.setEnabled(usable and bool(self.state))
        self.settings_btn.setEnabled(usable and bool(self.state))
        self.snapshot_btn.setEnabled(usable and enabled)
        self.refresh_btn.setEnabled(usable)
        can_restore=usable and enabled and bool(point) and not self.state.get('active')
        self.undo.setEnabled(can_restore and point.get('kind') not in {'baseline','before_run'})
        self.undo_run.setEnabled(can_restore and bool(point and point.get('run')))
        self.restore_state.setEnabled(can_restore)
        self.restore_file.setEnabled(can_restore and bool(self.files.currentItem()))
        self.open_chat.setEnabled(usable and bool(point and point.get('session')))

    def _session_changed(self,*_):
        self.run_scope=''
        self._filter()

    def _reset_filters(self):
        self.run_scope='';self.focus_run='';self.focus_snapshot='';self.session_filter.setCurrentIndex(0);self.search.clear();self._filter()

    def _filter(self,*_):
        point=self._point();previous=point.get('id') if point else ''
        self.timeline.blockSignals(True);self.timeline.clear()
        query=self.search.text().casefold().strip();session=self.session_filter.currentData()
        focused=None
        for row in self.rows:
            # Named points/external edits belong to the shared workspace and
            # remain available inside its virtual folders as well.
            if self.allowed_sessions is not None and row['session'] and row['session'] not in self.allowed_sessions:continue
            if self.run_scope and row['run']!=self.run_scope and row['parent_run']!=self.run_scope:continue
            if session and row['session']!=session:continue
            if query and query not in (row['label']+' '+' '.join(x['path'] for x in row['files'])).casefold():continue
            stamp=datetime.fromtimestamp(row['created']).strftime('%d.%m %H:%M:%S')
            text=tr('ui.caption.9728aa9b4861', v0=stamp, v1=row['label'], v2=len(row['files']))
            item=QTreeWidgetItem([text,(row['session']+' · ' if row['session'] else '')+row['agent']])
            item.setData(0,Qt.UserRole,row)
            bind_caption(item, text+'\n'+'\n'.join(row['warnings'][:30])+ (tr('ui.caption.6595ad92da20') if row['shared'] else ''), setter='setToolTip', index=0)
            bind_caption(item, (row['session']+'\n' if row['session'] else '')+row['agent'], setter='setToolTip', index=1)
            self.timeline.addTopLevelItem(item)
            if row['id']==previous:self.timeline.setCurrentItem(item)
            if (self.focus_snapshot and row['id']==self.focus_snapshot
                    or self.focus_run and row['run']==self.focus_run
                       and (focused is None or row['kind']=='run'
                            and focused.data(0,Qt.UserRole)['kind']!='run')):
                focused=item
        if focused is not None:
            self.timeline.setCurrentItem(focused);self.timeline.scrollToItem(focused)
            self.focus_snapshot='';self.focus_run=''
        if not self.timeline.currentItem() and self.timeline.topLevelItemCount():self.timeline.setCurrentItem(self.timeline.topLevelItem(0))
        self.timeline.blockSignals(False)
        if self.run_scope:set_text(self.message, 'ui.project_history.showing_changes_from_the_selected_run_3a09')
        self._selected()

    def _point(self):
        item=self.timeline.currentItem() if hasattr(self,'timeline') else None
        return item.data(0,Qt.UserRole) if item else None

    def _selected(self,*_):
        old=self.files.currentItem();path=old.text(0) if old else ''
        self.files.blockSignals(True);self.files.clear();self._clear_preview()
        point=self._point()
        if point:
            for change in point['files']:
                item=QTreeWidgetItem([change['path'],{'added':tr('ui.caption.758aa155871b'),'deleted':tr('ui.caption.b23189c0265d'),'modified':tr('ui.caption.4a215c69782c')}[change['status']]])
                self.files.addTopLevelItem(item)
                if change['path']==path:self.files.setCurrentItem(item)
            if not self.files.currentItem() and self.files.topLevelItemCount():self.files.setCurrentItem(self.files.topLevelItem(0))
            if point['warnings']:bind_caption(self.message, tr('ui.caption.ddb957108f40')+str(len(point['warnings']))+tr('ui.caption.ae9c447ce2fd'))
            if point['shared']:set_text(self.message, 'ui.project_history.shared_snapshot_of_concurrent_runs_check_e8c3')
        self.files.blockSignals(False);self._actions();self._file_selected()

    def _clear_preview(self):
        for widget in (self.diff,self.old,self.new):widget.clear()
        self.image_before.clear();self.image_after.clear()

    def _file_selected(self,*_):
        self._actions()
        point=self._point();item=self.files.currentItem()
        self.preview_toggle.setEnabled(bool(point and item))
        if not point or not item:
            self._preview_key=None;self._set_preview_expanded(False);return
        project=self.project;identity=point['id'];path=item.text(0);revision=self.revision
        key=(project,identity,path)
        if key!=self._preview_key:
            self._preview_key=key;self._set_preview_expanded(True)
        def loaded(value):
            current=self._point();selected=self.files.currentItem()
            if revision!=self.revision or not current or current['id']!=identity or not selected or selected.text(0)!=path:return
            before,after=value['_before'],value['_after']
            if value['binary']:
                self.diff.setPlainText(tr('ui.caption.b969f70a0590', v0=value['before_size'], v1=value['after_size']))
            else:
                old,new=value['_old'],value['_new']
                self.old.setPlainText(old);self.new.setPlainText(new)
                self.diff.setPlainText(value['_diff'] or tr('ui.caption.6815a2c89d07'))
            if value['before_truncated'] or value['after_truncated'] or value['_text_truncated']:
                self.diff.appendPlainText(tr('ui.caption.63816e180d52'))
            for label,data in ((self.image_before,before),(self.image_after,after)):
                label.clear()
                buffer=QBuffer();buffer.setData(QByteArray(data));buffer.open(QIODevice.ReadOnly)
                reader=QImageReader(buffer)
                if bytes(reader.format()).lower().startswith(b'svg'):
                    from ui.views.svg_preview import svg_bytes
                    try:svg_bytes(data.decode('utf-8'))
                    except (ValueError,UnicodeError):set_text(label, 'ui.project_history.svg_is_unavailable_for_safe_preview_66f5');continue
                size=reader.size()
                if size.isValid():reader.setScaledSize(size.scaled(QSize(800,800),Qt.KeepAspectRatio))
                rendered=reader.read()
                if not rendered.isNull():label.setPixmap(QPixmap.fromImage(rendered))
                else:set_text(label, 'ui.project_history.no_image_be33')
        # Previews can overlap navigation; immutable IDs discard stale results.
        def load_preview():
            value=self._call(project,'file',dict(snapshot=identity,path=path))
            value['_before'],value['_after']=(base64.b64decode(value[k]) for k in ('before','after'))
            old,new=(value[k].decode('utf-8',errors='replace') for k in ('_before','_after'))
            value['_text_truncated']=max(len(old),len(new))>250000
            value['_old'],value['_new']=old[:250000],new[:250000]
            changes=difflib.unified_diff(value['_old'].splitlines(),value['_new'].splitlines(),fromfile=tr('ui.caption.cd4527105526')+path,tofile=tr('ui.caption.4c67783d9621')+path,lineterm='')
            value['_diff']='\n'.join(itertools.islice(changes,30000)) if not value['binary'] else ''
            return value
        self.runner.submit(load_preview,loaded,
                           lambda error:bind_caption(self.message, str(error)) if revision==self.revision else None)

    def _toggle(self,checked):
        project=self.project
        self._submit(lambda:self._call(project,'settings',dict(enabled=checked)),lambda _:self.refresh())

    def _snapshot(self):
        label,ok=QInputDialog.getText(self,tr('ui.project_history.checkpoint_9946'),
            tr('ui.project_history.save_the_current_state_of_tracked_49af'))
        if ok:
            project=self.project
            def created(result):
                self._reset_filters();self.focus_snapshot=result.get('id') or '';self.refresh()
            self._submit(lambda:self._call(project,'snapshot',dict(label=label)),created)

    def _settings(self):
        settings=self.state.get('settings',{})
        dialog=SettingsDialog(self);set_text(dialog, 'ui.project_history.file_history_settings_2c89', setter='setWindowTitle')
        layout=QVBoxLayout(dialog);form=QFormLayout();layout.addLayout(form)
        size=QSpinBox(dialog);size.setRange(1,500);size.setValue(settings.get('max_file_mb',50));add_row(form, 'ui.project_history.per_file_limit_mib_644f', size)
        count=QSpinBox(dialog);count.setRange(10,5000);count.setValue(settings.get('max_snapshots',200));add_row(form, 'ui.project_history.recent_checkpoints_to_keep_de8a', count)
        excludes=form_field(QPlainTextEdit(dialog));excludes.setPlainText('\n'.join(settings.get('excludes',[])))
        add_row(form, 'ui.project_history.exclusions_one_pattern_per_line_4f13', excludes)
        note=localize(QLabel('',dialog), 'ui.project_history.base_exclusions_git_python_environments_credentials_efe4')
        note.setWordWrap(True);layout.addWidget(note)
        gc=localize(QPushButton('',dialog), 'ui.project_history.free_space_from_deleted_checkpoints_1298');layout.addWidget(gc)
        gc.clicked.connect(lambda:self._gc(dialog))
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel,dialog)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()==QDialog.Accepted:
            project=self.project
            body=dict(max_file_mb=size.value(),max_snapshots=count.value(),excludes=[x.strip() for x in excludes.toPlainText().splitlines() if x.strip()])
            self._submit(lambda:self._call(project,'settings',body),lambda _:self.refresh())

    def _gc(self,dialog):
        project=self.project;dialog.reject()
        self._submit(lambda:self._call(project,'gc'),lambda _:set_text(self.message, 'ui.project_history.cleanup_completed_0cdc'))

    def _chat(self):
        point=self._point()
        if point and point['session']:event_bus.session_selected.emit(point['session'],point['branch'] or 'main')

    def _plan(self,mode):
        point=self._point()
        if not point:return
        project=self.project;item=self.files.currentItem();path=item.text(0) if item else ''
        self._submit(lambda:self._call(project,'plan',dict(snapshot=point['id'],mode=mode,path=path)),self._review)

    def _review(self,plan):
        operations=plan['operations']
        if not operations:
            bind_caption(self.message, tr('ui.caption.82f31caf19ff')+'\n'.join(plan['warnings'][:12])
                if plan.get('warnings') else tr('ui.caption.79bb4a621c43'));return
        dialog=QDialog(self);set_text(dialog, 'ui.project_history.restore_preview_0bcf', setter='setWindowTitle');dialog.resize(700,450)
        layout=QVBoxLayout(dialog)
        note=localize(QLabel('',dialog), 'ui.project_history.files_in_the_shared_project_folder_b539')
        note.setWordWrap(True);layout.addWidget(note)
        if plan.get('warnings'):
            warning=QLabel('\n'.join(plan['warnings'][:12]),dialog)
            warning.setWordWrap(True);warning.setTextFormat(Qt.PlainText);layout.addWidget(warning)
        listing=QTreeWidget(dialog);set_caption_headers(listing, [tr('nav.file'),tr('ui.transform_settings.action_6b53'),tr('ui.caption.e56a412d1285')])
        for item in operations:
            listing.addTopLevelItem(QTreeWidgetItem([item['path'],item['action'],tr('ui.caption.3c01166f6d7a') if item['conflict'] else tr('ui.caption.ef05d57959cf')]))
        layout.addWidget(listing)
        skip=localize(QCheckBox('',dialog), 'ui.project_history.skip_conflicting_files_0c8a');skip.setChecked(True)
        skip.setVisible(any(x['conflict'] for x in operations));layout.addWidget(skip)
        buttons=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel,dialog)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject);layout.addWidget(buttons)
        if dialog.exec()!=QDialog.Accepted:return
        chosen=[x['path'] for x in operations if not(skip.isChecked() and x['conflict'])]
        if not chosen:return
        root=Path(self.state['root']);absolute=[str(root/p) for p in chosen]
        from ui.history_integration import prepare_restore,finish_restore
        if not prepare_restore(self.window(),absolute):return
        project=self.project;token=plan['token'];skip_conflicts=skip.isChecked()
        def done(result):
            finish_restore(self.window(),absolute)
            self.refresh()
            set_text(self.message, 'ui.project_history.restored_v0_skipped_v1_eb34', v0=len(result['restored']), v1=len(result['skipped']))
        self._submit(lambda:self._call(project,'restore',dict(token=token,skip_conflicts=skip_conflicts)),done,freeze=True)
