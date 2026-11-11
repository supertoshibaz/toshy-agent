"""Persistent, user-only pipeline panels. Text is never appended to model content."""
from ui.icons import set_icon
from ui.i18n import set_text

import copy
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QSizePolicy
from PySide6.QtGui import QColor
from ui.widgets.error_actions import copy_error_button


class NotificationTurn(QFrame):
    delete_requested = Signal(object)

    def __init__(self, segment, parent=None):
        super().__init__(parent)
        self.segment = copy.deepcopy(segment)
        color = QColor(segment.get('color', '#5b8def'))
        if not color.isValid(): color = QColor('#5b8def')
        self.setObjectName('PipelineNotification')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.setStyleSheet(f'QFrame#PipelineNotification {{ background:#202026; border:1px solid {color.name()}; border-radius:5px; }}')
        layout = QHBoxLayout(self); layout.setContentsMargins(12, 8, 8, 8)
        label = QLabel(str(segment.get('text', '')), self)
        label.setTextFormat(Qt.PlainText); label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet(f'color:{color.name()};border:0;')
        layout.addWidget(label, 1)
        copy_button = copy_error_button(self, lambda: str(self.segment.get('text', '')))
        set_text(copy_button, 'ui.chat_notifications.copy_notice_51ae', setter='setToolTip')
        layout.addWidget(copy_button, 0, Qt.AlignTop)
        close = QToolButton(self); set_icon(close, 'close', square=True); set_text(close, 'ui.chat_notifications.dismiss_notice_25d3', setter='setToolTip')
        close.setFixedSize(22, 22); close.clicked.connect(lambda: self.delete_requested.emit(self))
        layout.addWidget(close, 0, Qt.AlignTop)

    def export_segments(self):
        return [copy.deepcopy(self.segment)]


def render_notification(chat, segment):
    identity = segment.get('id')
    for index in range(chat.turns_layout.count()):
        turn = chat.turns_layout.itemAt(index).widget()
        if isinstance(turn, NotificationTurn) and turn.segment.get('id') == identity:
            return turn
    turn = NotificationTurn(segment, chat.turns_container)
    worker = chat.current_worker
    if worker: turn._run_state = {'run_id': worker.run_id, 'status': 'running'}
    chat.turns_layout.insertWidget(chat._next_generated_turn_index(None), turn)
    def remove():
        chat.turns_layout.removeWidget(turn); turn.hide(); turn.deleteLater()
        chat._sync_session_to_disk()
    turn.delete_requested.connect(remove)
    return turn
