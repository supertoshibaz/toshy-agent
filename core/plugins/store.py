"""Versioned plugin storage. Install/remove never change Toshy's source tree."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from threading import RLock

from .manifest import PLUGIN_ID, PluginError, validate_manifest
from .package import MAX_BYTES, extract_package, package_digest


class PluginStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = RLock()

    def _managed(self, *parts):
        target = self.root
        for part in parts:
            target = target / part
            if target.is_symlink():
                raise PluginError('Plugin storage paths cannot be symbolic links.')
        return target

    @contextmanager
    def _write_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock, (self.root / '.store-lock').open('a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b'\0'); stream.flush()
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def state(self):
        path = self.root / 'state.json'
        if not path.exists():
            return dict(schema_version=1, order=[], enabled=[], versions={})
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('schema_version') != 1:
                raise PluginError('Unsupported plugin store state version.')
            versions = value['versions']
            if not isinstance(versions, dict) or any(not re.fullmatch(PLUGIN_ID, key) for key in versions):
                raise PluginError('Invalid installed plugin identifiers.')
            for version in versions.values():
                if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+)*', version):
                    raise PluginError('Invalid selected plugin version.')
            for key in ('order', 'enabled'):
                if not isinstance(value[key], list) or len(value[key]) != len(set(value[key])) or set(value[key]) - versions.keys():
                    raise PluginError(f'Invalid plugin {key}.')
            if set(value['order']) != versions.keys():
                raise PluginError('Every installed plugin must have a priority position.')
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise PluginError(f'Cannot read plugin store state: {error}') from error

    def _save(self, state):
        descriptor, filename = tempfile.mkstemp(prefix='.state-', suffix='.json', dir=self.root)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                json.dump(state, target, ensure_ascii=False, indent=2)
                target.flush(); os.fsync(target.fileno())
            os.replace(filename, self.root / 'state.json')
        finally:
            Path(filename).unlink(missing_ok=True)

    def install(self, archive, *, expected_sha256=None, expected_plugin_id=None,
                expected_version=None, source=None, expected_state=None):
        if Path(archive).stat().st_size > MAX_BYTES:
            raise PluginError('Plugin archive exceeds 256 MiB.')
        with self._write_lock(), tempfile.TemporaryDirectory(prefix='.install-', dir=self.root) as temporary:
            if expected_state is not None and self.state() != expected_state:
                raise PluginError('Installed plugins changed during the update. Check updates again.')
            # A single private byte snapshot is checked and extracted, preventing
            # a caller from changing the source archive between those operations.
            snapshot = Path(temporary) / 'source.zip'
            shutil.copyfile(archive, snapshot)
            digest = package_digest(snapshot)
            if expected_sha256 is not None and digest != expected_sha256.lower():
                raise PluginError('Plugin archive SHA-256 does not match the catalog.')
            stage = Path(temporary) / 'payload'
            manifest = extract_package(snapshot, stage)
            identity, version = manifest['id'], manifest['version']
            if ((expected_plugin_id is not None and identity != expected_plugin_id)
                    or (expected_version is not None and version != expected_version)):
                raise PluginError('Пакет не соответствует выбранному плагину или версии.')
            # Canonical final versions are portable installation directory names.
            if not re.fullmatch(r'\d+(?:\.\d+)*', version):
                raise PluginError('Plugin storage requires a final numeric release version.')
            state = self.state()
            target = self._managed('packages', identity, version)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent.is_symlink() or target.is_symlink():
                raise PluginError('Plugin storage paths cannot be symbolic links.')
            created = not target.exists()
            if not created:
                try:
                    existing = json.loads((target / '.toshy-receipt.json').read_text(encoding='utf-8'))
                except (OSError, ValueError) as error:
                    raise PluginError('Existing plugin version has no valid installation receipt.') from error
                if existing.get('sha256') != digest:
                    raise PluginError('This plugin version already exists with different bytes. Publish a new version.')
            else:
                receipt = {'sha256': digest}
                if identity in state['versions']:
                    receipt['previous_version'] = state['versions'][identity]
                if source is not None:
                    receipt['source'] = str(source)[:2048]
                (stage / '.toshy-receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
                os.replace(stage, target)
            state['versions'][identity] = version
            if identity not in state['order']:
                state['order'].append(identity)
            # New packages are disabled. An upgrade preserves the user's flag;
            # the compatibility planner must check the new version before use.
            try:
                self._save(state)
            except Exception:
                if created:
                    shutil.rmtree(target)
                raise
            return manifest

    def manifests(self, state=None):
        state = self.state() if state is None else state
        result = []
        for identity in state['order']:
            directory = self._managed('packages', identity, state['versions'][identity])
            if directory.is_symlink() or directory.parent.is_symlink():
                raise PluginError('Plugin storage paths cannot be symbolic links.')
            value = validate_manifest(json.loads((directory / 'plugin.json').read_text(encoding='utf-8')))
            if value['id'] != identity or value['version'] != state['versions'][identity]:
                raise PluginError('Installed manifest does not match its storage identity.')
            result.append(value)
        return result

    def reorder(self, identities):
        identities = list(identities)
        with self._write_lock():
            state = self.state()
            if len(identities) != len(set(identities)) or set(identities) != state['versions'].keys():
                raise PluginError('Priority order must contain every installed plugin exactly once.')
            state['order'] = identities
            self._save(state)

    def set_enabled(self, identity, enabled):
        with self._write_lock():
            state = self.state()
            if identity not in state['versions']:
                raise PluginError(f'Unknown installed plugin: {identity}')
            chosen = set(state['enabled'])
            if enabled:
                chosen.add(identity)
            else:
                chosen.discard(identity)
            state['enabled'] = [item for item in state['order'] if item in chosen]
            from .module_config import synchronize_modules
            synchronize_modules(self, state, commit=lambda: self._save(state))

    def reconcile_module_config(self):
        """Repair selections left by older versions, including blocked dependents."""
        with self._write_lock():
            from .module_config import synchronize_modules
            synchronize_modules(self, self.state())

    def data_directory(self, identity):
        """Resolve this plugin's private data without creating it."""
        if not isinstance(identity, str) or not re.fullmatch(PLUGIN_ID, identity):
            raise PluginError('Invalid plugin identifier.')
        return self._managed('data', identity)

    def remove(self, identity, *, keep_data=True):
        """Remove registration/code, optionally deleting private plugin data."""
        with self._write_lock():
            state = self.state()
            if identity not in state['versions']:
                raise PluginError(f'Unknown installed plugin: {identity}')
            manifests = self.manifests(state)
            target = self._managed('packages', identity)
            if target.is_symlink():
                raise PluginError('Plugin storage paths cannot be symbolic links.')
            data = self.data_directory(identity) if not keep_data else None
            with tempfile.TemporaryDirectory(prefix='.remove-', dir=self.root) as temporary:
                staged = Path(temporary) / 'package'
                staged_data = Path(temporary) / 'data'
                try:
                    if target.exists():
                        os.replace(target, staged)
                    if data is not None and data.exists():
                        os.replace(data, staged_data)
                    state['versions'].pop(identity)
                    state['order'].remove(identity)
                    state['enabled'] = [item for item in state['enabled'] if item != identity]
                    from .module_config import synchronize_modules
                    synchronize_modules(self, state, manifests=manifests, commit=lambda: self._save(state))
                except Exception:
                    if staged_data.exists():
                        os.replace(staged_data, data)
                    if staged.exists():
                        os.replace(staged, target)
                    raise
