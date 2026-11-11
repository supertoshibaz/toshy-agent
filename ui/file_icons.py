"""File decorations shared with the Project Folder's QFileSystemModel."""
from pathlib import Path
from weakref import ref

from PySide6.QtCore import QObject, Signal, QDir, QSize, Qt, QFileInfo
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFileSystemModel, QStyle
from shiboken6 import isValid


class FileIcons(QObject):
    changed = Signal(str)

    def __init__(self, app):
        super().__init__(app)
        self.models = []
        self.fallback = None

    def register(self, model):
        if any(candidate() is model for candidate in self.models):return
        self.models.insert(0, ref(model))
        model.directoryLoaded.connect(self.changed)
        model.dataChanged.connect(lambda first, *_args, m=ref(model): self._changed(m, first))
        self.changed.emit('')

    def _changed(self, model, index):
        value = model()
        if value is not None and isValid(value):
            self.changed.emit(value.filePath(index.parent()))

    def model(self):
        self.models = [candidate for candidate in self.models if candidate() is not None and isValid(candidate())]
        if self.models:return self.models[0]()
        if self.fallback is None:
            self.fallback = QFileSystemModel(self)
            self.fallback.setFilter(QDir.AllDirs | QDir.Files | QDir.NoDotAndDotDot)
            self.fallback.setReadOnly(True)
            self.fallback.directoryLoaded.connect(self.changed)
            self.fallback.dataChanged.connect(lambda first, *_: self._changed(ref(self.fallback), first))
            self.fallback.setRootPath(QDir.rootPath())
        return self.fallback

    def icon(self, path):
        model = self.model()
        index = model.index(str(path))
        decoration = model.data(index, Qt.DecorationRole) if index.isValid() else None
        if isinstance(decoration, QIcon) and not decoration.isNull():return decoration
        # The model fills MIME decorations asynchronously. Until that arrives,
        # use this very model's provider, rather than constructing another one.
        return model.iconProvider().icon(QFileInfo(str(path)))


def file_icons():
    app = QApplication.instance()
    if not hasattr(app, '_toshy_file_icons'):app._toshy_file_icons = FileIcons(app)
    return app._toshy_file_icons


def register_file_model(model):
    file_icons().register(model)


class FileIconBinding(QObject):
    def __init__(self, label, path, size):
        super().__init__(label)
        self.label, self.path, self.size = label, str(path), size
        file_icons().changed.connect(self.refresh)
        self.refresh()

    def refresh(self, directory=''):
        if directory and Path(directory) != Path(self.path).parent:return
        icon = file_icons().icon(self.path)
        pixmap = icon.pixmap(QSize(self.size, self.size), self.label.devicePixelRatioF())
        if pixmap.isNull():
            pixmap = self.label.style().standardIcon(QStyle.SP_FileIcon).pixmap(self.size, self.size)
        self.label.setPixmap(pixmap)


def bind_file_icon(label, path, size=18):
    label._file_icon_binding = FileIconBinding(label, path, size)
