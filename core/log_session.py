"""One append-only log shared by the UI, its core and module loggers."""
import os
import uuid
from datetime import datetime
from pathlib import Path
from core.paths import APP_PATHS

LOG_ENV = 'TOSHY_APP_LOG'


def session_log_path(directory=None, *, new=False):
    inherited = os.environ.get(LOG_ENV, '')
    if inherited and not new:
        return Path(inherited)
    directory = Path(directory or APP_PATHS.logs).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    path = directory / f'app-session-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:6]}.log'
    path.touch(exist_ok=False)
    prune_session_logs(directory, keep=10, current=path)
    os.environ[LOG_ENV] = str(path)
    return path


def append_record(path, record):
    # One write per record, O_APPEND, no process ever truncates the shared file.
    # Also used before Qt starts and while its child core is shutting down.
    import json
    payload = (json.dumps(record, ensure_ascii=False, default=str) + '\n').encode('utf-8')
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        # Lock the actual file across processes. Windows locks an existing byte;
        # POSIX flock has equivalent ownership for a complete record write.
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
        finally:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def prune_session_logs(directory, *, keep=10, current=None):
    paths = sorted(Path(directory).glob('app-session-*.log'), key=lambda p:p.name, reverse=True)
    for path in paths[max(1, keep):]:
        if path == current:continue
        try:
            pid = int(path.stem.split('-')[-2]);os.kill(pid, 0)
        except ProcessLookupError:
            path.unlink(missing_ok=True)
        except (ValueError, OSError):continue
