"""Local durable storage. Each session/knowledge base owns its SQLite file.

Connections never escape an operation or cross threads. Network/model work must
finish before entering a write transaction. Rich message/provider metadata stays
lossless JSON within individual rows; histories and documents are not monolithic
JSON blobs. All readers use one snapshot for related tables.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 1
APPLICATION_ID = 0x544F5348  # TOSH
_locks_guard = threading.Lock()
_locks = {}


def _lock(path):
    key = str(Path(path).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


_DOCUMENT_SCHEMA = """
CREATE TABLE documents (id TEXT PRIMARY KEY, position INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE pages (document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document_id,position));
CREATE TABLE chunks (document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document_id,position));
"""
_SCHEMAS = {
    'session': """
CREATE TABLE branches (name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE messages (branch TEXT NOT NULL REFERENCES branches(name) ON DELETE CASCADE ON UPDATE CASCADE,
    position INTEGER NOT NULL, payload TEXT NOT NULL, run_id TEXT, PRIMARY KEY(branch,position));
CREATE INDEX messages_run ON messages(branch,run_id);
CREATE INDEX messages_run_id ON messages(run_id);
CREATE TABLE tasks (position INTEGER PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, branch TEXT NOT NULL, status TEXT NOT NULL,
    updated_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX runs_branch ON runs(branch);
CREATE TABLE run_messages (run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, position INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(run_id,kind,position));
CREATE TABLE run_segments (run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,position));
CREATE TABLE run_tools (run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tool_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,tool_id));
""" + _DOCUMENT_SCHEMA,
    'permanent': """
CREATE TABLE facts (id TEXT PRIMARY KEY, position INTEGER NOT NULL, payload TEXT NOT NULL);
""",
    'knowledge': _DOCUMENT_SCHEMA,
}


class SQLiteStore:
    def __init__(self, path, kind='session'):
        self.path = Path(path).resolve()
        if kind not in _SCHEMAS:
            raise ValueError('Unknown database kind')
        self.kind = kind

    @contextmanager
    def connection(self, write=False, *, compact=False, create=True):
        """Serialize local writers only; SQLite also coordinates other processes."""
        guard = _lock(self.path) if write else None
        if guard:
            guard.acquire()
        db = None
        try:
            if write:
                if create:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
                else:
                    # Background statistics must not recreate a session that
                    # was renamed or deleted while a request was in flight.
                    db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True,
                                         timeout=10, isolation_level=None)
            else:
                if not self.path.is_file():
                    raise FileNotFoundError(self.path)
                db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                     timeout=10, isolation_level=None)
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=10000')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute('BEGIN')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            application = db.execute('PRAGMA application_id').fetchone()[0]
            if version == 0 and write:
                # Never claim an unrelated SQLite file as a Toshy database.
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone():
                    raise ValueError('Файл не является базой Toshy')
                # Older system builds may lack the WAL-reset fix. Rollback
                # journaling is a supported fallback, preserving concurrency
                # between the independent session files.
                v = sqlite3.sqlite_version_info
                fixed_wal = v >= (3, 51, 3) or (v[:2] == (3, 50) and v >= (3, 50, 7)) or (v[:2] == (3, 44) and v >= (3, 44, 6))
                db.execute('CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                for statement in _SCHEMAS[self.kind].split(';'):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                db.execute('INSERT INTO state VALUES (?,?)', ('kind', _dump(self.kind)))
                db.commit()
                if fixed_wal:
                    try:
                        db.execute('PRAGMA journal_mode=WAL')
                    except sqlite3.OperationalError as error:
                        # A second process can start writing immediately after
                        # schema creation. DELETE journaling remains correct.
                        if 'locked' not in str(error).lower() and 'busy' not in str(error).lower():
                            raise
                db.execute('BEGIN IMMEDIATE')
            else:
                if application != APPLICATION_ID:
                    raise ValueError('Файл не является базой Toshy')
                if version != SCHEMA_VERSION:
                    raise ValueError(f'Неподдерживаемая версия базы: {version}')
                if self._state(db, 'kind') != self.kind:
                    raise ValueError('Неверный тип базы Toshy')
            if write and self.kind == 'session':
                # Additive index for existing v1 files; no data/schema rewrite.
                db.execute('CREATE INDEX IF NOT EXISTS messages_run_id ON messages(run_id)')
            yield db
            if write:
                db.commit()
                if compact:
                    self._compact_if_needed(db, force=compact == 'force' or isinstance(compact, dict) and compact.get('force', False))
        except sqlite3.DatabaseError as error:
            if db is not None:
                db.rollback()
            raise ValueError(f'Ошибка SQLite ({self.path.name}): {error}') from error
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()
            if guard:
                guard.release()

    def _compact_if_needed(self, db, *, force=False):
        """Явное удаление уплотняется независимо от порога; потоковые записи — нет.

        Ошибка обслуживания не отменяет уже сохранённую транзакцию.
        """
        try:
            free = db.execute('PRAGMA freelist_count').fetchone()[0]
            pages = db.execute('PRAGMA page_count').fetchone()[0]
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            if not force and (free * page_size < 256 * 1024 or free * 4 < pages):
                return
            db.execute('PRAGMA busy_timeout=100')
            db.execute('VACUUM')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.DatabaseError as error:
            # A reader, another process or lack of spare disk space may defer
            # compaction. The next history/memory save will retry maintenance.
            from core.logger import core_logger
            core_logger.event('storage', 'SQLITE_COMPACTION_DEFERRED', str(error), level='WARNING',
                              details={'path': str(self.path)})

    @staticmethod
    def _state(db, key, default=None):
        row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(default)

    @staticmethod
    def _set_state(db, key, value):
        db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, _dump(value)))

    @staticmethod
    def _sequence(db, table, keys, values):
        """Update only changed rows and remove the truncated tail."""
        where = ' AND '.join(f'{key}=?' for key in keys)
        args = tuple(keys.values())
        existing = dict(db.execute(f'SELECT position,payload FROM {table}' +
                                  (f' WHERE {where}' if keys else ''), args))
        names = list(keys) + ['position', 'payload']
        if table == 'messages':
            names.append('run_id')
        for position, value in enumerate(values):
            payload = _dump(value)
            if existing.get(position) != payload:
                values_row = args + (position, payload)
                if table == 'messages':
                    identity = value.get('data', {}).get('additional_kwargs', {}).get('run_state', {}).get('run_id')
                    values_row += (identity,)
                db.execute(f'INSERT OR REPLACE INTO {table} ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
                           values_row)
        db.execute(f'DELETE FROM {table} WHERE ' + (where + ' AND ' if keys else '') + 'position>=?',
                   args + (len(values),))

    @staticmethod
    def _read_sequence(db, table, keys):
        where = ' AND '.join(f'{key}=?' for key in keys)
        rows = db.execute(f'SELECT payload FROM {table}' + (f' WHERE {where}' if keys else '') +
                          ' ORDER BY position', tuple(keys.values()))
        return [json.loads(row[0]) for row in rows]

    def exists(self):
        return self.path.is_file()

    def initialize(self):
        with self.connection(write=True):
            pass

    def has_branch(self, name='main'):
        if not self.exists():
            return False
        with self.connection() as db:
            return db.execute('SELECT 1 FROM branches WHERE name=?', (name,)).fetchone() is not None

    def _read_branch(self, db, name):
        row = db.execute('SELECT payload FROM branches WHERE name=?', (name,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload['messages'] = self._read_sequence(db, 'messages', {'branch': name})
        return payload

    def read_branch(self, name):
        if not self.exists():
            return None
        with self.connection() as db:
            return self._read_branch(db, name)

    def read_branch_header(self, name):
        if not self.exists():
            return None
        with self.connection() as db:
            row = db.execute('SELECT payload FROM branches WHERE name=?', (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def read_message_page(self, name, *, limit=10, before=None):
        limit = max(1, min(int(limit), 500))
        if not self.exists():
            return {'messages': [], 'header': None, 'before': 0, 'total': 0}
        with self.connection() as db:
            header = db.execute('SELECT payload FROM branches WHERE name=?', (name,)).fetchone()
            total = db.execute('SELECT COUNT(*) FROM messages WHERE branch=?', (name,)).fetchone()[0]
            boundary = total if before is None else max(0, min(int(before), total))
            rows = list(db.execute('SELECT position,payload FROM messages WHERE branch=? AND position<? '
                'ORDER BY position DESC LIMIT ?', (name, boundary, limit)))
            rows.reverse()
            return {'header': json.loads(header[0]) if header else None,
                'messages': [json.loads(row[1]) for row in rows],
                'before': rows[0][0] if rows else boundary, 'total': total}

    def read_timeline(self, name, namespace=''):
        """All user-message positions/IDs, without materializing message text."""
        if not self.exists():
            return []
        with self.connection() as db:
            rows = db.execute("SELECT position,json_extract(payload,'$.data.additional_kwargs.chat_id'),"
                "json_extract(payload,'$.type') FROM messages WHERE branch=? "
                "AND json_extract(payload,'$.type')='human' ORDER BY position", (name,))
            from core.chat_references import valid_chat_id, stable_chat_id
            result = []
            for pos, identity, role in rows.fetchall():
                if not valid_chat_id(identity):
                    # Legacy rows need source text only while deriving their ID;
                    # neither the response nor the client index retains it.
                    raw = db.execute("SELECT json_extract(payload,'$.data.content') FROM messages WHERE branch=? AND position=?", (name,pos)).fetchone()[0]
                    identity = stable_chat_id(f'{namespace}:{pos}:{role}:{raw or ""}')
                result.append({'position': pos, 'chat_id': identity, 'role': role})
            return result

    def read_user_messages(self, name, *, limit=None):
        """Read user metadata without materializing assistant/tool histories."""
        if not self.exists():
            return []
        with self.connection() as db:
            query = "SELECT payload FROM messages WHERE branch=? AND json_extract(payload,'$.type')='human' ORDER BY position DESC"
            result = []
            for row in db.execute(query, (name,)):
                item = json.loads(row[0])
                if limit is not None and not str(item.get('data', {}).get('content') or '').strip():
                    continue
                result.append(item)
                if limit is not None and len(result) >= limit:
                    break
            return result

    def _write_branch(self, db, name, payload):
        header = {k: v for k, v in payload.items() if k != 'messages'}
        header['branch_name'] = name
        db.execute('INSERT INTO branches VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                   (name, _dump(header), time.time()))
        self._sequence(db, 'messages', {'branch': name}, payload.get('messages') or [])

    def write_branch(self, name, payload, *, activate=False):
        maintenance = {'force': False}
        with self.connection(write=True, compact=maintenance) as db:
            old_count = db.execute('SELECT COUNT(*) FROM messages WHERE branch=?', (name,)).fetchone()[0]
            previous_runs = {row[0] for row in db.execute('SELECT DISTINCT run_id FROM messages WHERE branch=? AND run_id IS NOT NULL', (name,))}
            self._write_branch(db, name, payload)
            # New journals are created before their placeholder is inserted.
            # Ordinary saves may collect only previously referenced runs.
            current_runs = {item.get('data', {}).get('additional_kwargs', {}).get('run_state', {}).get('run_id')
                            for item in payload.get('messages') or []}
            removed = self._prune_runs(db, candidates=previous_runs - current_runs)
            maintenance['force'] = old_count > len(payload.get('messages') or []) or bool(removed)
            if maintenance['force']:
                self._prune_compactions(db)
            if activate:
                self._activate(db, name)

    @staticmethod
    def _prune_runs(db, *, candidates=None, protected=()):
        protected = set(protected)
        parents = dict(db.execute("SELECT id,json_extract(payload,'$.parent_run_id') FROM runs"))
        referenced = {row[0] for row in db.execute('SELECT DISTINCT run_id FROM messages WHERE run_id IS NOT NULL')}
        keep = referenced | protected
        # Child journals live as long as the parent appears in any branch.
        while True:
            children = {identity for identity,parent in parents.items() if parent in keep}
            if children <= keep:break
            keep.update(children)
        candidates = set(parents) if candidates is None else set(candidates)
        while True:
            children = {identity for identity,parent in parents.items() if parent in candidates}
            if children <= candidates:break
            candidates.update(children)
        removed = 0
        for identity in candidates - keep:
            removed += db.execute('DELETE FROM runs WHERE id=?', (identity,)).rowcount
            removed += db.execute('DELETE FROM state WHERE key=?', ('interrupted_notes:' + identity,)).rowcount
        if candidates is not None:
            for key, in db.execute("SELECT key FROM state WHERE key GLOB 'interrupted_notes:*'").fetchall():
                identity = key[len('interrupted_notes:'):]
                if identity not in keep and (identity in candidates or identity not in parents):
                    removed += db.execute('DELETE FROM state WHERE key=?', (key,)).rowcount
        return removed

    @staticmethod
    def _prune_compactions(db):
        # Резюме общей исходной переписки может ещё понадобиться другой ветке.
        branches = {}
        counts = {}
        for name, raw in db.execute('SELECT branch,payload FROM messages'):
            message = json.loads(raw)
            counts[name] = counts.get(name, 0) + 1
            if message.get('type') == 'human':
                identity = message.get('data', {}).get('additional_kwargs', {}).get('chat_id')
                if identity:
                    branches.setdefault(name, set()).add(identity)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='context_compactions'").fetchone():
            for identity, branch, raw in db.execute('SELECT id,branch,payload FROM context_compactions').fetchall():
                entry = json.loads(raw)
                required = set(entry.get('source_user_ids') or [])
                keep = any(required <= ids for ids in branches.values()) if required else any(counts.values())
                if not keep:
                    db.execute('DELETE FROM context_compactions WHERE id=?', (identity,))
        for key, in db.execute("SELECT key FROM state WHERE key GLOB 'compression:*'").fetchall():
            branch = key.split(':', 2)[1]
            if not counts.get(branch):
                db.execute('DELETE FROM state WHERE key=?', (key,))

    def prune_unreferenced_runs(self, *, candidates=None, protected=()):
        """Explicit history cleanup; preserve references in every branch."""
        if not self.exists():
            return 0
        maintenance = {'force': False}
        with self.connection(write=True, compact=maintenance) as db:
            removed = self._prune_runs(db, candidates=candidates, protected=protected)
            self._prune_compactions(db)
            maintenance['force'] = bool(removed)
            return removed

    def _activate(self, db, name):
        row = db.execute("SELECT payload FROM branches WHERE name='main'").fetchone()
        if row:
            header = json.loads(row[0])
            header.setdefault('metadata', {})['active_branch'] = name
            db.execute("UPDATE branches SET payload=?,updated_at=? WHERE name='main'",
                       (_dump(header), time.time()))
        else:
            self._write_branch(db, 'main', {'metadata': {'active_branch': name, 'temp_memory_mode': 'text',
                                                        'limit_ranking': True}, 'messages': []})

    def activate(self, name):
        with self.connection(write=True) as db:
            self._activate(db, name)

    def update_branch_metadata(self, name, updates):
        with self.connection(write=True) as db:
            row = db.execute('SELECT payload FROM branches WHERE name=?', (name,)).fetchone()
            if row is None:
                raise FileNotFoundError(name)
            header = json.loads(row[0])
            metadata = header.setdefault('metadata', {})
            metadata.update(updates)
            db.execute('UPDATE branches SET payload=?,updated_at=? WHERE name=?', (_dump(header), time.time(), name))
            self._activate(db, name)
            return metadata

    def branch_headers(self):
        if not self.exists():
            return []
        with self.connection() as db:
            result = []
            for name, header, updated, count in db.execute('SELECT b.name,b.payload,b.updated_at,COUNT(m.position) '
                    'FROM branches b LEFT JOIN messages m ON m.branch=b.name GROUP BY b.name ORDER BY b.name'):
                result.append((name, json.loads(header), updated, count))
            return result

    def delete_branch(self, name):
        with self.connection(write=True, compact="force") as db:
            candidates = {row[0] for row in db.execute('SELECT id FROM runs WHERE branch=?', (name,))}
            candidates.update(row[0] for row in db.execute('SELECT run_id FROM messages WHERE branch=? AND run_id IS NOT NULL', (name,)))
            db.execute('DELETE FROM branches WHERE name=?', (name,))
            db.execute('DELETE FROM state WHERE key=?', ('tasks:' + name,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_page_receipts'").fetchone():
                db.execute('DELETE FROM memory_page_receipts WHERE branch=?', (name,))
            self._prune_runs(db, candidates=candidates)
            self._prune_compactions(db)
            row = db.execute("SELECT payload FROM branches WHERE name='main'").fetchone()
            main = json.loads(row[0]) if row else None
            if main and main.get('metadata', {}).get('active_branch') == name:
                self._activate(db, 'main')

    def rename_branch(self, old, new):
        with self.connection(write=True) as db:
            original = self._read_branch(db, old)
            if original is None:
                raise FileNotFoundError(old)
            if db.execute('SELECT 1 FROM branches WHERE name=?', (new,)).fetchone():
                raise FileExistsError(new)
            db.execute('UPDATE branches SET name=? WHERE name=?', (new, old))
            db.execute('UPDATE state SET key=? WHERE key=?', ('tasks:' + new, 'tasks:' + old))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_page_receipts'").fetchone():
                db.execute('UPDATE memory_page_receipts SET branch=? WHERE branch=?', (new, old))
            for name, raw in db.execute('SELECT name,payload FROM branches').fetchall():
                data = json.loads(raw)
                if name == new:
                    data['branch_name'] = new
                if data.get('parent_branch') == old:
                    data['parent_branch'] = new
                if data.get('metadata', {}).get('active_branch') == old:
                    data['metadata']['active_branch'] = new
                db.execute('UPDATE branches SET payload=? WHERE name=?', (_dump(data), name))
            db.execute('UPDATE runs SET branch=? WHERE branch=?', (new, old))
            for run_id, raw in db.execute('SELECT id,payload FROM runs WHERE branch=?', (new,)).fetchall():
                data = json.loads(raw); data['branch'] = new
                db.execute('UPDATE runs SET payload=? WHERE id=?', (_dump(data), run_id))

    def set_branch_policy(self, key, value):
        with self.connection(write=True) as db:
            for name, raw in db.execute('SELECT name,payload FROM branches').fetchall():
                data = json.loads(raw); data.setdefault('metadata', {})[key] = value
                db.execute('UPDATE branches SET payload=?,updated_at=? WHERE name=?', (_dump(data), time.time(), name))

    def read_tasks(self, default, branch="main"):
        if not self.exists():
            return copy.deepcopy(default)
        with self.connection() as db:
            if branch != 'main':
                return self._state(db, 'tasks:' + branch, default)
            state = self._state(db, 'tasks', default)
            state['tasks'] = self._read_sequence(db, 'tasks', {})
            return state

    def write_tasks(self, state, branch="main"):
        with self.connection(write=True, compact=True) as db:
            if branch != 'main':
                if not db.execute('SELECT 1 FROM branches WHERE name=?', (branch,)).fetchone():
                    raise FileNotFoundError('Ветка не найдена: ' + branch)
                self._set_state(db, 'tasks:' + branch, state)
                return
            self._set_state(db, 'tasks', {k: v for k, v in state.items() if k != 'tasks'})
            self._sequence(db, 'tasks', {}, state.get('tasks') or [])

    def write_run(self, data, visible_message=None, *, write_history=True):
        with self.connection(write=True) as db:
            run_id = data['run_id']
            header = {k: v for k, v in data.items() if k not in {'input_messages', 'protocol_messages', 'segments', 'tools', 'model_request'}}
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'branch=excluded.branch,status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload',
                       (run_id, data.get('branch', 'main'), data.get('status', ''), data.get('updated_at', time.time()), _dump(header)))
            if write_history:
                for kind in ('input_messages', 'protocol_messages'):
                    self._sequence(db, 'run_messages', {'run_id': run_id, 'kind': kind}, data.get(kind) or [])
            if write_history and 'model_request' in data:
                self._sequence(db, 'run_messages', {'run_id': run_id, 'kind': 'model_request'}, [data['model_request']])
            self._sequence(db, 'run_segments', {'run_id': run_id}, data.get('segments') or [])
            tools = data.get('tools') or {}
            existing = dict(db.execute('SELECT tool_id,payload FROM run_tools WHERE run_id=?', (run_id,)))
            for identity, tool in tools.items():
                raw = _dump(tool)
                if existing.get(identity) != raw:
                    db.execute('INSERT OR REPLACE INTO run_tools VALUES (?,?,?)', (run_id, identity, raw))
            for identity in existing.keys() - tools.keys():
                db.execute('DELETE FROM run_tools WHERE run_id=? AND tool_id=?', (run_id, identity))
            if visible_message is not None:
                # Update an existing chat projection in the same transaction as
                # its checkpoint. Initial input/history is inserted by the API.
                branch = data.get('branch', 'main')
                row = db.execute('SELECT position FROM messages WHERE branch=? AND run_id=? ORDER BY position DESC LIMIT 1',
                                 (branch, run_id)).fetchone()
                if row:
                    db.execute('UPDATE messages SET payload=? WHERE branch=? AND position=?',
                               (_dump(visible_message), branch, row[0]))
                    db.execute('UPDATE branches SET updated_at=? WHERE name=?', (time.time(), branch))

    def read_run(self, run_id):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f'Запуск {run_id} не найден в {self.path}')
            data = json.loads(row[0])
            for kind in ('input_messages', 'protocol_messages'):
                data[kind] = self._read_sequence(db, 'run_messages', {'run_id': run_id, 'kind': kind})
            request = self._read_sequence(db, 'run_messages', {'run_id': run_id, 'kind': 'model_request'})
            if request:
                data['model_request'] = request[0]
            data['segments'] = self._read_sequence(db, 'run_segments', {'run_id': run_id})
            data['tools'] = {key: json.loads(raw) for key, raw in db.execute('SELECT tool_id,payload FROM run_tools WHERE run_id=?', (run_id,))}
            return data

    def read_run_projection(self, run_id):
        """UI/backfill data only, never use this shortened record for resume.

        The original input can contain the whole conversation and is stored
        twice in a checkpoint. Display only needs this run's protocol suffix.
        """
        with self.connection() as db:
            row = db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f'Запуск {run_id} не найден в {self.path}')
            data = json.loads(row[0])
            prefix = db.execute("SELECT COUNT(*) FROM run_messages WHERE run_id=? AND kind='input_messages'", (run_id,)).fetchone()[0]
            data['input_messages'] = []
            data['protocol_messages'] = [json.loads(raw) for raw, in db.execute(
                "SELECT payload FROM run_messages WHERE run_id=? AND kind='protocol_messages' AND position>=? ORDER BY position",
                (run_id, prefix))]
            data['segments'] = self._read_sequence(db, 'run_segments', {'run_id': run_id})
            data['tools'] = {key: {'status': status} for key, status in db.execute(
                "SELECT tool_id,json_extract(payload,'$.status') FROM run_tools WHERE run_id=?", (run_id,))}
            return data

    def rename_session(self, session_id):
        with self.connection(write=True) as db:
            for identity, raw in db.execute('SELECT id,payload FROM runs').fetchall():
                data = json.loads(raw); data['session_id'] = session_id
                db.execute('UPDATE runs SET payload=? WHERE id=?', (_dump(data), identity))

    def _read_document(self, db, identity):
        row = db.execute('SELECT payload FROM documents WHERE id=?', (identity,)).fetchone()
        if row is None:
            return None
        doc = json.loads(row[0])
        for table in ('pages', 'chunks'):
            # Empty representations still matter when switching memory modes.
            if table in doc:
                doc[table] = self._read_sequence(db, table, {'document_id': identity})
        return doc

    def _write_document(self, db, identity, doc):
        old = db.execute('SELECT position FROM documents WHERE id=?', (identity,)).fetchone()
        position = old[0] if old else db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM documents').fetchone()[0]
        header = {k: ([] if k in {'pages', 'chunks'} else v) for k, v in doc.items()}
        db.execute('INSERT INTO documents VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                   (identity, position, _dump(header)))
        for table in ('pages', 'chunks'):
            self._sequence(db, table, {'document_id': identity}, doc.get(table) or [])

    def read_documents(self, identity=None):
        if not self.exists():
            return {}
        with self.connection() as db:
            if identity is not None:
                value = self._read_document(db, identity)
                return {identity: value} if value is not None else {}
            identities = [r[0] for r in db.execute('SELECT id FROM documents ORDER BY position')]
            return {key: self._read_document(db, key) for key in identities}

    def read_document_header(self, identity):
        """Read representation metadata without loading existing text/vectors."""
        if not self.exists():
            return None
        with self.connection() as db:
            row = db.execute('SELECT payload FROM documents WHERE id=?', (identity,)).fetchone()
            if row is None:
                return None
            header = json.loads(row[0])
            header['_has_chunks'] = bool(db.execute('SELECT 1 FROM chunks WHERE document_id=? LIMIT 1', (identity,)).fetchone())
            return header

    def add_document(self, doc):
        with self.connection(write=True) as db:
            counter = int(self._state(db, 'next_document_id', 1))
            identity = f'temp_doc_{counter}'
            self._write_document(db, identity, doc)
            self._set_state(db, 'next_document_id', counter + 1)
            return identity

    def append_document(self, identity, *, pages=(), chunks=(), embedding_model=None, drop_vector=False):
        """Append only new rows, validating metadata inside the same transaction."""
        from core.embeddings import EmbeddingError
        with self.connection(write=True, compact=drop_vector) as db:
            row = db.execute('SELECT payload FROM documents WHERE id=?', (identity,)).fetchone()
            if row is None:
                return
            doc = json.loads(row[0])
            if 'pages' in doc:
                position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM pages WHERE document_id=?', (identity,)).fetchone()[0]
                additions = []
                for offset, page in enumerate(pages):
                    page = dict(page, page_num=position + offset + 1)
                    additions.append((identity, position + offset, _dump(page)))
                db.executemany('INSERT INTO pages(document_id,position,payload) VALUES (?,?,?)', additions)
            if drop_vector:
                doc.pop('chunks', None); doc.pop('embedding_model', None)
                doc['formats'] = ['text']; doc['mode'] = 'text'
                db.execute('DELETE FROM chunks WHERE document_id=?', (identity,))
            elif 'chunks' in doc:
                old_model = doc.get('embedding_model')
                first = db.execute("SELECT json_array_length(payload,'$.vector') FROM chunks WHERE document_id=? ORDER BY position LIMIT 1", (identity,)).fetchone()
                if chunks and first and old_model and old_model != embedding_model:
                    raise EmbeddingError(f"Нельзя дописать документ '{identity}': несовместимые embedding-модели.")
                dimension = first[0] if first else None
                position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM chunks WHERE document_id=?', (identity,)).fetchone()[0]
                additions = []
                for offset, chunk in enumerate(chunks):
                    if dimension is not None and len(chunk['vector']) != dimension:
                        raise EmbeddingError('Несовместимые размерности embedding-векторов.')
                    dimension = len(chunk['vector'])
                    chunk = dict(chunk, chunk_id=position + offset + 1)
                    additions.append((identity, position + offset, _dump(chunk)))
                db.executemany('INSERT INTO chunks(document_id,position,payload) VALUES (?,?,?)', additions)
                if embedding_model:
                    doc['embedding_model'] = embedding_model
            db.execute('UPDATE documents SET payload=? WHERE id=?', (_dump(doc), identity))

    def delete_document(self, identity):
        with self.connection(write=True, compact="force") as db:
            db.execute('PRAGMA secure_delete=ON')
            # Foreign keys remove pages and chunks atomically.
            return db.execute('DELETE FROM documents WHERE id=?', (identity,)).rowcount > 0

    def read_permanent(self):
        if not self.exists():
            return None, []
        with self.connection() as db:
            return self._state(db, 'embedding_model'), [json.loads(r[0]) for r in db.execute('SELECT payload FROM facts ORDER BY position')]

    def add_fact(self, text, vector, embedding_model):
        from core.embeddings import EmbeddingError
        with self.connection(write=True) as db:
            first = db.execute('SELECT payload FROM facts ORDER BY position LIMIT 1').fetchone()
            model = self._state(db, 'embedding_model')
            if first:
                if model and model != embedding_model:
                    raise EmbeddingError(f'Несовпадение embedding-моделей: {model} и {embedding_model}.')
                if len(json.loads(first[0])['vector']) != len(vector):
                    raise EmbeddingError('Несовпадение размерности embedding-векторов в постоянной памяти.')
            identity = 'fact_' + uuid.uuid4().hex
            position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM facts').fetchone()[0]
            record = {'id': identity, 'text': text, 'vector': vector, 'timestamp': datetime.now().isoformat()}
            db.execute('INSERT INTO facts VALUES (?,?,?)', (identity, position, _dump(record)))
            self._set_state(db, 'embedding_model', embedding_model)
            return identity

    def delete_fact(self, identity):
        with self.connection(write=True, compact="force") as db:
            # Erase the deleted text/vector payload instead of leaving it in
            # free SQLite pages. Large free areas are compacted after commit.
            db.execute('PRAGMA secure_delete=ON')
            return db.execute('DELETE FROM facts WHERE id=?', (identity,)).rowcount > 0

    def read_knowledge_metadata(self):
        with self.connection() as db:
            return self._state(db, 'knowledge_metadata', {})

    def read_knowledge(self):
        with self.connection() as db:
            payload = {'__toshy_kb__': self._state(db, 'knowledge_metadata', {})}
            for identity, in db.execute('SELECT id FROM documents ORDER BY position').fetchall():
                payload[identity] = self._read_document(db, identity)
            return payload

    def write_knowledge(self, payload):
        maintenance = {'force': False}
        with self.connection(write=True, compact=maintenance) as db:
            old_rows = sum(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('documents', 'pages', 'chunks'))
            self._set_state(db, 'knowledge_metadata', payload.get('__toshy_kb__') or {})
            docs = {key: value for key, value in payload.items() if key != '__toshy_kb__' and isinstance(value, dict)}
            for identity in [r[0] for r in db.execute('SELECT id FROM documents')]:
                if identity not in docs:
                    db.execute('DELETE FROM documents WHERE id=?', (identity,))
            for identity, doc in docs.items():
                self._write_document(db, identity, doc)
            new_rows = sum(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('documents', 'pages', 'chunks'))
            maintenance['force'] = new_rows < old_rows

    def backup(self, target):
        """Create a consistent portable copy, including committed WAL changes."""
        target = Path(target).resolve()
        if target == self.path:
            raise ValueError('Нельзя создать резервную копию поверх исходной базы')
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
        destination = None
        try:
            destination = sqlite3.connect(str(target))
            source.backup(destination)
            destination.execute('PRAGMA journal_mode=DELETE')
        finally:
            if destination is not None:
                destination.close()
            source.close()
