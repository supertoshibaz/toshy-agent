"""Context menus for resource choices and inline entry."""
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QPixmap
from PySide6.QtWidgets import QLabel, QLineEdit, QMenu, QWidgetAction


class ContextMenu(QMenu):
    """A normal popup list, with inline text entry only when needed."""
    accepted = Signal()

    def __init__(self, parent, title, anchor=None):
        super().__init__(parent)
        self.setObjectName('FileManagerContextMenu')
        # QWidgetAction can still own the focused editor when a menu closes.
        # Keep the popup owned by the workspace rather than deleting it during
        # an input event; Qt releases it with its parent.
        self.anchor = anchor
        add_caption_action(self, 'addSection', title.replace('&', '&&'))
        self.error_action = None

    def show(self):
        position = (self.anchor.mapToGlobal(self.anchor.rect().bottomLeft())
                    if self.anchor is not None else QCursor.pos())
        self.popup(position)

    def add_editor(self, edit):
        action = QWidgetAction(self)
        action.setDefaultWidget(edit)
        add_caption_action(self, 'addAction', action)
        return action

    def set_error(self, message):
        if self.error_action is None:
            label = QLabel()
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
            label.setMaximumWidth(360)
            label.setContentsMargins(8, 4, 8, 4)
            self.error_label = label
            self.error_action = self.add_editor(label)
        bind_caption(self.error_label, str(message))
        # QAction closes the menu before invoking the callback. Keep validation
        # failures in that same menu without resurrecting one that was replaced.
        if getattr(self.parentWidget(), '_inline_editor', None) is self:
            self.show()
        edit = getattr(self, 'edit', None)
        if edit is not None:
            edit.setFocus()

    def apply(self, callback, *args):
        try:
            callback(*args)
        except Exception as exc:
            self.set_error(exc)
            return False
        self.close()
        return True


def _menu(parent, title, anchor=None):
    previous = getattr(parent, '_inline_editor', None)
    if previous is not None:
        previous.close()
    menu = ContextMenu(parent, title, anchor)
    parent._inline_editor = menu
    menu.destroyed.connect(lambda: setattr(parent, '_inline_editor', None)
                           if getattr(parent, '_inline_editor', None) is menu else None)
    return menu


def _message(menu, message):
    # Long errors fit the popup width instead of stretching past the screen.
    label = QLabel(str(message))
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setMaximumWidth(360)
    label.setContentsMargins(8, 4, 8, 4)
    menu.add_editor(label)


def notice(parent, title, message):
    menu = _menu(parent, title)
    _message(menu, message)
    localize(add_caption_action(menu, 'addAction', 'Закрыть'), 'ui.top_bar.close_a7a4').triggered.connect(menu.close)
    menu.show()
    return menu


def confirm(parent, title, message, callback):
    menu = _menu(parent, title)
    _message(menu, message)
    menu.addSeparator()
    localize(add_caption_action(menu, 'addAction', 'Удалить'), 'ui.widget.delete_be99').triggered.connect(lambda: menu.apply(callback))
    localize(add_caption_action(menu, 'addAction', 'Отмена'), 'ui.close_panel.cancel_8fbe').triggered.connect(menu.close)
    menu.show()
    return menu


def text_input(parent, title, value, callback, anchor=None):
    menu = _menu(parent, title, anchor)
    edit = QLineEdit(value)
    edit.setMinimumWidth(260)
    set_text(edit, 'ui.subagents_panel.name_0918', setter='setPlaceholderText')
    menu.edit = edit
    menu.add_editor(edit)
    def submit():
        if not edit.text().strip():
            menu.set_error(tr('ui.caption.e45da69c3ec3')); return
        menu.apply(callback, edit.text().strip())
    localize(add_caption_action(menu, 'addAction', 'Сохранить'), 'ui.settings.save_b4d3').triggered.connect(submit)
    edit.returnPressed.connect(submit)
    menu.show(); edit.setFocus(); edit.selectAll()
    return menu


def choose_item(parent, title, items, callback, anchor=None):
    menu = _menu(parent, title, anchor)
    for label, value in items:
        action = add_caption_action(menu, 'addAction', str(label).replace('&', '&&'))
        action.setData(value)
        action.triggered.connect(lambda checked=False, v=value: menu.apply(callback, v))
    menu.show()
    return menu


def choose_color(parent, value, callback, anchor=None):
    menu = _menu(parent, tr('ui.caption.99bb8299bfa8'), anchor)
    colors = ((tr('ui.caption.6b64572a4b37'), '#ec4899'), (tr('ui.caption.21b4d916f326'), '#ef4444'), (tr('ui.caption.b381c35c9f19'), '#f97316'),
              (tr('ui.caption.faf8c4a14f74'), '#eab308'), (tr('ui.caption.a4bb19e1436f'), '#22c55e'), (tr('ui.caption.1174a37a5ab4'), '#14b8a6'),
              (tr('ui.caption.3a14ef9fbd49'), '#0ea5e9'), (tr('ui.caption.66d12fb99282'), '#6366f1'), (tr('ui.caption.4aed2cd288de'), '#a855f7'),
              (tr('ui.caption.a024a93d4e0c'), '#ffffff'), (tr('ui.caption.ea1ae91dfebc'), '#777777'), (tr('ui.caption.1ca9b6b05dfc'), '#222222'))
    for label, color in colors:
        swatch = QPixmap(16, 16); swatch.fill(QColor(color))
        action = add_caption_action(menu, 'addAction', QIcon(swatch), label)
        action.setData(color)
        action.setCheckable(True); action.setChecked(color.lower() == value.lower())
        action.triggered.connect(lambda checked=False, c=color: menu.apply(callback, c))
    menu.addSeparator()
    edit = QLineEdit(value)
    bind_caption(edit, '#RRGGBB', setter='setPlaceholderText')
    edit.setMinimumWidth(200)
    menu.edit = edit
    menu.add_editor(edit)
    def submit():
        color = QColor(edit.text())
        if not color.isValid():
            menu.set_error(tr('ui.caption.4d3b6dbb806b')); return
        menu.apply(callback, color.name())
    localize(add_caption_action(menu, 'addAction', 'Применить введённый цвет'), 'ui.inline_editor.apply_entered_color_d6eb').triggered.connect(submit)
    edit.returnPressed.connect(submit)
    menu.show()
    return menu
