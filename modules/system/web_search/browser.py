"""Optional local rendering. One browser lease; no browser survives its tool call."""

import asyncio
import base64
from dataclasses import dataclass
import json
from pathlib import Path
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import weakref

from core.background import run_data_task
from .browser_cdp import CDP
from .browser_process import BrowserUnavailable, find_browser
from .network import ResponseTooLarge, RedirectOutsideScope
from .page_parser import normalize_url, parse_html, CrawlScope, PageContent
from .request_policy import host_gate, retry_after

_gates = weakref.WeakKeyDictionary()
BLOCKED_RESOURCES = {"Image", "Media", "Font"}
HOP_HEADERS = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
}


@dataclass
class RenderedPage:
    url: str
    content: PageContent | None = None
    status: int | None = None
    issues: tuple[str, ...] = ()


class BrowserSession:
    def __init__(self, network, settings):
        self.network, self.settings = network, settings
        self.lock = asyncio.Lock()
        self.gate = _gates.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))
        self.leased = False
        self.worker = self.cdp = self.blocker = None
        self.browser_pid = None
        self.failure = None
        self.closed = False
        self.state = None
        self.tasks = set()
        self._cleanup = None

    def _read_startup(self):
        for _ in range(3):
            raw = self.worker.stdout.readline(65536)
            if not raw:
                break
            message = json.loads(raw)
            if "pid" in message:
                self.browser_pid = int(message["pid"])
            if "error" in message:
                raise BrowserUnavailable(message["error"])
            if "websocket" in message:
                return message["websocket"]
        raise BrowserUnavailable("Не удалось получить адрес локального браузера.")

    async def _start(self):
        if self.failure:
            raise BrowserUnavailable(self.failure)
        if self.cdp:
            return
        CDP.connector()
        path = await run_data_task(find_browser, self.settings["path"])
        # Reject unattended native requests. All page HTTP traffic must pass
        # through Fetch interception and the module's existing proxy router.
        self.blocker = await asyncio.start_server(
            lambda reader, writer: writer.close(), "127.0.0.1", 0
        )
        port = self.blocker.sockets[0].getsockname()[1]
        self.worker = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).with_name("browser_process.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.worker.stdin.write(
            (json.dumps({"path": path, "block_port": port}) + "\n").encode()
        )
        self.worker.stdin.flush()
        endpoint = await asyncio.wait_for(
            asyncio.to_thread(self._read_startup), timeout=15
        )
        if urlsplit(endpoint).hostname != "127.0.0.1":
            raise BrowserUnavailable("Неверный адрес управления браузером.")
        limit = self.network.crawler_settings["max_page_mb"] * 1024 * 1024
        self.cdp = await CDP.connect(endpoint, self._event, limit)
        for target in (await self.cdp.call("Target.getTargets"))["targetInfos"]:
            if target.get("type") == "page":
                await self.cdp.call(
                    "Target.closeTarget", {"targetId": target["targetId"]}
                )
        await self.cdp.call("Target.setDiscoverTargets", {"discover": True})

    def _spawn(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.add(task)

        def complete(task):
            self.tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error and self.state:
                    self.state["issues"].add("BROWSER_RESOURCE_FAILED")

        task.add_done_callback(complete)

    def _event(self, message):
        state = self.state
        if not state:
            return
        method, params = message.get("method"), message.get("params", {})
        if (
            method == "Fetch.requestPaused"
            and message.get("sessionId") == state["session"]
        ):
            state["requests"] += 1
            if state["requests"] > 256:
                state["fatal"] = "BROWSER_REQUEST_LIMIT"
            else:
                self._spawn(self._forward(state, params))
        elif (
            method == "Page.javascriptDialogOpening"
            and message.get("sessionId") == state["session"]
        ):
            self._spawn(
                self.cdp.call(
                    "Page.handleJavaScriptDialog", {"accept": False}, state["session"]
                )
            )
        elif method in {"Network.webSocketCreated", "Network.webTransportCreated"}:
            state["issues"].add("BROWSER_UNSUPPORTED_TRANSPORT")
        elif method == "Target.targetCreated":
            target = params.get("targetInfo", {})
            if (
                target.get("type") == "page"
                and state.get("target")
                and target.get("targetId") != state["target"]
            ):
                self._spawn(
                    self.cdp.call(
                        "Target.closeTarget", {"targetId": target["targetId"]}
                    )
                )
            elif target.get("type") in {
                "worker",
                "service_worker",
                "shared_worker",
                "iframe",
            }:
                # These targets cannot fetch around our proxy router. Their
                # content isn't silently advertised as successfully read.
                state["issues"].add("BROWSER_UNREAD_SUBTARGET")

    async def _forward(self, state, event):
        identity, resource = event["requestId"], event.get("resourceType", "Other")

        async def fail():
            await self.cdp.call(
                "Fetch.failRequest",
                {"requestId": identity, "errorReason": "BlockedByClient"},
                state["session"],
            )

        if resource in BLOCKED_RESOURCES:
            await fail()
            return
        request = event["request"]
        try:
            url = normalize_url(request["url"])
        except ValueError:
            state["issues"].add("BROWSER_UNSUPPORTED_RESOURCE")
            await fail()
            return
        main = resource == "Document" and event.get("frameId") == state["frame"]
        if main and state["scope"] and not state["scope"].contains(url):
            state["fatal"] = "REDIRECT_OUTSIDE_SCOPE"
            await fail()
            return
        if main:
            state["navigations"] += 1
            if state["navigations"] > 21:
                state["fatal"] = "BROWSER_REDIRECT_LIMIT"
                await fail()
                return
        async with state["slots"]:
            try:
                headers = {
                    key: value
                    for key, value in request.get("headers", {}).items()
                    if key.lower()
                    not in HOP_HEADERS
                    | {"host", "proxy-authorization", "accept-encoding"}
                }
                data = None
                if (
                    request.get("hasPostData")
                    or "postData" in request
                    or request.get("postDataEntries")
                ):
                    if request.get("postDataEntries"):
                        data = b"".join(
                            base64.b64decode(entry["bytes"], validate=True)
                            for entry in request["postDataEntries"]
                        )
                    elif "postData" in request:
                        data = request["postData"].encode("utf-8")
                    else:
                        post = await self.cdp.call(
                            "Network.getRequestPostData",
                            {"requestId": event["networkId"]},
                            state["session"],
                        )
                        data = post["postData"].encode("utf-8")
                    if len(data) > 1024 * 1024:
                        raise ResponseTooLarge("Слишком большой запрос страницы.")
                response = await self.network.request(
                    request["method"],
                    url,
                    headers=headers,
                    data=data,
                    follow_redirects=False,
                    timeout=self.network.crawler_settings["timeout"],
                    max_bytes=state["limit"],
                )
                if response.status_code in {429, 503}:
                    pause = retry_after(response.headers.get("retry-after"))
                    if pause:
                        host_gate(url).defer(pause)
                state["transferred"] += len(response.content)
                if state["transferred"] > state["limit"] * 4:
                    state["fatal"] = "BROWSER_TRANSFER_LIMIT"
                    await fail()
                    return
                if main:
                    state["url"], state["status"] = url, response.status_code
                elif response.status_code >= 400 or response.status_code == 206:
                    state["issues"].add("BROWSER_RESOURCE_HTTP_ERROR")
                pairs = response.headers.multi_items()
                output = [
                    {"name": key, "value": value}
                    for key, value in pairs
                    if key.lower() not in HOP_HEADERS
                ]
                await self.cdp.call(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": identity,
                        "responseCode": response.status_code,
                        "responseHeaders": output,
                        "body": base64.b64encode(response.content).decode("ascii"),
                    },
                    state["session"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                state["issues"].add("BROWSER_RESOURCE_FAILED")
                if main:
                    state["fatal"] = "BROWSER_DOCUMENT_FAILED"
                await fail()

    async def render(self, url, scope=None, discover_links=True):
        async with self.lock:
            if self.closed:
                raise BrowserUnavailable("Браузерное чтение завершено.")
            if not self.leased:
                await self.gate.acquire()
                self.leased = True
            try:
                await self._start()
            except BaseException as error:
                if not isinstance(error, asyncio.CancelledError):
                    self.failure = str(error)
                await self.close()
                raise
            state = {
                "scope": scope,
                "url": url,
                "status": None,
                "issues": set(),
                "fatal": "",
                "requests": 0,
                "navigations": 0,
                "transferred": 0,
                "limit": self.network.crawler_settings["max_page_mb"] * 1024 * 1024,
                "slots": asyncio.Semaphore(2),
                "target": "",
                "session": "",
                "frame": "",
            }
            self.state = state
            result = RenderedPage(url)
            try:
                async with asyncio.timeout(self.settings["timeout"]):
                    target = await self.cdp.call(
                        "Target.createTarget", {"url": "about:blank"}
                    )
                    state["target"] = target["targetId"]
                    attached = await self.cdp.call(
                        "Target.attachToTarget",
                        {"targetId": state["target"], "flatten": True},
                    )
                    state["session"] = attached["sessionId"]
                    await self.cdp.call("Page.enable", session=state["session"])
                    frame = await self.cdp.call(
                        "Page.getFrameTree", session=state["session"]
                    )
                    state["frame"] = frame["frameTree"]["frame"]["id"]
                    await self.cdp.call(
                        "Network.enable",
                        {"maxPostDataSize": 1024 * 1024},
                        state["session"],
                    )
                    await self.cdp.call(
                        "Network.setCacheDisabled",
                        {"cacheDisabled": True},
                        state["session"],
                    )
                    await self.cdp.call(
                        "Network.setBypassServiceWorker",
                        {"bypass": True},
                        state["session"],
                    )
                    await self.cdp.call(
                        "Fetch.enable",
                        {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                        state["session"],
                    )
                    cookies = []
                    for cookie in self.network._cookies:
                        if cookie.is_expired() or not cookie.domain:
                            continue
                        value = {
                            "name": cookie.name,
                            "value": cookie.value,
                            "domain": cookie.domain,
                            "path": cookie.path or "/",
                            "secure": bool(cookie.secure),
                            "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
                        }
                        if cookie.expires is not None:
                            value["expires"] = cookie.expires
                        cookies.append(value)
                    if cookies:
                        try:
                            await self.cdp.call(
                                "Network.setCookies",
                                {"cookies": cookies},
                                state["session"],
                            )
                        except BrowserUnavailable:
                            state["issues"].add("BROWSER_COOKIE_IMPORT_FAILED")
                    await self.cdp.call("Page.navigate", {"url": url}, state["session"])
                    previous, changed, started = (
                        None,
                        time.monotonic(),
                        time.monotonic(),
                    )
                    parsed = None
                    while True:
                        if state["fatal"]:
                            break
                        await asyncio.sleep(0.4)
                        document = await self.cdp.call(
                            "DOM.getDocument", {"depth": 0}, state["session"]
                        )
                        root = document["root"]
                        current = root.get("documentURL", "")
                        if not current.startswith(("http://", "https://")):
                            continue
                        current = normalize_url(current)
                        if scope and not scope.contains(current):
                            state["fatal"] = "REDIRECT_OUTSIDE_SCOPE"
                            break
                        html = (
                            await self.cdp.call(
                                "DOM.getOuterHTML",
                                {"nodeId": root["nodeId"]},
                                state["session"],
                            )
                        )["outerHTML"]
                        if (
                            len(html) > state["limit"]
                            or len(html.encode("utf-8")) > state["limit"]
                        ):
                            state["fatal"] = "BROWSER_DOM_LIMIT"
                            break
                        fingerprint = hash(html)
                        if previous != fingerprint:
                            previous, changed = fingerprint, time.monotonic()
                        if (
                            time.monotonic() - started < 1.5
                            or time.monotonic() - changed < 0.7
                        ):
                            continue
                        if parsed != fingerprint:
                            content = await run_data_task(
                                parse_html,
                                html.encode("utf-8"),
                                current,
                                scope or CrawlScope.from_url(current),
                                discover_links=discover_links,
                                limit=self.network.crawler_settings["max_pages"],
                            )
                            result = RenderedPage(current, content, state["status"])
                            parsed = fingerprint
                        if not self.tasks and not set(result.content.issues) & {
                            "JAVASCRIPT_REQUIRED",
                            "ACCESS_CHALLENGE",
                            "EMPTY_CONTENT",
                        }:
                            break
                        await asyncio.sleep(0.4)
            except TimeoutError:
                state["issues"].add("BROWSER_TIMEOUT")
            finally:
                self.state = None
                for task in list(self.tasks):
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                self.tasks.clear()
                if state["target"]:
                    try:
                        await asyncio.wait_for(
                            self.cdp.call(
                                "Target.closeTarget", {"targetId": state["target"]}
                            ),
                            timeout=2,
                        )
                    except Exception:
                        pass
            if state["fatal"] == "REDIRECT_OUTSIDE_SCOPE":
                raise RedirectOutsideScope(
                    "Перенаправление браузера выходит за пределы выбранного раздела."
                )
            if state["fatal"]:
                state["issues"].add(state["fatal"])
            result.issues = tuple(sorted(state["issues"]))
            return result

    async def _close(self):
        try:
            if self.cdp:
                try:
                    await self.cdp.close()
                except Exception:
                    pass
                self.cdp = None
            if self.worker:
                if self.worker.stdin:
                    self.worker.stdin.close()
                try:
                    await asyncio.to_thread(self.worker.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix" and self.browser_pid:
                        try:
                            os.killpg(self.browser_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    self.worker.kill()
                    await asyncio.to_thread(self.worker.wait, timeout=3)
                if self.worker.stdout:
                    self.worker.stdout.close()
                self.worker = None
            if self.blocker:
                self.blocker.close()
                await self.blocker.wait_closed()
                self.blocker = None
        finally:
            if self.leased:
                self.gate.release()
                self.leased = False

    async def close(self):
        self.closed = True
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._close())
        cancelled = False
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError:
                cancelled = True
        self._cleanup.result()
        if cancelled:
            raise asyncio.CancelledError()
