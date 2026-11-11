from typing import Any, Dict, List, Optional, Tuple

import httpx
from ui.core_bridge.transport import core_http_client
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict

from core.security import auth_headers, load_or_create_core_token


class SessionApiClient:
    """Единственная точка доступа GUI к сессиям, которыми владеет ядро."""

    def __init__(self, base_url: str = "http://127.0.0.1:5000"):
        self.base_url = base_url
        self.core_token = load_or_create_core_token()

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        self.core_token = load_or_create_core_token()
        timeout = kwargs.pop("timeout", 5.0)
        with core_http_client(self.base_url, timeout=timeout) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                headers=auth_headers(self.core_token),
                **kwargs,
            )
            if response.is_error:
                try:
                    detail = response.json().get('detail')
                except (ValueError, AttributeError):
                    detail = None
                if isinstance(detail, str) and detail.strip():
                    raise ValueError(detail)
                response.raise_for_status()
            return response.json()

    def list_sessions(self) -> List[str]:
        return self._request("GET", "/v1/sessions").get("sessions", [])

    def set_message_pin(self, session_id, identity, pinned, branch='main'):
        return self._request('PATCH', f'/v1/sessions/{session_id}/messages/{identity}/pin',
                             json={'branch': branch, 'pinned': bool(pinned)})

    def list_session_summaries(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/sessions").get("items", [])

    def create_session(
        self,
        metadata: Dict[str, Any],
        folder_id: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"metadata": metadata}
        if folder_id:
            payload["folder_id"] = folder_id
        if settings is not None:
            payload["settings"] = dict(settings or {})
        return self._request("POST", "/v1/sessions", json=payload)

    def get_projects(self) -> Dict[str, Any]:
        return self._request("GET", "/v1/projects", timeout=30.0)

    def get_system_prompt_preview(
        self, session_id: Optional[str] = None, branch: str = "main"
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"branch": branch or "main"}
        if session_id:
            params["session_id"] = session_id
        return self._request(
            "GET", "/v1/system-prompt-preview", params=params, timeout=30.0
        )

    def get_compressed_context_preview(self, session_id=None, branch='main'):
        params = {'branch': branch or 'main'}
        if session_id:
            params['session_id'] = session_id
        return self._request('GET', '/v1/compressed-context-preview', params=params, timeout=30.0)

    def create_project_folder(self, name: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST", "/v1/projects/folders",
            json={"name": name, "parent_id": parent_id},
        )

    def update_project_folder(self, folder_id: str, **updates) -> Dict[str, Any]:
        return self._request("PATCH", f"/v1/projects/folders/{folder_id}", json=updates)

    def delete_project_folder(self, folder_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/v1/projects/folders/{folder_id}")

    def update_project_session(
        self,
        session_id: str,
        folder_id: Any = ...,
        settings: Any = ...,
        memory_mode: Any = ...,
        include_thoughts_in_context: Any = ...,
        include_replied_thoughts_in_context: Any = ...,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if folder_id is not ...:
            payload["folder_id"] = folder_id
        if settings is not ...:
            payload["settings"] = settings
        if memory_mode is not ...:
            payload["memory_mode"] = memory_mode
        if include_thoughts_in_context is not ...:
            payload["include_thoughts_in_context"] = bool(include_thoughts_in_context)
        if include_replied_thoughts_in_context is not ...:
            payload["include_replied_thoughts_in_context"] = bool(include_replied_thoughts_in_context)
        return self._request("PATCH", f"/v1/projects/sessions/{session_id}", json=payload)

    def update_project_branch(
        self,
        session_id: str,
        branch_name: str,
        settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/projects/sessions/{session_id}/branches/{branch_name}",
            json={"settings": dict(settings or {})},
        )

    def list_modules(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/modules").get("modules", [])

    def get_tasks(self, session_id: str, branch="main") -> Dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{session_id}/tasks", params={"branch": branch})

    def add_tasks(self, session_id: str, tasks: List[str], branch="main") -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/tasks",
            json={"tasks": list(tasks or []), "branch": branch},
        )

    def create_empty_task(self, session_id: str, branch="main") -> Dict[str, Any]:
        # Use the long-existing /tasks endpoint instead of a new route. The zero-width
        # sentinel keeps this compatible with an older still-running core, while a
        # current core recognizes create_empty and stores a real empty string.
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/tasks",
            json={"tasks": ["\u200b"], "create_empty": True, "branch": branch},
        )

    def update_task(self, session_id: str, task_id: str, *, branch="main", **updates) -> Dict[str, Any]:
        payload = dict(updates or {})
        payload["allow_empty_text"] = True
        payload["branch"] = branch
        return self._request("PATCH", f"/v1/sessions/{session_id}/tasks/{task_id}", json=payload)

    def delete_task(self, session_id: str, task_id: str, branch="main") -> Dict[str, Any]:
        return self._request("DELETE", f"/v1/sessions/{session_id}/tasks/{task_id}", params={"branch": branch})

    def apply_task_statuses(
        self,
        session_id: str,
        statuses: List[Dict[str, str]],
        *,
        mark_checked: bool = False,
        branch="main",
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/tasks/statuses",
            json={"statuses": list(statuses or []), "mark_checked": bool(mark_checked), "branch": branch},
        )

    def list_dirty_task_sessions(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/tasks/dirty-sessions").get("sessions", [])

    def run_chat_message(
        self,
        session_id: str,
        branch: str,
        text: str,
        *,
        additional_kwargs: Optional[Dict[str, Any]] = None,
    ) -> str:
        payload = {
            "messages": [{
                "role": "user",
                "content": str(text or ""),
                "additional_kwargs": dict(additional_kwargs or {}),
            }],
            "stream": False,
            "session_id": session_id,
            "branch": branch or "main",
        }
        data = self._request("POST", "/v1/chat/completions", json=payload, timeout=3600.0)
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str(((choices[0] or {}).get("message") or {}).get("content") or "")

    def import_attachment(self, session_id: str, source_path: str, *, force_copy: bool = False,
                          to_memory=False, branch='main', request_id='') -> Dict[str, Any]:
        payload = {"source_path": source_path, "to_memory": bool(to_memory), "branch": branch, "request_id": request_id}
        if force_copy:
            payload["force_copy"] = True
        data = self._request(
            "POST",
            f"/v1/sessions/{session_id}/attachments/import",
            json=payload,
            timeout=3600.0,
        )
        return data.get("attachment", {})

    def cleanup_attachments(self, session_id: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/attachments/cleanup",
            timeout=60.0,
        )

    def load_session(
        self,
        session_id: str,
        branch: Optional[str] = None,
    ) -> Tuple[List[BaseMessage], Dict[str, Any], str]:
        params = {"branch": branch} if branch else None
        data = self._request("GET", f"/v1/sessions/{session_id}", params=params)
        return messages_from_dict(data.get("messages", [])), data.get("metadata", {}), data.get("branch", "main")

    def load_session_page(self, session_id, branch='main', *, limit=10, before=None):
        params = {'branch': branch, 'limit': limit}
        if before is not None:
            params['before'] = before
        data = self._request('GET', f'/v1/sessions/{session_id}', params=params)
        data['messages'] = messages_from_dict(data.get('messages', []))
        return data

    def token_usage(self, session_id, branch='main', app_id='', *, refresh_model=False):
        return self._request('GET', f'/v1/sessions/{session_id}/token-usage',
                             params={'branch': branch, 'app_id': app_id, 'refresh_model': refresh_model}, timeout=8.0)

    def compact_context(self, session_id, branch='main', *, cancel=False):
        return self._request('POST', f'/v1/sessions/{session_id}/context/compact',
                             json={'branch':branch,'cancel':cancel}, timeout=10.0)

    def update_metadata(self, session_id: str, metadata: Dict[str, Any], branch: str = "main") -> Dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/sessions/{session_id}/metadata",
            json={"metadata": metadata, "branch": branch},
        )

    def replace_messages(self, session_id: str, messages: List[BaseMessage], branch: str = "main", *, preserve_prefix=0, prefix_anchor=None) -> Dict[str, Any]:
        return self._request(
            "PUT",
            f"/v1/sessions/{session_id}/messages",
            json={"messages": [message_to_dict(message) for message in messages], "branch": branch,
                  'preserve_prefix': preserve_prefix, 'prefix_anchor': prefix_anchor},
        )

    def move_selection(self, session_id, *, branch, snapshot, selected, remaining,
                       destination_session, destination_branch='main', new_branch=False):
        return self._request('POST', f'/v1/sessions/{session_id}/messages/move-selection', timeout=120.0,
            json={'branch': branch, 'snapshot': [message_to_dict(m) for m in snapshot],
                  'selected': [message_to_dict(m) for m in selected],
                  'remaining': [message_to_dict(m) for m in remaining],
                  'destination_session': destination_session, 'destination_branch': destination_branch,
                  'new_branch': new_branch})

    def rename_session(self, session_id: str, new_name: str) -> Dict[str, Any]:
        return self._request("PATCH", f"/v1/sessions/{session_id}", json={"new_name": new_name})

    def forward_messages(self, session_id, *, branch, snapshot, selected, destination_session, destination_branch='main'):
        return self._request('POST', f'/v1/sessions/{session_id}/messages/forward', timeout=120.0,
            json={'branch': branch, 'snapshot': [message_to_dict(message) for message in snapshot],
                  'selected': [message_to_dict(message) for message in selected],
                  'destination_session': destination_session, 'destination_branch': destination_branch})

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/v1/sessions/{session_id}")

    def list_branches(self, session_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/v1/sessions/{session_id}/branches").get("branches", [])

    def create_branch(
        self,
        session_id: str,
        branch_name: str,
        source_branch: str = "main",
        fork_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/branches/create",
            json={"branch_name": branch_name, "source_branch": source_branch, "fork_index": fork_index},
        )

    def rename_branch(self, session_id: str, old_name: str, new_name: str) -> Dict[str, Any]:
        return self._request(
            "PATCH",
            f"/v1/sessions/{session_id}/branches/{old_name}",
            json={"new_name": new_name},
        )

    def delete_branch(self, session_id: str, branch_name: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/sessions/{session_id}/branches/delete",
            json={"branch_name": branch_name},
        )
