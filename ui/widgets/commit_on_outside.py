"""Завершение переименования с безопасным временем жизни глобального фильтра."""
import weakref

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QStyledItemDelegate, QAbstractItemDelegate
from shiboken6 import isValid


class _OutsideCommitFilter(QObject):
    """Один фильтр принадлежит приложению, а не уничтожаемому полю ввода."""
    def __init__(self, application):
        super().__init__(application)
        self._application = weakref.ref(application)
        self._states = []
        self._installed = False

    def add(self, state):
        self._states.append(state)
        application = self._application()
        if not self._installed and application is not None and isValid(application):
            application.installEventFilter(self)
            self._installed = True

    def remove(self, state):
        if state in self._states:
            self._states.remove(state)
        if self._states or not self._installed:
            return
        self._installed = False
        application = self._application()
        if application is not None and isValid(application) and isValid(self):
            application.removeEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Hide):
            return False
        for state in tuple(reversed(self._states)):
            if not state._active:
                continue
            editor = state._editor()
            # destroyed может ещё не дойти до нашего слота: другие обработчики
            # уничтожения способны вложенно послать событие всему приложению.
            if editor is None or not isValid(editor):
                state.detach()
                continue
            if kind == QEvent.Hide:
                if watched is editor:
                    state.detach()
                continue
            if not editor.isVisible():
                continue
            if kind == QEvent.MouseButtonPress:
                position = editor.mapFromGlobal(event.globalPosition().toPoint())
                if not editor.rect().contains(position):
                    state.finish()
                    # Обработчик сохранения мог удалить и получателя события.
                    if not isValid(watched):
                        return True
            elif watched is editor and event.key() == Qt.Key_Escape:
                state.finish(cancel=True)
                return True
        return False


def _application_filter():
    application = QApplication.instance()
    manager = getattr(application, '_toshy_outside_commit_filter', None)
    if manager is None or not isValid(manager):
        manager = _OutsideCommitFilter(application)
        application._toshy_outside_commit_filter = manager
    return manager


class OutsideCommit:
    """Короткоживущее состояние редактора без дочернего QObject-фильтра."""
    def __init__(self, editor, commit, cancel=None):
        self._editor = weakref.ref(editor)
        self._commit, self._cancel = commit, cancel
        self._active = True
        self._manager = _application_filter()
        editor.destroyed.connect(self._destroyed, Qt.DirectConnection)
        editor.editingFinished.connect(self.detach)
        self._manager.add(self)

    def _destroyed(self, *args):
        self.detach()

    def detach(self):
        if not self._active:
            return
        self._active = False
        # Сначала прекращаем глобальную обработку. Здесь нельзя обращаться
        # к C++-методам editor до проверки isValid().
        self._manager.remove(self)
        editor = self._editor()
        if editor is not None and isValid(editor):
            try:
                editor.destroyed.disconnect(self._destroyed)
                editor.editingFinished.disconnect(self.detach)
            except (RuntimeError, TypeError):
                pass
        self._commit = self._cancel = None

    def finish(self, cancel=False):
        if not self._active:
            return
        editor = self._editor()
        callback = self._commit
        if cancel:
            callback = self._cancel
            if callback is None and editor is not None and isValid(editor):
                callback = editor.hide
        # commitData/closeEditor и пользовательский callback могут синхронно
        # удалить поле или запустить вложенный цикл событий.
        self.detach()
        if callback is not None and editor is not None and isValid(editor):
            callback()


class OutsideCommitDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            def commit():
                if not isValid(self) or not isValid(editor):
                    return
                self.commitData.emit(editor)
                if isValid(self) and isValid(editor):
                    self.closeEditor.emit(editor)

            def cancel():
                if isValid(self) and isValid(editor):
                    self.closeEditor.emit(editor, QAbstractItemDelegate.RevertModelCache)

            editor._outside = OutsideCommit(editor, commit, cancel)
        return editor

    def destroyEditor(self, editor, index):
        state = getattr(editor, '_outside', None)
        if state is not None:
            state.detach()
        if isValid(editor):
            super().destroyEditor(editor, index)
