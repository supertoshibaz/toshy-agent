"""Small Python scaffolds and explicit module packaging, without GitHub login."""
import base64
from copy import deepcopy
from datetime import date
import json
import os
from pathlib import Path
import shutil
import tempfile
from .manifest import validate_manifest, PluginError
from .package import inspect_directory, shareable

_ICON = 'iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAn0lEQVR4nO3WoQ2AQAAEwe/lFWXiKRgHPZCQIfyK9ZdRN+bcrpUbeoAuAD1AF4AeoAtAD9AFoAfoAtADdAHoAboA9ADdI4D9OD9bAAEEEMDrAH8qAD1AF4AeoAtAD9AFoAfoeoIBBBBAACsXgB6gC0AP0AWgB+gC0AN0PcEAAggggJULQA/QBaAH6ALQA3QB6AG6APQAXQB6gC4APUB3A8t+gU1vxVzbAAAAAElFTkSuQmCC'
_MIT = '''MIT License

Copyright (c) {year} {author}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''


def create_scaffold(destination, *, identity, name, author, description, kind='widget',
                    target=None, operation=None, mode='add', image=None, module_source=None, module_name=None):
    """Create a new folder atomically. Never infer a plugin from app-source diffs."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise PluginError('Выберите новую папку для исходников плагина.')
    pipeline_nodes = kind == 'pipeline-nodes'
    if pipeline_nodes:
        if module_source or target or operation or mode != 'add':
            raise PluginError('Шаблон узлов создаёт новый пакет операций.')
        from .node_template import node_module_name
        module_name = module_name or node_module_name(identity)
        kind = 'system-module'
    component = {'id': 'main', 'type': kind, 'mode': mode}
    if target:
        component['target'] = target
        if mode == 'replace':
            from .contracts import host_targets
            declaration = host_targets().get(target)
            if declaration is not None and declaration.provides:
                component['provides_contracts'] = dict(declaration.provides)
    if operation:
        component['operation'] = operation
    if module_name:
        component['module_name'] = module_name
    files = ['LICENSE', 'README.md', 'icon.png']
    if pipeline_nodes:
        component['path'] = 'module'; files.append('module')
    elif kind is None:
        if module_source or target or operation or mode != 'add':
            raise PluginError('Пустая заготовка не содержит компонентов.')
    elif module_source:
        component['path'] = 'module'; files.append('module')
    else:
        component['entrypoint'] = 'plugin:create'; files.append('plugin.py')
    if kind == 'workspace' and mode == 'add':
        component['placement'] = 'top'
    if kind == 'widget' and mode == 'add':
        component['placement'] = 'right_top'
    manifest = _validate_authoring_manifest({'schema_version': 1, 'id': identity, 'version': '1.0.0',
        'name': name, 'description': description, 'author': author, 'license': 'MIT',
        'image': 'icon.png', 'tags': ['pipeline-nodes'] if pipeline_nodes else [],
        'compatibility': {'api': 1, 'python': '>=3.12',
        'platforms': ['linux', 'windows']}, 'files': files,
        'components': [] if kind is None else [component]})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.plugin-source-', dir=destination.parent) as staging:
        root = Path(staging) / 'source'; root.mkdir()
        (root / 'plugin.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        (root / 'LICENSE').write_text(_MIT.format(year=date.today().year, author=author), encoding='utf-8')
        if image:
            source = Path(image)
            if source.suffix.lower() != '.png':
                raise PluginError('Для шаблона выберите PNG. В готовом manifest поддерживаются JPEG/WebP.')
            shutil.copyfile(source, root / 'icon.png')
        else:
            (root / 'icon.png').write_bytes(base64.b64decode(_ICON))
        if pipeline_nodes:
            from .node_template import write_node_module
            write_node_module(root / 'module', identity, module_name, name)
        elif module_source:
            source = Path(module_source).resolve()
            if not (source / '__init__.py').is_file():
                raise PluginError('В папке модуля отсутствует __init__.py.')
            def ignore(folder, names):
                return [n for n in names if not shareable((Path(folder) / n).relative_to(source).as_posix())]
            if source.is_symlink():
                raise PluginError('Модуль с символическими ссылками нельзя экспортировать автоматически.')
            for folder, directories, names in os.walk(source, followlinks=False):
                directories[:] = [n for n in directories if shareable((Path(folder) / n).relative_to(source).as_posix())]
                for name in [*directories, *names]:
                    path = Path(folder) / name
                    if shareable(path.relative_to(source).as_posix()) and path.is_symlink():
                        raise PluginError('Модуль с символическими ссылками нельзя экспортировать автоматически.')
            shutil.copytree(source, root / 'module', ignore=ignore)
            if (source / 'LICENSE').is_file():
                shutil.copyfile(source / 'LICENSE', root / 'LICENSE')
                manifest['license'] = 'SEE LICENSE'
                (root / 'plugin.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            # Local launch paths/environments/credentials are machine-owned.
            # Preserve only portable server identity/source, schema caches and UI settings.
            for path in (root / 'module' / 'mcp').glob('*/server.json'):
                data = json.loads(path.read_text(encoding='utf-8'))
                for field in ('command', 'error'):
                    data[field] = ''
                for field in ('headers', 'env', 'user_config', 'values'):
                    data[field] = {}
                data['args'] = []; data['installation'] = 'pending'; data['autostart'] = False
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        elif kind is not None:
            if mode == 'replace':
                code = 'def create(context):\n    # Keep the host class contract. You may subclass the original class.\n    widget = context.default_factory()\n    return widget\n'
            elif mode == 'extend' and operation != 'slot':
                signature = {'around': 'next_call, payload', 'filter': 'result, payload',
                    'after': 'result, payload', 'before': 'payload', 'listen': 'payload'}[operation]
                body = 'return await next_call(payload)' if operation == 'around' else 'return result' if operation in ('filter', 'after') else 'pass'
                prefix = 'async ' if operation == 'around' else ''
                code = f'def create(context):\n    {prefix}def handler({signature}):\n        {body}\n    return handler\n'
            elif kind in ('system-module', 'custom-module'):
                code = 'def create(context):\n    from core.registry import BaseModule\n    return BaseModule()\n'
            elif kind == 'core':
                code = 'def create(context):\n    # Register resource cleanup through context.register_cleanup(callback).\n    return None\n'
            else:
                code = 'def create(context):\n    from PySide6.QtWidgets import QLabel\n    return QLabel("My plugin", context.parent)\n'
            (root / 'plugin.py').write_text(code, encoding='utf-8')
        (root / 'README.md').write_text(f'''# {name}

{description}

Edit plugin.py / module and plugin.json. Plugin files remain in their package.
This template uses MIT for your new code. Preserve licenses of copied code;
edit LICENSE and plugin.json if the included module has another license.

Import is disabled by default. Enable and restart Toshy to activate.
Changes of order/toggles take effect after restart. Publish manually via PR.
Inspect every exported file for secrets; filename filters cannot find all secrets.
MCP local environments are excluded; reinstall downloaded dependencies locally.

Components can declare requires_components ["author.plugin/component"],
requires_contracts {{"ui.chat.message.actions": ">=1,<2"}}, and group "feature".
A replacement must explicitly provide child interfaces it preserves, e.g.
provides_contracts {{"ui.chat.message.actions": "1.0"}}.

Qt builtin replacements return the original widget class or a subclass.
Slot factories return QWidget. Hook factories return a callable:
listen(payload); before(payload); after(result,payload); filter(result,payload);
around(next_call,payload). Core callbacks may be async; UI callbacks are sync.
Do not retry a mutating next_call. before/after are manager-owned.
''', encoding='utf-8')
        if kind is None:
            (root / 'README.md').write_text(f'# {name}\n\n{description}\n\n'
                'Empty scaffold. Add components with "Add component…" '
                'or create Python files and declare them in plugin.json. Then inspect the package and export a ZIP.\n', encoding='utf-8')
        else:
            inspect_directory(root)
        root.replace(destination)
    return manifest


def _validate_authoring_manifest(value):
    """Permit empty local drafts without weakening install/export validation."""
    if not isinstance(value, dict) or value.get('components') != []:
        return validate_manifest(value)
    checked = deepcopy(value)
    checked['components'] = [{'id': 'draft', 'type': 'core', 'mode': 'add', 'entrypoint': 'plugin:create'}]
    validate_manifest(checked)
    return deepcopy(value)


def append_component(directory, *, component_id, kind, module_source=None, module_name=None):
    """Append a generated fragment; commit the new manifest last, atomically."""
    import re
    from .package import inspect_directory_manifest
    root = Path(directory)
    if root.is_symlink() or (root / 'plugin.json').is_symlink():
        raise PluginError('Папка исходников и plugin.json не должны быть символическими ссылками.')
    root = root.resolve()
    if (root / '.toshy-receipt.json').exists():
        raise PluginError('Установленную версию нельзя редактировать. Сначала скопируйте исходники в отдельную папку.')
    manifest_path = root / 'plugin.json'
    original_bytes = manifest_path.read_bytes()
    manifest = _validate_authoring_manifest(json.loads(original_bytes))
    if not isinstance(component_id, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,79}', component_id):
        raise PluginError('ID компонента: строчные английские буквы, цифры, _ и -, начиная с буквы.')
    if kind is None:
        raise PluginError('Выберите содержательный шаблон. «Без шаблона» создаёт только целый пустой плагин.')
    if any(c['id'] == component_id for c in manifest['components']):
        raise PluginError('Компонент с таким ID уже есть.')
    folder_name = component_id.replace('-', '_')
    parent = root / 'components'
    destination = parent / folder_name
    if parent.is_symlink() or destination.exists() or destination.is_symlink():
        raise PluginError('Папка компонента уже существует или является символической ссылкой.')
    if kind == 'pipeline-nodes':
        from .node_template import node_module_name
        module_name = module_name or node_module_name(manifest['id'] + '.' + component_id)
    if module_name and any(c.get('module_name') == module_name for c in manifest['components']):
        raise PluginError('В этом плагине уже есть модуль с таким именем. Укажите другое имя модуля.')
    with tempfile.TemporaryDirectory(prefix='.plugin-component-', dir=root.parent) as temporary:
        stage = Path(temporary) / 'fragment'
        component_title = (component_id + ' · ' + manifest['name'])[:120]
        fragment = create_scaffold(stage, identity=manifest['id'], name=component_title,
            author=manifest['author'], description=manifest['description'], kind=kind,
            module_source=module_source, module_name=module_name)
        component = deepcopy(fragment['components'][0]); component['id'] = component_id
        component['title'] = component_title
        prefix = 'components/' + folder_name
        if 'path' in component:
            component['path'] = prefix + '/' + component['path']
        else:
            component['entrypoint'] = 'components.' + folder_name + '.' + component['entrypoint']
        updated = deepcopy(manifest)
        updated['components'].append(component)
        if not any(prefix == p or prefix.startswith(p + '/') for p in updated['files']):
            updated['files'].append(prefix)
        updated['tags'] = sorted(set(updated['tags']) | set(fragment['tags']))
        validate_manifest(updated)
        from .package import MAX_MANIFEST_BYTES
        if len(json.dumps(updated, ensure_ascii=False, indent=2).encode('utf-8')) > MAX_MANIFEST_BYTES:
            raise PluginError('Описание плагина превышает 128 КиБ.')
        (stage / 'plugin.json').unlink()
        # Explicit package boundary, no global sys.path changes or implicit imports.
        (stage / '__init__.py').touch()
        parent.mkdir(exist_ok=True)
        committed = False; owned = False
        try:
            destination.mkdir()  # Exclusive reservation: never overwrite another component.
            owned = True
            for child in stage.iterdir():
                child.replace(destination / child.name)
            inspect_directory_manifest(root, updated)
            if manifest_path.read_bytes() != original_bytes:
                raise PluginError('plugin.json изменился во время операции. Повторите добавление.')
            descriptor, temporary_manifest = tempfile.mkstemp(prefix='.compose-', dir=root)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                    json.dump(updated, output, ensure_ascii=False, indent=2)
                    output.flush(); os.fsync(output.fileno())
                os.replace(temporary_manifest, manifest_path)
                committed = True
            finally:
                Path(temporary_manifest).unlink(missing_ok=True)
        finally:
            if owned and not committed and destination.is_dir():
                shutil.rmtree(destination)
    return updated
