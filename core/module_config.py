"""Serialize module-list changes without overwriting unrelated configuration."""
from copy import deepcopy
from pathlib import Path

import yaml

from .private_files import atomic_private, file_lock


def edit_module_config(path, edit, *, commit=None):
    path = Path(path)
    with file_lock(path.with_name(path.name + '.lock')):
        if path.is_symlink():
            raise OSError('Module configuration cannot be a symbolic link.')
        original = path.read_bytes() if path.exists() else None
        data = yaml.safe_load(original.decode('utf-8-sig')) if original else {}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError('Application configuration must be a mapping.')
        for key in ('system_modules', 'custom_modules'):
            value = data.get(key)
            if value is not None and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)):
                raise ValueError(f'{key} must be a list of module names.')
        before = deepcopy(data)
        edit(data)
        changed = data != before
        if changed:
            atomic_private(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode('utf-8'))
        try:
            if commit is not None:
                commit()
        except BaseException:
            if changed:
                if original is None:
                    path.unlink()
                else:
                    atomic_private(path, original)
            raise
        return data


def set_module_enabled(path, key, name, enabled):
    if key not in ('system_modules', 'custom_modules'):
        raise ValueError('Unknown module category.')

    def edit(data):
        values = list(data.get(key) or [])
        if enabled and name not in values:
            values.append(name)
        elif not enabled:
            values = [value for value in values if value != name]
        data[key] = values

    return edit_module_config(path, edit)
