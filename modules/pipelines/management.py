"""Module-aware pipeline library operations with recoverable folder moves."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import re
import shutil
import uuid

from core.paths import APP_PATHS
from core.private_files import atomic_private, file_lock
from .packages import PackageStore
from .storage import create_empty_pipeline, load_pipeline_script
from .contracts import dependencies


class LibraryError(ValueError):
    def __init__(self, key, **parameters):
        super().__init__(key)
        self.key, self.parameters = key, parameters


def package_owner(path, store=None):
    store = store or PackageStore()
    path = Path(path).resolve()
    for data in store.list():
        for pid in data.get('pipelines', {}):
            try:
                if store.resolve(data['id'], pid).resolve() == path:
                    return data['id'], pid
            except (OSError, ValueError, KeyError):
                continue
    return None, None


class PipelineLibrary:
    def __init__(self, root=None, store=None, runtime=None):
        self.root = Path(root or APP_PATHS.modules / 'pipelines').resolve()
        self.store = store or PackageStore()
        self.runtime = Path(runtime or APP_PATHS.runtime / 'pipeline-library')
        self.root.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.recover()

    def groups(self):
        groups = [{'id': None, 'name': '', 'pipelines': [
            {'path': p, 'name': p.name, 'mode': ''} for p in sorted(self.root.iterdir(), key=lambda p: p.name.casefold())
            if not p.name.startswith('.') and p.is_dir() and not p.is_symlink() and (p / 'pipeline.py').is_file()]}]
        for manifest in sorted(self.store.list(), key=lambda m: str(m.get('name') or m['id']).casefold()):
            entries = []
            pipelines = manifest.get('pipelines') or {}
            for pid, item in (pipelines.items() if isinstance(pipelines, dict) else ()):
                if not isinstance(item, dict):
                    continue
                try:
                    path = self.store.resolve(manifest['id'], pid)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                entries.append(dict(item, path=path, id=pid, name=str(item.get('name') or pid)))
            groups.append({'id': manifest['id'], 'name': str(manifest.get('name') or manifest['id']),
                           'pipelines': sorted(entries, key=lambda p: p.get('name', '').casefold())})
        return groups

    def locate(self, path):
        path = Path(path).resolve()
        for group in self.groups():
            for item in group['pipelines']:
                if item['path'].resolve() == path:
                    return group['id'], item.get('id'), item
        raise LibraryError('pipelines.library.unknown')

    def _plain_path(self, path):
        path = Path(path)
        for part in [path, *path.parents]:
            if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('Pipeline paths cannot contain links or junctions.')
        path = path.resolve()
        allowed = [self.root, *(self.store.path(m['id']) / 'pipelines' for m in self.store.list())]
        if not any(path.is_relative_to(base.resolve()) and path != base.resolve() for base in allowed):
            raise ValueError('Pipeline path is outside the library.')
        return path

    def _metadata(self, modules):
        result = {}
        for module in modules:
            if module is None:
                continue
            directory = self.store.path(module)
            for path in [directory / 'module.json', directory / 'ui/config.json']:
                if any(p.is_symlink() or (p.exists() and getattr(p.stat(), 'st_file_attributes', 0) & 0x400)
                       for p in (path, *path.parents)):
                    raise ValueError('Module metadata cannot contain links or junctions.')
                result[str(path)] = base64.b64encode(path.read_bytes()).decode('ascii') if path.exists() else None
        return result

    def _validated_metadata(self, saved):
        if not isinstance(saved, dict):
            raise ValueError('Invalid module metadata in move journal.')
        allowed = {str(self.store.path(m['id']) / rel) for m in self.store.list()
                   for rel in ('module.json', 'ui/config.json')}
        result = []
        for filename, value in saved.items():
            if filename not in allowed:
                raise ValueError('Unexpected module metadata in move journal.')
            path = Path(filename)
            if any(p.is_symlink() or (p.exists() and getattr(p.stat(), 'st_file_attributes', 0) & 0x400)
                   for p in (path, *path.parents)):
                raise ValueError('Module metadata cannot contain links or junctions.')
            if value is not None and not isinstance(value, str):
                raise ValueError('Invalid encoded module metadata.')
            result.append((path, None if value is None else base64.b64decode(value, validate=True)))
        return result

    def _restore_metadata(self, saved):
        for path, value in self._validated_metadata(saved):
            if value is None:
                path.unlink(missing_ok=True)
            else:
                atomic_private(path, value)

    def _rollback(self, record):
        source, target = self._plain_path(record['source']), self._plain_path(record['target'])
        self._validated_metadata(record['metadata'])
        if target.exists():
            if source.exists():
                raise RuntimeError('Both pipeline locations exist; files were preserved for recovery.')
            target.rename(source)
        elif not source.exists():
            raise RuntimeError('Pipeline move cannot be recovered: both locations are missing.')
        self._restore_metadata(record['metadata'])

    def recover(self):
        self.recovery_errors = []
        with file_lock(self.runtime / 'library.lock'):
            for journal in self.runtime.glob('move-*.json'):
                try:
                    record = json.loads(journal.read_text(encoding='utf-8'))
                    if not isinstance(record, dict) or record.get('version') != 1:
                        raise ValueError('Unknown pipeline move journal version.')
                    if record.get('status') != 'committed':
                        self._rollback(record)
                    journal.unlink()
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    self.recovery_errors.append(str(error))
                    from core.logger import core_logger
                    core_logger.event('pipelines', 'PIPELINE_MOVE_RECOVERY_ERROR', str(error), level='ERROR')

    def _move_transaction(self, source, target, modules, commit):
        if self.recovery_errors:
            raise LibraryError('pipelines.library.recovery_required')
        source, target = self._plain_path(source), self._plain_path(target)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        journal = self.runtime / ('move-' + uuid.uuid4().hex + '.json')
        record = {'version': 1, 'status': 'prepared', 'source': str(source), 'target': str(target),
                  'metadata': self._metadata(modules)}
        atomic_private(journal, json.dumps(record).encode('utf-8'))
        try:
            # An atomic rename avoids copying assets and never replaces a file.
            # Different filesystems fail before metadata is changed.
            source.rename(target)
            commit()
            record['status'] = 'committed'
            atomic_private(journal, json.dumps(record).encode('utf-8'))
        except BaseException:
            try:
                self._rollback(record)
            except BaseException as error:
                self.recovery_errors.append(str(error))
                raise
            journal.unlink()
            raise
        journal.unlink()

    @staticmethod
    def _refresh_dependencies(data):
        data['dependencies'] = sorted({d for p in data.get('pipelines', {}).values()
                                       for d in p.get('dependencies', []) if d != data['id']})

    def _available_name(self, directory, name):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip(' .') or 'Untitled'
        if name.casefold() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}:
            name = '_' + name
        names = {p.name.casefold() for p in directory.iterdir()}
        result, index = name[:100], 2
        while result.casefold() in names:
            result = name[:90] + '_' + str(index)
            index += 1
        return directory / result

    def create(self, module=None):
        with file_lock(self.runtime / 'library.lock'):
            data = self.store.read(module) if module is not None else None
            names = {p['name'] for g in self.groups() if g['id'] == module for p in g['pipelines']}
            index = 1
            while f'Untitled_{index}' in names:
                index += 1
            name = f'Untitled_{index}'
            while module is None and (self.root / name).exists():
                index += 1
                name = f'Untitled_{index}'
            pid = 'pipeline_' + uuid.uuid4().hex[:12]
            path = self.root / name if module is None else self.store.path(module) / 'pipelines' / pid
            self._plain_path(path)
            snapshots = self._metadata([module])
            temporary = path.with_name('.new-' + uuid.uuid4().hex)
            moved = False
            try:
                definition = create_empty_pipeline(temporary, name)
                if path.exists():
                    raise FileExistsError(path)
                temporary.rename(path)
                moved = True
                if data is not None:
                    data.setdefault('pipelines', {})[pid] = {'name': name, 'description': '', 'path': f'pipelines/{pid}',
                                                           'mode': 'user', 'dependencies': dependencies(definition)}
                    self._refresh_dependencies(data)
                    self.store.write(module, data)
            except BaseException:
                self._restore_metadata(snapshots)
                if moved and path.is_dir():
                    shutil.rmtree(path)
                if temporary.is_dir():
                    shutil.rmtree(temporary)
                raise
            return path

    def _check_references(self, source_module, pid, definition, destination):
        if source_module == destination:
            return
        if any(n.type_id == 'pipeline.call' and n.settings.get('pipeline') for n in definition.nodes):
            raise LibraryError('pipelines.library.nested_move')
        if source_module is not None:
            manifest = self.store.read(source_module)
            for other_id, item in manifest.get('pipelines', {}).items():
                if other_id == pid:
                    continue
                graph = load_pipeline_script(self.store.resolve(source_module, other_id) / 'pipeline.py')
                if any(n.type_id == 'pipeline.call' and n.settings.get('pipeline') == pid for n in graph.nodes):
                    raise LibraryError('pipelines.library.referenced', name=item['name'])

    def move(self, path, destination=None):
        with file_lock(self.runtime / 'library.lock'):
            source = Path(path).resolve()
            source_module, pid, item = self.locate(source)
            if source_module == destination:
                return source
            definition = load_pipeline_script(source / 'pipeline.py')
            self._check_references(source_module, pid, definition, destination)
            manifests = {m: copy.deepcopy(self.store.read(m)) for m in (source_module, destination) if m is not None}
            if destination is None:
                target = self._available_name(self.root, item['name'])
            else:
                new_id = pid or 'pipeline_' + uuid.uuid4().hex[:12]
                if new_id in manifests[destination].get('pipelines', {}) or (self.store.path(destination) / 'pipelines' / new_id).exists():
                    new_id = 'pipeline_' + uuid.uuid4().hex[:12]
                target = self.store.path(destination) / 'pipelines' / new_id
                entry = {k: copy.deepcopy(v) for k, v in item.items() if k not in {'id', 'path'}}
                entry.update(path=f'pipelines/{new_id}', mode=item.get('mode') or 'user',
                             dependencies=[d for d in dependencies(definition) if d != destination])
                manifests[destination].setdefault('pipelines', {})[new_id] = entry
            if source_module is not None:
                manifests[source_module]['pipelines'].pop(pid)
            def commit():
                for module, data in manifests.items():
                    self._refresh_dependencies(data)
                    self.store.write(module, data)
            self._move_transaction(source, target, manifests, commit)
            return target

    def set_mode(self, path, mode):
        from .packages import MODES
        if mode not in MODES:
            raise ValueError('Unknown pipeline mode.')
        module, pid, _ = self.locate(path)
        if module is None:
            raise LibraryError('pipelines.library.unknown')
        data = self.store.read(module)
        data['pipelines'][pid]['mode'] = mode
        self.store.write(module, data)

    def remove(self, path):
        with file_lock(self.runtime / 'library.lock'):
            source = Path(path).resolve()
            module, pid, _ = self.locate(source)
            definition = load_pipeline_script(source / 'pipeline.py')
            if module is not None:
                # Deleting a caller is safe; deleting its nested target is not.
                check = copy.deepcopy(definition)
                check.nodes = [n for n in check.nodes if n.type_id != 'pipeline.call']
                self._check_references(module, pid, check, None)
            target = source.with_name('.deleted-' + uuid.uuid4().hex)
            def commit():
                if module is not None:
                    data = self.store.read(module)
                    data['pipelines'].pop(pid)
                    self._refresh_dependencies(data)
                    self.store.write(module, data)
            self._move_transaction(source, target, [module], commit)
            shutil.rmtree(target)
