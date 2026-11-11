"""Virtual module locations. Plugin Python stays inside its installed package."""
import importlib.abc
import importlib.util
from pathlib import Path
import sys
import types
from .runtime import get_runtime
from .manifest import PluginError


def module_name(identity, component):
    return component.get('module_name') or component.get('target', '').rsplit('.', 1)[-1] or (identity.replace('.', '_').replace('-', '_') + '_' + component['id'].replace('-', '_'))


def plugin_modules(runtime=None):
    runtime = runtime or get_runtime()
    if runtime is None:
        return {}
    result = {}
    for key, c in runtime.active():
        if c['type'] not in ('system-module', 'custom-module'):
            continue
        category = 'system' if c['type'] == 'system-module' else 'custom'
        name = module_name(key.split('/')[0], c)
        directory = runtime.directory(key.split('/')[0]) / c.get('path', '.')
        result[(category, name)] = dict(category=category, name=name, directory=directory,
            component=key, runtime=runtime, display_name=c.get('title') or runtime.by_id[key.split('/')[0]]['name'])
    return result


def module_locations(root=None, category=None):
    if root is None:
        from core.paths import APP_PATHS
        root = APP_PATHS.root
    root = Path(root).resolve()
    result = {}
    for cat in ((category,) if category else ('system', 'custom')):
        from core.paths import APP_PATHS
        base = APP_PATHS.code_root if cat == 'system' and root == APP_PATHS.root else root
        for path in sorted((base / 'modules' / cat).glob('*')):
            if not path.name.startswith('.') and (path / '__init__.py').is_file():
                result[(cat, path.name)] = path
    runtime = get_runtime()
    if runtime is not None and runtime.store.root.parent == root:
        for pair, item in plugin_modules(runtime).items():
            if category is None or pair[0] == category:
                result[pair] = item['directory']
    return result


def module_directory(category, name, root=None):
    from core.paths import APP_PATHS
    root = Path(root or APP_PATHS.root)
    return module_locations(root, category).get((category, name), root / 'modules' / category / name)


def original_module_factory(category, name):
    """Load the built-in implementation under a separate package namespace."""
    from core.paths import APP_PATHS
    directory = (APP_PATHS.code_root / 'modules' if category == 'system' else APP_PATHS.modules) / category / name
    def create():
        import hashlib
        prefix = '_toshy_original_' + hashlib.sha256(str(directory).encode()).hexdigest()[:20]
        module = sys.modules.get(prefix)
        if module is None:
            source = directory / '__init__.py'
            if not source.is_file():
                raise PluginError(f'Исходный встроенный модуль {category}/{name} не найден.')
            spec = importlib.util.spec_from_file_location(prefix, source, submodule_search_locations=[str(directory)])
            module = importlib.util.module_from_spec(spec)
            sys.modules[prefix] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(prefix, None)
                raise
        return module.Module()
    return create


class _FactoryLoader(importlib.abc.Loader):
    def __init__(self, item):
        self.item = item

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        item = self.item
        runtime, key = item['runtime'], item['component']
        if not runtime.selected(key):
            raise PluginError(f'Модуль плагина {key} отключён из-за ошибки или зависимости.')
        def create():
            component = runtime.components[key]
            default = original_module_factory(item['category'], item['name']) if component['mode'] == 'replace' else None
            return runtime.factory(key)(runtime.context(key, default_factory=default,
                data={'category': item['category'], 'module_name': item['name']}))
        module.Module = create
        module.__file__ = str(item['directory'] / '__init__.py')
        module.__path__ = [str(item['directory'])]


class PluginModuleFinder(importlib.abc.MetaPathFinder):
    def __init__(self, runtime):
        self.runtime = runtime

    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.split('.')
        if len(parts) != 3 or parts[:2] not in (['modules', 'system'], ['modules', 'custom']):
            return None
        item = plugin_modules(self.runtime).get((parts[1], parts[2]))
        if not item:
            return None
        c = self.runtime.components[item['component']]
        if 'entrypoint' in c:
            return importlib.util.spec_from_loader(fullname, _FactoryLoader(item), is_package=True)
        directory = item['directory']
        if not (directory / '__init__.py').is_file():
            raise PluginError(f'В модуле {fullname} отсутствует __init__.py.')
        return importlib.util.spec_from_file_location(fullname, directory / '__init__.py', submodule_search_locations=[str(directory)])


def install_module_finder(runtime):
    for finder in list(sys.meta_path):
        if isinstance(finder, PluginModuleFinder):
            sys.meta_path.remove(finder)
    finder = PluginModuleFinder(runtime)
    sys.meta_path.insert(0, finder)
    return finder


def module_reference(path, root=None):
    """Persist pipeline tabs by module identity, independent of plugin version."""
    from core.paths import APP_PATHS
    root = Path(root or APP_PATHS.root).resolve()
    path = Path(path).resolve()
    if path.is_relative_to(root / 'modules'):
        return str(path.relative_to(root / 'modules'))
    for (category, name), item in plugin_modules().items():
        directory = item['directory'].resolve()
        if path.is_relative_to(directory):
            return '@plugin/' + category + '/' + name + '/' + path.relative_to(directory).as_posix()
    raise PluginError('Pipeline path does not belong to a module.')


def resolve_module_reference(reference, root=None):
    from core.paths import APP_PATHS
    root = Path(root or APP_PATHS.root).resolve()
    if not isinstance(reference, str):
        return None
    if reference.startswith('@plugin/'):
        parts = reference.split('/', 3)
        if len(parts) != 4:
            return None
        item = plugin_modules().get((parts[1], parts[2]))
        if item is None:
            return None
        base, relative = item['directory'].resolve(), parts[3]
    else:
        base, relative = root / 'modules', reference
    path = (base / relative).resolve()
    return path if path.is_relative_to(base) else None
