"""Skills-over-MCP transport. Ordinary resources/prompts are not skills."""

import base64
import hashlib
import re
from urllib.parse import unquote

EXTENSION = "io.modelcontextprotocol/skills"


def supported(record):
    return EXTENSION in (record.get("capabilities", {}).get("extensions") or {})


async def rpc(session, method, arguments):
    from mcp import types
    from pydantic import ConfigDict

    class ExtensionResult(types.Result):
        model_config = ConfigDict(extra="allow")

    class ExtensionRequest(types.Request):
        method: str
        params: dict

    request = ExtensionRequest(method=method, params=arguments)
    result = await session.send_request(request, ExtensionResult)
    return result.model_dump(by_alias=True, mode="json")


async def discover(session, record):
    if not supported(record):
        return []
    result, cursor, seen = [], None, set()
    while True:
        data = await rpc(session, "skills/list", {"cursor": cursor} if cursor else {})
        result.extend(data.get("skills", []))
        cursor = data.get("nextCursor")
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError("MCP Skills повторяет курсор каталога.")
        seen.add(cursor)


def resource_path(entry, uri):
    from modules.skills.store import relative_path

    root = str(entry["uri"])
    if not root.endswith("/SKILL.md"):
        raise ValueError("URI скилла должен оканчиваться на /SKILL.md.")
    prefix = root[: -len("SKILL.md")]
    if not str(uri).startswith(prefix):
        raise ValueError("Ресурс находится вне пакета MCP-скилла.")
    return relative_path(unquote(str(uri)[len(prefix) :]))


def validate_entry(entry):
    from modules.skills.store import MAX_FILES, MAX_BYTES

    meta = entry.get("frontmatter") or {}
    if not isinstance(meta.get("name"), str) or not isinstance(
        meta.get("description"), str
    ):
        raise ValueError("MCP Skills: отсутствуют name/description.")
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", meta["name"])
        or len(meta["name"]) > 64
        or not meta["description"].strip()
        or len(meta["description"]) > 1024
    ):
        raise ValueError("MCP Skills: некорректные name/description.")
    if resource_path(entry, entry["uri"]) != "SKILL.md":
        raise ValueError("Некорректный URI скилла.")
    if unquote(entry["uri"][: -len("/SKILL.md")].rsplit("/", 1)[-1]) != meta["name"]:
        raise ValueError("MCP Skills: имя не совпадает с директорией URI.")
    resources = entry.get("resources")
    if resources == "dynamic":
        return
    if not isinstance(resources, list) or not resources or len(resources) > MAX_FILES:
        raise ValueError(
            "MCP Skills: отсутствует полный список файлов либо превышен лимит."
        )
    names, total = set(), 0
    for resource in resources:
        path = resource_path(entry, resource["uri"])
        if path.casefold() in names:
            raise ValueError("MCP Skills: повторяющийся путь ресурса.")
        names.add(path.casefold())
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(resource.get("digest", ""))):
            raise ValueError("MCP Skills: некорректный SHA-256 ресурса.")
        size = resource.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("MCP Skills: некорректный размер ресурса.")
        total += size
    if (
        not any(resource["uri"] == entry["uri"] for resource in resources)
        or total > MAX_BYTES
    ):
        raise ValueError("MCP Skills: нет SKILL.md либо пакет превышает 16 МиБ.")


async def fetch(manager, owner, server_id, entry, relative="SKILL.md"):
    from modules.skills.store import MAX_BYTES, frontmatter, relative_path

    validate_entry(entry)
    relative = relative_path(relative)
    from urllib.parse import quote

    uri = entry["uri"][: -len("SKILL.md")] + quote(relative, safe="/")
    manifest = entry["resources"]
    resource = (
        next((r for r in manifest if resource_path(entry, r["uri"]) == relative), None)
        if isinstance(manifest, list)
        else None
    )
    if isinstance(manifest, list) and resource is None:
        raise ValueError("Файла нет в текущем манифесте MCP-скилла.")
    if resource:
        uri = resource["uri"]
    # Origin is part of every cache key; identical URIs on two servers differ.
    cache = manager.store.path(owner, server_id) / "runtime" / "skills-cache"
    key = hashlib.sha256(
        (
            entry["uri"] + "\0" + relative + "\0" + str((resource or {}).get("digest"))
        ).encode()
    ).hexdigest()
    path = cache / key
    body = None
    if resource and path.is_file() and path.stat().st_size == resource["size"]:
        candidate = path.read_bytes()
        if "sha256:" + hashlib.sha256(candidate).hexdigest() == resource["digest"]:
            body = candidate
    if body is None:
        result = await manager.call(
            owner,
            server_id,
            "__toshy_skill_read",
            {"uri": uri},
            operation="skill_resource",
        )
        contents = result.get("contents") or []
        if len(contents) != 1 or str(contents[0].get("uri")) != uri:
            raise ValueError("MCP Skills: ожидался один ресурс с запрошенным URI.")
        item = contents[0]
        body = (
            base64.b64decode(item["blob"], validate=True)
            if "blob" in item
            else str(item.get("text", "")).encode("utf-8")
        )
        if len(body) > MAX_BYTES:
            raise ValueError("MCP Skills: ресурс превышает лимит.")
        if resource and (
            len(body) != resource["size"]
            or "sha256:" + hashlib.sha256(body).hexdigest() != resource["digest"]
        ):
            raise ValueError(
                "MCP Skills: содержимое изменилось или не соответствует SHA-256. Обновите каталог сервера."
            )
        if resource:
            from .store import atomic_json

            cache.mkdir(parents=True, exist_ok=True)
            import os, tempfile

            fd, temp = tempfile.mkstemp(dir=cache)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                os.replace(temp, path)
            finally:
                __import__("pathlib").Path(temp).unlink(missing_ok=True)
    if (
        relative == "SKILL.md"
        and frontmatter(body.decode("utf-8-sig")) != entry["frontmatter"]
    ):
        raise ValueError("MCP Skills: метаданные SKILL.md не совпадают с каталогом.")
    return body
