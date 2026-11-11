"""Inspector controls for execution policies, structured transforms and grouping."""
from __future__ import annotations
from ui.styles.binding import apply_style
from ui.icons import set_icon
from ui.i18n import bind_caption, add_caption_item, tr, set_text, add_item, add_row

import json
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton, QColorDialog, QListWidget, QToolButton, QSizePolicy)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt
from modules.pipelines.execution import NOTIFICATION, PARALLEL, TRANSFORM
from modules.pipelines.transform import OPERATIONS

EXAMPLES = {
    'get': {'path': 'items.0.name'}, 'select': {'fields': ['name', 'date']}, 'rename': {'fields': {'old': 'new'}},
    'filter': {'path': 'status', 'comparison': 'eq', 'value': 'done'}, 'sort': {'path': '', 'reverse': False},
    'unique': {'path': ''}, 'replace': {'old': '', 'new': ''}, 'regex': {'pattern': '', 'replacement': ''},
    'split': {'separator': '\n'}, 'join': {'separator': '\n'},
    'date': {'input_format': 'iso', 'output_format': '%Y-%m-%d', 'input_timezone': 'UTC', 'timezone': 'UTC'},
    'convert': {'type': 'string'},
}


class ExecutionSettings(QWidget):
    def __init__(self, inspector):
        super().__init__(inspector)
        self.setObjectName('PipelineExecutionSettings')
        apply_style(self, '\n            #PipelineExecutionSettings QLabel { color:@text.primary@; background:transparent; }\n            #PipelineExecutionSettings QPlainTextEdit, #PipelineExecutionSettings QLineEdit, #PipelineExecutionSettings QComboBox,\n            #PipelineExecutionSettings QSpinBox, #PipelineExecutionSettings QDoubleSpinBox,\n            #PipelineExecutionSettings QListWidget, #PipelineExecutionSettings QTableWidget {\n                background:@background.window@; color:@text.primary@; border:1px solid @border.subtle@;\n                border-radius:5px; padding:5px; selection-background-color:#3f51b5;\n            }\n            #PipelineExecutionSettings QCheckBox {color:@text.primary@;background:transparent;spacing:6px;}\n            #PipelineExecutionSettings QHeaderView::section {background:@background.surface@;color:@text.primary@;padding:4px;border:1px solid @border.subtle@;}\n            #PipelineExecutionSettings QComboBox QAbstractItemView { background:@background.surface@; color:@text.primary@; }\n            #PipelineExecutionSettings QPushButton {\n                background:@background.surface@; color:@text.primary@; border:1px solid @border.subtle@; border-radius:4px; padding:4px;\n            }\n            #PipelineExecutionSettings QPushButton:hover { border-color:@accent.primary@; }\n            #PipelineExecutionSettings QComboBox:disabled, #PipelineExecutionSettings QSpinBox:disabled,\n            #PipelineExecutionSettings QDoubleSpinBox:disabled { color:@text.disabled@; background:@background.panel@; }\n        ')
        self.inspector, self.node, self.loading = inspector, None, False
        self.layout = QVBoxLayout(self); self.layout.setContentsMargins(0, 0, 0, 0)
        self.details = QWidget(self); self.form = QFormLayout(self.details)
        self.form.setContentsMargins(0, 0, 0, 0); self.form.setRowWrapPolicy(QFormLayout.WrapLongRows); self.layout.addWidget(self.details)
        self._error_expanded = False
        self.error_header = QToolButton(self)
        set_text(self.error_header, 'ui.execution_settings.error_handling_2208')
        self.error_header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        set_icon(self.error_header, 'right', size=16, icon_only=False)
        self.error_header.setCheckable(True)
        self.error_header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        apply_style(self.error_header, 'QToolButton {color:@text.primary@;background:@background.surface@;border:1px solid @border.subtle@;border-radius:4px;padding:7px;text-align:left;}')
        self.error_header.toggled.connect(self._toggle_errors)
        self.layout.addWidget(self.error_header)
        self.error_box = QWidget(self); form = QFormLayout(self.error_box)
        form.setContentsMargins(0, 4, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.layout.addWidget(self.error_box); self.error_box.hide()
        self.mode = QComboBox(self)
        for text, value in [(tr('ui.caption.0a7b03b27b53'), 'legacy'), (tr('ui.widget.stop_3396'), 'stop'), (tr('common.retry'), 'retry'), (tr('ui.caption.35716d53aa28'), 'skip'), (tr('ui.caption.c380c7a448eb'), 'branch')]: add_caption_item(self.mode, text, value)
        self.retries = QSpinBox(self); self.retries.setRange(0, 100)
        self.delay = QDoubleSpinBox(self); self.delay.setRange(0, 3600); bind_caption(self.delay, tr('ui.caption.ddae8eb8bf21'), setter='setSuffix')
        self.exhausted = QComboBox(self)
        for text, value in [(tr('ui.widget.stop_3396'), 'stop'), (tr('ui.caption.35716d53aa28'), 'skip'), (tr('ui.caption.c380c7a448eb'), 'branch')]: add_caption_item(self.exhausted, text, value)
        for control in (self.mode, self.retries, self.delay, self.exhausted):
            control.setFixedHeight(32)
            control.setMinimumWidth(130)
            control.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        add_row(form, 'ui.execution_settings.on_error_5acf', self.mode); add_row(form, 'ui.execution_settings.retries_6604', self.retries)
        add_row(form, 'ui.execution_settings.delay_b051', self.delay); add_row(form, 'ui.execution_settings.after_retries_0e34', self.exhausted)
        for control in (self.mode, self.exhausted): control.currentIndexChanged.connect(self.policy_changed)
        self.retries.valueChanged.connect(self.policy_changed); self.delay.valueChanged.connect(self.policy_changed)

    def _toggle_errors(self, expanded):
        self._error_expanded = bool(expanded)
        set_icon(self.error_header, ('down' if expanded else 'right'), size=16, icon_only=False)
        self.error_box.setVisible(expanded)

    def changed(self):
        if self.loading or not self.node: return
        self.node.normalize_dynamic_ports()
        self.node.update()
        self.inspector.modified.emit()

    def policy_changed(self, *_):
        if self.loading or not self.node: return
        settings=dict(self.node.settings)
        if self.mode.currentData() == 'legacy': settings.pop('_error_policy', None)
        else: settings['_error_policy'] = {'mode': self.mode.currentData(), 'retries': self.retries.value(),
            'delay': self.delay.value(), 'exhausted': self.exhausted.currentData()}
        view=self.node._pipeline_view()
        if view is not None:view.change_node_settings(self.node,settings)
        else:self.node.settings=settings
        retry = self.mode.currentData() == 'retry'
        for control in (self.retries, self.delay, self.exhausted): control.setEnabled(retry)
        self.changed()

    def set_node(self, node):
        self.loading = True; self.node = node
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget(): item.widget().hide(); item.widget().deleteLater()
        if node is None: self.hide(); self.loading = False; return
        s, kind = node.settings, node.spec.type_id
        executable = any(p.stream == 'exec' for p in node.spec.inputs)
        owned = bool(s.get('_parallel_parent') or getattr(node, '_parallel_chain', False))
        self.error_header.setVisible(executable)
        self.error_box.setVisible(executable and self._error_expanded)
        config = s.get('_error_policy') or {'mode': 'legacy'}
        self.mode.setCurrentIndex(max(0, self.mode.findData(config.get('mode', 'stop'))))
        self.retries.setValue(config.get('retries', 1)); self.delay.setValue(config.get('delay', 0))
        self.exhausted.setCurrentIndex(max(0, self.exhausted.findData(config.get('exhausted', 'stop'))))
        self.mode.model().item(self.mode.findData('branch')).setEnabled(not owned)
        self.exhausted.model().item(self.exhausted.findData('branch')).setEnabled(not owned)
        for control in (self.retries, self.delay, self.exhausted): control.setEnabled(config.get('mode') == 'retry')
        if kind == NOTIFICATION:
            button = QPushButton(str(s.get('color', '#5b8def')), self)
            def color():
                chosen = QColorDialog.getColor(QColor(s.get('color', '#5b8def')), self)
                if chosen.isValid(): bind_caption(button, chosen.name()); self.assign('color', chosen.name())
            button.clicked.connect(color); add_row(self.form, 'ui.module_view.color_92ab', button)
        if kind == PARALLEL:
            execution_mode = QComboBox(self)
            execution_mode.setObjectName('ParallelExecutionMode')
            add_item(execution_mode, 'ui.execution_settings.concurrent_request_1d21', 'concurrent')
            add_item(execution_mode, 'ui.execution_settings.sequential_request_a222', 'sequential')
            set_text(execution_mode, 'ui.execution_settings.the_model_plans_the_whole_batch_a68e', setter='setToolTip')
            execution_mode.setCurrentIndex(max(0, execution_mode.findData(s.get('_parallel_mode', 'concurrent'))))
            execution_mode.currentIndexChanged.connect(lambda: self.assign('_parallel_mode', execution_mode.currentData()))
            add_row(self.form, 'ui.execution_settings.execution_2a51', execution_mode)
            layout_mode = QComboBox(self)
            add_item(layout_mode, 'ui.execution_settings.column_bce6', 'column'); add_item(layout_mode, 'ui.execution_settings.row_d06d', 'row')
            layout_mode.setCurrentIndex(max(0, layout_mode.findData(s.get('_parallel_layout', 'column'))))
            def arrange():
                self.assign('_parallel_layout', layout_mode.currentData()); node.arrange()
            layout_mode.currentIndexChanged.connect(arrange)
            add_row(self.form, 'ui.execution_settings.layout_a70a', layout_mode)
            from .parallel_tools_editor import ParallelToolsEditor
            add_row(self.form, 'ui.execution_settings.tools_4e90', ParallelToolsEditor(node, self))
            button = set_icon(QPushButton('', self), 'add', square=True)
            button.setFixedSize(28, 28)
            set_text(button, 'ui.execution_settings.add_tool_5d2c', setter='setToolTip')
            button.clicked.connect(lambda: node.open_tool_menu())
            self.form.addRow(button)
        if s.get('_parallel_parent'):
            limit = QSpinBox(self); limit.setRange(0, 10000); bind_caption(limit, tr('ui.caption.6f92d366bce1'), setter='setSpecialValueText')
            limit.setValue(int(s.get('_parallel_max_calls', 1)))
            limit.valueChanged.connect(lambda value: self.assign('_parallel_max_calls', value))
            add_row(self.form, 'ui.execution_settings.calls_per_visit_f807', limit)
        if kind == 'graph.loop':
            retention = QComboBox(self)
            for text, value in [(tr('ui.caption.d8913929652f'), 'all'), (tr('ui.caption.c09b10c85115'), 'results'), (tr('ui.caption.6adb89ee2ea6'), 'last')]: add_caption_item(retention, text, value)
            retention.setCurrentIndex(max(0, retention.findData(s.get('context_retention', 'all'))))
            retention.currentIndexChanged.connect(lambda: self.assign('context_retention', retention.currentData()))
            add_row(self.form, 'ui.execution_settings.iteration_context_0a41', retention)
        if kind == TRANSFORM: self.transforms(s)
        self.setVisible(executable or self.form.count() > 0)
        self.loading = False

    def assign(self, key, value):
        if self.loading or not self.node: return
        self.node.settings[key] = value; self.changed()

    def transforms(self, settings):
        from .transform_settings import TransformSettings
        panel = TransformSettings(self.node, self.changed, self)
        self.form.addRow(panel)
