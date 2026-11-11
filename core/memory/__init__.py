"""Always-on Toshy Memory core system module.

The package mirrors the regular module structure for maintainability, but it
belongs to core and cannot be disabled.
"""

from .manager import MemoryManager

__all__ = ["MemoryManager"]
