"""Compatibility imports for the refactored Modules node package."""

from ui.views.modules.nodes.base import (
    NODE_WIDTH,
    PORT_HIT_RADIUS,
    PORT_RADIUS,
    PipelineNodeItem,
    initial_node_settings,
)
from ui.views.modules.nodes.registry import NodeRegistry, build_default_node_registry
from ui.views.modules.nodes.specs import (
    STREAM_ARG,
    STREAM_EXEC,
    STREAM_PROMPT,
    PipelineNodeSpec,
    PipelinePortSpec,
    stream_color,
)


def build_node_library():
    """Backward-compatible facade for the old monolithic node module."""
    return build_default_node_registry().specs()


__all__ = [
    "NODE_WIDTH",
    "PORT_HIT_RADIUS",
    "PORT_RADIUS",
    "STREAM_ARG",
    "STREAM_EXEC",
    "STREAM_PROMPT",
    "NodeRegistry",
    "PipelineNodeItem",
    "PipelineNodeSpec",
    "PipelinePortSpec",
    "build_default_node_registry",
    "build_node_library",
    "initial_node_settings",
    "stream_color",
]
