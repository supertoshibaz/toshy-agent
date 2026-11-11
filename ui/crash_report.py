"""Keep Python thread stacks for native GUI crashes, before Qt is imported."""
import faulthandler
import os
import platform
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from core.paths import APP_PATHS


_fault_log_file = None  # faulthandler requires this descriptor to remain open.


def enable_ui_fault_log(directory=None):
    global _fault_log_file
    if _fault_log_file is not None:
        return Path(_fault_log_file.name)
    stream = None
    try:
        from core.log_session import session_log_path
        path = session_log_path(directory, new=directory is not None)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        stream = path.open('a', encoding='utf-8')
        try:
            pyside_version = version('PySide6')
        except PackageNotFoundError:
            pyside_version = 'not installed'
        stream.write(f'Started (UTC): {stamp}\nPython: {sys.version}\n'
                     f'Platform: {platform.platform()}\nPySide6: {pyside_version}\n'
                     f'QT_QPA_PLATFORM: {os.environ.get("QT_QPA_PLATFORM", "auto")}\n'
                     f'XDG_SESSION_TYPE: {os.environ.get("XDG_SESSION_TYPE", "unknown")}\n\n')
        stream.flush()
        faulthandler.enable(file=stream, all_threads=True)
        _fault_log_file = stream
    except (OSError, RuntimeError, ValueError):
        if stream is not None:
            stream.close()
        # A read-only installation must still be able to start its GUI.
        try:
            faulthandler.enable(all_threads=True)
        except (OSError, RuntimeError, ValueError, AttributeError):
            pass
        return None
    return path
