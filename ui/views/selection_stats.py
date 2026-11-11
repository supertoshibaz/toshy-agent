"""Debounced selection statistics for text files; counting stays off the GUI thread."""
from ui.i18n import bind_caption, tr
import re
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel
from ui.core_bridge.async_runner import AsyncRunner


def count_selection(text):
    text = str(text).replace('\u2029', '\n').replace('\u2028', '\n')
    # Unicode letters/numbers, with internal apostrophes/hyphens treated as one
    # word. Count source text, without interpreting Markdown or estimating tokens.
    words = sum(1 for _ in re.finditer(r"[^\W_]+(?:[’'\-][^\W_]+)*", text, re.UNICODE))
    return words, len(text), sum(not character.isspace() for character in text)


class SelectionStats(QFrame):
    def __init__(self, editor, get_path, parent=None):
        super().__init__(parent)
        self.setObjectName('FileSelectionStatistics')
        self.editor, self.get_path = editor, get_path
        self.runner = AsyncRunner(self, max_threads=1)
        self.generation = 0
        self.busy = False
        self.pending = None
        self.label = QLabel(self)
        self.label.setObjectName('StartupSettingDescription')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 3, 16, 3)
        layout.addWidget(self.label)
        self.setFixedHeight(26)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._request)
        editor.selectionChanged.connect(self.schedule)
        editor.textChanged.connect(self.schedule)
        self.hide()

    def schedule(self):
        self.generation += 1
        enabled = Path(self.get_path()).suffix.lower() in {'.md', '.markdown', '.txt'}
        if not enabled or not self.editor.textCursor().hasSelection():
            self.timer.stop()
            self.pending = None
            self.hide()
            return
        self.timer.start()

    def _request(self):
        if not self.editor.textCursor().hasSelection():
            self.hide()
            return
        self.pending = self.generation, self.editor.textCursor().selectedText()
        self._start_pending()

    def _start_pending(self):
        if self.busy or self.pending is None:
            return
        generation, text = self.pending
        self.pending = None
        self.busy = True
        self.runner.submit(lambda: (generation, count_selection(text)), self._received, self._failed)

    def _received(self, result):
        self.busy = False
        generation, (words, chars, compact) = result
        if generation == self.generation:
            bind_caption(self.label, tr('ui.caption.d1a0ff81ba4f', v0=words, v1=chars, v2=compact).replace(',', ' '))
            self.show()
        self._start_pending()

    def _failed(self, error):
        self.busy = False
        self.hide()
        self._start_pending()
