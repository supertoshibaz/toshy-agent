"""Plain startup options, usable before Qt and workspace imports."""
import json
from pathlib import Path


def workspace_options(data):
    options = data.get('workspaces') if isinstance(data, dict) else None
    options = options if isinstance(options, dict) else {}
    return {kind: options.get(kind, True) is not False for kind in ('board', 'paint')}


def load_workspace_options(path=None):
    if path is None:
        from core.paths import APP_PATHS
        path = APP_PATHS.runtime / 'app_performance.json'
    try:
        return workspace_options(json.loads(Path(path).read_text(encoding='utf-8')))
    except (OSError, ValueError):
        return workspace_options({})
