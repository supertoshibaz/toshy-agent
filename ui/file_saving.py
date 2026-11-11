"""Atomic text writes, enclosed by the same history lease as native saves."""
from contextvars import copy_context
from threading import Thread
from PySide6.QtCore import QSaveFile,QIODevice,QObject,Signal,QEventLoop,QCoreApplication,QThread
from core.file_history import record_file_save as _record_file_save, HistoryBusy
from core.text_files import TextFormat

_history_busy=False


class _Completion(QObject):
    finished=Signal()


def history_call(function,*args):
    """Keep synchronous save contracts while history IO leaves the GUI thread."""
    app=QCoreApplication.instance()
    if app is None or QThread.currentThread()!=app.thread():return function(*args)
    global _history_busy
    if _history_busy:raise HistoryBusy('Another file-history transaction is in progress.')
    _history_busy=True
    loop=QEventLoop();completion=_Completion();completion.finished.connect(loop.quit)
    outcome=[];context=copy_context()
    def work():
        try:outcome.append((context.run(function,*args),None))
        except BaseException as error:outcome.append((None,error))
        finally:completion.finished.emit()
    worker=Thread(target=work,name='toshy-file-history')
    try:
        worker.start()
        if not outcome:loop.exec(QEventLoop.ExcludeUserInputEvents)
        # A global quit may exit nested loops before the transaction completes.
        # Never abandon a storage transaction during teardown.
        worker.join()
        value,error=outcome[0]
        if error is not None:raise error
        return value
    finally:_history_busy=False


def record_file_save(path_getter,label=None):
    return _record_file_save(path_getter,label,run_history=history_call)


@record_file_save(lambda path,text,**options:path)
def write_text(path,text,*,text_format=None):
    payload=(text_format or TextFormat()).encode(text)
    output=QSaveFile(str(path))
    if not output.open(QIODevice.WriteOnly):raise OSError(output.errorString())
    try:
        if output.write(payload)!=len(payload) or not output.commit():raise OSError(output.errorString())
    except BaseException:
        output.cancelWriting();raise
