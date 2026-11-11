"""Bounded HTML extraction and section-aware URL normalization."""

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, urljoin, unquote, parse_qsl, urlencode

from bs4 import BeautifulSoup

IGNORED_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".svg",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".ico",
    ".woff",
    ".woff2",
)
NOISE_TAGS = (
    "head",
    "title",
    "script",
    "style",
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    "noscript",
    "svg",
    "iframe",
    "button",
    "template",
)
TRACKING_KEYS = {"fbclid", "gclid", "dclid", "msclkid", "_ga", "mc_cid", "mc_eid"}


def normalize_url(url):
    parsed = urlsplit(str(url).strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
    ):
        raise ValueError("Нужен HTTP/HTTPS-адрес без логина и пароля.")
    host = parsed.hostname.lower()
    host = "[" + host + "]" if ":" in host else host.encode("idna").decode("ascii")
    port = parsed.port
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme.lower()]:
        host += ":" + str(port)
    if any(ord(char) < 32 or char.isspace() for char in str(url)):
        raise ValueError("Адрес содержит пробелы или управляющие символы.")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [
        (key, value)
        for key, value in pairs
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    # Preserve query order and original escaping unless tracking was removed.
    query = parsed.query if pairs == filtered else urlencode(filtered)
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", query, ""))


def _path(value):
    return posixpath.normpath("/" + unquote(value).lstrip("/"))


@dataclass(frozen=True)
class CrawlScope:
    domain: str
    path: str

    @classmethod
    def from_url(cls, url):
        parsed = urlsplit(normalize_url(url))
        path = parsed.path
        if path.lower().endswith((".html", ".htm")):
            path = posixpath.dirname(path)
        return cls(parsed.netloc, _path(path))

    def contains(self, url):
        try:
            parsed = urlsplit(normalize_url(url))
        except (ValueError, UnicodeError):
            return False
        path = _path(parsed.path)
        return parsed.netloc == self.domain and (
            self.path == "/"
            or path == self.path
            or path.startswith(self.path.rstrip("/") + "/")
        )


def filtered_links(links, scope, current_url, limit):
    result, seen = [], set()
    for link in links:
        try:
            url = normalize_url(urljoin(current_url, str(link)))
        except (ValueError, UnicodeError):
            continue
        if (
            url in seen
            or not scope.contains(url)
            or urlsplit(url).path.lower().endswith(IGNORED_EXTENSIONS)
        ):
            continue
        seen.add(url)
        result.append(url)
        if len(result) >= limit:
            break
    return result


def challenge_text(text, title=""):
    title = title.strip().casefold()
    lead = text.strip().casefold()
    titles = (
        "just a moment",
        "attention required",
        "access denied",
        "security check",
        "verify you are human",
        "проверка браузера",
        "проверка безопасности",
    )
    messages = (
        "checking your browser",
        "verifying you are human",
        "verify you are human",
        "please enable javascript",
        "enable javascript and cookies to continue",
        "performing security verification",
        "проверяем ваш браузер",
        "подтвердите, что вы человек",
        "докажите, что вы не робот",
    )
    return (
        len(text) < 12000
        and title.startswith(titles)
        or len(text) < 3000
        and lead.startswith(messages)
    )


@dataclass
class PageContent:
    text: str
    title: str
    links: list[str]
    issues: tuple[str, ...] = ()


def content_issues(
    text, title="", *, scripts=False, shell=False, embedded=False, noscript=""
):
    if challenge_text(text, title):
        return ("ACCESS_CHALLENGE",)
    lead = text.strip().casefold()
    javascript = (
        "please enable javascript",
        "you need to enable javascript",
        "javascript is required",
        "enable javascript to",
        "включите javascript",
        "для работы сайта необходимо включить javascript",
    )
    loading = bool(
        re.fullmatch(r"(?:loading|please wait|загрузка|подождите)[. …!]*", lead)
    )
    if lead.startswith(javascript) or (
        (not lead or loading or shell and len(text) < 400)
        and (
            scripts or shell or any(word in noscript.casefold() for word in javascript)
        )
    ):
        return ("JAVASCRIPT_REQUIRED",)
    if embedded and len(text) < 200:
        return ("EMBEDDED_CONTENT_UNREAD",)
    if not lead:
        return ("EMPTY_CONTENT",)
    if len(text) < 3000 and re.search(
        r"(?:subscribe to (?:continue reading|read (?:the full|this) (?:article|story))|"
        r"(?:sign|log) in to (?:continue reading|read (?:the full|this) (?:article|story))|"
        r"подпишитесь[^.\n]{0,60}(?:читать|прочитать|доступ)|"
        r"войдите[^.\n]{0,60}(?:читать|прочитать|продолжить))",
        lead,
    ):
        return ("ACCESS_RESTRICTED",)
    return ()


def parse_html(content, url, scope=None, *, discover_links=True, limit=1500):
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else url
    scripts = any(
        str(element.get("type", "")).lower().split(";", 1)[0]
        in {"", "text/javascript", "application/javascript", "module"}
        for element in soup.find_all("script")
    )
    shell = any(
        not element.get_text(strip=True)
        for element in soup.find_all(id={"app", "root", "__next", "__nuxt"})
    )
    embedded = bool(soup.find("iframe", src=True))
    noscript = " ".join(
        element.get_text(" ", strip=True) for element in soup.find_all("noscript")
    )
    scope = scope or CrawlScope.from_url(url)
    base = soup.find("base", href=True)
    base_url = urljoin(url, base["href"]) if base else url
    links = (
        filtered_links(
            (a["href"] for a in soup.find_all("a", href=True)), scope, base_url, limit
        )
        if discover_links
        else []
    )
    for element in soup.find_all(NOISE_TAGS):
        element.decompose()
    for table in reversed(soup.find_all("table")):
        rows = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if cells:
                rows.append(
                    " | ".join(
                        cell.get_text(" ", strip=True).replace("|", "\\|")
                        for cell in cells
                    )
                )
        table.replace_with("\n" + "\n".join(rows) + "\n")
    # Block boundaries become newlines; inline markup keeps the surrounding words.
    for element in soup.find_all(
        [
            "p",
            "div",
            "section",
            "article",
            "li",
            "pre",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "br",
            "hr",
        ]
    ):
        element.insert_before("\n")
        element.insert_after("\n")
    body = soup.body or soup
    text = "\n".join(
        line.strip() for line in body.get_text().splitlines() if line.strip()
    )
    issues = content_issues(
        text, title, scripts=scripts, shell=shell, embedded=embedded, noscript=noscript
    )
    if "ACCESS_CHALLENGE" in issues:
        text, links = "", []
    elif "JAVASCRIPT_REQUIRED" in issues and re.fullmatch(
        r"(?:loading|please wait|загрузка|подождите)[. …!]*", text.strip().casefold()
    ):
        text = ""
    return PageContent(text, title, links, issues)
