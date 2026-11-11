from typing import Dict, Any, List
from core.registry import BaseTool
from core.paths import APP_PATHS
from core.session_manager import SessionManager
from core.context import get_execution_context


class ReportTaskStatusesTool(BaseTool):
    """Structured status report used only for an explicit application-triggered check."""

    def __init__(self):
        super().__init__(
            name="report_task_statuses",
            description=(
                "Report the completion state of tasks ONLY when the user message explicitly asks you to "
                "check/update task statuses and supplies the task IDs to review. This tool does not let you "
                "read the task list; it only accepts a report for the tasks authorized by the current check. "
                "Submit every listed task exactly once. Set completed=true only when that task is fully done. "
                "If anything remains unfinished, unresolved, deferred, or still requires user input or future work, "
                "set completed=false and explain what remains in comment. For completed=true, comment MUST be an empty "
                "string. After a successful call, the final user-visible response must mention ONLY incomplete tasks and "
                "what remains unfinished; do not mention or comment on completed tasks and do not add unrelated text. "
                "If every task is complete, reply only that there are no unfinished tasks. Do not call this tool during "
                "ordinary conversation or ordinary work."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Exact task ID from the current status-check message.",
                                },
                                "completed": {
                                    "type": "boolean",
                                    "description": "True only if the task is completely finished.",
                                },
                                "comment": {
                                    "type": "string",
                                    "description": (
                                        "For completed=false, briefly state what remains unfinished. "
                                        "For completed=true, this MUST be an empty string."
                                    ),
                                },
                            },
                            "required": ["id", "completed", "comment"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        )
        self.sessions = SessionManager(sessions_dir=str(APP_PATHS.sessions))

    async def execute(self, tasks: List[Dict[str, Any]], **kwargs) -> str:
        context = get_execution_context()
        if not context.session_id:
            return "System Error: report_task_statuses requires an active saved session."
        if not context.task_status_check or not context.task_status_expected_ids:
            return (
                "System Error: report_task_statuses is only valid during an explicit task-status check "
                "started by the application. Do not use it in ordinary conversation."
            )

        result = self.sessions.apply_task_status_report(
            context.session_id,
            list(tasks or []),
            list(context.task_status_expected_ids),
            branch=context.branch,
        )
        if not result.get("success"):
            return (
                "System Error: task status report was rejected and no task statuses were changed. "
                f"Reason: {result.get('message', 'Invalid task status report.')} "
                "Correct the report and call report_task_statuses again before giving the final answer."
            )

        incomplete = list(result.get("incomplete") or [])
        if incomplete:
            return (
                f"System: Task status report accepted atomically for {len(result.get('updated') or [])} task(s). "
                "The application task list has been updated. Now give one final response containing ONLY the "
                "incomplete tasks and what remains unfinished. Do not mention completed tasks or add extra commentary."
            )
        return (
            f"System: Task status report accepted atomically for {len(result.get('updated') or [])} task(s). "
            "The application task list has been updated. Reply only: Незавершённых задач нет."
        )

# Memory tools live in the always-on core system module. Re-export the
# classes here for compatibility with older imports.
from core.memory.tools import (
    RememberThisTool,
    ForgetThisTool,
    SearchMemoryTool,
    ListMemoriesTool,
    SearchTempMemoryTool,
    ListKnowledgeBasesTool,
    SearchKnowledgeBaseTool,
)
