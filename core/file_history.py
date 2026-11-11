"""Private Git objects + SQLite ownership. Never invokes the user's repository."""
from __future__ import annotations

import asyncio
import errno
import fnmatch
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from functools import wraps
from contextlib import contextmanager, asynccontextmanager, closing, ExitStack

from core.paths import APP_PATHS

_EDIT_FOCUS = ('', uuid.uuid4().hex)
_EDIT_LOCK = threading.RLock()


def focus_user_file(path):
    """Смена открытого пользователем файла завершает серию объединяемых правок."""
    global _EDIT_FOCUS
    canonical = str(Path(path).expanduser().resolve()) if path else ''
    with _EDIT_LOCK:
        if canonical != _EDIT_FOCUS[0]:
            _EDIT_FOCUS = (canonical, uuid.uuid4().hex)


def _edit_token(path):
    canonical = str(Path(path).resolve())
    with _EDIT_LOCK:
        if not _EDIT_FOCUS[0]:
            focus_user_file(canonical)
        return _EDIT_FOCUS[1] if canonical == _EDIT_FOCUS[0] else uuid.uuid4().hex


log = logging.getLogger(__name__)
DEFAULTS = dict(enabled=False, max_file_mb=50, max_snapshots=200,
                excludes=['.git', '.venv', 'venv', '__pycache__', 'node_modules',
                          '.env', '.env.*', '*.pem', '*.key', '*.gguf', '*.safetensors',
                          '*.pyc', '*-wal', '*-shm', '.paint-save-*'])
_thread_lock = threading.RLock()
_scan_cache = {}
_activity_guard = threading.RLock()
_activity_handles = {}


def _activity_lock(handle, unlock=False):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _claim_activity(directory, identity):
    with _activity_guard:
        path=directory/'activity'/(identity+'.lock')
        path.parent.mkdir(parents=True,exist_ok=True)
        handle=path.open('a+b')
        try:
            handle.seek(0)
            if not handle.read(1):handle.write(b'0');handle.flush()
            _activity_lock(handle)
            _activity_handles[(str(directory),identity)]=handle
        except BaseException:
            handle.close()
            raise


def _release_activity(directory, identity):
    with _activity_guard:
        handle=_activity_handles.pop((str(directory),identity),None)
        if handle is not None:
            handle.close()  # The OS releases ownership even after process exit.
            try:(directory/'activity'/(identity+'.lock')).unlink(missing_ok=True)
            except OSError:pass


def _activity_held(directory, identity):
    if not identity or not str(identity).isalnum():return False
    with _activity_guard:
        handle=_activity_handles.get((str(directory),identity))
        if handle is not None and not handle.closed:return True
        try:handle=(directory/'activity'/(identity+'.lock')).open('r+b')
        except FileNotFoundError:return False
        with handle:
            try:_activity_lock(handle)
            except OSError as error:
                if error.errno in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:return True
                raise
            _activity_lock(handle,unlock=True)
            return False


def _process_alive(pid):
    # Legacy records lack activity ownership. Never send os.kill on Windows.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype=wintypes.BOOL
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        kernel.CloseHandle.restype=wintypes.BOOL
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()==5
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:pass
    return True


class HistoryError(ValueError):
    pass


class HistoryBusy(HistoryError):
    pass


class FileHistory:
    def __init__(self, directory=None):
        self.directory = Path(directory or APP_PATHS.runtime / 'file_history').resolve()
        self.git = shutil.which('git')

    @contextmanager
    def locked(self, database=True):
        self.directory.mkdir(parents=True, exist_ok=True)
        with _thread_lock, open(self.directory / 'history.lock', 'a+b') as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b'0'); handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise HistoryBusy('История занята другим процессом. Повторите операцию.')
                    time.sleep(.03)
            db = sqlite3.connect(self.directory / 'history.sqlite', timeout=30) if database else None
            if db is not None:db.row_factory = sqlite3.Row
            try:
                if db is not None:db.executescript('''
                    CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, root TEXT UNIQUE,
                        app_root TEXT, settings TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, workspace TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                        created REAL, label TEXT, kind TEXT, session TEXT, branch TEXT, run TEXT,
                        parent_run TEXT, agent TEXT, oid TEXT, base_oid TEXT, manifest TEXT,
                        base_manifest TEXT, warnings TEXT, shared INTEGER DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS snapshots_workspace ON snapshots(workspace,created);
                    CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, workspace TEXT, pid INTEGER,
                        run TEXT, session TEXT, family TEXT);
                    CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, workspace TEXT, created REAL, data TEXT);
                    CREATE TABLE IF NOT EXISTS user_series(snapshot TEXT PRIMARY KEY, token TEXT, path TEXT);
                    CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, workspace TEXT, kind TEXT,
                        baseline TEXT NOT NULL, scopes TEXT NOT NULL, blocked TEXT NOT NULL);
                ''')
                if db is not None and 'family' not in {r[1] for r in db.execute('PRAGMA table_info(leases)')}:
                    db.execute('ALTER TABLE leases ADD COLUMN family TEXT')
                if db is not None and 'tracked' not in {r[1] for r in db.execute('PRAGMA table_info(snapshots)')}:
                    db.execute('ALTER TABLE snapshots ADD COLUMN tracked INTEGER NOT NULL DEFAULT 0')
                yield db
                if db is not None:db.commit()
            except BaseException:
                if db is not None:db.rollback()
                raise
            finally:
                if db is not None:db.close()
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _row(self, db, identity):
        row = db.execute('SELECT * FROM workspaces WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise HistoryError('Project Folder не зарегистрирована в истории.')
        return dict(row)

    def _root(self, row):
        root = Path(row['root'])
        old_app = Path(row['app_root'])
        # Moving the whole Toshy directory also moves its managed workspace.
        if not root.exists() and root.is_relative_to(old_app):
            moved = APP_PATHS.root / root.relative_to(old_app)
            if moved.is_dir():
                return moved.resolve()
        return root

    def bind(self, project, root):
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise HistoryError(f'Project Folder не существует: {root}')
        with self.locked() as db:
            by_path = db.execute('SELECT * FROM workspaces WHERE root=?', (str(root),)).fetchone()
            previous = db.execute('SELECT w.* FROM projects p JOIN workspaces w ON w.id=p.workspace WHERE p.id=?', (project,)).fetchone()
            if by_path:
                identity = by_path['id']
            elif previous and (not Path(previous['root']).exists() or self._root(previous) == root):
                identity = previous['id']
                db.execute('UPDATE workspaces SET root=?,app_root=? WHERE id=?', (str(root), str(APP_PATHS.root), identity))
            else:
                identity = uuid.uuid4().hex
                db.execute('INSERT INTO workspaces VALUES(?,?,?,?)', (identity, str(root), str(APP_PATHS.root), json.dumps(DEFAULTS)))
            db.execute('INSERT OR REPLACE INTO projects VALUES(?,?)', (project, identity))
            return identity

    def _workspace_for_path(self, db, root):
        root = Path(root).resolve()
        for row in db.execute('SELECT * FROM workspaces'):
            if self._root(row) == root:
                return dict(row)
        return None

    def _git(self, identity, *args, data=None, index=None):
        if not self.git:
            raise HistoryError('Git не найден. Установите Git и перезапустите Toshy.')
        repo = self.directory / identity / 'store.git'
        repo.parent.mkdir(parents=True, exist_ok=True)
        # Strip inherited repository/config overrides. Never discover .git or
        # execute project filters, hooks, or global attributes.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Toshy',
                   GIT_AUTHOR_EMAIL='history@localhost', GIT_COMMITTER_NAME='Toshy',
                   GIT_COMMITTER_EMAIL='history@localhost', LC_ALL='C')
        if index:
            env['GIT_INDEX_FILE'] = str(index)
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        if not repo.exists():
            r = subprocess.run([self.git, 'init', '--bare', '--template=', str(repo)], env=env,
                               capture_output=True, creationflags=flags, timeout=30)
            if r.returncode:
                raise HistoryError(r.stderr.decode(errors='replace'))
        r = subprocess.run([self.git, '--git-dir='+str(repo), '-c', 'core.hooksPath='+os.devnull,
                            '-c', 'core.autocrlf=false', *args], input=data, env=env,
                           capture_output=True, creationflags=flags, timeout=120)
        if r.returncode:
            raise HistoryError(r.stderr.decode(errors='replace').strip() or 'Ошибка Git')
        return r.stdout

    def _excluded(self, relative, settings):
        parts = PurePosixPath(relative).parts
        for pattern in DEFAULTS['excludes'] + settings.get('excludes', []):
            pattern = str(pattern).strip().replace('\\', '/').rstrip('/')
            if pattern and (fnmatch.fnmatchcase(relative, pattern) or any(fnmatch.fnmatchcase(p, pattern) for p in parts)):
                return True
        return False

    def _internal_paths(self):
        # Absolute Toshy-owned locations only. A different project may have
        # source directories named cache, plugins, models or elements.
        internal=[self.directory,APP_PATHS.runtime,APP_PATHS.sessions,APP_PATHS.logs,
                  APP_PATHS.root/'backups',APP_PATHS.root/'preferences',
                  APP_PATHS.root/'cache/documents',APP_PATHS.models,APP_PATHS.browsers,
                  APP_PATHS.elements/'ocr',APP_PATHS.elements/'managed',APP_PATHS.lsp/'servers',APP_PATHS.root/'plugins',
                  APP_PATHS.root/'tools',APP_PATHS.root/'launcher',APP_PATHS.root/'bin',APP_PATHS.root/'cache/uv',
                  APP_PATHS.root/'cache/pycache']
        try:
            backup_settings=json.loads((APP_PATHS.runtime/'backup_settings.json').read_text(encoding='utf-8'))
            if backup_settings.get('directory'):internal.append(Path(backup_settings['directory']).expanduser().resolve())
        except (OSError,ValueError):pass
        private=[APP_PATHS.ui_state,APP_PATHS.config,APP_PATHS.root/'ui/tags.json',
                 APP_PATHS.root/'core/permanent_memory.sqlite',APP_PATHS.lsp/'lsp_config.json',
                 *[APP_PATHS.root/name for name in ('installation.json','transaction.json',
                   'launcher-receipts.json','shortcuts-created.json','update-handoff.log',
                   'install.log','.complete','.install-lock','.toshy-bootstrap',
                   'start.sh','start.cmd','Toshy.desktop','Toshy.lnk')]]
        private.extend(p for p in (APP_PATHS.root/'cache').glob('*')
                       if p.name.endswith(('.zip','.tar.gz','.part','.http.json')))
        return ({p.absolute() for p in internal} | {p.resolve() for p in internal},
                {p.absolute() for p in private} | {p.resolve() for p in private})

    @staticmethod
    def _is_internal(path, directories, files):
        path=path.absolute()
        return path in files or any(path.is_relative_to(p) for p in directories)

    def _scan(self, row, *, write=True, known=None):
        root = self._root(row)
        if not root.is_dir():
            raise HistoryError(f'Project Folder недоступна: {root}')
        settings = json.loads(row['settings'])
        limit = settings['max_file_mb'] * 1024 * 1024
        internal = self._internal_paths()
        if self._is_internal(root,*internal):
            return {}, []
        manifest, warnings = {}, []
        cache_key=(str(self.directory),row['id'])
        cached=_scan_cache.get(cache_key,{}) if write else {}
        updated={}
        for folder, directories, files in os.walk(root, followlinks=False):
            directories[:] = [d for d in sorted(directories) if not (Path(folder)/d).is_symlink()
                              and not self._excluded((Path(folder)/d).relative_to(root).as_posix(), settings)
                              and not self._is_internal(Path(folder)/d,*internal)]
            for name in sorted(files):
                path = Path(folder) / name
                relative = path.relative_to(root).as_posix()
                if self._is_internal(path,*internal):continue
                if self._excluded(relative, settings):
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    warnings.append(relative + ': ссылка или специальный файл пропущен'); continue
                if info.st_size > limit:
                    warnings.append(relative + ': превышен лимит размера'); continue
                fingerprint=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)
                wal=Path(str(path)+'-wal')
                if wal.exists():
                    wal_info=wal.stat()
                    fingerprint+=(wal_info.st_size,wal_info.st_mtime_ns,wal_info.st_ctime_ns)
                old=cached.get(relative)
                if old and old[0]==fingerprint and old[1]['oid'] in (known or set()):
                    manifest[relative]=dict(old[1]);updated[relative]=old;continue
                # Use an open descriptor, reject symlink replacement on Linux,
                # and verify the file did not change during capture.
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                        raise HistoryError('Файл изменился во время снимка: '+relative)
                    content = stream.read(limit + 1)
                    after = os.fstat(stream.fileno())
                if len(content) > limit or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise HistoryError('Файл изменился во время снимка: '+relative)
                if content.startswith(b'SQLite format 3\0'):
                    # Include committed WAL pages; a raw main-file copy can be
                    # incomplete even while no writer is currently running.
                    with tempfile.TemporaryDirectory(prefix='toshy-db-snapshot-') as temporary:
                        saved=Path(temporary)/'copy.sqlite'
                        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(saved)) as dst:
                            src.backup(dst)
                        if saved.stat().st_size>limit:
                            warnings.append(relative+': превышен лимит размера SQLite');continue
                        content=saved.read_bytes()
                header = b'blob '+str(len(content)).encode()+b'\0'
                oid = hashlib.sha1(header+content).hexdigest()
                if write and oid not in (known or set()):
                    actual = self._git(row['id'], 'hash-object', '-w', '--stdin', data=content).decode().strip()
                    if actual != oid:
                        raise HistoryError('Неожиданный формат объектов Git.')
                manifest[relative] = dict(oid=oid, mode='100755' if before.st_mode & stat.S_IXUSR else '100644',
                                          permissions=stat.S_IMODE(before.st_mode),size=len(content))
                updated[relative]=(fingerprint,dict(manifest[relative]))
                if len(manifest) > 50000:
                    raise HistoryError('Более 50 000 файлов. Уточните исключения истории.')
        if write:_scan_cache[cache_key]=updated
        return manifest, warnings

    def _last(self, db, workspace):
        row = db.execute('SELECT * FROM snapshots WHERE workspace=? ORDER BY created DESC,rowid DESC LIMIT 1', (workspace,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def changes(before, after):
        return [dict(path=p, status='added' if p not in before else 'deleted' if p not in after else 'modified')
                for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]

    def _commit_manifest(self, row, manifest, label):
        identity = uuid.uuid4().hex
        index = self.directory / row['id'] / ('index-'+identity)
        try:
            self._git(row['id'], 'read-tree', '--empty', index=index)
            entries = b''.join((f"{v['mode']} {v['oid']}\t".encode()+os.fsencode(p)+b'\0') for p,v in manifest.items())
            self._git(row['id'], 'update-index', '-z', '--index-info', data=entries, index=index)
            tree = self._git(row['id'], 'write-tree', index=index).decode().strip()
            return self._git(row['id'], 'commit-tree', tree, data=str(label).encode('utf-8')).decode().strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index)+'.lock').unlink(missing_ok=True)

    def _snapshot(self, db, row, label, kind, context=None, force=False, *,
                  manifest_override=None, before_override=None, warnings_override=None, shared_override=None):
        settings = json.loads(row['settings'])
        if not settings['enabled']:
            return None
        previous = self._last(db, row['id'])
        before = json.loads(previous['manifest']) if previous else {}
        if manifest_override is None:
            manifest, warnings = self._scan(row,known={item['oid'] for item in before.values()})
        else:
            manifest, warnings = manifest_override, list(warnings_override or [])
        initial=None
        if kind=='run' and context is not None:
            initial=db.execute("SELECT * FROM snapshots WHERE workspace=? AND run=? AND kind='before_run' ORDER BY created LIMIT 1",(row['id'],context.run_id)).fetchone()
            changed_tool=db.execute("WITH RECURSIVE family(run) AS (SELECT ? UNION "
                "SELECT s.run FROM snapshots s JOIN family f ON s.parent_run=f.run WHERE s.workspace=?) "
                "SELECT 1 FROM snapshots WHERE workspace=? AND run IN family AND kind='tool' LIMIT 1",
                (context.run_id,row['id'],row['id'])).fetchone()
            if initial and not changed_tool and not warnings:
                starts=db.execute("SELECT id FROM snapshots WHERE workspace=? AND run=? AND kind='before_run'",(row['id'],context.run_id)).fetchall()
                for point in starts:self._drop(db,row['id'],point['id'])
                return None
        if before_override is not None:before = before_override
        if kind=='run':before = dict(manifest)
        if before == manifest and previous and not force:
            return None
        identity = uuid.uuid4().hex
        oid = self._commit_manifest(row,manifest,label)
        base_oid = previous['oid'] if previous else ''
        coalesced = None
        edit_path = None
        edit_token = None
        if kind == 'user':
            paths = [name for name in before.keys() | manifest.keys() if before.get(name) != manifest.get(name)]
            if len(paths) == 1:
                edit_path = paths[0]
                edit_token = _edit_token(self._root(row) / edit_path)
                series = db.execute('SELECT token,path FROM user_series WHERE snapshot=?', (previous['id'],)).fetchone() if previous else None
                if (previous and previous['kind']=='user' and series and series['token']==edit_token
                        and series['path']==edit_path and not previous['shared']):
                    before = json.loads(previous['base_manifest'])
                    base_oid = previous['base_oid']
                    coalesced = identity = previous['id']
        if previous is None or before != json.loads(previous['manifest']):
            base_oid = self._commit_manifest(row,before,'До: '+str(label))
        self._git(row['id'], 'update-ref', 'refs/snapshots/'+identity, oid)
        if base_oid:
            self._git(row['id'], 'update-ref', 'refs/before/'+identity, base_oid)
        values = dict(session=getattr(context,'session_id','') or '', branch=getattr(context,'branch','') or '',
                      run=getattr(context,'run_id','') or '', parent_run=getattr(context,'parent_run_id','') or '',
                      agent=getattr(context,'subagent_id','') or ('Агент' if context else 'Пользователь'))
        if kind=='mixed':values['agent']='Совместные правки'
        elif kind in {'external','before'}:values['agent']='Внешние изменения'
        active = self._active(db, row['id'])
        shared = len({item.get('family') or item['run'] for item in active}) > 1
        if context is not None:
            shared=shared or bool(db.execute('SELECT 1 FROM snapshots WHERE workspace=? AND run=? AND shared=1 LIMIT 1',(row['id'],context.run_id)).fetchone())
        if shared_override is not None:shared = shared_override
        db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (identity,row['id'],time.time(),str(label)[:300],kind,values['session'],values['branch'],
                    values['run'],values['parent_run'],values['agent'],oid,base_oid,
                    json.dumps(manifest),json.dumps(before),json.dumps(warnings,ensure_ascii=False),int(shared),1))
        if edit_token and not shared:
            db.execute('INSERT OR REPLACE INTO user_series VALUES (?,?,?)', (identity,edit_token,edit_path))
        # Independent checkpoint commits allow real retention, without retaining
        # a deleted commit through an unbroken parent chain.
        stale = db.execute("SELECT id FROM snapshots WHERE workspace=? AND kind!='before_run' ORDER BY created DESC,rowid DESC LIMIT -1 OFFSET ?",
                           (row['id'],settings['max_snapshots'])).fetchall()
        for old in stale:
            self._drop(db,row['id'],old['id'])
        orphaned=db.execute("SELECT id FROM snapshots WHERE workspace=? AND kind='before_run' AND run NOT IN (SELECT run FROM leases) AND run NOT IN (SELECT run FROM snapshots WHERE kind!='before_run')",(row['id'],)).fetchall()
        for old in orphaned:self._drop(db,row['id'],old['id'])
        return identity

    def _drop(self,db,workspace,identity):
        db.execute('DELETE FROM user_series WHERE snapshot=?', (identity,))
        self._git(workspace,'update-ref','-d','refs/snapshots/'+identity)
        self._git(workspace,'update-ref','-d','refs/before/'+identity)
        db.execute('DELETE FROM snapshots WHERE id=?',(identity,))

    def status(self, identity):
        with self.locked() as db:
            row = self._row(db, identity)
            return dict(workspace=identity,root=str(self._root(row)),git_available=bool(self.git),
                        settings=json.loads(row['settings']),active=self._active(db,identity))

    def configure(self, identity, updates):
        with self.locked() as db:
            row = self._row(db, identity)
            if self._active(db,identity):
                raise HistoryError('Дождитесь завершения запусков в этой Project Folder.')
            settings = json.loads(row['settings'])
            settings.update({k:v for k,v in updates.items() if k in DEFAULTS})
            settings['enabled'] = bool(settings['enabled'])
            for key, low, high in [('max_file_mb',1,500),('max_snapshots',10,5000)]:
                settings[key] = max(low,min(high,int(settings[key])))
            if not isinstance(settings['excludes'],list) or not all(isinstance(x,str) for x in settings['excludes']):
                raise HistoryError('Исключения должны быть списком шаблонов.')
            root = self._root(row)
            if settings['enabled'] and (root == Path(root.anchor) or root == Path.home().resolve()):
                raise HistoryError('Выберите Project Folder, а не корень диска или домашний каталог.')
            row['settings'] = json.dumps(settings)
            db.execute('UPDATE workspaces SET settings=? WHERE id=?',(row['settings'],identity))
            if settings['enabled']:
                self._snapshot(db,row,'Начальное состояние / изменение настроек','baseline',force=True)
        return self.status(identity)

    def snapshot(self, identity, label='Контрольная точка', kind='manual', context=None, force=True):
        with self.locked() as db:
            return self._snapshot(db,self._row(db,identity),label,kind,context,force)

    def list(self, identity, session='', path=''):
        with self.locked() as db:
            rows = db.execute('SELECT * FROM snapshots WHERE workspace=? ORDER BY created DESC,rowid DESC', (identity,)).fetchall()
            result = []
            for raw in rows:
                row = dict(raw)
                if session and row['session'] != session:
                    continue
                before,after = json.loads(row.pop('base_manifest')),json.loads(row.pop('manifest'))
                run_warnings=[]
                if row['kind']=='run' and (row['tracked'] or before==after):
                    before,after,run_warnings,shared=self._run_manifests(db,identity,row['run'])
                    row['shared']=int(shared)
                row['files'] = self.changes(before,after)
                if not row['files'] and row['kind'] in {'before_run','run'} and not (json.loads(row['warnings']) or run_warnings):
                    continue
                if path and not any(path.casefold() in x['path'].casefold() for x in row['files']):
                    continue
                row['warnings'] = json.loads(row['warnings']) + run_warnings
                result.append(row)
            return result

    def _run_manifests(self, db, workspace, run):
        # Reconstruct the run from its saved boundaries, not the delta of the
        # last tool or a possibly empty legacy summary. Include descendants.
        rows=[dict(p) for p in db.execute(
            'SELECT * FROM snapshots WHERE workspace=? ORDER BY created,rowid',(workspace,))]
        family={run}
        while True:
            expanded=family | {p['run'] for p in rows if p['run'] and p['parent_run'] in family}
            if expanded==family:break
            family=expanded
        points=[p for p in rows if p['run'] in family]
        own=[p for p in points if p['run']==run]
        initial=next((p for p in own if p['kind']=='before_run'),None)
        end=next((p for p in reversed(own) if p['kind']=='run'),None)
        tools=[p for p in points if p['kind']=='tool']
        warnings=[]
        if any(p.get('tracked') for p in own):
            # Итог строится только из подтверждённых дельт инструментов.
            # Полный снимок Project Folder сам по себе не доказывает авторство.
            before, after, seen, conflicts = {}, {}, set(), set()
            for point in tools:
                old, new = json.loads(point['base_manifest']), json.loads(point['manifest'])
                for change in self.changes(old,new):
                    path = change['path']
                    if path in seen and after.get(path)!=old.get(path):conflicts.add(path)
                    if path not in seen and path in old:before[path] = old[path]
                    seen.add(path)
                    if path in new:after[path] = new[path]
                    else:after.pop(path,None)
            for path in sorted(conflicts):
                before.pop(path,None);after.pop(path,None)
                warnings.append('Правки разных авторов в '+path+' не объединены в итог запуска; смотрите отдельные операции.')
            if end is None:warnings.append('Итог запуска ещё не сохранён; показаны завершённые операции инструментов.')
            return before,after,warnings,bool(conflicts) or any(p['shared'] for p in tools)
        if end is None:
            warnings.append('Итоговая точка запуска не сохранена. Отменяются только изменения до последнего сохранённого инструмента.')
        last=end or (tools[-1] if tools else initial)
        if last is None:
            raise HistoryError('Не найдены сохранённые изменения этого запуска.')
        after=json.loads(last['manifest'])
        if initial is not None and end is not None:
            before=json.loads(initial['manifest'])
        elif end is not None and json.loads(end['base_manifest'])!=after:
            before=json.loads(end['base_manifest'])
        else:
            # Old/interrupted histories may have lost the start marker. Invert
            # only paths with actual saved tool deltas; never guess other files.
            if initial is None:
                warnings.append('Начальная точка запуска отсутствует. Возврат ограничен файлами с сохранёнными изменениями инструментов.')
            before=dict(after);seen=set()
            for point in tools:
                old,new=json.loads(point['base_manifest']),json.loads(point['manifest'])
                for change in self.changes(old,new):
                    path=change['path']
                    if path in seen:continue
                    seen.add(path)
                    if path in old:before[path]=old[path]
                    else:before.pop(path,None)
        return before,after,warnings,any(p['shared'] for p in points)

    def _get(self, db, identity, snapshot):
        raw = db.execute('SELECT * FROM snapshots WHERE id=? AND workspace=?',(snapshot,identity)).fetchone()
        if raw is None:
            raise HistoryError('Контрольная точка не найдена.')
        point=dict(raw)
        if point['kind']=='run' and (point['tracked'] or json.loads(point['base_manifest'])==json.loads(point['manifest'])):
            before,after,_,_=self._run_manifests(db,identity,point['run'])
            point['base_manifest']=json.dumps(before)
            point['manifest']=json.dumps(after)
        return point

    def file(self, identity, snapshot, path, current=False):
        with self.locked() as db:
            row = self._row(db,identity)
            point = self._get(db,identity,snapshot)
            before,after = json.loads(point['base_manifest']),json.loads(point['manifest'])
            def read(item):
                if not item:
                    return b''
                return self._git(identity,'cat-file','blob',item['oid'])
            old,new = read(before.get(path)),read(after.get(path))
            if current:
                target = self._safe_target(row,path)
                new = target.read_bytes() if target.is_file() else b''
            binary = any(b'\0' in data[:8192] for data in (old,new))
            return dict(path=path,binary=binary,before=old,after=new)

    def _safe_target(self,row,relative):
        path = PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative or not path.parts:
            raise HistoryError('Недопустимый путь файла.')
        root = self._root(row).resolve()
        target = root.joinpath(*path.parts)
        cursor = root
        for part in path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise HistoryError('Восстановление через символьные ссылки запрещено: '+relative)
        if not target.resolve().is_relative_to(root):
            raise HistoryError('Файл находится вне Project Folder.')
        return target

    def _active(self, db, identity):
        result=[]
        for row in db.execute('SELECT * FROM leases WHERE workspace=?',(identity,)).fetchall():
            # The prefix distinguishes lock-owned records from legacy UUIDs,
            # without changing the database schema used by previous versions.
            owner=row['id'] if (len(row['id'])==33 and row['id'].startswith('g')
                and all(c in '0123456789abcdef' for c in row['id'][1:])) else ''
            # A live application process is not proof that its old run is live.
            # New records require an OS lock held for that particular operation.
            active=(_activity_held(self.directory,owner) if owner else
                    row['pid']!=os.getpid() and _process_alive(row['pid']))
            if not active:
                db.execute('DELETE FROM operations WHERE id=?',(row['id'],))
                self._git(identity,'update-ref','-d','refs/operations/'+row['id'])
                db.execute('DELETE FROM leases WHERE id=?',(row['id'],))
                if owner:
                    try:(self.directory/'activity'/(owner+'.lock')).unlink(missing_ok=True)
                    except OSError:pass
                continue
            result.append(dict(row))
        return result

    def _lease(self,db,row,context=None):
        identity='g'+uuid.uuid4().hex
        run=getattr(context,'run_id','') or 'user:'+identity
        parent=getattr(context,'parent_run_id','') or ''
        parent_lease=db.execute('SELECT family FROM leases WHERE workspace=? AND run=? LIMIT 1',(row['id'],parent)).fetchone() if parent else None
        family=(parent_lease['family'] if parent_lease else parent) or run
        _claim_activity(self.directory,identity)
        try:
            db.execute('INSERT INTO leases(id,workspace,pid,run,session,family) VALUES(?,?,?,?,?,?)',
                       (identity,row['id'],os.getpid(),run,getattr(context,'session_id','') or '',family))
            if len({x.get('family') or x['run'] for x in self._active(db,row['id'])})>1:
                db.execute('UPDATE snapshots SET shared=1 WHERE workspace=? AND tracked=0 AND run IN (SELECT run FROM leases WHERE workspace=?)',(row['id'],row['id']))
        except BaseException:
            _release_activity(self.directory,identity)
            raise
        return identity

    def plan(self, identity, snapshot, mode='undo', path=''):
        if mode not in {'undo','state','file','run'}:
            raise HistoryError('Неизвестный режим восстановления.')
        with self.locked() as db:
            row = self._row(db,identity)
            if not json.loads(row['settings'])['enabled']:
                raise HistoryError('Включите историю файлов перед восстановлением.')
            if self._active(db,identity):
                raise HistoryError('Завершите генерации и сохранения в этой Project Folder.')
            point = self._get(db,identity,snapshot)
            before,after = json.loads(point['base_manifest']),json.loads(point['manifest'])
            if mode=='state':
                # Полное восстановление точки явно выбирает пользователь.
                raw=db.execute('SELECT manifest,base_manifest FROM snapshots WHERE id=?',(snapshot,)).fetchone()
                before,after=json.loads(raw['base_manifest']),json.loads(raw['manifest'])
            if mode=='run':
                if not point['run']:
                    raise HistoryError('У записи нет связанного запуска.')
                before,after,run_warnings,shared=self._run_manifests(db,identity,point['run'])
                if shared:
                    raise HistoryError('Есть совместные или внешние изменения. Используйте восстановление отдельных изменений или файлов.')
            current,warnings = self._scan(row,write=False)
            if mode=='run':warnings=run_warnings+warnings
            if mode in {'undo','run'}:
                paths=[p['path'] for p in self.changes(before,after)]
                desired=before
            elif mode == 'file':
                if path not in after and path not in before:
                    raise HistoryError('Файл отсутствует в выбранной записи.')
                paths=[path]; desired=after if path in after else before
            else:
                paths=sorted(current.keys() | after.keys()); desired=after
            operations=[]
            settings=json.loads(row['settings'])
            internal=self._internal_paths()
            excluded=0
            for relative in paths:
                # Old snapshots may contain resources that are now protected.
                # Never restore them, even if they no longer exist on disk.
                if (self._excluded(relative,settings)
                        or self._is_internal(self._root(row)/relative,*internal)):
                    if mode=='file':
                        raise HistoryError('Файл исключён из истории: '+relative)
                    excluded+=1
                    continue
                target=self._safe_target(row,relative)
                value=desired.get(relative)
                now=current.get(relative)
                # Excluded/oversized files must not be mistaken for absent files.
                if target.exists() and relative not in current:
                    raise HistoryError('Файл исключён или слишком большой: '+relative)
                if now == value:
                    continue
                operations.append(dict(path=relative,current=now,desired=value,
                    conflict=mode in {'undo','run'} and now!=after.get(relative),
                    action='удалить' if value is None else 'создать' if now is None else 'заменить'))
            if excluded:
                warnings.append(f'Исключённые и служебные файлы пропущены: {excluded}.')
            token=uuid.uuid4().hex
            data=dict(operations=operations,snapshot=snapshot,mode=mode,warnings=warnings)
            db.execute('DELETE FROM plans WHERE created<?',(time.time()-600,))
            db.execute('INSERT INTO plans VALUES(?,?,?,?)',(token,identity,time.time(),json.dumps(data)))
            return dict(token=token,**data)

    def restore(self, identity, token, skip_conflicts=True):
        with self.locked() as db:
            row=self._row(db,identity)
            if not json.loads(row['settings'])['enabled']:
                raise HistoryError('Включите историю файлов перед восстановлением.')
            if self._active(db,identity):
                raise HistoryError('В Project Folder выполняется запуск или сохранение.')
            raw=db.execute('SELECT * FROM plans WHERE id=? AND workspace=?',(token,identity)).fetchone()
            if raw is None or raw['created']<time.time()-600:
                raise HistoryError('Предпросмотр устарел. Откройте его повторно.')
            plan=json.loads(raw['data'])
            operations=[x for x in plan['operations'] if not (skip_conflicts and x['conflict'])]
            current,_=self._scan(row,write=False)
            settings=json.loads(row['settings'])
            internal=self._internal_paths()
            for item in operations:
                target=self._safe_target(row,item['path'])
                if self._excluded(item['path'],settings) or self._is_internal(target,*internal):
                    raise HistoryError('Файл исключён из истории. Обновите предпросмотр: '+item['path'])
                if current.get(item['path'])!=item['current']:
                    raise HistoryError('Файлы изменились после предпросмотра: '+item['path'])
            if not operations:
                return dict(restored=[],skipped=[x['path'] for x in plan['operations'] if x['conflict']])
            safety=self._snapshot(db,row,'Перед восстановлением','safety',force=True)
            # Publish safety checkpoint before touching a working file. A crash
            # during restore must still leave a usable recovery point.
            db.commit()
            prepared=[]
            try:
                for item in operations:
                    target=self._safe_target(row,item['path'])
                    value=item['desired']; temp=None
                    if value:
                        content=self._git(identity,'cat-file','blob',value['oid'])
                        target.parent.mkdir(parents=True,exist_ok=True)
                        fd,temp=tempfile.mkstemp(prefix='.toshy-restore-',dir=target.parent)
                        with os.fdopen(fd,'wb') as stream:
                            stream.write(content);stream.flush();os.fsync(stream.fileno())
                        os.chmod(temp,value.get('permissions',0o755 if value['mode']=='100755' else 0o644))
                    prepared.append((item,target,temp))
                for item,target,temp in prepared:
                    self._safe_target(row,item['path'])
                    if temp:
                        os.replace(temp,target)
                    else:
                        target.unlink(missing_ok=True)
                self._snapshot(db,row,'Восстановление '+plan['mode'],'restore',force=True)
                db.execute('DELETE FROM plans WHERE id=?',(token,))
            except BaseException as error:
                raise HistoryError(f'Восстановление прервано: {error}. Исходное состояние сохранено в точке {safety}.') from error
            finally:
                for _,_,temp in prepared:
                    if temp:
                        Path(temp).unlink(missing_ok=True)
            return dict(restored=[x['path'] for x in operations],
                        skipped=[x['path'] for x in plan['operations'] if skip_conflicts and x['conflict']],safety=safety)

    @staticmethod
    def _in_scopes(path, scopes):
        if os.name=='nt':
            path=path.casefold()
            scopes=[scope.casefold() for scope in scopes] if scopes is not None else None
        return scopes is None or any(not scope or path==scope or path.startswith(scope.rstrip('/')+'/') for scope in scopes)

    def _operation_begin(self, db, row, lease, kind, paths):
        """Намерение сохранить файл учитывается до записи и видно другим процессам."""
        root = self._root(row)
        scopes = None if paths is None else []
        for path in paths or []:
            target = Path(path)
            target = (target if target.is_absolute() else root/target).resolve()
            if target.is_relative_to(root):scopes.append('' if target==root else target.relative_to(root).as_posix())
        previous = self._last(db,row['id'])
        baseline = json.loads(previous['manifest']) if previous else {}
        blocked = []
        for other in db.execute('SELECT id,kind,scopes,blocked FROM operations WHERE workspace=?',(row['id'],)).fetchall():
            if kind=='user' and other['kind']=='tool':
                value = json.loads(other['blocked']) + (scopes or [])
                db.execute('UPDATE operations SET blocked=? WHERE id=?',(json.dumps(sorted(set(value))),other['id']))
            elif kind=='tool' and other['kind']=='user':
                blocked.extend(json.loads(other['scopes']) or [])
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',
                   (lease,row['id'],kind,json.dumps(baseline),json.dumps(scopes),json.dumps(sorted(set(blocked)))))
        if previous:self._git(row['id'],'update-ref','refs/operations/'+lease,previous['oid'])

    def _delta_snapshot(self, db, row, label, kind, context, old, current, paths, warnings=(), shared=False):
        if not paths:return None
        # Сам снимок полный; его дельта содержит только файлы этой операции.
        before = dict(current)
        for path in paths:
            if path in old:before[path] = old[path]
            else:before.pop(path,None)
        if before==current:return None
        return self._snapshot(db,row,label,kind,context,force=True,manifest_override=current,
                              before_override=before,warnings_override=warnings,shared_override=shared)

    def _operation_end(self, db, row, lease, label, context=None):
        operation = db.execute('SELECT * FROM operations WHERE id=?',(lease,)).fetchone()
        if operation is None:return
        initial = json.loads(operation['baseline'])
        scopes, blocked = json.loads(operation['scopes']), json.loads(operation['blocked'])
        previous = self._last(db,row['id'])
        last = json.loads(previous['manifest']) if previous else {}
        current, warnings = self._scan(row,known={v['oid'] for v in last.values()})
        changed = {item['path'] for item in self.changes(initial,current) if self._in_scopes(item['path'],scopes)}
        mixed = {path for path in changed if blocked and self._in_scopes(path,blocked)}
        owned = changed - mixed
        outside = {item['path'] for item in self.changes(last,current)} - owned - mixed
        self._delta_snapshot(db,row,'Внешние изменения','external',None,last,current,outside)
        if mixed:
            self._delta_snapshot(db,row,'Одновременные правки пользователя и инструмента','mixed',None,
                                 initial,current,mixed, ['Авторство общей правки файла не определено.'],shared=True)
        # Сохранение пользователя в другом файле не делает правки агента совместными.
        families = {item.get('family') or item['run'] for item in self._active(db,row['id'])
                    if not item['run'].startswith('user:')}
        self._delta_snapshot(db,row,label,operation['kind'],context,initial,current,owned,warnings,
                             shared=operation['kind']=='tool' and len(families)>1)

    def _operation_drop(self, db, row, lease):
        self._git(row['id'],'update-ref','-d','refs/operations/'+lease)
        db.execute('DELETE FROM operations WHERE id=?',(lease,))
        db.execute('DELETE FROM leases WHERE id=?',(lease,))

    def begin(self, context):
        lease=None
        try:
            with self.locked() as db:
                row=self._workspace_for_path(db,context.workspace_root)
                if not row or not json.loads(row['settings'])['enabled']:
                    return None
                lease=self._lease(db,row,context)
                self._snapshot(db,row,'Внешние изменения перед запуском','external')
                self._snapshot(db,row,'Перед запуском','before_run',context,force=True)
                return row['id'],lease
        except BaseException:
            if lease:_release_activity(self.directory,lease)
            raise

    def end(self, ticket, context):
        try:
            if not ticket:
                return
            with self.locked() as db:
                try:
                    row=self._row(db,ticket[0])
                    external=self._snapshot(db,row,'Внешние изменения во время запуска','external')
                    if external:
                        db.execute('UPDATE snapshots SET shared=1 WHERE workspace=? AND (run=? OR id=?)',(row['id'],context.run_id,external))
                    self._snapshot(db,row,'Завершение запуска','run',context,force=True)
                finally:
                    db.execute('DELETE FROM leases WHERE id=?',(ticket[1],))
                    db.commit()
        finally:
            if ticket:_release_activity(self.directory,ticket[1])

    def capture_context(self, context, label, kind):
        with self.locked() as db:
            row=self._workspace_for_path(db,context.workspace_root)
            if row:
                return self._snapshot(db,row,label,kind,context)

    def tool_begin(self,context,name,paths=None):
        if paths == []:return None
        lease=None
        try:
            with self.locked() as db:
                row=self._workspace_for_path(db,context.workspace_root)
                if not row or not json.loads(row['settings'])['enabled']:return None
                lease=self._lease(db,row,context)
                self._snapshot(db,row,'Перед инструментом: '+name,'external')
                self._operation_begin(db,row,lease,'tool',paths)
                return row['id'],lease
        except BaseException:
            if lease:_release_activity(self.directory,lease)
            raise

    def tool_end(self,ticket,context,name):
        try:
            if not ticket:return
            with self.locked() as db:
                row=self._row(db,ticket[0])
                try:self._operation_end(db,row,ticket[1],name,context)
                finally:
                    self._operation_drop(db,row,ticket[1]);db.commit()
        finally:
            if ticket:_release_activity(self.directory,ticket[1])

    def user_begin(self,path):
        lease=None
        try:
            paths = list(path) if isinstance(path,(list,tuple,set)) else [path]
            target=Path(paths[0]).resolve()
            with self.locked() as db:
                rows=[dict(row) for row in db.execute('SELECT * FROM workspaces')
                      if target.is_relative_to(self._root(row)) and json.loads(row['settings'])['enabled']]
                if not rows:return None
                row=max(rows,key=lambda r:len(str(self._root(r))))
                if self._excluded(target.relative_to(self._root(row)).as_posix(),json.loads(row['settings'])):return None
                lease=self._lease(db,row)
                self._operation_begin(db,row,lease,'user',paths)
                try:
                    self._snapshot(db,row,'Перед сохранением: '+target.name,'external')
                except (HistoryError,OSError) as error:
                    # Даже нестабильный файл параллельной команды не отменяет
                    # известное намерение пользователя сохранить свой файл.
                    _report(error)
                else:
                    previous=self._last(db,row['id'])
                    if previous:
                        db.execute('UPDATE operations SET baseline=? WHERE id=?',(previous['manifest'],lease))
                        self._git(row['id'],'update-ref','refs/operations/'+lease,previous['oid'])
                return row['id'],lease,str(target.relative_to(self._root(row)))
        except BaseException:
            if lease:_release_activity(self.directory,lease)
            raise

    def user_end(self,ticket,label=None):
        try:
            if not ticket:return
            with self.locked() as db:
                try:
                    self._operation_end(db,self._row(db,ticket[0]),ticket[1],label or 'Сохранение: '+ticket[2])
                finally:
                    self._operation_drop(db,self._row(db,ticket[0]),ticket[1])
                    db.commit()
        finally:
            if ticket:_release_activity(self.directory,ticket[1])

    def file_origin(self, path, oid):
        # Авторство подтверждается последней дельтой именно этой версии файла.
        target = Path(path).resolve()
        with self.locked() as db:
            rows = [dict(value) for value in db.execute('SELECT * FROM workspaces')
                    if target.is_relative_to(self._root(value))]
            if not rows:
                return {'model': False}
            row = max(rows, key=lambda value: len(str(self._root(value))))
            relative = target.relative_to(self._root(row)).as_posix()
            for point in db.execute("SELECT kind,manifest,base_manifest FROM snapshots WHERE workspace=? "
                                    "AND kind IN ('tool','user','external','mixed','restore') "
                                    "ORDER BY created DESC,rowid DESC", (row['id'],)):
                before = json.loads(point['base_manifest']).get(relative)
                after = json.loads(point['manifest']).get(relative)
                if before == after:
                    continue
                return {'model': point['kind'] == 'tool' and bool(after) and after.get('oid') == oid}
        return {'model': False}

    def last_restore_notice(self,root):
        with self.locked() as db:
            row=self._workspace_for_path(db,root)
            if not row:return ''
            point=db.execute("SELECT * FROM snapshots WHERE workspace=? AND kind='restore' ORDER BY created DESC LIMIT 1",(row['id'],)).fetchone()
            if not point:return ''
            changes=self.changes(json.loads(point['base_manifest']),json.loads(point['manifest']))
            return ('The user restored Project Folder files at '+time.strftime('%Y-%m-%d %H:%M',time.localtime(point['created']))+
                    '. Previous tool results may describe an outdated state. Read the current files before editing. Changed: '+
                    ', '.join(x['path'] for x in changes[:100]))

    def gc(self, identity):
        with self.locked() as db:
            if self._active(db,identity):
                raise HistoryError('Дождитесь завершения запусков.')
            self._row(db,identity)
            self._git(identity,'reflog','expire','--expire=now','--all')
            self._git(identity,'gc','--prune=now')

    def poll_external(self):
        with self.locked() as db:
            for raw in db.execute('SELECT * FROM workspaces').fetchall():
                row=dict(raw)
                if json.loads(row['settings'])['enabled'] and not self._active(db,row['id']):
                    try:self._snapshot(db,row,'Внешние изменения / терминал','external')
                    except (OSError,HistoryError) as error:_report(error)


def get_history():
    return FileHistory()


def _report(error):
    log.warning('File history: %s',error)


async def _join_history_worker(task):
    # Disconnect/Stop may cancel the consumer repeatedly. Never propagate
    # cancellation into the Task wrapping a still-running to_thread worker.
    cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True
        except Exception:break  # Re-raise via result(), preserving cancellation.
    try:result=task.result()
    except Exception as error:
        if cancelled:raise asyncio.CancelledError from error
        raise
    return result,cancelled


async def _finish_thread(fn, *args):
    task=asyncio.create_task(asyncio.to_thread(fn,*args))
    result,cancelled=await _join_history_worker(task)
    if cancelled:raise asyncio.CancelledError
    return result


@asynccontextmanager
async def history_run(context):
    store=get_history();ticket=None
    begin=asyncio.create_task(asyncio.to_thread(store.begin,context))
    try:
        ticket,cancelled=await _join_history_worker(begin)
        if cancelled:
            if ticket:await _finish_thread(store.end,ticket,context)
            raise asyncio.CancelledError
    except Exception as error:
        _report(error)
    try:
        yield
    finally:
        if ticket:
            try:
                await _finish_thread(store.end,ticket,context)
            except Exception as error:
                _report(error)


@asynccontextmanager
async def history_tool(context,name,paths=None):
    if paths == []:
        yield
        return
    if context is None:
        from core.context import get_execution_context
        context=get_execution_context()
    store=get_history();ticket=None
    begin=asyncio.create_task(asyncio.to_thread(store.tool_begin,context,name,paths))
    try:
        ticket,cancelled=await _join_history_worker(begin)
        if cancelled:
            if ticket:await _finish_thread(store.tool_end,ticket,context,name)
            raise asyncio.CancelledError
    except HistoryBusy:
        raise
    except Exception as error:
        _report(error)
    from core.file_origins import model_begin, model_end
    origin_ticket = None
    try:
        try:
            origin_ticket = await asyncio.to_thread(model_begin, context, paths)
        except Exception as error:
            _report(error)
        yield
    finally:
        try:
            try:
                await _finish_thread(model_end, origin_ticket)
            except Exception as error:
                _report(error)
        finally:
            # Повторная отмена во время учёта авторства не оставляет lease истории.
            try:
                if ticket:await _finish_thread(store.tool_end,ticket,context,name)
            except Exception as error:
                _report(error)


def record_file_save(path_getter,label=None,*,run_history=None):
    """Keep the write and its two snapshots in one cross-process lease."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            store=get_history();ticket=None;path=None
            call=run_history or (lambda function,*values:function(*values))
            try:
                path=path_getter(*args,**kwargs)
                if path:ticket=call(store.user_begin,path)
            except HistoryBusy:
                raise
            except Exception as error:
                _report(error)
            try:
                from core.file_origins import user_save
                if path:
                    paths = path if isinstance(path,(list,tuple,set)) else [path]
                    with ExitStack() as origins:
                        for target in paths:origins.enter_context(user_save(target))
                        return fn(*args,**kwargs)
                return fn(*args,**kwargs)
            finally:
                if ticket:
                    try:call(store.user_end,ticket,label)
                    except Exception as error:_report(error)
        return wrapped
    return decorate
