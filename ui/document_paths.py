"""Display filesystem paths without tying breadcrumbs to the launch directory."""
from pathlib import Path
from core.paths import APP_PATHS
from PySide6.QtCore import Qt
from ui.i18n import bind_caption


def document_path(path):
    absolute = Path(path).expanduser().resolve()
    try:
        return absolute.relative_to(APP_PATHS.root.resolve()).as_posix()
    except ValueError:
        return absolute.as_posix()


def show_document_path(label, path, prefix=''):
    label.setTextFormat(Qt.PlainText)
    bind_caption(label, prefix + document_path(path))
    bind_caption(label, str(Path(path).expanduser().resolve()), setter='setToolTip')
