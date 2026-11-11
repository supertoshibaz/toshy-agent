"""Provider-independent context policy, shared by the UI and core."""

import os
import tempfile

import yaml
from core.paths import APP_PATHS

DEFAULTS = dict(
    enabled=True,
    threshold=75,
    context_limit=0,
    tail_mode="lean",
    target_ratio=20,
    protect_last_n=20,
    min_tail_user_messages=1,
    summary_model_profile_id="",
    micro_compact=False,
    micro_compact_every_n_turns=1,
    micro_compact_defrag_tokens=2000,
)


def normalize(values):
    result = dict(DEFAULTS)
    for key in ("enabled", "micro_compact"):
        if isinstance(values.get(key), bool):
            result[key] = values[key]
    for key, low, high in (
        ("threshold", 10, 95),
        ("context_limit", 0, 100_000_000),
        ("target_ratio", 5, 50),
        ("protect_last_n", 1, 100),
        ("min_tail_user_messages", 1, 20),
        ("micro_compact_every_n_turns", 1, 100),
        ("micro_compact_defrag_tokens", 256, 32000),
    ):
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(low, min(high, value))
    if values.get("tail_mode") in {"lean", "legacy"}:
        result["tail_mode"] = values["tail_mode"]
    result["summary_model_profile_id"] = str(
        values.get("summary_model_profile_id") or ""
    )
    result["micro_compact"] = False
    return result


def load_settings():
    with APP_PATHS.config.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    return normalize(config.get("context_compression") or {})


def save_settings(values):
    # Re-read other settings: changing context policy must not restore an old
    # snapshot of API credentials, module settings or embedding configuration.
    with APP_PATHS.config.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    config["context_compression"] = normalize(values)
    fd, temporary = tempfile.mkstemp(prefix=".context-", dir=APP_PATHS.config.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, APP_PATHS.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
