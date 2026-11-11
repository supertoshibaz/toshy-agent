"""Remove unavailable plugin modules from the next core configuration."""
from core.module_config import edit_module_config
from core.paths import APP_PATHS
from .modules import module_name
from .resolver import resolve_plugins


def synchronize_modules(store, state, *, manifests=None, commit=None):
    manifests = store.manifests(state) if manifests is None else manifests
    plan = resolve_plugins(manifests, state['order'], state['enabled'])
    declared, active = set(), set()
    for manifest in manifests:
        identity = manifest['id']
        for component in manifest['components']:
            if component['type'] not in ('system-module', 'custom-module'):
                continue
            pair = ('system' if component['type'] == 'system-module' else 'custom',
                    module_name(identity, component))
            declared.add(pair)
            if identity + '/' + component['id'] in plan.plugins[identity].active_components:
                active.add(pair)
    root = store.root.parent
    unavailable = set()
    for category, name in declared - active:
        base = APP_PATHS.code_root if category == 'system' and root == APP_PATHS.root else root
        # Disabling a replacement must retain the original built-in module.
        if not (base / 'modules' / category / name / '__init__.py').is_file():
            unavailable.add((category, name))

    def edit(config):
        for category in ('system', 'custom'):
            key = category + '_modules'
            values = config.get(key)
            if values is not None:
                config[key] = [name for name in values if (category, name) not in unavailable]

    return edit_module_config(root / 'config.yaml', edit, commit=commit)
