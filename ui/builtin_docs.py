"""Language selection shared by all packaged Markdown documentation."""
from pathlib import Path

from ui.i18n import i18n

DOCS_DIR = Path(__file__).resolve().parent / 'docs'


def _filename(value):
    if (not isinstance(value, str) or not value or value in {'.', '..'}
            or any(char in value for char in ('/', '\\', ':', '\x00'))
            or Path(value).suffix.lower() != '.md'):
        raise ValueError('Built-in documentation requires a Markdown filename without a directory.')
    return value


def document_path(filename, language=None):
    filename = _filename(filename)
    language = i18n.current_lang if language is None else language
    root = DOCS_DIR.resolve()
    original = root / filename
    path = root / 'ru' / filename if language == 'ru' else original
    if not path.is_file():
        path = original
    if not path.resolve().is_relative_to(root):
        raise ValueError('Built-in documentation must stay inside its resource directory.')
    return path


def filename_for_path(path):
    """Recognize originals/translations without capturing user files of the same name."""
    try:
        relative = Path(path).resolve().relative_to(DOCS_DIR.resolve())
        if len(relative.parts) == 1 or len(relative.parts) == 2 and relative.parts[0] == 'ru':
            return _filename(relative.name)
    except (ValueError, OSError):
        pass
    return None
