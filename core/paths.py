from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class AppPaths:
    """Единая карта расположения кода и изменяемых данных Toshy."""

    root: Path
    config: Path
    modules: Path
    sessions: Path
    workspace: Path
    logs: Path
    runtime: Path
    ui_state: Path

    @property
    def code_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def elements(self) -> Path:
        return self.root / "elements"

    @property
    def managed_elements(self) -> Path:
        """Release-owned components; custom models and LSP settings stay in elements."""
        return Path(os.environ.get('TOSHY_MANAGED_ELEMENTS') or self.elements)

    @property
    def models(self) -> Path:
        return self.elements / "models"

    @property
    def lsp(self) -> Path:
        return self.elements / "lsp"

    @property
    def browsers(self) -> Path:
        return self.elements / "browsers"

    @classmethod
    def discover(cls) -> "AppPaths":
        # Source checkouts retain their existing layout. Managed launchers set
        # this before importing any application module, including the splash.
        root = Path(os.environ.get('TOSHY_DATA_DIR') or Path(__file__).resolve().parent.parent).expanduser().resolve()
        return cls(
            root=root,
            config=root / "config.yaml",
            modules=root / "modules",
            sessions=root / "sessions",
            workspace=root / "workspace",
            logs=root / "logs",
            runtime=root / "runtime",
            ui_state=root / "ui" / "app_state.json",
        )

    def ensure_runtime_dirs(self) -> None:
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.ensure_element_dirs()

    def ensure_element_dirs(self) -> None:
        for directory in (self.lsp, self.models, self.browsers):
            directory.mkdir(parents=True, exist_ok=True)


APP_PATHS = AppPaths.discover()

