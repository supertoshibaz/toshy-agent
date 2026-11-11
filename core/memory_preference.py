"""Глобальный последний выбор типа временной памяти."""
import json
import os
import tempfile
from core.paths import APP_PATHS


def last_memory_mode():
    try:
        value = json.loads((APP_PATHS.root / 'preferences' / 'memory.json').read_text(encoding='utf-8'))
        return 'vector' if value.get('mode') == 'vector' else 'text'
    except (OSError, ValueError, AttributeError):
        return 'text'


def remember_memory_mode(mode):
    if mode not in {'text', 'vector'}:
        return
    directory = APP_PATHS.root / 'preferences'
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.memory-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'mode': mode}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / 'memory.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
