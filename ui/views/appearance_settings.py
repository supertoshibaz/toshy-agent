"""Theme and language controls in the existing Personalization settings page."""
from ui.i18n import bind_caption, add_caption_item, i18n, tr, localize
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                              QLabel, QComboBox, QPushButton, QLineEdit, QCheckBox, QSpinBox)
from ui.themes.manager import theme_manager
from ui.themes.resources import custom_theme, save_custom
from ui.widgets.dialogs import Dialog, MessageBox, equal_buttons
from ui.views.startup_view import SettingsPage, SettingRow


class CustomThemeDialog(Dialog):
    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self.theme = theme or {}
        self.theme_id = None
        localize(self, 'appearance.custom_title', setter='setWindowTitle')
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.name_edit = QLineEdit(self.theme.get('name', ''), self)
        self.foundation = QComboBox(self)
        add_caption_item(self.foundation, tr('appearance.foundation_dark'), 'dark')
        add_caption_item(self.foundation, tr('appearance.foundation_light'), 'light')
        i18n.bind(self.foundation, 'appearance.foundation_dark', setter='setItemText', index=0)
        i18n.bind(self.foundation, 'appearance.foundation_light', setter='setItemText', index=1)
        self.foundation.setCurrentIndex(1 if self.theme.get('foundation') == 'light' else 0)
        self.tone = QLineEdit(self.theme.get('tone', '#55EBC5'), self)
        for key, field in [('appearance.name', self.name_edit), ('appearance.foundation', self.foundation), ('appearance.tone', self.tone)]:
            label = localize(QLabel(self), key)
            form.addRow(label, field)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        self.save_button = localize(QPushButton(self), 'common.save')
        self.cancel_button = localize(QPushButton(self), 'common.cancel')
        self.save_button.clicked.connect(self.save)
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.save_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.save_button.setDefault(True)
        i18n.language_changed.connect(self.equalize)
        self.equalize()

    def equalize(self, *_):
        equal_buttons([self.save_button, self.cancel_button])

    def save(self):
        try:
            data = custom_theme(self.name_edit.text(), self.foundation.currentData(), self.tone.text().strip(),
                                identity=self.theme.get('id'))
            save_custom(data)
        except (OSError, ValueError) as error:
            bind_caption(self.error, tr('appearance.invalid_theme', details=str(error)))
            self.error.show()
            self._fit_contents()
            return
        self.theme_id = data['id']
        self.accept()


class AppearanceSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('appearance.title'), tr('appearance.description'), ('App', 'Personalization'), parent)
        self.themes = QComboBox(self)
        self.languages = QComboBox(self)
        self.add_row(SettingRow(tr('appearance.theme'), tr('appearance.theme_description'), self.themes, parent=self))
        self.add_row(SettingRow(tr('appearance.language'), tr('appearance.language_description'), self.languages, parent=self))
        controls = QWidget(self)
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        self.create_button = localize(QPushButton(controls), 'appearance.create')
        self.edit_button = localize(QPushButton(controls), 'appearance.edit')
        row.addWidget(self.create_button)
        row.addWidget(self.edit_button)
        row.addStretch()
        self.add_row(SettingRow(tr('appearance.custom'), '', controls, parent=self))
        from ui.tooltips import tooltip_preferences
        self.tooltip_preferences = tooltip_preferences
        self.tooltips_enabled = QCheckBox(self)
        self.tooltip_delay = QSpinBox(self)
        self.tooltip_delay.setRange(0, 5000)
        self.tooltip_delay.setSingleStep(50)
        self.tooltip_delay.setKeyboardTracking(False)
        self.add_row(SettingRow(tr('appearance.tooltips'), tr('appearance.tooltips_help'), self.tooltips_enabled,
                                search_terms=('tooltip подсказки задержка',), parent=self))
        self.add_row(SettingRow(tr('appearance.tooltip_delay'), tr('appearance.tooltip_delay_help'), self.tooltip_delay,
                                search_terms=('tooltip delay подсказки задержка',), parent=self))
        self.tooltips_enabled.toggled.connect(lambda value: self.write_tooltips(enabled=value))
        self.tooltip_delay.valueChanged.connect(lambda value: self.write_tooltips(delay_ms=value))
        tooltip_preferences.changed.connect(self.refresh_tooltips)
        i18n.language_changed.connect(self.refresh_tooltips)
        self.refresh_tooltips()
        self.restart_notice = localize(QLabel(self), 'appearance.restart_required')
        self.restart_notice.setWordWrap(True)
        self.body_layout.insertWidget(self.body_layout.indexOf(self.rows_host)+1,self.restart_notice)
        self.themes.currentIndexChanged.connect(self.select_theme)
        self.languages.currentIndexChanged.connect(self.select_language)
        self.create_button.clicked.connect(self.create_theme)
        self.edit_button.clicked.connect(self.edit_theme)
        i18n.language_changed.connect(self.refresh)
        theme_manager.selection_changed.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        selected = theme_manager.selected_id
        theme_manager.refresh()
        self.themes.blockSignals(True)
        self.themes.clear()
        for identity, item in theme_manager.themes.items():
            name = tr('appearance.theme_' + identity) if item['builtin'] else item['name']
            add_caption_item(self.themes, name, identity)
        self.themes.setCurrentIndex(self.themes.findData(selected))
        self.themes.blockSignals(False)
        self.languages.blockSignals(True)
        self.languages.clear()
        # Additional JSON packs are discoverable without Python changes.
        for code in i18n._translations:
            self.languages.addItem({'en': 'English', 'ru': 'Русский'}.get(code, code), code)
        self.languages.setCurrentIndex(self.languages.findData(i18n.current_lang))
        self.languages.blockSignals(False)
        self.edit_button.setEnabled(not theme_manager.themes.get(selected, {}).get('builtin', True))
        self.restart_notice.setVisible(theme_manager.restart_required)
        equal_buttons([self.create_button, self.edit_button])

    def select_theme(self, *_):
        identity = self.themes.currentData()
        if identity and identity != theme_manager.selected_id:
            theme_manager.request(identity)

    def refresh_tooltips(self, *_):
        for control, value in ((self.tooltips_enabled, self.tooltip_preferences.enabled),
                               (self.tooltip_delay, self.tooltip_preferences.delay_ms)):
            blocked = control.blockSignals(True)
            try:
                control.setChecked(value) if isinstance(control, QCheckBox) else control.setValue(value)
            finally:
                control.blockSignals(blocked)
        self.tooltip_delay.setEnabled(self.tooltip_preferences.enabled)
        bind_caption(self.tooltip_delay, tr('appearance.milliseconds'), setter='setSuffix')

    def write_tooltips(self, **values):
        try:
            self.tooltip_preferences.update(**values)
        except OSError as error:
            MessageBox.warning(self, tr('appearance.title'), str(error))
            self.refresh_tooltips()

    def select_language(self, *_):
        language = self.languages.currentData()
        if language:
            i18n.set_language(language)

    def create_theme(self, *_):
        self._edit()

    def edit_theme(self, *_):
        theme = theme_manager.themes.get(theme_manager.selected_id)
        if theme and not theme['builtin']:
            self._edit(theme)

    def _edit(self, theme=None):
        dialog = CustomThemeDialog(self, theme)
        if dialog.exec() == Dialog.Accepted and dialog.theme_id:
            theme_manager.request(dialog.theme_id)
        dialog.deleteLater()
