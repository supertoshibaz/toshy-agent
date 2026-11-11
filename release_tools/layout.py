"""Durable install state, interprocess lock and data preparation."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from .network import ReleaseError


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {} if default is None else default


def default_root():
    return Path(__file__).resolve().parent.parent


def is_portable(root, state=None):
    state = read_json(Path(root) / 'installation.json') if state is None else state
    return state.get('layout') == 'portable'


def data_root(root, state=None):
    root = Path(root)
    return root if is_portable(root, state) else root / 'data'


def program_root(root, info, state=None):
    root = Path(root)
    return root if is_portable(root, state) else root / 'versions' / info['key']


def owned(root):
    root = Path(root).expanduser().absolute()
    if root.is_symlink() or root == Path.home() or root == Path(root.anchor):
        raise ReleaseError('Unsafe installation directory.')
    marker = read_json(root / 'installation.json')
    if marker.get('product') != 'Toshy' or marker.get('root') != str(root):
        raise ReleaseError('This directory is not a registered Toshy installation.')
    return marker


@contextmanager
def lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.install-lock').open('a+b') as f:
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0, 2)
                if not f.tell():
                    f.write(b'0'); f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise ReleaseError('Another installer/updater is running.') from e
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def _process_running(pid):
    if pid <= 0:
        raise ValueError('Invalid process ID.')
    if os.name != 'nt':
        os.kill(pid, 0)
        return True
    # Windows os.kill(pid, 0) isn't a POSIX existence probe. Query a
    # synchronization handle instead, without permission to terminate anything.
    import ctypes
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    api.OpenProcess.restype = w.HANDLE
    api.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    api.WaitForSingleObject.restype = w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return False
        raise ctypes.WinError(error)
    try:
        status = api.WaitForSingleObject(handle, 0)
        if status == 0:  # Signalled: process has exited.
            return False
        if status == 0x102:  # WAIT_TIMEOUT: process is still running.
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def running(root):
    result = []
    for p in (data_root(root) / 'runtime/backup_processes').glob('*.pid'):
        try:
            pid = int(p.read_text())
            if pid == os.getpid():
                continue
            if _process_running(pid):
                result.append(pid)
            else:
                p.unlink(missing_ok=True)
        except ProcessLookupError:
            p.unlink(missing_ok=True)
        except (ValueError, OSError):
            # Permission errors are not proof that a process is gone.
            result.append(str(p))
    return result


def require_stopped(root):
    active = running(root)
    if active:
        raise ReleaseError('Close Toshy and stop its core first. Active processes: ' + ', '.join(map(str, active)))


def seed_data(root, source):
    root, source = Path(root), Path(source)
    data = data_root(root)
    data.mkdir(parents=True, exist_ok=True)
    for directory in ('core', 'ui', 'preferences', 'sessions', 'workspace', 'materials', 'logs', 'runtime', 'plugins', 'modules/custom', 'modules/system', 'elements'):
        (data / directory).mkdir(parents=True, exist_ok=True)
    if not (data / 'config.yaml').exists():
        shutil.copy2(source / 'release_tools/default_config.json', data / 'config.yaml')
    for module in (source / 'modules/system').iterdir():
        if not module.is_dir():
            continue
        for file in (module / 'ui').glob('*'):
            if file.name == 'config.json' or file.suffix == '.svg':
                target = data / 'modules/system' / module.name / 'ui' / file.name
                if file.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
    # The authoring module is an editable starter package, copied once. User
    # modules are never replaced during application updates.
    starter = source / 'modules/custom/toshy_developer'
    target = data / 'modules/custom/toshy_developer'
    if starter.is_dir() and not target.exists():
        shutil.copytree(starter, target, ignore=shutil.ignore_patterns('.git', '__pycache__'))


def backup_data(root):
    root = Path(root)
    require_stopped(root)
    path = root / 'backups' / (time.strftime('before-update-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    path.mkdir(parents=True)
    (path / '.toshy-update-backup').write_text('Toshy update backup\n')
    for name in ('config.yaml', 'core', 'ui', 'preferences', 'sessions', 'modules', 'plugins'):
        src, dest = data_root(root) / name, path / name
        if src.is_symlink():
            raise ReleaseError('Linked data cannot be included in an automatic update backup.')
        if src.is_dir():
            def ignore(directory, names):
                if any(Path(directory, n).is_symlink() for n in names):
                    raise ReleaseError('Linked data needs a separate backup before updating. No active files were changed.')
                return [n for n in names if n in {'__pycache__'}]
            shutil.copytree(src, dest, ignore=ignore)
        elif src.is_file():
            shutil.copy2(src, dest)
    return path
