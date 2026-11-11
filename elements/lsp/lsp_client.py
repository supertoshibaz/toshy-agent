from core.logger import core_logger
import os
import sys
import json
import shutil
import signal
from pathlib import Path
from typing import Dict, Any, Optional, Callable, List, Tuple
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal, QTimer
from core.paths import APP_PATHS


def relocated_lsp_path(path):
    """Keep explicit paths into the former app-local LSP directory usable."""
    path = Path(path).expanduser()
    if path.is_absolute() and not path.exists():
        try:
            relative = path.relative_to(APP_PATHS.root / 'lsp')
        except ValueError:
            pass
        else:
            moved = APP_PATHS.lsp / relative
            if moved.exists():
                return moved
    return path


def server_command_path(command, directory=None):
    if command == 'python':
        return Path(sys.executable)
    if command.startswith(('servers/', 'servers\\')):
        return Path(directory or APP_PATHS.lsp) / command.replace('\\', '/')
    path = relocated_lsp_path(command)
    if path.is_absolute():
        return path
    return Path(shutil.which(command) or command)


class LspClient(QObject):
    """
    Асинхронный клиент Language Server Protocol (JSON-RPC over stdio).
    С очередью документов до завершения инициализации и поддержкой синхронизации.
    """
    diagnostics_received = Signal(str, list)  # uri, list of diagnostics
    server_started = Signal(str)
    server_stopped = Signal(str)
    log_message = Signal(str)

    def __init__(self, name: str, config: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.name = name
        self.config = config
        self.process: Optional[QProcess] = None
        self._buffer = b""
        self._request_id = 0
        self._pending_requests: Dict[int, Callable[[Any], None]] = {}
        self.is_initialized = False

        self._pending_opens: List[Tuple[str, str, str]] = []
        self._stopping = False
        self._stderr_tail = b""
        self._server_pid = 0
        self._command = ""
        self._arguments = []
        self._last_error = ""
        self._request_methods = {}

    def start(self, root_path: str):
        if self.process and self.process.state() != QProcess.NotRunning:
            return
        if self.process is not None:
            self.process.deleteLater()
        self._stopping = False
        self.is_initialized = False
        self._buffer = b""
        self._stderr_tail = b""
        self._server_pid = 0
        self._last_error = ""
        self._pending_requests.clear()
        self._request_methods.clear()

        command = self.config.get("command", "python")
        if os.name == "nt" and "command_windows" in self.config:
            command = self.config["command_windows"]

        args = list(self.config.get("args", []))

        # Do not resolve a venv interpreter symlink to the system interpreter.
        target_file = server_command_path(command)
        if target_file.is_absolute():
            if target_file.exists():
                command = str(target_file)
                if os.name != "nt" and target_file.is_relative_to(APP_PATHS.lsp):
                    try:
                        target_file.chmod(target_file.stat().st_mode | 0o111)
                    except Exception:
                        pass
            else:
                core_logger.event('resources', 'RESOURCES_EVENT', f"[LSP] ⚠️ Сервер '{self.name}' не найден по пути: {target_file}", level='WARN')
                return

        self.process = QProcess(self)
        self._command, self._arguments = command, args
        if self.name == 'harper_ls':
            environment = QProcessEnvironment.systemEnvironment()
            if not environment.contains('RUST_BACKTRACE'):
                environment.insert('RUST_BACKTRACE', '1')
            self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(root_path)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.stateChanged.connect(self._on_state_changed)

        self.process.started.connect(lambda: self._on_started(root_path))
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.start(command, args)

    def _on_started(self, root_path):
        self._server_pid = int(self.process.processId())
        core_logger.event('resources', 'LSP_START', self.name,
                          details={**self._process_details(), 'root': root_path})
        self._send_initialize(root_path)
        self.server_started.emit(self.name)

    def _process_details(self):
        return {'server_pid': self._server_pid, 'command': self._command,
                'args': self._arguments, 'stopping': self._stopping,
                'process_error': self._last_error,
                'stderr_tail': self._stderr_tail.decode('utf-8', errors='replace')}

    def _on_process_error(self, error):
        self._last_error = self.process.errorString()
        # CrashExit is reported by finished, after exit code and stderr are available.
        if error == QProcess.Crashed:
            return
        self._on_stderr()
        core_logger.event('resources', 'LSP_ERROR', f'{self.name}: {self._last_error}',
                          level='INFO' if self._stopping else 'ERROR', details=self._process_details())
        self.log_message.emit(self._last_error)
        if error == QProcess.FailedToStart:
            self._pending_requests.clear()
            self._request_methods.clear()
            self.server_stopped.emit(self.name)

    def _on_finished(self, code, status):
        self._on_stderr()
        self.is_initialized = False
        self._pending_requests.clear()
        self._request_methods.clear()
        details = {**self._process_details(), 'exit_code': code, 'exit_status': status.name}
        if status == QProcess.CrashExit and os.name != 'nt':
            try:
                details['signal'] = signal.Signals(abs(code)).name
            except ValueError:
                pass
        unexpected = not self._stopping and (status == QProcess.CrashExit or code != 0)
        if unexpected:
            message = f'{self.name}: {self._last_error or "Server exited unexpectedly"}'
            core_logger.event('resources', 'LSP_ERROR', message, level='ERROR', details=details)
            self.log_message.emit(message)
        core_logger.event('resources', 'LSP_STOP', self.name,
                          level='INFO' if self._stopping else 'WARN', details=details)
        self.server_stopped.emit(self.name)

    def stop_async(self):
        """Release an evicted server without blocking the Qt event loop."""
        if self._stopping:
            return
        self._stopping = True
        process = self.process
        self._pending_opens.clear()
        if process is None or process.state() == QProcess.NotRunning:
            return
        if self.is_initialized:
            self.send_request("shutdown", None, lambda _: self.send_notification("exit", None))
        else:
            process.terminate()
        QTimer.singleShot(1000, process, lambda: process.terminate() if process.state() != QProcess.NotRunning else None)
        QTimer.singleShot(2000, process, lambda: process.kill() if process.state() != QProcess.NotRunning else None)

    def stop(self):
        self._stopping = True
        process = self.process
        if process is None:
            self.is_initialized = False
            return

        if process.state() != QProcess.NotRunning:
            # The GUI is shutting down, so make process termination deterministic.
            # LSP requires shutdown -> response -> exit for a successful exit.
            if self.is_initialized:
                self.send_request("shutdown", None, lambda _: self.send_notification("exit", None))
            else:
                self.send_notification("exit", None)
            process.waitForBytesWritten(200)
            if not process.waitForFinished(1000):
                process.terminate()
                if not process.waitForFinished(500):
                    process.kill()
                    process.waitForFinished(1000)

        self._pending_requests.clear()
        self._request_methods.clear()
        self._pending_opens.clear()
        self._buffer = b""
        self.is_initialized = False

    def _on_state_changed(self, state):
        if state == QProcess.NotRunning:
            self.is_initialized = False

    def _send_payload(self, payload: Dict[str, Any]):
        if not self.process or self.process.state() != QProcess.Running:
            return
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self.process.write(header + body)

    def send_request(self, method: str, params: Optional[Dict[str, Any]], callback: Optional[Callable] = None) -> int:
        self._request_id += 1
        req_id = self._request_id
        if callback:
            self._pending_requests[req_id] = callback
            self._request_methods[req_id] = method
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload['params'] = params
        self._send_payload(payload)
        return req_id

    def send_notification(self, method: str, params: Optional[Dict[str, Any]]):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload['params'] = params
        self._send_payload(payload)

    def _send_initialize(self, root_path: str):
        root_uri = Path(root_path).resolve().as_uri()
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": root_path,
            "capabilities": {
                "workspace": {
                    "applyEdit": False,
                    "workspaceFolders": True,
                    "configuration": True
                },
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                        "didSave": True
                    },
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "tagSupport": {"valueSet": [1, 2]}
                    },
                    "hover": {
                        "contentFormat": ["markdown", "plaintext"]
                    }
                }
            }
        }

        def on_init_response(resp):
            if self._stopping:
                self.send_notification("exit", None)
                return
            if not isinstance(resp, dict) or not isinstance(resp.get('capabilities'), dict):
                core_logger.event('resources', 'LSP_ERROR', f'{self.name}: Invalid initialize response', level='ERROR')
                return
            self.is_initialized = True
            self.send_notification("initialized", {})
            self.send_notification("workspace/didChangeConfiguration", {"settings": self.config.get("settings", {})})
            core_logger.event('resources', 'RESOURCES_EVENT', f"[LSP] Сервер '{self.name}' инициализирован и готов к работе.", level='INFO')

            for f_path, l_id, text in self._pending_opens:
                self._do_open(f_path, l_id, text)
            self._pending_opens.clear()

        self.send_request("initialize", params, on_init_response)

    def _do_open(self, file_path: str, language_id: str, text: str):
        uri = Path(file_path).resolve().as_uri()
        params = {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text
            }
        }
        self.send_notification("textDocument/didOpen", params)

    def did_open(self, file_path: str, language_id: str, text: str):
        if not self.is_initialized:
            self._pending_opens = [item for item in self._pending_opens if item[0] != file_path]
            self._pending_opens.append((file_path, language_id, text))
            return
        self._do_open(file_path, language_id, text)

    def did_change(self, file_path: str, text: str, version: int):
        if not self.is_initialized:
            self._pending_opens = [(p, language, text if p == file_path else body)
                                   for p, language, body in self._pending_opens]
            return
        uri = Path(file_path).resolve().as_uri()
        params = {
            "textDocument": {
                "uri": uri,
                "version": version
            },
            "contentChanges": [{"text": text}]
        }
        self.send_notification("textDocument/didChange", params)

    def did_save(self, file_path: str, text: str):
        if not self.is_initialized:
            return
        uri = Path(file_path).resolve().as_uri()
        params = {
            "textDocument": {"uri": uri},
            "text": text
        }
        self.send_notification("textDocument/didSave", params)

    def did_close(self, file_path: str):
        self._pending_opens = [item for item in self._pending_opens if item[0] != file_path]
        if not self.is_initialized:
            return
        uri = Path(file_path).resolve().as_uri()
        params = {"textDocument": {"uri": uri}}
        self.send_notification("textDocument/didClose", params)

    def _on_stdout(self):
        data = self.process.readAllStandardOutput().data()
        self._buffer += data

        while True:
            header_end = self._buffer.find(b"\r\n\r\n")
            if header_end == -1:
                break

            header_bytes = self._buffer[:header_end]
            content_length = None
            for line in header_bytes.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    try:
                        content_length = int(line.split(b":")[1].strip())
                    except ValueError:
                        pass
                    break

            if content_length is None:
                self._buffer = self._buffer[header_end + 4:]
                continue

            total_len = header_end + 4 + content_length
            if len(self._buffer) < total_len:
                break

            body_bytes = self._buffer[header_end + 4:total_len]
            self._buffer = self._buffer[total_len:]

            try:
                msg = json.loads(body_bytes.decode("utf-8"))
                self._handle_message(msg)
            except Exception as e:
                core_logger.event('resources', 'RESOURCES_EVENT', f"[LSP Error] Ошибка парсинга JSON: {e}", level='ERROR')

    def _on_stderr(self):
        data = self.process.readAllStandardError().data()
        self._stderr_tail = (self._stderr_tail + data)[-16384:]
        err = data.decode("utf-8", errors="replace")
        if err.strip():
            core_logger.event('resources', 'LSP_STDERR', f'{self.name}: {err.strip()[:16000]}')
            self.log_message.emit(err)

    def _handle_message(self, msg: Dict[str, Any]):
        # Client and server request IDs occupy separate namespaces.
        if "method" not in msg and "id" in msg and msg["id"] in self._pending_requests:
            callback = self._pending_requests.pop(msg["id"])
            method = self._request_methods.pop(msg['id'], '')
            if 'error' in msg:
                core_logger.event('resources', 'LSP_REQUEST_ERROR', f'{self.name}: {method}',
                                  level='ERROR', details={'error': msg['error']})
                return
            callback(msg.get("result"))
            return

        if "method" in msg and "id" in msg:
            method = msg["method"]
            req_id = msg["id"]
            if method == 'workspace/workspaceFolders':
                root = Path(self.process.workingDirectory()).resolve()
                self._send_payload({'jsonrpc': '2.0', 'id': req_id,
                                    'result': [{'uri': root.as_uri(), 'name': root.name}]})
                return
            if method == "workspace/configuration":
                items = msg.get("params", {}).get("items", [])
                result = []
                for item in items:
                    if self.config.get("category") == "linguistic":
                        settings = self.config.get("settings", {})
                        section = item.get('section')
                        value = settings
                        if section:
                            # A missing section is null, never an unrelated settings object.
                            for part in section.split('.'):
                                value = value.get(part) if isinstance(value, dict) else None
                        result.append(value)
                        continue
                    jedi_cfg = {
                        "enabled": True,
                        "extra_paths": [self.process.workingDirectory()]
                    }
                    if hasattr(self, "user_python_env") and self.user_python_env:
                        jedi_cfg["environment"] = self.user_python_env

                    result.append({
                        "plugins": {
                            "pyflakes": {"enabled": True},
                            "pycodestyle": {"enabled": True},
                            "jedi": jedi_cfg
                        },
                        "pylsp": {
                            "plugins": {
                                "jedi": jedi_cfg
                            }
                        }
                    })
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result
                }
                self._send_payload(response)
                return
            self._send_payload({'jsonrpc': '2.0', 'id': req_id,
                                'error': {'code': -32601, 'message': f'Unsupported client method: {method}'}})
            return

        method = msg.get("method")
        if method in ('window/logMessage', 'window/showMessage'):
            params = msg.get('params', {})
            core_logger.event('resources', 'LSP_SERVER_MESSAGE', f"{self.name}: {str(params.get('message', ''))[:16000]}",
                              level={1: 'ERROR', 2: 'WARN'}.get(params.get('type'), 'INFO'))
            return
        if method == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self.diagnostics_received.emit(uri, diagnostics)
