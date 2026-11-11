"""Ядро переживает GUI без скрытого Qt-процесса и унаследованного терминала."""
import os
import subprocess
import uuid
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QTimer, QProcess, QProcessEnvironment, QByteArray
from core.paths import APP_PATHS


class DetachedCoreProcess(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    stateChanged = Signal(object)
    errorOccurred = Signal(object)
    finished = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._environment = QProcessEnvironment.systemEnvironment()
        self._directory = str(APP_PATHS.root)
        self._process = None
        self._error = ''
        self._reported = True
        self._offset = 0
        self.output_path = None
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll)

    def setWorkingDirectory(self, directory):
        self._directory = str(directory)

    def setProcessEnvironment(self, environment):
        self._environment = QProcessEnvironment(environment)

    def processEnvironment(self):
        return QProcessEnvironment(self._environment)

    def state(self):
        return QProcess.Running if self._process is not None and self._process.poll() is None else QProcess.NotRunning

    def processId(self):
        return self._process.pid if self._process is not None else 0

    def errorString(self):
        return self._error

    def start(self, program, arguments):
        if self.state() != QProcess.NotRunning:
            return
        try:
            directory = APP_PATHS.runtime / 'core-output'
            directory.mkdir(parents=True, exist_ok=True)
            self.output_path = directory / (uuid.uuid4().hex + '.log')
            self._offset = 0
            options = {'start_new_session':True} if os.name != 'nt' else {
                'creationflags':subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            environment = {key:self._environment.value(key) for key in self._environment.keys()}
            with self.output_path.open('wb') as output:
                self._process = subprocess.Popen([str(program),*arguments], cwd=self._directory,
                    env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    close_fds=True, **options)
            self._reported = False
            self.stateChanged.emit(QProcess.Running)
            self.timer.start()
        except OSError as error:
            self._error = str(error)
            self._process = None
            self.errorOccurred.emit(QProcess.FailedToStart)

    def readAllStandardOutput(self):
        if self.output_path is None:
            return QByteArray()
        try:
            with self.output_path.open('rb') as output:
                output.seek(self._offset)
                data = output.read(256*1024)
                self._offset = output.tell()
            return QByteArray(data)
        except OSError:
            return QByteArray()

    def readAllStandardError(self):
        return QByteArray()

    def _poll(self):
        if self._process is None:
            return
        if self.output_path and self.output_path.exists() and self.output_path.stat().st_size > self._offset:
            self.readyReadStandardOutput.emit()
        code = self._process.poll()
        if code is not None and not self._reported:
            self._reported = True
            self.timer.stop()
            self.stateChanged.emit(QProcess.NotRunning)
            self.finished.emit(code, QProcess.NormalExit if code == 0 else QProcess.CrashExit)

    def waitForFinished(self, milliseconds):
        if self._process is None:
            return True
        try:
            self._process.wait(timeout=max(0,milliseconds)/1000)
        except subprocess.TimeoutExpired:
            return False
        self._poll()
        return True

    def terminate(self):
        if self.state() != QProcess.NotRunning:
            self._process.terminate()

    def kill(self):
        if self.state() != QProcess.NotRunning:
            self._process.kill()

    def detach(self):
        self.timer.stop()
