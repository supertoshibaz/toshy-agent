from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from PySide6.QtGui import QColor


STREAM_EXEC = "exec"
STREAM_ARG = "arg"
STREAM_PROMPT = "prompt"

STREAM_COLORS = {
    STREAM_EXEC: QColor("#f59e0b"),
    STREAM_ARG: QColor("#60a5fa"),
    STREAM_PROMPT: QColor("#c084fc"),
}


@dataclass(frozen=True)
class PipelinePortSpec:
    key: str
    label: str
    stream: str
    required: bool = False
    arg_name: str = ""
    schema_type: str = ""
    default: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class PipelineNodeSpec:
    type_id: str
    title: str
    kind: str
    category_path: Tuple[str, ...]
    description: str = ""
    inputs: Tuple[PipelinePortSpec, ...] = ()
    outputs: Tuple[PipelinePortSpec, ...] = ()
    implemented: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)


def exec_in() -> PipelinePortSpec:
    return PipelinePortSpec("exec:in", "Exec", STREAM_EXEC)


def exec_out() -> PipelinePortSpec:
    return PipelinePortSpec("exec:out", "Exec", STREAM_EXEC)


def exec_port(key: str, label: str) -> PipelinePortSpec:
    """Create a named Exec port for control-flow nodes."""
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("exec port key must not be empty")
    if not clean_key.startswith("exec:"):
        clean_key = f"exec:{clean_key}"
    return PipelinePortSpec(clean_key, str(label or "Exec"), STREAM_EXEC)


def prompt_in(index: int = 1) -> PipelinePortSpec:
    return PipelinePortSpec(
        f"prompt:{index}",
        "Prompt" if index == 1 else f"Prompt {index}",
        STREAM_PROMPT,
    )


def prompt_node_in() -> PipelinePortSpec:
    return PipelinePortSpec("prompt:in", "Prompt", STREAM_PROMPT)


def prompt_node_out() -> PipelinePortSpec:
    return PipelinePortSpec("prompt:out", "Prompt", STREAM_PROMPT)


def arg_in(
    name: str,
    schema: Optional[Dict[str, Any]] = None,
    *,
    required: bool = False,
) -> PipelinePortSpec:
    schema = dict(schema or {})
    suffix = " *" if required else ""
    return PipelinePortSpec(
        key=f"arg:{name}",
        label=f"Arg · {name}{suffix}",
        stream=STREAM_ARG,
        required=required,
        arg_name=name,
        schema_type=str(schema.get("type") or ""),
        default=schema.get("default"),
    )


def arg_result() -> PipelinePortSpec:
    return PipelinePortSpec(
        "arg:result",
        "Arg · result",
        STREAM_ARG,
        arg_name="result",
    )


def content_arg_in(schema_type: str) -> PipelinePortSpec:
    return PipelinePortSpec(
        "arg:in",
        "Arg",
        STREAM_ARG,
        arg_name="value",
        schema_type=str(schema_type or ""),
    )


def prompt_arg_in(index: int, schema_type: str = "string") -> PipelinePortSpec:
    """Dynamic Arg input used by Prompt content nodes.

    The first key stays ``arg:in`` for backward compatibility with already saved
    pipelines. Additional slots use stable numbered keys.
    """
    index = max(1, int(index))
    key = "arg:in" if index == 1 else f"arg:in:{index}"
    return PipelinePortSpec(
        key=key,
        label=f"Arg {index}",
        stream=STREAM_ARG,
        arg_name=f"value_{index}",
        schema_type=str(schema_type or "string"),
    )


def content_arg_out(schema_type: str) -> PipelinePortSpec:
    return PipelinePortSpec(
        "arg:out",
        "Arg",
        STREAM_ARG,
        arg_name="value",
        schema_type=str(schema_type or ""),
    )


def array_arg_in(index: int, schema_type: str) -> PipelinePortSpec:
    return PipelinePortSpec(
        key=f"array:{index}",
        label=f"Arg {index}",
        stream=STREAM_ARG,
        arg_name=f"item_{index}",
        schema_type=str(schema_type or "string"),
    )


def array_arg_out(schema_type: str) -> PipelinePortSpec:
    return PipelinePortSpec(
        key="arg:out",
        label="Arg · array",
        stream=STREAM_ARG,
        arg_name="value",
        schema_type=f"array[{str(schema_type or 'string')}]",
    )


def stream_color(stream: str) -> QColor:
    return QColor(STREAM_COLORS.get(stream, QColor("#a1a1aa")))
