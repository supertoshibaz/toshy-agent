"""In-place installation: code, .venv, elements and user data share one root.

Only verified release files are replaced. Staged source and the previous
release stay under runtime/installer; personal directories are never erased
by installation failure or source rollback.
"""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid
import threading
import copy
from . import layout, source, system
from . import UPDATER_VERSION
from .network import ReleaseError, sha256


def target(root, name):
    path = source.safe_name(name)
    cursor = Path(root)
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ReleaseError('Installer cannot write through a link: ' + str(cursor))
    return cursor


def workspace(root):
    path = target(root, 'runtime/installer')
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_environment(engine, program, config):
    from .engine import python_in
    uv = engine.uv(program)
    engine.run([uv, 'python', 'install', config['python']], env=engine.environment(), retry_network=True)
    venv = target(engine.root, '.venv')
    work = workspace(engine.root)
    version_file = work / 'environment.json'
    if python_in(venv).is_file():
        actual = layout.read_json(version_file).get('python')
        if not actual:
            actual = engine.run([python_in(venv), '-c', 'import platform; print(platform.python_version())'],
                                label='Checking existing Python environment').strip()
        if actual != config['python']:
            if Path(sys.executable).absolute().is_relative_to(venv):
                raise ReleaseError('Run install.sh or install.ps1 to replace this Python environment safely.')
            saved = work / ('environment-previous-' + uuid.uuid4().hex)
            layout.write_json(work / 'environment-recovery.json', {'directory': saved.name, 'python': actual})
            os.replace(venv, saved)
            engine.progress('Previous Python environment preserved in ' + str(saved))
    if not python_in(venv).is_file():
        engine.run([uv, 'venv', '--python', config['python'], venv], env=engine.environment(), retry_network=True)
    detected = engine.state()['system']
    lock = program / f'release_tools/locks/{detected["os"]}-x86_64.txt'
    engine.run([uv, 'pip', 'sync', '--python', python_in(venv), '--require-hashes', '--only-binary', ':all:', lock],
               env=engine.environment(), retry_network=True)
    layout.write_json(version_file, {'python': config['python']})
    return uv


def restore_environment_directory(engine):
    work = workspace(engine.root)
    record = layout.read_json(work / 'environment-recovery.json')
    name = record.get('directory', '')
    if not name or not re.fullmatch(r'environment-previous-[0-9a-f]{32}', name):
        return
    saved = target(engine.root, 'runtime/installer/' + name)
    if saved.is_dir():
        venv = target(engine.root, '.venv')
        if venv.exists():
            shutil.rmtree(venv)
        os.replace(saved, venv)
        layout.write_json(work / 'environment.json', {'python': record.get('python', '')})
    (work / 'environment-recovery.json').unlink(missing_ok=True)


def restore_environment(engine, program):
    # A cancelled upgrade must restore its old lock before returning. A second
    # Ctrl+C must not interrupt an atomic recovery half-way through pip sync.
    restore_environment_directory(engine)
    original_cancel = engine.cancel
    network_cancel = engine.network.cancel
    engine.cancel = engine.network.cancel = threading.Event()
    engine.recovering = True
    try:
        return sync_environment(engine, program, source.verify(program))
    finally:
        engine.recovering = False
        engine.cancel = original_cancel
        engine.network.cancel = network_cancel


def replace_source(root, program, previous=None):
    """Replace individual files atomically, retaining unowned/modified files."""
    config = source.verify(program)
    manifest = layout.read_json(program / 'release-manifest.json')['files']
    old = (previous or {}).get('files', {})
    # Check the whole transaction before replacing the first source file.
    for name, digest in manifest.items():
        out = target(root, name)
        if out.exists() and (not out.is_file() or (
                sha256(out) != digest and (name not in old or sha256(out) != old[name]))):
            raise ReleaseError('Preserved a locally modified file; resolve it before updating: ' + name)
    for name in [*manifest, 'release-manifest.json']:
        out = target(root, name)
        origin = program / name
        if out.is_file() and sha256(out) == sha256(origin):
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name('.' + out.name + '.' + uuid.uuid4().hex + '.install-tmp')
        if temporary.exists() or temporary.is_symlink():
            raise ReleaseError('An installer temporary file already exists: ' + str(temporary))
        try:
            with origin.open('rb') as src, temporary.open('xb') as dest:
                shutil.copyfileobj(src, dest); dest.flush(); os.fsync(dest.fileno())
            shutil.copymode(origin, temporary)
            os.replace(temporary, out)
        finally:
            temporary.unlink(missing_ok=True)
    for name, digest in old.items():
        if name not in manifest:
            out = target(root, name)
            if out.is_file() and sha256(out) == digest:
                out.unlink()
    return config


def recover(engine, state):
    journal = layout.read_json(engine.root / 'transaction.json')
    if journal.get('status') not in {'activating-portable', 'awaiting-startup'}:
        return
    owner = journal.get('updater_pid')
    if owner and owner != os.getpid():
        try:
            os.kill(int(owner), 0)
        except ProcessLookupError:
            pass
        else:
            raise ReleaseError('An update is still running. Wait for its window to finish.')
    if journal.get('status') == 'awaiting-startup' and journal.get('ready_file') and journal.get('ready_token'):
        try:
            ready = layout.read_json(journal['ready_file'])
        except (OSError, ValueError):
            ready = {}
        if (ready.get('token') == journal['ready_token'] and ready.get('pid')
                and (not journal.get('trial_pid') or ready['pid'] == journal['trial_pid'])):
            # A healthy GUI may already have accepted new user work before the
            # helper crashed. Never restore its pre-update data over that work.
            engine.journal = journal
            complete_activation(engine)
            return
    layout.require_stopped(engine.root)
    if journal.get('update_protocol') == 2:
        undo_activation(engine)
        return
    previous = workspace(engine.root) / 'recovery'
    if not previous.is_dir() and not state.get('current'):
        source.verify(workspace(engine.root) / 'pending')
        engine.record(status='prepared')
        return
    if not previous.is_dir():
        raise ReleaseError('Interrupted activation needs its runtime/installer/recovery copy. Files were preserved.')
    engine.progress('Restoring the previous source after interrupted activation…')
    if journal.get('backup'):
        from .migrations import restore_backup
        restore_backup(engine.root, journal['backup'])
    _restore_source(engine.root, previous, journal.get('incoming', {}))
    if state.get('current'):
        restore_environment(engine, previous)
    else:
        restore_environment_directory(engine)
    engine.record(status='prepared')


def _restore_source(root, saved, incoming):
    # Recovery source is already verified. Changes from the interrupted version
    # are installer-owned, so they may be overwritten by its prior snapshot.
    source.verify(saved)
    replace_source(root, saved, {'files': dict(incoming)})


def prepare(engine, *, repository='', local_source=None, components=(), skip_system=False, release_info=None):
    from .engine import python_in
    root = engine.root
    if root.is_symlink() or root in (Path.home(), Path(root.anchor)):
        raise ReleaseError('Choose a private Toshy directory, not a home or filesystem root.')
    original = Path(local_source).resolve() if local_source else None
    existing = layout.read_json(root / 'installation.json')
    if existing and not layout.is_portable(root, existing):
        raise ReleaseError('This installation uses the old layout. Install into the unpacked Toshy source folder.')
    if not existing and root.exists():
        entries = {p.name for p in root.iterdir()}
        bootstrap = root / '.toshy-bootstrap'
        permitted = {'tools', 'cache', '.toshy-bootstrap', '.install-lock'}
        from_source = original == root.resolve() and (root / 'release-manifest.json').is_file()
        if from_source:
            source.verify(root)
        elif entries and (entries - permitted or not bootstrap.is_file()
                          or Path(bootstrap.read_text(encoding='utf-8-sig').strip()).absolute() != root):
            raise ReleaseError('Choose an empty directory or the unpacked verified Toshy source folder.')
    detected = system.detect()
    with layout.lock(root):
        if not existing:
            layout.write_json(root / 'installation.json', {'product': 'Toshy', 'schema': 1, 'layout': 'portable',
                'root': str(root), 'system': detected, 'system_packages': [], 'external_files': [], 'components': []})
        state = engine.state()
        if layout.running(root) and not state.get('current'):
            layout.require_stopped(root)
        recover(engine, state)
        work = workspace(root)
        stage = work / 'pending'
        old_source = work / 'before-prepare'
        if stage.is_symlink() or old_source.is_symlink():
            raise ReleaseError('Installer staging directories cannot be links.')
        # Keep a verified source/lock copy before preparing the new environment.
        if state.get('current'):
            source.verify(root)
            shutil.rmtree(old_source, ignore_errors=True)
            source.copy_source(root, old_source)
        engine.record(status='preparing-portable')
        temp = None
        environment_changed = False
        try:
            if shutil.disk_usage(root).free < 3 * 1024 ** 3:
                raise ReleaseError('At least 3 GiB of free space is required for installation.')
            if original:
                config = source.verify(original)
                info = {'version': config['version'], 'commit': 'local-' + sha256(original / 'release-manifest.json')[:16],
                        'repository': repository or config.get('repository', ''), 'notes': '', 'published_at': '', 'url': ''}
            else:
                repo = source.repository(repository or state.get('repository', ''))
                info = dict(release_info or source.latest(engine.network, repo))
                source.version(info.get('version'))
                if info.get('repository') != repo or not re.fullmatch('[0-9a-f]{40}', info.get('commit', '')):
                    raise ReleaseError('Invalid selected release identity.')
                archive = root / 'cache' / (info['commit'] + '.zip')
                engine.network.download(f'https://api.github.com/repos/{repo}/zipball/{info["commit"]}', archive)
                temp = Path(tempfile.mkdtemp(prefix='source-', dir=work))
                original = source.extract(archive, temp, strip_root=True)
                config = source.verify(original, info['version'])
            if (config.get('minimum_updater', 1) > UPDATER_VERSION
                    or config.get('update_protocol', 1) > 2
                    or detected['os'] + '-x86_64' not in config['platforms']):
                raise ReleaseError('The release needs a newer installer or does not support this platform.')
            from .migrations import scripts
            scripts(original, state.get('data_schema', config['data_schema']), config['data_schema'])
            state['pending'] = None
            layout.write_json(root / 'installation.json', state)
            if original != stage:
                shutil.rmtree(stage, ignore_errors=True)
                source.copy_source(original, stage)
            if state.get('current'):
                from .components import select, prerequisites
                from .environments import build
                components = select(stage, components)
                prerequisites(engine, components, consent=getattr(engine, 'system_consent', False))
                engine.progress('Preparing the new environment while Toshy remains available…')
                runtime = build(engine, stage, config, components=components)
                state = engine.state()
                info.update(runtime, key='v' + config['version'] + '-' + info['commit'][:16],
                            manifest_sha256=sha256(stage / 'release-manifest.json'),
                            data_schema=config['data_schema'], needs_environment=False, update_protocol=2)
                (stage / '.complete').touch()
                state.update(pending=info, repository=info['repository'], components=list(components))
                layout.write_json(root / 'installation.json', state)
                engine.record(status='prepared')
                engine.progress('The update and its dependencies are ready. Toshy can now restart.')
                return info
            from .components import select
            components = select(stage, components)
            if not skip_system:
                engine.packages(detected, components)
            # packages() updates the ownership receipt; never overwrite it with
            # the stale state read before system package installation.
            state = engine.state()
            environment_changed = True
            uv = sync_environment(engine, stage, config)
            lsp = engine.resources(stage, uv, config, components=components)
            engine.verify_runtime(stage)
            if 'ocr' in components:
                env = engine.environment()
                env.update(PYTHONPATH=str(stage), TOSHY_DATA_DIR=str(root))
                engine.run([python_in(root / '.venv'), '-c',
                    'from core.paths import APP_PATHS; from modules.system.file_manager.documents.engines import private_ocr; '
                    't=private_ocr(); assert t is not None; a=t.PyTessBaseAPI(path=str(APP_PATHS.elements/"ocr/tessdata"),lang="eng+rus"); a.End(); print("Private OCR ready")'],
                    env=env, label='Checking private OCR')
            interpreter = engine.run([uv, 'python', 'find', config['python']], env=engine.environment()).strip().splitlines()[-1]
            info.update(key='v' + config['version'] + '-' + info['commit'][:16],
                        manifest_sha256=sha256(stage / 'release-manifest.json'), lsp=lsp,
                        data_schema=config['data_schema'], launcher_python=interpreter)
            (stage / '.complete').touch()
            state.update(pending=info, repository=info['repository'], components=list(components))
            layout.write_json(root / 'installation.json', state)
            engine.record(status='prepared')
            engine.progress('Installation prepared in ' + str(root))
            return info
        except BaseException:
            restore_environment_directory(engine)
            if environment_changed and state.get('current') and old_source.is_dir():
                try:
                    restore_environment(engine, old_source)
                except BaseException as recovery_error:
                    engine.progress('Environment recovery requires repair: ' + str(recovery_error))
            engine.record(status='failed')
            raise
        finally:
            if temp:
                shutil.rmtree(temp, ignore_errors=True)


def activate(engine, *, shortcuts=True, wait_for_startup=False):
    from .engine import python_in
    from .migrations import scripts, restore_backup
    root = engine.root
    with layout.lock(root):
        state = engine.state()
        layout.require_stopped(root)
        recover(engine, state)
        state = engine.state()
        info = dict(state.get('pending') or {})
        if not info:
            raise ReleaseError('No prepared installation.')
        work = workspace(root)
        stage = work / 'pending'
        config = source.verify(stage, info['version'])
        if info.get('manifest_sha256') and sha256(stage / 'release-manifest.json') != info['manifest_sha256']:
            raise ReleaseError('The prepared source changed. Prepare the installation again.')
        if not (stage / '.complete').is_file():
            raise ReleaseError('The prepared installation is incomplete.')
        current = state.get('current')
        recovery = work / 'recovery'
        if recovery.is_symlink():
            raise ReleaseError('Installer recovery directory cannot be a link.')
        shutil.rmtree(recovery, ignore_errors=True)
        previous_manifest = {}
        # On first in-place install the source is already present, and is still
        # a useful recovery point if shortcut creation or verification fails.
        if (root / 'release-manifest.json').is_file():
            source.verify(root)
            source.copy_source(root, recovery)
            previous_manifest = layout.read_json(recovery / 'release-manifest.json')
        migrations = scripts(stage, state.get('data_schema', config['data_schema']), config['data_schema'])
        if current:
            engine.progress('Backing up session data before applying the update…')
        backup = layout.backup_data(root) if current else None
        lsp_path = target(root, 'elements/lsp/lsp_config.json')
        lsp_before = {'exists': lsp_path.is_file(), 'text': lsp_path.read_text(encoding='utf-8') if lsp_path.is_file() else ''}
        engine.record(status='activating-portable', backup=str(backup) if backup else '',
                      incoming=layout.read_json(stage / 'release-manifest.json')['files'],
                      previous_state=copy.deepcopy(state), lsp_before=lsp_before,
                      updater_pid=os.getpid(), update_protocol=2,
                      ready_file='', ready_token='', trial_pid=None)
        try:
            from .environments import build, environment, resource_root
            if info.get('needs_environment'):
                # A prepared update from the older installer is upgraded without
                # touching the active .venv.
                from .components import select, prerequisites
                components = select(stage, state.get('components', []))
                prerequisites(engine, components, consent=getattr(engine, 'system_consent', False))
                info.update(build(engine, stage, config, components=components), needs_environment=False)
            for script in migrations:
                engine.run([python_in(environment(root, info, state)), script, root], label='Migrating application data')
            replace_source(root, stage, previous_manifest)
            layout.seed_data(root, root)
            path = root / 'elements/lsp/lsp_config.json'
            conf = layout.read_json(path, {'servers': {}})
            defaults = layout.read_json(root / 'elements/lsp/lsp_defaults.json')
            from .lsp_config import configure
            configure(conf, defaults, info['lsp'])
            layout.write_json(path, conf)
            engine.verify_runtime(root, environment=environment(root, info, state), data=resource_root(root, info))
            if current and (current['key'] != info['key'] or current.get('environment') != info.get('environment')):
                state['previous'] = current
            state.update(current=info, pending=None, data_schema=config['data_schema'], launcher_python=info['launcher_python'])
            if backup:
                state['backup'] = str(backup)
            launcher = root / 'launcher'
            if launcher.is_symlink():
                raise ReleaseError('Launcher directory cannot be a link.')
            launcher.mkdir(exist_ok=True)
            shutil.copy2(root / 'toshy.py', launcher / 'toshy.py')
            shutil.copytree(root / 'release_tools', launcher / 'release_tools', dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
            from .shortcuts import create
            state = create(root, state, desktop=shortcuts)
            (root / '.complete').touch()
            layout.write_json(root / 'installation.json', state)
            (work / 'environment-recovery.json').unlink(missing_ok=True)
        except BaseException:
            undo_activation(engine)
            raise
        if wait_for_startup and current:
            engine.record(status='awaiting-startup')
        else:
            complete_activation(engine)
        engine.progress('Toshy installed in ' + str(root))
        from .shortcuts import summarize
        summarize(state, engine.progress)
        return state


def undo_activation(engine):
    """Restore source, data, runtime selection and launchers as one recovery."""
    journal = layout.read_json(engine.root / 'transaction.json')
    previous_state = journal.get('previous_state')
    saved = workspace(engine.root) / 'recovery'
    reserved = workspace(engine.root) / 'previous-reserved'
    if reserved.is_dir() and not reserved.is_symlink():
        previous = workspace(engine.root) / 'previous'
        if previous.is_symlink():
            raise ReleaseError('Previous source cannot be a link.')
        shutil.rmtree(previous, ignore_errors=True)
        os.replace(reserved, previous)
    if journal.get('backup'):
        from .migrations import restore_backup
        restore_backup(engine.root, journal['backup'])
    if saved.is_dir():
        _restore_source(engine.root, saved, journal.get('incoming', {}))
    lsp_before = journal.get('lsp_before')
    if lsp_before is not None:
        path = target(engine.root, 'elements/lsp/lsp_config.json')
        if lsp_before['exists']:
            layout.write_json(path, json.loads(lsp_before['text']))
        else:
            path.unlink(missing_ok=True)
    if previous_state:
        layout.write_json(engine.root / 'installation.json', previous_state)
        if previous_state.get('current'):
            _publish_launchers(engine.root, previous_state)
    engine.record(status='rolled-back')
    engine.progress('The previous version and its environment have been restored.')


def _publish_launchers(root, state):
    launcher = target(root, 'launcher')
    launcher.mkdir(exist_ok=True)
    shutil.copy2(Path(root) / 'toshy.py', launcher / 'toshy.py')
    shutil.copytree(Path(root) / 'release_tools', launcher / 'release_tools', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    from .shortcuts import create
    state = create(root, state)
    layout.write_json(Path(root) / 'installation.json', state)


def complete_activation(engine):
    state = engine.state()
    original = (layout.read_json(engine.root / 'transaction.json').get('previous_state') or {}).get('current') or {}
    if state.get('previous') == original and (workspace(engine.root) / 'recovery').is_dir():
        previous = workspace(engine.root) / 'previous'
        replacement = workspace(engine.root) / 'previous-next'
        reserved = workspace(engine.root) / 'previous-reserved'
        if any(path.is_symlink() for path in (previous, replacement, reserved)):
            raise ReleaseError('Previous source cannot be a link.')
        shutil.rmtree(replacement, ignore_errors=True)
        source.copy_source(workspace(engine.root) / 'recovery', replacement)
        shutil.rmtree(reserved, ignore_errors=True)
        if previous.exists():
            os.replace(previous, reserved)
        os.replace(replacement, previous)
    engine.record(status='complete', previous_state=None)
    from .environments import prune
    try:
        prune(engine, state)
        shutil.rmtree(workspace(engine.root) / 'previous-reserved', ignore_errors=True)
        retained = {str(state.get('backup') or ''), str(engine.journal.get('backup') or '')}
        for path in (engine.root / 'backups').glob('before-update-*'):
            if not path.is_symlink() and str(path) not in retained and (path / '.toshy-update-backup').is_file():
                shutil.rmtree(path)
    except Exception as error:
        engine.progress('Cleanup deferred: ' + str(error))


def rollback(engine):
    root = engine.root
    with layout.lock(root):
        state = engine.state()
        layout.require_stopped(root)
        old = state.get('previous')
        if not old or old.get('data_schema') != state.get('data_schema'):
            raise ReleaseError('No previous version with a compatible data schema.')
        work = workspace(root)
        previous = work / 'previous'
        config = source.verify(previous, old['version'])
        pending = work / 'pending'
        if pending.is_symlink():
            raise ReleaseError('Prepared source cannot be a link.')
        shutil.rmtree(pending, ignore_errors=True)
        source.copy_source(previous, pending)
        (pending / '.complete').touch()
        # Legacy releases need an isolated environment on first rollback.
        if not old.get('environment'):
            from .environments import build
            old = dict(old, **build(engine, pending, config, components=state.get('components', [])))
        state['pending'] = dict(old, manifest_sha256=sha256(pending / 'release-manifest.json'))
        layout.write_json(root / 'installation.json', state)
    return activate(engine)


def uninstall_files(root, state, progress):
    """Disconnect a source-folder installation without erasing personal files."""
    root = Path(root)
    manifest = layout.read_json(root / 'release-manifest.json').get('files', {})
    for name, digest in manifest.items():
        path = target(root, name)
        if path.is_file() and sha256(path) == digest:
            path.unlink()
        elif path.exists():
            progress('Preserved a modified file: ' + name)
    state.update(current=None, pending=None, kept_data=True)
    layout.write_json(root / 'installation.json', state)
    progress('Toshy source and registered shortcuts removed. User data and local environments remain in ' + str(root))
    return 0
