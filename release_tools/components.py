"""Release dependency contract. No shell commands are accepted from manifests."""
from pathlib import Path
from . import layout, system
from .network import ReleaseError, sha256

PRIVATE = {'python', 'harrier', 'pylsp', 'harper', 'browser', 'ocr'}
SYSTEM = {'git', 'docker'}
BASE = {'python', 'harrier', 'pylsp', 'harper'}


class SystemDependenciesRequired(ReleaseError):
    def __init__(self, components, packages):
        self.components, self.packages = list(components), list(packages)
        super().__init__('System components need administrator approval: ' + ', '.join(packages))


def describe(program):
    program = Path(program)
    config = layout.read_json(program / 'release.json')
    required = config.get('required_components', sorted(BASE))
    if (not isinstance(required, list) or any(not isinstance(item, str) for item in required)
            or not BASE <= set(required) or set(required) - PRIVATE - SYSTEM):
        raise ReleaseError('Unknown or missing required release components.')
    pins = layout.read_json(program / 'release_tools/resources.json')
    return {'schema': 1, 'python': config['python'], 'required': sorted(set(required)),
            'inputs': {name: sha256(program / name) for name in ('requirements.txt', 'release_tools/pylsp.in')},
            'optional': sorted((PRIVATE | SYSTEM) - set(required)),
            'locks': {name: sha256(program / 'release_tools/locks' / name)
                      for name in ('linux-x86_64.txt', 'windows-x86_64.txt',
                                   'pylsp-linux-x86_64.txt', 'pylsp-windows-x86_64.txt')},
            'resources_sha256': sha256(program / 'release_tools/resources.json'),
            'versions': {name: pins[name].get('version', pins[name].get('revision', ''))
                         for name in ('uv', 'browser', 'harper', 'harrier', 'ocr')}}


def verify(program):
    path = Path(program) / 'release_tools/components.json'
    expected = describe(program)
    if path.exists() and layout.read_json(path) != expected:
        raise ReleaseError('Release dependency manifest is stale. Regenerate locks and inventory.')
    return expected


def select(program, components):
    contract = verify(program)
    selected = set(components) | (set(contract['required']) - BASE)
    if selected - (PRIVATE | SYSTEM):
        raise ReleaseError('Unknown selected release components.')
    return sorted(selected)


def prerequisites(engine, components, *, consent=False):
    detected = engine.state()['system']
    plan = system.package_plan(detected, components)
    if not plan:
        return
    if not consent:
        raise SystemDependenciesRequired(components, [name for name, _ in plan])
    # Elevated execution is restricted to the built-in OS package plan. User
    # release manifests cannot provide executables, scripts or arbitrary paths.
    engine.gui_elevation = True
    try:
        engine.packages(detected, components)
    finally:
        engine.gui_elevation = False


def write(program):
    value = describe(program)
    layout.write_json(Path(program) / 'release_tools/components.json', value)
    return value
