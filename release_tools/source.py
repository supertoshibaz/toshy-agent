"""Release source selection and portable archive validation."""
from pathlib import Path, PurePosixPath
import json
import re
import shutil
import stat
import zipfile
from urllib.parse import quote
from .network import ReleaseError, sha256


def repository(value):
    value = str(value).strip().removeprefix('https://github.com/').rstrip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', value):
        raise ReleaseError('Set a GitHub repository as OWNER/REPOSITORY.')
    return value


def version(value):
    if not re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value or ''):
        raise ReleaseError('Stable releases must use vMAJOR.MINOR.PATCH.')
    return tuple(map(int, value.removeprefix('v').split('.')))


def latest(network, repo):
    repo = repository(repo)
    release = network.json(f'https://api.github.com/repos/{repo}/releases/latest')
    if release.get('draft') or release.get('prerelease'):
        raise ReleaseError('The release is not stable.')
    version(release.get('tag_name'))
    commit = network.json(f'https://api.github.com/repos/{repo}/commits/' + quote(release['tag_name'], safe=''))
    revision = commit.get('sha', '')
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ReleaseError('Could not resolve the release to an exact commit.')
    return {'version': release['tag_name'].removeprefix('v'), 'commit': revision,
            'notes': str(release.get('body') or ''), 'published_at': release.get('published_at') or '',
            'url': f'https://github.com/{repo}/releases/tag/' + quote(release['tag_name'], safe=''),
            'repository': repo}


def safe_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '\\' in name or any(x in name for x in ('\x00', ':')) or '..' in p.parts:
        raise ReleaseError(f'Unsafe archive path: {name!r}')
    for part in p.parts:
        if part.endswith((' ', '.')) or part.split('.')[0].lower() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1,10)), *(f'lpt{i}' for i in range(1,10))}:
            raise ReleaseError('Archive contains a non-portable filename.')
    return p


def extract(archive, target, *, strip_root=False):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    seen = set()
    total = 0
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > 30000:
            raise ReleaseError('Too many archive entries.')
        roots = {safe_name(e.filename).parts[0] for e in entries}
        if strip_root and len(roots) != 1:
            raise ReleaseError('Source archive must have one root directory.')
        for entry in entries:
            path = safe_name(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                raise ReleaseError('Links and encrypted entries are not allowed.')
            parts = path.parts[1:] if strip_root else path.parts
            if not parts:
                continue
            name = '/'.join(parts)
            if name.casefold() in seen:
                raise ReleaseError('Duplicate or case-colliding archive entry.')
            seen.add(name.casefold())
            total += entry.file_size
            if total > 1024 ** 3 or entry.file_size > 128 * 1024 ** 2:
                raise ReleaseError('Source archive exceeds its size limit.')
            out = target.joinpath(*parts)
            if entry.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, out.open('xb') as dest:
                    shutil.copyfileobj(source, dest)
    return target


ROOT_FILES = {'app.py', 'run_ui.py', 'toshy.py', 'install.py', 'install.sh', 'install.ps1',
              'requirements.txt', 'release.json', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.txt'}
EXCLUDE = {'data', '__pycache__', 'node_modules', '.git', '.venv', 'venv', 'logs', 'sessions',
           'workspace', 'runtime', 'tests', 'knowledge', 'subagents'}


def distributable(path):
    p = PurePosixPath(path)
    if any(x in EXCLUDE or x.startswith('.') for x in p.parts) or p.suffix in {'.pyc', '.sqlite', '.db', '.log', '.bak'}:
        return False
    if len(p.parts) == 1:
        return path in ROOT_FILES
    if p.parts[0] in {'core', 'ui', 'release_tools'}:
        return path not in {'ui/app_state.json', 'ui/tags.json'} and p.name != 'config.yaml'
    if p.parts[0] == 'elements':
        return len(p.parts) == 3 and p.parts[1] == 'lsp' and (
            p.suffix == '.py' or p.name == 'lsp_defaults.json')
    if p.parts[0] == 'modules':
        if p.parts[1] == 'pipelines':
            # Saved graphs live in subdirectories beside the engine, but are
            # user data, not bundled engine modules.
            return len(p.parts) == 3 and p.suffix == '.py'
        return len(p.parts) == 2 or p.parts[1] in {'system', 'skills'} or p.parts[:3] == ('modules', 'custom', 'toshy_developer') or path == 'modules/custom/__init__.py'
    return False


def inventory(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob('*'))
            if p.is_file() and not p.is_symlink() and distributable(p.relative_to(root).as_posix())
            and p.name != 'release-manifest.json'}


def verify(root, expected_version=None):
    root = Path(root)
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    config = json.loads((root / 'release.json').read_text(encoding='utf-8'))
    if config.get('schema') != 1 or not isinstance(config.get('python'), str) or not re.fullmatch(r'3\.\d+\.\d+', config['python']):
        raise ReleaseError('Invalid release configuration or Python version.')
    if not isinstance(config.get('platforms'), list) or not config['platforms']:
        raise ReleaseError('The release must declare supported platforms.')
    for field in ('data_schema', 'minimum_updater'):
        if type(config.get(field)) is not int or config[field] < 1:
            raise ReleaseError(f'Invalid {field} in release configuration.')
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
        raise ReleaseError('Invalid release inventory.')
    version(config.get('version'))
    if expected_version is not None and config['version'] != expected_version:
        raise ReleaseError('Release tag and application version differ.')
    if manifest.get('version') != config['version']:
        raise ReleaseError('Release inventory version differs.')
    actual = inventory(root)
    if actual != manifest['files']:
        raise ReleaseError('Release file inventory or SHA-256 verification failed.')
    for required in ('app.py', 'run_ui.py', 'core/version.py', 'release_tools/engine.py',
                     'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.txt',
                     'elements/lsp/lsp_defaults.json', 'modules/skills/__init__.py',
                     'modules/skills/store.py', 'modules/skills/runtime.py',
                     'release_tools/locks/linux-x86_64.txt', 'release_tools/locks/windows-x86_64.txt'):
        if required not in actual:
            raise ReleaseError(f'Required release file is missing: {required}')
    return config


def copy_source(source, destination):
    source, destination = Path(source), Path(destination)
    manifest = json.loads((source / 'release-manifest.json').read_text(encoding='utf-8'))
    for name in [*manifest['files'], 'release-manifest.json']:
        safe_name(name)
        origin = source / name
        if origin.is_symlink():
            raise ReleaseError('Source inventory contains a symbolic link.')
        out = destination / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, out)
