"""Qt preferences stay inside a managed installation's data directory."""
import os
from pathlib import Path

if os.environ.get('TOSHY_DATA_DIR'):
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(Path(os.environ['TOSHY_DATA_DIR']) / 'preferences/qt'))
    if os.name != 'nt':
        # Organization/application constructors use NativeFormat explicitly on
        # Linux, independently of setDefaultFormat.
        QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope,
                          str(Path(os.environ['TOSHY_DATA_DIR']) / 'preferences/qt'))
