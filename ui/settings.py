"""Portable QSettings constructors; managed Windows installs avoid registry keys."""
import os
from PySide6.QtCore import QSettings as QtSettings


class QSettings(QtSettings):
    def __init__(self, *args, **kwargs):
        if os.environ.get('TOSHY_DATA_DIR'):
            if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
                args = (QtSettings.IniFormat, QtSettings.UserScope, *args)
            elif len(args) >= 4 and isinstance(args[0], QtSettings.Format):
                args = (QtSettings.IniFormat, *args[1:])
        super().__init__(*args, **kwargs)
