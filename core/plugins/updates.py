"""Catalog-pinned upgrades, checked against the complete selected plugin set."""
from packaging.version import Version
from .catalog import CatalogClient
from .manifest import PluginError
from .resolver import resolve_plugins
import json
import re


def check_updates(store, catalog_source, *, cancel=None):
    client = CatalogClient(store.root / 'cache')
    catalog = client.fetch_catalog(catalog_source, cancel=cancel)
    state = store.state()
    installed = {m['id']: m for m in store.manifests(state)}
    entries = {m['id']: m for m in catalog['plugins']}
    result = []
    for identity, manifest in installed.items():
        entry = entries.get(identity)
        if entry is None:
            result.append({'id': identity, 'installed': manifest['version'], 'status': 'unlisted'})
        elif Version(entry['version']) > Version(manifest['version']):
            result.append({'id': identity, 'installed': manifest['version'], 'status': 'available', 'entry': entry})
        else:
            result.append({'id': identity, 'installed': manifest['version'], 'status': 'current'})
    return result


def install_update(store, entry, catalog_source, *, cancel=None, progress=None):
    client = CatalogClient(store.root / 'cache')
    candidate = {'url': entry['archive']['url'], 'sha256': entry['archive']['sha256'], 'size': entry['archive']['size']}
    downloaded = client.download_package(candidate, entry=entry, cancel=cancel, progress=progress)
    try:
        state = store.state()
        old = store.manifests(state)
        manifests = [downloaded.manifest if m['id'] == entry['id'] else m for m in old]
        if not any(m['id'] == entry['id'] for m in old):
            raise PluginError('The plugin was removed while the update was being prepared.')
        previous = next(m for m in old if m['id'] == entry['id'])
        if previous.get('data_schema', 1) != downloaded.manifest.get('data_schema', 1):
            raise PluginError('This plugin update changes its data schema. An explicit plugin data migration is required; automatic replacement was refused.')
        before = resolve_plugins(old, state['order'], state['enabled'])
        plan = resolve_plugins(manifests, state['order'], state['enabled'])
        # Check the upgraded plugin even when currently disabled. Dependencies
        # must not be installed into the application's environment implicitly.
        enabled = list(dict.fromkeys([*state['enabled'], entry['id']]))
        trial = resolve_plugins(manifests, state['order'], enabled)
        status = trial.plugins[entry['id']]
        if status.state in {'blocked', 'error'} or not status.active_components:
            raise PluginError('The update is incompatible: ' + '; '.join(status.warnings))
        for identity in state['enabled']:
            previous = before.plugins[identity]
            after = plan.plugins[identity]
            if previous.active_components and (not after.active_components or after.state == 'blocked'):
                raise PluginError(f'The update would disable {identity}: ' + '; '.join(after.warnings))
        return store.install(downloaded.path, expected_sha256=downloaded.digest,
                             expected_plugin_id=entry['id'], expected_version=entry['version'], source=catalog_source,
                             expected_state=state)
    finally:
        downloaded.close()


def previous_version(store, identity):
    state = store.state()
    current = state['versions'].get(identity)
    if not current:
        return None
    receipt = store._managed('packages', identity, current) / '.toshy-receipt.json'
    try:
        value = json.loads(receipt.read_text()).get('previous_version')
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)*', value) and value != current else None


def rollback(store, identity):
    with store._write_lock():
        state = store.state()
        version = previous_version(store, identity)
        if not version:
            raise PluginError('No previous plugin version is available.')
        path = store._managed('packages', identity, version) / 'plugin.json'
        old = json.loads(path.read_text(encoding='utf-8'))
        manifests = store.manifests(state)
        current = next(m for m in manifests if m['id'] == identity)
        if old.get('data_schema', 1) != current.get('data_schema', 1):
            raise PluginError('Plugin rollback requires a compatible data schema.')
        candidate = [old if m['id'] == identity else m for m in manifests]
        before = resolve_plugins(manifests, state['order'], state['enabled'])
        after = resolve_plugins(candidate, state['order'], state['enabled'])
        for key in state['enabled']:
            if before.plugins[key].active_components and not after.plugins[key].active_components:
                raise PluginError(f'Rollback would disable {key}.')
        state['versions'][identity] = version
        store._save(state)
        return version
