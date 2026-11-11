"""Blocking work with QObject-owned dispatch and explicit shutdown guards."""
from threading import Event
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from shiboken6 import isValid
from ui.startup import begin_startup_activity, end_startup_activity

_pools=set()


def wait_pools(milliseconds):
    import time
    deadline=time.monotonic()+max(0,milliseconds)/1000
    complete=True
    for pool in list(_pools):
        if pool.waitForDone(max(0,int((deadline-time.monotonic())*1000))):_pools.discard(pool)
        else:complete=False
    return complete


class _TaskSignals(QObject):
    completed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn, startup_token=None):
        super().__init__()
        self.fn, self.startup_token = fn, startup_token
        self.cancel = Event()
        self.signals = _TaskSignals()

    def run(self):
        result, error = None, None
        try:
            if not self.cancel.is_set():result = self.fn()
        except Exception as caught:error = caught
        finally:
            try:self.signals.completed.emit((self, result, error))
            except RuntimeError:end_startup_activity(self.startup_token)


class AsyncRunner(QObject):
    def __init__(self, parent=None, max_threads=None):
        super().__init__(parent)
        # A widget must not destroy a live QThreadPool (its destructor waits
        # forever). Retain private pools through the bounded shutdown barrier.
        self._pool = QThreadPool.globalInstance() if max_threads is None else QThreadPool()
        if max_threads is not None:
            self._pool.setMaxThreadCount(max_threads);_pools.add(self._pool)
        self._tasks, self._callbacks = set(), {}
        self._closed = False
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown, wait=self.wait_for_done)
        self.destroyed.connect(self._owner_destroyed)

    def submit(self, fn, on_success=None, on_error=None):
        if self._closed:return None
        owner = self.parent()
        name = str(owner.objectName() or owner.__class__.__name__) if owner is not None else 'AsyncRunner'
        task = _Task(fn, begin_startup_activity('async:'+name))
        self._tasks.add(task)
        self._callbacks[task] = (on_success, on_error)
        task.signals.completed.connect(self._task_finished)
        self._pool.start(task)
        return task

    @Slot(object)
    def _task_finished(self, outcome):
        task, result, error = outcome
        self._tasks.discard(task)
        success, failed = self._callbacks.pop(task, (None, None))
        end_startup_activity(task.startup_token)
        owner = self.parent()
        if self._closed or task.cancel.is_set() or (owner is not None and not isValid(owner)):return
        callback = failed if error is not None else success
        if callback is not None:callback(error if error is not None else result)

    def _owner_destroyed(self, *_):self.shutdown()

    def shutdown(self):
        self._closed = True
        for task in self._tasks:task.cancel.set()
        self._callbacks.clear()
        if self._pool.activeThreadCount()==0:_pools.discard(self._pool)

    def wait_for_done(self,milliseconds=5000):
        if not self._pool.waitForDone(max(0,int(milliseconds))):return False
        for task in self._tasks:end_startup_activity(task.startup_token)
        self._tasks.clear()
        _pools.discard(self._pool)
        return True
