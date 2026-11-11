"""Document capabilities exposed alongside existing file-manager tools."""
import asyncio
import json

from core.registry import BaseTool
from core.context import get_execution_context
from core.tool_visuals import VisualToolResult
from .service import Documents

PATH={'type':'string','description':'File path inside Project Folder, or attachment://att_ID from the attachment notice. The exact host path of an explicitly attached file also works. Terminal paths belong to the selected terminal environment.'}
PASSWORD={'type':'string','description':'Password for a protected PDF, only if supplied by the user.'}


class DocumentTool(BaseTool):
    history_read_only=True
    repeat_after_interruption=True
    def __init__(self,name,operation,description,properties,required=()):
        super().__init__(name,description,{'type':'object','properties':properties,'required':list(required),'additionalProperties':False})
        self.operation=operation

    async def execute(self,**kwargs):
        context=get_execution_context();service=Documents(context.workspace_root,context.storage_dir)
        try:
            result=await asyncio.to_thread(service.perform,self.operation,**kwargs)
            output=json.dumps(result,ensure_ascii=False,allow_nan=False)
            return VisualToolResult(output,result['images']) if self.operation=='render' else output
        except Exception as error:
            return 'System Error: '+str(error)

    def get_sources(self,args,output,workspace):
        if str(output).startswith('System Error:'):return []
        try:
            from .access import resolve_read_path
            path=resolve_read_path(workspace,args['path'])
            return [{'kind':'file','url':path.as_uri(),'path':str(path),'label':path.name}]
        except (KeyError,ValueError,OSError):return []


def tools():
    return [
        DocumentTool('inspect_document','info',
            'Inspect PDF, Office, EPUB, HTML, RTF, images or spreadsheets without loading their full content into context. Returns outline, unit/page counts, sheet names, images and original file paths. Omit path to list supported formats and OCR/conversion capabilities.',
            {'path':PATH,'password':PASSWORD}),
        DocumentTool('read_document','read',
            'Read selected document units: PDF/image pages, presentation slides, or ordered paragraphs/headings/tables. Use inspect_document first. Text is bounded; follow next.start/offset to continue without losing content. OCR auto handles scanned pages if Tesseract is installed. No full-file automatic inlining.',
            {'path':PATH,'start':{'type':'integer','minimum':1},'end':{'type':'integer','minimum':1},
             'offset':{'type':'integer','minimum':0},'max_chars':{'type':'integer','minimum':256,'maximum':24000},
             'ocr':{'type':'string','enum':['auto','always','never']},'languages':{'type':'string','description':'OCR language codes, e.g. eng+rus, or auto.'},'password':PASSWORD},('path',)),
        DocumentTool('search_document','search',
            'Search literal text in document sections or spreadsheet cell values/formulas. Returns snippets, page/unit or sheet/cell locations, and a continuation cursor (start/offset). OCR defaults to never; use auto to search scanned pages.',
            {'path':PATH,'query':{'type':'string'},'start':{'type':'integer','minimum':1},'offset':{'type':'integer','minimum':0},
             'max_results':{'type':'integer','minimum':1,'maximum':100},'ocr':{'type':'string','enum':['auto','always','never']},
             'languages':{'type':'string'},'password':PASSWORD},('path','query')),
        DocumentTool('read_spreadsheet','spreadsheet',
            'Read bounded XLSX/XLSM/ODS/CSV/TSV/XLS cell ranges. Returns addresses, values, formulas, merged ranges and original file paths. Formula values are cached results and may be missing/stale; formulas are not recalculated. Use inspect_document for sheet names. Follow next.cursor/offset with the same range to continue.',
            {'path':PATH,'sheet':{'type':'string'},'cell_range':{'type':'string','description':'A1 notation, up to 2000 cells, e.g. A1:F30.'},
             'cursor':{'type':'string'},'offset':{'type':'integer','minimum':0},'max_chars':{'type':'integer','minimum':512,'maximum':24000},'password':PASSWORD},('path',)),
        DocumentTool('render_document','render',
            'Return images of selected document pages or an embedded image ID from inspect_document. Page rendering supports PDFs and raster images; other supported documents can be read as text and their embedded raster images can be extracted. At most 4 pages per call. Images are sent to the model as visual content; use only with a vision-capable model. Original file path is included for terminal work.',
            {'path':PATH,'pages':{'type':'string','description':'1-based pages, e.g. 1-3 or 2,5.'},'image_id':{'type':'string'},
             'dpi':{'type':'integer','minimum':72,'maximum':200},'password':PASSWORD},('path',))]
