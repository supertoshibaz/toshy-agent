"""Dismissible, transient construction warnings anchored above pipeline cards."""
from ui.icons import set_icon, symbol_button
from ui.i18n import bind_caption, tr, set_text

from types import SimpleNamespace
from PySide6.QtCore import QObject,QRectF,QPointF,Qt
from PySide6.QtGui import QFont,QFontMetricsF,QPainter,QColor,QPen
from PySide6.QtWidgets import QGraphicsObject
from shiboken6 import isValid
from modules.pipelines.topology import GraphIndex,parallel_data_issues,unfinished_exec,first_cycle
from modules.pipelines.contracts import is_data,identity,validate
from modules.pipelines.runtime import PipelineRunner,MISSING,typed


class CardWarning(QGraphicsObject):
    def __init__(self, manager, node, messages, keys):
        super().__init__()
        self.manager,self.node,self.keys = manager,node,keys
        self.text = '\n'.join(messages)
        self.width = max(240.,node.card_width())
        self.font = QFont();self.font.setPointSize(9)
        bounds=QFontMetricsF(self.font).boundingRect(QRectF(0,0,self.width-42,10000),Qt.TextWordWrap,self.text)
        self.height=max(42.,bounds.height()+20)
        self.setZValue(10000)
        self.setAcceptedMouseButtons(Qt.LeftButton)
        bind_caption(self, self.text, setter='setToolTip')
        self.reposition()

    def boundingRect(self):return QRectF(0,0,self.width,self.height)
    def close_rect(self):return QRectF(self.width-27,5,22,22)
    def reposition(self):
        if isValid(self.node):self.setPos(self.node.mapToScene(QPointF(0,0))+QPointF(0,-self.height-8))
    def paint(self,painter,option,widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#80651e'),1));painter.setBrush(QColor('#2d2512'))
        painter.drawRoundedRect(self.boundingRect(),5,5)
        painter.setFont(self.font);painter.setPen(QColor('#facc15'))
        painter.drawText(QRectF(10,10,self.width-42,self.height-20),Qt.TextWordWrap,self.text)
        r=self.close_rect().adjusted(7,7,-7,-7)
        painter.drawLine(r.topLeft(),r.bottomRight());painter.drawLine(r.topRight(),r.bottomLeft())
    def mousePressEvent(self,event):
        self.closing=self.close_rect().contains(event.pos())
        event.accept()
    def mouseMoveEvent(self,event):event.accept()
    def mouseReleaseEvent(self,event):
        event.accept()
        if getattr(self,'closing',False) and self.close_rect().contains(event.pos()):self.manager.dismiss(self.node.node_id,self.keys)


class PipelineWarnings(QObject):
    def __init__(self,view):
        super().__init__(view)
        self.view,self.panels,self.dismissed=view,{},set()
        # Reports are snapshots. Editing clears them; it never starts a check.
        view.modified.connect(self.clear)

    def clear(self,*_):
        for key in list(self.panels):self.remove(key)
        self.dismissed.clear()
        button=getattr(self.view,'check_graph_button',None)
        if button is not None:
            symbol_button(button, '✓');button.setStyleSheet('')
            set_text(button, 'ui.warnings.check_graph_for_errors_9dbd', setter='setToolTip')
    def remove(self,key):
        item=self.panels.pop(key,None)
        if item is not None and isValid(item):
            if item.scene() is not None:item.scene().removeItem(item)
            item.deleteLater()
    def dismiss(self,key,keys):self.dismissed.update(keys);self.remove(key)
    def reposition(self):
        for item in list(self.panels.values()):
            if isValid(item):item.reposition()

    def run_check(self):
        self.clear()
        from .nodes.base import PipelineNodeItem
        from .edge import PipelineEdgeItem
        nodes={n.node_id:n for n in self.view.canvas_scene.items() if isinstance(n,PipelineNodeItem)}
        edges=[e for e in self.view.canvas_scene.items() if isinstance(e,PipelineEdgeItem)]
        pipeline=self.view.build_pipeline_definition('');index=GraphIndex(pipeline)
        by_link={(e.source.node_id,e.source_port,e.target.node_id,e.target_port):e for e in edges}
        messages,active={},set()
        def add(node,key,message):
            active.add(key)
            if key not in self.dismissed:messages.setdefault(node,[]).append((key,message))
        for edge in edges:
            port=edge.target.port_spec('input',edge.target_port)
            if port is None or port.stream!='arg' or not port.schema_type:continue
            source=index.nodes.get(edge.source.node_id)
            if source is None or not is_data(source):continue
            # Pure data evaluation only: no model, tool or external file read.
            evaluator=object.__new__(PipelineRunner)
            evaluator.pipeline,evaluator.nodes,evaluator.state=pipeline,index.nodes,SimpleNamespace(values={})
            try:value=evaluator.value(source.node_id,edge.source_port)
            except (ValueError,TypeError,KeyError,RecursionError):continue
            if value is MISSING:continue
            try:typed(value,port.schema_type)
            except (ValueError,TypeError,OverflowError):
                add(edge.target.node_id,('type',edge.edge_id),tr('ui.caption.f5708c5a2757')+port.schema_type+tr('ui.caption.1a5bb19f6bcd'))
        try:
            for issue in parallel_data_issues(pipeline,index=index):
                inlet=issue['edge'];edge=by_link.get((inlet.source_id,inlet.source_port,inlet.target_id,inlet.target_port))
                if edge is not None:add(issue['node_id'],('parallel',edge.edge_id,issue['source_id']),issue['message'])
            for key,ports in unfinished_exec(pipeline,index=index).items():
                for port in ports:
                    spec=nodes[key].port_spec('output',port) if key in nodes else None
                    label=spec.label if spec is not None else port
                    add(key,('branch',key,port),tr('ui.caption.4232585562a9')+label+tr('ui.caption.e7e176373ac9'))
        except (ValueError,TypeError):pass
        # Validate against locally discovered schemas, without contacting the
        # kernel, model server, or executing any node.
        tools={}
        for spec in self.view.node_registry.specs():
            if spec.kind!='tool':continue
            from modules.pipelines.dsl import PipelineNodeDefinition
            try:_,name=identity(PipelineNodeDefinition('',spec.type_id,{}))
            except (ValueError,KeyError,TypeError):continue
            if not name:continue
            schema=spec.metadata.get('args_schema')
            if not schema:
                schema={'type':'object','properties':{p.arg_name or p.key.split(':',1)[-1]:{'type':p.schema_type or 'string'}
                    for p in spec.inputs if p.stream=='arg'}}
            tools[name]=SimpleNamespace(args_schema=schema)
        resolve=None
        directory=getattr(self.view,'pipeline_dir',None)
        if directory is not None and directory.parent.parent.parent.name=='custom':
            from modules.pipelines.packages import PackageStore
            from modules.pipelines.storage import load_pipeline_script
            store=PackageStore();module=directory.parent.parent.name
            def resolve(ref):
                entry=store.read(module)['pipelines'].get(ref)
                if not entry or entry.get('mode')!='internal':raise ValueError('Nested Pipeline должен ссылаться на внутренний pipeline того же модуля')
                return load_pipeline_script(store.resolve(module,ref)/'pipeline.py',migrate=False)
        errors,_,live=validate(pipeline,tools=tools,resolve_call=resolve)
        cycle_errors=set()
        for data,text in [(True,tr('ui.caption.ff459852503f')),
                          (False,tr('ui.caption.67255a9d4d3e'))]:
            cycle=first_cycle(index.adjacency(live,data=data,omit_repeat=not data))
            if cycle:
                for key in set(cycle):add(key,('cycle',data,key),text)
                cycle_errors.add(tr('ui.caption.26ac2cf8f2c2') if data else tr('ui.caption.375ba83935e9'))
        fallback=next((key for key,n in nodes.items() if n.spec.type_id=='graph.start'),next(iter(nodes),None))
        for error in errors:
            if any(error.startswith(prefix) for prefix in cycle_errors):continue
            key,separator,text=error.partition(': ')
            if key not in nodes:key,text=fallback,error
            if key is not None:add(key,('validation',error),text)
        self.dismissed.intersection_update(active)
        for key in set(self.panels)-set(messages):self.remove(key)
        for key,items in messages.items():
            if key not in nodes:continue
            keys=frozenset(x[0] for x in items);texts=list(dict.fromkeys(x[1] for x in items))
            old=self.panels.get(key)
            if old is not None and old.keys==keys and old.text=='\n'.join(texts):old.reposition();continue
            self.remove(key)
            item=CardWarning(self,nodes[key],texts,keys);self.panels[key]=item;self.view.canvas_scene.addItem(item)
        count=sum(len(set(text for _,text in items)) for items in messages.values())
        button=getattr(self.view,'check_graph_button',None)
        if button is not None:
            set_icon(button, ('warning' if count or errors else 'check'), square=True)
            button.setStyleSheet('color:#facc15;' if count or errors else 'color:#4ade80;')
            bind_caption(button, tr('ui.caption.afa2b09c761e') if not count and not errors
                              else tr('ui.caption.3b77e92f059c', v0=count or len(errors)), setter='setToolTip')
        return count
