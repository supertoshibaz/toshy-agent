from ui.themes.manager import theme_manager
"""Общие параметры заголовков для редактирования и просмотра Markdown."""
HEADING_POINTS = {1: 17, 2: 15, 3: 13, 4: 12, 5: 11}
HEADING_COLOR = '#a1a1aa'


def heading_css(level):
    return (f"font-size:{HEADING_POINTS[level]}pt; "
            f"font-weight:700; color:{theme_manager.value('text.secondary')}; margin:0;")
