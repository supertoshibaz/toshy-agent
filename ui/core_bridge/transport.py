"""Запросы GUI к ядру с отменой при выходе и без прокси для loopback."""
import asyncio
import threading
import weakref
import socket
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urlparse
import httpx

_clients = weakref.WeakSet()
_lock = threading.RLock()
_closing = False


class CoreHttpClient:
    def __init__(self, **options):
        self.options = options
        self._closed = False
        self._jobs = set()
        self._stream_client = None
        self._stream_thread = None
        self._stream_socket = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def request(self, method, url, **kwargs):
        async def send():
            loop, task = asyncio.get_running_loop(), asyncio.current_task()
            with _lock:
                if self._closed:
                    raise RuntimeError('Запрос отменён при завершении интерфейса.')
                self._jobs.add((loop,task))
            try:
                async with httpx.AsyncClient(**self.options) as client:
                    return await client.request(method, url, **kwargs)
            finally:
                with _lock:
                    self._jobs.discard((loop,task))
        try:
            return asyncio.run(send())
        except asyncio.CancelledError as error:
            raise RuntimeError('Запрос отменён при завершении интерфейса.') from error

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request('DELETE', url, **kwargs)

    def head(self, url, **kwargs):
        return self.request('HEAD', url, **kwargs)

    @contextmanager
    def stream(self, method, url, **kwargs):
        # Поток ChatStreamWorker сохраняет свой протокол остановки генерации.
        with _lock:
            if self._closed:
                raise RuntimeError('Интерфейс завершает работу.')
            if self._stream_client is None:
                self._stream_client = httpx.Client(**self.options)
            self._stream_thread = threading.get_ident()
            client = self._stream_client
        try:
            with client.stream(method, url, **kwargs) as response:
                stream = response.extensions.get('network_stream')
                with _lock:
                    self._stream_socket = stream.get_extra_info('socket') if stream else None
                    closed = self._closed
                if closed:
                    self.interrupt_stream()
                    raise RuntimeError('Интерфейс завершает работу.')
                yield response
        finally:
            with _lock:self._stream_socket = None
            client.close()

    def interrupt_stream(self):
        with _lock:connection = self._stream_socket
        if connection is not None:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass

    def close(self):
        with _lock:
            self._closed = True
            jobs = list(self._jobs)
            stream = self._stream_client
        for loop, task in jobs:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if stream is not None:
            if self._stream_thread == threading.get_ident():stream.close()
            else:self.interrupt_stream()


def close_core_clients():
    global _closing
    with _lock:
        _closing = True
        clients = list(_clients)
    for client in clients:
        client.close()


from ui.shutdown import on_shutdown
on_shutdown(close_core_clients)


def core_http_client(base_url, **kwargs):
    host = urlparse(base_url).hostname or ''
    try:
        local = ip_address(host).is_loopback
    except ValueError:
        local = host.casefold() == 'localhost'
    with _lock:
        if _closing:
            raise RuntimeError('Интерфейс завершает работу.')
        client = CoreHttpClient(trust_env=not local, **kwargs)
        _clients.add(client)
        return client
