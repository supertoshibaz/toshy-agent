import os
import json
import re
import time
import shutil
import uuid

import yaml
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
from langchain_core.messages import BaseMessage, messages_from_dict, message_to_dict
from core.paths import APP_PATHS
from core.sqlite_storage import SQLiteStore
from core.context import sanitize_session_id
from core.attachments import AttachmentError, inspect_attachment_path, _safe_filename


class SessionManager:
    """
    Менеджер сессий и веток диалогов.
    Ветки, сообщения, задачи, память и запуски находятся в session.sqlite
    и создаются строго как ответвления от главной ветки (main).
    """
    def __init__(self, sessions_dir: Optional[str] = None):
        self.sessions_dir = str(Path(sessions_dir or APP_PATHS.sessions).resolve())
        if not os.path.exists(self.sessions_dir):
            os.makedirs(self.sessions_dir, exist_ok=True)

    def get_session_dir(self, session_id: str, create: bool = True) -> str:
        safe_name = sanitize_session_id(session_id)
        session_folder = os.path.join(self.sessions_dir, safe_name)
        if create and not os.path.exists(session_folder):
            os.makedirs(session_folder, exist_ok=True)
        return session_folder

    def _write_json_atomic(self, path: str, payload: Any) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, target)

    def storage(self, session_id: str) -> SQLiteStore:
        return SQLiteStore(Path(self.get_session_dir(session_id, create=False)) / "session.sqlite")

    @staticmethod
    def clean_branch_name(branch_name: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]', '_', str(branch_name or '')).lower().strip('_') or "main"

    def has_session(self, session_id: str) -> bool:
        return self.storage(session_id).has_branch("main")

    def has_branch(self, session_id: str, branch_name: str) -> bool:
        return self.storage(session_id).has_branch(self.clean_branch_name(branch_name))

    def read_branch_payload(self, session_id: str, branch_name: str):
        return self.storage(session_id).read_branch(self.clean_branch_name(branch_name))

    def write_branch_payload(self, session_id: str, branch_name: str, payload):
        self.storage(session_id).write_branch(self.clean_branch_name(branch_name), payload)

    def get_main_session_path(self, session_id: str) -> str:
        """Physical database path; existence of a session is checked with has_session."""
        return str(self.storage(session_id).path)


    def _get_active_branch_name(self, session_id: str) -> str:
        payload = self.storage(session_id).read_branch_header("main")
        return (payload or {}).get("metadata", {}).get("active_branch", "main")

    def _set_active_branch_name(self, session_id: str, branch_name: str):
        self.storage(session_id).activate(self.clean_branch_name(branch_name))

    # =========================================================================
    # ВЛОЖЕНИЯ ЧАТА
    # =========================================================================

    def _attachment_storage_settings(self) -> Tuple[str, int]:
        mode = "copy"
        limit_mb = 25
        try:
            with open(APP_PATHS.config, "r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
            settings = config.get("attachments", {}) or {}
            mode = str(settings.get("storage_mode", "copy")).strip().lower()
            if mode not in {"copy", "link", "hybrid"}:
                mode = "copy"
            limit_mb = max(0, int(settings.get("copy_limit_mb", 25)))
        except Exception:
            pass
        return mode, limit_mb

    def import_attachment(self, session_id: str, source_path: str, *, force_copy: bool = False, memory_only: bool = False) -> Dict[str, Any]:
        """Register one local attachment and, depending on policy, copy it into the session.

        The GUI never writes session files directly. It only sends a local path to
        the core; the core owns copying and returns stable metadata for the message.
        """
        info = inspect_attachment_path(source_path)
        if memory_only and info['media_type'] != 'text':
            raise AttachmentError('Во временную память можно загрузить только текстовое вложение.')
        source = Path(info["source_path"])
        metadata, branch = self.load_session_metadata(session_id, branch='main')
        settings = self.resolve_session_project_settings(session_id, metadata, branch=branch)
        workspace = Path(settings.get('workspace_path') or APP_PATHS.workspace).expanduser().resolve()
        workspace_reference = source.is_relative_to(workspace) and not force_copy
        mode, limit_mb = self._attachment_storage_settings()
        effective = mode
        if mode == "hybrid":
            effective = "copy" if info["size"] <= limit_mb * 1024 * 1024 else "link"
        # Generated snapshots have no durable original to link to. The producer
        # can release its temporary file only after the session owns a copy.
        if force_copy or (memory_only and not workspace_reference):
            effective = "copy"
        elif workspace_reference:
            effective = 'link'

        attachment_id = f"att_{uuid.uuid4().hex}"
        if effective == "copy":
            session_dir = Path(self.get_session_dir(session_id, create=True))
            rel_dir = Path("attachments") / attachment_id
            target_dir = session_dir / rel_dir
            target_dir.mkdir(parents=True, exist_ok=False)
            filename = _safe_filename(info["filename"])
            target = target_dir / filename
            try:
                shutil.copy2(source, target)
                if os.name != 'nt':
                    target.chmod(0o600)
            except Exception:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise
            stored_path = (rel_dir / filename).as_posix()
        else:
            stored_path = str(source.resolve())

        workspace_data = {}
        if memory_only:
            # Внешний оригинал хранится только среди вложений сессии.
            workspace_data['memory_only'] = True
        elif workspace_reference:
            workspace_data['workspace_path'] = source.relative_to(workspace).as_posix()
        return {
            **workspace_data,
            "workspace_reference": workspace_reference,
            "id": attachment_id,
            "filename": info["filename"],
            "mime_type": info["mime_type"],
            "media_type": info["media_type"],
            "size": info["size"],
            "storage": effective,
            "path": stored_path,
        }

    @staticmethod
    def _collect_attachment_ids(value: Any, target: set) -> None:
        if isinstance(value, dict):
            attachments = value.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if isinstance(attachment, dict) and attachment.get("id"):
                        target.add(str(attachment["id"]))
            for nested in value.values():
                SessionManager._collect_attachment_ids(nested, target)
        elif isinstance(value, list):
            for nested in value:
                SessionManager._collect_attachment_ids(nested, target)

    def _referenced_attachment_ids(self, session_id: str) -> set:
        referenced = set()
        store = self.storage(session_id)
        for name, _, _, _ in store.branch_headers():
            self._collect_attachment_ids(store.read_branch(name), referenced)
        return referenced

    def cleanup_orphan_attachments(self, session_id: str) -> None:
        """Delete copied attachment folders no longer referenced by any branch.

        Link-mode attachments are never deleted from the user's filesystem.
        """
        session_dir = Path(self.get_session_dir(session_id, create=False))
        attachments_dir = session_dir / "attachments"
        if not attachments_dir.exists():
            return
        referenced = self._referenced_attachment_ids(session_id)
        for child in attachments_dir.iterdir():
            if child.is_dir() and child.name.startswith("att_") and child.name not in referenced:
                shutil.rmtree(child, ignore_errors=True)

    # =========================================================================
    # ЗАГРУЗКА И СОХРАНЕНИЕ
    # =========================================================================

    def load_session_metadata(self, session_id: str, default_meta=None, branch=None):
        active = self.clean_branch_name(branch or self._get_active_branch_name(session_id))
        header = self.storage(session_id).read_branch_header(active)
        if header is None and active != 'main':
            active = 'main'
            header = self.storage(session_id).read_branch_header(active)
        metadata = dict((header or {}).get('metadata') or default_meta or {
            'temp_memory_mode': 'text', 'limit_ranking': True, 'include_thoughts_in_context': True})
        metadata['active_branch'] = active if header is not None else 'main'
        return metadata, metadata['active_branch']

    def update_session_metadata(self, session_id: str, updates, branch=None):
        metadata, active = self.load_session_metadata(session_id, branch=branch)
        metadata.update(updates)
        if self.storage(session_id).has_branch(active):
            self.storage(session_id).update_branch_metadata(active, metadata)
        else:
            self.save_session(session_id, [], metadata, branch=active)
        if 'model_profile_id' in updates:
            from core.token_usage import reset_for_session
            reset_for_session(self, session_id, active)
        return metadata, active

    def load_session(self, session_id: str, default_meta=None, branch=None):
        active_branch = self.clean_branch_name(branch or self._get_active_branch_name(session_id))
        fallback_meta = default_meta or {"temp_memory_mode": "text", "limit_ranking": True,
                                        "include_thoughts_in_context": True}
        try:
            data = self.read_branch_payload(session_id, active_branch)
            if data is None and active_branch != "main":
                active_branch = "main"
                data = self.read_branch_payload(session_id, active_branch)
            if data is None:
                return [], dict(fallback_meta), "main"
            messages = messages_from_dict(data.get("messages", []))
            from core.interrupted_context import freeze_interrupted_notes
            freeze_interrupted_notes(messages)
            from core.chat_references import ensure_history_ids
            ensure_history_ids(messages, session_id)
            # Older SQLite chats already have the original protocol in their
            # journal. Recover only model metadata; keep displayed/user-edited text.
            from core.chat_runs import visible_run_message
            for message in messages:
                state = message.additional_kwargs.get('run_state') or {}
                if state.get('status') != 'completed' or 'model_segments' in state or not state.get('run_id'):
                    continue
                try:
                    checkpoint = self.storage(session_id).read_run_projection(state['run_id'])
                except FileNotFoundError:
                    continue  # A moved/imported message may have no journal here.
                recovered = visible_run_message(checkpoint).additional_kwargs['run_state']
                if 'model_segments' in recovered:
                    state = dict(state)
                    state.update({key: recovered[key] for key in ('model_segments', 'projection_thought_text')})
                    message.additional_kwargs['run_state'] = state
            metadata = data.get("metadata") or dict(fallback_meta)
            metadata["active_branch"] = active_branch
            return messages, metadata, active_branch
        except Exception as error:
            from core.logger import core_logger
            core_logger.event('storage', 'SESSION_LOAD_ERROR', str(error), level='ERROR',
                              context={'session_id': session_id, 'branch': active_branch})
            raise RuntimeError(f"Ошибка загрузки ветки '{active_branch}' в сессии '{session_id}': {error}") from error

    def load_session_page(self, session_id, *, branch=None, limit=10, before=None):
        active = self.clean_branch_name(branch or self._get_active_branch_name(session_id))
        data = self.storage(session_id).read_message_page(active, limit=limit, before=before)
        if data['header'] is None and active != 'main':
            active = 'main'
            data = self.storage(session_id).read_message_page(active, limit=limit, before=before)
        messages = messages_from_dict(data['messages'])
        from core.interrupted_context import restore_interrupted_notes
        restore_interrupted_notes(messages, self.storage(session_id))
        from core.chat_references import ensure_history_ids
        ensure_history_ids(messages, session_id, start_index=data['before'])
        metadata = dict((data['header'] or {}).get('metadata') or {})
        metadata['active_branch'] = active
        return messages, metadata, active, {'before': data['before'], 'total': data['total']}

    def save_session(self, session_id: str, messages: List[BaseMessage], metadata: Dict[str, Any], branch=None):
        from core.interrupted_context import freeze_interrupted_notes
        freeze_interrupted_notes(messages)
        active = self.clean_branch_name(branch or metadata.get("active_branch") or self._get_active_branch_name(session_id))
        from core.chat_references import ensure_history_ids
        ensure_history_ids(messages, session_id)
        try:
            store = self.storage(session_id)
            # Снимок GUI не обязан знать скрытые заметки. Восстанавливаем их по run_id.
            from core.interrupted_context import RETAINED_NOTES
            with store.connection(write=True) as db:
                for message in messages:
                    if message.type != 'ai':continue
                    run_id = (message.additional_kwargs.get('run_state') or {}).get('run_id')
                    if not run_id:continue
                    key = 'interrupted_notes:' + str(run_id)
                    notes = message.additional_kwargs.get(RETAINED_NOTES)
                    if notes:
                        SQLiteStore._set_state(db, key, notes)
                    else:
                        saved_notes = SQLiteStore._state(db, key)
                        if saved_notes:message.additional_kwargs[RETAINED_NOTES] = saved_notes
            payload = store.read_branch_header(active) or {}
            from core.chat_references import CHAT_ID, REQUEST_MEMORY
            # GUI snapshots may omit model-only metadata added by the core after
            # the request started. Keep it only for the same, unedited user text.
            saved_users = {item.get('data', {}).get('additional_kwargs', {}).get(CHAT_ID): item['data']
                           for item in store.read_user_messages(active) if item.get('type') == 'human'} if messages else {}
            for message in messages:
                if message.type != 'human':
                    continue
                current_memory = message.additional_kwargs.get(REQUEST_MEMORY)
                if isinstance(current_memory, dict) and current_memory.get('source_content') != str(message.content or ''):
                    message.additional_kwargs.pop(REQUEST_MEMORY, None)
                module_context = message.additional_kwargs.get('module_context')
                if isinstance(module_context, dict):
                    message.additional_kwargs['module_context'] = {key: value for key, value in module_context.items()
                        if isinstance(value, dict) and value.get('source_content') == str(message.content or '')}
                original = saved_users.get(message.additional_kwargs.get(CHAT_ID))
                if not original or original.get('content') != message.content:
                    continue
                for field in ('task_status_check', 'task_status_task_ids', 'task_status_display', 'task_status_refresh'):
                    if field in original.get('additional_kwargs', {}) and field not in message.additional_kwargs:
                        message.additional_kwargs[field] = original['additional_kwargs'][field]
                if 'module_context' not in message.additional_kwargs and isinstance(original.get('additional_kwargs', {}).get('module_context'), dict):
                    message.additional_kwargs['module_context'] = dict(original['additional_kwargs']['module_context'])
                if 'requested_resources' not in message.additional_kwargs and isinstance(original.get('additional_kwargs', {}).get('requested_resources'), dict):
                    message.additional_kwargs['requested_resources'] = dict(original['additional_kwargs']['requested_resources'])
                memory = original.get('additional_kwargs', {}).get(REQUEST_MEMORY)
                if (isinstance(memory, dict) and memory.get('source_content') == str(message.content or '')
                        and REQUEST_MEMORY not in message.additional_kwargs):
                    message.additional_kwargs[REQUEST_MEMORY] = dict(memory)
            payload.update(branch_name=active, metadata=dict(metadata), messages=[message_to_dict(m) for m in messages])
            if active != "main":
                payload.setdefault("parent_branch", "main")
                payload.setdefault("fork_message_index", 0)
                payload.setdefault("created_at", datetime.now().isoformat())
            store.write_branch(active, payload, activate=True)
            from core.logger import core_logger
            run_id = next((m.additional_kwargs.get('run_state', {}).get('run_id', '') for m in reversed(messages)
                           if m.additional_kwargs.get('run_state')), '')
            core_logger.event('storage', 'SESSION_SAVE', f'Saved {len(messages)} messages',
                context={'session_id': session_id, 'branch': active, 'run_id': run_id}, details={'path': str(store.path)})
        except Exception as error:
            raise RuntimeError(f"Ошибка сохранения ветки '{active}': {error}") from error

    # =========================================================================
    # УПРАВЛЕНИЕ ВЕТКАМИ
    # =========================================================================

    def create_branch(self, session_id: str, branch_name: str, source_branch="main", fork_index=None):
        clean = re.sub(r'[^a-zA-Z0-9_-]', '_', branch_name).lower().strip('_')
        if not clean or clean == "main":
            return {"success": False, "message": "Имя ветки не может быть 'main' или пустым."}
        if self.has_branch(session_id, clean):
            return {"success": False, "message": f"Ветка '{clean}' уже существует."}
        source_branch = self.clean_branch_name(source_branch)
        source = self.read_branch_payload(session_id, source_branch) or {}
        messages = source.get('messages', [])
        fork = fork_index if fork_index is not None and 0 <= fork_index <= len(messages) else len(messages)
        metadata = dict(source.get('metadata') or {"temp_memory_mode": "text", "limit_ranking": True,
                                                  "include_thoughts_in_context": True})
        metadata.pop('branch_project_settings', None)
        payload = {"branch_name": clean, "parent_branch": source_branch, "fork_message_index": fork,
                   "created_at": datetime.now().isoformat(), "metadata": metadata, "messages": messages[:fork]}
        self.storage(session_id).write_branch(clean, payload, activate=True)
        return {"success": True, "branch": clean, "fork_index": fork,
                "message": f"Ветка '{clean}' создана от '{source_branch}' (сообщений в ветке: {fork})."}

    def checkout_branch(self, session_id: str, branch_name: str):
        clean = self.clean_branch_name(branch_name)
        if not self.has_branch(session_id, clean):
            return {"success": False, "message": f"Ветка '{clean}' не найдена в сессии '{session_id}'."}
        self._set_active_branch_name(session_id, clean)
        return {"success": True, "branch": clean, "message": f"Переключено на ветку '{clean}'."}

    def list_branches(self, session_id: str):
        active = self._get_active_branch_name(session_id)
        headers = self.storage(session_id).branch_headers()
        from core.session_titles import titles
        labels = titles(self.storage(session_id))
        result = []
        for name, data, _, count in sorted(headers, key=lambda item: (item[0] != 'main', item[0])):
            result.append({"name": name, "title": labels.get(name, {}).get('title') or (session_id if name == 'main' else name),
                           "title_manual": bool(labels.get(name, {}).get('manual')), "is_active": active == name, "messages_count": count,
                           "parent": None if name == 'main' else data.get('parent_branch', 'main'),
                           "fork_index": None if name == 'main' else data.get('fork_message_index', 0)})
        return result or [{"name": "main", "is_active": True, "messages_count": 0, "parent": None, "fork_index": None}]

    def delete_branch(self, session_id: str, branch_name: str):
        clean = self.clean_branch_name(branch_name)
        if clean == 'main':
            return {"success": False, "message": "Главную ветку 'main' удалить нельзя."}
        if not self.has_branch(session_id, clean):
            return {"success": False, "message": f"Ветка '{clean}' не найдена."}
        try:
            self.storage(session_id).delete_branch(clean)
            with self.storage(session_id).connection(write=True, create=False) as db:
                db.execute('DELETE FROM state WHERE key=?', ('conversation_title:' + clean,))
            self.cleanup_orphan_attachments(session_id)
            return {"success": True, "message": f"Ветка '{clean}' успешно удалена."}
        except Exception as error:
            return {"success": False, "message": f"Ошибка удаления ветки '{clean}': {error}"}

    def list_sessions(self) -> List[str]:
        if not os.path.isdir(self.sessions_dir):
            return []
        sessions = []
        for folder in os.listdir(self.sessions_dir):
            if not (Path(self.sessions_dir) / folder / 'session.sqlite').is_file():
                continue
            try:
                if self.has_session(folder):
                    sessions.append(folder)
            except Exception:
                # Keep corrupt/future-version databases visible in Projects so
                # their load error is reported rather than hiding user data.
                sessions.append(folder)
        return sessions


    # =========================================================================
    # TASK LISTS
    # =========================================================================


    @staticmethod
    def _empty_tasks_state() -> Dict[str, Any]:
        return {"version": 1, "next_id": 1, "dirty": False, "tasks": []}

    def _load_tasks_state(self, session_id: str, branch=None) -> Dict[str, Any]:
        state = self._empty_tasks_state()
        branch = self.clean_branch_name(branch or self._get_active_branch_name(session_id))
        raw = self.storage(session_id).read_tasks(state, branch)
        if not isinstance(raw, dict):
            return state

        tasks: List[Dict[str, Any]] = []
        max_numeric_id = 0
        for item in raw.get("tasks", []) if isinstance(raw.get("tasks"), list) else []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").replace("\u200b", "").strip()
            status = str(item.get("status") or "in_progress").strip().lower()
            if not task_id:
                continue
            if status not in {"in_progress", "completed"}:
                status = "in_progress"
            match = re.fullmatch(r"task_(\d+)", task_id)
            if match:
                max_numeric_id = max(max_numeric_id, int(match.group(1)))
            tasks.append({
                "id": task_id,
                "text": text,
                "status": status,
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            })

        try:
            next_id = max(int(raw.get("next_id", 1)), max_numeric_id + 1, 1)
        except (TypeError, ValueError):
            next_id = max(max_numeric_id + 1, 1)
        dirty = bool(raw.get("dirty", False)) and any(task["status"] == "in_progress" for task in tasks)
        return {"version": 1, "next_id": next_id, "dirty": dirty, "tasks": tasks,
                "last_request": str(raw.get('last_request') or ''),
                "checked_request": str(raw.get('checked_request') or ''),
                "report_count": int(raw.get('report_count') or 0)}

    def _save_tasks_state(self, session_id: str, state: Dict[str, Any], branch=None) -> None:
        session_dir = self.get_session_dir(session_id, create=False)
        if not os.path.exists(session_dir):
            raise FileNotFoundError(f"Сессия '{session_id}' не найдена.")
        normalized = dict(state or {})
        normalized["version"] = 1
        tasks = list(normalized.get("tasks") or [])
        normalized["tasks"] = tasks
        if not any(str(task.get("status") or "") == "in_progress" for task in tasks if isinstance(task, dict)):
            normalized["dirty"] = False
        self.storage(session_id).write_tasks(normalized, self.clean_branch_name(branch or self._get_active_branch_name(session_id)))

    def get_tasks_state(self, session_id: str, branch=None) -> Dict[str, Any]:
        state = self._load_tasks_state(session_id, branch)
        return {
            "dirty": bool(state.get("dirty", False)),
            "tasks": [dict(task) for task in state.get("tasks", [])],
            "report_count": state.get("report_count", 0),
            "checked_request": state.get("checked_request", ""),
        }

    def add_tasks(
        self,
        session_id: str,
        task_texts: List[str],
        *,
        allow_empty: bool = False,
        branch=None,
    ) -> Dict[str, Any]:
        state = self._load_tasks_state(session_id, branch)
        created = []
        now = datetime.now().isoformat()
        for raw_text in task_texts or []:
            text = str(raw_text or "").strip()
            if not text and not allow_empty:
                continue
            task_id = f"task_{int(state.get('next_id', 1))}"
            state["next_id"] = int(state.get("next_id", 1)) + 1
            task = {
                "id": task_id,
                "text": text,
                "status": "in_progress",
                "created_at": now,
                "updated_at": now,
            }
            state["tasks"].append(task)
            created.append(dict(task))
        if not created:
            return {"success": False, "message": "Не передано ни одной задачи.", "tasks": []}
        self._save_tasks_state(session_id, state, branch)
        return {"success": True, "tasks": created, "state": self.get_tasks_state(session_id, branch)}

    def update_task(
        self,
        session_id: str,
        task_id: str,
        updates: Dict[str, Any],
        *,
        allow_empty_text: bool = False,
        branch=None,
    ) -> Dict[str, Any]:
        state = self._load_tasks_state(session_id, branch)
        target = next((task for task in state["tasks"] if task.get("id") == task_id), None)
        if target is None:
            return {"success": False, "message": f"Задача '{task_id}' не найдена."}
        if "text" in updates:
            text = str(updates.get("text") or "").strip()
            if not text and not allow_empty_text:
                return {"success": False, "message": "Текст задачи не может быть пустым."}
            target["text"] = text
        if "status" in updates:
            status = str(updates.get("status") or "").strip().lower()
            if status not in {"in_progress", "completed"}:
                return {"success": False, "message": "Допустимые статусы: in_progress, completed."}
            target["status"] = status
        target["updated_at"] = datetime.now().isoformat()
        self._save_tasks_state(session_id, state, branch)
        return {"success": True, "task": dict(target), "state": self.get_tasks_state(session_id, branch)}

    def delete_task(self, session_id: str, task_id: str, branch=None) -> Dict[str, Any]:
        state = self._load_tasks_state(session_id, branch)
        before = len(state["tasks"])
        state["tasks"] = [task for task in state["tasks"] if task.get("id") != task_id]
        if len(state["tasks"]) == before:
            return {"success": False, "message": f"Задача '{task_id}' не найдена."}
        self._save_tasks_state(session_id, state, branch)
        return {"success": True, "state": self.get_tasks_state(session_id, branch)}

    def apply_task_statuses(
        self,
        session_id: str,
        statuses: List[Dict[str, Any]],
        *,
        mark_checked: bool = False,
        branch=None,
    ) -> Dict[str, Any]:
        state = self._load_tasks_state(session_id, branch)
        by_id = {str(task.get("id")): task for task in state["tasks"]}
        updated = []
        now = datetime.now().isoformat()
        for item in statuses or []:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if task_id not in by_id or status not in {"in_progress", "completed"}:
                continue
            by_id[task_id]["status"] = status
            by_id[task_id]["updated_at"] = now
            updated.append(task_id)
        if mark_checked:
            state["dirty"] = False
            state["checked_request"] = state.get("last_request", "")
        self._save_tasks_state(session_id, state, branch)
        return {"success": True, "updated": updated, "state": self.get_tasks_state(session_id, branch)}

    def apply_task_status_report(
        self,
        session_id: str,
        reports: List[Dict[str, Any]],
        expected_ids: List[str],
        branch=None,
    ) -> Dict[str, Any]:
        """Validate and atomically apply one explicit task-status report.

        Nothing is written unless the model reports every expected task exactly once,
        reports no extra tasks, and provides an explanation for every unfinished task.
        """
        expected = [str(value).strip() for value in (expected_ids or []) if str(value).strip()]
        expected = list(dict.fromkeys(expected))
        if not expected:
            return {"success": False, "message": "No task IDs were authorized for this status check."}

        state = self._load_tasks_state(session_id, branch)
        by_id = {str(task.get("id") or ""): task for task in state["tasks"]}

        missing_from_store = [task_id for task_id in expected if task_id not in by_id]
        if missing_from_store:
            return {
                "success": False,
                "message": "Task list changed during the check; missing task(s): " + ", ".join(missing_from_store),
            }

        no_longer_active = [
            task_id for task_id in expected
            if str(by_id[task_id].get("status") or "") != "in_progress"
        ]
        if no_longer_active:
            return {
                "success": False,
                "message": "Task list changed during the check; task(s) are no longer in progress: "
                + ", ".join(no_longer_active),
            }

        normalized: Dict[str, Dict[str, Any]] = {}
        for item in reports or []:
            if not isinstance(item, dict):
                return {"success": False, "message": "Every task report must be an object."}
            task_id = str(item.get("id") or "").strip()
            if not task_id:
                return {"success": False, "message": "Every task report must contain an ID."}
            if task_id in normalized:
                return {"success": False, "message": f"Task '{task_id}' was reported more than once."}
            completed = item.get("completed")
            if not isinstance(completed, bool):
                return {"success": False, "message": f"Task '{task_id}' must use a boolean completed value."}
            comment = str(item.get("comment") or "").strip()
            if not completed and not comment:
                return {
                    "success": False,
                    "message": f"Task '{task_id}' is unfinished, so comment must explain what remains.",
                }
            if completed and comment:
                return {
                    "success": False,
                    "message": f"Task '{task_id}' is complete, so comment must be empty.",
                }
            normalized[task_id] = {
                "completed": completed,
                "comment": comment,
            }

        submitted = set(normalized)
        expected_set = set(expected)
        missing = [task_id for task_id in expected if task_id not in submitted]
        extras = sorted(submitted - expected_set)
        if missing or extras:
            parts = []
            if missing:
                parts.append("missing: " + ", ".join(missing))
            if extras:
                parts.append("unexpected: " + ", ".join(extras))
            return {
                "success": False,
                "message": "Status report must cover exactly the authorized tasks (" + "; ".join(parts) + " ).",
            }

        now = datetime.now().isoformat()
        updated = []
        incomplete = []
        for task_id in expected:
            report = normalized[task_id]
            target = by_id[task_id]
            target["status"] = "completed" if report["completed"] else "in_progress"
            target["updated_at"] = now
            updated.append(task_id)
            if not report["completed"]:
                incomplete.append({
                    "id": task_id,
                    "comment": report["comment"],
                })

        state["dirty"] = False
        state["checked_request"] = state.get("last_request", "")
        state["report_count"] = state.get("report_count", 0) + 1
        self._save_tasks_state(session_id, state, branch)
        return {
            "success": True,
            "updated": updated,
            "incomplete": incomplete,
            "state": self.get_tasks_state(session_id, branch),
        }

    def mark_task_activity(self, session_id: str, branch=None, request=None) -> bool:
        import hashlib
        state = self._load_tasks_state(session_id, branch)
        if request is not None:
            data = getattr(request, 'additional_kwargs', {}) or {}
            if data.get('task_status_check'):
                return False
            identity = str(data.get('chat_id') or '') + ':' + hashlib.sha256(str(request.content).encode()).hexdigest()
        else:
            identity = state.get('last_request') or 'legacy'
        if identity != state.get('last_request'):
            state['last_request'] = identity
            state['dirty'] = any(t['status'] == 'in_progress' for t in state['tasks'])
            self._save_tasks_state(session_id, state, branch)
        elif identity != state.get('checked_request') and any(t['status'] == 'in_progress' for t in state['tasks']):
            state['dirty'] = True
            self._save_tasks_state(session_id, state, branch)
        return bool(state.get('dirty'))

    def begin_task_review(self, session_id, branch=None):
        state = self._load_tasks_state(session_id, branch)
        state['checked_request'] = state.get('last_request') or 'legacy'
        state['last_request'] = state.get('last_request') or 'legacy'
        state['dirty'] = False
        self._save_tasks_state(session_id, state, branch)

    def get_dirty_task_sessions(self) -> List[Dict[str, Any]]:
        result = []
        for session_id in sorted(self.list_sessions()):
            for branch, _, _, _ in self.storage(session_id).branch_headers():
                state = self._load_tasks_state(session_id, branch)
                active = [dict(task) for task in state['tasks'] if task['status'] == 'in_progress']
                if not state.get('dirty') or not active:
                    continue
                result.append({'session_id': session_id, 'active_branch': branch, 'branch': branch, 'tasks': active})
        return result

    # =========================================================================
    # PROJECTS / SESSION HIERARCHY
    # =========================================================================

    @property
    def projects_catalog_path(self) -> str:
        return os.path.join(self.sessions_dir, "_projects.json")

    @staticmethod
    def _empty_project_settings() -> Dict[str, Any]:
        return {
            "system_prompt": "",
            "inherit_system_prompt": True,
            "workspace_path": "",
            "assets": {"mode": "toshy", "path": ""},
            "modules": None,
            "store_temp_memory_both": None,
        }

    def _load_projects_catalog(self) -> Dict[str, Any]:
        default = {"version": 2, "folders": {}, "session_folders": {}, "session_settings": {}}
        path = self.projects_catalog_path
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return default
            folders = data.get("folders") if isinstance(data.get("folders"), dict) else {}
            session_folders = data.get("session_folders") if isinstance(data.get("session_folders"), dict) else {}
            session_settings = data.get("session_settings") if isinstance(data.get("session_settings"), dict) else {}
            existing = set(self.list_sessions())
            session_folders = {
                str(session_id): folder_id
                for session_id, folder_id in session_folders.items()
                if str(session_id) in existing and (folder_id is None or str(folder_id) in folders)
            }
            session_settings = {
                str(session_id): self._normalize_folder_settings(settings)
                for session_id, settings in session_settings.items()
                if str(session_id) in existing
            }
            return {
                "version": 2,
                "folders": folders,
                "session_folders": session_folders,
                "session_settings": session_settings,
            }
        except Exception:
            return default

    def _save_projects_catalog(self, catalog: Dict[str, Any]) -> None:
        self._write_json_atomic(self.projects_catalog_path, catalog)

    def _normalize_folder_settings(self, value: Any) -> Dict[str, Any]:
        base = self._empty_project_settings()
        if isinstance(value, dict):
            base["system_prompt"] = str(value.get("system_prompt") or "")
            base["inherit_system_prompt"] = bool(value.get("inherit_system_prompt", True))
            base["workspace_path"] = str(value.get("workspace_path") or "")
            from core.project_assets import normalize_assets
            base["assets"] = normalize_assets(value.get("assets"))
            modules = value.get("modules", None)
            if modules is None:
                base["modules"] = None
            elif isinstance(modules, list):
                base["modules"] = list(dict.fromkeys(str(name) for name in modules if str(name).strip()))
            store_both = value.get("store_temp_memory_both", None)
            base["store_temp_memory_both"] = None if store_both is None else bool(store_both)
        return base

    @staticmethod
    def _empty_branch_project_settings() -> Dict[str, Any]:
        return {
            "system_prompt": "",
            "inherit_system_prompt": True,
            "workspace_path": "",
            "assets": {"mode": "toshy", "path": ""},
            "modules": None,
            "store_temp_memory_both": None,
            "memory_mode": None,
        }

    def _normalize_branch_project_settings(self, value: Any) -> Dict[str, Any]:
        base = self._empty_branch_project_settings()
        if isinstance(value, dict):
            common = self._normalize_folder_settings(value)
            base.update(common)
            mode = str(value.get("memory_mode") or "").strip().lower()
            base["memory_mode"] = mode if mode in {"text", "vector"} else None
        return base

    def get_branch_project_settings(self, session_id: str, branch_name: str) -> Dict[str, Any]:
        clean_branch = re.sub(r'[^a-zA-Z0-9_-]', '_', str(branch_name or '')).lower().strip('_')
        if not clean_branch or clean_branch == "main":
            return self._empty_branch_project_settings()
        if not self.has_branch(session_id, clean_branch):
            raise FileNotFoundError(f"Ветка '{clean_branch}' не найдена в сессии '{session_id}'.")
        payload = self.storage(session_id).read_branch_header(clean_branch)
        metadata = payload.get("metadata") if isinstance(payload, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        return self._normalize_branch_project_settings(metadata.get("branch_project_settings"))

    def update_branch_project_settings(
        self,
        session_id: str,
        branch_name: str,
        settings_updates: Dict[str, Any],
    ) -> Dict[str, Any]:
        clean_branch = re.sub(r'[^a-zA-Z0-9_-]', '_', str(branch_name or '')).lower().strip('_')
        if not clean_branch or clean_branch == "main":
            return {"success": False, "message": "Настройки main задаются на уровне сессии."}
        if not self.has_branch(session_id, clean_branch):
            return {"success": False, "message": f"Ветка '{clean_branch}' не найдена."}
        try:
            payload = self.read_branch_payload(session_id, clean_branch)
            if not isinstance(payload, dict):
                return {"success": False, "message": "Некорректный файл ветки."}
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                payload["metadata"] = metadata
            settings = self._normalize_branch_project_settings(metadata.get("branch_project_settings"))
            incoming = settings_updates if isinstance(settings_updates, dict) else {}
            if "system_prompt" in incoming:
                settings["system_prompt"] = str(incoming.get("system_prompt") or "")
            if "inherit_system_prompt" in incoming:
                settings["inherit_system_prompt"] = bool(incoming.get("inherit_system_prompt"))
            if "workspace_path" in incoming:
                raw_workspace = str(incoming.get("workspace_path") or "").strip()
                if raw_workspace:
                    workspace = Path(raw_workspace).expanduser().resolve()
                    if not workspace.exists() or not workspace.is_dir():
                        return {"success": False, "message": f"Project Folder does not exist: {workspace}"}
                    settings["workspace_path"] = str(workspace)
                else:
                    settings["workspace_path"] = ""
            settings["modules"] = None  # Inherited only from project folders.
            if "store_temp_memory_both" in incoming:
                value = incoming.get("store_temp_memory_both")
                settings["store_temp_memory_both"] = None if value is None else bool(value)
            if "memory_mode" in incoming:
                raw_mode = incoming.get("memory_mode")
                if raw_mode is None or str(raw_mode).strip() == "":
                    settings["memory_mode"] = None
                else:
                    mode = str(raw_mode).strip().lower()
                    if mode not in {"text", "vector"}:
                        return {"success": False, "message": "Memory mode must be 'text', 'vector' or inherit."}
                    settings["memory_mode"] = mode
            metadata["branch_project_settings"] = settings
            self.write_branch_payload(session_id, clean_branch, payload)
            return {"success": True, "settings": settings}
        except Exception as error:
            return {"success": False, "message": f"Не удалось обновить настройки ветки: {error}"}

    def get_project_folders(self) -> List[Dict[str, Any]]:
        catalog = self._load_projects_catalog()
        result = []
        for folder_id, raw in catalog["folders"].items():
            if not isinstance(raw, dict):
                continue
            result.append({
                "id": folder_id,
                "name": str(raw.get("name") or "Folder"),
                "parent_id": raw.get("parent_id"),
                "settings": self._normalize_folder_settings(raw.get("settings")),
            })
        return result

    def _folder_chain(self, folder_id: Optional[str], catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        catalog = catalog or self._load_projects_catalog()
        folders = catalog["folders"]
        chain = []
        seen = set()
        current = folder_id
        while current and current in folders and current not in seen:
            seen.add(current)
            raw = folders[current]
            chain.append({
                "id": current,
                "name": str(raw.get("name") or "Folder"),
                "parent_id": raw.get("parent_id"),
                "settings": self._normalize_folder_settings(raw.get("settings")),
            })
            current = raw.get("parent_id")
        chain.reverse()
        return chain

    def create_project_folder(self, name: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
        catalog = self._load_projects_catalog()
        clean_name = str(name or "").strip() or "New Folder"
        if parent_id and parent_id not in catalog["folders"]:
            return {"success": False, "message": "Parent folder was not found."}
        folder_id = f"folder_{uuid.uuid4().hex}"
        catalog["folders"][folder_id] = {
            "name": clean_name,
            "parent_id": parent_id or None,
            "settings": self._empty_project_settings(),
        }
        self._save_projects_catalog(catalog)
        return {"success": True, "folder": {"id": folder_id, **catalog["folders"][folder_id]}}

    def update_project_folder(self, folder_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        catalog = self._load_projects_catalog()
        folder = catalog["folders"].get(folder_id)
        if not isinstance(folder, dict):
            return {"success": False, "message": "Folder was not found."}

        if "name" in updates:
            name = str(updates.get("name") or "").strip()
            if name:
                folder["name"] = name

        if "parent_id" in updates:
            parent_id = updates.get("parent_id") or None
            if parent_id == folder_id:
                return {"success": False, "message": "A folder cannot contain itself."}
            if parent_id and parent_id not in catalog["folders"]:
                return {"success": False, "message": "Parent folder was not found."}
            # Reject moving a folder into one of its descendants.
            cursor = parent_id
            seen = set()
            while cursor and cursor not in seen:
                if cursor == folder_id:
                    return {"success": False, "message": "A folder cannot be moved into its descendant."}
                seen.add(cursor)
                cursor = (catalog["folders"].get(cursor) or {}).get("parent_id")
            folder["parent_id"] = parent_id

        if "settings" in updates:
            incoming = updates.get("settings") or {}
            settings = self._normalize_folder_settings(folder.get("settings"))
            if isinstance(incoming, dict):
                if "system_prompt" in incoming:
                    settings["system_prompt"] = str(incoming.get("system_prompt") or "")
                if "inherit_system_prompt" in incoming:
                    settings["inherit_system_prompt"] = bool(incoming.get("inherit_system_prompt"))
                if "workspace_path" in incoming:
                    raw_workspace = str(incoming.get("workspace_path") or "").strip()
                    if raw_workspace:
                        workspace = Path(raw_workspace).expanduser().resolve()
                        if not workspace.exists() or not workspace.is_dir():
                            return {"success": False, "message": f"Project Folder does not exist: {workspace}"}
                        settings["workspace_path"] = str(workspace)
                    else:
                        settings["workspace_path"] = ""
                if 'assets' in incoming:
                    from core.project_assets import normalize_assets, assets_root
                    try:
                        settings['assets'] = normalize_assets(incoming['assets'])
                        assets_root(folder_id, settings.get('workspace_path') or APP_PATHS.workspace, settings['assets'], catalog['folders'])
                    except ValueError as error:
                        return {'success': False, 'message': str(error)}
                if "modules" in incoming:
                    modules = incoming.get("modules")
                    settings["modules"] = None if modules is None else list(dict.fromkeys(
                        str(name) for name in (modules or []) if str(name).strip()
                    ))
                if "store_temp_memory_both" in incoming:
                    value = incoming.get("store_temp_memory_both")
                    settings["store_temp_memory_both"] = None if value is None else bool(value)
            folder["settings"] = settings

        self._save_projects_catalog(catalog)
        return {"success": True, "folder": {"id": folder_id, **folder}}

    def delete_project_folder(self, folder_id: str) -> Dict[str, Any]:
        catalog = self._load_projects_catalog()
        folder = catalog["folders"].get(folder_id)
        if not isinstance(folder, dict):
            return {"success": False, "message": "Folder was not found."}
        parent_id = folder.get("parent_id")
        for raw in catalog["folders"].values():
            if isinstance(raw, dict) and raw.get("parent_id") == folder_id:
                raw["parent_id"] = parent_id
        for session_id, assigned in list(catalog["session_folders"].items()):
            if assigned == folder_id:
                if parent_id:
                    catalog["session_folders"][session_id] = parent_id
                else:
                    catalog["session_folders"].pop(session_id, None)
        catalog["folders"].pop(folder_id, None)
        self._save_projects_catalog(catalog)
        return {"success": True}

    def _default_project(self, catalog):
        identity = 'project_inbox'
        if identity not in catalog['folders']:
            catalog['folders'][identity] = {'name': 'Без проекта', 'parent_id': None,
                                           'settings': self._empty_project_settings()}
        return identity

    def reconcile_session_projects(self):
        """Keep legacy/imported chats without a folder inside a real project."""
        catalog = self._load_projects_catalog()
        missing = [identity for identity in self.list_sessions()
                   if catalog['session_folders'].get(identity) not in catalog['folders']]
        if missing:
            folder = self._default_project(catalog)
            for identity in missing:
                catalog['session_folders'][identity] = folder
            self._save_projects_catalog(catalog)
        return catalog

    def set_session_folder(self, session_id: str, folder_id: Optional[str]) -> Dict[str, Any]:
        if session_id not in set(self.list_sessions()):
            return {"success": False, "message": f"Session '{session_id}' was not found."}
        catalog = self._load_projects_catalog()
        folder_id = folder_id or self._default_project(catalog)
        if folder_id not in catalog["folders"]:
            return {"success": False, "message": "Folder was not found."}
        if folder_id:
            catalog["session_folders"][session_id] = folder_id
        else:
            catalog["session_folders"].pop(session_id, None)
        self._save_projects_catalog(catalog)
        return {"success": True, "folder_id": folder_id}

    def update_session_project_settings(self, session_id: str, settings_updates: Dict[str, Any]) -> Dict[str, Any]:
        if session_id not in set(self.list_sessions()):
            return {"success": False, "message": f"Session '{session_id}' was not found."}
        catalog = self._load_projects_catalog()
        settings = self._normalize_folder_settings(catalog["session_settings"].get(session_id))
        incoming = settings_updates if isinstance(settings_updates, dict) else {}
        if "system_prompt" in incoming:
            settings["system_prompt"] = str(incoming.get("system_prompt") or "")
        if "inherit_system_prompt" in incoming:
            settings["inherit_system_prompt"] = bool(incoming.get("inherit_system_prompt"))
        if "workspace_path" in incoming:
            raw_workspace = str(incoming.get("workspace_path") or "").strip()
            if raw_workspace:
                workspace = Path(raw_workspace).expanduser().resolve()
                if not workspace.exists() or not workspace.is_dir():
                    return {"success": False, "message": f"Project Folder does not exist: {workspace}"}
                settings["workspace_path"] = str(workspace)
            else:
                settings["workspace_path"] = ""
        settings["modules"] = None  # Module sets belong to project folders only.
        if "store_temp_memory_both" in incoming:
            value = incoming.get("store_temp_memory_both")
            settings["store_temp_memory_both"] = None if value is None else bool(value)
        catalog["session_settings"][session_id] = settings
        self._save_projects_catalog(catalog)
        return {"success": True, "settings": settings}

    def set_session_memory_mode(self, session_id: str, mode: str):
        clean = str(mode or '').strip().lower()
        if clean not in {'text', 'vector'}:
            return {"success": False, "message": "Memory mode must be 'text' or 'vector'."}
        if not self.has_session(session_id):
            return {"success": False, "message": f"Session '{session_id}' was not found."}
        self.storage(session_id).set_branch_policy('temp_memory_mode', clean)
        from core.memory_preference import remember_memory_mode
        remember_memory_mode(clean)
        return {"success": True, "memory_mode": clean}

    def set_session_include_thoughts_in_context(self, session_id: str, enabled: bool):
        if not self.has_session(session_id):
            return {"success": False, "message": f"Session '{session_id}' was not found."}
        value = bool(enabled)
        self.storage(session_id).set_branch_policy('include_thoughts_in_context', value)
        return {"success": True, "include_thoughts_in_context": value}

    def set_session_include_replied_thoughts_in_context(self, session_id: str, enabled: bool):
        if not self.has_session(session_id):
            return {"success": False, "message": f"Session '{session_id}' was not found."}
        value = bool(enabled)
        self.storage(session_id).set_branch_policy('include_replied_thoughts_in_context', value)
        return {"success": True, "include_replied_thoughts_in_context": value}

    def resolve_session_project_settings(
        self,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        branch: str = "main",
    ) -> Dict[str, Any]:
        catalog = self._load_projects_catalog()
        folder_id = catalog["session_folders"].get(session_id)
        chain = self._folder_chain(folder_id, catalog)
        raw_session_settings = catalog.get("session_settings", {}).get(session_id)
        if raw_session_settings is None:
            # Compatibility with any early build that stored this in session metadata.
            raw_session_settings = (metadata or {}).get("project_settings")
        session_settings = self._normalize_folder_settings(raw_session_settings)

        prompts = []
        prompt_sources = []
        workspace_path = ""
        modules = None
        store_temp_memory_both = False
        for depth, folder in enumerate(chain):
            settings = folder["settings"]
            if not bool(settings.get("inherit_system_prompt", True)):
                prompts.clear()
                prompt_sources.clear()
            prompt = str(settings.get("system_prompt") or "").strip()
            if prompt:
                prompts.append(prompt)
                prompt_sources.append({
                    "kind": "folder",
                    "id": folder["id"],
                    "name": folder["name"],
                    "depth": depth,
                    "prompt": prompt,
                })
            if depth == 0 and str(settings.get("workspace_path") or "").strip():
                workspace_path = str(settings["workspace_path"]).strip()
            if settings.get("modules") is not None:
                modules = list(settings.get("modules") or [])
            if settings.get("store_temp_memory_both") is not None:
                store_temp_memory_both = bool(settings.get("store_temp_memory_both"))

        session_prompt = str(session_settings.get("system_prompt") or "").strip()
        if session_prompt:
            prompts.append(session_prompt)
            prompt_sources.append({
                "kind": "session",
                "id": session_id,
                "name": session_id,
                "depth": len(chain),
                "prompt": session_prompt,
            })
        if session_settings.get("store_temp_memory_both") is not None:
            store_temp_memory_both = bool(session_settings.get("store_temp_memory_both"))

        branch_name = str(branch or "main")
        branch_settings = self._empty_branch_project_settings()
        if branch_name != "main":
            raw_branch_settings = (metadata or {}).get("branch_project_settings")
            branch_settings = self._normalize_branch_project_settings(raw_branch_settings)
            if not bool(branch_settings.get("inherit_system_prompt", True)):
                prompts.clear()
                prompt_sources.clear()
            branch_prompt = str(branch_settings.get("system_prompt") or "").strip()
            if branch_prompt:
                prompts.append(branch_prompt)
                prompt_sources.append({
                    "kind": "branch",
                    "id": f"{session_id}:{branch_name}",
                    "name": branch_name,
                    "depth": len(chain) + 1,
                    "prompt": branch_prompt,
                })
            if branch_settings.get("store_temp_memory_both") is not None:
                store_temp_memory_both = bool(branch_settings.get("store_temp_memory_both"))

        inherited_memory_mode = str((metadata or {}).get("temp_memory_mode") or "text").lower()
        if inherited_memory_mode not in {"text", "vector"}:
            inherited_memory_mode = "text"
        memory_mode = branch_settings.get("memory_mode") if branch_name != "main" else None
        if memory_mode not in {"text", "vector"}:
            memory_mode = inherited_memory_mode

        return {
            "folder_id": folder_id,
            "project_id": chain[0]["id"] if chain else None,
            "project_name": chain[0]["name"] if chain else "",
            "folder_chain": [{"id": item["id"], "name": item["name"]} for item in chain],
            "system_prompt": "\n\n".join(prompts),
            "system_prompt_sources": prompt_sources,
            "workspace_path": workspace_path,
            "modules": modules,
            "store_temp_memory_both": store_temp_memory_both,
            "memory_mode": memory_mode,
            "session_settings": session_settings,
            "branch_settings": branch_settings,
        }

    def get_projects_snapshot(self) -> Dict[str, Any]:
        catalog = self.reconcile_session_projects()
        folders = self.get_project_folders()
        sessions = []

        def recent_user_messages(messages: List[BaseMessage]) -> List[str]:
            result: List[str] = []
            for message in reversed(messages):
                if str(getattr(message, "type", "")) != "human":
                    continue
                text = str(getattr(message, "content", "") or "").strip().replace("\n", " ")
                if text:
                    result.append(text[:280] + ("…" if len(text) > 280 else ""))
                # Projects deliberately exposes only the last three user requests,
                # regardless of the age/length of the session.
                if len(result) >= 3:
                    break
            result.reverse()
            return result

        for session_id in sorted(self.list_sessions()):
            try:
                main_metadata, _ = self.load_session_metadata(session_id, branch="main")
                main_messages = messages_from_dict(self.storage(session_id).read_user_messages('main', limit=3))[::-1]
                active_branch = self._get_active_branch_name(session_id)
                headers = self.storage(session_id).branch_headers()
                modified = {name: updated for name, _, updated, _ in headers}
                message_counts = {name: count for name, _, _, count in headers}
                updated_at = datetime.fromtimestamp(modified["main"]).isoformat()
                digest = main_metadata.get("session_digest") if isinstance(main_metadata.get("session_digest"), dict) else {}
                effective = self.resolve_session_project_settings(session_id, main_metadata, branch="main")
                task_state = self.get_tasks_state(session_id)

                branch_items = []
                for branch_info in self.list_branches(session_id):
                    branch_name = str(branch_info.get("name") or "main")
                    item = dict(branch_info)
                    if branch_name == "main":
                        item.update({
                            "updated_at": updated_at,
                            "recent_user_messages": recent_user_messages(main_messages),
                            "memory_mode": effective["memory_mode"],
                            "project_settings": effective["session_settings"],
                            "effective_settings": {
                                "workspace_path": effective["workspace_path"],
                                "modules": effective["modules"],
                                "store_temp_memory_both": effective["store_temp_memory_both"],
                                "memory_mode": effective["memory_mode"],
                            },
                        })
                    else:
                        branch_metadata, _ = self.load_session_metadata(session_id, branch=branch_name)
                        branch_messages = messages_from_dict(self.storage(session_id).read_user_messages(branch_name, limit=3))[::-1]
                        branch_effective = self.resolve_session_project_settings(
                            session_id, branch_metadata, branch=branch_name
                        )
                        branch_updated = datetime.fromtimestamp(modified[branch_name]).isoformat()
                        item.update({
                            "updated_at": branch_updated,
                            "recent_user_messages": recent_user_messages(branch_messages),
                            "memory_mode": branch_effective["memory_mode"],
                            "project_settings": branch_effective["branch_settings"],
                            "effective_settings": {
                                "workspace_path": branch_effective["workspace_path"],
                                "modules": branch_effective["modules"],
                                "store_temp_memory_both": branch_effective["store_temp_memory_both"],
                                "memory_mode": branch_effective["memory_mode"],
                            },
                        })
                    branch_items.append(item)

                sessions.append({
                    "session_id": session_id,
                    "title": next((b['title'] for b in branch_items if b['name']=='main'), session_id),
                    "folder_id": catalog["session_folders"].get(session_id),
                    "active_branch": active_branch,
                    "memory_mode": effective["memory_mode"],
                    "include_thoughts_in_context": bool(
                        main_metadata.get("include_thoughts_in_context", True)
                    ),
                    "include_replied_thoughts_in_context": bool(main_metadata.get("include_replied_thoughts_in_context", False)),
                    "branches": branch_items,
                    "message_count": message_counts['main'],
                    "updated_at": updated_at,
                    "recent_user_messages": recent_user_messages(main_messages),
                    "project_settings": effective["session_settings"],
                    "effective_settings": {
                        "workspace_path": effective["workspace_path"],
                        "modules": effective["modules"],
                        "store_temp_memory_both": effective["store_temp_memory_both"],
                        "memory_mode": effective["memory_mode"],
                        "folder_chain": effective["folder_chain"],
                    },
                    "digest": digest,
                    "tasks": task_state.get("tasks", []),
                    "tasks_dirty": bool(task_state.get("dirty", False)),
                })
            except Exception as error:
                sessions.append({
                    "session_id": session_id,
                    "error": str(error),
                    "folder_id": catalog["session_folders"].get(session_id),
                })
        return {"folders": folders, "sessions": sessions}

    # =========================================================================
    # Работа с сессиями
    # =========================================================================

    def get_next_default_session_id(self) -> str:
        """Находит следующий свободный номер session_N на диске."""
        existing = set(self.list_sessions())
        idx = 1
        while f"session_{idx}" in existing:
            idx += 1
        return f"session_{idx}"

    def get_next_project_session_number(self, folder_id=None) -> int:
        """Number new chats within the root project, including its subfolders.

        Storage IDs stay globally unique. The number survives automatic title
        generation in metadata, so renaming a conversation never frees a slot.
        """
        catalog = self.reconcile_session_projects()
        folder_id = folder_id or self._default_project(catalog)
        if folder_id not in catalog['folders']:
            raise ValueError('Folder was not found.')
        project = self._folder_chain(folder_id, catalog)[0]['id']
        used = set()
        for identity, assigned in catalog['session_folders'].items():
            chain = self._folder_chain(assigned, catalog)
            if not chain or chain[0]['id'] != project:continue
            metadata, _ = self.load_session_metadata(identity, branch='main')
            number = metadata.get('project_session_number')
            if type(number) is not int or number < 1:
                legacy = re.fullmatch(r'session_(\d+)', identity)
                number = int(legacy[1]) if legacy else None
            if number is not None:used.add(number)
        number = 1
        while number in used:number += 1
        return number

    def set_default_session_title(self, session_id, number):
        from core.session_titles import PREFIX
        title = f'session_{number}'
        with self.storage(session_id).connection(write=True, create=False) as db:
            SQLiteStore._set_state(db, PREFIX+'main', {'title':title, 'manual':False, 'default':True})
        return title

    def rename_session(self, old_session_id: str, new_session_id: str) -> Dict[str, Any]:
        """Переименовывает директорию сессии."""
        clean_new = "".join([c for c in new_session_id if c.isalnum() or c in "_-"]).strip()
        if not clean_new:
            return {"success": False, "message": "Некорректное имя сессии."}

        old_dir = self.get_session_dir(old_session_id, create=False)
        new_dir = os.path.join(self.sessions_dir, clean_new)

        if not os.path.exists(old_dir):
            return {"success": False, "message": f"Сессия '{old_session_id}' не найдена."}
        if os.path.exists(new_dir) and old_dir != new_dir:
            return {"success": False, "message": f"Сессия '{clean_new}' уже существует."}

        moved = False
        try:
            catalog = self._load_projects_catalog()
            os.rename(old_dir, new_dir)
            moved = old_dir != new_dir
            self.storage(clean_new).rename_session(clean_new)
            changed_catalog = False
            if old_session_id in catalog["session_folders"]:
                catalog["session_folders"][clean_new] = catalog["session_folders"].pop(old_session_id)
                changed_catalog = True
            if old_session_id in catalog.get("session_settings", {}):
                catalog["session_settings"][clean_new] = catalog["session_settings"].pop(old_session_id)
                changed_catalog = True
            if changed_catalog:
                self._save_projects_catalog(catalog)
            return {"success": True, "new_session_id": clean_new, "message": f"Сессия переименована в '{clean_new}'."}
        except Exception as e:
            if moved:
                try:
                    self.storage(clean_new).rename_session(old_session_id)
                    os.rename(new_dir, old_dir)
                except Exception as rollback_error:
                    return {"success": False, "message": f"Ошибка переименования: {e}; данные остались в '{clean_new}': {rollback_error}"}
            return {"success": False, "message": f"Ошибка переименования: {e}"}

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        """Удаляет папку сессии со всеми ветками и планами."""
        s_dir = self.get_session_dir(session_id, create=False)
        if not os.path.exists(s_dir):
            return {"success": False, "message": f"Сессия '{session_id}' не найдена."}
        try:
            import shutil
            catalog = self._load_projects_catalog()
            shutil.rmtree(s_dir)
            changed_catalog = False
            if session_id in catalog["session_folders"]:
                catalog["session_folders"].pop(session_id, None)
                changed_catalog = True
            if session_id in catalog.get("session_settings", {}):
                catalog["session_settings"].pop(session_id, None)
                changed_catalog = True
            if changed_catalog:
                self._save_projects_catalog(catalog)
            return {"success": True, "message": f"Сессия '{session_id}' полностью удалена."}
        except Exception as e:
            return {"success": False, "message": f"Ошибка удаления сессии: {e}"}

    def rename_branch(self, session_id: str, old_name: str, new_name: str):
        old = self.clean_branch_name(old_name)
        new = re.sub(r'[^a-zA-Z0-9_-]', '_', new_name).lower().strip('_')
        if not new or old == 'main' or new == 'main':
            return {"success": False, "message": "Некорректное имя ветки."}
        if not self.has_branch(session_id, old):
            return {"success": False, "message": f"Ветка '{old}' не найдена."}
        if self.has_branch(session_id, new):
            return {"success": False, "message": f"Ветка '{new}' уже существует."}
        try:
            self.storage(session_id).rename_branch(old, new)
            return {"success": True, "branch": new, "message": f"Ветка переименована в '{new}'."}
        except Exception as error:
            return {"success": False, "message": f"Ошибка переименования ветки: {error}"}
