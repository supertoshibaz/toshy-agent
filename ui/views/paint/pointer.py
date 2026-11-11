"""Keep native pointer samples during drawing without flooding repaints."""
from PySide6.QtCore import QCoreApplication, Qt


class PointerSamples:
    _owners = set()
    _previous = False

    def __init__(self, owner):
        self.token = object()
        owner.destroyed.connect(self.release)

    def acquire(self):
        if self.token in self._owners:
            return
        app = QCoreApplication.instance()
        if app is None:
            return
        if not self._owners:
            PointerSamples._previous = app.testAttribute(Qt.AA_CompressHighFrequencyEvents)
            app.setAttribute(Qt.AA_CompressHighFrequencyEvents, False)
        self._owners.add(self.token)

    def release(self, *_):
        if self.token not in self._owners:
            return
        self._owners.remove(self.token)
        app = QCoreApplication.instance()
        if not self._owners and app is not None:
            app.setAttribute(Qt.AA_CompressHighFrequencyEvents, self._previous)
