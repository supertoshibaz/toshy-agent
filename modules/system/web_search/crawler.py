"""Local HTTP crawling with bounded requests and explicit content diagnostics."""

import asyncio
from dataclasses import dataclass, field
import json

from core.background import run_data_task
from core.logger import RotatingFileLogger
from .network import (
    RoutedWebSession,
    network_options,
    ResponseTooLarge,
    RedirectOutsideScope,
    NetworkConfigurationError,
    safe_error,
)
from .settings import load_settings
from .page_parser import (
    CrawlScope,
    normalize_url,
    parse_html,
    content_issues,
    filtered_links,
    IGNORED_EXTENSIONS,
)
from .request_policy import host_gate, retry_after

web_logger = RotatingFileLogger(prefix="web-log-")
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRY_WAIT = 120.0


class CrawlSession(RoutedWebSession):
    def __init__(self, options, headers=None):
        super().__init__(options, headers)
        self.browser = None

    async def __aexit__(self, *args):
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            await super().__aexit__(*args)


def create_web_session(options=None, *, settings=None):
    snapshot = settings if settings is not None else load_settings()
    session = CrawlSession(
        options if options is not None else network_options(snapshot),
        headers=DEFAULT_HEADERS,
    )
    session.crawler_settings = dict(snapshot["crawler"])
    session.browser_settings = dict(snapshot["browser"])
    session.errors = []
    session.last_error = ""

    async def pace(url):
        await host_gate(url).wait(session.crawler_settings["delay"])

    session.before_request = pace
    return session


WEB_READ_OK = "WEB_READ_OK"
WEB_CONTENT_INCOMPLETE = "WEB_CONTENT_INCOMPLETE"
WEB_READ_FAILED = "WEB_READ_FAILED"
WEB_NOT_FETCHED = "WEB_NOT_FETCHED"


@dataclass
class PageResult:
    url: str
    text: str = ""
    title: str = ""
    links: list[str] = field(default_factory=list)
    code: str = WEB_READ_OK
    reasons: tuple[str, ...] = ()
    detail: str = ""
    http_status: int | None = None
    reader: str = "http"

    def status_text(self):
        return json.dumps(
            {
                "code": self.code,
                "url": self.url,
                "reasons": self.reasons,
                "http_status": self.http_status,
                "detail": self.detail,
                "reader": self.reader,
            },
            ensure_ascii=False,
        )

    @property
    def storage_text(self):
        if self.code == WEB_READ_OK:
            return self.text
        return (
            f"[{self.code}] {self.status_text()}\n"
            "This is partial source material. Do not treat it as the complete page; "
            "seek another source or disclose the limitation.\n\n" + self.text
        )


def _record(session, page):
    if page.code != WEB_READ_OK:
        session.last_error = page.detail or ", ".join(page.reasons)
        if len(session.errors) < 10:
            session.errors.append(page.status_text())
        web_logger.warn("WEB_PAGE_STATUS", page.status_text())
    return page


async def _request(session, url, *, timeout, headers=None, scope=None):
    options = session.crawler_settings
    for attempt in range(options["retries"] + 1):
        try:
            response = await session.get(
                url,
                timeout=timeout,
                max_bytes=options["max_page_mb"] * 1024 * 1024,
                headers=headers,
                redirect_check=scope.contains if scope else None,
            )
        except (ResponseTooLarge, RedirectOutsideScope, NetworkConfigurationError):
            raise
        except Exception as error:
            if attempt == options["retries"]:
                raise
            pause = options["retry_backoff"] * 2**attempt
            host_gate(url).defer(pause)
            web_logger.warn(
                "WEB_RETRY",
                f"Retry {attempt + 1}: {url}; {safe_error(error, session._toshy_proxy_enabled)}",
            )
            continue
        if response.status_code not in RETRY_STATUSES:
            return response
        final_url = str(response.url)
        pause = max(
            retry_after(response.headers.get("retry-after")),
            options["retry_backoff"] * 2**attempt,
        )
        host_gate(final_url).defer(pause)
        if attempt == options["retries"] or pause > MAX_RETRY_WAIT:
            return response
        web_logger.warn(
            "WEB_RETRY",
            f"HTTP {response.status_code}, retry {attempt + 1}, wait {pause:.2f} s: {final_url}",
        )
    raise RuntimeError("Не удалось выполнить запрос.")


async def _browser_fallback(session, page, scope, discover_links):
    if not session.browser_settings["enabled"] or not set(page.reasons) & {
        "JAVASCRIPT_REQUIRED",
        "EMPTY_CONTENT",
        "ACCESS_CHALLENGE",
        "EMBEDDED_CONTENT_UNREAD",
    }:
        return page
    try:
        if session.browser is None:
            from .browser import BrowserSession

            session.browser = BrowserSession(session, session.browser_settings)
        rendered = await session.browser.render(page.url, scope, discover_links)
        if rendered.content is None:
            page.reasons = tuple(
                dict.fromkeys((*page.reasons, *rendered.issues, "BROWSER_NO_CONTENT"))
            )
            return page
        content = rendered.content
        issues = tuple(dict.fromkeys((*content.issues, *rendered.issues)))
        if rendered.status == 206:
            issues = (*issues, "HTTP_PARTIAL_RESPONSE")
        if rendered.status not in {200, 206}:
            issues = (*issues, "BROWSER_HTTP_ERROR")
            content.text = ""
        if not content.text and page.text:
            page.reasons = tuple(
                dict.fromkeys((*page.reasons, *issues, "BROWSER_NO_CONTENT"))
            )
            return page
        return PageResult(
            rendered.url,
            content.text,
            content.title,
            content.links,
            WEB_CONTENT_INCOMPLETE if issues else WEB_READ_OK,
            issues,
            http_status=rendered.status,
            reader="browser",
        )
    except RedirectOutsideScope:
        page.reasons = tuple(dict.fromkeys((*page.reasons, "REDIRECT_OUTSIDE_SCOPE")))
    except Exception as error:
        page.reasons = tuple(dict.fromkeys((*page.reasons, "BROWSER_UNAVAILABLE")))
        from .browser_process import BrowserUnavailable

        page.detail = (
            str(error)
            if isinstance(error, BrowserUnavailable)
            else safe_error(error, session._toshy_proxy_enabled)
        )
    return page


async def fetch_page(
    session,
    url,
    base_domain=None,
    base_path=None,
    semaphore=None,
    delay=None,
    *,
    scope=None,
    root=False,
    discover_links=True,
):
    """Read via HTTP, optionally rendering a detected JavaScript shell locally."""
    url = normalize_url(url)
    if scope is None and base_domain is not None and not root:
        scope = CrawlScope(base_domain, base_path or "/")
    options = session.crawler_settings
    final, status = url, None
    try:
        response = await _request(session, url, timeout=options["timeout"], scope=scope)
        final, status = normalize_url(str(response.url)), response.status_code
        web_logger.info(
            "HTTP_RESP", f"HTTP {status}; {len(response.content)} bytes: {final}"
        )
        if status not in {200, 206}:
            reason = "ACCESS_DENIED" if status in {401, 403} else "HTTP_ERROR"
            return _record(
                session,
                PageResult(
                    final,
                    code=WEB_READ_FAILED,
                    reasons=(reason,),
                    detail=f"HTTP {status}.",
                    http_status=status,
                ),
            )
        content_type = (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        if content_type and not (
            content_type.startswith("text/")
            or content_type in {"application/xhtml+xml", "application/xml"}
        ):
            return _record(
                session,
                PageResult(
                    final,
                    code=WEB_READ_FAILED,
                    reasons=("UNSUPPORTED_CONTENT",),
                    detail="Ответ не является текстовой веб-страницей: " + content_type,
                    http_status=status,
                ),
            )
        if content_type == "text/plain":
            text, title, links = response.text.strip(), final, []
            issues = content_issues(text)
            if "ACCESS_CHALLENGE" in issues or "JAVASCRIPT_REQUIRED" in issues:
                text = ""
        else:
            content = await run_data_task(
                parse_html,
                response.content,
                final,
                scope or CrawlScope.from_url(final),
                discover_links=discover_links,
                limit=options["max_pages"],
            )
            text, title, links, issues = (
                content.text,
                content.title,
                content.links,
                content.issues,
            )
        if status == 206:
            issues = (*issues, "HTTP_PARTIAL_RESPONSE")
        page = PageResult(
            final,
            text,
            title,
            links,
            WEB_CONTENT_INCOMPLETE if issues else WEB_READ_OK,
            issues,
            http_status=status,
        )
        page = await _browser_fallback(session, page, scope, discover_links)
        return _record(session, page)
    except ResponseTooLarge as error:
        return _record(
            session,
            PageResult(
                final,
                code=WEB_CONTENT_INCOMPLETE,
                reasons=("RESPONSE_TOO_LARGE",),
                detail=str(error),
            ),
        )
    except RedirectOutsideScope as error:
        return _record(
            session,
            PageResult(
                final,
                code=WEB_READ_FAILED,
                reasons=("REDIRECT_OUTSIDE_SCOPE",),
                detail=str(error),
            ),
        )
    except NetworkConfigurationError as error:
        return _record(
            session,
            PageResult(
                final,
                code=WEB_READ_FAILED,
                reasons=("NETWORK_CONFIGURATION",),
                detail=str(error),
            ),
        )
    except Exception as error:
        return _record(
            session,
            PageResult(
                final,
                code=WEB_READ_FAILED,
                reasons=("NETWORK_OR_PARSE_ERROR",),
                detail=safe_error(error, session._toshy_proxy_enabled),
                http_status=status,
            ),
        )


@dataclass
class CrawlReport:
    attempted: int = 0
    saved: int = 0
    failed: int = 0
    partial: int = 0
    duplicates: int = 0
    limit_reached: bool = False

    @property
    def code(self):
        if self.partial or self.limit_reached or self.saved and self.failed:
            return WEB_CONTENT_INCOMPLETE
        return WEB_READ_FAILED if self.failed else WEB_READ_OK


async def crawl_pages(session, root_url, limit, save_page):
    """A fixed worker pool: slow pages do not hold the next batch hostage."""
    root_url = normalize_url(root_url)
    pending = asyncio.Queue()
    pending.put_nowait(root_url)
    seen, claimed = {root_url}, set()
    scheduled, scope = 1, None
    report = CrawlReport()

    async def worker():
        nonlocal scheduled, scope
        while True:
            requested = await pending.get()
            try:
                report.attempted += 1
                page = await fetch_page(
                    session, requested, scope=scope, root=(requested == root_url)
                )
                final, text, title, links = page.url, page.text, page.title, page.links
                if requested == root_url:
                    scope = CrawlScope.from_url(final)
                seen.add(final)
                duplicate = final in claimed
                if not duplicate:
                    if text:
                        claimed.add(final)
                    for link in links:
                        if link in seen or not scope.contains(link):
                            continue
                        if scheduled >= limit:
                            report.limit_reached = True
                            break
                        seen.add(link)
                        scheduled += 1
                        pending.put_nowait(link)
                if duplicate:
                    report.duplicates += 1
                else:
                    if page.code == WEB_CONTENT_INCOMPLETE:
                        report.partial += 1
                    if text:
                        await save_page(final, page.storage_text, title or final)
                        report.saved += 1
                    else:
                        report.failed += 1
            finally:
                pending.task_done()

    tasks = [
        asyncio.create_task(worker())
        for _ in range(session.crawler_settings["concurrency"])
    ]
    joined = asyncio.create_task(pending.join())
    try:
        done, _ = await asyncio.wait(
            [joined, *tasks], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            task.result()
        return report
    finally:
        for task in [joined, *tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(joined, *tasks, return_exceptions=True)


# Compatibility for code which only needs extraction, without activating a crawl.
def extract_clean_text_and_title(html_bytes, fallback_url):
    content = parse_html(html_bytes, fallback_url, discover_links=False)
    return content.text, content.title


def extract_sublinks(soup, current_url, base_domain, base_path):
    return filtered_links(
        (a["href"] for a in soup.find_all("a", href=True)),
        CrawlScope(base_domain, base_path or "/"),
        current_url,
        100000,
    )
