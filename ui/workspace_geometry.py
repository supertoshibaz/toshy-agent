"""Shared sizing for the Projects and Module workspaces."""

def editor_workspace_width():
    # Include the editor's two navigation rails, not only its text viewport.
    # Import lazily: FileView can itself be loaded while building BottomPanel.
    from ui.views.file_view import EDITOR_READING_WIDTH, EDITOR_LINE_NUMBER_GAP, EDITOR_MINIMAP_GAP
    from ui.views.minimap import CodeMinimap
    return EDITOR_READING_WIDTH + 2 * CodeMinimap.WIDTH + EDITOR_LINE_NUMBER_GAP + EDITOR_MINIMAP_GAP


def centered_workspace_geometry(host, available_width, preferred_width):
    from PySide6.QtCore import QPoint
    width = min(max(0, int(available_width)), int(preferred_width))
    window = host.window()
    center = host.mapFrom(window, QPoint(window.width() // 2, 0)).x()
    return max(0, min(center - width // 2, available_width - width)), width

def reduced_margin_width(available_width, nominal_width):
    available_width = max(0, int(available_width))
    old_unused_width = max(0, available_width - nominal_width)
    return available_width - round(old_unused_width / 3)
