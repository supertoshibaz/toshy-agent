from pathlib import Path
from typing import Optional
from PySide6.QtGui import QSyntaxHighlighter, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit

from .markdown_highlighter import ObsidianLivePreviewHighlighter
from .pygments_highlighter import PygmentsHighlighter


def get_highlighter_for_file(
    file_path: str,
    doc: QTextDocument,
    editor: Optional[QPlainTextEdit] = None
) -> Optional[QSyntaxHighlighter]:
    """
    Фабрика подбора хайлайтера:
    - Для .md / .markdown использует ObsidianLivePreviewHighlighter.
    - Для ВСЕХ остальных языков (.py, .js, .json, .sh, .rs, .cpp и др.)
      автоматически запускает универсальный PygmentsHighlighter.
    """
    ext = Path(file_path).suffix.lower()

    if ext in {".md", ".markdown"}:
        if editor:
            return ObsidianLivePreviewHighlighter(doc, editor)
        return None

    # Для любых других файлов (включая .py) работает Pygments
    return PygmentsHighlighter(doc, file_path)


__all__ = [
    "get_highlighter_for_file",
    "ObsidianLivePreviewHighlighter",
    "PygmentsHighlighter"
]
