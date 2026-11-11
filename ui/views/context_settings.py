"""Startup → Core → Context."""
from ui.i18n import bind_caption, add_caption_item, tr, localize, add_item


from PySide6.QtWidgets import (QCheckBox, QComboBox, QSpinBox)
from ui.widgets.dialogs import MessageBox as QMessageBox
from core.context_settings import load_settings, save_settings
from core.model_connections import load_settings as model_settings
from ui.views.startup_view import SettingsPage, SettingRow
from ui.event_bus import event_bus


class ContextSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(
            "Context",
            tr('ui.caption.284990447207'),
            ("Core", "Context"),
            parent,
        )
        self.options = load_settings()
        self.controls = {}
        self._loading = True
        self.toggle(
            "enabled",
            tr('ui.caption.9760619913c0'),
            tr('ui.caption.0defc71ffeda'),
        )
        self.spin(
            "threshold",
            "Compression threshold",
            tr('ui.caption.f0e40abc3e0d'),
            10,
            95,
            " %",
        )
        self.spin(
            "context_limit",
            "Context limit fallback",
            tr('ui.caption.63cdbda2f1e1'),
            0,
            100_000_000,
            tr('ui.caption.20016d708b6c'),
        )
        self.models = QComboBox(self)
        self.controls["summary_model_profile_id"] = self.models
        self.add_row(
            SettingRow(
                tr('ui.context_settings.summary_model_e271'),
                tr('ui.context_settings.uses_this_session_s_model_by_23ac'),
                self.models,
                parent=self,
            )
        )
        self.reload_models()
        self.models.currentIndexChanged.connect(
            lambda _: self.write("summary_model_profile_id", self.models.currentData())
        )
        tail = QComboBox(self)
        add_item(tail, 'ui.context_settings.lean_short_tail_9501', "lean")
        add_item(tail, 'ui.context_settings.legacy_threshold_fraction_dc0f', "legacy")
        self.controls["tail_mode"] = tail
        tail.setCurrentIndex(tail.findData(self.options["tail_mode"]))
        self.add_row(
            SettingRow(
                tr('ui.context_settings.recent_history_policy_dcef'),
                tr('ui.context_settings.lean_2_5_of_the_window_51b8'),
                tail,
                parent=self,
            )
        )
        tail.currentIndexChanged.connect(
            lambda _: self.write("tail_mode", tail.currentData())
        )
        self.spin(
            "target_ratio",
            "Legacy tail size",
            tr('ui.caption.2051aef5dbdc'),
            5,
            50,
            " %",
        )
        self.spin(
            "min_tail_user_messages",
            "Preserve recent user messages",
            tr('ui.caption.da34a4d7ca39'),
            1,
            20,
        )
        event_bus.model_profiles_changed.connect(self.reload_models)
        self._loading = False
        self.update_enabled()

    def toggle(self, key, title, description):
        control = localize(QCheckBox('', self), 'ui.context_settings.enabled_4d1d')
        control.setChecked(self.options[key])
        self.controls[key] = control
        self.add_row(
            SettingRow(
                title,
                description,
                control,
                search_terms=("context compression сжатие контекст"),
                parent=self,
            )
        )
        control.toggled.connect(lambda value: self.write(key, value))

    def spin(self, key, title, description, low, high, suffix=""):
        control = QSpinBox(self)
        control.setRange(low, high)
        bind_caption(control, suffix, setter='setSuffix')
        control.setValue(self.options[key])
        if key == "context_limit":
            bind_caption(control, tr('ui.caption.a402b3d034fb'), setter='setSpecialValueText')
        self.controls[key] = control
        self.add_row(
            SettingRow(
                title,
                description,
                control,
                search_terms=("context compression сжатие контекст"),
                parent=self,
            )
        )
        control.valueChanged.connect(lambda value: self.write(key, value))

    def reload_models(self):
        selected = self.options["summary_model_profile_id"]
        blocked = self.models.blockSignals(True)
        try:
            self.models.clear()
            add_item(self.models, 'ui.context_settings.current_session_model_6fdf', "")
            for item in model_settings()["connections"]:
                self.models.addItem(str(item.get("name") or item["id"]), item["id"])
            index = self.models.findData(selected)
            if index < 0:
                add_caption_item(self.models, tr('ui.caption.9b279d0e8359') + selected, selected)
                index = self.models.count() - 1
            self.models.setCurrentIndex(index)
        finally:
            self.models.blockSignals(blocked)

    def update_enabled(self):
        self.controls["target_ratio"].setEnabled(self.options["tail_mode"] == "legacy")


    def write(self, key, value):
        if self._loading:
            return
        candidate = dict(self.options, **{key: value})
        try:
            save_settings(candidate)
            self.options = candidate
            self.update_enabled()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, tr('ui.context_settings.context_a6e6'), str(error))
