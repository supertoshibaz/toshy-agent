from core.logger import core_logger
import os
import yaml
import importlib
import asyncio
import sys
from contextlib import asynccontextmanager, AsyncExitStack, contextmanager, ExitStack, nullcontext
from pathlib import Path
from typing import AsyncIterator, List, Dict, Any, Optional, Tuple
from core.memory.manager import MemoryManager
from core.context import ExecutionContext, bind_execution_context
from core.embeddings import EmbeddingError
from core.paths import APP_PATHS


class BaseTool:
    model_visible = True
    catalog_hidden = False
    history_read_only = False
    history_managed = False
    parallel_group = ""
    # Opt in only for operations that can safely be repeated after an unknown outcome.
    repeat_after_interruption = False
    result_metadata = {}

    def get_sources(self, args, output, workspace):
        return []

    def get_error(self, output):
        """Optional module-owned decoder for tool-specific error results."""
        return None

    def __init__(self, name: str, description: str, args_schema: Dict[str, Any]):
        self.name = name
        self.description = description
        self.args_schema = args_schema

    async def execute(self, **kwargs) -> str:
        raise NotImplementedError("Каждый инструмент должен реализовывать метод execute.")

    def history_paths(self, args, context):
        """None — неизвестный набор файлов, [] — инструмент их не меняет."""
        return [] if self.history_read_only or self.history_managed else None

    def reset(self):
        pass

    def for_execution(self, context):
        """Return a run-local tool when its schema depends on session settings."""
        return self


class BaseModule:
    def dispatch_scope(self, context, tools, messages, emit, *, call_id=""):
        return nullcontext()

    def refresh_request_guidance(self, frozen, context, tools):
        pass

    async def deactivate(self) -> None:
        """Immediately release services when the module is switched off."""

    async def modules_changed(self) -> None:
        """React to changes in the loaded module set."""

    def refresh_tools(self) -> None:
        pass

    def filter_execution_tools(self, tools, allowed_modules):
        return tools

    @asynccontextmanager
    async def execution_scope(self, tools):
        yield

    @asynccontextmanager
    async def execution_context_scope(self, context, tools):
        """Reserve context-owned services for the complete run, without starting them."""
        yield

    def get_request_messages(self, context, tools):
        return []

    def get_resource_catalog(self, kind, context=None):
        return []

    async def get_model_context(self, context, messages, tools, *, preview=False):
        """Return request-scoped instructions restored outside lossy compaction."""
        return []

    def allows_public_request(self, request) -> bool:
        return False

    async def async_init(self) -> None:
        pass

    def get_tools(self) -> List[BaseTool]:
        return []

    def get_system_prompt_addon(self) -> str:
        return ""

    def get_knowledge_bases(self) -> List[Any]:
        """Return additional knowledge-base paths or descriptor dictionaries.

        SQLite files inside the module's conventional ``knowledge/`` directory are
        discovered automatically. This hook remains useful for external paths or
        descriptor overrides such as a custom name/manifest. Every returned file
        is still mounted as an independent knowledge base.
        """
        return []

    async def shutdown(self) -> None:
        pass


class ModuleRegistry:
    def __init__(self, config_path: Optional[str] = None, memory: Optional[MemoryManager] = None):
        self.config_path = str(Path(config_path or APP_PATHS.config).resolve())
        self.config = self._load_config()
        self.memory = memory or MemoryManager()
        self.tools: Dict[str, BaseTool] = {}
        self.system_prompt_addons: List[str] = []
        self.loaded_modules: Dict[str, BaseModule] = {}
        self.module_categories: Dict[str, str] = {}
        self.module_tools_map: Dict[str, List[str]] = {}
        self.core_tool_names: List[str] = []
        self.core_components = []
        self.known_module_tools: Dict[str, List[Dict[str, str]]] = {}
        self._mutation_lock = asyncio.Lock()
        self._active_executions = 0
        self._pending_shutdown: List[Tuple[str, BaseModule]] = []
        self._pending_changes = {}

        from core.plugins.runtime import get_runtime, bootstrap
        from core.plugins.modules import install_module_finder
        existing = get_runtime()
        self.plugins = existing if existing is not None and existing.domain == 'core' else bootstrap('core')
        install_module_finder(self.plugins)
        self.plugins.prepare(self)
        self._load_all_modules()

    def _load_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Файл конфигурации не найден: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _knowledge_owner(module_name: str, category: str) -> str:
        prefix = "system" if category == "Системный" else "custom"
        return f"{prefix}.{module_name}"

    def _mount_module_knowledge_bases(
        self,
        module_name: str,
        module_instance: BaseModule,
        category: str,
    ) -> None:
        # Explicit declarations remain supported (including descriptor dicts and
        # paths outside the module), but every SQLite file placed in the module's
        # conventional knowledge/ directory is also a database automatically.
        # New databases become available without editing the module source.
        kb_entries = list(module_instance.get_knowledge_bases() or [])

        category_dir = "system" if category == "Системный" else "custom"
        from core.plugins.modules import module_directory
        knowledge_dir = module_directory(category_dir, module_name) / 'knowledge'
        try:
            discovered = sorted(knowledge_dir.glob("*.sqlite"), key=lambda path: path.name.casefold())
        except OSError:
            discovered = []

        declared_paths = set()
        for entry in kb_entries:
            raw_path = entry.get("path") if isinstance(entry, dict) else entry
            if raw_path:
                try:
                    declared_paths.add(str(Path(raw_path).expanduser().resolve()))
                except Exception:
                    pass

        for path in discovered:
            resolved = str(path.resolve())
            if resolved not in declared_paths:
                kb_entries.append(resolved)
                declared_paths.add(resolved)

        self.memory.mount_external_kb(
            module_name,
            kb_entries,
            owner=self._knowledge_owner(module_name, category),
        )

    def refresh_module_knowledge_bases(self, module_name: str) -> List[Dict[str, Any]]:
        """Reload one active module's KB files without changing its tools/prompts."""
        module_instance = self.loaded_modules.get(module_name)
        if module_instance is None:
            return []
        category = self.module_categories.get(module_name, "Пользовательский")
        self._mount_module_knowledge_bases(module_name, module_instance, category)
        return [
            item
            for item in self.memory.list_knowledge_bases(use_execution_context=False)
            if item.get("module") == module_name
        ]

    def _import_and_register(self, module_path: str, module_name: str, category: str):
        try:
            imported_lib = importlib.import_module(module_path)
            if not hasattr(imported_lib, "Module"):
                core_logger.event('modules', 'MODULES_EVENT', f"[Core Warning] Module '{module_name}' has no class 'Module'.", level='WARN')
                return

            module_instance: BaseModule = imported_lib.Module()

            if hasattr(module_instance, "set_context"):
                module_instance.set_context(self)

            module_tools = list(module_instance.get_tools())
            for tool in module_tools:tool._dispatch_module = module_instance
            for tool in module_tools:
                if tool.name in self.tools:
                    raise ValueError(f"Конфликт имен инструментов! '{tool.name}' уже зарегистрирован.")

            self.loaded_modules[module_name] = module_instance
            self.module_categories[module_name] = category
            self.module_tools_map[module_name] = []
            for tool in module_tools:
                self.tools[tool.name] = tool
                self.module_tools_map[module_name].append(tool.name)
            self._remember_module_tools(module_name, module_tools)

            addon = self._module_call(module_instance, 'get_system_prompt_addon', fallback='')
            if addon:
                self.system_prompt_addons.append(addon)

            # Every module KB stays an independent database namespace.
            self._mount_module_knowledge_bases(module_name, module_instance, category)

            tools_count = len(module_tools)
            core_logger.event('modules', 'MODULES_EVENT', f"[Core Registry] {'system' if category == 'Системный' else 'custom'} module '{module_name}' loaded. Tools: {tools_count}", level='INFO')

        except Exception as e:
            self._discard_module(module_name)
            from core.plugins.modules import plugin_modules
            runtime = getattr(self, 'plugins', None)
            item = plugin_modules(runtime).get(('system' if category == 'Системный' else 'custom', module_name))
            if runtime is not None and item is not None:
                runtime.failure(item['component'], e)
            core_logger.event('modules', 'MODULES_EVENT', f"[Core Error] Failed to load {'system' if category == 'Системный' else 'custom'} module '{module_name}': {e}", level='ERROR')

    def _load_all_modules(self):
        system_modules = self.config.get("system_modules", [])
        for mod_name in system_modules:
            self._import_and_register(f"modules.system.{mod_name}", mod_name, category="Системный")

        custom_modules = self.config.get("custom_modules", [])
        for mod_name in custom_modules:
            self._import_and_register(f"modules.custom.{mod_name}", mod_name, category="Пользовательский")

    async def async_init_all(self):
        for mod_name, module_instance in list(self.loaded_modules.items()):
            try:
                await module_instance.async_init()
                for tool in module_instance.get_tools():
                    tool._dispatch_module = module_instance
                    if tool.name not in self.tools:
                        self.tools[tool.name] = tool
                        if tool.name not in self.module_tools_map.setdefault(mod_name, []):
                            self.module_tools_map[mod_name].append(tool.name)
                self._remember_module_tools(mod_name, list(module_instance.get_tools()))
            except Exception as e:
                self._discard_module(mod_name)
                try:
                    await module_instance.shutdown()
                except Exception as cleanup_error:
                    self._hook_error(module_instance, 'shutdown after failed init', cleanup_error)
                core_logger.event('modules', 'MODULES_EVENT', f"[Core Error] Module async initialization failed: '{mod_name}': {e}", level='ERROR')

        await self.notify_modules_changed()
        if getattr(self, 'plugins', None) is not None:
            await self.plugins.emit_async('core.ready', {'registry': self})
        core_logger.event('modules', 'MODULES_EVENT', f"[Core Registry] Total available core tools: {len(self.tools)}\n", level='INFO')

    async def shutdown_all(self):
        if getattr(self, "plugins", None) is not None:
            await self.plugins.aclose()
        for module_instance in self.loaded_modules.values():
            try:
                await module_instance.shutdown()
            except Exception:
                pass

    def get_available_modules_disk(self) -> Dict[str, str]:
        from core.plugins.modules import module_locations
        return {name: 'Системный' if category == 'system' else 'Пользовательский'
                for category, name in module_locations()}

    @staticmethod
    def _tool_metadata(tools: List[BaseTool]) -> List[Dict[str, str]]:
        result: List[Dict[str, str]] = []
        seen = set()
        for tool in tools:
            name = str(getattr(tool, "name", "") or "").strip()
            if not name or name in seen or getattr(tool, "catalog_hidden", False):
                continue
            seen.add(name)
            result.append({
                "name": name,
                "description": str(getattr(tool, "description", "") or ""),
            })
        return result

    def _remember_module_tools(self, module_name: str, tools: List[BaseTool]) -> None:
        metadata = self._tool_metadata(tools)
        if metadata:
            self.known_module_tools[module_name] = metadata

    def _probe_disabled_module_tools(self, module_name: str, category: str) -> List[Dict[str, str]]:
        cached = self.known_module_tools.get(module_name)
        if cached:
            return list(cached)

        cat_path = "system" if category == "Системный" else "custom"
        try:
            imported_lib = importlib.import_module(f"modules.{cat_path}.{module_name}")
            module_cls = getattr(imported_lib, "Module", None)
            if module_cls is None:
                return []
            instance = module_cls()
            tools = list(instance.get_tools())
            self._remember_module_tools(module_name, tools)
            return list(self.known_module_tools.get(module_name, []))
        except Exception as error:
            core_logger.event('modules', 'MODULES_EVENT', f"[Core Graph] Could not inspect tools of disabled module '{module_name}': {error}", level='INFO')
            return []

    def get_graph_snapshot(self) -> Dict[str, Any]:
        disk_modules = self.get_available_modules_disk()
        modules: List[Dict[str, Any]] = []

        for module_name, category in sorted(disk_modules.items()):
            enabled = module_name in self.loaded_modules
            if enabled:
                tool_names = self.module_tools_map.get(module_name, [])
                tools = []
                for tool_name in tool_names:
                    tool = self.tools.get(tool_name)
                    if getattr(tool, "catalog_hidden", False): continue
                    tools.append({
                        "name": tool_name,
                        "description": str(getattr(tool, "description", "") or "") if tool else "",
                    })
                if tools:
                    self.known_module_tools[module_name] = list(tools)
            else:
                tools = self._probe_disabled_module_tools(module_name, category)

            modules.append({
                "name": module_name,
                "category": category,
                "enabled": enabled,
                "tools": tools,
            })

        core_tools = []
        for tool_name in self.core_tool_names:
            tool = self.tools.get(tool_name)
            core_tools.append({
                "name": tool_name,
                "description": str(getattr(tool, "description", "") or "") if tool else "",
            })

        return {
            "core": {"name": "Toshy Core"},
            "core_tools": core_tools,
            "core_components": [component.snapshot() for component in self.core_components],
            "modules": modules,
        }

    def get_modules_status(self) -> List[Dict[str, Any]]:
        disk_modules = self.get_available_modules_disk()
        status_list = []

        for mod_name, category in disk_modules.items():
            is_enabled = mod_name in self.loaded_modules
            tools_count = sum(not getattr(self.tools.get(name), "catalog_hidden", False)
                              for name in self.module_tools_map.get(mod_name, [])) if is_enabled else 0
            status_list.append({
                "name": mod_name,
                "category": category,
                "enabled": is_enabled,
                "tools_count": tools_count,
                "pending_disable": 'module:'+mod_name in self._pending_changes
            })
        return status_list

    async def enable_module(self, module_name: str) -> Dict[str, Any]:
        async with self._mutation_lock:
            if self._pending_changes.pop('module:'+module_name, None) is not None:
                return {'success': True, 'enabled': True, 'pending': False, 'message': 'Отключение отменено.'}
            if module_name in self.loaded_modules:
                return {"success": False, "message": f"Модуль '{module_name}' уже активен."}

            disk_modules = self.get_available_modules_disk()
            if module_name not in disk_modules:
                return {"success": False, "message": f"Модуль '{module_name}' не найден на диске."}

            category = disk_modules[module_name]
            cat_path = "system" if category == "Системный" else "custom"
            module_path = f"modules.{cat_path}.{module_name}"
            module_instance = None
            try:
                imported_lib = importlib.import_module(module_path)
                importlib.reload(imported_lib)
                module_instance = imported_lib.Module()
                if hasattr(module_instance, "set_context"):
                    module_instance.set_context(self)
                await module_instance.async_init()

                module_tools = list(module_instance.get_tools())
                for tool in module_tools:tool._dispatch_module = module_instance
                conflicts = sorted({tool.name for tool in module_tools if tool.name in self.tools})
                if conflicts:
                    raise ValueError(f"Конфликт имен инструментов: {', '.join(conflicts)}")

                # Сначала фиксируем намерение на диске; после этого публикация в runtime
                # состоит только из безопасных операций в памяти.
                self._persist_module_enabled(module_name, category, enabled=True)
                self.loaded_modules[module_name] = module_instance
                self.module_categories[module_name] = category
                self.module_tools_map[module_name] = [tool.name for tool in module_tools]
                self._remember_module_tools(module_name, module_tools)
                for tool in module_tools:
                    self.tools[tool.name] = tool

                self._mount_module_knowledge_bases(module_name, module_instance, category)
                await self.notify_modules_changed()
                tools_count = len(module_tools)
                core_logger.event('modules', 'MODULE_ENABLED', f'{module_name}: {tools_count} tools')
                return {
                    "success": True,
                    "message": f"Модуль '{module_name}' успешно подключен ({tools_count} инструментов).",
                    "tools_count": tools_count,
                }
            except Exception as e:
                self._discard_module(module_name)
                if module_instance is not None:
                    try:
                        await module_instance.shutdown()
                    except Exception:
                        pass
                core_logger.event('modules', 'MODULE_ENABLE_ERROR', f'{module_name}: {e}', level='ERROR')
                return {"success": False, "message": f"Ошибка запуска модуля '{module_name}': {e}"}

    async def disable_module(self, module_name: str) -> Dict[str, Any]:
        async with self._mutation_lock:
            if self._active_executions and module_name in self.loaded_modules:
                self._pending_changes['module:'+module_name] = lambda: self._disable_module_now(module_name)
                return {'success': True, 'enabled': True, 'pending': True, 'message': 'Ожидает завершения всех генераций.'}
            return await self._disable_module_now(module_name)

    async def apply_when_idle(self, key, operation):
        async with self._mutation_lock:
            if self._active_executions:
                self._pending_changes[key] = operation
                return {'success': True, 'pending': True}
            await operation()
            return {'success': True, 'pending': False}

    async def _flush_pending_changes(self):
        if self._active_executions:
            return
        pending, self._pending_changes = self._pending_changes, {}
        for key, operation in pending.items():
            try:
                await operation()
            except Exception as error:
                core_logger.error('PENDING_CHANGE_ERROR', f'{key}: {error}')

    async def _disable_module_now(self, module_name):
        if module_name not in self.loaded_modules:
            return {"success": False, "message": f"Модуль '{module_name}' уже отключен."}

        try:
            module_instance = self.loaded_modules[module_name]
            category = self.module_categories[module_name]
            self._persist_module_enabled(module_name, category, enabled=False)

            # Текущие запуски используют снимок инструментов; изменение затронет новые запуски.
            registered_tools = self.module_tools_map.get(module_name, [])
            self._remember_module_tools(module_name, list(module_instance.get_tools()))
            for t_name in registered_tools:
                self.tools.pop(t_name, None)
            self.loaded_modules.pop(module_name, None)
            self.module_categories.pop(module_name, None)
            self.module_tools_map.pop(module_name, None)
            await module_instance.deactivate()
            await self.notify_modules_changed()

            if self._active_executions:
                self._pending_shutdown.append((module_name, module_instance))
            else:
                self.memory.unmount_external_kb(module_name)
                try:
                    await module_instance.shutdown()
                except Exception as shutdown_error:
                    core_logger.event('modules', 'MODULES_EVENT', f"[Core Warning] Module '{module_name}' was disabled, but resource cleanup failed: {shutdown_error}", level='WARN')

            core_logger.event('modules', 'MODULES_EVENT', f"[Core Registry Hot-Swap] Module disabled: '{module_name}'. Tools released: {len(registered_tools)}", level='INFO')
            return {
                "success": True,
                "message": f"Модуль '{module_name}' отключен. Удалено {len(registered_tools)} инструментов."
            }
        except Exception as e:
            core_logger.event('modules', 'MODULE_DISABLE_ERROR', f'{module_name}: {e}', level='ERROR')
            return {"success": False, "message": f"Ошибка отключения модуля '{module_name}': {e}"}

    def _persist_module_enabled(self, module_name: str, category: str, enabled: bool) -> None:
        key = "system_modules" if category == "Системный" else "custom_modules"
        from core.module_config import set_module_enabled
        self.config = set_module_enabled(self.config_path, key, module_name, enabled)

    def register_core_tool(self, tool: BaseTool):
        if tool.name in self.tools:
            raise ValueError(f"Имя '{tool.name}' зарезервировано или уже занято другим инструментом.")
        self.tools[tool.name] = tool
        if tool.name not in self.core_tool_names:
            self.core_tool_names.append(tool.name)

    def _discard_module(self, name):
        for tool_name in self.module_tools_map.pop(name, []):
            self.tools.pop(tool_name, None)
        self.loaded_modules.pop(name, None)
        self.module_categories.pop(name, None)
        memory = getattr(self, 'memory', None)
        if memory is not None:
            try:
                memory.unmount_external_kb(name)
            except Exception as error:
                core_logger.event('modules', 'MODULE_CLEANUP_ERROR', f'{name}: {error}', level='ERROR')

    @staticmethod
    def _hook_error(module, hook, error):
        core_logger.event('modules', 'MODULE_HOOK_ERROR',
            f'{type(module).__module__}.{type(module).__name__}.{hook}: {error}', level='ERROR')

    def _module_call(self, module, hook, *args, fallback=None, **kwargs):
        try:
            return getattr(module, hook)(*args, **kwargs)
        except Exception as error:
            self._hook_error(module, hook, error)
            return fallback

    @asynccontextmanager
    async def _module_scope(self, module, hook, *args):
        scope = None
        entered = False
        try:
            scope = getattr(module, hook)(*args)
            await scope.__aenter__()
            entered = True
        except Exception as error:
            self._hook_error(module, hook, error)
        failure = (None, None, None)
        try:
            yield entered
        except BaseException:
            failure = sys.exc_info()
            raise
        finally:
            if entered:
                try:
                    await scope.__aexit__(*failure)
                except Exception as error:
                    self._hook_error(module, hook, error)

    def reset_all_tools(self):
        for tool in self.tools.values():
            try:
                tool.reset()
            except Exception as error:
                self._hook_error(tool, 'reset', error)

    def refresh_package_tools(self):
        from modules.pipelines.package_module import PackageModule
        for name, module in list(self.loaded_modules.items()):
            if isinstance(module, PackageModule):
                tools = self._module_call(module, 'get_tools')
                if tools is not None:
                    tools = list(tools)
                    for old in self.module_tools_map.get(name, []):
                        self.tools.pop(old, None)
                    self.module_tools_map[name] = [t.name for t in tools]
                    for tool in tools:
                        tool._dispatch_module = module
                        self.tools[tool.name] = tool
            self._module_call(module, 'refresh_tools')

    async def notify_modules_changed(self):
        for module in list(self.loaded_modules.values()):
            try:
                await module.modules_changed()
            except Exception as error:
                core_logger.event('modules', 'MODULE_REFRESH_ERROR', str(error), level='ERROR')

    def get_request_messages(self, context, tools):
        messages = []
        for name, module in list(self.loaded_modules.items()):
            if context.allowed_modules is None or name in context.allowed_modules:
                messages.extend(self._module_call(module, 'get_request_messages', context, tools, fallback=[]) or [])
        return messages

    @contextmanager
    def _module_dispatch_scope(self, module, context, tools, messages, emit, call_id):
        scope, entered = None, False
        try:
            scope = module.dispatch_scope(context, tools, messages, emit, call_id=call_id)
            scope.__enter__()
            entered = True
        except Exception as error:
            self._hook_error(module, 'dispatch_scope', error)
            # Never expose tools that depend on an unavailable dispatch context.
            for name, tool in list(tools.items()):
                if getattr(tool, '_dispatch_module', None) is module:
                    tools.pop(name)
        failure = (None, None, None)
        try:
            yield
        except BaseException:
            failure = sys.exc_info()
            raise
        finally:
            if entered:
                try:
                    scope.__exit__(*failure)
                except Exception as error:
                    self._hook_error(module, 'dispatch_scope exit', error)

    @contextmanager
    def dispatch_scope(self, context, tools, messages, emit, *, call_id=""):
        with ExitStack() as stack:
            owners = {id(tool._dispatch_module):tool._dispatch_module for tool in tools.values()
                      if getattr(tool, '_dispatch_module', None) is not None}
            modules = list(owners.values()) if owners else [m for name,m in self.loaded_modules.items()
                       if context.allowed_modules is None or name in context.allowed_modules]
            for module in modules:
                stack.enter_context(self._module_dispatch_scope(module, context, tools, messages, emit, call_id))
            yield

    def refresh_request_guidance(self, frozen, context, tools):
        for name, module in list(self.loaded_modules.items()):
            if context.allowed_modules is None or name in context.allowed_modules:
                self._module_call(module, 'refresh_request_guidance', frozen, context, tools)

    def resource_catalog(self, kind, context=None):
        result = []
        for name, module in list(self.loaded_modules.items()):
            if context is None or context.allowed_modules is None or name in context.allowed_modules:
                result.extend(self._module_call(module, 'get_resource_catalog', kind, context, fallback=[]) or [])
        return result

    async def model_context(self, context, messages, tools, *, preview=False):
        from langchain_core.messages import HumanMessage
        user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)
                     and not m.additional_kwargs.get('module_guidance') and not m.additional_kwargs.get('runtime_instruction')), None)
        if user is not None and not context.raw_request:
            for kind, identities in (user.additional_kwargs.get('requested_resources') or {}).items():
                available = {x['id'] for x in self.resource_catalog(kind, context)
                             if x.get('enabled', True) and not x.get('error')}
                missing = set(identities) - available
                if missing:
                    raise ValueError('Выбранные ресурсы недоступны: ' + ', '.join(sorted(missing)))
        result = []
        with bind_execution_context(context):
            for name, module in list(self.loaded_modules.items()):
                if context.allowed_modules is None or name in context.allowed_modules:
                    try:
                        result.extend(await module.get_model_context(context, messages, tools, preview=preview))
                    except Exception as error:
                        self._hook_error(module, 'get_model_context', error)
        return result

    def snapshot_tools(self) -> Dict[str, BaseTool]:
        return dict(self.tools)

    @asynccontextmanager
    async def execution_snapshot(
        self,
        allowed_modules: Optional[Tuple[str, ...]] = None,
        *, context=None,
    ) -> AsyncIterator[Tuple[Dict[str, BaseTool], str, List[Dict[str, Any]]]]:
        """Freeze tools for one run, optionally limiting module-owned tools.

        Core tools are always present. ``None`` means all globally loaded modules;
        an explicit tuple means only those loaded modules participate in this run.
        """
        async with self._mutation_lock:
            self.refresh_package_tools()
            self._active_executions += 1
            if allowed_modules is None:
                tools = dict(self.tools)
                active_module_names = list(self.loaded_modules.keys())
            else:
                allowed = set(allowed_modules)
                names = set(self.core_tool_names)
                active_module_names = []
                for module_name in self.loaded_modules:
                    if module_name in allowed:
                        active_module_names.append(module_name)
                        names.update(self.module_tools_map.get(module_name, []))
                tools = {name: tool for name, tool in self.tools.items() if name in names}

            scopes = AsyncExitStack()
            await scopes.__aenter__()
            try:
                if context is not None:
                    tools = {name: tool.for_execution(context) for name, tool in tools.items()}
                for module in list(self.loaded_modules.values()):
                    filtered = self._module_call(module, 'filter_execution_tools', dict(tools), allowed_modules)
                    tools = filtered if isinstance(filtered, dict) else {
                        n: t for n, t in tools.items() if getattr(t, '_dispatch_module', None) is not module}
                for module in list(self.loaded_modules.values()):
                    entered = await scopes.enter_async_context(self._module_scope(module, 'execution_scope', tools))
                    if not entered:
                        tools = {n: t for n, t in tools.items() if getattr(t, '_dispatch_module', None) is not module}
                if context is not None:
                    for name in active_module_names:
                        module = self.loaded_modules.get(name)
                        if module is not None:
                            entered = await scopes.enter_async_context(self._module_scope(module, 'execution_context_scope', context, tools))
                            if not entered:
                                tools = {n: t for n, t in tools.items() if getattr(t, '_dispatch_module', None) is not module}

                module_sections: List[Dict[str, Any]] = []
                prompt_parts: List[str] = []
                for module_name in active_module_names:
                    module_instance = self.loaded_modules.get(module_name)
                    if module_instance is None:
                        continue
                    unavailable = object()
                    prompt = self._module_call(module_instance, 'get_system_prompt_addon', fallback=unavailable)
                    if prompt is unavailable:
                        tools = {n: t for n, t in tools.items() if getattr(t, '_dispatch_module', None) is not module_instance}
                        continue
                    prompt = str(prompt or '')
                    if prompt:
                        prompt_parts.append(prompt)
                    module_tools: List[Dict[str, str]] = []
                    for tool_name in self.module_tools_map.get(module_name, []):
                        tool = tools.get(tool_name)
                        if tool is None or not tool.model_visible:
                            continue
                        module_tools.append({
                            "name": tool_name,
                            "description": str(getattr(tool, "description", "") or ""),
                        })
                    if prompt or module_tools:
                        module_sections.append({
                            "name": module_name,
                            "category": self.module_categories.get(module_name, ""),
                            "prompt": prompt,
                            "tools": module_tools,
                        })

                prompt_addons = ""
                if prompt_parts:
                    prompt_addons = "\n\n=== MODULE INSTRUCTIONS ===\n" + "\n\n".join(prompt_parts)
            except BaseException:
                try:
                    await scopes.aclose()
                finally:
                    self._active_executions -= 1
                    if self._active_executions == 0:
                        await self._flush_pending_changes()
                raise

        try:
            yield tools, prompt_addons, module_sections
        finally:
            try:
                await scopes.aclose()
            finally:
                await self._release_execution()

    async def _release_execution(self):
        pending: List[Tuple[str, BaseModule]] = []
        async with self._mutation_lock:
            self._active_executions -= 1
            if self._active_executions == 0:
                pending, self._pending_shutdown = self._pending_shutdown, []
                await self._flush_pending_changes()
        for module_name, module_instance in pending:
            if module_name not in self.loaded_modules:
                self.memory.unmount_external_kb(module_name)
            try:
                await module_instance.shutdown()
            except Exception:
                pass


    def get_openai_tool_schemas(
        self,
        tools: Optional[Dict[str, BaseTool]] = None,
    ) -> List[Dict[str, Any]]:
        schemas = []
        active_tools = self.tools if tools is None else tools
        for tool in active_tools.values():
            if not tool.model_visible:
                continue
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args_schema
                }
            })
        return schemas

    def get_prompt_addon_sources(
        self,
        allowed_modules: Optional[Tuple[str, ...]] = None,
    ) -> List[Dict[str, str]]:
        allowed = None if allowed_modules is None else set(allowed_modules)
        sources: List[Dict[str, str]] = []
        for module_name, module_instance in self.loaded_modules.items():
            if allowed is not None and module_name not in allowed:
                continue
            addon = self._module_call(module_instance, 'get_system_prompt_addon', fallback='')
            if addon:
                sources.append({
                    "name": module_name,
                    "category": self.module_categories.get(module_name, ""),
                    "prompt": str(addon),
                })
        return sources

    def get_combined_prompt_addons(
        self,
        allowed_modules: Optional[Tuple[str, ...]] = None,
    ) -> str:
        addons = [item["prompt"] for item in self.get_prompt_addon_sources(allowed_modules)]
        if not addons:
            return ""
        return "\n\n=== MODULE INSTRUCTIONS ===\n" + "\n\n".join(addons)

    async def execute_tool(
        self,
        name: str,
        args: Dict[str, Any],
        context: Optional[ExecutionContext] = None,
        tools: Optional[Dict[str, BaseTool]] = None,
    ) -> str:
        from core.reasoning_control import finish_requested, BLOCKED_TOOL
        if finish_requested(context):return BLOCKED_TOOL
        active_tools = self.tools if tools is None else tools
        tool = active_tools.get(name)
        if not tool and name != 'multi_tool_use.parallel':
            return f"Ошибка: Инструмент '{name}' не найден."
        try:
            if tool is None and name == 'multi_tool_use.parallel':
                from core.parallel_tools import execute_parallel
                return await execute_parallel(self, args, context, active_tools)
            async def execute(payload):
                if finish_requested(context):return BLOCKED_TOOL
                selected = active_tools.get(payload['name'])
                if selected is None:
                    raise ValueError('Расширение запросило недоступный инструмент.')
                if context is None:
                    return await selected.execute(**payload['args'])
                with bind_execution_context(context):
                    return await selected.execute(**payload['args'])
            from core.file_history import history_tool
            history_paths = getattr(tool,'history_paths',None)
            paths = history_paths(args,context) if callable(history_paths) else None
            async with history_tool(context, name, paths):
                runtime = getattr(self, 'plugins', None)
                if runtime is not None:
                    return await runtime.invoke('core.tool.execute', execute,
                        {'name': name, 'args': args, 'context': context})
                return await execute({'name': name, 'args': args})
        except EmbeddingError:
            # Embedding failures are surfaced as a dedicated runtime event by AgentCore.
            raise
        except Exception as e:
            from modules.pipelines.runtime import PipelineShutdown
            from core.reasoning_control import FinalizationRequested
            if isinstance(e, (PipelineShutdown, FinalizationRequested)):
                raise
            return f"Ошибка при выполнении инструмента '{name}': {e}"
