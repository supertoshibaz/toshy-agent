"""Bounded document readers owned by the existing file-manager module."""

DOCUMENT_SUFFIXES = frozenset({
    '.pdf', '.docx', '.odt', '.xlsx', '.xlsm', '.ods', '.csv', '.tsv',
    '.pptx', '.odp', '.epub', '.html', '.htm', '.rtf',
    '.doc', '.xls', '.ppt', '.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff', '.bmp',
})
SPREADSHEET_SUFFIXES = frozenset({'.xlsx', '.xlsm', '.ods', '.csv', '.tsv', '.xls'})
MAX_FILE = 128 * 1024 * 1024
MAX_TEXT = 4_000_000
MAX_CELLS = 200_000
MAX_UNITS = 20_000
MAX_PAGES = 2_000
VERSION = 2


class DocumentError(ValueError):
    pass
