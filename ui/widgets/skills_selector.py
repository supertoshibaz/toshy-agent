"""Explicit per-request skill choices in the chat's tools menu."""
from ui.i18n import add_caption_action, bind_caption, tr, localize


from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QMenu
from core.security import auth_headers
from ui.core_bridge.transport import core_http_client
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.async_runner import AsyncRunner
from ui.app_state import app_state


class ChoiceMenu(QMenu):
    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.position().toPoint())
        if (
            event.button() == Qt.LeftButton
            and action is not None
            and action.isCheckable()
            and action.isEnabled()
        ):
            action.trigger()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        action = self.activeAction()
        if (
            event.key() in {Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter}
            and action is not None
            and action.isCheckable()
            and action.isEnabled()
        ):
            action.trigger()
            return
        super().keyPressEvent(event)


class SkillsSelector(QObject):
    changed = Signal()

    def __init__(self, chat, parent_menu):
        super().__init__(chat)
        self.chat = chat
        self.selected = set()
        self.entries = []
        self.runner = AsyncRunner(self, max_threads=1)
        self.menu = ChoiceMenu(tr('ui.caption.1bf02da215ac'), parent_menu)
        self.menu.setObjectName("ChatMessageContextMenu")
        add_caption_action(parent_menu, 'addMenu', self.menu)
        self.menu.aboutToShow.connect(self.refresh)
        self.error = ""
        self.pending = False
        self.generation = 0
        self.populate()

    def reset(self):
        self.set_selected([])

    def set_selected(self, values, *, clear_catalog=True):
        self.generation += 1
        self.pending = False
        self.selected = {str(value) for value in values if value}
        if clear_catalog:
            self.entries.clear()
        self.error = ''
        self.populate()
        self.changed.emit()

    def toggle(self, identity, checked):
        if checked:
            self.selected.add(identity)
        else:
            self.selected.discard(identity)
        bind_caption(self.menu, tr('ui.caption.1bf02da215ac') + (f" ({len(self.selected)})" if self.selected else ""), setter='setTitle')
        self.changed.emit()

    def consume(self):
        result = sorted(self.selected)
        self.selected.clear()
        self.populate()
        self.changed.emit()
        return result

    def populate(self):
        self.menu.clear()
        bind_caption(self.menu, tr('ui.caption.1bf02da215ac') + (f" ({len(self.selected)})" if self.selected else ""), setter='setTitle')
        if self.selected:
            localize(add_caption_action(self.menu, 'addAction', "Снять выбор"), 'ui.skills_selector.clear_selection_8bcd').triggered.connect(self.consume)
            self.menu.addSeparator()
        groups = {}
        listed = set()
        for entry in self.entries:
            if not entry.get("enabled"):
                continue
            owner = entry["owner"]
            if owner not in groups:
                group = ChoiceMenu(owner, self.menu)
                group.setObjectName("ChatMessageContextMenu")
                add_caption_action(self.menu, 'addMenu', group)
                groups[owner] = group
            action = add_caption_action(groups[owner], 'addAction', entry["name"]
                + (" · " + entry["source_name"] if entry.get("source_name") else ""))
            action.setCheckable(True)
            action.setChecked(entry["id"] in self.selected)
            action.setEnabled(not bool(entry.get("error")))
            bind_caption(action, entry.get("error") or entry["description"], setter='setToolTip')
            action.toggled.connect(
                lambda checked, identity=entry["id"]: self.toggle(identity, checked)
            )
            listed.add(entry["id"])
        for identity in sorted(self.selected - listed):
            action = add_caption_action(self.menu, 'addAction', identity + tr('ui.caption.bee71ba19032'))
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, identity=identity: self.toggle(identity, checked)
            )
        if self.error or not self.entries:
            add_caption_action(self.menu, 'addAction', self.error or tr('ui.caption.07e70dbc1f00')).setEnabled(False)

    def refresh(self):
        if self.pending:
            return
        if not core_process.is_running():
            self.error = tr('ui.caption.32e5fc50c6af')
            self.populate()
            return
        scope = {"branch": self.chat.current_branch or "main"}
        if self.chat.current_session_id:
            scope["session_id"] = self.chat.current_session_id
        else:
            identity = (
                self.chat._pending_new_session_folder_id
                or app_state.expanded_project_id
                or app_state.project_id
            )
            if identity:
                scope["project_id"] = identity
        generation = self.generation
        session = (self.chat.current_session_id, self.chat.current_branch)

        def read():
            with core_http_client(core_process.base_url, timeout=15) as client:
                response = client.get(
                    core_process.base_url + "/v1/resources/skills",
                    params=scope,
                    headers=auth_headers(),
                )
                response.raise_for_status()
                return response.json()["resources"]

        def finished(entries):
            if generation != self.generation or session != (
                self.chat.current_session_id,
                self.chat.current_branch,
            ):
                return
            self.pending = False
            self.entries, self.error = entries, ""
            self.populate()

        def failed(error):
            if generation == self.generation and session == (self.chat.current_session_id, self.chat.current_branch):
                self.pending = False
                self.error = str(error)
                self.populate()

        self.pending = True
        self.runner.submit(read, finished, failed)
