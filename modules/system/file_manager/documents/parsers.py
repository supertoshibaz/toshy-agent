"""Read-only parsers. Originals are never opened by Office or modified."""
import csv
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import zipfile
from urllib.parse import unquote, urlsplit
from defusedxml import ElementTree as ET

from . import DocumentError, MAX_FILE, MAX_TEXT, MAX_CELLS, MAX_UNITS, MAX_PAGES

NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'a':'http://schemas.openxmlformats.org/drawingml/2006/main',
    'p':'http://schemas.openxmlformats.org/presentationml/2006/main',
    'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    's':'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'text':'urn:oasis:names:tc:opendocument:xmlns:text:1.0',
    'table':'urn:oasis:names:tc:opendocument:xmlns:table:1.0',
    'office':'urn:oasis:names:tc:opendocument:xmlns:office:1.0'}

def q(prefix,name):return '{'+NS[prefix]+'}'+name

def archive(path):
    try:z=zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:raise DocumentError('Invalid or encrypted Office/EPUB archive.') from error
    infos=z.infolist()
    if (len(infos)>10000 or sum(x.file_size for x in infos)>MAX_FILE*2 or
            any(x.flag_bits&1 or x.file_size>MAX_FILE or '\\' in x.filename or
                PurePosixPath(x.filename).is_absolute() or '..' in PurePosixPath(x.filename).parts for x in infos)):
        z.close();raise DocumentError('Archive is encrypted or exceeds document extraction limits.')
    return z

def xml(z,name):return ET.fromstring(z.read(name))
def local(tag):return tag.rsplit('}',1)[-1]
def text(node):
    parts=[]
    for item in node.iter():
        if local(item.tag)=='p' and parts:parts.append('\n')
        if local(item.tag) in ('t','tab','br','line-break','s'):
            parts.append(item.text or '' if local(item.tag)=='t' else '\t' if local(item.tag)=='tab' else '\n' if local(item.tag) in ('br','line-break') else ' ')
    return ''.join(parts)

def odf_text(node):
    parts=[node.text or '']
    for item in node:
        kind=local(item.tag)
        parts.append(' ' * min(100,int(item.get(q('text','c'),'1'))) if kind=='s' else '\t' if kind=='tab' else '\n' if kind=='line-break' else odf_text(item))
        parts.append(item.tail or '')
    return ''.join(parts)

def unit(doc,kind,value,**extra):
    if len(doc['units'])>=MAX_UNITS:raise DocumentError('Document has too many sections; split it into smaller files.')
    value=str(value)
    doc['_chars']+=len(value)
    if doc['_chars']>MAX_TEXT:raise DocumentError(f'Extracted content exceeds {MAX_TEXT} characters; split the source file.')
    doc['units'].append({'number':len(doc['units'])+1,'kind':kind,'text':value,**extra})

def table_text(rows):return '\n'.join('\t'.join(str(c) for c in row) for row in rows)
def media(doc,z,prefixes):
    for name in sorted(z.namelist()):
        if any(name.startswith(prefix) for prefix in prefixes) and not name.endswith('/'):
            doc['images'].append({'id':f'image-{len(doc["images"])+1}','part':name,'filename':PurePosixPath(name).name})

def links(z,name):
    if name not in z.namelist():return {}
    return {e.get('Id'):e.get('Target') for e in xml(z,name) if e.get('TargetMode')!='External'}

def part(base,target):
    if urlsplit(target).scheme or target.startswith('//'):
        raise DocumentError('External document resources are not fetched.')
    target=unquote(target.split('#',1)[0])
    result=posixpath.normpath(posixpath.join(posixpath.dirname(base),target.lstrip('/'))) if not target.startswith('/') else target.lstrip('/')
    if result.startswith('../'):raise DocumentError('Document relationship points outside its archive.')
    return result


def parse_pdf(path,doc,password):
    from pypdf import PdfReader
    try:
        reader=PdfReader(path,strict=False)
        if reader.is_encrypted and (not password or not reader.decrypt(password)):
            raise DocumentError('PDF password is required or incorrect.')
        if len(reader.pages)>MAX_PAGES:raise DocumentError(f'PDF exceeds {MAX_PAGES} pages.')
        for n,page in enumerate(reader.pages,1):
            value=page.extract_text() or ''
            unit(doc,'page',value,page=n,ocr_recommended=not bool(value.strip()))
    except DocumentError:raise
    except Exception as error:raise DocumentError('Cannot extract PDF text: '+str(error)[:300]) from error


def parse_docx(path,doc):
    with archive(path) as z:
        root=xml(z,'word/document.xml');body=root.find('w:body',NS)
        for item in body:
            if local(item.tag)=='p':
                style=item.find('w:pPr/w:pStyle',NS);name=style.get(q('w','val'),'') if style is not None else ''
                kind='heading' if re.match(r'heading\d|title',name,re.I) else 'list_item' if item.find('w:pPr/w:numPr',NS) is not None else 'paragraph'
                value=text(item)
                if value.strip():unit(doc,kind,value,style=name)
            elif local(item.tag)=='tbl':
                rows=[[text(cell) for cell in row.findall('w:tc',NS)] for row in item.findall('w:tr',NS)]
                unit(doc,'table',table_text(rows),rows=rows)
        for name in sorted(z.namelist()):
            if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml',name):
                for p in xml(z,name).iter(q('w','p')):
                    if text(p).strip():unit(doc,'annotation',text(p),source=name)
        media(doc,z,('word/media/',))


def parse_pptx(path,doc):
    with archive(path) as z:
        rels=links(z,'ppt/_rels/presentation.xml.rels')
        for entry in xml(z,'ppt/presentation.xml').findall('p:sldIdLst/p:sldId',NS):
            name=part('ppt/presentation.xml',rels[entry.get(q('r','id'))]);root=xml(z,name)
            paragraphs=[''.join(p.itertext()) if not list(p) else text(p) for p in root.iter(q('a','p'))]
            notes=[];related=links(z,posixpath.dirname(name)+'/_rels/'+posixpath.basename(name)+'.rels')
            for target in related.values():
                if 'notesSlides/' in target:
                    note=xml(z,part(name,target))
                    notes.extend(text(p) for p in note.iter(q('a','p')) if text(p).strip())
            unit(doc,'slide','\n'.join(paragraphs),slide=len(doc['units'])+1,notes='\n'.join(notes))
        media(doc,z,('ppt/media/',))


def parse_odf(path,doc):
    with archive(path) as z:
        root=xml(z,'content.xml')
        if doc['format']=='ods':
            from openpyxl.utils.cell import get_column_letter
            for table in root.iter(q('table','table')):
                cells={};r=0;maxcol=0;merged=[]
                for row in table.findall('table:table-row',NS):
                    repeat=int(row.get(q('table','number-rows-repeated'),'1'));c=0;values=[]
                    for cell in row:
                        if local(cell.tag) not in ('table-cell','covered-table-cell'):continue
                        count=int(cell.get(q('table','number-columns-repeated'),'1'))
                        colspan=int(cell.get(q('table','number-columns-spanned'),'1'))
                        rowspan=int(cell.get(q('table','number-rows-spanned'),'1'))
                        if colspan>1 or rowspan>1:
                            if repeat>2000 or len(merged)+repeat>20000:raise DocumentError('Spreadsheet has too many merged ranges.')
                            for rr in range(r+1,r+repeat+1):
                                merged.append(f'{get_column_letter(c+1)}{rr}:{get_column_letter(c+colspan)}{rr+rowspan-1}')
                        value=cell.get(q('office','value')) or cell.get(q('office','date-value')) or cell.get(q('office','boolean-value'))
                        value=value if value is not None else '\n'.join(odf_text(p) for p in cell.findall('text:p',NS))
                        value_type=cell.get(q('office','value-type'))
                        if value_type in ('float','currency','percentage') and value:
                            value=float(value);value=int(value) if value.is_integer() else value
                        elif value_type=='boolean':value=value=='true'
                        formula=cell.get(q('table','formula'))
                        if value not in ('',None) or formula:
                            if count*repeat+len(cells)>MAX_CELLS:raise DocumentError('Spreadsheet has too many populated cells.')
                            for col in range(c+1,c+count+1):values.append((col,{'value':value,'formula':formula}))
                        c+=count
                    if values:
                        for rr in range(r+1,r+repeat+1):
                            for cc,value in values:cells[f'{get_column_letter(cc)}{rr}']=value
                        maxcol=max(maxcol,max(cc for cc,_ in values))
                    r+=repeat
                    if r>1048576 or c>16384:raise DocumentError('Spreadsheet dimensions exceed supported bounds.')
                used=max((int(re.search(r'\d+$',key)[0]) for key in cells),default=0)
                doc['sheets'].append({'name':table.get(q('table','name'),'Sheet'), 'rows':used,'columns':maxcol,'cells':cells,'merged_ranges':merged})
            doc['warnings'].append('Formula values are saved results from the file; they may be missing or stale. Formulas are not recalculated.')
        elif doc['format']=='odp':
            for slide in root.iter():
                if local(slide.tag)=='page':unit(doc,'slide','\n'.join(odf_text(p) for p in slide.iter() if local(p.tag) in ('p','h')),slide=len(doc['units'])+1)
        else:
            body=root.find('office:body/office:text',NS)
            if body is None:raise DocumentError('ODT text body is missing.')
            def visit(item):
                kind=local(item.tag)
                if kind in ('p','h'):unit(doc,'heading' if kind=='h' else 'paragraph',odf_text(item))
                elif kind=='table':
                    rows=[[odf_text(c) for c in row if local(c.tag)=='table-cell'] for row in item if local(row.tag)=='table-row']
                    unit(doc,'table',table_text(rows),rows=rows)
                else:
                    for child in item:visit(child)
            visit(body)
        media(doc,z,('Pictures/',))


def parse_xlsx(path,doc):
    from openpyxl.utils.datetime import from_excel,CALENDAR_MAC_1904,CALENDAR_WINDOWS_1900
    from openpyxl.styles.numbers import BUILTIN_FORMATS,is_date_format
    from openpyxl.formula.translate import Translator
    with archive(path) as z:
        workbook=xml(z,'xl/workbook.xml');rels=links(z,'xl/_rels/workbook.xml.rels')
        strings=[]
        if 'xl/sharedStrings.xml' in z.namelist():strings=[''.join(x.itertext()) for x in xml(z,'xl/sharedStrings.xml')]
        formats={};styles=[]
        if 'xl/styles.xml' in z.namelist():
            sr=xml(z,'xl/styles.xml');formats={int(x.get('numFmtId')):x.get('formatCode') for x in sr.findall('s:numFmts/s:numFmt',NS)}
            styles=[int(x.get('numFmtId','0')) for x in sr.findall('s:cellXfs/s:xf',NS)]
        props=workbook.find('s:workbookPr',NS);epoch=CALENDAR_MAC_1904 if props is not None and props.get('date1904') in ('1','true') else CALENDAR_WINDOWS_1900
        total=0
        for sheet in workbook.findall('s:sheets/s:sheet',NS):
            name=part('xl/workbook.xml',rels[sheet.get(q('r','id'))]);root=xml(z,name)
            cells={};rows=columns=0;shared={}
            from openpyxl.utils.cell import coordinate_to_tuple
            for cell in root.iter(q('s','c')):
                address=cell.get('r');rr,cc=coordinate_to_tuple(address)
                if rr>1048576 or cc>16384:raise DocumentError('Invalid spreadsheet coordinates.')
                raw=cell.findtext('s:v',None,NS);kind=cell.get('t','n');value=raw
                if kind=='s' and raw is not None:value=strings[int(raw)]
                elif kind=='inlineStr':value=''.join(cell.find('s:is',NS).itertext())
                elif kind=='b' and raw is not None:value=raw=='1'
                elif kind=='n' and raw is not None:
                    value=float(raw);value=int(value) if value.is_integer() else value
                    sid=int(cell.get('s','0'));fmt=styles[sid] if sid<len(styles) else 0
                    if is_date_format(formats.get(fmt,BUILTIN_FORMATS.get(fmt,''))):value=from_excel(value,epoch).isoformat()
                formula=cell.find('s:f',NS);expression=None
                if formula is not None:
                    expression='='+formula.text if formula.text else None
                    if formula.get('t')=='shared':
                        index=formula.get('si')
                        if expression:shared[index]=(expression,address)
                        elif index in shared:expression=Translator(shared[index][0],origin=shared[index][1]).translate_formula(address)
                    if expression is None:expression='[shared or array formula; original formula not available]'
                if value is None and expression is None:continue
                total+=1
                if total>MAX_CELLS:raise DocumentError('Spreadsheet has too many populated cells.')
                cells[address]={'value':value,'formula':expression};rows=max(rows,rr);columns=max(columns,cc)
            doc['sheets'].append({'name':sheet.get('name'),'rows':rows,'columns':columns,'cells':cells,
                                  'merged_ranges':[x.get('ref') for x in root.findall('s:mergeCells/s:mergeCell',NS)]})
        doc['warnings'].append('Formula values are saved results from the file; they may be missing or stale. Formulas are not recalculated.')
        media(doc,z,('xl/media/',))



def parse_xls(path, doc):
    """BIFF worksheets without a desktop Office installation or formula execution."""
    import datetime
    import math
    from python_calamine import CalamineWorkbook
    from openpyxl.utils.cell import get_column_letter
    workbook = CalamineWorkbook.from_path(str(path))
    total = 0
    try:
        if len(workbook.sheet_names) > MAX_UNITS:
            raise DocumentError('Spreadsheet has too many sheets.')
        for name in workbook.sheet_names:
            sheet = workbook.get_sheet_by_name(name)
            if sheet.height * sheet.width > MAX_CELLS:
                raise DocumentError('Legacy worksheet exceeds the cell limit.')
            _, start_column = sheet.start or (0, 0)
            cells = {}
            rows = columns = 0
            # Calamine iter_rows preserves leading empty rows, while trimming
            # the leading empty columns of the sheet's used range.
            for row_number, row in enumerate(sheet.iter_rows(), 1):
                for column, value in enumerate(row, start_column + 1):
                    if value is None or value == '':
                        continue
                    total += 1
                    if total > MAX_CELLS:
                        raise DocumentError('Spreadsheet has too many populated cells.')
                    if isinstance(value, (datetime.date, datetime.time, datetime.datetime)):
                        value = value.isoformat()
                    elif isinstance(value, datetime.timedelta):
                        value = str(value)
                    elif isinstance(value, float) and not math.isfinite(value):
                        value = str(value)
                    cells[f'{get_column_letter(column)}{row_number}'] = {'value': value, 'formula': None}
                    rows, columns = max(rows, row_number), max(columns, column)
            merges = [f'{get_column_letter(a[1]+1)}{a[0]+1}:{get_column_letter(b[1]+1)}{b[0]+1}'
                      for a, b in sheet.merged_cell_ranges or []]
            doc['sheets'].append({'name': name, 'rows': rows, 'columns': columns,
                                  'cells': cells, 'merged_ranges': merges})
        doc['warnings'].append('Legacy XLS returns saved cell values. Formula expressions and recalculation are unavailable.')
    finally:
        workbook.close()

def decode(raw):
    for encoding in ('utf-8-sig','utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8','cp1251'):
        try:return raw.decode(encoding)
        except UnicodeError:pass
    return raw.decode('utf-8',errors='replace')


def parse_csv(path,doc):
    from openpyxl.utils.cell import get_column_letter
    value=decode(Path(path).read_bytes());delimiter='\t' if doc['format']=='tsv' else None
    if not delimiter:
        try:delimiter=csv.Sniffer().sniff(value[:8192],delimiters=',;\t|').delimiter
        except csv.Error:delimiter=','
    cells={};rows=cols=0
    csv.field_size_limit(MAX_TEXT)
    for rows,row in enumerate(csv.reader(io.StringIO(value,newline=''),delimiter=delimiter),1):
        cols=max(cols,len(row))
        if rows>1048576 or cols>16384:raise DocumentError('Delimited file exceeds spreadsheet dimensions.')
        for c,cell in enumerate(row,1):
            if not cell:continue
            if len(cells)>=MAX_CELLS:raise DocumentError('Delimited file has too many populated cells.')
            cells[f'{get_column_letter(c)}{rows}']={'value':cell,'formula':None}
    doc['sheets'].append({'name':'Sheet1','rows':rows,'columns':cols,'cells':cells,'merged_ranges':[]})
    doc['delimiter']=delimiter


def html_units(value,doc,*,source=''):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(value,'html.parser')
    for item in soup(['script','style','noscript']):item.decompose()
    def visit(item):
        if getattr(item,'name',None) is None:
            if str(item).strip():unit(doc,'paragraph',str(item).strip(),source=source)
        elif item.name=='table':
            rows=[[c.get_text(' ',strip=True) for c in r.find_all(['td','th'],recursive=False)] for r in item.find_all('tr')]
            unit(doc,'table',table_text(rows),rows=rows,source=source)
        elif item.name in ('h1','h2','h3','h4','h5','h6','p','li','pre'):
            unit(doc,'heading' if item.name.startswith('h') else 'paragraph',item.get_text(' ',strip=True),source=source)
        else:
            for child in item.children:visit(child)
    visit(soup.body or soup)


def parse_epub(path,doc):
    with archive(path) as z:
        container=xml(z,'META-INF/container.xml');opf=next(e.get('full-path') for e in container.iter() if local(e.tag)=='rootfile')
        root=xml(z,opf);items={e.get('id'):e for e in root.iter() if local(e.tag)=='item'}
        for ref in root.iter():
            if local(ref.tag)=='itemref':
                item=items[ref.get('idref')];name=part(opf,item.get('href'))
                html_units(decode(z.read(name)),doc,source=name)
        for item in items.values():
            if str(item.get('media-type','')).startswith('image/'):
                name=part(opf,item.get('href'));doc['images'].append({'id':f'image-{len(doc["images"])+1}','part':name,'filename':PurePosixPath(name).name})


def parse(path,*,password='',directory=None):
    path=Path(path);suffix=path.suffix.lower().lstrip('.')
    original=suffix
    if suffix in ('doc', 'ppt'):
        raise DocumentError('Legacy DOC/PPT reading is unavailable without an Office conversion engine. Convert the file to DOCX/PPTX or PDF first. Originals are never modified.')
    doc={'format':suffix,'units':[],'sheets':[],'images':[],'warnings':[],'_chars':0}
    if suffix=='pdf':parse_pdf(path,doc,password)
    elif suffix=='docx':parse_docx(path,doc)
    elif suffix in ('odt','ods','odp'):parse_odf(path,doc)
    elif suffix in ('xlsx','xlsm'):parse_xlsx(path,doc)
    elif suffix=='xls':parse_xls(path,doc)
    elif suffix=='pptx':parse_pptx(path,doc)
    elif suffix in ('csv','tsv'):parse_csv(path,doc)
    elif suffix=='epub':parse_epub(path,doc)
    elif suffix in ('html','htm'):html_units(decode(path.read_bytes()),doc)
    elif suffix=='rtf':
        from striprtf.striprtf import rtf_to_text
        unit(doc,'section',rtf_to_text(decode(path.read_bytes())))
    elif suffix in ('png','jpg','jpeg','webp','tif','tiff','bmp'):
        from PIL import Image
        with Image.open(path) as image:
            if image.width*image.height>40_000_000:raise DocumentError('Image exceeds 40 million pixels.')
            if getattr(image,'n_frames',1)>MAX_PAGES:raise DocumentError('Image has too many frames.')
            for n in range(getattr(image,'n_frames',1)):unit(doc,'page','',page=n+1,ocr_recommended=True)
    else:raise DocumentError('Unsupported document format: '+suffix)
    doc.pop('_chars');doc['original_format']=original
    if original!=suffix:doc['converted_path']=str(path)
    if sum(len(str(x)) for x in doc['sheets'])>MAX_TEXT*4:raise DocumentError('Spreadsheet extraction exceeds the size limit.')
    return doc
