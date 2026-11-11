"""Execution and network limits; new runs/requests read the latest saved values."""
import os
import tempfile
import yaml
from core.paths import APP_PATHS

DEFAULTS = {'max_steps': 20, 'response_timeout': 6000, 'connect_timeout': 15}
BOUNDS = {'max_steps': (1, 10000), 'response_timeout': (1, 86400), 'connect_timeout': (1, 600)}


def normalize(values):
    result = dict(DEFAULTS)
    if isinstance(values, dict):
        for key, (low, high) in BOUNDS.items():
            value = values.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result[key] = max(low, min(high, value))
    return result


def load_settings():
    try:
        with APP_PATHS.config.open(encoding='utf-8') as handle:
            config = yaml.safe_load(handle) or {}
        return normalize(config.get('core_timeouts'))
    except (OSError, yaml.YAMLError, AttributeError):
        return dict(DEFAULTS)


def save_settings(values):
    with APP_PATHS.config.open(encoding='utf-8') as handle:
        config = yaml.safe_load(handle) or {}
    config['core_timeouts'] = normalize(values)
    fd, temporary = tempfile.mkstemp(prefix='.timeouts-', dir=APP_PATHS.config.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, APP_PATHS.config)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
