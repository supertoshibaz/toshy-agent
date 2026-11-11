"""One disposable document-engine process per operation; no agent imports."""
import json
from pathlib import Path
import sys

from . import DocumentError
from .engines import pdf_images, ocr, page_numbers, capabilities
from .parsers import parse, archive


def image_pages(path,pages,directory):
    from PIL import Image, ImageOps
    rows=[]
    with Image.open(path) as original:
        if original.width*original.height>40_000_000:raise DocumentError('Image exceeds 40 million pixels.')
        total=getattr(original,'n_frames',1)
        for number in page_numbers(pages,total):
            original.seek(number-1)
            if original.width*original.height>40_000_000:raise DocumentError('Image exceeds 40 million pixels.')
            rgba=ImageOps.exif_transpose(original.copy()).convert('RGBA')
            image=Image.new('RGB',rgba.size,'white');image.paste(rgba,mask=rgba.getchannel('A'));rgba.close()
            image.thumbnail((2048,2048))
            target=Path(directory)/f'image-page-{number}.png';image.save(target);image.close()
            rows.append({'page':number,'path':str(target),'mime_type':'image/png'})
    return {'images':rows,'total_pages':total}


def render(path,options,directory):
    kind=path.suffix.lower()
    if kind in {'.png','.jpg','.jpeg','.webp','.tif','.tiff','.bmp'}:
        return image_pages(path,options.get('pages','1'),directory)
    if kind!='.pdf':
        raise DocumentError('Page rendering is supported for PDFs and raster images. Use read_document/read_spreadsheet for Office, HTML, EPUB and RTF text, or render an embedded image ID.')
    return pdf_images(path,options.get('pages','1'),directory,password=options.get('password',''),dpi=options.get('dpi',120))


def process(request):
    action=request['action'];directory=Path(request['directory']);path=Path(request.get('path',''))
    options=request.get('options') or {}
    if action=='capabilities':return capabilities()
    if action=='parse':return parse(path,password=options.get('password',''),directory=directory)
    if action=='render':return render(path,options,directory)
    if action=='ocr':
        result=render(path,options,directory)
        return {'units':[{'page':item['page'],'text':ocr(item['path'],options.get('languages','auto'))} for item in result['images']]}
    if action=='image':
        info=options['image'];original=Path(options.get('converted_path') or path)
        with archive(original) as z:
            raw=z.read(info['part'])
        target=directory/('embedded-'+info['id']+Path(info['filename']).suffix.lower())
        target.write_bytes(raw)
        return image_pages(target,'1',directory)
    raise DocumentError('Unknown document operation.')


def main():
    try:
        request=json.load(sys.stdin)
        result={'ok':True,'result':process(request)}
    except Exception as error:
        result={'ok':False,'error':f'{type(error).__name__}: {error}'}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)


if __name__=='__main__':main()
