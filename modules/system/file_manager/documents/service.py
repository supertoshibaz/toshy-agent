"""Workspace-scoped snapshots, content-keyed cache and bounded tool responses."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from core.paths import APP_PATHS
from . import DOCUMENT_SUFFIXES, MAX_FILE, VERSION, DocumentError

_LOCK=threading.RLock()
_WORKERS=threading.BoundedSemaphore(2)


def atomic_json(path,data):
    temporary=path.with_name(path.name+'.part')
    temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    if os.name!='nt':temporary.chmod(0o600)
    os.replace(temporary,path)


def worker(action,directory,path='',**options):
    request={'action':action,'directory':str(directory),'path':str(path),'options':options}
    # Start at the code root, including direct run_ui.py launches. A child
    # crashing in a native PDF/image library cannot terminate the agent core.
    with _WORKERS:
        try:
            result=subprocess.run([sys.executable,'-m','modules.system.file_manager.documents.worker'],
                input=json.dumps(request),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                cwd=APP_PATHS.code_root,encoding='utf-8',errors='replace',timeout=150,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as error:raise DocumentError('Document extraction timed out; use a smaller file.') from error
    if result.returncode:raise DocumentError('Document reader process failed: '+result.stderr[-1200:])
    try:payload=json.loads(result.stdout)
    except ValueError as error:raise DocumentError('Document reader returned an invalid response.') from error
    if not payload.get('ok'):raise DocumentError(payload.get('error','Document extraction failed.'))
    return payload['result']


class Documents:
    def __init__(self,workspace,storage=None):
        self.workspace=Path(workspace).resolve()
        self.storage=Path(storage) if storage else APP_PATHS.runtime/'document_previews'
        self.cache=APP_PATHS.root/'cache'/'documents'

    def snapshot(self,path,password=''):
        if not isinstance(path,str) or not path.strip():raise DocumentError('A document path is required.')
        from .access import authorized, resolve_read_path, read_bytes
        target=resolve_read_path(self.workspace,path)
        inside=target.is_relative_to(self.workspace)
        attachment=authorized().get(str(target))
        if not inside and attachment is None:raise DocumentError('Document must be inside Project Folder or explicitly attached to this conversation.')
        if target.suffix.lower() not in DOCUMENT_SUFFIXES:raise DocumentError('Unsupported document extension: '+target.suffix)
        raw=read_bytes(self.workspace,str(target),MAX_FILE+1)
        if len(raw)>MAX_FILE:raise DocumentError('Document exceeds the 128 MiB file limit.')
        digest=hashlib.sha256(raw).hexdigest()
        key=hashlib.sha256(f'{VERSION}:{target.suffix.lower()}:{digest}:{password}'.encode()).hexdigest()
        directory=self.cache/key;directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        source=directory/('source'+target.suffix.lower())
        if not source.is_file():
            temporary=directory/'source.part';temporary.write_bytes(raw)
            if os.name!='nt':temporary.chmod(0o600)
            os.replace(temporary,source)
        os.utime(directory,None)
        base={'path':str(target),'file_url':target.as_uri(),'workspace_path':target.relative_to(self.workspace).as_posix() if inside else (attachment or {}).get('workspace_path'),
              'size_bytes':len(raw),'sha256':digest,
              'terminal_note':'Use workspace_path in the project terminal. For Docker/SSH, use the attachment terminal_path mapping when supplied; path/file_url refer to the host.'}
        return base,directory,source

    def load(self,path,password=''):
        base,directory,source=self.snapshot(path,password)
        saved=directory/'document.json'
        hit=saved.is_file()
        if hit:
            try:doc=json.loads(saved.read_text(encoding='utf-8'))
            except (ValueError,OSError):hit=False
        if not hit:
            doc=worker('parse',directory,source,password=password);atomic_json(saved,doc)
        return base,directory,source,doc

    def info(self,path='',password=''):
        if not path:
            return {'formats':sorted(DOCUMENT_SUFFIXES - {'.doc', '.ppt'}),
                    'unavailable_formats':{'.doc':'Convert to DOCX or PDF first.', '.ppt':'Convert to PPTX or PDF first.'},
                    **worker('capabilities',self.cache),
                    'limits':{'file_mib':128,'render_pages_per_call':4,'text_characters_per_call':24000,'spreadsheet_cells_per_call':2000},
                    'tools':['inspect_document','read_document','search_document','read_spreadsheet','render_document']}
        base,directory,source,doc=self.load(path,password)
        return {**base,'format':doc['format'],'original_format':doc['original_format'],'units':len(doc['units']),
            'unit_kind':'pages' if doc['format']=='pdf' or source.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.tif','.tiff','.bmp'} else 'slides' if doc['format'] in ('pptx','odp') else 'paragraphs, headings and tables (not physical pages)',
            'outline':[{'number':x['number'],'text':x['text'][:160]} for x in doc['units'] if x['kind']=='heading'][:100],
            'sheets':[{'name':s['name'],'rows':s['rows'],'columns':s['columns'],
                       'merged_ranges':s['merged_ranges'][:20],'merged_range_count':len(s['merged_ranges'])} for s in doc['sheets']],
            'images':[{k:v for k,v in image.items() if k!='part'} for image in doc['images']][:200],
            'warnings':doc['warnings'],'ocr_recommended_pages':[x['number'] for x in doc['units'] if x.get('ocr_recommended')][:100],
            'reading':'Use read_document for text, read_spreadsheet for cells, search_document for matches, render_document for visual pages or an embedded image.'}

    def read(self,path,start=1,end=None,offset=0,max_chars=12000,ocr='auto',languages='auto',password=''):
        base,directory,source,doc=self.load(path,password)
        if doc['sheets']:raise DocumentError('This is a spreadsheet. Use read_spreadsheet with a sheet name and cell range.')
        if ocr not in ('auto','always','never'):raise DocumentError('ocr must be auto, always or never.')
        total=len(doc['units'])
        if not total:return {**base,'units':[],'total_units':0,'next':None}
        start=int(start);end=min(total,start+19) if end is None else int(end);offset=int(offset)
        if start<1 or end<start or end>total or end-start>=20 or offset<0:raise DocumentError('Read 1..20 valid units per call, using a nonnegative character offset.')
        budget=max(256,min(24000,int(max_chars)));rows=[];next_cursor=None;warnings=[]
        for index in range(start-1,end):
            item=doc['units'][index];value=item['text']
            if item.get('notes'):value+='\nSpeaker notes:\n'+item['notes']
            if (ocr=='always' or (ocr=='auto' and not value.strip())) and item['kind']=='page':
                key=hashlib.sha256(f'{index}:{languages}'.encode()).hexdigest();cached=directory/('ocr-'+key+'.json')
                try:
                    if cached.is_file():value=json.loads(cached.read_text(encoding='utf-8'))['text']
                    else:
                        value=worker('ocr',directory,source,pages=str(index+1),languages=languages,password=password)['units'][0]['text']
                        atomic_json(cached,{'text':value})
                except DocumentError as error:
                    if ocr=='always':raise
                    if str(error) not in warnings:warnings.append(str(error))
            begin=offset if index==start-1 else 0
            if begin>len(value):raise DocumentError('Character offset exceeds the selected unit.')
            fragment=value[begin:begin+budget]
            rows.append({'number':index+1,'kind':item['kind'],'text':fragment,'offset':begin,'total_chars':len(value)})
            budget-=len(fragment)
            if begin+len(fragment)<len(value):next_cursor={'start':index+1,'offset':begin+len(fragment)};break
            if budget<=0:
                next_cursor={'start':index+2,'offset':0} if index+1<total else None;break
        else:next_cursor={'start':end+1,'offset':0} if end<total else None
        return {**base,'units':rows,'total_units':total,'next':next_cursor,'warnings':warnings}

    def search(self,path,query,start=1,offset=0,max_results=20,password='',ocr='never',languages='auto'):
        base,directory,source,doc=self.load(path,password)
        if not isinstance(query,str) or not query or len(query)>1000:raise DocumentError('Provide a nonempty literal search query of at most 1000 characters.')
        if int(start)<1 or int(offset)<0:raise DocumentError('Invalid search cursor.')
        if ocr not in ('auto','always','never'):raise DocumentError('ocr must be auto, always or never.')
        count=max(1,min(100,int(max_results)));results=[];cursor=None;budget=22000;warnings=[]
        if doc['sheets']:
            units=({'text':(str(cell['value']) if cell.get('value') is not None else '')+'\n'+str(cell.get('formula') or ''),'sheet':sheet['name'],'cell':address}
                   for sheet in doc['sheets'] for address,cell in sheet['cells'].items())
        else:units=iter(doc['units'])
        for number,item in enumerate(units,1):
            if number<int(start):continue
            value=item['text']
            if item.get('notes'):value+='\n'+item['notes']
            if ocr!='never' and item.get('kind')=='page' and (ocr=='always' or not value.strip()):
                key=hashlib.sha256(f'{number-1}:{languages}'.encode()).hexdigest();cached=directory/('ocr-'+key+'.json')
                try:
                    if cached.is_file():value=json.loads(cached.read_text(encoding='utf-8'))['text']
                    else:
                        value=worker('ocr',directory,source,pages=str(number),languages=languages,password=password)['units'][0]['text']
                        atomic_json(cached,{'text':value})
                except DocumentError as error:
                    if ocr=='always':raise
                    if str(error) not in warnings:warnings.append(str(error))
            # re.IGNORECASE preserves character offsets even for Unicode folds.
            for match in re.finditer(re.escape(query),value,re.IGNORECASE):
                if number==int(start) and match.start()<int(offset):continue
                results.append({'unit':number,'offset':match.start(),'snippet':value[max(0,match.start()-90):match.end()+150][:400],
                                **{k:item[k] for k in ('sheet','cell','page','slide') if k in item}})
                budget-=len(json.dumps(results[-1],ensure_ascii=False))
                if len(results)>=count or budget<600:cursor={'start':number,'offset':match.end()};break
            if cursor:break
        return {**base,'matches':results,'next':cursor,'ocr':ocr,'warnings':warnings,
                'note':'Search uses extracted text. Use ocr="auto" to include scanned PDF/image pages.'}

    def spreadsheet(self,path,sheet='',cell_range='A1:J30',cursor='',offset=0,max_chars=16000,password=''):
        from openpyxl.utils.cell import range_boundaries,get_column_letter,coordinate_to_tuple
        base,directory,source,doc=self.load(path,password)
        sheets=doc['sheets']
        if not sheets:raise DocumentError('Document is not a spreadsheet.')
        chosen=next((x for x in sheets if x['name']==sheet),None) if sheet else sheets[0]
        if chosen is None:raise DocumentError('Sheet not found. Available: '+', '.join(x['name'] for x in sheets))
        if not re.fullmatch(r'\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?',cell_range):raise DocumentError('Use a bounded cell range such as A1:F30.')
        c1,r1,c2,r2=range_boundaries(cell_range)
        if r1<1 or c1<1 or r2<r1 or c2<c1 or r2>1048576 or c2>16384 or (r2-r1+1)*(c2-c1+1)>2000:raise DocumentError('Request at most 2000 cells within spreadsheet bounds.')
        begin=coordinate_to_tuple(cursor) if cursor else (r1,c1)
        if not (r1<=begin[0]<=r2 and c1<=begin[1]<=c2) or int(offset)<0:raise DocumentError('Invalid spreadsheet cursor/offset.')
        budget=max(512,min(24000,int(max_chars)));rows=[];next_cursor=None
        for rr in range(r1,r2+1):
            for cc in range(c1,c2+1):
                if (rr,cc)<begin:continue
                address=f'{get_column_letter(cc)}{rr}';cell=chosen['cells'].get(address,{'value':None,'formula':None})
                item={'cell':address,**cell};size=len(json.dumps(item,ensure_ascii=False))
                if size>budget and rows:next_cursor={'cursor':address,'offset':0};break
                if size>budget or (int(offset)>0 and (rr,cc)==begin):
                    # A single long cell remains readable in successive chunks.
                    value=json.dumps(cell,ensure_ascii=False);start=int(offset)
                    if start>len(value):raise DocumentError('Cell offset exceeds its content.')
                    fragment=value[start:start+budget-100]
                    item={'cell':address,'json_fragment':fragment,'offset':start,'total_chars':len(value)}
                    if start+len(fragment)<len(value):next_cursor={'cursor':address,'offset':start+len(fragment)}
                elif int(offset) and (rr,cc)==begin:raise DocumentError('Offset is only valid for a fragmented long cell.')
                rows.append(item);budget-=min(size,budget)
                if next_cursor:break
            if next_cursor:break
        return {**base,'sheet':chosen['name'],'range':cell_range,'cells':rows,'next':next_cursor,
                'merged_ranges':chosen['merged_ranges'][:200],'warnings':doc['warnings']}

    def render(self,path,pages='1',image_id='',dpi=120,password=''):
        base,directory,source,doc=self.load(path,password)
        options={'pages':pages,'dpi':dpi,'password':password}
        if image_id:
            image=next((x for x in doc['images'] if x['id']==image_id),None)
            if image is None:raise DocumentError('Embedded image ID not found; inspect_document lists available images.')
            options.update(image=image,converted_path=doc.get('converted_path',''))
        result=worker('image' if image_id else 'render',directory,source,**options)
        saved=[]
        destination=self.storage/'document_previews';destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        for item in result['images']:
            raw=Path(item['path']).read_bytes();digest=hashlib.sha256(raw).hexdigest();target=destination/(digest+'.png')
            if not target.is_file():
                temporary=target.with_suffix('.part');temporary.write_bytes(raw)
                if os.name!='nt':temporary.chmod(0o600)
                os.replace(temporary,target)
            saved.append({**item,'path':str(target),'file_url':target.as_uri(),'sha256':digest})
        return {**base,'images':saved,'total_pages':result.get('total_pages'),'image_id':image_id,
                'note':'Rendered pages are visual tool results. Reading them requires an image-capable model; extracted text/OCR remains available.'}

    def perform(self,operation,**kwargs):
        # Avoid duplicate snapshots/cache writers across parallel tool calls.
        with _LOCK:
            try:return getattr(self,operation)(**kwargs)
            finally:self.prune()

    def prune(self):
        if not self.cache.is_dir():return
        entries=[]
        for directory in self.cache.iterdir():
            if directory.is_dir():
                size=sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
                entries.append((directory.stat().st_mtime,size,directory))
        total=sum(row[1] for row in entries)
        for _,size,directory in sorted(entries):
            if total<=1024*1024*1024:break
            shutil.rmtree(directory,ignore_errors=True);total-=size
