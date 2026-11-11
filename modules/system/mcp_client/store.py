"""Owner-local MCP installations. Descriptors contain no user credentials.

Source coordinates and version policy are kept separately from local launch data
so a future plugin importer can rebuild installations on another platform.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path

from core.paths import APP_PATHS

GLOBAL_OWNER = "global"
VISIBILITIES = {"model", "pipeline"}
_lock = threading.RLock()
_secrets = {}
_SERVICE = "Toshy MCP instances"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def secret_key(owner, identity, field):
    return f"{owner}/{identity}/{field}"


def set_secret(owner, identity, field, value):
    key = secret_key(owner, identity, field)
    # The existing OS vault policy rejects plaintext keyring backends.
    from core.model_connections import _secure_keyring
    with _lock:
        if value:
            _secrets[key] = str(value)
        else:
            _secrets.pop(key, None)
        try:
            backend = _secure_keyring()
            if value:
                backend.set_password(_SERVICE, key, str(value))
            else:
                try:
                    backend.delete_password(_SERVICE, key)
                except Exception:
                    pass
            return "os"
        except Exception:
            return "runtime"


def get_secret(owner, identity, field):
    key = secret_key(owner, identity, field)
    with _lock:
        if key in _secrets:
            return _secrets[key]
    try:
        from core.model_connections import _secure_keyring
        return _secure_keyring().get_password(_SERVICE, key) or ""
    except Exception:
        return ""


def tool_name(owner, identity, original):
    digest = hashlib.sha256(f"{owner}\0{identity}\0{original}".encode()).hexdigest()[:20]
    hint = re.sub(r"[^A-Za-z0-9_]", "_", original)[:35]
    return f"mcp_{digest}_{hint}"


class McpStore:
    def __init__(self, modules=None):
        self.modules = Path(modules or APP_PATHS.modules).resolve()

    def root(self, owner):
        if owner == GLOBAL_OWNER:
            raise ValueError('MCP-серверы устанавливаются только в кастомные модули.')
        if not isinstance(owner, str) or not owner.isidentifier():
            raise ValueError("Неверный владелец MCP")
        if self.modules == APP_PATHS.modules.resolve():
            from core.plugins.modules import module_directory
            module = module_directory('custom', owner)
        else:
            module = self.modules / 'custom' / owner
        if not (module / "__init__.py").is_file():
            raise ValueError("Кастомный модуль не найден")
        return module / "mcp"

    def path(self, owner, identity):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(identity)):
            raise ValueError("Неверный ID MCP-сервера")
        return self.root(owner) / identity

    def read(self, owner, identity):
        value = json.loads((self.path(owner, identity) / "server.json").read_text(encoding="utf-8"))
        if value.get("id") != identity or value.get("schema_version") != 1:
            raise ValueError("Некорректное описание MCP-сервера")
        return value

    def write(self, owner, record):
        if record.get("visibility", "model") not in VISIBILITIES:
            raise ValueError("Неизвестный режим доступа MCP")
        with _lock:
            atomic_json(self.path(owner, record["id"]) / "server.json", record)
            self.sync_owner(owner)

    def sync_owner(self, owner):
        if owner != GLOBAL_OWNER:
            path = self.root(owner).parent / 'module.json'
            if path.is_file():
                from modules.pipelines.packages import PackageStore
                PackageStore(self.modules / 'custom').write(owner, json.loads(path.read_text(encoding='utf-8')))

    def owners(self):
        values = []
        if self.modules == APP_PATHS.modules.resolve():
            from core.plugins.modules import module_locations
            folders = [(name, path) for (_, name), path in module_locations(category='custom').items()]
        else:
            folders = [(p.name, p) for p in sorted((self.modules / 'custom').glob('*'))]
        for name, folder in folders:
            if not folder.is_dir() or not (folder / '__init__.py').is_file():
                continue
            try:
                data = json.loads((folder / "module.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            values.append({"id": name, "name": data.get("name") or name})
        return values

    def records(self, owner=None):
        result = []
        owners = [owner] if owner is not None else [x["id"] for x in self.owners()]
        for identity in owners:
            for path in sorted(self.root(identity).glob("*/server.json")):
                try:
                    result.append((identity, self.read(identity, path.parent.name)))
                except (OSError, ValueError):
                    continue
        return result

    def create(self, owner, name, source):
        identity = "s_" + uuid.uuid4().hex[:16]
        record = {"schema_version": 1, "id": identity, "name": name or "MCP",
                  "source": source, "visibility": "model", "autostart": False,
                  "installation": "pending", "command": "", "args": [], "env": {},
                  "transport": "stdio", "url": "", "headers": {},
                  "user_config": {}, "values": {}, "tools": [], "disabled_tools": [], "error": "",
                  "reconnect": True, "reconnect_attempts": 5, "sampling": True, "elicitation": True, "roots": True}
        self.write(owner, record)
        return record

    def remove(self, owner, identity):
        """Delete only this managed installation, never its external source."""
        with _lock:
            directory = self.path(owner, identity)
            module = self.modules / 'custom' / owner
            if any(path.is_symlink() or path.is_junction() for path in (module, self.root(owner), directory)):
                raise ValueError('Нельзя удалять установку MCP через ссылку на другую папку.')
            record = self.read(owner, identity)
            def readonly_error(function, path, error):
                target = Path(path)
                if (not isinstance(error, PermissionError) or target.is_symlink()
                        or not target.resolve().is_relative_to(directory.resolve())):
                    raise error
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
                function(path)
            # Git checkouts on Windows can contain read-only objects.
            try:
                shutil.rmtree(directory, onexc=readonly_error)
            except OSError:
                # Keep a failed/partial uninstall visible and retryable.
                if directory.is_dir():
                    atomic_json(directory / 'server.json', record)
                raise
            for field in [*record.get('secret_fields', []), 'oauth:tokens', 'oauth:client']:
                set_secret(owner, identity, field, '')
            self.sync_owner(owner)

    def public(self, owner, record):
        value = copy.deepcopy(record)
        # Secrets are references, never returned to a GUI or model.
        value["credential_fields"] = {key: bool(get_secret(owner, record["id"], key))
            for key in record.get("secret_fields", [])}
        return value
