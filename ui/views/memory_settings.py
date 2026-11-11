"""Startup → Core → System Modules → Memory."""
from PySide6.QtWidgets import QComboBox, QSpinBox
from core.memory.settings import load_settings, save_settings
from ui.i18n import tr, add_item
from ui.views.startup_view import SettingsPage, SettingRow
from ui.widgets.dialogs import MessageBox


class MemorySettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('nav.memory'), tr('memory.settings.description'),
                         ('Core', 'System Modules', 'Memory'), parent)
        self.options = load_settings()
        self.page_limit = QSpinBox(self)
        self.page_limit.setRange(1, 1000)
        self.page_limit.setValue(self.options['page_limit'])
        self.add_row(SettingRow(tr('memory.settings.page_limit'), tr('memory.settings.page_limit_help'),
                               self.page_limit, search_terms=('pages ranking страницы выдача память'), parent=self))
        self.arguments = QComboBox(self)
        add_item(self.arguments, 'memory.settings.arguments_both', 'both')
        add_item(self.arguments, 'memory.settings.arguments_active', 'active_mode')
        self.arguments.setCurrentIndex(self.arguments.findData(self.options['temp_query_arguments']))
        self.add_row(SettingRow(tr('memory.settings.arguments'), tr('memory.settings.arguments_help'),
                               self.arguments, search_terms=('query keywords запрос ключевые слова память'), parent=self))
        self.page_limit.valueChanged.connect(lambda value: self.write('page_limit', value))
        self.arguments.currentIndexChanged.connect(lambda _: self.write('temp_query_arguments', self.arguments.currentData()))

    def write(self, key, value):
        candidate = dict(self.options, **{key: value})
        try:
            save_settings(candidate)
        except (OSError, ValueError) as error:
            control = self.page_limit if key == 'page_limit' else self.arguments
            control.blockSignals(True)
            if key == 'page_limit':
                control.setValue(self.options[key])
            else:
                control.setCurrentIndex(control.findData(self.options[key]))
            control.blockSignals(False)
            MessageBox.warning(self, tr('nav.memory'), str(error))
        else:
            self.options = candidate
