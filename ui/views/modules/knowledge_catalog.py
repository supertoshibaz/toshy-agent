from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

from core.paths import APP_PATHS


_KB_METADATA_KEY = "__toshy_kb__"
_KB_METADATA_PREFIX_LIMIT = 65536


def _read_kb_metadata_prefix(path: Path) -> Dict[str, Any]:
    """Read only the small metadata object at the start of a KB JSON file.

    Knowledge-base files can be very large. The compiler writes __toshy_kb__ as
    the first root entry, so the Modules UI can populate selectors without
    parsing the document/chunk payload.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            prefix = handle.read(_KB_METADATA_PREFIX_LIMIT)
        marker = json.dumps(_KB_METADATA_KEY)
        marker_pos = prefix.find(marker)
        if marker_pos < 0:
            return {}
        colon_pos = prefix.find(":", marker_pos + len(marker))
        if colon_pos < 0:
            return {}
        value_text = prefix[colon_pos + 1 :].lstrip()
        value, _ = json.JSONDecoder().raw_decode(value_text)
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        return {}


def discover_knowledge_base_catalog() -> List[Dict[str, str]]:
    """Return KBs belonging to modules enabled in config.yaml.

    This is editor-only discovery. Runtime authorization is still enforced by
    the core and search_knowledge_base.
    """
    try:
        config = yaml.safe_load(APP_PATHS.config.read_text(encoding="utf-8")) or {}
    except Exception:
        config = {}

    result: List[Dict[str, str]] = []
    seen = set()
    for category in ("system", "custom"):
        config_key = f"{category}_modules"
        enabled = [str(name) for name in (config.get(config_key) or []) if str(name).strip()]
        for module_name in enabled:
            from core.plugins.modules import module_directory
            knowledge_dir = module_directory(category, module_name) / 'knowledge'
            if not knowledge_dir.is_dir():
                continue
            paths = [*knowledge_dir.glob('*.sqlite'), *knowledge_dir.glob('*.json')]
            # SQLite имеет приоритет над оставшейся после миграции JSON-копией.
            for path in sorted(paths, key=lambda p: (p.suffix != '.sqlite', p.name.casefold())):
                if path.suffix == '.sqlite':
                    from core.sqlite_storage import SQLiteStore
                    try:
                        metadata = SQLiteStore(path, 'knowledge').read_knowledge_metadata()
                    except Exception:
                        continue
                else:
                    metadata = _read_kb_metadata_prefix(path)
                owner = str(metadata.get("owner") or f"{category}.{module_name}").strip()
                local_or_global_id = str(metadata.get("id") or path.stem).strip()
                database_id = (
                    local_or_global_id
                    if "::" in local_or_global_id
                    else f"{owner}::{local_or_global_id or path.stem}"
                )
                if database_id in seen:
                    continue
                seen.add(database_id)
                result.append(
                    {
                        "id": database_id,
                        "name": str(metadata.get("name") or path.stem).strip() or path.stem,
                        "manifest": str(metadata.get("manifest") or "").strip(),
                        "owner": owner,
                    }
                )
    result.sort(key=lambda item: (item["name"].casefold(), item["id"].casefold()))
    return result
