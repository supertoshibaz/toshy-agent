"""Startup → Core → Timeouts. Changes apply to new executions and requests."""
from PySide6.QtWidgets import QSpinBox
from core.timeout_settings import load_settings, save_settings, BOUNDS
from ui.i18n import tr
from ui.views.startup_view import SettingsPage, SettingRow
from ui.widgets.dialogs import MessageBox


class TimeoutSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('nav.timeouts'), tr('ui.timeouts.description'), ('Core','Timeouts'), parent)
        self.options = load_settings()
        self.controls = {}
        for key in BOUNDS:
            control = QSpinBox(self)
            control.setRange(*BOUNDS[key])
            control.setValue(self.options[key])
            self.controls[key] = control
            self.add_row(SettingRow(tr('ui.timeouts.' + key), tr('ui.timeouts.' + key + '_help'),
                control, search_terms=('timeout steps tool network таймаут шаги инструменты сеть',), parent=self))
            control.valueChanged.connect(lambda value, key=key: self.write(key,value))

    def write(self, key, value):
        candidate = dict(self.options, **{key:value})
        try:
            save_settings(candidate)
        except (OSError, ValueError) as error:
            control = self.controls[key]
            control.blockSignals(True)
            control.setValue(self.options[key])
            control.blockSignals(False)
            MessageBox.warning(self, tr('nav.timeouts'), str(error))
        else:self.options = candidate
