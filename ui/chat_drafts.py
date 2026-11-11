"""Branch-owned composer drafts. Stored in the session, never sent as context."""
import hashlib
import json
import os
import shutil
from copy import deepcopy
from pathlib import Path
from core.context import sanitize_session_id
from core.paths import APP_PATHS
from core.sqlite_storage import SQLiteStore


class DraftStore:
    def __init__(self, sessions=None):self.sessions = Path(sessions or APP_PATHS.sessions)
    def storage(self, session):return SQLiteStore(self.sessions / sanitize_session_id(session) / 'session.sqlite')

    def read(self, session, branch):
        store = self.storage(session)
        if not store.exists():return {}
        with store.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='composer_drafts'").fetchone():return {}
            row = db.execute('SELECT payload FROM composer_drafts WHERE branch=?',(branch,)).fetchone()
        data = json.loads(row[0]) if row else {}
        for item in data.get('attachments',[]):
            if item.get('_draft_source'):
                item['source_path'] = str(store.path.parent/'draft-assets'/Path(item.get('source_path') or '').name)
        return data

    def write(self, session, branch, data):
        store = self.storage(session)
        if not store.exists():return
        data = deepcopy(data);assets = store.path.parent/'draft-assets'
        for item in data.get('attachments',[]):
            if not item.get('_temporary_source'):continue
            source = Path(item.get('source_path') or '')
            if not source.is_file():continue
            if not source.resolve().is_relative_to(assets.resolve()):
                identity = hashlib.sha256(str(item.get('pending_id') or source).encode()).hexdigest()
                target = assets/(identity+source.suffix);assets.mkdir(parents=True,exist_ok=True)
                temporary = target.with_suffix(target.suffix+'.part')
                if not target.is_file():
                    try:
                        shutil.copyfile(source,temporary)
                        if os.name!='nt':temporary.chmod(0o600)
                        os.replace(temporary,target)
                    finally:temporary.unlink(missing_ok=True)
                item['source_path'] = str(target)
            item.pop('_temporary_source',None);item['_draft_source'] = True
        with store.connection(write=True,create=False) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS composer_drafts(
                branch TEXT PRIMARY KEY REFERENCES branches(name) ON UPDATE CASCADE ON DELETE CASCADE,
                payload TEXT NOT NULL)''')
            if not db.execute('SELECT 1 FROM branches WHERE name=?',(branch,)).fetchone():return
            db.execute('INSERT INTO composer_drafts VALUES (?,?) ON CONFLICT(branch) DO UPDATE SET payload=excluded.payload',
                       (branch,json.dumps(data,ensure_ascii=False)))
            retained={str(assets/Path(i.get('source_path') or '').name) for row in db.execute('SELECT payload FROM composer_drafts')
                      for i in json.loads(row[0]).get('attachments',[]) if i.get('_draft_source')}
        if assets.is_dir():
            for path in assets.iterdir():
                if path.is_file() and str(path) not in retained:path.unlink(missing_ok=True)


def capture(chat):
    from ui.views.quote_input import QUOTE_SEPARATOR
    editor=chat.input_edit;cursor=editor.textCursor();separators=[];block=editor.document().begin()
    while block.isValid():
        marker=block.blockFormat().property(QUOTE_SEPARATOR)
        if marker:separators.append([block.position(),marker])
        block=block.next()
    return {'text':editor.document().toPlainText(),'quotes':editor.quote_objects(),'separators':separators,
            'position':cursor.position(),'anchor':cursor.anchor(),'attachments':deepcopy(chat._pending_attachments),
            'reply':chat.chat_references.snapshot_reply(),'settings':chat.input_state.capture()}


def restore(chat,data):
    from PySide6.QtGui import QTextCursor,QTextCharFormat,QTextBlockFormat
    from ui.views.quote_input import QUOTE_TYPE,QUOTE_DATA,QUOTE_SEPARATOR
    editor=chat.input_edit;editor.setPlainText(data.get('text') or '');end=editor.document().characterCount()-1
    for quote in data.get('quotes') or []:
        position=int(quote.get('position',-1))
        if not 0<=position<end:continue
        cursor=QTextCursor(editor.document());cursor.setPosition(position);cursor.setPosition(position+1,QTextCursor.KeepAnchor)
        if cursor.selectedText()!='\ufffc':continue
        fmt=QTextCharFormat();fmt.setObjectType(QUOTE_TYPE)
        fmt.setProperty(QUOTE_DATA,json.dumps({k:v for k,v in quote.items() if k!='position'},ensure_ascii=False));cursor.setCharFormat(fmt)
    for position,marker in data.get('separators') or []:
        block=editor.document().findBlock(max(0,min(int(position),end)))
        fmt=QTextBlockFormat(block.blockFormat());fmt.setProperty(QUOTE_SEPARATOR,marker);QTextCursor(block).setBlockFormat(fmt)
    cursor=QTextCursor(editor.document());cursor.setPosition(max(0,min(int(data.get('anchor',end)),end)))
    cursor.setPosition(max(0,min(int(data.get('position',end)),end)),QTextCursor.KeepAnchor);editor.setTextCursor(cursor)
    chat._pending_attachments[:]=[dict(i) for i in data.get('attachments') or [] if Path(i.get('source_path') or '').is_file()]
    chat._refresh_pending_attachments_panel();chat.chat_references.pending=deepcopy(data.get('reply'));chat.chat_references.refresh()
