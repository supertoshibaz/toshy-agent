"""Maintainer commands: release inventory, pinned locks and plugin publication."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import shutil
import tempfile
import zipfile
from . import source, layout
from .network import ReleaseError


def inventory(root):
    root = Path(root).resolve()
    config = json.loads((root / 'release.json').read_text())
    source.version(config['version'])
    namespace = {}
    # This is the maintainer's own source, not downloaded third-party code.
    text = (root / 'core/version.py').read_text()
    match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not match or match.group(1) != config['version']:
        raise ReleaseError('core/version.py and release.json versions differ.')
    from .components import write
    write(root)
    hashes = source.inventory(root)
    layout.write_json(root / 'release-manifest.json', {'schema': 1, 'version': config['version'], 'files': hashes})
    source.verify(root)
    return hashes


def compile_locks(root):
    config = layout.read_json(root / 'release.json')
    uv = shutil.which('uv')
    if not uv:
        candidate = root / 'tools' / ('uv.exe' if sys.platform == 'win32' else 'uv')
        if candidate.is_file():
            uv = str(candidate)
    if not uv:
        raise ReleaseError('uv is required to prepare release dependency locks. Use the installed tools/uv.')
    # Replace the locks only after both platforms have resolved successfully.
    with tempfile.TemporaryDirectory(prefix='toshy-locks-') as temporary:
        outputs = []
        for os_name, target in [('linux', 'x86_64-unknown-linux-gnu'), ('windows', 'x86_64-pc-windows-msvc')]:
            for filename, prefix in [('requirements.txt', ''), ('release_tools/pylsp.in', 'pylsp-')]:
                output = Path(temporary) / f'{prefix}{os_name}-x86_64.txt'
                subprocess.run([uv, 'pip', 'compile', str(root / filename), '--python-version', config['python'],
                    '--python-platform', target, '--generate-hashes', '--no-header', '--only-binary', ':all:',
                    '--output-file', str(output)], check=True)
                outputs.append(output)
        for output in outputs:
            shutil.copy2(output, root / 'release_tools/locks' / output.name)


def prepare_release(root, version, *, keep_locks=False, output=None):
    source.version(version)
    output = Path(output or root.parent / ('toshy-' + version.removeprefix('v') + '.zip')).resolve()
    if output.exists():
        raise ReleaseError('The release archive already exists: ' + str(output))
    if output.is_relative_to(root) and source.distributable(output.relative_to(root).as_posix()):
        raise ReleaseError('Write the release archive outside the application source directories.')
    if keep_locks:
        from .components import describe
        previous = layout.read_json(root / 'release_tools/components.json')
        current = describe(root)
        if not previous or any(previous.get(key) != current[key] for key in ('python', 'inputs', 'locks')):
            raise ReleaseError('Requirements, Python or dependency locks changed. Run release without --keep-locks.')
    else:
        compile_locks(root)
    value = layout.read_json(root / 'release.json')
    value['version'] = version.removeprefix('v')
    layout.write_json(root / 'release.json', value)
    path = root / 'core/version.py'
    path.write_text(re.sub(r"(__version__\s*=\s*)['\"][^'\"]+['\"]",
        lambda m: m.group(1) + repr(value['version']), path.read_text()))
    inventory(root)
    with tempfile.TemporaryDirectory(prefix='toshy-release-') as temporary:
        stage = Path(temporary) / 'toshy'
        source.copy_source(root, stage)
        shutil.copy2(root / 'release_tools/default_config.json', stage / 'config.yaml')
        for name in ('.gitignore', '.github', 'repository_templates', 'README.md', 'README.ru.md'):
            origin = root / name
            if origin.is_symlink():
                raise ReleaseError('Release extras cannot be links.')
            if origin.is_dir():
                for item in origin.rglob('*'):
                    relative = item.relative_to(origin)
                    if any(part in source.EXCLUDE or part.startswith('.env') for part in relative.parts):
                        continue
                    if item.is_symlink():
                        raise ReleaseError('Release extras cannot contain links.')
                    if item.is_file() and item.suffix != '.pyc':
                        target = stage / name / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
            elif origin.is_file():
                shutil.copy2(origin, stage / name)
        source.verify(stage)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(stage.rglob('*')):
                    if file.is_file():
                        archive.write(file, file.relative_to(stage.parent))
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare source releases without attaching application archives.')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inventory')
    commands.add_parser('verify')
    bump = commands.add_parser('version')
    bump.add_argument('version')
    configure = commands.add_parser('configure')
    configure.add_argument('--repository', required=True)
    configure.add_argument('--catalog', required=True)
    commands.add_parser('lock')
    release = commands.add_parser('release')
    release.add_argument('version')
    release.add_argument('--keep-locks', action='store_true', help='Reuse verified locks when requirements and Python did not change.')
    release.add_argument('--output', type=Path)
    entry = commands.add_parser('plugin-entry')
    entry.add_argument('source', type=Path)
    entry.add_argument('archive', type=Path)
    entry.add_argument('--archive-url', required=True)
    entry.add_argument('--image-url', required=True)
    entry.add_argument('--source-url', required=True)
    entry.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == 'inventory':
            print('Release files:', len(inventory(root)))
        elif args.command == 'verify':
            source.verify(root)
            from .components import verify
            verify(root)
            print('Release inventory and dependencies verified.')
        elif args.command == 'version':
            source.version(args.version)
            value = json.loads((root / 'release.json').read_text())
            value['version'] = args.version.removeprefix('v')
            layout.write_json(root / 'release.json', value)
            path = root / 'core/version.py'
            text = re.sub(r"(__version__\s*=\s*)['\"][^'\"]+['\"]", lambda m: m.group(1) + repr(value['version']), path.read_text())
            path.write_text(text)
            inventory(root)
        elif args.command == 'configure':
            repo = source.repository(args.repository)
            catalog = source.repository(args.catalog)
            value = layout.read_json(root / 'release.json')
            value.update(repository=repo, plugin_catalog='https://github.com/' + catalog)
            layout.write_json(root / 'release.json', value)
            inventory(root)
        elif args.command == 'lock':
            compile_locks(root)
            inventory(root)
        elif args.command == 'release':
            print('Release archive:', prepare_release(root, args.version, keep_locks=args.keep_locks, output=args.output))
        elif args.command == 'plugin-entry':
            from core.plugins.package import inspect_package, source_digest, package_digest
            from core.plugins.manifest import tags_for, validate_catalog
            manifest = inspect_package(args.archive)
            record = {k: manifest[k] for k in ('id', 'version', 'name', 'description', 'author', 'license', 'compatibility')}
            record.update(tags=tags_for(manifest), image=args.image_url, source=args.source_url,
                          source_sha256=source_digest(args.source), archive={
                              'url': args.archive_url, 'sha256': package_digest(args.archive), 'size': args.archive.stat().st_size})
            validate_catalog({'schema_version': 1, 'plugins': [record]})
            layout.write_json(args.output, record)
        return 0
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
