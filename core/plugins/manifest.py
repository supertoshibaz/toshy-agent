"""Declarative plugin metadata. Inspecting a package never executes its code."""
from copy import deepcopy
from pathlib import PurePosixPath
import re
import unicodedata

from jsonschema import Draft202012Validator
from packaging.requirements import Requirement, InvalidRequirement
from packaging.specifiers import SpecifierSet, InvalidSpecifier
from packaging.version import Version, InvalidVersion

FORMAT_VERSION = 1
API_VERSION = 1
KINDS = ('widget', 'workspace', 'topbar', 'core', 'system-module', 'custom-module')
ENTITY_TAGS = {'widget': 'widget', 'workspace': 'workspace', 'topbar': 'top-bar',
               'core': 'core', 'system-module': 'system-module', 'custom-module': 'custom-module'}
PLUGIN_ID = r'^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$'
LOCAL_ID = r'^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$'
TAG = r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$'
SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Toshy plugin manifest v1', 'type': 'object', 'additionalProperties': False,
    'required': ['schema_version', 'id', 'version', 'name', 'description', 'author',
                 'license', 'image', 'tags', 'compatibility', 'files', 'components'],
    'properties': {
        'schema_version': {'const': FORMAT_VERSION},
        'id': {'type': 'string', 'pattern': PLUGIN_ID, 'maxLength': 100},
        'version': {'type': 'string', 'minLength': 1, 'maxLength': 50},
        'data_schema': {'type': 'integer', 'minimum': 1},
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 120},
        'description': {'type': 'string', 'minLength': 1, 'maxLength': 5000},
        'author': {'type': 'string', 'minLength': 1, 'maxLength': 120},
        'license': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'image': {'type': 'string', 'minLength': 1, 'maxLength': 240},
        'homepage': {'type': 'string', 'maxLength': 500},
        'tags': {'type': 'array', 'maxItems': 32, 'uniqueItems': True,
                 'items': {'type': 'string', 'pattern': TAG, 'maxLength': 40}},
        'compatibility': {'type': 'object', 'additionalProperties': False,
            'required': ['api', 'python', 'platforms'], 'properties': {
                'api': {'type': 'integer', 'minimum': 1},
                'python': {'type': 'string', 'maxLength': 120},
                'architectures': {'type': 'array', 'uniqueItems': True, 'minItems': 1, 'items': {'enum': ['x86_64', 'aarch64']}},
                'toshy': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                'platforms': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                              'items': {'enum': ['linux', 'windows']}}}},
        'requires_packages': {'$ref': '#/$defs/requirements'},
        'requires_plugins': {'type': 'object', 'maxProperties': 64,
            'propertyNames': {'pattern': PLUGIN_ID},
            'additionalProperties': {'type': 'string', 'maxLength': 120}},
        'files': {'type': 'array', 'minItems': 1, 'maxItems': 256, 'uniqueItems': True,
                  'items': {'type': 'string', 'minLength': 1, 'maxLength': 240}},
        'components': {'type': 'array', 'minItems': 1, 'maxItems': 128,
                       'items': {'$ref': '#/$defs/component'}}},
    '$defs': {
        'contracts': {'type': 'object', 'maxProperties': 64, 'propertyNames': {'pattern': r'^[a-z][a-z0-9_.:/-]+$'}, 'additionalProperties': {'type': 'string', 'minLength': 1, 'maxLength': 120}},
        'requirements': {'type': 'array', 'maxItems': 64, 'uniqueItems': True,
                         'items': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
        'component': {'type': 'object', 'additionalProperties': False,
            'required': ['id', 'type', 'mode'], 'properties': {
                'id': {'type': 'string', 'pattern': LOCAL_ID, 'maxLength': 80},
                'type': {'enum': list(KINDS)}, 'mode': {'enum': ['add', 'replace', 'extend']},
                'target': {'type': 'string', 'pattern': r'^[a-z][a-z0-9_.:/-]+$', 'maxLength': 180},
                'entrypoint': {'type': 'string', 'pattern': r'^[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*$', 'maxLength': 180},
                'path': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                'placement': {'enum': ['left_top', 'left_bottom', 'right_top', 'right_bottom',
                                       'top', 'bottom', 'left', 'right', 'menu']},
                'requires_packages': {'$ref': '#/$defs/requirements'},
                'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                'module_name': {'type': 'string', 'minLength': 1, 'maxLength': 100},
                'operation': {'enum': ['listen', 'before', 'after', 'filter', 'around', 'slot']},
                'group': {'type': 'string', 'pattern': LOCAL_ID, 'maxLength': 80},
                'requires_components': {'type': 'array', 'uniqueItems': True, 'maxItems': 64,
                    'items': {'type': 'string', 'pattern': r'^[a-z][a-z0-9_.-]*/[a-z][a-z0-9_-]*$'}},
                'requires_contracts': {'$ref': '#/$defs/contracts'},
                'provides_contracts': {'$ref': '#/$defs/contracts'},
                'extension_points': {'type': 'object', 'maxProperties': 64,
                    'additionalProperties': {'type': 'object', 'additionalProperties': False,
                        'required': ['strategy', 'version'], 'properties': {
                            'strategy': {'enum': ['event', 'operation', 'slot']},
                            'version': {'type': 'string', 'minLength': 1, 'maxLength': 40}}}}},
            'oneOf': [{'required': ['entrypoint'], 'not': {'required': ['path']}},
                      {'required': ['path'], 'not': {'required': ['entrypoint']}}],
            'allOf': [{'if': {'properties': {'mode': {'const': 'replace'}}},
                       'then': {'required': ['target']}}]}}
}

_RELEASE_URL = r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^\s]+$'
CATALOG_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Toshy plugin catalog v1', 'type': 'object', 'additionalProperties': False,
    'required': ['schema_version', 'plugins'], 'properties': {
        'schema_version': {'const': 1},
        'plugins': {'type': 'array', 'maxItems': 10000, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['id', 'version', 'name', 'description', 'author', 'license',
                         'compatibility', 'tags', 'image', 'archive', 'source', 'source_sha256'],
            'properties': {
                **{name: deepcopy(SCHEMA['properties'][name]) for name in
                   ('id', 'version', 'name', 'description', 'author', 'license', 'compatibility', 'tags')},
                'image': {'type': 'string', 'pattern': _RELEASE_URL, 'maxLength': 1000},
                'source': {'type': 'string', 'pattern': r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/tree/[^\s]+$', 'maxLength': 1000},
                'source_sha256': {'type': 'string', 'pattern': r'^[a-f0-9]{64}$'},
                'archive': {'type': 'object', 'additionalProperties': False,
                    'required': ['url', 'sha256', 'size'], 'properties': {
                        'url': {'type': 'string', 'pattern': _RELEASE_URL, 'maxLength': 1000},
                        'sha256': {'type': 'string', 'pattern': r'^[a-f0-9]{64}$'},
                        'size': {'type': 'integer', 'minimum': 1, 'maximum': 256 * 1024 * 1024}}}}}}}}


class PluginError(ValueError):
    pass


def safe_path(value):
    """One portable relative path, valid on Windows and Linux."""
    if not isinstance(value, str) or not value or len(value) > 240:
        raise PluginError('Invalid package path.')
    if unicodedata.normalize('NFC', value) != value or re.search(r'[<>:"\\|?*\x00-\x1f\x7f]', value):
        raise PluginError(f'Non-portable package path: {value!r}')
    path = PurePosixPath(value)
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if path.is_absolute() or any(part in ('', '.', '..') or part.endswith((' ', '.'))
                                 or part.split('.')[0].upper() in reserved for part in parts):
        raise PluginError(f'Unsafe package path: {value!r}')
    return path


def requirement(value):
    try:
        parsed = Requirement(value)
    except InvalidRequirement as error:
        raise PluginError(f'Invalid dependency: {value}') from error
    if parsed.url or parsed.extras:
        raise PluginError('V1 dependencies must use package names/version ranges without URLs or extras.')
    return parsed


def validate_manifest(value, *, allow_empty=False):
    # Empty authoring drafts are local only, never installable/exportable packages.
    schema = SCHEMA
    if allow_empty:
        schema = deepcopy(SCHEMA)
        schema['properties']['components']['minItems'] = 0
    elif isinstance(value, dict) and value.get('components') == []:
        raise PluginError('Добавьте хотя бы один компонент в plugin.json перед проверкой, экспортом или установкой.')
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        first = errors[0]
        raise PluginError(f"Manifest {'.'.join(map(str, first.path)) or '<root>'}: {first.message}")
    result = deepcopy(value)
    try:
        version = Version(result['version'])
        if version.local is not None or version.is_prerelease or version.is_devrelease or version.is_postrelease or version.epoch:
            raise PluginError('V1 plugin releases use final versions without local/post/epoch suffixes.')
        if str(version) != result['version']:
            raise PluginError('Use a canonical version, for example 1.0.0.')
        SpecifierSet(result['compatibility']['python'])
        if 'toshy' in result['compatibility']:
            SpecifierSet(result['compatibility']['toshy'])
        for name, spec in result.get('requires_plugins', {}).items():
            if name == result['id']:
                raise PluginError('A plugin cannot depend on itself.')
            SpecifierSet(spec)
    except (InvalidVersion, InvalidSpecifier) as error:
        raise PluginError(f'Invalid version constraint: {error}') from error
    names = set()
    extension_names = set()
    for component in result['components']:
        if component['id'] in names:
            raise PluginError(f"Duplicate component id: {component['id']}")
        names.add(component['id'])
        for item in component.get('requires_packages', []):
            requirement(item)
        if 'module_name' in component:
            if not component['module_name'].isidentifier():
                raise PluginError('module_name must be a Python identifier.')
            safe_path(component['module_name'])
        if 'path' in component:
            safe_path(component['path'])
            if component['type'] not in ('system-module', 'custom-module'):
                raise PluginError('Only module components may declare a content path.')
        for spec in component.get('requires_contracts', {}).values():
            try:
                SpecifierSet(spec)
            except InvalidSpecifier as error:
                raise PluginError(f'Invalid contract requirement: {spec}') from error
        for version_text in component.get('provides_contracts', {}).values():
            try:
                version_value = Version(version_text)
                if str(version_value) != version_text or not version_value.release or version_value.is_prerelease:
                    raise InvalidVersion(version_text)
            except InvalidVersion as error:
                raise PluginError('Provided contracts need canonical numeric versions.') from error
        for point, declaration in component.get('extension_points', {}).items():
            if not any(point.startswith(domain + '.plugin.' + result['id'] + '.') for domain in ('ui', 'core')):
                raise PluginError('Plugin extension point names must start with ui.plugin.<plugin-id>. or core.plugin.<plugin-id>.')
            if point in extension_names:
                raise PluginError(f'Duplicate extension point: {point}')
            extension_names.add(point)
            try:
                version_value = Version(declaration['version'])
                if str(version_value) != declaration['version'] or version_value.is_prerelease:
                    raise InvalidVersion(declaration['version'])
            except InvalidVersion as error:
                raise PluginError(f'Invalid extension point version: {point}') from error
        if component['mode'] == 'extend':
            if not component.get('target') or not component.get('operation') or 'path' in component:
                raise PluginError('An extension requires target, operation and entrypoint.')
        elif component.get('operation'):
            raise PluginError('operation is only valid for extensions.')
        if component['type'] in ('system-module', 'custom-module') and component['mode'] == 'extend':
            raise PluginError('Extend module behavior through core hooks or its declared extension points.')
        if component['mode'] == 'replace':
            prefix = {'widget': 'widget.', 'workspace': 'workspace.', 'topbar': 'topbar.',
                      'core': 'core.', 'system-module': 'module.system.', 'custom-module': 'module.custom.'}[component['type']]
            if not component['target'].startswith(prefix):
                raise PluginError(f"Replacement target must start with {prefix}")
    for item in result.get('requires_packages', []):
        requirement(item)
    for filename in [result['image'], *result['files']]:
        safe_path(filename)
    if result['image'].lower().rsplit('.', 1)[-1] not in ('png', 'jpg', 'jpeg', 'webp'):
        raise PluginError('Plugin image must be PNG, JPEG or WebP.')
    if any(path == 'plugin.json' or path.startswith('.toshy-') for path in result['files']):
        raise PluginError('The manifest and installation receipts cannot be declared as payload files.')
    return result


def tags_for(manifest):
    return sorted(set(manifest['tags']) | {ENTITY_TAGS[c['type']] for c in manifest['components']})


def validate_catalog(value):
    error = next(Draft202012Validator(CATALOG_SCHEMA).iter_errors(value), None)
    if error is not None:
        raise PluginError(f'Invalid plugin catalog: {error.message}')
    identities = [entry['id'] for entry in value['plugins']]
    if len(identities) != len(set(identities)):
        raise PluginError('Duplicate plugin id in catalog.')
    for entry in value['plugins']:
        try:
            Version(entry['version'])
            SpecifierSet(entry['compatibility']['python'])
        except (InvalidVersion, InvalidSpecifier) as error:
            raise PluginError(f'Invalid catalog compatibility/version: {error}') from error
    return deepcopy(value)
