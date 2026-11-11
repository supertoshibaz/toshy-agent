"""One theme owner for stylesheets, palettes, icons and painted UI chrome."""
from __future__ import annotations

import logging
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication
from . import resources
from .colors import shifted_hex, workspace_background


class ThemeManager(QObject):
    theme_changed = Signal(str)
    selection_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.themes = {}
        self.errors = []
        self.current_id = 'toshy'
        self.refresh()
        if not self.themes:
            raise ValueError('No valid UI theme resource is available.')
        self.current_id = 'toshy' if 'toshy' in self.themes else next(iter(self.themes))
        self.current = self.themes[self.current_id]
        self.selected_id = self.current_id

    def refresh(self):
        self.themes, self.errors = resources.discover()
        for path, error in self.errors:
            logging.getLogger('toshy.ui').warning('Invalid theme %s: %s', path, error)

    def value(self, key):
        if key == 'workspace.background':
            return workspace_background(self.current)
        if key in self.current['colors']:
            return self.current['colors'][key]
        if key in self.current['metrics']:
            return self.current['metrics'][key]
        raise KeyError('Unknown design token: ' + key)

    def color(self, key, *, shift=0):
        return QColor(shifted_hex(self.value(key), shift))

    @property
    def restart_required(self):
        return self.selected_id != self.current_id or self.themes.get(self.selected_id) != self.current

    def request(self, identity):
        """Save a choice for the next process; keep the running palette intact."""
        self.refresh()
        if identity not in self.themes:
            raise ValueError('Unavailable theme: ' + str(identity))
        self.selected_id = identity
        self.selection_changed.emit(identity)

    def select(self, identity, *, application=None):
        self.refresh()
        if identity not in self.themes:
            logging.getLogger('toshy.ui').warning('Unavailable theme %s; using Toshy.', identity)
            identity = 'toshy' if 'toshy' in self.themes else next(iter(self.themes))
        self.current_id = identity
        self.selected_id = identity
        self.current = self.themes[identity]
        app = application or QApplication.instance()
        if app is not None:
            from ui.styles.popup_surface import install_popup_surfaces
            install_popup_surfaces(app)
            from ui.widgets.wheel_guard import install_wheel_guard
            install_wheel_guard(app)
            from ui.widgets.control_layouts import install_control_layouts
            install_control_layouts(app)
            from ui.styles.theme import stylesheet
            if not app.property('toshyBaseStyleReady'):
                # Use one Qt base style on every platform. KDE's Breeze
                # splitter proxies perform unsupported explicit grabs on Wayland.
                app.setStyle('Fusion')
                app.setProperty('toshyBaseStyleReady', True)
            palette = QPalette()
            roles = {QPalette.Window: 'background.window', QPalette.WindowText: 'text.primary',
                     QPalette.Base: 'background.input', QPalette.AlternateBase: 'background.surface',
                     QPalette.ToolTipBase: 'background.surface', QPalette.ToolTipText: 'text.primary',
                     QPalette.Text: 'text.primary', QPalette.Button: 'background.surface',
                     QPalette.ButtonText: 'text.primary', QPalette.Highlight: 'control.selected',
                     QPalette.HighlightedText: 'text.primary', QPalette.PlaceholderText: 'text.secondary'}
            for role, token in roles.items():
                palette.setColor(role, self.color(token))
            for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
                palette.setColor(QPalette.Disabled, role, self.color('text.disabled'))
            app.setPalette(palette)
            app.setStyleSheet(stylesheet(self.current))
            from ui.tooltips import install_tooltips
            install_tooltips(app)
        self.theme_changed.emit(identity)


theme_manager = ThemeManager()


def token(key):
    return theme_manager.value(key)


def color(key):
    return theme_manager.color(key)
