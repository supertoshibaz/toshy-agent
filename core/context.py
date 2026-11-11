import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Optional, Tuple

from core.paths import APP_PATHS


def sanitize_session_id(session_id: Optional[str]) -> str:
    if not session_id:
        return "_default"
    # Match SessionManager exactly, including Unicode names. Otherwise the
    # chat API and a run could silently open two different session databases.
    safe = ''.join(c for c in str(session_id) if c.isalnum() or c in '_-').strip()
    return safe or "default_session"


@dataclass(frozen=True)
class ExecutionContext:
    """Неизменяемый паспорт одного запуска агента или субагента."""

    run_id: str
    session_id: Optional[str]
    branch: str
    workspace_root: Path
    temp_memory_mode: str = "text"
    store_temp_memory_both_formats: bool = False
    limit_ranking: bool = True
    include_thoughts_in_context: bool = True
    project_system_prompt: str = ""
    knowledge_base_snapshot_text: str = ""
    allowed_modules: Optional[Tuple[str, ...]] = None
    selected_pipeline: str = ""
    model_profile_id: str = ""
    task_status_check: bool = False
    task_status_expected_ids: Tuple[str, ...] = ()
    resume_run_id: str = ""
    resume_mode: str = "native"
    raw_request: bool = False
    parent_run_id: str = ''
    subagent_id: str = ''
    include_replied_thoughts_in_context: bool = False
    reasoning_effort: str = ""
    full_web_pages: bool = False

    @classmethod
    def create(
        cls,
        session_id: Optional[str] = None,
        branch: str = "main",
        workspace_root: Optional[str] = None,
        temp_memory_mode: str = "text",
        store_temp_memory_both_formats: bool = False,
        limit_ranking: bool = True,
        include_thoughts_in_context: bool = True,
        include_replied_thoughts_in_context: bool = False,
        project_system_prompt: str = "",
        knowledge_base_snapshot_text: str = "",
        allowed_modules: Optional[Tuple[str, ...]] = None,
        selected_pipeline: str = "",
        model_profile_id: str = "",
        task_status_check: bool = False,
        task_status_expected_ids: Optional[Tuple[str, ...]] = None,
        raw_request: bool = False,
        full_web_pages: bool = False,
    ) -> "ExecutionContext":
        root = Path(workspace_root or APP_PATHS.workspace).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"Project Folder не существует или не является каталогом: {root}")
        mode = temp_memory_mode if temp_memory_mode in {"text", "vector"} else "text"
        return cls(
            run_id=uuid.uuid4().hex,
            session_id=session_id,
            branch=branch or "main",
            workspace_root=root,
            temp_memory_mode=mode,
            store_temp_memory_both_formats=bool(store_temp_memory_both_formats),
            limit_ranking=bool(limit_ranking),
            include_thoughts_in_context=bool(include_thoughts_in_context),
            include_replied_thoughts_in_context=bool(include_replied_thoughts_in_context),
            project_system_prompt=str(project_system_prompt or ""),
            knowledge_base_snapshot_text=str(knowledge_base_snapshot_text or ""),
            allowed_modules=tuple(allowed_modules) if allowed_modules is not None else None,
            selected_pipeline=str(selected_pipeline or ""),
            model_profile_id=str(model_profile_id or ""),
            raw_request=bool(raw_request),
            full_web_pages=bool(full_web_pages),
            task_status_check=bool(task_status_check),
            task_status_expected_ids=tuple(dict.fromkeys(
                str(value).strip() for value in (task_status_expected_ids or ()) if str(value).strip()
            )),
        )

    @property
    def safe_session_id(self) -> str:
        return sanitize_session_id(self.session_id) if self.session_id else f"_run_{self.run_id}"

    @property
    def storage_dir(self) -> Path:
        """Хранилище сессии либо изолированное временное хранилище запуска."""
        if self.session_id:
            return APP_PATHS.sessions / sanitize_session_id(self.session_id)
        return APP_PATHS.runtime / "runs" / self.run_id

    def child(self) -> "ExecutionContext":
        # Application-only authorizations never propagate to sub-runs/subagents.
        return replace(
            self,
            run_id=uuid.uuid4().hex,
            parent_run_id=self.run_id,
            selected_pipeline="",
            task_status_check=False,
            task_status_expected_ids=(),
            resume_run_id="",
            resume_mode="native",
            full_web_pages=False,
        )


_DEFAULT_CONTEXT = ExecutionContext(
    run_id="default",
    session_id=None,
    branch="main",
    workspace_root=APP_PATHS.workspace.resolve(),
)
_CURRENT_CONTEXT: ContextVar[ExecutionContext] = ContextVar(
    "toshy_execution_context",
    default=_DEFAULT_CONTEXT,
)


def get_execution_context() -> ExecutionContext:
    return _CURRENT_CONTEXT.get()


@contextmanager
def bind_execution_context(context: ExecutionContext) -> Iterator[None]:
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)
