"""Plan complete contributions before importing plugin code.

Priority selects exclusive providers; it never erases hooks or satisfies a
missing interface. Invalid component/group dependencies are excluded together.
"""
from dataclasses import dataclass, field
from importlib import metadata
import platform
import sys
from packaging.markers import default_environment
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version, InvalidVersion
from .manifest import API_VERSION, PluginError, requirement, validate_manifest
from .contracts import host_targets, OPERATIONS, Target


@dataclass
class PluginStatus:
    plugin_id: str
    state: str = 'disabled'
    active_components: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class PluginPlan:
    order: list
    plugins: dict
    replacements: dict = field(default_factory=dict)
    additions: list = field(default_factory=list)
    extensions: dict = field(default_factory=dict)
    contracts: dict = field(default_factory=dict)
    runtime_activation_available: bool = True


def resolve_plugins(manifests, order=(), enabled=(), *, packages=None, python_version=None,
                    platform_name=None, api_version=API_VERSION, toshy_version=None,
                    targets=None, excluded=None, architecture=None):
    from core.version import __version__
    toshy_version = toshy_version or __version__
    architecture = (architecture or platform.machine()).lower()
    architecture = {'amd64': 'x86_64', 'arm64': 'aarch64'}.get(architecture, architecture)
    manifests = [validate_manifest(m) for m in manifests]
    by_id = {m['id']: m for m in manifests}
    if len(by_id) != len(manifests):
        raise PluginError('Multiple selected versions of a plugin.')
    if len(by_id) > 256:
        raise PluginError('At most 256 installed plugins are supported.')
    order, enabled = list(order), set(enabled)
    if len(order) != len(set(order)) or (set(order) | enabled) - by_id.keys():
        raise PluginError('Invalid plugin priority/enabled identifiers.')
    order.extend(sorted(by_id.keys() - set(order)))
    catalog = dict(host_targets() if targets is None else targets)
    builtin_contracts = set(catalog)
    components = {m['id'] + '/' + c['id']: c for m in manifests for c in m['components']}
    owners = {key: key.split('/')[0] for key in components}
    for key, c in components.items():
        for name, declaration in c.get('extension_points', {}).items():
            strategy = declaration['strategy']
            catalog[name] = Target(strategy, name.split('.')[0], strategy, declaration['version'])
    # Module names are global across categories because tools/project scopes use them.
    baseline_modules = {name.rsplit('.', 1)[-1] for name in catalog if name.startswith('module.')}
    module_targets = {}
    for key, c in components.items():
        if c['type'] in ('system-module', 'custom-module'):
            name = c.get('module_name') or c.get('target', '').rsplit('.', 1)[-1] or (owners[key].replace('.', '_').replace('-', '_') + '_' + c['id'].replace('-', '_'))
            target = 'module.' + ('system' if c['type'] == 'system-module' else 'custom') + '.' + name
            module_targets[key] = target
            if c['mode'] == 'add':
                catalog.setdefault(target, Target(c['type'], 'core'))
    environment = default_environment()
    py = python_version or platform.python_version()
    plat = platform_name or ('windows' if sys.platform == 'win32' else sys.platform)
    environment.update(python_full_version=py, python_version='.'.join(py.split('.')[:2]),
        sys_platform='win32' if plat == 'windows' else plat,
        platform_system='Windows' if plat == 'windows' else 'Linux', os_name='nt' if plat == 'windows' else 'posix')
    supplied = None if packages is None else {canonicalize_name(k): str(v) for k, v in packages.items()}
    def package_errors(items):
        errors = []
        for text in items:
            req = requirement(text)
            if req.marker and not req.marker.evaluate(environment):
                continue
            try:
                actual = supplied.get(canonicalize_name(req.name)) if supplied is not None else metadata.version(req.name)
                if actual is None:
                    raise metadata.PackageNotFoundError(req.name)
                if req.specifier and Version(actual) not in req.specifier:
                    errors.append(f'Несовместимая зависимость {text}; установлена {actual}.')
            except metadata.PackageNotFoundError:
                errors.append(f'Не установлена зависимость {text}. Автоустановка отключена.')
            except InvalidVersion:
                errors.append(f'Не удалось проверить версию {req.name}.')
        return errors
    blocked_plugins = {}
    for identity in enabled:
        m = by_id[identity]; compatibility = m['compatibility']; errors = []
        if compatibility['api'] != api_version:
            errors.append(f"Нужен API {compatibility['api']}; доступен {api_version}.")
        if compatibility.get('architectures') and architecture not in compatibility['architectures']:
            errors.append(f'Unsupported processor architecture: {architecture}.')
        if plat not in compatibility['platforms']:
            errors.append(f'Платформа {plat} не поддерживается.')
        if Version(py) not in SpecifierSet(compatibility['python']):
            errors.append(f"Нужен Python {compatibility['python']}; доступен {py}.")
        if 'toshy' in compatibility and (toshy_version is None or Version(toshy_version) not in SpecifierSet(compatibility['toshy'])):
            errors.append('Версия Toshy несовместима или неизвестна.')
        errors.extend(package_errors(m.get('requires_packages', [])))
        for dep, spec in m.get('requires_plugins', {}).items():
            if dep not in enabled or Version(by_id[dep]['version']) not in SpecifierSet(spec):
                errors.append(f'Нужен включённый плагин {dep}{spec}.')
        if errors:
            blocked_plugins[identity] = errors
    # Dependency cycles must be detected independently of load/list order.
    visiting, visited, stack = set(), set(), []
    def visit(key, edges, errors):
        if key in visiting:
            for member in stack[stack.index(key):]:
                errors.setdefault(member, []).append('Циклическая зависимость.')
            return
        if key in visited:
            return
        visiting.add(key); stack.append(key)
        for dep in edges.get(key, ()):
            if dep in edges:
                visit(dep, edges, errors)
        stack.pop(); visiting.remove(key); visited.add(key)
    edges = {p: list(by_id[p].get('requires_plugins', {})) for p in enabled}
    for key in edges:
        visit(key, edges, blocked_plugins)
    errors = {key: list(value) for key, value in (excluded or {}).items()}
    visiting.clear(); visited.clear(); stack.clear()
    edges = {}
    for key, c in components.items():
        if owners[key] not in enabled:
            continue
        dependencies = list(c.get('requires_components', []))
        for plugin in by_id[owners[key]].get('requires_plugins', {}):
            dependencies.extend(k for k in components if owners[k] == plugin)
        edges[key] = dependencies
    for key in edges:
        visit(key, edges, errors)
    for key, c in components.items():
        if owners[key] not in enabled:
            continue
        issues = package_errors(c.get('requires_packages', []))
        if key in module_targets and c['mode'] == 'add' and module_targets[key].rsplit('.', 1)[-1] in baseline_modules:
            issues.append('Имя модуля занято встроенным модулем. Для замены укажите mode=replace и точную цель.')
        target = c.get('target')
        if c['mode'] == 'extend':
            contract = catalog.get(target)
            if contract is None or c['operation'] not in OPERATIONS[contract.strategy]:
                issues.append(f'Недоступна точка расширения {target} / {c["operation"]}.')
        elif c['mode'] == 'replace':
            contract = catalog.get(target)
            if contract is None or contract.kind != c['type'] or contract.strategy != 'component':
                issues.append(f'Неизвестная или несовместимая цель замены {target}.')
        if issues:
            errors.setdefault(key, []).extend(issues)
    ranked = [p + '/' + c['id'] for p in order for c in by_id[p]['components']]
    def select():
        chosen, replacements, hidden, module_winners = [], {}, {}, {}
        for key in ranked:
            c = components[key]
            if owners[key] not in enabled or owners[key] in blocked_plugins or key in errors:
                continue
            target = c.get('target')
            if key in module_targets:
                name = module_targets[key].rsplit('.', 1)[-1]
                if name in module_winners:
                    hidden[key] = module_winners[name]; continue
                module_winners[name] = key
            if c['mode'] == 'replace':
                if target in replacements:
                    hidden[key] = replacements[target]; continue
                replacements[target] = key
            chosen.append(key)
        contracts = {name: target.version for name, target in catalog.items() if name in builtin_contracts}
        # Children belong to their provider. Replacement may explicitly preserve
        # their contract; they must never silently survive an incompatible swap.
        for target in replacements:
            for child in catalog[target].provides:
                contracts.pop(child, None)
        for key in chosen:
            if key in module_targets:
                contracts[module_targets[key]] = '1.0'
            for name, declaration in components[key].get('extension_points', {}).items():
                contracts[name] = declaration['version']
            # Highest-priority declaration wins an exclusive contract value.
            for name, version in components[key].get('provides_contracts', {}).items():
                if not any(name in components[earlier].get('provides_contracts', {}) for earlier in chosen[:chosen.index(key)]):
                    contracts[name] = version
        return chosen, replacements, hidden, contracts
    for _ in range(len(components) + len(by_id) + 1):
        chosen, replacements, hidden, contracts = select()
        old = (len(errors), len(blocked_plugins))
        chosen_set = set(chosen)
        for key in chosen:
            c = components[key]; issues = []
            for dependency in c.get('requires_components', []):
                if dependency not in chosen_set:
                    issues.append(f'Не работает необходимый компонент {dependency}.')
            required = dict(c.get('requires_contracts', {}))
            if c['mode'] == 'extend':
                required.setdefault(c['target'], '>=1,<2')
            for name, spec in required.items():
                if name not in contracts or Version(contracts[name]) not in SpecifierSet(spec):
                    issues.append(f'Недоступен необходимый интерфейс {name}{spec}.')
            if issues:
                errors.setdefault(key, []).extend(issues)
        for p in enabled:
            groups = {}
            for c in by_id[p]['components']:
                if c.get('group'):
                    groups.setdefault(c['group'], []).append(p + '/' + c['id'])
            for group, members in groups.items():
                if any(k in errors or k in hidden for k in members):
                    for member in members:
                        errors.setdefault(member, [f'Связанная группа {group} недоступна полностью.'])
            if p not in blocked_plugins:
                failed = [dep for dep in by_id[p].get('requires_plugins', {})
                    if dep in blocked_plugins or not any(owners[k] == dep and k not in errors for k in chosen)]
                if failed:
                    blocked_plugins[p] = ['Не работает обязательный плагин: ' + ', '.join(failed)]
        if old == (len(errors), len(blocked_plugins)):
            break
    chosen, replacements, hidden, contracts = select()
    statuses = {p: PluginStatus(p) for p in order}
    plan = PluginPlan(order, statuses, replacements=replacements, contracts=contracts)
    for key in chosen:
        c = components[key]; statuses[owners[key]].active_components.append(key)
        if c['mode'] == 'add':
            plan.additions.append(key)
        elif c['mode'] == 'extend':
            plan.extensions.setdefault(c['target'], []).append(key)
    for p in enabled:
        status = statuses[p]
        status.warnings.extend(blocked_plugins.get(p, []))
        for key in ranked:
            if owners[key] == p:
                status.warnings.extend(f'{key}: {message}' for message in errors.get(key, []))
    # Mark every participant, including the winner. A warning is distinct from
    # blocking: additive pieces of both plugins can remain usable.
    for loser, winner in hidden.items():
        if loser in module_targets:
            message = f'Конфликт имени модуля: {winner} имеет приоритет над {loser}.'
            for participant in {owners[loser], owners[winner]}:
                statuses[participant].warnings.append(message)
    for target, winner in replacements.items():
        contestants = [k for k in ranked if components[k]['mode'] == 'replace'
            and components[k].get('target') == target and owners[k] in enabled and k != winner]
        for loser in contestants:
            message = f'Конфликт {target}: {winner} имеет приоритет над {loser}.'
            for participant in {owners[loser], owners[winner]}:
                statuses[participant].warnings.append(message)
    for p in enabled:
        status = statuses[p]
        status.warnings = list(dict.fromkeys(status.warnings))
        total = len(by_id[p]['components'])
        status.state = ('blocked' if not status.active_components else
            'partial' if len(status.active_components) < total else 'ready')
    return plan
