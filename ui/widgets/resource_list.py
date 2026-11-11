"""Flat resource trees sharing the file manager's item appearance."""
from ui.icons import get as get_icon
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView, QHeaderView, QStyle


class ResourceItem(QTreeWidgetItem):
    def __init__(self, text, parent=None):
        if parent is None:
            super().__init__([text])
        else:
            super().__init__(parent, [text])

    def data(self, *args):
        return super().data(0, args[0]) if len(args) == 1 else super().data(*args)

    def setData(self, *args):
        if len(args) == 2:
            super().setData(0, *args)
        else:
            super().setData(*args)

    def text(self, column=0):
        return super().text(column)


class ResourceList(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('FileManagerTreeView')
        self.setColumnCount(1)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(14)
        self.setAnimated(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.setContextMenuPolicy(Qt.CustomContextMenu)

    def addItem(self, item):
        if isinstance(item, str):
            item = ResourceItem(item)
        if item.icon(0).isNull():
            item.setIcon(0, get_icon('file'))
        self.addTopLevelItem(item)
        return item

    def count(self):
        return self.topLevelItemCount()

    def item(self, index):
        return self.topLevelItem(index)

    def currentRow(self):
        return self.indexOfTopLevelItem(self.currentItem())

    def setCurrentRow(self, index):
        self.setCurrentItem(self.topLevelItem(index))
