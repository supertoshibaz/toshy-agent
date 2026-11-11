"""Update checks shared by the Qt window and CLI."""
from pathlib import Path
import os
import time
from . import layout, source
from .network import Network, ReleaseError


def installation_root():
    value = os.environ.get('TOSHY_INSTALL_ROOT')
    if value:
        return Path(value).absolute()
    root = Path(__file__).resolve().parent.parent
    if (root / 'installation.json').is_file() and layout.is_portable(root):
        layout.owned(root)
        return root
    return None


def settings_path():
    from core.paths import APP_PATHS
    return APP_PATHS.runtime / 'update-settings.json'


def settings():
    value = layout.read_json(settings_path())
    if not value.get('repository'):
        root = installation_root()
        if root:
            value['repository'] = layout.owned(root).get('repository', '')
        else:
            from core.paths import APP_PATHS
            value['repository'] = layout.read_json(APP_PATHS.code_root / 'release.json').get('repository', '')
    return value


def save_settings(**values):
    state = settings()
    state.update(values)
    layout.write_json(settings_path(), state)
    return state


def check(*, cancel=None):
    value = settings()
    release = source.latest(Network(cancel), value.get('repository', ''))
    from core.version import __version__
    release['newer'] = source.version(release['version']) > source.version(__version__)
    save_settings(last_checked=time.time(), latest=release)
    return release
