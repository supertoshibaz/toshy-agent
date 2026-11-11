from __future__ import annotations
from ui.styles.binding import apply_style
from ui.i18n import bind_caption, add_caption_item, add_caption_items, tr, i18n, bind_source, localize, set_text, add_item


import os
import json
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QLayout, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QListWidget, QPushButton)
from ui.widgets.dialogs import MessageBox as QMessageBox

from core.embeddings import (
    BUILTIN_DIMENSION,
    BUILTIN_MODEL_NAME,
    BUILTIN_PROVIDER,
    REMOTE_PROVIDER,
    embedding_provider_from_config,
)
from core.paths import APP_PATHS
from ui.components.orbital_core_hub import OrbitalCoreHub
from ui.components.subagent_orbitals import SubagentOrbitals


NAV_DEFAULT_WIDTH = 270
SETTINGS_TOP = 430


def _normalized_tokens(text: str) -> List[str]:
    return [part for part in str(text or "").casefold().split() if part]


def _system_module_names() -> List[str]:
    """Discover system modules from the application tree, independent of enablement."""
    modules_dir = APP_PATHS.code_root / "modules" / "system"
    names: List[str] = []
    from ui.startup_data import startup_modules
    catalog = startup_modules(APP_PATHS.root)
    if catalog is not None:
        names = [entry['name'] for entry in catalog if entry['category'] == 'system']
        if names:
            return names

    try:
        if modules_dir.is_dir():
            for entry in sorted(modules_dir.iterdir(), key=lambda path: path.name.casefold()):
                if (
                    entry.is_dir()
                    and not entry.name.startswith(".")
                    and (entry / "__init__.py").is_file()
                ):
                    names.append(entry.name)
    except OSError:
        pass

    # Fallback for packaged layouts where the module tree is not directly
    # browsable but config.yaml still contains the known system modules.
    if not names:
        try:
            from ui.startup_data import read_startup_data
            config = read_startup_data(APP_PATHS.config, "yaml")
            names = [
                str(name).strip()
                for name in (config.get("system_modules") or [])
                if str(name).strip()
            ]
        except Exception:
            names = []

    return names


def _custom_module_names() -> List[str]:
    """Discover custom modules from the application tree, independent of enablement."""
    modules_dir = APP_PATHS.root / "modules" / "custom"
    names: List[str] = []
    from ui.startup_data import startup_modules
    catalog = startup_modules(APP_PATHS.root)
    if catalog is not None:
        names = [entry['name'] for entry in catalog if entry['category'] == 'custom']
        if names:
            return names

    try:
        if modules_dir.is_dir():
            for entry in sorted(modules_dir.iterdir(), key=lambda path: path.name.casefold()):
                if (
                    entry.is_dir()
                    and not entry.name.startswith(".")
                    and (entry / "__init__.py").is_file()
                ):
                    names.append(entry.name)
    except OSError:
        pass

    if not names:
        try:
            from ui.startup_data import read_startup_data
            config = read_startup_data(APP_PATHS.config, "yaml")
            names = [
                str(name).strip()
                for name in (config.get("custom_modules") or [])
                if str(name).strip()
            ]
        except Exception:
            names = []

    return names


def _module_display_name(module_name: str) -> str:
    """Human-readable tree label while preserving common initialisms."""
    from ui.startup_data import startup_modules
    catalog = startup_modules(APP_PATHS.modules.parent)
    if catalog is not None:
        name = next((entry['display_name'] for entry in catalog
                     if entry['category'] == 'custom' and entry['name'] == module_name), None)
        if name:
            return str(name)
    manifest = APP_PATHS.modules / 'custom' / module_name / 'module.json'
    try:
        name = json.loads(manifest.read_text(encoding='utf-8')).get('name')
        if name:
            return str(name)
    except (OSError, ValueError):
        pass
    aliases = {
        "mcp": "MCP",
        "ui": "UI",
        "api": "API",
        "llm": "LLM",
    }
    return " ".join(
        aliases.get(part.casefold(), part.capitalize())
        for part in str(module_name or "").split("_")
        if part
    )


def _control_search_text(widget: QWidget) -> str:
    if isinstance(widget, QComboBox):
        return " ".join(widget.itemText(index) for index in range(widget.count()))
    if isinstance(widget, QSpinBox):
        return f"{widget.value()} {widget.minimum()} {widget.maximum()}"
    if isinstance(widget, QLineEdit):
        return f"{widget.text()} {widget.placeholderText()}"
    if isinstance(widget, QLabel):
        return widget.text()
    return ""


class SettingRow(QFrame):
    """One Godot-like property row with searchable visible/help text."""

    def __init__(
        self,
        title: str,
        description: str,
        editor: QWidget,
        *,
        search_terms: Iterable[str] = (),
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("StartupSettingRow")
        self._title = str(title or "")
        self._description = str(description or "")
        self._search_terms = [str(value or "") for value in search_terms]
        self.editor = editor

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(18)

        text_box = QWidget(self)
        text_box.setObjectName("StartupSettingText")
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title_label = bind_source(QLabel(text_box), title)
        self._title_label = title_label
        title_label.setObjectName("StartupSettingTitle")
        description_label = bind_source(QLabel(text_box), description)
        self._description_label = description_label
        description_label.setObjectName("StartupSettingDescription")
        description_label.setWordWrap(True)

        text_layout.addWidget(title_label)
        if self._description:
            text_layout.addWidget(description_label)

        layout.addWidget(text_box, 1)
        layout.addWidget(editor, 0, Qt.AlignRight | Qt.AlignVCenter)

    def search_blob(self) -> str:
        return " ".join(
            [
                self._title_label.text(),
                self._description_label.text(),
                *self._search_terms,
                _control_search_text(self.editor),
            ]
        ).casefold()

    def matches(self, tokens: Sequence[str]) -> bool:
        if not tokens:
            return True
        blob = self.search_blob()
        return all(token in blob for token in tokens)


class SettingsPage(QScrollArea):
    """Search-filterable settings page shown in the right pane."""

    def __init__(
        self,
        title: str,
        description: str,
        category_path: Sequence[str],
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("StartupSettingsPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self._title = str(title or "")
        self._description = str(description or "")
        self._category_path = tuple(str(value or "") for value in category_path)
        self._rows: List[SettingRow] = []

        body = QWidget(self)
        body.setObjectName("StartupSettingsPageBody")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.body_layout.setContentsMargins(18, 16, 18, 20)
        self.body_layout.setSpacing(0)

        heading = bind_source(QLabel(body), title)
        self._heading_label = heading
        self._summary_label = None
        heading.setObjectName("StartupSettingsHeading")
        self.body_layout.addWidget(heading)

        if self._description:
            summary = bind_source(QLabel(body), description)
            self._summary_label = summary
            summary.setObjectName("StartupSettingsSummary")
            summary.setWordWrap(True)
            self.body_layout.addWidget(summary)
            self.body_layout.addSpacing(12)

        self.rows_host = QWidget(body)
        self.rows_host.setObjectName("StartupSettingsRows")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.body_layout.addWidget(self.rows_host)
        self.body_layout.addStretch(1)

        self.empty_label = localize(QLabel('', body), 'ui.startup_view.no_settings_match_this_search_f6dd')
        self.empty_label.setObjectName("StartupSettingsEmpty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        self.body_layout.addWidget(self.empty_label)

        self.setWidget(body)

    def add_row(self, row: SettingRow) -> None:
        self._rows.append(row)
        self.rows_layout.addWidget(row)

    def page_search_blob(self) -> str:
        return " ".join(
            [self._heading_label.text(), self._summary_label.text() if self._summary_label else "", *self._category_path]
        ).casefold()

    def apply_filter(self, query: str) -> bool:
        tokens = _normalized_tokens(query)
        if not tokens:
            for row in self._rows:
                row.show()
            self.rows_host.show()
            self.empty_label.hide()
            return True

        # Searching for a page/category name behaves like Godot: once the page
        # itself matches, keep all of its properties visible.
        page_blob = self.page_search_blob()
        page_match = all(token in page_blob for token in tokens)

        any_row = False
        for row in self._rows:
            visible = page_match or row.matches(tokens)
            row.setVisible(visible)
            any_row = any_row or visible

        self.rows_host.setVisible(any_row)
        self.empty_label.setVisible(not any_row)
        return page_match or any_row


class EmptySettingsPage(SettingsPage):
    """Placeholder for an application-owned settings section."""

    def __init__(
        self,
        title: str,
        category_path: Sequence[str],
        *,
        description: str = tr('ui.caption.9c44461ac553'),
        search_terms: Iterable[str] = (),
        parent=None,
    ):
        super().__init__(
            title,
            description,
            category_path,
            parent,
        )
        self._extra_search_terms = [
            str(value or "").casefold()
            for value in search_terms
            if str(value or "").strip()
        ]

    def page_search_blob(self) -> str:
        base = super().page_search_blob()
        if not self._extra_search_terms:
            return base
        return f"{base} {' '.join(self._extra_search_terms)}"


class EmbeddingSettingsPage(SettingsPage):
    """The embedding settings previously exposed through App -> Embeddings."""

    def __init__(self, parent=None):
        super().__init__(
            "Embeddings",
            (
                tr('ui.caption.4d75a6ea15c3')
            ),
            ("Core", "System Modules", "Memory", "Embeddings"),
            parent,
        )
        self._loading = False

        self.provider_combo = QComboBox(self.rows_host)
        self.provider_combo.setMinimumWidth(310)
        self.provider_row = SettingRow(
            tr('ui.startup_view.provider_5592'),
            (
                tr('ui.startup_view.choose_the_built_in_harrier_oss_0618')
            ),
            self.provider_combo,
            search_terms=(
                "provider провайдер builtin built-in local remote",
                "Harrier OSS v1 270M Q4 ONNX",
                "OpenAI-compatible API",
                "model модель vector vectors вектора",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.provider_row)

        from ui.embedding_fields import EmbeddingModelFields
        self._model_fields = EmbeddingModelFields(self)

        self.max_tokens_combo = QComboBox(self.rows_host)
        self.max_tokens_combo.setMinimumWidth(180)
        for value in (2048, 4096, 8192, 16384, 32768):
            self.max_tokens_combo.addItem(
                f"{value:,} tokens".replace(",", " "),
                value,
            )
        self.max_tokens_row = SettingRow(
            tr('ui.startup_view.per_input_embedding_limit_dec4'),
            (
                tr('ui.startup_view.maximum_tokens_in_one_fragment_sent_fbcc')
            ),
            self.max_tokens_combo,
            search_terms=(
                "max tokens token context fragment chunk лимит токены фрагмент",
                "2048 4096 8192 16384 32768",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.max_tokens_row)

        self.threads_spin = QSpinBox(self.rows_host)
        self.threads_spin.setRange(1, 2_147_483_647)
        self.threads_spin.setKeyboardTracking(False)
        self.threads_spin.setAccelerated(True)
        self.threads_spin.setMinimumWidth(130)
        self.threads_row = SettingRow(
            tr('ui.startup_view.cpu_threads_661d'),
            (
                tr('ui.startup_view.number_of_onnx_runtime_cpu_threads_7a54')
            ),
            self.threads_spin,
            search_terms=(
                "cpu threads thread потоки поток onnx runtime harrier performance",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.threads_row)

        note = localize(QLabel(
            '',
            self.rows_host,
        ), 'ui.startup_view.changing_the_embedding_model_requires_reindexing_db5c')
        note.setObjectName("StartupSettingsWarning")
        note.setWordWrap(True)
        self.reindex_row = SettingRow(
            tr('ui.startup_view.vector_compatibility_0dce'),
            (
                tr('ui.startup_view.existing_vectors_remain_associated_with_the_efbb')
            ),
            note,
            search_terms=(
                "reindex reindexing переиндексация compatibility совместимость old vectors старые вектора",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.reindex_row)

        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.max_tokens_combo.currentIndexChanged.connect(self._max_tokens_changed)
        self.threads_spin.valueChanged.connect(self._threads_changed)

        self.reload_from_config()

    # ---- same config semantics as the old AppSettingsMenu -----------------

    def _read_config(self) -> Dict[str, Any]:
        try:
            from ui.startup_data import read_startup_data
            return read_startup_data(APP_PATHS.config, "yaml")
        except Exception:
            return {}

    def _write_config(self, config: Dict[str, Any]) -> None:
        APP_PATHS.config.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".config.",
            suffix=".yaml",
            dir=str(APP_PATHS.config.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, APP_PATHS.config)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _ensure_remote_snapshot(embedding: Dict[str, Any]) -> None:
        if embedding.get("remote"):
            return
        embedding["remote"] = {
            "api_url": embedding.get("api_url", "http://localhost:8081/v1"),
            "model_name": embedding.get("model_name", "embedding-model"),
            "dimension": embedding.get("dimension", 768),
        }

    def _update_embedding(self, updater: Callable[[Dict[str, Any]], None]) -> None:
        config = self._read_config()
        embedding = dict(config.get("embedding") or {})
        self._ensure_remote_snapshot(embedding)
        updater(embedding)
        config["embedding"] = embedding
        self._write_config(config)

    def _show_write_error(self, error: Exception) -> None:
        QMessageBox.critical(self, tr('ui.startup_view.embedding_settings_d332'), str(error))

    def reload_from_config(self) -> None:
        config = self._read_config()
        embedding = dict(config.get("embedding") or {})
        self._ensure_remote_snapshot(embedding)
        provider = embedding_provider_from_config(embedding)

        remote = embedding.get("remote") or {}
        remote_model = str(
            remote.get("model_name")
            or embedding.get("model_name")
            or "embedding-model"
        )

        self._loading = True
        try:
            self.provider_combo.clear()
            add_item(self.provider_combo, 'ui.startup_view.built_in_harrier_oss_v1_270m_4a2f', BUILTIN_PROVIDER)
            add_caption_item(self.provider_combo, f"OpenAI-compatible API · {remote_model}", REMOTE_PROVIDER)
            self._model_fields.reload(embedding, provider)
            provider_index = self.provider_combo.findData(provider)
            self.provider_combo.setCurrentIndex(max(0, provider_index))

            max_tokens = int(embedding.get("max_tokens", 4096))
            token_index = self.max_tokens_combo.findData(max_tokens)
            if token_index < 0:
                # Preserve an unusual existing value instead of silently changing it.
                self.max_tokens_combo.addItem(
                    f"{max_tokens:,} tokens".replace(",", " "),
                    max_tokens,
                )
                token_index = self.max_tokens_combo.findData(max_tokens)
            self.max_tokens_combo.setCurrentIndex(max(0, token_index))

            threads = int(
                embedding.get(
                    "threads",
                    min(4, os.cpu_count() or 1),
                )
            )
            self.threads_spin.setValue(max(1, threads))
        finally:
            self._loading = False

    def _provider_changed(self, _index: int) -> None:
        if self._loading:
            return
        provider = self.provider_combo.currentData()
        if provider not in {BUILTIN_PROVIDER, REMOTE_PROVIDER}:
            return

        def apply(embedding: Dict[str, Any]) -> None:
            embedding["provider"] = provider
            if provider == BUILTIN_PROVIDER:
                embedding["model_name"] = BUILTIN_MODEL_NAME
                embedding["dimension"] = BUILTIN_DIMENSION
                embedding.setdefault("max_tokens", 4096)
                embedding.setdefault("threads", min(4, os.cpu_count() or 1))
            elif provider == REMOTE_PROVIDER:
                remote = embedding.get("remote") or {}
                embedding["api_url"] = remote.get(
                    "api_url",
                    embedding.get("api_url", "http://localhost:8081/v1"),
                )
                embedding["model_name"] = remote.get(
                    "model_name",
                    "embedding-model",
                )
                embedding["dimension"] = remote.get(
                    "dimension",
                    embedding.get("dimension", 768),
                )

        try:
            self._update_embedding(apply)
            self.reload_from_config()
        except Exception as error:
            self._show_write_error(error)
            self.reload_from_config()

    def _max_tokens_changed(self, _index: int) -> None:
        if self._loading:
            return
        value = self.max_tokens_combo.currentData()
        if value is None:
            return
        try:
            self._update_embedding(
                lambda embedding: embedding.__setitem__("max_tokens", int(value))
            )
        except Exception as error:
            self._show_write_error(error)
            self.reload_from_config()

    def _threads_changed(self, value: int) -> None:
        if self._loading:
            return
        try:
            self._update_embedding(
                lambda embedding: embedding.__setitem__("threads", int(value))
            )
        except Exception as error:
            self._show_write_error(error)
            self.reload_from_config()


class AttachmentSettingsPage(SettingsPage):
    """Application-wide attachment storage settings moved from the old App menu."""

    STORAGE_MODES = (
        ("copy", tr('ui.caption.f5bbd377d2d9')),
        ("link", tr('ui.caption.907fc11cc120')),
        ("hybrid", tr('ui.caption.21a8145163b7')),
    )

    def __init__(self, parent=None):
        super().__init__(
            tr('nav.attachments'),
            (
                tr('ui.caption.d434d89919d8')
            ),
            ("App", "Workspaces", "Chat", "Attachments"),
            parent,
        )
        self._loading = False

        self.storage_combo = QComboBox(self.rows_host)
        self.storage_combo.setMinimumWidth(310)
        for mode, label in self.STORAGE_MODES:
            add_caption_item(self.storage_combo, label, mode)

        self.storage_row = SettingRow(
            tr('ui.startup_view.file_storage_9fb4'),
            (
                tr('ui.startup_view.choose_whether_to_copy_attachments_into_34ee')
            ),
            self.storage_combo,
            search_terms=(
                "attachments attachment вложения вложение files файлы",
                "storage хранение copy копировать link ссылка hybrid гибрид",
                "session сессия original исходный файл",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.storage_row)

        self.copy_limit_spin = QSpinBox(self.rows_host)
        self.copy_limit_spin.setRange(0, 2_147_483_647)
        self.copy_limit_spin.setKeyboardTracking(False)
        self.copy_limit_spin.setAccelerated(True)
        bind_caption(self.copy_limit_spin, tr('ui.caption.883817f1999f'), setter='setSuffix')
        self.copy_limit_spin.setMinimumWidth(130)

        self.copy_limit_row = SettingRow(
            tr('ui.startup_view.copy_threshold_d271'),
            (
                tr('ui.startup_view.used_in_hybrid_mode_smaller_files_77c8')
            ),
            self.copy_limit_spin,
            search_terms=(
                "copy limit mb мегабайт размер size threshold порог",
                "hybrid гибридный крупные файлы ссылка",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.copy_limit_row)

        info = localize(QLabel(
            '',
            self.rows_host,
        ), 'ui.startup_view.copy_mode_stores_a_separate_attachment_0dde')
        info.setWordWrap(True)
        info.setObjectName("StartupSettingDescription")
        self.info_row = SettingRow(
            tr('ui.startup_view.storage_behavior_10de'),
            (
                tr('ui.startup_view.this_mode_controls_attachment_storage_only_413d')
            ),
            info,
            search_terms=(
                "session data данные сессии original path исходный путь",
                "copy link копия ссылка не изменяет исходный файл",
            ),
            parent=self.rows_host,
        )
        self.add_row(self.info_row)

        self.storage_combo.currentIndexChanged.connect(self._storage_mode_changed)
        self.copy_limit_spin.valueChanged.connect(self._copy_limit_changed)

        self.reload_from_config()

    def _read_config(self) -> Dict[str, Any]:
        try:
            from ui.startup_data import read_startup_data
            return read_startup_data(APP_PATHS.config, "yaml")
        except Exception:
            return {}

    def _write_config(self, config: Dict[str, Any]) -> None:
        APP_PATHS.config.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".config.",
            suffix=".yaml",
            dir=str(APP_PATHS.config.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    config,
                    handle,
                    allow_unicode=True,
                    sort_keys=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, APP_PATHS.config)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _update_attachments(
        self,
        updater: Callable[[Dict[str, Any]], None],
    ) -> None:
        config = self._read_config()
        settings = dict(config.get("attachments") or {})
        updater(settings)
        config["attachments"] = settings
        self._write_config(config)

    def _show_write_error(self, error: Exception) -> None:
        QMessageBox.critical(self, tr('ui.startup_view.attachment_settings_20b9'), str(error))

    def reload_from_config(self) -> None:
        config = self._read_config()
        attachments = dict(config.get("attachments") or {})

        storage_mode = str(
            attachments.get("storage_mode", "copy")
        ).strip().lower()
        if storage_mode not in {"copy", "link", "hybrid"}:
            storage_mode = "copy"

        try:
            copy_limit_mb = max(
                0,
                int(attachments.get("copy_limit_mb", 25)),
            )
        except (TypeError, ValueError):
            copy_limit_mb = 25

        self._loading = True
        try:
            storage_index = self.storage_combo.findData(storage_mode)
            self.storage_combo.setCurrentIndex(max(0, storage_index))
            self.copy_limit_spin.setValue(copy_limit_mb)
        finally:
            self._loading = False

    def _storage_mode_changed(self, _index: int) -> None:
        if self._loading:
            return
        mode = str(self.storage_combo.currentData() or "")
        if mode not in {"copy", "link", "hybrid"}:
            return
        try:
            self._update_attachments(
                lambda settings: settings.__setitem__(
                    "storage_mode",
                    mode,
                )
            )
        except Exception as error:
            self._show_write_error(error)
            self.reload_from_config()

    def _copy_limit_changed(self, value: int) -> None:
        if self._loading:
            return
        try:
            self._update_attachments(
                lambda settings: settings.__setitem__(
                    "copy_limit_mb",
                    max(0, int(value)),
                )
            )
        except Exception as error:
            self._show_write_error(error)
            self.reload_from_config()


class LinguisticLspSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__(tr('nav.linguistic'), tr('ui.caption.d8324d2758e3'),
                         ('App', 'Workspaces', 'File', 'LSP', tr('nav.linguistic')), parent)
        from elements.lsp import lsp_manager
        self.manager = lsp_manager
        self.server = None
        self._reloading = False
        self.servers = QListWidget(self.rows_host)
        self.servers.itemChanged.connect(self._toggle_server)
        self.servers.currentItemChanged.connect(self._select_server)
        self.rows_layout.addWidget(self.servers)
        self.details = QWidget(self.rows_host)
        detail_layout = QVBoxLayout(self.details)
        self.title = QLabel(self.details)
        self.status = QLabel(self.details)
        self.status.setWordWrap(True)
        self.words = QListWidget(self.details)
        self.words.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.word_input = QLineEdit(self.details)
        set_text(self.word_input, 'ui.startup_view.word_to_add_to_exceptions_d753', setter='setPlaceholderText')
        self.word_input.returnPressed.connect(self._add_word)
        add = localize(QPushButton('', self.details), 'ui.widget.add_7103')
        add.clicked.connect(self._add_word)
        remove = localize(QPushButton('', self.details), 'ui.startup_view.delete_selected_8027')
        remove.clicked.connect(self._remove_words)
        input_row = QHBoxLayout()
        input_row.addWidget(self.word_input, 1)
        input_row.addWidget(add)
        for widget in (self.title, self.status, self.words):
            detail_layout.addWidget(widget)
        detail_layout.addLayout(input_row)
        detail_layout.addWidget(remove)
        self.rows_layout.addWidget(self.details)
        self.manager.settings_changed.connect(self.reload)
        self.reload()

    def apply_filter(self, query):
        blob = self.page_search_blob() + ' dictionary словарь exceptions исключения harper ' + ' '.join(self.manager.servers_config)
        match = all(token in blob for token in _normalized_tokens(query))
        self.rows_host.setVisible(match)
        self.empty_label.setVisible(not match)
        return match

    def reload(self):
        from PySide6.QtWidgets import QListWidgetItem
        self._reloading = True
        try:
            selected = self.server
            self.servers.clear()
            for name, conf in self.manager.installed_servers('linguistic'):
                item = QListWidgetItem(conf.get('display_name', name))
                item.setData(Qt.UserRole, name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if conf.get('enabled', True) else Qt.Unchecked)
                add_caption_item(self.servers, item)
                if name == selected:
                    self.servers.setCurrentItem(item)
            if self.servers.currentItem() is None and self.servers.count():
                self.servers.setCurrentRow(0)
            self._select_server(self.servers.currentItem(), None)
        finally:
            self._reloading = False

    def _select_server(self, item, _previous):
        self.server = item.data(Qt.UserRole) if item else None
        self.details.setVisible(bool(self.server))
        if not self.server:
            return
        conf = self.manager.servers_config[self.server]
        bind_caption(self.title, conf.get('display_name', self.server))
        bind_caption(self.status, tr('ui.caption.0deebf44c72f') + ', '.join(conf.get('file_extensions', []))
                            + tr('ui.caption.f5dd8b018dad') + str(self.manager.dictionary_path(self.server)))
        self.words.clear()
        try:
            add_caption_items(self.words, self.manager.dictionary_words(self.server))
        except OSError as error:
            QMessageBox.warning(self, tr('ui.file_view.lsp_821e'), str(error))

    def _toggle_server(self, item):
        if not self._reloading:
            try:
                self.manager.set_enabled(item.data(Qt.UserRole), item.checkState() == Qt.Checked)
            except OSError as error:
                QMessageBox.warning(self, tr('ui.file_view.lsp_821e'), str(error))
                self.reload()

    def _add_word(self):
        if self.server and self.word_input.text().strip():
            try:
                self.manager.add_exception(self.server, self.word_input.text().strip())
                self.word_input.clear()
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, tr('ui.file_view.lsp_821e'), str(error))

    def _remove_words(self):
        if self.server:
            selected = {item.text() for item in self.words.selectedItems()}
            try:
                self.manager.set_dictionary(self.server, [w for w in self.manager.dictionary_words(self.server) if w not in selected])
            except OSError as error:
                QMessageBox.warning(self, tr('ui.file_view.lsp_821e'), str(error))


class FileUnloadingSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__("File unloading", tr('ui.caption.4da391060321'),
                         ("App", "Performance", "File unloading"), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self.controls = {}
        self._reloading = False
        for kind, label in (("text", "Text files"), ("board", "Boards"), ("pipeline", "Pipelines"), ("paint", "Sketch canvases")):
            enabled = localize(QCheckBox('', self), 'ui.startup_view.enabled_92c1')
            limit = QSpinBox(self)
            limit.setRange(1, 10000)
            self.controls[kind] = (enabled, limit)
            self.add_row(SettingRow(
                "Unload " + label.lower(),
                tr('ui.startup_view.unload_inactive_tabs_from_memory_while_e51a'), enabled,
                search_terms=(kind, "memory ram unloading выгрузка память"), parent=self))
            self.add_row(SettingRow(
                label + " kept in memory",
                tr('ui.startup_view.the_limit_includes_the_current_tab_6096'),
                limit, search_terms=(kind, "limit threshold порог лимит"), parent=self))
            enabled.toggled.connect(lambda value, k=kind: self._write(k, "enabled", value))
            limit.valueChanged.connect(lambda value, k=kind: self._write(k, "limit", value))
        self.unsaved = QComboBox(self)
        add_item(self.unsaved, 'ui.startup_view.keep_unsaved_documents_loaded_b4e8', "hold")
        add_item(self.unsaved, 'ui.startup_view.save_and_unload_db2d', "save")
        self.add_row(SettingRow(
            tr('ui.startup_view.unsaved_changes_a710'), tr('ui.startup_view.keeping_unsaved_changes_may_exceed_the_5dc7'), self.unsaved,
            search_terms=("save undo redo dirty несохраненные"), parent=self))
        self.unsaved.currentIndexChanged.connect(
            lambda _: self._write("unsaved", None, self.unsaved.currentData()))
        self.preferences.changed.connect(self.reload)
        self.reload()

    def reload(self):
        self._reloading = True
        try:
            for kind, (enabled, limit) in self.controls.items():
                option = self.preferences.values[kind]
                enabled.setChecked(option["enabled"])
                limit.setValue(option["limit"])
                limit.setEnabled(option["enabled"])
            self.unsaved.setCurrentIndex(self.unsaved.findData(self.preferences.values["unsaved"]))
        finally:
            self._reloading = False

    def _write(self, kind, key, value):
        if self._reloading:
            return
        try:
            self.preferences.update(kind, key, value)
        except OSError as error:
            QMessageBox.warning(self, tr('ui.startup_view.performance_442a'), tr('ui.startup_view.could_not_save_settings_v0_772f', v0=error))
            self.reload()


class SessionsSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__('Sessions', tr('ui.caption.43093d4a38db'),
                         ('App', 'Performance', 'Sessions'), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self._reloading = False
        self.keep = localize(QCheckBox('', self), 'ui.startup_view.keep_opened_sessions_loaded_until_the_53e5')
        self.paged = localize(QCheckBox('', self), 'ui.startup_view.load_history_in_pages_cb33')
        self.size = QSpinBox(self)
        self.size.setRange(1, 500)
        for title, description, control in (
            ('Keep opened sessions', tr('ui.caption.864983e17d44'), self.keep),
            ('Paged loading', tr('ui.caption.84a050337417'), self.paged),
            ('Messages per page', tr('ui.caption.578fe50955d9'), self.size)):
            self.add_row(SettingRow(title, description, control,
                search_terms=('sessions history memory cache сообщения история сессии память'), parent=self))
        self.keep.toggled.connect(lambda value: self.write('keep_loaded', value))
        self.paged.toggled.connect(lambda value: self.write('paged', value))
        self.size.valueChanged.connect(lambda value: self.write('page_size', value))
        self.preferences.changed.connect(self.reload)
        self.reload()

    def reload(self):
        self._reloading = True
        values = self.preferences.values['sessions']
        self.keep.setChecked(values['keep_loaded'])
        self.paged.setChecked(values['paged'])
        self.size.setValue(values['page_size'])
        self.size.setEnabled(values['paged'])
        self._reloading = False

    def write(self, key, value):
        if not self._reloading:
            try:
                self.preferences.update('sessions', key, value)
            except OSError as error:
                QMessageBox.warning(self, tr('ui.startup_view.sessions_6fa3'), str(error))
                self.reload()


class TerminalSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__('Terminal', tr('ui.caption.9b70fa86fb43'),
                         ('App', 'Performance', 'Terminal'), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self.lines = QSpinBox(self)
        self.lines.setRange(100, 200000)
        self.lines.setSingleStep(1000)
        self.add_row(SettingRow(tr('ui.startup_view.scrollback_lines_1519'),
            tr('ui.startup_view.maximum_lines_in_each_console_applies_0118'),
            self.lines, search_terms=('terminal console history lines терминал консоль история строки'), parent=self))
        self.lines.setValue(self.preferences.values['terminal']['lines'])
        self.lines.valueChanged.connect(self.write)
        self.preferences.changed.connect(self.reload)

    def reload(self):
        blocked = self.lines.blockSignals(True)
        try:
            self.lines.setValue(self.preferences.values['terminal']['lines'])
        finally:
            self.lines.blockSignals(blocked)

    def write(self, value):
        try:
            self.preferences.update('terminal', 'lines', value)
        except OSError as error:
            QMessageBox.warning(self, tr('ui.startup_view.terminal_e092'), str(error))
            self.reload()


class WorkspaceSettingsPage(SettingsPage):
    def __init__(self, kind, title, description='', parent=None):
        super().__init__(title, description, ('App', 'Workspaces', title), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self.workspace_kind = kind
        self.workspace_enabled = bind_caption(QCheckBox('', self), tr('ui.caption.edbfa8e11e1a') + title)
        self.add_row(SettingRow(tr('ui.startup_view.load_workspace_0983'),
            tr('ui.startup_view.applies_after_a_full_application_restart_6ab2'),
            self.workspace_enabled,
            search_terms=(kind, 'enable disable startup включить отключить загрузка воркспейс'), parent=self))
        self._reload_workspace_option()
        self.workspace_enabled.toggled.connect(self._write_workspace_option)
        self.preferences.changed.connect(self._reload_workspace_option)

    def _reload_workspace_option(self):
        blocked = self.workspace_enabled.blockSignals(True)
        try:
            self.workspace_enabled.setChecked(self.preferences.values['workspaces'][self.workspace_kind])
        finally:
            self.workspace_enabled.blockSignals(blocked)

    def _write_workspace_option(self, value):
        try:
            self.preferences.update('workspaces', self.workspace_kind, value)
        except OSError as error:
            QMessageBox.warning(self, tr('ui.startup_view.workspaces_1377'), str(error))
            self._reload_workspace_option()


class BoardSettingsPage(WorkspaceSettingsPage):
    def __init__(self, parent=None):
        super().__init__('board', 'Board',
                         tr('ui.caption.75a7a7e6f34d'), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self._reloading = False
        self.hide_contents = localize(QCheckBox('', self), 'ui.startup_view.hide_card_contents_when_zoomed_out_5eae')
        self.threshold = QSpinBox(self)
        self.threshold.setRange(1, 100)
        bind_caption(self.threshold, ' %', setter='setSuffix')
        self.add_row(SettingRow(tr('ui.startup_view.reduce_detail_when_zoomed_out_3c55'),
            tr('ui.startup_view.below_the_selected_zoom_text_becomes_f222'),
            self.hide_contents, search_terms=('board zoom performance доска масштаб содержимое карточек'), parent=self))
        self.add_row(SettingRow(tr('ui.startup_view.content_visibility_threshold_5f93'),
            tr('ui.startup_view.for_example_40_contents_are_visible_7545'),
            self.threshold, search_terms=('board zoom threshold порог отдаление оптимизация'), parent=self))
        self.hide_contents.toggled.connect(lambda value: self.write('enabled', value))
        self.threshold.valueChanged.connect(lambda value: self.write('threshold', value))
        self.preferences.changed.connect(self.reload)
        self.reload()

    def reload(self):
        self._reloading = True
        try:
            options = self.preferences.values['board_rendering']
            self.hide_contents.setChecked(options['enabled'])
            self.threshold.setValue(options['threshold'])
            self.threshold.setEnabled(options['enabled'])
        finally:
            self._reloading = False

    def write(self, key, value):
        if not self._reloading:
            try:
                self.preferences.update('board_rendering', key, value)
            except OSError as error:
                QMessageBox.warning(self, tr('ui.startup_view.board_4816'), str(error))
                self.reload()


class MarkdownSettingsPage(SettingsPage):
    def __init__(self, parent=None):
        super().__init__('Markdown', tr('ui.caption.9a990e1dd613'),
                         ('App', 'Workspaces', 'Markdown'), parent)
        from ui.tab_memory import performance_preferences
        self.preferences = performance_preferences
        self.auto_pairs = localize(QCheckBox('', self), 'ui.startup_view.automatically_insert_matching_markup_characters_2dc9')
        self.add_row(SettingRow(tr('ui.startup_view.auto_pairs_a609'), tr('ui.startup_view.the_cursor_is_placed_between_matching_2953'),
            self.auto_pairs, search_terms=('markdown pairs парные символы разметка'), parent=self))
        self.auto_pairs.setChecked(self.preferences.values['markdown']['auto_pairs'])
        self.auto_pairs.toggled.connect(self.write)

    def write(self, value):
        try:
            self.preferences.update('markdown', 'auto_pairs', value)
        except OSError as error:
            QMessageBox.warning(self, tr('ui.startup_view.markdown_0e52'), str(error))


class StartupSettingsPanel(QFrame):
    """Godot-style settings browser: searchable category tree + property pane."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StartupSettingsPanel")
        apply_style(self, 'QFrame#StartupSettingsPanel {border:1px solid @border.subtle@;border-radius:6px;} QFrame#StartupSettingsPanel:focus {border-color:@border.subtle@;} QSplitter#StartupSettingsSplitter {border:0;}')
        self._pages: Dict[str, SettingsPage] = {}
        self._tree_items: Dict[str, QTreeWidgetItem] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setObjectName("StartupSettingsSplitter")
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

        # Left: search + category/subcategory tree.
        navigation = QFrame(splitter)
        navigation.setObjectName("StartupSettingsNavigation")
        nav_layout = QVBoxLayout(navigation)
        nav_layout.setContentsMargins(10, 10, 8, 10)
        nav_layout.setSpacing(8)

        self.search_edit = QLineEdit(navigation)
        self.search_edit.setObjectName("StartupSettingsSearch")
        set_text(self.search_edit, 'ui.startup_view.search_settings_d5fa', setter='setPlaceholderText')
        self.search_edit.setClearButtonEnabled(True)
        nav_layout.addWidget(self.search_edit)

        self.tree = QTreeWidget(navigation)
        self.tree.setObjectName("StartupSettingsTree")
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        # Disclosure and hierarchy offset are rendered as text. Native tree
        # indentation is zero, so KDE/Qt has no branch rectangle to paint.
        self.tree.setItemsExpandable(False)
        self.tree.setExpandsOnDoubleClick(False)
        nav_layout.addWidget(self.tree, 1)

        # Right: actual settings.
        right = QFrame(splitter)
        right.setObjectName("StartupSettingsContent")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.stack = QStackedWidget(right)
        self.stack.setObjectName("StartupSettingsStack")
        right_layout.addWidget(self.stack)

        self.empty_page = localize(QLabel(
            '',
            self.stack,
        ), 'ui.startup_view.no_matching_settings_found_472e')
        self.empty_page.setObjectName("StartupSettingsNoResults")
        self.empty_page.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(self.empty_page)

        splitter.addWidget(navigation)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([NAV_DEFAULT_WIDTH, 900])

        from ui.views.api_settings import ApiSettingsPage
        self._register_page('api', ('API',), ApiSettingsPage(self.stack))
        from ui.views.update_settings import UpdateSettingsPage
        self._register_page("app.updates", ("App", "Updates"), UpdateSettingsPage(self.stack))
        from ui.views.backup_settings import BackupSettings
        self._register_page('app.workspaces.projects.backups', ('App', 'Workspaces', 'Projects', 'Backups'), BackupSettings(self.stack))
        from ui.views.hotkey_settings import HotkeySettingsPage
        self._register_page('app.hotkeys', ('App', tr('ui.settings.hotkeys')),
            HotkeySettingsPage(self.stack))
        from ui.views.context_settings import ContextSettingsPage
        self._register_page('core.context', ('Core', 'Context'), ContextSettingsPage(self.stack))
        from ui.views.timeout_settings import TimeoutSettingsPage
        self._register_page('core.timeouts', ('Core', 'Timeouts'), TimeoutSettingsPage(self.stack))

        from ui.views.memory_settings import MemorySettingsPage
        self._register_page('core.system_modules.memory', ('Core', 'System Modules', 'Memory'),
                            MemorySettingsPage(self.stack))
        self._register_page(
            "core.system_modules.memory.embeddings",
            ("Core", "System Modules", "Memory", "Embeddings"),
            EmbeddingSettingsPage(self.stack),
        )

        # Installed modules can supply their own ui/settings.py. The host only
        # discovers and places their pages, independent of module enablement.
        for module_name in _system_module_names():
            # Memory is an application/core settings group, not one of the
            # currently discovered module packages.
            if module_name == "memory":
                continue
            self._register_module_page('system', module_name)

        self._register_page(
            "app.performance.file_unloading",
            ("App", "Performance", "File unloading"),
            FileUnloadingSettingsPage(self.stack),
        )
        self._register_page('app.performance.sessions', ('App', 'Performance', 'Sessions'), SessionsSettingsPage(self.stack))
        self._register_page('app.performance.terminal', ('App', 'Performance', 'Terminal'), TerminalSettingsPage(self.stack))
        self._register_page('app.workspaces.board', ('App', 'Workspaces', 'Board'), BoardSettingsPage(self.stack))
        self._register_page('app.workspaces.paint', ('App', 'Workspaces', 'Sketch'),
                            WorkspaceSettingsPage('paint', 'Sketch', tr('ui.caption.9ea705a70855'), self.stack))
        self._register_page('app.workspaces.markdown', ('App', 'Workspaces', 'Markdown'), MarkdownSettingsPage(self.stack))
        self._register_page('app.workspaces.file.lsp.code', ('App', 'Workspaces', 'File', 'LSP', 'code'),
                            EmptySettingsPage(tr('ui.startup_view.code_5694'), ('App', 'Workspaces', 'File', 'LSP', 'code'), parent=self.stack))
        self._register_page('app.workspaces.file.lsp.linguistic', ('App', 'Workspaces', 'File', 'LSP', tr('nav.linguistic')),
                            LinguisticLspSettingsPage(self.stack))
        self._register_page(
            "app.workspaces.chat.attachments",
            ("App", "Workspaces", "Chat", "Attachments"),
            AttachmentSettingsPage(self.stack),
        )
        from ui.views.appearance_settings import AppearanceSettingsPage
        self._register_page('app.personalization', ('App', 'Personalization'), AppearanceSettingsPage(self.stack))

        self.search_edit.textChanged.connect(self._apply_search)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemClicked.connect(self._tree_item_clicked)
        self.tree.itemExpanded.connect(self._refresh_tree_item_label)
        self.tree.itemCollapsed.connect(self._refresh_tree_item_label)

        i18n.language_changed.connect(self._refresh_all_tree_labels)
        self.tree.expandAll()
        self._refresh_all_tree_labels()
        first = self._first_visible_leaf()
        if first is not None:
            self.tree.setCurrentItem(first)
            self._selection_changed()

    def _register_module_page(self, category, name):
        from ui.module_settings import load_settings_page
        display_name = _module_display_name(name)
        group = 'System Modules' if category == 'system' else 'Custom Modules'
        path = ('Core', group, display_name)
        description = tr('ui.caption.c8e1aa8999f9')
        try:
            page = load_settings_page(category, name, self.stack, root=APP_PATHS.root)
            if page is not None and not isinstance(page, SettingsPage):
                page.deleteLater()
                raise TypeError('Страница настроек модуля должна наследовать SettingsPage.')
        except Exception as error:
            from core.logger import core_logger
            core_logger.warn('MODULE_SETTINGS', f'Could not load settings for {category}/{name}: {error}')
            description = tr('ui.caption.cb4fcee0e5a7')
            page = None
        if page is None:
            page = EmptySettingsPage(display_name, path, description=description,
                search_terms=(name, category, 'module модуль'), parent=self.stack)
        self._register_page(f'core.{category}_modules.{name}', path, page)

    def _ensure_category_path(
        self,
        path: Sequence[str],
    ) -> Optional[QTreeWidgetItem]:
        parent_item: Optional[QTreeWidgetItem] = None
        path_parts: List[str] = []

        for label in path:
            path_parts.append(label)
            node_key = "/".join(path_parts)
            item = self._tree_items.get(node_key)
            if item is None:
                item = QTreeWidgetItem(
                    parent_item if parent_item is not None else self.tree
                )
                bind_caption(item, label, setter='setText', index=0)
                item.setData(0, Qt.UserRole, None)
                item.setData(0, Qt.UserRole + 1, label)
                self._tree_items[node_key] = item
            parent_item = item

        return parent_item

    def _register_page(
        self,
        key: str,
        path: Sequence[str],
        page: SettingsPage,
    ) -> None:
        self._pages[key] = page
        self.stack.addWidget(page)

        parent_item = self._ensure_category_path(path)
        if parent_item is not None:
            parent_item.setData(0, Qt.UserRole, key)
            bind_caption(parent_item, " / ".join(path), setter='setToolTip', index=0)

    def _refresh_tree_item_label(self, item: QTreeWidgetItem) -> None:
        """Render hierarchy without using QTreeView's native indent area."""
        if item is None:
            return

        raw_label = str(
            item.data(0, Qt.UserRole + 1) or item.text(0)
        ).lstrip("▾▸ ").strip()
        item.setData(0, Qt.UserRole + 1, raw_label)

        depth = 0
        parent = item.parent()
        while parent is not None:
            depth += 1
            parent = parent.parent()

        # Category identities remain in UserRole; module-supplied names stay literal.
        navigation = {'App':'nav.app','Core':'nav.core','Context':'nav.context','Timeouts':'nav.timeouts','Personalization':'nav.personalization',
                      'Updates':'nav.updates','System Modules':'nav.system_modules','Custom Modules':'nav.custom_modules',
                      'Performance':'nav.performance','Workspaces':'nav.workspaces','File':'nav.file',
                      'Board':'nav.board','Sketch':'nav.sketch','Sessions':'nav.sessions',
                      'Chat':'nav.chat','Projects':'nav.projects','Attachments':'nav.attachments','Backups':'nav.backups',
                      'Вложения':'nav.attachments','Резервные копии':'nav.backups',
                      'Memory':'nav.memory','Embeddings':'nav.embeddings','Лингвистика':'nav.linguistic'}
        parent_label = item.parent().data(0, Qt.UserRole + 1) if item.parent() is not None else ''
        module_name = parent_label == 'Custom Modules' or (parent_label == 'System Modules' and raw_label != 'Memory')
        key = None if module_name else navigation.get(raw_label)
        display = tr(key) if key else raw_label
        if item.childCount() > 0:
            arrow = "▾" if item.isExpanded() else "▸"
            bind_caption(item, f"{'    ' * depth}{arrow}  {display}", setter='setText', index=0)
        else:
            # Plain spaces provide the visual child offset. Because the actual
            # QTreeWidget indentation is zero, there is no native rectangle
            # left of the selected item for KDE/Breeze to color.
            bind_caption(item, f"{'    ' * depth}{display}", setter='setText', index=0)

    def _refresh_all_tree_labels(self, *_) -> None:
        for item in self._tree_items.values():
            self._refresh_tree_item_label(item)

    def _first_visible_leaf(self) -> Optional[QTreeWidgetItem]:
        root = self.tree.invisibleRootItem()
        stack = [root.child(index) for index in range(root.childCount())]
        while stack:
            item = stack.pop(0)
            if item.isHidden():
                continue
            key = item.data(0, Qt.UserRole)
            if key in self._pages:
                return item
            for index in range(item.childCount()):
                stack.append(item.child(index))
        return None

    def _page_item(self, key: str) -> Optional[QTreeWidgetItem]:
        for item in self._tree_items.values():
            if item.data(0, Qt.UserRole) == key:
                return item
        return None

    def _tree_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # Leaf/page items just select their settings page.
        if item.data(0, Qt.UserRole) in self._pages:
            return

        # Category items only expand/collapse. Do not select a child here:
        # making a hidden child current causes QTreeWidget to auto-expand its
        # ancestors again, which made Core/App look impossible to close.
        if item.childCount() > 0:
            item.setExpanded(not item.isExpanded())
            self._refresh_tree_item_label(item)
            return

    def _selection_changed(self) -> None:
        item = self.tree.currentItem()
        if item is None or item.isHidden():
            self.stack.setCurrentWidget(self.empty_page)
            return

        key = item.data(0, Qt.UserRole)
        page = self._pages.get(str(key))
        if page is None:
            return
        self.stack.setCurrentWidget(page)

    def _apply_search(self, query: str) -> None:
        visible_keys = {
            key
            for key, page in self._pages.items()
            if page.apply_filter(query)
        }

        # Leaves follow page matches.
        for key in self._pages:
            item = self._page_item(key)
            if item is not None:
                item.setHidden(key not in visible_keys)

        # Parent categories stay visible only if at least one descendant page
        # survived the full-text settings search.
        tokens = _normalized_tokens(query)
        for item in reversed(list(self._tree_items.values())):
            if item.data(0, Qt.UserRole) in self._pages:
                continue

            if item.childCount() == 0:
                # Empty structural categories (for example Custom Modules when
                # there are no custom modules yet) remain visible normally.
                label = str(item.data(0, Qt.UserRole + 1) or "").casefold()
                item.setHidden(bool(tokens) and not all(token in label for token in tokens))
                continue

            any_visible = any(
                not item.child(index).isHidden()
                for index in range(item.childCount())
            )
            item.setHidden(not any_visible)

        if query:
            i18n.language_changed.connect(self._refresh_all_tree_labels)
        self.tree.expandAll()

        current = self.tree.currentItem()
        if (
            current is None
            or current.isHidden()
            or current.data(0, Qt.UserRole) not in visible_keys
        ):
            first = self._first_visible_leaf()
            if first is None:
                self.tree.setCurrentItem(None)
                self.stack.setCurrentWidget(self.empty_page)
            else:
                self.tree.setCurrentItem(first)
                self._selection_changed()
        else:
            self._selection_changed()


class StartupView(QWidget):
    """Startup controls above a settings browser sized to its visible viewport."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StartupView")
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)

        self.core_area = QFrame(self)
        self.core_area.setObjectName("StartupCoreArea")
        self.core_area.setMinimumSize(0, 0)

        self.core_hub = OrbitalCoreHub(self.core_area)
        self.core_hub.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.subagent_orbitals = SubagentOrbitals(self.core_hub, self.core_area)

        from ui.core_bridge.process_manager import core_process
        from ui.widgets.error_actions import ErrorDetailsPanel
        self.core_error_panel = ErrorDetailsPanel(self.core_area)
        self.core_error_panel.setObjectName('StartupCoreError')
        self.core_error_panel.dismissed.connect(self._update_child_geometry)
        apply_style(self.core_error_panel, 'QPlainTextEdit { background: @background.panel@; color: #fca5a5; border: 1px solid #71383d; border-radius: 5px; padding: 8px; font-size: 12px; }')
        self.core_error_panel.hide()
        core_process.failure_details_changed.connect(self._core_failure_details)
        self.core_error_panel.setPlainText(core_process.failure_details)
        self.core_error_panel.setVisible(bool(core_process.failure_details))

        self.settings_panel = StartupSettingsPanel(self)
        self.settings_panel.setMinimumSize(0, 0)

        # Track the visible settings viewport, including when the panel shrinks.
        self._settings_surface_height = 1

        apply_style(self, '\n            #StartupView {\n                background: @background.window@;\n            }\n            #StartupCoreArea {\n                background: @background.window@;\n                border-bottom: 1px solid @border.subtle@;\n            }\n            #StartupSettingsPanel,\n            #StartupSettingsContent,\n            #StartupSettingsPage,\n            #StartupSettingsPageBody {\n                background: @background.window@;\n                border: none;\n            }\n            #StartupSettingsNavigation {\n                background: @background.window@;\n                border-right: 1px solid @border.subtle@;\n            }\n            #StartupSettingsSearch {\n                min-height: 28px;\n                padding: 0 8px;\n                background: @background.window@;\n                color: @text.primary@;\n                border: 1px solid @border.subtle@;\n                border-radius: 4px;\n            }\n            #StartupSettingsSearch:focus {\n                border-color: @border.focus@;\n            }\n            #StartupSettingsTree {\n                background: transparent;\n                color: @text.primary@;\n                border: none;\n                outline: none;\n            }\n            #StartupSettingsTree::branch,\n            #StartupSettingsTree::branch:selected,\n            #StartupSettingsTree::branch:open,\n            #StartupSettingsTree::branch:closed,\n            #StartupSettingsTree::branch:has-children,\n            #StartupSettingsTree::branch:has-siblings,\n            #StartupSettingsTree::branch:adjoins-item {\n                background: transparent;\n                border: 0px;\n                image: none;\n            }\n            #StartupSettingsTree::item {\n                min-height: 27px;\n                padding: 2px 4px;\n                border: none;\n                border-radius: 3px;\n            }\n            #StartupSettingsTree::item:selected {\n                background: @control.selected@;\n                color: @text.primary@;\n            }\n            #StartupSettingsHeading {\n                color: @text.primary@;\n                background: transparent;\n                font-size: 18px;\n                font-weight: 700;\n                padding-bottom: 4px;\n            }\n            #StartupSettingsSummary,\n            #StartupSettingDescription {\n                color: @text.secondary@;\n                background: transparent;\n                font-size: 11px;\n            }\n            #StartupSettingsRows {\n                background: @background.surface@;\n                border: 1px solid @border.subtle@;\n                border-radius: 4px;\n            }\n            #StartupSettingRow {\n                background: transparent;\n                border-bottom: 1px solid @background.surface@;\n            }\n            #StartupSettingTitle {\n                color: @text.primary@;\n                font-size: 12px;\n                font-weight: 600;\n            }\n            #StartupSettingsWarning {\n                color: #d6a82d;\n                background: transparent;\n            }\n            #StartupSettingsEmpty,\n            #StartupSettingsNoResults {\n                color: @text.disabled@;\n            }\n            ')


    def _core_failure_details(self, text):
        self.core_error_panel.setPlainText(text)
        self.core_error_panel.setVisible(bool(text))
        self._update_child_geometry()

    def _update_child_geometry(self) -> None:
        width = max(0, self.width())
        height = max(0, self.height())

        # The settings browser has a stable top edge. When the whole lower
        # panel becomes shorter than this point, the browser simply disappears
        # below the parent's clipping boundary instead of pushing anything.
        error_height = 170 if not self.core_error_panel.isHidden() else 0
        settings_y = SETTINGS_TOP + error_height

        # Core controls occupy only the actually visible space above settings.
        # If the parent is shorter than SETTINGS_TOP, the hub is centered in
        # that smaller visible area.
        core_height = min(height, settings_y)
        self.core_area.setGeometry(0, 0, width, core_height)

        # OrbitalCoreHub sets itself to a fixed 370x370 size. Do not use
        # QWidget.sizeHint() here: for a plain QWidget it may be invalid even
        # though the widget has a real fixed size, which shifts the hub by
        # roughly half of its own dimensions.
        hub_width = self.core_hub.width()
        hub_height = self.core_hub.height()

        # Only move the already-sized hub. This centers the *actual* 370x370
        # widget inside the visible core area.
        hub_x = (width - hub_width) // 2
        hub_y = (min(height, SETTINGS_TOP) - hub_height) // 2
        self.core_hub.move(hub_x, hub_y)
        self.core_hub.raise_()
        self.subagent_orbitals.setGeometry(0, 0, width, min(height, SETTINGS_TOP))
        self.subagent_orbitals.raise_()
        if error_height:
            margin = min(16, width // 4)
            self.core_error_panel.setGeometry(margin, SETTINGS_TOP + 4, max(1, width - 2 * margin), error_height - 12)
            self.core_error_panel.raise_()

        available_settings_height = max(1, height - settings_y)
        self._settings_surface_height = available_settings_height

        # Scroll areas must see their actual visible height after every resize;
        # otherwise the lower scroll range is clipped outside the workspace.
        self.settings_panel.setGeometry(
            0,
            settings_y,
            width,
            self._settings_surface_height,
        )
        self.settings_panel.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_child_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_child_geometry()
