from __future__ import annotations
from ui.i18n import tr, set_text


from typing import Optional

from PySide6.QtWidgets import (QWidget)
from ui.widgets.dialogs import MessageBox as QMessageBox


MEMORY_MODE_SWITCH_WARNING = (
    tr('ui.caption.9e8604d5d3cc')
)


def memory_mode_label(mode: str) -> str:
    return tr('ui.projects_view.vectors_embeddings_4859') if str(mode or "").lower() == "vector" else tr('ui.projects_view.pages_text_memory_9567')


def choose_memory_mode(parent: Optional[QWidget] = None) -> Optional[str]:
    """Ask which temporary-memory representation a newly created session should use."""
    box = QMessageBox(parent)
    set_text(box, 'ui.memory_mode_dialog.session_memory_type_cabc', setter='setWindowTitle')
    box.setIcon(QMessageBox.Question)
    set_text(box, 'ui.memory_mode_dialog.choose_the_memory_type_for_a_0492')
    set_text(box, 'ui.memory_mode_dialog.pages_use_keyword_based_text_search_912b', setter='setInformativeText')

    text_button = box.addButton(tr('ui.caption.a4910ebcf12a'), QMessageBox.AcceptRole)
    vector_button = box.addButton(tr('ui.caption.4318c91d6d2c'), QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    from core.memory_preference import last_memory_mode, remember_memory_mode
    box.setDefaultButton(vector_button if last_memory_mode() == "vector" else text_button)
    box.exec()

    clicked = box.clickedButton()
    if clicked is text_button:
        remember_memory_mode("text")
        return "text"
    if clicked is vector_button:
        remember_memory_mode("vector")
        return "vector"
    return None
