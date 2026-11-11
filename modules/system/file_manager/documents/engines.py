"""Private optional OCR and bounded PDF rendering; no Office dependency."""
import os
import sys
from pathlib import Path
import re
import shutil
import subprocess

from . import DocumentError, MAX_PAGES


def executable(kind):
    configured = os.environ.get('TOSHY_' + kind.upper() + '_PATH', '')
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():return str(path.resolve())
        raise DocumentError(f'Configured {kind} executable does not exist: {configured}')
    if kind != 'tesseract':
        return ''
    return shutil.which('tesseract') or ''


def private_ocr():
    from core.paths import APP_PATHS
    from release_tools.ocr import runtime_info, install
    from release_tools.network import ReleaseError
    try:
        root, runtime, item = runtime_info(APP_PATHS.managed_elements)
        receipt = runtime / '.complete'
        if not receipt.is_file() or receipt.read_text().strip() != item['sha256']:
            # Only an explicitly installed or bundled component is activated.
            # Document reading never starts a network download.
            if not (root / 'downloads' / item['filename']).is_file():
                return None
            runtime = install(APP_PATHS.managed_elements, offline=True)
        if str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        import tesserocr
        return tesserocr
    except (ImportError, OSError, ReleaseError) as error:
        raise DocumentError('Cannot load private OCR: ' + str(error)) from error


def run(args, *, timeout=90):
    try:
        result = subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout,
                                text=True, encoding='utf-8', errors='replace',
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise DocumentError('Document engine timed out. Try a smaller document or page range.') from error
    except OSError as error:
        raise DocumentError(f'Cannot start document engine: {error}') from error
    if result.returncode:
        raise DocumentError('Document engine failed: ' + (result.stderr or result.stdout)[-2000:])
    return result.stdout


def capabilities():
    problem = ''
    try:
        private = private_ocr()
    except DocumentError as error:
        private = None
        problem = str(error)
    tesseract = '' if private else executable('tesseract')
    langs = []
    if private:
        from core.paths import APP_PATHS
        try:
            _, langs = private.get_languages(str(APP_PATHS.managed_elements / 'ocr/tessdata'))
        except RuntimeError:
            pass
    elif tesseract:
        try:
            langs = [s.strip() for s in run([tesseract, *tessdata_arguments(), '--list-langs'], timeout=10).splitlines()[1:] if re.fullmatch(r'[\w-]+', s.strip())]
        except DocumentError:
            pass
    return {'pdf_rendering': True, 'office_conversion': False,
            'ocr': bool(private or tesseract) and bool(langs), 'ocr_languages': langs,
            'ocr_error': problem,
            'configuration': 'Private OCR: elements/ocr. Optional external override: TOSHY_TESSERACT_PATH.',
            'installation': 'Optional installer component: ocr. Source checkout: python -m release_tools.ocr. No LibreOffice is used.'}


def page_numbers(spec, total, maximum=4):
    if not isinstance(spec, str) or not re.fullmatch(r'\s*\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*\s*', spec):
        raise DocumentError('Use 1-based page numbers, for example "1", "1-3" or "2,5".')
    selected = []
    for part in spec.split(','):
        bounds = [int(x) for x in part.split('-')];start=bounds[0];end=bounds[-1]
        if start < 1 or end < start or end > total:raise DocumentError(f'Page range is outside 1..{total}.')
        if end-start+1 > maximum:raise DocumentError(f'Request at most {maximum} pages at once.')
        selected.extend(range(start, end+1))
    selected = sorted(set(selected))
    if len(selected) > maximum:raise DocumentError(f'Request at most {maximum} pages at once.')
    return selected


def pdf_images(path, pages, directory, *, password='', dpi=120):
    import pypdfium2 as pdfium
    dpi = max(72, min(200, int(dpi)))
    try:doc = pdfium.PdfDocument(str(path), password=password or None)
    except Exception as error:raise DocumentError('Cannot render PDF; check its password and integrity.') from error
    output = []
    try:
        if len(doc) > MAX_PAGES:raise DocumentError(f'PDF exceeds {MAX_PAGES} pages.')
        selected = page_numbers(pages, len(doc))
        for number in selected:
            page = doc[number-1]
            try:
                width,height=page.get_size();scale=min(dpi/72, 2048/max(width,height))
                bitmap=page.render(scale=scale)
                try:
                    image=bitmap.to_pil();target=Path(directory)/f'page-{number}-{dpi}.png'
                    image.save(target);image.close()
                    output.append({'page':number,'path':str(target),'mime_type':'image/png'})
                finally:bitmap.close()
            finally:page.close()
        return {'images':output,'total_pages':len(doc)}
    finally:doc.close()


def tessdata_arguments():
    from core.paths import APP_PATHS
    directory=APP_PATHS.managed_elements/'ocr/tessdata'
    return ['--tessdata-dir',str(directory)] if (directory/'eng.traineddata').is_file() else []


def ocr(image_path, languages='auto'):
    private = private_ocr()
    binary = '' if private else executable('tesseract')
    if not private and not binary:
        raise DocumentError('OCR is optional. Install the ocr component or run python -m release_tools.ocr.')
    available=capabilities()['ocr_languages']
    if languages == 'auto':
        selected=[lang for lang in ('eng','rus') if lang in available] or available[:1]
    else:
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:\+[A-Za-z0-9_-]+)*',languages):raise DocumentError('Invalid OCR language list.')
        selected=languages.split('+')
    if not selected or set(selected)-set(available):
        raise DocumentError('Requested OCR language data is unavailable. Installed languages: '+', '.join(available))
    if private:
        from core.paths import APP_PATHS
        with private.PyTessBaseAPI(path=str(APP_PATHS.managed_elements / 'ocr/tessdata'), lang='+'.join(selected)) as api:
            api.SetImageFile(str(image_path))
            return api.GetUTF8Text().strip()
    return run([binary,image_path,'stdout',*tessdata_arguments(),'-l','+'.join(selected)], timeout=60).strip()
