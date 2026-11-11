import os
import json
import yaml
import httpx
from ui.core_bridge.transport import core_http_client
from pathlib import Path
from typing import List, Dict, Any
from core.paths import APP_PATHS
from core.security import auth_headers, load_or_create_core_token


DEFAULT_PALETTE = {
    # Системные
    "web_search": "#38bdf8",            # Неоново-голубой
    "file_manager": "#34d399",          # Изумрудный
    "terminal_sandbox": "#fbbf24",      # Янтарный
    "subagent_manager": "#fb923c",      # Оранжевый
    "mcp_client": "#818cf8",            # Индиго
    # Кастомные (дефолтные цвета при создании)
    "custom_default": "#ec4899",
}

DEFAULT_DISABLED_COLOR = "#2a2f3a"
DEFAULT_EMPTY_COLOR = "#1e222b"


class ModuleConfigSync:
    """
    Синхронизация системных и кастомных модулей:
    - Загружает цвет и метаданные из <mod_dir>/ui/config.json.
    - Разделяет модули по категориям: 'system' и 'custom'.
    """
    runtime_status = {}

    def __init__(self, config_path: str = None, base_url: str = "http://127.0.0.1:5000"):
        self.config_path = Path(config_path or APP_PATHS.config).resolve()
        self.base_url = base_url
        self.root_dir = APP_PATHS.root
        self.core_token = load_or_create_core_token()

    def _get_module_ui_dir(self, category: str, mod_name: str) -> Path:
        from core.plugins.modules import module_directory
        directory = module_directory(category, mod_name, self.root_dir) / 'ui'
        if category == 'system' and APP_PATHS.root != APP_PATHS.code_root and directory.is_relative_to(APP_PATHS.code_root):
            return APP_PATHS.modules / 'system' / mod_name / 'ui'
        return directory

    def _ensure_module_ui_config(self, category: str, mod_name: str) -> Dict[str, Any]:
        """Проверяет наличие ui/config.json, при отсутствии генерирует дефолтный."""
        ui_dir = self._get_module_ui_dir(category, mod_name)
        config_file = ui_dir / "config.json"

        if not config_file.exists():
            ui_dir.mkdir(parents=True, exist_ok=True)
            default_color = DEFAULT_PALETTE.get(mod_name, "#ec4899" if category == "custom" else "#60a5fa")
            data = {
                "color": default_color,
                "icon": "icon.svg",
                "display_name": mod_name.replace("_", " ").title(),
                "default_dock_position": "left_bottom" if mod_name == "file_manager" else "left_top"
            }
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[ModuleSync] Could not create config.json for {mod_name}: {e}")
            return data

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"color": DEFAULT_PALETTE.get(mod_name, "#60a5fa"), "icon": "icon.svg"}

    def _scan_category_modules(self, category: str) -> List[Dict[str, Any]]:
        """Сканирует папку категории ('system' или 'custom')."""
        from ui.startup_data import scan_modules
        modules = []
        for entry in scan_modules(self.root_dir):
            if entry['category'] != category:
                continue
            name = entry['name']
            meta = entry['ui'] or self._ensure_module_ui_config(category, name)
            modules.append(dict(name=name, category=category,
                color=meta.get('color', DEFAULT_PALETTE.get(name, '#60a5fa')),
                icon=meta.get('icon', 'icon.svg'),
                display_name=entry.get('display_name') or meta.get('display_name', name),
                ui_dir=str(self._get_module_ui_dir(category, name))))
        return modules

    def get_enabled_modules_from_config(self) -> Dict[str, List[str]]:
        """Считывает списки активных модулей из config.yaml."""
        if not self.config_path.exists():
            return {"system": [], "custom": []}
        try:
            from ui.startup_data import read_startup_data
            data = read_startup_data(self.config_path, 'yaml')
            return {
                "system": data.get("system_modules", []) or [],
                "custom": data.get("custom_modules", []) or []
            }
        except Exception:
            return {"system": [], "custom": []}

    def get_system_modules_data(self) -> List[Dict[str, Any]]:
        """Возвращает список системных модулей для внутреннего кольца."""
        available = self._scan_category_modules("system")
        enabled = set(self.get_enabled_modules_from_config()["system"])
        for m in available:
            m["enabled"] = m["name"] in enabled
            m['pending_disable'] = bool(self.runtime_status.get(m['name'], {}).get('pending_disable'))
        return available

    def get_custom_modules_data(self) -> List[Dict[str, Any]]:
        """Возвращает список кастомных модулей для внешнего кольца."""
        available = self._scan_category_modules("custom")
        enabled = set(self.get_enabled_modules_from_config()["custom"])
        for m in available:
            m["enabled"] = m["name"] in enabled
            m['pending_disable'] = bool(self.runtime_status.get(m['name'], {}).get('pending_disable'))
        return available

    def toggle_module(self, module_name: str, category: str, is_core_online: bool) -> bool:
        """Переключает статус системного или кастомного модуля."""
        if not self.config_path.exists():
            return False

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as error:
            print(f"[ModuleSync] Cannot read config.yaml: {error}")
            return False

        key = "system_modules" if category == "system" else "custom_modules"
        current_list = cfg.get(key, []) or []
        is_currently_on = module_name in current_list

        if is_currently_on:
            cfg[key] = [m for m in current_list if m != module_name]
            api_action = "disable"
        else:
            current_list.append(module_name)
            cfg[key] = current_list
            api_action = "enable"

        if is_core_online:
            if self.runtime_status.get(module_name, {}).get('pending_disable'):
                api_action = 'enable'
            try:
                with core_http_client(self.base_url, timeout=3.0) as client:
                    response = client.post(
                        f"{self.base_url}/v1/modules/{module_name}/{api_action}",
                        headers=auth_headers(load_or_create_core_token()),
                    )
                    response.raise_for_status()
                    result = response.json()
                    if not result.get("success"):
                        print(f"[ModuleSync HotSwap] {result.get('message', 'Operation rejected by the core')}")
                        return is_currently_on
                    self.runtime_status[module_name] = {'pending_disable': bool(result.get('pending'))}
                    return bool(result.get('enabled', not is_currently_on))
            except Exception as e:
                print(f"[ModuleSync HotSwap] API error: {e}")
                return is_currently_on

        # Когда ядро выключено, GUI может подготовить конфигурацию следующего запуска.
        try:
            from core.module_config import set_module_enabled
            set_module_enabled(self.config_path, key, module_name, not is_currently_on)
        except Exception as e:
            print(f"[ModuleSync] Could not write config.yaml: {e}")
            return is_currently_on

        return not is_currently_on
