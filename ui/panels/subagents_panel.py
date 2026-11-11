"""Reusable subagent definitions; execution details belong to the chat."""
from ui.icons import set_icon
from ui.styles.binding import form_field, apply_style
from ui.i18n import bind_caption, add_caption_item, tr, localize, set_text, add_item, add_row


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QToolButton, QCheckBox, QStackedWidget, QScrollArea, QFormLayout, QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QListWidget, QListWidgetItem)
from ui.widgets.dialogs import MessageBox as QMessageBox
from shiboken6 import isValid
from ui.dock_system.dock_panel import DockPanel
from ui.core_bridge.subagents_client import subagents_client
from ui.event_bus import event_bus
from core.model_connections import load_settings


class SubagentsPanelContent(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = subagents_client()
        self.identity = ""
        self.drafts = {}
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(localize(QLabel('', self), 'ui.subagents_panel.subagents_cf58'), 1)
        self.add = QToolButton(self)
        set_icon(self.add, 'add', square=True)
        self.add.setObjectName('SubagentToolButton')
        self.add.setFixedSize(28, 28)
        set_text(self.add, 'ui.subagents_panel.create_empty_template_2fed', setter='setToolTip')
        self.add.clicked.connect(self.create)
        self.context_stats = QToolButton(self)
        set_icon(self.context_stats, 'chart-pie', square=True)
        self.context_stats.setObjectName('SubagentToolButton')
        self.context_stats.setFixedSize(28, 28)
        set_text(self.context_stats, 'ui.subagents_panel.subagent_context_7764', setter='setAccessibleName')
        header.addWidget(self.context_stats)
        header.addWidget(self.add)
        layout.addLayout(header)
        self.notice = QLabel(self)
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("color:#eab308;font-size:11px;")
        layout.addWidget(self.notice)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        listing = QWidget(self.stack)
        listing_layout = QVBoxLayout(listing)
        listing_layout.setContentsMargins(0, 0, 0, 0)
        limits = QHBoxLayout()
        limits.addWidget(localize(QLabel('', listing), 'ui.subagents_panel.concurrent_016f'), 1)
        self.parallel = QSpinBox(listing)
        self.parallel.setRange(1, 32)
        self.parallel.setValue(4)
        self.parallel.valueChanged.connect(self.parallel_changed)
        limits.addWidget(self.parallel)
        listing_layout.addLayout(limits)
        self.list = QListWidget(listing)
        apply_style(self.list,'QListWidget::item, QListWidget::item:hover, QListWidget::item:selected {background:transparent;border:0;}')
        self.list.itemClicked.connect(lambda item: self.open(item.data(Qt.UserRole)))
        listing_layout.addWidget(self.list)
        self.stack.addWidget(listing)
        editor = QWidget(self.stack)
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        back = set_icon(QToolButton(editor), 'left', size=18, square=True)
        back.setObjectName('SubagentToolButton')
        back.setFixedSize(28, 28)
        set_text(back, 'ui.subagents_panel.back_to_list_901e', setter='setToolTip')
        back.clicked.connect(self.back)
        top.addWidget(back)
        save = set_icon(QToolButton(editor), 'save', size=18, square=True)
        save.setObjectName('SubagentToolButton')
        save.setFixedSize(28, 28)
        set_text(save, 'ui.settings.save_b4d3', setter='setToolTip')
        save.clicked.connect(self.save)
        top.addWidget(save)
        editor_layout.addLayout(top)
        scroll = QScrollArea(editor)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        editor_layout.addWidget(scroll)
        body = QWidget(scroll)
        scroll.setWidget(body)
        form = QFormLayout(body)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.name = QLineEdit(body)
        add_row(form, 'ui.subagents_panel.name_0918', self.name)
        self.id_label = QLabel(body)
        self.id_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.id_label.hide()
        self.description = form_field(QPlainTextEdit(body))
        set_text(self.description, 'ui.subagents_panel.briefly_describe_when_the_main_agent_b657', setter='setPlaceholderText')
        set_text(self.description, 'ui.subagents_panel.the_short_description_appears_in_the_b83d', setter='setToolTip')
        self.description.setMaximumHeight(80)
        add_row(form, 'ui.subagents_panel.purpose_for_the_main_agent_e579', self.description)
        self.profile = QComboBox(body)
        self.profile.currentIndexChanged.connect(self.profile_changed)
        add_row(form, 'ui.subagents_panel.model_f3bd', self.profile)
        self.endpoint = QLabel(body)
        self.endpoint.setWordWrap(True)
        self.endpoint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.endpoint)
        api = set_icon(QToolButton(body), 'settings', size=18, square=True)
        api.setObjectName('SubagentToolButton')
        api.setFixedSize(28, 28)
        set_text(api, 'ui.subagents_panel.configure_model_url_api_5f06', setter='setToolTip')
        api.clicked.connect(self.open_api)
        form.addRow(api)
        self.use_system = set_icon(QToolButton(editor), 'code', size=18, square=True)
        self.use_system.setObjectName('SubagentToolButton')
        self.use_system.setFixedSize(28, 28)
        set_text(self.use_system, 'ui.subagents_panel.no_system_context', setter='setAccessibleName')
        set_text(self.use_system, 'ui.subagents_panel.no_system_context_help', setter='setToolTip')
        self.use_system.setCheckable(True)
        self.use_system.setChecked(False)
        self.prompt = form_field(QPlainTextEdit(body))
        self.use_system.toggled.connect(lambda disabled: self.prompt.setEnabled(not disabled))
        self.prompt.setMinimumHeight(150)
        add_row(form, 'ui.subagents_panel.subagent_system_prompt_4734', self.prompt)
        self.context = QComboBox(body)
        add_item(self.context, 'ui.subagents_panel.task_and_attached_files_17a3', "task")
        add_item(self.context, 'ui.subagents_panel.conversation_copy_and_task_d6f0', "conversation")
        add_row(form, 'ui.subagents_panel.initial_context_8f2c', self.context)
        self.direct = localize(QCheckBox('', body), 'ui.subagents_panel.show_response_directly_to_the_user_b94e')
        set_text(self.direct, 'ui.subagents_panel.the_full_result_is_retained_in_bc6a', setter='setToolTip')
        form.addRow(self.direct)
        self.steps = QSpinBox(body)
        self.steps.setRange(1, 100)
        add_row(form, 'ui.subagents_panel.maximum_steps_3205', self.steps)
        self.modules = QListWidget(body)
        set_text(self.modules, 'ui.subagents_panel.at_launch_this_list_is_intersected_a86f', setter='setToolTip')
        self.modules.setMinimumHeight(150)
        add_row(form, 'ui.subagents_panel.allowed_modules_4329', self.modules)
        self.preview = set_icon(QToolButton(body), 'eye', size=18, square=True)
        self.preview.setObjectName('SubagentToolButton')
        self.preview.setFixedSize(28, 28)
        set_text(self.preview, 'ui.subagents_panel.view_prompt_9702', setter='setToolTip')
        self.preview.clicked.connect(self.preview_prompt)
        top.addWidget(self.preview)
        top.addWidget(self.use_system)
        duplicate = set_icon(QToolButton(body), 'copy', size=18, square=True)
        duplicate.setObjectName('SubagentToolButton')
        duplicate.setFixedSize(28, 28)
        set_text(duplicate, 'ui.subagents_panel.duplicate_template_b503', setter='setToolTip')
        duplicate.clicked.connect(self.duplicate)
        top.addWidget(duplicate)
        remove = set_icon(QToolButton(body), 'delete', size=18, square=True)
        remove.setObjectName('SubagentToolButton')
        remove.setFixedSize(28, 28)
        set_text(remove, 'ui.subagents_panel.delete_template_4ff0', setter='setToolTip')
        remove.clicked.connect(self.delete)
        top.addWidget(remove)
        top.addStretch(1)
        self.stack.addWidget(editor)
        self.client.changed.connect(self.refresh)
        self.client.failed.connect(self.show_error)
        event_bus.subagent_settings_requested.connect(self.open)
        event_bus.model_profiles_changed.connect(self.reload_profiles)
        self.refresh(self.client.data)

    def show_error(self, text):
        bind_caption(self.notice, text)

    def refresh(self, data):
        self._update_statistics(data)
        self._filling = True
        self.list.clear()
        pending = data.get("pending") or {}
        for identity, cfg in (data.get("subagents") or {}).items():
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, identity)
            row = QWidget(self.list)
            box = QHBoxLayout(row)
            box.setContentsMargins(6, 4, 6, 4)
            button = QPushButton(cfg["name"], row)
            button.setFlat(True)
            bind_caption(button, cfg.get("description", ""), setter='setToolTip')
            button.clicked.connect(
                lambda checked=False, identity=identity: self.open(identity)
            )
            box.addWidget(button, 1)
            toggle = QToolButton(row)
            toggle.setObjectName('SubagentToolButton')
            toggle.setCheckable(True)
            toggle.setChecked(bool(pending.get(identity, cfg).get("enabled")))
            set_icon(toggle, 'stop' if toggle.isChecked() else 'run', size=18)
            toggle.setFixedSize(28, 28)
            bind_caption(toggle, tr('ui.caption.d4b62204494f')
                if identity in pending
                else tr('ui.caption.4334b00288ba'), setter='setToolTip')
            toggle.clicked.connect(
                lambda checked, identity=identity: self.toggle(identity, checked)
            )
            box.addWidget(toggle)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        self.parallel.setValue((data.get("settings") or {}).get("max_parallel", 4))
        self._filling = False
        if not data.get("module_enabled"):
            set_text(self.notice, 'ui.subagents_panel.the_subagent_module_is_disabled_templates_6a7a')
        elif pending or data.get("settings_pending"):
            set_text(self.notice, 'ui.subagents_panel.changes_apply_after_all_generations_finish_e6dc')
        else:
            self.notice.clear()

    def toggle(self, identity, enabled):
        self.client.mutate("save", identity, {"enabled": enabled})

    def parallel_changed(self, value):
        if not self._filling:
            self.client.mutate(
                "settings", data={"max_parallel": value}
            )

    def saved(self, result):
        if not isValid(self):
            return
        bind_caption(self.notice, tr('ui.caption.a9d7642d4548')
            if result.get("pending")
            else tr('ui.caption.7c719c2d1f2b'))

    def create(self):
        def ready(result):
            if isValid(self):
                self.open(result["subagent"]["id"], result["subagent"])

        self.client.mutate("create", data={"name": tr("ui.subagents_panel.new_template")}, done=ready)

    def back(self):
        if self.identity:
            self.drafts[self.identity] = self.capture()
        self.stack.setCurrentIndex(0)

    def open(self, identity, data=None):
        if self.stack.currentIndex() == 1 and self.identity:
            if identity == self.identity and data is None:
                return
            self.drafts[self.identity] = self.capture()
        cfg = (
            data
            or self.drafts.get(identity)
            or (self.client.data.get("pending") or {}).get(identity)
            or (self.client.data.get("subagents") or {}).get(identity)
        )
        if not cfg:
            return
        self.identity = identity
        self.original = dict(cfg)
        bind_caption(self.name, cfg.get("name", ""))
        bind_caption(self.id_label, identity)
        self.description.setPlainText(cfg.get("description", ""))
        self.prompt.setPlainText(cfg.get("system_prompt", ""))
        self.use_system.setChecked(not cfg.get('use_system_prompt', True))
        self.prompt.setEnabled(not self.use_system.isChecked())
        self.steps.setValue(int(cfg.get("max_steps", 10)))
        self.direct.setChecked(bool(cfg.get("direct_output")))
        self.context.setCurrentIndex(
            1 if cfg.get("context_mode") == "conversation" else 0
        )
        self.reload_profiles(
            cfg.get("model_profile_id", "") or ("__legacy__" if cfg.get("llm") else "")
        )
        self.modules.clear()
        records = {m["name"]: m for m in self.client.data.get("modules", [])}
        for name in sorted(
            (set(records) | set(cfg.get("allowed_modules") or []))
            - {"subagent_manager"}
        ):
            module = records.get(name, {})
            item = QListWidgetItem(
                name + (tr('ui.caption.63fe4bf60c2c') if not module.get("enabled") else ""),
                self.modules,
            )
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if name in cfg.get("allowed_modules", []) else Qt.Unchecked
            )
        self.stack.setCurrentIndex(1)

    def reload_profiles(self, selected=None):
        if not isinstance(selected, str):
            selected = self.profile.currentData() or ""
        self.profile.blockSignals(True)
        self.profile.clear()
        add_item(self.profile, 'ui.subagents_panel.same_as_the_main_agent_d7a0', "")
        if getattr(self, "original", {}).get("llm"):
            add_item(self.profile, 'ui.subagents_panel.previous_subagent_connection_99b7', "__legacy__")
        for record in load_settings().get("connections", []):
            self.profile.addItem(
                record.get("name") or record.get("model") or record["id"], record["id"]
            )
        if selected and self.profile.findData(selected) < 0:
            self.profile.addItem(tr('subagents.unavailable_profile', profile=selected), selected)
        self.profile.setCurrentIndex(max(0, self.profile.findData(selected)))
        self.profile.blockSignals(False)
        self.profile_changed()

    def profile_changed(self, *_):
        identity = self.profile.currentData()
        record = next(
            (r for r in load_settings().get("connections", []) if r["id"] == identity),
            None,
        )
        if identity == "__legacy__":
            bind_caption(self.endpoint, str(self.original["llm"].get("api_url", ""))
                + "\n"
                + str(self.original["llm"].get("model_name", "")))
            return
        bind_caption(self.endpoint, (str(record.get("base_url", "")) + "\n" + str(record.get("model", "")))
            if record else tr('subagents.unavailable_profile', profile=identity)
            if identity else tr('ui.caption.d7d5cc81a911'))

    def capture(self):
        cfg = dict(
            self.original,
            name=self.name.text(),
            description=self.description.toPlainText(),
            system_prompt=self.prompt.toPlainText(),
            use_system_prompt=not self.use_system.isChecked(),
            model_profile_id=(
                ""
                if self.profile.currentData() == "__legacy__"
                else self.profile.currentData() or ""
            ),
            max_steps=self.steps.value(),
            direct_output=self.direct.isChecked(),
            context_mode=self.context.currentData(),
            allowed_modules=[
                self.modules.item(i).data(Qt.UserRole)
                for i in range(self.modules.count())
                if self.modules.item(i).checkState() == Qt.Checked
            ],
        )
        if self.profile.currentData() != "__legacy__":
            cfg.pop("llm", None)
        current = (self.client.data.get("pending") or {}).get(self.identity) or (
            self.client.data.get("subagents") or {}
        ).get(self.identity)
        if current is not None:
            cfg["enabled"] = current.get("enabled", False)
        return cfg

    def save(self):
        if not self.identity:
            return
        self.drafts.pop(self.identity, None)
        self.client.mutate("save", self.identity, self.capture(), done=self.saved)

    def delete(self):
        if not self.identity:
            return
        decision = QMessageBox.question(
            self,
            tr('ui.subagents_panel.delete_subagent_template_e047'),
            tr('ui.caption.af737f409602')
            + str(self.original.get("name") or "Субагент")
            + tr('ui.caption.5888926faac1'),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if decision != QMessageBox.Yes:
            return
        self.client.mutate("delete", self.identity, done=self.saved)
        self.drafts.pop(self.identity, None)
        self.identity = ""
        self.stack.setCurrentIndex(0)

    def duplicate(self):
        data = self.capture()
        data["enabled"] = False
        data["name"] += tr("ui.subagents_panel.copy_suffix")

        def created(result):
            if not isValid(self):
                return
            identity = result["subagent"]["id"]
            self.client.mutate(
                "save",
                identity,
                data,
                done=lambda result: (
                    self.open(identity, data) if isValid(self) else None
                ),
            )

        self.client.mutate("create", done=created)

    def _update_statistics(self, data):
        from html import escape
        from ui.app_state import app_state

        if not app_state.session_id:
            set_text(self.context_stats, 'ui.subagents_panel.select_a_chat_to_view_subagent_582a', setter='setToolTip')
            return
        stats = data.get("context_stats") or {}
        rows = []
        for identity, cfg in (data.get("subagents") or {}).items():
            values = stats.get(identity) or {}
            total, limit = values.get("total"), values.get("limit")

            def number(value):
                return "—" if value is None else f"{value:,}".replace(",", " ")

            text = number(total) + " / " + number(limit)
            if total is not None and limit is not None and total > limit:
                text = '<span style="color:#ef4444">' + text + "</span>"
            if values.get("status") in {"running", "queued"}:
                text += tr('ui.caption.517543e3e5a2')
            rows.append(
                "<tr><td>"
                + escape(str(cfg.get("name") or "Субагент"))
                + '</td><td style="padding-left:14px">'
                + text
                + "</td></tr>"
            )
        bind_caption(self.context_stats, tr('ui.caption.8c5485f5d4da')
            + "".join(rows)
            + "</table>", setter='setToolTip')

    def preview_prompt(self):
        if self.identity:
            event_bus.subagent_prompt_requested.emit(self.identity, self.capture())

    def open_api(self):
        event_bus.subagent_api_settings_requested.emit(
            ""
            if self.profile.currentData() == "__legacy__"
            else self.profile.currentData() or ""
        )


class SubagentsPanel(DockPanel):
    def __init__(self, parent=None):
        super().__init__(
            panel_id="subagents_panel",
            title_key="panel_subagents",
            content_widget=SubagentsPanelContent(),
            parent=parent,
        )
