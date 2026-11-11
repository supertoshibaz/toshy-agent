import importlib
from pathlib import Path
from typing import List, Optional, Set, Tuple
from PySide6.QtWidgets import QWidget

from ui.dock_system.dock_panel import DockPanel
from ui.dock_system.types import DockSlotPosition


class ModuleUILoader:
    """
    Автоматический загрузчик интерфейсов модулей:
    - Сканирует modules/system и modules/custom на наличие подпапки ui/widget.py.
    - Динамически импортирует класс ModuleWidget.
    - Оборачивает в DockPanel и считывает дефолтную позицию из ui/config.json.
    """
    @staticmethod
    def discover_module_panels(
        root_dir: Path,
        enabled_modules: Optional[Set[Tuple[str, str]]] = None,
        existing_panel_ids: Optional[Set[str]] = None,
    ) -> List[Tuple[DockPanel, DockSlotPosition]]:
        panels = []

        from ui.startup_data import startup_modules, scan_modules
        catalog = scan_modules(root_dir)
        excluded = set(existing_panel_ids or ())
        builtin_ids = {'terminal_sandbox': 'terminal_panel', 'mcp_client': 'mcp_panel',
                       'subagent_manager': 'subagents_panel'}
        for entry in catalog:
            category, mod_name = entry['category'], entry['name']
            panel_id = f"{mod_name}_panel"
            builtin = builtin_ids.get(mod_name, panel_id) if category == 'system' else panel_id
            if panel_id in excluded or builtin in excluded or not entry['widget']:
                continue
            if enabled_modules is not None and (category, mod_name) not in enabled_modules:
                continue
            try:
                meta = entry['ui'] or {}
                panel_title = meta.get('display_name', mod_name.replace('_', ' ').title())
                try:
                    default_pos = DockSlotPosition(meta.get('default_dock_position', 'left_top'))
                except ValueError:
                    default_pos = DockSlotPosition.LEFT_TOP
                # Creating widgets and importing plugin UI code stays on the GUI.
                imported_lib = importlib.import_module(f"modules.{category}.{mod_name}.ui.widget")
                widget_class = getattr(imported_lib, 'ModuleWidget', getattr(imported_lib, 'Widget', None))
                if widget_class and issubclass(widget_class, QWidget):
                    panel = DockPanel(panel_id=panel_id, title_key=panel_title,
                                      content_widget=widget_class())
                    panels.append((panel, default_pos))
                    excluded.add(panel_id)
                    print(f"[Module UI Loader] Automatically attached module UI: {category}/{mod_name}")
            except Exception as error:
                print(f"[Module UI Loader Error] Could not load UI for module '{mod_name}': {error}")
        return panels
