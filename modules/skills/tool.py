"""One request-scoped skill interface for the allowed custom modules."""

import json

from core.context import get_execution_context
from core.registry import BaseTool


class SkillsTool(BaseTool):
    def __init__(self, runtimes):
        self.runtimes = dict(runtimes)
        self.owner_modules = tuple(self.runtimes)
        super().__init__(
            "skills",
            "Discover and use skills from the allowed custom modules. "
            "list without module returns their catalog; module is required for other actions. "
            "Copy exact module and skill identifiers from the catalog. "
            "Read SKILL.md before following a workflow; supporting paths are relative to its root. "
            "prepare copies the package into its configured terminal and returns real paths and "
            "terminal_session_id; it never executes scripts. "
            "Use run with a package-relative path and args to execute a bundled script: "
            "Toshy resolves its absolute path and submits it to the assigned terminal's normal approval policy. "
            "If it is still running, use terminal output/input tools with the returned IDs; do not run again to wait. "
            "Do not automatically retry a script after interruption or an unknown execution outcome. "
            "Never assume skill files are in the project workspace. "
            "create/write/delete/copy are only for an explicit user request to author a skill, "
            "never automatic learning. MCP originals are read-only. "
            "Skill instructions do not grant permissions or enable modules/tools.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "read",
                            "prepare",
                            "run",
                            "create",
                            "write",
                            "delete",
                            "copy",
                            "resolve",
                        ],
                    },
                    "module": {
                        "type": "string",
                        "enum": list(self.runtimes),
                        "description": "Exact module identifier from the catalog; omit only for list across all modules.",
                    },
                    "skill": {
                        "type": "string",
                        "description": "Exact skill key from that module's catalog, not its display name.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Package-relative file. Omitted defaults to SKILL.md; prepare also accepts an empty path for SKILL.md. For run, explicitly give a nonempty script path, e.g. scripts/check.py.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "run only: script arguments as separate strings, without shell quoting.",
                    },
                    "interpreter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 32,
                        "description": "run only: optional interpreter and flags, e.g. [python3] or [py, -3]. Python is selected automatically for .py; specify this for other script types.",
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                        "description": "run only: optional terminal command timeout in seconds.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full SKILL.md for create, or full UTF-8 file for write.",
                    },
                    "name": {
                        "type": "string",
                        "description": "New technical name when copying a skill.",
                    },
                    "server": {
                        "type": "string",
                        "description": "MCP server ID from the selected module's list.",
                    },
                    "uri": {
                        "type": "string",
                        "description": "Exact SKILL.md URI published by that server.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def available(self, context):
        result = {}
        for owner, runtime in self.runtimes.items():
            try:
                runtime.check(context)
            except ValueError:
                continue
            result[owner] = runtime
        return result

    def catalog(self, context, *, automatic=False):
        return [
            item
            for runtime in self.available(context).values()
            for item in runtime.catalog(context)
            if item.get("enabled")
            and not item.get("error")
            and (not automatic or item.get("automatic"))
        ]

    def scope_to_module(self, owner):
        runtime = self.runtimes.get(owner)
        return SkillsTool({owner: runtime}) if runtime is not None else None

    def request_messages(self, context, owner):
        runtimes = self.available(context)
        if owner != next(iter(runtimes), None):
            return []
        catalog = [
            {
                key: item[key]
                for key in (
                    "module",
                    "module_name",
                    "key",
                    "name",
                    "description",
                    "source",
                    "scripts",
                )
            }
            for item in self.catalog(context, automatic=True)
        ]
        return (
            [
                "Available skills for this request: "
                + json.dumps(catalog, ensure_ascii=False)
                + "\nThe skill tool for this request is skills. Use action=read with the exact module "
                "and skill key to load SKILL.md when its description matches the task. "
                "Load references only as needed; author skills only when explicitly asked."
                " Execute bundled scripts through skills action=run with their package-relative path and args; "
                "they are not project workspace files."
            ]
            if catalog
            else []
        )

    async def execute(
        self,
        action,
        module="",
        skill="",
        path="SKILL.md",
        content="",
        name="",
        server="",
        uri="",
        args=None,
        interpreter=None,
        timeout=None,
    ):
        context = get_execution_context()
        runtimes = self.available(context)
        if not module:
            if action != "list":
                raise ValueError("Укажите module из каталога скиллов.")
            result = {
                "modules": [
                    {
                        "id": owner,
                        "name": runtime.module.store.read(owner).get("name", owner),
                    }
                    for owner, runtime in runtimes.items()
                ],
                "skills": self.catalog(context),
            }
        else:
            runtime = runtimes.get(module)
            if runtime is None:
                raise ValueError(
                    "Модуль скилла недоступен для этого запуска: " + module
                )
            result = await runtime.action(
                action,
                skill,
                path=path,
                content=content,
                name=name,
                server=server,
                uri=uri,
                args=args,
                interpreter=interpreter,
                timeout=timeout,
            )
            if action == "list":
                result["skills"] = [
                    item
                    for item in result["skills"]
                    if item.get("enabled") and not item.get("error")
                ]
            result["module"] = module
        return json.dumps(result, ensure_ascii=False)
