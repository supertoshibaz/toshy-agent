"""Application hotkeys, using the shared Startup settings rows and theme."""
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget, QHBoxLayout, QKeySequenceEdit, QPushButton, QLabel
from ui.i18n import tr, localize, bind_caption
from ui.chat_hotkeys import send_sequence, set_send_sequence, normalize
from ui.views.startup_view import SettingsPage, SettingRow


class HotkeySettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('ui.settings.hotkeys'), tr('hotkeys.description'), ('App', tr('ui.settings.hotkeys')), parent)
        self.send_key = QKeySequenceEdit(send_sequence(), self)
        if hasattr(self.send_key, 'setMaximumSequenceLength'):
            self.send_key.setMaximumSequenceLength(1)
        if hasattr(self.send_key, 'setFinishingKeyCombinations'):
            self.send_key.setFinishingKeyCombinations([])
        self.add_row(SettingRow(tr('hotkeys.send_message'), tr('hotkeys.send_description'), self.send_key, parent=self))
        controls = QWidget(self)
        row = QHBoxLayout(controls); row.setContentsMargins(0, 0, 0, 0)
        self.save_button = localize(QPushButton(controls), 'common.save')
        self.reset_button = localize(QPushButton(controls), 'hotkeys.restore_default')
        row.addWidget(self.save_button); row.addWidget(self.reset_button); row.addStretch()
        self.rows_layout.addWidget(controls)
        self.status = QLabel(self); self.status.setWordWrap(True); self.rows_layout.addWidget(self.status)
        self.save_button.clicked.connect(self.save)
        self.reset_button.clicked.connect(self.reset)
        self.send_key.keySequenceChanged.connect(self.validate)

    def validate(self, *_):
        try:
            normalize(self.send_key.keySequence())
        except ValueError:
            self.status.show()
            self.save_button.setEnabled(False)
            bind_caption(self.status, tr('hotkeys.choose_key'))
        else:
            self.save_button.setEnabled(True)
            self.status.hide()

    def save(self):
        self.status.show()
        try:
            set_send_sequence(self.send_key.keySequence())
        except (OSError, ValueError) as error:
            bind_caption(self.status, tr('hotkeys.save_failed', error=str(error)))
            return
        bind_caption(self.status, tr('hotkeys.saved'))

    def reset(self):
        self.send_key.setKeySequence(QKeySequence('Ctrl+Return'))
        self.save()
