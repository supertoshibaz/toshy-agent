"""Authoring field hints and the shared read-only documentation entry point."""
from ui.i18n import bind_caption, tr, localize, set_text

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QPushButton


FIELD_HELP = {
    'source_edit': (
        tr('ui.caption.e83207b4812f')
    ),
    'template_kind': (
        tr('ui.caption.af9d737a82ca')
    ),
    'plugin_id': (
        tr('ui.caption.52a09eb0438b')
    ),
    'plugin_name': (
        tr('ui.caption.0ac6c747e532')
    ),
    'plugin_author': tr('ui.caption.8b7bee1c930c'),
    'plugin_description': (
        tr('ui.caption.ce4a69b821fe')
    ),
    'image_edit': (
        tr('ui.caption.e8820364b8a2')
    ),
}


def show_authoring_help(panel):
    from core.paths import APP_PATHS
    from ui.widgets.inline_editor import notice

    path = APP_PATHS.root / 'ui' / 'docs' / 'plugin-docs.md'
    if not panel.host_window._open_builtin_doc('plugin-docs.md'):
        notice(panel, tr('ui.plugin_authoring_help.documentation_unavailable_1887'),
               tr('ui.plugin_authoring_help.could_not_read_the_documentation_place_2b5a', v0=path))
        return
    notice(panel, tr('ui.plugin_authoring_help.documentation_opened_7a8f'),
           tr('ui.plugin_authoring_help.plugin_docs_md_is_open_in_cbd5'))


def install_authoring_help(panel):
    page = panel.tabs.widget(2)
    form = next((page.layout().itemAt(i).layout() for i in range(page.layout().count())
                 if isinstance(page.layout().itemAt(i).layout(), QFormLayout)), None)
    for name, explanation in FIELD_HELP.items():
        field = getattr(panel, name)
        bind_caption(field, explanation, setter='setToolTip')
        if form is not None:
            label = form.labelForField(field)
            if label is not None:
                bind_caption(label, explanation, setter='setToolTip')
                if name == 'plugin_id':
                    set_text(label, 'ui.plugin_authoring_help.plugin_id_9958')
    set_text(panel.component_label, 'ui.plugin_authoring_help.parts_of_the_selected_plugin_such_a484', setter='setToolTip')
    header = QHBoxLayout()
    hint = localize(QLabel('', page), 'ui.plugin_authoring_help.id_name_and_author_are_used_7838')
    hint.setWordWrap(True)
    header.addWidget(hint, 1)
    panel.authoring_help_button = localize(QPushButton('', page), 'ui.plugin_authoring_help.documentation_b123')
    set_text(panel.authoring_help_button, 'ui.plugin_authoring_help.open_ui_docs_plugin_docs_md_2c19', setter='setToolTip')
    panel.authoring_help_button.clicked.connect(lambda: show_authoring_help(panel))
    header.addWidget(panel.authoring_help_button, 0, Qt.AlignTop)
    page.layout().insertLayout(0, header)
