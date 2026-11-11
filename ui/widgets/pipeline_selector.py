from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QPushButton, QMenu, QLineEdit, QLabel, QWidget, QHBoxLayout
from ui.widgets.inline_editor import ContextMenu
import httpx
import json
from core.security import auth_headers, load_or_create_core_token
from ui.core_bridge.async_runner import AsyncRunner
from ui.core_bridge.process_manager import core_process
from ui.event_bus import event_bus

class PipelineSelector(QPushButton):
    selection_changed = Signal(str)
    display_changed = Signal()
    def __init__(self, parent=None):
        super().__init__(tr('ui.caption.9baed6e290ff'), parent)
        self.setMaximumWidth(200)
        self.setMinimumWidth(100)
        self.setObjectName('ChatInputAuxBtn'); self.setCursor(Qt.PointingHandCursor)
        self.selected = ''; self.session_id = None; self.branch = 'main'; self.catalog = []
        self._catalog_generation = 0
        self.runner = AsyncRunner(self, max_threads=1)
        self.clicked.connect(self.open_menu)
        event_bus.module_status_changed.connect(lambda *_: self.refresh())
        event_bus.pipeline_resources_changed.connect(self.refresh)

    def set_session(self, session_id, branch, selected):
        self._catalog_generation += 1
        if (self.session_id,self.branch)!=(session_id,branch):
            self.catalog=[]
        self.session_id, self.branch, self.selected = session_id, branch, selected or ''
        bind_caption(self, 'Pipeline: ' + (tr('ui.caption.954fea171639') if self.selected else tr('ui.pipeline_selector.no_pipeline_99a0')))
        self.display_changed.emit()
        self.refresh()

    def fetch(self, session_id, branch):
        with httpx.Client(timeout=5) as client:
            response = client.get(core_process.base_url + '/v1/pipelines',
                params={'session_id': session_id, 'branch': branch} if session_id else {},
                headers=auth_headers(load_or_create_core_token()))
            response.raise_for_status(); return response.json()['pipelines']

    def refresh(self, *_args, show_menu=False):
        if not core_process.is_running():
            return
        session = (self.session_id, self.branch)
        self._catalog_generation += 1
        generation = self._catalog_generation
        def loaded(catalog):
            if generation != self._catalog_generation or session != (self.session_id, self.branch):
                return
            self.catalog = [x for x in catalog if x.get('mode') == 'user']
            active = next((x for x in self.catalog if x['id'] == self.selected), None)
            if self.selected and active is None:
                self.choose('')
                set_text(self, 'ui.pipeline_selector.the_selected_pipeline_is_no_longer_5eaf', setter='setToolTip')
            elif active:
                bind_caption(self, 'Pipeline: ' + active['name'])
                bind_caption(self, '\n'.join(active.get('errors') or [active['module_name']]), setter='setToolTip')
            self.display_changed.emit()
            if show_menu:
                self.show_menu()
        def failed(error):
            if generation == self._catalog_generation and session == (self.session_id, self.branch):
                bind_caption(self, str(error), setter='setToolTip')
        self.runner.submit(lambda: self.fetch(*session), loaded, failed)

    def open_menu(self):
        self.refresh(show_menu=True)

    def show_menu(self):
        self.menu = QMenu(self)
        self.populate_menu(self.menu)
        self.menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def populate_menu(self, menu):
        menu.clear()
        action = localize(add_caption_action(menu, 'addAction', 'Без пайплайна'), 'ui.pipeline_selector.no_pipeline_99a0'); action.setCheckable(True); action.setChecked(not self.selected)
        action.triggered.connect(lambda: self.choose(''))
        groups = {}
        for entry in self.catalog:
            if entry['module'] not in groups:
                groups[entry['module']] = add_caption_action(menu, 'addMenu', entry['module_name'])
            group = groups[entry['module']]
            label = ('⚠ ' if entry.get('errors') else '') + entry['name']
            action = add_caption_action(group, 'addAction', label); action.setCheckable(True); action.setChecked(self.selected == entry['id'])
            bind_caption(action, '\n'.join(entry.get('errors') or []), setter='setToolTip')
            action.triggered.connect(lambda _checked=False, pid=entry['id']: self.choose(pid))

    def choose(self, pid):
        self.selected = pid
        active = next((x for x in self.catalog if x['id'] == pid), None)
        bind_caption(self, 'Pipeline: ' + (active['name'] if active else tr('ui.pipeline_selector.no_pipeline_99a0')))
        self.display_changed.emit()
        self.selection_changed.emit(pid)

class PipelinePauseDialog(ContextMenu):
    def __init__(self, payload, parent=None):
        super().__init__(parent, tr('ui.caption.81eb78e48f98'),
                         anchor=getattr(parent, 'pipeline_continue_btn', None))
        self.payload = payload
        self.runner = AsyncRunner(self, max_threads=1)
        # Requests can be hidden and reopened without dismissing the pause.
        label = QLabel(payload.get('message', '')); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        label.setMaximumWidth(400); label.setContentsMargins(8, 4, 8, 4); self.add_editor(label)
        self.fields = {}
        for item in payload.get('fields', []):
            row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(8, 4, 8, 4)
            label = QLabel(item['name']); label.setTextFormat(Qt.PlainText); layout.addWidget(label)
            edit = QLineEdit(); bind_caption(edit, item['type'], setter='setPlaceholderText'); bind_caption(edit, item.get('description', ''), setter='setToolTip')
            edit.setMinimumWidth(180); layout.addWidget(edit)
            self.fields[item['name']] = (edit, item['type']); self.add_editor(row)
        self.addSeparator()
        labels = {'continue': tr('ui.chat_resume.continue_a42a'), 'repeat': tr('common.retry'), 'skip_node': tr('ui.caption.c2b189dcb1e9'),
            'retry_script': tr('ui.caption.dc6f163a503a'), 'finish_graph': tr('ui.caption.90cfd8be9140'), 'shutdown_generation': tr('ui.caption.490edf98089b')}
        self.buttons = []
        for action in payload.get('actions', ['continue']):
            label = labels.get(action, f"Exec {action} — {payload.get('route_descriptions', {}).get(action, '')}")
            button = add_caption_action(self, 'addAction', label.replace('&', '&&'))
            button.triggered.connect(lambda _checked=False, a=action: self.respond(a))
            self.buttons.append(button)

    def respond(self, action):
        try:
            values = {}
            for name, (edit, kind) in self.fields.items():
                text = edit.text()
                if not text.strip():
                    continue
                if kind == 'array':
                    values[name] = json.loads(text)
                else:
                    from modules.pipelines.runtime import typed
                    values[name] = typed(text, kind)
        except Exception as exc:
            self.set_error(exc); return
        for button in self.buttons:
            button.setEnabled(False)
        def request():
            with httpx.Client(timeout=10) as client:
                response = client.post(core_process.base_url + f"/v1/pipelines/requests/{self.payload['request_id']}/resume",
                    json={'action': action, 'values': values}, headers=auth_headers(load_or_create_core_token()))
                response.raise_for_status(); return response.json()
        def failed(exc):
            self.set_error(exc)
            for button in self.buttons:
                button.setEnabled(True)
        self.runner.submit(request, self._resumed, failed)

    def _resumed(self, _result):
        self.accepted.emit()
        self.close()
        self.deleteLater()

    def set_error(self, message):
        super().set_error(message)
        # Selecting an action hides a QMenu before validation or HTTP completion.
        self.show()
