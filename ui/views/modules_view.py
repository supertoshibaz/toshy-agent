"""Compatibility import for the Modules workspace.

The implementation now lives in the dedicated ``ui.views.modules`` package.
"""

from ui.views.modules.workspace import ModulesView

__all__ = ["ModulesView"]
