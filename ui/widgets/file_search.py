"""Поиск имён файлов вне GUI-потока с отменой устаревшего обхода."""
from ui.i18n import bind_caption, tr, set_text

import os
from pathlib import Path
from threading import Event
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem
from shiboken6 import isValid
from ui.core_bridge.async_runner import AsyncRunner


def scan_names(root, query='', extensions=None, cancel=None, limit=5000):
    root = Path(root)
    found = []
    errors = []
    query = query.casefold()
    def failed(error):
        if len(errors) < 5:
            errors.append(str(error))
    for directory, _, files in os.walk(root, followlinks=False, onerror=failed):
        if cancel and cancel.is_set():
            return [], False, []
        for name in sorted(files, key=str.casefold):
            if cancel and cancel.is_set():
                return [], False, []
            if query in name.casefold() and (extensions is None or Path(name).suffix.lower() in extensions):
                if len(found) >= limit:
                    return found, True, errors
                found.append(str(Path(directory) / name))
    return sorted(found, key=str.casefold), False, errors


class FileSearchResults(QWidget):
    opened = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = AsyncRunner(self)
        self.cancel = Event()
        self.revision = 0
        self._key = None
        self._paths = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.list = QTreeWidget(self)
        self.list.setHeaderHidden(True)
        self.list.setRootIsDecorated(False)
        self.list.itemDoubleClicked.connect(lambda item, _: self.opened.emit(item.data(0, Qt.UserRole)))
        layout.addWidget(self.list)
        self.destroyed.connect(lambda: self.cancel.set())
        from ui.shutdown import on_shutdown
        on_shutdown(self.stop)

    def stop(self):
        self.cancel.set()
        self.revision += 1

    def search(self, root, query='', extensions=None):
        self.stop()
        revision = self.revision
        cancel = self.cancel = Event()
        key = (str(root), query, frozenset(extensions) if extensions is not None else None)
        if key != self._key:
            set_text(self.status, 'ui.file_manager_widget.search_cf30')
            self.list.clear()
            self._paths = None
        self._key = key
        def ready(result):
            if not isValid(self) or revision != self.revision:
                return
            paths, truncated, errors = result
            if paths == self._paths:
                return
            self._paths = paths
            selected = {item.data(0, Qt.UserRole) for item in self.list.selectedItems()}
            self.list.clear()
            for path in paths:
                item = QTreeWidgetItem([str(Path(path).relative_to(root))])
                item.setData(0, Qt.UserRole, path)
                bind_caption(item, path, setter='setToolTip', index=0)
                self.list.addTopLevelItem(item)
                item.setSelected(path in selected)
            bind_caption(self.status, (tr('ui.caption.1fb8adaf81d1') if truncated else tr('ui.caption.78fd4aa5d16c', v0=len(paths)))
                + (tr('ui.caption.af0df7c9e44c') if errors else ''))
        def failed(error):
            if isValid(self) and revision == self.revision:
                bind_caption(self.status, str(error))
        self.runner.submit(lambda: scan_names(root, query, extensions, cancel), ready, failed)
