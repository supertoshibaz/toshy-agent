import sys
import json
import os
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.paths import APP_PATHS

class RotatingFileLogger:
    """Legacy module API; all instances append to the application session log."""
    def __init__(self, log_dir: Optional[str] = None, prefix: str = "toshy-log-", ext: str = ".log", max_files: int = 10):
        from core.log_session import session_log_path
        self.current_log_file = session_log_path()
        self.log_dir = self.current_log_file.parent
        self.prefix, self.ext, self.max_files = prefix, ext, max_files
        self.event_file = self.current_log_file
        self.source = prefix.rstrip('-')
        self._lock = threading.RLock()
        self.event('app', 'LOG_START', 'Logger attached', details={'pid': os.getpid(), 'source': self.source})

    def log(self, level: str, tag: str, message: str, *, category=None, context=None, details=None):
        from core.log_events import category_for
        if context is None:
            from core.context import get_execution_context
            context = get_execution_context()
        def field(name, default=''):
            return (context.get(name, default) if isinstance(context, dict) else getattr(context, name, default)) or default
        event = {'id': uuid.uuid4().hex, 'ts': time.time(),
            'time': datetime.now().isoformat(timespec='milliseconds'),
            'level': level.upper(), 'tag': tag, 'category': category or category_for(tag),
            'message': str(message), 'session': field('session_id'),
            'branch': field('branch') if field('session_id') else '',
            'run': field('run_id') if field('run_id') != 'default' else '',
            'pid': os.getpid(), 'details': details or {}}
        event['source'] = self.source
        try:
            from core.log_session import append_record, session_log_path
            with self._lock:
                self.current_log_file = session_log_path()
                self.event_file = self.current_log_file
                append_record(self.current_log_file, event)
        except Exception as e:
            print(f"[Logger Error] Could not write the log: {e}", file=sys.stderr)

    def event(self, category, tag, message, *, level='INFO', context=None, details=None):
        self.log(level, tag, message, category=category, context=context, details=details)

    def info(self, tag: str, message: str):
        self.log("INFO", tag, message)

    def debug(self, tag: str, message: str):
        self.log("DEBUG", tag, message)

    def warn(self, tag: str, message: str):
        self.log("WARN", tag, message)

    def error(self, tag: str, message: str):
        self.log("ERROR", tag, message)


# Глобальный инстанс для основного ядра
core_logger = RotatingFileLogger(prefix="toshy-log-", ext=".log", max_files=10)


def log_operation(category, tag):
    """Log operation boundaries without copying texts/vectors into the log."""
    import inspect
    from functools import wraps
    def decorate(function):
        def begin(args, kwargs):
            owner = args[0] if args else None
            path = getattr(owner, 'file_path', None) or getattr(owner, 'source', None)
            details = {'operation':function.__name__}
            if path: details['path'] = str(path)
            if getattr(owner, 'model_name', None):
                details.update(model=owner.model_name, endpoint=owner.llm_url)
            bound = inspect.signature(function).bind_partial(*args, **kwargs).arguments
            for key in ('source', 'keywords', 'max_pages', 'module_name', 'doc_id', 'fact_id', 'limit', 'purpose', 'task'):
                if key in bound: details[key] = bound[key]
            core_logger.event(category, tag+'_START', function.__name__, details=details)
            return time.perf_counter()
        def end(start, result=None, error=None):
            if error is not None:
                level = 'INFO' if isinstance(error, BaseException) and type(error).__name__ in {'CancelledError', 'GeneratorExit'} else 'ERROR'
                core_logger.event(category, tag+'_ERROR', f'{function.__name__}: {error}', level=level)
            else:
                details = {'seconds':time.perf_counter()-start}
                if isinstance(result, (list, dict, tuple)): details['items'] = len(result)
                elif isinstance(result, (str, bool, int, float)): details['result'] = str(result)[:300]
                core_logger.event(category, tag+'_DONE', f'{function.__name__} · {details["seconds"]:.3f} s', details=details)
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def wrapped(*args, **kwargs):
                start = begin(args, kwargs)
                try: result = await function(*args, **kwargs)
                except BaseException as error:
                    end(start, error=error); raise
                end(start, result); return result
        else:
            @wraps(function)
            def wrapped(*args, **kwargs):
                start = begin(args, kwargs)
                try: result = function(*args, **kwargs)
                except BaseException as error:
                    end(start, error=error); raise
                end(start, result); return result
        return wrapped
    return decorate
