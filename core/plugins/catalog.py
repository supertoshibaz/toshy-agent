"""Public HTTPS catalogs and GitHub release packages. Never executes plugin code."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .manifest import PluginError, validate_catalog, tags_for
from .package import MAX_BYTES, inspect_package, package_digest, source_digest

MAX_JSON = 8 * 1024 * 1024
MAX_ICON = 2 * 1024 * 1024


class DownloadCancelled(PluginError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled('Загрузка отменена.')


def https_url(value):
    value = str(value).strip()
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.port in (None, 443)
            and parsed.username is None and parsed.password is None and not parsed.fragment
            and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise PluginError('Нужна HTTPS-ссылка без логина, пароля и фрагмента.')
    return value


class _HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        https_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def github_parts(value):
    parsed = urlsplit(https_url(value))
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname != 'github.com' or len(parts) < 2 or not all(
            re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.', '..') for p in parts[:2]):
        raise PluginError('Укажите ссылку https://github.com/OWNER/REPOSITORY.')
    owner, repo = parts[:2]
    if repo.endswith('.git'):
        repo = repo[:-4]
    if not repo:
        raise PluginError('Не указано имя репозитория.')
    return owner, repo, parts[2:]


def release_archive_url(value):
    value = https_url(value)
    _, _, suffix = github_parts(value)
    if len(suffix) < 4 or suffix[:2] != ['releases', 'download'] or not suffix[-1].lower().endswith('.zip'):
        raise PluginError('Нужна ссылка на готовый ZIP в GitHub Releases, не архив ветки с исходниками.')
    return value


@dataclass
class DownloadedPackage:
    temporary: object
    path: Path
    manifest: dict
    digest: str
    source: str

    def close(self):
        self.temporary.cleanup()


class CatalogClient:
    def __init__(self, cache_dir, opener=None):
        self.cache_dir = Path(cache_dir)
        self.opener = opener or build_opener(_HTTPSRedirect())

    def _read(self, url, *, destination=None, limit=MAX_JSON, expected_size=None,
              cancel=None, progress=None):
        url = https_url(url); check_cancel(cancel)
        request = Request(url, headers={'User-Agent': 'Toshy-Plugins/1',
            'Accept': 'application/vnd.github+json' if urlsplit(url).hostname == 'api.github.com' else '*/*'})
        started = time.monotonic(); result = bytearray(); count = 0
        try:
            with self.opener.open(request, timeout=10) as response:
                https_url(response.geturl())
                length = response.headers.get('Content-Length')
                total = int(length) if length is not None else 0
                if total < 0 or total > limit:
                    raise PluginError('Размер загрузки превышает допустимый.')
                if expected_size is not None and total and total != expected_size:
                    raise PluginError('Размер файла не совпадает с опубликованным.')
                output = Path(destination).open('wb') if destination is not None else None
                try:
                    if progress: progress(0, total or expected_size or 0, 'Скачивание')
                    while True:
                        check_cancel(cancel)
                        if time.monotonic() - started > 600:
                            raise PluginError('Превышено время загрузки.')
                        block = getattr(response, 'read1', response.read)(64 * 1024)
                        if not block: break
                        count += len(block)
                        if count > limit: raise PluginError('Размер загрузки превышает допустимый.')
                        if output is not None: output.write(block)
                        else: result.extend(block)
                        if progress: progress(count, total or expected_size or 0, 'Скачивание')
                    if (total and count != total) or (expected_size is not None and count != expected_size):
                        raise PluginError('Загрузка оборвалась или размер файла не совпадает.')
                    check_cancel(cancel)
                finally:
                    if output is not None: output.close()
            return bytes(result)
        except HTTPError as error:
            error.close()
            if error.code in (403, 429):
                raise PluginError('GitHub ограничил запросы или доступ запрещён. Повторите позже; аккаунт для публичных пакетов не требуется.') from error
            if error.code == 404:
                raise PluginError('Адрес, публичный репозиторий или опубликованный релиз не найден.') from error
            raise PluginError(f'Ошибка HTTP {error.code}.') from error
        except (URLError, TimeoutError, OSError) as error:
            raise PluginError(f'Не удалось скачать файл: {error}') from error

    def _json(self, url, **kwargs):
        try:
            return json.loads(self._read(url, **kwargs).decode('utf-8-sig'))
        except (UnicodeError, ValueError) as error:
            if isinstance(error, PluginError): raise
            raise PluginError('Сервер вернул некорректный JSON.') from error

    def catalog_url(self, source, **kwargs):
        source = https_url(source); parsed = urlsplit(source)
        if parsed.hostname != 'github.com': return source
        owner, repo, suffix = github_parts(source)
        if not suffix:
            info = self._json(f'https://api.github.com/repos/{owner}/{repo}', **kwargs)
            branch = info.get('default_branch') if isinstance(info, dict) else None
            if not isinstance(branch, str) or not branch:
                raise PluginError('Не удалось определить основную ветку репозитория.')
            suffix = [branch, 'catalog.json']
        elif suffix[0] in ('tree', 'blob') and len(suffix) >= 2:
            suffix = suffix[1:]
            if not suffix[-1].endswith('.json'): suffix.append('catalog.json')
        else:
            raise PluginError('Для библиотеки укажите репозиторий, ветку или catalog.json.')
        return f'https://raw.githubusercontent.com/{owner}/{repo}/' + '/'.join(quote(unquote(p), safe='') for p in suffix)

    def _cache_path(self, source, suffix):
        digest = hashlib.sha256(source.encode('utf-8')).hexdigest()
        return self.cache_dir / (digest + suffix)

    def _atomic_bytes(self, path, data):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.cache-', dir=self.cache_dir)
        try:
            with os.fdopen(descriptor, 'wb') as stream: stream.write(data)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def cached_catalog(self, source):
        try:
            data = self._cache_path(source, '.json').read_bytes()
            if len(data) > MAX_JSON: return None
            return validate_catalog(json.loads(data))
        except (OSError, ValueError, TypeError): return None

    def fetch_catalog(self, source, *, cancel=None, progress=None):
        source = https_url(source)
        url = self.catalog_url(source, cancel=cancel)
        catalog = validate_catalog(self._json(url, cancel=cancel, progress=progress))
        check_cancel(cancel)
        self._atomic_bytes(self._cache_path(source, '.json'), json.dumps(catalog, ensure_ascii=False).encode('utf-8'))
        return catalog

    def release_candidates(self, source, *, cancel=None, progress=None):
        """Repository/latest or pinned release tag; never builds branch source ZIPs."""
        source = https_url(source)
        owner, repo, suffix = github_parts(source)
        if suffix[:2] == ['releases', 'download']:
            return [dict(name=unquote(suffix[-1]), url=release_archive_url(source), size=None, sha256=None)]
        if not suffix or suffix in (['releases'], ['releases', 'latest']):
            endpoint = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
        elif suffix[:2] == ['releases', 'tag'] and len(suffix) >= 3:
            endpoint = f'https://api.github.com/repos/{owner}/{repo}/releases/tags/' + quote(unquote('/'.join(suffix[2:])), safe='')
        else:
            raise PluginError('Укажите репозиторий, страницу релиза или ссылку на ZIP релиза.')
        release = self._json(endpoint, cancel=cancel, progress=progress)
        if not isinstance(release, dict) or release.get('draft'):
            raise PluginError('Опубликованный релиз не найден.')
        assets = release.get('assets', [])
        # The embedded list may be truncated; use the paginated asset endpoint.
        if isinstance(release.get('id'), int):
            assets = []
            for page in range(1, 11):
                batch = self._json(f'https://api.github.com/repos/{owner}/{repo}/releases/{release["id"]}/assets?per_page=100&page={page}', cancel=cancel)
                if not isinstance(batch, list): raise PluginError('Некорректный список файлов релиза.')
                assets.extend(batch)
                if len(batch) < 100: break
            else: raise PluginError('В релизе слишком много файлов. Вставьте прямую ссылку на нужный ZIP.')
        if not isinstance(assets, list): raise PluginError('Некорректный список файлов релиза.')
        result = []
        for asset in assets:
            if not isinstance(asset, dict): continue
            name, url, size = asset.get('name'), asset.get('browser_download_url'), asset.get('size')
            if not isinstance(name, str) or not name.lower().endswith('.zip'): continue
            if not isinstance(size, int) or not 0 < size <= MAX_BYTES: continue
            try: release_archive_url(url)
            except PluginError: continue
            if github_parts(url)[:2] != (owner, repo): continue
            digest = asset.get('digest') or ''
            digest = digest[7:] if isinstance(digest, str) and re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest) else None
            result.append(dict(name=name, url=url, size=size, sha256=digest))
        if not result:
            raise PluginError('В релизе нет готового ZIP-пакета. Нужен пакет с plugin.json в корне; исходники автоматически не собираются.')
        return result

    def download_package(self, candidate, *, entry=None, cancel=None, progress=None):
        url = release_archive_url(candidate['url'])
        temporary = tempfile.TemporaryDirectory(prefix='toshy-plugin-')
        try:
            path = Path(temporary.name) / 'plugin.zip'
            self._read(url, destination=path, limit=MAX_BYTES, expected_size=candidate.get('size'), cancel=cancel, progress=progress)
            digest = package_digest(path)
            if candidate.get('sha256') and digest.lower() != candidate['sha256'].lower():
                raise PluginError('SHA-256 скачанного ZIP не совпадает с опубликованной.')
            check_cancel(cancel)
            if progress: progress(0, 0, 'Проверка пакета')
            manifest = inspect_package(path)
            if entry is not None:
                for key in ('id', 'version', 'name', 'description', 'author', 'license', 'compatibility'):
                    if manifest[key] != entry[key]:
                        raise PluginError(f'Пакет не соответствует каталогу: {key}.')
                if tags_for(manifest) != sorted(entry['tags']):
                    raise PluginError('Теги пакета не соответствуют каталогу.')
                # Hash of the approved source is independent of ZIP metadata.
                from .package import extract_package
                unpacked = Path(temporary.name) / 'source'
                extract_package(path, unpacked)
                if source_digest(unpacked) != entry['source_sha256']:
                    raise PluginError('Содержимое пакета не соответствует исходникам каталога.')
            check_cancel(cancel)
            return DownloadedPackage(temporary, path, manifest, digest, url)
        except BaseException:
            temporary.cleanup(); raise

    def cached_icon(self, source):
        path = self._cache_path(source, '.image')
        return path if path.is_file() and path.stat().st_size <= MAX_ICON else None

    def fetch_icon(self, source, *, cancel=None):
        cached = self.cached_icon(source)
        if cached: return cached
        data = self._read(source, limit=MAX_ICON, cancel=cancel)
        check_cancel(cancel)
        path = self._cache_path(source, '.image'); self._atomic_bytes(path, data)
        return path
