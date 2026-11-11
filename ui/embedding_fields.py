"""OpenAI-compatible model settings (local servers and cloud services) for the Memory page."""
from ui.i18n import bind_caption, tr
from PySide6.QtWidgets import QLineEdit, QSpinBox
from core.embeddings import REMOTE_PROVIDER


class EmbeddingModelFields:
    def __init__(self, page):
        from ui.views.startup_view import SettingRow
        self.page, self.fields, self.rows = page, {}, {}
        definitions = [
            ('remote','api_url',tr('ui.caption.ded50799fd57'),tr('ui.caption.7949e2d79777'), 'text'),
            ('remote','api_key','API key',tr('ui.caption.998d6f2a7dfb'), 'secret'),
            ('remote','model_name',tr('ui.caption.2105a384c025'),tr('ui.caption.bab424748c48'), 'text'),
            ('remote','dimension',tr('ui.caption.75cc82d14e4d'),tr('ui.caption.37b51894308d'), 'number'),
        ]
        for group, key, title, explanation, kind in definitions:
            if kind == 'number':
                field = QSpinBox(page.rows_host); field.setRange(1, 65536)
            else:
                field = QLineEdit(page.rows_host)
                if kind == 'secret':field.setEchoMode(QLineEdit.Password)
            field.setMinimumWidth(260)
            row = SettingRow(title, explanation, field, search_terms=('embedding model модель '+group), parent=page.rows_host)
            page.add_row(row)
            self.fields[group,key], self.rows[group,key] = field, row
            field.editingFinished.connect(lambda *_, g=group:self.save(g))

    def reload(self, config, provider):
        defaults = {'remote':dict(api_url=config.get('api_url','http://localhost:8081/v1'),api_key=config.get('api_key',''),model_name=config.get('model_name','embedding-model'),dimension=config.get('dimension',768))}
        for (group,key),field in self.fields.items():
            value=(config.get(group) or {}).get(key, defaults[group][key])
            if isinstance(field,QSpinBox):field.setValue(int(value))
            else:bind_caption(field, str(value))
            self.rows[group,key].setVisible(provider==REMOTE_PROVIDER)

    def save(self, group):
        if self.page._loading:return
        values={}
        for (g,key),field in self.fields.items():
            if g!=group:continue
            values[key]=(field.value() if isinstance(field,QSpinBox) else field.text().strip())
        def apply(config):
            data=dict(config.get(group) or {});data.update(values);config[group]=data
        try:
            self.page._update_embedding(apply)
            self.page.reload_from_config()
        except Exception as error:self.page._show_write_error(error)
