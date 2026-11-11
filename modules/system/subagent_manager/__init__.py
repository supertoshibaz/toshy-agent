"""Subagent module. Catalog readers do not load the engine."""

def __getattr__(name):
    if name == "Module":
        from .engine import Module
        return Module
    raise AttributeError(name)

# Static catalog sources; the editor parses these files without importing them.
PIPELINE_TOOL_SOURCES = ("engine.py",)
