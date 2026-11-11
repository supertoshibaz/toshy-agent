import json
import threading
import time
import uuid

import httpx
from ui.core_bridge.transport import core_http_client
from typing import List, Dict, Any, Optional
from PySide6.QtCore import QThread, Signal
from core.security import auth_headers, load_or_create_core_token


class ChatStreamWorker(QThread):
    """
    Асинхронный поток стриминга:
    - Принимает события 'thought', 'tool_start', 'tool_result', 'content', 'content_interim'.
    - Передает флаг include_thoughts_in_context.
    """
    steering_event = Signal(dict)
    subagent_event = Signal(dict)
    pipeline_paused = Signal(dict)
    pipeline_notification = Signal(dict)
    message_identity = Signal(str, str)
    thought_chunk_received = Signal(str)
    tool_started = Signal(str, dict, str)
    tool_finished = Signal(str, str, str)
    tool_sources_received = Signal(str, object)
    content_chunk_received = Signal(str)
    content_interim_received = Signal()
    content_reset_received = Signal()
    embedding_error_received = Signal(str)
    embedding_status_received = Signal(bool, str)
    stream_finished = Signal()
    error_occurred = Signal(str)
    run_state_received = Signal(dict)
    completed_segments = None
    answer_restart_received = Signal()
    answer_continue_received = Signal()
    reasoning_control_received = Signal(dict)

    def __init__(
        self,
        messages: List[Dict[str, str]],
        base_url: str = "http://127.0.0.1:5000",
        session_id: Optional[str] = None,
        branch: str = "main",
        include_thoughts_in_context: bool = True,
        workspace_root: Optional[str] = None,
        temp_memory_mode: str = "text",
        limit_ranking: bool = True,
        persist_result: bool = True,
        use_provided_history: bool = False,
        resume_run_id: str = '',
        resume_mode: str = 'native',
        raw_request: bool = False,
        parent=None
    ):
        super().__init__(parent)
        self.pipeline = None
        self.tool_result_statuses = {}
        self.reasoning_effort = ''
        self.model_profile_id = ''
        self.resume_model_profile_id = ''
        self.messages = messages
        self.base_url = base_url
        self.session_id = session_id
        self.branch = branch
        self.include_thoughts_in_context = include_thoughts_in_context
        self.workspace_root = workspace_root
        self.temp_memory_mode = temp_memory_mode
        self.limit_ranking = limit_ranking
        self.persist_result = bool(persist_result)
        self.use_provided_history = bool(use_provided_history)
        self.resume_run_id = resume_run_id
        self.resume_mode = resume_mode
        self.raw_request = bool(raw_request)
        self.result_state = None
        self.reasoning_control_state = {}
        self.error_code = ''
        self.core_token = load_or_create_core_token()
        self._preserve_pause = False
        self._is_cancelled = False
        self.was_cancelled = False
        self.run_id = resume_run_id or uuid.uuid4().hex
        self._client = None
        self._cancel_thread = None
        self._final_content_parts: List[str] = []
        self.task_status_check = None
        self.task_status_report_called = False
        self.task_status_report_success = False
        self.task_status_report_output = ""

    def _request_server_cancel(self):
        """Ask Toshy Core to cancel the actual graph/provider request.

        Closing the GUI-side HTTP stream alone only disconnects the reader; it
        does not reliably stop generation inside the core/provider.  The
        dedicated control request cancels the server task that owns the graph.
        """
        url = f"{self.base_url}/v1/chat/runs/{self.run_id}/cancel"
        # A very fast click can race the server registering the stream. Retry a
        # few times when the run is not visible yet.
        for _ in range(4):
            if not self._is_cancelled:
                return
            try:
                with core_http_client(self.base_url, timeout=httpx.Timeout(2.0, connect=1.0)) as client:
                    response = client.post(url, json={"preserve_pause": self._preserve_pause}, headers=auth_headers(self.core_token))
                if response.status_code == 200:
                    payload = response.json() if response.content else {}
                    if payload.get("cancelled") or payload.get("active"):
                        return
            except Exception:
                pass
            time.sleep(0.12)

    def cancel(self, preserve_pause=False):
        if self._is_cancelled:
            return
        self._preserve_pause = preserve_pause
        self._is_cancelled = True
        self.was_cancelled = True
        # Do not close the streaming httpx.Client from the GUI thread. Besides
        # being racy, that used to surface a local timeout while the model kept
        # generating on the server.
        self._cancel_thread = threading.Thread(target=self._request_server_cancel, daemon=True)
        self._cancel_thread.start()

    @property
    def final_content(self) -> str:
        return "".join(self._final_content_parts)

    def stop_thinking(self, control_id=None):
        """Called by an AsyncRunner, never blocks the GUI or closes the stream."""
        if control_id is None:
            control_id = self.reasoning_control_state.get('control_id')
        with core_http_client(self.base_url, timeout=httpx.Timeout(7.0, connect=2.0)) as client:
            response = client.post(f'{self.base_url}/v1/chat/runs/{self.run_id}/stop-thinking',
                                   json={'control_id': control_id},
                                   headers=auth_headers(self.core_token))
            response.raise_for_status()
            result = response.json()
        state = result.get('control')
        if isinstance(state, dict) and state.get('control_id') == self.reasoning_control_state.get('control_id'):
            self.reasoning_control_state = state
        return result

    def run(self):
        self.core_token = load_or_create_core_token()
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "messages": self.messages,
            "stream": True,
            "temp_memory_mode": self.temp_memory_mode,
            "limit_ranking": self.limit_ranking,
            "include_thoughts_in_context": self.include_thoughts_in_context,
            "run_id": self.run_id,
            "persist_result": self.persist_result,
            "use_provided_history": self.use_provided_history,
            "raw_request": self.raw_request,
        }
        if self.pipeline is not None:
            payload["pipeline"] = self.pipeline
        payload['model_profile_id'] = self.model_profile_id
        payload['reasoning_effort'] = self.reasoning_effort
        if self.resume_run_id:
            payload.update(resume_run_id=self.resume_run_id, resume_mode=self.resume_mode)
        if self.session_id:
            payload["session_id"] = self.session_id
            payload["branch"] = self.branch
        if self.workspace_root:
            payload["workspace_root"] = self.workspace_root

        try:
            from core.model_connections import load_settings, push_runtime_keys
            profile_id = self.resume_model_profile_id or self.model_profile_id or load_settings().get('selected')
            if profile_id:
                push_runtime_keys(connection_id=profile_id)
            client_timeout = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0)
            with core_http_client(self.base_url, timeout=client_timeout) as client:
                self._client = client
                with client.stream("POST", url, json=payload, headers=auth_headers(self.core_token)) as response:
                    if response.status_code != 200:
                        if self._is_cancelled:
                            return
                        err_text = response.read().decode('utf-8', errors='replace')
                        self.error_occurred.emit(f"HTTP {response.status_code}: {err_text}")
                        return

                    terminal = False
                    for line in response.iter_lines():
                        if self._is_cancelled:
                            break

                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                terminal = True
                                break

                            try:
                                data = json.loads(data_str)
                                event_type = data.get("type")
                                if event_type == 'reasoning_control':
                                    self.reasoning_control_state = dict(data)
                                    self.reasoning_control_received.emit(dict(data))
                                    continue
                                if event_type in {'user_update', 'steering_applied'}:
                                    self.steering_event.emit(data)
                                    continue
                                if event_type == 'run_projection':
                                    self.completed_segments = data.get('segments') or []
                                    continue
                                if event_type == 'run_status':
                                    self.result_state = dict(data)
                                    self.run_state_received.emit(dict(data))
                                    continue
                                if event_type == 'answer_continue':
                                    self.answer_continue_received.emit()
                                    continue
                                if event_type == 'answer_restart':
                                    self._final_content_parts.clear()
                                    self.answer_restart_received.emit()
                                    continue
                                if data.get('chat_id') and event_type in {'content', 'thought', 'tool_start', 'tool_result'}:
                                    self.message_identity.emit('assistant' if event_type == 'content' else 'thought', data['chat_id'])

                                if event_type == "pipeline_notification":
                                    self.pipeline_notification.emit(data)
                                elif event_type == "pipeline_pause":
                                    self.pipeline_paused.emit(data)
                                elif event_type == "subagent":
                                    self.subagent_event.emit(data)
                                elif event_type == "thought":
                                    self.thought_chunk_received.emit(data.get("delta", ""))
                                elif event_type == "tool_start":
                                    self.tool_started.emit(data.get("name", "tool"), data.get("args", {}), data.get("id", ""))
                                elif event_type == "tool_result":
                                    tool_name = data.get("name", "tool")
                                    tool_output = data.get("output", "")
                                    if tool_name == "report_task_statuses":
                                        self.task_status_report_called = True
                                        self.task_status_report_output = str(tool_output or "")
                                        self.task_status_report_success = self.task_status_report_output.startswith(
                                            "System: Task status report accepted atomically"
                                        )
                                    self.tool_result_statuses[data.get("id", "")] = data.get("status") or ("failed" if data.get("error") else "completed")
                                    self.tool_finished.emit(data.get("id", ""), tool_name, tool_output)
                                    if tool_name in {'create_tasks', 'report_task_statuses'}:
                                        from ui.event_bus import event_bus
                                        event_bus.sessions_updated.emit()
                                    self.tool_sources_received.emit(data.get("id", ""), data.get("sources") or [])
                                elif event_type == "content":
                                    delta = data.get("delta", "")
                                    if delta:
                                        self._final_content_parts.append(delta)
                                    self.content_chunk_received.emit(delta)
                                elif event_type == "content_interim":
                                    self._final_content_parts.clear()
                                    self.content_interim_received.emit()
                                elif event_type == "content_reset":
                                    self._final_content_parts.clear()
                                    self.content_reset_received.emit()
                                elif event_type == "embedding_error":
                                    self.embedding_error_received.emit(data.get("message", "Неизвестная ошибка embeddings"))
                                elif event_type == "embedding_status":
                                    self.embedding_status_received.emit(
                                        bool(data.get("active", False)),
                                        data.get("message", ""),
                                    )
                                elif event_type == "error":
                                    if self._is_cancelled:
                                        return
                                    self.error_code = data.get('code', '')
                                    prefix = 'CONTINUATION_UNSUPPORTED::' if self.error_code == 'continuation_unsupported' else ''
                                    self.error_occurred.emit(prefix + data.get("message", "Неизвестная ошибка ядра"))
                                    return
                                elif "choices" in data:
                                    delta = data["choices"][0]["delta"].get("content", "")
                                    if delta:
                                        self._final_content_parts.append(delta)
                                        self.content_chunk_received.emit(delta)
                            except Exception:
                                pass
                    if not terminal and not self._is_cancelled:
                        self.error_occurred.emit('Соединение с ядром прервано до завершения ответа.')

        except httpx.ConnectError:
            if not self._is_cancelled:
                self.error_occurred.emit("CONNECTION_OFFLINE")
        except Exception as e:
            if not self._is_cancelled:
                self.error_occurred.emit(str(e))
        finally:
            self._client = None
            self.stream_finished.emit()
