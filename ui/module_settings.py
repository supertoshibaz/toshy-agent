"""Discover module-owned Startup pages without executing module entrypoints."""

import hashlib
import importlib
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import types

from PySide6.QtWidgets import QWidget


def load_settings_page(category, name, parent=None, *, root=None):
    from core.plugins.modules import module_directory

    directory = Path(module_directory(category, name, root)).resolve()
    if not (directory / "ui" / "settings.py").is_file():
        return None
    # UI imports use a private package. Loading settings must not activate the
    # module, initialize its services, or require its runtime dependencies.
    prefix = (
        "_toshy_module_settings_"
        + hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    )
    if prefix not in sys.modules:
        package = types.ModuleType(prefix)
        package.__package__ = prefix
        package.__path__ = [str(directory)]
        package.__spec__ = ModuleSpec(prefix, loader=None, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        sys.modules[prefix] = package
    try:
        module = importlib.import_module(prefix + ".ui.settings")
        factory = getattr(module, "create_settings_page", None)
        if not callable(factory):
            raise ValueError(
                "В ui/settings.py отсутствует create_settings_page(parent)."
            )
        page = factory(parent)
        if not isinstance(page, QWidget):
            raise TypeError("create_settings_page(parent) должен вернуть QWidget.")
        return page
    except Exception:
        for key in list(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                sys.modules.pop(key, None)
        raise
