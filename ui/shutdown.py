"""Явная остановка фоновой работы до уничтожения QApplication."""
import weakref

_callbacks = []
_waiters = []
_closing = False


def on_shutdown(callback, *, wait=None):
    try:
        reference = weakref.WeakMethod(callback)
    except TypeError:
        reference = lambda: callback
    _callbacks.append(reference)
    if wait is not None:_waiters.append(weakref.WeakMethod(wait))


def shutdown_background():
    global _closing
    if _closing:
        return
    _closing = True
    from core.logger import core_logger
    for reference in reversed(_callbacks):
        callback = reference()
        if callback is not None:
            try:
                callback()
            except Exception as error:
                core_logger.warn('GUI_SHUTDOWN', str(error))


def finish_background(milliseconds=5000):
    """Finish workers while QApplication and its native resources still exist."""
    shutdown_background()
    import time
    deadline=time.monotonic()+max(0,milliseconds)/1000
    complete=True
    from core.logger import core_logger
    for reference in reversed(_waiters):
        callback = reference()
        if callback is not None:
            try:
                remaining=max(0,int((deadline-time.monotonic())*1000))
                if callback(milliseconds=remaining) is False:complete=False
            except RuntimeError:pass
            except Exception as error:core_logger.warn('GUI_SHUTDOWN_WAIT', str(error))
    from PySide6.QtCore import QThreadPool, QCoreApplication, QEvent
    from ui.core_bridge.async_runner import wait_pools
    from ui.components.live_log import wait_log_workers
    if not wait_log_workers(max(0,int((deadline-time.monotonic())*1000))):complete=False
    if not wait_pools(max(0,int((deadline-time.monotonic())*1000))):complete=False
    if not QThreadPool.globalInstance().waitForDone(max(0,int((deadline-time.monotonic())*1000))):complete=False
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    if complete:QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return complete


def finish_interactive(app):
    """A stalled extension must not silently freeze exit or be killed during a write."""
    while not finish_background():
        from ui.widgets.dialogs import MessageBox
        from ui.i18n import tr
        dialog=MessageBox()
        dialog.setWindowTitle(tr('shutdown.wait_title'))
        dialog.setText(tr('shutdown.wait_text'))
        wait=dialog.addButton(tr('shutdown.wait'),MessageBox.AcceptRole)
        force=dialog.addButton(tr('shutdown.force'),MessageBox.DestructiveRole)
        dialog.setDefaultButton(wait);dialog.setEscapeButton(wait)
        dialog.exec()
        if dialog.clickedButton() is force:
            import os
            os._exit(1)  # Explicit user choice; do not destroy Qt below live workers.
