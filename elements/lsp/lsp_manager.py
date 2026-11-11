"""LSP clients belong to loaded, activated documents, not to visible tabs."""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

from PySide6.QtCore import QObject, Signal, QSaveFile, QIODevice, QProcess
from .lsp_client import LspClient, relocated_lsp_path, server_command_path


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    target = QSaveFile(str(path))
    if not target.open(QIODevice.WriteOnly):
        raise OSError(target.errorString())
    if target.write(text.encode('utf-8')) < 0 or not target.commit():
        raise OSError(target.errorString())


class LspManager(QObject):
    diagnostics_updated = Signal(str, list)
    settings_changed = Signal()

    def __init__(self, parent=None, config_path=None):
        super().__init__(parent)
        from core.paths import APP_PATHS
        self.config_path = Path(config_path or APP_PATHS.lsp / 'lsp_config.json')
        if config_path is None and not self.config_path.exists():
            from core.private_files import file_lock
            with file_lock(APP_PATHS.runtime / 'lsp-config.lock'):
                if not self.config_path.exists():
                    defaults = Path(__file__).with_name('lsp_defaults.json').read_text(encoding='utf-8')
                    _atomic_text(self.config_path, defaults)
        self.servers_config = self._load_config()
        self.active_clients = {}  # (server, workspace) -> client
        self.documents = {}       # only documents activated at least once
        self.diagnostics_cache = {}
        self._raw_diagnostics = {}
        self._dictionaries = {}
        self._retiring = set()
        self._failures = {}  # bounded retry history for currently open workspaces

    @staticmethod
    def _path(path):
        return os.path.normcase(os.path.abspath(path))

    def _load_config(self):
        try:
            return json.loads(self.config_path.read_text(encoding='utf-8')).get('servers', {})
        except (OSError, ValueError, TypeError):
            return {}

    def command_path(self, name):
        conf = self.servers_config[name]
        command = conf.get('command_windows' if os.name == 'nt' else 'command', conf.get('command', ''))
        return server_command_path(command, self.config_path.parent)

    def dictionary_path(self, name):
        conf = self.servers_config[name]
        directory = conf.get('dictionary_directory')
        if directory:
            directory = relocated_lsp_path(directory)
            if not directory.is_absolute():
                directory = self.config_path.parent / directory
        else:
            directory = self.command_path(name).parent
        return directory / (name + '.dictionary.txt')

    def installed_servers(self, category):
        return [(name, conf) for name, conf in self.servers_config.items()
                if conf.get('category') == category and self.command_path(name).is_file()]

    def dictionary_words(self, name):
        if name not in self._dictionaries:
            try:
                words = self.dictionary_path(name).read_text(encoding='utf-8').splitlines()
            except FileNotFoundError:
                words = []
            self._dictionaries[name] = sorted(set(w.strip() for w in words if w.strip()), key=str.casefold)
        return list(self._dictionaries[name])

    def set_dictionary(self, name, words):
        cleaned = sorted(set(str(w).strip() for w in words if str(w).strip()), key=str.casefold)
        if any('\n' in w or '\r' in w or any(c.isspace() for c in w) for w in cleaned):
            raise ValueError('В словаре должны быть отдельные слова, по одному на строку.')
        _atomic_text(self.dictionary_path(name), '\n'.join(cleaned) + ('\n' if cleaned else ''))
        self._dictionaries[name] = cleaned
        # Native dictionaries may be read only at initialization. Restart just
        # this server; reopen all its loaded documents with their latest text.
        for key in list(self.active_clients):
            if key[0] == name:
                self._retire(key)
        for path, document in list(self.documents.items()):
            if self._get_server_name_for_file(path) == name:
                self.ensure_server(path, document['root'])
        for path, diagnostics in list(self._raw_diagnostics.items()):
            self._publish(path, diagnostics)
        self.settings_changed.emit()

    def add_exception(self, name, word):
        self.set_dictionary(name, [*self.dictionary_words(name), word])

    def set_enabled(self, name, enabled):
        data = json.loads(self.config_path.read_text(encoding='utf-8'))
        data['servers'][name]['enabled'] = bool(enabled)
        _atomic_text(self.config_path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        self.servers_config = data['servers']
        if not enabled:
            for key in list(self.active_clients):
                if key[0] == name:
                    self._retire(key)
            for path, diagnostics in list(self._raw_diagnostics.items()):
                self._publish(path, [d for d in diagnostics if d.get('_server') != name])
        else:
            for path, document in list(self.documents.items()):
                if self._get_server_name_for_file(path) == name:
                    self.ensure_server(path, document['root'])
        self.settings_changed.emit()

    def _get_server_name_for_file(self, path):
        ext = Path(path).suffix.lower()
        for name, conf in self.servers_config.items():
            if conf.get('enabled', True) and ext in conf.get('file_extensions', []):
                return name
        return None

    @staticmethod
    def _uri_to_path(uri):
        path = unquote(urlparse(uri).path)
        if os.name == 'nt' and len(path) > 2 and path[0] == '/' and path[2] == ':':
            path = path[1:]
        return LspManager._path(path)

    def diagnostic_word(self, file_path, diagnostic):
        name = diagnostic.get('_server')
        if self.servers_config.get(name, {}).get('category') != 'linguistic':
            return None
        code = str(diagnostic.get('code', '')).casefold()
        message = str(diagnostic.get('message', '')).casefold()
        if not any(s in code + ' ' + message for s in (
                'spell', 'dictionary', 'unknown word', 'unrecognized word', 'орфограф', 'неизвестное слово')):
            return None
        document = self.documents.get(self._path(file_path))
        if not document:
            return None
        start = diagnostic.get('range', {}).get('start', {})
        end = diagnostic.get('range', {}).get('end', {})
        if start.get('line') != end.get('line'):
            return None
        lines = document['lines']
        line = int(start.get('line', -1))
        if not 0 <= line < len(lines):
            return None
        # LSP and Qt both count UTF-16 code units, including surrogate pairs.
        encoded = lines[line].encode('utf-16-le')
        word = encoded[int(start.get('character', 0))*2:int(end.get('character', 0))*2].decode('utf-16-le', errors='replace')
        if word and re.fullmatch(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", word, re.UNICODE):
            return name, word
        return None

    def _publish(self, path, diagnostics):
        self._raw_diagnostics[path] = diagnostics
        filtered = []
        word_sets = {}
        for diagnostic in diagnostics:
            item = self.diagnostic_word(path, diagnostic)
            if item:
                if item[0] not in word_sets:
                    word_sets[item[0]] = {w.casefold() for w in self.dictionary_words(item[0])}
                if item[1].casefold() in word_sets[item[0]]:
                    continue
            filtered.append(diagnostic)
        self.diagnostics_cache[path] = filtered
        self.diagnostics_updated.emit(path, filtered)

    def _on_diagnostics(self, key, client, uri, diagnostics):
        if self.active_clients.get(key) is not client:
            return  # late results from an evicted server must not reappear
        path = self._uri_to_path(uri)
        if path in self.documents:
            self._publish(path, [{**d, '_server': key[0]} for d in diagnostics])

    def _find_workspace_venv_python(self, root):
        for suffix in ('.venv/bin/python', 'venv/bin/python', '.venv/Scripts/python.exe', 'venv/Scripts/python.exe'):
            path = Path(root) / suffix
            if path.is_file():
                return str(path)
        return None

    def ensure_server(self, file_path, workspace_root):
        name = self._get_server_name_for_file(file_path)
        if not name:
            return None
        key = (name, self._path(workspace_root))
        if key not in self.active_clients:
            now = time.monotonic()
            failures = [stamp for stamp in self._failures.get(key, []) if now - stamp < 60]
            self._failures[key] = failures
            if failures and (now - failures[-1] < 5 or len(failures) >= 3):
                return None
            conf = dict(self.servers_config[name])
            command_key = 'command_windows' if os.name == 'nt' and 'command_windows' in conf else 'command'
            conf[command_key] = str(self.command_path(name))
            if name == 'harper_ls':
                conf['settings'] = {'harper-ls': {**conf.get('settings', {}).get('harper-ls', {}),
                                                 'userDictPath': str(self.dictionary_path(name).absolute())}}
            client = LspClient(name, conf, self)
            if name == 'pylsp':
                client.user_python_env = self._find_workspace_venv_python(workspace_root)
            self.active_clients[key] = client
            client.diagnostics_received.connect(lambda uri, ds, k=key, c=client: self._on_diagnostics(k, c, uri, ds))
            client.server_stopped.connect(lambda _, k=key, c=client: self._on_client_stopped(k, c))
            client.start(workspace_root)
            for path, document in self.documents.items():
                if self._get_server_name_for_file(path) == name and document['root'] == key[1]:
                    client.did_open(path, self._language_id(path), document['text'])
        return self.active_clients.get(key)

    def _on_client_stopped(self, key, client):
        if self.active_clients.get(key) is not client:
            return  # Retired clients are owned by _retire until their process exits.
        self.active_clients.pop(key, None)
        if not client._stopping:
            now = time.monotonic()
            self._failures[key] = [stamp for stamp in self._failures.get(key, []) if now - stamp < 60] + [now]
        for path, document in self.documents.items():
            if (self._get_server_name_for_file(path), document['root']) == key:
                self._raw_diagnostics.pop(path, None)
                self.diagnostics_cache.pop(path, None)
                self.diagnostics_updated.emit(path, [])
        client.deleteLater()

    @staticmethod
    def _language_id(path):
        return {'.py': 'python', '.md': 'markdown', '.markdown': 'markdown'}.get(Path(path).suffix.lower(), 'plaintext')

    def notify_open(self, file_path, text, workspace_root):
        path = self._path(file_path)
        existing = path in self.documents
        self.documents[path] = {'text': text, 'lines': text.split('\n'), 'root': self._path(workspace_root), 'version': 1}
        name = self._get_server_name_for_file(path)
        key = (name, self._path(workspace_root))
        running = key in self.active_clients
        client = self.ensure_server(path, workspace_root)
        if client and running and not existing:
            client.did_open(path, self._language_id(path), text)
        elif client and existing:
            self.notify_change(path, text, 1)

    def notify_change(self, file_path, text, version):
        path = self._path(file_path)
        document = self.documents.get(path)
        if not document:
            return
        document.update(text=text, lines=text.split('\n'), version=version)
        key = (self._get_server_name_for_file(path), document['root'])
        if key in self.active_clients:
            self.active_clients[key].did_change(path, text, version)
        else:
            # Reopen current buffers on the next edit, with a crash-loop limit.
            self.ensure_server(path, document['root'])

    def notify_save(self, file_path, text):
        path = self._path(file_path)
        document = self.documents.get(path)
        if document:
            key = (self._get_server_name_for_file(path), document['root'])
            if key in self.active_clients:
                self.active_clients[key].did_save(path, text)

    def _retire(self, key):
        client = self.active_clients.pop(key, None)
        if client is not None:
            self._retiring.add(client)
            if client.process is not None:
                def release(*_):
                    self._retiring.discard(client)
                    client.deleteLater()
                client.process.finished.connect(release)
                if client.process.state() == QProcess.NotRunning:
                    release()
            else:
                self._retiring.discard(client)
                client.deleteLater()
            client.stop_async()

    def notify_close(self, file_path):
        path = self._path(file_path)
        document = self.documents.pop(path, None)
        if document:
            key = (self._get_server_name_for_file(path), document['root'])
            if key in self.active_clients:
                self.active_clients[key].did_close(path)
            if not any((self._get_server_name_for_file(p), d['root']) == key for p, d in self.documents.items()):
                self._retire(key)
                self._failures.pop(key, None)
        self._raw_diagnostics.pop(path, None)
        self.diagnostics_cache.pop(path, None)
        self.diagnostics_updated.emit(path, [])

    def shutdown_all(self):
        clients = [*self.active_clients.values(), *self._retiring]
        self.active_clients.clear()
        for client in clients:
            client.stop()
        self._retiring.clear()
        self._failures.clear()


lsp_manager = LspManager()
