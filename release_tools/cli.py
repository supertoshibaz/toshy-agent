"""Public installer, launcher, diagnostics, update and uninstall commands."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from . import layout, source, system
from .engine import Installer, python_in
from .network import Network, ReleaseError


def gui(root, arguments=()):
    journal = layout.read_json(Path(root) / 'transaction.json')
    trial_token = os.environ.get('TOSHY_UPDATE_READY_TOKEN')
    own_trial = (journal.get('status') == 'awaiting-startup' and bool(trial_token)
                 and trial_token == journal.get('ready_token')
                 and os.environ.get('TOSHY_UPDATE_READY_FILE') == journal.get('ready_file'))
    if journal.get('status') in {'activating-portable', 'awaiting-startup'} and not own_trial:
        owner = journal.get('updater_pid')
        if owner and owner != os.getpid():
            try:
                active_owner = layout._process_running(int(owner))
            except ProcessLookupError:
                pass
            else:
                if active_owner:
                    raise ReleaseError('Toshy is being updated. Wait for the update window to finish.')
        from .portable import recover
        with layout.lock(root):
            recover(Installer(root), layout.owned(root))
    state = layout.owned(root)
    current = state.get('current')
    if not current:
        raise ReleaseError('No active version. Finish installation or run repair.')
    program = layout.program_root(root, current, state)
    if not (program / '.complete').is_file():
        raise ReleaseError('Active version is incomplete. Run repair.')
    from .environments import launch, environment as runtime_environment
    command, environment = launch(root, state)
    if os.name == 'nt':
        if os.environ.get('TOSHY_GUI_PROCESS') == str(os.getpid()):
            # The dedicated host already initialized the selected virtual env.
            # Preserve the updater's one-use startup acknowledgement.
            for key in ('TOSHY_UPDATE_READY_FILE', 'TOSHY_UPDATE_READY_TOKEN', 'TOSHY_UPDATE_TRIAL_DIR'):
                if os.environ.get(key):
                    environment[key] = os.environ[key]
        directories = []
        for base in filter(None, (os.environ.get('PROGRAMFILES'), os.environ.get('LOCALAPPDATA'))):
            directories.extend(str(Path(base) / tail) for tail in ('Git/cmd', 'Programs/Git/cmd', 'Docker/Docker/resources/bin') if (Path(base) / tail).is_dir())
        directories.extend(str(Path(p).parent) for p in (system.executable('git'), system.executable('docker')) if p)
        environment['PATH'] = os.pathsep.join([*directories, environment['PATH']])
        if os.environ.get('TOSHY_GUI_PROCESS') == str(os.getpid()):
            # python.exe and pythonw.exe share the same virtual environment.
            # Compare its prefix, not the interpreter executable's filename.
            expected_prefix = os.path.normcase(str(runtime_environment(root, current, state).resolve()))
            actual_prefix = os.path.normcase(str(Path(sys.prefix).resolve()))
            if actual_prefix != expected_prefix:
                # An old pinned host must not import a new release with retired dependencies.
                if environment.get('TOSHY_RUNTIME_HANDOFF') == expected_prefix:
                    raise ReleaseError('Could not select the active Python environment after one retry. '
                                       f'Expected: {expected_prefix}; current: {actual_prefix}. Run install.ps1 again.')
                print('The launcher environment is stale; switching to the active Python environment.', flush=True)
                environment['TOSHY_RUNTIME_HANDOFF'] = expected_prefix
                environment.pop('TOSHY_WINDOWS_HOST_PROCESS', None)
                environment.pop('TOSHY_GUI_PROCESS', None)
                startup = Path(root) / 'launcher/release_tools/windows_startup.py'
                pythonw = Path(command[0]).with_name('pythonw.exe')
                os.execve(str(pythonw), [str(pythonw), str(startup), '--toshy-inprocess', *arguments], environment)
            environment.pop('TOSHY_RUNTIME_HANDOFF', None)
            os.environ.pop('TOSHY_RUNTIME_HANDOFF', None)
            import runpy
            os.environ.update(environment)
            sys.path.insert(0, str(program))
            sys.argv = [str(program / 'run_ui.py'), *arguments]
            runpy.run_path(str(program / 'run_ui.py'), run_name='__main__')
            return
        from .windows_host import available
        host = Path(root) / 'launcher/Toshy.exe'
        if available(root, command[0]):
            os.execve(str(host), [str(host), *arguments], environment)
        startup = Path(root) / 'launcher/release_tools/windows_startup.py'
        if startup.is_file():
            pythonw = Path(command[0]).with_name('pythonw.exe')
            os.execve(str(pythonw), [str(pythonw), str(startup), '--toshy-inprocess', *arguments], environment)
    python = Path(command[0])
    if os.name == 'nt' and Path(sys.executable).name.lower() == 'pythonw.exe':
        python = python.with_name('pythonw.exe')
    os.execve(str(python), [str(python), str(program / 'run_ui.py'), *arguments], environment)


def _uninstall_locked(root, *, keep_data=False, remove_system=False, yes=False, progress=print):
    root = Path(root)
    state = layout.owned(root)
    layout.require_stopped(root)
    progress('Remove installation: ' + str(root))
    progress('User data: ' + ('keep' if keep_data or layout.is_portable(root, state) else 'DELETE'))
    packages = state.get('system_packages', []) if remove_system else []
    progress('Remove newly installed shared packages: ' + (', '.join(packages) or 'none'))
    if 'Docker.DockerDesktop' in packages:
        progress('Removing Docker Desktop can delete its Docker data for other applications too.')
    if not yes and input('Continue? [y/N] ').strip().lower() != 'y':
        return 1
    if not keep_data and not layout.is_portable(root, state) and (layout.data_root(root, state) / 'runtime/installed-credentials.sqlite').exists():
        current = state.get('current')
        if current:
            program = root / 'versions' / current['key']
            environment = os.environ.copy()
            environment.update(TOSHY_DATA_DIR=str(layout.data_root(root, state)), PYTHONPATH=str(program))
            subprocess.run([str(python_in(program / '.venv')), '-c',
                            'import sys; from release_tools.credentials import clean; clean(sys.argv[1])', str(layout.data_root(root, state))],
                           env=environment, check=True)
    # Remove only containers belonging to this exact installation, never prune
    # other users' Docker data. Resolve labels before uninstalling Docker.
    terminal = layout.data_root(root, state) / 'modules/system/terminal_sandbox/data'
    import hashlib
    owner = hashlib.sha256(str(terminal.resolve()).encode()).hexdigest()[:16]
    if shutil.which('docker'):
        result = subprocess.run(['docker', 'ps', '-aq', '--filter', f'label=toshy.terminal.owner={owner}'], capture_output=True, text=True)
        if result.returncode:
            if layout.read_json(terminal / 'instances.json'):
                raise ReleaseError('Docker is unavailable and owned resources are registered. Start Docker and retry; installation files were preserved.')
            progress('Docker is unavailable; this installation has no registered Docker instances.')
        else:
            for identity in result.stdout.split():
                subprocess.run(['docker', 'rm', '-f', identity], check=True)
    if shutil.which('docker'):
        for kind in ('volume', 'network', 'image'):
            if keep_data and kind == 'volume':
                continue
            result = subprocess.run(['docker', kind, 'ls', '-q', '--filter', f'label=toshy.terminal.owner={owner}'], capture_output=True, text=True)
            if result.returncode == 0:
                for identity in dict.fromkeys(result.stdout.split()):
                    removal = subprocess.run(['docker', kind, 'rm', identity], capture_output=True, text=True)
                    if removal.returncode:
                        progress(f'Preserved an in-use Docker {kind}: {identity}')
    engine = Installer(root)
    system.apply(engine, state['system'], packages, remove=True)
    from .shortcuts import remove
    remove(state, progress)
    if layout.is_portable(root, state):
        from .portable import uninstall_files
        return uninstall_files(root, state, progress)
    if keep_data:
        state = {key: state[key] for key in ('product', 'schema', 'root', 'system', 'data_schema') if key in state}
        state.update(system_packages=[] if remove_system else layout.owned(root).get('system_packages', []),
                     external_files=[], components=[], kept_data=True)
        layout.write_json(root / 'installation.json', state)
    if os.name == 'nt':
        # Windows cannot remove its running Python executable. An external
        # PowerShell helper waits for this process and deletes only this root.
        import tempfile
        helper = Path(tempfile.gettempdir()) / ('toshy-remove-' + str(os.getpid()) + '.ps1')
        helper.write_text('''param([int]$ProcessId,[string]$Root,[string]$Keep)
Wait-Process -Id $ProcessId -ErrorAction SilentlyContinue
$m=Get-Content -Raw -LiteralPath (Join-Path $Root 'installation.json') | ConvertFrom-Json
if($m.product -ne 'Toshy' -or $m.root -ne $Root) {exit 2}
Get-ChildItem -Force -LiteralPath $Root | Where-Object { $Keep -ne '1' -or $_.Name -notin @('data','backups','installation.json') } | Remove-Item -Recurse -Force
if($Keep -ne '1') {Remove-Item -LiteralPath $Root -Force}
Remove-Item -LiteralPath $PSCommandPath -Force
''', encoding='utf-8')
        subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(helper),
                          '-ProcessId', str(os.getpid()), '-Root', str(root), '-Keep', '1' if keep_data else '0'],
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
        progress('Removal will finish after this command exits.')
    else:
        for item in root.iterdir():
            if keep_data and item.name in {'data', 'backups', 'installation.json'}:
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        if not keep_data:
            root.rmdir()
        progress('Toshy removed.')
    return 0


def uninstall(root, **options):
    layout.owned(root)
    with layout.lock(root):
        return _uninstall_locked(root, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='toshy', description='Install, run, update or remove Toshy.')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('TOSHY_INSTALL_ROOT', layout.default_root())))
    commands = parser.add_subparsers(dest='command')
    install = commands.add_parser('install')
    install.add_argument('--source', type=Path)
    install.add_argument('--repository', default='')
    install.add_argument('--components', help='Comma-separated: git,docker,browser,ocr; empty means none.')
    install.add_argument('--skip-system', action='store_true', help='System prerequisites are already installed.')
    install.add_argument('--no-desktop', action='store_true')
    install.add_argument('--yes', action='store_true')
    install.add_argument('--prepare-only', action='store_true')
    update = commands.add_parser('update')
    update.add_argument('--check', action='store_true')
    update.add_argument('--prepare-only', action='store_true')
    update.add_argument('--repository', default='')
    update.add_argument('--wait-until-stopped', type=int, default=0)
    commands.add_parser('repair')
    commands.add_parser('doctor')
    commands.add_parser('rollback')
    apply = commands.add_parser('apply')
    apply.add_argument('--wait-until-stopped', type=int, default=0)
    apply.add_argument('--restart', action='store_true')
    remove = commands.add_parser('uninstall')
    remove.add_argument('--keep-data', action='store_true')
    remove.add_argument('--remove-system', action='store_true')
    remove.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    root = Path(os.path.abspath(args.root.expanduser()))
    cancel = threading.Event()
    engine = Installer(root, cancel=cancel)
    def interrupt(*_):
        cancel.set()
        if not engine.atomic_running:
            raise KeyboardInterrupt
    for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, interrupt)
    try:
        if args.command is None:
            gui(root)
        elif args.command == 'install':
            if args.components is None and not args.yes:
                components = []
                previous_components = layout.read_json(root / 'installation.json').get('components', [])
                for item, description in [('git', 'Git for file history'), ('docker', 'Docker for isolated terminals'), ('browser', 'local Chromium in elements/browsers for browser tools'),
                                          ('ocr', 'local OCR for scanned PDFs and images (English/Russian, stored in elements/ocr; no system installation)')]:
                    if item == 'browser' and ('browser' in previous_components
                            or (layout.data_root(root) / 'elements/browsers/installed.json').is_file()):
                        print('Local Chromium enabled: elements/browsers', flush=True)
                        components.append('browser')
                        continue
                    if item in {'git', 'docker'} and system.executable(item):
                        print(description.split(' for ')[0] + ' found: ' + system.executable(item), flush=True)
                        continue
                    if input(f'Install {description} if needed? [y/N] ').strip().lower() == 'y':
                        components.append(item)
            else:
                components = [p for p in (args.components or '').split(',') if p]
            if set(components) - {'git', 'docker', 'browser', 'ocr'}:
                raise ReleaseError('Unknown optional component.')
            print('Destination:', root, '\nRequired: Python, Toshy, Harrier, pylsp. Harper is installed when available.\nOptional:', ', '.join(components) or 'none')
            if os.name == 'nt':
                from .windows_runtime import missing
                absent = missing()
                if absent:
                    print('Required system component: Microsoft Visual C++ x64 Runtime. Missing: ' + ', '.join(absent), flush=True)
                    print('Its official setup may request administrator permission.', flush=True)
            if not args.yes and input('Start installation? [y/N] ').strip().lower() != 'y':
                return 1
            engine.prepare(repository=args.repository, local_source=args.source, components=components, skip_system=args.skip_system)
            if not args.prepare_only:
                engine.activate(shortcuts=not args.no_desktop)
        elif args.command == 'update':
            state = layout.owned(root)
            repo = args.repository or state.get('repository', '')
            release = source.latest(Network(cancel), repo)
            current = state.get('current', {})
            if source.version(release['version']) <= source.version(current.get('version', '0.0.0')):
                print('No newer stable release.'); return 0
            if args.check:
                print(json.dumps(release, ensure_ascii=False, indent=2)); return 0
            engine.prepare(repository=repo, components=state.get('components', []), skip_system=True, release_info=release)
            if not args.prepare_only:
                engine.activate()
        elif args.command == 'apply':
            deadline = time.monotonic() + args.wait_until_stopped
            while layout.running(root) and time.monotonic() < deadline:
                if cancel.wait(1):
                    engine.network.check()
            engine.activate()
            if args.restart:
                gui(root)
        elif args.command == 'rollback':
            engine.rollback(); print('Previous version selected.')
        elif args.command == 'repair':
            state = layout.owned(root)
            program = layout.program_root(root, state['current'], state)
            engine.prepare(repository=state.get('repository', ''), local_source=program,
                           components=state.get('components', []), skip_system=True)
            engine.activate()
        elif args.command == 'doctor':
            state = layout.owned(root)
            program = layout.program_root(root, state['current'], state)
            source.verify(program, state['current']['version'])
            from .environments import environment, resource_root
            engine.verify_runtime(program, environment=environment(root, state['current'], state),
                                  data=resource_root(root, state['current']))
            print('Active version:', state['current']['version'])
            print('Data:', layout.data_root(root, state))
            print('Git:', system.executable('git') or 'unavailable; file history needs Git')
            docker = system.executable('docker')
            if docker:
                result = subprocess.run([docker, 'info'], capture_output=True, text=True)
                print('Docker:', 'ready' if result.returncode == 0 else 'installed; start the service / check permissions or restart Windows')
            else:
                print('Docker: not installed (optional)')
        elif args.command == 'uninstall':
            return uninstall(root, keep_data=args.keep_data, remove_system=args.remove_system, yes=args.yes)
        return 0
    except KeyboardInterrupt:
        print('Toshy: operation cancelled.', file=sys.stderr)
        return 130
    except EOFError:
        print('Toshy: no interactive input. Use --yes and --components for unattended installation.', file=sys.stderr)
        return 2
    except (ReleaseError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Toshy: ' + str(error), file=sys.stderr)
        return 130 if cancel.is_set() else 2


if __name__ == '__main__':
    raise SystemExit(main())
