from typing import Any, Dict, List

from core.context import get_execution_context
from core.paths import APP_PATHS
from core.registry import BaseModule, BaseTool
from core.session_manager import SessionManager


class CreateTasksTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="create_tasks",
            description=(
                "Append new concrete work items to the current branch task list. Use this tool when the user "
                "introduces a new actionable task or an independent branch of existing work. Create entries "
                "proactively; the user does not need to explicitly ask to add them to the task list. "
                "Keep already-recorded tasks and do not duplicate them. "
                "Do not use it when the user merely asks a question, requests an explanation, "
                "or invites discussion. Phrases such as 'let's discuss' are not work by themselves; phrases such "
                "as 'let's implement/build/change X' usually are when they contain a concrete action. Each task "
                "must describe one specific, self-contained piece of work with the important implementation "
                "details from the user's request (for example: 'Implement X using Y on top of Z'), never a vague "
                "label such as 'Work on feature'. Never merge two independent jobs into one task: if the user "
                "asks to do X and separately do Y, create two list items. Do not create subtasks or hierarchy; "
                "the task list is flat. Choose the number of tasks from the actual request. This tool only appends "
                "new tasks; it never replaces, edits, completes, or deletes existing tasks. Do not use it for a "
                "task-status review. After a successful call, continue performing the requested work in the "
                "same turn. Creating a task records work; it does not complete it. Stop after task creation only if the user explicitly asked to only record or plan tasks."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                        "description": "Flat list of specific, independent task descriptions to append.",
                    }
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        )
        self.sessions = SessionManager(sessions_dir=str(APP_PATHS.sessions))

    async def execute(self, tasks: List[str], **kwargs) -> str:
        context = get_execution_context()
        if not context.session_id:
            return "System Error: create_tasks requires an active saved session."
        result = self.sessions.add_tasks(context.session_id, list(tasks or []), branch=context.branch)
        if not result.get("success"):
            return f"System Error: {result.get('message', 'Unable to create tasks.')}"
        created = result.get("tasks", [])
        return (
            f"System: Successfully appended {len(created)} new task(s) to the current branch task list. "
            "Existing tasks were left unchanged. Continue the requested work now, unless the user explicitly asked only to record or plan tasks."
        )


class Module(BaseModule):
    def __init__(self):
        self._tools = [CreateTasksTool()]

    def get_tools(self) -> List[BaseTool]:
        return self._tools

    async def get_model_context(self, context, messages, tools, *, preview=False):
        if not context.session_id:
            return []
        import asyncio
        import json
        state = await asyncio.to_thread(self._tools[0].sessions.get_tasks_state, context.session_id, context.branch)
        return ['The task list belongs only to the current branch (' + context.branch + '). '
                'Tasks mentioned in inherited conversation history belong to other branches unless listed here. '
                'Current task list:\n' + json.dumps(state['tasks'], ensure_ascii=False)]


    def get_system_prompt_addon(self) -> str:
        return (
            "Task planning: actively record concrete new assignments with create_tasks whenever the user "
            "introduces new work or a new actionable branch of an existing task. Do not wait for an explicit "
            "request to add an item to the list. Capture independent assignments and branches as separate "
            "flat tasks, preserving the user's important requirements and implementation details. "
            "Do not recreate tasks already recorded in the conversation; a clarification without additional "
            "work is not a new task. Questions, explanations, brainstorming and explicit discussion do not "
            "create tasks unless the user also assigns concrete work. During a task-status review, only "
            "review existing tasks and never call create_tasks. After successfully creating tasks, "
            "continue the requested work in the same turn. Stop after recording tasks only if the user explicitly requested recording or planning without execution."
        )
