"""Optional self-contained OCR. No system packages, registry or extra Python."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tempfile
import zipfile
from .network import Network, ReleaseError, sha256


def pins():
    return json.loads(Path(__file__).with_name('resources.json').read_text())


def runtime_info(elements, *, python_tag=None, os_name=None, resource_pins=None):
    python_tag = python_tag or f'cp{sys.version_info.major}{sys.version_info.minor}'
    os_name = os_name or ('windows' if os.name == 'nt' else 'linux')
    if platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise ReleaseError('Portable OCR currently supports x86-64 only.')
    spec = (resource_pins or pins())['ocr']
    key = os_name + '-' + python_tag
    item = spec['wheels'].get(key)
    if item is None:
        raise ReleaseError(f'No pinned OCR wheel for {key}. Use Python 3.12, 3.13 or 3.14.')
    root = Path(elements) / 'ocr'
    runtime = root / 'runtime' / (spec['version'] + '-' + key)
    return root, runtime, item


def install(elements, network=None, *, python_tag=None, os_name=None, offline=False, resource_pins=None):
    root, runtime, item = runtime_info(elements, python_tag=python_tag, os_name=os_name, resource_pins=resource_pins)
    network = network or Network(wait=True)
    data_files = (resource_pins or pins())['tesseract_data']['files']
    for name, data in data_files.items():
        target = root / 'tessdata' / name
        if offline:
            if not target.is_file() or sha256(target) != data['sha256']:
                raise ReleaseError('Bundled OCR language data is missing or damaged. Run python -m release_tools.ocr.')
        else:
            network.download(data['url'], target, digest=data['sha256'], size=data['size'])
    receipt = runtime / '.complete'
    if receipt.is_file() and receipt.read_text().strip() == item['sha256']:
        return runtime
    wheel = root / 'downloads' / item['filename']
    if offline:
        if not wheel.is_file() or sha256(wheel) != item['sha256']:
            raise ReleaseError('Portable OCR is not installed for this interpreter. Run python -m release_tools.ocr.')
    else:
        network.download(item['url'], wheel, digest=item['sha256'], size=item['size'])
    runtime.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ocr-', dir=runtime.parent) as temporary:
        staging = Path(temporary) / 'runtime'
        staging.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            entries = archive.infolist()
            if len(entries) > 4000 or sum(entry.file_size for entry in entries) > 300 * 1024**2:
                raise ReleaseError('OCR wheel exceeds its extraction limit.')
            for entry in entries:
                network.check()
                name = PurePosixPath(entry.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in entry.filename or ':' in entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ReleaseError('Unsafe OCR wheel member.')
                target = staging.joinpath(*name.parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
        (staging / '.complete').write_text(item['sha256'] + '\n')
        network.check()
        if runtime.exists():
            # Another document worker may have finished the same extraction.
            if receipt.is_file() and receipt.read_text().strip() == item['sha256']:
                return runtime
            raise ReleaseError('Incomplete OCR runtime exists. Remove ' + str(runtime) + ' and retry.')
        try:
            staging.rename(runtime)
        except OSError:
            if not receipt.is_file() or receipt.read_text().strip() != item['sha256']:
                raise
    return runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install optional local OCR for scanned PDFs and images into Toshy elements/ocr (English and Russian). No global packages are installed.')
    parser.add_argument('--elements', type=Path)
    args = parser.parse_args(argv)
    if args.elements is None:
        from core.paths import APP_PATHS
        args.elements = APP_PATHS.elements
    try:
        destination = install(args.elements, Network(progress=print, wait=True))
        sys.path.insert(0, str(destination))
        import tesserocr
        from PIL import Image, ImageDraw
        with tesserocr.PyTessBaseAPI(path=str(args.elements / 'ocr/tessdata'), lang='eng+rus') as api:
            image = Image.new('RGB', (400, 100), 'white')
            ImageDraw.Draw(image).text((15, 20), 'OCR ready 123', fill='black', font_size=32)
            api.SetImage(image)
            print(api.GetUTF8Text().strip())
        print('OCR ready:', args.elements / 'ocr')
        return 0
    except (OSError, ReleaseError, ImportError, RuntimeError) as error:
        print('OCR installation failed:', error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('OCR installation cancelled; rerun the command to resume.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
