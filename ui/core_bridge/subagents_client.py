"""Shared asynchronous catalog for the subagent widget and Startup orbitals."""

from PySide6.QtCore import QObject, QTimer, Signal
from shiboken6 import isValid
from modules.system.subagent_manager.definitions import SubagentStore
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.core_bridge.session_client import SessionApiClient
from ui.core_bridge.module_manager import ModuleConfigSync
from ui.event_bus import event_bus
from ui.app_state import app_state


class SubagentsClient(QObject):
    changed = Signal(object)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.data = {}
        self.runner = AsyncRunner(self)
        self.api = SessionApiClient(core_process.base_url)
        self.busy = False
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        core_process.status_changed.connect(self.status_changed)
        app_state.session_changed.connect(self.scope_changed)
        self.refresh()

    def scope_changed(self, *_):
        self.data = {**self.data, "context_stats": {}}
        self.changed.emit(self.data)
        self.refresh()

    def status_changed(self, online, *_):
        if not online:
            ModuleConfigSync.runtime_status.clear()
        self.refresh()

    @staticmethod
    def offline(session_id=None, branch="main"):
        sync = ModuleConfigSync()
        enabled = sync.get_enabled_modules_from_config()
        configs = SubagentStore().read()
        stats = {}
        if session_id:
            from core.paths import APP_PATHS
            from core.context import sanitize_session_id
            from core.sqlite_storage import SQLiteStore
            from core.token_usage import session_connection
            from modules.system.subagent_manager.history import context_statistics

            store = SQLiteStore(
                APP_PATHS.sessions / sanitize_session_id(session_id) / "session.sqlite"
            )
            if store.exists():
                stats = context_statistics(
                    store, branch, configs, session_connection(store, branch)
                )
        return dict(
            subagents=configs,
            context_stats=stats,
            context_scope={"session_id": session_id, "branch": branch},
            settings=SubagentStore().settings(),
            pending={},
            module_enabled="subagent_manager" in enabled["system"],
            modules=sync.get_system_modules_data() + sync.get_custom_modules_data(),
        )

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        online = core_process.is_running()
        scope = (app_state.session_id, app_state.branch)

        def ready(data):
            self.busy = False
            if online != core_process.is_running() or scope != (
                app_state.session_id,
                app_state.branch,
            ):
                self.refresh()
                return
            previous = ModuleConfigSync.runtime_status
            current = {m["name"]: m for m in data.get("modules", [])} if online else {}
            ModuleConfigSync.runtime_status = current
            for name, value in current.items():
                if name in previous and value.get("enabled") != previous[name].get(
                    "enabled"
                ):
                    event_bus.module_status_changed.emit(
                        name, bool(value.get("enabled"))
                    )
            if data != self.data:
                self.data = data
                self.changed.emit(data)

        def failed(error):
            self.busy = False

        self.runner.submit(
            lambda: (
                self.api._request(
                    "GET",
                    "/v1/subagents",
                    params={
                        **({"session_id": scope[0]} if scope[0] else {}),
                        "branch": scope[1],
                    },
                )
                if online
                else self.offline(*scope)
            ),
            ready,
            failed,
        )

    def mutate(self, action, identity="", data=None, done=None):
        online = core_process.is_running()

        def work():
            if online:
                method = {
                    "create": "POST",
                    "save": "PUT",
                    "delete": "DELETE",
                    "settings": "PUT",
                }[action]
                suffix = (
                    "/settings"
                    if action == "settings"
                    else "/" + identity if identity else ""
                )
                return self.api._request(
                    method,
                    "/v1/subagents" + suffix,
                    **({"json": data} if data is not None else {})
                )
            store = SubagentStore()
            if action == "create":
                return {"subagent": store.create((data or {}).get("name") or "New subagent")}
            if action == "settings":
                store.write_settings(data)
            elif action == "delete":
                store.delete(identity)
            else:
                store.write(identity, {**store.read().get(identity, {}), **data})
            return {"success": True, "pending": False}

        def ready(result):
            if done is not None:
                done(result)
            self.refresh()

        self.runner.submit(work, ready, lambda error: self.failed.emit(str(error)))


_client = None


def subagents_client():
    global _client
    if _client is None:
        _client = SubagentsClient()
    return _client
