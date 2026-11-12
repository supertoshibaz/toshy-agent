"""The onboarding accent is the one deliberate exception to theme tokens.

Everything else continues to use the active Toshy theme.  Custom themes use
exactly the same dark/light foundation contract as the built-in themes.
"""
from ui.themes.manager import theme_manager


def onboarding_colors():
    if theme_manager.current['foundation'] == 'light':
        return {
            'outline': '#3978C9',
            'tab': '#B6D8FF',
            'tab_text': '#173D6C',
            'link': '#3978C9',
            'highlight': '#89BFFF',
        }
    return {
        'outline': '#E0C356',
        'tab': '#B6A05D',
        'tab_text': '#242115',
        'link': '#E0C356',
        'highlight': '#B6A05D',
    }
