"""Независимая заставка. Этот модуль можно импортировать до загрузки Qt."""
import json
import subprocess
import sys
import threading
from pathlib import Path

_display = None


class StartupDisplay:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_phase = None
        executable = Path(sys.executable)
        # pythonw не предоставляет sys.stdin; консольный Python запускаем без окна.
        if sys.platform == 'win32' and executable.name.lower() == 'pythonw.exe':
            console_python = executable.with_name('python.exe')
            if console_python.is_file():
                executable = console_python
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
        self.process = subprocess.Popen(
            [str(executable), '-m', 'ui.startup_splash'],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdin=subprocess.PIPE, text=True, encoding='utf-8', bufsize=1,
            **options)

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def phase(self, label):
        with self._lock:
            if not self.is_running() or label == self._last_phase:
                return
            try:
                self.process.stdin.write(json.dumps({'phase': str(label)}, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                self._last_phase = label
            except (OSError, ValueError):
                pass

    def close(self):
        with self._lock:
            process, self.process = self.process, None
            if process is None:
                return
            try:
                process.stdin.close()
            except OSError:
                pass
        # Ожидание отдельного GUI не задерживает показ главного окна.
        def reap():
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        threading.Thread(target=reap, name='toshy-splash-exit', daemon=True).start()


def start_startup_display():
    global _display
    if _display is None:
        try:
            _display = StartupDisplay()
        except OSError:
            return None
    return _display


def report_startup_phase(label):
    if _display is not None:
        _display.phase(label)


def stop_startup_display():
    global _display
    display, _display = _display, None
    if display is not None:
        display.close()
