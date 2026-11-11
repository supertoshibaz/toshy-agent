"""Verified private Chromium distribution for web tools (never a system browser)."""
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from .layout import read_json, write_json
from .network import ReleaseError, sha256
from .source import safe_name


def _directory(value):
    return isinstance(value, str) and re.fullmatch(r'chromium-\d+(?:\.\d+){3}', value)


def _record(path):
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def install(elements, network, pins):
    version = pins['version']
    name = 'chromium-' + version
    if not _directory(name):
        raise ReleaseError('Invalid private browser version.')
    platform = 'windows' if os.name == 'nt' else 'linux'
    item = pins[platform]
    root = Path(elements) / 'browsers'
    target = root / name
    binary = 'chrome-headless-shell.exe' if platform == 'windows' else 'chrome-headless-shell'
    for path in (Path(elements), root, target):
        if path.is_symlink():
            raise ReleaseError('Private browser directories cannot be links.')
    root.mkdir(parents=True, exist_ok=True)
    record = _record(target / '.installation.json')
    expected = item['sha256']
    def activate():
        active = _record(root / 'installed.json')
        previous = active.get('directory') if active.get('directory') != name else active.get('previous', '')
        write_json(root / 'installed.json', {'schema': 1, 'directory': name, 'previous': previous, 'executable': binary})
        # Retain this and the previous owned distribution for rollback. Never
        # touch browsers manually unpacked by the user.
        for path in root.glob('chromium-*'):
            if (path.name in {name, previous} or path.is_symlink() or not path.is_dir()
                    or not _directory(path.name)):
                continue
            if _record(path / '.installation.json').get('product') == 'Toshy private browser':
                shutil.rmtree(path)
    executable = target / binary
    if (record.get('product') == 'Toshy private browser' and record.get('archive_sha256') == expected
            and isinstance(record.get('files'), dict) and record['files'] and all((target / safe_name(n)).is_file()
            and not (target / n).is_symlink() and sha256(target / n) == digest
            for n, digest in record['files'].items())):
        if os.name != 'nt':
            executable.chmod(0o755)
        activate()
        return executable
    cache = root.parent.parent / 'cache' / ('chromium-' + version + '-' + platform + '.zip')
    network.download(item['url'], cache, digest=expected, size=item['size'])
    network.progress('Unpacking local Chromium ' + version + '…')
    temporary = Path(tempfile.mkdtemp(prefix='.chromium-', dir=root))
    try:
        hashes = {}
        with zipfile.ZipFile(cache) as archive:
            files = archive.infolist()
            if len(files) > 5000 or sum(p.file_size for p in files) > 1024 ** 3:
                raise ReleaseError('Private browser archive exceeds its size limit.')
            prefixes = {safe_name(p.filename).parts[0] for p in files}
            if len(prefixes) != 1:
                raise ReleaseError('Private browser archive must have one root.')
            seen = set()
            for info in files:
                parts = safe_name(info.filename).parts[1:]
                if not parts:
                    continue
                relative = '/'.join(parts)
                if (stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1
                        or info.file_size > 512 * 1024 ** 2 or relative.casefold() in seen):
                    raise ReleaseError('Unsafe private browser archive entry.')
                seen.add(relative.casefold())
                out = temporary.joinpath(*parts)
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                network.check()
                out.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, out.open('xb') as dest:
                    shutil.copyfileobj(src, dest)
                if os.name != 'nt':
                    out.chmod(0o755 if info.external_attr >> 16 & 0o111 or relative == binary else 0o644)
                hashes[relative] = sha256(out)
        if binary not in hashes or not (temporary / binary).stat().st_size:
            raise ReleaseError('The private browser executable is missing.')
        write_json(temporary / '.installation.json', {'schema': 1, 'product': 'Toshy private browser',
            'version': version, 'archive_sha256': expected, 'files': hashes})
        if target.exists():
            if record.get('product') != 'Toshy private browser':
                raise ReleaseError('A user browser already occupies ' + str(target))
            saved = root / ('.chromium-replaced-' + uuid.uuid4().hex)
            os.replace(target, saved)
            try:
                os.replace(temporary, target)
            except BaseException:
                os.replace(saved, target)
                raise
            shutil.rmtree(saved)
        else:
            os.replace(temporary, target)
        activate()
        return executable
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
