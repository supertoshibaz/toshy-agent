"""Immutable managed environments and resources, prepared beside the running app."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid
from . import layout
from .network import ReleaseError, sha256


def valid_files(root, hashes):
    from .source import safe_name
    if not isinstance(hashes, dict) or not hashes or len(hashes) > 100000:
        return False
    try:
        for name, digest in hashes.items():
            path = root / safe_name(name)
            if (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                    or path.is_symlink() or not path.is_file() or sha256(path) != digest):
                return False
    except (OSError, ValueError, ReleaseError, TypeError):
        return False
    return True


def owned_path(root, relative, prefix):
    if not isinstance(relative, str) or not re.fullmatch(re.escape(prefix) + r'/[a-f0-9]{32}', relative):
        raise ReleaseError('Invalid managed runtime path.')
    path = Path(root)
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink():
            raise ReleaseError('Managed runtime paths cannot contain links.')
    return path


def environment(root, info=None, state=None):
    info = info or (state or layout.owned(root)).get('current') or {}
    if info.get('environment'):
        return owned_path(root, info['environment'], 'runtime/environments')
    program = layout.program_root(root, info, state)
    return program / '.venv'


def resource_root(root, info):
    relative = (info or {}).get('resources')
    return owned_path(root, relative, 'elements/managed') if relative else layout.data_root(root)


def build(engine, program, config, *, components=()):
    """Never install into or rename the active interpreter, even on Windows."""
    from .engine import python_in
    root = engine.root
    identity = uuid.uuid4().hex
    relative = 'runtime/environments/' + identity
    venv = owned_path(root, relative, 'runtime/environments')
    venv.parent.mkdir(parents=True, exist_ok=True)
    info = {'environment': relative}
    layout.write_json(venv / '.toshy-owned.json', {'product': 'Toshy runtime', 'schema': 1})
    try:
        uv = engine.uv(program)
        engine.run([uv, 'python', 'install', config['python']], env=engine.environment(), retry_network=True)
        engine.run([uv, 'venv', '--allow-existing', '--python', config['python'], venv],
                   env=engine.environment(), retry_network=True, label='Creating the new Python environment')
        lock = Path(program) / f'release_tools/locks/{engine.state()["system"]["os"]}-x86_64.txt'
        engine.run([uv, 'pip', 'sync', '--python', python_in(venv), '--require-hashes',
                    '--only-binary', ':all:', lock], env=engine.environment(), retry_network=True,
                   label='Installing the new release dependencies')
        engine.run([uv, 'pip', 'check', '--python', python_in(venv)], env=engine.environment(),
                   label='Checking installed dependency compatibility')
        info['launcher_python'] = engine.run([uv, 'python', 'find', config['python']],
            env=engine.environment(), label='Locating the managed Python').strip().splitlines()[-1]
        # A changed resource manifest gets its own immutable directory. This
        # includes the Python ABI used by OCR and the language-server lock.
        fingerprint = hashlib.sha256((sha256(Path(program) / 'release_tools/resources.json') +
            sha256(Path(program) / f'release_tools/locks/pylsp-{engine.state()["system"]["os"]}-x86_64.txt') +
            config['python'] + engine.state()['system']['os'] + json.dumps(sorted(components))).encode()).hexdigest()
        bundles = root / 'elements/managed'
        owned_path(root, 'elements/managed/' + '0' * 32, 'elements/managed')
        bundles.mkdir(parents=True, exist_ok=True)
        resources = None
        partial_resources = None
        for path in bundles.iterdir():
            if path.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.name):
                continue
            try:
                receipt = layout.read_json(path / '.toshy-owned.json')
            except (OSError, ValueError):
                continue
            if not isinstance(receipt, dict):
                continue
            if (receipt.get('product') == 'Toshy resources' and receipt.get('fingerprint') == fingerprint
                    and not receipt.get('complete')):
                partial_resources = path
            hashes = receipt.get('files') or {}
            if (receipt.get('product') == 'Toshy resources' and receipt.get('fingerprint') == fingerprint
                    and receipt.get('complete') and valid_files(path, hashes)):
                resources = path
                info['lsp'] = receipt['lsp']
                break
        if resources is None:
            resources = partial_resources or owned_path(root, 'elements/managed/' + uuid.uuid4().hex, 'elements/managed')
            layout.write_json(resources / '.toshy-owned.json', {'product': 'Toshy resources', 'fingerprint': fingerprint})
            info['lsp'] = engine.resources(program, uv, config, components=components, data=resources)
            hashes = {p.relative_to(resources).as_posix(): sha256(p) for p in resources.rglob('*')
                if p.is_file() and not p.is_symlink() and p.name != '.toshy-owned.json'
                and '__pycache__' not in p.parts and 'downloads' not in p.parts and 'cache' not in p.parts}
            layout.write_json(resources / '.toshy-owned.json', {'product': 'Toshy resources',
                'fingerprint': fingerprint, 'complete': all(info['lsp'].values()), 'lsp': info['lsp'], 'files': hashes})
        info['resources'] = resources.relative_to(root).as_posix()
        engine.verify_runtime(program, environment=venv, data=resources)
        if 'ocr' in components:
            from .ocr import runtime_info
            pins = layout.read_json(Path(program) / 'release_tools/resources.json')
            _, ocr, _ = runtime_info(resources / 'elements',
                python_tag='cp' + ''.join(config['python'].split('.')[:2]), resource_pins=pins)
            engine.run([python_in(venv), '-c',
                'import sys;sys.path.insert(0,sys.argv[1]);import tesserocr;'
                'a=tesserocr.PyTessBaseAPI(path=sys.argv[2],lang="eng+rus");a.End();print("Private OCR ready")',
                ocr, resources / 'elements/ocr/tessdata'], label='Checking private OCR for the new Python')
        layout.write_json(venv / '.toshy-owned.json', {'product': 'Toshy runtime',
            'schema': 1, 'complete': True, 'python': config['python'], 'lock': sha256(lock)})
        return info
    except BaseException:
        # Only this unfinished environment belongs to this operation. Completed
        # resource downloads can be reused by the next attempt.
        shutil.rmtree(venv, ignore_errors=True)
        raise


def launch(root, state, *, info=None, ready_file=None, native_host=False):
    from .engine import python_in
    root = Path(root)
    info = info or state['current']
    program = layout.program_root(root, info, state)
    venv = environment(root, info, state)
    env = os.environ.copy()
    env.pop('TOSHY_MANAGED_ELEMENTS', None)
    env.pop('TOSHY_UPDATE_READY_FILE', None)
    env.pop('TOSHY_UPDATE_READY_TOKEN', None)
    env.pop('TOSHY_UPDATE_TRIAL_DIR', None)
    if info.get('resources'):
        env['TOSHY_MANAGED_ELEMENTS'] = str(resource_root(root, info) / 'elements')
    env.update(TOSHY_DATA_DIR=str(layout.data_root(root, state)), TOSHY_INSTALL_ROOT=str(root),
               TOSHY_APP_ROOT=str(program), UV_CACHE_DIR=str(root / 'cache/uv'),
               UV_TOOL_DIR=str(layout.data_root(root, state) / 'runtime/uv-tools'),
               PYTHONPYCACHEPREFIX=str(root / 'cache/pycache' / info['key']))
    env['PATH'] = os.pathsep.join([str(python_in(venv).parent), str(root / 'tools'), env.get('PATH', '')])
    if ready_file:
        env['TOSHY_UPDATE_READY_FILE'] = str(ready_file)
    host = root / 'launcher/Toshy.exe'
    if os.name == 'nt' and native_host:
        from .windows_host import available
        if available(root, python_in(venv)):
            return [str(host)], env
    return [str(python_in(venv)), str(program / 'run_ui.py')], env


def prune(engine, state):
    """Keep active, previous and prepared runtimes; leave unowned files alone."""
    keep = [state.get(k) or {} for k in ('current', 'previous', 'pending')]
    # The independent update window still uses the previous environment.
    try:
        active = Path(__import__('sys').prefix).relative_to(engine.root).as_posix()
        keep.append({'environment': active})
    except ValueError:
        pass
    for prefix, field, product in [('runtime/environments', 'environment', 'Toshy runtime'),
                                    ('elements/managed', 'resources', 'Toshy resources')]:
        retained = {item.get(field) for item in keep}
        parent = engine.root / prefix
        if not parent.is_dir() or parent.is_symlink():
            continue
        for path in parent.iterdir():
            relative = path.relative_to(engine.root).as_posix()
            if relative in retained or path.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.name):
                continue
            try:
                if layout.read_json(path / '.toshy-owned.json').get('product') == product:
                    shutil.rmtree(path)
            except OSError as error:
                engine.progress('Runtime cleanup deferred: ' + str(error))
