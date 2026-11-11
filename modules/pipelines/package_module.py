from contextlib import asynccontextmanager
from pathlib import Path
import hashlib
import re
from core.registry import BaseModule, BaseTool
from core.context import get_execution_context
from .packages import PackageStore
from .storage import load_pipeline_script
from .contracts import interface, schema_for


class PipelineTool(BaseTool):
    def __init__(self, module, pid, item, definition):
        self.module, self.pipeline_id = module, pid
        name = f"pipeline_{module}_{pid}"
        if len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            # Human-readable Unicode folders still need an ASCII provider name.
            label = re.sub(r"[^A-Za-z0-9_]+", "_", module).strip("_") or "module"
            digest = hashlib.sha256(f"{module}\0{pid}".encode()).hexdigest()[:16]
            name = f"pipeline_{label[:30]}_{digest}"
        super().__init__(
            name,
            item.get("description") or item["name"],
            schema_for(interface(definition)[0]),
        )

    async def execute(self, **kwargs):
        service = getattr(self.registry, "pipeline_service", None)
        if not service:
            raise RuntimeError("Pipeline runtime ещё не подключён")
        return await service.run(self.module, self.pipeline_id, kwargs)


class PackageModule(BaseModule):
    def __init__(self, directory):
        self.directory = Path(directory)
        from core.plugins.modules import module_locations

        published = next(
            (
                name
                for (category, name), path in module_locations(
                    category="custom"
                ).items()
                if path.resolve() == self.directory.resolve()
            ),
            None,
        )
        self.module_name = published or self.directory.name
        self.store = (
            PackageStore() if published else PackageStore(self.directory.parent)
        )
        self.registry = None
        from modules.skills.runtime import SkillRuntime

        self.skills = SkillRuntime(self)

    def set_context(self, registry):
        self.registry = registry

    def get_system_prompt_addon(self):
        data = self.store.read(self.module_name)
        # Older packages used their description as the prompt. Preserve that
        # until the user explicitly saves the separate General field.
        return data.get("system_prompt", data.get("description", ""))

    def get_tools(self):
        result = []
        for pid, item in self.store.read(self.module_name).get("pipelines", {}).items():
            if item.get("mode") != "tool":
                continue
            try:
                tool = PipelineTool(
                    self.module_name,
                    pid,
                    item,
                    load_pipeline_script(
                        self.store.resolve(self.module_name, pid) / "pipeline.py"
                    ),
                )
                tool.registry = self.registry
                result.append(tool)
            except Exception:
                # A corrupt resource must not prevent the package from loading;
                # its catalog entry will expose the concrete validation error.
                continue
        return result

    def filter_execution_tools(self, tools, allowed_modules):
        from dataclasses import replace
        from modules.skills.tool import SkillsTool

        runtimes = {
            name: module.skills
            for name, module in self.registry.loaded_modules.items()
            if isinstance(module, PackageModule)
            and (allowed_modules is None or name in allowed_modules)
        }
        if not runtimes or self.skills is not next(iter(runtimes.values())):
            return tools
        tool = SkillsTool(runtimes)
        context = replace(get_execution_context(), allowed_modules=allowed_modules)
        if not tool.catalog(context):
            return tools
        if "skills" in tools:
            raise ValueError("Имя инструмента skills занято другим модулем.")
        return {**tools, tool.name: tool}

    def get_request_messages(self, context, tools):
        from modules.skills.tool import SkillsTool

        tool = tools.get("skills")
        return (
            tool.request_messages(context, self.module_name)
            if isinstance(tool, SkillsTool)
            else []
        )

    def get_resource_catalog(self, kind, context=None):
        return self.skills.catalog(context) if kind == "skills" else []

    async def get_model_context(self, context, messages, tools, *, preview=False):
        return await self.skills.model_context(
            context, messages, tools, preview=preview
        )

    @asynccontextmanager
    async def execution_context_scope(self, context, tools):
        async with self.skills.scope(context):
            yield

    async def handle_request(self, request, path="", *, session_manager=None):
        from modules.skills.api import handle_request

        return await handle_request(self.skills, request, path, session_manager)
