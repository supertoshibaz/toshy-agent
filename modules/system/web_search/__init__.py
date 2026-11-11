"""DuckDuckGo discovery, bounded page reading and recursive section crawling."""

from typing import List

from core.background import run_data_task
from core.registry import BaseModule, BaseTool
from core.memory import MemoryManager
from .network import network_options, async_search, safe_error
from .settings import load_settings
from .crawler import (
    fetch_page,
    create_web_session,
    crawl_pages,
    web_logger,
    WEB_READ_OK,
    WEB_CONTENT_INCOMPLETE,
    WEB_READ_FAILED,
    WEB_NOT_FETCHED,
)
from .page_parser import normalize_url

memory_manager = MemoryManager()


def _status(code):
    if code in {WEB_CONTENT_INCOMPLETE, WEB_READ_FAILED}:
        return (
            f"[{code}] Do not assume complete source coverage. Check the page reason codes; "
            "search other sources or tell the user which information could not be obtained.\n"
        )
    if code == WEB_NOT_FETCHED:
        return (
            f"[{code}] Search links/snippets only; full page content was not fetched.\n"
        )
    return f"[{code}] No incompleteness detected in the fetched HTML; dynamic content completeness is not verified.\n"


def _diagnostics(session):
    if not session.errors:
        return ""
    return "\nPage diagnostics (up to 5):\n" + "\n".join(session.errors[:5])


class WebSourceTool(BaseTool):
    def get_error(self, output):
        text = str(output)
        return text if text.startswith(f'[{WEB_READ_FAILED}]') else None

    def get_sources(self, args, output, workspace):
        from .sources import tool_sources
        return tool_sources(self.name, args, output, workspace)


class DeepSearchTool(WebSourceTool):
    history_read_only = True
    def __init__(self):
        super().__init__(
            name="deep_search",
            description="Locally crawls linked HTML pages within section boundaries and configured limits, with an optional local browser for JavaScript pages. Returns WEB_CONTENT_INCOMPLETE when pages or crawl coverage are incomplete; saves readable material to temporary memory.",
            args_schema={
                "type": "object",
                "properties": {
                    "root_url": {
                        "type": "string",
                        "description": "HTTP/HTTPS root URL of a website or documentation section.",
                    },
                    "max_pages": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Maximum distinct page addresses to try. 0 uses the Startup limit; a positive value cannot exceed that configured limit.",
                    },
                },
                "required": ["root_url"],
            },
        )

    async def execute(self, root_url: str, max_pages: int = 0, **kwargs):
        options, temp_id = None, None
        try:
            settings = load_settings()
            options = await run_data_task(network_options, settings)
            root_url = normalize_url(root_url)
            if (
                isinstance(max_pages, bool)
                or not isinstance(max_pages, int)
                or max_pages < 0
            ):
                raise ValueError("max_pages должен быть целым неотрицательным числом.")
            limit = min(
                max_pages or settings["crawler"]["max_pages"],
                settings["crawler"]["max_pages"],
            )
            temp_id = await run_data_task(
                memory_manager.init_temp_doc, source="Deep Crawl: " + root_url
            )

            async def save(url, text, title):
                await memory_manager.append_temp_stream_data(
                    temp_id, text, title=title, url=url
                )

            async with create_web_session(options, settings=settings) as session:
                report = await crawl_pages(session, root_url, limit, save)
                diagnostics = _diagnostics(session)
            web_logger.info(
                "CRAWL_FINISH",
                f"{root_url}: saved {report.saved}, inspected {report.attempted}, failed {report.failed}",
            )
            return (
                _status(report.code)
                + f"System: Crawl finished. Saved {report.saved} page(s); tried {report.attempted} address(es); "
                f"failed {report.failed}; incomplete pages {report.partial}; duplicate destinations {report.duplicates}. "
                f"Limit: {limit}. Limit reached: {report.limit_reached}. "
                f"Temporary memory document ID: {temp_id}.\n"
                "Use 'search_temp_memory' to search the saved material. This is a bounded crawl, not a guarantee that the whole site was downloaded."
                + diagnostics
            )
        except Exception as error:
            message = safe_error(error, bool(options and options.uses_proxy))
            web_logger.error("CRAWL_ERROR", message)
            partial = (
                f" Уже сохранённые страницы остаются в документе {temp_id}."
                if temp_id
                else ""
            )
            return (
                _status(WEB_CONTENT_INCOMPLETE if temp_id else WEB_READ_FAILED)
                + "System: Crawl error: "
                + message
                + partial
            )


class WebSearchTool(WebSourceTool):
    history_read_only = True
    def __init__(self):
        super().__init__(
            name="web_search",
            description="Searches DuckDuckGo first, with independent Brave/Yahoo fallbacks if it is unavailable. Returns ranked URLs, titles and snippets only, with the actual search provider. Does not fetch pages. Use focused queries and supported operators. Choose sources to read with read_url. Do not repeatedly retry equivalent queries after a provider refusal.",
            args_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"],
            },
        )

    async def execute(self, query: str, **kwargs):
        options = None
        try:
            settings = load_settings()
            options = await run_data_task(network_options, settings)
            raw = await async_search(query, options, settings['crawler']['search_results'])
            rows, seen = [], set()
            for entry in raw:
                try:
                    url = normalize_url(entry.get('href', ''))
                except (ValueError, UnicodeError):
                    continue
                if url in seen:continue
                seen.add(url)
                rows.append(f"{len(rows)+1}. {entry.get('title', '')}\nURL: {url}\nSearch provider: {entry.get('engine', 'duckduckgo')}\nSnippet: {entry.get('body', '')}")
            if not rows:
                return f'[WEB_NOT_FETCHED] No search results for {query!r}. Try different precise terms.'
            return ('[WEB_NOT_FETCHED] Search results only. These pages have NOT been read. '
                    'Choose relevant URLs and call read_url.\n\n' + '\n\n'.join(rows))
        except Exception as error:
            return _status(WEB_READ_FAILED) + 'Search failed: ' + safe_error(error, bool(options and options.uses_proxy))


class ReadUrlTool(WebSourceTool):
    history_read_only = True
    def __init__(self):
        super().__init__(
            name="read_url",
            description="Locally reads one HTTP/HTTPS page with an optional local browser for JavaScript pages. Returns WEB_CONTENT_INCOMPLETE for partial or inaccessible dynamic content, WEB_READ_FAILED for read failures. Saves usable text with its status to temporary memory; does not save challenge text.",
            args_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The exact HTTP/HTTPS URL.",
                    }
                },
                "required": ["url"],
            },
        )

    async def execute(self, url: str, **kwargs):
        options = None
        try:
            settings = load_settings()
            options = await run_data_task(network_options, settings)
            url = normalize_url(url)
            async with create_web_session(options, settings=settings) as session:
                page = await fetch_page(session, url, discover_links=False)
                if not page.text:
                    return (
                        _status(page.code)
                        + "System: Unable to read page.\n"
                        + page.status_text()
                    )
            identity = await memory_manager.save_temp_data(
                content=page.storage_text, source=page.url
            )
            from .full_page import authorized_page_text
            full = await authorized_page_text(page, memory_manager)
            return (
                _status(page.code)
                + full
                + f"System: Page {page.title!r} ({page.url}) saved to temporary memory ID: {identity}. Use search_temp_memory to inspect it.\n"
                + page.status_text()
            )
        except Exception as error:
            return (
                _status(WEB_READ_FAILED)
                + "System: Read error: "
                + safe_error(error, bool(options and options.uses_proxy))
            )


class Module(BaseModule):
    def get_tools(self) -> List[BaseTool]:
        return [WebSearchTool(), ReadUrlTool(), DeepSearchTool()]

    def get_system_prompt_addon(self):
        return (
            'Web research: web_search returns links and snippets only; read_url reads a selected URL '
            '(also use it for URLs supplied by the user); deep_search crawls a bounded section. '
            'For complex requests, identify separate information needs and search each with focused '
            'queries containing relevant entities, versions and constraints. Do not paste or merely '
            'shorten the entire user prompt into one search. Adapt queries to results and missing facts. '
            'Use exact phrase quotes, site: filters, exclusions and other supported search operators '
            'when useful; do not add them mechanically. Prefer original documentation, maintainers '
            'and independent measurements. Read selected sources before drawing conclusions from snippets. '
            'Ordinary pages are stored in session temporary memory; use search_temp_memory. '
            'Full text is returned only when the USER enabled full-page reading for this request '
            'and it fits the remaining context budget; tool arguments cannot grant that permission. '
            'Check incomplete-page and crawl-limit diagnostics. WEB_READ_OK does not prove that '
            'all dynamic content was obtained. Do not invent missing material.'
        )
