"""Add multi-component authoring to the plugin overlay without altering its library."""
from ui.i18n import bind_caption, add_caption_item, tr, localize, set_text, add_item

import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QFileDialog)
from ui.widgets.dialogs import InputDialog as QInputDialog

from core.plugins.manifest import PluginError


TEMPLATES = (
    (tr('ui.caption.afaa863ccd28'), None),
    (tr('ui.caption.af28df96a997'), 'custom-module'),
    (tr('ui.caption.bffa3c8b1b0f'), 'system-module'),
    (tr('ui.caption.aa7b9fdf081c'), 'widget'),
    (tr('ui.caption.4820f03bad81'), 'workspace'),
    (tr('ui.caption.7212e4cc2e3d'), 'topbar'),
    (tr('nav.core'), 'core'),
    (tr('ui.caption.4404a996b430'), 'pipeline-nodes'),
)


def install_authoring_ui(panel):
    page = panel.tabs.widget(2)
    panel.template_kind.clear()
    for label, kind in TEMPLATES:
        add_caption_item(panel.template_kind, label, (kind, 'add', None, None))
    set_text(panel.template_kind, 'ui.plugin_authoring.initial_scaffold_or_the_next_component_31d4', setter='setToolTip')
    row = QHBoxLayout()
    panel.component_label = localize(QLabel('', page), 'ui.plugin_authoring.components_1e84')
    row.addWidget(panel.component_label)
    panel.component_list = QComboBox(page)
    panel.component_list.setMinimumContentsLength(12)
    panel.component_list.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    row.addWidget(panel.component_list, 1)
    panel.add_component_button = localize(QPushButton('', page), 'ui.plugin_authoring.add_component_c144')
    set_text(panel.add_component_button, 'ui.plugin_authoring.add_the_selected_type_to_the_13bf', setter='setToolTip')
    row.addWidget(panel.add_component_button)
    page.layout().insertLayout(1, row)
    panel.source_edit.textChanged.connect(lambda _: refresh_components(panel))
    panel.add_component_button.clicked.connect(lambda: add_component(panel))
    from ui.plugin_authoring_help import install_authoring_help
    install_authoring_help(panel)
    refresh_components(panel)


def refresh_components(panel):
    panel.component_list.clear()
    try:
        root = Path(panel.source_edit.text()).expanduser()
        data = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
        values = data.get('components', [])
        set_text(panel.component_label, 'ui.plugin_authoring.components_v0_5632', v0=len(values))
        for component in values:
            text = f"{component['id']} · {component['type']}"
            if component.get('module_name'): text += ' · ' + component['module_name']
            add_caption_item(panel.component_list, text, component['id'])
        if not values: add_item(panel.component_list, 'ui.plugin_authoring.empty_scaffold_397d')
        bind_caption(panel.component_list, tr('ui.caption.31e47815abd7') + str(data.get('id', '')) + '\n' + '\n'.join(
            f"{c['id']}: {c.get('entrypoint') or c.get('path', '')}" for c in values), setter='setToolTip')
        panel.add_component_button.setEnabled(not panel._busy and not (root / '.toshy-receipt.json').exists())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        set_text(panel.component_label, 'ui.plugin_authoring.components_1e84')
        add_item(panel.component_list, 'ui.plugin_authoring.select_a_plugin_folder_first_bfac')
        panel.add_component_button.setEnabled(False)


def add_component(panel):
    if panel._busy: return
    kind = panel.template_kind.currentData()[0]
    if kind is None:
        panel.error_message(PluginError(tr('ui.caption.8eda77887f70'))); return
    source = Path(panel.source_edit.text()).expanduser()
    try:
        manifest = json.loads((source / 'plugin.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        panel.error_message(error); return
    existing = {c['id'] for c in manifest.get('components', [])}
    prefix = kind.replace('-', '_'); index = 1
    while f'{prefix}_{index}' in existing: index += 1
    component_id, accepted = QInputDialog.getText(panel, tr('ui.plugin_authoring.new_component_01b7'),
        tr('ui.plugin_authoring.component_id_within_this_plugin_lowercase_3090'), text=f'{prefix}_{index}')
    if not accepted: return
    module_source = module_name = None
    if kind in ('system-module', 'custom-module'):
        module_source = QFileDialog.getExistingDirectory(panel, 'Выберите исходный модуль')
        if not module_source: return
        module_name, accepted = QInputDialog.getText(panel, tr('ui.plugin_authoring.module_name_5bba'),
            tr('ui.plugin_authoring.unique_internal_module_name_python_identifier_e8b9'), text=Path(module_source).name)
        if not accepted: return
        module_name = module_name.strip()
    from core.plugins.authoring import append_component
    def done(_):
        refresh_components(panel)
        if Path(panel.source_edit.text()).expanduser() != source:
            panel.error_message(PluginError(tr('ui.caption.d552bf394379') + str(source))); return
        index = panel.component_list.findData(component_id.strip())
        if index >= 0: panel.component_list.setCurrentIndex(index)
        panel.preview_source()
    panel.run_job(lambda: append_component(source, component_id=component_id.strip(),
        kind=kind, module_source=module_source, module_name=module_name), done)
