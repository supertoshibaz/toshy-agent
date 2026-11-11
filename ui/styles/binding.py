"""Explicit theme bindings for local QSS; semantic colors stay with their owner."""
from weakref import ref
from PySide6.QtCore import QObject, Slot
from shiboken6 import isValid
from ui.themes.manager import theme_manager
from ui.themes.colors import resolve_values


def resolve(template):
    return resolve_values(template, theme_manager.current)


class _StyleBinding(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.widget = ref(widget)
        self.template = ''
        theme_manager.theme_changed.connect(self.apply)

    @Slot()
    def apply(self):
        widget = self.widget()
        if widget is not None and isValid(widget):
            widget.setStyleSheet(resolve(self.template))


def apply_style(widget, template):
    binding = getattr(widget, '_toshy_style_binding', None)
    if binding is None:
        binding = widget._toshy_style_binding = _StyleBinding(widget)
    binding.template = template
    binding.apply()


def watch_paint(widget):
    """Qt disconnects the bound receiver automatically when the widget dies."""
    if not getattr(widget, '_toshy_paint_bound', False):
        widget._toshy_paint_bound = True
        theme_manager.theme_changed.connect(widget.update)


def form_field(widget):
    widget.setProperty('uiField', True)
    return widget


class _HtmlBinding(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.widget = ref(widget)
        self.template = ''
        theme_manager.theme_changed.connect(self.apply)

    def apply(self, *_):
        widget = self.widget()
        if widget is not None and isValid(widget):
            widget.setText(resolve(self.template))


def apply_rich_text(label, template):
    binding = getattr(label, '_toshy_html_binding', None)
    if binding is None:
        binding = label._toshy_html_binding = _HtmlBinding(label)
    binding.template = template
    binding.apply()


def themed_color(token, alpha=None):
    value = theme_manager.color(token)
    if alpha is not None:
        value.setAlpha(alpha)
    return value
