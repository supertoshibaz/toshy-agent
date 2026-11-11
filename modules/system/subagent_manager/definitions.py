"""Subagent definitions and live dispatch; running graphs keep private config copies."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import yaml
from core.paths import APP_PATHS

_frame = ContextVar("subagent_frame", default=None)


class SubagentResult(str):
    def __new__(cls, text, *, direct=False):
        result = super().__new__(cls, text)
        result.direct = direct
        return result


class SubagentStore:
    def __init__(self, root=None):
        self.root = Path(
            root or APP_PATHS.root / "modules/system/subagent_manager/subagents"
        )

    def read(self):
        result = {}
        if not self.root.exists():
            return result
        for path in sorted(self.root.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                item = copy.deepcopy(data)
                item.update(
                    id=path.stem,
                    name=str(data.get("name") or path.stem),
                    enabled=bool(data.get("enabled", True)),
                )
                result[path.stem] = item
            except (OSError, ValueError, yaml.YAMLError):
                continue
        return result

    def path(self, identity):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", str(identity)):
            raise ValueError("Неверный ID субагента")
        legacy = self.root / (identity + ".yml")
        return legacy if legacy.exists() else self.root / (identity + ".yaml")

    @staticmethod
    def validate(data):
        result = copy.deepcopy(data)
        result["name"] = str(result.get("name") or "New subagent").strip()[:160]
        result["description"] = str(result.get("description") or "")
        result["system_prompt"] = str(result.get("system_prompt") or "")
        result["enabled"] = bool(result.get("enabled", False))
        result["use_system_prompt"] = bool(result.get("use_system_prompt", True))
        modules = result.get("allowed_modules") or []
        if not isinstance(modules, list) or any(
            not isinstance(x, str) for x in modules
        ):
            raise ValueError("Разрешённые модули должны быть списком имён")
        result["allowed_modules"] = sorted(set(modules))
        result["max_steps"] = max(1, min(100, int(result.get("max_steps", 10))))
        result["direct_output"] = bool(result.get("direct_output", False))
        result["context_mode"] = (
            "conversation" if result.get("context_mode") == "conversation" else "task"
        )
        result["model_profile_id"] = str(result.get("model_profile_id") or "")
        if result["enabled"] and not result["description"].strip():
            raise ValueError("Перед включением укажите описание назначения субагента")
        return result

    def write(self, identity, data):
        path = self.path(identity)
        result = self.validate(data)
        result["id"] = identity
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".subagent-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(result, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return result

    def create(self, name="New subagent"):
        identity = "agent_" + uuid.uuid4().hex[:12]
        return self.write(identity, dict(name=name, enabled=False))

    def delete(self, identity):
        self.path(identity).unlink(missing_ok=True)

    def settings(self):
        try:
            data = json.loads((self.root / "settings.json").read_text(encoding="utf-8"))
            return {"max_parallel": max(1, min(32, int(data.get("max_parallel", 4))))}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"max_parallel": 4}

    def write_settings(self, data):
        value = {"max_parallel": max(1, min(32, int(data.get("max_parallel", 4))))}
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.root / "settings.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return value


def active_catalog():
    return {
        key: item for key, item in SubagentStore().read().items() if item["enabled"]
    }


async def save_definition(registry, data, *, identity=None, defaults=None):
    """Persist immediately; running children already own private config copies."""
    import asyncio
    import hashlib

    async with registry._mutation_lock:
        store = SubagentStore()
        records = await asyncio.to_thread(store.read)
        if identity is not None:
            store.path(identity)
            if identity not in records:
                raise KeyError(identity)
        else:
            name = str(data.get('name') or '').strip()[:160]
            identity = next((key for key, value in records.items() if value.get('name') == name), '')
            if not identity:
                base = re.sub('[^a-zA-Z0-9_-]', '_', name).lower().strip('_')[:80]
                base = base or 'agent_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
                identity = base
                while identity in records:
                    identity = base + '_' + uuid.uuid4().hex[:12]
        value = store.validate({**(defaults or {}), **records.get(identity, {}), **data, 'id': identity})
        write = asyncio.create_task(asyncio.to_thread(store.write, identity, value))
        # The atomic write must finish before releasing the mutation lock even
        # if the owning generation is stopped during filesystem work.
        try:
            saved = await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            registry._pending_changes.pop('subagent:' + identity, None)
            getattr(registry, '_subagent_drafts', {}).pop(identity, None)
            raise
        registry._pending_changes.pop('subagent:' + identity, None)
        getattr(registry, '_subagent_drafts', {}).pop(identity, None)
        return saved


def delegation_catalog():
    """Discover definitions at dispatch time, without mutating running graphs."""
    frame = _frame.get()
    if frame and frame.get('catalog_pinned'):
        return copy.deepcopy(frame['catalog'])
    return active_catalog()


def delegation_frame():
    return _frame.get()


@contextmanager
def delegation_scope(context, tools, messages, emit, *, catalog=None, call_id=""):
    parent = _frame.get()
    value = dict(
        context=context,
        tools=tools,
        messages=messages,
        emit=emit,
        call_id=call_id,
        catalog_pinned=catalog is not None or bool(parent and parent.get('catalog_pinned')),
        catalog=copy.deepcopy(
            catalog
            if catalog is not None
            else parent["catalog"] if parent else active_catalog()
        ),
    )
    token = _frame.set(value)
    try:
        yield value
    finally:
        _frame.reset(token)


from core.child_runs import track_child_run as track_subagent
