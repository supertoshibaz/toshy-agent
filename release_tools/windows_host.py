"""Build the GUI host with Windows' .NET Framework compiler, without bundling Python."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from . import layout
from .network import sha256


def available(root, python):
    directory = Path(root) / 'launcher'
    try:
        config = (directory / 'python-host.cfg').read_text(encoding='utf-8').splitlines()
        return (len(config) == 4 and (directory / 'Toshy.exe').is_file()
                and os.path.normcase(os.path.abspath(config[2])) == os.path.normcase(os.path.abspath(str(python)))
                and Path(config[1]).is_file() and Path(config[3]).is_file())
    except (OSError, ValueError):
        return False


def build(publication, root, state, python, icon):
    from .engine import python_in
    from .environments import environment
    root = Path(root)
    source = Path(__file__).with_suffix('.cs')
    compiler_root = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET'
    compiler = next((p for kind in ('Framework64', 'Framework')
                     if (p := compiler_root / kind / 'v4.0.30319/csc.exe').is_file()), None)
    if compiler is None:
        raise RuntimeError('Windows .NET Framework C# compiler is unavailable. Toshy will use its logged Python launcher.')
    program = layout.program_root(root, state['current'], state)
    version = layout.read_json(program / 'release.json')['python'].split('.')
    dll = Path(python).parent / ('python' + ''.join(version[:2]) + '.dll')
    if not dll.is_file():
        raise RuntimeError('Private Python DLL is missing: ' + str(dll))
    target = root / 'launcher/Toshy.exe'
    previous = publication.guard(target)
    fingerprint = hashlib.sha256((sha256(source) + sha256(icon)).encode()).hexdigest()
    stamp = root / 'launcher/windows-host.json'
    stored = layout.read_json(stamp)
    if not target.is_file() or stored.get('source') != fingerprint or stored.get('sha256') != sha256(target):
        with tempfile.TemporaryDirectory(prefix='.windows-host-', dir=root / 'launcher') as temporary:
            output = Path(temporary) / 'Toshy.exe'
            result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
                                     '/out:' + str(output), '/win32icon:' + str(icon), str(source)],
                                    capture_output=True, text=True, errors='replace', timeout=120,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or not output.is_file():
                raise RuntimeError('Could not build Toshy.exe: ' + (result.stdout + result.stderr)[-2000:])
            os.replace(output, target)
        layout.write_json(stamp, {'source': fingerprint, 'sha256': sha256(target)})
    publication.record(target, previous, 'Windows application host')
    startup = root / 'launcher/release_tools/windows_startup.py'
    config = '\n'.join(map(str, (root, dll, python_in(environment(root, state['current'], state)), startup))) + '\n'
    publication.write(root / 'launcher/python-host.cfg', config, label='Windows application host configuration')
    return target
