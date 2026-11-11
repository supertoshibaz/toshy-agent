"""Trusted Python plugins, loaded under private package names.

Only the manager composes registered hooks. This is not a Python sandbox.
Code changes take effect after application restart; a frozen snapshot is shared
with the core process so toggling a tile cannot change an ongoing session.
"""
import asyncio
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import importlib.util
import inspect
import json
import tempfile
import os
from pathlib import Path
import sys
import types
import uuid
from packaging.version import Version
from .contracts import host_targets
from .manifest import PluginError
from .resolver import resolve_plugins
from .store import PluginStore

SNAPSHOT_ENV = 'TOSHY_PLUGIN_SNAPSHOT'
_current = ContextVar('toshy_plugin_runtime', default=None)
_process_runtime = None


def _log(message):
    from core.logger import core_logger
    core_logger.event('app', 'PLUGIN_EVENT', message, level='WARN')


@dataclass
class PluginContext:
    runtime: object
    plugin_id: str
    component_id: str
    host: object = None
    parent: object = None
    default_factory: object = None
    data: object = None

    @property
    def app(self):
        return self.host

    @property
    def plugin_dir(self):
        return self.runtime.directory(self.plugin_id)

    @property
    def data_dir(self):
        directory = self.runtime.store.root / 'data' / self.plugin_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def register_cleanup(self, callback):
        if not callable(callback):
            raise TypeError('Cleanup must be callable.')
        self.runtime.cleanups[self.component_id].append(callback)


class PluginRuntime:
    def __init__(self, store, *, domain='ui', state=None, targets=None):
        self.store = store
        self.domain = domain
        self.state = json.loads(json.dumps(store.state() if state is None else state))
        self.manifests = store.manifests(self.state)
        self.by_id = {m['id']: m for m in self.manifests}
        self.components = {m['id'] + '/' + c['id']: c for m in self.manifests for c in m['components']}
        self.targets = host_targets() if targets is None else targets
        self.excluded = {}
        self.plan = self.resolve()
        self.cleanups = defaultdict(list)
        self.callbacks = {}
        self.modules = {}
        self.objects = {}
        self.runtime_errors = defaultdict(list)
        self.started = set()
        self.closed = False
        self.cleanup_tasks = set()
        self.snapshot_path = None
        self._inside = ContextVar('toshy_plugin_hooks_' + uuid.uuid4().hex, default=frozenset())

    def resolve(self):
        return resolve_plugins(self.manifests, self.state['order'], self.state['enabled'],
            targets=self.targets, excluded=self.excluded)

    @property
    def pending_restart(self):
        return self.state != self.store.state()

    def directory(self, identity):
        return self.store._managed('packages', identity, self.state['versions'][identity])

    def selected(self, key):
        return key in self.plan.plugins[key.split('/')[0]].active_components

    def component_domain(self, c):
        return 'ui' if c['type'] in ('widget', 'workspace', 'topbar') or c.get('target', '').startswith('ui.') else 'core'

    def active(self, *, mode=None, domain=None):
        for p in self.plan.order:
            for key in self.plan.plugins[p].active_components:
                c = self.components[key]
                if (mode is None or c['mode'] == mode) and (domain is None or self.component_domain(c) == domain):
                    yield key, c

    def activation_order(self, *, mode=None, domain=None):
        selected = dict(self.active())
        visited, visiting, ordered = set(), set(), []
        def visit(key):
            if key in visited or key not in selected:
                return
            if key in visiting:
                raise PluginError('Циклическая зависимость компонентов при активации.')
            visiting.add(key)
            component = selected[key]
            dependencies = list(component.get('requires_components', []))
            for plugin in self.by_id[key.split('/')[0]].get('requires_plugins', {}):
                dependencies.extend(k for k in selected if k.startswith(plugin + '/'))
            for dependency in dependencies:
                visit(dependency)
            visiting.remove(key); visited.add(key); ordered.append(key)
        for key in selected:
            visit(key)
        return [(key, selected[key]) for key in ordered
                if (mode is None or selected[key]['mode'] == mode)
                and (domain is None or self.component_domain(selected[key]) == domain)]

    def factory(self, key):
        identity = key.split('/')[0]
        root = self.directory(identity)
        prefix = '_toshy_plugin_' + hashlib.sha256((str(root) + identity).encode()).hexdigest()[:24]
        if prefix not in sys.modules:
            package = types.ModuleType(prefix)
            package.__path__ = [str(root)]
            package.__package__ = prefix
            package.__spec__ = importlib.util.spec_from_loader(prefix, loader=None, is_package=True)
            sys.modules[prefix] = package
        module, name = self.components[key]['entrypoint'].split(':')
        imported = __import__(prefix + '.' + module, fromlist=[name])
        factory = getattr(imported, name)
        if not callable(factory):
            raise PluginError(f'{key}: entrypoint is not callable.')
        return factory

    def context(self, key, **kwargs):
        return PluginContext(self, key.split('/')[0], key, **kwargs)

    def failure(self, key, error):
        message = f'{key}: {type(error).__name__}: {error}'
        if message not in self.runtime_errors[key]:
            self.runtime_errors[key].append(message)
            _log(message)
        # A failed contribution and its declared dependents/group must stop
        # together. Never silently activate a lower replacement mid-session.
        disabled = {key}
        changed = True
        while changed:
            changed = False
            for other, c in self.components.items():
                if not self.selected(other) or other in disabled:
                    continue
                same_group = c.get('group') and any(k.split('/')[0] == other.split('/')[0]
                    and self.components[k].get('group') == c['group'] for k in disabled)
                dependency = bool(set(c.get('requires_components', [])) & disabled)
                lost_contracts = set()
                for failed in disabled:
                    failed_component = self.components[failed]
                    lost_contracts.update(failed_component.get('provides_contracts', {}))
                    lost_contracts.update(failed_component.get('extension_points', {}))
                    if failed_component['mode'] == 'replace':
                        lost_contracts.add(failed_component['target'])
                contract_dependency = bool(set(c.get('requires_contracts', {})) & lost_contracts)
                if c['mode'] == 'extend' and c.get('target') in lost_contracts:
                    contract_dependency = True
                dependency = dependency or contract_dependency
                plugin_dependency = bool(set(self.by_id[other.split('/')[0]].get('requires_plugins', {})) & {k.split('/')[0] for k in disabled})
                if same_group or dependency or plugin_dependency:
                    disabled.add(other); changed = True
        for other in disabled:
            status = self.plan.plugins[other.split('/')[0]]
            if other in status.active_components:
                status.active_components.remove(other)
            status.state = 'partial' if status.active_components else 'blocked'
            if message not in status.warnings:
                status.warnings.append(message)
            self._cleanup(other)
        self.write_status()

    def prepare(self, host=None):
        """Load handlers before any execution. Slot/widget factories remain lazy."""
        for key, c in self.activation_order(domain=self.domain):
            if not self.selected(key):
                continue
            if c['mode'] == 'extend' and c['operation'] != 'slot':
                try:
                    callback = self.factory(key)(self.context(key, host=host))
                    if not callable(callback):
                        raise TypeError('Hook factory must return a callable.')
                    if self.domain == 'ui' and inspect.iscoroutinefunction(callback):
                        raise TypeError('Qt UI hooks must be synchronous.')
                    self.callbacks[key] = callback
                    self.started.add(key)
                except Exception as error:
                    self.failure(key, error)
            elif c['type'] == 'core' and c['mode'] == 'add':
                try:
                    obj = self.factory(key)(self.context(key, host=host))
                    if inspect.isawaitable(obj):
                        obj.close() if inspect.iscoroutine(obj) else None
                        raise TypeError('Factory must be synchronous; schedule async work in the core.')
                    self.objects[key] = obj
                    if obj is not None and callable(getattr(obj, 'close', None)):
                        self.cleanups[key].append(obj.close)
                    self.started.add(key)
                except Exception as error:
                    self.failure(key, error)

    def listeners(self, target, operation):
        return [(key, self.callbacks[key]) for key in self.plan.extensions.get(target, [])
            if self.selected(key) and self.components[key]['operation'] == operation and key in self.callbacks]

    def emit(self, target, payload):
        if target in self._inside.get():
            return
        token = self._inside.set(self._inside.get() | {target})
        try:
            for key, callback in self.listeners(target, 'listen'):
                try:
                    result = callback(payload)
                    if inspect.isawaitable(result):
                        result.close() if inspect.iscoroutine(result) else None
                        raise TypeError('Use synchronous callbacks for synchronous events.')
                except Exception as error:
                    self.failure(key, error)
        finally:
            self._inside.reset(token)

    async def emit_async(self, target, payload):
        if target in self._inside.get():
            return
        token = self._inside.set(self._inside.get() | {target})
        try:
            for key, callback in self.listeners(target, 'listen'):
                try:
                    result = callback(payload)
                    if inspect.isawaitable(result):
                        await result
                except Exception as error:
                    self.failure(key, error)
        finally:
            self._inside.reset(token)

    async def invoke(self, target, default, payload):
        """before(payload), around(next,payload), filter(result,payload), after(result,payload).

        before/after always belong to the manager, outside the around chain.
        A failed mutating operation is never automatically retried.
        """
        if target in self._inside.get():
            value = default(payload)
            return await value if inspect.isawaitable(value) else value
        token = self._inside.set(self._inside.get() | {target})
        async def call(callback, *args):
            result = callback(*args)
            return await result if inspect.isawaitable(result) else result
        base_errors = []
        async def original(data):
            try:
                return await call(default, data)
            except BaseException as error:
                base_errors.append(error)
                raise
        async def guarded(key, callback, *args):
            try:
                return await call(callback, *args)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if any(error is base for base in base_errors):
                    raise
                self.failure(key, error)
                raise PluginError(f'Ошибка расширения {key}: {error}') from error
        try:
            for key, callback in self.listeners(target, 'before'):
                await guarded(key, callback, payload)
            operation = original
            for key, callback in reversed(self.listeners(target, 'around')):
                following = operation
                async def wrapper(data, key=key, callback=callback, following=following):
                    called = False
                    async def next_once(value):
                        nonlocal called
                        if called:
                            raise PluginError('An operation wrapper may call next_call at most once.')
                        called = True
                        return await following(value)
                    return await guarded(key, callback, next_once, data)
                operation = wrapper
            result = await operation(payload)
            # Higher priority gets the final transformation.
            for key, callback in reversed(self.listeners(target, 'filter')):
                result = await guarded(key, callback, result, payload)
            for key, callback in self.listeners(target, 'after'):
                try:
                    await guarded(key, callback, result, payload)
                except PluginError:
                    # The external action already succeeded. A failed observer
                    # must not report its successful result as a failed action.
                    continue
            return result
        finally:
            self._inside.reset(token)

    def build(self, target, default_factory, *, host=None, parent=None, validate=None, data=None):
        key = self.plan.replacements.get(target)
        if key is None or not self.selected(key):
            return default_factory()
        try:
            obj = self.factory(key)(self.context(key, host=host, parent=parent, default_factory=default_factory, data=data))
            if validate:
                validate(obj)
            self.objects[key] = obj
            self.started.add(key)
            return obj
        except Exception as error:
            self.failure(key, error)
            return default_factory()

    def slots(self, target, *, host=None, parent=None, data=None):
        for key in self.plan.extensions.get(target, []):
            if not self.selected(key) or self.components[key]['operation'] != 'slot':
                continue
            try:
                obj = self.factory(key)(self.context(key, host=host, parent=parent, data=data))
                self.started.add(key)
                yield key, obj
            except Exception as error:
                self.failure(key, error)

    def write_snapshot(self):
        root = self.store.root / 'runtime'
        root.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = root / ('app-' + uuid.uuid4().hex + '.json')
        self.snapshot_path.write_text(json.dumps({'store': str(self.store.root), 'state': self.state}), encoding='utf-8')
        return str(self.snapshot_path)

    def write_status(self):
        if self.snapshot_path is None or self.domain != 'core':
            return
        path = self.snapshot_path.with_suffix('.core-status.json')
        payload = {'snapshot': self.snapshot_path.name, 'pid': os.getpid(),
                   'warnings': {p: status.warnings for p, status in self.plan.plugins.items() if status.warnings}}
        try:
            fd, temporary = tempfile.mkstemp(prefix='.plugin-status-', dir=path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(temporary, path)
        except OSError as error:
            _log(f'Не удалось сохранить состояние плагинов: {error}')

    def core_warnings(self):
        if self.snapshot_path is None:
            return {}
        try:
            payload = json.loads(self.snapshot_path.with_suffix('.core-status.json').read_text(encoding='utf-8'))
            if payload.get('snapshot') == self.snapshot_path.name:
                return payload.get('warnings', {})
        except (OSError, ValueError):
            pass
        return {}

    def _cleanup(self, key):
        for callback in reversed(self.cleanups.pop(key, [])):
            try:
                result = callback()
                if inspect.iscoroutine(result):
                    try:
                        task = asyncio.get_running_loop().create_task(result)
                        self.cleanup_tasks.add(task)
                    except RuntimeError:
                        result.close()
            except Exception as error:
                _log(f'Очистка {key}: {error}')

    def close(self):
        if self.closed:
            return
        self.closed = True
        for key in reversed(list(self.cleanups)):
            self._cleanup(key)
        self.callbacks.clear()


    async def aclose(self):
        self.close()
        if self.cleanup_tasks:
            tasks, self.cleanup_tasks = self.cleanup_tasks, set()
            await asyncio.gather(*tasks, return_exceptions=True)


def get_runtime():
    return _current.get() or _process_runtime


def set_runtime(runtime):
    global _process_runtime
    _process_runtime = runtime


def bootstrap(domain='ui', store=None, snapshot=None):
    if store is None:
        from core.paths import APP_PATHS
        store = PluginStore(APP_PATHS.root / 'plugins')
    if snapshot is None and domain == 'core':
        snapshot = os.environ.get(SNAPSHOT_ENV)
    if snapshot is None:
        store.reconcile_module_config()
    state = None
    if snapshot:
        value = json.loads(Path(snapshot).read_text(encoding='utf-8'))
        if Path(value['store']).resolve() != store.root:
            raise PluginError('Plugin snapshot belongs to another application root.')
        state = value['state']
    runtime = PluginRuntime(store, domain=domain, state=state)
    if snapshot:
        runtime.snapshot_path = Path(snapshot)
    runtime.write_status()
    set_runtime(runtime)
    return runtime
