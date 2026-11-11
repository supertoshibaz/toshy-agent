"""Размещение материалов проекта без изменения уже существующих файлов."""
from pathlib import Path
from core.paths import APP_PATHS


def normalize_assets(value):
    value = value if isinstance(value, dict) else {}
    mode = value.get('mode', 'toshy')
    if mode not in {'toshy', 'project', 'custom'}:
        raise ValueError('Неизвестный способ хранения материалов.')
    path = str(value.get('path') or '').strip()
    target_project = str(value.get('project_id') or '')
    if mode == 'project' and not target_project:
        candidate = Path(path or 'toshy-assets')
        if candidate.is_absolute() or '..' in candidate.parts or candidate == Path('.'):
            raise ValueError('Выберите подпапку внутри Project Folder.')
        path = str(candidate)
    elif mode == 'custom' and not path:
        raise ValueError('Укажите папку материалов.')
    return dict(mode=mode, path=path, **({'project_id': target_project} if mode == 'project' and target_project else {}))


def assets_root(project_id, workspace, settings=None, projects=None):
    choice = normalize_assets(settings)
    if choice['mode'] == 'project':
        target = choice.get('project_id')
        if target:
            record = (projects or {}).get(target)
            if record is None and choice.get('path'):
                return Path(choice['path']).expanduser().resolve()
            if record is None and target != project_id:
                raise ValueError('Проект, выбранный для материалов, недоступен.')
            raw = ((record or {}).get('settings') or {}).get('workspace_path') or (record or {}).get('workspace')
            if not raw and target == project_id:
                raw = workspace
            if not raw:
                raise ValueError('У выбранного проекта не задан Project Folder.')
            return Path(raw).expanduser().resolve()
        root = Path(workspace).expanduser().resolve()
        path = (root / choice['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Папка материалов выходит за пределы Project Folder.')
        return path
    if choice['mode'] == 'custom':
        return Path(choice['path']).expanduser().resolve()
    # Internal materials are the shared Inbox, independent of the selected
    # project's catalog ID. Existing project-specific folders stay untouched.
    return APP_PATHS.root / 'materials' / 'project_inbox'
