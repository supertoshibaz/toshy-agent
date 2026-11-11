"""Find across saved chat history and live messages, with paged navigation."""
import re
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QTextDocument
from PySide6.QtWidgets import QLabel, QTextEdit, QPlainTextEdit
from shiboken6 import isValid
from core.chat_references import CHAT_ID
from ui.core_bridge.async_runner import AsyncRunner
from ui.widgets.search_popup import SearchReplacePopup


def saved_cards(message):
    """Index displayed cards, including stopped runs, without model projection."""
    data = message.additional_kwargs or {}
    if message.type == 'human':
        text = data.get('canvas_user_text', data.get('file_user_text', message.content))
        yield data.get(CHAT_ID), str(text or '')
        return
    if message.type != 'ai':return
    segments = data.get('run_segments')
    if segments:
        cards = {}
        for segment in segments:
            if segment.get('type') not in {'reasoning','assistant_interim','assistant_final'}:
                continue
            identity = segment.get(CHAT_ID)
            if identity:cards[identity] = cards.get(identity,'') + str(segment.get('text') or '')
        yield from cards.items()
        return
    text = str(message.content or '')
    thought = '\n'.join(re.findall(r'<think>([\s\S]*?)</think>', text))
    if thought:yield (data.get('chat_thought') or {}).get(CHAT_ID), thought
    yield data.get(CHAT_ID), re.sub(r'<think>[\s\S]*?</think>', '', text).strip()


class ChatSearch(QObject):
    def __init__(self, chat, button):
        super().__init__(chat)
        self.chat = chat
        self.popup = SearchReplacePopup(chat, replace_enabled=False)
        self.runner = AsyncRunner(self, max_threads=1)
        self.records, self.matches = [], []
        self.key, self.index, self.revision = None, -1, 0
        self.selected = None
        self.pending = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.search)
        self.jump_timer = QTimer(self)
        self.jump_timer.setInterval(80)
        self.jump_timer.timeout.connect(self._settle_jump)
        self.popup.search_changed.connect(self._schedule_search)
        self.popup.next_requested.connect(self.next)
        self.popup.closed.connect(self.clear)
        button.clicked.connect(self.open)
        self.shortcut = QShortcut(QKeySequence.Find, chat)
        self.shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.shortcut.activated.connect(self.open)

    def identity(self):
        return self.chat.current_session_id, self.chat.current_branch

    def open(self):
        if not self.chat.welcome_widget.isHidden():return
        self.popup.open_at()
        key = self.identity()
        self.key, self.records = key, []
        if key[0]:
            def load():
                messages, _, _ = self.chat.session_manager.load_session(key[0], branch=key[1])
                records = []
                for position, message in enumerate(messages):
                    records.extend((identity,text,position) for identity,text in saved_cards(message) if identity)
                return records
            def loaded(records):
                if key == self.identity() and key == self.key:
                    self.records = records
                    self.search()
            self.runner.submit(load, loaded, lambda _error: None)
        self.search()

    def _schedule_search(self, *_):
        self.clear()
        self.matches, self.index = [], -1
        self.timer.start()

    def search(self):
        if not self.popup.isVisible():return
        if self.key != self.identity():
            self.open()
            return
        query = self.popup.find_edit.text()
        records = list(self.records)
        live = {}
        for turn in self.chat.chat_references.turns():
            meta = getattr(turn, 'chat_meta', {})
            text = getattr(turn, 'raw_text', getattr(turn, 'raw_content', getattr(turn, 'total_thought_text', '')))
            if meta.get(CHAT_ID):live[meta[CHAT_ID]] = str(text)
        records = [(identity, live.pop(identity, text), position) for identity,text,position in records]
        records.extend((identity,text,-1) for identity,text in live.items())
        pattern = re.escape(query)
        if self.popup.word_btn.isChecked():pattern = r'(?<!\w)' + pattern + r'(?!\w)'
        flags = 0 if self.popup.case_btn.isChecked() else re.IGNORECASE
        self.revision += 1
        revision, key = self.revision, self.key
        def find():
            return [(identity, position, occurrence) for identity,text,position in records
                    for occurrence, _ in enumerate(re.finditer(pattern,text,flags))] if query else []
        def ready(matches):
            if revision != self.revision or key != self.identity():return
            self.matches, self.index = matches, 0 if matches else -1
            self.popup.set_status(self.index,len(matches))
            if matches:self.show_match()
        self.runner.submit(find,ready)

    def next(self, backwards=False):
        if self.matches:
            self.index = (self.index + (-1 if backwards else 1)) % len(self.matches)
            self.show_match()

    def clear(self):
        self.timer.stop()
        self.jump_timer.stop()
        if self.pending is not None:
            target = getattr(self.chat, '_history_jump_target', None)
            if isinstance(target, dict) and target.get('chat_id') == self.pending[1]:
                self.chat._history_jump_target = None
        self.pending = None
        self.revision += 1
        if self.selected is not None and isValid(self.selected):
            if isinstance(self.selected,QLabel):self.selected.setSelection(0,0)
            else:
                cursor=self.selected.textCursor();cursor.clearSelection();self.selected.setTextCursor(cursor)
        self.selected = None

    def show_match(self):
        self.clear()
        if not 0 <= self.index < len(self.matches):return
        identity,position,occurrence = self.matches[self.index]
        self.popup.set_status(self.index,len(self.matches))
        turn = self.chat.chat_references.lookup(identity)
        if turn is None and position >= 0:
            marker = {'chat_id':identity,'position':position}
            self.pending = self.identity(),identity,occurrence
            self.chat.jump_to_history_message(marker)
            self.jump_timer.start()
        elif turn is not None:self._highlight(turn,occurrence)

    def _settle_jump(self):
        if self.pending is None or self.pending[0] != self.identity():
            self.jump_timer.stop();return
        _,identity,occurrence = self.pending
        turn=self.chat.chat_references.lookup(identity)
        if turn is not None:
            self.jump_timer.stop();self.pending=None;self._highlight(turn,occurrence)
        elif not self.chat._history_fetching and self.chat._history_jump_target is None:
            self.jump_timer.stop();self.pending=None

    def _highlight(self, turn, occurrence):
        if hasattr(turn,'is_expanded') and not turn.is_expanded:turn.toggle_collapse()
        self.chat.chat_references.jump(turn,align='top')
        flags=QTextDocument.FindFlags()
        if self.popup.case_btn.isChecked():flags |= QTextDocument.FindCaseSensitively
        if self.popup.word_btn.isChecked():flags |= QTextDocument.FindWholeWords
        widgets = turn.findChildren(QLabel) + turn.findChildren(QTextEdit) + turn.findChildren(QPlainTextEdit)
        widgets.sort(key=lambda widget: widget.mapTo(turn,widget.rect().topLeft()).y())
        for widget in widgets:
            if isinstance(widget,QLabel):
                if widget.objectName()!='ChatMessageTextLabel':continue
                document=QTextDocument();document.setHtml(widget.text())
            else:
                if not widget.isReadOnly():continue
                document=widget.document()
            start=0
            while True:
                found=document.find(self.popup.find_edit.text(),start,flags)
                if found.isNull():break
                if occurrence==0:
                    if widget.objectName() == 'ChatCodeBlockBody':
                        card=widget.parentWidget()
                        card._preview_requested=False
                        card.set_folded(False)
                    if isinstance(widget,QLabel):widget.setSelection(found.selectionStart(),found.selectionEnd()-found.selectionStart())
                    else:widget.setTextCursor(found)
                    self.selected=widget
                    point=widget.mapTo(self.chat.turns_container,widget.rect().topLeft())
                    self.chat.scroll_area.verticalScrollBar().setValue(max(0,point.y()-40))
                    return
                occurrence-=1;start=found.selectionEnd()
