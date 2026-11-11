from ui.i18n import add_caption_action, bind_caption, tr
from ui.icons import set_icon
"""Общий компактный выбор при закрытии несохранённой вкладки."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QHBoxLayout, QMenu, QStyle, QToolButton, QWidget, QWidgetAction)
from ui.widgets.dialogs import MessageBox as QMessageBox
from shiboken6 import isValid


def close_choice(parent, title, *, document=None, tab_bar=None):
    menu = QMenu(parent)
    menu.setObjectName('FileManagerContextMenu')
    bind_caption(menu, tr('ui.caption.e6f3dfdaf942') + str(title), setter='setAccessibleName')
    body = QWidget(menu)
    buttons = QHBoxLayout(body)
    buttons.setContentsMargins(6, 6, 6, 6)
    buttons.setSpacing(4)
    result = []
    for value, text, icon in (
        (QMessageBox.Cancel, tr('common.cancel'), 'cancel'),
        (QMessageBox.Save, tr('common.save'), 'save'),
        (QMessageBox.Discard, tr('common.discard'), 'delete'),
    ):
        button = QToolButton(body)
        set_icon(button, icon, size=18)
        bind_caption(button, text, setter='setToolTip')
        bind_caption(button, text, setter='setAccessibleName')
        button.setFixedSize(36, 36)
        button.setCursor(Qt.PointingHandCursor)
        def choose(_checked=False, choice=value):
            result.append(choice)
            menu.close()
        button.clicked.connect(choose)
        buttons.addWidget(button)
    action = QWidgetAction(menu)
    action.setDefaultWidget(body)
    add_caption_action(menu, 'addAction', action)
    anchor = parent.mapToGlobal(QPoint(12, 12))
    if tab_bar is not None:
        for index in range(tab_bar.count()):
            if tab_bar.tabData(index) is document:
                anchor = tab_bar.mapToGlobal(tab_bar.tabRect(index).bottomLeft())
                break
    parent._unsaved_popup = menu
    try:
        menu.exec(anchor)
    finally:
        if isValid(parent):
            parent._unsaved_popup = None
        if isValid(menu):
            menu.deleteLater()
    # Escape, клик снаружи и закрытие родителя означают отмену.
    return result[0] if result else QMessageBox.Cancel


def confirm_close(parent, document, *, title, tab_bar=None, save=None, discard=None):
    timer = getattr(document, '_auto_save_timer', None)
    remaining = timer.remainingTime() if timer is not None and timer.isActive() else None
    if remaining is not None:
        timer.stop()
    accepted = False
    try:
        answer = close_choice(parent, title, document=document, tab_bar=tab_bar)
        if not isValid(parent):
            return False
        if answer == QMessageBox.Save:
            accepted = bool(save())
        elif answer == QMessageBox.Discard:
            if discard is not None:
                discard()
            accepted = True
        return accepted
    finally:
        # Отмена или ошибка сохранения возвращают прежний режим автосохранения.
        if not accepted and remaining is not None and isValid(timer):
            window = parent.window() if isValid(parent) else None
            review = getattr(window, '_close_review', None)
            if review is not None:
                if all(timer is not old for old, _ in review.paused_timers):
                    review.paused_timers.append((timer, remaining))
            else:
                timer.start(max(1, remaining))
