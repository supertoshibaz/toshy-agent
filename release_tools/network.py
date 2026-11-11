"""Bounded HTTPS IO with cancellable retries and verified resumable downloads."""
from __future__ import annotations
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import json
import logging
import os
from pathlib import Path
import ssl
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler


class ReleaseError(RuntimeError):
    pass


class Cancelled(ReleaseError):
    pass


def tls_context():
    """Use native platform trust; fall back only if it cannot initialize."""
    try:
        # Vendored Python code contains no certificates and works before pip
        # dependencies exist. Windows validation goes through CryptoAPI.
        from .vendor.truststore import SSLContext
        return SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        logging.getLogger(__name__).warning('System TLS verifier could not initialize; '
                                            'using OpenSSL defaults: %s', error)
        return ssl.create_default_context()


def https(url):
    p = urlsplit(str(url))
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ReleaseError('An HTTPS URL without credentials is required.')
    return str(url)


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class Network:
    def __init__(self, cancel=None, progress=None, *, wait=False):
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda message: None)
        self.wait = wait
        self.opener = build_opener(Redirects(), HTTPSHandler(context=tls_context()))

    def check(self):
        if self.cancel.is_set():
            raise Cancelled('Operation cancelled.')

    def retry(self, error, attempt, *, url=None):
        self.check()
        if isinstance(error, HTTPError):
            code = error.code
            error.close()
            if code not in (408, 425, 429, 500, 502, 503, 504):
                raise ReleaseError(f'HTTP {code}. Check the repository URL, visibility and release.') from error
        reason = getattr(error, 'reason', error)
        if isinstance(reason, ssl.SSLError):
            # Query strings can contain signed download tokens.
            target = urlsplit(url) if url else None
            address = (target.scheme + '://' + target.netloc + target.path) if target else 'HTTPS request'
            if isinstance(reason, ssl.SSLCertVerificationError):
                detail = getattr(reason, 'verify_message', None) or str(reason)
                code = getattr(reason, 'verify_code', None)
                suffix = f' (verification code {code})' if code is not None else ''
                raise ReleaseError(f'TLS certificate verification failed for {address}: {detail}{suffix}. '
                                   'Check system time, trusted certificates and HTTPS interception.') from error
            raise ReleaseError(f'TLS connection failed for {address}: {reason}') from error
        if not self.wait and attempt >= 2:
            raise ReleaseError(f'Network request failed: {error}') from error
        delay = min(30, 2 ** min(attempt + 1, 5))
        self.progress(f'Connection unavailable; retrying in {delay} seconds. Cancel to stop.')
        if self.cancel.wait(delay):
            self.check()

    def request(self, url, headers=None):
        return self.opener.open(Request(https(url), headers={
            'User-Agent': 'Toshy-Installer/1', 'Accept': 'application/json', **(headers or {})}), timeout=15)

    def json(self, url, limit=8 * 1024 * 1024):
        attempt = 0
        while True:
            self.check()
            try:
                with self.request(url) as r:
                    data = r.read(limit + 1)
                if len(data) > limit:
                    raise ReleaseError('Metadata exceeds its size limit.')
                return json.loads(data)
            except (HTTPError, URLError, TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected, ssl.SSLError) as e:
                self.retry(e, attempt, url=url)
                attempt += 1
            except (ValueError, UnicodeError) as e:
                raise ReleaseError('The server returned invalid JSON.') from e

    def download(self, url, target, *, digest=None, size=None, limit=2 * 1024 ** 3):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and digest and sha256(target) == digest:
            return target
        partial = target.with_name(target.name + '.part')
        metadata = target.with_name(target.name + '.http.json')
        attempt = 0
        while True:
            self.check()
            try:
                try:
                    previous = json.loads(metadata.read_text())
                except (OSError, ValueError):
                    previous = {}
                offset = partial.stat().st_size if partial.exists() and previous.get('url') == url and previous.get('etag') else 0
                headers = {'Accept': 'application/octet-stream'}
                if offset:
                    headers.update(Range=f'bytes={offset}-', **{'If-Range': previous['etag']})
                with self.request(url, headers) as r:
                    resume = offset and r.status == 206
                    if resume and not r.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise ReleaseError('Invalid partial download response.')
                    if not resume:
                        offset = 0
                    declared = r.headers.get('Content-Length')
                    expected = offset + int(declared) if declared is not None else None
                    if expected is not None and (expected > limit or (size is not None and size != expected)):
                        raise ReleaseError('Unexpected download size.')
                    metadata.write_text(json.dumps({'url': url, 'etag': r.headers.get('ETag')}))
                    with partial.open('ab' if resume else 'wb') as f:
                        count = offset
                        reported = -1
                        while True:
                            self.check()
                            block = r.read(256 * 1024)
                            if not block:
                                break
                            f.write(block)
                            count += len(block)
                            if count > limit:
                                raise ReleaseError('Download exceeds its size limit.')
                            percent = int(count * 100 / (size or expected)) if (size or expected) else count // (1024 * 1024)
                            if percent != reported:
                                download = getattr(self.progress, 'download', None)
                                if download:
                                    download(target.name, count, size or expected)
                                else:
                                    self.progress(f'Downloading {target.name}: {percent}' + ('%' if size or expected else ' MiB'))
                                reported = percent
                    if expected is not None and count != expected:
                        raise ConnectionError('Download interrupted.')
                if size is not None and partial.stat().st_size != size:
                    raise ReleaseError('Downloaded size does not match the manifest.')
                if digest and sha256(partial) != digest:
                    partial.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    raise ReleaseError('SHA-256 mismatch; the download was rejected.')
                os.replace(partial, target)
                download = getattr(self.progress, 'download', None)
                if download:
                    download(target.name, count, size or expected, finished=True)
                metadata.unlink(missing_ok=True)
                return target
            except HTTPError as e:
                if e.code == 416:
                    e.close()
                    partial.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    continue
                self.retry(e, attempt)
                attempt += 1
            except (URLError, TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected, ssl.SSLError) as e:
                self.retry(e, attempt, url=url)
                attempt += 1
