"""Display-only snapshots of resources explicitly chosen for a user request."""

from copy import deepcopy

from core.chat_references import CHAT_ID

BADGES = "ui_resource_badges"


def resource_badges(metadata):
    saved = metadata.get(BADGES)
    if not isinstance(saved, list):
        requested = metadata.get("requested_resources") or {}
        saved = (
            [
                {"kind": "skill", "id": identity, "name": identity.rsplit("/", 1)[-1]}
                for identity in requested.get("skills", [])
                if isinstance(identity, str)
            ]
            if isinstance(requested, dict)
            else []
        )
    result, seen = [], set()
    for kind in ("skill", "pipeline"):
        for item in saved:
            if not isinstance(item, dict) or item.get("kind") != kind:
                continue
            identity = item.get("id")
            if (
                not isinstance(identity, str)
                or not identity
                or (kind, identity) in seen
            ):
                continue
            name = " ".join(str(item.get("name") or identity).split())
            result.append({"kind": kind, "id": identity, "name": name})
            seen.add((kind, identity))
    return result


def snapshot_request_badges(chat, worker):
    # Resuming continues the recorded execution, rather than making a new choice.
    if getattr(worker, "resume_run_id", ""):
        return
    message = next(
        (item for item in reversed(worker.messages) if item.get("role") == "user"), None
    )
    if message is None:
        return
    metadata = message.setdefault("additional_kwargs", {})
    previous = {(item["kind"], item["id"]): item for item in resource_badges(metadata)}
    badges = []
    if not getattr(worker, "raw_request", False) and not metadata.get(
        "task_status_check"
    ):
        entries = {item["id"]: item for item in chat.input_controls.skills.entries}
        requested = metadata.get("requested_resources") or {}
        for identity in requested.get("skills", []):
            entry = entries.get(identity) or previous.get(("skill", identity)) or {}
            badges.append(
                {
                    "kind": "skill",
                    "id": identity,
                    "name": entry.get("name") or identity.rsplit("/", 1)[-1],
                }
            )
        if worker.pipeline:
            entry = next(
                (
                    item
                    for item in chat.pipeline_selector.catalog
                    if item["id"] == worker.pipeline
                ),
                None,
            )
            entry = entry or previous.get(("pipeline", worker.pipeline)) or {}
            badges.append(
                {
                    "kind": "pipeline",
                    "id": worker.pipeline,
                    "name": entry.get("name") or worker.pipeline.rsplit("/", 1)[-1],
                }
            )
    metadata[BADGES] = resource_badges({BADGES: badges})
    identity = metadata.get(CHAT_ID)
    if not identity:
        return
    for turn in chat.user_turn_widgets:
        if getattr(turn, "chat_meta", {}).get(CHAT_ID) == identity:
            turn.chat_meta[BADGES] = deepcopy(metadata[BADGES])
            turn._refresh_tag_badges()
            break
    for item in chat.messages_history:
        if (
            item.get("role") == "user"
            and item.get("additional_kwargs", {}).get(CHAT_ID) == identity
        ):
            item["additional_kwargs"][BADGES] = deepcopy(metadata[BADGES])
