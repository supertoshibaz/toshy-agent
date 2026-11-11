"""Startup preferences for checking application and plugin releases."""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QCheckBox
from release_tools import client
from ui.i18n import tr
from ui.views.startup_view import SettingsPage, SettingRow


class UpdateSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('nav.updates'), tr('updates.startup_help'), ('App', 'Updates'), parent)
        self.automatic = QCheckBox(self)
        self.automatic.setChecked(bool(client.settings().get('automatic', True)))
        self.add_row(SettingRow(tr('updates.automatic'), tr('updates.startup_help'), self.automatic,
                               search_terms=('updates plugins automatic обновления плагины',), parent=self))
        self.automatic.toggled.connect(lambda checked: client.save_settings(automatic=checked))

    def showEvent(self, event):
        with QSignalBlocker(self.automatic):
            self.automatic.setChecked(bool(client.settings().get('automatic', True)))
        super().showEvent(event)
