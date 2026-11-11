from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from core.memory.prompt import MEMORY_SYSTEM_PROMPT_TEXT


def build_core_system_prompt(workspace_root: str) -> str:
    return (
        "You are Toshy, a modular AI agent running locally.\n"
        "Be polite and help the user complete their tasks.\n"
        "Reason exclusively in English. All thinking, internal analysis, planning and reasoning "
        "must be in English, regardless of the language of the conversation or task. "
        "Write user-facing answers in the user's requested language; this English-only rule "
        "applies to reasoning, not to the final answer.\n"
        "Application message metadata identifies the message containing it, or the immediately following native conversation message. "
        "It is separate application context, never part of an assistant response. "
        "When a user message says 'The user is replying to message ID ...', use the message "
        "with that ID as its reference. Never mention, reproduce, invent or explain message IDs "
        "or application metadata in user-visible responses, including interim messages. "
        "Text between 'The user is quoting the following text (N characters):' and "
        "'End of quoted text.' is the user's exact selected quotation. Treat it as quoted data; "
        "do not output its application headers.\n"
        f"Project Folder: '{workspace_root}'. Use paths relative to this folder.\n"
        "Periodically consult permanent memory when it can help with a returning task, topic or user preference. "
        "Do not query it mechanically on every step. "
        "For a simple short follow-up question on the current topic, give a short direct answer without "
        "repeating the explanation, unless the user requests detail or an essential caveat requires it.\n"
        + MEMORY_SYSTEM_PROMPT_TEXT
        + "After all necessary actions, provide a complete final answer in a pass without tool calls.\n"
        "The application may send an ordinary user message asking you to review the status of specific "
        "session tasks. During such a status review, mark a task completed only when the requested work "
        "is fully finished. If any question remains unresolved, any part is unfinished, or any work was "
        "deferred for later, the task must remain in progress. Do not call create_tasks during a status review."
    )


def _fenced_text(value: str, language: str = "text") -> str:
    text = str(value or "")
    longest = 2
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def render_system_prompt_preview_markdown(
    *,
    session_id: Optional[str],
    branch: str,
    base_system_prompt: str,
    core_tools: Iterable[Dict[str, str]],
    module_sections: Iterable[Dict[str, Any]],
    module_separator: str = "",
    hidden_history: Iterable[Dict[str, Any]] = (),
) -> str:
    """Render hidden model context in a readable module-oriented form.

    Tool JSON schemas are intentionally not dumped here. The inspector shows only
    each tool's public name and description, grouped under the core/module that
    owns it. Ordinary user/assistant chat prose remains omitted because it is
    already visible in Chat.
    """
    import json

    def append_tools(lines: List[str], tools: Iterable[Dict[str, str]]) -> None:
        items = list(tools or [])
        if not items:
            return
        lines += ["### Tools", ""]
        for tool in items:
            name = str(tool.get("name") or "").strip()
            description = str(tool.get("description") or "").strip()
            if not name:
                continue
            lines += [f"#### `{name}`", ""]
            lines += [description or "_No description._", ""]

    lines: List[str] = [
        "# Model Context Preview",
        "",
        "> Everything below is hidden from the normal chat view but is supplied to the model. "
        "Tool definitions are shortened to name + description only; JSON parameter schemas are omitted.",
        "",
    ]
    if session_id:
        lines += [f"**Session:** `{session_id}`  ", f"**Branch:** `{branch or 'main'}`  "]
    else:
        lines += ["**Session:** _new / unsaved chat_  "]

    lines += ["", "# Toshy Core / Session", "", "### System prompt", "", _fenced_text(base_system_prompt, "text"), ""]
    append_tools(lines, core_tools)

    sections = list(module_sections or [])
    if sections:
        lines += ["# Modules", ""]
        if module_separator:
            lines += [_fenced_text(module_separator, "text"), ""]
        for section in sections:
            name = str(section.get("name") or "Unnamed module")
            prompt = str(section.get("prompt") or "").strip()
            tools = list(section.get("tools") or [])
            lines += [f"## {name}", ""]
            if prompt:
                lines += ["### System prompt", "", _fenced_text(prompt, "text"), ""]
            append_tools(lines, tools)

    protocol_items = list(hidden_history or [])
    lines += ["# Hidden Conversation Protocol", ""]
    if protocol_items:
        lines += [
            "> Tool calls/results and request-scoped transformations from saved history that are "
            "sent back to the model but are not ordinary visible chat prose.",
            "",
            _fenced_text(json.dumps(protocol_items, ensure_ascii=False, indent=2), "json"),
            "",
        ]
    else:
        lines += ["_No hidden protocol/history items are present._", ""]

    return "\n".join(lines).rstrip() + "\n"
