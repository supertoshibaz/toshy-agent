"""Official signed Git/Docker setup programs when Windows has no winget."""
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from .network import ReleaseError

PACKAGES = {
    'Git.Git': ('Git for Windows', r'Johannes Schindelin|Git for Windows'),
    'Docker.DockerDesktop': ('Docker Desktop', r'Docker(?:,? Inc\.?)?'),
}


def records(package):
    if package not in PACKAGES:
        raise ReleaseError('Unsupported Windows component: ' + package)
    if os.name != 'nt':
        return []
    import winreg
    result = []
    key_name = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = str(winreg.QueryValueEx(key, 'DisplayName')[0]).strip().lower()
                                matches = (name == 'git' or name.startswith('git version ')) if package == 'Git.Git' else name.startswith('docker desktop')
                                if not matches:
                                    continue
                                try:
                                    command = str(winreg.QueryValueEx(key, 'UninstallString')[0])
                                except OSError:
                                    command = ''
                                result.append({'name': name, 'uninstall': command})
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def installed(package):
    return bool(records(package))


def download(engine, package):
    if package == 'Git.Git':
        release = engine.network.json('https://api.github.com/repos/git-for-windows/git/releases/latest')
        assets = [item for item in release.get('assets', [])
                  if re.fullmatch(r'Git-[0-9][0-9A-Za-z.]*-64-bit\.exe', str(item.get('name', '')))]
        if len(assets) != 1:
            raise ReleaseError('The official Git release has no unique x64 setup program.')
        item = assets[0]
        url = item['browser_download_url']
        parsed = urlsplit(url)
        if parsed.hostname != 'github.com' or not parsed.path.startswith('/git-for-windows/git/releases/download/'):
            raise ReleaseError('Unexpected Git installer source.')
        digest = item.get('digest') or ''
        digest = digest[7:] if re.fullmatch('sha256:[a-f0-9]{64}', digest) else None
        target = engine.root / 'cache/windows-installers' / item['name']
        return engine.network.download(url, target, digest=digest, size=item.get('size'), limit=512 * 1024**2)
    if package == 'Docker.DockerDesktop':
        target = engine.root / 'cache/windows-installers/Docker Desktop Installer.exe'
        return engine.network.download('https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe', target)
    raise ReleaseError('Unsupported Windows component: ' + package)


def setup(engine, package, executable, arguments=''):
    title, publisher = PACKAGES[package]
    executable = Path(executable).absolute()
    if executable.suffix.lower() != '.exe' or not executable.is_file():
        raise ReleaseError('Windows setup executable is missing: ' + str(executable))
    env = os.environ.copy()
    env.update(TOSHY_SETUP_FILE=str(executable), TOSHY_SETUP_ARGUMENTS=arguments, TOSHY_SETUP_PUBLISHER=publisher)
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$signature = Get-AuthenticodeSignature -LiteralPath $env:TOSHY_SETUP_FILE
if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or
    $signature.SignerCertificate.Subject -notmatch $env:TOSHY_SETUP_PUBLISHER) {
    throw ('Installer signature/publisher verification failed: ' + $env:TOSHY_SETUP_FILE + '. Status: ' + $signature.Status)
}
Write-Host ('Verified publisher: ' + $signature.SignerCertificate.Subject)
Write-Host 'Complete the setup window to continue. Windows may request administrator permission.'
$options = @{FilePath=$env:TOSHY_SETUP_FILE; Wait=$true; PassThru=$true}
if ($env:TOSHY_SETUP_ARGUMENTS) { $options.ArgumentList = $env:TOSHY_SETUP_ARGUMENTS }
$process = Start-Process @options
if ($process.ExitCode -in @(3010,1641)) {
    Write-Host 'The component requires a Windows restart before it can be used.'
    exit 0
}
exit $process.ExitCode
'''
    engine.progress('Opening the official ' + title + ' setup window…')
    engine.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
               env=env, atomic=True, label='Waiting for ' + title + ' setup')


def install(engine, package):
    executable = download(engine, package)
    setup(engine, package, executable, 'install' if package == 'Docker.DockerDesktop' else '')
    from .system import executable as find_executable
    name = 'git' if package == 'Git.Git' else 'docker'
    if not installed(package) and not find_executable(name):
        raise ReleaseError(PACKAGES[package][0] + ' was not installed. Setup may have been cancelled. Retry or deselect this component.')


def remove(engine, package):
    entries = records(package)
    if not entries:
        return
    # Parse only a registry EXE command. Never pass registry text to a shell.
    command = os.path.expandvars(entries[0]['uninstall']).strip()
    match = re.fullmatch(r'"([^"\r\n]+\.exe)"\s*(.*)', command, flags=re.I)
    if not match:
        match = re.fullmatch(r'([^"\r\n]+?\.exe)(?:\s+(.*))?', command, flags=re.I)
    if not match or not Path(match[1]).is_absolute():
        raise ReleaseError('Remove ' + PACKAGES[package][0] + ' from Windows Settings → Apps; its registered uninstaller is unavailable.')
    setup(engine, package, match[1], match[2] or '')
