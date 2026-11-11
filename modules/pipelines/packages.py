"""Portable custom module packages. No GUI imports or links to source pipelines."""
from __future__ import annotations
import json
import re
import shutil
import uuid
from pathlib import Path
from core.paths import APP_PATHS
from .storage import load_pipeline_script, save_layout, load_layout, save_pipeline_script
from .contracts import dependencies, interface, schema_for

MODES = {'user': 'По назначению пользователя', 'tool': 'Как инструмент модели', 'internal': 'Только внутри модуля'}

class PackageStore:
    def __init__(self, root=None):
        self.root = Path(root or APP_PATHS.modules / 'custom').resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, module):
        if not isinstance(module, str) or not module.isidentifier() or len(module) > 100:
            raise ValueError('Имя папки модуля: буквы, цифры и подчёркивания, до 100 символов')
        if self.root == (APP_PATHS.modules / 'custom').resolve():
            from core.plugins.modules import module_directory
            return module_directory('custom', module)
        return self.root / module

    def read(self, module):
        data = json.loads((self.path(module) / 'module.json').read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('id') != module:
            raise ValueError('Некорректный manifest модуля')
        return data

    def write(self, module, data):
        color = data.get('color')
        if color and not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise ValueError('Цвет модуля должен иметь формат #RRGGBB')
        from .extensions import package_extensions
        for extension in package_extensions():
            handler = getattr(extension, 'sync_package', None)
            if handler:
                handler(self, module, data)
        # Keep disk manifest and UI metadata in the same naming convention.
        save_layout(self.path(module) / 'module.json', data)
        ui = load_layout(self.path(module) / 'ui' / 'config.json')
        ui.update(display_name=data.get('name', module), color=color or ui.get('color', '#ec4899'))
        save_layout(self.path(module) / 'ui' / 'config.json', ui)

    def list(self):
        result = []
        if self.root == (APP_PATHS.modules / 'custom').resolve():
            from core.plugins.modules import module_locations
            names = [name for category, name in module_locations(category='custom')]
        else:
            names = [p.parent.name for p in sorted(self.root.glob('*/module.json'))]
        for name in names:
            try:
                result.append(self.read(name))
            except (ValueError, OSError):
                pass
        return result

    def create(self, name):
        name = str(name).strip()
        if not name:
            raise ValueError('Укажите название модуля')
        module = re.sub(r'\W+', '_', name, flags=re.UNICODE).strip('_')
        if module and module[0].isdigit():
            module = '_' + module
        if not module or len(module) > 100:
            raise ValueError('В названии нужны буквы или цифры; длина имени папки — до 100 символов')
        reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
        if module.casefold() in reserved:
            raise ValueError('Это имя папки зарезервировано в Windows')
        existing = {p.name.casefold() for p in self.root.iterdir()}
        system = self.root.parent / 'system'
        if system.is_dir():
            existing.update(p.name.casefold() for p in system.iterdir())
        if module.casefold() in existing:
            raise ValueError(f'Модуль или папка «{module}» уже существует. Выберите другое название.')
        p = self.path(module)
        p.mkdir()
        for folder in ('pipelines', 'knowledge', 'tools', 'skills', 'mcp', 'ui'):
            (p / folder).mkdir()
        (p / '__init__.py').write_text('from pathlib import Path\nfrom modules.pipelines.package_module import PackageModule\n\nclass Module(PackageModule):\n    def __init__(self):\n        super().__init__(Path(__file__).parent)\n', encoding='utf-8')
        self.write(module, {'schema_version': 1, 'id': module, 'name': name, 'description': '',
                            'system_prompt': '', 'color': '#ec4899', 'pipelines': {}, 'dependencies': []})
        return module

    def resolve(self, module, pipeline):
        data = self.read(module)
        item = data['pipelines'].get(pipeline)
        if not item:
            raise ValueError(f'Pipeline {pipeline} не найден в модуле {module}')
        relative = Path(item['path'])
        target = (self.path(module) / relative).resolve()
        if not target.is_relative_to((self.path(module) / 'pipelines').resolve()):
            raise ValueError('Pipeline должен находиться внутри своего модуля')
        return target

    def add_pipeline(self, module, source, mode='user'):
        if mode not in MODES:
            raise ValueError('Неизвестный режим pipeline')
        source = Path(source)
        definition = load_pipeline_script(source / 'pipeline.py')
        data = self.read(module)
        pid = 'pipeline_' + uuid.uuid4().hex[:10]
        target = self.path(module) / 'pipelines' / pid
        shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        data['pipelines'][pid] = {'name': definition.name or source.name, 'description': '', 'path': f'pipelines/{pid}', 'mode': mode}
        copied_servers = []
        try:
            from .extensions import package_extensions
            for extension in package_extensions():
                handler = getattr(extension, 'copy_bindings', None)
                if handler:
                    handler(self, module, definition, copied_servers)
            save_pipeline_script(target / 'pipeline.py', definition)
            data['pipelines'][pid]['dependencies'] = dependencies(definition)
            data['dependencies'] = sorted({d for x in data['pipelines'].values() for d in x.get('dependencies', [])})
            self.write(module, data)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            for server in copied_servers:
                shutil.rmtree(server, ignore_errors=True)
            raise
        return pid

    def internal_definition(self, module, pid):
        item = self.read(module)['pipelines'].get(pid)
        if not item or item.get('mode') != 'internal':
            raise ValueError('Nested Pipeline должен ссылаться на внутренний pipeline того же модуля')
        return load_pipeline_script(self.resolve(module, pid) / 'pipeline.py')

    def catalog(self, enabled, tools=None):
        from .contracts import validate
        result = []
        for m in self.list():
            if m['id'] not in enabled:
                continue
            for pid, item in m.get('pipelines', {}).items():
                entry = dict(item, id=f"{m['id']}/{pid}", module=m['id'], module_name=m['name'])
                try:
                    p = load_pipeline_script(self.resolve(m['id'], pid) / 'pipeline.py')
                    errors, warnings, _ = validate(p, tools=tools, enabled=enabled,
                        resolve_call=lambda ref, mid=m['id']: self.internal_definition(mid, ref), stack=(pid,))
                    entry.update(errors=errors, warnings=warnings, inputs=interface(p)[0])
                except Exception as exc:
                    entry['errors'] = [str(exc)]
                result.append(entry)
        return result
