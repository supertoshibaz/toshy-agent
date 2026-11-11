"""Chat-local model selection. Opening this menu never contacts a provider."""
from ui.icons import bind_popup
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QMenu, QPushButton

from core.model_connections import load_settings
from ui.event_bus import event_bus


class ModelSelector(QPushButton):
    selection_changed = Signal(str)
    display_changed = Signal(str, str)

    def __init__(self, parent=None, *, before_change=None):
        super().__init__('API', parent)
        self.before_change = before_change
        self.selected = ''
        self.settings = {}
        self.setObjectName('ChatInputAuxBtn')
        self.setProperty('iconOnly', True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(32, 32)
        self.setStyleSheet('QPushButton {padding:0;min-width:30px;max-width:30px;min-height:30px;max-height:30px;}')
        set_text(self, 'ui.model_selector.api_and_model_selection_2b1d', setter='setAccessibleName')
        self.clicked.connect(self.open_menu)
        event_bus.model_profiles_changed.connect(self.refresh)
        from ui.i18n import i18n
        i18n.language_changed.connect(self.refresh)
        self.refresh()

    def set_profile(self, identity):
        self.selected = str(identity or '')
        self.refresh()

    def resolved_profile_id(self):
        return self.selected or str(self.settings.get('selected') or '')

    def profile_fingerprint(self, identity):
        from core.model_connections import ModelConnection
        record = next((item for item in self.settings.get('connections', []) if item.get('id') == identity), None)
        if record is None:
            return None
        mode = record.get('mode', 'api')
        return ModelConnection(mode, str(record.get('base_url') or '').rstrip('/'),
            str(record.get('model') or ''),
            record.get('protocol', 'chat_completions') if mode == 'api' else 'chat_completions').fingerprint

    def refresh(self):
        try:
            self.settings = load_settings()
        except Exception as error:
            set_text(self, 'ui.model_selector.api_c8e5')
            bind_caption(self, str(error), setter='setToolTip')
            self.display_changed.emit(tr('ui.caption.2d953dfbc566'), self.toolTip())
            return
        identity = self.resolved_profile_id()
        record = next((item for item in self.settings['connections'] if item.get('id') == identity), None)
        if record:
            name = record.get('name') or record.get('model') or tr('ui.caption.359497aff53e')
            set_text(self, 'ui.model_selector.api_c8e5')
            bind_caption(self, name + '\n' + str(record.get('model') or '') + '\n' +
                           str(record.get('base_url') or '') + tr('ui.caption.ab3d6bf6f7ac'), setter='setToolTip')
            self.display_changed.emit(str(record.get('model') or name), self.toolTip())
        else:
            set_text(self, 'ui.model_selector.api_c8e5')
            bind_caption(self, tr('ui.caption.ab174087f0e6') if identity else tr('ui.caption.5937ff70bb48'), setter='setToolTip')
            self.display_changed.emit(tr('ui.caption.2b2f98487f09'), self.toolTip())

    def open_menu(self):
        self.refresh()
        previous = getattr(self, 'menu', None)
        if isinstance(previous, QMenu):
            previous.close()
            previous.deleteLater()
        self.menu = bind_popup(QMenu(self), self)
        self.menu.setObjectName('ChatMessageContextMenu')
        records = self.settings.get('connections') or []
        default_id = self.settings.get('selected')
        default = next((item for item in records if item.get('id') == default_id), None)
        if default:
            action = add_caption_action(self.menu, 'addAction', tr('ui.caption.d177bbd879be') + (default.get('name') or tr('ui.caption.359497aff53e')).replace('&', '&&'))
            action.triggered.connect(lambda checked=False: self.choose(default['id']))
            self.menu.addSeparator()
        for mode, title in [('local', tr('ui.caption.7af077d5ab0f')), ('api', tr('ui.caption.b96113342100'))]:
            group = [item for item in records if item.get('mode', 'api') == mode]
            if not group:
                continue
            add_caption_action(self.menu, 'addSection', title)
            for record in group:
                identity = record['id']
                name = record.get('name') or record.get('model') or tr('ui.caption.359497aff53e')
                action = add_caption_action(self.menu, 'addAction', name.replace('&', '&&'))
                action.setCheckable(True)
                action.setChecked(identity == self.resolved_profile_id())
                bind_caption(action, str(record.get('model') or '') + '\n' + str(record.get('base_url') or ''), setter='setToolTip')
                action.triggered.connect(lambda checked=False, identity=identity: self.choose(identity))
        if not records:
            localize(add_caption_action(self.menu, 'addAction', 'Создайте профиль в Startup → API'), 'ui.model_selector.create_a_profile_in_startup_api_2f52').setEnabled(False)
        self.menu.ensurePolished()
        self.menu.adjustSize()
        anchor = self.mapToGlobal(QPoint(0, 0))
        screen = self.screen()
        if screen is not None:
            self.menu.setMaximumHeight(max(1, anchor.y() - screen.availableGeometry().top()))
        self.menu.popup(anchor - QPoint(0, min(self.menu.sizeHint().height(), self.menu.maximumHeight())))

    def choose(self, identity):
        if identity == self.selected:
            return
        if self.before_change is not None and not self.before_change(identity):
            return
        self.set_profile(identity)
        self.selection_changed.emit(self.selected)
