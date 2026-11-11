"""Module-owned skill discovery, pinned instructions and scoped preparation."""

import asyncio
import base64
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.messages import HumanMessage
from core.context import get_execution_context
from .store import (
    SkillStore,
    MAX_BYTES,
    MAX_FILES,
    digest,
    frontmatter,
    has_scripts,
    relative_path,
)


class SkillRuntime:
    def __init__(self, module):
        self.module, self.owner = module, module.module_name
        self.store = SkillStore(module.store, self.owner)
        self.frames = {}

    def check(self, context=None):
        context = context or get_execution_context()
        registry = self.module.registry
        if (
            registry is None
            or registry.loaded_modules.get(self.owner) is not self.module
            or context.allowed_modules is not None
            and self.owner not in context.allowed_modules
        ):
            raise ValueError("Модуль-владелец скилла недоступен для этого запуска.")
        return context

    def catalog(self, context=None):
        try:
            metadata = self.module.store.read(self.owner)
        except FileNotFoundError:
            return []
        result = self.store.catalog()
        settings = self.store.settings()
        # Persisted MCP metadata is inspectable offline. Loading still requires
        # the live MCP module, owner and server to pass their usual scope checks.
        for path in sorted((self.module.directory / "mcp").glob("*/server.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for entry in record.get("skills", []):
                uri = str(entry.get("uri", ""))
                key = (
                    "mcp:"
                    + record["id"]
                    + ":"
                    + hashlib.sha256(uri.encode()).hexdigest()[:20]
                )
                meta = entry.get("frontmatter") or {}
                resources = entry.get("resources")
                scripts = resources == "dynamic"
                # URI paths include a namespace; inspect their actual suffixes.
                if isinstance(resources, list):
                    scripts = any(
                        "/scripts/" in str(r.get("uri"))
                        or Path(str(r.get("uri"))).suffix.lower()
                        in {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".mjs"}
                        for r in resources
                    )
                item = dict(
                    id=self.owner + "/" + key,
                    key=key,
                    owner=self.owner,
                    name=meta.get("name") or uri,
                    description=meta.get("description") or "",
                    source="mcp",
                    source_name=record.get("name") or record["id"],
                    server_id=record["id"],
                    uri=uri,
                    scripts=scripts,
                    enabled=True,
                    automatic=not meta.get("disable-model-invocation", False),
                    environment="",
                    visibility=record.get("visibility", "model"),
                )
                item.update(settings.get(key, {}))
                result.append(item)
        module_name = metadata.get("name", self.owner)
        for item in result:
            item.update(tool="skills", module=self.owner, module_name=module_name)
        if context is not None:
            registry = self.module.registry
            mcp = registry.loaded_modules.get("mcp_client") if registry else None
            for item in result:
                if item["source"] == "mcp":
                    reason = (
                        mcp.manager.access_reason(context.allowed_modules, self.owner)
                        if mcp
                        else "Модуль MCP отключён."
                    )
                    if not reason and item.get("visibility") == "pipeline":
                        from modules.pipelines.service import _frame

                        pipeline = _frame.get()
                        if not pipeline or pipeline.get("depth", 0) < 1:
                            reason = "Этот MCP-сервер доступен только пайплайнам."
                    if not reason:
                        actor = mcp.manager.actors.get((self.owner, item["server_id"]))
                        if (
                            actor is None
                            or actor.stop_requested
                            or actor.status
                            not in {"connected", "connecting", "reconnecting"}
                        ):
                            reason = "MCP-сервер не подключён."
                    if reason:
                        item["error"] = reason
        return result

    def entry(self, key, context=None):
        key = str(key).removeprefix(self.owner + "/")
        entry = next((e for e in self.catalog(context) if e["key"] == key), None)
        if entry is None or not entry.get("enabled"):
            raise ValueError("Скилл отключён или не найден: " + key)
        if entry.get("error"):
            raise ValueError(entry["error"])
        return entry

    def mcp(self):
        module = self.module.registry.loaded_modules.get("mcp_client")
        if module is None:
            raise ValueError("Для загрузки этого скилла нужен модуль MCP.")
        return module.manager

    @asynccontextmanager
    async def scope(self, context):
        self.frames[context.run_id] = {"loaded": {}, "user": None, "initialized": False}
        try:
            yield
        finally:
            self.frames.pop(context.run_id, None)

    async def bundle(self, entry, context, *, all_files=False, path="SKILL.md"):
        frame = self.frames.get(context.run_id, {})
        pinned = frame.get("loaded", {}).get(entry["key"])
        if (
            pinned
            and pinned.get("_files") is not None
            and (all_files or path in pinned["_files"])
        ):
            return pinned["_files"] if all_files else {path: pinned["_files"][path]}
        if entry["source"] == "local":
            files = await asyncio.to_thread(self.store.read, entry["key"])
            return files if all_files else {path: files[path]}
        from modules.system.mcp_client.skills import (
            fetch,
            resource_path,
            validate_entry,
        )

        manager = self.mcp()
        manifest = pinned.get("_manifest") if pinned else None
        manifest = manifest or frame.get("manifests", {}).get(entry["key"])
        if manifest is None:
            response = await manager.call(
                self.owner,
                entry["server_id"],
                "__toshy_skill_get",
                {"uri": entry["uri"]},
                operation="skill_get",
            )
            manifest = response["skill"]
            if manifest.get("uri") != entry["uri"]:
                raise ValueError("MCP вернул другой скилл.")
            validate_entry(manifest)
            if frame:
                frame.setdefault("manifests", {})[entry["key"]] = manifest
        paths = [path]
        if all_files:
            if manifest["resources"] == "dynamic":
                pending = [manifest["uri"][: -len("/SKILL.md")]]
                seen, paths = set(), []
                while pending:
                    directory = pending.pop()
                    if directory in seen or len(seen) + len(paths) >= MAX_FILES:
                        raise ValueError(
                            "Динамический MCP-скилл содержит цикл или слишком много файлов."
                        )
                    seen.add(directory)
                    cursor, cursors = None, set()
                    while True:
                        arguments = {"uri": directory}
                        if cursor:
                            arguments["cursor"] = cursor
                        response = await manager.call(
                            self.owner,
                            entry["server_id"],
                            "__toshy_skill_directory",
                            arguments,
                            operation="skill_directory",
                        )
                        for resource in response.get("resources", []):
                            relative = resource_path(manifest, resource["uri"])
                            if len(seen) + len(paths) + len(pending) >= MAX_FILES:
                                raise ValueError(
                                    "Динамический скилл превышает лимит файлов."
                                )
                            if resource.get("mimeType") == "inode/directory":
                                pending.append(resource["uri"])
                            else:
                                paths.append(relative)
                        cursor = response.get("nextCursor")
                        if not cursor:
                            break
                        if cursor in cursors:
                            raise ValueError("MCP повторяет курсор каталога.")
                        cursors.add(cursor)
                if "SKILL.md" not in paths:
                    raise ValueError(
                        "Каталог динамического скилла не содержит SKILL.md."
                    )
            else:
                paths = [
                    resource_path(manifest, resource["uri"])
                    for resource in manifest["resources"]
                ]
        files = {}
        for relative in paths:
            files[relative] = await fetch(
                manager, self.owner, entry["server_id"], manifest, relative
            )
            if sum(map(len, files.values())) > MAX_BYTES:
                raise ValueError("MCP-скилл превышает лимит 16 МиБ.")
        return files

    def remember(self, context, entry, text, files=None, prepared=None):
        frame = self.frames.setdefault(
            context.run_id, {"loaded": {}, "user": None, "initialized": False}
        )
        item = dict(
            id=entry["id"],
            key=entry["key"],
            instructions=text,
            source=entry.get("source_name", "local"),
            source_content="",
            prepared=prepared,
            _files=files,
        )
        item["_manifest"] = frame.get("manifests", {}).get(entry["key"])
        frame["loaded"][entry["key"]] = item
        user = frame.get("user")
        if user is not None:
            contexts = user.additional_kwargs.setdefault("module_context", {})
            contexts[self.owner] = dict(
                source_content=str(user.content),
                subagent_id=context.subagent_id,
                resources=[
                    {
                        k: v
                        for k, v in value.items()
                        if not k.startswith("_")
                        and k not in {"prepared", "source_content"}
                    }
                    for value in frame["loaded"].values()
                ],
            )

    async def prepare(self, entry, context, files=None):
        terminal = self.module.registry.loaded_modules.get("terminal_sandbox")
        if (
            terminal is None
            or context.allowed_modules is not None
            and "terminal_sandbox" not in context.allowed_modules
        ):
            raise ValueError(
                "Скилл содержит скрипты. Для выполнения нужен разрешённый модуль Terminal."
            )
        files = files or await self.bundle(entry, context, all_files=True)
        if context.run_id in self.frames:
            self.frames[context.run_id].setdefault("prepared_files", {})[
                entry["key"]
            ] = files
        profile = (
            terminal.manager.store.get(entry["environment"])
            if entry.get("environment")
            else terminal.manager.route(context)
        )
        metadata = frontmatter(files["SKILL.md"].decode("utf-8-sig"))
        import sys

        target_os = (
            profile.get("target_os")
            if profile["backend"] == "ssh"
            else (
                "linux"
                if profile["backend"] == "docker"
                else (
                    "windows"
                    if sys.platform == "win32"
                    else "macos" if sys.platform == "darwin" else "linux"
                )
            )
        )
        if metadata.get("platforms") and target_os not in metadata["platforms"]:
            raise ValueError(
                "Скилл не поддерживает ОС выбранного окружения: " + target_os
            )
        origin = {
            "resource": entry["id"],
            "source": entry["source"],
            "uri": entry.get("uri", ""),
        }
        version = hashlib.sha256(
            (entry["id"] + "\0" + digest(files)).encode()
        ).hexdigest()
        return await terminal.manager.prepare_resources(
            files, version, context, entry.get("environment", ""), origin
        )

    async def action(
        self,
        action,
        key="",
        *,
        path="SKILL.md",
        content="",
        name="",
        preview=False,
        server="",
        uri="",
        args=None,
        interpreter=None,
        timeout=None,
    ):
        context = self.check()
        if action == 'prepare' and path == '':
            path = 'SKILL.md'
        if action == "list":
            servers = []
            for path_ in (self.module.directory / "mcp").glob("*/server.json"):
                try:
                    record = json.loads(path_.read_text(encoding="utf-8"))
                    from modules.system.mcp_client.skills import supported

                    if supported(record):
                        servers.append({"id": record["id"], "name": record["name"]})
                except (OSError, ValueError, ImportError):
                    continue
            return {"skills": self.catalog(context), "servers": servers}
        if action == "resolve":
            from modules.system.mcp_client.skills import validate_entry

            manager = self.mcp()
            record = manager.store.read(self.owner, server)
            result = await manager.call(
                self.owner,
                server,
                "__toshy_skill_get",
                {"uri": uri},
                operation="skill_get",
            )
            entry = result["skill"]
            if entry.get("uri") != uri:
                raise ValueError("MCP вернул другой URI.")
            validate_entry(entry)
            record["skills"] = [
                item for item in record.get("skills", []) if item.get("uri") != uri
            ] + [entry]
            manager.store.write(self.owner, record)
            return {
                "skills": [
                    item
                    for item in self.catalog(context)
                    if item.get("uri") == uri and item.get("server_id") == server
                ]
            }
        if action == "create":
            identity = await asyncio.to_thread(
                self.store.install_files, {"SKILL.md": content.encode("utf-8")}
            )
            return {"created": self.owner + "/" + identity}
        if preview or action in {"write", "delete", "copy"}:
            entry = next(
                (
                    item
                    for item in self.catalog(context)
                    if item["key"] == str(key).removeprefix(self.owner + "/")
                ),
                None,
            )
            if entry is None:
                raise ValueError("Скилл не найден.")
        else:
            entry = self.entry(key, context)
        if action == "run":
            if preview:
                raise ValueError("Предпросмотр скилла не выполняет скрипты.")
            from .execution import run

            return await run(self, entry, context, path, args, interpreter, timeout)
        if action in {"write", "delete"}:
            if entry["source"] != "local":
                raise ValueError(
                    "MCP-оригинал доступен только для чтения. Создайте локальную копию."
                )
            if action == "write":
                await asyncio.to_thread(
                    self.store.write_file, entry["key"], path, content
                )
            else:
                await asyncio.to_thread(self.store.remove, entry["key"])
            return {"success": True}
        if action == "copy":
            files = dict(await self.bundle(entry, context, all_files=True))
            if name:
                meta = frontmatter(files["SKILL.md"].decode("utf-8-sig"))
                text = files["SKILL.md"].decode("utf-8-sig")
                import re, yaml

                body = re.split(r"\r?\n---(?:\s*\r?\n|\s*\Z)", text, maxsplit=1)[1]
                meta["name"] = name
                files["SKILL.md"] = (
                    "---\n"
                    + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
                    + "---\n"
                    + body
                ).encode("utf-8")
            identity = await asyncio.to_thread(self.store.install_files, files)
            return {"created": self.owner + "/" + identity}
        if action not in {"read", "prepare"}:
            raise ValueError("Неизвестное действие скилла.")
        path = relative_path(path)
        files = await self.bundle(
            entry,
            context,
            all_files=action == "prepare"
            or path == "SKILL.md"
            and entry["source"] == "local",
            path=path,
        )
        if path not in files:
            raise ValueError("Файл отсутствует в скилле: " + path)
        try:
            text = files[path].decode("utf-8-sig")
        except UnicodeError:
            return {
                "skill": entry["id"],
                "path": path,
                "binary": True,
                "size": len(files[path]),
                "note": "Use prepare to obtain this binary asset in the configured terminal; binary bytes are not inserted into text context.",
            }
        result = dict(skill=entry["id"], path=path, content=text)
        prepared = None
        if not preview and (
            action == "prepare" or path == "SKILL.md" and entry["scripts"]
        ):
            try:
                prepared = await self.prepare(
                    entry,
                    context,
                    (
                        files
                        if entry["source"] == "local" or action == "prepare"
                        else None
                    ),
                )
                result["execution"] = prepared
            except Exception as error:
                if action == "prepare":
                    raise
                result["execution_error"] = str(error)
        if path == "SKILL.md" and not preview:
            if (
                prepared is not None
                and entry["source"] == "mcp"
                and action != "prepare"
            ):
                files = self.frames.get(context.run_id, {}).get(
                    "prepared_files", {}
                ).get(entry["key"]) or await self.bundle(entry, context, all_files=True)
            self.remember(
                context,
                entry,
                text,
                (
                    files
                    if entry["source"] == "local"
                    or action == "prepare"
                    or prepared is not None
                    else None
                ),
                prepared,
            )
            if result.get("execution_error"):
                self.frames[context.run_id]["loaded"][entry["key"]][
                    "execution_error"
                ] = result["execution_error"]
        return result

    async def model_context(self, context, messages, tools, *, preview=False):
        if context.raw_request:
            return []
        user = next(
            (
                m
                for m in reversed(messages)
                if isinstance(m, HumanMessage)
                and not m.additional_kwargs.get("module_guidance")
                and not m.additional_kwargs.get("runtime_instruction")
            ),
            None,
        )
        if user is None:
            return []
        frame = (
            self.frames.setdefault(
                context.run_id, {"loaded": {}, "user": None, "initialized": False}
            )
            if not preview
            else {"loaded": {}, "user": None, "initialized": False}
        )
        frame["user"] = user
        if not frame["initialized"]:
            for message in [user]:
                item = (message.additional_kwargs.get("module_context") or {}).get(
                    self.owner
                )
                if (
                    not item
                    or item.get("subagent_id", "") != context.subagent_id
                    or item.get("source_content") != str(message.content)
                ):
                    continue
                for resource in item.get("resources", []):
                    try:
                        self.entry(resource["key"], context)
                    except ValueError:
                        continue
                    restored = dict(resource, _files=None)
                    if not preview:
                        current_entry = self.entry(resource["key"], context)
                        if current_entry["source"] == "local":
                            current_files = await asyncio.to_thread(
                                self.store.read, resource["key"]
                            )
                            restored.update(
                                instructions=current_files["SKILL.md"].decode(
                                    "utf-8-sig"
                                ),
                                _files=current_files,
                            )
                    frame["loaded"][resource["key"]] = restored
                break
            requested = (user.additional_kwargs.get("requested_resources") or {}).get(
                "skills", []
            )
            for identity in requested:
                if identity.startswith(self.owner + "/"):
                    if preview:
                        entry = self.entry(identity, context)
                        if entry["source"] == "local":
                            files = await asyncio.to_thread(
                                self.store.read, entry["key"]
                            )
                            frame["loaded"][entry["key"]] = dict(
                                key=entry["key"],
                                id=entry["id"],
                                instructions=files["SKILL.md"].decode("utf-8-sig"),
                                source="local",
                            )
                    else:
                        await self.action("read", identity)
            frame["initialized"] = True
        result = []
        for item in frame["loaded"].values():
            result.append(
                "Active skill "
                + item["id"]
                + " (source: "
                + item["source"]
                + "). Follow its workflow within the tools and permissions of this run.\n"
                + f"For bundled scripts use skills action=run, module={self.owner}, skill={item['key']}, "
                "path=<package-relative script>, args=[...]. Toshy resolves the real file and terminal. "
                "Relative paths in this skill are relative to its package, not the project workspace.\n"
                + item["instructions"]
                + (
                    "\nExecution is unavailable: " + item["execution_error"]
                    if item.get("execution_error")
                    else ""
                )
                + (
                    "\nPrepared execution files: "
                    + json.dumps(item["prepared"], ensure_ascii=False)
                    if item.get("prepared")
                    else f"\nUse skills action=run with module={self.owner} to prepare and execute bundled scripts in the configured terminal."
                )
            )
        return result
