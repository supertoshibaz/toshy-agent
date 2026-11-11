"""Portable, explicit-file plugin archives; extraction never runs Python/pip."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, BadZipFile

from .manifest import PluginError, safe_path, validate_manifest

MAX_FILES = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
EXCLUDED_DIRS = {'.git', '.hg', '.svn', '.venv', 'venv', 'env', '__pycache__',
                 'node_modules', 'logs', 'sessions', 'runtime', '.pytest_cache', '.mypy_cache'}
EXCLUDED_FILES = {'.env', 'auth.json', 'credentials.json', 'secrets.json', 'oauth_tokens.json'}


def shareable(path):
    parts = PurePosixPath(path).parts
    return not (any(part.casefold() in EXCLUDED_DIRS for part in parts)
                or any(part.startswith('.toshy-') for part in parts)
                or parts[-1].casefold() in EXCLUDED_FILES
                or parts[-1].casefold().startswith('.env.')
                or parts[-1].casefold().endswith(('.pyc', '.pyo', '.log', '.pem', '.key')))


def _declared(path, manifest):
    return any(path == item or path.startswith(item + '/') for item in manifest['files'])


def _validate_inventory(manifest, sizes):
    if len(sizes) > MAX_FILES or sum(sizes.values()) > MAX_BYTES:
        raise PluginError('Package exceeds file count or uncompressed size limit.')
    folded = {}
    for name, size in sizes.items():
        safe_path(name)
        if name.casefold() in folded:
            raise PluginError(f'Case-insensitive path collision: {name}')
        folded[name.casefold()] = name
        if size > MAX_FILE_BYTES:
            raise PluginError(f'Package file exceeds 32 MiB: {name}')
        if name != 'plugin.json' and (not _declared(name, manifest) or not shareable(name)):
            raise PluginError(f'Undeclared or private file: {name}')
    # Also reject a file which would have to become another file's directory.
    for name in sizes:
        for parent in PurePosixPath(name).parents:
            if str(parent).casefold() in folded:
                raise PluginError(f'File/directory collision: {name}')
    for item in manifest['files']:
        if not any(name == item or name.startswith(item + '/') for name in sizes):
            raise PluginError(f'Declared payload is empty or missing: {item}')
    image = manifest['image']
    if image not in sizes or sizes[image] > MAX_IMAGE_BYTES:
        raise PluginError('Plugin image is missing or exceeds 2 MiB.')
    if 'LICENSE' not in sizes:
        raise PluginError('A declared LICENSE file is required.')
    for component in manifest['components']:
        if 'path' in component:
            path = component['path']
            if not any(name == path or name.startswith(path + '/') for name in sizes):
                raise PluginError(f'Module content is missing: {path}')
        else:
            module = component['entrypoint'].split(':')[0].replace('.', '/')
            if module + '.py' not in sizes and module + '/__init__.py' not in sizes:
                raise PluginError(f'Entrypoint source is missing: {module}')


def _json(data):
    if len(data) > MAX_MANIFEST_BYTES:
        raise PluginError('Manifest exceeds 128 KiB.')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PluginError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    try:
        return validate_manifest(json.loads(data.decode('utf-8'), object_pairs_hook=unique))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PluginError(f'Invalid manifest JSON: {error}') from error


def _archive_manifest(archive):
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise PluginError('Too many archive entries.')
    sizes, members = {}, {}
    directory_names = set()
    for entry in infos:
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        safe_path(name)
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise PluginError('Links and special files are not allowed in plugin packages.')
        if entry.flag_bits & 1:
            raise PluginError('Encrypted plugin archives are not supported.')
        if name.casefold() in directory_names or name in members:
            raise PluginError(f'Duplicate archive path: {name}')
        if entry.is_dir():
            directory_names.add(name.casefold())
            continue
        sizes[name] = entry.file_size
        members[name] = entry
    if 'plugin.json' not in members or sizes['plugin.json'] > MAX_MANIFEST_BYTES:
        raise PluginError('A root plugin.json manifest is required.')
    manifest = _json(archive.read(members['plugin.json']))
    _validate_inventory(manifest, sizes)
    for directory in directory_names:
        if directory in {name.casefold() for name in members}:
            raise PluginError(f'File/directory collision: {directory}')
    return manifest, members


def inspect_package(path):
    if Path(path).stat().st_size > MAX_BYTES:
        raise PluginError('Plugin archive exceeds 256 MiB.')
    try:
        with ZipFile(path) as archive:
            manifest, members = _archive_manifest(archive)
            for entry in members.values():
                # Verify CRCs with bounded memory before the package is accepted.
                with archive.open(entry) as stream:
                    while stream.read(64 * 1024):
                        pass
            return manifest
    except (BadZipFile, RuntimeError, EOFError) as error:
        raise PluginError(f'Invalid plugin archive: {error}') from error


def extract_package(path, destination):
    """Destination must be a new, private staging directory owned by the host."""
    destination = Path(destination)
    if destination.exists():
        raise PluginError('Extraction requires a fresh staging directory.')
    try:
        with ZipFile(path) as archive:
            manifest, members = _archive_manifest(archive)
            destination.mkdir(parents=True)
            for name, entry in members.items():
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('xb') as output:
                    remaining = entry.file_size
                    while remaining:
                        chunk = source.read(min(64 * 1024, remaining))
                        if not chunk:
                            raise PluginError(f'Truncated archive member: {name}')
                        output.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        raise PluginError(f'Incorrect archive member length: {name}')
            return manifest
    except (BadZipFile, RuntimeError, EOFError) as error:
        raise PluginError(f'Invalid plugin archive: {error}') from error


def inspect_directory(directory):
    if Path(directory).is_symlink():
        raise PluginError('Plugin source directory cannot be a symbolic link.')
    root = Path(directory).resolve()
    if (root / 'plugin.json').is_symlink():
        raise PluginError('The manifest cannot be a symbolic link.')
    manifest = _json((root / 'plugin.json').read_bytes())
    return inspect_directory_manifest(root, manifest)


def inspect_directory_manifest(directory, manifest):
    """Validate staged authoring metadata against actual files, without rewriting it."""
    if Path(directory).is_symlink():
        raise PluginError('Plugin source directory cannot be a symbolic link.')
    root = Path(directory).resolve()
    if (root / 'plugin.json').is_symlink():
        raise PluginError('The manifest cannot be a symbolic link.')
    manifest = validate_manifest(manifest)
    files = {'plugin.json': root / 'plugin.json'}
    for item in manifest['files']:
        source = root.joinpath(*safe_path(item).parts)
        # Reject symlinks in all parent components, not just the final file.
        current = source
        while current != root:
            if current.is_symlink():
                raise PluginError(f'Symbolic link in payload: {item}')
            current = current.parent
        def walk():
            for folder, directories, filenames in os.walk(source, followlinks=False):
                directories[:] = [name for name in directories
                    if shareable((Path(folder) / name).relative_to(root).as_posix())]
                for name in directories:
                    if (Path(folder) / name).is_symlink():
                        raise PluginError(f'Symbolic link directory in payload: {name}')
                for name in filenames:
                    yield Path(folder) / name
        candidates = walk() if source.is_dir() else (source,)
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            if not shareable(relative):
                continue
            if candidate.is_symlink():
                raise PluginError(f'Symbolic link in payload: {relative}')
            if candidate.is_file():
                files[relative] = candidate
    _validate_inventory(manifest, {name: path.stat().st_size for name, path in files.items()})
    return manifest, files


def export_package(directory, destination):
    manifest, files = inspect_directory(directory)
    destination = Path(destination).resolve()
    root = Path(directory).resolve()
    if destination.is_relative_to(root):
        raise PluginError('Export destination must be outside the plugin source directory.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.plugin-export-', suffix='.zip', dir=destination.parent)
    os.close(descriptor)
    try:
        with ZipFile(temporary, 'w', ZIP_DEFLATED) as archive:
            for name, source in sorted(files.items()):
                info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, source.read_bytes())
        inspect_package(temporary)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def package_digest(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        while chunk := source.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_digest(directory):
    """Bind a release to declared bytes independently of ZIP compression tools."""
    _, files = inspect_directory(directory)
    fingerprints = [(name, package_digest(path)) for name, path in sorted(files.items())]
    return hashlib.sha256(json.dumps(fingerprints, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()
