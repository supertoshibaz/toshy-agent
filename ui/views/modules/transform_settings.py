"""Operation-specific forms and a local preview for deterministic transforms."""
from __future__ import annotations
from ui.i18n import set_caption_table_headers, bind_caption, add_caption_item, add_caption_items, tr, localize, set_text, add_item, add_row
from ui.styles.binding import apply_style, form_field
from ui.icons import set_icon

import copy
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,
    QLineEdit,QPlainTextEdit,QPushButton,QSpinBox,QCheckBox,QListWidget,QTableWidget,
    QTableWidgetItem,QHeaderView,QToolButton)
from ui.core_bridge.async_runner import AsyncRunner
from modules.pipelines.transform import OPERATIONS,apply

DEFAULTS = {
    'get':{'path':''}, 'select':{'fields':[]}, 'rename':{'fields':{}},
    'filter':{'path':'','comparison':'eq','value':''}, 'sort':{'path':'','reverse':False},
    'unique':{'path':''}, 'replace':{'old':'','new':'','count':-1},
    'regex':{'pattern':''}, 'split':{'separator':'\n'}, 'join':{'separator':'\n'},
    'date':{'input_format':'iso','output_format':'iso','input_timezone':'UTC','timezone':'UTC'},
    'convert':{'type':'string'}, 'json_dump':{'indent':2},
}
HELP = {
    'get':tr('ui.caption.93e3c11802ab'),
    'select':tr('ui.caption.174c052ebdb2'),
    'rename':tr('ui.caption.8fbb86cf1ea1'),
    'filter':tr('ui.caption.c7002b650211'),
    'sort':tr('ui.caption.616e9e03411a'),
    'unique':tr('ui.caption.f07b43e6949b'),
    'date':tr('ui.caption.c28548e90ccf'),
    'json_parse':tr('ui.caption.60f4a6bbda47'),
    'json_dump':tr('ui.caption.05690197f484'),
    'replace':tr('ui.caption.9f0fa7b77077'),
    'regex':tr('ui.caption.853b0e40315c'),
    'split':tr('ui.caption.9547f6c8eba6'),
    'join':tr('ui.caption.e7a340dbc021'),
    'strip_markdown':tr('ui.caption.ca985de0ee71'),
    'convert':tr('ui.caption.ebfa62f1936f'),
}


class TransformSettings(QWidget):
    def __init__(self,node,changed,parent=None):
        super().__init__(parent)
        self.node,self.changed,self._building = node,changed,True
        self.ops = node.settings.setdefault('operations',[{'op':'get','path':''}])
        self.runner = AsyncRunner(self,max_threads=1)
        self.box = QVBoxLayout(self);self.box.setContentsMargins(0,0,0,0)
        self.box.addWidget(self.label(tr('ui.caption.fa1488d509bd')))
        self.steps = QListWidget(self);self.steps.setMaximumHeight(110);self.box.addWidget(self.steps)
        buttons=QHBoxLayout()
        for title,callback in [('+',self.add),('−',self.remove),('↑',lambda:self.move(-1)),('↓',lambda:self.move(1))]:
            button=bind_caption(QPushButton('',self), title);button.setFixedSize(28,28);button.clicked.connect(callback);buttons.addWidget(button)
        buttons.addStretch();self.box.addLayout(buttons)
        self.kind=QComboBox(self)
        for value,title in OPERATIONS.items():add_caption_item(self.kind, title, value)
        self.box.addWidget(self.kind)
        self.help=self.label('');self.box.addWidget(self.help)
        self.parse=localize(QCheckBox('',self), 'ui.transform_settings.parse_json_when_the_input_is_9466')
        self.parse.setChecked(bool(node.settings.get('parse_json_input',True)))
        set_text(self.parse, 'ui.transform_settings.for_field_extraction_and_array_processing_190b', setter='setToolTip')
        self.box.addWidget(self.parse)
        self.fields=QWidget(self);self.form=QFormLayout(self.fields)
        self.form.setContentsMargins(0,0,0,0);self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows);self.box.addWidget(self.fields)
        self.error=self.label('');self.error.setStyleSheet('color:#f0b85a;');self.error.hide();self.box.addWidget(self.error)
        self.preview_header=QToolButton(self);set_text(self.preview_header, 'ui.transform_settings.test_with_an_example_aa54')
        self.preview_header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon);set_icon(self.preview_header, 'right', size=16, icon_only=False)
        self.preview_header.setCheckable(True);apply_style(self.preview_header, 'QToolButton {color:@text.primary@;background:@background.surface@;border:1px solid @border.subtle@;border-radius:4px;padding:6px;}');self.box.addWidget(self.preview_header)
        self.preview=QWidget(self);preview=QVBoxLayout(self.preview);preview.setContentsMargins(0,0,0,0)
        preview.addWidget(self.label(tr('ui.caption.dad00cb28c88')))
        self.sample_type=QComboBox(self.preview);add_item(self.sample_type, 'ui.transform_settings.text_input_d798', 'text');add_item(self.sample_type, 'ui.transform_settings.json_value_object_2aab', 'json');preview.addWidget(self.sample_type)
        self.sample=form_field(QPlainTextEdit(self.preview));self.sample.setMaximumHeight(100)
        self.sample.setPlainText('{"user":{"name":"Тоши"}}');preview.addWidget(self.sample)
        self.test_button=localize(QPushButton('',self.preview), 'ui.transform_settings.test_e442');self.test_button.clicked.connect(self.test);preview.addWidget(self.test_button)
        self.result=form_field(QPlainTextEdit(self.preview));self.result.setReadOnly(True);self.result.setMaximumHeight(130)
        set_text(self.result, 'ui.transform_settings.result_or_a_clear_error_f879', setter='setPlaceholderText');preview.addWidget(self.result)
        self.preview.hide();self.box.addWidget(self.preview)
        self.preview_header.toggled.connect(self.show_preview)
        self.steps.currentRowChanged.connect(self.render)
        self.kind.currentIndexChanged.connect(self.switch)
        self.parse.toggled.connect(self.set_parse)
        self.refresh(0);self._building=False

    def label(self,text):
        label=bind_caption(QLabel('',self), text);label.setWordWrap(True);label.setTextFormat(Qt.PlainText);return label

    def show_preview(self,expanded):
        set_icon(self.preview_header, ('down' if expanded else 'right'), size=16, icon_only=False);self.preview.setVisible(expanded)

    def refresh(self,index):
        self.steps.blockSignals(True);self.steps.clear()
        add_caption_items(self.steps, [f'{i+1}. {OPERATIONS.get(op.get("op"),op.get("op","?"))}' for i,op in enumerate(self.ops)])
        self.steps.setCurrentRow(max(0,min(index,len(self.ops)-1)));self.steps.blockSignals(False)
        self.render()

    def render(self,*_):
        old=self._building;self._building=True
        while self.form.count():
            item=self.form.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        index=self.steps.currentRow()
        if index<0:self._building=old;return
        self.op=self.ops[index];kind=self.op.get('op','get')
        self.kind.blockSignals(True);self.kind.setCurrentIndex(max(0,self.kind.findData(kind)));self.kind.blockSignals(False)
        bind_caption(self.help, HELP.get(kind,''))
        self.parse.setVisible(self.ops[0].get('op') in {'get','select','rename','filter','sort','unique'})
        self.error.hide()
        if kind in {'get','filter','sort','unique'}:self.text(tr('ui.caption.2fbd95952ec7'),'path',tr('ui.caption.ecb4094c4bbf'))
        if kind=='select':
            editor=form_field(QPlainTextEdit(self));editor.setMaximumHeight(100);editor.setPlainText('\n'.join(self.op.get('fields',[])))
            set_text(editor, 'ui.transform_settings.name_date_user_email_9be8', setter='setPlaceholderText');add_row(self.form, 'ui.transform_settings.fields_paths_8691', editor)
            editor.textChanged.connect(lambda:self.set('fields',[s.strip() for s in editor.toPlainText().splitlines() if s.strip()]))
        elif kind=='rename':self.rename_fields()
        elif kind=='filter':
            self.combo(tr('ui.caption.64d7a667bf1b'),'comparison',[(tr('ui.caption.00b5be0e2e64'),'eq'),(tr('ui.caption.ae2a16c83946'),'ne'),(tr('ui.caption.d2e6a34667b5'),'contains'),(tr('ui.caption.458db18cf6df'),'gt'),(tr('ui.caption.039d1d9bd44e'),'lt')])
            self.filter_value()
        elif kind=='sort':self.check(tr('ui.caption.60a8d7e8462b'),'reverse')
        elif kind=='replace':
            self.text(tr('ui.caption.be6156b8e4de'),'old');self.text(tr('ui.caption.6e4964d15bac'),'new',multiline=True)
            self.number(tr('ui.caption.64bfe83c4ac4'),'count',-1,100000,tr('ui.search_popup.all_2158'))
        elif kind=='regex':
            self.text(tr('ui.plugin_manager_pages.template_fff8'),'pattern');self.check(tr('ui.caption.d7e1d0aa2963'),'ignore_case')
            mode=QComboBox(self);add_item(mode, 'ui.transform_settings.find_matches_8a1c', False);add_item(mode, 'ui.transform_settings.replace_c201', True)
            mode.setCurrentIndex(1 if 'replacement' in self.op else 0);add_row(self.form, 'ui.transform_settings.action_6b53', mode)
            def change_mode():
                if self._building:return
                if mode.currentData():self.op.setdefault('replacement','')
                else:self.op.pop('replacement',None)
                self.changed();self.render()
            mode.currentIndexChanged.connect(change_mode)
            if 'replacement' in self.op:self.text(tr('ui.caption.6e4964d15bac'),'replacement',multiline=True)
        elif kind in {'split','join'}:self.text(tr('ui.caption.0b25285709bc'),'separator',tr('ui.caption.c0531b1ecb12'),multiline=True)
        elif kind=='date':
            formats=[('ISO 8601','iso'),(tr('ui.caption.5cb3e5025f75'),'timestamp'),(tr('ui.caption.704c1f4c5b02'),'timestamp_ms'),('%Y-%m-%d','%Y-%m-%d'),('%d.%m.%Y','%d.%m.%Y'),('%Y-%m-%d %H:%M:%S','%Y-%m-%d %H:%M:%S')]
            self.combo(tr('ui.caption.6052f8fab353'),'input_format',formats,True)
            self.combo(tr('ui.caption.fba2410ce365'),'output_format',formats,True)
            zones=[(x,x) for x in ('UTC','Europe/Moscow','Europe/Berlin','America/New_York')]
            self.combo(tr('ui.caption.536b84f27de4'),'input_timezone',zones,True)
            self.combo(tr('ui.caption.642753ebcf7b'),'timezone',zones,True)
        elif kind=='convert':self.combo(tr('ui.caption.8cd1b88010cf'),'type',[(x,y) for x,y in [(tr('ui.caption.3091aca226ef'),'string'),(tr('ui.caption.1b6d957febdc'),'boolean'),(tr('ui.caption.6dc8ea379038'),'integer'),(tr('ui.caption.e605178e518a'),'float'),(tr('ui.caption.cc03a3164ef4'),'array'),(tr('ui.caption.f2f87306b653'),'object')]])
        elif kind=='json_dump':self.combo(tr('ui.caption.eed9a1830481'),'indent',[(tr('ui.caption.3a3da9f0b305'),None),('0',0),(tr('ui.caption.b11e9802ee19'),2),(tr('ui.caption.de030da96932'),4)])
        self._building=old

    def set(self,key,value):
        if self._building:return
        self.op[key]=value;self.error.hide();self.changed()

    def set_parse(self,value):
        if self._building:return
        self.node.settings['parse_json_input']=value;self.changed()

    def text(self,label,key,placeholder='',multiline=False):
        editor=QPlainTextEdit(self) if multiline else QLineEdit(self)
        value=str(self.op.get(key,''))
        if multiline:editor.setMaximumHeight(70);editor.setPlainText(value)
        else:bind_caption(editor, value);editor.setFixedHeight(32)
        bind_caption(editor, placeholder, setter='setPlaceholderText');self.form.addRow(label,editor)
        editor.textChanged.connect(lambda:self.set(key,editor.toPlainText() if multiline else editor.text()))
        return editor

    def combo(self,label,key,items,editable=False):
        control=QComboBox(self);control.setEditable(editable);control.setFixedHeight(32)
        for title,value in items:add_caption_item(control, title, value)
        value=self.op.get(key,items[0][1]);index=control.findData(value)
        if index<0:control.addItem(str(value),value);index=control.count()-1
        control.setCurrentIndex(index);self.form.addRow(label,control)
        def update():
            found=control.findText(control.currentText())
            self.set(key,control.itemData(found) if found>=0 else control.currentText())
        if editable:control.currentTextChanged.connect(update)
        else:control.currentIndexChanged.connect(update)
        return control

    def check(self,label,key):
        control=bind_caption(QCheckBox('',self), label);control.setChecked(bool(self.op.get(key,False)))
        self.form.addRow(control);control.toggled.connect(lambda value:self.set(key,value));return control

    def number(self,label,key,minimum,maximum,special=''):
        control=QSpinBox(self);control.setRange(minimum,maximum);control.setValue(int(self.op.get(key,minimum)))
        control.setFixedHeight(32)
        if special:bind_caption(control, special, setter='setSpecialValueText')
        self.form.addRow(label,control);control.valueChanged.connect(lambda value:self.set(key,value))

    def filter_value(self):
        value=self.op.get('value','');kind='null' if value is None else 'boolean' if isinstance(value,bool) else 'number' if isinstance(value,(int,float)) else 'json' if isinstance(value,(dict,list)) else 'string'
        control=QComboBox(self)
        for title,key in [(tr('ui.caption.3091aca226ef'),'string'),(tr('ui.caption.8adacec63cb6'),'number'),(tr('ui.caption.1b6d957febdc'),'boolean'),(tr('ui.caption.1d91c53c5e27'),'null'),(tr('ui.caption.bbe404791988'),'json')]:add_caption_item(control, title, key)
        control.setCurrentIndex(control.findData(kind));add_row(self.form, 'ui.transform_settings.value_type_fa86', control)
        def switch():
            if self._building:return
            self.set('value',{'string':'','number':0,'boolean':False,'null':None,'json':{}}[control.currentData()]);self.render()
        control.currentIndexChanged.connect(switch)
        if kind=='boolean':self.combo(tr('ui.transform_settings.value_f869'),'value',[(tr('common.yes'),True),(tr('common.no'),False)])
        elif kind!='null':
            edit=QLineEdit(value if kind=='string' else json.dumps(value,ensure_ascii=False),self);edit.setFixedHeight(32);add_row(self.form, 'ui.transform_settings.value_f869', edit)
            def update():
                try:
                    result=edit.text() if kind=='string' else json.loads(edit.text())
                    if kind=='number' and (isinstance(result,bool) or not isinstance(result,(int,float))):raise ValueError('Ожидается число')
                    self.set('value',result)
                except ValueError as error:bind_caption(self.error, str(error));self.error.show()
            edit.textChanged.connect(update)

    def rename_fields(self):
        table=QTableWidget(self);table.setColumnCount(2);set_caption_table_headers(table, [tr('ui.caption.532593432c0f'),tr('ui.caption.121601ce3f85')])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);table.verticalHeader().hide();table.setMaximumHeight(140)
        fields=self.op.get('fields',{});table.setRowCount(max(1,len(fields)))
        for row,(old,new) in enumerate(fields.items()):
            table.setItem(row,0,QTableWidgetItem(str(old)));table.setItem(row,1,QTableWidgetItem(str(new)))
        def update():
            values={}
            for row in range(table.rowCount()):
                old=table.item(row,0).text() if table.item(row,0) else ''
                new=table.item(row,1).text() if table.item(row,1) else ''
                if old or new:values[old]=new
            self.set('fields',values)
        table.cellChanged.connect(update);self.form.addRow(table)
        actions=QWidget(self);row=QHBoxLayout(actions);row.setContentsMargins(0,0,0,0)
        add=localize(QPushButton('',actions), 'ui.transform_settings.add_field_a73e');add.clicked.connect(lambda:table.insertRow(table.rowCount()))
        delete=localize(QPushButton('',actions), 'ui.transform_settings.remove_field_5260')
        def remove():
            if table.currentRow()>=0:table.removeRow(table.currentRow());update()
        delete.clicked.connect(remove);row.addWidget(add);row.addWidget(delete);self.form.addRow(actions)

    def switch(self,*_):
        if self._building:return
        key=self.kind.currentData();index=self.steps.currentRow()
        self.ops[index]=dict(copy.deepcopy(DEFAULTS.get(key,{})),op=key);self.changed();self.refresh(index)

    def add(self):self.ops.append({'op':'get','path':''});self.changed();self.refresh(len(self.ops)-1)
    def remove(self):
        index=self.steps.currentRow()
        if index>=0 and len(self.ops)>1:self.ops.pop(index);self.changed();self.refresh(index)
    def move(self,delta):
        index=self.steps.currentRow();target=index+delta
        if 0<=target<len(self.ops):self.ops[index],self.ops[target]=self.ops[target],self.ops[index];self.changed();self.refresh(target)

    def test(self):
        raw=self.sample.toPlainText();ops=copy.deepcopy(self.ops);parse=bool(self.node.settings.get('parse_json_input',True))
        self.test_button.setEnabled(False);self.result.setPlainText(tr('ui.caption.638f8008a173'))
        def success(value):
            self.test_button.setEnabled(True)
            self.result.setPlainText(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))
        def error(value):self.test_button.setEnabled(True);self.result.setPlainText(tr('ui.caption.37eceb9f52a5')+str(value))
        input_type=self.sample_type.currentData()
        def calculate():
            value=json.loads(raw) if input_type=='json' else raw
            return apply(value,ops,parse_json_input=parse)
        self.runner.submit(calculate,success,error)
