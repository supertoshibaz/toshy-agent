import json
import os
from pathlib import Path
from typing import Dict, Any

from core.paths import APP_PATHS


class StateManager:
    def __init__(self, state_file: str = None):
        self.state_path = Path(state_file).resolve() if state_file else APP_PATHS.ui_state
        self.default_state: Dict[str, Any] = {
            "language": "en",
            "theme": "toshy",
            "active_view_index": 0,
            "dock_positions": {
                "sessions_panel": "left_top",
                "tasks_panel": "right_top",
                "file_manager_panel": "left_bottom",
                "materials_panel": "left_bottom",
                "web_search_panel": "left_top",
                "terminal_panel": "right_top",
                "subagents_panel": "right_bottom",
                "mcp_panel": "right_bottom"
            },
            "active_tabs": {
                "left_top": 0,
                "left_bottom": 0,
                "right_top": 0,
                "right_bottom": 0
            },
            "splitter_sizes": [340, 720, 340],
            "bottom_panel_height": -1,
            "workspace_path": "",
            "expanded_folders": [],
            "active_project_id": "",
            "expanded_project_id": "",
            "project_workspaces": {},
            "view_states": {},
            "project_file_expansions": {},
            "open_files": [],
            "active_file_index": 0,
            "open_boards": [],
            "active_board_index": 0,
            "open_paints": [],
            "active_paint_index": 0
        }
        self.state: Dict[str, Any] = self.load_state()

    def load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return dict(self.default_state)
        try:
            from ui.startup_data import read_startup_data
            saved = read_startup_data(self.state_path)
            state = dict(self.default_state)
            state.update(saved)
            return state
        except Exception as e:
            print(f"[StateManager] Could not load application state: {e}")
            return dict(self.default_state)

    def save_state(self, new_state: Dict[str, Any]):
        self.state.update(new_state)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.state_path)
        except Exception as e:
            print(f"[StateManager] Could not save application state: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set(self, key: str, value: Any):
        self.state[key] = value
