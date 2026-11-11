"""Editable vector text objects. Sparse layer tiles are their rendering cache."""
import copy
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QTextDocument, QTransform, QPainter


def document(item):
    doc = QTextDocument()
    font = QFont(item.get('font', 'Sans Serif'));font.setPixelSize(24)
    doc.setDefaultFont(font);doc.setDocumentMargin(0)
    doc.setHtml(item.get('html',''))
    doc.setTextWidth(max(16.,min(32768.,float(item.get('width',420)))))
    return doc


def transform(item):
    return QTransform(*item.get('matrix',[1,0,0,1,0,0]))


def bounds(item):
    doc=document(item)
    return transform(item).mapRect(QRectF(0,0,doc.size().width(),
        max(doc.size().height(), float(item.get('height', 0)))))


def rebuild(db,meta,action,layer,cancel):
    from . import raster
    from .storage import _check
    coords=set()
    for x,y in list(db.execute('SELECT x,y FROM layer_tiles WHERE layer=?',(layer['id'],))):
        _check(cancel);raster._write(db,action,layer['id'],x,y,raster.blank());coords.add((x,y))
    for item in layer.get('objects',[]):
        _check(cancel)
        doc=document(item);matrix=transform(item)
        rect=matrix.mapRect(QRectF(0,0,doc.size().width(),doc.size().height())).adjusted(-2,-2,2,2).toAlignedRect()
        def draw(p,d=doc,m=matrix):
            p.save();p.setRenderHint(QPainter.Antialiasing);p.setTransform(m,True);d.drawContents(p);p.restore()
        coords |= raster._paint(db,meta,action,layer['id'],rect,draw,None,cancel)
    return coords


def edit(db,meta,action,payload,cancel):
    layer=next((x for x in meta['layers'] if x['id']==payload['layer']),None)
    if layer is None and payload.get('new_layer'):
        layer=dict(id=payload['layer'],name='Text',kind='vector',objects=[],background=False,visible=True,opacity=1.,bounds=None)
        meta['layers'].append(layer)
    if layer is None or layer.get('kind')!='vector':raise ValueError('Выберите векторный слой текста.')
    item=copy.deepcopy(payload['object'])
    items=layer.setdefault('objects',[])
    previous=next((x for x in items if x['id']==item['id']),None)
    if previous==item:return set()
    if previous is not None:items.remove(previous)
    if document(item).toPlainText().strip():items.append(item)
    meta['active_layer']=layer['id']
    return rebuild(db,meta,action,layer,cancel)


def move(db,meta,action,layer,kind,payload,cancel):
    selection=payload.get('selection');matrix=QTransform(*payload.get('matrix',[1,0,0,1,0,0]))
    items=[]
    for obj in layer.get('objects',[]):
        if (not payload.get('object_id') or obj['id'] == payload['object_id']) and (selection is None or selection.intersects(bounds(obj))):
            if kind=='delete_selection':continue
            result=transform(obj)*matrix
            obj=dict(obj,matrix=[result.m11(),result.m12(),result.m21(),result.m22(),result.dx(),result.dy()])
        items.append(obj)
    layer['objects']=items
    return rebuild(db,meta,action,layer,cancel)


def render_objects(layer, bounds, size, *, only=None, exclude=None, cancel=None):
    """Bounded vector preview, isolating objects even when their pixels overlap."""
    from .raster import blank
    from .storage import _check
    image = blank(size.width(), size.height())
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(size.width()/bounds.width(), size.height()/bounds.height())
        painter.translate(-bounds.x(), -bounds.y())
        for obj in layer.get('objects', []):
            _check(cancel)
            if (only and obj['id'] != only) or obj['id'] == exclude:
                continue
            doc = document(obj)
            painter.save()
            painter.setTransform(transform(obj), True)
            doc.drawContents(painter)
            painter.restore()
    finally:
        painter.end()
    return image
