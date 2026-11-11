"""Per-operation proxy snapshots; no changes to the process environment."""

import asyncio
import logging
import re
import traceback
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from urllib.parse import quote, urlsplit, urlunsplit, urljoin

from .settings import (
    load_settings,
    normalize_settings,
    resolve_password,
    connection_identity,
)


@dataclass(frozen=True)
class NetworkOptions:
    http_proxy: str | None = field(default=None, repr=False)
    https_proxy: str | None = field(default=None, repr=False)
    manual: bool = False
    socks_proxy: str | None = field(default=None, repr=False)

    @property
    def uses_proxy(self):
        return bool(self.http_proxy or self.https_proxy)

    def proxy_for(self, url):
        scheme = urlsplit(str(url)).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("Web Search поддерживает только HTTP и HTTPS.")
        return self.http_proxy if scheme == "http" else self.https_proxy


def network_options(settings=None, password=None):
    supplied = settings is not None
    settings = normalize_settings(settings) if supplied else load_settings()
    if not settings["enabled"]:
        return NetworkOptions()
    if password is None:
        saved = load_settings() if supplied else settings
        password = (
            resolve_password(saved)
            if connection_identity(saved) == connection_identity(settings)
            else ""
        )

    def authenticate(address):
        if not address:
            return None
        parsed = urlsplit(address)
        if settings["username"] or password:
            userinfo = (
                quote(settings["username"], safe="")
                + ":"
                + quote(password, safe="")
                + "@"
            )
            return urlunsplit(parsed._replace(netloc=userinfo + parsed.netloc))
        return address

    socks = authenticate(settings["socks5"])
    http = authenticate(settings["http"]) or socks
    https = (
        authenticate(
            settings["http"] if settings["use_http_for_https"] else settings["https"]
        )
        or socks
    )
    return NetworkOptions(http, https, True, socks)


class NetworkConfigurationError(ValueError):
    pass


class ResponseTooLarge(NetworkConfigurationError):
    pass


class RedirectOutsideScope(NetworkConfigurationError):
    pass


def safe_error(error, proxy_enabled=False):
    if isinstance(error, NetworkConfigurationError):
        return str(error)
    if proxy_enabled:
        return (
            f"Ошибка работы через прокси ({type(error).__name__}). "
            "Проверьте адрес, авторизацию и доступ к сети."
        )
    return re.sub(r"(?i)\b(https?|socks5h?)://[^/\s@]+@", r"\1://***@", str(error))


class _ProxyLogFilter(logging.Filter):
    def filter(self, record):
        record.msg = safe_error(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_error(
                "".join(traceback.format_exception(*record.exc_info))
            )
            record.exc_info = None
        return True


_proxy_log_filter = _ProxyLogFilter()


def search(query, options, max_results=8):
    from .search import search as html_search
    return html_search(query, options, max_results)


async def async_search(query, options, max_results=8):
    from .search import async_search as html_search
    return await html_search(query,options,max_results)


class RoutedWebSession:
    def __init__(self, options, headers=None):
        self.options, self.headers = options, headers
        self._clients = {}
        self._cookies = CookieJar()
        self._client_lock = asyncio.Lock()
        self._closed = False
        self.before_request = None
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            self._curl = False
        else:
            self._curl = True

    @property
    def _toshy_proxy_enabled(self):
        return self.options.uses_proxy

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self._closed = True
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(
            *(client.__aexit__(*args) for client in clients), return_exceptions=True
        )

    async def _client(self, proxy):
        key = proxy
        if self._closed:
            raise RuntimeError("Сетевое подключение закрыто.")
        async with self._client_lock:
            if proxy not in self._clients:
                trust_env = not self.options.manual
                if self._curl:
                    from curl_cffi import CurlOpt
                    from curl_cffi.requests import AsyncSession

                    curl_options = (
                        {CurlOpt.NOPROXY: "", CurlOpt.PROXY: proxy or ""}
                        if self.options.manual
                        else None
                    )
                    client = AsyncSession(
                        impersonate="chrome",
                        headers=self.headers,
                        proxy=proxy,
                        trust_env=trust_env,
                        curl_options=curl_options,
                        cookies=self._cookies,
                    )
                else:
                    import httpx

                    if proxy and proxy.startswith("socks5://"):
                        raise NetworkConfigurationError(
                            "Для локального DNS с SOCKS5 нужен curl_cffi. Включите разрешение DNS через прокси или установите curl_cffi."
                        )
                    proxy = (
                        proxy.replace("socks5h://", "socks5://", 1) if proxy else None
                    )
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    }
                    headers.update(self.headers or {})
                    client = httpx.AsyncClient(
                        headers=headers,
                        timeout=15.0,
                        follow_redirects=False,
                        proxy=proxy,
                        trust_env=trust_env,
                        cookies=self._cookies,
                    )
                await client.__aenter__()
                self._clients[key] = client
            return self._clients[key]

    async def _bounded_request(self, client, url, arguments, max_bytes):
        body = bytearray()
        method = arguments.pop("_method", "GET")
        payload = arguments.pop("_body", None)
        if payload is not None:
            arguments["data" if self._curl else "content"] = payload
        if self._curl:
            exceeded = False

            def receive(chunk):
                nonlocal exceeded
                if len(body) + len(chunk) > max_bytes:
                    exceeded = True
                    return 0
                body.extend(chunk)
                return len(chunk)

            try:
                response = await client.request(
                    method, url, content_callback=receive, **arguments
                )
            except Exception:
                if exceeded:
                    raise ResponseTooLarge(
                        "Страница превышает установленный предел размера."
                    ) from None
                raise
            if exceeded:
                raise ResponseTooLarge(
                    "Страница превышает установленный предел размера."
                )
            response.content = bytes(body)
            return response
        import httpx

        async with client.stream(method, url, **arguments) as response:
            length = response.headers.get("content-length", "")
            if method != "HEAD" and length.isdigit() and int(length) > max_bytes:
                raise ResponseTooLarge(
                    "Страница превышает установленный предел размера."
                )
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ResponseTooLarge(
                        "Страница превышает установленный предел размера."
                    )
                body.extend(chunk)
            headers = [
                (key, value)
                for key, value in response.headers.multi_items()
                if key.lower()
                not in {"content-encoding", "transfer-encoding", "content-length"}
            ]
            headers.append(("content-length", str(len(body))))
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
            )

    async def get(
        self, url, *, max_bytes=8 * 1024 * 1024, redirect_check=None, **kwargs
    ):
        return await self.request(
            "GET", url, max_bytes=max_bytes, redirect_check=redirect_check, **kwargs
        )

    async def request(
        self,
        method,
        url,
        *,
        max_bytes=8 * 1024 * 1024,
        redirect_check=None,
        follow_redirects=True,
        data=None,
        **kwargs,
    ):
        url = str(url)
        method = method.upper()
        headers = kwargs.pop("headers", None)
        remaining = float(kwargs.get("timeout", 15))
        for redirect in range(21):
            if redirect_check is not None and not redirect_check(url):
                raise RedirectOutsideScope(
                    "Перенаправление выходит за пределы выбранного раздела сайта."
                )
            proxy = self.options.proxy_for(url)
            if self.before_request is not None:
                await self.before_request(url)
            client = await self._client(proxy)
            arguments = dict(kwargs, _method=method, _body=data)
            if headers is not None:
                arguments["headers"] = headers
            arguments["allow_redirects" if self._curl else "follow_redirects"] = False
            if remaining <= 0:
                raise TimeoutError("Истёк таймаут загрузки страницы.")
            arguments["timeout"] = remaining
            started = time.monotonic()
            async with asyncio.timeout(remaining):
                response = await self._bounded_request(
                    client, url, arguments, max_bytes
                )
            remaining -= time.monotonic() - started
            location = response.headers.get("location")
            if (
                not follow_redirects
                or response.status_code not in {301, 302, 303, 307, 308}
                or not location
            ):
                return response
            if redirect == 20:
                raise ValueError("Слишком много перенаправлений страницы.")
            if (
                response.status_code == 303
                and method != "HEAD"
                or response.status_code in {301, 302}
                and method == "POST"
            ):
                method, data = "GET", None
                if headers:
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() not in {"content-type", "content-length"}
                    }
            target = urljoin(url, location)
            if (
                urlsplit(target).netloc != urlsplit(url).netloc
                or urlsplit(target).scheme != urlsplit(url).scheme
            ):
                if headers:
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() not in {"authorization", "cookie", "host"}
                    }
            url = target


def create_web_session(options=None, *, headers=None):
    return RoutedWebSession(
        options if options is not None else network_options(), headers
    )


async def probe_proxy(settings, password=None):
    settings = normalize_settings(settings)
    options = network_options(settings, password)
    if not options.uses_proxy:
        raise ValueError("Включите и заполните прокси для проверки.")
    outcomes = []
    for label, address, proxy, target in (
        ("HTTP", settings["http"], options.http_proxy, "http://example.com/"),
        (
            "HTTPS",
            settings["http"] if settings["use_http_for_https"] else settings["https"],
            options.https_proxy,
            "https://example.com/",
        ),
        ("SOCKS5", settings["socks5"], options.socks_proxy, "https://example.com/"),
    ):
        if not address:
            continue
        try:
            async with create_web_session(
                NetworkOptions(proxy, proxy, True)
            ) as session:
                response = await session.get(target, timeout=8)
                outcomes.append(
                    f"{label}: "
                    + (
                        "подключение работает."
                        if response.status_code == 200
                        else f"HTTP {response.status_code}."
                    )
                )
        except Exception as error:
            outcomes.append(f"{label}: " + safe_error(error, True))
    if options.https_proxy:
        try:
            results = await async_search("DuckDuckGo", options, 1)
            outcomes.append(
                "DuckDuckGo: " + ("поиск работает." if results else "нет результатов.")
            )
        except Exception as error:
            outcomes.append("DuckDuckGo: " + safe_error(error, True))
    else:
        outcomes.append(
            "Для поиска DuckDuckGo HTTPS-прокси не задан; поиск будет идти напрямую."
        )
    return "\n".join(outcomes)
