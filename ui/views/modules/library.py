from __future__ import annotations
from ui.i18n import add_caption_action, localize, set_text


from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QMenu, QVBoxLayout, QWidget, QWidgetAction

from .nodes.specs import PipelineNodeSpec
from .presentation import node_caption, category_caption
from ui.i18n import bind_caption


PREFERRED_ROOT_ORDER = (
    "System modules", "Custom modules", "routing", "controls", "processing", "Arguments",
)


class NodeLibraryMenu(QMenu):
    """Searchable menu generated entirely from registered node category paths."""

    def __init__(
        self,
        specs: Sequence[PipelineNodeSpec],
        on_create,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("PipelineNodeLibraryMenu")
        self._specs = [
            spec for spec in specs
            if not bool(spec.metadata.get("library_hidden"))
        ]
        self._on_create = on_create
        self._dynamic_actions = []

        search_host = QWidget(self)
        search_layout = QVBoxLayout(search_host)
        search_layout.setContentsMargins(6, 6, 6, 6)
        search_layout.setSpacing(0)
        self.search = QLineEdit(search_host)
        set_text(self.search, 'ui.library.search_nodes_0b5b', setter='setPlaceholderText')
        self.search.setClearButtonEnabled(True)
        search_layout.addWidget(self.search)

        search_action = QWidgetAction(self)
        search_action.setDefaultWidget(search_host)
        add_caption_action(self, 'addAction', search_action)
        self.addSeparator()

        self.search.textChanged.connect(self._rebuild)
        self._rebuild("")

    def showEvent(self, event):
        super().showEvent(event)
        self.search.setFocus(Qt.PopupFocusReason)

    def _clear_dynamic(self):
        for action in list(self._dynamic_actions):
            self.removeAction(action)
            action.deleteLater()
        self._dynamic_actions.clear()

    def _add_node_action(self, owner: QMenu, label: str, spec: PipelineNodeSpec):
        caption = node_caption(spec)
        label = caption if label == spec.title else label[:-len(spec.title)] + caption if spec.title and label.endswith(spec.title) else label
        action = add_caption_action(owner, 'addAction', label)
        action.setEnabled(spec.implemented)
        if not spec.implemented:
            set_text(action, 'ui.library.node_type_is_present_in_the_a1ee', setter='setToolTip')
        else:
            action.triggered.connect(lambda _checked=False, item=spec: self._on_create(item))
        return action

    def _root_names(self) -> List[str]:
        discovered = {
            spec.category_path[0]
            for spec in self._specs
            if spec.category_path
        }
        ordered = [root for root in PREFERRED_ROOT_ORDER if root in discovered]
        ordered.extend(sorted(discovered.difference(ordered), key=str.casefold))
        return ordered

    def _populate_path(self, menu: QMenu, prefix: tuple[str, ...]):
        direct = [
            spec for spec in self._specs
            if tuple(spec.category_path) == prefix
        ]
        child_names = sorted(
            {
                spec.category_path[len(prefix)]
                for spec in self._specs
                if len(spec.category_path) > len(prefix)
                and tuple(spec.category_path[:len(prefix)]) == prefix
            },
            key=str.casefold,
        )

        # Module/subgraph nodes naturally appear before their tools because they
        # live at exactly the same category path as the tool submenu.
        for spec in sorted(direct, key=lambda item: (item.kind != "module", item.title.casefold())):
            self._add_node_action(menu, spec.title, spec)

        if direct and child_names:
            menu.addSeparator()

        for child in child_names:
            child_menu = add_caption_action(menu, 'addMenu', category_caption(child) if not prefix else child)
            self._populate_path(child_menu, (*prefix, child))

        if not direct and not child_names:
            empty = localize(add_caption_action(menu, 'addAction', "(empty)"), 'ui.library.empty_d29a')
            empty.setEnabled(False)

    def _rebuild(self, query: str):
        self._clear_dynamic()
        query = str(query or "").strip().casefold()

        if query:
            matches = []
            for spec in self._specs:
                haystack = " ".join(
                    [
                        spec.title,
                        spec.type_id,
                        spec.kind,
                        *spec.category_path,
                        *[str(value) for value in spec.metadata.values()],
                    ]
                ).casefold()
                if query in haystack:
                    matches.append(spec)

            if not matches:
                action = localize(add_caption_action(self, 'addAction', "No matching nodes"), 'ui.library.no_matching_nodes_647b')
                action.setEnabled(False)
                self._dynamic_actions.append(action)
                return

            for spec in sorted(matches, key=lambda item: (item.category_path, item.title.casefold())):
                prefix = " / ".join(spec.category_path)
                action = self._add_node_action(self, f"{prefix} / {spec.title}", spec)
                self._dynamic_actions.append(action)
            return

        for spec in sorted((s for s in self._specs if not s.category_path), key=lambda s: s.title.casefold()):
            self._dynamic_actions.append(self._add_node_action(self, spec.title, spec))
        for root in self._root_names():
            submenu = QMenu(self)
            bind_caption(submenu, category_caption(root), setter='setTitle')
            action = add_caption_action(self, 'addMenu', submenu)
            self._dynamic_actions.append(action)
            self._populate_path(submenu, (root,))
