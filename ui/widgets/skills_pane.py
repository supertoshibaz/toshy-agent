"""Custom-module skill resources. Local authoring also works with the core off."""
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text, add_item


from pathlib import Path
from types import SimpleNamespace
import json
from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QComboBox,
    QFileDialog,
)
from core.security import auth_headers
from ui.core_bridge.transport import core_http_client
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.async_runner import AsyncRunner
from ui.event_bus import event_bus
from ui.widgets.resource_list import ResourceItem
from ui.widgets import inline_editor as inline
from modules.skills.store import SkillStore


def catalog(packages, owner):
    from modules.skills.runtime import SkillRuntime

    module = SimpleNamespace(
        module_name=owner, directory=packages.path(owner), store=packages, registry=None
    )
    return SkillRuntime(module).catalog()


class SkillsPane(QWidget):
    def __init__(self, packages, tree, parent=None):
        super().__init__(parent)
        self.packages, self.tree = packages, tree
        self.owner = None
        self.entry = None
        self.populating = False
        self.runner = AsyncRunner(self, max_threads=1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.title = localize(QLabel(''), 'ui.skills_pane.select_skill_e96d')
        self.title.setTextFormat(Qt.PlainText)
        self.description = QLabel()
        self.description.setTextFormat(Qt.PlainText)
        self.description.setWordWrap(True)
        self.source = QLabel()
        self.source.setTextFormat(Qt.PlainText)
        self.source.setWordWrap(True)
        self.dependency = QLabel()
        self.dependency.setWordWrap(True)
        self.enabled = localize(QCheckBox(''), 'ui.skills_pane.enabled_a87a')
        self.automatic = localize(QCheckBox(''), 'ui.skills_pane.allow_automatic_selection_by_the_model_1875')
        self.environment = QComboBox()
        set_text(self.environment, 'ui.skills_pane.only_used_for_running_scripts_environment_09b6', setter='setToolTip')
        for widget in (
            self.title,
            self.description,
            self.source,
            self.dependency,
            self.enabled,
            self.automatic,
            localize(QLabel(''), 'ui.skills_pane.execution_environment_5277'),
            self.environment,
        ):
            layout.addWidget(widget)
        self.open_button = localize(QPushButton(''), 'ui.skills_pane.open_skill_md_b036')
        self.open_button.clicked.connect(self.open_skill)
        layout.addWidget(self.open_button)
        for label, handler in [
            (tr('ui.caption.4aace8ad2b5e'), self.create),
            (tr('ui.caption.8b1f6b8f1bbb'), self.import_skill),
            (tr('ui.caption.0a388a2b5cfc'), self.copy_skill),
            (tr('ui.skills_pane.delete_skill_7dda'), self.delete),
        ]:
            button = bind_caption(QPushButton(''), label)
            button.clicked.connect(handler)
            if handler == self.create:
                self.create_button = button
            elif handler == self.import_skill:
                self.import_button = button
            layout.addWidget(button)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.enabled.toggled.connect(self.save_settings)
        self.automatic.toggled.connect(self.save_settings)
        self.environment.currentIndexChanged.connect(self.save_settings)
        tree.currentItemChanged.connect(self.select)

    def set_owner(self, owner):
        if self.owner != owner:
            self.entry = None
        self.owner = owner
        self.reload()

    def reload(self):
        selected = self.entry["key"] if self.entry else None
        error = ""
        self.populating = True
        try:
            with QSignalBlocker(self.tree):
                self.tree.clear()
                current = None
                try:
                    entries = catalog(self.packages, self.owner) if self.owner else []
                except (OSError, ValueError) as exc:
                    entries = []
                    error = tr('ui.caption.cf4852966a1d') + str(exc)
                for entry in entries:
                    label = ("✓ " if entry["enabled"] else "○ ") + entry["name"]
                    if entry["source"] == "mcp":
                        label += " · " + entry["source_name"]
                    item = ResourceItem(label)
                    item.setData(Qt.UserRole, entry)
                    bind_caption(item, entry.get("error") or entry["description"], setter='setToolTip', index=0)
                    add_caption_item(self.tree, item)
                    if entry["key"] == selected:
                        current = item
                self.tree.setCurrentItem(current or self.tree.item(0))
        finally:
            self.populating = False
        self.select(self.tree.currentItem())
        if error:
            bind_caption(self.status, error)

    def select(self, item, *_):
        if self.populating:
            return
        self.populating = True
        try:
            self.entry = item.data(Qt.UserRole) if item else None
            entry = self.entry or {}
            bind_caption(self.title, entry.get("name", tr('ui.skills_pane.select_skill_e96d')))
            bind_caption(self.description, entry.get("description", ""))
            bind_caption(self.source, tr('ui.caption.f30d1d487948')
                + (entry.get("source_name") or tr('ui.caption.235a517bab24'))
                + ("\n" + entry["uri"] if entry.get("uri") else ""))
            bind_caption(self.dependency, tr('ui.caption.b60b6ebf4552')
                if entry.get("scripts")
                else tr('ui.caption.117551b8b6c6'))
            self.enabled.setChecked(bool(entry.get("enabled")))
            self.automatic.setChecked(bool(entry.get("automatic")))
            self.enabled.setEnabled(bool(entry))
            self.automatic.setEnabled(bool(entry))
            self.environment.clear()
            add_item(self.environment, 'ui.skills_pane.current_session_terminal_17ec', "")
            try:
                from modules.system.terminal_sandbox.store import ContainerStore

                for profile in ContainerStore().profiles():
                    add_caption_item(self.environment, profile["name"]
                        + " · "
                        + profile["backend"]
                        + ("" if profile["active"] else tr('ui.caption.111a4e391195')), profile["id"])
            except ImportError:
                pass
            identity = entry.get("environment") or ""
            index = self.environment.findData(identity)
            if index < 0:
                add_caption_item(self.environment, tr('ui.caption.dd3fadaf5964') + identity, identity)
                index = self.environment.count() - 1
            self.environment.setCurrentIndex(index)
            self.environment.setEnabled(bool(entry))
            self.open_button.setEnabled(bool(entry))
            bind_caption(self.status, entry.get("error", ""))
        finally:
            self.populating = False

    def save_settings(self, *_):
        if self.populating or not self.entry or not self.owner:
            return
        try:
            SkillStore(self.packages, self.owner).configure(
                self.entry["key"],
                enabled=self.enabled.isChecked(),
                automatic=self.automatic.isChecked(),
                environment=self.environment.currentData(),
            )
            self.reload()
            event_bus.pipeline_resources_changed.emit()
        except Exception as error:
            bind_caption(self.status, str(error))

    def create(self):
        if not self.owner:
            return
        owner = self.owner

        def create(name):
            import yaml

            text = (
                "---\n"
                + yaml.safe_dump(
                    {
                        "name": name,
                        "description": "Опишите задачу и условия применения скилла.",
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n\n# Порядок работы\n\n1. Уточните входные данные.\n2. Выполните задачу.\n3. Проверьте результат.\n"
            )
            key = SkillStore(self.packages, owner).install_files(
                {"SKILL.md": text.encode("utf-8")}
            )
            event_bus.pipeline_resources_changed.emit()
            event_bus.open_file_requested.emit(
                str(SkillStore(self.packages, owner).directory(key) / "SKILL.md")
            )
            event_bus.switch_view_tab_requested.emit(2)

        inline.text_input(
            self, tr('ui.skills_pane.skill_name_lowercase_latin_letters_digits_708f'), "", create,
            anchor=self.create_button
        )

    def import_skill(self, _checked=False, *, anchor=None):
        if not self.owner:
            return
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        folder = localize(add_caption_action(menu, 'addAction', "Импорт папки"), 'ui.skills_pane.import_folder_9912')
        archive = localize(add_caption_action(menu, 'addAction', "Импорт ZIP"), 'ui.skills_pane.import_zip_006c')
        trigger = anchor or self.import_button
        chosen = menu.exec(trigger.mapToGlobal(trigger.rect().bottomLeft()))
        if chosen == folder:
            source = QFileDialog.getExistingDirectory(self, "Папка с SKILL.md")
        elif chosen == archive:
            source, _ = QFileDialog.getOpenFileName(
                self, "ZIP со скиллом", "", "ZIP (*.zip)"
            )
        else:
            return
        if source:
            owner = self.owner
            self.runner.submit(
                lambda: SkillStore(self.packages, owner).import_path(source),
                self.changed,
                self.failed,
            )

    def changed(self, *_):
        self.status.clear()
        self.reload()
        event_bus.pipeline_resources_changed.emit()

    def failed(self, error):
        bind_caption(self.status, str(error))

    def request(self, action, entry, **values):
        if not core_process.is_running():
            raise ValueError(
                "Для чтения MCP-скилла запустите ядро и подключите сервер."
            )
        url = (
            core_process.base_url + "/v1/modules/" + entry["owner"] + "/service/skills"
        )
        with core_http_client(core_process.base_url, timeout=90) as client:
            response = client.post(
                url,
                json=dict(action=action, skill=entry["key"], **values),
                headers=auth_headers(),
            )
            if response.is_error:
                raise ValueError(response.json().get("detail", response.text))
            return response.json()

    def open_skill(self):
        if not self.entry:
            return
        entry = dict(self.entry)
        if entry["source"] == "local":
            event_bus.open_file_requested.emit(
                str(
                    SkillStore(self.packages, self.owner).directory(entry["key"])
                    / "SKILL.md"
                )
            )
            event_bus.switch_view_tab_requested.emit(2)
            return

        def opened(result):
            window = self.window()
            window.file_view.open_temporary_markdown(
                key="skill:" + entry["id"],
                title=entry["name"] + ".md",
                content=result["content"],
                breadcrumb="MCP / " + entry["source_name"] + " / " + entry["name"],
            )
            event_bus.switch_view_tab_requested.emit(2)

        self.runner.submit(lambda: self.request("read", entry), opened, self.failed)

    def copy_skill(self):
        if not self.entry:
            return
        entry = dict(self.entry)

        def copy(name):
            if entry["source"] == "mcp":
                self.runner.submit(
                    lambda: self.request("copy", entry, name=name),
                    self.changed,
                    self.failed,
                )
            else:
                from modules.skills.store import frontmatter
                import re, yaml

                store = SkillStore(self.packages, entry["owner"])
                files = store.read(entry["key"])
                text = files["SKILL.md"].decode("utf-8-sig")
                metadata = frontmatter(text)
                metadata["name"] = name
                body = re.split(r"\r?\n---(?:\s*\r?\n|\s*\Z)", text, maxsplit=1)[1]
                files["SKILL.md"] = (
                    "---\n"
                    + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
                    + "---\n"
                    + body
                ).encode("utf-8")
                store.install_files(files)
                self.changed()

        inline.text_input(self, tr('ui.skills_pane.local_copy_name_6259'), entry["name"] + "-copy", copy)

    def delete(self):
        if not self.entry:
            return
        entry = dict(self.entry)
        if entry["source"] != "local":
            set_text(self.status, 'ui.skills_pane.this_skill_belongs_to_an_mcp_3f46')
            return

        def remove():
            SkillStore(self.packages, entry["owner"]).remove(entry["key"])
            self.changed()

        inline.confirm(
            self,
            tr('ui.skills_pane.delete_skill_7dda'),
            tr('ui.caption.e5e1e8cf4876') + entry["name"] + tr('ui.caption.2ff3627498c1'),
            remove,
        )
