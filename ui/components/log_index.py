"""Worker-owned disposable index of offsets; only one page reaches Qt."""
import json
import hashlib
from datetime import datetime
import math
import sqlite3
import tempfile
from threading import Event
from pathlib import Path

from core.log_events import CATEGORIES

PAGE_SIZE = 100
SCAN_BYTES = 256 * 1024


class LogIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.cancel = Event()
        self.streams = {}
        self.connection = None
        self.temporary = None
        self.version = 0
        self.cached_key = None
        self.cached_result = None

    def _open(self):
        if self.connection is not None: return
        self.temporary = tempfile.TemporaryDirectory(prefix='toshy-log-index-')
        self.connection = sqlite3.connect(str(Path(self.temporary.name) / 'offsets.sqlite'))
        self.connection.executescript('''
            CREATE TABLE events (id TEXT PRIMARY KEY, ts REAL, category TEXT,
                path TEXT, offset INTEGER, length INTEGER);
            CREATE INDEX events_time ON events(ts, id);
            CREATE INDEX events_category_time ON events(category, ts, id);
            CREATE INDEX events_path ON events(path);
        ''')

    @staticmethod
    def visible_event(event):
        return isinstance(event, dict)

    @staticmethod
    def decode(line, path, offset, timestamp=0):
        try:return json.loads(line)
        except (ValueError, UnicodeDecodeError):
            message = line.decode('utf-8', errors='replace').rstrip() if isinstance(line, bytes) else str(line).rstrip()
            if not message:return None
            identity = hashlib.sha256((str(path)+':'+str(offset)+':'+message).encode()).hexdigest()
            return {'id':'raw-'+identity,'ts':timestamp,'time':datetime.fromtimestamp(timestamp).isoformat(timespec='milliseconds'),
                    'category':'app','level':'ERROR' if any(w in message for w in ('Fatal Python error','Aborted','corrupted','Traceback')) else 'INFO',
                    'tag':'RAW_OUTPUT','message':message,'details':{'raw':True}}

    def scan(self):
        self._open()
        paths = ({self.directory} if self.directory.suffix == '.log' else
                 set(self.directory.glob('app-session-*.log')) | set(self.directory.glob('toshy-log-*.jsonl')))
        changed = False
        pending = False
        for missing in set(self.streams) - paths:
            self.connection.execute('DELETE FROM events WHERE path=?', (str(missing),))
            del self.streams[missing]
            changed = True
        for path in sorted(paths):
            if self.cancel.is_set():
                break
            try:
                stat = path.stat()
                with path.open('rb') as handle:
                    head = handle.readline(128)
                    state = self.streams.get(path)
                    if state is None or state['head'] != head or stat.st_size < state['cursor'] or state['inode'] != stat.st_ino:
                        self.connection.execute('DELETE FROM events WHERE path=?', (str(path),))
                        state = {'head':head, 'cursor':0, 'tail':b'', 'inode':stat.st_ino}
                        self.streams[path] = state
                        changed = True
                    handle.seek(state['cursor'])
                    chunk = handle.read(SCAN_BYTES)
                    offset = state['cursor'] - len(state['tail'])
                    state['cursor'] = handle.tell()
                pending |= state['cursor'] < stat.st_size
                lines = (state['tail']+chunk).split(b'\n')
                state['tail'] = lines.pop()
                rows = []
                for line in lines:
                    if self.cancel.is_set():
                        break
                    length = len(line)+1
                    try:
                        event = self.decode(line, path, offset, stat.st_mtime)
                        if isinstance(event, dict) and event.get('id') and self.visible_event(event):
                            timestamp = float(event.get('ts', 0))
                            if not math.isfinite(timestamp): timestamp = 0
                            category = event.get('category', 'app')
                            if category not in CATEGORIES: category = 'app'
                            rows.append((str(event['id']), timestamp, category, str(path), offset, length))
                    except (ValueError, UnicodeDecodeError, TypeError):
                        pass
                    offset += length
                if rows:
                    before = self.connection.total_changes
                    self.connection.executemany('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)', rows)
                    changed |= self.connection.total_changes != before
            except OSError:
                continue
        self.connection.commit()
        if changed: self.version += 1
        return pending

    def page(self, categories, number=1, follow=False, size=PAGE_SIZE):
        pending = self.scan()
        categories = tuple(sorted(set(categories) & set(CATEGORIES)))
        key = (self.version, categories, number, follow, size)
        if key == self.cached_key: return {**self.cached_result, 'pending':pending}
        where = 'category IN (' + ','.join('?' for _ in categories) + ')' if categories else '0'
        total = self.connection.execute('SELECT count(*) FROM events WHERE '+where, categories).fetchone()[0]
        pages = max(1, (total+size-1)//size)
        number = pages if follow else max(1, min(number, pages))
        rows = self.connection.execute('SELECT id,path,offset,length,ts FROM events WHERE '+where+
            ' ORDER BY ts,id LIMIT ? OFFSET ?', (*categories, size, (number-1)*size)).fetchall()
        events = []
        handles = {}
        try:
            for identity, path, start, length, timestamp in rows:
                try:
                    if path not in handles: handles[path] = open(path, 'rb')
                    handles[path].seek(start)
                    event = self.decode(handles[path].read(length), path, start, timestamp)
                    if isinstance(event, dict) and str(event.get('id')) == identity and self.visible_event(event): events.append(event)
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
        finally:
            for handle in handles.values(): handle.close()
        result = {'events':events, 'total':total, 'pages':pages, 'page':number,
                  'pending':pending, 'version':self.version}
        self.cached_key, self.cached_result = key, result
        return result

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
