"""Bounded startup IO preparation; workers never import or construct widgets."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Lock


_active = None


def _signature(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns, stat.st_size, stat.st_ino


def _read(path, kind):
    # A file changed during startup must not publish a stale cached buffer.
    before = _signature(path)
    if kind == 'yaml':
        import yaml
        with open(path, encoding='utf-8') as source:
            value = yaml.safe_load(source) or {}
    elif kind == 'json':
        with open(path, encoding='utf-8') as source:
            value = json.load(source)
    else:
        from core.text_files import decode_text
        snapshot = decode_text(Path(path).read_bytes())
        value = snapshot if kind == 'text_snapshot' else snapshot[0]
    return before if before == _signature(path) else None, value


def scan_modules(root):
    """Plain metadata only. Module Python code is always imported on the GUI."""
    result = []
    for category in ('system', 'custom'):
        from core.plugins.modules import module_locations
        entries = module_locations(root, category)
        for (_, name), module in sorted(entries.items()):
            entry = dict(category=category, name=name,
                         widget=(module / 'ui' / 'widget.py').is_file(), ui=None,
                         display_name=None)
            for filename, field in (('ui/config.json', 'ui'), ('module.json', 'manifest')):
                try:
                    from core.paths import APP_PATHS
                    path = module / filename
                    overlay = APP_PATHS.modules / category / name / filename
                    if category == 'system' and filename == 'ui/config.json' and overlay.is_file():
                        path = overlay
                    data = _read(path, 'json')[1]
                    if isinstance(data, dict):
                        if field == 'ui':
                            entry['ui'] = data
                        else:
                            entry['display_name'] = data.get('name')
                except (OSError, ValueError):
                    pass
            result.append(entry)
    return result


class StartupData:
    def __init__(self, paths):
        self.root = Path(paths.root).resolve()
        from ui.workspace_settings import load_workspace_options
        self.workspace_options = load_workspace_options(paths.runtime / 'app_performance.json')
        self.pool = ThreadPoolExecutor(max_workers=min(4, max(2, os.cpu_count() or 2)),
                                       thread_name_prefix='toshy-startup')
        self.lock = Lock()
        self.futures = {}
        self.reserved = 0
        self.closed = False
        self._submit(paths.config, 'yaml')
        self.catalog = self.pool.submit(scan_modules, self.root)
        state_path = str(Path(paths.ui_state).resolve())
        self.futures[('json', state_path)] = self.pool.submit(self._state, state_path)

    def _submit(self, path, kind, *, document=False):
        path = str(Path(path).resolve())
        key = (kind, path)
        with self.lock:
            if self.closed or key in self.futures:
                return
            if document:
                try:
                    size = Path(path).stat().st_size
                except OSError:
                    return
                if size > 8 * 1024 * 1024 or self.reserved + size > 32 * 1024 * 1024:
                    return
                self.reserved += size
            self.futures[key] = self.pool.submit(_read, path, kind)

    def _state(self, path):
        result = _read(path, 'json')
        # Prepare restored documents while Qt imports and constructs controls.
        def walk(value):
            if isinstance(value, dict):
                for key, children in value.items():
                    if key in ('open_files', 'open_boards') and isinstance(children, list):
                        if key == 'open_boards' and not self.workspace_options['board']:
                            continue
                        for filename in children[:128]:
                            if isinstance(filename,dict):filename = filename.get("path")
                            if isinstance(filename, str):
                                self._submit(filename, 'json' if key == 'open_boards' else 'text_snapshot', document=True)
                    elif isinstance(children, (dict, list)):
                        walk(children)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(result[1])
        return result

    def read(self, path, kind):
        key = (kind, str(Path(path).resolve()))
        with self.lock:
            future = self.futures.get(key)
        if future is not None:
            signature, value = future.result()
            if signature is not None and signature == _signature(path):
                return deepcopy(value) if kind != 'text' else value
        return _read(path, kind)[1]

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


def begin_startup_data(paths):
    global _active
    end_startup_data()
    _active = StartupData(paths)
    return _active


def end_startup_data():
    global _active
    previous, _active = _active, None
    if previous is not None:
        previous.close()


def read_startup_data(path, kind='json'):
    return _active.read(path, kind) if _active is not None else _read(path, kind)[1]


def startup_modules(root):
    if _active is not None and Path(root).resolve() == _active.root:
        return deepcopy(_active.catalog.result())
    return None
