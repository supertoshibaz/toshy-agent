"""Explicit system-package plans and installation ownership receipts."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
from .network import ReleaseError


def detect():
    machine = platform.machine().lower()
    if machine not in {'amd64', 'x86_64'}:
        raise ReleaseError('This release supports x86-64 only; ARM is not supported yet.')
    if os.name == 'nt':
        return {'os': 'windows', 'id': 'windows', 'manager': 'winget', 'arch': 'x86_64'}
    if platform.system() != 'Linux':
        raise ReleaseError('Only Windows and Linux are supported.')
    libc, libc_version = platform.libc_ver()
    if libc != 'glibc' or tuple(map(int, libc_version.split('.')[:2])) < (2, 34):
        raise ReleaseError('This Qt build needs glibc 2.34 or newer (for example Ubuntu 22.04+, Debian 12+).')
    values = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip('"')
    identity = values.get('ID', '')
    manager = {'fedora': 'dnf', 'ubuntu': 'apt-get', 'debian': 'apt-get', 'arch': 'pacman'}.get(identity)
    if manager is None:
        raise ReleaseError(f'Unsupported distribution: {identity}. Supported: Fedora, Ubuntu, Debian, Arch.')
    return {'os': 'linux', 'id': identity, 'manager': manager, 'arch': 'x86_64', 'version': values.get('VERSION_ID', '')}


def executable(name):
    found = shutil.which(name)
    if found or os.name != 'nt':
        return found
    tails = {'git': ('Git/cmd/git.exe', 'Programs/Git/cmd/git.exe'),
             'docker': ('Docker/Docker/resources/bin/docker.exe', 'Programs/DockerDesktop/resources/bin/docker.exe')}
    for directory in filter(None, (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA'))):
        for tail in tails.get(name, ()):
            path = Path(directory) / tail
            if path.is_file():
                return str(path)
    return None


def installed(system, package):
    manager = system['manager']
    if manager == 'winget':
        from .windows_packages import installed as native_installed
        name = {'Git.Git': 'git', 'Docker.DockerDesktop': 'docker'}.get(package)
        if name and (executable(name) or native_installed(package)):
            return True
        if not shutil.which('winget'):
            return False
        command = ['winget', 'list', '--id', package, '--exact', '--accept-source-agreements']
    elif manager == 'apt-get':
        command = ['dpkg-query', '-W', '-f=${Status}', package]
    elif manager == 'dnf':
        command = ['rpm', '-q', package]
    else:
        command = ['pacman', '-Q', package]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode == 0 and (manager != 'apt-get' or 'install ok installed' in result.stdout)


def package_plan(system, components):
    if system['os'] == 'windows':
        plan = ([('Git.Git', 'git')] if 'git' in components and not executable('git') else []) + ([('Docker.DockerDesktop', 'docker')] if 'docker' in components and not executable('docker') else [])
        return [(package, kind) for package, kind in plan if not installed(system, package)]
    packages = {
        'apt-get': ['libegl1', 'libopengl0', 'libxcb-cursor0', 'libxkbcommon-x11-0', 'libxcb-icccm4', 'libxcb-keysyms1', 'libxcb-shape0', 'libxcb-xinerama0', 'libxcb-xfixes0', 'libxcb-render-util0'],
        'dnf': ['mesa-libEGL', 'libglvnd-opengl', 'xcb-util-cursor', 'libxkbcommon-x11', 'xcb-util-wm', 'xcb-util-keysyms', 'xcb-util-image'],
        'pacman': ['libglvnd', 'xcb-util-cursor', 'libxkbcommon-x11', 'xcb-util-wm', 'xcb-util-keysyms', 'xcb-util-image'],
    }[system['manager']]
    result = [(p, 'qt') for p in packages]
    if 'git' in components and not executable('git'):
        result.append(('git', 'git'))
    if 'docker' in components and not executable('docker'):
        result.extend((p, 'docker') for p in {'apt-get': ['docker.io'], 'dnf': ['moby-engine', 'docker-cli'], 'pacman': ['docker']}[system['manager']])
    if 'browser' in components:
        # Install native shared libraries only. Chromium itself is private.
        browser_packages = {
            'dnf': ['nss','nspr','atk','at-spi2-atk','cups-libs','libdrm','libXcomposite',
                    'libXdamage','libXrandr','libxkbcommon','mesa-libgbm','alsa-lib','pango','cairo'],
            'apt-get': ['libnss3','libnspr4','libatk1.0-0','libatk-bridge2.0-0','libcups2',
                        'libdrm2','libxcomposite1','libxdamage1','libxrandr2','libgbm1','libasound2','libpango-1.0-0','libcairo2'],
            'pacman': ['nss','nspr','at-spi2-core','libcups','libdrm','libxcomposite','libxdamage',
                       'libxrandr','libxkbcommon','mesa','alsa-lib','pango','cairo'],
        }[system['manager']]
        if system['manager'] == 'apt-get':
            version = tuple(int(p) for p in system.get('version', '0').split('.') if p.isdigit())
            if ((system['id'] == 'ubuntu' and version >= (24,4))
                    or (system['id'] == 'debian' and version >= (13,))):
                t64 = {'libatk1.0-0','libatk-bridge2.0-0','libcups2','libasound2'}
                browser_packages = [p + 't64' if p in t64 else p for p in browser_packages]
        result.extend((p, 'browser-library') for p in browser_packages)
    result = list(dict.fromkeys(result))
    return [(package, kind) for package, kind in result if not installed(system, package)]


def snapshot(system):
    """Include new dependencies in the receipt, without global autoremove."""
    manager = system['manager']
    if manager == 'winget':
        return None
    args = {'apt-get': ['dpkg-query', '-W', '-f=${binary:Package}\t${Status}\n'],
            'dnf': ['rpm', '-qa', '--qf', '%{NAME}\n'],
            'pacman': ['pacman', '-Qq']}[manager]
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return {line.split('\t')[0] for line in result.stdout.splitlines()
            if manager != 'apt-get' or 'install ok installed' in line}


def command(system, packages, *, remove=False):
    if not packages:
        return []
    if system['os'] == 'windows':
        action = 'uninstall' if remove else 'install'
        return [['winget', action, '--id', p, '--exact', '--accept-source-agreements', *([] if remove else ['--accept-package-agreements'])] for p in packages]
    manager = system['manager']
    prefix = [] if os.geteuid() == 0 else ['sudo']
    if prefix and not shutil.which('sudo'):
        raise ReleaseError('sudo is required for system packages. Install the displayed packages as administrator first.')
    if manager == 'pacman':
        return [prefix + ['pacman', '-R' if remove else '-S', '--noconfirm', *packages]]
    extra = ['--no-auto-remove'] if remove and manager == 'apt-get' else (['--no-autoremove'] if remove and manager == 'dnf' else [])
    return [prefix + [manager, 'remove' if remove else 'install', '-y', *extra, *packages]]


def apply(engine, detected, packages, *, remove=False):
    if not packages:
        return
    if detected['os'] == 'windows' and not shutil.which('winget'):
        from . import windows_packages
        engine.progress('winget is unavailable; using official signed Windows setup programs.')
        for package in packages:
            (windows_packages.remove if remove else windows_packages.install)(engine, package)
        return
    for args in command(detected, packages, remove=remove):
        engine.run(args, atomic=True, retry_network=not remove)
