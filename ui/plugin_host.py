"""Small Qt adapter for the UI-independent plugin runtime."""
from ui.i18n import tr
from PySide6.QtWidgets import QApplication, QWidget
from core.plugins.runtime import get_runtime, bootstrap, PluginRuntime
from core.plugins.store import PluginStore
from core.plugins.manifest import PluginError
from core.plugins.modules import install_module_finder


def start_plugins(window):
    from core.paths import APP_PATHS
    try:
        runtime = bootstrap('ui')
    except Exception as error:
        from core.plugins.runtime import _log
        _log(f'Плагины не загружены: {error}')
        runtime = PluginRuntime(PluginStore(APP_PATHS.root / 'plugins'), state={
            'schema_version': 1, 'order': [], 'enabled': [], 'versions': {}})
        runtime.startup_error = str(error)
        from core.plugins.runtime import set_runtime
        set_runtime(runtime)
    # Disabled host workspaces cannot be replaced or required by UI components.
    # Resolve before importing hook factories, including declared dependents.
    for kind, enabled in window.workspace_options.items():
        if not enabled:
            runtime.targets.pop('workspace.' + kind, None)
    runtime.plan = runtime.resolve()
    install_module_finder(runtime)
    runtime.prepare(window)
    window.plugin_runtime = runtime
    # Explicit process environment is copied when QProcess starts, not through
    # mutable global environment inherited by arbitrary child applications.
    try:
        window.plugin_snapshot = runtime.write_snapshot()
    except OSError as error:
        from core.plugins.runtime import _log
        _log(f'Не удалось сохранить конфигурацию плагинов: {error}')
        window.plugin_snapshot = None
    QApplication.instance().aboutToQuit.connect(runtime.close)
    return runtime


def build_widget(target, widget_class, parent=None, *, host=None, **kwargs):
    from ui.startup_process import report_startup_phase
    labels = {'topbar.main': tr('ui.caption.d24d295cb925'), 'workspace.chat': tr('tab_chat'),
              'workspace.projects': tr('ui.projects_view.projects_04e2'), 'workspace.files': tr('ui.caption.7ae4f502fdcd'),
              'workspace.board': tr('ui.caption.9143eb5dcc80'), 'workspace.paint': tr('ui.caption.47a0badade12'),
              'workspace.startup': tr('ui.caption.5c531ca204cd'), 'widget.file_manager': 'Project Folder',
              'widget.sessions': tr('ui.caption.ab738977157c'), 'widget.tasks': tr('ui.projects_view.tasks_03cc'),
              'widget.subagents': tr('ui.subagents_panel.subagents_cf58'), 'widget.mcp': 'MCP', 'widget.terminal': tr('ui.caption.bebf1d00e6d5')}
    if target in labels:
        report_startup_phase(tr('ui.caption.fef60cd15c08') + labels[target] + '…')
    runtime = get_runtime()
    default = lambda: widget_class(parent, **kwargs)
    if runtime is None or runtime.domain != 'ui':
        return default()
    def validate(widget):
        # V1 builtin replacements retain the class contract used throughout the
        # app. Authors can subclass and rebuild its UI without source patches.
        if not isinstance(widget, widget_class):
            raise PluginError(f'{target}: replacement must inherit {widget_class.__name__}.')
        widget.objectName()  # Reject Python subclasses without QWidget initialization.
        fields = {
            'topbar.main': ('view_tabs', 'about_action', 'help_action', 'plugins_action', 'update_action', 'plugin_slots'),
            'workspace.chat': ('session_manager', 'scroll_area', 'system_prompt_requested'),
            'workspace.files': ('open_docs', 'agent_request_submitted'),
            'workspace.board': ('open_boards', 'agent_request_submitted'),
            'workspace.paint': ('documents', 'agent_request_submitted'),
            'workspace.startup': ('core_hub',),
        }.get(target, ('panel_id', 'request_move', 'current_position') if target.startswith('widget.') else ())
        for field in fields:
            if not hasattr(widget, field):
                raise PluginError(f'{target}: replacement does not provide {field}.')
    return runtime.build(target, default, parent=parent,
        host=host or (parent.window() if parent is not None else None), validate=validate)


def populate_slot(target, layout, *, host=None, parent=None, data=None):
    runtime = get_runtime()
    if runtime is None or runtime.domain != 'ui':
        return []
    widgets = []
    for key, widget in runtime.slots(target, host=host or (parent.window() if parent else None), parent=parent, data=data):
        if not isinstance(widget, QWidget):
            runtime.failure(key, PluginError(f'{target}: slot factory must return QWidget.'))
            continue
        layout.addWidget(widget)
        widget.show()
        widgets.append(widget)
    return widgets


def add_components(window):
    runtime = window.plugin_runtime
    from ui.dock_system.dock_panel import DockPanel
    from ui.dock_system.types import DockSlotPosition
    for key, c in runtime.activation_order(mode='add', domain='ui'):
        if c['type'] not in {'widget', 'workspace', 'topbar'}:
            continue  # UI-side service registrations were prepared before widgets.
        if not runtime.selected(key):
            continue
        try:
            widget = runtime.factory(key)(runtime.context(key, host=window, parent=window))
            if not isinstance(widget, QWidget):
                raise PluginError('UI factory must return QWidget.')
            title = c.get('title') or runtime.by_id[key.split('/')[0]]['name']
            if c['type'] == 'widget':
                panel = DockPanel(panel_id='plugin:' + key, title_key=title, content_widget=widget)
                position = window.state_manager.get('dock_positions', {}).get(panel.panel_id, c.get('placement', 'left_top'))
                try:
                    position = DockSlotPosition(position)
                except ValueError:
                    position = DockSlotPosition.LEFT_TOP
                window.dock_manager.register_panel(panel, position)
            elif c['type'] == 'workspace':
                if c.get('placement', 'top') == 'bottom':
                    stack = window.bottom_panel.content_stack
                    tabs = window.bottom_panel.header.tabs
                else:
                    stack = window.central_stack
                    tabs = window.top_bar.view_tabs
                index = stack.addWidget(widget)
                tabs.addTab(title)
                tabs.setTabData(index, 'plugin:' + key)
            elif c['type'] == 'topbar':
                position = 'ui.topbar.right' if c.get('placement') == 'right' else 'ui.topbar.left'
                window.top_bar.plugin_slots[position].addWidget(widget)
            runtime.objects[key] = widget
            runtime.started.add(key)
        except Exception as error:
            runtime.failure(key, error)
    window.top_bar._position_workspace_tabs()
    window.bottom_panel.header._position_overlay_controls()
