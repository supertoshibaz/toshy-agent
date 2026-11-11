"""Publish user launchers without making desktop integration an install prerequisite."""
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import uuid
from .network import sha256
from .layout import read_json, write_json, program_root


_FAILURES = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)


def powershell(script, **values):
    env = os.environ.copy()
    env.update({'TOSHY_' + key.upper(): str(value) for key, value in values.items()})
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
         "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);\n" + script],
        env=env, check=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
    return result.stdout.strip()


def _on_path(directory):
    # Inspect the inherited environment, without predicting a future login shell.
    target = os.path.normcase(os.path.realpath(directory))
    return any(os.path.normcase(os.path.realpath(entry)) == target
               for entry in os.environ.get('PATH', '').split(os.pathsep) if entry)


def _desktop_argument(value):
    # Desktop Entry quoting has a second escaping layer and is not shell quoting.
    text = str(value).replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        text = text.replace(char, '\\' + char)
    return '"' + text.replace('\\', '\\\\') + '"'


def _entry_value(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def _desktop_directory():
    command = shutil.which('xdg-user-dir')
    if command:
        result = subprocess.run([command, 'DESKTOP'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            directory = Path(result.stdout.strip())
            if directory.is_absolute():
                return directory
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not config.is_absolute():
        config = Path.home() / '.config'
    path = config / 'user-dirs.dirs'
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() == 'XDG_DESKTOP_DIR':
                words = shlex.split(value, comments=True)
                if len(words) == 1:
                    value = words[0].replace('${HOME}', str(Path.home())).replace('$HOME', str(Path.home()))
                    directory = Path(value)
                    if directory.is_absolute():
                        return directory
    return Path.home() / 'Desktop'


def _registered_windows_shortcut(path):
    """Recognize an unchanged shortcut owned by another Toshy installation."""
    data = json.loads(powershell("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:TOSHY_SHORTCUT); @{arguments=$s.Arguments; description=$s.Description} | ConvertTo-Json -Compress", shortcut=path))
    match = re.match(r'^\s*"([^"]+)"', data.get('arguments', ''))
    if data.get('description') != 'Toshy' or match is None:
        return False
    script = Path(match[1])
    if script.name != 'toshy.py' or script.parent.name not in {'launcher', 'bin'}:
        return False
    previous_root = script.parent.parent
    marker = read_json(previous_root / 'installation.json')
    receipts = [*marker.get('external_files', []), *read_json(previous_root / 'launcher-receipts.json', [])]
    return (marker.get('product') == 'Toshy' and marker.get('root') == str(previous_root)
            and any(item.get('path') == str(path) and item.get('sha256') == sha256(path) for item in receipts))


class _Publication:
    def __init__(self, root, state):
        self.root = root
        self.state = state
        self.report = {'created': [], 'warnings': [], 'notes': [], 'path': ''}
        try:
            receipts = read_json(root / 'launcher-receipts.json', [])
            if not isinstance(receipts, list) or any(not isinstance(item, dict) or 'path' not in item or 'sha256' not in item for item in receipts):
                raise ValueError('Invalid launcher receipts.')
        except (OSError, ValueError) as error:
            receipts = []
            self.report['warnings'].append('Could not read launcher receipts: ' + str(error))
        records = [*state.get('external_files', []), *receipts]
        self.owned = {item['path']: item for item in records}
        state['launch_report'] = self.report
        state.pop('local_launcher', None)

    def attempt(self, label, action):
        try:
            return action()
        except _FAILURES as error:
            detail = getattr(error, 'stderr', None) or str(error)
            self.report['warnings'].append(label + ': ' + str(detail).strip())
            return None

    def guard(self, path):
        path = Path(path)
        if path.is_symlink():
            raise RuntimeError('Refusing to replace a linked launcher: ' + str(path))
        previous = self.owned.get(str(path), {})
        if path.exists():
            if not path.is_file():
                raise RuntimeError('Launcher path is not a file: ' + str(path))
            if previous:
                if sha256(path) != previous.get('sha256'):
                    raise RuntimeError('Preserved a modified launcher: ' + str(path))
                return previous
            text = path.read_text(encoding='utf-8') if path.suffix == '.desktop' or path.name == 'toshy' else ''
            generated = path.suffix == '.desktop' and 'X-Toshy-Generated=true' in text.splitlines()
            if path.name == 'toshy' and text.startswith('#!/bin/sh\nexec '):
                try:
                    words = shlex.split(text.splitlines()[1])
                    previous_root = Path(words[words.index('--root') + 1])
                    marker = read_json(previous_root / 'installation.json')
                    receipts = [*marker.get('external_files', []), *read_json(previous_root / 'launcher-receipts.json', [])]
                    generated = (marker.get('product') == 'Toshy' and marker.get('root') == str(previous_root)
                                 and any(r.get('path') == str(path) and r.get('sha256') == sha256(path) for r in receipts))
                except (ValueError, IndexError, OSError):
                    generated = False
            if path.suffix.lower() == '.lnk':
                generated = _registered_windows_shortcut(path)
            if not generated:
                raise RuntimeError('An unrelated launcher already exists: ' + str(path))
            backup = self.root / 'launcher/previous-shortcuts' / hashlib.sha256(str(path).encode()).hexdigest()
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            previous = {'backup': str(backup)}
        return previous

    def record(self, path, previous, label):
        self.owned[str(path)] = {**previous, 'path': str(path), 'sha256': sha256(path)}
        self.state['external_files'] = list(self.owned.values())
        write_json(self.root / 'launcher-receipts.json', list(self.owned.values()))
        self.report['created'].append({'kind': label, 'path': str(path)})
        return path

    def write(self, path, content, *, executable=False, label='Launcher'):
        path = Path(path)
        previous = self.guard(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(content.encode('utf-8'))
            if executable:
                temporary.chmod(0o755)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.record(path, previous, label)


def _linux(publication, root, state, program, python, launcher, icon, desktop):
    local = publication.attempt('Local launcher', lambda: publication.write(
        root / 'start.sh', '#!/bin/sh\n'
        'agent_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
        'exec "$agent_dir/' + os.path.relpath(python, root) + '" "$agent_dir/launcher/toshy.py" --root "$agent_dir" "$@"\n',
        executable=True, label='Local launcher'))
    binpath = Path.home() / '.local/bin/toshy'
    command = publication.attempt('Command launcher', lambda: publication.write(
        binpath, '#!/bin/sh\nexec ' + ' '.join(shlex.quote(str(p)) for p in (python, launcher, '--root', root)) + ' "$@"\n',
        executable=True, label='Command launcher'))
    if command:
        publication.report['path'] = ('The launcher directory is in the current PATH.' if _on_path(binpath.parent) else
            str(binpath.parent) + ' is not in the current PATH. Use the launcher directly or open Toshy from the application menu.')
        if _on_path(binpath.parent):
            resolved = shutil.which('toshy')
            if resolved and os.path.realpath(resolved) != os.path.realpath(binpath):
                publication.report['warnings'].append('Another command shadows Toshy in PATH: ' + resolved)
    # Keep shortcuts usable even if publishing into ~/.local/bin was denied.
    target = command or local
    if target is None:
        publication.report['warnings'].append('No launcher was created. Start the application with: ' +
            shlex.join([str(python), str(program / 'run_ui.py')]))
        return
    content = ('[Desktop Entry]\nType=Application\nName=Toshy\nExec=' + _desktop_argument(target) +
               '\nIcon=' + _entry_value(icon) + '\nTerminal=false\nCategories=Development;Utility;\n'
               'StartupWMClass=org.toshy.AgentStudio\nX-Toshy-Generated=true\n')
    local_shortcut = publication.attempt('Local shortcut', lambda: publication.write(
        root / 'Toshy.desktop', content, executable=True, label='Local shortcut'))
    if local_shortcut:
        state['local_launcher'] = str(local_shortcut)
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    if not data.is_absolute():
        data = Path.home() / '.local/share'
    menu_entry = publication.attempt('Application menu shortcut', lambda: publication.write(
        data / 'applications/org.toshy.AgentStudio.desktop', content, label='Application menu shortcut'))
    if menu_entry:
        publication.attempt('Application menu registration', lambda: _refresh_application_menu(publication, menu_entry))
    if desktop:
        def desktop_shortcut():
            directory = _desktop_directory()
            if directory == Path.home():
                raise RuntimeError('The desktop directory is disabled; no desktop shortcut was created.')
            return publication.write(directory / 'Toshy.desktop', content, executable=True, label='Desktop shortcut')
        publication.attempt('Desktop shortcut', desktop_shortcut)


def _refresh_application_menu(publication, entry):
    if not entry.is_file() or 'Name=Toshy' not in entry.read_text(encoding='utf-8').splitlines():
        raise RuntimeError('The application menu entry could not be verified: ' + str(entry))
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
    if 'KDE' not in desktop and os.environ.get('KDE_FULL_SESSION') != 'true':
        return
    command = shutil.which('kbuildsycoca6') or shutil.which('kbuildsycoca5')
    if not command:
        publication.report['warnings'].append('The menu entry was created, but the KDE cache refresh tool is unavailable.')
        return
    environment = dict(os.environ, LC_ALL='C', LANG='C')
    subprocess.run([command], env=environment, check=True, capture_output=True, text=True, timeout=20)
    publication.report['notes'].append('KDE application menu cache refreshed.')


def _windows(publication, root, state, program, python, launcher, icon, desktop):
    bindir = root / 'bin'
    # One Python entry point is shared by the console command and windowless shortcuts.
    script = publication.attempt('Command entry point', lambda: publication.write(
        bindir / 'toshy.py', 'import sys\nfrom pathlib import Path\n'
        'root = Path(__file__).resolve().parent.parent\n'
        'sys.path.insert(0, str(root / "launcher"))\n'
        'from release_tools.cli import main\n'
        'raise SystemExit(main(["--root", str(root), *sys.argv[1:]]))\n', label='Command entry point'))
    command = None
    if script:
        command = publication.attempt('Command launcher', lambda: publication.write(
            bindir / 'toshy.cmd', '@echo off\r\n"%~dp0' + os.path.relpath(python, bindir).replace('/', '\\') +
            '" "%~dp0toshy.py" %*\r\n', label='Command launcher'))
        publication.attempt('Local launcher', lambda: publication.write(
            root / 'start.cmd', '@echo off\r\n"%~dp0' + os.path.relpath(python.with_name('pythonw.exe'), root).replace('/', '\\') +
            '" "%~dp0bin\\toshy.py" %*\r\n', label='Local launcher'))
    if command:
        def publish_path():
            result = json.loads(powershell('''$p=[Environment]::GetEnvironmentVariable('Path','User');
$entries=@($p -split ';' | ForEach-Object {[Environment]::ExpandEnvironmentVariables($_).Trim().Trim('"').TrimEnd('\\')});
$changed=-not ($entries -contains $env:TOSHY_BIN.TrimEnd('\\'));
if($changed) {
[Environment]::SetEnvironmentVariable('Path',(($env:TOSHY_BIN+';'+$p).TrimEnd(';')),'User')
}; @{added=$changed} | ConvertTo-Json -Compress''', bin=bindir))
            if result['added']:
                state['path_entry'] = str(bindir)
            publication.report['path'] = 'The launcher directory is in the user PATH. Open a new terminal to use toshy.'
        publication.attempt('User PATH registration', publish_path)
    # A new stable name also invalidates Explorer's old single-image icon cache.
    iconfile = root / 'launcher/toshy-multisize.ico'
    def convert_icon():
        from .environments import environment as app_environment
        from .engine import python_in
        environment = os.environ.copy()
        environment['QT_QPA_PLATFORM'] = 'offscreen'
        # Qt is installed in the app environment, not the private base Python
        # used by the stable launcher.
        renderer = python_in(app_environment(root, state['current'], state))
        subprocess.run([str(renderer), str(Path(__file__).with_name('windows_icon.py')), str(icon), str(iconfile)],
                       env=environment, check=True, capture_output=True, text=True, timeout=20)
        return iconfile
    shortcut_icon = publication.attempt('Shortcut icon', convert_icon) or python.with_name('pythonw.exe')
    if script is None:
        publication.report['warnings'].append('No command entry point was created. Run .venv\\Scripts\\python.exe run_ui.py from the agent folder.')
        return
    uninstaller = publication.attempt('Uninstaller entry point', lambda: publication.write(
        root / 'launcher/uninstall.py',
        'import sys\nfrom pathlib import Path\n'
        'root = Path(__file__).resolve().parent.parent\n'
        'sys.path.insert(0, str(root / "launcher"))\n'
        'try:\n'
        '    from release_tools.cli import main\n'
        '    result = main(["--root", str(root), "uninstall", "--keep-data"])\n'
        'except Exception as error:\n'
        '    print("Toshy uninstall failed:", error, file=sys.stderr)\n'
        '    result = 2\n'
        'if result:\n'
        '    try:\n'
        '        input("Press Enter to close this window.")\n'
        '    except (EOFError, KeyboardInterrupt):\n'
        '        pass\n'
        'raise SystemExit(result or 0)\n', label='Uninstaller entry point'))
    if uninstaller:
        from .windows_apps import register
        registered = publication.attempt('Windows application registration',
            lambda: register(root, state, python, uninstaller, shortcut_icon))
        if registered:
            publication.report['notes'].append('Toshy registered in Windows Installed apps with an Uninstall action.')
    def make_shortcut(path, label):
        path = Path(path)
        previous = publication.guard(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write COM output to a sibling temporary file before replacing a valid shortcut.
        temporary = path.with_name(path.stem + '.' + uuid.uuid4().hex + '.lnk')
        try:
            powershell('''$shell=New-Object -ComObject WScript.Shell;
$s=$shell.CreateShortcut($env:TOSHY_SHORTCUT);$s.TargetPath=$env:TOSHY_PYTHON;
$s.Arguments='"'+$env:TOSHY_ENTRYPOINT+'"';$s.WorkingDirectory=$env:TOSHY_ROOT;
$s.Description='Toshy';$s.IconLocation=$env:TOSHY_ICON;$s.Save()''',
                       shortcut=temporary, python=python.with_name('pythonw.exe'), entrypoint=script, root=root, icon=shortcut_icon)
            os.replace(temporary, path)
            return publication.record(path, previous, label)
        finally:
            temporary.unlink(missing_ok=True)
    local = publication.attempt('Local shortcut', lambda: make_shortcut(root / 'Toshy.lnk', 'Local shortcut'))
    if local:
        state['local_launcher'] = str(local)
    def publish_known_folder(name, filename, label):
        directory = powershell("[Environment]::GetFolderPath($env:TOSHY_FOLDER)", folder=name)
        if not directory or not Path(directory).is_absolute():
            raise RuntimeError('Windows did not return an absolute ' + name + ' directory.')
        return make_shortcut(Path(directory) / filename, label)
    publication.attempt('Start menu shortcut', lambda: publish_known_folder('Programs', 'Toshy/Toshy.lnk', 'Start menu shortcut'))
    if desktop:
        publication.attempt('Desktop shortcut', lambda: publish_known_folder('DesktopDirectory', 'Toshy.lnk', 'Desktop shortcut'))


def create(root, state, *, desktop=True):
    root = Path(root)
    publication = _Publication(root, state)
    program = program_root(root, state['current'], state)
    # The stable launcher uses private Python, not an environment that may be
    # retired by a later update. The CLI selects the active app environment.
    python = Path(state['launcher_python'])
    launcher = root / 'launcher/toshy.py'
    icon = root / 'launcher/toshy_logo.svg'
    publication.attempt('Launcher icon', lambda: shutil.copy2(program / 'ui/assets/toshy_logo.svg', icon))
    implementation = _windows if os.name == 'nt' else _linux
    implementation(publication, root, state, program, python, launcher, icon, desktop)
    state['external_files'] = list(publication.owned.values())
    return state


def summarize(state, progress=print):
    report = state.get('launch_report', {})
    for item in report.get('created', []):
        progress(item['kind'] + ' created: ' + item['path'])
    if report.get('path'):
        progress(report['path'])
    for message in report.get('notes', []):
        progress(message)
    for message in report.get('warnings', []):
        progress('Warning: ' + message)


def remove(state, progress=print):
    extra = read_json(Path(state['root']) / 'launcher-receipts.json', [])
    records = {item['path']: item for item in [*state.get('external_files', []), *extra]}
    for item in records.values():
        path = Path(item['path'])
        if path.is_file() and not path.is_symlink() and sha256(path) == item['sha256']:
            if item.get('backup') and Path(item['backup']).is_file():
                shutil.copy2(item['backup'], path)
            else:
                path.unlink()
        elif path.exists():
            progress('Preserved a modified launcher: ' + str(path))
    # Only undo blocks recorded by older installers; new installs never edit shell files.
    for file in state.get('shell_profiles', []):
        path = Path(file)
        if path.is_file():
            text = path.read_text()
            path.write_text(text.replace(state.get('shell_block', '\x00'), ''))
    if os.name == 'nt' and state.get('path_entry'):
        powershell('''$p=[Environment]::GetEnvironmentVariable('Path','User');
[Environment]::SetEnvironmentVariable('Path',((($p -split ';') | Where-Object {[Environment]::ExpandEnvironmentVariables($_).Trim().Trim('"').TrimEnd('\\') -ine $env:TOSHY_BIN.TrimEnd('\\')}) -join ';'),'User')''', bin=state['path_entry'])
    if os.name == 'nt':
        from .windows_apps import remove as unregister
        unregister(state['root'])
