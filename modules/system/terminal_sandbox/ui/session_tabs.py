"""Konsole-style terminal tabs; UI selection never selects an agent's target."""
from ui.i18n import add_caption_action, bind_caption, add_caption_tab, tr, localize, set_text
from ui.styles.binding import apply_style
from ui.icons import symbol_button

import json
import re
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSyntaxHighlighter,QTextCharFormat,QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabBar, QStackedWidget, QToolButton, QLabel, QMenu, QStackedLayout)
from ui.widgets.dialogs import MessageBox as QMessageBox
from shiboken6 import isValid

from .console import TerminalConsole,TerminalRail
from .feed_client import FeedClient
from ..terminal_screen import TerminalScreen
from ui.tab_memory import performance_preferences
from ui.views.file_view import TabCloseButton
from ui.generation_status import generation_status

PREFIX=re.compile(r'^(user|toshy)@([^:]+):.*?\$ (.*)$')


class PromptColors(QSyntaxHighlighter):
    def highlightBlock(self,text):
        match=PREFIX.match(text)
        if not match:return
        sender=QTextCharFormat();sender.setForeground(QColor('#86efac'));self.setFormat(0,len(match[1]),sender)
        if match[2]=='local':
            local=QTextCharFormat();local.setForeground(QColor('#facc15'));self.setFormat(len(match[1])+1,5,local)


class TabConsole(TerminalConsole):
    def markers(self):
        return getattr(self,'command_markers',[])


class SessionTabBar(QTabBar):
    """Keep page identities stable when tabs move; the plus has its own row slot."""
    def __init__(self, pages, parent):
        super().__init__(parent)
        self.pages=pages
        self.currentChanged.connect(self.show_page)
        self.tabMoved.connect(lambda *_:self.show_page(self.currentIndex()))

    def widget(self,index):
        return self.tabData(index) if 0<=index<self.count() else None

    def currentWidget(self):
        return self.widget(self.currentIndex())

    def indexOf(self,pane):
        return next((i for i in range(self.count()) if self.widget(i) is pane),-1)

    def setCurrentWidget(self,pane):
        self.setCurrentIndex(self.indexOf(pane))

    def center_close_buttons(self):
        for index in range(self.count()):
            close = self.tabButton(index,QTabBar.RightSide)
            if close is not None:
                tab = self.tabRect(index)
                close.move(close.x(),tab.y()+(tab.height()-close.height())//2)

    def paintEvent(self,event):
        self.center_close_buttons()
        super().paintEvent(event)

    def show_page(self,index):
        pane=self.widget(index)
        if pane is not None:self.pages.setCurrentWidget(pane)

    def addTab(self,pane,title):
        self.pages.addWidget(pane)
        index=super().addTab(title);self.setTabData(index,pane)
        close=TabCloseButton(self)
        close.clicked.connect(lambda:self.tabCloseRequested.emit(self.indexOf(pane)) if self.indexOf(pane)>=0 else None)
        self.setTabButton(index,QTabBar.RightSide,close)
        self.show_page(self.currentIndex())
        return index

    def removeTab(self,index):
        pane=self.widget(index)
        super().removeTab(index)
        if pane is not None:self.pages.removeWidget(pane)
        self.show_page(self.currentIndex())


class SessionPane(QWidget):
    def __init__(self, owner, metadata):
        super().__init__(owner)
        self.metadata=metadata;self.cursors=None;self.sending=False
        self.screen=TerminalScreen()
        self.screen.history=deque(maxlen=performance_preferences.values['terminal']['lines'])
        self.console=TabConsole(self,performance_preferences.values['terminal']['lines'])
        self.colors=PromptColors(self.console.document())
        self.console.process_mode=True
        self.client=FeedClient(self)
        self.completion=FeedClient(self)
        layout=QHBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        self.rail=TerminalRail(self.console,self);layout.addWidget(self.console,1);layout.addWidget(self.rail)
        self.console.command_requested.connect(lambda text:owner.send(self,text))
        self.console.process_key_requested.connect(lambda key:owner.send(self,'' if key=='ctrl+c' else self.console.draft(),key))
        self.console.cancel_requested.connect(lambda:owner.send(self,'','ctrl+c'))
        self.console.completion_requested.connect(lambda line,pos:owner.complete(self,line,pos))

    def apply(self, records):
        for record in records:
            root=record['job']['id']
            if self.cursors is None:self.cursors={}
            if record['offset']<=self.cursors.get(root,0):continue
            self.screen.feed(record['text']);self.cursors[root]=record['offset']
        tab=self.metadata
        job={'id':tab['id'],'source':'manual' if tab['owner']=='user' else 'agent','command':'',
             'terminal_shell':True,'tty':True,'cwd':tab.get('cwd',''),'backend':tab.get('backend','docker'),
             'target':tab.get('target','docker'),'status':'running'}
        self.console.apply_records([{'job':job,'text':'','offset':0,'size':0,'screen':{'text':self.screen.transcript()}}])
        markers=[];block=self.console.document().firstBlock();history=[]
        while block.isValid() and block.blockNumber()<self.console.prompt.blockNumber():
            match=PREFIX.match(block.text())
            if match:
                markers.append((block.blockNumber(),match[3]))
                if match[1]=='user':history.append(match[3])
            block=block.next()
        self.console.command_markers=markers;self.console.markers_changed.emit()
        if history:self.console.history=history[-500:];self.console.history_index=len(self.console.history)


class SessionTabs(QWidget):
    def __init__(self, controller):
        super().__init__(controller)
        self.controller=controller;self.profile_id=None;self.panes={};self.profile=None
        self.revision=None;self.generation=0;self.selected={};self.backlog=False
        try:self.selected=json.loads(str(controller.ui_preferences.value("selected_tabs","{}")))
        except (ValueError,TypeError):self.selected={}
        if not isinstance(self.selected,dict):self.selected={}
        self.initialized_profiles=set()
        self.seen_tabs=None
        self.feed=FeedClient(self);self.operation=FeedClient(self)
        self.agent_busy=False
        generation_status.changed.connect(self.refresh_input)
        outer=QStackedLayout(self);outer.setContentsMargins(0,0,0,0)
        self.content=QWidget(self);outer.addWidget(self.content)
        layout=QVBoxLayout(self.content);layout.setContentsMargins(0,0,0,0)
        self.unavailable=QWidget(self);self.unavailable.setObjectName('TerminalUnavailable')
        apply_style(self.unavailable, 'QWidget#TerminalUnavailable {background:@background.window@;border-radius:5px;} QLabel {color:@text.secondary@;background:transparent;}')
        blocked=QVBoxLayout(self.unavailable);blocked.setContentsMargins(18,18,18,18)
        self.unavailable_label=QLabel(self.unavailable);self.unavailable_label.setAlignment(Qt.AlignCenter);self.unavailable_label.setWordWrap(True)
        blocked.addWidget(self.unavailable_label,1);outer.addWidget(self.unavailable)
        self.pages=QStackedWidget(self)
        self.tabs=SessionTabBar(self.pages,self);self.tabs.setMovable(True)
        self.tabs.setObjectName('TerminalSessions')
        apply_style(self.tabs, 'QTabBar#TerminalSessions::tab {\n            background:@background.window@;color:@text.secondary@;min-width:60px;max-width:220px;\n            padding:5px 8px;margin-right:2px;border:1px solid @border.subtle@;\n            border-top-left-radius:3px;border-top-right-radius:3px;}\n            QTabBar#TerminalSessions::tab:selected {background:@control.selected@;color:@text.primary@;border-color:@border.subtle@;}\n            QTabBar#TerminalSessions::tab:hover:!selected {background:@control.hover@;color:@text.primary@;}')
        self.tabs.setExpanding(False);self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setDocumentMode(True);self.tabs.setUsesScrollButtons(True)
        self.tabs.currentChanged.connect(self.current_changed);self.tabs.tabCloseRequested.connect(self.close_tab)
        header=QHBoxLayout();header.setContentsMargins(0,0,0,0)
        header.setSpacing(4);header.addWidget(self.tabs,1)
        self.new_tab_button=QToolButton(self);symbol_button(self.new_tab_button, '+')
        self.new_tab_button.setObjectName("TerminalToolbarButton")
        self.new_tab_button.setFixedSize(28,28)
        apply_style(self.new_tab_button, 'QToolButton {min-width:26px;max-width:26px;min-height:26px;max-height:26px;padding:0;border:1px solid @border.subtle@;border-radius:6px;background:@background.surface@;} QToolButton:hover {background:@control.hover@;border-color:@accent.primary@;}')
        set_text(self.new_tab_button, 'ui.session_tabs.open_another_session_in_the_selected_3b22', setter='setToolTip')
        self.new_tab_button.clicked.connect(self.open_tab);header.addWidget(self.new_tab_button)
        layout.addLayout(header)
        self.empty=localize(QLabel('',self), 'ui.session_tabs.the_first_tab_will_appear_when_cb8f')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.pages,1);layout.addWidget(self.empty,1)
        self.pages.hide()
        self.tabs.hide()
        self.placeholder=TerminalConsole(self);self.placeholder.hide()
        controller.console=self.placeholder;controller.output=self.placeholder
        self.update_availability()

    def update_availability(self):
        reason=self.controller.availability_message()
        bind_caption(self.unavailable_label, reason)
        self.layout().setCurrentWidget(self.unavailable if reason else self.content)
        if reason:
            self.feed.cancel()
            for pane in self.panes.values():pane.completion.cancel()
        self.refresh_input()

    def pane(self):
        current=self.tabs.currentWidget()
        return current if isinstance(current,SessionPane) else None

    def current_changed(self,*args):
        pane=self.pane()
        if pane:
            self.selected[self.profile_id]=pane.metadata['id']
            self.controller.ui_preferences.setValue('selected_tabs',json.dumps(self.selected))
        self.controller.console=pane.console if pane else self.placeholder
        self.controller.output=self.controller.console
        self.generation+=1;self.revision=None;self.feed.cancel()
        self.refresh_input()
        QTimer.singleShot(0,self.poll)

    def set_profile(self, profile):
        changed=self.profile_id!=profile['id']
        self.profile=profile;self.profile_id=profile['id']
        if changed:
            self.seen_tabs=None
            self.generation+=1;self.feed.cancel();self.revision=None
            self.tabs.blockSignals(True)
            while self.tabs.count():self.tabs.removeTab(0)
            self.tabs.blockSignals(False)
        self.update_availability()
        if self.controller.terminal_ready():self.poll()
        self.refresh_input()

    def sync(self, metadata, *, follow_new=False):
        selected=(self.pane().metadata['id'] if self.pane() else self.selected.get(self.profile_id))
        known={tab['id'] for tab in metadata}
        if follow_new and self.seen_tabs is not None:
            created=[tab for tab in metadata if tab['id'] not in self.seen_tabs and tab['owner']=='agent']
            if created:selected=max(created,key=lambda tab:tab.get('created',0))['id']
        self.seen_tabs=known
        self.tabs.blockSignals(True)
        for index in reversed(range(self.tabs.count())):
            pane=self.tabs.widget(index)
            if pane.metadata['id'] not in known:
                self.tabs.removeTab(index);self.panes.pop(pane.metadata['id'],None)
                pane.client.cancel();pane.completion.cancel();pane.deleteLater()
        for tab in metadata:
            tab=dict(tab,backend=self.profile.get('backend','docker'),target=self.profile.get('ssh_host') or self.profile.get('backend','docker'))
            pane=self.panes.get(tab['id'])
            if pane is None:pane=SessionPane(self,tab);self.panes[tab['id']]=pane
            pane.metadata=tab
            index=self.tabs.indexOf(pane)
            if index<0:index=add_caption_tab(self.tabs, pane, '')
            sender='user' if tab['owner']=='user' else 'toshy'
            bind_caption(self.tabs, sender+': '+Path(tab.get('title') or tab.get('program') or 'shell').name, setter='setTabText', index=index)
            bind_caption(self.tabs, tr('ui.caption.549ef77cc92d')+(tab.get('chat_session_id') or tr('ui.caption.f7e9486dd210'))+tr('ui.caption.c8f5cef77bac')+tab.get('cwd','')+'\nID: '+tab['id'], setter='setTabToolTip', index=index)
            pane.console.set_directory(tab.get('cwd',''));pane.console.set_environment(tab['target'],tab['backend'])
            pane.console.process_mode=tab['state']=='busy'
            if tab['id']==selected:self.tabs.setCurrentIndex(index)
        self.tabs.blockSignals(False)
        self.empty.setVisible(not self.tabs.count());self.tabs.setVisible(bool(self.tabs.count()))
        self.pages.setVisible(bool(self.tabs.count()))
        self.tabs.show_page(self.tabs.currentIndex())
        if not self.tabs.count():
            bind_caption(self.empty, tr('ui.caption.c10faa0da62f') if self.profile_id in self.initialized_profiles
                               else tr('ui.session_tabs.the_first_tab_will_appear_when_cb8f'))
        pane=self.pane()
        if pane:
            self.selected[self.profile_id]=pane.metadata['id']
            self.controller.ui_preferences.setValue('selected_tabs',json.dumps(self.selected))
        self.controller.console=pane.console if pane else self.placeholder;self.controller.output=self.controller.console
        self.refresh_input()

    def generation_busy(self):
        return self.agent_busy or bool(self.profile and self.profile.get('agent_busy')) or any(
            state=='running' for state,_ in generation_status.states.values())

    def refresh_input(self,*args):
        agent_busy=self.generation_busy()
        for pane in self.panes.values():
            state=pane.metadata['state']
            available=bool(self.controller.terminal_ready() and self.profile and self.profile.get('active'))
            enabled=available and not agent_busy and not pane.sending and state in {'ready','stopped'}
            pane.console.allow_interrupt=bool(available and not agent_busy and not pane.sending and state=='busy')
            pane.console.set_input_enabled(enabled,tr('ui.caption.cb11784b974f') if agent_busy else '')
            pane.console.set_prompt_visible(self.controller.terminal_ready() and state in {'ready','stopped'} and not pane.sending)
            if not enabled:pane.completion.cancel()

    def poll(self):
        if not isValid(self) or not self.controller.terminal_ready() or not self.profile_id or self.feed.reply is not None:return
        pane=self.pane();identity=pane.metadata['id'] if pane else None
        generation=self.generation;profile_id=self.profile_id
        body={'profile_id':profile_id,'terminal_session_id':identity,'cursors':pane.cursors if pane else None,
              'revision':None if self.backlog else self.revision,'wait':0 if self.revision is None or self.backlog else 20}
        if profile_id not in self.initialized_profiles:
            body.update(self.controller.scope(),ensure_default=True)
        def done(result):
            if not isValid(self) or generation!=self.generation or profile_id!=self.profile_id:return
            self.initialized_profiles.add(profile_id)
            self.agent_busy=bool(result.get('agent_busy',False))
            self.revision=result['revision'];self.sync(result['tabs'],follow_new=True)
            target=self.panes.get(identity)
            if target:
                if result.get('reset'):
                    target.screen=TerminalScreen();target.screen.history=deque(maxlen=performance_preferences.values['terminal']['lines'])
                    target.cursors={};target.console.reset_feed()
                target.apply(result.get('records',[]))
                target.cursors=dict(result.get('cursors',target.cursors or {}))
            self.backlog=bool(result.get('backlog'))
            self.controller.show_approvals(result.get('pending',[]))
            # The first response may discover a tab which was not selected yet.
            if (self.pane().metadata['id'] if self.pane() else None)!=identity:self.revision=None
            QTimer.singleShot(0,self.poll)
        def failed(error):
            if isValid(self):self.controller.notice(str(error));QTimer.singleShot(1500,self.poll)
        self.feed.post(body,done,failed,path='terminal-feed')

    def open_tab(self):
        if not self.controller.terminal_ready():return
        profile_id=self.profile_id
        def done(tab):
            if profile_id!=self.profile_id:return
            self.selected[profile_id]=tab['id']
            metadata=[pane.metadata for pane in self.panes.values() if pane.metadata['profile_id']==profile_id]+[tab]
            self.sync(metadata)
            self.tabs.setCurrentWidget(self.panes[tab['id']])
            self.current_changed()
        self.operation.post({'profile_id':profile_id,**self.controller.scope()},done,
                            lambda error:self.controller.notice(str(error)),path='terminal-open')

    def close_tab(self,index):
        pane=self.tabs.widget(index);identity=pane.metadata['id']
        if pane.metadata['state'] in {'busy','starting'}:
            if QMessageBox.question(self,tr('ui.session_tabs.close_tab_adea'),tr('ui.session_tabs.stop_the_program_and_close_this_6d51'),
                 QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)!=QMessageBox.Yes:return
        if not self.controller.terminal_ready():return
        pane.client.cancel()
        def done(result):
            self.revision=None;self.feed.cancel();self.poll()
        self.operation.post({'profile_id':self.profile_id,'terminal_session_id':identity},done,
                            lambda error:self.controller.notice(str(error)),path='terminal-close')

    def send(self,pane,text,key='enter'):
        if pane.sending or not self.controller.terminal_ready():return
        if self.generation_busy():return
        if pane.metadata['state'] not in {'ready','stopped'} and (key!='ctrl+c' or text):return
        identity=pane.metadata['id'];profile_id=pane.metadata['profile_id']
        command=key=='enter' and pane.metadata['state'] in {'ready','stopped'}
        if command and not text.strip():return
        body={'profile_id':profile_id,'terminal_session_id':identity}
        if command:body['command']=text
        else:body.update(text=text,key=key)
        pane.sending=True;self.refresh_input()
        def done(result):
            if not isValid(pane):return
            if result.get('terminal'):
                pane.metadata.update(result['terminal'])
                pane.console.set_directory(pane.metadata.get('cwd',''))
            pane.sending=False;pane.console.set_draft('')
            if command:pane.console.remember(text)
            self.revision=None;self.feed.cancel();self.refresh_input();self.poll()
        def failed(error):
            if isValid(pane):pane.sending=False;self.refresh_input()
            self.controller.notice(str(error))
        pane.client.post(body,done,failed,path='terminal-command' if command else 'terminal-input',timeout=1860000)

    def complete(self,pane,line,position):
        if pane.sending or not self.controller.terminal_ready() or self.generation_busy() or pane.console.isReadOnly():return
        body={'profile_id':pane.metadata['profile_id'],'terminal_session_id':pane.metadata['id'],'line':line,'position':position}
        def done(result):
            if not isValid(pane) or pane.console.isReadOnly() or pane.console.draft()!=line:return
            def insert(value):
                if isValid(pane) and not pane.console.isReadOnly() and pane.console.draft()==line:
                    start,length=result['start'],result['length']
                    pane.console.set_draft(line[:start]+value+line[start+length:])
            items=result.get('items',[])
            if len(items)==1:insert(items[0])
            elif items:
                menu=QMenu(pane)
                for value in items:add_caption_action(menu, 'addAction', value, lambda checked=False,value=value:insert(value))
                menu.popup(pane.console.viewport().mapToGlobal(pane.console.cursorRect().bottomLeft()))
                pane.completion_menu=menu
        pane.completion.post(body,done,lambda error:self.controller.notice(str(error)),path='terminal-complete',timeout=1860000)

    def preferences_changed(self):
        limit=performance_preferences.values['terminal']['lines']
        for pane in self.panes.values():
            pane.console.set_line_limit(limit);pane.screen.history=deque(pane.screen.history,maxlen=limit)
