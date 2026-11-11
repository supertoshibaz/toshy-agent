"""Versioned offline data migrations shipped inside the verified source tree.

Each schema_N_to_N+1.py is a stdlib-only script accepting DATA_DIRECTORY. It
must restrict writes to config.yaml, core, ui, preferences, sessions, modules
and plugins. Workspace files and model/cache directories are not migration
targets. The updater runs scripts only after both UI and core have stopped.
"""
from pathlib import Path
import shutil
from .network import ReleaseError
from .layout import data_root


DATA_ITEMS = ('config.yaml', 'core', 'ui', 'preferences', 'sessions', 'modules', 'plugins')


def scripts(program, previous, target):
    if target < previous:
        raise ReleaseError('A release cannot downgrade the data schema.')
    result = []
    for schema in range(previous, target):
        path = Path(program) / 'release_tools/data_migrations' / f'schema_{schema}_to_{schema + 1}.py'
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f'Missing data migration: schema {schema} to {schema + 1}.')
        result.append(path)
    return result


def restore_backup(root, backup):
    data = data_root(root)
    backup = Path(backup)
    for name in DATA_ITEMS:
        target, original = data / name, backup / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        if original.is_dir():
            shutil.copytree(original, target)
        elif original.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
