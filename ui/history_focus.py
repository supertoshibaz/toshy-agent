"""Фокус редактора задаёт границы пользовательской серии правок."""
from core.file_history import focus_user_file


def focus_document(document):
    path = getattr(document, 'file_path', '') or getattr(document, 'source', '')
    focus_user_file(path or ('untitled-' + str(id(document)) if document is not None else ''))
