"""Canonical, language-selected pipeline documentation resource."""
from ui import builtin_docs

FILENAME = 'pipelienes_help.md'


def document_path():
    return builtin_docs.document_path(FILENAME)


def is_document(path):
    return builtin_docs.filename_for_path(path) == FILENAME
