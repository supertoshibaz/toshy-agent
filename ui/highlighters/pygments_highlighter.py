from ui.themes.manager import theme_manager
"""Подсветка целого документа без потери многострочного состояния лексера."""
from concurrent.futures import ThreadPoolExecutor
from weakref import ref
from threading import Event
from PySide6.QtCore import QTimer, Signal, Qt
from PySide6.QtGui import QSyntaxHighlighter

try:
    from pygments.lexers import get_lexer_for_filename, TextLexer
    from .syntax_theme import get_format_for_token
    PYGMENTS_AVAILABLE = True
except ImportError:
    PYGMENTS_AVAILABLE = False

_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='toshy-syntax')
_STOP = Event()


def shutdown_highlighting():
    _STOP.set()
    _POOL.shutdown(wait=False, cancel_futures=True)


from ui.shutdown import on_shutdown
on_shutdown(shutdown_highlighting)


def _tokenize(path, source):
    try:
        lexer = get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except Exception:
        lexer = TextLexer(stripnl=False, ensurenl=False)
    rows, row, column = {}, 0, 0
    # Один проход лексера сохраняет состояние строк, комментариев и вложенных языков.
    for _, token, value in lexer.get_tokens_unprocessed(source):
        if _STOP.is_set():
            return {}
        pieces = value.split('\n')
        for index, piece in enumerate(pieces):
            length = len(piece.encode('utf-16-le')) // 2
            if length:
                rows.setdefault(row, []).append((column, length, token))
                column += length
            if index < len(pieces) - 1:
                row, column = row + 1, 0
    return rows


class PygmentsHighlighter(QSyntaxHighlighter):
    _ready = Signal(int, object)

    def __init__(self, parent_doc, file_path):
        super().__init__(parent_doc)
        self.file_path = file_path
        self._generation = 0
        self._rows = {}
        self._busy = False
        self._formats = {}
        theme_manager.theme_changed.connect(self._theme_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._start)
        self._ready.connect(self._apply, Qt.QueuedConnection)
        parent_doc.contentsChange.connect(self._changed)
        self._timer.start()

    def _theme_changed(self, *_):
        self._formats.clear()
        self.rehighlight()

    def _changed(self, position, removed, added):
        if not removed and not added:
            return
        self._generation += 1
        self._rows = {}
        self._timer.start()

    def _start(self):
        if _STOP.is_set() or self._busy or not PYGMENTS_AVAILABLE or self.document() is None:
            return
        self._busy = True
        generation = self._generation
        future = _POOL.submit(_tokenize, self.file_path, self.document().toPlainText())
        weak = ref(self)
        def complete(result):
            target = weak()
            if target is not None:
                try:
                    rows = result.result()
                except Exception:
                    rows = {}
                try:
                    target._ready.emit(generation, rows)
                except RuntimeError:
                    pass  # Документ мог закрыться до завершения разбора.
        future.add_done_callback(complete)

    def _apply(self, generation, rows):
        self._busy = False
        if _STOP.is_set():
            return
        if generation != self._generation:
            self._timer.start()
            return
        self._rows = rows
        self.rehighlight()

    def highlightBlock(self, text):
        for start, length, token in self._rows.get(self.currentBlock().blockNumber(), ()):
            if token not in self._formats:
                self._formats[token] = get_format_for_token(token)
            fmt = self._formats[token]
            if fmt:
                self.setFormat(start, length, fmt)
