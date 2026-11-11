"""Авторство версий открытых файлов независимо от включения истории Git."""
from contextlib import contextmanager
from collections import OrderedDict
from threading import Lock
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from core.paths import APP_PATHS

_versions = OrderedDict()
_versions_lock = Lock()


@contextmanager
def _database():
    path = APP_PATHS.runtime / 'file_origins.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS watches(path TEXT, pid INTEGER, touched REAL, PRIMARY KEY(path,pid))')
        db.execute('CREATE TABLE IF NOT EXISTS origins(path TEXT PRIMARY KEY, oid TEXT, revision INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 0)')
        if 'busy' not in {row[1] for row in db.execute('PRAGMA table_info(origins)')}:
            db.execute('ALTER TABLE origins ADD COLUMN busy INTEGER NOT NULL DEFAULT 0')
        yield db


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def watch(path):
    try:
        with _database() as db:
            db.execute('INSERT OR REPLACE INTO watches VALUES(?,?,?)', (_key(path), os.getpid(), time.time()))
    except (OSError, sqlite3.Error):
        pass


def unwatch(path):
    try:
        with _database() as db:
            db.execute('DELETE FROM watches WHERE path=? AND pid=?', (_key(path), os.getpid()))
    except (OSError, sqlite3.Error):
        pass


def version(path):
    try:
        path = str(path)
        stamp = Path(path).stat()
        signature = (stamp.st_mtime_ns, stamp.st_ctime_ns, stamp.st_size, stamp.st_ino)
        with _versions_lock:
            cached = _versions.get(path)
            if cached and cached[0] == signature:
                _versions.move_to_end(path)
                return cached[1]
        with open(path, 'rb') as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha1(b'blob ' + str(before.st_size).encode('ascii') + b'\0')
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        current = Path(path).stat()
        if (before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino):
            return None
        if (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_ino) != (current.st_mtime_ns, current.st_ctime_ns, current.st_size, current.st_ino):
            return None
        value = digest.hexdigest()
        with _versions_lock:
            _versions[path] = ((current.st_mtime_ns, current.st_ctime_ns, current.st_size, current.st_ino), value)
            _versions.move_to_end(path)
            while len(_versions) > 512:
                _versions.popitem(last=False)
        return value
    except OSError:
        return None


def model_begin(context, paths=None):
    root = context.workspace_root.resolve()
    scopes = [(Path(path) if Path(path).is_absolute() else root / path).resolve() for path in paths] if paths is not None else None
    with _database() as db:
        # Не держим учёт давно закрытого аварийно завершившегося интерфейса.
        db.execute('DELETE FROM watches WHERE touched<?', (time.time() - 7*86400,))
        watched = db.execute('SELECT DISTINCT watches.path,COALESCE(origins.revision,0) '
                             'FROM watches LEFT JOIN origins ON origins.path=watches.path').fetchall()
    result = []
    for key, revision in watched:
        path = Path(key)
        if path.is_relative_to(root) and (scopes is None or any(path == scope or path.is_relative_to(scope) for scope in scopes)):
            result.append((key, version(path), revision))
    return result


def model_end(ticket):
    for key, before, revision in ticket or []:
        after = version(key)
        if after is None or after == before:
            continue
        with _database() as db:
            db.execute('INSERT OR IGNORE INTO origins(path,oid,revision) VALUES(?,NULL,0)', (key,))
            # Сохранение пользователя внутри операции делает авторство неоднозначным.
            db.execute('UPDATE origins SET oid=? WHERE path=? AND revision=? AND busy=0', (after, key, revision))


@contextmanager
def user_save(path):
    key = _key(path)
    try:
        with _database() as db:
            db.execute('INSERT INTO origins(path,oid,revision,busy) VALUES(?,NULL,1,1) '
                       'ON CONFLICT(path) DO UPDATE SET oid=NULL,revision=revision+1,busy=busy+1', (key,))
    except (OSError, sqlite3.Error):
        # Необязательная отметка авторства не должна запрещать сохранение файла.
        yield
        return
    try:
        yield
    finally:
        try:
            with _database() as db:
                db.execute('UPDATE origins SET oid=NULL,revision=revision+1,busy=MAX(0,busy-1) WHERE path=?', (key,))
        except (OSError, sqlite3.Error):
            pass


def origin(path, oid):
    with _database() as db:
        row = db.execute('SELECT oid FROM origins WHERE path=?', (_key(path),)).fetchone()
    return None if row is None else bool(oid and row[0] == oid)
