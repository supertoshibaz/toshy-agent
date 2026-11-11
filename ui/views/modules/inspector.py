from __future__ import annotations
from ui.i18n import add_caption_action, bind_caption, add_caption_item, tr, localize, set_text
from ui.styles.binding import apply_style, form_field
from .presentation import node_caption, metadata_caption
from ui.icons import set_icon, symbol_button


import keyword
import math
import re
import uuid
from typing import List, Optional

from PySide6.QtCore import QEasingCurve, QPoint, QParallelAnimationGroup, QPropertyAnimation, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from modules.pipelines.control import (
    ARGUMENT_SCHEMA_TYPES,
    LOOP_CONDITION_DEFINITIONS,
    ROUTER_MECHANISM_DEFINITIONS,
    normalize_argument_definitions,
    normalize_loop_conditions,
    normalize_router_mechanisms,
)

from .knowledge_catalog import discover_knowledge_base_catalog
from .nodes.base import PipelineNodeItem


class BinaryValueChip(QFrame):
    value_changed = Signal(bool)
    remove_requested = Signal()

    def __init__(self, value: bool, parent=None):
        super().__init__(parent)
        self._value = bool(value)
        self.setObjectName("PipelineBinaryChip")
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 6, 4)
        row.setSpacing(6)

        self.label = QLabel(self._label_text(), self)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self.label)

        self.close_btn = QToolButton(self)
        symbol_button(self.close_btn, "✕")
        self.close_btn.setFixedSize(14, 14)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.remove_requested.emit)
        self.close_btn.hide()
        row.addWidget(self.close_btn)

    def _label_text(self) -> str:
        return "True" if self._value else "False"

    def value(self) -> bool:
        return self._value

    def set_value(self, value: bool):
        self._value = bool(value)
        bind_caption(self.label, self._label_text())

    def enterEvent(self, event):
        super().enterEvent(event)
        self.close_btn.show()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.close_btn.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            menu = QMenu(self)
            true_action = localize(add_caption_action(menu, 'addAction', "True"), 'ui.inspector.true_3cbc')
            false_action = localize(add_caption_action(menu, 'addAction', "False"), 'ui.inspector.false_60a3')
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen == true_action:
                self.set_value(True)
                self.value_changed.emit(True)
            elif chosen == false_action:
                self.set_value(False)
                self.value_changed.emit(False)
            event.accept()
            return
        super().mousePressEvent(event)


class LoopConditionDragHandle(QLabel):
    """Small explicit drag handle used to reorder enabled Loop mechanisms."""

    pressed = Signal()
    drag_moved = Signal(int)
    drag_finished = Signal()

    def __init__(self, parent=None):
        super().__init__("≡", parent)
        self.setObjectName("PipelineLoopConditionDragHandle")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedWidth(22)
        self.setCursor(Qt.SizeVerCursor)
        self._press_global_y = None
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_global_y = int(event.globalPosition().y())
            self._dragging = False
            self.pressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._press_global_y is not None
            and (event.buttons() & Qt.LeftButton)
        ):
            global_y = int(event.globalPosition().y())
            if self._dragging or abs(global_y - self._press_global_y) >= 4:
                self._dragging = True
                self.drag_moved.emit(global_y)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press_global_y is not None:
            was_dragging = self._dragging
            self._press_global_y = None
            self._dragging = False
            if was_dragging:
                self.drag_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class LoopConditionRow(QFrame):
    selected = Signal(str)
    remove_requested = Signal(str)
    drag_moved = Signal(str, int)
    drag_finished = Signal()

    def __init__(self, condition_type: str, title: str, parent=None):
        super().__init__(parent)
        self.condition_type = str(condition_type)
        self.setObjectName("PipelineLoopConditionRow")
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(40)

        row = QHBoxLayout(self)
        row.setContentsMargins(5, 4, 5, 4)
        row.setSpacing(7)

        self.drag_handle = LoopConditionDragHandle(self)
        self.drag_handle.pressed.connect(self._emit_selected)
        self.drag_handle.drag_moved.connect(self._emit_drag_moved)
        self.drag_handle.drag_finished.connect(self.drag_finished.emit)
        row.addWidget(self.drag_handle, 0, Qt.AlignVCenter)

        self.title_label = QLabel(str(title), self)
        self.title_label.setObjectName("PipelineLoopConditionTitle")
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self.title_label, 1, Qt.AlignVCenter)

        self.remove_button = QToolButton(self)
        self.remove_button.setObjectName("PipelineLoopConditionRemove")
        symbol_button(self.remove_button, "✕")
        self.remove_button.setFixedSize(22, 22)
        self.remove_button.setCursor(Qt.PointingHandCursor)
        self.remove_button.clicked.connect(self._emit_remove)
        row.addWidget(self.remove_button, 0, Qt.AlignVCenter)

    def _emit_selected(self):
        self.selected.emit(self.condition_type)

    def _emit_remove(self):
        self.remove_requested.emit(self.condition_type)

    def _emit_drag_moved(self, global_y: int):
        self.drag_moved.emit(self.condition_type, int(global_y))

    def set_selected(self, selected: bool):
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._emit_selected()
            event.accept()
            return
        super().mousePressEvent(event)


class PipelineInspector(QFrame):
    modified = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PipelineInspector")
        self.setMinimumWidth(290)
        self.setMaximumWidth(380)
        self._node: Optional[PipelineNodeItem] = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # The inspector itself has a fixed viewport. Its controls live in a
        # scrollable content widget so a tall node never causes settings to
        # overlap or escape the panel.
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("PipelineInspectorScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(self.scroll_area)

        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("PipelineInspectorScrollContent")
        self.scroll_area.setWidget(self.scroll_content)

        layout = QVBoxLayout(self.scroll_content)
        self._main_layout = layout
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self.title = QLabel("", self.scroll_content)
        self.title.setObjectName("PipelineInspectorTitle")
        layout.addWidget(self.title)

        self.router_cycle_warning = localize(QLabel(
            '',
            self.scroll_content,
        ), 'ui.inspector.this_router_participates_in_a_backward_ec4d')
        self.router_cycle_warning.setObjectName("PipelineRouterCycleWarning")
        self.router_cycle_warning.setWordWrap(True)
        self.router_cycle_warning.hide()
        layout.addWidget(self.router_cycle_warning)

        self.description = QLabel("", self)
        self.description.setObjectName("PipelineInspectorDescription")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        self.prompt_label = localize(QLabel('', self), 'ui.inspector.prompt_5c39')
        self.prompt_label.setObjectName("PipelineInspectorFieldLabel")
        layout.addWidget(self.prompt_label)

        self.prompt_edit = QPlainTextEdit(self)
        # This editor intentionally keeps its native QTextDocument undo stack.
        # Ctrl+Z here edits prompt text; Ctrl+Z on the canvas uses canvas.undo_stack.
        self.prompt_edit.setObjectName("PipelinePromptEditor")
        set_text(self.prompt_edit, 'ui.inspector.additional_prompt_text_60b6', setter='setPlaceholderText')
        self.prompt_edit.setMinimumHeight(110)
        self.prompt_edit.setMaximumHeight(180)
        self.prompt_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.prompt_edit.textChanged.connect(self._text_changed)
        layout.addWidget(self.prompt_edit)

        self.binary_button = localize(QPushButton('', self), 'ui.inspector.true_3cbc')
        self.binary_button.setObjectName("PipelineBinaryMenuButton")
        self.binary_button.setCursor(Qt.PointingHandCursor)
        self.binary_button.clicked.connect(self._open_binary_menu)
        layout.addWidget(self.binary_button)

        self.scalar_edit = QLineEdit(self)
        self.scalar_edit.setObjectName("PipelineScalarEditor")
        set_text(self.scalar_edit, 'ui.inspector.value_a36f', setter='setPlaceholderText')
        self.scalar_edit.textChanged.connect(self._scalar_changed)
        self.scalar_edit.editingFinished.connect(self._scalar_editing_finished)
        layout.addWidget(self.scalar_edit)

        self.array_type_label = localize(QLabel('', self), 'ui.inspector.item_type_5b71')
        self.array_type_label.setObjectName("PipelineInspectorFieldLabel")
        layout.addWidget(self.array_type_label)

        self.array_type_combo = QComboBox(self)
        self.array_type_combo.setObjectName("PipelineArrayTypeCombo")
        for label, value in (
            ("String", "string"),
            # UI terminology is Bool; keep the internal value "binary" for
            # backward compatibility with existing pipeline.py settings.
            ("Bool", "binary"),
            ("Integer", "integer"),
            ("Float", "float"),
        ):
            add_caption_item(self.array_type_combo, label, value)
        self.array_type_combo.currentIndexChanged.connect(self._array_type_changed)
        layout.addWidget(self.array_type_combo)

        self.array_edit = QLineEdit(self)
        self.array_edit.setObjectName("PipelineArrayEditor")
        set_text(self.array_edit, 'ui.inspector.value1_value2_value3_ed84', setter='setPlaceholderText')
        self.array_edit.textChanged.connect(self._array_text_changed)
        self.array_edit.editingFinished.connect(self._array_editing_finished)
        layout.addWidget(self.array_edit)

        self.binary_array_host = QFrame(self)
        self.binary_array_host.setObjectName("PipelineBinaryArrayHost")
        self.binary_array_layout = QHBoxLayout(self.binary_array_host)
        self.binary_array_layout.setContentsMargins(0, 0, 0, 0)
        self.binary_array_layout.setSpacing(6)
        self.binary_array_add = QToolButton(self.binary_array_host)
        self.binary_array_add.setObjectName("PipelineBinaryArrayAdd")
        set_icon(self.binary_array_add, 'add', square=True)
        self.binary_array_add.setFixedSize(28, 28)
        self.binary_array_add.setCursor(Qt.PointingHandCursor)
        self.binary_array_add.clicked.connect(self._open_binary_array_add_menu)
        self.binary_array_layout.addWidget(self.binary_array_add)
        self.binary_array_layout.addStretch(1)
        layout.addWidget(self.binary_array_host)

        self.knowledge_db_label = localize(QLabel('', self), 'ui.inspector.knowledge_bases_60cb')
        self.knowledge_db_label.setObjectName("PipelineInspectorFieldLabel")
        layout.addWidget(self.knowledge_db_label)

        self.knowledge_db_list = QListWidget(self)
        self.knowledge_db_list.setObjectName("PipelineKnowledgeBaseList")
        self.knowledge_db_list.setMinimumHeight(90)
        self.knowledge_db_list.setMaximumHeight(190)
        self.knowledge_db_list.setAlternatingRowColors(False)
        self.knowledge_db_list.itemChanged.connect(self._knowledge_databases_changed)
        layout.addWidget(self.knowledge_db_list)

        # Graph terminal nodes -------------------------------------------------
        self.pipeline_interface_button = localize(QPushButton('', self), 'ui.inspector.pipeline_arguments_9b70')
        self.pipeline_interface_button.clicked.connect(self._configure_pipeline_interface)
        self.pipeline_interface_button.hide()
        layout.addWidget(self.pipeline_interface_button)

        self.graph_message_label = localize(QLabel('', self), 'ui.inspector.message_2f77')
        self.graph_message_label.setObjectName("PipelineInspectorFieldLabel")
        layout.addWidget(self.graph_message_label)

        self.graph_message_edit = form_field(QPlainTextEdit(self))
        self.graph_message_edit.setObjectName("PipelineGraphMessageEditor")
        self.graph_message_edit.setMinimumHeight(90)
        self.graph_message_edit.setMaximumHeight(150)
        self.graph_message_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.graph_message_edit.textChanged.connect(self._graph_message_changed)
        layout.addWidget(self.graph_message_edit)

        # Loop control ---------------------------------------------------------
        self.loop_panel = QFrame(self)
        self.loop_panel.setObjectName("PipelineLoopPanel")
        self.loop_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        loop_layout = QVBoxLayout(self.loop_panel)
        loop_layout.setContentsMargins(0, 0, 0, 0)
        loop_layout.setSpacing(8)

        self.loop_max_label = localize(QLabel('', self.loop_panel), 'ui.inspector.maximum_iterations_82f4')
        self.loop_max_label.setObjectName("PipelineInspectorFieldLabel")
        loop_layout.addWidget(self.loop_max_label)

        self.loop_max_repeats = QSpinBox(self.loop_panel)
        self.loop_max_repeats.setObjectName("PipelineLoopSpin")
        self.loop_max_repeats.setRange(1, 9999)
        self.loop_max_repeats.setValue(10)
        self.loop_max_repeats.valueChanged.connect(self._loop_max_repeats_changed)
        loop_layout.addWidget(self.loop_max_repeats)

        self.loop_conditions_label = localize(QLabel(
            '',
            self.loop_panel,
        ), 'ui.inspector.repeat_mechanisms_order_evaluation_priority_5eee')
        self.loop_conditions_label.setObjectName("PipelineInspectorFieldLabel")
        self.loop_conditions_label.setWordWrap(True)
        loop_layout.addWidget(self.loop_conditions_label)

        self.loop_add_condition_button = QToolButton(self.loop_panel)
        self.loop_add_condition_button.setObjectName("PipelineLoopAddCondition")
        set_text(self.loop_add_condition_button, 'ui.inspector.add_mechanism_5232')
        self.loop_add_condition_button.setCursor(Qt.PointingHandCursor)
        self.loop_add_condition_button.clicked.connect(self._open_loop_mechanism_menu)
        loop_layout.addWidget(self.loop_add_condition_button)

        # Keep reorderable mechanism rows inside their own geometry island.
        # During drag the rows are temporarily detached from their QVBoxLayout and
        # positioned manually for animation; a dedicated outer frame prevents those
        # absolute coordinates from interfering with the rest of the inspector.
        self.loop_conditions_area = QFrame(self.loop_panel)
        self.loop_conditions_area.setObjectName("PipelineLoopConditionsArea")
        self.loop_conditions_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.loop_conditions_area_layout = QVBoxLayout(self.loop_conditions_area)
        self.loop_conditions_area_layout.setContentsMargins(6, 6, 6, 6)
        self.loop_conditions_area_layout.setSpacing(0)

        self.loop_conditions_host = QFrame(self.loop_conditions_area)
        self.loop_conditions_host.setObjectName("PipelineLoopConditionsHost")
        self.loop_conditions_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.loop_conditions_layout = QVBoxLayout(self.loop_conditions_host)
        self.loop_conditions_layout.setContentsMargins(0, 0, 0, 0)
        self.loop_conditions_layout.setSpacing(6)
        self.loop_conditions_area_layout.addWidget(self.loop_conditions_host)
        loop_layout.addWidget(self.loop_conditions_area)

        self.loop_conditions_empty = localize(QLabel(
            '',
            self.loop_conditions_host,
        ), 'ui.inspector.nothing_for_now_9ec6')
        self.loop_conditions_empty.setObjectName("PipelineLoopConditionsEmpty")
        self.loop_conditions_empty.setWordWrap(False)
        self.loop_conditions_empty.setAlignment(Qt.AlignCenter)
        self.loop_conditions_layout.addWidget(self.loop_conditions_empty)

        self._loop_condition_rows_by_type = {}
        self._selected_loop_condition_type = ""

        # Mechanism-specific settings live in a separate transparent island.
        # It expands with the inspector instead of forcing one fixed height, and it
        # stays present while switching rows so the whole panel does not jump.
        self.loop_detail_area = QFrame(self.loop_panel)
        self.loop_detail_area.setObjectName("PipelineLoopDetailArea")
        self.loop_detail_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.loop_detail_layout = QVBoxLayout(self.loop_detail_area)
        self.loop_detail_layout.setContentsMargins(0, 0, 0, 0)
        self.loop_detail_layout.setSpacing(8)

        self.loop_detail_title = QLabel("", self.loop_detail_area)
        self.loop_detail_title.setObjectName("PipelineInspectorFieldLabel")
        self.loop_detail_title.setWordWrap(True)
        self.loop_detail_layout.addWidget(self.loop_detail_title)

        self.loop_counter_spin = QSpinBox(self.loop_detail_area)
        self.loop_counter_spin.setObjectName("PipelineLoopSpin")
        self.loop_counter_spin.setRange(1, 9999)
        self.loop_counter_spin.valueChanged.connect(self._loop_counter_changed)
        self.loop_detail_layout.addWidget(self.loop_counter_spin)

        self.loop_prompt_edit = QPlainTextEdit(self.loop_detail_area)
        self.loop_prompt_edit.setObjectName("PipelineLoopTextEditor")
        set_text(self.loop_prompt_edit, 'ui.inspector.prompt_to_evaluate_the_result_931e', setter='setPlaceholderText')
        self.loop_prompt_edit.setMinimumHeight(96)
        self.loop_prompt_edit.setMaximumHeight(150)
        self.loop_prompt_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.loop_prompt_edit.textChanged.connect(self._loop_prompt_changed)
        self.loop_detail_layout.addWidget(self.loop_prompt_edit)

        self.loop_python_edit = form_field(QPlainTextEdit(self.loop_detail_area))
        self.loop_python_edit.setObjectName("PipelineLoopCodeEditor")
        bind_caption(self.loop_python_edit, "# Условие выполняется отдельно от Python-кода агента.\n"
            "# Верните настоящий bool:\n"
            "#   True  — выполнить Repeat\n"
            "#   False — перейти к Continue\n"
            "# Доступно runtime: result, error, iteration.\n"
            "# Любое другое значение или ошибка скрипта приостановит pipeline\n"
            "# и попросит пользователя выбрать Repeat / Continue.\n\n"
            "return False", setter='setPlaceholderText')
        self.loop_python_edit.setMinimumHeight(110)
        self.loop_python_edit.setMaximumHeight(180)
        self.loop_python_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.loop_python_edit.textChanged.connect(self._loop_python_changed)
        self.loop_detail_layout.addWidget(self.loop_python_edit)

        self.loop_user_edit = form_field(QPlainTextEdit(self.loop_detail_area))
        self.loop_user_edit.setObjectName("PipelineLoopTextEditor")
        set_text(self.loop_user_edit, 'ui.inspector.optional_message_or_question_before_choosing_8bb6', setter='setPlaceholderText')
        self.loop_user_edit.setMinimumHeight(96)
        self.loop_user_edit.setMaximumHeight(150)
        self.loop_user_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.loop_user_edit.textChanged.connect(self._loop_user_message_changed)
        self.loop_detail_layout.addWidget(self.loop_user_edit)
        loop_layout.addWidget(self.loop_detail_area)

        layout.addWidget(self.loop_panel)

        # Router control -------------------------------------------------------
        self.router_panel = QFrame(self)
        self.router_panel.setObjectName("PipelineRouterPanel")
        self.router_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        router_layout = QVBoxLayout(self.router_panel)
        router_layout.setContentsMargins(0, 0, 0, 0)
        router_layout.setSpacing(8)

        self.router_routes_label = localize(QLabel('', self.router_panel), 'ui.inspector.routes_and_descriptions_8539')
        self.router_routes_label.setObjectName("PipelineInspectorFieldLabel")
        router_layout.addWidget(self.router_routes_label)

        self.router_routes_host = QFrame(self.router_panel)
        self.router_routes_host.setObjectName("PipelineRouterRoutesHost")
        self.router_routes_layout = QVBoxLayout(self.router_routes_host)
        self.router_routes_layout.setContentsMargins(0, 0, 0, 0)
        self.router_routes_layout.setSpacing(6)
        router_layout.addWidget(self.router_routes_host)
        self._router_route_edits = {}

        self.router_mechanisms_label = localize(QLabel(
            '',
            self.router_panel,
        ), 'ui.inspector.routing_mechanisms_order_evaluation_priority_307a')
        self.router_mechanisms_label.setObjectName("PipelineInspectorFieldLabel")
        self.router_mechanisms_label.setWordWrap(True)
        router_layout.addWidget(self.router_mechanisms_label)

        self.router_add_mechanism_button = QToolButton(self.router_panel)
        self.router_add_mechanism_button.setObjectName("PipelineLoopAddCondition")
        set_text(self.router_add_mechanism_button, 'ui.inspector.add_mechanism_5232')
        self.router_add_mechanism_button.setCursor(Qt.PointingHandCursor)
        self.router_add_mechanism_button.clicked.connect(self._open_router_mechanism_menu)
        router_layout.addWidget(self.router_add_mechanism_button)

        self.router_mechanisms_area = QFrame(self.router_panel)
        self.router_mechanisms_area.setObjectName("PipelineLoopConditionsArea")
        self.router_mechanisms_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.router_mechanisms_area_layout = QVBoxLayout(self.router_mechanisms_area)
        self.router_mechanisms_area_layout.setContentsMargins(6, 6, 6, 6)
        self.router_mechanisms_area_layout.setSpacing(0)

        self.router_mechanisms_host = QFrame(self.router_mechanisms_area)
        self.router_mechanisms_host.setObjectName("PipelineLoopConditionsHost")
        self.router_mechanisms_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.router_mechanisms_layout = QVBoxLayout(self.router_mechanisms_host)
        self.router_mechanisms_layout.setContentsMargins(0, 0, 0, 0)
        self.router_mechanisms_layout.setSpacing(6)
        self.router_mechanisms_area_layout.addWidget(self.router_mechanisms_host)
        router_layout.addWidget(self.router_mechanisms_area)

        self._router_mechanism_rows_by_type = {}
        self._selected_router_mechanism_type = ""

        self.router_detail_area = QFrame(self.router_panel)
        self.router_detail_area.setObjectName("PipelineLoopDetailArea")
        self.router_detail_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.router_detail_layout = QVBoxLayout(self.router_detail_area)
        self.router_detail_layout.setContentsMargins(0, 0, 0, 0)
        self.router_detail_layout.setSpacing(8)

        self.router_detail_title = QLabel("", self.router_detail_area)
        self.router_detail_title.setObjectName("PipelineInspectorFieldLabel")
        self.router_detail_layout.addWidget(self.router_detail_title)

        self.router_route_combo = QComboBox(self.router_detail_area)
        self.router_route_combo.setObjectName("PipelineArrayTypeCombo")
        self.router_route_combo.currentIndexChanged.connect(self._router_route_choice_changed)
        self.router_detail_layout.addWidget(self.router_route_combo)

        self.router_prompt_edit = QPlainTextEdit(self.router_detail_area)
        self.router_prompt_edit.setObjectName("PipelineLoopTextEditor")
        self.router_prompt_edit.setMinimumHeight(96)
        self.router_prompt_edit.setMaximumHeight(150)
        self.router_prompt_edit.textChanged.connect(self._router_prompt_changed)
        self.router_detail_layout.addWidget(self.router_prompt_edit)

        self.router_python_edit = form_field(QPlainTextEdit(self.router_detail_area))
        self.router_python_edit.setObjectName("PipelineLoopCodeEditor")
        bind_caption(self.router_python_edit, "# Верните номер Exec-потока целым числом, начиная с нуля.\n"
            "# Доступно runtime: result, error.\n"
            "# Например: return 0\n"
            "# Нестандартное или недопустимое значение должно запросить выбор у пользователя.\n\n"
            "return 0", setter='setPlaceholderText')
        self.router_python_edit.setMinimumHeight(110)
        self.router_python_edit.setMaximumHeight(180)
        self.router_python_edit.textChanged.connect(self._router_python_changed)
        self.router_detail_layout.addWidget(self.router_python_edit)

        self.router_user_edit = form_field(QPlainTextEdit(self.router_detail_area))
        self.router_user_edit.setObjectName("PipelineLoopTextEditor")
        set_text(self.router_user_edit, 'ui.inspector.optional_message_before_choosing_a_route_35b0', setter='setPlaceholderText')
        self.router_user_edit.setMinimumHeight(96)
        self.router_user_edit.setMaximumHeight(150)
        self.router_user_edit.textChanged.connect(self._router_user_message_changed)
        self.router_detail_layout.addWidget(self.router_user_edit)
        router_layout.addWidget(self.router_detail_area)

        layout.addWidget(self.router_panel)

        # Python Code ---------------------------------------------------------
        self.python_code_panel = QFrame(self)
        self.python_code_panel.setObjectName("PipelinePythonCodePanel")
        self.python_code_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        python_layout = QVBoxLayout(self.python_code_panel)
        python_layout.setContentsMargins(0, 0, 0, 0)
        python_layout.setSpacing(8)

        self.python_inputs_label = localize(QLabel('', self.python_code_panel), 'ui.inspector.input_arguments_69a7')
        self.python_inputs_label.setObjectName("PipelineInspectorFieldLabel")
        python_layout.addWidget(self.python_inputs_label)

        self.python_inputs_host = QFrame(self.python_code_panel)
        self.python_inputs_host.setObjectName("PipelineDynamicArgsHost")
        self.python_inputs_layout = QVBoxLayout(self.python_inputs_host)
        self.python_inputs_layout.setContentsMargins(0, 0, 0, 0)
        self.python_inputs_layout.setSpacing(6)
        python_layout.addWidget(self.python_inputs_host)

        self.python_add_input = QToolButton(self.python_code_panel)
        self.python_add_input.setObjectName("PipelineLoopAddCondition")
        set_text(self.python_add_input, 'ui.inspector.add_input_9ddd')
        self.python_add_input.clicked.connect(lambda: self._python_add_argument("inputs"))
        python_layout.addWidget(self.python_add_input)

        self.python_outputs_label = localize(QLabel('', self.python_code_panel), 'ui.inspector.output_arguments_a5ce')
        self.python_outputs_label.setObjectName("PipelineInspectorFieldLabel")
        python_layout.addWidget(self.python_outputs_label)

        self.python_outputs_host = QFrame(self.python_code_panel)
        self.python_outputs_host.setObjectName("PipelineDynamicArgsHost")
        self.python_outputs_layout = QVBoxLayout(self.python_outputs_host)
        self.python_outputs_layout.setContentsMargins(0, 0, 0, 0)
        self.python_outputs_layout.setSpacing(6)
        python_layout.addWidget(self.python_outputs_host)

        self.python_add_output = QToolButton(self.python_code_panel)
        self.python_add_output.setObjectName("PipelineLoopAddCondition")
        set_text(self.python_add_output, 'ui.inspector.add_output_b371')
        self.python_add_output.clicked.connect(lambda: self._python_add_argument("outputs"))
        python_layout.addWidget(self.python_add_output)

        self.python_code_label = localize(QLabel('', self.python_code_panel), 'ui.inspector.python_code_9134')
        self.python_code_label.setObjectName("PipelineInspectorFieldLabel")
        python_layout.addWidget(self.python_code_label)

        self.python_code_edit = QPlainTextEdit(self.python_code_panel)
        self.python_code_edit.setObjectName("PipelineLoopCodeEditor")
        bind_caption(self.python_code_edit, "# Входные Arg доступны как обычные Python-переменные по их именам.\n"
            "# Выходные Arg задаются присваиванием переменным с именами выходов.\n"
            "# Пример: если вход text, а выход result:\n"
            "# result = text.upper()\n"
            "#\n"
            "# Во время выполнения этой ноды модель стоит на паузе и не участвует в скрипте.\n"
            "# Если скрипт завершится ошибкой, откроется Human In Loop с вариантами:\n"
            "# пропустить ноду / повторить скрипт / завершить граф / остановить генерацию целиком.", setter='setPlaceholderText')
        self.python_code_edit.setMinimumHeight(180)
        self.python_code_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.python_code_edit.textChanged.connect(self._python_code_changed)
        python_layout.addWidget(self.python_code_edit)
        layout.addWidget(self.python_code_panel)

        # Human In Loop -------------------------------------------------------
        self.human_loop_panel = QFrame(self)
        self.human_loop_panel.setObjectName("PipelineHumanLoopPanel")
        self.human_loop_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        human_layout = QVBoxLayout(self.human_loop_panel)
        human_layout.setContentsMargins(0, 0, 0, 0)
        human_layout.setSpacing(8)

        self.human_message_label = localize(QLabel('', self.human_loop_panel), 'ui.inspector.message_to_the_user_1594')
        self.human_message_label.setObjectName("PipelineInspectorFieldLabel")
        human_layout.addWidget(self.human_message_label)

        self.human_message_edit = form_field(QPlainTextEdit(self.human_loop_panel))
        self.human_message_edit.setObjectName("PipelineGraphMessageEditor")
        set_text(self.human_message_edit, 'ui.inspector.message_before_waiting_for_confirmation_4796', setter='setPlaceholderText')
        self.human_message_edit.setMinimumHeight(96)
        self.human_message_edit.setMaximumHeight(160)
        self.human_message_edit.textChanged.connect(self._human_message_changed)
        human_layout.addWidget(self.human_message_edit)

        self.human_arguments_toggle = localize(QCheckBox('', self.human_loop_panel), 'ui.inspector.request_arguments_788f')
        self.human_arguments_toggle.toggled.connect(self._human_arguments_toggled)
        human_layout.addWidget(self.human_arguments_toggle)

        self.human_outputs_host = QFrame(self.human_loop_panel)
        self.human_outputs_host.setObjectName("PipelineDynamicArgsHost")
        self.human_outputs_layout = QVBoxLayout(self.human_outputs_host)
        self.human_outputs_layout.setContentsMargins(0, 0, 0, 0)
        self.human_outputs_layout.setSpacing(6)
        human_layout.addWidget(self.human_outputs_host)

        self.human_add_output = QToolButton(self.human_loop_panel)
        self.human_add_output.setObjectName("PipelineLoopAddCondition")
        set_text(self.human_add_output, 'ui.inspector.add_argument_03d1')
        self.human_add_output.clicked.connect(self._human_add_output)
        human_layout.addWidget(self.human_add_output)

        self.human_fill_hint = localize(QLabel(
            '',
            self.human_loop_panel,
        ), 'ui.inspector.empty_fields_are_allowed_after_confirmation_321b')
        self.human_fill_hint.setObjectName("PipelineInspectorDescription")
        self.human_fill_hint.setWordWrap(True)
        human_layout.addWidget(self.human_fill_hint)
        layout.addWidget(self.human_loop_panel)

        # Unused inspector height always stays below the controls.
        self._bottom_spacer = QSpacerItem(
            0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding
        )
        layout.addItem(self._bottom_spacer)

        self._loading_loop = False
        self._loop_drag_state = None
        self._loop_drag_animation_group = None
        self._loading_router = False
        self._router_drag_state = None
        self._router_drag_animation_group = None

        apply_style(self, '\n            #PipelineInspector {\n                background: @background.window@;\n                border-left: 1px solid @control.hover@;\n            }\n            #PipelineInspectorScrollArea,\n            #PipelineInspectorScrollArea > QWidget > QWidget,\n            #PipelineInspectorScrollContent {\n                background: transparent;\n                border: none;\n            }\n            #PipelineInspectorTitle {\n                background: transparent;\n                color: @text.primary@;\n                font-size: 16px;\n                font-weight: 700;\n            }\n            #PipelineInspectorDescription,\n            #PipelineInspectorFieldLabel {\n                background: transparent;\n                color: @text.secondary@;\n            }\n            #PipelineInspectorFieldLabel {\n                color: @text.primary@;\n                font-weight: 600;\n                padding-top: 6px;\n            }\n            #PipelineRouterCycleWarning {\n                background: @background.panel@;\n                color: #fbbf24;\n                border: 1px solid #6b4520;\n                border-radius: 6px;\n                padding: 8px;\n            }\n            #PipelineRouterRoutesHost {\n                background: transparent;\n                border: none;\n            }\n            #PipelinePromptEditor,\n            #PipelineScalarEditor,\n            #PipelineArrayEditor,\n            #PipelineArrayTypeCombo,\n            #PipelineKnowledgeBaseList,\n            #PipelineGraphMessageEditor,\n            #PipelineLoopTextEditor,\n            #PipelineLoopCodeEditor,\n            #PipelineLoopSpin {\n                background: @background.window@;\n                color: @text.primary@;\n                border: 1px solid @border.subtle@;\n                border-radius: 6px;\n                padding: 8px;\n                selection-background-color: @control.selected@;\n            }\n            #PipelineKnowledgeBaseList::item {\n                padding: 5px 4px;\n            }\n            #PipelineLoopAddCondition {\n                background: @background.window@;\n                color: @text.primary@;\n                border: 1px solid @border.subtle@;\n                border-radius: 6px;\n                padding: 7px 10px;\n                text-align: left;\n            }\n            #PipelineLoopAddCondition:hover {\n                border-color: @border.subtle@;\n            }\n            #PipelineLoopConditionsArea {\n                background: @background.window@;\n                border: 1px solid @border.subtle@;\n                border-radius: 8px;\n            }\n            #PipelineLoopConditionsHost {\n                background: transparent;\n                border: none;\n            }\n            #PipelineLoopConditionsEmpty {\n                color: @text.disabled@;\n                background: transparent;\n                padding: 7px 4px;\n            }\n            QFrame#PipelineLoopConditionRow {\n                background: @background.window@;\n                border: 1px solid @border.subtle@;\n                border-radius: 6px;\n            }\n            QFrame#PipelineLoopConditionRow[selected="true"] {\n                background: @background.panel@;\n                border-color: @text.disabled@;\n            }\n            QLabel#PipelineLoopConditionDragHandle {\n                color: @text.disabled@;\n                background: transparent;\n                border: none;\n                font-size: 16px;\n                font-weight: 700;\n            }\n            QLabel#PipelineLoopConditionTitle {\n                color: @text.primary@;\n                background: transparent;\n                font-weight: 600;\n            }\n            QToolButton#PipelineLoopConditionRemove {\n                color: @text.secondary@;\n                background: transparent;\n                border: none;\n                border-radius: 4px;\n                padding: 0;\n            }\n            QToolButton#PipelineLoopConditionRemove:hover {\n                color: @text.primary@;\n                background: @control.hover@;\n            }\n            #PipelineLoopPanel {\n                background: transparent;\n                border: none;\n            }\n            #PipelineLoopDetailArea {\n                background: transparent;\n                border: none;\n            }\n            #PipelineKnowledgeBaseList::item:selected {\n                background: @background.surface@;\n            }\n            #PipelineBinaryMenuButton {\n                background: @background.window@;\n                color: @text.primary@;\n                border: 1px solid @border.subtle@;\n                border-radius: 6px;\n                padding: 7px 10px;\n                text-align: left;\n            }\n            #PipelineBinaryMenuButton:hover {\n                border-color: @border.subtle@;\n            }\n            #PipelineBinaryArrayHost {\n                background: transparent;\n                border: none;\n            }\n            QFrame#PipelineBinaryChip {\n                background-color: @background.surface@;\n                border: 1px solid @accent.primary@;\n                border-radius: 8px;\n            }\n            QFrame#PipelineBinaryChip QLabel {\n                color: @text.primary@;\n                font-size: 11px;\n                font-weight: 700;\n                background: transparent;\n                border: none;\n            }\n            QFrame#PipelineBinaryChip QToolButton {\n                color: #c7d2fe;\n                background: transparent;\n                border: none;\n                padding: 0;\n            }\n            #PipelineBinaryArrayAdd {\n                color: @text.primary@;\n                background: @background.surface@;\n                border: 1px solid @border.subtle@;\n                border-radius: 8px;\n                font-size: 17px;\n                font-weight: 600;\n            }\n            #PipelineBinaryArrayAdd:hover {\n                border-color: @accent.primary@;\n                color: @text.primary@;\n            }\n        ')
        from .execution_settings import ExecutionSettings
        self.execution_settings = ExecutionSettings(self)
        self._main_layout.insertWidget(3, self.execution_settings)
        self.hide()

    def set_node(self, node: Optional[PipelineNodeItem]):
        self._node = node
        self.execution_settings.set_node(node)
        if node is None:
            self.hide()
            return

        bind_caption(self.title, node_caption(node.spec))
        bind_caption(self.description, node_caption(node.spec, 'description'))

        is_prompt = node.value_type == "prompt"
        is_string_value = node.value_type == "string"
        is_binary_value = node.value_type == "binary"
        is_scalar_value = node.value_type in {"integer", "float"}
        is_array_value = node.value_type == "array"
        inspector_type = str(node.spec.metadata.get("inspector_type") or "")
        self.pipeline_interface_button.setVisible(node.spec.type_id in {"graph.end_subgraph", "pipeline.call"})
        bind_caption(self.pipeline_interface_button, tr('ui.caption.eec2ca2dcfdd') if node.spec.type_id == "pipeline.call" else tr('ui.inspector.pipeline_arguments_9b70'))
        is_knowledge_search = inspector_type == "knowledge_search"
        is_graph_message = inspector_type == "graph_message"
        is_graph_loop = inspector_type == "graph_loop"
        is_graph_router = inspector_type == "graph_router"
        is_python_code = inspector_type == "python_code"
        is_human_in_loop = inspector_type == "human_in_loop"

        has_value_editor = (
            is_prompt
            or is_string_value
            or is_binary_value
            or is_scalar_value
            or is_array_value
        )
        self.prompt_label.setVisible(has_value_editor)
        self.prompt_edit.setVisible(is_prompt or is_string_value)
        self.binary_button.setVisible(is_binary_value)
        self.scalar_edit.setVisible(is_scalar_value)
        self.array_type_label.setVisible(is_array_value)
        self.array_type_combo.setVisible(is_array_value)
        self.array_edit.setVisible(is_array_value)
        self.binary_array_host.setVisible(is_array_value and str(node.settings.get("item_type") or "string") == "binary")
        self.knowledge_db_label.setVisible(is_knowledge_search)
        self.knowledge_db_list.setVisible(is_knowledge_search)
        self.graph_message_label.setVisible(is_graph_message)
        self.graph_message_edit.setVisible(is_graph_message)
        self.loop_panel.setVisible(is_graph_loop)
        self.router_panel.setVisible(is_graph_router)
        self.python_code_panel.setVisible(is_python_code)
        self.human_loop_panel.setVisible(is_human_in_loop)

        router_cycle = False
        if is_graph_router:
            view = node._pipeline_view()
            checker = getattr(view, "node_participates_in_exec_cycle", None) if view is not None else None
            if callable(checker):
                try:
                    router_cycle = bool(checker(node))
                except RuntimeError:
                    router_cycle = False
        self.router_cycle_warning.setVisible(is_graph_router and router_cycle)

        # Every inspector, including Loop, stays packed at the top.  The bottom
        # spacer owns all unused height; mechanism editors only take the space
        # their active control actually needs.
        loop_index = self._main_layout.indexOf(self.loop_panel)
        self._main_layout.setStretch(loop_index, 0)
        self._bottom_spacer.changeSize(
            0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding
        )
        self._main_layout.invalidate()

        if is_knowledge_search:
            self._populate_knowledge_databases(node)
        if is_graph_message:
            bind_caption(self.graph_message_label, metadata_caption(node.spec, 'message_label', 'Message'))
            bind_caption(self.graph_message_edit, metadata_caption(node.spec, 'message_placeholder'), setter='setPlaceholderText')
            self.graph_message_edit.blockSignals(True)
            self.graph_message_edit.setPlainText(str(node.settings.get("message") or ""))
            self.graph_message_edit.blockSignals(False)
        if is_graph_loop:
            self._populate_loop_controls(node)
        if is_graph_router:
            self._populate_router_controls(node)
        if is_python_code:
            self._populate_python_code_controls(node)
        if is_human_in_loop:
            self._populate_human_in_loop_controls(node)

        if is_prompt:
            set_text(self.prompt_label, 'ui.inspector.prompt_5c39')
            set_text(self.prompt_edit, 'ui.inspector.additional_prompt_text_60b6', setter='setPlaceholderText')
            self.prompt_edit.blockSignals(True)
            self.prompt_edit.setPlainText(str(node.settings.get("text") or ""))
            self.prompt_edit.blockSignals(False)
        elif is_string_value:
            set_text(self.prompt_label, 'ui.inspector.string_b2ef')
            set_text(self.prompt_edit, 'ui.inspector.string_value_ab39', setter='setPlaceholderText')
            self.prompt_edit.blockSignals(True)
            self.prompt_edit.setPlainText(str(node.settings.get("value") or ""))
            self.prompt_edit.blockSignals(False)
        elif is_binary_value:
            set_text(self.prompt_label, 'ui.inspector.binary_ee1c')
            self._sync_binary_button_text(node.settings.get("value"))
        elif is_scalar_value:
            bind_caption(self.prompt_label, node_caption(node.spec))
            value = node.settings.get("value")
            self.scalar_edit.blockSignals(True)
            bind_caption(self.scalar_edit, "" if value is None else str(value))
            self.scalar_edit.blockSignals(False)
        elif is_array_value:
            set_text(self.prompt_label, 'ui.inspector.values_53b0')
            item_type = str(node.settings.get("item_type") or "string")
            combo_index = self.array_type_combo.findData(item_type)
            if combo_index < 0:
                combo_index = 0
            self.array_type_combo.blockSignals(True)
            self.array_type_combo.setCurrentIndex(combo_index)
            self.array_type_combo.blockSignals(False)

            self.array_edit.blockSignals(True)
            bind_caption(self.array_edit, str(node.settings.get("literal_text") or ""))
            self.array_edit.blockSignals(False)
            self._sync_array_editor_mode()

        self._sync_content_editor_enabled(node)

        # Port/schema summaries are intentionally not duplicated in the inspector.

        self.show()

    def _configure_pipeline_interface(self):
        from .interface_dialog import configure_interface
        configure_interface(self)

    def _graph_message_changed(self):
        node = self._node
        if node is None:
            return
        if str(node.spec.metadata.get("inspector_type") or "") != "graph_message":
            return
        node.settings["message"] = self.graph_message_edit.toPlainText()
        node.update()
        self.modified.emit()

    @staticmethod
    def _loop_definition_map():
        return {str(item.get("type") or ""): item for item in LOOP_CONDITION_DEFINITIONS}

    def _loop_enabled_types(self) -> List[str]:
        node = self._node
        if node is None:
            return []
        return [
            str(item.get("type") or "")
            for item in normalize_loop_conditions(node.settings.get("conditions"))
            if bool(item.get("enabled"))
        ]

    def _clear_loop_condition_rows(self):
        # A selection change can rebuild the inspector while a reorder animation
        # is still alive. Stop it first so no animation keeps touching rows that
        # are about to be deleted.
        if self._loop_drag_animation_group is not None:
            self._loop_drag_animation_group.stop()
            self._loop_drag_animation_group.deleteLater()
            self._loop_drag_animation_group = None
        self._loop_drag_state = None

        self._loop_condition_rows_by_type = {}
        while self.loop_conditions_layout.count():
            item = self.loop_conditions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _resize_loop_conditions_host(self, row_count: int):
        row_count = max(0, int(row_count))
        if row_count <= 0:
            host_height = 34
        else:
            spacing = max(0, int(self.loop_conditions_layout.spacing()))
            host_height = row_count * 40 + max(0, row_count - 1) * spacing

        self.loop_conditions_host.setFixedHeight(host_height)
        margins = self.loop_conditions_area_layout.contentsMargins()
        area_height = host_height + margins.top() + margins.bottom()
        self.loop_conditions_area.setFixedHeight(area_height)
        self.loop_conditions_host.updateGeometry()
        self.loop_conditions_area.updateGeometry()

    def _populate_loop_controls(self, node: PipelineNodeItem):
        self._loading_loop = True
        try:
            try:
                max_repeats = max(1, int(node.settings.get("max_repeats", 10) or 10))
            except (TypeError, ValueError):
                max_repeats = 10
            node.settings["max_repeats"] = max_repeats
            conditions = normalize_loop_conditions(node.settings.get("conditions"))
            node.settings["conditions"] = conditions

            self.loop_max_repeats.blockSignals(True)
            self.loop_max_repeats.setValue(max_repeats)
            self.loop_max_repeats.blockSignals(False)

            previous_type = str(self._selected_loop_condition_type or "")
            enabled = [item for item in conditions if bool(item.get("enabled"))]
            enabled_types = [str(item.get("type") or "") for item in enabled]
            if previous_type not in enabled_types:
                previous_type = enabled_types[0] if enabled_types else ""
            self._selected_loop_condition_type = previous_type

            self._clear_loop_condition_rows()
            definitions = self._loop_definition_map()
            if not enabled:
                self.loop_conditions_empty = localize(QLabel(
                    '',
                    self.loop_conditions_host,
                ), 'ui.inspector.nothing_for_now_9ec6')
                self.loop_conditions_empty.setObjectName("PipelineLoopConditionsEmpty")
                self.loop_conditions_empty.setWordWrap(False)
                self.loop_conditions_empty.setAlignment(Qt.AlignCenter)
                self.loop_conditions_layout.addWidget(self.loop_conditions_empty)
            else:
                self.loop_conditions_empty = None
                for condition in enabled:
                    condition_type = str(condition.get("type") or "")
                    definition = definitions.get(condition_type, {})
                    row = LoopConditionRow(
                        condition_type,
                        mechanism_text(definition, 'title'),
                        self.loop_conditions_host,
                    )
                    row.selected.connect(self._loop_select_condition)
                    row.remove_requested.connect(self._loop_remove_condition)
                    row.drag_moved.connect(self._loop_condition_drag_moved)
                    row.drag_finished.connect(self._loop_condition_drag_finished)
                    row.set_selected(condition_type == previous_type)
                    self.loop_conditions_layout.addWidget(row)
                    self._loop_condition_rows_by_type[condition_type] = row
            self._resize_loop_conditions_host(len(enabled))
        finally:
            self._loading_loop = False

        node.sync_loop_argument_port()
        self._loop_condition_selected()

    def _open_loop_mechanism_menu(self):
        node = self._node
        if node is None or str(node.spec.metadata.get("inspector_type") or "") != "graph_loop":
            return
        selected = set(self._loop_enabled_types())
        menu = QMenu(self.loop_add_condition_button)
        menu.setToolTipsVisible(True)
        actions = {}
        for definition in LOOP_CONDITION_DEFINITIONS:
            condition_type = str(definition.get("type") or "")
            action = add_caption_action(menu, 'addAction', mechanism_text(definition, 'title'))
            description = mechanism_text(definition, 'description')
            if description:
                bind_caption(action, description, setter='setToolTip')
            if condition_type in selected:
                action.setEnabled(False)
            actions[action] = condition_type
        chosen = menu.exec(
            self.loop_add_condition_button.mapToGlobal(
                self.loop_add_condition_button.rect().bottomLeft()
            )
        )
        if chosen is not None and chosen.isEnabled():
            condition_type = actions.get(chosen, "")
            if condition_type:
                self._loop_add_condition(condition_type)

    def _loop_add_condition(self, condition_type: str):
        node = self._node
        condition_type = str(condition_type or "")
        if node is None or not condition_type:
            return
        conditions = normalize_loop_conditions(node.settings.get("conditions"))
        enabled = []
        disabled = []
        found = False
        for condition in conditions:
            item = dict(condition)
            current_type = str(item.get("type") or "")
            if current_type == condition_type:
                item["enabled"] = True
                found = True
            if bool(item.get("enabled")):
                enabled.append(item)
            else:
                disabled.append(item)
        if not found:
            return
        node.settings["conditions"] = enabled + disabled
        self._selected_loop_condition_type = condition_type
        node.sync_loop_argument_port()
        node.update()
        self._populate_loop_controls(node)
        self._refresh_loop_schema(node)
        self.modified.emit()

    def _loop_remove_condition(self, condition_type: str):
        node = self._node
        condition_type = str(condition_type or "")
        if node is None or not condition_type:
            return
        conditions = normalize_loop_conditions(node.settings.get("conditions"))
        remaining_enabled = []
        disabled = []
        for condition in conditions:
            item = dict(condition)
            current_type = str(item.get("type") or "")
            if current_type == condition_type:
                item["enabled"] = False
            if bool(item.get("enabled")):
                remaining_enabled.append(item)
            else:
                disabled.append(item)
        node.settings["conditions"] = remaining_enabled + disabled
        if self._selected_loop_condition_type == condition_type:
            self._selected_loop_condition_type = (
                str(remaining_enabled[0].get("type") or "")
                if remaining_enabled else ""
            )
        node.sync_loop_argument_port()
        node.update()
        self._populate_loop_controls(node)
        self._refresh_loop_schema(node)
        self.modified.emit()

    def _loop_select_condition(self, condition_type: str):
        condition_type = str(condition_type or "")
        self._selected_loop_condition_type = condition_type
        for row_type, row in self._loop_condition_rows_by_type.items():
            row.set_selected(row_type == condition_type)
        self._loop_condition_selected()

    def _loop_condition_row_order(self) -> List[str]:
        result = []
        for index in range(self.loop_conditions_layout.count()):
            widget = self.loop_conditions_layout.itemAt(index).widget()
            if isinstance(widget, LoopConditionRow):
                result.append(widget.condition_type)
        return result

    def _loop_store_row_order(self, *, emit_modified: bool = True):
        if self._loading_loop:
            return
        node = self._node
        if node is None or str(node.spec.metadata.get("inspector_type") or "") != "graph_loop":
            return
        ordered_types = self._loop_condition_row_order()
        conditions = normalize_loop_conditions(node.settings.get("conditions"))
        by_type = {str(item.get("type") or ""): dict(item) for item in conditions}
        selected = []
        for condition_type in ordered_types:
            item = by_type.pop(condition_type, None)
            if item is None:
                continue
            item["enabled"] = True
            selected.append(item)
        disabled = []
        for item in conditions:
            condition_type = str(item.get("type") or "")
            leftover = by_type.pop(condition_type, None)
            if leftover is None:
                continue
            leftover["enabled"] = False
            disabled.append(leftover)
        node.settings["conditions"] = selected + disabled
        node.sync_loop_argument_port()
        node.update()
        self._refresh_loop_schema(node)
        if emit_modified:
            self.modified.emit()

    def _loop_condition_rows(self):
        if self._loop_drag_state is not None:
            return list(self._loop_drag_state.get("order") or [])
        rows = [
            self.loop_conditions_layout.itemAt(index).widget()
            for index in range(self.loop_conditions_layout.count())
        ]
        return [item for item in rows if isinstance(item, LoopConditionRow)]

    def _loop_begin_condition_drag(self, row: LoopConditionRow, global_y: int) -> bool:
        rows = [
            self.loop_conditions_layout.itemAt(index).widget()
            for index in range(self.loop_conditions_layout.count())
        ]
        rows = [item for item in rows if isinstance(item, LoopConditionRow)]
        if len(rows) < 2 or row not in rows:
            return False

        if self._loop_drag_animation_group is not None:
            self._loop_drag_animation_group.stop()
            self._loop_drag_animation_group.deleteLater()
            self._loop_drag_animation_group = None

        # Detach rows from QVBoxLayout for the duration of the drag. A layout
        # owns widget geometry, so animating rows while they remain inside it
        # makes them snap/teleport back on every relayout. The host keeps the
        # same fixed height, and the rows keep the host as their QWidget parent.
        geometries = {candidate: candidate.geometry() for candidate in rows}
        for candidate in rows:
            self.loop_conditions_layout.removeWidget(candidate)
        for candidate in rows:
            candidate.setGeometry(geometries[candidate])
            candidate.show()

        row_top_global = row.mapToGlobal(QPoint(0, 0)).y()
        self._loop_drag_state = {
            "row": row,
            "order": list(rows),
            "grab_offset": int(global_y) - int(row_top_global),
        }
        row.setProperty("dragging", True)
        row.style().unpolish(row)
        row.style().polish(row)
        row.raise_()
        return True

    def _loop_animate_rows_to_slots(self, order, *, skip=None, duration: int = 115):
        spacing = max(0, int(self.loop_conditions_layout.spacing()))
        step = 40 + spacing

        # Individual short animations are intentionally kept as children of a
        # parallel group. Replacing the group stops any previous transition at
        # its current visual position, so rapidly crossing several slots still
        # feels continuous rather than queuing animations.
        if self._loop_drag_animation_group is not None:
            self._loop_drag_animation_group.stop()
            self._loop_drag_animation_group.deleteLater()

        group = QParallelAnimationGroup(self)
        animated = False
        for index, candidate in enumerate(order):
            if candidate is skip:
                continue
            target = QPoint(candidate.x(), index * step)
            if candidate.pos() == target:
                continue
            animation = QPropertyAnimation(candidate, b"pos", group)
            animation.setDuration(max(1, int(duration)))
            animation.setStartValue(candidate.pos())
            animation.setEndValue(target)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)
            animated = True

        self._loop_drag_animation_group = group
        if animated:
            group.start()
        else:
            group.deleteLater()
            self._loop_drag_animation_group = None

    def _loop_condition_drag_moved(self, condition_type: str, global_y: int):
        row = self._loop_condition_rows_by_type.get(str(condition_type or ""))
        if row is None:
            return

        state = self._loop_drag_state
        if state is None:
            if not self._loop_begin_condition_drag(row, global_y):
                return
            state = self._loop_drag_state
        if state is None or state.get("row") is not row:
            return

        order = list(state.get("order") or [])
        if len(order) < 2:
            return

        host_global = self.loop_conditions_host.mapToGlobal(
            self.loop_conditions_host.rect().topLeft()
        )
        cursor_y = int(global_y) - int(host_global.y())
        grab_offset = int(state.get("grab_offset", row.height() // 2))
        max_y = max(0, self.loop_conditions_host.height() - row.height())
        dragged_y = max(0, min(cursor_y - grab_offset, max_y))
        row.move(row.x(), dragged_y)
        row.raise_()

        spacing = max(0, int(self.loop_conditions_layout.spacing()))
        step = 40 + spacing
        others = [candidate for candidate in order if candidate is not row]
        target_index = int(round(float(dragged_y) / float(max(1, step))))
        target_index = max(0, min(target_index, len(others)))
        new_order = list(others)
        new_order.insert(target_index, row)

        if new_order != order:
            state["order"] = new_order
            self._loop_animate_rows_to_slots(new_order, skip=row)

    def _loop_finalize_condition_drag(self):
        state = self._loop_drag_state
        if state is None:
            return
        order = list(state.get("order") or [])
        row = state.get("row")

        if self._loop_drag_animation_group is not None:
            self._loop_drag_animation_group.stop()
            self._loop_drag_animation_group.deleteLater()
            self._loop_drag_animation_group = None

        # Reattach only after the dragged row has eased into its final slot.
        # This prevents the last few pixels from snapping on mouse release.
        spacing = max(0, int(self.loop_conditions_layout.spacing()))
        step = 40 + spacing
        group = QParallelAnimationGroup(self)
        for index, candidate in enumerate(order):
            target = QPoint(candidate.x(), index * step)
            if candidate.pos() == target:
                continue
            animation = QPropertyAnimation(candidate, b"pos", group)
            animation.setDuration(105)
            animation.setStartValue(candidate.pos())
            animation.setEndValue(target)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)

        def finish():
            current = self._loop_drag_state
            if current is None:
                return
            current_order = list(current.get("order") or [])
            dragged = current.get("row")
            if isinstance(dragged, LoopConditionRow):
                dragged.setProperty("dragging", False)
                dragged.style().unpolish(dragged)
                dragged.style().polish(dragged)

            for candidate in current_order:
                self.loop_conditions_layout.addWidget(candidate)
            self.loop_conditions_layout.activate()
            self._loop_drag_state = None
            self._loop_drag_animation_group = None
            self._loop_store_row_order()

        if group.animationCount() == 0:
            group.deleteLater()
            finish()
            return

        self._loop_drag_animation_group = group
        group.finished.connect(finish)
        group.start()

    def _loop_condition_drag_finished(self):
        self._loop_finalize_condition_drag()

    def _selected_loop_type(self) -> str:
        return str(self._selected_loop_condition_type or "")

    def _selected_loop_condition(self):
        node = self._node
        selected_type = self._selected_loop_type()
        if node is None or not selected_type:
            return None
        for item in normalize_loop_conditions(node.settings.get("conditions")):
            if str(item.get("type") or "") == selected_type and bool(item.get("enabled")):
                return item
        return None

    def _loop_condition_selected(self):
        if self._loading_loop:
            return
        node = self._node
        if node is None or str(node.spec.metadata.get("inspector_type") or "") != "graph_loop":
            return
        condition_type = self._selected_loop_type()
        definitions = self._loop_definition_map()
        definition = definitions.get(condition_type, {})
        title = mechanism_text(definition, 'title')
        bind_caption(self.loop_detail_title, tr('ui.caption.73df6719df07', v0=title) if condition_type else "")
        self.loop_detail_area.setVisible(bool(condition_type))
        self.loop_detail_title.setVisible(bool(condition_type))

        self.loop_counter_spin.hide()
        self.loop_prompt_edit.hide()
        self.loop_python_edit.hide()
        self.loop_user_edit.hide()

        condition = self._selected_loop_condition() or {}
        if condition_type == "counter":
            self.loop_counter_spin.blockSignals(True)
            self.loop_counter_spin.setValue(max(1, int(condition.get("repeats", 1) or 1)))
            self.loop_counter_spin.blockSignals(False)
            self.loop_counter_spin.show()
        elif condition_type in {"subagent", "agent"}:
            self.loop_prompt_edit.blockSignals(True)
            self.loop_prompt_edit.setPlainText(str(condition.get("prompt") or ""))
            bind_caption(self.loop_prompt_edit, tr('ui.caption.79cd3d45da83')
                if condition_type == "subagent"
                else tr('ui.caption.abdfbed3bb2b'), setter='setPlaceholderText')
            self.loop_prompt_edit.blockSignals(False)
            self.loop_prompt_edit.show()
        elif condition_type == "python":
            self.loop_python_edit.blockSignals(True)
            self.loop_python_edit.setPlainText(str(condition.get("code") or ""))
            self.loop_python_edit.blockSignals(False)
            self.loop_python_edit.show()
        elif condition_type == "user":
            self.loop_user_edit.blockSignals(True)
            self.loop_user_edit.setPlainText(str(condition.get("message") or ""))
            self.loop_user_edit.blockSignals(False)
            self.loop_user_edit.show()

        self.loop_detail_layout.invalidate()
        self.loop_detail_area.updateGeometry()
        self.loop_panel.updateGeometry()
        self._main_layout.invalidate()

    def _update_loop_condition_field(self, field: str, value):
        if self._loading_loop:
            return
        node = self._node
        selected_type = self._selected_loop_type()
        if node is None or not selected_type:
            return
        conditions = normalize_loop_conditions(node.settings.get("conditions"))
        changed = False
        for item in conditions:
            if str(item.get("type") or "") == selected_type and bool(item.get("enabled")):
                if item.get(field) != value:
                    item[field] = value
                    changed = True
                break
        if changed:
            node.settings["conditions"] = conditions
            node.update()
            self.modified.emit()

    def _loop_max_repeats_changed(self, value: int):
        if self._loading_loop:
            return
        node = self._node
        if node is None or str(node.spec.metadata.get("inspector_type") or "") != "graph_loop":
            return
        node.settings["max_repeats"] = max(1, int(value))
        self._refresh_loop_schema(node)
        self.modified.emit()

    def _loop_counter_changed(self, value: int):
        if self._selected_loop_type() == "counter":
            self._update_loop_condition_field("repeats", max(1, int(value)))

    def _loop_prompt_changed(self):
        if self._selected_loop_type() in {"subagent", "agent"}:
            self._update_loop_condition_field("prompt", self.loop_prompt_edit.toPlainText())

    def _loop_python_changed(self):
        if self._selected_loop_type() == "python":
            self._update_loop_condition_field("code", self.loop_python_edit.toPlainText())

    def _loop_user_message_changed(self):
        if self._selected_loop_type() == "user":
            self._update_loop_condition_field("message", self.loop_user_edit.toPlainText())

    def _refresh_loop_schema(self, node: PipelineNodeItem):
        # Kept as a compatibility hook for existing call sites. The inspector no
        # longer duplicates information that is already visible on the node.
        return



    # Router ---------------------------------------------------------------
    @staticmethod
    def _router_definition_map():
        return {str(item.get("type") or ""): item for item in ROUTER_MECHANISM_DEFINITIONS}

    def _router_enabled_types(self) -> List[str]:
        node = self._node
        if node is None:
            return []
        return [
            str(item.get("type") or "")
            for item in normalize_router_mechanisms(node.settings.get("mechanisms"))
            if bool(item.get("enabled"))
        ]

    def _clear_router_route_rows(self):
        self._router_route_edits = {}
        while self.router_routes_layout.count():
            item = self.router_routes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _populate_router_routes(self, node: PipelineNodeItem):
        node.normalize_router_ports()
        count = max(1, node.router_output_count())
        descriptions = node.settings.get("route_descriptions")
        if not isinstance(descriptions, dict):
            descriptions = {}
        descriptions = {
            str(index): str(descriptions.get(str(index), "") or "")
            for index in range(count)
        }
        node.settings["route_descriptions"] = descriptions

        self._clear_router_route_rows()
        for index in range(count):
            row = QFrame(self.router_routes_host)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            label = localize(QLabel('', row), 'ui.inspector.exec_v0_6037', v0=index)
            label.setMinimumWidth(54)
            row_layout.addWidget(label)
            edit = QLineEdit(row)
            edit.setObjectName("PipelineScalarEditor")
            set_text(edit, 'ui.inspector.description_for_the_user_d618', setter='setPlaceholderText')
            edit.setProperty("router_route_index", index)
            bind_caption(edit, descriptions.get(str(index), ""))
            edit.textChanged.connect(self._router_route_description_changed)
            row_layout.addWidget(edit, 1)
            self.router_routes_layout.addWidget(row)
            self._router_route_edits[index] = edit

    def _router_route_description_changed(self, text: str):
        if self._loading_router:
            return
        node = self._node
        if node is None or not node.is_graph_router:
            return
        edit = self.sender()
        try:
            index = int(edit.property("router_route_index"))
        except (TypeError, ValueError, AttributeError):
            return
        descriptions = node.settings.get("route_descriptions")
        if not isinstance(descriptions, dict):
            descriptions = {}
        descriptions = dict(descriptions)
        descriptions[str(index)] = str(text or "")
        node.settings["route_descriptions"] = descriptions
        self._populate_router_route_combo(node, preserve=True)
        self.modified.emit()

    def _clear_router_mechanism_rows(self):
        if self._router_drag_animation_group is not None:
            self._router_drag_animation_group.stop()
            self._router_drag_animation_group.deleteLater()
            self._router_drag_animation_group = None
        self._router_drag_state = None
        self._router_mechanism_rows_by_type = {}
        while self.router_mechanisms_layout.count():
            item = self.router_mechanisms_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _resize_router_mechanisms_host(self, row_count: int):
        row_count = max(0, int(row_count))
        if row_count <= 0:
            host_height = 34
        else:
            spacing = max(0, int(self.router_mechanisms_layout.spacing()))
            host_height = row_count * 40 + max(0, row_count - 1) * spacing
        self.router_mechanisms_host.setFixedHeight(host_height)
        margins = self.router_mechanisms_area_layout.contentsMargins()
        self.router_mechanisms_area.setFixedHeight(host_height + margins.top() + margins.bottom())
        self.router_mechanisms_host.updateGeometry()
        self.router_mechanisms_area.updateGeometry()

    def _populate_router_controls(self, node: PipelineNodeItem):
        self._loading_router = True
        try:
            node.normalize_router_ports()
            mechanisms = normalize_router_mechanisms(node.settings.get("mechanisms"))
            node.settings["mechanisms"] = mechanisms
            self._populate_router_routes(node)

            previous_type = str(self._selected_router_mechanism_type or "")
            enabled = [item for item in mechanisms if bool(item.get("enabled"))]
            enabled_types = [str(item.get("type") or "") for item in enabled]
            if previous_type not in enabled_types:
                previous_type = enabled_types[0] if enabled_types else ""
            self._selected_router_mechanism_type = previous_type

            self._clear_router_mechanism_rows()
            definitions = self._router_definition_map()
            if not enabled:
                empty = localize(QLabel('', self.router_mechanisms_host), 'ui.inspector.nothing_for_now_9ec6')
                empty.setObjectName("PipelineLoopConditionsEmpty")
                empty.setAlignment(Qt.AlignCenter)
                self.router_mechanisms_layout.addWidget(empty)
            else:
                for mechanism in enabled:
                    mechanism_type = str(mechanism.get("type") or "")
                    definition = definitions.get(mechanism_type, {})
                    row = LoopConditionRow(
                        mechanism_type,
                        mechanism_text(definition, 'title'),
                        self.router_mechanisms_host,
                    )
                    row.selected.connect(self._router_select_mechanism)
                    row.remove_requested.connect(self._router_remove_mechanism)
                    row.drag_moved.connect(self._router_mechanism_drag_moved)
                    row.drag_finished.connect(self._router_mechanism_drag_finished)
                    row.set_selected(mechanism_type == previous_type)
                    self.router_mechanisms_layout.addWidget(row)
                    self._router_mechanism_rows_by_type[mechanism_type] = row
            self._resize_router_mechanisms_host(len(enabled))
        finally:
            self._loading_router = False

        node.sync_router_argument_port()
        self._router_mechanism_selected()

    def _open_router_mechanism_menu(self):
        node = self._node
        if node is None or not node.is_graph_router:
            return
        selected = set(self._router_enabled_types())
        menu = QMenu(self.router_add_mechanism_button)
        menu.setToolTipsVisible(True)
        actions = {}
        for definition in ROUTER_MECHANISM_DEFINITIONS:
            mechanism_type = str(definition.get("type") or "")
            action = add_caption_action(menu, 'addAction', mechanism_text(definition, 'title'))
            description = mechanism_text(definition, 'description')
            if description:
                bind_caption(action, description, setter='setToolTip')
            if mechanism_type in selected:
                action.setEnabled(False)
            actions[action] = mechanism_type
        chosen = menu.exec(
            self.router_add_mechanism_button.mapToGlobal(
                self.router_add_mechanism_button.rect().bottomLeft()
            )
        )
        if chosen is not None and chosen.isEnabled():
            mechanism_type = actions.get(chosen, "")
            if mechanism_type:
                self._router_add_mechanism(mechanism_type)

    def _router_add_mechanism(self, mechanism_type: str):
        node = self._node
        mechanism_type = str(mechanism_type or "")
        if node is None or not mechanism_type:
            return
        mechanisms = normalize_router_mechanisms(node.settings.get("mechanisms"))
        enabled, disabled = [], []
        found = False
        for mechanism in mechanisms:
            item = dict(mechanism)
            current_type = str(item.get("type") or "")
            if current_type == mechanism_type:
                item["enabled"] = True
                found = True
            (enabled if bool(item.get("enabled")) else disabled).append(item)
        if not found:
            return
        node.settings["mechanisms"] = enabled + disabled
        self._selected_router_mechanism_type = mechanism_type
        node.sync_router_argument_port()
        node.update()
        self._populate_router_controls(node)
        self.modified.emit()

    def _router_remove_mechanism(self, mechanism_type: str):
        node = self._node
        mechanism_type = str(mechanism_type or "")
        if node is None or not mechanism_type:
            return
        mechanisms = normalize_router_mechanisms(node.settings.get("mechanisms"))
        enabled, disabled = [], []
        for mechanism in mechanisms:
            item = dict(mechanism)
            if str(item.get("type") or "") == mechanism_type:
                item["enabled"] = False
            (enabled if bool(item.get("enabled")) else disabled).append(item)
        node.settings["mechanisms"] = enabled + disabled
        if self._selected_router_mechanism_type == mechanism_type:
            self._selected_router_mechanism_type = (
                str(enabled[0].get("type") or "") if enabled else ""
            )
        node.sync_router_argument_port()
        node.update()
        self._populate_router_controls(node)
        self.modified.emit()

    def _router_select_mechanism(self, mechanism_type: str):
        mechanism_type = str(mechanism_type or "")
        self._selected_router_mechanism_type = mechanism_type
        for row_type, row in self._router_mechanism_rows_by_type.items():
            row.set_selected(row_type == mechanism_type)
        self._router_mechanism_selected()

    def _router_mechanism_row_order(self) -> List[str]:
        result = []
        for index in range(self.router_mechanisms_layout.count()):
            widget = self.router_mechanisms_layout.itemAt(index).widget()
            if isinstance(widget, LoopConditionRow):
                result.append(widget.condition_type)
        return result

    def _router_store_row_order(self):
        if self._loading_router:
            return
        node = self._node
        if node is None or not node.is_graph_router:
            return
        ordered_types = self._router_mechanism_row_order()
        mechanisms = normalize_router_mechanisms(node.settings.get("mechanisms"))
        by_type = {str(item.get("type") or ""): dict(item) for item in mechanisms}
        selected = []
        for mechanism_type in ordered_types:
            item = by_type.pop(mechanism_type, None)
            if item is not None:
                item["enabled"] = True
                selected.append(item)
        disabled = []
        for item in mechanisms:
            mechanism_type = str(item.get("type") or "")
            leftover = by_type.pop(mechanism_type, None)
            if leftover is not None:
                leftover["enabled"] = False
                disabled.append(leftover)
        node.settings["mechanisms"] = selected + disabled
        node.sync_router_argument_port()
        node.update()
        self.modified.emit()

    def _router_begin_mechanism_drag(self, row, global_y: int) -> bool:
        rows = [
            self.router_mechanisms_layout.itemAt(i).widget()
            for i in range(self.router_mechanisms_layout.count())
        ]
        rows = [item for item in rows if isinstance(item, LoopConditionRow)]
        if len(rows) < 2 or row not in rows:
            return False
        if self._router_drag_animation_group is not None:
            self._router_drag_animation_group.stop()
            self._router_drag_animation_group.deleteLater()
            self._router_drag_animation_group = None
        geometries = {candidate: candidate.geometry() for candidate in rows}
        for candidate in rows:
            self.router_mechanisms_layout.removeWidget(candidate)
        for candidate in rows:
            candidate.setGeometry(geometries[candidate])
            candidate.show()
        row_top_global = row.mapToGlobal(QPoint(0, 0)).y()
        self._router_drag_state = {
            "row": row,
            "order": list(rows),
            "grab_offset": int(global_y) - int(row_top_global),
        }
        row.setProperty("dragging", True)
        row.style().unpolish(row)
        row.style().polish(row)
        row.raise_()
        return True

    def _router_animate_rows_to_slots(self, order, *, skip=None, duration: int = 115):
        spacing = max(0, int(self.router_mechanisms_layout.spacing()))
        step = 40 + spacing
        if self._router_drag_animation_group is not None:
            self._router_drag_animation_group.stop()
            self._router_drag_animation_group.deleteLater()
        group = QParallelAnimationGroup(self)
        animated = False
        for index, candidate in enumerate(order):
            if candidate is skip:
                continue
            target = QPoint(candidate.x(), index * step)
            if candidate.pos() == target:
                continue
            animation = QPropertyAnimation(candidate, b"pos", group)
            animation.setDuration(max(1, int(duration)))
            animation.setStartValue(candidate.pos())
            animation.setEndValue(target)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)
            animated = True
        self._router_drag_animation_group = group
        if animated:
            group.start()
        else:
            group.deleteLater()
            self._router_drag_animation_group = None

    def _router_mechanism_drag_moved(self, mechanism_type: str, global_y: int):
        row = self._router_mechanism_rows_by_type.get(str(mechanism_type or ""))
        if row is None:
            return
        state = self._router_drag_state
        if state is None:
            if not self._router_begin_mechanism_drag(row, global_y):
                return
            state = self._router_drag_state
        if state is None or state.get("row") is not row:
            return
        order = list(state.get("order") or [])
        if len(order) < 2:
            return
        host_global = self.router_mechanisms_host.mapToGlobal(
            self.router_mechanisms_host.rect().topLeft()
        )
        cursor_y = int(global_y) - int(host_global.y())
        grab_offset = int(state.get("grab_offset", row.height() // 2))
        max_y = max(0, self.router_mechanisms_host.height() - row.height())
        dragged_y = max(0, min(cursor_y - grab_offset, max_y))
        row.move(row.x(), dragged_y)
        row.raise_()
        spacing = max(0, int(self.router_mechanisms_layout.spacing()))
        step = 40 + spacing
        others = [candidate for candidate in order if candidate is not row]
        target_index = int(round(float(dragged_y) / float(max(1, step))))
        target_index = max(0, min(target_index, len(others)))
        new_order = list(others)
        new_order.insert(target_index, row)
        if new_order != order:
            state["order"] = new_order
            self._router_animate_rows_to_slots(new_order, skip=row)

    def _router_mechanism_drag_finished(self):
        state = self._router_drag_state
        if state is None:
            return
        order = list(state.get("order") or [])
        if self._router_drag_animation_group is not None:
            self._router_drag_animation_group.stop()
            self._router_drag_animation_group.deleteLater()
            self._router_drag_animation_group = None
        spacing = max(0, int(self.router_mechanisms_layout.spacing()))
        step = 40 + spacing
        group = QParallelAnimationGroup(self)
        for index, candidate in enumerate(order):
            target = QPoint(candidate.x(), index * step)
            if candidate.pos() == target:
                continue
            animation = QPropertyAnimation(candidate, b"pos", group)
            animation.setDuration(105)
            animation.setStartValue(candidate.pos())
            animation.setEndValue(target)
            animation.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(animation)

        def finish():
            current = self._router_drag_state
            if current is None:
                return
            current_order = list(current.get("order") or [])
            dragged = current.get("row")
            if isinstance(dragged, LoopConditionRow):
                dragged.setProperty("dragging", False)
                dragged.style().unpolish(dragged)
                dragged.style().polish(dragged)
            for candidate in current_order:
                self.router_mechanisms_layout.addWidget(candidate)
            self.router_mechanisms_layout.activate()
            self._router_drag_state = None
            self._router_drag_animation_group = None
            self._router_store_row_order()

        if group.animationCount() == 0:
            group.deleteLater()
            finish()
            return
        self._router_drag_animation_group = group
        group.finished.connect(finish)
        group.start()

    def _selected_router_type(self) -> str:
        return str(self._selected_router_mechanism_type or "")

    def _selected_router_mechanism(self):
        node = self._node
        selected_type = self._selected_router_type()
        if node is None or not selected_type:
            return None
        for item in normalize_router_mechanisms(node.settings.get("mechanisms")):
            if str(item.get("type") or "") == selected_type and bool(item.get("enabled")):
                return item
        return None

    def _populate_router_route_combo(self, node: PipelineNodeItem, *, preserve: bool = False):
        current = self.router_route_combo.currentData() if preserve else None
        count = max(1, node.router_output_count())
        descriptions = node.settings.get("route_descriptions")
        if not isinstance(descriptions, dict):
            descriptions = {}
        self.router_route_combo.blockSignals(True)
        self.router_route_combo.clear()
        for index in range(count):
            description = str(descriptions.get(str(index), "") or "").strip()
            label = f"Exec {index}" + (f" — {description}" if description else "")
            add_caption_item(self.router_route_combo, label, index)
        if preserve and current is not None:
            idx = self.router_route_combo.findData(current)
            if idx >= 0:
                self.router_route_combo.setCurrentIndex(idx)
        self.router_route_combo.blockSignals(False)

    def _router_mechanism_selected(self):
        if self._loading_router:
            return
        node = self._node
        if node is None or not node.is_graph_router:
            return
        mechanism_type = self._selected_router_type()
        definition = self._router_definition_map().get(mechanism_type, {})
        title = mechanism_text(definition, 'title')
        bind_caption(self.router_detail_title, tr('ui.caption.73df6719df07', v0=title) if mechanism_type else "")
        self.router_detail_area.setVisible(bool(mechanism_type))
        self.router_detail_title.setVisible(bool(mechanism_type))
        self.router_route_combo.hide()
        self.router_prompt_edit.hide()
        self.router_python_edit.hide()
        self.router_user_edit.hide()

        mechanism = self._selected_router_mechanism() or {}
        if mechanism_type in {"error", "always"}:
            self._populate_router_route_combo(node)
            route = max(0, int(mechanism.get("route", 0) or 0))
            idx = self.router_route_combo.findData(route)
            self.router_route_combo.setCurrentIndex(max(0, idx))
            self.router_route_combo.show()
        elif mechanism_type in {"subagent", "agent"}:
            self.router_prompt_edit.blockSignals(True)
            self.router_prompt_edit.setPlainText(str(mechanism.get("prompt") or ""))
            bind_caption(self.router_prompt_edit, tr('ui.caption.e74bf8594b19')
                if mechanism_type == "subagent"
                else tr('ui.caption.066d0cdb3ab5'), setter='setPlaceholderText')
            self.router_prompt_edit.blockSignals(False)
            self.router_prompt_edit.show()
        elif mechanism_type == "python":
            self.router_python_edit.blockSignals(True)
            self.router_python_edit.setPlainText(str(mechanism.get("code") or ""))
            self.router_python_edit.blockSignals(False)
            self.router_python_edit.show()
        elif mechanism_type == "user":
            self.router_user_edit.blockSignals(True)
            self.router_user_edit.setPlainText(str(mechanism.get("message") or ""))
            self.router_user_edit.blockSignals(False)
            self.router_user_edit.show()

        self.router_detail_layout.invalidate()
        self.router_detail_area.updateGeometry()
        self.router_panel.updateGeometry()
        self._main_layout.invalidate()

    def _update_router_mechanism_field(self, field: str, value):
        if self._loading_router:
            return
        node = self._node
        selected_type = self._selected_router_type()
        if node is None or not selected_type:
            return
        mechanisms = normalize_router_mechanisms(node.settings.get("mechanisms"))
        changed = False
        for item in mechanisms:
            if str(item.get("type") or "") == selected_type and bool(item.get("enabled")):
                if item.get(field) != value:
                    item[field] = value
                    changed = True
                break
        if changed:
            node.settings["mechanisms"] = mechanisms
            node.sync_router_argument_port()
            node.update()
            self.modified.emit()

    def _router_route_choice_changed(self, _index: int):
        if self._selected_router_type() not in {"error", "always"}:
            return
        value = self.router_route_combo.currentData()
        if value is not None:
            self._update_router_mechanism_field("route", int(value))

    def _router_prompt_changed(self):
        if self._selected_router_type() in {"subagent", "agent"}:
            self._update_router_mechanism_field("prompt", self.router_prompt_edit.toPlainText())

    def _router_python_changed(self):
        if self._selected_router_type() == "python":
            self._update_router_mechanism_field("code", self.router_python_edit.toPlainText())

    def _router_user_message_changed(self):
        if self._selected_router_type() == "user":
            self._update_router_mechanism_field("message", self.router_user_edit.toPlainText())

    @staticmethod
    def _clear_widget_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _argument_type_label(schema_type: str) -> str:
        return {
            "string": "String",
            "boolean": "Bool",
            "integer": "Integer",
            "float": "Float",
            "array": "Array",
            "object": "Object / JSON",
        }.get(str(schema_type or "string"), "String")

    @classmethod
    def _fill_argument_type_combo(cls, combo: QComboBox, current: str):
        combo.blockSignals(True)
        combo.clear()
        for schema_type in ARGUMENT_SCHEMA_TYPES:
            combo.addItem(cls._argument_type_label(schema_type), schema_type)
        index = combo.findData(str(current or "string"))
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    @staticmethod
    def _safe_python_identifier(raw: str, fallback: str, used: set[str]) -> str:
        value = re.sub(r"\W", "_", str(raw or "").strip())
        if not value:
            value = str(fallback or "arg")
        if value[0].isdigit():
            value = f"_{value}"
        if not value.isidentifier() or keyword.iskeyword(value):
            value = str(fallback or "arg")
        base = value
        suffix = 2
        while value in used:
            value = f"{base}_{suffix}"
            suffix += 1
        return value

    @staticmethod
    def _edge_matches_port(edge, node: PipelineNodeItem, direction: str, port_key: str) -> bool:
        if direction == "input":
            return edge.target is node and str(edge.target_port) == str(port_key)
        return edge.source is node and str(edge.source_port) == str(port_key)

    def _drop_edges_for_dynamic_port(self, node: PipelineNodeItem, direction: str, port_key: str):
        view = node._pipeline_view()
        for edge in list(node.edges):
            if not self._edge_matches_port(edge, node, direction, port_key):
                continue
            scene = edge.scene()
            if scene is not None:
                scene.removeItem(edge)
            edge.detach()
            refs = getattr(view, "_pipeline_edge_refs", None) if view is not None else None
            if refs is not None:
                try:
                    refs.discard(edge)
                except (AttributeError, RuntimeError):
                    pass

    def _populate_python_code_controls(self, node: PipelineNodeItem):
        if not node.is_python_code:
            return
        node.sync_python_code_ports()
        self.python_code_edit.blockSignals(True)
        self.python_code_edit.setPlainText(str(node.settings.get("code") or ""))
        self.python_code_edit.blockSignals(False)
        self._populate_python_argument_rows(node, "inputs", self.python_inputs_layout)
        self._populate_python_argument_rows(node, "outputs", self.python_outputs_layout)

    def _populate_python_argument_rows(self, node: PipelineNodeItem, section: str, layout):
        self._clear_widget_layout(layout)
        definitions = normalize_argument_definitions(node.settings.get(section))
        node.settings[section] = definitions
        if not definitions:
            empty = localize(QLabel('', self.python_code_panel), 'ui.inspector.nothing_for_now_9ec6')
            empty.setObjectName("PipelineInspectorDescription")
            layout.addWidget(empty)
            return
        for item in definitions:
            item_id = str(item.get("id") or "")
            row = QFrame(self.python_code_panel)
            row.setObjectName("PipelineDynamicArgRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            name_edit = QLineEdit(row)
            name_edit.setObjectName("PipelineScalarEditor")
            bind_caption(name_edit, str(item.get("name") or ""))
            set_text(name_edit, 'ui.inspector.name_82a3', setter='setPlaceholderText')
            name_edit.editingFinished.connect(
                lambda sec=section, iid=item_id, edit=name_edit: self._python_argument_name_changed(sec, iid, edit)
            )
            row_layout.addWidget(name_edit, 1)

            type_combo = QComboBox(row)
            type_combo.setObjectName("PipelineArrayTypeCombo")
            self._fill_argument_type_combo(type_combo, str(item.get("type") or "string"))
            type_combo.currentIndexChanged.connect(
                lambda _idx, sec=section, iid=item_id, combo=type_combo: self._python_argument_type_changed(sec, iid, combo)
            )
            row_layout.addWidget(type_combo, 0)

            remove = QToolButton(row)
            set_icon(remove, 'close', square=True)
            remove.setFixedSize(24, 24)
            remove.clicked.connect(
                lambda _checked=False, sec=section, iid=item_id: self._python_remove_argument(sec, iid)
            )
            row_layout.addWidget(remove, 0)
            layout.addWidget(row)

    def _python_add_argument(self, section: str):
        node = self._node
        if node is None or not node.is_python_code or section not in {"inputs", "outputs"}:
            return
        definitions = normalize_argument_definitions(node.settings.get(section))
        used = {str(item.get("name") or "") for item in definitions}
        base = "input" if section == "inputs" else "output"
        name = self._safe_python_identifier(f"{base}_{len(definitions) + 1}", base, used)
        definitions.append({
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "type": "string",
        })
        node.settings[section] = definitions
        node.sync_python_code_ports()
        self._populate_python_code_controls(node)
        node.update()
        self.modified.emit()

    def _python_remove_argument(self, section: str, item_id: str):
        node = self._node
        if node is None or not node.is_python_code:
            return
        definitions = normalize_argument_definitions(node.settings.get(section))
        target = next((item for item in definitions if str(item.get("id")) == str(item_id)), None)
        if target is None:
            return
        settings = dict(node.settings)
        settings[section] = [item for item in definitions if str(item.get("id")) != str(item_id)]
        view = node._pipeline_view()
        if view is not None: view.change_node_settings(node, settings)
        else: node.settings = settings
        node.sync_python_code_ports()
        self._populate_python_code_controls(node)
        node.update()
        self.modified.emit()

    def _python_argument_name_changed(self, section: str, item_id: str, edit: QLineEdit):
        node = self._node
        if node is None or not node.is_python_code:
            return
        definitions = normalize_argument_definitions(node.settings.get(section))
        used = {
            str(item.get("name") or "")
            for item in definitions
            if str(item.get("id")) != str(item_id)
        }
        fallback = "input" if section == "inputs" else "output"
        new_name = self._safe_python_identifier(edit.text(), fallback, used)
        changed = False
        for item in definitions:
            if str(item.get("id")) == str(item_id):
                if str(item.get("name") or "") != new_name:
                    item["name"] = new_name
                    changed = True
                break
        if edit.text() != new_name:
            edit.blockSignals(True)
            bind_caption(edit, new_name)
            edit.blockSignals(False)
        if changed:
            node.settings[section] = definitions
            node.sync_python_code_ports()
            node.update()
            self.modified.emit()

    def _python_argument_type_changed(self, section: str, item_id: str, combo: QComboBox):
        node = self._node
        if node is None or not node.is_python_code:
            return
        value = str(combo.currentData() or "string")
        definitions = normalize_argument_definitions(node.settings.get(section))
        changed = False
        for item in definitions:
            if str(item.get("id")) == str(item_id):
                if str(item.get("type") or "string") != value:
                    item["type"] = value
                    changed = True
                break
        if changed:
            node.settings[section] = definitions
            node.sync_python_code_ports()
            node.update()
            self.modified.emit()

    def _python_code_changed(self):
        node = self._node
        if node is None or not node.is_python_code:
            return
        value = self.python_code_edit.toPlainText()
        if str(node.settings.get("code") or "") == value:
            return
        node.settings["code"] = value
        node.update()
        self.modified.emit()

    def _populate_human_in_loop_controls(self, node: PipelineNodeItem):
        if not node.is_human_in_loop:
            return
        node.sync_human_in_loop_ports()
        self.human_message_edit.blockSignals(True)
        self.human_message_edit.setPlainText(str(node.settings.get("message") or ""))
        self.human_message_edit.blockSignals(False)

        enabled = bool(node.settings.get("arguments_enabled"))
        self.human_arguments_toggle.blockSignals(True)
        self.human_arguments_toggle.setChecked(enabled)
        self.human_arguments_toggle.blockSignals(False)
        self.human_outputs_host.setVisible(enabled)
        self.human_add_output.setVisible(enabled)
        self.human_fill_hint.setVisible(enabled)
        self._populate_human_output_rows(node)

    def _populate_human_output_rows(self, node: PipelineNodeItem):
        self._clear_widget_layout(self.human_outputs_layout)
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        node.settings["outputs"] = definitions
        if not definitions:
            empty = localize(QLabel('', self.human_loop_panel), 'ui.inspector.nothing_for_now_9ec6')
            empty.setObjectName("PipelineInspectorDescription")
            self.human_outputs_layout.addWidget(empty)
            return
        for item in definitions:
            item_id = str(item.get("id") or "")
            block = QFrame(self.human_loop_panel)
            block.setObjectName("PipelineDynamicArgRow")
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 0, 0, 0)
            block_layout.setSpacing(5)

            top = QFrame(block)
            top_layout = QHBoxLayout(top)
            top_layout.setContentsMargins(0, 0, 0, 0)
            top_layout.setSpacing(6)

            name_edit = QLineEdit(top)
            name_edit.setObjectName("PipelineScalarEditor")
            bind_caption(name_edit, str(item.get("name") or ""))
            set_text(name_edit, 'ui.inspector.name_82a3', setter='setPlaceholderText')
            name_edit.editingFinished.connect(
                lambda iid=item_id, edit=name_edit: self._human_output_name_changed(iid, edit)
            )
            top_layout.addWidget(name_edit, 1)

            type_combo = QComboBox(top)
            type_combo.setObjectName("PipelineArrayTypeCombo")
            self._fill_argument_type_combo(type_combo, str(item.get("type") or "string"))
            type_combo.currentIndexChanged.connect(
                lambda _idx, iid=item_id, combo=type_combo: self._human_output_type_changed(iid, combo)
            )
            top_layout.addWidget(type_combo, 0)

            remove = QToolButton(top)
            set_icon(remove, 'close', square=True)
            remove.setFixedSize(24, 24)
            remove.clicked.connect(lambda _checked=False, iid=item_id: self._human_remove_output(iid))
            top_layout.addWidget(remove, 0)
            block_layout.addWidget(top)

            description = QLineEdit(block)
            description.setObjectName("PipelineScalarEditor")
            set_text(description, 'ui.inspector.field_description_for_the_user_6aff', setter='setPlaceholderText')
            bind_caption(description, str(item.get("description") or ""))
            description.textChanged.connect(
                lambda text, iid=item_id: self._human_output_description_changed(iid, text)
            )
            block_layout.addWidget(description)
            self.human_outputs_layout.addWidget(block)

    def _human_message_changed(self):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        value = self.human_message_edit.toPlainText()
        if str(node.settings.get("message") or "") == value:
            return
        node.settings["message"] = value
        node.update()
        self.modified.emit()

    def _human_arguments_toggled(self, enabled: bool):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        enabled = bool(enabled)
        settings = dict(node.settings, arguments_enabled=enabled)
        view = node._pipeline_view()
        if view is not None: view.change_node_settings(node, settings)
        else: node.settings = settings
        node.sync_human_in_loop_ports()
        self.human_outputs_host.setVisible(enabled)
        self.human_add_output.setVisible(enabled)
        self.human_fill_hint.setVisible(enabled)
        node.update()
        self.modified.emit()

    def _human_add_output(self):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        used = {str(item.get("name") or "") for item in definitions}
        name = self._safe_python_identifier(
            f"argument_{len(definitions) + 1}",
            "argument",
            used,
        )
        definitions.append({
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "type": "string",
            "description": "",
        })
        node.settings["arguments_enabled"] = True
        node.settings["outputs"] = definitions
        node.sync_human_in_loop_ports()
        self._populate_human_in_loop_controls(node)
        node.update()
        self.modified.emit()

    def _human_remove_output(self, item_id: str):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        target = next((item for item in definitions if str(item.get("id")) == str(item_id)), None)
        if target is None:
            return
        settings = dict(node.settings)
        settings['outputs'] = [item for item in definitions if str(item.get("id")) != str(item_id)]
        view = node._pipeline_view()
        if view is not None: view.change_node_settings(node, settings)
        else: node.settings = settings
        node.sync_human_in_loop_ports()
        self._populate_human_in_loop_controls(node)
        node.update()
        self.modified.emit()

    def _human_output_name_changed(self, item_id: str, edit: QLineEdit):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        used = {
            str(item.get("name") or "")
            for item in definitions
            if str(item.get("id")) != str(item_id)
        }
        new_name = self._safe_python_identifier(edit.text(), "argument", used)
        changed = False
        for item in definitions:
            if str(item.get("id")) == str(item_id):
                if str(item.get("name") or "") != new_name:
                    item["name"] = new_name
                    changed = True
                break
        if edit.text() != new_name:
            edit.blockSignals(True)
            bind_caption(edit, new_name)
            edit.blockSignals(False)
        if changed:
            node.settings["outputs"] = definitions
            node.sync_human_in_loop_ports()
            node.update()
            self.modified.emit()

    def _human_output_type_changed(self, item_id: str, combo: QComboBox):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        value = str(combo.currentData() or "string")
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        changed = False
        for item in definitions:
            if str(item.get("id")) == str(item_id):
                if str(item.get("type") or "string") != value:
                    item["type"] = value
                    changed = True
                break
        if changed:
            node.settings["outputs"] = definitions
            node.sync_human_in_loop_ports()
            node.update()
            self.modified.emit()

    def _human_output_description_changed(self, item_id: str, text: str):
        node = self._node
        if node is None or not node.is_human_in_loop:
            return
        definitions = normalize_argument_definitions(
            node.settings.get("outputs"),
            with_description=True,
        )
        changed = False
        for item in definitions:
            if str(item.get("id")) == str(item_id):
                if str(item.get("description") or "") != str(text):
                    item["description"] = str(text)
                    changed = True
                break
        if changed:
            node.settings["outputs"] = definitions
            self.modified.emit()

    def _populate_knowledge_databases(self, node: PipelineNodeItem):
        selected = {
            str(value)
            for value in (node.settings.get("databases") or [])
            if str(value).strip()
        }
        catalog = discover_knowledge_base_catalog()
        known_ids = {str(entry.get("id") or "") for entry in catalog}

        self.knowledge_db_list.blockSignals(True)
        self.knowledge_db_list.clear()
        try:
            for entry in catalog:
                database_id = str(entry.get("id") or "").strip()
                if not database_id:
                    continue
                name = str(entry.get("name") or database_id).strip() or database_id
                manifest = str(entry.get("manifest") or "").strip()
                owner = str(entry.get("owner") or "").strip()

                item = QListWidgetItem(name, self.knowledge_db_list)
                item.setData(Qt.UserRole, database_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.Checked if database_id in selected else Qt.Unchecked
                )
                tooltip = [f"ID: {database_id}"]
                if owner:
                    tooltip.append(f"Owner: {owner}")
                if manifest:
                    tooltip.extend(["", manifest])
                bind_caption(item, "\n".join(tooltip), setter='setToolTip')

            # Keep stale selections visible instead of silently deleting them.
            for database_id in sorted(selected - known_ids):
                item = QListWidgetItem(f"[missing] {database_id}", self.knowledge_db_list)
                item.setData(Qt.UserRole, database_id)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                set_text(item, 'ui.inspector.this_database_is_selected_by_the_0ede', setter='setToolTip')
        finally:
            self.knowledge_db_list.blockSignals(False)

        # A graph-provided Arg value and an inspector-fixed value must not both
        # own the same argument. Mirror the scalar-node rule and disable local
        # selection while an incoming databases edge exists.
        self.knowledge_db_list.setEnabled(
            not bool(node.incoming_edges("arg:databases"))
        )
        if not catalog and not selected:
            set_text(self.knowledge_db_list, 'ui.inspector.no_knowledge_bases_are_currently_exposed_3818', setter='setToolTip')
        elif node.incoming_edges("arg:databases"):
            set_text(self.knowledge_db_list, 'ui.inspector.databases_are_supplied_through_the_arg_e39b', setter='setToolTip')
        else:
            set_text(self.knowledge_db_list, 'ui.inspector.check_one_or_more_databases_to_2c50', setter='setToolTip')

    def _knowledge_databases_changed(self, _item=None):
        node = self._node
        if node is None:
            return
        if str(node.spec.metadata.get("inspector_type") or "") != "knowledge_search":
            return
        if node.incoming_edges("arg:databases"):
            return

        selected: List[str] = []
        for index in range(self.knowledge_db_list.count()):
            item = self.knowledge_db_list.item(index)
            if item.checkState() != Qt.Checked:
                continue
            database_id = str(item.data(Qt.UserRole) or "").strip()
            if database_id and database_id not in selected:
                selected.append(database_id)

        node.settings["databases"] = selected
        node.sync_fixed_argument_ports()
        node.normalize_dynamic_ports()
        node.update()
        # Rebuild the schema summary because fixing databases hides/restores the
        # corresponding Arg port. Signals are blocked while the list is rebuilt.
        self.set_node(node)
        self.modified.emit()

    def _sync_content_editor_enabled(self, node: PipelineNodeItem):
        # Prompt is intentionally additive: local text remains editable while an
        # Arg input is connected, and the incoming value is appended below it.
        # Other scalar content nodes still use exactly one value source. Array
        # remains excluded because it may combine literals with incoming items.
        enabled = not node.content_input_connected()
        if node.value_type == "prompt":
            self.prompt_edit.setEnabled(True)
        elif node.value_type == "string":
            self.prompt_edit.setEnabled(enabled)
        elif node.value_type == "binary":
            self.binary_button.setEnabled(enabled)
        elif node.value_type in {"integer", "float"}:
            self.scalar_edit.setEnabled(enabled)

    def _text_changed(self):
        if self._node is None:
            return
        if self._node.value_type == "prompt":
            self._node.settings["text"] = self.prompt_edit.toPlainText()
        elif self._node.value_type == "string":
            self._node.settings["value"] = self.prompt_edit.toPlainText()
        else:
            return
        self._node.sync_content_input_port()
        self._node.update()
        self.modified.emit()

    def _sync_binary_button_text(self, value):
        if value is None:
            set_text(self.binary_button, 'ui.inspector.none_dc93')
        else:
            bind_caption(self.binary_button, "True" if bool(value) else "False")

    def _open_binary_menu(self):
        if self._node is None or self._node.value_type != "binary":
            return
        menu = QMenu(self.binary_button)
        true_action = localize(add_caption_action(menu, 'addAction', "True"), 'ui.inspector.true_3cbc')
        false_action = localize(add_caption_action(menu, 'addAction', "False"), 'ui.inspector.false_60a3')
        none_action = localize(add_caption_action(menu, 'addAction', "None"), 'ui.inspector.none_dc93')
        chosen = menu.exec(self.binary_button.mapToGlobal(self.binary_button.rect().bottomLeft()))
        if chosen == true_action:
            value = True
        elif chosen == false_action:
            value = False
        elif chosen == none_action:
            value = None
        else:
            return
        self._node.settings["value"] = value
        self._sync_binary_button_text(value)
        self._node.sync_content_input_port()
        self._node.update()
        self.modified.emit()

    @staticmethod
    def _sanitize_integer_text(text: str) -> str:
        return "".join(char for char in str(text or "") if char.isdigit())

    @staticmethod
    def _sanitize_float_text(text: str) -> str:
        source = str(text or "").replace(",", ".")
        result = []
        dot_seen = False
        for char in source:
            if char.isdigit():
                result.append(char)
            elif char == "." and not dot_seen:
                result.append(char)
                dot_seen = True
        return "".join(result)

    def _replace_scalar_text(self, value: str):
        self.scalar_edit.blockSignals(True)
        bind_caption(self.scalar_edit, value)
        self.scalar_edit.setCursorPosition(len(value))
        self.scalar_edit.blockSignals(False)

    def _scalar_changed(self, text: str):
        if self._node is None:
            return

        value_type = self._node.value_type
        if value_type == "integer":
            value_text = self._sanitize_integer_text(text)
        elif value_type == "float":
            value_text = self._sanitize_float_text(text)
        else:
            return

        if value_text != text:
            self._replace_scalar_text(value_text)

        if not value_text or value_text == ".":
            self._node.settings["value"] = None
            self._node.sync_content_input_port()
            self._node.update()
            self.modified.emit()
            return

        try:
            if value_type == "integer":
                value = int(value_text, 10)
            else:
                value = float(value_text)
                if not math.isfinite(value):
                    return
        except ValueError:
            return

        self._node.settings["value"] = value
        self._node.sync_content_input_port()
        self._node.update()
        self.modified.emit()

    def _scalar_editing_finished(self):
        if self._node is None:
            return

        value_type = self._node.value_type
        if value_type == "integer":
            normalized = self._sanitize_integer_text(self.scalar_edit.text())
            if not normalized:
                self._node.settings["value"] = None
                self._replace_scalar_text("")
                self._node.sync_content_input_port()
                self._node.update()
                self.modified.emit()
                return
            value = int(normalized, 10)
            normalized = str(value)
        elif value_type == "float":
            normalized = self._sanitize_float_text(self.scalar_edit.text())
            if not normalized or normalized == ".":
                self._node.settings["value"] = None
                self._replace_scalar_text("")
                self._node.sync_content_input_port()
                self._node.update()
                self.modified.emit()
                return
            if normalized.startswith("."):
                normalized = "0" + normalized
            if "." not in normalized:
                normalized += ".0"
            elif normalized.endswith("."):
                normalized += "0"
            value = float(normalized)
        else:
            return

        self._node.settings["value"] = value
        self._replace_scalar_text(normalized)
        self._node.sync_content_input_port()
        self._node.update()
        self.modified.emit()

    @staticmethod
    def _sanitize_array_live(text: str, item_type: str) -> str:
        source = str(text or "")
        tokens = source.split(",")
        cleaned_tokens: List[str] = []

        for index, raw_token in enumerate(tokens):
            # Whitespace immediately after a separator belongs to formatting,
            # not to the value. Internal spaces in strings are preserved.
            token = raw_token.lstrip() if index > 0 else raw_token
            if item_type == "string":
                cleaned = token
            elif item_type == "integer":
                cleaned = "".join(char for char in token if char.isdigit())
            elif item_type == "float":
                chars: List[str] = []
                dot_seen = False
                for char in token:
                    if char.isdigit():
                        chars.append(char)
                    elif char == "." and not dot_seen:
                        chars.append(char)
                        dot_seen = True
                cleaned = "".join(chars)
            elif item_type == "binary":
                # Binary arrays are edited through chips, never free text.
                cleaned = ""
            else:
                cleaned = token
            cleaned_tokens.append(cleaned)

        # Always format delimiters as comma + one space while typing.
        return ", ".join(cleaned_tokens)

    @staticmethod
    def _parse_array_literal(text: str, item_type: str) -> List[object]:
        values: List[object] = []
        tokens = [token.strip() for token in str(text or "").split(",")]
        for token in tokens:
            if not token:
                continue
            if item_type == "string":
                values.append(token)
            elif item_type == "binary":
                lowered = token.casefold()
                if lowered == "true":
                    values.append(True)
                elif lowered == "false":
                    values.append(False)
            elif item_type == "integer":
                if token.isdigit():
                    values.append(int(token, 10))
            elif item_type == "float":
                dot_seen = False
                valid = True
                for char in token:
                    if char.isdigit():
                        continue
                    if char == "." and not dot_seen:
                        dot_seen = True
                        continue
                    valid = False
                    break
                if valid and token != ".":
                    try:
                        value = float(("0" + token) if token.startswith(".") else token)
                    except ValueError:
                        continue
                    if math.isfinite(value):
                        values.append(value)
        return values

    @staticmethod
    def _normalize_array_literal(text: str, item_type: str) -> str:
        raw_tokens = [token.strip() for token in str(text or "").split(",")]
        normalized: List[str] = []
        for token in raw_tokens:
            if not token:
                continue
            if item_type == "string":
                normalized.append(token)
            elif item_type == "binary":
                lowered = token.casefold()
                if lowered in {"true", "false"}:
                    normalized.append("True" if lowered == "true" else "False")
            elif item_type == "integer":
                clean = "".join(char for char in token if char.isdigit())
                if clean:
                    normalized.append(str(int(clean, 10)))
            elif item_type == "float":
                clean_chars = []
                dot_seen = False
                for char in token:
                    if char.isdigit():
                        clean_chars.append(char)
                    elif char == "." and not dot_seen:
                        clean_chars.append(char)
                        dot_seen = True
                clean = "".join(clean_chars)
                if not clean or clean == ".":
                    continue
                if clean.startswith("."):
                    clean = "0" + clean
                if "." not in clean:
                    clean += ".0"
                elif clean.endswith("."):
                    clean += "0"
                try:
                    value = float(clean)
                except ValueError:
                    continue
                if math.isfinite(value):
                    normalized.append(clean)
        return ", ".join(normalized)

    def _array_type_changed(self, index: int):
        if self._node is None or self._node.value_type != "array":
            return
        item_type = str(self.array_type_combo.itemData(index) or "string")
        self._node.set_array_item_type(item_type)
        if item_type == "binary":
            existing = self._parse_array_literal(
                str(self._node.settings.get("literal_text") or ""),
                "binary",
            )
            self._set_binary_array_values([bool(value) for value in existing])
        else:
            self._array_editing_finished()
        self._sync_array_editor_mode()
        self.modified.emit()

    def _sync_array_editor_mode(self):
        if self._node is None or self._node.value_type != "array":
            self.array_edit.hide()
            self.binary_array_host.hide()
            return
        is_binary = str(self._node.settings.get("item_type") or "string") == "binary"
        self.array_edit.setVisible(not is_binary)
        self.binary_array_host.setVisible(is_binary)
        if is_binary:
            self._rebuild_binary_array_chips()

    def _binary_array_values(self) -> List[bool]:
        if self._node is None:
            return []
        values = self._node.settings.get("value")
        if not isinstance(values, list):
            return []
        return [bool(value) for value in values if isinstance(value, bool)]

    def _set_binary_array_values(self, values: List[bool]):
        if self._node is None or self._node.value_type != "array":
            return
        clean = [bool(value) for value in values]
        self._node.settings["value"] = clean
        self._node.settings["literal_text"] = ", ".join(
            "True" if value else "False" for value in clean
        )
        self._node.update()
        self._rebuild_binary_array_chips()
        self.modified.emit()

    def _clear_binary_array_chips(self):
        # Keep the trailing + button and stretch; remove only value chips.
        for index in range(self.binary_array_layout.count() - 1, -1, -1):
            item = self.binary_array_layout.itemAt(index)
            widget = item.widget()
            if isinstance(widget, BinaryValueChip):
                self.binary_array_layout.takeAt(index)
                widget.deleteLater()

    def _rebuild_binary_array_chips(self):
        self._clear_binary_array_chips()
        values = self._binary_array_values()
        for index, value in enumerate(values):
            chip = BinaryValueChip(value, self.binary_array_host)
            chip.value_changed.connect(
                lambda new_value, idx=index: self._change_binary_array_value(idx, new_value)
            )
            chip.remove_requested.connect(
                lambda idx=index: self._remove_binary_array_value(idx)
            )
            # Insert before the + button.
            plus_index = self.binary_array_layout.indexOf(self.binary_array_add)
            self.binary_array_layout.insertWidget(max(0, plus_index), chip)

    def _open_binary_array_add_menu(self):
        if self._node is None or self._node.value_type != "array":
            return
        menu = QMenu(self.binary_array_add)
        true_action = localize(add_caption_action(menu, 'addAction', "True"), 'ui.inspector.true_3cbc')
        false_action = localize(add_caption_action(menu, 'addAction', "False"), 'ui.inspector.false_60a3')
        chosen = menu.exec(
            self.binary_array_add.mapToGlobal(self.binary_array_add.rect().bottomLeft())
        )
        if chosen == true_action:
            value = True
        elif chosen == false_action:
            value = False
        else:
            return
        values = self._binary_array_values()
        values.append(value)
        self._set_binary_array_values(values)

    def _change_binary_array_value(self, index: int, value: bool):
        values = self._binary_array_values()
        if 0 <= index < len(values):
            values[index] = bool(value)
            self._set_binary_array_values(values)

    def _remove_binary_array_value(self, index: int):
        values = self._binary_array_values()
        if 0 <= index < len(values):
            del values[index]
            self._set_binary_array_values(values)

    def _array_text_changed(self, text: str):
        if self._node is None or self._node.value_type != "array":
            return
        item_type = str(self._node.settings.get("item_type") or "string")
        if item_type == "binary":
            return
        sanitized = self._sanitize_array_live(text, item_type)
        if sanitized != text:
            self.array_edit.blockSignals(True)
            bind_caption(self.array_edit, sanitized)
            self.array_edit.setCursorPosition(len(sanitized))
            self.array_edit.blockSignals(False)
        self._node.settings["literal_text"] = sanitized
        self._node.settings["value"] = self._parse_array_literal(sanitized, item_type)
        self._node.update()
        self.modified.emit()

    def _array_editing_finished(self):
        if self._node is None or self._node.value_type != "array":
            return
        item_type = str(self._node.settings.get("item_type") or "string")
        if item_type == "binary":
            return
        normalized = self._normalize_array_literal(self.array_edit.text(), item_type)
        self.array_edit.blockSignals(True)
        bind_caption(self.array_edit, normalized)
        self.array_edit.setCursorPosition(len(normalized))
        self.array_edit.blockSignals(False)
        self._node.settings["literal_text"] = normalized
        self._node.settings["value"] = self._parse_array_literal(normalized, item_type)
        self._node.update()
        self.modified.emit()


def mechanism_text(definition, field):
    family = 'router' if definition in ROUTER_MECHANISM_DEFINITIONS else 'loop'
    identity = definition.get('type', '')
    return tr(f'ui.mechanisms.{family}.{identity}.{field}')
