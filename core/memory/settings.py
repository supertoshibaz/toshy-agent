"""Shared memory-search preferences; new executions take a fresh snapshot."""
import yaml

from core.paths import APP_PATHS
from core.private_files import atomic_private, file_lock

DEFAULTS = {'page_limit': 3, 'temp_query_arguments': 'both'}


def normalize(values):
    result = dict(DEFAULTS)
    if not isinstance(values, dict):
        return result
    value = values.get('page_limit')
    if isinstance(value, int) and not isinstance(value, bool):
        result['page_limit'] = max(1, min(1000, value))
    if values.get('temp_query_arguments') in {'both', 'active_mode'}:
        result['temp_query_arguments'] = values['temp_query_arguments']
    return result


def load_settings():
    try:
        config = yaml.safe_load(APP_PATHS.config.read_text(encoding='utf-8')) or {}
    except FileNotFoundError:
        config = {}
    return normalize(config.get('memory_search'))


def save_settings(values):
    with file_lock(APP_PATHS.config.with_suffix('.lock')):
        config = yaml.safe_load(APP_PATHS.config.read_text(encoding='utf-8')) or {}
        config['memory_search'] = normalize(values)
        atomic_private(APP_PATHS.config, yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode('utf-8'))
