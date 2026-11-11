import sys
from datetime import datetime
from pathlib import Path
import httpx
from ui.core_bridge.transport import core_http_client
from PySide6.QtCore import QObject, Signal, QProcess, QTimer, QProcessEnvironment

from core.paths import APP_PATHS
from core.log_session import session_log_path
from core.security import TOKEN_ENV, auth_headers, load_or_create_core_token
from ui.core_bridge.async_runner import AsyncRunner


class CoreProcessManager(QObject):
    """
    Менеджер фонового процесса ядра (app.py):
    - Остановка через API /v1/shutdown (работает и для терминала, и для GUI).
    - Адаптивный опрос: 5 секунд когда сервер Онлайн, 2 секунды когда Офлайн.
    - Мгновенная проверка доступности при запуске UI.
    """
    status_changed = Signal(bool, str)
    failure_changed = Signal(bool)
    log_received = Signal(str)
    failure_details_changed = Signal(str)

    def __init__(self, base_url: str = "http://127.0.0.1:5000", parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.root_dir = APP_PATHS.code_root
        self.core_token = load_or_create_core_token()

        from ui.core_bridge.detached_process import DetachedCoreProcess
        self.process = DetachedCoreProcess(self)
        self.process.setWorkingDirectory(str(self.root_dir))
        process_env = QProcessEnvironment.systemEnvironment()
        process_env.insert(TOKEN_ENV, self.core_token)
        self.process.setProcessEnvironment(process_env)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.stateChanged.connect(self._on_process_state_changed)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.finished.connect(self._on_process_finished)

        self._is_online = False
        self._has_failed = False
        self._is_starting = False
        self._owns_process = False
        self.ping_timer = None
        self.async_runner = AsyncRunner(self)
        self._health_check_in_flight = False
        self._health_generation = 0
        self._health_failures = 0
        self._stop_requested = False
        self._diagnostic_tail = ''
        self.failure_details = ''
        self.startup_log_path = session_log_path()
        self._closing = False
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown_gui)

    def _write_startup_log(self, text, *, reset=False):
        try:
            from core.logger import core_logger
            core_logger.event('app', 'CORE_OUTPUT', str(text).rstrip(), details={'pid': self.process.processId()})
        except (OSError, RuntimeError):
            pass

    def _set_failure_details(self, text):
        if text != self.failure_details:
            self.failure_details = text
            self.failure_details_changed.emit(text)

    def _report_failure(self, message):
        self._write_startup_log("\n" + message + "\n")
        self.log_received.emit(message + "\n")
        self._is_starting = False
        self._is_online = False
        self._invalidate_health_checks()
        details = str(message)
        if self._diagnostic_tail.strip():
            details += '\n\nВывод процесса ядра:\n' + self._diagnostic_tail.strip()
        self._set_failure_details(details)
        self._set_failed(True)
        self.status_changed.emit(False, "core_status_offline")
        self._update_poll_interval(is_online=False)
        try:
            from core.logger import core_logger

            core_logger.event(
                "app",
                "CORE_START_ERROR",
                message,
                level="ERROR",
                details={
                    "startup_log": str(self.startup_log_path),
                    "output": self._diagnostic_tail,
                },
            )
        except Exception:
            pass

    def _on_process_error(self, error):
        if error == QProcess.FailedToStart:
            self._report_failure('Не удалось запустить Python ядра: ' + self.process.errorString())

    def _on_process_finished(self, exit_code, exit_status):
        self._on_stdout()
        self._on_stderr()
        message = f"Процесс ядра завершён: код {exit_code}, статус {exit_status.name}."
        self._invalidate_health_checks()
        failed = not self._stop_requested and (
            exit_code != 0 or exit_status == QProcess.CrashExit or self._is_starting
        )
        if failed:
            self._report_failure(message + " Диагностика: " + str(self.startup_log_path))
        else:
            self._write_startup_log("\n" + message + "\n")
            self._is_online = False
            self._is_starting = False
            self.status_changed.emit(False, "core_status_offline")
            self._update_poll_interval(is_online=False)
        if not self._stop_requested and not self._closing:
            self.check_server_health()

    def start_monitoring(self):
        """Запуск мониторинга доступности ядра."""
        self.check_server_health()

        if self.ping_timer is None:
            self.ping_timer = QTimer(self)
            self.ping_timer.setInterval(2000)
            self.ping_timer.timeout.connect(self.check_server_health)
            self.ping_timer.start()

    def stop_monitoring(self):
        if self.ping_timer and self.ping_timer.isActive():
            self.ping_timer.stop()

    def start_core(self):
        """Запуск подпроцесса ядра из GUI."""
        if self.is_running() or self._is_starting or self.process.state() != QProcess.NotRunning:
            return

        self._invalidate_health_checks()
        self._set_failed(False)
        self._set_failure_details('')
        self.core_token = load_or_create_core_token()
        env = self.process.processEnvironment()
        env.insert(TOKEN_ENV, self.core_token)
        from core.log_session import LOG_ENV, session_log_path
        env.insert(LOG_ENV, str(session_log_path()))
        from core.plugins.runtime import get_runtime, SNAPSHOT_ENV
        plugins = get_runtime()
        if plugins is not None and plugins.domain == 'ui' and plugins.snapshot_path is not None:
            env.insert(SNAPSHOT_ENV, str(plugins.snapshot_path))
        env.insert('PYTHONUNBUFFERED', '1')
        env.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(env)
        self._stop_requested = False
        self._diagnostic_tail = ''
        self.startup_log_path = session_log_path()
        entrypoint = self.root_dir / 'app.py'
        self._write_startup_log(f'Started: {datetime.now().isoformat()}\nPython: {sys.executable}\n'
            f'Entrypoint: {entrypoint}\nWorking directory: {self.root_dir}\n\n', reset=True)
        self.process.setWorkingDirectory(str(self.root_dir))
        self._is_starting = True
        self._owns_process = True
        self._is_online = False
        self.log_received.emit(f"[*] Запуск ядра агента: {sys.executable} app.py\n")
        self.status_changed.emit(False, "core_status_starting")
        self.process.start(sys.executable, ['-u', '-X', 'faulthandler', str(entrypoint)])

    def stop_core(self):
        """
        Остановка ядра агента:
        1. Отправляет запрос на POST /v1/shutdown (гасит сервер в терминале).
        2. Если процесс был запущен через QProcess — дополнительно контролирует завершение.
        """
        self._invalidate_health_checks()
        self._stop_requested = True
        self.log_received.emit("[*] Остановка ядра агента...\n")

        # 1. Отправляем запрос на мягкое выключение через API
        owned = self.owns_running_core()
        shutdown_error = None
        if self._is_online:
            try:
                with core_http_client(self.base_url, timeout=1.5) as client:
                    response = client.post(f"{self.base_url}/v1/shutdown", headers=auth_headers(self.core_token))
                    response.raise_for_status()
            except Exception as error:
                shutdown_error = error

        # 2. Если процесс дочерний для GUI — ждем или добиваем
        if self.process.state() != QProcess.NotRunning:
            if not self.process.waitForFinished(1500):
                self.process.terminate()
                if not self.process.waitForFinished(1000):
                    self.process.kill()
                    if not self.process.waitForFinished(1500):
                        raise RuntimeError("Ядро не завершилось после принудительной остановки.")

        if shutdown_error is not None and not owned:
            raise RuntimeError("Ядро не подтвердило запрос завершения: " + str(shutdown_error))
        self._invalidate_health_checks()
        self._is_online = False
        self._is_starting = False
        self._owns_process = False
        self.status_changed.emit(False, "core_status_offline")
        self._update_poll_interval(is_online=False)

    def is_running(self) -> bool:
        return self._is_online

    def is_starting(self) -> bool:
        return self._is_starting

    def has_failed(self) -> bool:
        return self._has_failed

    def _set_failed(self, failed):
        if failed != self._has_failed:
            self._has_failed = failed
            self.failure_changed.emit(failed)

    def owns_running_core(self) -> bool:
        return self._owns_process and self.process.state() != QProcess.NotRunning

    def keep_core_after_ui_close(self):
        """Отсоединённое ядро не удерживает окно и процесс GUI."""
        self.process.detach()
        return self.owns_running_core()

    def shutdown_gui(self):
        self._closing = True
        self.stop_monitoring()
        self._invalidate_health_checks()
        self.process.detach()

    def _on_stdout(self):
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._process_output(data)

    def _on_stderr(self):
        data = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
        self._process_output(data)

    def _process_output(self, data):
        if data:
            self._diagnostic_tail = (self._diagnostic_tail + data)[-16000:]
            self._write_startup_log(data)
            self.log_received.emit(data)

    def _on_process_state_changed(self, state):
        if state == QProcess.NotRunning:
            self._invalidate_health_checks()
            self._owns_process = False

    def _update_poll_interval(self, is_online: bool):
        """Адаптивный интервал: 5 сек когда Онлайн, 2 сек когда Офлайн."""
        if self.ping_timer:
            interval = 5000 if is_online else 2000
            if self.ping_timer.interval() != interval:
                self.ping_timer.setInterval(interval)

    def check_server_health(self):
        """Проверка доступности API."""
        if self._closing or self._health_check_in_flight:
            return
        self._health_check_in_flight = True
        generation = self._health_generation
        self.async_runner.submit(
            self._probe_server,
            lambda ok: self._apply_health_result(ok, generation),
            lambda error: self._apply_health_result(None, generation),
        )

    def _invalidate_health_checks(self):
        self._health_generation += 1
        self._health_check_in_flight = False
        self._health_failures = 0

    def _probe_server(self) -> bool | None:
        try:
            with core_http_client(self.base_url, timeout=1.5) as client:
                res = client.get(f"{self.base_url}/v1/models", headers=auth_headers(load_or_create_core_token()))
                if res.status_code >= 500 or res.status_code in {408, 429}:
                    return None
                return res.status_code == 200 and res.json().get("service") == "toshy-core"
        except (httpx.TimeoutException, httpx.RemoteProtocolError):
            # A busy API or a lost health response does not prove that its
            # independently streaming generations have stopped.
            return None
        except httpx.ConnectError:
            return False
        except Exception:
            return None

    def _health_check_failed(self, _error):
        self._apply_health_result(None)

    def _apply_health_result(self, is_ok: bool | None, generation=None):
        if generation is not None and generation != self._health_generation:
            return
        self._health_check_in_flight = False

        if is_ok is None:
            self._health_failures = 0
            return
        if not is_ok and self._is_online:
            self._health_failures += 1
            if self._health_failures < 2:
                current = self._health_generation
                QTimer.singleShot(500, self, lambda: self.check_server_health()
                                  if current == self._health_generation else None)
                return
        else:
            self._health_failures = 0

        if is_ok:
            self._is_starting = False
            self._set_failed(False)
            self._set_failure_details('')
        elif self._is_online and not self._stop_requested and not self._has_failed:
            self._set_failed(True)
            self._set_failure_details('Соединение с ядром потеряно. Диагностика ядра доступна в журналах запуска и runtime/core-output; для ядра, запущенного вручную, также проверьте его терминал.')

        if is_ok != self._is_online:
            self._is_online = is_ok
            status_key = "core_status_online" if is_ok else ("core_status_starting" if self._is_starting else "core_status_offline")
            self.status_changed.emit(is_ok, status_key)
            self._update_poll_interval(is_ok)
            if is_ok:
                self.async_runner.submit(self._attach_session_log, lambda _: None)

    def _attach_session_log(self):
        # An already-running core may have been started outside this UI.
        # Authenticated local API lets it join this application's log as well.
        try:
            with core_http_client(self.base_url, timeout=1.0) as client:
                client.post(self.base_url + '/v1/log-session',
                            json={'path': str(session_log_path())}, headers=auth_headers(self.core_token))
        except Exception:
            pass


core_process = CoreProcessManager()
