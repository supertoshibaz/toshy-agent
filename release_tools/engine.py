"""Transactional per-user installs. No dependency on Qt or the agent core."""
from __future__ import annotations
import json
import codecs
import queue
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from . import layout, source, system
from . import UPDATER_VERSION
from .progress import ConsoleProgress, command_label
from .network import Network, ReleaseError, Cancelled, sha256


def python_in(directory):
    return Path(directory) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


class Installer:
    def __init__(self, root, *, cancel=None, progress=print, wait=True):
        self.root = Path(root).expanduser().absolute()
        self.cancel = cancel or threading.Event()
        self.progress = ConsoleProgress() if progress is print else progress
        self.network = Network(self.cancel, self.progress, wait=wait)
        self.journal = {}
        self.atomic_running = False

    def record(self, **values):
        self.journal.update(values)
        layout.write_json(self.root / 'transaction.json', self.journal)

    def run(self, args, *, env=None, atomic=False, retry_network=False, label=None):
        self.network.check()
        atomic = atomic or getattr(self, 'recovering', False)
        args = list(map(str, args))
        if args[0] == 'sudo':
            # Authenticate on the actual terminal before drawing a working line.
            if getattr(self, 'gui_elevation', False):
                pkexec = shutil.which('pkexec')
                executable = shutil.which(args[1])
                if not pkexec or not executable:
                    raise ReleaseError('A graphical administrator prompt is unavailable. Install the listed system packages in a terminal and retry.')
                args = [pkexec, executable, *args[2:]]
            else:
                subprocess.run(['sudo', '-v'], check=True)
                args.insert(1, '-n')
        label = label or command_label(args)
        attempt = 0
        while True:
            self.progress(label + '…')
            self.atomic_running = atomic
            self.root.mkdir(parents=True, exist_ok=True)
            chunks = queue.Queue(maxsize=32)
            stop = threading.Event()
            output = ''
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            started = time.monotonic()
            last_output = started
            try:
                process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=None, env=env,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except BaseException:
                self.atomic_running = False
                raise
            def read():
                try:
                    while not stop.is_set():
                        block = os.read(process.stdout.fileno(), 4096)
                        while not stop.is_set():
                            try:
                                chunks.put(block, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                        if not block:
                            break
                except OSError:
                    while not stop.is_set():
                        try:
                            chunks.put(b'', timeout=0.1)
                            break
                        except queue.Full:
                            continue
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                with (self.root / 'install.log').open('a', encoding='utf-8') as log:
                    log.write('\n' + ' '.join(args) + '\n'); log.flush()
                    eof = False
                    while not eof or process.poll() is None:
                        if self.cancel.is_set() and not atomic:
                            self.network.check()
                        try:
                            block = chunks.get(timeout=0.1)
                            eof = not block
                            text = decoder.decode(block, final=eof)
                        except queue.Empty:
                            text = ''
                        if text:
                            log.write(text); log.flush()
                            output = (output + text)[-2 * 1024 * 1024:]
                            raw = getattr(self.progress, 'raw', None)
                            if raw:
                                raw(text)
                            else:
                                self.progress(text.rstrip())
                            last_output = time.monotonic()
                        # Do not draw over a partial output line or a prompt.
                        if time.monotonic() - last_output > 0.5 and (not output or output.endswith(('\n', '\r'))):
                            tick = getattr(self.progress, 'tick', None)
                            if tick:
                                tick(label, time.monotonic() - started)
                    process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                stop.set(); reader.join(timeout=1); process.stdout.close()
                clear = getattr(self.progress, 'clear', None)
                if clear:
                    clear()
                if output and not output.endswith(('\n', '\r')):
                    self.progress('')
                self.atomic_running = False
            if process.returncode == 0:
                self.network.check()
                self.progress('Completed: ' + label)
                return output
            if retry_network and re.search(r'(?i)(connection (?:reset|refused|failed)|timed? out|temporary failure|failed to (?:download|connect)|dns error|network is unreachable)', output):
                self.network.retry(ConnectionError('Package download interrupted.'), attempt)
                attempt += 1
                continue
            detail = None
            if os.name == 'nt':
                from .windows_runtime import explain_exit
                detail = explain_exit(args[0], process.returncode)
            detail = detail or f'Command failed ({process.returncode}).'
            with (self.root / 'install.log').open('a', encoding='utf-8') as log:
                log.write('\nERROR: ' + detail + '\n')
            raise ReleaseError(f'{detail} See {self.root / "install.log"}\n' + output[-1800:])

    def state(self):
        return layout.owned(self.root)

    def environment(self):
        env = os.environ.copy()
        env.update(UV_PYTHON_INSTALL_DIR=str(self.root / 'tools/python'),
                   UV_CACHE_DIR=str(self.root / 'cache/uv'), UV_NO_PROGRESS='1',
                   UV_PYTHON_PREFERENCE='only-managed', PIP_DISABLE_PIP_VERSION_CHECK='1')
        return env

    def uv(self, config):
        pins = json.loads((config / 'release_tools/resources.json').read_text())['uv']
        target = self.root / 'tools' / ('uv.exe' if os.name == 'nt' else 'uv')
        marker = target.with_name('uv-version.json')
        if target.is_file() and layout.read_json(marker).get('version') == pins['version']:
            return target
        if not re.fullmatch(r'\d+\.\d+\.\d+', pins['version']):
            raise ReleaseError('Invalid uv version.')
        # Keep an older uv usable by running plugins while a release prepares.
        target = self.root / 'tools' / ('uv-' + pins['version']) / target.name
        marker = target.with_name('uv-version.json')
        if any(path.is_symlink() for path in (self.root / 'tools', target.parent, target, marker)):
            raise ReleaseError('Managed uv paths cannot be links.')
        if target.is_file() and layout.read_json(marker).get('version') == pins['version']:
            return target
        item = pins['windows' if os.name == 'nt' else 'linux']
        archive = self.root / 'cache' / ('uv-' + pins['version'] + ('.zip' if os.name == 'nt' else '.tar.gz'))
        self.network.download(item['url'], archive, digest=item['sha256'], size=item['size'])
        install_binary(archive, target, target.name)
        layout.write_json(marker, {'version': pins['version'], 'archive_sha256': item['sha256']})
        return target

    def packages(self, detected, components):
        plan = system.package_plan(detected, components)
        self.progress('System packages: ' + (', '.join(p for p, _ in plan) or 'none'))
        state = self.state()
        pending = [p for p, _ in plan]
        before = system.snapshot(detected)
        self.record(system_plan=plan)
        try:
            system.apply(self, detected, pending)
        finally:
            added = [p for p in pending if system.installed(detected, p)]
            added = [p for p in added if p != 'Microsoft.VCRedist.2015+.x64']
            if before is not None:
                added = sorted(system.snapshot(detected) - before)
            state['system_packages'] = sorted(set(state.get('system_packages', [])) | set(added))
            layout.write_json(self.root / 'installation.json', state)
            self.record(system_packages=added)
        if 'docker' in components and detected['os'] == 'linux' and shutil.which('systemctl'):
            if any(kind == 'docker' for _, kind in plan):
                prefix = [] if os.geteuid() == 0 else ['sudo']
                self.run([*prefix, 'systemctl', 'enable', '--now', 'docker'], atomic=True)
            self.progress('Docker service installed. If doctor reports a permission error, configure access to the Docker socket and log in again.')

    def resources(self, program, uv, config, components=(), *, data=None):
        pins = json.loads((program / 'release_tools/resources.json').read_text())
        for name, item in pins.get('notices', {}).items():
            self.network.download(item['url'], self.root / 'tools/licenses' / (name + '.txt'), digest=item['sha256'], size=item['size'])
        data = Path(data) if data is not None else layout.data_root(self.root)
        if 'browser' in components:
            from .browser import install as install_browser
            browser_pins = pins.get('browser') or layout.read_json(Path(__file__).with_name('resources.json'))['browser']
            browser = install_browser(data / 'elements', self.network, browser_pins)
            self.run([browser, '--version'], label='Checking local Chromium')
        if 'ocr' in components:
            from .ocr import install as install_ocr
            tag = 'cp' + ''.join(str(config['python']).split('.')[:2])
            install_ocr(data / 'elements', self.network, python_tag=tag, resource_pins=pins)
        model = data / 'elements/models/embeddings/harrier-oss-v1-270m'
        for name, item in pins['harrier']['files'].items():
            self.network.download(item['url'], model / name, digest=item['sha256'], size=item['size'])
        shutil.copy2(program / 'release_tools/HARRIER_LICENSE.txt', model / 'LICENSE')
        platform_key = 'windows' if os.name == 'nt' else 'linux'
        lock = program / f'release_tools/locks/pylsp-{platform_key}-x86_64.txt'
        lsp = data / 'elements/lsp'
        venv = lsp / 'servers/code/pylsp' / sha256(lock)[:16] / '.venv'
        if not (venv / '.complete').exists():
            self.run([uv, 'venv', '--python', config['python'], venv], env=self.environment(), retry_network=True)
            self.run([uv, 'pip', 'sync', '--python', python_in(venv), '--require-hashes', '--only-binary', ':all:', lock], env=self.environment(), retry_network=True)
            self.run([python_in(venv), '-c', 'import pylsp; print("pylsp ready")'])
            (venv / '.complete').touch()
        harper = pins['harper']
        name = 'harper-ls.exe' if os.name == 'nt' else 'harper-ls'
        binary = lsp / 'servers/linguistic/en' / harper['version'] / name
        if not binary.is_file():
            item = harper[platform_key]
            archive = self.root / 'cache' / ('harper-' + harper['version'] + ('.zip' if os.name == 'nt' else '.tar.gz'))
            self.network.download(item['url'], archive, digest=item['sha256'], size=item['size'])
            install_binary(archive, binary, name)
        harper_path = str(binary)
        try:
            self.run([binary, '--version'])
        except (ReleaseError, OSError) as error:
            if self.cancel.is_set():
                raise
            harper_path = None
            warning = 'Harper is unavailable; Studio will be installed without Harper grammar diagnostics. ' + str(error)
            self.progress('WARNING: ' + warning)
            self.progress('Run Toshy repair after resolving the missing runtime to retry Harper.')
            with (self.root / 'install.log').open('a', encoding='utf-8') as log:
                log.write('\nWARNING: ' + warning + '\n')
        return {'pylsp': str(python_in(venv)), 'harper_ls': harper_path}

    def prepare(self, *, repository='', local_source=None, components=(), skip_system=False, release_info=None):
        if not (self.root / 'installation.json').exists() or layout.is_portable(self.root):
            from .portable import prepare
            return prepare(self, repository=repository, local_source=local_source, components=components,
                           skip_system=skip_system, release_info=release_info)
        detected = system.detect()
        if self.root.exists() and not (self.root / 'installation.json').exists():
            allowed = {'tools', 'cache', '.install-lock', '.toshy-bootstrap'}
            entries = set(p.name for p in self.root.iterdir())
            marker = self.root / '.toshy-bootstrap'
            valid_bootstrap = marker.is_file() and marker.read_text(encoding='utf-8-sig').strip() == str(self.root)
            if entries and (entries - allowed or not valid_bootstrap):
                raise ReleaseError('Choose an empty directory or a registered Toshy installation.')
        if self.root == Path.home() or self.root == Path(self.root.anchor) or self.root.is_symlink():
            raise ReleaseError('Unsafe installation directory.')
        with layout.lock(self.root):
            if os.name != 'nt':
                self.root.chmod(0o700)
            if not (self.root / 'installation.json').exists():
                layout.write_json(self.root / 'installation.json', {'product': 'Toshy', 'schema': 1,
                    'root': str(self.root), 'system': detected, 'system_packages': [], 'external_files': [], 'components': list(components)})
            state = self.state()
            if layout.read_json(self.root / 'transaction.json').get('status') == 'preparing':
                self.progress('Recovering an interrupted preparation; verified downloads will be reused.')
            stage = None
            self.record(status='preparing', system_packages=[])
            try:
                if shutil.disk_usage(self.root).free < 3 * 1024 ** 3:
                    raise ReleaseError('At least 3 GiB of free space is required for installation.')
                if local_source:
                    local_source = Path(local_source).resolve()
                    config = source.verify(local_source)
                    info = {'version': config['version'], 'commit': 'local-' + sha256(local_source / 'release-manifest.json')[:16],
                            'repository': repository or config.get('repository', ''), 'notes': '', 'published_at': '', 'url': ''}
                    original = local_source
                else:
                    repo = source.repository(repository or state.get('repository', ''))
                    info = dict(release_info or source.latest(self.network, repo))
                    source.version(info.get('version'))
                    if info.get('repository') != repo or not re.fullmatch('[0-9a-f]{40}', info.get('commit', '')):
                        raise ReleaseError('Invalid selected release identity.')
                    archive = self.root / 'cache' / (info['commit'] + '.zip')
                    self.network.download(f'https://api.github.com/repos/{info["repository"]}/zipball/{info["commit"]}', archive)
                    stage = Path(tempfile.mkdtemp(prefix='source-', dir=self.root))
                    original = source.extract(archive, stage, strip_root=True)
                    config = source.verify(original, info['version'])
                if config.get('minimum_updater', 1) > UPDATER_VERSION:
                    raise ReleaseError('This release requires a newer installer. Run its installation command.')
                if detected['os'] + '-x86_64' not in config['platforms']:
                    raise ReleaseError('This release does not support the current platform.')
                from .migrations import scripts
                scripts(original, state.get('data_schema', config['data_schema']), config['data_schema'])
                source.version(config['version'])
                key = 'v' + config['version'] + '-' + info['commit'][:16]
                program = self.root / 'versions' / key
                if program.is_symlink():
                    raise ReleaseError('Version directories cannot be links.')
                if program.exists() and not (program / '.complete').exists():
                    if state.get('current', {}).get('key') == key:
                        raise ReleaseError('The active version is incomplete. Use repair.')
                    shutil.rmtree(program)
                fresh = not program.exists()
                self.record(program=str(program), created_program=fresh)
                if fresh:
                    source.copy_source(original, program)
                source.verify(program, config['version'])
                if not skip_system:
                    self.packages(detected, components)
                uv = self.uv(program)
                self.run([uv, 'python', 'install', config['python']], env=self.environment(), retry_network=True)
                venv = program / '.venv'
                if fresh or not python_in(venv).exists():
                    self.run([uv, 'venv', '--python', config['python'], venv], env=self.environment(), retry_network=True)
                lock = program / f'release_tools/locks/{detected["os"]}-x86_64.txt'
                self.run([uv, 'pip', 'sync', '--python', python_in(venv), '--require-hashes', '--only-binary', ':all:', lock], env=self.environment(), retry_network=True)
                lsp = self.resources(program, uv, config, components=components)
                self.verify_runtime(program)
                if 'ocr' in components:
                    ocr_env = self.environment()
                    ocr_env.update(PYTHONPATH=str(program), TOSHY_DATA_DIR=str(self.root / 'data'))
                    self.run([python_in(program / '.venv'), '-c',
                              'from core.paths import APP_PATHS; from modules.system.file_manager.documents.engines import private_ocr; '
                              't=private_ocr(); assert t is not None; '
                              'a=t.PyTessBaseAPI(path=str(APP_PATHS.elements/"ocr/tessdata"),lang="eng+rus"); '
                              'a.End(); print("Private OCR ready")'], env=ocr_env)
                interpreter = self.run([uv, 'python', 'find', config['python']], env=self.environment()).strip().splitlines()[-1]
                info.update(key=key, lsp=lsp, data_schema=config['data_schema'], launcher_python=interpreter)
                (program / '.complete').touch()
                state.update(pending=info, repository=info['repository'], components=list(components))
                layout.write_json(self.root / 'installation.json', state)
                self.record(status='prepared')
                self.progress('Version prepared. Close Toshy and its core before applying it.')
                return info
            except BaseException:
                active = state.get('current', {}).get('key')
                target = self.journal.get('program')
                if target and self.journal.get('created_program') and Path(target).name != active:
                    shutil.rmtree(target, ignore_errors=True)
                if self.cancel.is_set() and not active and not state.get('kept_data'):
                    # A fresh interrupted install owns these directories. Never
                    # erase data retained by uninstall --keep-data or an active
                    # version. A running private Python is left for bootstrap
                    # cleanup after this process exits.
                    for name in ('data', 'cache', 'versions'):
                        shutil.rmtree(self.root / name, ignore_errors=True)
                    executable = Path(sys.executable).absolute()
                    if not executable.is_relative_to(self.root / 'tools'):
                        shutil.rmtree(self.root / 'tools', ignore_errors=True)
                self.record(status='cancelled' if self.cancel.is_set() else 'failed')
                if self.journal.get('system_packages'):
                    self.progress('New shared system packages were retained: ' + ', '.join(self.journal['system_packages']) + '. Use uninstall --remove-system to remove them explicitly.')
                raise
            finally:
                if stage:
                    shutil.rmtree(stage, ignore_errors=True)

    def verify_runtime(self, program, *, environment=None, data=None):
        env = self.environment()
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['PYTHONPATH'] = str(program)
        data = Path(data) if data is not None else layout.data_root(self.root)
        env['TOSHY_MANAGED_ELEMENTS'] = str(data / 'elements')
        model = data / 'elements/models/embeddings/harrier-oss-v1-270m'
        script = ('import sys; sys.path.insert(0,sys.argv[2]); from pathlib import Path; from PySide6.QtWidgets import QApplication; '
                  'import pypdf, pypdfium2, PIL, openpyxl, defusedxml, striprtf, python_calamine; '
                  'a=QApplication([]); import onnxruntime as o; from tokenizers import Tokenizer; '
                  'Tokenizer.from_file(str(Path(sys.argv[1])/"tokenizer.json")); '
                  's=o.InferenceSession(str(Path(sys.argv[1])/"model_q4.onnx"),providers=["CPUExecutionProvider"]); '
                  'print("Qt, tokenizer and Harrier ONNX loaded", len(s.get_inputs()))')
        script += ('; import os,tempfile; t=tempfile.TemporaryDirectory(); '
                   'os.environ["TOSHY_DATA_DIR"]=t.name; import ui.main_window; '
                   'print("Application UI imports ready")')
        self.run([python_in(environment or (self.root if layout.is_portable(self.root) else program) / '.venv'), '-c', script, model, program], env=env, label='Checking Qt, application and embedding model')

    def activate(self, *, shortcuts=True, wait_for_startup=False):
        if layout.is_portable(self.root):
            from .portable import activate
            return activate(self, shortcuts=shortcuts, wait_for_startup=wait_for_startup)
        with layout.lock(self.root):
            state = self.state()
            pending = state.get('pending')
            if not pending:
                raise ReleaseError('No prepared update.')
            layout.require_stopped(self.root)
            program = self.root / 'versions' / pending['key']
            source.verify(program, pending['version'])
            if not (program / '.complete').is_file():
                raise ReleaseError('The prepared version is incomplete.')
            from .migrations import scripts, restore_backup
            interrupted = layout.read_json(self.root / 'transaction.json')
            if interrupted.get('status') == 'activating' and interrupted.get('backup') and interrupted.get('previous_key') == state.get('current', {}).get('key'):
                self.progress('Restoring the offline backup after an interrupted activation.')
                restore_backup(self.root, interrupted['backup'])
            backup = None
            if state.get('current') and state['current']['key'] != pending['key']:
                backup = layout.backup_data(self.root)
            migration_scripts = scripts(program, state.get('data_schema', pending['data_schema']), pending['data_schema'])
            if migration_scripts and backup is None:
                raise ReleaseError('Data migrations require an offline backup.')
            self.record(status='activating', backup=str(backup) if backup else '',
                        previous_key=state.get('current', {}).get('key'), target_key=pending['key'])
            try:
                for script in migration_scripts:
                    self.run([python_in(program / '.venv'), script, self.root / 'data'])
                layout.seed_data(self.root, program)
                lsp_config = self.root / 'data/elements/lsp/lsp_config.json'
                conf = layout.read_json(lsp_config, {'servers': {}})
                base = json.loads((program / 'elements/lsp/lsp_defaults.json').read_text(encoding='utf-8'))
                from .lsp_config import configure
                configure(conf, base, pending['lsp'])
                layout.write_json(lsp_config, conf)
                if state.get('current', {}).get('key') != pending['key']:
                    state['previous'] = state.get('current')
                state.update(current=pending, pending=None, data_schema=pending['data_schema'], launcher_python=pending['launcher_python'])
                if backup:
                    state['backup'] = str(backup)
                launcher = self.root / 'launcher'
                launcher.mkdir(exist_ok=True)
                shutil.copy2(program / 'toshy.py', launcher / 'toshy.py')
                shutil.copytree(program / 'release_tools', launcher / 'release_tools', dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__'))
                from .shortcuts import create
                state = create(self.root, state, desktop=shortcuts)
                # Switch the active version only after all preparation succeeded.
                layout.write_json(self.root / 'installation.json', state)
            except BaseException:
                if backup:
                    restore_backup(self.root, backup)
                self.record(status='prepared')
                raise
            self.record(status='complete')
            self.progress('Toshy installed: ' + pending['version'])
            from .shortcuts import summarize
            summarize(state, self.progress)
            return state

    def rollback(self):
        if layout.is_portable(self.root):
            from .portable import rollback
            return rollback(self)
        with layout.lock(self.root):
            layout.require_stopped(self.root)
            state = self.state()
            old = state.get('previous')
            if not old or old.get('data_schema') != state.get('data_schema'):
                raise ReleaseError('No previous version with a compatible data schema.')
            program = self.root / 'versions' / old['key']
            source.verify(program, old['version'])
            if not (program / '.complete').exists():
                raise ReleaseError('The previous runtime is missing.')
            state['current'], state['previous'] = old, state['current']
            layout.write_json(self.root / 'installation.json', state)
            return state


def install_binary(archive, target, name):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.tmp')
    try:
        if str(archive).endswith('.zip'):
            import zipfile
            with zipfile.ZipFile(archive) as z:
                names = [x for x in z.namelist() if Path(x).name == name]
                if len(names) != 1:
                    raise ReleaseError(f'The archive must contain exactly one {name}.')
                with z.open(names[0]) as f, temp.open('wb') as out:
                    shutil.copyfileobj(f, out)
        else:
            with tarfile.open(archive, 'r:gz') as t:
                entries = [x for x in t.getmembers() if x.isfile() and Path(x.name).name == name]
                if len(entries) != 1 or entries[0].size > 128 * 1024 ** 2:
                    raise ReleaseError(f'Invalid binary archive: {name}.')
                with t.extractfile(entries[0]) as f, temp.open('wb') as out:
                    shutil.copyfileobj(f, out)
        temp.chmod(0o755)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
